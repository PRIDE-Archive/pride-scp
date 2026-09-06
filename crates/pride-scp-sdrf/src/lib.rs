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
pub const GENERATOR_VERSION: &str = "pride-scp-sdrf-v0.2.4";
pub const SDRF_SOURCE_RESOLVER_VERSION: &str = "pride-scp-sdrf-source-resolver-v0.1";
pub const SDRF_AUDITOR_VERSION: &str = "pride-scp-sdrf-auditor-v0.1";
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
    pub locally_valid: usize,
    pub requires_review: usize,
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
    missing_target_fields: Vec<String>,
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
    missing_target_fields: usize,
    missing_target_field_names: String,
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
struct DatasetEvidence {
    accession: String,
    project_json_path: String,
    files_json_path: String,
    existing_sdrf_path: String,
    raw_files: Vec<RawFile>,
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
    let mut per_file_rows: BTreeMap<String, usize> = BTreeMap::new();
    let mut per_file_cells: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    let mut per_file_labels: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for row in rows {
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
    let has_multi_cells = per_file_cells.values().any(|x| x.len() > 1);
    let has_multi_labels = per_file_labels.values().any(|x| x.len() > 1);
    let has_multi_rows = per_file_rows.values().any(|&n| n > 1);
    if has_multi_cells || (has_multi_rows && has_multi_labels) {
        return "multiplexed_cells_per_data_file".into();
    }
    if !per_file_cells.is_empty() && per_file_cells.values().all(|x| x.len() == 1) {
        return "one_cell_per_data_file".into();
    }
    if let Some(j) = sample_type_idx {
        let target_rows = rows
            .iter()
            .filter(|r| j < r.len() && r[j].trim().eq_ignore_ascii_case("single cell"))
            .count();
        if target_rows > 0 && !has_multi_rows {
            return "one_cell_per_data_file".into();
        }
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

fn manuscript_keyword_windows(text: &str, max_windows: usize) -> Vec<String> {
    let re = Regex::new(
        r"(?i)single[- ]cell|single[- ]nucle|cellenone|facs|nanopots|tmt|carrier|reference channel|label[- ]free|dia|dda|orbitrap|tims?tof|astral|trypsin|proteom",
    )
    .unwrap();
    let normalized = text.replace('\r', "\n");
    let paragraphs: Vec<&str> = normalized.split("\n\n").collect();
    let mut out = Vec::new();
    for (i, p) in paragraphs.iter().enumerate() {
        if !re.is_match(p) {
            continue;
        }
        let start = i.saturating_sub(1);
        let end = (i + 2).min(paragraphs.len());
        let joined = paragraphs[start..end].join(" ");
        let clipped: String = joined
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
            .chars()
            .take(1800)
            .collect();
        if !clipped.is_empty() && !out.contains(&clipped) {
            out.push(clipped);
        }
        if out.len() >= max_windows {
            break;
        }
    }
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

    let mut evidence = Vec::new();
    // Existing SDRF is the highest-value source only when it contains a real
    // sample-to-data mapping. The PRIDE SDRF endpoint can yield header-only cache
    // files, so file existence alone must never activate the preservation path.
    let usable_existing_sdrf = existing_sdrf_is_usable(&existing_sdrf);
    if usable_existing_sdrf {
        add_existing_sdrf_evidence(
            &mut evidence,
            &existing_sdrf,
            opts.max_evidence_items,
            opts.max_evidence_chars,
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
        opts.max_evidence_items,
        opts.max_evidence_chars,
    );

    let mut annotation_sources = Vec::new();
    for path in annotation_json_paths(&opts.annotations_dir, accession)? {
        if evidence.len() >= opts.max_evidence_items {
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
            opts.max_evidence_items,
            opts.max_evidence_chars,
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
                opts.max_evidence_items,
                opts.max_evidence_chars,
            );
        }
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
    let mut manuscript_sources = Vec::new();
    for path in manuscript_paths {
        if evidence.len() >= opts.max_evidence_items {
            break;
        }
        match read_text_evidence(&path, opts.max_evidence_chars.min(80_000)) {
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

    Ok(DatasetEvidence {
        accession: accession.to_string(),
        project_json_path: project_path.display().to_string(),
        files_json_path: files_path.display().to_string(),
        existing_sdrf_path: usable_existing_sdrf
            .then(|| existing_sdrf.display().to_string())
            .unwrap_or_default(),
        raw_files,
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
TARGET FIELDS: {target_list}\n\
LOCKED/ALREADY-STRUCTURED FIELDS: {locked_list}\n\n\
RULES:\n\
1. Use ONLY the field-specific evidence shown under the matching field heading. Never invent metadata or borrow a value from an unrelated field.\n\
2. Fields in LOCKED/ALREADY-STRUCTURED FIELDS must be returned exactly as 'not available' with an empty evidence_refs list; for relation_mode use 'uncertain'. Rust preserves existing SDRF values deterministically.\n\
3. If a TARGET field is not supported by its own evidence section, return exactly 'not available' (or relation_mode='uncertain').\n\
4. Every concrete TARGET value must cite one or more E#### refs from that SAME field section.\n\
5. relation_mode means sample-to-RAW design: one_cell_per_data_file, multiplexed_cells_per_data_file, mixed, or uncertain. Do not infer it merely from the phrase 'single-cell'.\n\
6. single_cell_isolation_method must be a cell-isolation method such as FACS, cellenONE, microfluidics, laser capture microdissection, manual picking, nanoPOTS, droplet microfluidics, or acoustic droplet ejection. Software such as MaxQuant is never an isolation method.\n\
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
        return fields.iter().map(|x| x.to_string()).collect();
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
            "plex",
            "carrier",
            "reference channel",
            "channel",
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
            "sort",
            "sorting",
            "facs",
            "flow cytometry",
            "cellenone",
            "cellenone",
            "microfluid",
            "laser capture",
            "lcm",
            "manual picking",
            "nanopots",
            "nanowell",
            "droplet",
            "acoustic droplet",
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
        "carrier_channel" => &["carrier channel", "carrier proteome", "carrier", "tmt"],
        "reference_channel" => &["reference channel", "reference sample", "reference", "tmt"],
        _ => &[],
    };
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
            "cellenone",
            "microfluid",
            "laser capture",
            "lcm",
            "manual picking",
            "nanopots",
            "droplet",
            "acoustic droplet",
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

    fn repair_field(
        field: &str,
        value: &mut String,
        refs: &BTreeMap<String, Vec<String>>,
        targets: &BTreeSet<String>,
        issues: &mut Vec<ValidationIssue>,
    ) {
        if field != "relation_mode" {
            if let Some(canonical) = canonical_reserved_alias(value) {
                if value.trim() != canonical {
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

fn draft_rows(
    proposal: &SdrfProposal,
    evidence: &DatasetEvidence,
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
        "generated_one_cell_per_raw_file"
    } else {
        "generated_skeleton_unresolved_mapping"
    };
    for (i, file) in evidence.raw_files.iter().enumerate() {
        let mut row = vec!["not available".to_string(); headers.len()];
        let stem = safe_identifier_from_file(&file.file_name);
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
        set(
            &mut row,
            "comment[fraction identifier]",
            reserved_or(&proposal.fraction_identifier, "1"),
        );
        set(
            &mut row,
            "comment[technical replicate]",
            reserved_or(&proposal.technical_replicate, "1"),
        );
        set(&mut row, "comment[data file]", file.file_name.clone());
        if include_uri {
            set(
                &mut row,
                "comment[file uri]",
                reserved_or(&file.file_uri, "not available"),
            );
        }
        let sample_type = if one_per_file {
            "single cell".to_string()
        } else {
            reserved_or(&proposal.sample_type, "not available")
        };
        set(&mut row, SC_SAMPLE_TYPE, sample_type);
        set(
            &mut row,
            SC_ISOLATION_METHOD,
            reserved_or(&proposal.single_cell_isolation_method, "not available"),
        );
        set(
            &mut row,
            SC_CELL_IDENTIFIER,
            if one_per_file {
                stem
            } else {
                "not available".to_string()
            },
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
        set(
            &mut row,
            SC_CELLS_PER_WELL,
            if one_per_file {
                "1".to_string()
            } else {
                reserved_or(&proposal.cells_per_well, "not available")
            },
        );
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

fn validate_draft(
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
    let valid_files: BTreeSet<String> = evidence
        .raw_files
        .iter()
        .map(|f| data_file_basename(&f.file_name))
        .collect();
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
            if row[j].trim().eq_ignore_ascii_case("not available") {
                issues.push(ValidationIssue { level: "error".into(), code: "single_cell_isolation_unresolved".into(), row: ri + 1, column: SC_ISOLATION_METHOD.into(), message: "single-cell 1.0.0 template does not allow 'not available' for isolation method".into() });
            }
        }
        if let Some(&j) = index.get(SC_CELL_IDENTIFIER) {
            let v = row[j].trim();
            if !cell_id.is_match(v) {
                issues.push(ValidationIssue {
                    level: "error".into(),
                    code: "cell_identifier_invalid_or_unresolved".into(),
                    row: ri + 1,
                    column: SC_CELL_IDENTIFIER.into(),
                    message: format!("cell identifier is not template-valid: {v}"),
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
            let base = data_file_basename(v);
            if !valid_files.contains(&base) {
                issues.push(ValidationIssue {
                    level: "error".into(),
                    code: "data_file_not_in_pride_raw_inventory".into(),
                    row: ri + 1,
                    column: "comment[data file]".into(),
                    message: format!("SDRF data file is not in PRIDE RAW inventory: {v}"),
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
    fs::write(&proposal_path, serde_json::to_string_pretty(&proposal)?)?;
    validate_proposal_refs(&proposal, &evidence)?;
    let proposal_repair_count = provenance_issues.len();
    let existing = !evidence.existing_sdrf_path.is_empty();

    let (headers, rows, generation_mode) = draft_rows(&proposal, &evidence)?;
    write_sdrf(&draft_path, &headers, &rows)?;
    let mut issues = provenance_issues;
    issues.extend(validate_draft(&headers, &rows, &evidence));
    write_review(&review_path, &issues)?;
    let errors = issues.iter().filter(|x| x.level == "error").count();
    let locally_valid = errors == 0;
    let completeness = if existing && locally_valid {
        "existing_sdrf_enriched_locally_valid"
    } else if existing {
        "existing_sdrf_enriched_requires_review"
    } else if proposal.relation_mode == "one_cell_per_data_file" && locally_valid {
        "locally_valid_draft"
    } else if proposal.relation_mode != "one_cell_per_data_file" {
        "incomplete_sample_to_file_or_channel_mapping"
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
        raw_file_count: evidence.raw_files.len(), evidence_item_count: evidence.evidence.len(), manuscript_source_count: evidence.manuscript_sources.len(),
        annotation_source_count: evidence.annotation_sources.len(), ollama_model: opts.model.clone(), ollama_used,
        draft_path: draft_path.display().to_string(), proposal_path: proposal_path.display().to_string(), evidence_path: evidence_path.display().to_string(),
        review_path: review_path.display().to_string(), validation_issue_count: issues.len(), validation_error_count: errors, proposal_repair_count, locally_valid,
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
        evidence: Vec::new(),
        manuscript_sources: Vec::new(),
        annotation_sources: Vec::new(),
    };
    let relation_mode = existing_sdrf_relation_hint(&headers, &rows);
    let issues = validate_draft(&headers, &rows, &evidence);
    let errors = issues.iter().filter(|x| x.level == "error").count();
    let warnings = issues.iter().filter(|x| x.level == "warning").count();
    let missing = proposal_target_fields(&evidence)
        .into_iter()
        .collect::<Vec<_>>();
    let locally_valid = errors == 0;
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
        missing_target_fields: missing.clone(),
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
        missing_target_fields: missing.len(),
        missing_target_field_names: missing.join(";"),
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
                    eprintln!(
                        "  -> {} rows={} valid={} errors={} missing_fields={}",
                        row.source_kind,
                        row.rows,
                        row.locally_valid,
                        row.validation_errors,
                        row.missing_target_fields
                    );
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
                    missing_target_fields: 0,
                    missing_target_field_names: String::new(),
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
        requires_review: rows
            .iter()
            .filter(|r| r.status == "audited" && (!r.locally_valid || r.missing_target_fields > 0))
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
    fn multiplexed_skeleton_is_explicitly_invalid_until_mapping_exists() {
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
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
    fn unsupported_asserted_relation_mode_is_downgraded_not_fatal() {
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
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
}
