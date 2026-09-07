//! Provenance-first SDRF draft generation for PRIDE single-cell proteomics datasets.
//!
//! This crate intentionally separates LLM fact extraction from SDRF serialization.
//! Ollama proposes structured metadata with evidence references; deterministic Rust
//! code owns row construction, reserved values, validation, and provenance outputs.

use anyhow::{anyhow, bail, Context, Result};
use csv::{ReaderBuilder, WriterBuilder};
use regex::Regex;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

pub const SDRF_SPEC_VERSION: &str = "v1.1.0";
pub const SINGLE_CELL_TEMPLATE_VERSION: &str = "1.0.0";
pub const SINGLE_CELL_TEMPLATE_URL: &str =
    "https://github.com/bigbio/sdrf-templates/blob/main/single-cell/1.0.0/single-cell.yaml";
pub const SDRF_SPEC_URL: &str = "https://sdrf.quantms.org/specification.html";
pub const GENERATOR_VERSION: &str = "pride-scp-sdrf-v0.4.4";
const MANUSCRIPT_SCAN_MAX_CHARS: usize = 2_000_000;
const MANUSCRIPT_EVIDENCE_MAX_RESERVED_ITEMS: usize = 24;
const MANUSCRIPT_EVIDENCE_MAX_RESERVED_CHARS: usize = 12_000;
pub const SDRF_SOURCE_RESOLVER_VERSION: &str = "pride-scp-sdrf-source-resolver-v0.1";
pub const SDRF_AUDITOR_VERSION: &str = "pride-scp-sdrf-auditor-v0.2";
pub const DEFAULT_OLLAMA_URL: &str = "http://localhost:11434/api/generate";

// The linked single-cell template is work-in-progress. Generated drafts pin the
// column profile shown by the current rendered specification/GitHub view on
// 2026-09-06. The audit records this profile so drafts can be revalidated if the
// template changes before submission.
pub const SC_SAMPLE_TYPE: &str = "characteristics[sample type]";
pub const SC_ISOLATION_METHOD: &str = "characteristics[single cell isolation protocol]";
pub const SC_CELL_IDENTIFIER: &str = "characteristics[cell identifier]";
pub const SC_INDIVIDUAL: &str = "characteristics[individual]";
pub const SC_PREP_BATCH: &str = "comment[sample preparation batch]";
pub const SC_CELLS_PER_WELL: &str = "characteristics[cells per well]";
pub const SC_CARRIER_CHANNEL: &str = "comment[carrier channel]";
pub const SC_REFERENCE_CHANNEL: &str = "comment[reference channel]";

const RAW_EXTENSIONS: &[&str] = &[
    ".raw", ".d", ".wiff", ".wiff2", ".mzml", ".mzxml", ".tdf", ".tsf",
];

#[derive(Debug, Clone)]
pub struct SdrfResolveOptions {
    pub snapshot_dir: PathBuf,
    pub output_dir: PathBuf,
    pub accessions: Vec<String>,
    pub accessions_file: Option<PathBuf>,
    pub timeout_seconds: u64,
    pub force: bool,
    pub progress: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SdrfResolveSummary {
    pub resolver_version: String,
    pub accessions_requested: usize,
    pub curated_bigbio_usable: usize,
    pub repository_submitted_usable: usize,
    pub snapshot_usable: usize,
    pub unresolved: usize,
    pub selected_sources_tsv: String,
    pub resolved_dir: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct SdrfSourceCandidateAudit {
    source_kind: String,
    source_url: String,
    status: String,
    usable: bool,
    local_path: String,
    content_fingerprint: String,
    bytes: usize,
    note: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct SdrfSourceAudit {
    accession: String,
    resolver_version: String,
    selected_source_kind: String,
    selected_source_url: String,
    selected_local_path: String,
    selected_content_fingerprint: String,
    candidates: Vec<SdrfSourceCandidateAudit>,
}

#[derive(Debug, Clone)]
pub struct SdrfAuditOptions {
    pub snapshot_dir: PathBuf,
    pub resolved_sdrf_dir: PathBuf,
    pub output_dir: PathBuf,
    pub accessions: Vec<String>,
    pub accessions_file: Option<PathBuf>,
    pub progress: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SdrfAuditSummary {
    pub auditor_version: String,
    pub accessions_requested: usize,
    pub audited: usize,
    pub errors: usize,
    /// Local SDRF/template checks excluding repository-inventory linkage.
    pub locally_valid: usize,
    /// Accessions with complete exact/archive-normalized linkage to the local PRIDE RAW inventory.
    pub repository_linkage_complete: usize,
    /// Accessions with some but not all SDRF data files represented by the local PRIDE RAW inventory.
    pub repository_linkage_partial: usize,
    /// Accessions with no direct logical filename matches in the local PRIDE RAW inventory.
    pub repository_linkage_none: usize,
    /// Accessions for which the local PRIDE RAW inventory is empty/unavailable.
    pub repository_linkage_unavailable: usize,
    /// Structural/template review or an unresolved sample-to-file relationship is still needed.
    pub requires_review: usize,
    /// Any bounded metadata gap remains, including recommended/optional enrichment fields.
    pub metadata_enrichment_candidates: usize,
    pub priority_p0_structural_review: usize,
    pub priority_p1_blocking_gap: usize,
    pub priority_p2_recommended_gap: usize,
    pub priority_p3_optional_gap: usize,
    pub priority_p4_preserve: usize,
    pub curated_bigbio: usize,
    pub repository_submitted: usize,
    pub other_source: usize,
    pub results_tsv: String,
    pub unresolved_accessions_file: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct SdrfResolvedAudit {
    accession: String,
    auditor_version: String,
    source_kind: String,
    sdrf_path: String,
    row_count: usize,
    raw_file_count: usize,
    relation_mode: String,
    locally_valid: bool,
    validation_error_count: usize,
    validation_warning_count: usize,
    data_file_linkage: DataFileLinkageStats,
    missing_target_fields: Vec<String>,
    blocking_gap_fields: Vec<String>,
    recommended_gap_fields: Vec<String>,
    optional_gap_fields: Vec<String>,
    enrichment_priority: String,
    validation_issues: Vec<ValidationIssue>,
}

#[derive(Debug, Clone, Serialize)]
struct SdrfAuditResultRow {
    accession: String,
    status: String,
    source_kind: String,
    rows: usize,
    raw_files: usize,
    relation_mode: String,
    locally_valid: bool,
    validation_errors: usize,
    validation_warnings: usize,
    repository_linkage_status: String,
    sdrf_unique_data_files: usize,
    matched_unique_data_files: usize,
    unmatched_unique_data_files: usize,
    unmatched_data_file_rows: usize,
    missing_target_fields: usize,
    missing_target_field_names: String,
    blocking_gap_fields: usize,
    blocking_gap_field_names: String,
    recommended_gap_fields: usize,
    recommended_gap_field_names: String,
    optional_gap_fields: usize,
    optional_gap_field_names: String,
    enrichment_priority: String,
    sdrf_path: String,
    review_path: String,
    error: String,
}

#[derive(Debug, Clone)]
pub struct SdrfAnnotateOptions {
    pub snapshot_dir: PathBuf,
    pub annotations_dir: PathBuf,
    pub publication_manifest: Option<PathBuf>,
    pub manuscript_text_paths: Vec<PathBuf>,
    pub output_dir: PathBuf,
    pub accessions: Vec<String>,
    pub accessions_file: Option<PathBuf>,
    /// Optional directory populated by `sdrf-resolve`. A usable `{PXD}.sdrf.tsv`
    /// here takes precedence over the PRIDE snapshot SDRF cache.
    pub resolved_sdrf_dir: Option<PathBuf>,
    /// Optional source-grounded row-mapping manifest. When rows exist for an accession,
    /// deterministic serialization uses those explicit sample/run/channel relationships
    /// instead of the generic de-novo mapping scaffold.
    pub explicit_row_mapping_manifest: Option<PathBuf>,
    pub model: String,
    pub ollama_url: String,
    pub timeout_seconds: u64,
    pub max_evidence_items: usize,
    pub max_evidence_chars: usize,
    pub max_files_in_prompt: usize,
    pub force: bool,
    pub progress: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SdrfAnnotateSummary {
    pub generator_version: String,
    pub sdrf_spec_version: String,
    pub single_cell_template_version: String,
    pub template_profile: String,
    pub accessions_requested: usize,
    pub successful: usize,
    pub errors: usize,
    /// Usable existing SDRFs with at least one mapped data-file row.
    pub existing_sdrf_detected: usize,
    #[serde(default)]
    pub snapshot_sdrf_files_detected: usize,
    #[serde(default)]
    pub unusable_snapshot_sdrf_detected: usize,
    pub drafts_written: usize,
    pub locally_valid_drafts: usize,
    pub incomplete_drafts: usize,
    #[serde(default)]
    pub accessions_with_provenance_repairs: usize,
    #[serde(default)]
    pub provenance_repairs: usize,
    pub results_tsv: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct EvidenceItem {
    id: String,
    source_kind: String,
    source_label: String,
    text: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct RawFile {
    file_name: String,
    file_uri: String,
    category: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ExplicitRowMapping {
    accession: String,
    raw_file: String,
    source_name: String,
    cell_identifier: String,
    biological_replicate: String,
    technical_replicate: String,
    sample_type: String,
    cells_per_well: String,
    label: String,
    carrier_channel: String,
    reference_channel: String,
    design_source: String,
    design_ref: String,
    mapping_key: String,
    mapping_confidence: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct StudyDesignScaffold {
    relation_mode_hint: String,
    relation_confidence: String,
    #[serde(default)]
    relation_evidence_refs: Vec<String>,
    repository_file_mode: String,
    direct_acquisition_files: usize,
    wrapped_acquisition_files: usize,
    generic_archive_files: usize,
    #[serde(default)]
    generic_archive_file_names: Vec<String>,
    #[serde(default)]
    multiplex_chemistry_hint: String,
    #[serde(default)]
    multiplex_evidence_refs: Vec<String>,
    #[serde(default)]
    carrier_channel_hints: Vec<String>,
    #[serde(default)]
    reference_channel_hints: Vec<String>,
    #[serde(default)]
    multiplex_mapping_status: String,
    #[serde(default)]
    file_role_hint_counts: BTreeMap<String, usize>,
    notes: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct TemplateCompatibilityGap {
    field: String,
    observed_value: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    reason: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct DeterministicMetadataScaffold {
    #[serde(default)]
    values: BTreeMap<String, String>,
    #[serde(default)]
    evidence_refs: BTreeMap<String, Vec<String>>,
    #[serde(default)]
    template_gaps: Vec<TemplateCompatibilityGap>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct DatasetEvidence {
    accession: String,
    project_json_path: String,
    files_json_path: String,
    existing_sdrf_path: String,
    raw_files: Vec<RawFile>,
    #[serde(default)]
    study_design: StudyDesignScaffold,
    #[serde(default)]
    metadata_scaffold: DeterministicMetadataScaffold,
    evidence: Vec<EvidenceItem>,
    manuscript_sources: Vec<String>,
    annotation_sources: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct FactorProposal {
    name: String,
    value: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct SdrfProposal {
    relation_mode: String,
    organism: String,
    organism_part: String,
    disease: String,
    cell_type: String,
    sample_type: String,
    single_cell_isolation_method: String,
    individual: String,
    sample_preparation_batch: String,
    cells_per_well: String,
    proteomics_data_acquisition_method: String,
    label: String,
    instrument: String,
    cleavage_agent_details: String,
    fraction_identifier: String,
    technical_replicate: String,
    carrier_channel: String,
    reference_channel: String,
    #[serde(default)]
    factors: Vec<FactorProposal>,
    #[serde(default)]
    evidence_refs: BTreeMap<String, Vec<String>>,
    confidence: String,
    notes: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ValidationIssue {
    level: String,
    code: String,
    row: usize,
    column: String,
    message: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DataFileValidationPolicy {
    /// Generated SDRFs are owned by this tool, so every data-file value must map
    /// back to the PRIDE snapshot inventory.
    EnforceSnapshotInventory,
    /// Resolved external SDRFs may legitimately describe logical vendor entities
    /// (for example Bruker .d folders) that a repository exposes as archives or
    /// otherwise does not enumerate one-to-one. Keep linkage mismatches visible
    /// but do not conflate them with SDRF/template validity.
    AuditSnapshotInventory,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct DataFileLinkageStats {
    sdrf_unique_data_files: usize,
    snapshot_raw_files: usize,
    matched_unique_data_files: usize,
    unmatched_unique_data_files: usize,
    matched_rows: usize,
    unmatched_rows: usize,
    status: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct DatasetAudit {
    accession: String,
    generator_version: String,
    sdrf_spec_version: String,
    single_cell_template_version: String,
    template_profile: String,
    template_url: String,
    specification_url: String,
    existing_sdrf_present: bool,
    generation_mode: String,
    relation_mode: String,
    #[serde(default)]
    study_design: StudyDesignScaffold,
    #[serde(default)]
    metadata_scaffold: DeterministicMetadataScaffold,
    raw_file_count: usize,
    evidence_item_count: usize,
    manuscript_source_count: usize,
    annotation_source_count: usize,
    ollama_model: String,
    ollama_used: bool,
    draft_path: String,
    proposal_path: String,
    evidence_path: String,
    review_path: String,
    validation_issue_count: usize,
    validation_error_count: usize,
    #[serde(default)]
    proposal_repair_count: usize,
    #[serde(default)]
    explicit_row_mapping_manifest: String,
    #[serde(default)]
    explicit_row_mapping_rows: usize,
    locally_valid: bool,
    completeness_status: String,
    template_drift_note: String,
    validation_issues: Vec<ValidationIssue>,
}

#[derive(Debug, Clone, Serialize)]
struct ResultRow {
    accession: String,
    status: String,
    generation_mode: String,
    raw_files: usize,
    evidence_items: usize,
    relation_mode: String,
    locally_valid: bool,
    completeness_status: String,
    validation_errors: usize,
    proposal_repairs: usize,
    draft_path: String,
    review_path: String,
    error: String,
}

fn norm_accession(value: &str) -> Option<String> {
    let acc = value.trim().to_ascii_uppercase();
    let re = Regex::new(r"^PXD\d{6}$").unwrap();
    re.is_match(&acc).then_some(acc)
}

fn collect_accessions_values(
    accessions: &[String],
    accessions_file: Option<&Path>,
) -> Result<Vec<String>> {
    let mut out = BTreeSet::new();
    for raw in accessions {
        let acc = norm_accession(raw).ok_or_else(|| anyhow!("invalid PRIDE accession: {raw}"))?;
        out.insert(acc);
    }
    if let Some(path) = accessions_file {
        let text = fs::read_to_string(path)
            .with_context(|| format!("read accession file {}", path.display()))?;
        for line in text.lines() {
            let trimmed = line.trim();
            if trimmed.is_empty() || trimmed.starts_with('#') {
                continue;
            }
            let token = trimmed
                .split(|c: char| c == ',' || c == '\t' || c == ' ')
                .next()
                .unwrap_or("");
            if let Some(acc) = norm_accession(token) {
                out.insert(acc);
            }
        }
    }
    if out.is_empty() {
        bail!("no accessions supplied; use --accession and/or --accessions-file");
    }
    Ok(out.into_iter().collect())
}

fn collect_accessions(opts: &SdrfAnnotateOptions) -> Result<Vec<String>> {
    collect_accessions_values(&opts.accessions, opts.accessions_file.as_deref())
}

fn json_string_leaves(value: &Value, prefix: &str, out: &mut Vec<(String, String)>) {
    match value {
        Value::String(s) => {
            let t = s.split_whitespace().collect::<Vec<_>>().join(" ");
            if !t.is_empty() {
                out.push((prefix.to_string(), t));
            }
        }
        Value::Array(items) => {
            for (i, item) in items.iter().enumerate() {
                let path = format!("{prefix}[{i}]");
                json_string_leaves(item, &path, out);
            }
        }
        Value::Object(map) => {
            for (key, item) in map {
                let path = if prefix.is_empty() {
                    key.clone()
                } else {
                    format!("{prefix}.{key}")
                };
                json_string_leaves(item, &path, out);
            }
        }
        _ => {}
    }
}

fn push_evidence(
    items: &mut Vec<EvidenceItem>,
    source_kind: &str,
    source_label: impl Into<String>,
    text: impl Into<String>,
    max_items: usize,
    max_total_chars: usize,
) {
    if items.len() >= max_items {
        return;
    }
    let text = text.into().split_whitespace().collect::<Vec<_>>().join(" ");
    if text.len() < 2 {
        return;
    }
    let used: usize = items.iter().map(|x| x.text.len()).sum();
    if used >= max_total_chars {
        return;
    }
    let remain = max_total_chars - used;
    let clipped: String = text.chars().take(remain.min(1200)).collect();
    if clipped.is_empty() {
        return;
    }
    let id = format!("E{:04}", items.len() + 1);
    items.push(EvidenceItem {
        id,
        source_kind: source_kind.to_string(),
        source_label: source_label.into(),
        text: clipped,
    });
}

fn load_json(path: &Path) -> Result<Value> {
    let text = fs::read_to_string(path).with_context(|| format!("read {}", path.display()))?;
    serde_json::from_str(&text).with_context(|| format!("parse JSON {}", path.display()))
}

fn object_file_name(map: &Map<String, Value>) -> String {
    for key in ["fileName", "filename", "name"] {
        if let Some(Value::String(v)) = map.get(key) {
            if !v.trim().is_empty() {
                return v.trim().to_string();
            }
        }
    }
    String::new()
}

fn file_category(map: &Map<String, Value>) -> String {
    for key in ["fileCategory", "category"] {
        if let Some(value) = map.get(key) {
            match value {
                Value::String(v) => return v.trim().to_string(),
                Value::Object(obj) => {
                    for inner in ["value", "name"] {
                        if let Some(Value::String(v)) = obj.get(inner) {
                            return v.trim().to_string();
                        }
                    }
                }
                _ => {}
            }
        }
    }
    String::new()
}

fn file_uri(map: &Map<String, Value>) -> String {
    if let Some(Value::Array(locations)) = map.get("publicFileLocations") {
        let mut fallback = String::new();
        for loc in locations {
            let Value::Object(obj) = loc else { continue };
            let value = obj
                .get("value")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            if value.is_empty() {
                continue;
            }
            let name = obj
                .get("name")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_ascii_lowercase();
            if name.contains("ftp") || name.contains("http") {
                return value.to_string();
            }
            if fallback.is_empty() {
                fallback = value.to_string();
            }
        }
        if !fallback.is_empty() {
            return fallback;
        }
    }
    for key in ["downloadLink", "downloadUrl", "url"] {
        if let Some(Value::String(v)) = map.get(key) {
            if !v.trim().is_empty() {
                return v.trim().to_string();
            }
        }
    }
    String::new()
}

fn looks_raw(name: &str, category: &str) -> bool {
    if category.eq_ignore_ascii_case("RAW") {
        return true;
    }
    let lower = name.to_ascii_lowercase();
    RAW_EXTENSIONS.iter().any(|ext| lower.ends_with(ext))
}

fn extract_raw_files(value: &Value) -> Vec<RawFile> {
    fn walk(value: &Value, out: &mut BTreeMap<String, RawFile>) {
        match value {
            Value::Object(map) => {
                let name = object_file_name(map);
                let category = file_category(map);
                if !name.is_empty() && looks_raw(&name, &category) {
                    out.entry(name.clone()).or_insert_with(|| RawFile {
                        file_name: name,
                        file_uri: file_uri(map),
                        category,
                    });
                }
                for v in map.values() {
                    walk(v, out);
                }
            }
            Value::Array(items) => {
                for v in items {
                    walk(v, out);
                }
            }
            _ => {}
        }
    }
    let mut map = BTreeMap::new();
    walk(value, &mut map);
    map.into_values().collect()
}

fn relevant_sdrf_metadata_path(path: &str) -> bool {
    let p = path.to_ascii_lowercase();
    // Repository transport/file-record fields are provenance, never biological values.
    if [
        "publicfilelocations",
        "filecategory",
        "downloadlink",
        "downloadurl",
        "repository",
        "submission",
        "submitter",
        "filelist",
        "files.",
        "file.",
    ]
    .iter()
    .any(|noise| p.contains(noise))
    {
        return false;
    }
    [
        "title",
        "description",
        "organism",
        "species",
        "tissue",
        "organism part",
        "disease",
        "cell type",
        "celltype",
        "sample type",
        "sampletype",
        "instrument",
        "acquisition",
        "quant",
        "label",
        "tmt",
        "itraq",
        "dia",
        "dda",
        "cleavage",
        "trypsin",
        "isolation",
        "facs",
        "cellenone",
        "nanopots",
        "batch",
        "replicate",
        "carrier",
        "reference",
        "channel",
        "protocol",
        "single cell",
        "single_cell",
        "individual",
        "cells per well",
        "cells_per_well",
    ]
    .iter()
    .any(|term| p.contains(term))
}

fn add_json_evidence(
    items: &mut Vec<EvidenceItem>,
    kind: &str,
    prefix: &str,
    value: &Value,
    max_items: usize,
    max_chars: usize,
) {
    let mut leaves = Vec::new();
    json_string_leaves(value, "", &mut leaves);
    for (path, text) in leaves {
        if relevant_sdrf_metadata_path(&path) {
            push_evidence(
                items,
                kind,
                format!("{prefix}:{path}"),
                text,
                max_items,
                max_chars,
            );
        }
    }
}

fn normalize_header(value: &str) -> String {
    value.trim().to_ascii_lowercase()
}

fn read_existing_sdrf_table(path: &Path) -> Result<(Vec<String>, Vec<Vec<String>>)> {
    let mut reader = ReaderBuilder::new()
        .delimiter(b'\t')
        .flexible(true)
        .from_path(path)
        .with_context(|| format!("read existing SDRF {}", path.display()))?;
    let headers = reader
        .headers()?
        .iter()
        .map(normalize_header)
        .collect::<Vec<_>>();
    let mut rows = Vec::new();
    for rec in reader.records() {
        let rec = rec?;
        let mut row = rec.iter().map(|x| x.trim().to_string()).collect::<Vec<_>>();
        row.resize(headers.len(), String::new());
        rows.push(row);
    }
    Ok((headers, rows))
}

fn header_first_index(headers: &[String], name: &str) -> Option<usize> {
    headers.iter().position(|h| h == name)
}

/// Classify a cached SDRF response by whether it contains a usable sample-to-data mapping.
///
/// The PRIDE `/files/sdrf/{PXD}` endpoint historically returns a non-empty response for
/// many accessions that do not actually have SDRF data rows. A cache file therefore must
/// never be considered an existing SDRF based on file existence/size alone.
fn existing_sdrf_status(path: &Path) -> String {
    if !path.is_file() {
        return "missing".into();
    }
    let Ok((headers, rows)) = read_existing_sdrf_table(path) else {
        return "unreadable".into();
    };
    if rows.is_empty() {
        return "header_only".into();
    }
    let Some(data_idx) = header_first_index(&headers, "comment[data file]") else {
        return "missing_data_file_column".into();
    };
    let has_mapped_data_row = rows.iter().any(|row| {
        let Some(value) = row.get(data_idx) else {
            return false;
        };
        let value = value.trim();
        !value.is_empty()
            && !value.eq_ignore_ascii_case("not available")
            && !value.eq_ignore_ascii_case("not applicable")
    });
    if !has_mapped_data_row {
        return "no_mapped_data_rows".into();
    }
    "usable".into()
}

fn existing_sdrf_is_usable(path: &Path) -> bool {
    existing_sdrf_status(path) == "usable"
}

fn fnv1a64_hex(bytes: &[u8]) -> String {
    let mut hash: u64 = 0xcbf29ce484222325;
    for &b in bytes {
        hash ^= b as u64;
        hash = hash.wrapping_mul(0x100000001b3);
    }
    format!("fnv1a64:{hash:016x}")
}

fn extract_sdrf_file_candidates(value: &Value) -> Vec<(String, String)> {
    fn walk(value: &Value, out: &mut BTreeMap<String, String>) {
        match value {
            Value::Object(map) => {
                let name = object_file_name(map);
                let lower = name.to_ascii_lowercase();
                if !name.is_empty() && lower.contains("sdrf") && lower.ends_with(".tsv") {
                    out.entry(name).or_insert_with(|| file_uri(map));
                }
                for child in map.values() {
                    walk(child, out);
                }
            }
            Value::Array(items) => {
                for child in items {
                    walk(child, out);
                }
            }
            _ => {}
        }
    }
    let mut out = BTreeMap::new();
    walk(value, &mut out);
    out.into_iter().collect()
}

fn normalize_download_url(value: &str) -> Option<String> {
    let value = value.trim();
    if value.starts_with("https://") || value.starts_with("http://") {
        return Some(value.to_string());
    }
    if let Some(rest) = value.strip_prefix("ftp://") {
        // PRIDE's public FTP host is also HTTPS-accessible. For other hosts this
        // is attempted best-effort and retained in the source audit if it fails.
        return Some(format!("https://{rest}"));
    }
    None
}

async fn fetch_sdrf_candidate(
    client: &Client,
    source_kind: &str,
    source_url: &str,
    local_path: &Path,
    force: bool,
) -> SdrfSourceCandidateAudit {
    if local_path.is_file() && !force {
        let status = existing_sdrf_status(local_path);
        let bytes = fs::read(local_path).unwrap_or_default();
        return SdrfSourceCandidateAudit {
            source_kind: source_kind.into(),
            source_url: source_url.into(),
            status: format!("cached_{status}"),
            usable: status == "usable",
            local_path: local_path.display().to_string(),
            content_fingerprint: fnv1a64_hex(&bytes),
            bytes: bytes.len(),
            note: "reused cached source candidate".into(),
        };
    }
    let Some(url) = normalize_download_url(source_url) else {
        return SdrfSourceCandidateAudit {
            source_kind: source_kind.into(),
            source_url: source_url.into(),
            status: "unsupported_url_scheme".into(),
            usable: false,
            local_path: String::new(),
            content_fingerprint: String::new(),
            bytes: 0,
            note: "source URL is not HTTP(S) or convertible FTP".into(),
        };
    };
    let response = match client.get(&url).send().await {
        Ok(r) => r,
        Err(err) => {
            return SdrfSourceCandidateAudit {
                source_kind: source_kind.into(),
                source_url: url,
                status: "fetch_error".into(),
                usable: false,
                local_path: String::new(),
                content_fingerprint: String::new(),
                bytes: 0,
                note: err.to_string(),
            };
        }
    };
    let code = response.status().as_u16();
    if code == 404 {
        return SdrfSourceCandidateAudit {
            source_kind: source_kind.into(),
            source_url: url,
            status: "not_found".into(),
            usable: false,
            local_path: String::new(),
            content_fingerprint: String::new(),
            bytes: 0,
            note: "HTTP 404".into(),
        };
    }
    if !response.status().is_success() {
        return SdrfSourceCandidateAudit {
            source_kind: source_kind.into(),
            source_url: url,
            status: format!("http_{code}"),
            usable: false,
            local_path: String::new(),
            content_fingerprint: String::new(),
            bytes: 0,
            note: "non-success HTTP response".into(),
        };
    }
    let bytes = match response.bytes().await {
        Ok(v) => v.to_vec(),
        Err(err) => {
            return SdrfSourceCandidateAudit {
                source_kind: source_kind.into(),
                source_url: url,
                status: "body_read_error".into(),
                usable: false,
                local_path: String::new(),
                content_fingerprint: String::new(),
                bytes: 0,
                note: err.to_string(),
            };
        }
    };
    if let Some(parent) = local_path.parent() {
        if let Err(err) = fs::create_dir_all(parent) {
            return SdrfSourceCandidateAudit {
                source_kind: source_kind.into(),
                source_url: url,
                status: "cache_write_error".into(),
                usable: false,
                local_path: String::new(),
                content_fingerprint: fnv1a64_hex(&bytes),
                bytes: bytes.len(),
                note: err.to_string(),
            };
        }
    }
    if let Err(err) = fs::write(local_path, &bytes) {
        return SdrfSourceCandidateAudit {
            source_kind: source_kind.into(),
            source_url: url,
            status: "cache_write_error".into(),
            usable: false,
            local_path: String::new(),
            content_fingerprint: fnv1a64_hex(&bytes),
            bytes: bytes.len(),
            note: err.to_string(),
        };
    }
    let parsed_status = existing_sdrf_status(local_path);
    SdrfSourceCandidateAudit {
        source_kind: source_kind.into(),
        source_url: url,
        status: parsed_status.clone(),
        usable: parsed_status == "usable",
        local_path: local_path.display().to_string(),
        content_fingerprint: fnv1a64_hex(&bytes),
        bytes: bytes.len(),
        note: "downloaded and parsed as SDRF candidate".into(),
    }
}

fn select_sdrf_source_candidate(
    candidates: &[SdrfSourceCandidateAudit],
) -> Option<&SdrfSourceCandidateAudit> {
    [
        "curated_bigbio",
        "repository_submitted",
        "snapshot_pride_sdrf_api",
    ]
    .iter()
    .find_map(|kind| {
        candidates
            .iter()
            .find(|c| c.usable && c.source_kind.as_str() == *kind)
    })
}

#[derive(Debug, Clone, Serialize)]
struct SdrfSourceResultRow {
    accession: String,
    selected_source_kind: String,
    selected_source_url: String,
    selected_content_fingerprint: String,
    resolved_path: String,
    curated_bigbio_status: String,
    repository_candidate_count: usize,
    repository_usable_count: usize,
    snapshot_status: String,
    unresolved: bool,
}

async fn resolve_sdrf_one(
    opts: &SdrfResolveOptions,
    client: &Client,
    accession: &str,
) -> Result<SdrfSourceResultRow> {
    let cache_dir = opts.output_dir.join("cache").join(accession);
    let audit_dir = opts.output_dir.join("audit");
    let resolved_dir = opts.output_dir.join("resolved");
    fs::create_dir_all(&cache_dir)?;
    fs::create_dir_all(&audit_dir)?;
    fs::create_dir_all(&resolved_dir)?;

    let mut candidates = Vec::new();
    let curated_url = format!(
        "https://raw.githubusercontent.com/bigbio/sdrf-annotated-datasets/master/datasets/{0}/{0}.sdrf.tsv",
        accession
    );
    let curated_path = cache_dir.join("curated_bigbio.sdrf.tsv");
    let curated = fetch_sdrf_candidate(
        client,
        "curated_bigbio",
        &curated_url,
        &curated_path,
        opts.force,
    )
    .await;
    let curated_status = curated.status.clone();
    candidates.push(curated);

    let files_path = opts
        .snapshot_dir
        .join("files")
        .join(format!("{accession}.json"));
    let mut repository_candidate_count = 0usize;
    if files_path.is_file() {
        if let Ok(files) = load_json(&files_path) {
            for (idx, (name, uri)) in extract_sdrf_file_candidates(&files).into_iter().enumerate() {
                repository_candidate_count += 1;
                if uri.trim().is_empty() {
                    candidates.push(SdrfSourceCandidateAudit {
                        source_kind: "repository_submitted".into(),
                        source_url: String::new(),
                        status: "missing_public_url".into(),
                        usable: false,
                        local_path: String::new(),
                        content_fingerprint: String::new(),
                        bytes: 0,
                        note: format!("repository file manifest contains {name} but no public URL"),
                    });
                    continue;
                }
                let path = cache_dir.join(format!("repository_{:02}.sdrf.tsv", idx + 1));
                candidates.push(
                    fetch_sdrf_candidate(client, "repository_submitted", &uri, &path, opts.force)
                        .await,
                );
            }
        }
    }

    let snapshot_path = opts
        .snapshot_dir
        .join("sdrf")
        .join(format!("{accession}.sdrf.tsv"));
    let snapshot_status = existing_sdrf_status(&snapshot_path);
    if snapshot_path.is_file() {
        let bytes = fs::read(&snapshot_path).unwrap_or_default();
        candidates.push(SdrfSourceCandidateAudit {
            source_kind: "snapshot_pride_sdrf_api".into(),
            source_url: String::new(),
            status: snapshot_status.clone(),
            usable: snapshot_status == "usable",
            local_path: snapshot_path.display().to_string(),
            content_fingerprint: fnv1a64_hex(&bytes),
            bytes: bytes.len(),
            note: "local PRIDE SDRF API cache; only usable when mapped data rows are present"
                .into(),
        });
    }

    // Trust precedence: community-curated > original repository submission >
    // local snapshot API cache. Agentic sources are deliberately not selected
    // automatically in v0.1 of the resolver.
    let selected = select_sdrf_source_candidate(&candidates);

    let resolved_path = resolved_dir.join(format!("{accession}.sdrf.tsv"));
    let (selected_source_kind, selected_source_url, selected_fingerprint, resolved_path_text) =
        if let Some(candidate) = selected {
            fs::copy(Path::new(&candidate.local_path), &resolved_path).with_context(|| {
                format!(
                    "copy selected SDRF source {} -> {}",
                    candidate.local_path,
                    resolved_path.display()
                )
            })?;
            (
                candidate.source_kind.clone(),
                candidate.source_url.clone(),
                candidate.content_fingerprint.clone(),
                resolved_path.display().to_string(),
            )
        } else {
            let _ = fs::remove_file(&resolved_path);
            (String::new(), String::new(), String::new(), String::new())
        };

    let repository_usable_count = candidates
        .iter()
        .filter(|c| c.source_kind == "repository_submitted" && c.usable)
        .count();
    let audit = SdrfSourceAudit {
        accession: accession.into(),
        resolver_version: SDRF_SOURCE_RESOLVER_VERSION.into(),
        selected_source_kind: selected_source_kind.clone(),
        selected_source_url: selected_source_url.clone(),
        selected_local_path: resolved_path_text.clone(),
        selected_content_fingerprint: selected_fingerprint.clone(),
        candidates,
    };
    fs::write(
        audit_dir.join(format!("{accession}.sdrf_source_audit.json")),
        serde_json::to_string_pretty(&audit)?,
    )?;

    Ok(SdrfSourceResultRow {
        accession: accession.into(),
        selected_source_kind,
        selected_source_url,
        selected_content_fingerprint: selected_fingerprint,
        resolved_path: resolved_path_text,
        curated_bigbio_status: curated_status,
        repository_candidate_count,
        repository_usable_count,
        snapshot_status,
        unresolved: audit.selected_source_kind.is_empty(),
    })
}

pub async fn resolve_sdrf_sources(opts: SdrfResolveOptions) -> Result<SdrfResolveSummary> {
    let accessions = collect_accessions_values(&opts.accessions, opts.accessions_file.as_deref())?;
    fs::create_dir_all(&opts.output_dir)?;
    let client = Client::builder()
        .timeout(Duration::from_secs(opts.timeout_seconds))
        .user_agent("PRIDE-SCP-SDRF-source-resolver/0.1")
        .build()?;
    let mut rows = Vec::new();
    for (i, accession) in accessions.iter().enumerate() {
        if opts.progress {
            eprintln!("[{}/{}] {}", i + 1, accessions.len(), accession);
        }
        match resolve_sdrf_one(&opts, &client, accession).await {
            Ok(row) => {
                if opts.progress {
                    if row.unresolved {
                        eprintln!(
                            "  -> unresolved (curated={} repository_usable={} snapshot={})",
                            row.curated_bigbio_status,
                            row.repository_usable_count,
                            row.snapshot_status
                        );
                    } else {
                        eprintln!("  -> {} {}", row.selected_source_kind, row.resolved_path);
                    }
                }
                rows.push(row);
            }
            Err(err) => {
                eprintln!("  -> resolver error: {err:#}");
                rows.push(SdrfSourceResultRow {
                    accession: accession.clone(),
                    selected_source_kind: String::new(),
                    selected_source_url: String::new(),
                    selected_content_fingerprint: String::new(),
                    resolved_path: String::new(),
                    curated_bigbio_status: "resolver_error".into(),
                    repository_candidate_count: 0,
                    repository_usable_count: 0,
                    snapshot_status: "unknown".into(),
                    unresolved: true,
                });
            }
        }
    }
    let selected_sources_path = opts.output_dir.join("sdrf_source_resolution.tsv");
    let mut writer = WriterBuilder::new()
        .delimiter(b'\t')
        .from_path(&selected_sources_path)?;
    for row in &rows {
        writer.serialize(row)?;
    }
    writer.flush()?;
    let summary = SdrfResolveSummary {
        resolver_version: SDRF_SOURCE_RESOLVER_VERSION.into(),
        accessions_requested: accessions.len(),
        curated_bigbio_usable: rows
            .iter()
            .filter(|r| r.selected_source_kind == "curated_bigbio")
            .count(),
        repository_submitted_usable: rows
            .iter()
            .filter(|r| r.selected_source_kind == "repository_submitted")
            .count(),
        snapshot_usable: rows
            .iter()
            .filter(|r| r.selected_source_kind == "snapshot_pride_sdrf_api")
            .count(),
        unresolved: rows.iter().filter(|r| r.unresolved).count(),
        selected_sources_tsv: selected_sources_path.display().to_string(),
        resolved_dir: opts.output_dir.join("resolved").display().to_string(),
    };
    fs::write(
        opts.output_dir.join("sdrf_source_resolution_summary.json"),
        serde_json::to_string_pretty(&summary)?,
    )?;
    Ok(summary)
}

fn existing_sdrf_relation_hint(headers: &[String], rows: &[Vec<String>]) -> String {
    let Some(data_idx) = header_first_index(headers, "comment[data file]") else {
        return "uncertain".into();
    };
    let cell_idx = header_first_index(headers, SC_CELL_IDENTIFIER);
    let sample_type_idx = header_first_index(headers, SC_SAMPLE_TYPE);
    let label_idx = header_first_index(headers, "comment[label]");

    // When the SDRF declares sample roles, infer the relationship from the
    // single-cell study rows only. Pooled controls, carrier/reference material,
    // secretome controls, etc. may legitimately share one raw file or use
    // multiple labels and must not make a label-free single-cell study look
    // multiplexed.
    let single_cell_rows: Vec<&Vec<String>> = sample_type_idx
        .map(|j| {
            rows.iter()
                .filter(|r| j < r.len() && r[j].trim().eq_ignore_ascii_case("single cell"))
                .collect()
        })
        .unwrap_or_default();
    let scoped_rows: Vec<&Vec<String>> = if single_cell_rows.is_empty() {
        rows.iter().collect()
    } else {
        single_cell_rows
    };

    let mut per_file_rows: BTreeMap<String, usize> = BTreeMap::new();
    let mut per_file_cells: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    let mut per_file_labels: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for row in scoped_rows {
        if data_idx >= row.len() {
            continue;
        }
        let file = row[data_idx].trim();
        if file.is_empty() || file.eq_ignore_ascii_case("not available") {
            continue;
        }
        *per_file_rows.entry(file.to_string()).or_default() += 1;
        if let Some(j) = cell_idx {
            if j < row.len() {
                let v = row[j].trim();
                let low = v.to_ascii_lowercase();
                if !v.is_empty()
                    && ![
                        "not available",
                        "not applicable",
                        "carrier",
                        "reference",
                        "empty",
                    ]
                    .contains(&low.as_str())
                {
                    per_file_cells
                        .entry(file.to_string())
                        .or_default()
                        .insert(v.to_string());
                }
            }
        }
        if let Some(j) = label_idx {
            if j < row.len() {
                let v = row[j].trim();
                if !v.is_empty() && !v.eq_ignore_ascii_case("not available") {
                    per_file_labels
                        .entry(file.to_string())
                        .or_default()
                        .insert(v.to_string());
                }
            }
        }
    }
    if per_file_rows.is_empty() {
        return "uncertain".into();
    }
    let mut multiplexed = BTreeSet::new();
    for (file, cells) in &per_file_cells {
        if cells.len() > 1 {
            multiplexed.insert(file.clone());
        }
    }
    for (file, n) in &per_file_rows {
        if *n > 1
            && per_file_labels
                .get(file)
                .map(|x| x.len() > 1)
                .unwrap_or(false)
        {
            multiplexed.insert(file.clone());
        }
    }
    let multiplexed_files = multiplexed.len();
    let single_files = per_file_rows.len().saturating_sub(multiplexed_files);
    if multiplexed_files > 0 && single_files > 0 {
        return "mixed".into();
    }
    if multiplexed_files > 0 {
        return "multiplexed_cells_per_data_file".into();
    }
    if per_file_rows.values().all(|&n| n == 1) {
        return "one_cell_per_data_file".into();
    }
    if !per_file_cells.is_empty() && per_file_cells.values().all(|x| x.len() == 1) {
        return "one_cell_per_data_file".into();
    }
    "uncertain".into()
}

fn add_existing_sdrf_evidence(
    items: &mut Vec<EvidenceItem>,
    path: &Path,
    max_items: usize,
    max_chars: usize,
) -> Result<()> {
    let (headers, rows) = read_existing_sdrf_table(path)?;
    let relation = existing_sdrf_relation_hint(&headers, &rows);
    let data_files = header_first_index(&headers, "comment[data file]")
        .map(|j| {
            rows.iter()
                .filter_map(|r| r.get(j))
                .filter(|v| !v.trim().is_empty())
                .collect::<BTreeSet<_>>()
                .len()
        })
        .unwrap_or(0);
    push_evidence(
        items,
        "existing_sdrf_structured",
        "existing_sdrf:relationship_summary",
        format!(
            "existing SDRF rows={}; unique data files={}; deterministic relation hint={relation}",
            rows.len(),
            data_files
        ),
        max_items,
        max_chars,
    );
    let wanted = [
        "source name",
        "characteristics[organism]",
        "characteristics[organism part]",
        "characteristics[disease]",
        "characteristics[cell type]",
        "characteristics[biological replicate]",
        "assay name",
        "comment[proteomics data acquisition method]",
        "comment[label]",
        "comment[instrument]",
        "comment[cleavage agent details]",
        "comment[fraction identifier]",
        "comment[technical replicate]",
        SC_SAMPLE_TYPE,
        SC_ISOLATION_METHOD,
        SC_CELL_IDENTIFIER,
        SC_PREP_BATCH,
        SC_CELLS_PER_WELL,
        SC_CARRIER_CHANNEL,
        SC_REFERENCE_CHANNEL,
    ];
    for name in wanted {
        let Some(j) = header_first_index(&headers, name) else {
            continue;
        };
        let mut vals = BTreeSet::new();
        for row in &rows {
            let Some(v) = row.get(j) else {
                continue;
            };
            let v = v.trim();
            if v.is_empty() {
                continue;
            }
            vals.insert(v.to_string());
            if vals.len() >= 12 {
                break;
            }
        }
        if vals.is_empty() {
            continue;
        }
        push_evidence(
            items,
            "existing_sdrf_structured",
            format!("existing_sdrf:{name}"),
            format!(
                "{name} values: {}",
                vals.into_iter().collect::<Vec<_>>().join(" | ")
            ),
            max_items,
            max_chars,
        );
    }
    Ok(())
}

fn read_text_evidence(path: &Path, max_chars: usize) -> Result<String> {
    let ext = path
        .extension()
        .and_then(|x| x.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    if ext == "pdf" {
        bail!("PDF requires upstream text extraction; direct PDF parsing is intentionally not duplicated here");
    }
    let bytes =
        fs::read(path).with_context(|| format!("read manuscript text {}", path.display()))?;
    let text = String::from_utf8_lossy(&bytes);
    Ok(text.chars().take(max_chars).collect())
}

fn read_manuscript_for_keyword_scan(path: &Path) -> Result<String> {
    // Source scanning and prompt/evidence budgets are intentionally separate.
    // The evidence budget limits what reaches Ollama, but must not prevent late
    // Methods sections from being searched for deterministic metadata. A 2M-char
    // cap remains bounded while covering normal extracted journal full text.
    read_text_evidence(path, MANUSCRIPT_SCAN_MAX_CHARS)
}

fn manuscript_keyword_windows(text: &str, max_windows: usize) -> Vec<String> {
    fn char_boundary_at_or_after(text: &str, mut idx: usize, ceiling: usize) -> usize {
        idx = idx.min(ceiling).min(text.len());
        while idx < ceiling.min(text.len()) && !text.is_char_boundary(idx) {
            idx += 1;
        }
        idx
    }

    fn char_boundary_at_or_before(text: &str, mut idx: usize, floor: usize) -> usize {
        idx = idx.min(text.len());
        while idx > floor && !text.is_char_boundary(idx) {
            idx -= 1;
        }
        idx
    }

    fn append_match_contexts(text: &str, re: &Regex, limit: usize, out: &mut Vec<String>) {
        let mut added = 0usize;
        let mut last_selected_start: Option<usize> = None;
        for m in re.find_iter(text) {
            // PDF text extraction frequently produces one giant single-newline block.
            // Center evidence on the actual regex match instead of clipping the start
            // of an arbitrarily large "paragraph" and potentially losing the method.
            if last_selected_start
                .map(|prev| m.start().saturating_sub(prev) < 700)
                .unwrap_or(false)
            {
                continue;
            }
            let rough_start = m.start().saturating_sub(650);
            let rough_end = (m.end() + 1_150).min(text.len());
            let start = char_boundary_at_or_after(text, rough_start, m.start());
            let end = char_boundary_at_or_before(text, rough_end, m.end());
            if start >= end {
                continue;
            }
            let clipped: String = text[start..end]
                .split_whitespace()
                .collect::<Vec<_>>()
                .join(" ")
                .chars()
                .take(1800)
                .collect();
            if !clipped.is_empty() && !out.contains(&clipped) {
                out.push(clipped);
                added += 1;
                last_selected_start = Some(m.start());
            }
            if added >= limit {
                break;
            }
        }
    }

    // Prioritize biological sample/isolation evidence. Match-centered contexts are
    // intentionally used instead of blank-line paragraphs because normalized PDF
    // extraction often preserves only single line breaks.
    let isolation = Regex::new(
        r"(?i)cellenone|facs|flow cytometr|sort(?:ed|ing)?|manual(?:ly)? (?:pick|dissect|isolat)|mechanic(?:al|ally) (?:dissociat|isolat)|dissect(?:ed|ion|ing)?|tweezer|individual(?:ly)? (?:transferred|isolated|dissected)|individual fibers? (?:were )?(?:taken|transferred|isolated)|single muscle fib(?:er|re)|single oocyte|single blastomere|single neuron|microaspirat|patch[- ]clamp|micropipette|capillary microsampling|laser capture|microdissect|microwell|384[- ]well|96[- ]well|individual wells?|single cells? were (?:placed|deposited|transferred|sorted)|isolated single fibers?|skinned fibers?",
    )
    .unwrap();
    let sample_design = Regex::new(
        r"(?i)single[- ]cell|single[- ]nucle|few[- ]cell|\b\d{1,4} cells?\b|bulk|blank|quality control|carrier|reference channel|tmt(?:pro)?|itraq|plexdia|label[- ]free",
    )
    .unwrap();
    let acquisition = Regex::new(
        r"(?i)dia[- ]pasef|data[- ]independent|data[- ]dependent|\bdda\b|\bdia\b|orbitrap|q exactive|tims?tof|astral|trypsin|lys[- ]?c|acquisition|mass spectrom",
    )
    .unwrap();
    let broad = Regex::new(
        r"(?i)proteom|single[- ]cell|single muscle fib(?:er|re)|blastomere|oocyte|neuron",
    )
    .unwrap();

    let normalized = text.replace('\r', "\n").replace('\u{000c}', "\n");
    let mut out = Vec::new();
    let iso_budget = max_windows.min(10);
    append_match_contexts(&normalized, &isolation, iso_budget, &mut out);
    if out.len() < max_windows {
        append_match_contexts(
            &normalized,
            &sample_design,
            (max_windows - out.len()).min(7),
            &mut out,
        );
    }
    if out.len() < max_windows {
        append_match_contexts(
            &normalized,
            &acquisition,
            (max_windows - out.len()).min(5),
            &mut out,
        );
    }
    if out.len() < max_windows {
        append_match_contexts(&normalized, &broad, max_windows - out.len(), &mut out);
    }
    out.truncate(max_windows);
    out
}

fn publication_paths_for_accession(manifest: &Path, accession: &str) -> Result<Vec<PathBuf>> {
    if !manifest.is_file() {
        return Ok(Vec::new());
    }
    let mut reader = ReaderBuilder::new()
        .delimiter(b'\t')
        .flexible(true)
        .from_path(manifest)?;
    let headers = reader.headers()?.clone();
    let idx = |name: &str| headers.iter().position(|x| x == name);
    let acc_idx = idx("accession");
    let content_idx = idx("publication_content_path");
    let text_idx = idx("publication_content_text_path");
    let mut out = BTreeSet::new();
    for rec in reader.records() {
        let rec = rec?;
        let acc = acc_idx
            .and_then(|i| rec.get(i))
            .unwrap_or("")
            .trim()
            .to_ascii_uppercase();
        if acc != accession {
            continue;
        }
        for i in [text_idx, content_idx].into_iter().flatten() {
            let raw = rec.get(i).unwrap_or("").trim();
            if raw.is_empty() {
                continue;
            }
            let p = PathBuf::from(raw);
            if p.is_file() {
                out.insert(p);
            }
        }
    }
    Ok(out.into_iter().collect())
}

fn annotation_json_paths(root: &Path, accession: &str) -> Result<Vec<PathBuf>> {
    let dir = root.join(accession);
    if !dir.is_dir() {
        return Ok(Vec::new());
    }
    let mut out = Vec::new();
    fn walk(dir: &Path, out: &mut Vec<PathBuf>) -> Result<()> {
        for entry in fs::read_dir(dir)? {
            let path = entry?.path();
            if path.is_dir() {
                walk(&path, out)?;
            } else if path.extension().and_then(|x| x.to_str()) == Some("json") {
                out.push(path);
            }
        }
        Ok(())
    }
    walk(&dir, &mut out)?;
    out.sort();
    Ok(out)
}

fn semantic_bundle_explicit_pxds(bundle: &Value) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    let Some(raw) = bundle.get("publication_pride_accessions") else {
        return out;
    };
    let mut leaves = Vec::new();
    json_string_leaves(raw, "publication_pride_accessions", &mut leaves);
    let re = Regex::new(r"(?i)\bPXD\d{6}\b").unwrap();
    for (_, text) in leaves {
        for m in re.find_iter(&text) {
            out.insert(m.as_str().to_ascii_uppercase());
        }
    }
    out
}

fn file_name_is_direct_acquisition(name: &str) -> bool {
    let lower = name.trim().to_ascii_lowercase();
    RAW_EXTENSIONS.iter().any(|ext| lower.ends_with(ext))
}

fn file_name_is_wrapped_acquisition(name: &str) -> bool {
    let lower = name.trim().to_ascii_lowercase();
    for archive in [".tar.gz", ".tgz", ".zip", ".tar"] {
        if let Some(inner) = lower.strip_suffix(archive) {
            if RAW_EXTENSIONS.iter().any(|ext| inner.ends_with(ext)) {
                return true;
            }
        }
    }
    false
}

fn file_name_is_generic_archive(name: &str) -> bool {
    let lower = name.trim().to_ascii_lowercase();
    if file_name_is_wrapped_acquisition(&lower) {
        return false;
    }
    [".rar", ".7z", ".zip", ".tar.gz", ".tgz", ".tar"]
        .iter()
        .any(|ext| lower.ends_with(ext))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RawFileRole {
    SingleCell,
    FewCell(usize),
    Blank,
    QualityControl,
    Bulk,
    Unknown,
}

fn raw_file_role(name: &str) -> RawFileRole {
    let lower = name.trim().to_ascii_lowercase();
    let normalized = lower.replace('-', "_").replace('.', "_").replace(' ', "_");

    if ["blank", "buffer", "wash", "empty", "solvent"]
        .iter()
        .any(|term| normalized.contains(term))
    {
        return RawFileRole::Blank;
    }
    if [
        "quality_control",
        "qualitycontrol",
        "_qc_",
        "qc_",
        "_qc",
        "irt",
        "standard",
        "std_",
    ]
    .iter()
    .any(|term| normalized.contains(term))
    {
        return RawFileRole::QualityControl;
    }

    let cells = Regex::new(r"(?i)(?:^|[_-])(\d{1,4})(?:[_-]?cells?)(?:[_-]|$)").unwrap();
    if let Some(cap) = cells.captures(&normalized) {
        if let Some(n) = cap.get(1).and_then(|m| m.as_str().parse::<usize>().ok()) {
            if n == 1 {
                return RawFileRole::SingleCell;
            }
            if n > 1 {
                return RawFileRole::FewCell(n);
            }
        }
    }
    if ["singlecell", "single_cell", "1cell", "1_cell"]
        .iter()
        .any(|term| normalized.contains(term))
    {
        return RawFileRole::SingleCell;
    }

    let amount = Regex::new(r"(?i)(?:^|[_-])\d+(?:p|n)g(?:[_-]|$)").unwrap();
    if normalized.contains("bulk")
        || amount.is_match(&normalized)
        || ["hela_digest", "proteomix", "reference_digest"]
            .iter()
            .any(|term| normalized.contains(term))
    {
        return RawFileRole::Bulk;
    }
    RawFileRole::Unknown
}

fn raw_file_role_label(role: RawFileRole) -> &'static str {
    match role {
        RawFileRole::SingleCell => "single_cell",
        RawFileRole::FewCell(_) => "few_cell",
        RawFileRole::Blank => "blank",
        RawFileRole::QualityControl => "quality_control",
        RawFileRole::Bulk => "bulk",
        RawFileRole::Unknown => "unknown",
    }
}

fn raw_file_role_counts(raw_files: &[RawFile]) -> BTreeMap<String, usize> {
    let mut counts = BTreeMap::new();
    for file in raw_files {
        *counts
            .entry(raw_file_role_label(raw_file_role(&file.file_name)).to_string())
            .or_insert(0) += 1;
    }
    counts
}

fn evidence_refs_containing(evidence: &[EvidenceItem], terms: &[&str]) -> Vec<String> {
    let mut refs = Vec::new();
    for item in evidence {
        let hay = format!("{} {}", item.source_label, item.text).to_ascii_lowercase();
        if terms.iter().any(|term| hay.contains(term)) {
            refs.push(item.id.clone());
            if refs.len() >= 8 {
                break;
            }
        }
    }
    refs
}

fn evidence_hay(item: &EvidenceItem) -> String {
    format!("{} {}", item.source_label, item.text).to_ascii_lowercase()
}

fn relation_text_has_word(text: &str, word: &str) -> bool {
    text.split(|c: char| !c.is_ascii_alphanumeric())
        .any(|token| token == word)
}

fn relation_text_has_single_branch(text: &str) -> bool {
    [
        "single-cell",
        "single cell",
        "single neuron",
        "single neurons",
        "single oocyte",
        "single oocytes",
        "single zygote",
        "single zygotes",
        "single blastomere",
        "single blastomeres",
        "single muscle fiber",
        "single muscle fibre",
        "single fiber",
        "single fibre",
    ]
    .iter()
    .any(|term| text.contains(term))
}

fn relation_text_has_isobaric_reporter_evidence(text: &str) -> bool {
    [
        "tmtpro",
        "tmt pro",
        "tandem mass tag",
        "itraq",
        "carrier channel",
        "carrier proteome",
        "carrier sample",
        "reference channel",
        "reporter channel",
        "isobaric",
    ]
    .iter()
    .any(|term| text.contains(term))
        || relation_text_has_word(text, "tmt")
}

fn relation_text_has_nonisobaric_acquisition(text: &str) -> bool {
    [
        "label-free",
        "label free",
        "data-independent",
        "data independent",
        "dia-pasef",
        "diapasef",
        "ce-ms",
        "ce ms",
        "capillary electrophoresis",
        "maldi",
        "top-down",
        "top down",
        "direct injection",
    ]
    .iter()
    .any(|term| text.contains(term))
        || relation_text_has_word(text, "dia")
}

fn relation_scope_evidence(
    evidence: &[EvidenceItem],
) -> (Vec<String>, Vec<String>, Vec<String>, Vec<String>) {
    let mut linked_isobaric = BTreeSet::new();
    let mut linked_nonisobaric = BTreeSet::new();
    let mut dataset_only_isobaric = BTreeSet::new();
    let mut plexdia = BTreeSet::new();

    for item in evidence {
        let text = item.text.to_ascii_lowercase();
        if text.contains("plexdia") || text.contains("plex dia") {
            plexdia.insert(item.id.clone());
        }

        // Mirror the accepted v0.4.1 relation auditor: use a sentence-ish
        // previous/current/next neighborhood so PDF line wrapping is tolerated,
        // while evidence from unrelated sub-studies elsewhere in a deposit cannot
        // make a single-cell branch look isobarically multiplexed.
        let chunks = text
            .split(|c| matches!(c, '.' | ';' | '\n'))
            .filter(|chunk| !chunk.trim().is_empty())
            .collect::<Vec<_>>();
        for i in 0..chunks.len() {
            let start = i.saturating_sub(1);
            let end = (i + 2).min(chunks.len());
            let neighborhood = chunks[start..end].join(" ");
            let has_single = relation_text_has_single_branch(&neighborhood);
            let has_isobaric = relation_text_has_isobaric_reporter_evidence(&neighborhood);
            let has_nonisobaric = relation_text_has_nonisobaric_acquisition(&neighborhood);

            if has_isobaric && has_single {
                linked_isobaric.insert(item.id.clone());
            } else if has_isobaric {
                dataset_only_isobaric.insert(item.id.clone());
            }
            if has_nonisobaric && has_single {
                linked_nonisobaric.insert(item.id.clone());
            }
        }
    }

    let bounded = |set: BTreeSet<String>| set.into_iter().take(8).collect::<Vec<_>>();
    (
        bounded(linked_isobaric),
        bounded(linked_nonisobaric),
        bounded(dataset_only_isobaric),
        bounded(plexdia),
    )
}

fn refs_for_predicate<F>(evidence: &[EvidenceItem], field: &str, predicate: F) -> Vec<String>
where
    F: Fn(&str) -> bool,
{
    let mut refs = Vec::new();
    for item in evidence {
        if !evidence_relevant_to_field(field, item) {
            continue;
        }
        let hay = evidence_hay(item);
        if predicate(&hay) {
            refs.push(item.id.clone());
            if refs.len() >= 8 {
                break;
            }
        }
    }
    refs
}

fn channel_hints_from_evidence(evidence: &[EvidenceItem], role_terms: &[&str]) -> Vec<String> {
    let channel_re = Regex::new(r"(?i)\b(?:tmt(?:pro)?[- ]?)?(12[6-9]|13[0-5])([nc])?\b").unwrap();
    let mut out = BTreeSet::new();
    for item in evidence {
        let hay = evidence_hay(item);
        if !role_terms.iter().any(|term| hay.contains(term)) {
            continue;
        }
        for cap in channel_re.captures_iter(&item.text) {
            let Some(number) = cap.get(1) else { continue };
            let suffix = cap
                .get(2)
                .map(|m| m.as_str().to_ascii_uppercase())
                .unwrap_or_default();
            out.insert(format!("{}{}", number.as_str(), suffix));
            if out.len() >= 8 {
                break;
            }
        }
    }
    out.into_iter().collect()
}

fn structured_project_value(
    evidence: &[EvidenceItem],
    label_terms: &[&str],
) -> Option<(String, Vec<String>)> {
    for item in evidence {
        if item.source_kind != "pride_project" {
            continue;
        }
        let label = item.source_label.to_ascii_lowercase();
        if !label_terms.iter().any(|term| label.contains(term)) {
            continue;
        }
        if label.contains("accession")
            || label.contains("url")
            || label.contains("uri")
            || label.contains("cvlabel")
            || label.ends_with(".id")
        {
            continue;
        }
        let nameish = label.contains(".name")
            || label.contains("scientificname")
            || label.ends_with("species")
            || label.ends_with("organism")
            || label.ends_with("instrument");
        if !nameish {
            continue;
        }
        let value = item.text.trim();
        if value.is_empty()
            || value.starts_with("http://")
            || value.starts_with("https://")
            || value.to_ascii_lowercase().starts_with("ncbitaxon:")
            || value.to_ascii_lowercase().starts_with("ms:")
        {
            continue;
        }
        return Some((value.to_string(), vec![item.id.clone()]));
    }
    None
}

fn metadata_scaffold_insert(
    scaffold: &mut DeterministicMetadataScaffold,
    field: &str,
    value: String,
    refs: Vec<String>,
) {
    if value.trim().is_empty() || refs.is_empty() {
        return;
    }
    scaffold.values.insert(field.to_string(), value);
    scaffold.evidence_refs.insert(field.to_string(), refs);
}

fn manual_picking_evidence(hay: &str) -> bool {
    hay.contains("manual picking")
        || hay.contains("manually dissect")
        || hay.contains("manual dissection")
        || hay.contains("using tweezers")
        || hay.contains("fine-tipped tweezers")
        || hay.contains("fine tipped tweezers")
        || hay.contains("microdissect")
        || hay.contains("dissect single")
        || hay.contains("dissected single")
        || hay.contains("identify and dissect")
        || (hay.contains("mechanically dissociated") && hay.contains("tweezer"))
        || (hay.contains("mechanically dissociated") && hay.contains("individually transferred"))
}

fn infer_deterministic_metadata_scaffold(
    evidence: &[EvidenceItem],
    design: &StudyDesignScaffold,
) -> DeterministicMetadataScaffold {
    let mut out = DeterministicMetadataScaffold::default();

    if let Some((value, refs)) = structured_project_value(
        evidence,
        &["organism.name", "organisms", "species.name", "species"],
    ) {
        metadata_scaffold_insert(&mut out, "organism", value, refs);
    }
    if let Some((value, refs)) =
        structured_project_value(evidence, &["instrument.name", "instruments", "instrument"])
    {
        metadata_scaffold_insert(&mut out, "instrument", value, refs);
    }

    let label_free_refs = refs_for_predicate(evidence, "label", |hay| {
        ["label-free", "label free", "label‐free", "label–free"]
            .iter()
            .any(|term| hay.contains(term))
    });
    if !label_free_refs.is_empty() && design.relation_mode_hint != "multiplexed_cells_per_data_file"
    {
        metadata_scaffold_insert(
            &mut out,
            "label",
            "NT=label free sample;AC=MS:1002038".into(),
            label_free_refs,
        );
    }

    let dda_refs = refs_for_predicate(evidence, "proteomics_data_acquisition_method", |hay| {
        hay.contains("data-dependent") || hay.contains("data dependent") || hay.contains(" dda ")
    });
    if !dda_refs.is_empty() {
        metadata_scaffold_insert(
            &mut out,
            "proteomics_data_acquisition_method",
            "NT=data-dependent acquisition;AC=PRIDE:0000627".into(),
            dda_refs,
        );
    } else {
        let dia_refs = refs_for_predicate(evidence, "proteomics_data_acquisition_method", |hay| {
            hay.contains("data-independent")
                || hay.contains("data independent")
                || hay.contains("dia-pasef")
                || hay.contains("diapasef")
                || hay.contains("swath")
        });
        if !dia_refs.is_empty() {
            metadata_scaffold_insert(
                &mut out,
                "proteomics_data_acquisition_method",
                "data-independent acquisition".into(),
                dia_refs,
            );
        }
    }

    let trypsin_refs = refs_for_predicate(evidence, "cleavage_agent_details", |hay| {
        hay.contains("trypsin")
    });
    if !trypsin_refs.is_empty() {
        metadata_scaffold_insert(
            &mut out,
            "cleavage_agent_details",
            "trypsin".into(),
            trypsin_refs,
        );
    }

    let isolation_candidates: [(&str, &str, &[&str]); 7] = [
        (
            "FACS",
            "FACS",
            &["facs", "fluorescence-activated cell sort", "flow cytometr"],
        ),
        ("cellenONE", "cellenONE", &["cellenone"]),
        (
            "laser capture microdissection",
            "laser capture microdissection",
            &["laser capture", " lcm "],
        ),
        ("nanoPOTS", "nanoPOTS", &["nanopots"]),
        (
            "droplet microfluidics",
            "droplet microfluidics",
            &["droplet microfluid"],
        ),
        (
            "acoustic droplet ejection",
            "acoustic droplet ejection",
            &["acoustic droplet"],
        ),
        ("microfluidics", "microfluidics", &["microfluid"]),
    ];
    let mut isolation_set = false;
    for (_observed, template_value, terms) in isolation_candidates {
        let refs = refs_for_predicate(evidence, "single_cell_isolation_method", |hay| {
            terms.iter().any(|term| hay.contains(term))
        });
        if !refs.is_empty() {
            metadata_scaffold_insert(
                &mut out,
                "single_cell_isolation_method",
                template_value.to_string(),
                refs,
            );
            isolation_set = true;
            break;
        }
    }
    if !isolation_set {
        // `manual picking` requires an explicit manual action or instrumentation
        // cue. A generic repository statement that a sample was "taken" is not
        // enough to claim a specific isolation protocol.
        let manual_refs = refs_for_predicate(
            evidence,
            "single_cell_isolation_method",
            manual_picking_evidence,
        );
        if !manual_refs.is_empty() {
            metadata_scaffold_insert(
                &mut out,
                "single_cell_isolation_method",
                "manual picking".to_string(),
                manual_refs,
            );
            isolation_set = true;
        }
    }
    if !isolation_set {
        let unsupported = [
            (
                "patch-clamp-guided microaspiration",
                &[
                    "patch clamp",
                    "patch-clamp",
                    "patch clamp probe",
                    "neuronal soma",
                    "microaspirat",
                ] as &[&str],
            ),
            (
                "capillary microsampling",
                &["capillary microsampling", "in situ subcellular", "aspirat"] as &[&str],
            ),
            (
                "microwell-chip single-cell transfer",
                &[
                    "microwell chip",
                    "microwell-chip",
                    "transferring to the microwells",
                    "transfer to the microwell",
                    "single cells into microwells",
                ] as &[&str],
            ),
        ];
        for (observed, terms) in unsupported {
            let refs = refs_for_predicate(evidence, "single_cell_isolation_method", |hay| {
                terms.iter().any(|term| hay.contains(term))
            });
            if !refs.is_empty() {
                out.template_gaps.push(TemplateCompatibilityGap {
                    field: "single_cell_isolation_method".into(),
                    observed_value: observed.into(),
                    evidence_refs: refs,
                    reason: "the pinned single-cell 1.0.0 isolation-method vocabulary does not contain a faithful term for this experimentally supported sampling method; do not substitute a false allowed value".into(),
                });
                break;
            }
        }
    }
    out
}

fn infer_study_design_scaffold(
    raw_files: &[RawFile],
    evidence: &[EvidenceItem],
) -> StudyDesignScaffold {
    let direct_acquisition_files = raw_files
        .iter()
        .filter(|f| file_name_is_direct_acquisition(&f.file_name))
        .count();
    let wrapped_acquisition_files = raw_files
        .iter()
        .filter(|f| file_name_is_wrapped_acquisition(&f.file_name))
        .count();
    let generic_archive_file_names = raw_files
        .iter()
        .filter(|f| file_name_is_generic_archive(&f.file_name))
        .map(|f| f.file_name.clone())
        .collect::<Vec<_>>();
    let generic_archive_files = generic_archive_file_names.len();
    let repository_file_mode =
        if generic_archive_files > 0 && direct_acquisition_files + wrapped_acquisition_files == 0 {
            "generic_archives_only"
        } else if generic_archive_files > 0 {
            "mixed_acquisitions_and_generic_archives"
        } else if wrapped_acquisition_files > 0 {
            "direct_or_wrapped_acquisitions"
        } else {
            "direct_acquisitions"
        }
        .to_string();

    let single_refs = evidence_refs_containing(
        evidence,
        &[
            "single-cell",
            "single cell",
            "single muscle fiber",
            "single muscle fibre",
            "single neuron",
            "single blastomere",
            "single oocyte",
            "single egg",
        ],
    );
    // Dataset-level chemistry is retained as diagnostic evidence, but it no longer
    // asserts a multiplex relation by itself. In particular, lexical `plex` and
    // plexDIA are not isobaric-reporter evidence.
    let isobaric_refs = evidence_refs_containing(
        evidence,
        &[
            "tmtpro",
            "tmt pro",
            "tmt ",
            "tmt-",
            "tandem mass tag",
            "itraq",
            "carrier channel",
            "carrier proteome",
            "reference channel",
            "reporter channel",
            "isobaric",
        ],
    );
    let label_free_refs = evidence_refs_containing(
        evidence,
        &["label-free", "label free", "label‐free", "label–free"],
    );
    let few_cell_refs = evidence_refs_containing(
        evidence,
        &[
            "few-cell",
            "few cell",
            "small pool",
            "pooled cells",
            "multiple cells per",
            "multiple cells were",
        ],
    );
    let specific_single_refs = evidence_refs_containing(
        evidence,
        &[
            "single muscle fiber",
            "single muscle fibre",
            "single blastomere",
            "single neuron",
            "single oocyte",
            "single egg",
        ],
    );
    let (linked_isobaric_refs, linked_nonisobaric_refs, _dataset_only_isobaric_refs, plexdia_refs) =
        relation_scope_evidence(evidence);

    let mut relation_evidence_refs = Vec::new();
    let (relation_mode_hint, relation_confidence, note) = if !linked_isobaric_refs.is_empty() {
        relation_evidence_refs.extend(linked_isobaric_refs.iter().cloned());
        (
            "multiplexed_cells_per_data_file",
            "high",
            "isobaric reporter/carrier evidence is locally linked to the single-cell branch",
        )
    } else if !single_refs.is_empty() && !few_cell_refs.is_empty() {
        relation_evidence_refs.extend(single_refs.iter().cloned());
        relation_evidence_refs.extend(few_cell_refs.iter().cloned());
        (
            "mixed",
            "medium",
            "single-cell and explicit few-cell/small-pool evidence co-occur",
        )
    } else if !linked_nonisobaric_refs.is_empty() {
        relation_evidence_refs.extend(linked_nonisobaric_refs.iter().cloned());
        (
            "one_cell_per_data_file",
            "high",
            "non-isobaric acquisition evidence is locally linked to the single-cell branch",
        )
    } else if !single_refs.is_empty() && !label_free_refs.is_empty() && isobaric_refs.is_empty() {
        relation_evidence_refs.extend(single_refs.iter().cloned());
        relation_evidence_refs.extend(label_free_refs.iter().cloned());
        (
            "one_cell_per_data_file",
            "high",
            "single-cell and label-free evidence co-occur without isobaric reporter evidence",
        )
    } else if !specific_single_refs.is_empty()
        && isobaric_refs.is_empty()
        && plexdia_refs.is_empty()
    {
        relation_evidence_refs.extend(specific_single_refs.iter().cloned());
        (
            "one_cell_per_data_file",
            "medium",
            "specific single-sample evidence is present without isobaric reporter or plexDIA evidence",
        )
    } else {
        (
            "uncertain",
            "low",
            "evidence does not safely determine sample-to-data-file cardinality at single-cell branch scope",
        )
    };
    relation_evidence_refs.sort();
    relation_evidence_refs.dedup();
    relation_evidence_refs.truncate(8);

    let multiplex_chemistry_hint =
        if evidence_refs_containing(evidence, &["tmtpro", "tmt pro"]).is_empty() {
            if evidence_refs_containing(evidence, &["tmt ", "tmt-", "tandem mass tag"]).is_empty() {
                if evidence_refs_containing(evidence, &["itraq"]).is_empty() {
                    String::new()
                } else {
                    "iTRAQ".into()
                }
            } else {
                "TMT".into()
            }
        } else {
            "TMTpro".into()
        };
    let mut multiplex_evidence_refs = isobaric_refs.clone();
    multiplex_evidence_refs.sort();
    multiplex_evidence_refs.dedup();
    multiplex_evidence_refs.truncate(8);
    let carrier_channel_hints = channel_hints_from_evidence(
        evidence,
        &[
            "carrier channel",
            "carrier proteome",
            "carrier sample",
            "carrier cells",
        ],
    );
    let reference_channel_hints = channel_hints_from_evidence(
        evidence,
        &["reference channel", "reference sample", "bridge channel"],
    );
    let multiplex_mapping_status = if relation_mode_hint == "multiplexed_cells_per_data_file" {
        if carrier_channel_hints.is_empty() && reference_channel_hints.is_empty() {
            "chemistry_detected_channel_mapping_unresolved"
        } else {
            "channel_role_hints_detected_mapping_unresolved"
        }
    } else {
        "not_applicable"
    }
    .to_string();

    StudyDesignScaffold {
        relation_mode_hint: relation_mode_hint.to_string(),
        relation_confidence: relation_confidence.to_string(),
        relation_evidence_refs,
        repository_file_mode,
        direct_acquisition_files,
        wrapped_acquisition_files,
        generic_archive_files,
        generic_archive_file_names,
        multiplex_chemistry_hint,
        multiplex_evidence_refs,
        carrier_channel_hints,
        reference_channel_hints,
        multiplex_mapping_status,
        file_role_hint_counts: raw_file_role_counts(raw_files),
        notes: note.to_string(),
    }
}

fn pre_manuscript_evidence_caps(
    usable_existing_sdrf: bool,
    has_manuscript_sources: bool,
    max_items: usize,
    max_chars: usize,
) -> (usize, usize) {
    // Existing/resolved SDRFs are preservation-first and may legitimately consume
    // the evidence budget. For de-novo reconstruction, however, reserve a bounded
    // quarter of the packet for direct manuscript windows so project/annotation
    // JSON cannot starve the deterministic Methods/isolation scaffold.
    if usable_existing_sdrf || !has_manuscript_sources {
        return (max_items, max_chars);
    }
    let reserved_items = (max_items / 4)
        .min(MANUSCRIPT_EVIDENCE_MAX_RESERVED_ITEMS)
        .min(max_items.saturating_sub(1));
    let reserved_chars = (max_chars / 4)
        .min(MANUSCRIPT_EVIDENCE_MAX_RESERVED_CHARS)
        .min(max_chars.saturating_sub(1));
    (
        max_items.saturating_sub(reserved_items).max(1),
        max_chars.saturating_sub(reserved_chars).max(1),
    )
}

fn build_evidence(opts: &SdrfAnnotateOptions, accession: &str) -> Result<DatasetEvidence> {
    let project_path = opts
        .snapshot_dir
        .join("projects")
        .join(format!("{accession}.json"));
    let files_path = opts
        .snapshot_dir
        .join("files")
        .join(format!("{accession}.json"));
    let snapshot_sdrf = opts
        .snapshot_dir
        .join("sdrf")
        .join(format!("{accession}.sdrf.tsv"));
    let resolved_sdrf = opts
        .resolved_sdrf_dir
        .as_ref()
        .map(|dir| dir.join(format!("{accession}.sdrf.tsv")));
    let existing_sdrf = resolved_sdrf
        .as_ref()
        .filter(|p| existing_sdrf_is_usable(p))
        .cloned()
        .unwrap_or_else(|| snapshot_sdrf.clone());
    if !project_path.is_file() {
        bail!("missing PRIDE project snapshot {}", project_path.display());
    }
    if !files_path.is_file() {
        bail!("missing PRIDE file snapshot {}", files_path.display());
    }
    let project = load_json(&project_path)?;
    let files = load_json(&files_path)?;
    let raw_files = extract_raw_files(&files);
    if raw_files.is_empty() {
        bail!(
            "no RAW-category/data files resolved from {}",
            files_path.display()
        );
    }

    let mut manuscript_paths = Vec::new();
    if let Some(manifest) = &opts.publication_manifest {
        manuscript_paths.extend(publication_paths_for_accession(manifest, accession)?);
    }
    manuscript_paths.extend(
        opts.manuscript_text_paths
            .iter()
            .filter(|p| p.is_file())
            .cloned(),
    );
    manuscript_paths.sort();
    manuscript_paths.dedup();

    let mut evidence = Vec::new();
    // Existing SDRF is the highest-value source only when it contains a real
    // sample-to-data mapping. The PRIDE SDRF endpoint can yield header-only cache
    // files, so file existence alone must never activate the preservation path.
    let usable_existing_sdrf = existing_sdrf_is_usable(&existing_sdrf);
    let (pre_manuscript_max_items, pre_manuscript_max_chars) = pre_manuscript_evidence_caps(
        usable_existing_sdrf,
        !manuscript_paths.is_empty(),
        opts.max_evidence_items,
        opts.max_evidence_chars,
    );
    if usable_existing_sdrf {
        add_existing_sdrf_evidence(
            &mut evidence,
            &existing_sdrf,
            pre_manuscript_max_items,
            pre_manuscript_max_chars,
        )?;
    }
    // Project metadata may contain biological/acquisition facts. File-list JSON is
    // deliberately excluded from LLM evidence: fields such as fileCategory and
    // publicFileLocations describe repository transport, not biology.
    add_json_evidence(
        &mut evidence,
        "pride_project",
        "project",
        &project,
        pre_manuscript_max_items,
        pre_manuscript_max_chars,
    );

    let mut annotation_sources = Vec::new();
    for path in annotation_json_paths(&opts.annotations_dir, accession)? {
        if evidence.len() >= pre_manuscript_max_items {
            break;
        }
        let Ok(value) = load_json(&path) else {
            continue;
        };
        let semantic_path = value
            .get("provenance")
            .and_then(Value::as_object)
            .and_then(|m| m.get("semantic_evidence_file"))
            .and_then(Value::as_str)
            .map(PathBuf::from);
        let semantic_bundle = semantic_path
            .as_ref()
            .filter(|p| p.is_file())
            .and_then(|p| load_json(p).ok());
        if let Some(bundle) = &semantic_bundle {
            let explicit = semantic_bundle_explicit_pxds(bundle);
            if !explicit.is_empty() && !explicit.contains(accession) {
                log::debug!(
                    "{} skipping publication-linked annotation {} because explicit PXDs are {:?}",
                    accession,
                    path.display(),
                    explicit
                );
                continue;
            }
        }

        annotation_sources.push(path.display().to_string());
        add_json_evidence(
            &mut evidence,
            "existing_annotation",
            &format!(
                "annotation:{}",
                path.file_name()
                    .and_then(|x| x.to_str())
                    .unwrap_or("annotation.json")
            ),
            &value,
            pre_manuscript_max_items,
            pre_manuscript_max_chars,
        );
        // Stage04 annotations point to manuscript-derived semantic evidence. Only
        // route a bundle when its explicit PXD list is compatible with this target.
        if let (Some(path), Some(bundle)) = (semantic_path, semantic_bundle) {
            annotation_sources.push(path.display().to_string());
            add_json_evidence(
                &mut evidence,
                "manuscript_semantic_evidence",
                "stage04_semantic",
                &bundle,
                pre_manuscript_max_items,
                pre_manuscript_max_chars,
            );
        }
    }

    let mut manuscript_sources = Vec::new();
    for path in manuscript_paths {
        if evidence.len() >= opts.max_evidence_items {
            break;
        }
        match read_manuscript_for_keyword_scan(&path) {
            Ok(text) => {
                manuscript_sources.push(path.display().to_string());
                for (i, window) in manuscript_keyword_windows(&text, 24)
                    .into_iter()
                    .enumerate()
                {
                    push_evidence(
                        &mut evidence,
                        "manuscript_text",
                        format!(
                            "manuscript:{}:window={}",
                            path.file_name().and_then(|x| x.to_str()).unwrap_or("text"),
                            i + 1
                        ),
                        window,
                        opts.max_evidence_items,
                        opts.max_evidence_chars,
                    );
                }
            }
            Err(err) => {
                log::debug!(
                    "{} manuscript source skipped {}: {err:#}",
                    accession,
                    path.display()
                );
            }
        }
    }

    let study_design = infer_study_design_scaffold(&raw_files, &evidence);
    let metadata_scaffold = infer_deterministic_metadata_scaffold(&evidence, &study_design);

    Ok(DatasetEvidence {
        accession: accession.to_string(),
        project_json_path: project_path.display().to_string(),
        files_json_path: files_path.display().to_string(),
        existing_sdrf_path: usable_existing_sdrf
            .then(|| existing_sdrf.display().to_string())
            .unwrap_or_default(),
        raw_files,
        study_design,
        metadata_scaffold,
        evidence,
        manuscript_sources,
        annotation_sources,
    })
}

fn proposal_schema() -> Value {
    let fields = [
        "relation_mode",
        "organism",
        "organism_part",
        "disease",
        "cell_type",
        "sample_type",
        "single_cell_isolation_method",
        "individual",
        "sample_preparation_batch",
        "cells_per_well",
        "proteomics_data_acquisition_method",
        "label",
        "instrument",
        "cleavage_agent_details",
        "fraction_identifier",
        "technical_replicate",
        "carrier_channel",
        "reference_channel",
    ];
    let mut evidence_props = Map::new();
    for field in fields {
        evidence_props.insert(
            field.to_string(),
            json!({"type":"array","items":{"type":"string","pattern":"^E[0-9]{4}$"},"maxItems":8}),
        );
    }
    json!({
        "type": "object",
        "properties": {
            "relation_mode": {"type":"string","enum":["one_cell_per_data_file","multiplexed_cells_per_data_file","mixed","uncertain"]},
            "organism": {"type":"string","maxLength":160},
            "organism_part": {"type":"string","maxLength":160},
            "disease": {"type":"string","maxLength":160},
            "cell_type": {"type":"string","maxLength":160},
            "sample_type": {"type":"string","maxLength":160},
            "single_cell_isolation_method": {"type":"string","maxLength":160},
            "individual": {"type":"string","maxLength":160},
            "sample_preparation_batch": {"type":"string","maxLength":160},
            "cells_per_well": {"type":"string","maxLength":64},
            "proteomics_data_acquisition_method": {"type":"string","maxLength":160},
            "label": {"type":"string","maxLength":160},
            "instrument": {"type":"string","maxLength":160},
            "cleavage_agent_details": {"type":"string","maxLength":200},
            "fraction_identifier": {"type":"string","maxLength":64},
            "technical_replicate": {"type":"string","maxLength":64},
            "carrier_channel": {"type":"string","maxLength":160},
            "reference_channel": {"type":"string","maxLength":160},
            "factors": {"type":"array","maxItems":6,"items":{"type":"object","properties":{"name":{"type":"string","maxLength":80},"value":{"type":"string","maxLength":160},"evidence_refs":{"type":"array","items":{"type":"string","pattern":"^E[0-9]{4}$"},"maxItems":8}},"required":["name","value","evidence_refs"],"additionalProperties":false}},
            "evidence_refs": {"type":"object","properties": evidence_props,"additionalProperties":false},
            "confidence": {"type":"string","enum":["high","medium","low"]},
            "notes": {"type":"string","maxLength":800}
        },
        "required": ["relation_mode","organism","organism_part","disease","cell_type","sample_type","single_cell_isolation_method","individual","sample_preparation_batch","cells_per_well","proteomics_data_acquisition_method","label","instrument","cleavage_agent_details","fraction_identifier","technical_replicate","carrier_channel","reference_channel","factors","evidence_refs","confidence","notes"],
        "additionalProperties": false
    })
}

fn prompt_for(evidence: &DatasetEvidence, max_files: usize) -> String {
    let files = evidence
        .raw_files
        .iter()
        .take(max_files)
        .map(|f| format!("- {}", f.file_name))
        .collect::<Vec<_>>()
        .join("\n");
    let targets = proposal_target_fields(evidence);
    let ordered_fields = [
        "relation_mode",
        "organism",
        "organism_part",
        "disease",
        "cell_type",
        "sample_type",
        "single_cell_isolation_method",
        "individual",
        "sample_preparation_batch",
        "cells_per_well",
        "proteomics_data_acquisition_method",
        "label",
        "instrument",
        "cleavage_agent_details",
        "fraction_identifier",
        "technical_replicate",
        "carrier_channel",
        "reference_channel",
    ];
    let target_list = ordered_fields
        .iter()
        .filter(|f| targets.contains(**f))
        .copied()
        .collect::<Vec<_>>()
        .join(", ");
    let locked_list = ordered_fields
        .iter()
        .filter(|f| !targets.contains(**f))
        .copied()
        .collect::<Vec<_>>()
        .join(", ");
    let sections = ordered_fields
        .iter()
        .filter(|f| targets.contains(**f))
        .map(|f| format!("### {f}\n{}", field_evidence_block(evidence, f, 10)))
        .collect::<Vec<_>>()
        .join("\n\n");
    format!(
        "You are extracting ONLY missing metadata fields for an SDRF-Proteomics single-cell draft for {acc}.\n\n\
PRECOMPUTED STUDY-DESIGN SCAFFOLD (deterministic Rust; do not contradict a non-uncertain relation hint):\n\
- relation_mode_hint: {relation_hint}\n\
- relation_confidence: {relation_confidence}\n\
- repository_file_mode: {repository_file_mode}\n\
- design_note: {design_note}\n\
- multiplex_chemistry_hint: {multiplex_chemistry}\n\
- multiplex_mapping_status: {multiplex_mapping_status}\n\
- carrier_channel_hints: {carrier_hints}\n\
- reference_channel_hints: {reference_hints}\n\n\
TARGET FIELDS: {target_list}\n\
LOCKED/ALREADY-STRUCTURED FIELDS: {locked_list}\n\n\
RULES:\n\
1. Use ONLY the field-specific evidence shown under the matching field heading. Never invent metadata or borrow a value from an unrelated field.\n\
2. Fields in LOCKED/ALREADY-STRUCTURED FIELDS must be returned exactly as 'not available' with an empty evidence_refs list; for relation_mode use 'uncertain'. Rust preserves existing SDRF values deterministically.\n\
3. If a TARGET field is not supported by its own evidence section, return exactly 'not available' (or relation_mode='uncertain').\n\
4. Every concrete TARGET value must cite one or more E#### refs from that SAME field section.\n\
5. relation_mode means sample-to-RAW design: one_cell_per_data_file, multiplexed_cells_per_data_file, mixed, or uncertain. Do not infer it merely from the phrase 'single-cell'.\n\
6. For single_cell_isolation_method, use only a method faithfully represented by the pinned template vocabulary (for example FACS, cellenONE, microfluidics, laser capture microdissection, manual picking, nanoPOTS, droplet microfluidics, or acoustic droplet ejection). If the evidence instead supports a method such as patch-clamp aspiration or capillary microsampling that the pinned vocabulary cannot represent faithfully, return 'not available'; Rust records the template-compatibility gap separately. Software such as MaxQuant is never an isolation method.\n\
7. proteomics_data_acquisition_method describes MS acquisition (for example DDA, DIA, diaPASEF, PRM), not analysis/search software.\n\
8. instrument is the mass spectrometer/instrument, not software.\n\
9. Do not infer a per-cell identifier from filenames here. Rust constructs identifiers only when the row relationship is deterministically supported.\n\
10. Dataset-level biological values may vary by row. Return a concrete value only if the evidence supports a single dataset-wide value; otherwise use 'not available'.\n\
11. Return factors=[] in this version. Per-row factor reconstruction is deferred.\n\
12. Repository labels (PRIDE, PXD accessions, fileCategory, URLs) and analysis software must never be copied into biological/MS fields.\n\
13. Uncertainty is preferable to hallucination.\n\n\
RAW FILE COUNT: {nfiles}\n\
RAW FILE SAMPLE (context only; max {max_files}):\n{files}\n\n\
FIELD-SPECIFIC EVIDENCE:\n{sections}",
        acc = evidence.accession,
        relation_hint = evidence.study_design.relation_mode_hint.as_str(),
        relation_confidence = evidence.study_design.relation_confidence.as_str(),
        repository_file_mode = evidence.study_design.repository_file_mode.as_str(),
        design_note = evidence.study_design.notes.as_str(),
        multiplex_chemistry = evidence.study_design.multiplex_chemistry_hint.as_str(),
        multiplex_mapping_status = evidence.study_design.multiplex_mapping_status.as_str(),
        carrier_hints = if evidence.study_design.carrier_channel_hints.is_empty() { "none".into() } else { evidence.study_design.carrier_channel_hints.join(", ") },
        reference_hints = if evidence.study_design.reference_channel_hints.is_empty() { "none".into() } else { evidence.study_design.reference_channel_hints.join(", ") },
        nfiles = evidence.raw_files.len(),
    )
}

async fn call_ollama(
    opts: &SdrfAnnotateOptions,
    evidence: &DatasetEvidence,
) -> Result<SdrfProposal> {
    let client = Client::builder()
        .timeout(Duration::from_secs(opts.timeout_seconds))
        .build()?;
    let payload = json!({
        "model": opts.model,
        "prompt": prompt_for(evidence, opts.max_files_in_prompt),
        "stream": false,
        "format": proposal_schema(),
        "options": {"temperature": 0.0}
    });
    let response = client
        .post(&opts.ollama_url)
        .json(&payload)
        .send()
        .await
        .with_context(|| format!("Ollama request for {}", evidence.accession))?;
    let status = response.status();
    let body: Value = response.json().await.context("decode Ollama response")?;
    if !status.is_success() {
        bail!("Ollama HTTP {status}: {body}");
    }
    let raw = body.get("response").and_then(Value::as_str).unwrap_or("");
    if raw.trim().is_empty() {
        bail!("Ollama returned empty response");
    }
    let proposal: SdrfProposal =
        serde_json::from_str(raw).context("parse structured Ollama SDRF proposal")?;
    Ok(proposal)
}

fn canonical_reserved_alias(value: &str) -> Option<&'static str> {
    let lower = value.trim().to_ascii_lowercase();
    match lower.as_str() {
        "" | "unknown" | "not specified" | "unspecified" | "na" | "n/a" | "none" | "null"
        | "default" | "missing" | "not known" | "not provided" | "not reported"
        | "not determined" | "not available" => Some("not available"),
        "not applicable" => Some("not applicable"),
        "pooled" => Some("pooled"),
        _ => None,
    }
}

fn proposal_value_is_reserved(field: &str, value: &str) -> bool {
    if field == "relation_mode" && value.trim().eq_ignore_ascii_case("uncertain") {
        return true;
    }
    canonical_reserved_alias(value).is_some()
}

fn field_existing_header(field: &str) -> Option<&'static str> {
    match field {
        "organism" => Some("characteristics[organism]"),
        "organism_part" => Some("characteristics[organism part]"),
        "disease" => Some("characteristics[disease]"),
        "cell_type" => Some("characteristics[cell type]"),
        "sample_type" => Some(SC_SAMPLE_TYPE),
        "single_cell_isolation_method" => Some(SC_ISOLATION_METHOD),
        "individual" => Some(SC_INDIVIDUAL),
        "sample_preparation_batch" => Some(SC_PREP_BATCH),
        "cells_per_well" => Some(SC_CELLS_PER_WELL),
        "proteomics_data_acquisition_method" => Some("comment[proteomics data acquisition method]"),
        "label" => Some("comment[label]"),
        "instrument" => Some("comment[instrument]"),
        "cleavage_agent_details" => Some("comment[cleavage agent details]"),
        "fraction_identifier" => Some("comment[fraction identifier]"),
        "technical_replicate" => Some("comment[technical replicate]"),
        "carrier_channel" => Some(SC_CARRIER_CHANNEL),
        "reference_channel" => Some(SC_REFERENCE_CHANNEL),
        _ => None,
    }
}

fn concrete_table_value(value: &str) -> bool {
    let v = value.trim();
    if v.is_empty() {
        return false;
    }
    match canonical_reserved_alias(v) {
        // `not available` is unresolved and may still benefit from bounded
        // evidence-backed enrichment. `not applicable` and `pooled` are
        // deliberate SDRF states, not missing values, so they must not force
        // an otherwise complete existing SDRF back through Ollama.
        Some("not available") => false,
        Some("not applicable") | Some("pooled") => true,
        Some(_) => false,
        None => true,
    }
}

fn study_design_has_assertive_relation_hint(design: &StudyDesignScaffold) -> bool {
    let hint = design.relation_mode_hint.trim();
    !hint.is_empty() && hint != "uncertain"
}

fn proposal_target_fields(evidence: &DatasetEvidence) -> BTreeSet<String> {
    let fields = [
        "relation_mode",
        "organism",
        "organism_part",
        "disease",
        "cell_type",
        "sample_type",
        "single_cell_isolation_method",
        "individual",
        "sample_preparation_batch",
        "cells_per_well",
        "proteomics_data_acquisition_method",
        "label",
        "instrument",
        "cleavage_agent_details",
        "fraction_identifier",
        "technical_replicate",
        "carrier_channel",
        "reference_channel",
    ];
    if evidence.existing_sdrf_path.is_empty() {
        let mut out: BTreeSet<String> = fields.iter().map(|x| x.to_string()).collect();
        if study_design_has_assertive_relation_hint(&evidence.study_design) {
            out.remove("relation_mode");
        }
        if evidence.study_design.relation_mode_hint == "one_cell_per_data_file" {
            for field in [
                "sample_type",
                "cells_per_well",
                "fraction_identifier",
                "technical_replicate",
                "carrier_channel",
                "reference_channel",
            ] {
                out.remove(field);
            }
        }
        for field in evidence.metadata_scaffold.values.keys() {
            out.remove(field);
        }
        // If the real isolation/sampling method is known but cannot be represented
        // by the pinned template vocabulary, another LLM pass cannot safely fix it.
        for gap in &evidence.metadata_scaffold.template_gaps {
            out.remove(&gap.field);
        }
        return out;
    }
    let Ok((headers, rows)) = read_existing_sdrf_table(Path::new(&evidence.existing_sdrf_path))
    else {
        return fields.iter().map(|x| x.to_string()).collect();
    };
    if rows.is_empty() {
        return fields.iter().map(|x| x.to_string()).collect();
    }

    let mut out = BTreeSet::new();
    if existing_sdrf_relation_hint(&headers, &rows) == "uncertain" {
        out.insert("relation_mode".to_string());
    }
    for field in fields.iter().copied().filter(|f| *f != "relation_mode") {
        let Some(header) = field_existing_header(field) else {
            continue;
        };
        let Some(j) = header_first_index(&headers, header) else {
            out.insert(field.to_string());
            continue;
        };
        // Ask Ollama only when at least one existing row is unresolved. Existing
        // values are preserved and are never overwritten by a dataset-level guess.
        if rows
            .iter()
            .any(|r| r.get(j).map_or(true, |v| !concrete_table_value(v)))
        {
            out.insert(field.to_string());
        }
    }
    out
}

fn deterministic_existing_sdrf_proposal(evidence: &DatasetEvidence) -> Option<SdrfProposal> {
    if evidence.existing_sdrf_path.is_empty() {
        return None;
    }
    let targets = proposal_target_fields(evidence);
    if !targets.is_empty() {
        return None;
    }
    let mut proposal = SdrfProposal::default();
    if let Ok((headers, rows)) = read_existing_sdrf_table(Path::new(&evidence.existing_sdrf_path)) {
        proposal.relation_mode = existing_sdrf_relation_hint(&headers, &rows);
    }
    if proposal.relation_mode.trim().is_empty() {
        proposal.relation_mode = "uncertain".into();
    }
    proposal.confidence = "deterministic_existing_sdrf".into();
    proposal.notes = "No missing dataset-level target fields required Ollama extraction; existing SDRF values were preserved and validated deterministically.".into();
    Some(proposal)
}

fn evidence_relevant_to_field(field: &str, item: &EvidenceItem) -> bool {
    let label = item.source_label.to_ascii_lowercase();
    let text = item.text.to_ascii_lowercase();
    let hay = format!("{label} {text}");
    if item.source_kind == "existing_sdrf_structured" {
        if field == "relation_mode" && label.contains("relationship_summary") {
            return true;
        }
        if let Some(header) = field_existing_header(field) {
            if label.contains(&header.to_ascii_lowercase()) {
                return true;
            }
        }
    }
    let terms: &[&str] = match field {
        "relation_mode" => &[
            "single cell",
            "single-cell",
            "single nucleus",
            "single-nucleus",
            "tmt",
            "itraq",
            "carrier",
            "reference channel",
            "reporter channel",
            "multiplex",
        ],
        "organism" => &[
            "organism",
            "species",
            "homo sapiens",
            "human",
            "mus musculus",
            "mouse",
            "rat",
            "rattus",
            "xenopus",
            "zebrafish",
            "danio rerio",
            "drosophila",
            "yeast",
            "arabidopsis",
        ],
        "organism_part" => &[
            "organism part",
            "tissue",
            "brain",
            "blood",
            "marrow",
            "liver",
            "kidney",
            "embryo",
            "oocyte",
            "egg",
            "cell line",
        ],
        "disease" => &[
            "disease",
            "cancer",
            "tumor",
            "carcinoma",
            "leukemia",
            "lymphoma",
            "healthy",
            "control",
            "patient",
        ],
        "cell_type" => &[
            "cell type",
            "cell-type",
            "hela",
            "macrophage",
            "monocyte",
            "lymphocyte",
            "t cell",
            "b cell",
            "oocyte",
            "blastomere",
            "neuron",
            "fibroblast",
            "stem cell",
        ],
        "sample_type" => &[
            "sample type",
            "single cell",
            "single-cell",
            "carrier",
            "reference",
            "empty",
            "control",
            "bulk",
        ],
        "single_cell_isolation_method" => &[
            "isolation",
            "isolated",
            "isolate",
            "sort",
            "sorting",
            "facs",
            "flow cytometry",
            "cellenone",
            "microfluid",
            "laser capture",
            "lcm",
            "manual picking",
            "manual dissection",
            "manually dissect",
            "manual isolat",
            "microdissect",
            "microaspirat",
            "patch clamp",
            "patch-clamp",
            "micropipette",
            "capillary microsampling",
            "nanopots",
            "nanowell",
            "droplet",
            "acoustic droplet",
            "tweezer",
            "individually transferred",
            "individual fibers were transferred",
            "mechanically dissociated",
            "mechanically isolated",
            "single muscle fiber",
            "single muscle fibre",
            "single blastomere",
            "single oocyte",
        ],
        "individual" => &[
            "individual",
            "donor",
            "patient",
            "subject",
            "mouse",
            "animal",
            "embryo",
        ],
        "sample_preparation_batch" => &[
            "sample preparation batch",
            "batch",
            "plate",
            "chip",
            "processing batch",
        ],
        "cells_per_well" => &[
            "cells per well",
            "cell per well",
            "one cell",
            "single cell",
            "single-cell",
            "small pool",
        ],
        "proteomics_data_acquisition_method" => &[
            "acquisition",
            "data-dependent",
            "data dependent",
            "dda",
            "data-independent",
            "data independent",
            "dia",
            "dia-pasef",
            "diapasef",
            "pasef",
            "prm",
            "srm",
            "targeted",
        ],
        "label" => &[
            "label",
            "tmt",
            "tmtpro",
            "itraq",
            "silac",
            "dimethyl",
            "label-free",
            "label free",
            "plexdia",
        ],
        "instrument" => &[
            "instrument",
            "orbitrap",
            "q exactive",
            "exploris",
            "eclipse",
            "fusion",
            "lumos",
            "astral",
            "timstof",
            "tims tof",
            "tof",
            "mass spectrometer",
        ],
        "cleavage_agent_details" => &[
            "cleavage",
            "digest",
            "digestion",
            "trypsin",
            "lys-c",
            "lysc",
            "chymotrypsin",
            "glu-c",
            "asp-n",
            "arg-c",
            "pepsin",
        ],
        "fraction_identifier" => &["fraction", "fractionation"],
        "technical_replicate" => &["technical replicate", "replicate"],
        "carrier_channel" => &[
            "carrier channel",
            "carrier proteome",
            "carrier sample",
            "carrier cells",
        ],
        "reference_channel" => &[
            "reference channel",
            "reference sample channel",
            "bridge channel",
            "reference sample",
            "reference proteome",
        ],
        _ => &[],
    };
    if field == "single_cell_isolation_method" && manual_picking_evidence(&hay) {
        return true;
    }
    terms.iter().any(|t| hay.contains(t))
}

fn obviously_invalid_field_value(field: &str, value: &str) -> bool {
    let v = value.trim().to_ascii_lowercase();
    if v.is_empty() || proposal_value_is_reserved(field, value) {
        return false;
    }
    if ["pride", "singlecells", "single cells", "pxd", "repository"]
        .iter()
        .any(|x| v == *x || v.starts_with("pxd"))
    {
        return true;
    }
    let analysis_software = [
        "maxquant",
        "proteome discoverer",
        "fragpipe",
        "spectronaut",
        "dia-nn",
        "diann",
        "skyline",
        "msfragger",
    ];
    if matches!(
        field,
        "single_cell_isolation_method" | "instrument" | "proteomics_data_acquisition_method"
    ) && analysis_software.iter().any(|x| v.contains(x))
    {
        return true;
    }
    if field == "single_cell_isolation_method" {
        return ![
            "facs",
            "flow cytometry",
            "cellenone",
            "microfluid",
            "laser capture",
            "lcm",
            "manual picking",
            "nanopots",
            "droplet microfluid",
            "acoustic droplet",
        ]
        .iter()
        .any(|x| v.contains(x));
    }
    if field == "proteomics_data_acquisition_method" {
        return ![
            "data-dependent",
            "data dependent",
            "dda",
            "data-independent",
            "data independent",
            "dia",
            "dia-pasef",
            "diapasef",
            "pasef",
            "parallel accumulation",
            "prm",
            "srm",
            "mrm",
            "swath",
        ]
        .iter()
        .any(|x| v.contains(x));
    }
    if field == "carrier_channel" {
        return ![
            "carrier channel",
            "carrier proteome",
            "carrier sample",
            "carrier cells",
        ]
        .iter()
        .any(|x| v.contains(x));
    }
    if field == "reference_channel" {
        return ![
            "reference channel",
            "reference sample",
            "reference proteome",
            "bridge channel",
        ]
        .iter()
        .any(|x| v.contains(x));
    }
    if matches!(field, "fraction_identifier" | "technical_replicate") {
        return !v.chars().all(|c| c.is_ascii_digit());
    }
    false
}

fn field_evidence_block(evidence: &DatasetEvidence, field: &str, max_items: usize) -> String {
    let rows = evidence
        .evidence
        .iter()
        .filter(|e| evidence_relevant_to_field(field, e))
        .take(max_items)
        .map(|e| {
            format!(
                "[{}] {} / {}: {}",
                e.id, e.source_kind, e.source_label, e.text
            )
        })
        .collect::<Vec<_>>();
    if rows.is_empty() {
        "(no field-specific evidence)".to_string()
    } else {
        rows.join("\n")
    }
}

fn normalized_value_match_text(value: &str) -> String {
    value
        .to_ascii_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn recover_value_match_refs(field: &str, value: &str, evidence: &DatasetEvidence) -> Vec<String> {
    if field == "relation_mode" || proposal_value_is_reserved(field, value) {
        return Vec::new();
    }
    let needle = normalized_value_match_text(value);
    if needle.len() < 4 || needle.chars().all(|c| c.is_ascii_digit()) {
        return Vec::new();
    }
    let mut refs = Vec::new();
    for item in &evidence.evidence {
        if !evidence_relevant_to_field(field, item) {
            continue;
        }
        let hay = normalized_value_match_text(&format!("{} {}", item.source_label, item.text));
        if hay.contains(&needle) {
            refs.push(item.id.clone());
            if refs.len() >= 4 {
                break;
            }
        }
    }
    refs
}

fn recover_missing_proposal_refs(
    proposal: &mut SdrfProposal,
    evidence: &DatasetEvidence,
) -> Vec<ValidationIssue> {
    let mut issues = Vec::new();
    macro_rules! recover {
        ($field:literal, $value:expr) => {{
            let needs = proposal
                .evidence_refs
                .get($field)
                .map_or(true, |refs| refs.is_empty());
            if needs {
                let refs = recover_value_match_refs($field, $value, evidence);
                if !refs.is_empty() {
                    proposal.evidence_refs.insert($field.to_string(), refs.clone());
                    issues.push(ValidationIssue {
                        level: "warning".into(),
                        code: "proposal_provenance_recovered_by_exact_value_match".into(),
                        row: 0,
                        column: $field.into(),
                        message: format!(
                            "recovered {} field-specific evidence reference(s) because the concrete proposed value occurs verbatim in relevant source evidence",
                            refs.len()
                        ),
                    });
                }
            }
        }};
    }
    recover!("organism", &proposal.organism);
    recover!("organism_part", &proposal.organism_part);
    recover!("disease", &proposal.disease);
    recover!("cell_type", &proposal.cell_type);
    recover!("sample_type", &proposal.sample_type);
    recover!(
        "single_cell_isolation_method",
        &proposal.single_cell_isolation_method
    );
    recover!("individual", &proposal.individual);
    recover!(
        "sample_preparation_batch",
        &proposal.sample_preparation_batch
    );
    recover!("cells_per_well", &proposal.cells_per_well);
    recover!(
        "proteomics_data_acquisition_method",
        &proposal.proteomics_data_acquisition_method
    );
    recover!("label", &proposal.label);
    recover!("instrument", &proposal.instrument);
    recover!("cleavage_agent_details", &proposal.cleavage_agent_details);
    recover!("carrier_channel", &proposal.carrier_channel);
    recover!("reference_channel", &proposal.reference_channel);
    issues
}

fn repair_proposal_provenance(
    proposal: &mut SdrfProposal,
    evidence: &DatasetEvidence,
) -> Vec<ValidationIssue> {
    let evidence_by_id: BTreeMap<&str, &EvidenceItem> = evidence
        .evidence
        .iter()
        .map(|e| (e.id.as_str(), e))
        .collect();
    let targets = proposal_target_fields(evidence);
    let mut issues = Vec::new();

    for (field, refs) in &mut proposal.evidence_refs {
        let before = refs.clone();
        refs.clear();
        for r in before {
            let Some(item) = evidence_by_id.get(r.as_str()).copied() else {
                issues.push(ValidationIssue {
                    level: "warning".into(),
                    code: "proposal_unknown_evidence_ref_removed".into(),
                    row: 0,
                    column: field.clone(),
                    message: format!("removed unknown model evidence reference {r}"),
                });
                continue;
            };
            if !targets.contains(field) {
                issues.push(ValidationIssue {
                    level: "warning".into(),
                    code: "proposal_ref_removed_for_locked_field".into(),
                    row: 0,
                    column: field.clone(),
                    message: format!("removed evidence reference {r} because the field is already structured/locked by the deposited SDRF"),
                });
                continue;
            }
            if !evidence_relevant_to_field(field, item) {
                issues.push(ValidationIssue {
                    level: "warning".into(),
                    code: "proposal_irrelevant_evidence_ref_removed".into(),
                    row: 0,
                    column: field.clone(),
                    message: format!("removed evidence reference {r} because it is not field-specific support for {field}"),
                });
                continue;
            }
            refs.push(r);
        }
    }

    issues.extend(recover_missing_proposal_refs(proposal, evidence));

    fn repair_field(
        field: &str,
        value: &mut String,
        refs: &BTreeMap<String, Vec<String>>,
        targets: &BTreeSet<String>,
        issues: &mut Vec<ValidationIssue>,
    ) {
        if field != "relation_mode" {
            if let Some(canonical) = canonical_reserved_alias(value) {
                if value.trim().is_empty() {
                    // Empty strings are a routine structured-output omission, not a
                    // scientifically meaningful repair. Normalize silently so repair
                    // counts reflect substantive model/evidence problems.
                    *value = canonical.to_string();
                } else if value.trim() != canonical {
                    let original = value.clone();
                    *value = canonical.to_string();
                    issues.push(ValidationIssue {
                        level: "warning".into(),
                        code: "proposal_reserved_value_normalized".into(),
                        row: 0,
                        column: field.into(),
                        message: format!(
                            "normalized non-canonical missing value '{original}' to '{canonical}'"
                        ),
                    });
                }
                return;
            }
        }
        if proposal_value_is_reserved(field, value) {
            return;
        }
        if !targets.contains(field) {
            let original = value.clone();
            *value = if field == "relation_mode" {
                "uncertain".into()
            } else {
                "not available".into()
            };
            issues.push(ValidationIssue {
                level: "warning".into(),
                code: "proposal_locked_field_discarded".into(),
                row: 0,
                column: field.into(),
                message: format!("discarded model value '{original}' because {field} is already represented by the deposited SDRF"),
            });
            return;
        }
        if obviously_invalid_field_value(field, value) {
            let original = value.clone();
            *value = if field == "relation_mode" {
                "uncertain".into()
            } else {
                "not available".into()
            };
            issues.push(ValidationIssue {
                level: "warning".into(),
                code: "proposal_field_downgraded_semantically_invalid".into(),
                row: 0,
                column: field.into(),
                message: format!("model proposed semantically invalid value '{original}' for {field}; downgraded to '{}'", value),
            });
            return;
        }
        if refs.get(field).map_or(true, |r| r.is_empty()) {
            let original = value.clone();
            *value = if field == "relation_mode" {
                "uncertain".to_string()
            } else {
                "not available".to_string()
            };
            issues.push(ValidationIssue {
                level: "warning".into(),
                code: "proposal_field_downgraded_missing_provenance".into(),
                row: 0,
                column: field.into(),
                message: format!(
                    "model proposed '{original}' without a valid field-specific evidence reference; downgraded to '{}'",
                    value
                ),
            });
        }
    }

    let refs = &proposal.evidence_refs;
    repair_field(
        "relation_mode",
        &mut proposal.relation_mode,
        refs,
        &targets,
        &mut issues,
    );
    repair_field(
        "organism",
        &mut proposal.organism,
        refs,
        &targets,
        &mut issues,
    );
    repair_field(
        "organism_part",
        &mut proposal.organism_part,
        refs,
        &targets,
        &mut issues,
    );
    repair_field(
        "disease",
        &mut proposal.disease,
        refs,
        &targets,
        &mut issues,
    );
    repair_field(
        "cell_type",
        &mut proposal.cell_type,
        refs,
        &targets,
        &mut issues,
    );
    repair_field(
        "sample_type",
        &mut proposal.sample_type,
        refs,
        &targets,
        &mut issues,
    );
    repair_field(
        "single_cell_isolation_method",
        &mut proposal.single_cell_isolation_method,
        refs,
        &targets,
        &mut issues,
    );
    repair_field(
        "individual",
        &mut proposal.individual,
        refs,
        &targets,
        &mut issues,
    );
    repair_field(
        "sample_preparation_batch",
        &mut proposal.sample_preparation_batch,
        refs,
        &targets,
        &mut issues,
    );
    repair_field(
        "cells_per_well",
        &mut proposal.cells_per_well,
        refs,
        &targets,
        &mut issues,
    );
    repair_field(
        "proteomics_data_acquisition_method",
        &mut proposal.proteomics_data_acquisition_method,
        refs,
        &targets,
        &mut issues,
    );
    repair_field("label", &mut proposal.label, refs, &targets, &mut issues);
    repair_field(
        "instrument",
        &mut proposal.instrument,
        refs,
        &targets,
        &mut issues,
    );
    repair_field(
        "cleavage_agent_details",
        &mut proposal.cleavage_agent_details,
        refs,
        &targets,
        &mut issues,
    );
    repair_field(
        "fraction_identifier",
        &mut proposal.fraction_identifier,
        refs,
        &targets,
        &mut issues,
    );
    repair_field(
        "technical_replicate",
        &mut proposal.technical_replicate,
        refs,
        &targets,
        &mut issues,
    );
    repair_field(
        "carrier_channel",
        &mut proposal.carrier_channel,
        refs,
        &targets,
        &mut issues,
    );
    repair_field(
        "reference_channel",
        &mut proposal.reference_channel,
        refs,
        &targets,
        &mut issues,
    );

    if !proposal.factors.is_empty() {
        issues.push(ValidationIssue {
            level: "warning".into(),
            code: "proposal_factors_deferred".into(),
            row: 0,
            column: "factors".into(),
            message: format!(
                "discarded {} model factor proposal(s); per-row factor reconstruction is deferred",
                proposal.factors.len()
            ),
        });
        proposal.factors.clear();
    }

    if !evidence.existing_sdrf_path.is_empty() {
        if let Ok((headers, rows)) =
            read_existing_sdrf_table(Path::new(&evidence.existing_sdrf_path))
        {
            let hint = existing_sdrf_relation_hint(&headers, &rows);
            if hint != "uncertain" && proposal.relation_mode != hint {
                let before = proposal.relation_mode.clone();
                proposal.relation_mode = hint.clone();
                if let Some(item) = evidence.evidence.iter().find(|e| {
                    e.source_kind == "existing_sdrf_structured"
                        && e.source_label.contains("relationship_summary")
                }) {
                    proposal
                        .evidence_refs
                        .insert("relation_mode".into(), vec![item.id.clone()]);
                }
                issues.push(ValidationIssue {
                    level: "warning".into(),
                    code: "proposal_relation_overridden_by_existing_sdrf".into(),
                    row: 0,
                    column: "relation_mode".into(),
                    message: format!("replaced model relation '{before}' with deterministic deposited-SDRF relation '{hint}'"),
                });
            }
        }
    }

    issues
}

fn apply_metadata_scaffold_field(
    field: &str,
    slot: &mut String,
    scaffold: &DeterministicMetadataScaffold,
    proposal_refs: &mut BTreeMap<String, Vec<String>>,
    issues: &mut Vec<ValidationIssue>,
) {
    let Some(value) = scaffold.values.get(field) else {
        return;
    };
    if concrete_proposal_value(slot).is_some() {
        return;
    }
    *slot = value.clone();
    if let Some(refs) = scaffold.evidence_refs.get(field) {
        proposal_refs.insert(field.to_string(), refs.clone());
    }
    issues.push(ValidationIssue {
        level: "warning".into(),
        code: "proposal_field_determined_from_metadata_scaffold".into(),
        row: 0,
        column: field.into(),
        message: format!(
            "deterministic structured/manuscript evidence supplied '{}' for {}",
            value, field
        ),
    });
}

fn apply_deterministic_metadata_scaffold(
    proposal: &mut SdrfProposal,
    evidence: &DatasetEvidence,
) -> Vec<ValidationIssue> {
    let mut issues = Vec::new();
    apply_metadata_scaffold_field(
        "organism",
        &mut proposal.organism,
        &evidence.metadata_scaffold,
        &mut proposal.evidence_refs,
        &mut issues,
    );
    apply_metadata_scaffold_field(
        "instrument",
        &mut proposal.instrument,
        &evidence.metadata_scaffold,
        &mut proposal.evidence_refs,
        &mut issues,
    );
    apply_metadata_scaffold_field(
        "label",
        &mut proposal.label,
        &evidence.metadata_scaffold,
        &mut proposal.evidence_refs,
        &mut issues,
    );
    apply_metadata_scaffold_field(
        "proteomics_data_acquisition_method",
        &mut proposal.proteomics_data_acquisition_method,
        &evidence.metadata_scaffold,
        &mut proposal.evidence_refs,
        &mut issues,
    );
    apply_metadata_scaffold_field(
        "cleavage_agent_details",
        &mut proposal.cleavage_agent_details,
        &evidence.metadata_scaffold,
        &mut proposal.evidence_refs,
        &mut issues,
    );
    apply_metadata_scaffold_field(
        "single_cell_isolation_method",
        &mut proposal.single_cell_isolation_method,
        &evidence.metadata_scaffold,
        &mut proposal.evidence_refs,
        &mut issues,
    );

    for gap in &evidence.metadata_scaffold.template_gaps {
        issues.push(ValidationIssue {
            level: "warning".into(),
            code: "template_vocabulary_gap_supported_method".into(),
            row: 0,
            column: gap.field.clone(),
            message: format!(
                "evidence supports '{}' but it cannot be represented faithfully by the pinned single-cell template vocabulary: {}",
                gap.observed_value, gap.reason
            ),
        });
    }
    issues
}

fn validate_proposal_refs(proposal: &SdrfProposal, evidence: &DatasetEvidence) -> Result<()> {
    let valid: BTreeSet<&str> = evidence.evidence.iter().map(|e| e.id.as_str()).collect();
    for (field, refs) in &proposal.evidence_refs {
        for r in refs {
            if !valid.contains(r.as_str()) {
                bail!("proposal field {field} cites unknown evidence ref {r}");
            }
        }
    }

    let field_values = [
        ("relation_mode", proposal.relation_mode.as_str()),
        ("organism", proposal.organism.as_str()),
        ("organism_part", proposal.organism_part.as_str()),
        ("disease", proposal.disease.as_str()),
        ("cell_type", proposal.cell_type.as_str()),
        ("sample_type", proposal.sample_type.as_str()),
        (
            "single_cell_isolation_method",
            proposal.single_cell_isolation_method.as_str(),
        ),
        ("individual", proposal.individual.as_str()),
        (
            "sample_preparation_batch",
            proposal.sample_preparation_batch.as_str(),
        ),
        ("cells_per_well", proposal.cells_per_well.as_str()),
        (
            "proteomics_data_acquisition_method",
            proposal.proteomics_data_acquisition_method.as_str(),
        ),
        ("label", proposal.label.as_str()),
        ("instrument", proposal.instrument.as_str()),
        (
            "cleavage_agent_details",
            proposal.cleavage_agent_details.as_str(),
        ),
        ("fraction_identifier", proposal.fraction_identifier.as_str()),
        ("technical_replicate", proposal.technical_replicate.as_str()),
        ("carrier_channel", proposal.carrier_channel.as_str()),
        ("reference_channel", proposal.reference_channel.as_str()),
    ];
    for (field, value) in field_values {
        let lower = value.trim().to_ascii_lowercase();
        let reserved = lower.is_empty()
            || matches!(
                lower.as_str(),
                "not available" | "not applicable" | "pooled"
            )
            || (field == "relation_mode" && lower == "uncertain");
        if !reserved
            && proposal
                .evidence_refs
                .get(field)
                .map_or(true, |refs| refs.is_empty())
        {
            bail!("proposal field {field} has a concrete value but no evidence_refs: {value}");
        }
    }

    for factor in &proposal.factors {
        for r in &factor.evidence_refs {
            if !valid.contains(r.as_str()) {
                bail!("factor {} cites unknown evidence ref {r}", factor.name);
            }
        }
        let lower = factor.value.trim().to_ascii_lowercase();
        if !factor.name.trim().is_empty()
            && !matches!(
                lower.as_str(),
                "" | "not available" | "not applicable" | "pooled"
            )
            && factor.evidence_refs.is_empty()
        {
            bail!(
                "factor {} has a concrete value but no evidence_refs",
                factor.name
            );
        }
    }
    Ok(())
}

fn reserved_or(value: &str, fallback: &str) -> String {
    let v = value.trim();
    if v.is_empty() {
        fallback.to_string()
    } else {
        v.to_string()
    }
}

fn safe_identifier_from_file(file_name: &str) -> String {
    let mut base = file_name.to_string();
    for ext in [
        ".raw", ".wiff2", ".wiff", ".mzML", ".mzml", ".mzXML", ".mzxml", ".d",
    ] {
        if base
            .to_ascii_lowercase()
            .ends_with(&ext.to_ascii_lowercase())
        {
            let new_len = base.len().saturating_sub(ext.len());
            base.truncate(new_len);
            break;
        }
    }
    let re = Regex::new(r"[^A-Za-z0-9_.-]+").unwrap();
    let cleaned = re
        .replace_all(base.trim(), "_")
        .trim_matches('_')
        .to_string();
    if cleaned.is_empty() {
        "cell".to_string()
    } else {
        cleaned
    }
}

fn headers_for(_proposal: &SdrfProposal, include_file_uri: bool) -> Vec<String> {
    let mut h = vec![
        "source name".to_string(),
        "characteristics[organism]".to_string(),
        "characteristics[organism part]".to_string(),
        "characteristics[disease]".to_string(),
        "characteristics[cell type]".to_string(),
        "characteristics[biological replicate]".to_string(),
        "assay name".to_string(),
        "technology type".to_string(),
        "comment[proteomics data acquisition method]".to_string(),
        "comment[label]".to_string(),
        "comment[instrument]".to_string(),
        "comment[cleavage agent details]".to_string(),
        "comment[fraction identifier]".to_string(),
        "comment[technical replicate]".to_string(),
        "comment[data file]".to_string(),
    ];
    if include_file_uri {
        h.push("comment[file uri]".to_string());
    }
    h.extend([
        SC_SAMPLE_TYPE.to_string(),
        SC_ISOLATION_METHOD.to_string(),
        SC_CELL_IDENTIFIER.to_string(),
        SC_INDIVIDUAL.to_string(),
        SC_PREP_BATCH.to_string(),
        SC_CELLS_PER_WELL.to_string(),
        SC_CARRIER_CHANNEL.to_string(),
        SC_REFERENCE_CHANNEL.to_string(),
        "comment[sdrf version]".to_string(),
        "comment[sdrf template]".to_string(),
        "comment[sdrf annotation tool]".to_string(),
    ]);
    // Factor candidates are retained in the Ollama proposal for human review,
    // but v0.1 does not serialize them because per-row factor assignments are
    // not yet reconstructed safely.
    h
}

fn is_missing_cell_value(value: &str) -> bool {
    let v = value.trim();
    v.is_empty() || v.eq_ignore_ascii_case("not available")
}

fn concrete_proposal_value(value: &str) -> Option<String> {
    if proposal_value_is_reserved("", value) {
        return None;
    }
    let v = value.trim();
    (!v.is_empty()).then(|| v.to_string())
}

fn append_header(headers: &mut Vec<String>, rows: &mut [Vec<String>], name: &str) -> usize {
    if let Some(i) = header_first_index(headers, name) {
        return i;
    }
    headers.push(name.to_string());
    for row in rows.iter_mut() {
        row.push(String::new());
    }
    headers.len() - 1
}

fn set_missing(row: &mut [String], idx: Option<usize>, value: Option<String>) {
    let (Some(j), Some(value)) = (idx, value) else {
        return;
    };
    if j < row.len() && is_missing_cell_value(&row[j]) {
        row[j] = value;
    }
}

fn data_file_basename(value: &str) -> String {
    let trimmed = value.trim().trim_matches('"');
    let tail = trimmed
        .rsplit(|c| c == '/' || c == '\\')
        .next()
        .unwrap_or(trimmed);
    tail.to_string()
}

fn merge_existing_sdrf(
    proposal: &SdrfProposal,
    evidence: &DatasetEvidence,
) -> Result<(Vec<String>, Vec<Vec<String>>, String)> {
    let path = Path::new(&evidence.existing_sdrf_path);
    let (mut headers, mut rows) = read_existing_sdrf_table(path)?;
    if rows.is_empty() {
        bail!("existing SDRF has no data rows: {}", path.display());
    }
    let original_header_count = headers.len();

    for required in [
        "source name",
        "characteristics[organism]",
        "characteristics[organism part]",
        "characteristics[disease]",
        "characteristics[cell type]",
        "characteristics[biological replicate]",
        "assay name",
        "technology type",
        "comment[proteomics data acquisition method]",
        "comment[label]",
        "comment[instrument]",
        "comment[cleavage agent details]",
        "comment[fraction identifier]",
        "comment[technical replicate]",
        "comment[data file]",
        SC_SAMPLE_TYPE,
        SC_ISOLATION_METHOD,
        SC_CELL_IDENTIFIER,
        SC_INDIVIDUAL,
        SC_PREP_BATCH,
        SC_CELLS_PER_WELL,
        SC_CARRIER_CHANNEL,
        SC_REFERENCE_CHANNEL,
        "comment[sdrf version]",
        "comment[sdrf annotation tool]",
    ] {
        append_header(&mut headers, &mut rows, required);
    }

    let existing_has_sc_template = headers.iter().enumerate().any(|(j, h)| {
        h == "comment[sdrf template]"
            && rows.iter().any(|r| {
                r.get(j)
                    .map_or(false, |v| v.to_ascii_lowercase().contains("single-cell"))
            })
    });
    let sc_template_idx = if existing_has_sc_template {
        None
    } else {
        headers.push("comment[sdrf template]".to_string());
        for row in rows.iter_mut() {
            row.push(String::new());
        }
        Some(headers.len() - 1)
    };

    let relation = proposal.relation_mode.as_str();
    let raw_uri: BTreeMap<String, String> = evidence
        .raw_files
        .iter()
        .map(|f| (f.file_name.clone(), f.file_uri.clone()))
        .collect();
    let include_uri = raw_uri.values().any(|x| !x.is_empty());
    let uri_idx = if include_uri {
        Some(append_header(&mut headers, &mut rows, "comment[file uri]"))
    } else {
        None
    };
    let idx = |name: &str| header_first_index(&headers, name);

    let source_idx = idx("source name");
    let data_idx = idx("comment[data file]");
    let sample_type_idx = idx(SC_SAMPLE_TYPE);
    let cell_idx = idx(SC_CELL_IDENTIFIER);
    let isolation_idx = idx(SC_ISOLATION_METHOD);
    let cells_idx = idx(SC_CELLS_PER_WELL);

    for row in rows.iter_mut() {
        set_missing(
            row,
            idx("characteristics[organism]"),
            concrete_proposal_value(&proposal.organism),
        );
        set_missing(
            row,
            idx("characteristics[organism part]"),
            concrete_proposal_value(&proposal.organism_part),
        );
        set_missing(
            row,
            idx("characteristics[disease]"),
            concrete_proposal_value(&proposal.disease),
        );
        set_missing(
            row,
            idx("characteristics[cell type]"),
            concrete_proposal_value(&proposal.cell_type),
        );
        set_missing(
            row,
            idx("comment[proteomics data acquisition method]"),
            concrete_proposal_value(&proposal.proteomics_data_acquisition_method),
        );
        set_missing(
            row,
            idx("comment[label]"),
            concrete_proposal_value(&proposal.label),
        );
        set_missing(
            row,
            idx("comment[instrument]"),
            concrete_proposal_value(&proposal.instrument),
        );
        set_missing(
            row,
            idx("comment[cleavage agent details]"),
            concrete_proposal_value(&proposal.cleavage_agent_details),
        );
        if proposal
            .fraction_identifier
            .trim()
            .chars()
            .all(|c| c.is_ascii_digit())
        {
            set_missing(
                row,
                idx("comment[fraction identifier]"),
                Some(proposal.fraction_identifier.trim().to_string()),
            );
        }
        if proposal
            .technical_replicate
            .trim()
            .chars()
            .all(|c| c.is_ascii_digit())
        {
            set_missing(
                row,
                idx("comment[technical replicate]"),
                Some(proposal.technical_replicate.trim().to_string()),
            );
        }
        set_missing(
            row,
            idx(SC_INDIVIDUAL),
            concrete_proposal_value(&proposal.individual),
        );
        set_missing(
            row,
            idx(SC_PREP_BATCH),
            concrete_proposal_value(&proposal.sample_preparation_batch),
        );
        set_missing(
            row,
            idx(SC_CARRIER_CHANNEL),
            concrete_proposal_value(&proposal.carrier_channel),
        );
        set_missing(
            row,
            idx(SC_REFERENCE_CHANNEL),
            concrete_proposal_value(&proposal.reference_channel),
        );

        let current_sample_type = sample_type_idx
            .and_then(|j| row.get(j))
            .map(|x| x.trim().to_ascii_lowercase())
            .unwrap_or_default();
        if current_sample_type.is_empty() || current_sample_type == "not available" {
            if relation == "one_cell_per_data_file" {
                if let Some(j) = sample_type_idx {
                    row[j] = "single cell".into();
                }
            }
        }
        let sample_type = sample_type_idx
            .and_then(|j| row.get(j))
            .map(|x| x.trim().to_ascii_lowercase())
            .unwrap_or_default();
        if let Some(j) = isolation_idx {
            if is_missing_cell_value(&row[j]) {
                if [
                    "carrier",
                    "reference",
                    "empty",
                    "bulk control",
                    "negative control",
                ]
                .contains(&sample_type.as_str())
                {
                    row[j] = "not applicable".into();
                } else if let Some(v) =
                    concrete_proposal_value(&proposal.single_cell_isolation_method)
                {
                    row[j] = v;
                }
            }
        }
        if let Some(j) = cells_idx {
            if is_missing_cell_value(&row[j]) && sample_type == "single cell" {
                row[j] = "1".into();
            }
        }
        if let Some(j) = cell_idx {
            if is_missing_cell_value(&row[j]) {
                if sample_type == "carrier" {
                    row[j] = "carrier".into();
                } else if sample_type == "reference" {
                    row[j] = "reference".into();
                } else if sample_type == "empty" {
                    row[j] = "empty".into();
                } else if ["bulk control", "negative control"].contains(&sample_type.as_str()) {
                    row[j] = "not applicable".into();
                } else if relation == "one_cell_per_data_file" || sample_type == "single cell" {
                    if let Some(src_j) = source_idx {
                        let src = row.get(src_j).cloned().unwrap_or_default();
                        if !src.trim().is_empty() && !src.eq_ignore_ascii_case("not available") {
                            row[j] = safe_identifier_from_file(&src);
                        }
                    }
                }
            }
        }
        if let Some(j) = idx("technology type") {
            if is_missing_cell_value(&row[j]) {
                row[j] = "proteomic profiling by mass spectrometry".into();
            }
        }
        if let Some(j) = idx("comment[sdrf version]") {
            row[j] = SDRF_SPEC_VERSION.into();
        }
        if let Some(j) = idx("comment[sdrf annotation tool]") {
            row[j] = format!("pride-scp-sdrf {GENERATOR_VERSION}");
        }
        if let Some(j) = sc_template_idx {
            row[j] = format!("single-cell v{SINGLE_CELL_TEMPLATE_VERSION}");
        }
        if let (Some(data_j), Some(uri_j)) = (data_idx, uri_idx) {
            let file = row
                .get(data_j)
                .map(|x| data_file_basename(x))
                .unwrap_or_default();
            if is_missing_cell_value(&row[uri_j]) {
                if let Some(uri) = raw_uri.get(&file).filter(|x| !x.is_empty()) {
                    row[uri_j] = uri.clone();
                }
            }
        }
    }

    let mode = if headers.len() > original_header_count {
        "enriched_existing_sdrf"
    } else {
        "validated_existing_sdrf"
    };
    Ok((headers, rows, mode.into()))
}

fn draft_rows_from_explicit_mappings(
    proposal: &SdrfProposal,
    evidence: &DatasetEvidence,
    mappings: &[ExplicitRowMapping],
) -> Result<(Vec<String>, Vec<Vec<String>>, String)> {
    if mappings.is_empty() {
        bail!("explicit row-mapping generation requires at least one mapping row");
    }
    let include_uri = evidence.raw_files.iter().any(|f| !f.file_uri.is_empty());
    let headers = headers_for(proposal, include_uri);
    let idx: HashMap<&str, usize> = headers
        .iter()
        .enumerate()
        .map(|(i, h)| (h.as_str(), i))
        .collect();
    let set = |row: &mut Vec<String>, name: &str, value: String| {
        if let Some(&j) = idx.get(name) {
            row[j] = value;
        }
    };
    let mut rows = Vec::with_capacity(mappings.len());
    for mapping in mappings {
        let file = evidence
            .raw_files
            .iter()
            .find(|f| f.file_name.eq_ignore_ascii_case(mapping.raw_file.trim()))
            .ok_or_else(|| {
                anyhow!(
                    "explicit row mapping references RAW file absent from PRIDE snapshot: {}",
                    mapping.raw_file
                )
            })?;
        let mut row = vec!["not available".to_string(); headers.len()];
        let stem = safe_identifier_from_file(&file.file_name);
        set(&mut row, "source name", mapping.source_name.clone());
        set(
            &mut row,
            "characteristics[organism]",
            reserved_or(&proposal.organism, "not available"),
        );
        set(
            &mut row,
            "characteristics[organism part]",
            reserved_or(&proposal.organism_part, "not available"),
        );
        set(
            &mut row,
            "characteristics[disease]",
            reserved_or(&proposal.disease, "not available"),
        );
        set(
            &mut row,
            "characteristics[cell type]",
            reserved_or(&proposal.cell_type, "not available"),
        );
        set(
            &mut row,
            "characteristics[biological replicate]",
            mapping.biological_replicate.clone(),
        );
        set(&mut row, "assay name", stem);
        set(
            &mut row,
            "technology type",
            "proteomic profiling by mass spectrometry".to_string(),
        );
        set(
            &mut row,
            "comment[proteomics data acquisition method]",
            reserved_or(
                &proposal.proteomics_data_acquisition_method,
                "not available",
            ),
        );
        set(&mut row, "comment[label]", mapping.label.clone());
        set(
            &mut row,
            "comment[instrument]",
            reserved_or(&proposal.instrument, "not available"),
        );
        set(
            &mut row,
            "comment[cleavage agent details]",
            reserved_or(&proposal.cleavage_agent_details, "not available"),
        );
        let fraction_value = proposal.fraction_identifier.trim();
        let fraction_identifier =
            if !fraction_value.is_empty() && fraction_value.chars().all(|c| c.is_ascii_digit()) {
                fraction_value.to_string()
            } else {
                "1".to_string()
            };
        set(
            &mut row,
            "comment[fraction identifier]",
            fraction_identifier,
        );
        set(
            &mut row,
            "comment[technical replicate]",
            mapping.technical_replicate.clone(),
        );
        set(&mut row, "comment[data file]", file.file_name.clone());
        if include_uri {
            set(
                &mut row,
                "comment[file uri]",
                reserved_or(&file.file_uri, "not available"),
            );
        }
        set(&mut row, SC_SAMPLE_TYPE, mapping.sample_type.clone());
        set(
            &mut row,
            SC_ISOLATION_METHOD,
            reserved_or(&proposal.single_cell_isolation_method, "not available"),
        );
        set(
            &mut row,
            SC_CELL_IDENTIFIER,
            mapping.cell_identifier.clone(),
        );
        set(
            &mut row,
            SC_INDIVIDUAL,
            reserved_or(&proposal.individual, "not available"),
        );
        set(
            &mut row,
            SC_PREP_BATCH,
            reserved_or(&proposal.sample_preparation_batch, "not available"),
        );
        set(&mut row, SC_CELLS_PER_WELL, mapping.cells_per_well.clone());
        set(
            &mut row,
            SC_CARRIER_CHANNEL,
            mapping.carrier_channel.clone(),
        );
        set(
            &mut row,
            SC_REFERENCE_CHANNEL,
            reserved_or(&mapping.reference_channel, "not applicable"),
        );
        set(
            &mut row,
            "comment[sdrf version]",
            SDRF_SPEC_VERSION.to_string(),
        );
        set(
            &mut row,
            "comment[sdrf template]",
            format!("single-cell v{SINGLE_CELL_TEMPLATE_VERSION}"),
        );
        set(
            &mut row,
            "comment[sdrf annotation tool]",
            GENERATOR_VERSION.to_string(),
        );
        rows.push(row);
    }
    Ok((
        headers,
        rows,
        "generated_source_grounded_explicit_row_mapping".to_string(),
    ))
}

fn draft_rows_with_explicit_mappings(
    proposal: &SdrfProposal,
    evidence: &DatasetEvidence,
    explicit_mappings: &[ExplicitRowMapping],
) -> Result<(Vec<String>, Vec<Vec<String>>, String)> {
    // Existing SDRF content is authoritative row-relationship evidence. Never
    // silently fall back to a filename-derived skeleton if an existing SDRF was
    // detected: that can destroy a valid sample↔file/channel mapping while still
    // looking like a successful enrichment run. Surface the parse/merge error.
    if !evidence.existing_sdrf_path.is_empty() {
        return merge_existing_sdrf(proposal, evidence).with_context(|| {
            format!(
                "preserve/enrich existing SDRF {}",
                evidence.existing_sdrf_path
            )
        });
    }
    if !explicit_mappings.is_empty() {
        return draft_rows_from_explicit_mappings(proposal, evidence, explicit_mappings);
    }
    let include_uri = evidence.raw_files.iter().any(|f| !f.file_uri.is_empty());
    let headers = headers_for(proposal, include_uri);
    let idx: HashMap<&str, usize> = headers
        .iter()
        .enumerate()
        .map(|(i, h)| (h.as_str(), i))
        .collect();
    let mut rows = Vec::new();
    let one_per_file = proposal.relation_mode == "one_cell_per_data_file";
    let mode = if one_per_file {
        "generated_file_role_aware_one_row_per_raw_file"
    } else if evidence.study_design.repository_file_mode == "generic_archives_only" {
        "generated_archive_container_skeleton"
    } else {
        "generated_skeleton_unresolved_mapping"
    };
    for (i, file) in evidence.raw_files.iter().enumerate() {
        let mut row = vec!["not available".to_string(); headers.len()];
        let stem = safe_identifier_from_file(&file.file_name);
        let inferred_role = if one_per_file {
            raw_file_role(&file.file_name)
        } else {
            RawFileRole::Unknown
        };
        let effective_role = if one_per_file && inferred_role == RawFileRole::Unknown {
            RawFileRole::SingleCell
        } else {
            inferred_role
        };
        let source = if one_per_file {
            stem.clone()
        } else {
            format!("run_{:04}", i + 1)
        };
        let assay = stem.clone();
        let set = |row: &mut Vec<String>, name: &str, value: String| {
            if let Some(&j) = idx.get(name) {
                row[j] = value;
            }
        };
        set(&mut row, "source name", source);
        set(
            &mut row,
            "characteristics[organism]",
            reserved_or(&proposal.organism, "not available"),
        );
        set(
            &mut row,
            "characteristics[organism part]",
            reserved_or(&proposal.organism_part, "not available"),
        );
        set(
            &mut row,
            "characteristics[disease]",
            reserved_or(&proposal.disease, "not available"),
        );
        set(
            &mut row,
            "characteristics[cell type]",
            reserved_or(&proposal.cell_type, "not available"),
        );
        set(
            &mut row,
            "characteristics[biological replicate]",
            "not available".to_string(),
        );
        set(&mut row, "assay name", assay);
        set(
            &mut row,
            "technology type",
            "proteomic profiling by mass spectrometry".to_string(),
        );
        set(
            &mut row,
            "comment[proteomics data acquisition method]",
            reserved_or(
                &proposal.proteomics_data_acquisition_method,
                "not available",
            ),
        );
        set(
            &mut row,
            "comment[label]",
            reserved_or(&proposal.label, "not available"),
        );
        set(
            &mut row,
            "comment[instrument]",
            reserved_or(&proposal.instrument, "not available"),
        );
        set(
            &mut row,
            "comment[cleavage agent details]",
            reserved_or(&proposal.cleavage_agent_details, "not available"),
        );
        let fraction_value = proposal.fraction_identifier.trim();
        let fraction_identifier =
            if !fraction_value.is_empty() && fraction_value.chars().all(|c| c.is_ascii_digit()) {
                fraction_value.to_string()
            } else if one_per_file {
                "1".to_string()
            } else {
                "not available".to_string()
            };
        let technical_value = proposal.technical_replicate.trim();
        let technical_replicate =
            if !technical_value.is_empty() && technical_value.chars().all(|c| c.is_ascii_digit()) {
                technical_value.to_string()
            } else if one_per_file {
                "1".to_string()
            } else {
                "not available".to_string()
            };
        set(
            &mut row,
            "comment[fraction identifier]",
            fraction_identifier,
        );
        set(
            &mut row,
            "comment[technical replicate]",
            technical_replicate,
        );
        set(&mut row, "comment[data file]", file.file_name.clone());
        if include_uri {
            set(
                &mut row,
                "comment[file uri]",
                reserved_or(&file.file_uri, "not available"),
            );
        }
        let (sample_type, isolation, cell_identifier, cells_per_well) = if one_per_file {
            match effective_role {
                RawFileRole::SingleCell | RawFileRole::Unknown => (
                    "single cell".to_string(),
                    reserved_or(&proposal.single_cell_isolation_method, "not available"),
                    stem.clone(),
                    "1".to_string(),
                ),
                RawFileRole::FewCell(n) => (
                    "study sample".to_string(),
                    concrete_proposal_value(&proposal.single_cell_isolation_method)
                        .unwrap_or_else(|| "not applicable".to_string()),
                    "not applicable".to_string(),
                    n.to_string(),
                ),
                RawFileRole::Blank => (
                    "empty".to_string(),
                    "not applicable".to_string(),
                    "empty".to_string(),
                    "not applicable".to_string(),
                ),
                RawFileRole::QualityControl => (
                    "quality control sample".to_string(),
                    "not applicable".to_string(),
                    "not applicable".to_string(),
                    "not applicable".to_string(),
                ),
                RawFileRole::Bulk => (
                    "bulk control".to_string(),
                    "not applicable".to_string(),
                    "not applicable".to_string(),
                    "not applicable".to_string(),
                ),
            }
        } else {
            (
                reserved_or(&proposal.sample_type, "not available"),
                reserved_or(&proposal.single_cell_isolation_method, "not available"),
                "not available".to_string(),
                reserved_or(&proposal.cells_per_well, "not available"),
            )
        };
        set(&mut row, SC_SAMPLE_TYPE, sample_type);
        set(&mut row, SC_ISOLATION_METHOD, isolation);
        set(&mut row, SC_CELL_IDENTIFIER, cell_identifier);
        set(
            &mut row,
            SC_INDIVIDUAL,
            reserved_or(&proposal.individual, "not available"),
        );
        set(
            &mut row,
            SC_PREP_BATCH,
            reserved_or(&proposal.sample_preparation_batch, "not available"),
        );
        set(&mut row, SC_CELLS_PER_WELL, cells_per_well);
        set(
            &mut row,
            SC_CARRIER_CHANNEL,
            reserved_or(&proposal.carrier_channel, "not applicable"),
        );
        set(
            &mut row,
            SC_REFERENCE_CHANNEL,
            reserved_or(&proposal.reference_channel, "not applicable"),
        );
        set(
            &mut row,
            "comment[sdrf version]",
            SDRF_SPEC_VERSION.to_string(),
        );
        set(
            &mut row,
            "comment[sdrf template]",
            format!("single-cell v{SINGLE_CELL_TEMPLATE_VERSION}"),
        );
        set(
            &mut row,
            "comment[sdrf annotation tool]",
            GENERATOR_VERSION.to_string(),
        );
        rows.push(row);
    }
    Ok((headers, rows, mode.to_string()))
}

fn draft_rows(
    proposal: &SdrfProposal,
    evidence: &DatasetEvidence,
) -> Result<(Vec<String>, Vec<Vec<String>>, String)> {
    draft_rows_with_explicit_mappings(proposal, evidence, &[])
}

fn normalized_data_file_aliases(value: &str) -> BTreeSet<String> {
    let mut aliases = BTreeSet::new();
    let base = data_file_basename(value)
        .replace("%20", " ")
        .trim()
        .to_ascii_lowercase();
    if base.is_empty() {
        return aliases;
    }
    aliases.insert(base.clone());
    // Repositories often expose a logical vendor entity (especially Bruker .d)
    // as an archive. Treat the archive wrapper as a linkage alias, but retain the
    // exact name in audit output.
    for suffix in [".tar.gz", ".tgz", ".zip", ".tar"] {
        if let Some(stripped) = base.strip_suffix(suffix) {
            if !stripped.is_empty() {
                aliases.insert(stripped.to_string());
            }
        }
    }
    aliases
}

fn raw_inventory_aliases(raw_files: &[RawFile]) -> BTreeSet<String> {
    raw_files
        .iter()
        .flat_map(|f| normalized_data_file_aliases(&f.file_name))
        .collect()
}

fn load_explicit_row_mappings(
    path: &Path,
    accession: &str,
    raw_files: &[RawFile],
) -> Result<Vec<ExplicitRowMapping>> {
    let mut reader = ReaderBuilder::new()
        .delimiter(b'\t')
        .from_path(path)
        .with_context(|| format!("read explicit row-mapping manifest {}", path.display()))?;
    let raw_inventory: BTreeSet<String> = raw_files
        .iter()
        .map(|f| f.file_name.trim().to_ascii_lowercase())
        .collect();
    let cell_id = Regex::new(r"^[A-Za-z0-9_.-]+$").unwrap();
    let mut out = Vec::new();
    let mut seen_raw = BTreeSet::new();
    for row in reader.deserialize::<ExplicitRowMapping>() {
        let mut row =
            row.with_context(|| format!("parse explicit row-mapping manifest {}", path.display()))?;
        let Some(row_accession) = norm_accession(&row.accession) else {
            bail!(
                "explicit row mapping contains invalid accession: {}",
                row.accession
            );
        };
        if row_accession != accession {
            continue;
        }
        row.accession = row_accession;
        let raw_key = row.raw_file.trim().to_ascii_lowercase();
        if !raw_inventory.contains(&raw_key) {
            bail!(
                "explicit row mapping {} references RAW file absent from snapshot: {}",
                accession,
                row.raw_file
            );
        }
        if !seen_raw.insert(raw_key) {
            bail!(
                "explicit row mapping {} assigns more than one biological row to RAW {} under the single-analytical-channel contract",
                accession,
                row.raw_file
            );
        }
        if row.mapping_confidence.trim() != "high" {
            bail!(
                "explicit row mapping {} RAW {} is not high confidence: {}",
                accession,
                row.raw_file,
                row.mapping_confidence
            );
        }
        if !["exact_raw_name", "date_sc_run_key"].contains(&row.mapping_key.trim()) {
            bail!(
                "explicit row mapping {} RAW {} uses unsupported source key: {}",
                accession,
                row.raw_file,
                row.mapping_key
            );
        }
        if !cell_id.is_match(row.source_name.trim())
            || !cell_id.is_match(row.cell_identifier.trim())
        {
            bail!(
                "explicit row mapping {} RAW {} has non-template-safe source/cell identifier",
                accession,
                row.raw_file
            );
        }
        for (field, value) in [
            ("biological_replicate", row.biological_replicate.as_str()),
            ("technical_replicate", row.technical_replicate.as_str()),
            ("cells_per_well", row.cells_per_well.as_str()),
        ] {
            if value.trim().is_empty() || !value.trim().chars().all(|c| c.is_ascii_digit()) {
                bail!(
                    "explicit row mapping {} RAW {} requires numeric {}: {}",
                    accession,
                    row.raw_file,
                    field,
                    value
                );
            }
        }
        if row.sample_type.trim() != "single cell" {
            bail!(
                "explicit row mapping {} RAW {} is outside the narrow single-cell manifest contract: sample_type={}",
                accession,
                row.raw_file,
                row.sample_type
            );
        }
        if row.label.trim().is_empty() || row.carrier_channel.trim().is_empty() {
            bail!(
                "explicit row mapping {} RAW {} requires explicit analytical label and carrier channel",
                accession,
                row.raw_file
            );
        }
        if row.design_source.trim().is_empty() || row.design_ref.trim().is_empty() {
            bail!(
                "explicit row mapping {} RAW {} requires source provenance",
                accession,
                row.raw_file
            );
        }
        out.push(row);
    }
    Ok(out)
}

fn data_file_linkage_stats(
    headers: &[String],
    rows: &[Vec<String>],
    raw_files: &[RawFile],
) -> DataFileLinkageStats {
    let mut stats = DataFileLinkageStats {
        snapshot_raw_files: raw_files.len(),
        ..Default::default()
    };
    let Some(data_idx) = header_first_index(headers, "comment[data file]") else {
        stats.status = "missing_data_file_column".into();
        return stats;
    };
    let inventory = raw_inventory_aliases(raw_files);
    let mut unique = BTreeSet::new();
    let mut matched = BTreeSet::new();
    let mut unmatched = BTreeSet::new();
    for row in rows {
        let Some(value) = row.get(data_idx) else {
            continue;
        };
        let value = value.trim();
        if value.is_empty() || value.eq_ignore_ascii_case("not available") {
            continue;
        }
        let base = data_file_basename(value).to_ascii_lowercase();
        unique.insert(base.clone());
        let aliases = normalized_data_file_aliases(value);
        let is_match = aliases.iter().any(|x| inventory.contains(x));
        if is_match {
            matched.insert(base);
            stats.matched_rows += 1;
        } else {
            unmatched.insert(base);
            stats.unmatched_rows += 1;
        }
    }
    stats.sdrf_unique_data_files = unique.len();
    stats.matched_unique_data_files = matched.len();
    stats.unmatched_unique_data_files = unmatched.len();
    stats.status = if raw_files.is_empty() {
        "unavailable".into()
    } else if stats.sdrf_unique_data_files == 0 {
        "no_sdrf_data_files".into()
    } else if stats.unmatched_unique_data_files == 0 {
        "complete".into()
    } else if stats.matched_unique_data_files == 0 {
        "none".into()
    } else {
        "partial".into()
    };
    stats
}

fn row_explicit_non_single_cell_role(index: &HashMap<&str, usize>, row: &[String]) -> bool {
    let sample_type = index
        .get(SC_SAMPLE_TYPE)
        .and_then(|&j| row.get(j))
        .map(|v| v.trim().to_ascii_lowercase())
        .unwrap_or_default();
    if [
        "reference",
        "bridge",
        "carrier",
        "negative control",
        "positive control",
        "calibrator",
        "plate control",
        "quality control sample",
        "empty",
        "bulk control",
        "pooled",
        "not applicable",
    ]
    .contains(&sample_type.as_str())
    {
        return true;
    }
    if let Some(value) = index
        .get(SC_CELLS_PER_WELL)
        .and_then(|&j| row.get(j))
        .and_then(|v| v.trim().parse::<usize>().ok())
    {
        if value > 1 {
            return true;
        }
    }
    index
        .get("characteristics[pooled sample]")
        .and_then(|&j| row.get(j))
        .map(|v| {
            let low = v.trim().to_ascii_lowercase();
            low == "pooled" || low.starts_with("sn=")
        })
        .unwrap_or(false)
}

fn validate_draft_with_policy(
    headers: &[String],
    rows: &[Vec<String>],
    evidence: &DatasetEvidence,
    data_file_policy: DataFileValidationPolicy,
) -> Vec<ValidationIssue> {
    let mut issues = Vec::new();
    if rows.is_empty() {
        issues.push(ValidationIssue {
            level: "error".into(),
            code: "no_data_rows".into(),
            row: 0,
            column: String::new(),
            message: "SDRF contains a header but no sample/data rows".into(),
        });
    }
    let required = [
        "source name",
        "characteristics[organism]",
        "assay name",
        "technology type",
        "comment[proteomics data acquisition method]",
        "comment[label]",
        "comment[instrument]",
        "comment[cleavage agent details]",
        "comment[fraction identifier]",
        "comment[technical replicate]",
        "comment[data file]",
        SC_ISOLATION_METHOD,
        SC_CELL_IDENTIFIER,
    ];
    let index: HashMap<&str, usize> = headers
        .iter()
        .enumerate()
        .map(|(i, x)| (x.as_str(), i))
        .collect();
    for h in &required {
        if !index.contains_key(h) {
            issues.push(ValidationIssue {
                level: "error".into(),
                code: "missing_required_column".into(),
                row: 0,
                column: (*h).into(),
                message: format!("required SDRF/template column missing: {h}"),
            });
        }
    }
    for h in headers {
        if h != &h.to_ascii_lowercase() {
            issues.push(ValidationIssue {
                level: "error".into(),
                code: "header_not_lowercase".into(),
                row: 0,
                column: h.clone(),
                message: "SDRF headers are case-sensitive and must be lowercase".into(),
            });
        }
    }
    let valid_files = raw_inventory_aliases(&evidence.raw_files);
    let cell_id =
        Regex::new(r"^[A-Za-z0-9_.-]+$|^(?:carrier|reference|empty|not applicable)$").unwrap();
    let sample_types = [
        "study sample",
        "single cell",
        "reference",
        "bridge",
        "carrier",
        "negative control",
        "positive control",
        "calibrator",
        "plate control",
        "quality control sample",
        "empty",
        "bulk control",
        "not applicable",
        "not available",
    ];
    for (ri, row) in rows.iter().enumerate() {
        if row.len() != headers.len() {
            issues.push(ValidationIssue {
                level: "error".into(),
                code: "row_width_mismatch".into(),
                row: ri + 1,
                column: "".into(),
                message: format!("row has {} fields; expected {}", row.len(), headers.len()),
            });
            continue;
        }
        let non_single_role = row_explicit_non_single_cell_role(&index, row);
        for h in &required {
            let Some(&j) = index.get(h) else { continue };
            let v = row[j].trim();
            if v.is_empty() {
                issues.push(ValidationIssue {
                    level: "error".into(),
                    code: "required_value_blank".into(),
                    row: ri + 1,
                    column: (*h).into(),
                    message: "required value is blank".into(),
                });
            }
        }
        if let Some(&j) = index.get(SC_ISOLATION_METHOD) {
            let v = row[j].trim();
            if v.eq_ignore_ascii_case("not available") {
                let level = if non_single_role { "warning" } else { "error" };
                issues.push(ValidationIssue { level: level.into(), code: "single_cell_isolation_unresolved".into(), row: ri + 1, column: SC_ISOLATION_METHOD.into(), message: if non_single_role { "non-single-cell/reference/control row has no applicable isolation method; preserve for external validator review".into() } else { "single-cell 1.0.0 template does not allow 'not available' for isolation method on a study/single-cell row".into() } });
            }
        }
        if let Some(&j) = index.get(SC_CELL_IDENTIFIER) {
            let v = row[j].trim();
            if !cell_id.is_match(v) {
                let missing_reserved = v.eq_ignore_ascii_case("not available");
                let level = if non_single_role && missing_reserved {
                    "warning"
                } else {
                    "error"
                };
                issues.push(ValidationIssue {
                    level: level.into(),
                    code: "cell_identifier_invalid_or_unresolved".into(),
                    row: ri + 1,
                    column: SC_CELL_IDENTIFIER.into(),
                    message: format!(
                        "cell identifier is not template-valid for this row role: {v}"
                    ),
                });
            }
        }
        if let Some(&j) = index.get(SC_SAMPLE_TYPE) {
            let v = row[j].trim().to_ascii_lowercase();
            if !sample_types.contains(&v.as_str()) {
                issues.push(ValidationIssue { level: "warning".into(), code: "sample_type_requires_cv_validation".into(), row: ri + 1, column: SC_SAMPLE_TYPE.into(), message: format!("sample type is not in the local common-value set; defer ontology/CV validation to sdrf-pipelines: {}", row[j]) });
            }
        }
        if let Some(&j) = index.get("comment[data file]") {
            let v = row[j].trim();
            let aliases = normalized_data_file_aliases(v);
            let linked = aliases.iter().any(|x| valid_files.contains(x));
            if !linked {
                let (level, code, message) = match data_file_policy {
                    DataFileValidationPolicy::EnforceSnapshotInventory => (
                        "error",
                        "data_file_not_in_pride_raw_inventory",
                        format!("SDRF data file is not in PRIDE RAW inventory: {v}"),
                    ),
                    DataFileValidationPolicy::AuditSnapshotInventory => (
                        "warning",
                        "data_file_not_in_pride_snapshot_inventory",
                        format!("resolved SDRF data file is not directly represented in the local PRIDE RAW inventory; this is a repository-linkage audit warning, not an SDRF schema error: {v}"),
                    ),
                };
                issues.push(ValidationIssue {
                    level: level.into(),
                    code: code.into(),
                    row: ri + 1,
                    column: "comment[data file]".into(),
                    message,
                });
            }
        }
        for column in [
            "comment[fraction identifier]",
            "comment[technical replicate]",
        ] {
            if let Some(&j) = index.get(column) {
                let v = row[j].trim();
                if !v.chars().all(|c| c.is_ascii_digit()) {
                    issues.push(ValidationIssue {
                        level: "error".into(),
                        code: "required_integer_invalid".into(),
                        row: ri + 1,
                        column: column.into(),
                        message: format!("{column} must be an integer in the core MS profile: {v}"),
                    });
                }
            }
        }
    }
    issues
}

fn validate_incomplete_mapping_scaffold(
    headers: &[String],
    rows: &[Vec<String>],
    evidence: &DatasetEvidence,
) -> Vec<ValidationIssue> {
    let mut issues = Vec::new();
    if rows.is_empty() {
        issues.push(ValidationIssue {
            level: "error".into(),
            code: "no_data_rows".into(),
            row: 0,
            column: String::new(),
            message: "incomplete mapping scaffold contains no repository-file rows".into(),
        });
        return issues;
    }
    let required_headers = [
        "source name",
        "assay name",
        "technology type",
        "comment[data file]",
    ];
    let index: HashMap<&str, usize> = headers
        .iter()
        .enumerate()
        .map(|(i, h)| (h.as_str(), i))
        .collect();
    for header in required_headers {
        if !index.contains_key(header) {
            issues.push(ValidationIssue {
                level: "error".into(),
                code: "missing_scaffold_column".into(),
                row: 0,
                column: header.into(),
                message: format!(
                    "mapping scaffold is missing required structural column: {header}"
                ),
            });
        }
    }
    for (ri, row) in rows.iter().enumerate() {
        if row.len() != headers.len() {
            issues.push(ValidationIssue {
                level: "error".into(),
                code: "row_width_mismatch".into(),
                row: ri + 1,
                column: String::new(),
                message: format!("row has {} fields; expected {}", row.len(), headers.len()),
            });
        }
    }
    let (code, message) = if evidence.study_design.repository_file_mode == "generic_archives_only" {
        (
            "repository_archive_contents_mapping_unresolved",
            format!(
                "{} generic repository archive/container file(s) must be resolved to canonical acquisitions before SDRF rows can be finalized",
                evidence.study_design.generic_archive_files
            ),
        )
    } else if evidence.study_design.relation_mode_hint == "multiplexed_cells_per_data_file" {
        (
            "sample_to_channel_mapping_unresolved",
            format!(
                "{} design detected (chemistry='{}', mapping_status='{}'); per-channel/per-sample SDRF rows are intentionally not fabricated",
                evidence.study_design.relation_mode_hint,
                evidence.study_design.multiplex_chemistry_hint,
                evidence.study_design.multiplex_mapping_status
            ),
        )
    } else {
        (
            "sample_to_file_relation_unresolved",
            format!(
                "{} design remains unresolved at single-cell branch scope (chemistry='{}'); sample-to-file relationships are intentionally not fabricated",
                evidence.study_design.relation_mode_hint,
                evidence.study_design.multiplex_chemistry_hint
            ),
        )
    };
    issues.push(ValidationIssue {
        level: "error".into(),
        code: code.into(),
        row: 0,
        column: SC_CELL_IDENTIFIER.into(),
        message,
    });
    issues
}

#[cfg(test)]
fn validate_draft(
    headers: &[String],
    rows: &[Vec<String>],
    evidence: &DatasetEvidence,
) -> Vec<ValidationIssue> {
    validate_draft_with_policy(
        headers,
        rows,
        evidence,
        DataFileValidationPolicy::EnforceSnapshotInventory,
    )
}

fn validate_annotation_draft(
    headers: &[String],
    rows: &[Vec<String>],
    evidence: &DatasetEvidence,
    existing_sdrf: bool,
) -> Vec<ValidationIssue> {
    let policy = if existing_sdrf {
        // A resolved/community/repository SDRF is authoritative for its logical
        // data-file names. The local PRIDE snapshot may expose vendor containers
        // or archive wrappers instead, so repository linkage remains visible as
        // a warning but must not make an otherwise-valid preserved SDRF invalid.
        DataFileValidationPolicy::AuditSnapshotInventory
    } else {
        // PRIDE-SCP generated rows must remain strict: every asserted data-file
        // mapping must be grounded in the repository inventory.
        DataFileValidationPolicy::EnforceSnapshotInventory
    };
    validate_draft_with_policy(headers, rows, evidence, policy)
}

fn write_sdrf(path: &Path, headers: &[String], rows: &[Vec<String>]) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut writer = WriterBuilder::new().delimiter(b'\t').from_path(path)?;
    writer.write_record(headers)?;
    for row in rows {
        writer.write_record(row)?;
    }
    writer.flush()?;
    Ok(())
}

fn write_review(path: &Path, issues: &[ValidationIssue]) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut writer = WriterBuilder::new().delimiter(b'\t').from_path(path)?;
    writer.write_record(["level", "code", "row", "column", "message"])?;
    for issue in issues {
        let row_number = issue.row.to_string();
        writer.write_record([
            issue.level.as_str(),
            issue.code.as_str(),
            row_number.as_str(),
            issue.column.as_str(),
            issue.message.as_str(),
        ])?;
    }
    writer.flush()?;
    Ok(())
}

fn dataset_paths(root: &Path, accession: &str) -> (PathBuf, PathBuf, PathBuf, PathBuf, PathBuf) {
    (
        root.join("evidence")
            .join(format!("{accession}.evidence.json")),
        root.join("proposals")
            .join(format!("{accession}.ollama.json")),
        root.join("sdrf").join(format!("{accession}.sdrf.tsv")),
        root.join("review")
            .join(format!("{accession}.sdrf.review.tsv")),
        root.join("audit")
            .join(format!("{accession}.sdrf.audit.json")),
    )
}

async fn annotate_one(opts: &SdrfAnnotateOptions, accession: &str) -> Result<ResultRow> {
    let (evidence_path, proposal_path, draft_path, review_path, audit_path) =
        dataset_paths(&opts.output_dir, accession);
    if audit_path.is_file() && !opts.force {
        let audit: DatasetAudit = serde_json::from_str(&fs::read_to_string(&audit_path)?)?;
        if audit.generator_version == GENERATOR_VERSION
            && audit.sdrf_spec_version == SDRF_SPEC_VERSION
            && audit.single_cell_template_version == SINGLE_CELL_TEMPLATE_VERSION
        {
            return Ok(ResultRow {
                accession: accession.to_string(),
                status: "cached".into(),
                generation_mode: audit.generation_mode,
                raw_files: audit.raw_file_count,
                evidence_items: audit.evidence_item_count,
                relation_mode: audit.relation_mode,
                locally_valid: audit.locally_valid,
                completeness_status: audit.completeness_status,
                validation_errors: audit.validation_error_count,
                proposal_repairs: audit.proposal_repair_count,
                draft_path: audit.draft_path,
                review_path: audit.review_path,
                error: String::new(),
            });
        }
    }
    let evidence = build_evidence(opts, accession)?;
    let explicit_mappings = if let Some(path) = opts.explicit_row_mapping_manifest.as_deref() {
        if !path.is_file() {
            bail!(
                "explicit row-mapping manifest not found: {}",
                path.display()
            );
        }
        load_explicit_row_mappings(path, accession, &evidence.raw_files)?
    } else {
        Vec::new()
    };
    for p in [
        &evidence_path,
        &proposal_path,
        &draft_path,
        &review_path,
        &audit_path,
    ] {
        if let Some(parent) = p.parent() {
            fs::create_dir_all(parent)?;
        }
    }
    fs::write(&evidence_path, serde_json::to_string_pretty(&evidence)?)?;

    let (mut proposal, ollama_used) =
        if let Some(proposal) = deterministic_existing_sdrf_proposal(&evidence) {
            (proposal, false)
        } else {
            (call_ollama(opts, &evidence).await?, true)
        };
    // Preserve the model response verbatim when Ollama was used, then construct a
    // provenance-safe proposal. Existing SDRFs that already provide every target
    // field skip Ollama entirely and are validated deterministically.
    let raw_proposal_path = proposal_path.with_file_name(format!("{accession}.ollama.raw.json"));
    if ollama_used {
        fs::write(&raw_proposal_path, serde_json::to_string_pretty(&proposal)?)?;
    }
    let mut provenance_issues = if ollama_used {
        repair_proposal_provenance(&mut proposal, &evidence)
    } else {
        Vec::new()
    };
    if !evidence.existing_sdrf_path.is_empty() {
        if let Ok((headers, rows)) =
            read_existing_sdrf_table(Path::new(&evidence.existing_sdrf_path))
        {
            let hint = existing_sdrf_relation_hint(&headers, &rows);
            if hint != "uncertain" && proposal.relation_mode != hint {
                let previous = proposal.relation_mode.clone();
                proposal.relation_mode = hint.clone();
                provenance_issues.push(ValidationIssue {
                    level: "warning".into(),
                    code: "relation_mode_determined_from_existing_sdrf".into(),
                    row: 0,
                    column: "relation_mode".into(),
                    message: format!("deterministic existing-SDRF relationship evidence changed relation_mode from '{previous}' to '{hint}'"),
                });
            }
        }
    }
    if evidence.existing_sdrf_path.is_empty()
        && study_design_has_assertive_relation_hint(&evidence.study_design)
    {
        let hint = evidence.study_design.relation_mode_hint.clone();
        if proposal.relation_mode != hint {
            let previous = proposal.relation_mode.clone();
            proposal.relation_mode = hint.clone();
            if !evidence.study_design.relation_evidence_refs.is_empty() {
                proposal.evidence_refs.insert(
                    "relation_mode".into(),
                    evidence.study_design.relation_evidence_refs.clone(),
                );
            }
            provenance_issues.push(ValidationIssue {
                level: "warning".into(),
                code: "relation_mode_determined_from_study_design_scaffold".into(),
                row: 0,
                column: "relation_mode".into(),
                message: format!(
                    "deterministic manuscript/repository study-design scaffold changed relation_mode from '{previous}' to '{hint}' (confidence={})",
                    evidence.study_design.relation_confidence
                ),
            });
        }
    } else if evidence.existing_sdrf_path.is_empty()
        && evidence.study_design.relation_mode_hint == "uncertain"
        && proposal.relation_mode == "multiplexed_cells_per_data_file"
    {
        // A de-novo model is not allowed to restore the exact false-positive
        // architecture that the scoped scaffold rejected. Multiplexed reporter
        // cardinality requires locally linked single-cell/isobaric evidence; a
        // dataset-level TMT/iTRAQ mention or lexical `plex`/plexDIA is insufficient.
        proposal.relation_mode = "uncertain".into();
        proposal.evidence_refs.remove("relation_mode");
        provenance_issues.push(ValidationIssue {
            level: "warning".into(),
            code: "multiplex_relation_rejected_without_scoped_reporter_evidence".into(),
            row: 0,
            column: "relation_mode".into(),
            message: "model-proposed multiplexed_cells_per_data_file was downgraded to uncertain because the deterministic study-design scaffold found no isobaric reporter evidence locally linked to the single-cell branch".into(),
        });
    }
    provenance_issues.extend(apply_deterministic_metadata_scaffold(
        &mut proposal,
        &evidence,
    ));
    fs::write(&proposal_path, serde_json::to_string_pretty(&proposal)?)?;
    validate_proposal_refs(&proposal, &evidence)?;
    let proposal_repair_count = provenance_issues.len();
    let existing = !evidence.existing_sdrf_path.is_empty();

    let (headers, rows, generation_mode) =
        draft_rows_with_explicit_mappings(&proposal, &evidence, &explicit_mappings)?;
    write_sdrf(&draft_path, &headers, &rows)?;
    let mut issues = provenance_issues;
    if !explicit_mappings.is_empty() {
        let refs = explicit_mappings
            .iter()
            .map(|row| format!("{}:{}", row.design_source, row.design_ref))
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect::<Vec<_>>()
            .join("; ");
        issues.push(ValidationIssue {
            level: "warning".into(),
            code: "source_grounded_explicit_row_mapping_applied".into(),
            row: 0,
            column: "comment[data file]".into(),
            message: format!(
                "serialized {} source-grounded biological row(s) from explicit mapping manifest {} (refs: {})",
                explicit_mappings.len(),
                opts.explicit_row_mapping_manifest
                    .as_ref()
                    .map(|p| p.display().to_string())
                    .unwrap_or_default(),
                refs
            ),
        });
        let mapped_raws = explicit_mappings
            .iter()
            .map(|row| row.raw_file.trim().to_ascii_lowercase())
            .collect::<BTreeSet<_>>();
        let unmapped_raws = evidence
            .raw_files
            .iter()
            .filter(|file| !mapped_raws.contains(&file.file_name.trim().to_ascii_lowercase()))
            .map(|file| file.file_name.clone())
            .collect::<Vec<_>>();
        if !unmapped_raws.is_empty() {
            issues.push(ValidationIssue {
                level: "error".into(),
                code: "explicit_row_mapping_repository_scope_incomplete".into(),
                row: 0,
                column: "comment[data file]".into(),
                message: format!(
                    "explicit source-grounded mapping covers {} of {} repository RAW files; remaining RAW roles must be source-resolved before the accession is counted locally complete: {}",
                    mapped_raws.len(),
                    evidence.raw_files.len(),
                    unmapped_raws.join("; ")
                ),
            });
        }
    }
    if evidence.study_design.generic_archive_files > 0 {
        issues.push(ValidationIssue {
            level: "warning".into(),
            code: "repository_generic_archive_requires_contents_mapping".into(),
            row: 0,
            column: "comment[data file]".into(),
            message: format!(
                "repository inventory contains {} generic archive/container file(s); these may bundle multiple acquisitions and must not be treated as a proven one-row-per-acquisition mapping: {}",
                evidence.study_design.generic_archive_files,
                evidence.study_design.generic_archive_file_names.join("; ")
            ),
        });
    }
    if !existing
        && explicit_mappings.is_empty()
        && (proposal.relation_mode != "one_cell_per_data_file"
            || evidence.study_design.repository_file_mode == "generic_archives_only")
    {
        issues.extend(validate_incomplete_mapping_scaffold(
            &headers, &rows, &evidence,
        ));
    } else {
        issues.extend(validate_annotation_draft(
            &headers, &rows, &evidence, existing,
        ));
    }
    write_review(&review_path, &issues)?;
    let errors = issues.iter().filter(|x| x.level == "error").count();
    let locally_valid = errors == 0;
    let has_isolation_template_gap = evidence
        .metadata_scaffold
        .template_gaps
        .iter()
        .any(|gap| gap.field == "single_cell_isolation_method");
    let explicit_scope_incomplete =
        !explicit_mappings.is_empty() && explicit_mappings.len() < evidence.raw_files.len();
    let completeness = if existing && locally_valid {
        "existing_sdrf_enriched_locally_valid"
    } else if existing {
        "existing_sdrf_enriched_requires_review"
    } else if evidence.study_design.repository_file_mode == "generic_archives_only" {
        "incomplete_repository_archive_contents_mapping"
    } else if !explicit_mappings.is_empty() && locally_valid {
        "locally_valid_draft"
    } else if explicit_scope_incomplete {
        "incomplete_explicit_row_mapping_repository_scope"
    } else if !explicit_mappings.is_empty() && has_isolation_template_gap {
        "incomplete_template_isolation_method_gap"
    } else if !explicit_mappings.is_empty() {
        "incomplete_required_metadata"
    } else if proposal.relation_mode == "one_cell_per_data_file" && locally_valid {
        "locally_valid_draft"
    } else if proposal.relation_mode != "one_cell_per_data_file" {
        "incomplete_sample_to_file_or_channel_mapping"
    } else if has_isolation_template_gap {
        "incomplete_template_isolation_method_gap"
    } else {
        "incomplete_required_metadata"
    };
    let audit = DatasetAudit {
        accession: accession.to_string(),
        generator_version: GENERATOR_VERSION.into(),
        sdrf_spec_version: SDRF_SPEC_VERSION.into(),
        single_cell_template_version: SINGLE_CELL_TEMPLATE_VERSION.into(),
        template_profile: "bigbio-single-cell-1.0.0-github-main-observed-2026-09-06".into(),
        template_url: SINGLE_CELL_TEMPLATE_URL.into(), specification_url: SDRF_SPEC_URL.into(),
        existing_sdrf_present: existing, generation_mode: generation_mode.clone(), relation_mode: proposal.relation_mode.clone(),
        study_design: evidence.study_design.clone(),
        metadata_scaffold: evidence.metadata_scaffold.clone(),
        raw_file_count: evidence.raw_files.len(), evidence_item_count: evidence.evidence.len(), manuscript_source_count: evidence.manuscript_sources.len(),
        annotation_source_count: evidence.annotation_sources.len(), ollama_model: opts.model.clone(), ollama_used,
        draft_path: draft_path.display().to_string(), proposal_path: proposal_path.display().to_string(), evidence_path: evidence_path.display().to_string(),
        review_path: review_path.display().to_string(), validation_issue_count: issues.len(), validation_error_count: errors, proposal_repair_count,
        explicit_row_mapping_manifest: opts.explicit_row_mapping_manifest.as_ref().map(|p| p.display().to_string()).unwrap_or_default(),
        explicit_row_mapping_rows: explicit_mappings.len(), locally_valid,
        completeness_status: completeness.into(),
        template_drift_note: "The linked single-cell template is work-in-progress. This generator pins the 1.0.0 column profile shown by the rendered specification/GitHub view observed 2026-09-06. Revalidate against the live template before submission because the template may change.".into(),
        validation_issues: issues,
    };
    fs::write(&audit_path, serde_json::to_string_pretty(&audit)?)?;
    Ok(ResultRow {
        accession: accession.to_string(),
        status: "success".into(),
        generation_mode,
        raw_files: evidence.raw_files.len(),
        evidence_items: evidence.evidence.len(),
        relation_mode: proposal.relation_mode,
        locally_valid,
        completeness_status: completeness.into(),
        validation_errors: errors,
        proposal_repairs: proposal_repair_count,
        draft_path: draft_path.display().to_string(),
        review_path: review_path.display().to_string(),
        error: String::new(),
    })
}

fn resolved_source_kind(resolved_sdrf_dir: &Path, accession: &str) -> String {
    let Some(root) = resolved_sdrf_dir.parent() else {
        return "resolved_external".into();
    };
    let audit_path = root
        .join("audit")
        .join(format!("{accession}.sdrf_source_audit.json"));
    let Ok(text) = fs::read_to_string(audit_path) else {
        return "resolved_external".into();
    };
    let Ok(value) = serde_json::from_str::<Value>(&text) else {
        return "resolved_external".into();
    };
    value
        .get("selected_source_kind")
        .and_then(Value::as_str)
        .filter(|x| !x.trim().is_empty())
        .unwrap_or("resolved_external")
        .to_string()
}

fn audit_raw_files(snapshot_dir: &Path, accession: &str) -> Result<Vec<RawFile>> {
    let path = snapshot_dir.join("files").join(format!("{accession}.json"));
    let value = load_json(&path)
        .with_context(|| format!("load PRIDE file inventory for SDRF audit {accession}"))?;
    Ok(extract_raw_files(&value))
}

fn sdrf_uses_isobaric_labels(headers: &[String], rows: &[Vec<String>]) -> bool {
    let Some(j) = header_first_index(headers, "comment[label]") else {
        return false;
    };
    rows.iter().filter_map(|r| r.get(j)).any(|v| {
        let low = v.to_ascii_lowercase();
        low.contains("tmt") || low.contains("itraq") || low.contains("isobaric")
    })
}

fn field_needs_audit_enrichment(
    field: &str,
    headers: &[String],
    rows: &[Vec<String>],
    relation_mode: &str,
) -> bool {
    if field == "relation_mode" {
        return relation_mode == "uncertain";
    }
    let Some(header) = field_existing_header(field) else {
        return false;
    };
    let Some(j) = header_first_index(headers, header) else {
        // Carrier/reference are context-dependent and should not become generic
        // gaps in label-free or non-isobaric experiments.
        if matches!(field, "carrier_channel" | "reference_channel") {
            return relation_mode == "multiplexed_cells_per_data_file"
                && sdrf_uses_isobaric_labels(headers, rows);
        }
        return true;
    };

    let index: HashMap<&str, usize> = headers
        .iter()
        .enumerate()
        .map(|(i, x)| (x.as_str(), i))
        .collect();
    rows.iter().any(|row| {
        let relevant = match field {
            "single_cell_isolation_method" | "cells_per_well" => {
                !row_explicit_non_single_cell_role(&index, row)
            }
            "carrier_channel" | "reference_channel" => {
                relation_mode == "multiplexed_cells_per_data_file"
                    && sdrf_uses_isobaric_labels(headers, rows)
            }
            _ => true,
        };
        relevant && row.get(j).map_or(true, |v| !concrete_table_value(v))
    })
}

fn audit_gap_fields(
    headers: &[String],
    rows: &[Vec<String>],
    relation_mode: &str,
) -> (Vec<String>, Vec<String>, Vec<String>, Vec<String>) {
    let fields = [
        "relation_mode",
        "organism",
        "organism_part",
        "disease",
        "cell_type",
        "sample_type",
        "single_cell_isolation_method",
        "individual",
        "sample_preparation_batch",
        "cells_per_well",
        "proteomics_data_acquisition_method",
        "label",
        "instrument",
        "cleavage_agent_details",
        "fraction_identifier",
        "technical_replicate",
        "carrier_channel",
        "reference_channel",
    ];
    let missing = fields
        .iter()
        .copied()
        .filter(|f| field_needs_audit_enrichment(f, headers, rows, relation_mode))
        .map(str::to_string)
        .collect::<Vec<_>>();

    // Blocking means the gap affects core SDRF completeness or prevents this tool
    // from interpreting the sample-to-file relationship. These are enrichment
    // priorities, not claims that the external SDRF is invalid under sdrf-pipelines.
    let blocking_names = BTreeSet::from([
        "relation_mode",
        "organism",
        "organism_part",
        "single_cell_isolation_method",
        "proteomics_data_acquisition_method",
        "label",
        "instrument",
        "cleavage_agent_details",
        "fraction_identifier",
        "technical_replicate",
    ]);
    let recommended_names = BTreeSet::from([
        "disease",
        "cell_type",
        "sample_type",
        "sample_preparation_batch",
        "cells_per_well",
        "carrier_channel",
        "reference_channel",
    ]);
    let optional_names = BTreeSet::from(["individual"]);

    let blocking = missing
        .iter()
        .filter(|f| blocking_names.contains(f.as_str()))
        .cloned()
        .collect();
    let recommended = missing
        .iter()
        .filter(|f| recommended_names.contains(f.as_str()))
        .cloned()
        .collect();
    let optional = missing
        .iter()
        .filter(|f| optional_names.contains(f.as_str()))
        .cloned()
        .collect();
    (missing, blocking, recommended, optional)
}

fn write_validation_review(path: &Path, issues: &[ValidationIssue]) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    write_review(path, issues)
}

fn audit_resolved_one(opts: &SdrfAuditOptions, accession: &str) -> Result<SdrfAuditResultRow> {
    let sdrf_path = opts.resolved_sdrf_dir.join(format!("{accession}.sdrf.tsv"));
    if !existing_sdrf_is_usable(&sdrf_path) {
        bail!(
            "resolved SDRF missing or unusable: {} ({})",
            sdrf_path.display(),
            existing_sdrf_status(&sdrf_path)
        );
    }
    let raw_files = audit_raw_files(&opts.snapshot_dir, accession)?;
    let (headers, rows) = read_existing_sdrf_table(&sdrf_path)?;
    let source_kind = resolved_source_kind(&opts.resolved_sdrf_dir, accession);
    let evidence = DatasetEvidence {
        accession: accession.into(),
        project_json_path: String::new(),
        files_json_path: opts
            .snapshot_dir
            .join("files")
            .join(format!("{accession}.json"))
            .display()
            .to_string(),
        existing_sdrf_path: sdrf_path.display().to_string(),
        raw_files,
        study_design: StudyDesignScaffold::default(),
        metadata_scaffold: DeterministicMetadataScaffold::default(),
        evidence: Vec::new(),
        manuscript_sources: Vec::new(),
        annotation_sources: Vec::new(),
    };
    let relation_mode = existing_sdrf_relation_hint(&headers, &rows);
    let issues = validate_draft_with_policy(
        &headers,
        &rows,
        &evidence,
        DataFileValidationPolicy::AuditSnapshotInventory,
    );
    let errors = issues.iter().filter(|x| x.level == "error").count();
    let warnings = issues.iter().filter(|x| x.level == "warning").count();
    let linkage = data_file_linkage_stats(&headers, &rows, &evidence.raw_files);
    let (missing, blocking, recommended, optional) =
        audit_gap_fields(&headers, &rows, &relation_mode);
    let locally_valid = errors == 0;
    let enrichment_priority = if !locally_valid {
        "P0_structural_review"
    } else if !blocking.is_empty() {
        "P1_blocking_gap"
    } else if !recommended.is_empty() {
        "P2_recommended_gap"
    } else if !optional.is_empty() {
        "P3_optional_gap"
    } else {
        "P4_preserve"
    }
    .to_string();
    let review_path = opts
        .output_dir
        .join("review")
        .join(format!("{accession}.sdrf.review.tsv"));
    write_validation_review(&review_path, &issues)?;
    let audit = SdrfResolvedAudit {
        accession: accession.into(),
        auditor_version: SDRF_AUDITOR_VERSION.into(),
        source_kind: source_kind.clone(),
        sdrf_path: sdrf_path.display().to_string(),
        row_count: rows.len(),
        raw_file_count: evidence.raw_files.len(),
        relation_mode: relation_mode.clone(),
        locally_valid,
        validation_error_count: errors,
        validation_warning_count: warnings,
        data_file_linkage: linkage.clone(),
        missing_target_fields: missing.clone(),
        blocking_gap_fields: blocking.clone(),
        recommended_gap_fields: recommended.clone(),
        optional_gap_fields: optional.clone(),
        enrichment_priority: enrichment_priority.clone(),
        validation_issues: issues,
    };
    let audit_path = opts
        .output_dir
        .join("audit")
        .join(format!("{accession}.sdrf.audit.json"));
    if let Some(parent) = audit_path.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(&audit_path, serde_json::to_string_pretty(&audit)?)?;
    Ok(SdrfAuditResultRow {
        accession: accession.into(),
        status: "audited".into(),
        source_kind,
        rows: rows.len(),
        raw_files: evidence.raw_files.len(),
        relation_mode,
        locally_valid,
        validation_errors: errors,
        validation_warnings: warnings,
        repository_linkage_status: linkage.status.clone(),
        sdrf_unique_data_files: linkage.sdrf_unique_data_files,
        matched_unique_data_files: linkage.matched_unique_data_files,
        unmatched_unique_data_files: linkage.unmatched_unique_data_files,
        unmatched_data_file_rows: linkage.unmatched_rows,
        missing_target_fields: missing.len(),
        missing_target_field_names: missing.join(";"),
        blocking_gap_fields: blocking.len(),
        blocking_gap_field_names: blocking.join(";"),
        recommended_gap_fields: recommended.len(),
        recommended_gap_field_names: recommended.join(";"),
        optional_gap_fields: optional.len(),
        optional_gap_field_names: optional.join(";"),
        enrichment_priority,
        sdrf_path: sdrf_path.display().to_string(),
        review_path: review_path.display().to_string(),
        error: String::new(),
    })
}

pub fn audit_sdrf_sources(opts: SdrfAuditOptions) -> Result<SdrfAuditSummary> {
    let accessions = collect_accessions_values(&opts.accessions, opts.accessions_file.as_deref())?;
    fs::create_dir_all(&opts.output_dir)?;
    let mut rows = Vec::new();
    let mut unresolved = Vec::new();
    for (i, accession) in accessions.iter().enumerate() {
        if opts.progress {
            eprintln!("[{}/{}] {}", i + 1, accessions.len(), accession);
        }
        match audit_resolved_one(&opts, accession) {
            Ok(row) => {
                if opts.progress {
                    eprintln!("  -> {} rows={} valid={} errors={} linkage={} priority={} missing_fields={}", row.source_kind, row.rows, row.locally_valid, row.validation_errors, row.repository_linkage_status, row.enrichment_priority, row.missing_target_fields);
                }
                rows.push(row);
            }
            Err(err) => {
                if opts.progress {
                    eprintln!("  -> audit error: {err:#}");
                }
                unresolved.push(accession.clone());
                rows.push(SdrfAuditResultRow {
                    accession: accession.clone(),
                    status: "error".into(),
                    source_kind: String::new(),
                    rows: 0,
                    raw_files: 0,
                    relation_mode: String::new(),
                    locally_valid: false,
                    validation_errors: 0,
                    validation_warnings: 0,
                    repository_linkage_status: String::new(),
                    sdrf_unique_data_files: 0,
                    matched_unique_data_files: 0,
                    unmatched_unique_data_files: 0,
                    unmatched_data_file_rows: 0,
                    missing_target_fields: 0,
                    missing_target_field_names: String::new(),
                    blocking_gap_fields: 0,
                    blocking_gap_field_names: String::new(),
                    recommended_gap_fields: 0,
                    recommended_gap_field_names: String::new(),
                    optional_gap_fields: 0,
                    optional_gap_field_names: String::new(),
                    enrichment_priority: "error".into(),
                    sdrf_path: String::new(),
                    review_path: String::new(),
                    error: format!("{err:#}"),
                });
            }
        }
    }
    let results_path = opts.output_dir.join("sdrf_audit_results.tsv");
    let mut writer = WriterBuilder::new()
        .delimiter(b'\t')
        .from_path(&results_path)?;
    for row in &rows {
        writer.serialize(row)?;
    }
    writer.flush()?;
    let unresolved_path = opts.output_dir.join("audit_unresolved_accessions.txt");
    fs::write(
        &unresolved_path,
        if unresolved.is_empty() {
            String::new()
        } else {
            format!("{}\n", unresolved.join("\n"))
        },
    )?;
    let summary = SdrfAuditSummary {
        auditor_version: SDRF_AUDITOR_VERSION.into(),
        accessions_requested: accessions.len(),
        audited: rows.iter().filter(|r| r.status == "audited").count(),
        errors: rows.iter().filter(|r| r.status == "error").count(),
        locally_valid: rows
            .iter()
            .filter(|r| r.status == "audited" && r.locally_valid)
            .count(),
        repository_linkage_complete: rows
            .iter()
            .filter(|r| r.status == "audited" && r.repository_linkage_status == "complete")
            .count(),
        repository_linkage_partial: rows
            .iter()
            .filter(|r| r.status == "audited" && r.repository_linkage_status == "partial")
            .count(),
        repository_linkage_none: rows
            .iter()
            .filter(|r| r.status == "audited" && r.repository_linkage_status == "none")
            .count(),
        repository_linkage_unavailable: rows
            .iter()
            .filter(|r| r.status == "audited" && r.repository_linkage_status == "unavailable")
            .count(),
        requires_review: rows
            .iter()
            .filter(|r| r.status == "audited" && (!r.locally_valid || r.blocking_gap_fields > 0))
            .count(),
        metadata_enrichment_candidates: rows
            .iter()
            .filter(|r| r.status == "audited" && r.missing_target_fields > 0)
            .count(),
        priority_p0_structural_review: rows
            .iter()
            .filter(|r| r.enrichment_priority == "P0_structural_review")
            .count(),
        priority_p1_blocking_gap: rows
            .iter()
            .filter(|r| r.enrichment_priority == "P1_blocking_gap")
            .count(),
        priority_p2_recommended_gap: rows
            .iter()
            .filter(|r| r.enrichment_priority == "P2_recommended_gap")
            .count(),
        priority_p3_optional_gap: rows
            .iter()
            .filter(|r| r.enrichment_priority == "P3_optional_gap")
            .count(),
        priority_p4_preserve: rows
            .iter()
            .filter(|r| r.enrichment_priority == "P4_preserve")
            .count(),
        curated_bigbio: rows
            .iter()
            .filter(|r| r.source_kind == "curated_bigbio")
            .count(),
        repository_submitted: rows
            .iter()
            .filter(|r| r.source_kind == "repository_submitted")
            .count(),
        other_source: rows
            .iter()
            .filter(|r| {
                r.status == "audited"
                    && r.source_kind != "curated_bigbio"
                    && r.source_kind != "repository_submitted"
            })
            .count(),
        results_tsv: results_path.display().to_string(),
        unresolved_accessions_file: unresolved_path.display().to_string(),
    };
    fs::write(
        opts.output_dir.join("sdrf_audit_summary.json"),
        serde_json::to_string_pretty(&summary)?,
    )?;
    Ok(summary)
}

pub async fn annotate_sdrf(opts: SdrfAnnotateOptions) -> Result<SdrfAnnotateSummary> {
    let accessions = collect_accessions(&opts)?;
    if accessions.len() > 1 && !opts.manuscript_text_paths.is_empty() {
        bail!("--manuscript-text is accession-specific and may only be used when one accession is requested; use --publication-manifest for batch runs");
    }
    for path in &opts.manuscript_text_paths {
        if !path.is_file() {
            bail!(
                "--manuscript-text does not exist or is not a file: {}",
                path.display()
            );
        }
    }
    fs::create_dir_all(&opts.output_dir)?;
    let results_path = opts.output_dir.join("sdrf_annotation_results.tsv");
    let mut rows = Vec::new();
    for (i, accession) in accessions.iter().enumerate() {
        if opts.progress {
            eprintln!("[{}/{}] {}", i + 1, accessions.len(), accession);
        }
        match annotate_one(&opts, accession).await {
            Ok(row) => {
                if opts.progress {
                    eprintln!(
                        "  -> {} relation={} valid={} status={}",
                        row.status, row.relation_mode, row.locally_valid, row.completeness_status
                    );
                }
                rows.push(row);
            }
            Err(err) => {
                eprintln!("  -> error: {err:#}");
                let error_dir = opts.output_dir.join("errors");
                let _ = fs::create_dir_all(&error_dir);
                let _ = fs::write(
                    error_dir.join(format!("{accession}.error.txt")),
                    format!("{err:#}\n"),
                );
                rows.push(ResultRow {
                    accession: accession.clone(),
                    status: "error".into(),
                    generation_mode: String::new(),
                    raw_files: 0,
                    evidence_items: 0,
                    relation_mode: String::new(),
                    locally_valid: false,
                    completeness_status: "error".into(),
                    validation_errors: 0,
                    proposal_repairs: 0,
                    draft_path: String::new(),
                    review_path: String::new(),
                    error: format!("{err:#}"),
                });
            }
        }
    }
    let mut writer = WriterBuilder::new()
        .delimiter(b'\t')
        .from_path(&results_path)?;
    for row in &rows {
        writer.serialize(row)?;
    }
    writer.flush()?;
    let successful = rows.iter().filter(|r| r.status != "error").count();
    let errors = rows.len() - successful;
    let mut snapshot_sdrf_files = 0usize;
    let mut existing = 0usize;
    for accession in &accessions {
        let snapshot_path = opts
            .snapshot_dir
            .join("sdrf")
            .join(format!("{accession}.sdrf.tsv"));
        if snapshot_path.is_file() {
            snapshot_sdrf_files += 1;
        }
        let resolved_usable = opts
            .resolved_sdrf_dir
            .as_ref()
            .map(|dir| dir.join(format!("{accession}.sdrf.tsv")))
            .map(|p| existing_sdrf_is_usable(&p))
            .unwrap_or(false);
        if resolved_usable || existing_sdrf_is_usable(&snapshot_path) {
            existing += 1;
        }
    }
    let unusable_snapshot_sdrf = snapshot_sdrf_files.saturating_sub(existing);
    let locally_valid = rows.iter().filter(|r| r.locally_valid).count();
    let accessions_with_provenance_repairs = rows.iter().filter(|r| r.proposal_repairs > 0).count();
    let provenance_repairs = rows.iter().map(|r| r.proposal_repairs).sum();
    let summary = SdrfAnnotateSummary {
        generator_version: GENERATOR_VERSION.into(),
        sdrf_spec_version: SDRF_SPEC_VERSION.into(),
        single_cell_template_version: SINGLE_CELL_TEMPLATE_VERSION.into(),
        template_profile: "bigbio-single-cell-1.0.0-github-main-observed-2026-09-06".into(),
        accessions_requested: accessions.len(),
        successful,
        errors,
        existing_sdrf_detected: existing,
        snapshot_sdrf_files_detected: snapshot_sdrf_files,
        unusable_snapshot_sdrf_detected: unusable_snapshot_sdrf,
        drafts_written: successful,
        locally_valid_drafts: locally_valid,
        incomplete_drafts: successful.saturating_sub(locally_valid),
        accessions_with_provenance_repairs,
        provenance_repairs,
        results_tsv: results_path.display().to_string(),
    };
    fs::write(
        opts.output_dir.join("sdrf_annotation_summary.json"),
        serde_json::to_string_pretty(&summary)?,
    )?;
    Ok(summary)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn tmp() -> PathBuf {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!("pride_scp_sdrf_test_{stamp}"))
    }

    #[test]
    fn raw_files_are_filtered_from_pride_manifest() {
        let payload = json!([
            {"fileName":"cell_001.raw","fileCategory":{"value":"RAW"},"publicFileLocations":[{"name":"FTP Protocol","value":"ftp://example/cell_001.raw"}]},
            {"fileName":"results.tsv","fileCategory":{"value":"RESULT"}},
            {"fileName":"cell_002.d","fileCategory":{"value":"OTHER"}}
        ]);
        let rows = extract_raw_files(&payload);
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].file_name, "cell_001.raw");
        assert_eq!(rows[0].file_uri, "ftp://example/cell_001.raw");
        assert_eq!(rows[1].file_name, "cell_002.d");
    }

    #[test]
    fn one_cell_per_file_draft_has_template_columns_and_valid_ids() {
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![RawFile {
                file_name: "cell A-01.raw".into(),
                file_uri: "ftp://x/cell%20A-01.raw".into(),
                category: "RAW".into(),
            }],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let proposal = SdrfProposal {
            relation_mode: "one_cell_per_data_file".into(),
            organism: "Homo sapiens".into(),
            organism_part: "not available".into(),
            disease: "not available".into(),
            cell_type: "HeLa cell".into(),
            sample_type: "single cell".into(),
            single_cell_isolation_method: "cellenONE".into(),
            individual: "not applicable".into(),
            sample_preparation_batch: "not available".into(),
            cells_per_well: "1".into(),
            proteomics_data_acquisition_method: "Data-dependent acquisition".into(),
            label: "label free sample".into(),
            instrument: "Orbitrap".into(),
            cleavage_agent_details: "NT=Trypsin;AC=MS:1001251".into(),
            fraction_identifier: "1".into(),
            technical_replicate: "1".into(),
            carrier_channel: "not applicable".into(),
            reference_channel: "not applicable".into(),
            factors: vec![],
            evidence_refs: BTreeMap::new(),
            confidence: "high".into(),
            notes: String::new(),
        };
        let (headers, rows, _) = draft_rows(&proposal, &evidence).unwrap();
        assert!(headers.contains(&SC_CELL_IDENTIFIER.to_string()));
        let idx = headers
            .iter()
            .position(|h| h == SC_CELL_IDENTIFIER)
            .unwrap();
        assert_eq!(rows[0][idx], "cell_A-01");
        let issues = validate_draft(&headers, &rows, &evidence);
        assert!(issues.iter().all(|x| x.level != "error"), "{issues:?}");
    }

    #[test]
    fn explicit_source_grounded_manifest_serializes_multiplex_rows() {
        let evidence = DatasetEvidence {
            accession: "PXD028040".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![
                RawFile {
                    file_name: "2018-08-27_SC02.RAW".into(),
                    file_uri: "ftp://example/2018-08-27_SC02.RAW".into(),
                    category: "RAW".into(),
                },
                RawFile {
                    file_name: "2018-08-27_SC03_tmt_single_neurons.RAW".into(),
                    file_uri: "ftp://example/2018-08-27_SC03_tmt_single_neurons.RAW".into(),
                    category: "RAW".into(),
                },
            ],
            study_design: StudyDesignScaffold {
                relation_mode_hint: "multiplexed_cells_per_data_file".into(),
                relation_confidence: "high".into(),
                ..Default::default()
            },
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let root = tmp();
        fs::create_dir_all(&root).unwrap();
        let manifest = root.join("mapping.tsv");
        fs::write(
            &manifest,
            concat!(
                "accession\traw_file\tsource_name\tcell_identifier\tbiological_replicate\ttechnical_replicate\tsample_type\tcells_per_well\tlabel\tcarrier_channel\treference_channel\tdesign_source\tdesign_ref\tmapping_key\tmapping_confidence\n",
                "PXD028040\t2018-08-27_SC02.RAW\tDA_neuron_1\tDA_neuron_1\t1\t1\tsingle cell\t1\tTMT128\tTMT131\tnot applicable\tChoi_design.xlsx\tSheet2:row15\tdate_sc_run_key\thigh\n",
                "PXD028040\t2018-08-27_SC03_tmt_single_neurons.RAW\tDA_neuron_1\tDA_neuron_1\t1\t2\tsingle cell\t1\tTMT128\tTMT131\tnot applicable\tChoi_design.xlsx\tSheet2:row16\tdate_sc_run_key\thigh\n"
            ),
        )
        .unwrap();
        let mappings =
            load_explicit_row_mappings(&manifest, "PXD028040", &evidence.raw_files).unwrap();
        assert_eq!(mappings.len(), 2);
        let proposal = SdrfProposal {
            relation_mode: "multiplexed_cells_per_data_file".into(),
            organism: "Mus musculus".into(),
            organism_part: "substantia nigra pars compacta".into(),
            disease: "normal".into(),
            cell_type: "dopaminergic neuron".into(),
            sample_type: "single cell".into(),
            single_cell_isolation_method: "manual picking".into(),
            individual: "not available".into(),
            sample_preparation_batch: "not available".into(),
            cells_per_well: "1".into(),
            proteomics_data_acquisition_method: "data-dependent acquisition".into(),
            label: "TMT128".into(),
            instrument: "Orbitrap".into(),
            cleavage_agent_details: "trypsin".into(),
            fraction_identifier: "1".into(),
            technical_replicate: "1".into(),
            carrier_channel: "TMT131".into(),
            reference_channel: "not applicable".into(),
            ..Default::default()
        };
        let (headers, rows, mode) =
            draft_rows_with_explicit_mappings(&proposal, &evidence, &mappings).unwrap();
        assert_eq!(mode, "generated_source_grounded_explicit_row_mapping");
        assert_eq!(rows.len(), 2);
        let at = |name: &str| headers.iter().position(|h| h == name).unwrap();
        assert_eq!(rows[0][at("source name")], "DA_neuron_1");
        assert_eq!(rows[1][at("source name")], "DA_neuron_1");
        assert_eq!(rows[0][at("characteristics[biological replicate]")], "1");
        assert_eq!(rows[0][at("comment[technical replicate]")], "1");
        assert_eq!(rows[1][at("comment[technical replicate]")], "2");
        assert_eq!(rows[0][at("comment[label]")], "TMT128");
        assert_eq!(rows[0][at(SC_CARRIER_CHANNEL)], "TMT131");
        let issues = validate_draft(&headers, &rows, &evidence);
        assert!(issues.iter().all(|x| x.level != "error"), "{issues:?}");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn explicit_manifest_rejects_duplicate_raw_assignment() {
        let raw_files = vec![RawFile {
            file_name: "run.raw".into(),
            file_uri: String::new(),
            category: "RAW".into(),
        }];
        let root = tmp();
        fs::create_dir_all(&root).unwrap();
        let manifest = root.join("mapping.tsv");
        fs::write(
            &manifest,
            concat!(
                "accession\traw_file\tsource_name\tcell_identifier\tbiological_replicate\ttechnical_replicate\tsample_type\tcells_per_well\tlabel\tcarrier_channel\treference_channel\tdesign_source\tdesign_ref\tmapping_key\tmapping_confidence\n",
                "PXD028040\trun.raw\tcell1\tcell1\t1\t1\tsingle cell\t1\tTMT128\tTMT131\tnot applicable\tdesign.xlsx\trow1\texact_raw_name\thigh\n",
                "PXD028040\trun.raw\tcell2\tcell2\t2\t1\tsingle cell\t1\tTMT128\tTMT131\tnot applicable\tdesign.xlsx\trow2\texact_raw_name\thigh\n"
            ),
        )
        .unwrap();
        assert!(load_explicit_row_mappings(&manifest, "PXD028040", &raw_files).is_err());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn multiplexed_skeleton_is_explicitly_invalid_until_mapping_exists() {
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            raw_files: vec![RawFile {
                file_name: "plex01.raw".into(),
                file_uri: String::new(),
                category: "RAW".into(),
            }],
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let proposal = SdrfProposal {
            relation_mode: "multiplexed_cells_per_data_file".into(),
            sample_type: "single cell".into(),
            single_cell_isolation_method: "FACS".into(),
            ..Default::default()
        };
        let (headers, rows, _) = draft_rows(&proposal, &evidence).unwrap();
        let issues = validate_draft(&headers, &rows, &evidence);
        assert!(issues
            .iter()
            .any(|x| x.code == "cell_identifier_invalid_or_unresolved"));
    }

    #[test]
    fn semantic_bundle_explicit_pxd_routing_is_detected() {
        let bundle = json!({"publication_pride_accessions":["PXD123456", "see PXD654321"]});
        let pxds = semantic_bundle_explicit_pxds(&bundle);
        assert!(pxds.contains("PXD123456"));
        assert!(pxds.contains("PXD654321"));
        assert_eq!(pxds.len(), 2);
    }

    #[test]
    fn uncertain_relation_mode_is_non_assertive_and_needs_no_evidence_ref() {
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            raw_files: vec![],
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let proposal = SdrfProposal {
            relation_mode: "uncertain".into(),
            ..Default::default()
        };
        validate_proposal_refs(&proposal, &evidence).unwrap();
    }

    #[test]
    fn blank_study_design_relation_hint_is_non_assertive() {
        let design = StudyDesignScaffold::default();
        assert!(!study_design_has_assertive_relation_hint(&design));

        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            study_design: design,
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            raw_files: vec![],
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        assert!(proposal_target_fields(&evidence).contains("relation_mode"));
    }

    #[test]
    fn unsupported_asserted_relation_mode_is_downgraded_not_fatal() {
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            raw_files: vec![],
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let mut proposal = SdrfProposal {
            relation_mode: "one_cell_per_data_file".into(),
            ..Default::default()
        };
        let repairs = repair_proposal_provenance(&mut proposal, &evidence);
        assert_eq!(proposal.relation_mode, "uncertain");
        assert!(repairs
            .iter()
            .any(|x| x.code == "proposal_field_downgraded_missing_provenance"));
        validate_proposal_refs(&proposal, &evidence).unwrap();
    }

    #[test]
    fn unknown_ref_is_removed_and_field_is_downgraded() {
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            raw_files: vec![],
            evidence: vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "test".into(),
                source_label: "test".into(),
                text: "Homo sapiens".into(),
            }],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let mut refs = BTreeMap::new();
        refs.insert("cell_type".into(), vec!["E0000".into()]);
        let mut proposal = SdrfProposal {
            relation_mode: "uncertain".into(),
            cell_type: "HeLa".into(),
            evidence_refs: refs,
            ..Default::default()
        };
        let repairs = repair_proposal_provenance(&mut proposal, &evidence);
        assert_eq!(proposal.cell_type, "not available");
        assert!(repairs
            .iter()
            .any(|x| x.code == "proposal_unknown_evidence_ref_removed"));
        validate_proposal_refs(&proposal, &evidence).unwrap();
    }

    #[test]
    fn common_missing_aliases_are_normalized() {
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            raw_files: vec![],
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let mut proposal = SdrfProposal {
            relation_mode: "uncertain".into(),
            organism: "N/A".into(),
            disease: "default".into(),
            ..Default::default()
        };
        repair_proposal_provenance(&mut proposal, &evidence);
        assert_eq!(proposal.organism, "not available");
        assert_eq!(proposal.disease, "not available");
        validate_proposal_refs(&proposal, &evidence).unwrap();
    }

    #[test]
    fn structured_existing_sdrf_relation_detects_multiplexing() {
        let headers = vec![
            "source name".into(),
            "comment[data file]".into(),
            SC_CELL_IDENTIFIER.into(),
            "comment[label]".into(),
        ];
        let rows = vec![
            vec![
                "cell1".into(),
                "run.raw".into(),
                "cell1".into(),
                "TMT126".into(),
            ],
            vec![
                "cell2".into(),
                "run.raw".into(),
                "cell2".into(),
                "TMT127N".into(),
            ],
        ];
        assert_eq!(
            existing_sdrf_relation_hint(&headers, &rows),
            "multiplexed_cells_per_data_file"
        );
    }

    #[test]
    fn pooled_control_labels_do_not_make_single_cell_branch_multiplexed() {
        let headers = vec![
            "source name".into(),
            "comment[data file]".into(),
            SC_SAMPLE_TYPE.into(),
            SC_CELL_IDENTIFIER.into(),
            "comment[label]".into(),
        ];
        let rows = vec![
            vec![
                "cell1".into(),
                "cell1.raw".into(),
                "single cell".into(),
                "cell1".into(),
                "label free sample".into(),
            ],
            vec![
                "cell2".into(),
                "cell2.raw".into(),
                "single cell".into(),
                "cell2".into(),
                "label free sample".into(),
            ],
            vec![
                "poolA".into(),
                "pool.raw".into(),
                "pooled".into(),
                "not applicable".into(),
                "DIMETHYL0".into(),
            ],
            vec![
                "poolB".into(),
                "pool.raw".into(),
                "pooled".into(),
                "not applicable".into(),
                "DIMETHYL8".into(),
            ],
        ];
        assert_eq!(
            existing_sdrf_relation_hint(&headers, &rows),
            "one_cell_per_data_file"
        );
    }

    #[test]
    fn existing_sdrf_merge_preserves_core_values_and_adds_single_cell_columns() {
        let root = tmp();
        fs::create_dir_all(&root).unwrap();
        let existing = root.join("existing.sdrf.tsv");
        fs::write(&existing,
            "source name\tcharacteristics[organism]\tassay name\ttechnology type\tcomment[proteomics data acquisition method]\tcomment[label]\tcomment[instrument]\tcomment[cleavage agent details]\tcomment[fraction identifier]\tcomment[technical replicate]\tcomment[data file]\ncell_A\thomo sapiens\tassay_A\tproteomic profiling by mass spectrometry\tData-dependent acquisition\tlabel free sample\tOrbitrap\tNT=Trypsin;AC=MS:1001251\t1\t1\tcell_A.raw\n"
        ).unwrap();
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: existing.display().to_string(),
            raw_files: vec![RawFile {
                file_name: "cell_A.raw".into(),
                file_uri: "ftp://x/cell_A.raw".into(),
                category: "RAW".into(),
            }],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let proposal = SdrfProposal {
            relation_mode: "one_cell_per_data_file".into(),
            single_cell_isolation_method: "cellenONE".into(),
            ..Default::default()
        };
        let (headers, rows, mode) = merge_existing_sdrf(&proposal, &evidence).unwrap();
        assert_eq!(mode, "enriched_existing_sdrf");
        let org = header_first_index(&headers, "characteristics[organism]").unwrap();
        let cell = header_first_index(&headers, SC_CELL_IDENTIFIER).unwrap();
        let iso = header_first_index(&headers, SC_ISOLATION_METHOD).unwrap();
        let frac = header_first_index(&headers, "comment[fraction identifier]").unwrap();
        assert_eq!(rows[0][org], "homo sapiens");
        assert_eq!(rows[0][cell], "cell_A");
        assert_eq!(rows[0][iso], "cellenONE");
        assert_eq!(rows[0][frac], "1");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn repository_transport_paths_are_not_llm_metadata_evidence() {
        assert!(!relevant_sdrf_metadata_path("publicFileLocations[0].name"));
        assert!(!relevant_sdrf_metadata_path("fileCategory.value"));
        assert!(relevant_sdrf_metadata_path("organisms[0].name"));
        assert!(relevant_sdrf_metadata_path("instruments[0].name"));
    }

    #[test]
    fn existing_sdrf_resolved_reserved_values_do_not_trigger_ollama() {
        assert!(!concrete_table_value("not available"));
        assert!(concrete_table_value("not applicable"));
        assert!(concrete_table_value("pooled"));
    }

    #[test]
    fn existing_sdrf_targets_only_missing_fields() {
        let root = tmp();
        fs::create_dir_all(&root).unwrap();
        let existing = root.join("existing.sdrf.tsv");
        fs::write(&existing,
            "source name\tcharacteristics[organism]\tassay name\ttechnology type\tcomment[proteomics data acquisition method]\tcomment[label]\tcomment[instrument]\tcomment[cleavage agent details]\tcomment[fraction identifier]\tcomment[technical replicate]\tcomment[data file]\ncell_A\thomo sapiens\tassay_A\tproteomic profiling by mass spectrometry\tData-dependent acquisition\tlabel free sample\tOrbitrap\tNT=Trypsin;AC=MS:1001251\t1\t1\tcell_A.raw\n"
        ).unwrap();
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: existing.display().to_string(),
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            raw_files: vec![],
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let targets = proposal_target_fields(&evidence);
        assert!(!targets.contains("organism"));
        assert!(!targets.contains("instrument"));
        assert!(targets.contains("single_cell_isolation_method"));
        assert!(targets.contains("cell_type"));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn analysis_software_is_not_accepted_as_isolation_or_instrument() {
        assert!(obviously_invalid_field_value(
            "single_cell_isolation_method",
            "MaxQuant"
        ));
        assert!(obviously_invalid_field_value("instrument", "MaxQuant"));
        assert!(obviously_invalid_field_value(
            "proteomics_data_acquisition_method",
            "Proteome Discoverer"
        ));
        assert!(!obviously_invalid_field_value(
            "single_cell_isolation_method",
            "cellenONE"
        ));
        assert!(!obviously_invalid_field_value(
            "instrument",
            "Orbitrap Eclipse"
        ));
    }

    #[test]
    fn field_specific_evidence_filter_rejects_unrelated_software_text() {
        let item = EvidenceItem {
            id: "E0001".into(),
            source_kind: "manuscript_text".into(),
            source_label: "manuscript:paper.txt:window=1".into(),
            text: "Peptide identifications were processed using MaxQuant version 2.0.".into(),
        };
        assert!(!evidence_relevant_to_field(
            "single_cell_isolation_method",
            &item
        ));
        assert!(!evidence_relevant_to_field("instrument", &item));
    }

    #[test]
    fn header_only_sdrf_is_not_locally_valid() {
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            raw_files: vec![],
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let headers = vec!["source name".into()];
        let issues = validate_draft(&headers, &[], &evidence);
        assert!(issues
            .iter()
            .any(|x| x.code == "no_data_rows" && x.level == "error"));
    }

    #[test]
    fn existing_sdrf_draft_never_silently_falls_back_to_skeleton() {
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: "/definitely/missing/existing.sdrf.tsv".into(),
            raw_files: vec![RawFile {
                file_name: "run.raw".into(),
                file_uri: String::new(),
                category: "RAW".into(),
            }],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let proposal = SdrfProposal {
            relation_mode: "uncertain".into(),
            ..Default::default()
        };
        assert!(draft_rows(&proposal, &evidence).is_err());
    }

    #[test]
    fn repository_sdrf_candidates_are_extracted_from_file_manifest() {
        let payload = json!([
            {"fileName":"study.sdrf.tsv","fileCategory":{"value":"OTHER"},"publicFileLocations":[{"name":"FTP Protocol","value":"ftp://ftp.pride.ebi.ac.uk/path/study.sdrf.tsv"}]},
            {"fileName":"cell.raw","fileCategory":{"value":"RAW"}}
        ]);
        let rows = extract_sdrf_file_candidates(&payload);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].0, "study.sdrf.tsv");
        assert!(rows[0].1.ends_with("study.sdrf.tsv"));
    }

    #[test]
    fn ftp_sdrf_source_url_is_normalized_to_https() {
        assert_eq!(
            normalize_download_url("ftp://ftp.pride.ebi.ac.uk/pride/x.sdrf.tsv").as_deref(),
            Some("https://ftp.pride.ebi.ac.uk/pride/x.sdrf.tsv")
        );
    }

    #[test]
    fn sdrf_source_precedence_prefers_curated_then_repository() {
        let mk = |kind: &str, usable: bool| SdrfSourceCandidateAudit {
            source_kind: kind.into(),
            source_url: String::new(),
            status: "usable".into(),
            usable,
            local_path: String::new(),
            content_fingerprint: String::new(),
            bytes: 0,
            note: String::new(),
        };
        let candidates = vec![
            mk("snapshot_pride_sdrf_api", true),
            mk("repository_submitted", true),
            mk("curated_bigbio", true),
        ];
        assert_eq!(
            select_sdrf_source_candidate(&candidates)
                .unwrap()
                .source_kind,
            "curated_bigbio"
        );
        let candidates = vec![
            mk("snapshot_pride_sdrf_api", true),
            mk("repository_submitted", true),
        ];
        assert_eq!(
            select_sdrf_source_candidate(&candidates)
                .unwrap()
                .source_kind,
            "repository_submitted"
        );
    }

    #[test]
    fn header_only_snapshot_sdrf_is_not_usable_existing_sdrf() {
        let dir = tmp();
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("header_only.sdrf.tsv");
        fs::write(&path, "source name\tcomment[data file]\n").unwrap();
        assert_eq!(existing_sdrf_status(&path), "header_only");
        assert!(!existing_sdrf_is_usable(&path));
    }

    #[test]
    fn mapped_snapshot_sdrf_is_usable_existing_sdrf() {
        let dir = tmp();
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("mapped.sdrf.tsv");
        fs::write(
            &path,
            "source name\tcomment[data file]\ncell_1\tcell_1.raw\n",
        )
        .unwrap();
        assert_eq!(existing_sdrf_status(&path), "usable");
        assert!(existing_sdrf_is_usable(&path));
    }

    #[test]
    fn complete_existing_sdrf_can_skip_ollama() {
        let root = tmp();
        fs::create_dir_all(&root).unwrap();
        let existing = root.join("existing.sdrf.tsv");
        fs::write(&existing,
            concat!(
                "source name\tcharacteristics[organism]\tcharacteristics[organism part]\tcharacteristics[disease]\tcharacteristics[cell type]\tcharacteristics[sample type]\tcharacteristics[single cell isolation protocol]\tcharacteristics[cell identifier]\tcharacteristics[individual]\tcomment[sample preparation batch]\tcharacteristics[cells per well]\tassay name\ttechnology type\tcomment[proteomics data acquisition method]\tcomment[label]\tcomment[instrument]\tcomment[cleavage agent details]\tcomment[fraction identifier]\tcomment[technical replicate]\tcomment[data file]\tcomment[carrier channel]\tcomment[reference channel]\n",
                "cell1\thomo sapiens\tnot applicable\tnormal\tHeLa\tsingle cell\tcellenONE\tcell1\tdonor1\tbatch1\t1\tassay1\tproteomic profiling by mass spectrometry\tdata-dependent acquisition\tlabel free sample\tOrbitrap\tTrypsin\t1\t1\trun.raw\tnot applicable\tnot applicable\n"
            )
        ).unwrap();
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: existing.display().to_string(),
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            raw_files: vec![],
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        assert!(proposal_target_fields(&evidence).is_empty());
        let proposal = deterministic_existing_sdrf_proposal(&evidence).unwrap();
        assert_eq!(proposal.relation_mode, "one_cell_per_data_file");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn publication_manifest_resolves_text_paths_by_accession() {
        let root = tmp();
        fs::create_dir_all(&root).unwrap();
        let text_path = root.join("paper.txt");
        fs::write(&text_path, "single-cell proteomics method").unwrap();
        let manifest = root.join("manifest.tsv");
        fs::write(
            &manifest,
            format!(
                "accession\tpublication_content_text_path\nPXD123456\t{}\nPXD654321\t/nope\n",
                text_path.display()
            ),
        )
        .unwrap();
        let paths = publication_paths_for_accession(&manifest, "PXD123456").unwrap();
        assert_eq!(paths, vec![text_path]);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn audit_missing_fields_preserves_resolved_not_applicable_values() {
        let root = tmp();
        fs::create_dir_all(root.join("resolved")).unwrap();
        let p = root.join("resolved").join("PXD999999.sdrf.tsv");
        fs::write(&p,
            "source name\tcharacteristics[organism]\tcharacteristics[organism part]\tcharacteristics[disease]\tcharacteristics[cell type]\tassay name\ttechnology type\tcomment[proteomics data acquisition method]\tcomment[label]\tcomment[instrument]\tcomment[cleavage agent details]\tcomment[fraction identifier]\tcomment[technical replicate]\tcomment[data file]\tcharacteristics[sample type]\tcharacteristics[single cell isolation protocol]\tcharacteristics[cell identifier]\tcharacteristics[individual]\tcomment[sample preparation batch]\tcharacteristics[cells per well]\tcomment[carrier channel]\tcomment[reference channel]\ncell1\thomo sapiens\tnot applicable\tnormal\thela cell\trun1\tproteomic profiling by mass spectrometry\tdata-dependent acquisition\tlabel free sample\torbitrap\ttrypsin\t1\t1\tcell1.raw\tsingle cell\tmanual picking\tcell1\tnot applicable\tnot applicable\t1\tnot applicable\tnot applicable\n"
        ).unwrap();
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: p.display().to_string(),
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            raw_files: vec![RawFile {
                file_name: "cell1.raw".into(),
                file_uri: String::new(),
                category: "RAW".into(),
            }],
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        assert!(proposal_target_fields(&evidence).is_empty());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn resolved_audit_repository_filename_mismatch_is_warning_not_schema_error() {
        let headers = vec![
            "source name".into(),
            "characteristics[organism]".into(),
            "assay name".into(),
            "technology type".into(),
            "comment[proteomics data acquisition method]".into(),
            "comment[label]".into(),
            "comment[instrument]".into(),
            "comment[cleavage agent details]".into(),
            "comment[fraction identifier]".into(),
            "comment[technical replicate]".into(),
            "comment[data file]".into(),
            SC_SAMPLE_TYPE.into(),
            SC_ISOLATION_METHOD.into(),
            SC_CELL_IDENTIFIER.into(),
        ];
        let rows = vec![vec![
            "cell1".into(),
            "homo sapiens".into(),
            "run1".into(),
            "proteomic profiling by mass spectrometry".into(),
            "data-dependent acquisition".into(),
            "label free sample".into(),
            "Orbitrap".into(),
            "Trypsin".into(),
            "1".into(),
            "1".into(),
            "logical_run.d".into(),
            "single cell".into(),
            "FACS".into(),
            "cell1".into(),
        ]];
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![RawFile {
                file_name: "submission_bundle.tar.gz".into(),
                file_uri: String::new(),
                category: "RAW".into(),
            }],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let issues = validate_draft_with_policy(
            &headers,
            &rows,
            &evidence,
            DataFileValidationPolicy::AuditSnapshotInventory,
        );
        assert!(!issues
            .iter()
            .any(|x| x.code == "data_file_not_in_pride_snapshot_inventory" && x.level == "error"));
        assert!(
            issues
                .iter()
                .any(|x| x.code == "data_file_not_in_pride_snapshot_inventory"
                    && x.level == "warning")
        );
    }

    #[test]
    fn resolved_existing_annotation_repository_filename_mismatch_is_warning_not_error() {
        let headers = vec![
            "source name".into(),
            "characteristics[organism]".into(),
            "assay name".into(),
            "technology type".into(),
            "comment[proteomics data acquisition method]".into(),
            "comment[label]".into(),
            "comment[instrument]".into(),
            "comment[cleavage agent details]".into(),
            "comment[fraction identifier]".into(),
            "comment[technical replicate]".into(),
            "comment[data file]".into(),
            SC_SAMPLE_TYPE.into(),
            SC_ISOLATION_METHOD.into(),
            SC_CELL_IDENTIFIER.into(),
        ];
        let rows = vec![vec![
            "cell1".into(),
            "homo sapiens".into(),
            "run1".into(),
            "proteomic profiling by mass spectrometry".into(),
            "data-dependent acquisition".into(),
            "label free sample".into(),
            "Orbitrap".into(),
            "Trypsin".into(),
            "1".into(),
            "1".into(),
            "logical_run.d".into(),
            "single cell".into(),
            "FACS".into(),
            "cell1".into(),
        ]];
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: "/resolved/PXD999999.sdrf.tsv".into(),
            raw_files: vec![RawFile {
                file_name: "submission_bundle.tar.gz".into(),
                file_uri: String::new(),
                category: "RAW".into(),
            }],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let issues = validate_annotation_draft(&headers, &rows, &evidence, true);
        assert!(!issues
            .iter()
            .any(|x| x.code == "data_file_not_in_pride_snapshot_inventory" && x.level == "error"));
        assert!(
            issues
                .iter()
                .any(|x| x.code == "data_file_not_in_pride_snapshot_inventory"
                    && x.level == "warning")
        );
    }

    #[test]
    fn generated_draft_repository_filename_mismatch_remains_error() {
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![RawFile {
                file_name: "known.raw".into(),
                file_uri: String::new(),
                category: "RAW".into(),
            }],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let proposal = SdrfProposal {
            relation_mode: "one_cell_per_data_file".into(),
            organism: "homo sapiens".into(),
            sample_type: "single cell".into(),
            single_cell_isolation_method: "FACS".into(),
            proteomics_data_acquisition_method: "data-dependent acquisition".into(),
            label: "label free sample".into(),
            instrument: "Orbitrap".into(),
            cleavage_agent_details: "Trypsin".into(),
            fraction_identifier: "1".into(),
            technical_replicate: "1".into(),
            ..Default::default()
        };
        let (headers, mut rows, _) = draft_rows(&proposal, &evidence).unwrap();
        let j = header_first_index(&headers, "comment[data file]").unwrap();
        rows[0][j] = "different.raw".into();
        let issues = validate_draft(&headers, &rows, &evidence);
        assert!(issues
            .iter()
            .any(|x| x.code == "data_file_not_in_pride_raw_inventory" && x.level == "error"));
    }

    #[test]
    fn archive_wrapped_vendor_entity_matches_logical_sdrf_filename() {
        let raw = vec![RawFile {
            file_name: "run_001.d.tar.gz".into(),
            file_uri: String::new(),
            category: "RAW".into(),
        }];
        let headers = vec!["comment[data file]".into()];
        let rows = vec![vec!["run_001.d".into()]];
        let stats = data_file_linkage_stats(&headers, &rows, &raw);
        assert_eq!(stats.status, "complete");
        assert_eq!(stats.matched_unique_data_files, 1);
        assert_eq!(stats.unmatched_unique_data_files, 0);
    }

    #[test]
    fn non_single_cell_role_can_leave_cell_specific_values_unresolved_in_audit() {
        let headers = vec![
            "source name".into(),
            "characteristics[organism]".into(),
            "assay name".into(),
            "technology type".into(),
            "comment[proteomics data acquisition method]".into(),
            "comment[label]".into(),
            "comment[instrument]".into(),
            "comment[cleavage agent details]".into(),
            "comment[fraction identifier]".into(),
            "comment[technical replicate]".into(),
            "comment[data file]".into(),
            SC_SAMPLE_TYPE.into(),
            SC_ISOLATION_METHOD.into(),
            SC_CELL_IDENTIFIER.into(),
        ];
        let rows = vec![vec![
            "carrier".into(),
            "homo sapiens".into(),
            "run1".into(),
            "proteomic profiling by mass spectrometry".into(),
            "data-dependent acquisition".into(),
            "TMT126".into(),
            "Orbitrap".into(),
            "Trypsin".into(),
            "1".into(),
            "1".into(),
            "run1.raw".into(),
            "carrier".into(),
            "not available".into(),
            "not available".into(),
        ]];
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![RawFile {
                file_name: "run1.raw".into(),
                file_uri: String::new(),
                category: "RAW".into(),
            }],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let issues = validate_draft_with_policy(
            &headers,
            &rows,
            &evidence,
            DataFileValidationPolicy::AuditSnapshotInventory,
        );
        assert!(!issues.iter().any(|x| x.level == "error"
            && matches!(
                x.code.as_str(),
                "single_cell_isolation_unresolved" | "cell_identifier_invalid_or_unresolved"
            )));
    }

    #[test]
    fn label_free_resolved_sdrf_does_not_require_carrier_or_reference_channels() {
        let headers = vec![
            "source name".into(),
            "characteristics[organism]".into(),
            "characteristics[organism part]".into(),
            "characteristics[disease]".into(),
            "characteristics[cell type]".into(),
            SC_SAMPLE_TYPE.into(),
            SC_ISOLATION_METHOD.into(),
            SC_CELL_IDENTIFIER.into(),
            SC_INDIVIDUAL.into(),
            SC_PREP_BATCH.into(),
            SC_CELLS_PER_WELL.into(),
            "assay name".into(),
            "technology type".into(),
            "comment[proteomics data acquisition method]".into(),
            "comment[label]".into(),
            "comment[instrument]".into(),
            "comment[cleavage agent details]".into(),
            "comment[fraction identifier]".into(),
            "comment[technical replicate]".into(),
            "comment[data file]".into(),
        ];
        let rows = vec![vec![
            "cell1".into(),
            "homo sapiens".into(),
            "brain".into(),
            "normal".into(),
            "neuron".into(),
            "single cell".into(),
            "FACS".into(),
            "cell1".into(),
            "not available".into(),
            "batch1".into(),
            "1".into(),
            "run1".into(),
            "proteomic profiling by mass spectrometry".into(),
            "data-dependent acquisition".into(),
            "label free sample".into(),
            "Orbitrap".into(),
            "Trypsin".into(),
            "1".into(),
            "1".into(),
            "run1.raw".into(),
        ]];
        let (missing, blocking, recommended, optional) =
            audit_gap_fields(&headers, &rows, "one_cell_per_data_file");
        assert!(!missing.contains(&"carrier_channel".to_string()));
        assert!(!missing.contains(&"reference_channel".to_string()));
        assert!(optional.contains(&"individual".to_string()));
        assert!(blocking.is_empty());
        assert!(recommended.is_empty());
    }

    #[test]
    fn denovo_design_scaffold_infers_label_free_single_cell_mapping() {
        let raw = vec![RawFile {
            file_name: "blastomere_01.d.zip".into(),
            file_uri: String::new(),
            category: "RAW".into(),
        }];
        let evidence = vec![EvidenceItem {
            id: "E0001".into(),
            source_kind: "manuscript_text".into(),
            source_label: "methods".into(),
            text: "Label-free quantification of proteins in single embryonic cells and single blastomeres was performed.".into(),
        }];
        let design = infer_study_design_scaffold(&raw, &evidence);
        assert_eq!(design.relation_mode_hint, "one_cell_per_data_file");
        assert_eq!(design.relation_confidence, "high");
        assert_eq!(design.wrapped_acquisition_files, 1);
        assert_eq!(design.generic_archive_files, 0);
        assert!(design.relation_evidence_refs.contains(&"E0001".to_string()));
    }

    #[test]
    fn denovo_design_scaffold_infers_tmt_single_cell_multiplexing() {
        let raw = vec![RawFile {
            file_name: "plex_01.raw".into(),
            file_uri: String::new(),
            category: "RAW".into(),
        }];
        let evidence = vec![EvidenceItem {
            id: "E0002".into(),
            source_kind: "manuscript_text".into(),
            source_label: "methods".into(),
            text: "Single-cell proteomics samples were barcoded with TMTpro and combined with a carrier channel.".into(),
        }];
        let design = infer_study_design_scaffold(&raw, &evidence);
        assert_eq!(design.relation_mode_hint, "multiplexed_cells_per_data_file");
        assert_eq!(design.relation_confidence, "high");
        assert!(design.relation_evidence_refs.contains(&"E0002".to_string()));
    }

    #[test]
    fn denovo_design_scaffold_does_not_cross_substudy_tmt_into_single_cell_branch() {
        let raw = vec![RawFile {
            file_name: "single_cell_01.raw".into(),
            file_uri: String::new(),
            category: "RAW".into(),
        }];
        let evidence = vec![
            EvidenceItem {
                id: "E_SC".into(),
                source_kind: "manuscript_text".into(),
                source_label: "single-cell methods".into(),
                text: "Single-cell proteomics measurements were acquired label-free by data-independent acquisition.".into(),
            },
            EvidenceItem {
                id: "E_BULK".into(),
                source_kind: "manuscript_text".into(),
                source_label: "separate bulk validation".into(),
                text: "A separate bulk validation substudy used iTRAQ labeling for pooled tissue digests.".into(),
            },
        ];
        let design = infer_study_design_scaffold(&raw, &evidence);
        assert_eq!(design.relation_mode_hint, "one_cell_per_data_file");
        assert_eq!(design.relation_confidence, "high");
        assert!(design.relation_evidence_refs.contains(&"E_SC".to_string()));
        assert!(!design
            .relation_evidence_refs
            .contains(&"E_BULK".to_string()));
        assert_eq!(design.multiplex_chemistry_hint, "iTRAQ");
    }

    #[test]
    fn denovo_design_scaffold_plexdia_is_not_isobaric_reporter_evidence() {
        let raw = vec![RawFile {
            file_name: "plexdia_01.raw".into(),
            file_uri: String::new(),
            category: "RAW".into(),
        }];
        let evidence = vec![EvidenceItem {
            id: "E_PLEXDIA".into(),
            source_kind: "manuscript_text".into(),
            source_label: "methods".into(),
            text: "Single-cell proteomics samples were measured with plexDIA.".into(),
        }];
        let design = infer_study_design_scaffold(&raw, &evidence);
        assert_eq!(design.relation_mode_hint, "uncertain");
        assert!(design.multiplex_chemistry_hint.is_empty());
        assert!(design.multiplex_evidence_refs.is_empty());
    }

    #[test]
    fn denovo_design_scaffold_dataset_tmt_without_local_single_cell_link_stays_uncertain() {
        let raw = vec![RawFile {
            file_name: "sample_01.raw".into(),
            file_uri: String::new(),
            category: "RAW".into(),
        }];
        let evidence = vec![
            EvidenceItem {
                id: "E_SC".into(),
                source_kind: "manuscript_text".into(),
                source_label: "single-cell branch".into(),
                text: "Single-cell proteomics samples were prepared for mass spectrometry.".into(),
            },
            EvidenceItem {
                id: "E_TMT".into(),
                source_kind: "manuscript_text".into(),
                source_label: "bulk branch".into(),
                text: "Bulk tissue digests were labeled with TMTpro for a separate experiment."
                    .into(),
            },
        ];
        let design = infer_study_design_scaffold(&raw, &evidence);
        assert_eq!(design.relation_mode_hint, "uncertain");
        assert_eq!(design.multiplex_chemistry_hint, "TMTpro");
        assert!(design.relation_evidence_refs.is_empty());
    }

    #[test]
    fn denovo_design_scaffold_flags_generic_archive_containers() {
        let raw = vec![RawFile {
            file_name: "Figure1_AntigenRetrieval.rar".into(),
            file_uri: String::new(),
            category: "RAW".into(),
        }];
        let design = infer_study_design_scaffold(&raw, &[]);
        assert_eq!(design.repository_file_mode, "generic_archives_only");
        assert_eq!(design.generic_archive_files, 1);
        assert_eq!(design.direct_acquisition_files, 0);
    }

    #[test]
    fn exact_value_match_can_recover_missing_field_provenance() {
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_text".into(),
                source_label: "methods organism".into(),
                text: "Samples were obtained from Homo sapiens donors.".into(),
            }],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let mut proposal = SdrfProposal {
            relation_mode: "uncertain".into(),
            organism: "Homo sapiens".into(),
            ..Default::default()
        };
        let issues = repair_proposal_provenance(&mut proposal, &evidence);
        assert_eq!(proposal.organism, "Homo sapiens");
        assert_eq!(
            proposal.evidence_refs.get("organism"),
            Some(&vec!["E0001".to_string()])
        );
        assert!(issues
            .iter()
            .any(|x| x.code == "proposal_provenance_recovered_by_exact_value_match"));
    }

    #[test]
    fn capillary_electrophoresis_alone_is_not_ms_acquisition_method() {
        assert!(obviously_invalid_field_value(
            "proteomics_data_acquisition_method",
            "Capillary electrophoresis"
        ));
        assert!(!obviously_invalid_field_value(
            "proteomics_data_acquisition_method",
            "data-dependent acquisition"
        ));
    }

    #[test]
    fn microaspiration_is_evidence_relevant_but_not_a_template_value() {
        let item = EvidenceItem {
            id: "E0001".into(),
            source_kind: "manuscript_text".into(),
            source_label: "methods".into(),
            text: "A portion of the neuronal soma was collected by patch-clamp guided microaspiration.".into(),
        };
        assert!(evidence_relevant_to_field(
            "single_cell_isolation_method",
            &item
        ));
        assert!(obviously_invalid_field_value(
            "single_cell_isolation_method",
            "patch-clamp guided microaspiration"
        ));
    }

    #[test]
    fn manuscript_windows_center_on_keyword_in_single_newline_pdf_text() {
        let mut text = String::new();
        for i in 0..2_000 {
            text.push_str(&format!(
                "front matter line {i} describing general proteomics\n"
            ));
        }
        text.push_str("Fibers were mechanically dissociated in ice-cold solution using tweezers and individually transferred to standard tubes.\n");
        for i in 0..500 {
            text.push_str(&format!("trailing line {i}\n"));
        }
        let windows = manuscript_keyword_windows(&text, 8);
        assert!(windows.iter().any(|x| {
            let lower = x.to_ascii_lowercase();
            lower.contains("using tweezers") && lower.contains("individually transferred")
        }));
    }

    #[test]
    fn manual_picking_relevance_gate_accepts_tweezers_and_individual_transfer() {
        let item = EvidenceItem {
            id: "E0001".into(),
            source_kind: "manuscript_text".into(),
            source_label: "manuscript:paper.txt:window=1".into(),
            text: "Fibers were mechanically dissociated using tweezers and individually transferred to standard tubes.".into(),
        };
        assert!(evidence_relevant_to_field(
            "single_cell_isolation_method",
            &item
        ));
        let design = StudyDesignScaffold {
            relation_mode_hint: "one_cell_per_data_file".into(),
            relation_confidence: "high".into(),
            ..Default::default()
        };
        let scaffold = infer_deterministic_metadata_scaffold(&[item], &design);
        assert_eq!(
            scaffold
                .values
                .get("single_cell_isolation_method")
                .map(String::as_str),
            Some("manual picking")
        );
    }

    #[test]
    fn blastomere_dissection_maps_to_manual_picking() {
        let item = EvidenceItem {
            id: "E0001".into(),
            source_kind: "manuscript_text".into(),
            source_label: "manuscript:paper.txt:window=1".into(),
            text: "Using established cell biological tools and protocols, we reproducibly identify and dissect single D11 blastomeres from the embryo.".into(),
        };
        let design = StudyDesignScaffold {
            relation_mode_hint: "one_cell_per_data_file".into(),
            relation_confidence: "high".into(),
            ..Default::default()
        };
        let scaffold = infer_deterministic_metadata_scaffold(&[item], &design);
        assert_eq!(
            scaffold
                .values
                .get("single_cell_isolation_method")
                .map(String::as_str),
            Some("manual picking")
        );
    }

    #[test]
    fn repository_description_without_manual_action_does_not_claim_manual_picking() {
        let item = EvidenceItem {
            id: "E0001".into(),
            source_kind: "pride_project".into(),
            source_label: "project.description".into(),
            text: "Individual fibers were taken from biopsies and analyzed by single fiber proteomics.".into(),
        };
        let design = StudyDesignScaffold {
            relation_mode_hint: "one_cell_per_data_file".into(),
            relation_confidence: "medium".into(),
            ..Default::default()
        };
        let scaffold = infer_deterministic_metadata_scaffold(&[item], &design);
        assert!(!scaffold.values.contains_key("single_cell_isolation_method"));
    }

    #[test]
    fn manuscript_windows_capture_manual_isolation_methods() {
        let text = "Preparation of single muscle fibers\n\nFibers were mechanically dissociated using tweezers and individually transferred to standard tubes.\n\nMass spectrometry followed.";
        let windows = manuscript_keyword_windows(text, 8);
        assert!(windows
            .iter()
            .any(|w| w.to_ascii_lowercase().contains("individually transferred")));
    }

    #[test]
    fn deterministic_scaffold_maps_manual_dissection_to_manual_picking() {
        let evidence = vec![EvidenceItem {
            id: "E0001".into(),
            source_kind: "manuscript_text".into(),
            source_label: "methods isolation".into(),
            text: "Fibers were mechanically dissociated using tweezers and individually transferred to standard tubes.".into(),
        }];
        let design = StudyDesignScaffold {
            relation_mode_hint: "one_cell_per_data_file".into(),
            ..Default::default()
        };
        let scaffold = infer_deterministic_metadata_scaffold(&evidence, &design);
        assert_eq!(
            scaffold
                .values
                .get("single_cell_isolation_method")
                .map(String::as_str),
            Some("manual picking")
        );
        assert!(scaffold.template_gaps.is_empty());
    }

    #[test]
    fn deterministic_scaffold_records_patch_clamp_template_gap() {
        let evidence = vec![EvidenceItem {
            id: "E0001".into(),
            source_kind: "manuscript_text".into(),
            source_label: "methods isolation".into(),
            text: "Cells were accessed in whole-cell patch-clamp configuration and a portion of the neuronal soma was aspirated into the patch clamp probe.".into(),
        }];
        let design = StudyDesignScaffold {
            relation_mode_hint: "multiplexed_cells_per_data_file".into(),
            ..Default::default()
        };
        let scaffold = infer_deterministic_metadata_scaffold(&evidence, &design);
        assert!(!scaffold.values.contains_key("single_cell_isolation_method"));
        assert_eq!(scaffold.template_gaps.len(), 1);
        assert_eq!(
            scaffold.template_gaps[0].observed_value,
            "patch-clamp-guided microaspiration"
        );
    }

    #[test]
    fn multiplex_scaffold_extracts_carrier_channel_hint() {
        let raw = vec![RawFile {
            file_name: "plex.raw".into(),
            file_uri: String::new(),
            category: "RAW".into(),
        }];
        let evidence = vec![EvidenceItem {
            id: "E0001".into(),
            source_kind: "manuscript_text".into(),
            source_label: "methods".into(),
            text: "Single cells were labeled with TMTpro and 200 carrier cells were assigned to the 126N carrier channel.".into(),
        }];
        let design = infer_study_design_scaffold(&raw, &evidence);
        assert_eq!(design.multiplex_chemistry_hint, "TMTpro");
        assert!(design.carrier_channel_hints.contains(&"126N".to_string()));
        assert_eq!(
            design.multiplex_mapping_status,
            "channel_role_hints_detected_mapping_unresolved"
        );
    }

    #[test]
    fn incomplete_multiplex_scaffold_has_one_mapping_error_not_row_error_storm() {
        let proposal = SdrfProposal {
            relation_mode: "multiplexed_cells_per_data_file".into(),
            ..Default::default()
        };
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![
                RawFile {
                    file_name: "a.raw".into(),
                    file_uri: String::new(),
                    category: "RAW".into(),
                },
                RawFile {
                    file_name: "b.raw".into(),
                    file_uri: String::new(),
                    category: "RAW".into(),
                },
            ],
            study_design: StudyDesignScaffold {
                relation_mode_hint: "multiplexed_cells_per_data_file".into(),
                multiplex_chemistry_hint: "TMTpro".into(),
                multiplex_mapping_status: "chemistry_detected_channel_mapping_unresolved".into(),
                ..Default::default()
            },
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let (headers, rows, _) = draft_rows(&proposal, &evidence).unwrap();
        let issues = validate_incomplete_mapping_scaffold(&headers, &rows, &evidence);
        assert_eq!(issues.iter().filter(|x| x.level == "error").count(), 1);
        assert!(issues
            .iter()
            .any(|x| x.code == "sample_to_channel_mapping_unresolved"));
    }

    #[test]
    fn incomplete_uncertain_scaffold_reports_file_relation_not_channel_mapping() {
        let proposal = SdrfProposal {
            relation_mode: "uncertain".into(),
            ..Default::default()
        };
        let evidence = DatasetEvidence {
            accession: "PXD999998".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![RawFile {
                file_name: "a.raw".into(),
                file_uri: String::new(),
                category: "RAW".into(),
            }],
            study_design: StudyDesignScaffold {
                relation_mode_hint: "uncertain".into(),
                multiplex_chemistry_hint: "TMT".into(),
                multiplex_mapping_status: "not_applicable".into(),
                ..Default::default()
            },
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let (headers, rows, _) = draft_rows(&proposal, &evidence).unwrap();
        let issues = validate_incomplete_mapping_scaffold(&headers, &rows, &evidence);
        assert_eq!(issues.iter().filter(|x| x.level == "error").count(), 1);
        assert!(issues
            .iter()
            .any(|x| x.code == "sample_to_file_relation_unresolved"));
        assert!(!issues
            .iter()
            .any(|x| x.code == "sample_to_channel_mapping_unresolved"));
    }

    #[test]
    fn manuscript_windows_prioritize_methods_over_generic_proteomics_text() {
        let mut text = String::new();
        for i in 0..40 {
            text.push_str(&format!("Proteomics background paragraph {i} discusses proteome depth and mass spectrometry.\n\n"));
        }
        text.push_str("Single muscle fibers were manually dissected using tweezers and individually transferred to separate tubes.\n\n");
        let windows = manuscript_keyword_windows(&text, 24);
        assert!(windows.iter().any(|w| {
            let low = w.to_ascii_lowercase();
            low.contains("manually dissected") && low.contains("tweezers")
        }));
    }

    #[test]
    fn manuscript_scan_finds_isolation_after_large_front_matter() {
        let path = std::env::temp_dir().join(format!(
            "pride_scp_sdrf_late_methods_{}_{}.txt",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let mut text = String::new();
        for _ in 0..7000 {
            text.push_str(
                "Background proteomics benchmarking paragraph without isolation details.\n\n",
            );
        }
        text.push_str("Methods\n\nSingle muscle fibers were mechanically dissociated using tweezers and individually transferred to separate tubes.\n");
        std::fs::write(&path, text).unwrap();

        let scanned = read_manuscript_for_keyword_scan(&path).unwrap();
        let windows = manuscript_keyword_windows(&scanned, 24);
        let _ = std::fs::remove_file(&path);

        assert!(windows.iter().any(|w| {
            let lower = w.to_ascii_lowercase();
            lower.contains("using tweezers") && lower.contains("individually transferred")
        }));
    }

    #[test]
    fn denovo_evidence_budget_reserves_room_for_manuscript_windows() {
        let (items, chars) = pre_manuscript_evidence_caps(false, true, 128, 60_000);
        assert_eq!(items, 104);
        assert_eq!(chars, 48_000);
        let mut evidence = Vec::new();
        for i in 0..items {
            push_evidence(
                &mut evidence,
                "existing_annotation",
                format!("annotation:{i}"),
                "single-cell proteomics annotation evidence",
                items,
                chars,
            );
        }
        assert_eq!(evidence.len(), items);
        push_evidence(
            &mut evidence,
            "manuscript_text",
            "manuscript:paper.txt:window=1",
            "Single muscle fibers were mechanically dissociated using tweezers and individually transferred.",
            128,
            60_000,
        );
        assert!(evidence.iter().any(|x| x.source_kind == "manuscript_text"));
    }

    #[test]
    fn existing_sdrf_evidence_budget_is_not_reduced_by_manuscript_reservation() {
        assert_eq!(
            pre_manuscript_evidence_caps(true, true, 128, 60_000),
            (128, 60_000)
        );
    }

    #[test]
    fn raw_file_roles_distinguish_single_few_cell_blank_qc_and_bulk() {
        assert_eq!(raw_file_role("run_1cell_rep1.raw"), RawFileRole::SingleCell);
        assert_eq!(
            raw_file_role("run_40cells_rep1.raw"),
            RawFileRole::FewCell(40)
        );
        assert_eq!(raw_file_role("Blank03_S2-H12.d.zip"), RawFileRole::Blank);
        assert_eq!(
            raw_file_role("plate_QC_01.raw"),
            RawFileRole::QualityControl
        );
        assert_eq!(raw_file_role("HeLa_250pg_rep1.raw"), RawFileRole::Bulk);
    }

    #[test]
    fn one_row_per_file_generation_preserves_non_single_file_roles() {
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![
                RawFile {
                    file_name: "sample_1cell.raw".into(),
                    file_uri: String::new(),
                    category: "RAW".into(),
                },
                RawFile {
                    file_name: "sample_40cells.raw".into(),
                    file_uri: String::new(),
                    category: "RAW".into(),
                },
                RawFile {
                    file_name: "Blank01.raw".into(),
                    file_uri: String::new(),
                    category: "RAW".into(),
                },
                RawFile {
                    file_name: "HeLa_250pg.raw".into(),
                    file_uri: String::new(),
                    category: "RAW".into(),
                },
            ],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let proposal = SdrfProposal {
            relation_mode: "one_cell_per_data_file".into(),
            single_cell_isolation_method: "cellenONE".into(),
            ..Default::default()
        };
        let (headers, rows, mode) = draft_rows(&proposal, &evidence).unwrap();
        assert_eq!(mode, "generated_file_role_aware_one_row_per_raw_file");
        let idx = |name: &str| header_first_index(&headers, name).unwrap();
        assert_eq!(rows[0][idx(SC_SAMPLE_TYPE)], "single cell");
        assert_eq!(rows[0][idx(SC_CELLS_PER_WELL)], "1");
        assert_eq!(rows[1][idx(SC_SAMPLE_TYPE)], "study sample");
        assert_eq!(rows[1][idx(SC_CELLS_PER_WELL)], "40");
        assert_eq!(rows[1][idx(SC_CELL_IDENTIFIER)], "not applicable");
        assert_eq!(rows[2][idx(SC_SAMPLE_TYPE)], "empty");
        assert_eq!(rows[2][idx(SC_ISOLATION_METHOD)], "not applicable");
        assert_eq!(rows[3][idx(SC_SAMPLE_TYPE)], "bulk control");
        assert_eq!(rows[3][idx(SC_ISOLATION_METHOD)], "not applicable");
    }
}
