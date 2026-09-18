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
pub const GENERATOR_VERSION: &str = "pride-scp-sdrf-v0.5.4";
pub const DESIGN_AGENT_VERSION: &str = "pride-scp-design-agent-v0.4";
pub const VALIDATOR_REPAIR_AGENT_VERSION: &str = "pride-scp-validator-repair-v0.2";
const DESIGN_AGENT_MAX_ROUNDS: usize = 3;
const VALIDATOR_REPAIR_MAX_ROUNDS: usize = 1;
const VALIDATOR_REPAIR_MAX_TASKS: usize = 4;
const VALIDATOR_REPAIR_MAX_QUERIES_PER_TASK: usize = 4;

fn sdrf_annotation_tool_value() -> String {
    let version = GENERATOR_VERSION
        .strip_prefix("pride-scp-sdrf-")
        .unwrap_or(GENERATOR_VERSION);
    format!("pride-scp-sdrf {version}")
}
const MANUSCRIPT_SCAN_MAX_CHARS: usize = 2_000_000;
const MANUSCRIPT_EVIDENCE_MAX_RESERVED_ITEMS: usize = 24;
const MANUSCRIPT_EVIDENCE_MAX_RESERVED_CHARS: usize = 12_000;
pub const SDRF_SOURCE_RESOLVER_VERSION: &str = "pride-scp-sdrf-source-resolver-v0.1";
pub const SDRF_AUDITOR_VERSION: &str = "pride-scp-sdrf-auditor-v0.3";
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
struct EvidenceQuery {
    #[serde(rename = "match")]
    match_kind: String,
    #[serde(default)]
    value: String,
    #[serde(default)]
    terms: Vec<String>,
    #[serde(default)]
    document_hint: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct EvidenceActionRequest {
    action: String,
    reason: String,
    #[serde(default)]
    target_fields: Vec<String>,
    #[serde(default)]
    queries: Vec<EvidenceQuery>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct EvidenceActionResult {
    round: usize,
    action: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    normalized_from_action: String,
    query: EvidenceQuery,
    outcome: String,
    #[serde(default)]
    matched_evidence_refs: Vec<String>,
    summary: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct FieldScopeClaim {
    field: String,
    scope: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    confidence: String,
    reason: String,
    #[serde(default)]
    claim_origin: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct RelationAssessment {
    mode: String,
    scope: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    confidence: String,
    reason: String,
    #[serde(default)]
    claim_origin: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct CandidateDesignGroup {
    id: String,
    description: String,
    status: String,
    source_basis: String,
    confidence: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    #[serde(default)]
    linked_raw_files: Vec<String>,
    linkage_status: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct DatasetDesignAssessment {
    agent_version: String,
    design_homogeneous: bool,
    #[serde(default)]
    relation_assessment: RelationAssessment,
    #[serde(default)]
    biological_axes: Vec<String>,
    #[serde(default)]
    experimental_axes: Vec<String>,
    #[serde(default)]
    candidate_groups: Vec<CandidateDesignGroup>,
    #[serde(default)]
    field_scopes: Vec<FieldScopeClaim>,
    #[serde(default)]
    conflicts: Vec<String>,
    #[serde(default)]
    missing_linkages: Vec<String>,
    #[serde(default)]
    next_evidence_actions: Vec<EvidenceActionRequest>,
    #[serde(default)]
    terminal_status: String,
    notes: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct DesignAgentTrace {
    initial_assessment: DatasetDesignAssessment,
    #[serde(default)]
    evidence_action_results: Vec<EvidenceActionResult>,
    final_assessment: DatasetDesignAssessment,
    rounds_completed: usize,
    #[serde(default)]
    deferred_evidence_actions: Vec<EvidenceActionRequest>,
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

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct ValidatorRepairTask {
    field: String,
    #[serde(default)]
    error_codes: Vec<String>,
    error_count: usize,
    #[serde(default)]
    representative_rows: Vec<usize>,
    #[serde(default)]
    representative_messages: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct ValidatorRepairDecision {
    field: String,
    resolution: String,
    scope: String,
    value: String,
    #[serde(default)]
    evidence_refs: Vec<String>,
    confidence: String,
    reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct ValidatorRepairAssessment {
    repair_agent_version: String,
    #[serde(default)]
    decisions: Vec<ValidatorRepairDecision>,
    #[serde(default)]
    next_evidence_actions: Vec<EvidenceActionRequest>,
    terminal_status: String,
    notes: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct ValidatorRepairTrace {
    repair_agent_version: String,
    #[serde(default)]
    tasks: Vec<ValidatorRepairTask>,
    initial_assessment: ValidatorRepairAssessment,
    #[serde(default)]
    evidence_action_results: Vec<EvidenceActionResult>,
    final_assessment: ValidatorRepairAssessment,
    rounds_completed: usize,
    #[serde(default)]
    applied_fields: Vec<String>,
    #[serde(default)]
    deterministic_repairs: Vec<String>,
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
    #[serde(default)]
    design_agent_version: String,
    #[serde(default)]
    design_assessment_path: String,
    #[serde(default)]
    evidence_actions_path: String,
    #[serde(default)]
    validator_repair_agent_version: String,
    #[serde(default)]
    validator_repair_path: String,
    #[serde(default)]
    validator_repair_actions_path: String,
    #[serde(default)]
    validator_repair_rounds: usize,
    #[serde(default)]
    validator_repair_task_count: usize,
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
    #[serde(default)]
    agent_terminal_status: String,
    #[serde(default)]
    mapping_status: String,
    #[serde(default)]
    template_status: String,
    #[serde(default)]
    metadata_status: String,
    #[serde(default)]
    evidence_status: String,
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
    agent_terminal_status: String,
    mapping_status: String,
    template_status: String,
    metadata_status: String,
    evidence_status: String,
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

fn source_name_explicit_bulk_role(name: &str) -> bool {
    let normalized = name
        .trim()
        .to_ascii_lowercase()
        .replace('-', "_")
        .replace('.', "_")
        .replace(' ', "_");
    normalized.split('_').any(|token| token == "bulk")
}

fn label_is_clearly_nonisobaric(label: &str) -> bool {
    let normalized = label.trim().to_ascii_lowercase();
    ["label free", "label-free", "dimethyl", "silac"]
        .iter()
        .any(|token| normalized.contains(token))
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
        || hay.contains("eyebrow-hair knife")
        || hay.contains("eyebrow hair knife")
        || ((hay.contains("individual cell") || hay.contains("individual cells"))
            && hay.contains("cut and isolated"))
        || (hay.contains("hydrodynamic")
            && (hay.contains("single cell") || hay.contains("single-cell"))
            && (hay.contains("load") || hay.contains("inject")))
        || (hay.contains("mechanically dissociated") && hay.contains("tweezer"))
        || (hay.contains("mechanically dissociated") && hay.contains("individually transferred"))
}

fn isolation_evidence_source_score(item: &EvidenceItem) -> i32 {
    let label = item.source_label.to_ascii_lowercase();
    let mut score = match item.source_kind.as_str() {
        "existing_sdrf_structured" => 120,
        "manuscript_semantic_evidence" => 100,
        "existing_annotation" => 90,
        "manuscript_text" => 80,
        "pride_project" => 50,
        _ => 40,
    };
    if label.contains("single_cell_isolation_method")
        || label.contains("single cell isolation")
        || label.contains("isolation method")
    {
        score += 30;
    } else if label.contains("method") || label.contains("isolation") {
        score += 10;
    }
    score
}

fn explicit_facs_evidence(hay: &str) -> bool {
    let has_method = hay.contains("facs") || hay.contains("fluorescence-activated cell sort");
    let has_context = hay.contains("cell")
        && (hay.contains("sort")
            || hay.contains("isolat")
            || hay.contains("collect")
            || hay.contains("enrich"));
    has_method && has_context
}

fn infer_isolation_method_scaffold(evidence: &[EvidenceItem]) -> Option<(String, Vec<String>)> {
    let mut candidates: BTreeMap<String, (i32, Vec<String>)> = BTreeMap::new();

    for item in evidence {
        if !evidence_relevant_to_field("single_cell_isolation_method", item) {
            continue;
        }
        let hay = evidence_hay(item);
        let source_score = isolation_evidence_source_score(item);
        let mut observed: Vec<(&str, i32)> = Vec::new();

        if manual_picking_evidence(&hay) {
            observed.push(("manual picking", 30));
        }
        if explicit_facs_evidence(&hay) {
            observed.push(("FACS", 20));
        }
        if hay.contains("cellenone") {
            observed.push(("cellenONE", 25));
        }
        if hay.contains("laser capture microdissection")
            || (hay.contains("laser capture") && hay.contains("cell"))
            || (hay.contains(" lcm ") && hay.contains("cell"))
        {
            observed.push(("laser capture microdissection", 25));
        }
        if hay.contains("nanopots") {
            observed.push(("nanoPOTS", 15));
        }
        if hay.contains("droplet microfluid") {
            observed.push(("droplet microfluidics", 20));
        }
        if hay.contains("acoustic droplet") {
            observed.push(("acoustic droplet ejection", 20));
        }
        if hay.contains("microfluid") && !hay.contains("droplet microfluid") {
            observed.push(("microfluidics", 10));
        }

        for (value, method_score) in observed {
            let score = source_score + method_score;
            let entry = candidates
                .entry(value.to_string())
                .or_insert_with(|| (score, Vec::new()));
            if score > entry.0 {
                entry.0 = score;
                entry.1.clear();
            }
            if score == entry.0 && entry.1.len() < 8 && !entry.1.contains(&item.id) {
                entry.1.push(item.id.clone());
            }
        }
    }

    let mut ranked = candidates
        .into_iter()
        .filter(|(_, (score, refs))| *score >= 70 && !refs.is_empty())
        .collect::<Vec<_>>();
    ranked.sort_by(|a, b| b.1 .0.cmp(&a.1 .0).then_with(|| a.0.cmp(&b.0)));

    let (value, (top_score, refs)) = ranked.first()?.clone();
    if let Some((_, (second_score, _))) = ranked.get(1) {
        // Conflicting high-quality isolation methods are a branch/mapping problem,
        // not a reason to lock one dataset-wide value deterministically.
        if top_score - *second_score < 15 {
            return None;
        }
    }
    Some((value, refs))
}

fn infer_isolation_template_gap(evidence: &[EvidenceItem]) -> Option<TemplateCompatibilityGap> {
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
            &[
                "using capillary microsampling",
                "by capillary microsampling",
                "via capillary microsampling",
                "capillary microsampling was used",
                "capillary microsampling was performed",
                "collected by capillary microsampling",
                "sampled by capillary microsampling",
                "using in situ subcellular sampling",
                "in situ subcellular sampling was used",
                "in situ subcellular proteomics was performed",
            ] as &[&str],
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
            return Some(TemplateCompatibilityGap {
                field: "single_cell_isolation_method".into(),
                observed_value: observed.into(),
                evidence_refs: refs,
                reason: "the pinned single-cell 1.0.0 isolation-method vocabulary does not contain a faithful term for this experimentally supported sampling method; do not substitute a false allowed value".into(),
            });
        }
    }
    None
}

fn infer_acquisition_method_repair(evidence: &[EvidenceItem]) -> Option<(String, Vec<String>)> {
    let dda_refs = refs_for_predicate(evidence, "proteomics_data_acquisition_method", |hay| {
        hay.contains("data-dependent") || hay.contains("data dependent") || hay.contains(" dda ")
    });
    let dia_refs = refs_for_predicate(evidence, "proteomics_data_acquisition_method", |hay| {
        hay.contains("data-independent")
            || hay.contains("data independent")
            || hay.contains("dia-pasef")
            || hay.contains("diapasef")
            || hay.contains("swath")
            || hay.contains(" dia ")
    });
    match (dda_refs.is_empty(), dia_refs.is_empty()) {
        (false, true) => Some((
            "NT=data-dependent acquisition;AC=PRIDE:0000627".into(),
            dda_refs,
        )),
        (true, false) => Some(("Data-independent acquisition".into(), dia_refs)),
        _ => None,
    }
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
                "Data-independent acquisition".into(),
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

    let mut isolation_set = false;
    if let Some((value, refs)) = infer_isolation_method_scaffold(evidence) {
        metadata_scaffold_insert(&mut out, "single_cell_isolation_method", value, refs);
        isolation_set = true;
    }
    if !isolation_set {
        if let Some(gap) = infer_isolation_template_gap(evidence) {
            out.template_gaps.push(gap);
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

fn design_assessment_schema() -> Value {
    let canonical_fields = vec![
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
    let evidence_query = json!({
        "type":"object",
        "properties":{
            "match":{"type":"string","enum":["raw_exact","phrase","terms_all","terms_any","identifier","doi"]},
            "value":{"type":"string","maxLength":300},
            "terms":{"type":"array","items":{"type":"string","maxLength":120},"maxItems":12},
            "document_hint":{"type":"string","maxLength":240}
        },
        "required":["match","value","terms","document_hint"],
        "additionalProperties":false
    });
    json!({
        "type": "object",
        "properties": {
            "agent_version": {"type":"string","maxLength":80},
            "design_homogeneous": {"type":"boolean"},
            "relation_assessment": {"type":"object","properties":{
                "mode":{"type":"string","enum":["one_cell_per_data_file","multiplexed_cells_per_data_file","mixed","unresolved"]},
                "scope":{"type":"string","enum":["project","branch","unresolved"]},
                "evidence_refs":{"type":"array","items":{"type":"string","pattern":"^E[0-9]{4}$"},"maxItems":12},
                "confidence":{"type":"string","enum":["high","medium","low"]},
                "reason":{"type":"string","maxLength":400},
                "claim_origin":{"type":"string","enum":["model_explicit"]}
            },"required":["mode","scope","evidence_refs","confidence","reason","claim_origin"],"additionalProperties":false},
            "biological_axes": {"type":"array","items":{"type":"string","maxLength":120},"maxItems":16},
            "experimental_axes": {"type":"array","items":{"type":"string","maxLength":120},"maxItems":16},
            "candidate_groups": {"type":"array","maxItems":24,"items":{"type":"object","properties":{
                "id":{"type":"string","maxLength":40},
                "description":{"type":"string","maxLength":300},
                "status":{"type":"string","enum":["supported","search_hint","rejected"]},
                "source_basis":{"type":"string","enum":["source_evidence","filename_hint"]},
                "confidence":{"type":"string","enum":["high","medium","low"]},
                "evidence_refs":{"type":"array","items":{"type":"string","pattern":"^E[0-9]{4}$"},"maxItems":16},
                "linked_raw_files":{"type":"array","items":{"type":"string","maxLength":300},"maxItems":64},
                "linkage_status":{"type":"string","enum":["supported","partial","unresolved"]}
            },"required":["id","description","status","source_basis","confidence","evidence_refs","linked_raw_files","linkage_status"],"additionalProperties":false}},
            "field_scopes": {"type":"array","maxItems":32,"items":{"type":"object","properties":{
                "field":{"type":"string","enum":canonical_fields.clone()},
                "scope":{"type":"string","enum":["project","group","row","unresolved"]},
                "evidence_refs":{"type":"array","items":{"type":"string","pattern":"^E[0-9]{4}$"},"maxItems":12},
                "confidence":{"type":"string","enum":["high","medium","low"]},
                "reason":{"type":"string","maxLength":300},
                "claim_origin":{"type":"string","enum":["model_explicit"]}
            },"required":["field","scope","evidence_refs","confidence","reason","claim_origin"],"additionalProperties":false}},
            "conflicts": {"type":"array","items":{"type":"string","maxLength":300},"maxItems":24},
            "missing_linkages": {"type":"array","items":{"type":"string","maxLength":300},"maxItems":24},
            "next_evidence_actions": {"type":"array","maxItems":6,"items":{"type":"object","properties":{
                "action":{"type":"string","enum":["SEARCH_PUBLICATION","SEARCH_SUPPLEMENT","SEARCH_STRUCTURED_DESIGN","SEARCH_REPOSITORY_METADATA","SEARCH_EXACT_RAW_NAME","EXPAND_EVIDENCE_CONTEXT","LOOKUP_KG_TERM","COMPARE_CONFLICTING_EVIDENCE","ABSTAIN"]},
                "reason":{"type":"string","maxLength":300},
                "target_fields":{"type":"array","items":{"type":"string","enum":canonical_fields.clone()},"maxItems":12},
                "queries":{"type":"array","items":evidence_query,"maxItems":8}
            },"required":["action","reason","target_fields","queries"],"additionalProperties":false}},
            "terminal_status":{"type":"string","enum":["continue","resolved","partial","evidence_exhausted","abstained"]},
            "notes": {"type":"string","maxLength":1200}
        },
        "required": ["agent_version","design_homogeneous","relation_assessment","biological_axes","experimental_axes","candidate_groups","field_scopes","conflicts","missing_linkages","next_evidence_actions","terminal_status","notes"],
        "additionalProperties": false
    })
}

fn design_evidence_block(evidence: &DatasetEvidence, max_items: usize) -> String {
    evidence
        .evidence
        .iter()
        .take(max_items)
        .map(|item| {
            let text = item.text.replace('\n', " ");
            let text = if text.chars().count() > 900 {
                text.chars().take(900).collect::<String>() + "..."
            } else {
                text
            };
            format!(
                "{} [{}:{}] {}",
                item.id, item.source_kind, item.source_label, text
            )
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn design_assessment_prompt(
    evidence: &DatasetEvidence,
    max_files: usize,
    action_results: &[EvidenceActionResult],
) -> String {
    let files = evidence
        .raw_files
        .iter()
        .take(max_files)
        .map(|f| format!("- {}", f.file_name))
        .collect::<Vec<_>>()
        .join("\n");
    let results = if action_results.is_empty() {
        "none (initial planning pass)".into()
    } else {
        action_results
            .iter()
            .map(|r| {
                let query = serde_json::to_string(&r.query).unwrap_or_else(|_| "{}".into());
                format!(
                    "- round={} {} query={} outcome={} refs={:?}: {}",
                    r.round, r.action, query, r.outcome, r.matched_evidence_refs, r.summary
                )
            })
            .collect::<Vec<_>>()
            .join("\n")
    };
    format!(
        "You are the evidence-planning stage of a provenance-first SDRF annotation agent for dataset {acc}.\n\n\
Your job is NOT to fill SDRF values yet. Reconstruct the experimental design, explicitly assess the sample-to-file/channel relation architecture, assign canonical SDRF fields to project/group/row/unresolved scope, identify conflicts and missing linkage, and request bounded evidence actions when needed.\n\n\
SCIENTIFIC CONTRACT:\n\
1. Never treat a filename token as biological truth. Filename-derived possibilities must be candidate_groups with status='search_hint', source_basis='filename_hint', empty evidence_refs, empty linked_raw_files, and linkage_status='unresolved'. They may generate retrieval queries only.\n\
2. A candidate group may have status='supported' only when one or more supplied E#### references directly support the biological/experimental group. source_basis must then be 'source_evidence'.\n\
3. linked_raw_files may be populated only when cited evidence explicitly names or otherwise source-links those RAW files to that group. Filename resemblance alone is insufficient.\n\
4. relation_assessment is REQUIRED on every pass and answers ACQUISITION CARDINALITY ONLY: how many biological acquisition units/channels are represented by each data file. It does NOT answer which organism, condition, sex, cell type, treatment, replicate, or other biological identity belongs to a file. Use one_cell_per_data_file when one biological acquisition unit/sample contributes to one acquisition file; despite the legacy name, the unit may be a cell, fiber, digest, or other sample. Use multiplexed_cells_per_data_file only with locally linked reporter/channel evidence. Use mixed only when trusted evidence shows genuinely different acquisition-cardinality regimes (for example one-sample-per-file plus pooled/composite or reporter-multiplexed acquisitions). Use unresolved only when acquisition cardinality itself cannot be established. Missing control-vs-stroke, HeLa-vs-THP1, organism, sex, condition, replicate, or other row-attribute linkage MUST NOT by itself change a concrete relation mode to mixed/unresolved. scope='project' means the same acquisition cardinality applies across all repository acquisitions even if biological groups differ; scope='branch' is only for a cardinality conclusion that is established for one acquisition branch but not the others. Cite relation evidence refs whenever available.\n\
5. The deterministic relation hint below is diagnostic evidence, not unquestionable truth. If it has high confidence, cites trusted evidence, and you find no contradictory ACQUISITION-CARDINALITY evidence, you SHOULD normally adopt the same relation mode. Biological heterogeneity alone is not a contradiction. If the same cardinality applies to every repository acquisition, prefer scope='project'; use scope='branch' only when another acquisition branch has a different or unresolved cardinality. If you disagree with the deterministic hint, explain the acquisition-structure conflict explicitly.\n\
6. Use field_scopes with canonical field identifiers only. Each model-produced claim must have claim_origin='model_explicit'. scope='project' is allowed only when the same concrete value is supported across the dataset. If multiple organisms/cell lines/sample roles/regimes exist, affected fields must be group, row, or unresolved. You MAY omit an ordinary field scope when you have no scientific opinion; Rust treats omission as DEFER rather than a veto of deterministic evidence.\n\
7. A model-explicit non-project claim is a scientific veto of dataset-wide broadcasting. Use it only when supported by evidence/conflict, not merely because a field was not discussed.\n\
8. Evidence queries are TYPED. Do not write natural-language search instructions inside a query. Use match='raw_exact' with value equal to an actual RAW/mzML basename shown below; use phrase for a literal phrase; terms_all/terms_any with short atomic terms; identifier for sample/accession identifiers; doi for a DOI. document_hint may contain a DOI or source identifier.\n\
9. SEARCH_EXACT_RAW_NAME accepts only raw_exact queries whose value exactly matches a repository acquisition basename. Semantic terms such as K562, MCF-7, 32-cell, sex, animal, or neuron must use SEARCH_PUBLICATION, SEARCH_REPOSITORY_METADATA, SEARCH_STRUCTURED_DESIGN, or SEARCH_SUPPLEMENT with phrase/terms/identifier queries.\n\
10. The ACTION HISTORY below is authoritative. Any identical typed action/query with outcome=no_match, invalid_query, or duplicate_skipped MUST NOT be requested again. Escalate to another evidence source or ABSTAIN.\n\
11. LOOKUP_KG_TERM may clarify terminology/synonyms but never accession-specific row mapping.\n\
12. terminal_status='continue' only when you are requesting executable evidence actions now. Use resolved when the design needed for safe serialization is established; partial when useful design is established but unresolved linkage remains and no further current retrieval is necessary; abstained when evidence cannot support a safe decision. Rust may set evidence_exhausted when the bounded round budget ends.\n\
13. Keep retrieval bounded: at most six actions in this assessment.\n\n\
DETERMINISTIC STUDY-DESIGN HINT (diagnostic evidence):\n\
relation_mode_hint={relation}; confidence={confidence}; evidence_refs={relation_refs:?}; repository_file_mode={repo_mode}; note={note}\n\n\
RAW FILE COUNT: {nfiles}\nRAW FILE SAMPLE (search context only; max {max_files}):\n{files}\n\n\
EVIDENCE INVENTORY:\n{evidence_block}\n\n\
ACTION HISTORY / RESULTS:\n{results}\n\n\
TYPED QUERY EXAMPLES:\n\
- exact repository RAW linkage: {{\"match\":\"raw_exact\",\"value\":\"sample_01.raw\",\"terms\":[],\"document_hint\":\"\"}}\n\
- publication semantic search: {{\"match\":\"terms_all\",\"value\":\"\",\"terms\":[\"K562\",\"sample\"],\"document_hint\":\"10.xxxx/example\"}}\n\
- structured design identifier search: {{\"match\":\"identifier\",\"value\":\"E34\",\"terms\":[],\"document_hint\":\"\"}}\n\n\
Return a structured DatasetDesignAssessment. Set agent_version exactly to {agent_version} and claim_origin='model_explicit' for every relation/field claim you emit.",
        acc=evidence.accession,
        relation=evidence.study_design.relation_mode_hint,
        confidence=evidence.study_design.relation_confidence,
        relation_refs=&evidence.study_design.relation_evidence_refs,
        repo_mode=evidence.study_design.repository_file_mode,
        note=evidence.study_design.notes,
        nfiles=evidence.raw_files.len(),
        evidence_block=design_evidence_block(evidence, 36),
        agent_version=DESIGN_AGENT_VERSION,
    )
}

async fn call_ollama_design_assessment(
    opts: &SdrfAnnotateOptions,
    evidence: &DatasetEvidence,
    action_results: &[EvidenceActionResult],
) -> Result<DatasetDesignAssessment> {
    let client = Client::builder()
        .timeout(Duration::from_secs(opts.timeout_seconds))
        .build()?;
    let payload = json!({
        "model": opts.model,
        "prompt": design_assessment_prompt(evidence, opts.max_files_in_prompt, action_results),
        "stream": false,
        "think": false,
        "format": design_assessment_schema(),
        "options": {"temperature": 0.0}
    });
    let response = client
        .post(&opts.ollama_url)
        .json(&payload)
        .send()
        .await
        .with_context(|| {
            format!(
                "Ollama design-assessment request for {}",
                evidence.accession
            )
        })?;
    let status = response.status();
    let body: Value = response
        .json()
        .await
        .context("decode Ollama design-assessment response")?;
    if !status.is_success() {
        bail!("Ollama design-assessment HTTP {status}: {body}");
    }
    let raw = body.get("response").and_then(Value::as_str).unwrap_or("");
    if raw.trim().is_empty() {
        bail!("Ollama returned empty design assessment");
    }
    let mut assessment: DatasetDesignAssessment =
        serde_json::from_str(raw).context("parse structured Ollama design assessment")?;
    normalize_design_assessment(evidence, &mut assessment);
    Ok(assessment)
}

fn is_canonical_design_field(field: &str) -> bool {
    matches!(
        field,
        "organism"
            | "organism_part"
            | "disease"
            | "cell_type"
            | "sample_type"
            | "single_cell_isolation_method"
            | "individual"
            | "sample_preparation_batch"
            | "cells_per_well"
            | "proteomics_data_acquisition_method"
            | "label"
            | "instrument"
            | "cleavage_agent_details"
            | "fraction_identifier"
            | "technical_replicate"
            | "carrier_channel"
            | "reference_channel"
    )
}

fn normalize_evidence_query(query: &mut EvidenceQuery) {
    query.match_kind = query.match_kind.trim().to_ascii_lowercase();
    query.value = query.value.trim().to_string();
    query.document_hint = query.document_hint.trim().to_string();
    query.terms = query
        .terms
        .iter()
        .map(|term| term.trim().to_string())
        .filter(|term| !term.is_empty())
        .collect();
    query.terms.sort_by_key(|term| term.to_ascii_lowercase());
    query.terms.dedup_by(|a, b| a.eq_ignore_ascii_case(b));
}

fn normalize_design_assessment(
    evidence: &DatasetEvidence,
    assessment: &mut DatasetDesignAssessment,
) {
    assessment.agent_version = DESIGN_AGENT_VERSION.into();
    let valid_refs: BTreeSet<&str> = evidence
        .evidence
        .iter()
        .map(|item| item.id.as_str())
        .collect();

    let relation = &mut assessment.relation_assessment;
    relation
        .evidence_refs
        .retain(|r| valid_refs.contains(r.as_str()));
    relation.evidence_refs.sort();
    relation.evidence_refs.dedup();
    if relation.claim_origin.is_empty() {
        relation.claim_origin = "model_explicit".into();
    }
    if !matches!(
        relation.mode.as_str(),
        "one_cell_per_data_file" | "multiplexed_cells_per_data_file" | "mixed" | "unresolved"
    ) {
        relation.mode = "unresolved".into();
        relation.scope = "unresolved".into();
        relation.confidence = "low".into();
        relation.claim_origin = "default_missing".into();
        relation.reason = "invalid or missing relation mode; defer to deterministic evidence unless an explicit conflict is established".into();
    }
    if !matches!(relation.scope.as_str(), "project" | "branch" | "unresolved") {
        relation.scope = "unresolved".into();
        relation.confidence = "low".into();
        relation.claim_origin = "default_missing".into();
        relation.reason = "invalid or missing relation scope; defer to deterministic evidence unless an explicit conflict is established".into();
    }
    if relation.mode == "mixed" && relation.scope == "project" {
        relation.scope = "branch".into();
        relation.reason = format!(
            "mixed relation architecture cannot be project-uniform; {}",
            relation.reason
        );
    }
    if relation.scope == "project" && relation.evidence_refs.is_empty() {
        relation.mode = "unresolved".into();
        relation.scope = "unresolved".into();
        relation.confidence = "low".into();
        relation.claim_origin = "default_missing".into();
        relation.reason = format!(
            "project relation claim lacked trusted evidence and was converted to DEFER; {}",
            relation.reason
        );
    }

    for group in &mut assessment.candidate_groups {
        group
            .evidence_refs
            .retain(|r| valid_refs.contains(r.as_str()));
        group.evidence_refs.sort();
        group.evidence_refs.dedup();

        if group.evidence_refs.is_empty() {
            group.status = "search_hint".into();
            group.source_basis = "filename_hint".into();
            group.linked_raw_files.clear();
            group.linkage_status = "unresolved".into();
            if group.confidence == "high" {
                group.confidence = "low".into();
            }
            continue;
        }

        if group.status == "supported" {
            group.source_basis = "source_evidence".into();
        }

        if !group.linked_raw_files.is_empty() {
            let refs = group
                .evidence_refs
                .iter()
                .filter_map(|id| evidence.evidence.iter().find(|item| item.id == id.as_str()))
                .collect::<Vec<_>>();
            let all_explicit = group.linked_raw_files.iter().all(|raw| {
                let raw_lc = raw.to_ascii_lowercase();
                refs.iter().any(|item| {
                    format!("{}\n{}", item.source_label, item.text)
                        .to_ascii_lowercase()
                        .contains(&raw_lc)
                })
            });
            if !all_explicit {
                group.linked_raw_files.clear();
                group.linkage_status = "unresolved".into();
            }
        }
    }

    assessment.field_scopes.retain_mut(|claim| {
        claim
            .evidence_refs
            .retain(|r| valid_refs.contains(r.as_str()));
        claim.evidence_refs.sort();
        claim.evidence_refs.dedup();
        if claim.claim_origin.is_empty() {
            claim.claim_origin = "model_explicit".into();
        }
        if claim.scope == "project" && claim.evidence_refs.is_empty() {
            claim.scope = "unresolved".into();
            claim.confidence = "low".into();
            claim.claim_origin = "default_missing".into();
            claim.reason = format!(
                "project scope lacked trusted evidence and was converted to DEFER; {}",
                claim.reason
            );
        }
        is_canonical_design_field(&claim.field)
    });

    let mut seen = BTreeSet::new();
    assessment
        .field_scopes
        .retain(|claim| seen.insert(claim.field.clone()));

    for field in [
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
    ] {
        if !assessment
            .field_scopes
            .iter()
            .any(|claim| claim.field == field)
        {
            assessment.field_scopes.push(FieldScopeClaim {
                field: field.into(),
                scope: "unresolved".into(),
                evidence_refs: Vec::new(),
                confidence: "low".into(),
                reason:
                    "field scope omitted by planner; deterministic evidence may still resolve it"
                        .into(),
                claim_origin: "default_missing".into(),
            });
        }
    }

    for action in &mut assessment.next_evidence_actions {
        for query in &mut action.queries {
            normalize_evidence_query(query);
        }
    }

    if !matches!(
        assessment.terminal_status.as_str(),
        "continue" | "resolved" | "partial" | "evidence_exhausted" | "abstained"
    ) {
        assessment.terminal_status = if assessment.next_evidence_actions.is_empty() {
            if assessment.missing_linkages.is_empty() && assessment.conflicts.is_empty() {
                "resolved".into()
            } else {
                "partial".into()
            }
        } else {
            "continue".into()
        };
    }
    if assessment.terminal_status != "continue" {
        assessment.next_evidence_actions.clear();
    } else if assessment.next_evidence_actions.is_empty() {
        assessment.terminal_status =
            if assessment.missing_linkages.is_empty() && assessment.conflicts.is_empty() {
                "resolved".into()
            } else {
                "partial".into()
            };
    }
}

fn evidence_query_display(query: &EvidenceQuery) -> String {
    serde_json::to_string(query).unwrap_or_else(|_| "{}".into())
}

fn action_history_key(action: &str, query: &EvidenceQuery) -> String {
    format!(
        "{}\t{}",
        action.trim().to_ascii_uppercase(),
        evidence_query_display(query).to_ascii_lowercase()
    )
}

fn evidence_item_matches_action(item: &EvidenceItem, action: &str) -> bool {
    let kind = item.source_kind.to_ascii_lowercase();
    let label = item.source_label.to_ascii_lowercase();
    match action {
        "SEARCH_PUBLICATION" => {
            kind.contains("manuscript") || kind.contains("publication") || label.contains("doi")
        }
        "SEARCH_SUPPLEMENT" => {
            kind.contains("supp") || label.contains("supp") || label.contains("support")
        }
        "SEARCH_STRUCTURED_DESIGN" => {
            kind.contains("supp")
                || kind.contains("design")
                || label.contains("design")
                || label.ends_with(".xlsx")
                || label.ends_with(".csv")
                || label.ends_with(".tsv")
                || label.ends_with(".json")
        }
        "SEARCH_REPOSITORY_METADATA" => kind.contains("pride") || kind.contains("repository"),
        "SEARCH_EXACT_RAW_NAME" | "EXPAND_EVIDENCE_CONTEXT" | "COMPARE_CONFLICTING_EVIDENCE" => {
            true
        }
        "LOOKUP_KG_TERM" => {
            kind.contains("knowledge_graph") || kind.contains("semantic") || kind.contains("kg")
        }
        "ABSTAIN" => false,
        _ => false,
    }
}

fn next_agent_evidence_id(evidence: &DatasetEvidence) -> String {
    let mut n = evidence.evidence.len() + 1;
    loop {
        let id = format!("E{n:04}");
        if !evidence.evidence.iter().any(|item| item.id == id) {
            return id;
        }
        n += 1;
    }
}

fn source_file_is_relevant_to_action(path: &Path, action: &str) -> bool {
    let name = path
        .file_name()
        .and_then(|x| x.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    match action {
        "SEARCH_PUBLICATION" => true,
        "SEARCH_SUPPLEMENT" => name.contains("supp") || name.contains("support"),
        "SEARCH_STRUCTURED_DESIGN" => {
            name.contains("design")
                || name.ends_with(".csv")
                || name.ends_with(".tsv")
                || name.ends_with(".json")
                || name.ends_with(".xlsx")
        }
        "SEARCH_REPOSITORY_METADATA" => true,
        "SEARCH_EXACT_RAW_NAME" | "EXPAND_EVIDENCE_CONTEXT" | "COMPARE_CONFLICTING_EVIDENCE" => {
            true
        }
        "LOOKUP_KG_TERM" => {
            name.contains("semantic") || name.contains("graph") || name.contains("kg")
        }
        _ => false,
    }
}

fn loose_search_normalize(value: &str) -> String {
    value
        .chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .flat_map(|c| c.to_lowercase())
        .collect()
}

fn source_matches_document_hint(label: &str, hint: &str) -> bool {
    if hint.trim().is_empty() {
        return false;
    }
    let hay = loose_search_normalize(label);
    let needle = loose_search_normalize(hint);
    !needle.is_empty() && hay.contains(&needle)
}

fn evidence_query_matches_text(query: &EvidenceQuery, text: &str) -> bool {
    let hay = text.to_ascii_lowercase();
    match query.match_kind.as_str() {
        "raw_exact" | "phrase" | "identifier" => {
            let needle = query.value.trim().to_ascii_lowercase();
            !needle.is_empty() && hay.contains(&needle)
        }
        "doi" => {
            let needle = loose_search_normalize(&query.value);
            !needle.is_empty() && loose_search_normalize(text).contains(&needle)
        }
        "terms_all" => {
            !query.terms.is_empty()
                && query
                    .terms
                    .iter()
                    .all(|term| hay.contains(&term.to_ascii_lowercase()))
        }
        "terms_any" => {
            !query.terms.is_empty()
                && query
                    .terms
                    .iter()
                    .any(|term| hay.contains(&term.to_ascii_lowercase()))
        }
        _ => false,
    }
}

fn evidence_query_anchor(query: &EvidenceQuery, text: &str) -> Option<(usize, usize)> {
    let lower = text.to_ascii_lowercase();
    let candidates = match query.match_kind.as_str() {
        "terms_all" | "terms_any" => query.terms.clone(),
        _ => vec![query.value.clone()],
    };
    candidates.into_iter().find_map(|candidate| {
        let candidate = candidate.trim().to_ascii_lowercase();
        if candidate.is_empty() {
            return None;
        }
        lower.find(&candidate).map(|pos| (pos, candidate.len()))
    })
}

fn source_match_snippet(text: &str, query: &EvidenceQuery, radius: usize) -> Option<String> {
    if !evidence_query_matches_text(query, text) {
        return None;
    }
    let (pos, match_len) = evidence_query_anchor(query, text).unwrap_or((0, 0));
    let mut start = pos.saturating_sub(radius);
    let mut end = (pos + match_len + radius).min(text.len());
    while start > 0 && !text.is_char_boundary(start) {
        start -= 1;
    }
    while end < text.len() && !text.is_char_boundary(end) {
        end += 1;
    }
    Some(text[start..end].replace('\0', " ").replace('\n', " "))
}

fn validate_evidence_query_for_action(
    evidence: &DatasetEvidence,
    action: &str,
    query: &EvidenceQuery,
) -> std::result::Result<(), String> {
    if !matches!(
        query.match_kind.as_str(),
        "raw_exact" | "phrase" | "terms_all" | "terms_any" | "identifier" | "doi"
    ) {
        return Err(format!(
            "unsupported typed query match={:?}",
            query.match_kind
        ));
    }
    if matches!(query.match_kind.as_str(), "terms_all" | "terms_any") {
        if query.terms.is_empty() {
            return Err("terms_all/terms_any requires one or more atomic terms".into());
        }
        if query
            .terms
            .iter()
            .any(|term| term.split_whitespace().count() > 4)
        {
            return Err(
                "terms_all/terms_any terms must be short atomic terms, not search instructions"
                    .into(),
            );
        }
    } else if query.value.trim().is_empty() {
        return Err(format!(
            "{} query requires a non-empty value",
            query.match_kind
        ));
    }
    if query.match_kind == "phrase" && query.value.split_whitespace().count() > 16 {
        return Err("phrase query is too long; use atomic terms rather than a natural-language search instruction".into());
    }
    if action == "SEARCH_EXACT_RAW_NAME" {
        if query.match_kind != "raw_exact" {
            return Err("SEARCH_EXACT_RAW_NAME requires match='raw_exact'".into());
        }
        let requested = query.value.trim();
        if !evidence
            .raw_files
            .iter()
            .any(|raw| raw.file_name.eq_ignore_ascii_case(requested))
        {
            return Err(format!(
                "raw_exact value {:?} is not an acquisition basename in the repository inventory",
                requested
            ));
        }
    } else if query.match_kind == "raw_exact" {
        return Err("match='raw_exact' is reserved for SEARCH_EXACT_RAW_NAME".into());
    }
    Ok(())
}

fn retrieve_from_registered_sources(
    evidence: &mut DatasetEvidence,
    action: &str,
    query: &EvidenceQuery,
) -> Vec<String> {
    let mut paths = if action == "SEARCH_REPOSITORY_METADATA" {
        [&evidence.project_json_path, &evidence.files_json_path]
            .into_iter()
            .map(PathBuf::from)
            .filter(|p| p.is_file())
            .collect::<Vec<_>>()
    } else if action == "SEARCH_EXACT_RAW_NAME" {
        [&evidence.project_json_path, &evidence.files_json_path]
            .into_iter()
            .map(PathBuf::from)
            .chain(
                evidence
                    .manuscript_sources
                    .iter()
                    .chain(evidence.annotation_sources.iter())
                    .map(PathBuf::from),
            )
            .filter(|p| p.is_file() && source_file_is_relevant_to_action(p, action))
            .collect::<Vec<_>>()
    } else {
        evidence
            .manuscript_sources
            .iter()
            .chain(evidence.annotation_sources.iter())
            .map(PathBuf::from)
            .filter(|p| p.is_file() && source_file_is_relevant_to_action(p, action))
            .collect::<Vec<_>>()
    };
    paths.sort_by(|a, b| {
        let a_hint = source_matches_document_hint(&a.display().to_string(), &query.document_hint);
        let b_hint = source_matches_document_hint(&b.display().to_string(), &query.document_hint);
        b_hint
            .cmp(&a_hint)
            .then_with(|| a.display().to_string().cmp(&b.display().to_string()))
    });
    paths.dedup();

    let mut refs = Vec::new();
    for path in paths.into_iter().take(24) {
        let Ok(text) = fs::read_to_string(&path) else {
            continue;
        };
        let Some(snippet) = source_match_snippet(&text, query, 600) else {
            continue;
        };
        let id = next_agent_evidence_id(evidence);
        evidence.evidence.push(EvidenceItem {
            id: id.clone(),
            source_kind: format!("agent_retrieval:{}", action.to_ascii_lowercase()),
            source_label: path.display().to_string(),
            text: snippet,
        });
        refs.push(id);
        if refs.len() >= 6 {
            break;
        }
    }
    refs
}

fn canonicalize_action_for_query(
    evidence: &DatasetEvidence,
    requested_action: &str,
    query: &EvidenceQuery,
) -> (String, String) {
    if query.match_kind == "raw_exact" && requested_action != "SEARCH_EXACT_RAW_NAME" {
        let requested = query.value.trim();
        if evidence
            .raw_files
            .iter()
            .any(|raw| raw.file_name.eq_ignore_ascii_case(requested))
        {
            return ("SEARCH_EXACT_RAW_NAME".into(), requested_action.into());
        }
    }
    (requested_action.into(), String::new())
}

fn execute_evidence_actions(
    evidence: &mut DatasetEvidence,
    actions: &[EvidenceActionRequest],
    round: usize,
    attempted: &mut BTreeSet<String>,
) -> Vec<EvidenceActionResult> {
    let mut out = Vec::new();
    for request in actions.iter().take(6) {
        if request.action == "ABSTAIN" {
            out.push(EvidenceActionResult {
                round,
                action: request.action.clone(),
                normalized_from_action: String::new(),
                query: EvidenceQuery::default(),
                outcome: "abstain".into(),
                matched_evidence_refs: Vec::new(),
                summary: format!("agent abstained: {}", request.reason),
            });
            continue;
        }
        let queries = if request.queries.is_empty() {
            vec![EvidenceQuery::default()]
        } else {
            request.queries.iter().take(8).cloned().collect::<Vec<_>>()
        };
        for query in queries {
            let (effective_action, normalized_from_action) =
                canonicalize_action_for_query(evidence, &request.action, &query);
            let normalization_note = if normalized_from_action.is_empty() {
                String::new()
            } else {
                format!(
                    "action_normalized from '{}' to '{}'; ",
                    normalized_from_action, effective_action
                )
            };
            let key = action_history_key(&effective_action, &query);
            if !attempted.insert(key) {
                out.push(EvidenceActionResult {
                    round,
                    action: effective_action,
                    normalized_from_action,
                    query,
                    outcome: "duplicate_skipped".into(),
                    matched_evidence_refs: Vec::new(),
                    summary: format!(
                        "{}duplicate typed action/query blocked; choose a different evidence source or ABSTAIN; reason={}",
                        normalization_note, request.reason
                    ),
                });
                continue;
            }
            if let Err(reason) =
                validate_evidence_query_for_action(evidence, &effective_action, &query)
            {
                out.push(EvidenceActionResult {
                    round,
                    action: effective_action,
                    normalized_from_action,
                    query,
                    outcome: "invalid_query".into(),
                    matched_evidence_refs: Vec::new(),
                    summary: format!(
                        "{}typed query rejected without retrieval: {}; planner must reformulate or ABSTAIN; reason={}",
                        normalization_note, reason, request.reason
                    ),
                });
                continue;
            }

            let mut refs = Vec::new();
            for item in &evidence.evidence {
                if !evidence_item_matches_action(item, &effective_action) {
                    continue;
                }
                let hay = format!("{}\n{}", item.source_label, item.text);
                if evidence_query_matches_text(&query, &hay) {
                    refs.push(item.id.clone());
                    if refs.len() >= 12 {
                        break;
                    }
                }
            }
            if refs.is_empty() {
                refs = retrieve_from_registered_sources(evidence, &effective_action, &query);
            }
            let (outcome, summary) = if refs.is_empty() {
                (
                    "no_match".to_string(),
                    format!(
                        "{}no matching trusted evidence found for typed query {}; do not repeat this action/query; reason={}",
                        normalization_note,
                        evidence_query_display(&query),
                        request.reason
                    ),
                )
            } else {
                let preview = refs
                    .iter()
                    .filter_map(|id| evidence.evidence.iter().find(|item| item.id == id.as_str()))
                    .take(3)
                    .map(|item| {
                        let text = item.text.replace('\n', " ");
                        let excerpt = text.chars().take(260).collect::<String>();
                        format!("{} [{}] {}", item.id, item.source_label, excerpt)
                    })
                    .collect::<Vec<_>>()
                    .join(" || ");
                (
                    "matched".to_string(),
                    format!(
                        "{}matched {} evidence item(s) for typed query {}; evidence_preview={}; reason={}",
                        normalization_note,
                        refs.len(),
                        evidence_query_display(&query),
                        preview,
                        request.reason
                    ),
                )
            };
            out.push(EvidenceActionResult {
                round,
                action: effective_action,
                normalized_from_action,
                query,
                outcome,
                matched_evidence_refs: refs,
                summary,
            });
        }
    }
    out
}

fn proposal_field_mut<'a>(proposal: &'a mut SdrfProposal, field: &str) -> Option<&'a mut String> {
    match field {
        "relation_mode" => Some(&mut proposal.relation_mode),
        "organism" => Some(&mut proposal.organism),
        "organism_part" => Some(&mut proposal.organism_part),
        "disease" => Some(&mut proposal.disease),
        "cell_type" => Some(&mut proposal.cell_type),
        "sample_type" => Some(&mut proposal.sample_type),
        "single_cell_isolation_method" => Some(&mut proposal.single_cell_isolation_method),
        "individual" => Some(&mut proposal.individual),
        "sample_preparation_batch" => Some(&mut proposal.sample_preparation_batch),
        "cells_per_well" => Some(&mut proposal.cells_per_well),
        "proteomics_data_acquisition_method" => {
            Some(&mut proposal.proteomics_data_acquisition_method)
        }
        "label" => Some(&mut proposal.label),
        "instrument" => Some(&mut proposal.instrument),
        "cleavage_agent_details" => Some(&mut proposal.cleavage_agent_details),
        "fraction_identifier" => Some(&mut proposal.fraction_identifier),
        "technical_replicate" => Some(&mut proposal.technical_replicate),
        "carrier_channel" => Some(&mut proposal.carrier_channel),
        "reference_channel" => Some(&mut proposal.reference_channel),
        _ => None,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ScopeArbitration {
    Allow,
    Block,
    Defer,
}

fn design_field_project_arbitration(
    assessment: Option<&DatasetDesignAssessment>,
    field: &str,
) -> ScopeArbitration {
    let Some(assessment) = assessment else {
        return ScopeArbitration::Allow;
    };
    let Some(claim) = assessment
        .field_scopes
        .iter()
        .find(|claim| claim.field == field)
    else {
        return ScopeArbitration::Defer;
    };
    if claim.claim_origin == "default_missing" {
        return ScopeArbitration::Defer;
    }
    if claim.scope == "project" {
        ScopeArbitration::Allow
    } else {
        ScopeArbitration::Block
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RelationArbitration {
    Allow,
    AllowConfirm,
    BlockProjectRelation,
    BlockConflict,
    BlockUnresolved,
    Defer,
}

fn relation_mode_is_concrete(mode: &str) -> bool {
    matches!(
        mode,
        "one_cell_per_data_file" | "multiplexed_cells_per_data_file"
    )
}

fn relation_arbitration_allows_scaffold(arbitration: RelationArbitration) -> bool {
    matches!(
        arbitration,
        RelationArbitration::Allow | RelationArbitration::AllowConfirm | RelationArbitration::Defer
    )
}

fn relation_scaffold_arbitration(
    assessment: Option<&DatasetDesignAssessment>,
    deterministic_hint: &str,
) -> RelationArbitration {
    let Some(assessment) = assessment else {
        return RelationArbitration::Allow;
    };
    let relation = &assessment.relation_assessment;
    if relation.claim_origin == "default_missing" || relation.mode.trim().is_empty() {
        return RelationArbitration::Defer;
    }

    let deterministic_hint = deterministic_hint.trim();
    let deterministic_is_concrete = relation_mode_is_concrete(deterministic_hint);

    if relation.mode == "mixed" {
        return RelationArbitration::BlockProjectRelation;
    }
    if relation.mode == "unresolved" {
        return if relation.evidence_refs.is_empty() {
            RelationArbitration::Defer
        } else {
            RelationArbitration::BlockUnresolved
        };
    }
    if relation_mode_is_concrete(&relation.mode) {
        if deterministic_is_concrete {
            return if relation.mode == deterministic_hint {
                RelationArbitration::AllowConfirm
            } else {
                RelationArbitration::BlockConflict
            };
        }
        return if relation.scope == "project" {
            RelationArbitration::Allow
        } else {
            RelationArbitration::BlockProjectRelation
        };
    }
    RelationArbitration::Defer
}

fn relation_allows_global_row_serialization(
    assessment: Option<&DatasetDesignAssessment>,
    relation_mode: &str,
) -> bool {
    if relation_mode != "one_cell_per_data_file" {
        return false;
    }
    let Some(assessment) = assessment else {
        return true;
    };
    let relation = &assessment.relation_assessment;
    if relation.claim_origin == "default_missing" {
        return false;
    }
    relation.mode == "one_cell_per_data_file" && relation.scope == "project"
}

fn apply_design_assessment_guard(
    proposal: &mut SdrfProposal,
    assessment: &DatasetDesignAssessment,
    deterministic_relation_hint: &str,
) -> Vec<ValidationIssue> {
    let mut issues = Vec::new();

    let relation_arbitration =
        relation_scaffold_arbitration(Some(assessment), deterministic_relation_hint);
    if !relation_arbitration_allows_scaffold(relation_arbitration)
        && proposal.relation_mode != "uncertain"
        && !proposal.relation_mode.trim().is_empty()
    {
        let previous = proposal.relation_mode.clone();
        proposal.relation_mode = "uncertain".into();
        proposal.evidence_refs.remove("relation_mode");
        issues.push(ValidationIssue {
            level: "warning".into(),
            code: "agent_relation_conflict_not_broadcast".into(),
            row: 0,
            column: "relation_mode".into(),
            message: format!(
                "design agent explicitly assessed relation mode='{}' scope='{}' (confidence={}, refs={:?}); deterministic_hint='{}'; arbitration={:?}; dataset-level proposal '{}' was not broadcast; reason={}",
                assessment.relation_assessment.mode,
                assessment.relation_assessment.scope,
                assessment.relation_assessment.confidence,
                assessment.relation_assessment.evidence_refs,
                deterministic_relation_hint,
                relation_arbitration,
                previous,
                assessment.relation_assessment.reason
            ),
        });
    }

    for claim in &assessment.field_scopes {
        if design_field_project_arbitration(Some(assessment), &claim.field)
            != ScopeArbitration::Block
        {
            continue;
        }
        let field = claim.field.as_str();
        let Some(value) = proposal_field_mut(proposal, field) else {
            continue;
        };
        if concrete_proposal_value(value).is_some() {
            let previous = value.clone();
            *value = "not available".into();
            proposal.evidence_refs.remove(field);
            issues.push(ValidationIssue {
                level: "warning".into(),
                code: "agent_nonproject_value_not_broadcast".into(),
                row: 0,
                column: field.into(),
                message: format!(
                    "design agent explicitly scoped '{field}' as {} (confidence={}, refs={:?}), so dataset-level proposal '{previous}' was not broadcast; reason={}",
                    claim.scope, claim.confidence, claim.evidence_refs, claim.reason
                ),
            });
        }
    }
    if !assessment.missing_linkages.is_empty() {
        issues.push(ValidationIssue {
            level: "warning".into(),
            code: "agent_missing_row_linkage".into(),
            row: 0,
            column: "design_assessment".into(),
            message: assessment.missing_linkages.join(" | "),
        });
    }
    for group in assessment
        .candidate_groups
        .iter()
        .filter(|g| g.status == "search_hint")
    {
        issues.push(ValidationIssue {
            level: "warning".into(),
            code: "agent_filename_hint_not_annotation_truth".into(),
            row: 0,
            column: "design_assessment".into(),
            message: format!(
                "candidate group '{}' remains a search hint only and cannot drive SDRF annotation without source evidence: {}",
                group.id, group.description
            ),
        });
    }
    issues
}

fn repairable_field_for_issue(code: &str) -> Option<&'static str> {
    match code {
        "single_cell_isolation_unresolved" => Some("single_cell_isolation_method"),
        "multiorganism_project_collapsed_to_single_candidate_organism" => Some("organism"),
        "individual_duplicates_nonindividual_semantic_field" => Some("individual"),
        "data_file_name_acquisition_contradiction" => Some("proteomics_data_acquisition_method"),
        _ => None,
    }
}

fn cluster_validator_repair_tasks(issues: &[ValidationIssue]) -> Vec<ValidatorRepairTask> {
    let mut grouped: BTreeMap<String, ValidatorRepairTask> = BTreeMap::new();
    for issue in issues.iter().filter(|issue| issue.level == "error") {
        let Some(field) = repairable_field_for_issue(&issue.code) else {
            continue;
        };
        let task = grouped
            .entry(field.to_string())
            .or_insert_with(|| ValidatorRepairTask {
                field: field.to_string(),
                ..ValidatorRepairTask::default()
            });
        task.error_count += 1;
        if !task.error_codes.iter().any(|code| code == &issue.code) {
            task.error_codes.push(issue.code.clone());
        }
        if task.representative_rows.len() < 6 && !task.representative_rows.contains(&issue.row) {
            task.representative_rows.push(issue.row);
        }
        if task.representative_messages.len() < 4
            && !task.representative_messages.contains(&issue.message)
        {
            task.representative_messages.push(issue.message.clone());
        }
    }
    let mut tasks = grouped.into_values().collect::<Vec<_>>();
    tasks.sort_by(|a, b| {
        b.error_count
            .cmp(&a.error_count)
            .then_with(|| a.field.cmp(&b.field))
    });
    tasks.truncate(VALIDATOR_REPAIR_MAX_TASKS);
    tasks
}

fn individual_value_is_nonindividual_semantic(value: &str, proposal: &SdrfProposal) -> bool {
    let normalized = value.trim().to_ascii_lowercase();
    if normalized.is_empty() || proposal_value_is_reserved("individual", value) {
        return false;
    }
    for other in [
        proposal.organism.as_str(),
        proposal.organism_part.as_str(),
        proposal.disease.as_str(),
        proposal.cell_type.as_str(),
        proposal.sample_type.as_str(),
    ] {
        let other = other.trim().to_ascii_lowercase();
        if !other.is_empty()
            && !proposal_value_is_reserved("individual", &other)
            && normalized == other
        {
            return true;
        }
    }
    matches!(
        normalized.as_str(),
        "embryo"
            | "brain"
            | "brainstem"
            | "neuron"
            | "cell"
            | "cell line"
            | "single cell"
            | "tissue"
            | "hela"
            | "thp1"
            | "thp-1"
            | "k562"
            | "mcf-7"
            | "mouse"
            | "human"
            | "mus musculus"
            | "mus musculus (mouse)"
            | "homo sapiens"
            | "homo sapiens (human)"
            | "xenopus laevis"
            | "xenopus laevis (african clawed frog)"
    )
}

fn sanitize_nonindividual_semantic_proposal(
    proposal: &mut SdrfProposal,
) -> Option<ValidationIssue> {
    if !individual_value_is_nonindividual_semantic(&proposal.individual, proposal) {
        return None;
    }
    let previous = proposal.individual.clone();
    proposal.individual = "not available".into();
    proposal.evidence_refs.remove("individual");
    Some(ValidationIssue {
        level: "warning".into(),
        code: "validator_repair_individual_semantic_sanitized".into(),
        row: 0,
        column: SC_INDIVIDUAL.into(),
        message: format!(
            "dataset-level individual/donor value '{previous}' denotes a non-individual semantic concept or duplicates another biological field; sanitized to 'not available' before row serialization"
        ),
    })
}

fn validator_repair_schema() -> Value {
    let repair_fields = vec![
        "single_cell_isolation_method",
        "organism",
        "individual",
        "proteomics_data_acquisition_method",
    ];
    let evidence_query = json!({
        "type":"object",
        "properties":{
            "match":{"type":"string","enum":["raw_exact","phrase","terms_all","terms_any","identifier","doi"]},
            "value":{"type":"string","maxLength":300},
            "terms":{"type":"array","items":{"type":"string","maxLength":120},"maxItems":12},
            "document_hint":{"type":"string","maxLength":240}
        },
        "required":["match","value","terms","document_hint"],
        "additionalProperties":false
    });
    json!({
        "type":"object",
        "properties":{
            "repair_agent_version":{"type":"string","maxLength":80},
            "decisions":{"type":"array","maxItems":4,"items":{"type":"object","properties":{
                "field":{"type":"string","enum":repair_fields.clone()},
                "resolution":{"type":"string","enum":["set_project_value","keep_unresolved","template_gap","row_mapping_required","no_change"]},
                "scope":{"type":"string","enum":["project","role","row","unresolved"]},
                "value":{"type":"string","maxLength":200},
                "evidence_refs":{"type":"array","items":{"type":"string","pattern":"^E[0-9]{4}$"},"maxItems":12},
                "confidence":{"type":"string","enum":["high","medium","low"]},
                "reason":{"type":"string","maxLength":500}
            },"required":["field","resolution","scope","value","evidence_refs","confidence","reason"],"additionalProperties":false}},
            "next_evidence_actions":{"type":"array","maxItems":4,"items":{"type":"object","properties":{
                "action":{"type":"string","enum":["SEARCH_PUBLICATION","SEARCH_SUPPLEMENT","SEARCH_STRUCTURED_DESIGN","SEARCH_REPOSITORY_METADATA","SEARCH_EXACT_RAW_NAME","EXPAND_EVIDENCE_CONTEXT","LOOKUP_KG_TERM","COMPARE_CONFLICTING_EVIDENCE","ABSTAIN"]},
                "reason":{"type":"string","maxLength":300},
                "target_fields":{"type":"array","items":{"type":"string","enum":repair_fields.clone()},"maxItems":4},
                "queries":{"type":"array","items":evidence_query,"maxItems":4}
            },"required":["action","reason","target_fields","queries"],"additionalProperties":false}},
            "terminal_status":{"type":"string","enum":["continue","resolved","partial","abstained"]},
            "notes":{"type":"string","maxLength":900}
        },
        "required":["repair_agent_version","decisions","next_evidence_actions","terminal_status","notes"],
        "additionalProperties":false
    })
}

fn evidence_action_history_block(results: &[EvidenceActionResult], empty_message: &str) -> String {
    if results.is_empty() {
        return empty_message.into();
    }
    results
        .iter()
        .map(|result| {
            format!(
                "- round={} action={} query={} outcome={} refs={:?}: {}",
                result.round,
                result.action,
                evidence_query_display(&result.query),
                result.outcome,
                result.matched_evidence_refs,
                result.summary
            )
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn validator_repair_prompt(
    evidence: &DatasetEvidence,
    proposal: &SdrfProposal,
    design_assessment: Option<&DatasetDesignAssessment>,
    tasks: &[ValidatorRepairTask],
    phase: &str,
    prior_design_action_results: &[EvidenceActionResult],
    repair_action_results: &[EvidenceActionResult],
) -> String {
    let task_json = serde_json::to_string_pretty(tasks).unwrap_or_else(|_| "[]".into());
    let proposal_json = serde_json::to_string_pretty(proposal).unwrap_or_else(|_| "{}".into());
    let design_json = design_assessment
        .map(|assessment| serde_json::to_string_pretty(assessment).unwrap_or_else(|_| "{}".into()))
        .unwrap_or_else(|| "not available".into());
    let design_history = evidence_action_history_block(
        prior_design_action_results,
        "none (the design agent performed no evidence actions)",
    );
    let repair_history = evidence_action_history_block(
        repair_action_results,
        "none (no validator-repair evidence actions have executed yet)",
    );
    let phase_contract = if phase == "plan" {
        "PHASE=PLAN. Produce provisional decisions from current evidence, but request targeted evidence actions for every repair task that is not already safely provable. A raw natural-language value is not enough: Rust will deterministically canonicalize isolation/acquisition values. If a concrete repair cannot pass that preflight, terminal_status must be 'continue' and next_evidence_actions must seek the missing evidence. Do not mark keep_unresolved/no_change as terminal merely to avoid retrieval."
    } else {
        "PHASE=DECIDE. The single bounded repair-retrieval round is complete. Use the complete trusted evidence inventory and repair action history to return final decisions. Do not request further executable actions. Any task still unsupported must be template_gap, row_mapping_required, keep_unresolved, or no_change; never guess a validator-green value."
    };
    format!(
        "You are the bounded validator-repair stage of a provenance-first SDRF annotation agent for dataset {accession}.\n\n\
{phase_contract}\n\n\
TASK:\n\
Repair only the clustered scientific validation failures listed below. Do not redesign the dataset, change relation_mode, or modify fields that are not repair tasks. This stage gets at most ONE evidence-retrieval round. If trusted evidence cannot support a safe repair, explicitly keep the field unresolved or require row mapping.\n\n\
HARD SCIENTIFIC CONSTRAINTS:\n\
1. Never use filename semantics alone as biological truth. Filename acquisition tokens may trigger evidence retrieval, but they cannot by themselves set DDA/DIA or biological identity.\n\
2. single_cell_isolation_method: identify the evidence and experimental intent; Rust, not you, owns the final controlled-vocabulary normalization. A project repair is allowed only when the SAME supported method applies to all affected study/single-cell rows. Do not require organism/condition identity merely to assign an isolation method. If a real method is outside the pinned template vocabulary, use resolution='template_gap'.\n\
3. organism: if repository/project evidence exposes multiple organisms, NEVER collapse to one project value. Use row_mapping_required unless explicit trusted source linkage resolves rows/groups. This repair stage does not invent row mapping.\n\
4. individual: donor/individual identity must not be an organism part, developmental stage, cell type, cell line, or other semantic field. Do not invent donor IDs. Rust deterministically sanitizes obvious semantic aliases; use keep_unresolved when donor identity is absent.\n\
5. proteomics_data_acquisition_method: filename DDA/DIA tokens are contradiction detectors only. Identify trusted corroborating evidence; Rust owns final DDA/DIA canonicalization. If different branches use different modes, use row_mapping_required.\n\
6. set_project_value requires scope='project', one or more E#### refs, and direct field-relevant support. The value is an evidence-grounded intent, not authority over SDRF vocabulary.\n\
7. row_mapping_required must not contain a project-wide annotation value. keep_unresolved must not contain a concrete value. template_gap must not substitute a nearby allowed annotation term.\n\
8. A previous design-agent project/group/row scope remains a safety constraint. Do not override an explicit group/row design claim with a project-wide repair.\n\
9. Request at most {max_tasks} actions and at most {max_queries} queries per action. Prefer targeted terms/identifiers/DOIs and do not repeat exhausted design-agent searches.\n\
10. Success is binary per task: source-backed canonicalizable repair, deterministically corroborated template gap, explicit row-mapping requirement, or honest unresolved/abstain. Do not optimize for validator-green output.\n\n\
CLUSTERED VALIDATION TASKS:\n{task_json}\n\n\
CURRENT PROPOSAL (context; only task fields may change):\n{proposal_json}\n\n\
FINAL DESIGN ASSESSMENT (scope safety):\n{design_json}\n\n\
TRUSTED EVIDENCE INVENTORY:\n{evidence_block}\n\n\
PRIOR DESIGN-AGENT ACTION HISTORY (do not repeat exhausted searches):\n{design_history}\n\n\
VALIDATOR-REPAIR ACTION HISTORY:\n{repair_history}\n\n\
Return the structured ValidatorRepairAssessment. Set repair_agent_version exactly to {repair_version}.",
        accession = evidence.accession,
        max_tasks = VALIDATOR_REPAIR_MAX_TASKS,
        max_queries = VALIDATOR_REPAIR_MAX_QUERIES_PER_TASK,
        evidence_block = design_evidence_block(evidence, 42),
        repair_version = VALIDATOR_REPAIR_AGENT_VERSION,
    )
}

fn normalize_validator_repair_assessment(
    evidence: &DatasetEvidence,
    tasks: &[ValidatorRepairTask],
    phase: &str,
    assessment: &mut ValidatorRepairAssessment,
) {
    assessment.repair_agent_version = VALIDATOR_REPAIR_AGENT_VERSION.into();
    let task_fields = tasks
        .iter()
        .map(|task| task.field.as_str())
        .collect::<BTreeSet<_>>();
    let valid_refs = evidence
        .evidence
        .iter()
        .map(|item| item.id.as_str())
        .collect::<BTreeSet<_>>();
    assessment.decisions.retain_mut(|decision| {
        decision.field = decision.field.trim().to_string();
        decision.resolution = decision.resolution.trim().to_ascii_lowercase();
        decision.scope = decision.scope.trim().to_ascii_lowercase();
        decision.value = decision.value.trim().to_string();
        decision
            .evidence_refs
            .retain(|reference| valid_refs.contains(reference.as_str()));
        decision.evidence_refs.sort();
        decision.evidence_refs.dedup();
        match decision.resolution.as_str() {
            "row_mapping_required" => {
                if decision.scope == "project" || decision.scope == "unresolved" {
                    decision.scope = "row".into();
                }
                decision.value.clear();
            }
            "keep_unresolved" => {
                decision.scope = "unresolved".into();
                decision.value.clear();
            }
            "template_gap" | "no_change" => {
                decision.value.clear();
            }
            _ => {}
        }
        task_fields.contains(decision.field.as_str())
    });
    let mut seen = BTreeSet::new();
    assessment
        .decisions
        .retain(|decision| seen.insert(decision.field.clone()));
    assessment.decisions.truncate(VALIDATOR_REPAIR_MAX_TASKS);
    assessment.next_evidence_actions.retain_mut(|action| {
        action
            .target_fields
            .retain(|field| task_fields.contains(field.as_str()));
        action
            .queries
            .truncate(VALIDATOR_REPAIR_MAX_QUERIES_PER_TASK);
        for query in &mut action.queries {
            normalize_evidence_query(query);
        }
        !action.target_fields.is_empty() || action.action == "ABSTAIN"
    });
    assessment
        .next_evidence_actions
        .truncate(VALIDATOR_REPAIR_MAX_TASKS);
    if phase == "decide" {
        assessment.next_evidence_actions.clear();
        if assessment.terminal_status == "continue" {
            assessment.terminal_status = "partial".into();
        }
    }
    if !matches!(
        assessment.terminal_status.as_str(),
        "continue" | "resolved" | "partial" | "abstained"
    ) {
        assessment.terminal_status = if phase == "plan" {
            "continue".into()
        } else {
            "partial".into()
        };
    }
}

async fn call_ollama_validator_repair(
    opts: &SdrfAnnotateOptions,
    evidence: &DatasetEvidence,
    proposal: &SdrfProposal,
    design_assessment: Option<&DatasetDesignAssessment>,
    tasks: &[ValidatorRepairTask],
    phase: &str,
    prior_design_action_results: &[EvidenceActionResult],
    repair_action_results: &[EvidenceActionResult],
) -> Result<ValidatorRepairAssessment> {
    let client = Client::builder()
        .timeout(Duration::from_secs(opts.timeout_seconds))
        .build()?;
    let payload = json!({
        "model": opts.model,
        "prompt": validator_repair_prompt(
            evidence,
            proposal,
            design_assessment,
            tasks,
            phase,
            prior_design_action_results,
            repair_action_results,
        ),
        "stream": false,
        "think": false,
        "format": validator_repair_schema(),
        "options": {"temperature": 0.0}
    });
    let response = client
        .post(&opts.ollama_url)
        .json(&payload)
        .send()
        .await
        .with_context(|| {
            format!(
                "Ollama validator-repair {phase} request for {}",
                evidence.accession
            )
        })?;
    let status = response.status();
    let body: Value = response
        .json()
        .await
        .context("decode Ollama validator-repair response")?;
    if !status.is_success() {
        bail!("Ollama validator-repair HTTP {status}: {body}");
    }
    let raw = body.get("response").and_then(Value::as_str).unwrap_or("");
    if raw.trim().is_empty() {
        bail!("Ollama returned empty validator-repair assessment");
    }
    let mut assessment: ValidatorRepairAssessment =
        serde_json::from_str(raw).context("parse structured Ollama validator-repair assessment")?;
    normalize_validator_repair_assessment(evidence, tasks, phase, &mut assessment);
    Ok(assessment)
}

fn decision_refs_are_field_relevant(
    evidence: &DatasetEvidence,
    field: &str,
    refs: &[String],
) -> bool {
    !refs.is_empty()
        && refs.iter().all(|reference| {
            evidence
                .evidence
                .iter()
                .find(|item| item.id.as_str() == reference.as_str())
                .map_or(false, |item| evidence_relevant_to_field(field, item))
        })
}

fn validator_repair_evidence_subset(
    evidence: &DatasetEvidence,
    refs: &[String],
) -> Vec<EvidenceItem> {
    let wanted = refs.iter().map(String::as_str).collect::<BTreeSet<_>>();
    evidence
        .evidence
        .iter()
        .filter(|item| wanted.contains(item.id.as_str()))
        .cloned()
        .collect()
}

fn acquisition_repair_evidence_is_source_corroboration(item: &EvidenceItem) -> bool {
    let kind = item.source_kind.to_ascii_lowercase();
    let label = item.source_label.to_ascii_lowercase();
    kind != "pride_files"
        && !label.contains("/files/")
        && !label.contains("\\files\\")
        && !label.ends_with(".raw")
        && !label.ends_with(".mzml")
}

fn canonical_validator_repair_project_value(
    evidence: &DatasetEvidence,
    decision: &ValidatorRepairDecision,
) -> Option<(String, Vec<String>)> {
    let mut cited = validator_repair_evidence_subset(evidence, &decision.evidence_refs);
    if cited.is_empty() {
        return None;
    }
    match decision.field.as_str() {
        "single_cell_isolation_method" => infer_isolation_method_scaffold(&cited),
        "proteomics_data_acquisition_method" => {
            cited.retain(acquisition_repair_evidence_is_source_corroboration);
            infer_acquisition_method_repair(&cited)
        }
        _ => None,
    }
}

fn corroborated_validator_repair_template_gap(
    evidence: &DatasetEvidence,
    decision: &ValidatorRepairDecision,
) -> Option<TemplateCompatibilityGap> {
    if decision.field != "single_cell_isolation_method" || decision.resolution != "template_gap" {
        return None;
    }
    if let Some(existing) = evidence
        .metadata_scaffold
        .template_gaps
        .iter()
        .find(|gap| gap.field == decision.field)
    {
        return Some(existing.clone());
    }
    let cited = validator_repair_evidence_subset(evidence, &decision.evidence_refs);
    infer_isolation_template_gap(&cited)
}

fn validator_repair_decision_is_safe_terminal(
    evidence: &DatasetEvidence,
    design_assessment: Option<&DatasetDesignAssessment>,
    decision: &ValidatorRepairDecision,
) -> bool {
    match decision.resolution.as_str() {
        "set_project_value" => {
            matches!(
                decision.field.as_str(),
                "single_cell_isolation_method" | "proteomics_data_acquisition_method"
            ) && decision.scope == "project"
                && design_field_project_arbitration(design_assessment, &decision.field)
                    != ScopeArbitration::Block
                && decision_refs_are_field_relevant(
                    evidence,
                    &decision.field,
                    &decision.evidence_refs,
                )
                && canonical_validator_repair_project_value(evidence, decision).is_some()
                && !evidence
                    .metadata_scaffold
                    .template_gaps
                    .iter()
                    .any(|gap| gap.field == decision.field)
        }
        "template_gap" => corroborated_validator_repair_template_gap(evidence, decision).is_some(),
        "row_mapping_required" => {
            matches!(decision.field.as_str(), "organism" | "individual")
                && decision_refs_are_field_relevant(
                    evidence,
                    &decision.field,
                    &decision.evidence_refs,
                )
        }
        _ => false,
    }
}

fn validator_repair_plan_needs_retrieval(
    evidence: &DatasetEvidence,
    design_assessment: Option<&DatasetDesignAssessment>,
    tasks: &[ValidatorRepairTask],
    assessment: &ValidatorRepairAssessment,
) -> bool {
    tasks.iter().any(|task| {
        assessment
            .decisions
            .iter()
            .find(|decision| decision.field == task.field)
            .map_or(true, |decision| {
                !validator_repair_decision_is_safe_terminal(evidence, design_assessment, decision)
            })
    })
}

fn synthesized_validator_repair_actions(
    evidence: &DatasetEvidence,
    design_assessment: Option<&DatasetDesignAssessment>,
    tasks: &[ValidatorRepairTask],
    assessment: &ValidatorRepairAssessment,
) -> Vec<EvidenceActionRequest> {
    let mut actions = Vec::new();
    for task in tasks {
        let safe = assessment
            .decisions
            .iter()
            .find(|decision| decision.field == task.field)
            .map_or(false, |decision| {
                validator_repair_decision_is_safe_terminal(evidence, design_assessment, decision)
            });
        if safe {
            continue;
        }
        let (action, terms) = match task.field.as_str() {
            "single_cell_isolation_method" => (
                "SEARCH_PUBLICATION",
                vec![
                    "single cell isolation",
                    "manual picking",
                    "microdissection",
                    "FACS",
                    "hydrodynamic",
                    "capillary",
                    "aspiration",
                ],
            ),
            "proteomics_data_acquisition_method" => (
                "SEARCH_PUBLICATION",
                vec![
                    "data-dependent acquisition",
                    "data-independent acquisition",
                    "DDA",
                    "DIA",
                    "acquisition method",
                ],
            ),
            "organism" => (
                "SEARCH_STRUCTURED_DESIGN",
                vec!["organism", "species", "sample", "raw file"],
            ),
            "individual" => (
                "SEARCH_PUBLICATION",
                vec!["donor", "individual", "subject", "animal"],
            ),
            _ => continue,
        };
        actions.push(EvidenceActionRequest {
            action: action.into(),
            reason: format!(
                "validator repair preflight could not safely resolve '{}'; retrieve trusted field-specific evidence before the final repair decision",
                task.field
            ),
            target_fields: vec![task.field.clone()],
            queries: vec![EvidenceQuery {
                match_kind: "terms_any".into(),
                value: String::new(),
                terms: terms.into_iter().map(str::to_string).collect(),
                document_hint: String::new(),
            }],
        });
        if actions.len() >= VALIDATOR_REPAIR_MAX_TASKS {
            break;
        }
    }
    actions
}

fn seed_attempted_actions_from_history(results: &[EvidenceActionResult]) -> BTreeSet<String> {
    results
        .iter()
        .filter(|result| !result.action.is_empty())
        .map(|result| action_history_key(&result.action, &result.query))
        .collect()
}

fn apply_validator_repair_template_gaps(
    evidence: &mut DatasetEvidence,
    assessment: &ValidatorRepairAssessment,
) -> Vec<ValidationIssue> {
    let mut issues = Vec::new();
    for decision in assessment
        .decisions
        .iter()
        .filter(|decision| decision.resolution == "template_gap")
    {
        let Some(gap) = corroborated_validator_repair_template_gap(evidence, decision) else {
            issues.push(ValidationIssue {
                level: "warning".into(),
                code: "validator_repair_template_gap_unconfirmed".into(),
                row: 0,
                column: decision.field.clone(),
                message: format!(
                    "validator repair proposed a template gap for '{}' but deterministic source-backed unsupported-method detection did not corroborate it: {}",
                    decision.field, decision.reason
                ),
            });
            continue;
        };
        let already_present = evidence
            .metadata_scaffold
            .template_gaps
            .iter()
            .any(|existing| {
                existing.field == gap.field && existing.observed_value == gap.observed_value
            });
        if !already_present {
            evidence.metadata_scaffold.template_gaps.push(gap.clone());
        }
        issues.push(ValidationIssue {
            level: "warning".into(),
            code: "validator_repair_template_gap_confirmed".into(),
            row: 0,
            column: decision.field.clone(),
            message: format!(
                "validator repair confirmed source-backed template gap '{}' for field '{}' using refs {:?}",
                gap.observed_value, gap.field, gap.evidence_refs
            ),
        });
    }
    issues
}

fn apply_validator_repair_decisions(
    proposal: &mut SdrfProposal,
    evidence: &DatasetEvidence,
    design_assessment: Option<&DatasetDesignAssessment>,
    tasks: &[ValidatorRepairTask],
    assessment: &ValidatorRepairAssessment,
) -> (Vec<ValidationIssue>, Vec<String>) {
    let task_fields = tasks
        .iter()
        .map(|task| task.field.as_str())
        .collect::<BTreeSet<_>>();
    let mut issues = Vec::new();
    let mut applied_fields = Vec::new();
    for decision in &assessment.decisions {
        if !task_fields.contains(decision.field.as_str()) {
            continue;
        }
        if decision.resolution != "set_project_value" {
            issues.push(ValidationIssue {
                level: "warning".into(),
                code: format!("validator_repair_{}", decision.resolution),
                row: 0,
                column: decision.field.clone(),
                message: format!(
                    "validator repair left field '{}' without a project-wide value (scope={}, confidence={}): {}",
                    decision.field, decision.scope, decision.confidence, decision.reason
                ),
            });
            continue;
        }
        if !matches!(
            decision.field.as_str(),
            "single_cell_isolation_method" | "proteomics_data_acquisition_method"
        ) {
            issues.push(ValidationIssue {
                level: "warning".into(),
                code: "validator_repair_project_value_rejected_for_mapping_field".into(),
                row: 0,
                column: decision.field.clone(),
                message: format!(
                    "validator repair cannot assign project-wide '{}' because this failure requires source-grounded row/group mapping: {}",
                    decision.field, decision.reason
                ),
            });
            continue;
        }
        if decision.scope != "project"
            || design_field_project_arbitration(design_assessment, &decision.field)
                == ScopeArbitration::Block
            || !decision_refs_are_field_relevant(evidence, &decision.field, &decision.evidence_refs)
        {
            issues.push(ValidationIssue {
                level: "warning".into(),
                code: "validator_repair_project_value_rejected_by_provenance_guard".into(),
                row: 0,
                column: decision.field.clone(),
                message: format!(
                    "validator repair proposed project-wide '{}' but application was rejected by scope/provenance guards (scope={}, refs={:?}): {}",
                    decision.field, decision.scope, decision.evidence_refs, decision.reason
                ),
            });
            continue;
        }
        if evidence
            .metadata_scaffold
            .template_gaps
            .iter()
            .any(|gap| gap.field == decision.field)
        {
            issues.push(ValidationIssue {
                level: "warning".into(),
                code: "validator_repair_blocked_by_template_vocabulary_gap".into(),
                row: 0,
                column: decision.field.clone(),
                message: format!(
                    "validator repair did not apply '{}' because a source-backed method is outside the pinned template vocabulary: {}",
                    decision.field, decision.reason
                ),
            });
            continue;
        }
        let Some((canonical_value, canonical_refs)) =
            canonical_validator_repair_project_value(evidence, decision)
        else {
            issues.push(ValidationIssue {
                level: "warning".into(),
                code: "validator_repair_project_value_not_canonicalizable".into(),
                row: 0,
                column: decision.field.clone(),
                message: format!(
                    "validator repair intent for '{}' could not be converted to one deterministic source-backed SDRF value from refs {:?}; raw model wording '{}' was not serialized",
                    decision.field, decision.evidence_refs, decision.value
                ),
            });
            continue;
        };
        if obviously_invalid_field_value(&decision.field, &canonical_value) {
            issues.push(ValidationIssue {
                level: "warning".into(),
                code: "validator_repair_project_value_rejected_by_semantic_guard".into(),
                row: 0,
                column: decision.field.clone(),
                message: format!(
                    "deterministically canonicalized validator repair '{}'='{}' failed the semantic guard",
                    decision.field, canonical_value
                ),
            });
            continue;
        }
        let Some(slot) = proposal_field_mut(proposal, &decision.field) else {
            continue;
        };
        let previous = slot.clone();
        *slot = canonical_value.clone();
        proposal
            .evidence_refs
            .insert(decision.field.clone(), canonical_refs.clone());
        applied_fields.push(decision.field.clone());
        issues.push(ValidationIssue {
            level: "warning".into(),
            code: "validator_repair_project_value_applied".into(),
            row: 0,
            column: decision.field.clone(),
            message: format!(
                "bounded validator repair changed '{}' from '{}' to canonical '{}' using refs {:?} (model intent='{}', confidence={}): {}",
                decision.field,
                previous,
                canonical_value,
                canonical_refs,
                decision.value,
                decision.confidence,
                decision.reason
            ),
        });
    }
    applied_fields.sort();
    applied_fields.dedup();
    (issues, applied_fields)
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

fn prompt_for(
    evidence: &DatasetEvidence,
    max_files: usize,
    assessment: Option<&DatasetDesignAssessment>,
) -> String {
    let files = evidence
        .raw_files
        .iter()
        .take(max_files)
        .map(|f| format!("- {}", f.file_name))
        .collect::<Vec<_>>()
        .join("\n");
    let design_assessment = assessment
        .map(|a| serde_json::to_string_pretty(a).unwrap_or_else(|_| "{}".into()))
        .unwrap_or_else(|| "not available".into());
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
DESIGN-ASSESSMENT AGENT OUTPUT (canonical field scopes and group provenance):\n{design_assessment}\n\nOnly candidate_groups with status='supported' may influence metadata. Groups marked search_hint are retrieval hypotheses only. A concrete dataset-level proposal is allowed only for ordinary fields whose field_scopes entry has scope='project' and claim_origin='model_explicit'. relation_mode follows relation_assessment instead of field_scopes.\n\n\
TARGET FIELDS: {target_list}\n\
LOCKED/ALREADY-STRUCTURED FIELDS: {locked_list}\n\n\
RULES:\n\
1. Use ONLY the field-specific evidence shown under the matching field heading. Never invent metadata or borrow a value from an unrelated field.\n\
2. Fields in LOCKED/ALREADY-STRUCTURED FIELDS must be returned exactly as 'not available' with an empty evidence_refs list; for relation_mode use 'uncertain'. Rust preserves existing SDRF values deterministically.\n\
3. If a TARGET field is not supported by its own evidence section, return exactly 'not available' (or relation_mode='uncertain').\n\
4. Every concrete TARGET value must cite one or more E#### refs from that SAME field section.\n\
5. relation_mode means acquisition cardinality, not biological row identity: one_cell_per_data_file, multiplexed_cells_per_data_file, mixed, or uncertain. Do not infer it merely from the phrase 'single-cell', and do not downgrade a concrete one-sample-per-file relation merely because organism, condition, sex, cell type, treatment, or replicate linkage is unresolved.\n\
6. For single_cell_isolation_method, use only a method faithfully represented by the pinned template vocabulary (for example FACS, cellenONE, microfluidics, laser capture microdissection, manual picking, nanoPOTS, droplet microfluidics, or acoustic droplet ejection). If the evidence instead supports a method such as patch-clamp aspiration or capillary microsampling that the pinned vocabulary cannot represent faithfully, return 'not available'; Rust records the template-compatibility gap separately. Software such as MaxQuant is never an isolation method.\n\
7. proteomics_data_acquisition_method describes MS acquisition (for example DDA, DIA, diaPASEF, PRM), not analysis/search software.\n\
8. instrument is the mass spectrometer/instrument, not software.\n\
9. Do not infer a per-cell identifier from filenames here. Rust constructs identifiers only when the row relationship is deterministically supported.\n\
10. Dataset-level values may vary by row. For ordinary fields, return a concrete value only when the design assessment gives scope='project' with claim_origin='model_explicit' and field-specific evidence supports the value; otherwise use 'not available'. relation_mode is different: it represents acquisition cardinality. A concrete project-scoped relation may be returned directly. A branch-scoped concrete relation must not be changed merely because biological row attributes are unresolved; Rust separately arbitrates it against the deterministic relation scaffold and separately tracks whether branch membership/row mapping is safe to serialize.\n\
11. search_hint candidate groups must never be converted into metadata.\n\
12. Return factors=[] in this version. Per-row factor reconstruction is deferred.\n\
13. Repository labels (PRIDE, PXD accessions, fileCategory, URLs) and analysis software must never be copied into biological/MS fields.\n\
14. Uncertainty is preferable to hallucination.\n\n\
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
        design_assessment = design_assessment,
        nfiles = evidence.raw_files.len(),
    )
}

async fn call_ollama(
    opts: &SdrfAnnotateOptions,
    evidence: &DatasetEvidence,
    assessment: Option<&DatasetDesignAssessment>,
) -> Result<SdrfProposal> {
    let client = Client::builder()
        .timeout(Duration::from_secs(opts.timeout_seconds))
        .build()?;
    let payload = json!({
        "model": opts.model,
        "prompt": prompt_for(evidence, opts.max_files_in_prompt, assessment),
        "stream": false,
        "think": false,
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

fn validator_compatible_acquisition_method(value: &str) -> String {
    let text = value.trim();
    let compact = text.to_ascii_lowercase().replace(" ", "").replace("_", "-");
    let explicit_dia = compact == "data-independentacquisition"
        || compact == "nt=data-independentacquisition;ac=pride:0000450"
        || compact == "nt=data-independentacquisition;ac=pride:0000628"
        || compact == "diapasef"
        || compact == "dia-pasef"
        || compact == "nt=diapasef;ac=pride:0000650"
        || compact == "swathms"
        || compact == "nt=swathms;ac=pride:0000447";
    if explicit_dia {
        "Data-independent acquisition".to_string()
    } else {
        text.to_string()
    }
}

fn apply_publication_compatibility_normalization(
    proposal: &mut SdrfProposal,
) -> Vec<ValidationIssue> {
    let mut issues = Vec::new();
    let old_acq = proposal.proteomics_data_acquisition_method.clone();
    let new_acq = validator_compatible_acquisition_method(&old_acq);
    if new_acq != old_acq.trim() {
        proposal.proteomics_data_acquisition_method = new_acq.clone();
        issues.push(ValidationIssue {
            level: "warning".into(),
            code: "validator_compatible_dia_serialization_applied".into(),
            row: 0,
            column: "proteomics_data_acquisition_method".into(),
            message: format!("serialized explicit DIA value '{}' as '{}' for the current dia-acquisition validator", old_acq, new_acq),
        });
    }
    if proposal
        .sample_type
        .trim()
        .eq_ignore_ascii_case("study sample")
    {
        proposal.sample_type = "not available".into();
        issues.push(ValidationIssue {
            level: "warning".into(),
            code: "validator_gap_study_sample_normalized_fail_closed".into(),
            row: 0,
            column: "sample_type".into(),
            message: "normalized unsupported 'study sample' to 'not available' for the maintained validator".into(),
        });
    }
    issues
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
    if field == "individual" {
        let parts = value
            .split('|')
            .map(str::trim)
            .filter(|x| concrete_semantic_value(x))
            .collect::<Vec<_>>();
        if parts.len() >= 2 {
            return true;
        }
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
                message: format!("discarded model value '{original}' because {field} is already locked by deterministic structured evidence or an existing SDRF"),
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

fn apply_metadata_scaffold_field_with_arbitration(
    field: &str,
    slot: &mut String,
    scaffold: &DeterministicMetadataScaffold,
    proposal_refs: &mut BTreeMap<String, Vec<String>>,
    assessment: Option<&DatasetDesignAssessment>,
    issues: &mut Vec<ValidationIssue>,
) {
    let arbitration = design_field_project_arbitration(assessment, field);
    match arbitration {
        ScopeArbitration::Allow => {
            apply_metadata_scaffold_field(field, slot, scaffold, proposal_refs, issues);
        }
        ScopeArbitration::Defer => {
            let was_concrete = concrete_proposal_value(slot).is_some();
            apply_metadata_scaffold_field(field, slot, scaffold, proposal_refs, issues);
            if !was_concrete && concrete_proposal_value(slot).is_some() {
                issues.push(ValidationIssue {
                    level: "warning".into(),
                    code: "agent_scope_deferred_to_deterministic_scaffold".into(),
                    row: 0,
                    column: field.into(),
                    message: "planner supplied no evidence-backed scope claim, so trusted deterministic scaffold evidence was allowed to resolve the project-level value".into(),
                });
            }
        }
        ScopeArbitration::Block => {
            if scaffold.values.contains_key(field) {
                let claim = assessment
                    .and_then(|a| a.field_scopes.iter().find(|claim| claim.field == field));
                issues.push(ValidationIssue {
                    level: "warning".into(),
                    code: "agent_blocks_project_scaffold_broadcast".into(),
                    row: 0,
                    column: field.into(),
                    message: match claim {
                        Some(claim) => format!(
                            "design agent explicitly scoped field as '{}' (origin={}, confidence={}, refs={:?}); deterministic dataset-level scaffold value was not broadcast; reason={}",
                            claim.scope,
                            claim.claim_origin,
                            claim.confidence,
                            claim.evidence_refs,
                            claim.reason
                        ),
                        None => "design agent explicitly blocked project-wide scope; deterministic dataset-level scaffold value was not broadcast".into(),
                    },
                });
            }
        }
    }
}

fn apply_deterministic_metadata_scaffold(
    proposal: &mut SdrfProposal,
    evidence: &DatasetEvidence,
    assessment: Option<&DatasetDesignAssessment>,
) -> Vec<ValidationIssue> {
    let mut issues = Vec::new();

    apply_metadata_scaffold_field_with_arbitration(
        "organism",
        &mut proposal.organism,
        &evidence.metadata_scaffold,
        &mut proposal.evidence_refs,
        assessment,
        &mut issues,
    );
    apply_metadata_scaffold_field_with_arbitration(
        "instrument",
        &mut proposal.instrument,
        &evidence.metadata_scaffold,
        &mut proposal.evidence_refs,
        assessment,
        &mut issues,
    );
    apply_metadata_scaffold_field_with_arbitration(
        "label",
        &mut proposal.label,
        &evidence.metadata_scaffold,
        &mut proposal.evidence_refs,
        assessment,
        &mut issues,
    );
    apply_metadata_scaffold_field_with_arbitration(
        "proteomics_data_acquisition_method",
        &mut proposal.proteomics_data_acquisition_method,
        &evidence.metadata_scaffold,
        &mut proposal.evidence_refs,
        assessment,
        &mut issues,
    );
    apply_metadata_scaffold_field_with_arbitration(
        "cleavage_agent_details",
        &mut proposal.cleavage_agent_details,
        &evidence.metadata_scaffold,
        &mut proposal.evidence_refs,
        assessment,
        &mut issues,
    );
    apply_metadata_scaffold_field_with_arbitration(
        "single_cell_isolation_method",
        &mut proposal.single_cell_isolation_method,
        &evidence.metadata_scaffold,
        &mut proposal.evidence_refs,
        assessment,
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

        // Recommended single-cell metadata must never remain blank. This is reserved-word
        // normalization only: no batch or channel identity is invented. Non-isobaric labels do not
        // use carrier/reference channels; unknown/isobaric rows fail closed to `not available`.
        if let Some(j) = idx(SC_PREP_BATCH) {
            if row[j].trim().is_empty() {
                row[j] = "not available".into();
            }
        }
        let row_label = idx("comment[label]")
            .and_then(|j| row.get(j))
            .map(|value| value.as_str())
            .unwrap_or_default();
        let channel_reserved = if label_is_clearly_nonisobaric(row_label) {
            "not applicable"
        } else {
            "not available"
        };
        for header in [SC_CARRIER_CHANNEL, SC_REFERENCE_CHANNEL] {
            if let Some(j) = idx(header) {
                if row[j].trim().is_empty() {
                    row[j] = channel_reserved.into();
                }
            }
        }

        let current_sample_type = sample_type_idx
            .and_then(|j| row.get(j))
            .map(|x| x.trim().to_ascii_lowercase())
            .unwrap_or_default();
        let explicit_bulk_source = source_idx
            .and_then(|j| row.get(j))
            .map(|x| source_name_explicit_bulk_role(x))
            .unwrap_or(false);
        let cells_bulk_safe = cells_idx
            .and_then(|j| row.get(j))
            .map(|x| {
                matches!(
                    x.trim().to_ascii_lowercase().as_str(),
                    "" | "not available" | "not applicable" | "pooled"
                )
            })
            .unwrap_or(true);
        let cell_id_bulk_safe = cell_idx
            .and_then(|j| row.get(j))
            .map(|x| {
                matches!(
                    x.trim().to_ascii_lowercase().as_str(),
                    "" | "not available" | "not applicable" | "pooled"
                )
            })
            .unwrap_or(true);
        let explicit_bulk_row = explicit_bulk_source && cells_bulk_safe && cell_id_bulk_safe;
        if let Some(j) = sample_type_idx {
            if explicit_bulk_row
                && matches!(
                    current_sample_type.as_str(),
                    "" | "not available" | "single cell"
                )
            {
                row[j] = "pooled".into();
            } else if (current_sample_type.is_empty() || current_sample_type == "not available")
                && relation == "one_cell_per_data_file"
            {
                row[j] = "single cell".into();
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
                    "pooled",
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
                } else if ["bulk control", "pooled", "negative control"]
                    .contains(&sample_type.as_str())
                {
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
        let current_cells_per_well = cells_idx
            .and_then(|j| row.get(j))
            .map(|x| x.trim().to_ascii_lowercase())
            .unwrap_or_default();
        let current_source_name = source_idx
            .and_then(|j| row.get(j))
            .map(|x| x.trim().to_ascii_lowercase())
            .unwrap_or_default();
        let empty_control = matches!(sample_type.as_str(), "empty" | "blank" | "negative control")
            || current_cells_per_well == "0"
            || current_source_name.contains("blank");
        if empty_control {
            for header in [
                SC_INDIVIDUAL,
                "characteristics[cell type]",
                "characteristics[cell line]",
                "characteristics[cellosaurus accession]",
                "characteristics[cellosaurus name]",
                "characteristics[material type]",
            ] {
                if let Some(j) = idx(header) {
                    if concrete_semantic_value(&row[j]) {
                        row[j] = "not applicable".into();
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
            row[j] = sdrf_annotation_tool_value();
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
        let single_cell_row = mapping
            .sample_type
            .trim()
            .eq_ignore_ascii_case("single cell");
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
            if single_cell_row {
                reserved_or(&proposal.cell_type, "not available")
            } else {
                "not applicable".to_string()
            },
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
            if single_cell_row {
                reserved_or(&proposal.single_cell_isolation_method, "not available")
            } else {
                "not applicable".to_string()
            },
        );
        set(
            &mut row,
            SC_CELL_IDENTIFIER,
            mapping.cell_identifier.clone(),
        );
        set(
            &mut row,
            SC_INDIVIDUAL,
            if single_cell_row {
                reserved_or(&proposal.individual, "not available")
            } else {
                "not applicable".to_string()
            },
        );
        set(
            &mut row,
            SC_PREP_BATCH,
            if single_cell_row {
                reserved_or(&proposal.sample_preparation_batch, "not available")
            } else {
                "not applicable".to_string()
            },
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
            sdrf_annotation_tool_value(),
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
                    "not available".to_string(),
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
        // File-role-aware de-novo generation must not leak study-level biological
        // identity into true empty/blank controls. Generated drafts do not pass
        // through merge_existing_sdrf(), so sanitize them here as well.
        if matches!(effective_role, RawFileRole::Blank) {
            for header in [
                SC_INDIVIDUAL,
                "characteristics[cell type]",
                "characteristics[cell line]",
                "characteristics[cellosaurus accession]",
                "characteristics[cellosaurus name]",
                "characteristics[material type]",
            ] {
                set(&mut row, header, "not applicable".to_string());
            }
        }
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
            sdrf_annotation_tool_value(),
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
                "explicit row mapping {} assigns more than one row to RAW {}",
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
        let sample_type = row.sample_type.trim().to_ascii_lowercase();
        let single_cell_row = sample_type == "single cell";
        let non_single_study_row = sample_type == "not available";
        if sample_type == "study sample" {
            bail!(
                "explicit row mapping {} RAW {} uses sample_type=study sample, which is advertised by the SDRF glossary but is not currently validator-backed under PRIDE:0000895; use a source-backed role or not available",
                accession,
                row.raw_file
            );
        }
        if !single_cell_row && !non_single_study_row {
            bail!(
                "explicit row mapping {} RAW {} uses unsupported sample_type for the bounded manifest contract: {}",
                accession,
                row.raw_file,
                row.sample_type
            );
        }
        let cell_identifier_ok = if single_cell_row {
            cell_id.is_match(row.cell_identifier.trim())
        } else {
            row.cell_identifier
                .trim()
                .eq_ignore_ascii_case("not applicable")
        };
        if !cell_id.is_match(row.source_name.trim()) || !cell_identifier_ok {
            bail!(
                "explicit row mapping {} RAW {} has non-template-safe source/cell identifier",
                accession,
                row.raw_file
            );
        }
        if row.technical_replicate.trim().is_empty()
            || !row
                .technical_replicate
                .trim()
                .chars()
                .all(|c| c.is_ascii_digit())
        {
            bail!(
                "explicit row mapping {} RAW {} requires numeric technical_replicate: {}",
                accession,
                row.raw_file,
                row.technical_replicate
            );
        }
        if single_cell_row {
            for (field, value) in [
                ("biological_replicate", row.biological_replicate.as_str()),
                ("cells_per_well", row.cells_per_well.as_str()),
            ] {
                if value.trim().is_empty() || !value.trim().chars().all(|c| c.is_ascii_digit()) {
                    bail!(
                        "explicit row mapping {} RAW {} requires numeric {} for a single-cell row: {}",
                        accession,
                        row.raw_file,
                        field,
                        value
                    );
                }
            }
        } else {
            for (field, value) in [
                ("biological_replicate", row.biological_replicate.as_str()),
                ("cells_per_well", row.cells_per_well.as_str()),
            ] {
                if !value.trim().eq_ignore_ascii_case("not applicable") {
                    bail!(
                        "explicit row mapping {} RAW {} requires '{}'='not applicable' for a non-single-cell study row: {}",
                        accession,
                        row.raw_file,
                        field,
                        value
                    );
                }
            }
        }
        if single_cell_row && row.label.trim().is_empty() {
            bail!(
                "explicit row mapping {} RAW {} requires an explicit analytical label",
                accession,
                row.raw_file
            );
        }
        if row.label.trim().is_empty() || row.carrier_channel.trim().is_empty() {
            bail!(
                "explicit row mapping {} RAW {} requires non-empty label/carrier fields (reserved values are allowed for non-single rows)",
                accession,
                row.raw_file
            );
        }
        let is_isobaric = {
            let label = row.label.to_ascii_lowercase();
            label.contains("tmt") || label.contains("itraq") || label.contains("isobaric")
        };
        if is_isobaric
            && (row
                .carrier_channel
                .trim()
                .eq_ignore_ascii_case("not applicable")
                || row
                    .carrier_channel
                    .trim()
                    .eq_ignore_ascii_case("not available"))
        {
            bail!(
                "explicit row mapping {} RAW {} uses an isobaric analytical label without an explicit carrier channel",
                accession,
                row.raw_file
            );
        }
        if single_cell_row
            && (row
                .carrier_channel
                .trim()
                .eq_ignore_ascii_case("not applicable")
                || row
                    .carrier_channel
                    .trim()
                    .eq_ignore_ascii_case("not available"))
        {
            bail!(
                "explicit row mapping {} RAW {} requires an explicit carrier channel under the single-analytical-channel contract",
                accession,
                row.raw_file
            );
        }
        if row.reference_channel.trim().is_empty() {
            bail!(
                "explicit row mapping {} RAW {} requires an explicit or reserved reference-channel value",
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
    if sample_type == "not available" {
        let cells_not_applicable = index
            .get(SC_CELLS_PER_WELL)
            .and_then(|&j| row.get(j))
            .map(|v| v.trim().eq_ignore_ascii_case("not applicable"))
            .unwrap_or(false);
        let cell_identifier_not_applicable = index
            .get(SC_CELL_IDENTIFIER)
            .and_then(|&j| row.get(j))
            .map(|v| v.trim().eq_ignore_ascii_case("not applicable"))
            .unwrap_or(false);
        if cells_not_applicable && cell_identifier_not_applicable {
            return true;
        }
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

fn normalized_semantic_token(value: &str) -> String {
    value
        .chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .flat_map(|c| c.to_lowercase())
        .collect()
}

fn concrete_semantic_value(value: &str) -> bool {
    let low = value.trim().to_ascii_lowercase();
    !low.is_empty()
        && !matches!(
            low.as_str(),
            "not available" | "not applicable" | "unknown" | "pooled" | "anonymized"
        )
}

fn individual_looks_synthesized_from_cell_line(individual: &str, cell_line: &str) -> bool {
    if !concrete_semantic_value(individual) || !concrete_semantic_value(cell_line) {
        return false;
    }
    let mut ind = normalized_semantic_token(individual);
    let cell = normalized_semantic_token(cell_line);
    if !ind.ends_with("donor") {
        return false;
    }
    ind.truncate(ind.len().saturating_sub("donor".len()));
    ind.len() >= 3 && (cell.starts_with(&ind) || ind.starts_with(&cell))
}

fn filename_acquisition_conflict(data_file: &str, acquisition: &str) -> Option<&'static str> {
    let file = data_file.to_ascii_lowercase();
    let acq = acquisition.to_ascii_lowercase();
    let explicit_dda = Regex::new(r"(?:^|[-_.])dda(?:top[0-9]+)?(?:[-_.]|$)")
        .expect("static DDA regex")
        .is_match(&file);
    let explicit_dia = Regex::new(r"(?:^|[-_.])dia(?:[-_.]|$)")
        .expect("static DIA regex")
        .is_match(&file);
    let explicit_wwa = Regex::new(r"(?:^|[-_.])wwa(?:[0-9]+|[-_.]|$)")
        .expect("static WWA regex")
        .is_match(&file);
    let metadata_dia = acq.contains("data-independent")
        || acq.contains("data independent")
        || (acq.contains("dia") && !acq.contains("dda"));
    let metadata_dda =
        acq.contains("data-dependent") || acq.contains("data dependent") || acq.contains("dda");
    if (explicit_dda || explicit_wwa) && metadata_dia {
        Some(if explicit_wwa {
            "filename_explicit_wwa_but_metadata_dia"
        } else {
            "filename_explicit_dda_but_metadata_dia"
        })
    } else if explicit_dia && metadata_dda && !metadata_dia {
        Some("filename_explicit_dia_but_metadata_dda")
    } else {
        None
    }
}

fn evidence_project_values(evidence: &DatasetEvidence, collection: &str) -> BTreeSet<String> {
    evidence
        .evidence
        .iter()
        .filter(|item| {
            item.source_kind == "pride_project"
                && item
                    .source_label
                    .contains(&format!("project:{collection}["))
                && item.source_label.ends_with(".name")
        })
        .map(|item| item.text.trim().to_string())
        .filter(|value| concrete_semantic_value(value))
        .collect()
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
    let individual_alias_values: BTreeMap<&str, BTreeSet<String>> = [
        "characteristics[organism part]",
        "characteristics[cell line]",
        "characteristics[cell type]",
        "characteristics[developmental stage]",
    ]
    .into_iter()
    .map(|header| {
        let values = index
            .get(header)
            .map(|&j| {
                rows.iter()
                    .filter_map(|row| row.get(j))
                    .filter(|value| concrete_semantic_value(value))
                    .map(|value| value.trim().to_ascii_lowercase())
                    .collect::<BTreeSet<_>>()
            })
            .unwrap_or_default();
        (header, values)
    })
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
        "pooled",
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
            if v == "study sample" {
                issues.push(ValidationIssue {
                    level: "error".into(),
                    code: "sample_type_study_sample_not_currently_validator_backed".into(),
                    row: ri + 1,
                    column: SC_SAMPLE_TYPE.into(),
                    message: "'study sample' is advertised by the SDRF glossary but is not currently resolvable as a PRIDE:0000895 child by the maintained validator cache; use a source-backed sample-role term or 'not available'".into(),
                });
            } else if !sample_types.contains(&v.as_str()) {
                issues.push(ValidationIssue { level: "warning".into(), code: "sample_type_requires_cv_validation".into(), row: ri + 1, column: SC_SAMPLE_TYPE.into(), message: format!("sample type is not in the local common-value set; defer ontology/CV validation to sdrf-pipelines: {}", row[j]) });
            }
        }
        let row_sample_type = index
            .get(SC_SAMPLE_TYPE)
            .and_then(|&j| row.get(j))
            .map(|value| value.trim().to_ascii_lowercase())
            .unwrap_or_default();
        let explicit_bulk_source = index
            .get("source name")
            .and_then(|&j| row.get(j))
            .map(|value| source_name_explicit_bulk_role(value))
            .unwrap_or(false);
        if row_sample_type == "single cell" && explicit_bulk_source {
            let safe_reserved = |header: &str| {
                index
                    .get(header)
                    .and_then(|&j| row.get(j))
                    .map(|value| {
                        matches!(
                            value.trim().to_ascii_lowercase().as_str(),
                            "" | "not available" | "not applicable" | "pooled"
                        )
                    })
                    .unwrap_or(true)
            };
            if safe_reserved(SC_CELLS_PER_WELL) && safe_reserved(SC_CELL_IDENTIFIER) {
                issues.push(ValidationIssue {
                    level: "error".into(),
                    code: "single_cell_sample_type_conflicts_with_explicit_bulk_source_role".into(),
                    row: ri + 1,
                    column: SC_SAMPLE_TYPE.into(),
                    message: "source name explicitly marks a bulk sample while sample type is 'single cell'; use the pooled/non-single-cell role without inferring cell count".into(),
                });
            }
        }
        // Cross-field scientific semantics: an individual/donor is not an organism part,
        // cell type, or cell line. The native audit reports the contradiction; readiness may only
        // remove deterministic category leakage with a hash-audited fail-closed projection repair.
        if let Some(&individual_idx) = index.get(SC_INDIVIDUAL) {
            let individual = row[individual_idx].trim();
            if concrete_semantic_value(individual) {
                for other_header in [
                    "characteristics[organism part]",
                    "characteristics[cell line]",
                    "characteristics[cell type]",
                    "characteristics[developmental stage]",
                ] {
                    if let Some(&other_idx) = index.get(other_header) {
                        let other = row[other_idx].trim();
                        if concrete_semantic_value(other) && individual.eq_ignore_ascii_case(other)
                        {
                            issues.push(ValidationIssue {
                                level: "error".into(),
                                code: "individual_duplicates_nonindividual_semantic_field".into(),
                                row: ri + 1,
                                column: SC_INDIVIDUAL.into(),
                                message: format!(
                                    "individual/donor value '{individual}' duplicates {other_header}; source-specific donor identity is unresolved"
                                ),
                            });
                            break;
                        }
                    }
                }
                if let Some(&cell_idx) = index.get("characteristics[cell line]") {
                    let cell_line = row[cell_idx].trim();
                    if individual_looks_synthesized_from_cell_line(individual, cell_line) {
                        issues.push(ValidationIssue {
                            level: "error".into(),
                            code: "individual_looks_synthesized_from_cell_line".into(),
                            row: ri + 1,
                            column: SC_INDIVIDUAL.into(),
                            message: format!(
                                "individual/donor value '{individual}' appears synthesized from cell line '{cell_line}' rather than source evidence"
                            ),
                        });
                    }
                }
                let pipe_values = individual
                    .split('|')
                    .map(str::trim)
                    .filter(|value| concrete_semantic_value(value))
                    .map(|value| value.to_ascii_lowercase())
                    .collect::<BTreeSet<_>>();
                if pipe_values.len() >= 2 {
                    for (header, values) in &individual_alias_values {
                        if values.len() >= 2 && &pipe_values == values {
                            issues.push(ValidationIssue {
                                level: "error".into(),
                                code: "individual_concatenates_nonindividual_dataset_semantics".into(),
                                row: ri + 1,
                                column: SC_INDIVIDUAL.into(),
                                message: format!(
                                    "individual/donor value '{individual}' is the dataset-wide concatenation of {header} values; source-specific individual identity is unresolved"
                                ),
                            });
                            break;
                        }
                    }
                }
            }
        }

        // Empty/zero-cell controls must not inherit concrete biological cell identities from
        // neighboring study rows.
        let sample_type = index
            .get(SC_SAMPLE_TYPE)
            .and_then(|&j| row.get(j))
            .map(|v| v.trim().to_ascii_lowercase())
            .unwrap_or_default();
        let cells_per_well = index
            .get(SC_CELLS_PER_WELL)
            .and_then(|&j| row.get(j))
            .map(|v| v.trim().to_ascii_lowercase())
            .unwrap_or_default();
        let source_name = index
            .get("source name")
            .and_then(|&j| row.get(j))
            .map(|v| v.trim().to_ascii_lowercase())
            .unwrap_or_default();
        let empty_control = matches!(sample_type.as_str(), "empty" | "blank" | "negative control")
            || cells_per_well == "0"
            || source_name.contains("blank");
        if empty_control {
            for header in [
                SC_INDIVIDUAL,
                "characteristics[cell type]",
                "characteristics[cell line]",
                "characteristics[cellosaurus accession]",
                "characteristics[cellosaurus name]",
            ] {
                if let Some(&j) = index.get(header) {
                    let value = row[j].trim();
                    if concrete_semantic_value(value) {
                        issues.push(ValidationIssue {
                            level: "error".into(),
                            code: "empty_control_has_concrete_biological_identity".into(),
                            row: ri + 1,
                            column: header.into(),
                            message: format!(
                                "empty/zero-cell control carries concrete biological identity '{value}'"
                            ),
                        });
                    }
                }
            }
            for header in [
                "characteristics[organism part]",
                "characteristics[disease]",
                "characteristics[developmental stage]",
                "characteristics[sex]",
            ] {
                if let Some(&j) = index.get(header) {
                    let value = row[j].trim();
                    if concrete_semantic_value(value) {
                        issues.push(ValidationIssue {
                            level: "warning".into(),
                            code: "empty_control_carries_contextual_biological_metadata".into(),
                            row: ri + 1,
                            column: header.into(),
                            message: format!(
                                "empty/zero-cell control carries contextual biological metadata '{value}'; confirm this is intentional study context rather than row leakage"
                            ),
                        });
                    }
                }
            }
            if let Some(&j) = index.get("characteristics[material type]") {
                let value = row[j].trim().to_ascii_lowercase();
                if matches!(
                    value.as_str(),
                    "cell" | "cell line" | "tissue" | "biofluid" | "primary cell"
                ) {
                    issues.push(ValidationIssue {
                        level: "error".into(),
                        code: "empty_control_has_concrete_biological_identity".into(),
                        row: ri + 1,
                        column: "characteristics[material type]".into(),
                        message: format!(
                            "empty/zero-cell control carries biological material type '{}'",
                            row[j]
                        ),
                    });
                }
            }
            if let Some(&j) = index.get(SC_CELL_IDENTIFIER) {
                let value = row[j].trim();
                if concrete_semantic_value(value) && !value.eq_ignore_ascii_case("empty") {
                    issues.push(ValidationIssue {
                        level: "error".into(),
                        code: "empty_control_has_concrete_biological_identity".into(),
                        row: ri + 1,
                        column: SC_CELL_IDENTIFIER.into(),
                        message: format!(
                            "empty/zero-cell control carries concrete cell identifier '{value}'"
                        ),
                    });
                }
            }
        }

        // Row-local technique compatibility. A concrete LCM microscope model is meaningful only
        // for an LCM-isolated row; Q Exactive-family instruments use an HCD cell rather than the
        // generic ion-trap CID term MS:1000133.
        if let Some(&model_idx) = index.get("comment[lcm microscope model]") {
            let model = row[model_idx].trim();
            if concrete_semantic_value(model) {
                let isolation = index
                    .get(SC_ISOLATION_METHOD)
                    .and_then(|&j| row.get(j))
                    .map(|v| v.trim().to_ascii_lowercase())
                    .unwrap_or_default();
                if !isolation.contains("laser capture") && isolation != "lcm" {
                    issues.push(ValidationIssue {
                        level: "error".into(),
                        code: "lcm_microscope_model_without_lcm_isolation".into(),
                        row: ri + 1,
                        column: "comment[lcm microscope model]".into(),
                        message: format!(
                            "LCM microscope model '{model}' is attached to non-LCM isolation protocol '{isolation}'"
                        ),
                    });
                }
            }
        }
        if let (Some(&instrument_idx), Some(&dissociation_idx)) = (
            index.get("comment[instrument]"),
            index.get("comment[dissociation method]"),
        ) {
            let instrument = row[instrument_idx].trim().to_ascii_lowercase();
            let dissociation = row[dissociation_idx].trim().to_ascii_lowercase();
            if instrument.contains("q exactive")
                && (dissociation == "cid" || dissociation.contains("ms:1000133"))
            {
                issues.push(ValidationIssue {
                    level: "error".into(),
                    code: "q_exactive_row_uses_generic_cid_instead_of_hcd".into(),
                    row: ri + 1,
                    column: "comment[dissociation method]".into(),
                    message: format!(
                        "Q Exactive-family row uses generic CID metadata '{}'; require source-backed HCD or fail closed", row[dissociation_idx]
                    ),
                });
            }
        }

        // Single-cell recommended metadata must use explicit reserved words rather than blank
        // strings when the value is unavailable/inapplicable.
        for header in [SC_PREP_BATCH, SC_CARRIER_CHANNEL, SC_REFERENCE_CHANNEL] {
            if let Some(&j) = index.get(header) {
                if row[j].trim().is_empty() {
                    issues.push(ValidationIssue {
                        level: "error".into(),
                        code: "single_cell_recommended_value_blank".into(),
                        row: ri + 1,
                        column: header.into(),
                        message: "single-cell metadata value is blank; use a source-backed value or SDRF reserved word".into(),
                    });
                }
            }
        }

        if let (Some(&file_idx), Some(&acq_idx)) = (
            index.get("comment[data file]"),
            index.get("comment[proteomics data acquisition method]"),
        ) {
            if let Some(reason) = filename_acquisition_conflict(&row[file_idx], &row[acq_idx]) {
                issues.push(ValidationIssue {
                    level: "error".into(),
                    code: "data_file_name_acquisition_contradiction".into(),
                    row: ri + 1,
                    column: "comment[proteomics data acquisition method]".into(),
                    message: format!(
                        "explicit acquisition token in data file '{}' contradicts metadata '{}': {reason}",
                        row[file_idx], row[acq_idx]
                    ),
                });
            }
        }

        // Narrow model/version slots are identifiers, not containers for clipped methods prose.
        for header in [
            "comment[nanopots chip version]",
            "comment[microfluidics chip type]",
            "comment[lcm microscope model]",
        ] {
            if let Some(&j) = index.get(header) {
                let value = row[j].trim();
                let low = value.to_ascii_lowercase();
                let prose_hint = low.contains("figure")
                    || low.contains("prepared")
                    || low.contains("described")
                    || (low.contains('[') && (low.contains(" and ") || low.contains(" then ")));
                let last_is_single_char = value
                    .split_whitespace()
                    .last()
                    .map_or(false, |x| x.len() == 1);
                if concrete_semantic_value(value)
                    && prose_hint
                    && (value.len() > 35 || last_is_single_char)
                {
                    issues.push(ValidationIssue {
                        level: "error".into(),
                        code: "narrow_metadata_field_contains_truncated_protocol_prose".into(),
                        row: ri + 1,
                        column: header.into(),
                        message: format!(
                            "narrow metadata field contains prose/truncation rather than a model/version identifier: {value}"
                        ),
                    });
                }
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

    // A full-repository draft must not collapse a project with multiple explicit organisms or
    // organism parts into one uniform candidate value. This is a contradiction detector only;
    // it never assigns row-level biology.
    for (collection, header, code) in [
        (
            "organisms",
            "characteristics[organism]",
            "multiorganism_project_collapsed_to_single_candidate_organism",
        ),
        (
            "organismParts",
            "characteristics[organism part]",
            "multi_organism_part_project_collapsed_to_single_candidate_part",
        ),
    ] {
        let project_values = evidence_project_values(evidence, collection);
        if project_values.len() > 1 {
            if let Some(&j) = index.get(header) {
                let candidate_values = rows
                    .iter()
                    .filter_map(|row| row.get(j))
                    .map(|v| v.trim().to_string())
                    .filter(|v| concrete_semantic_value(v))
                    .collect::<BTreeSet<_>>();
                if candidate_values.len() == 1 {
                    let level = if collection == "organismParts" {
                        "warning"
                    } else {
                        "error"
                    };
                    issues.push(ValidationIssue {
                        level: level.into(),
                        code: code.into(),
                        row: 0,
                        column: header.into(),
                        message: format!(
                            "PRIDE project exposes multiple source values {:?}, but draft collapses them to {:?}; row-level source mapping may be unresolved. organismParts is diagnostic-only because repository metadata can contain cell-line/cell-type concepts",
                            project_values, candidate_values
                        ),
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
    relation_mode: &str,
    relation_global_row_serialization: bool,
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
    } else if relation_mode == "multiplexed_cells_per_data_file"
        || evidence.study_design.relation_mode_hint == "multiplexed_cells_per_data_file"
    {
        (
            "sample_to_channel_mapping_unresolved",
            format!(
                "{} design detected (chemistry='{}', mapping_status='{}'); per-channel/per-sample SDRF rows are intentionally not fabricated",
                relation_mode,
                evidence.study_design.multiplex_chemistry_hint,
                evidence.study_design.multiplex_mapping_status
            ),
        )
    } else if relation_mode == "one_cell_per_data_file" && !relation_global_row_serialization {
        (
            "sample_to_file_branch_mapping_unresolved",
            "acquisition cardinality is one biological acquisition unit per data file, but the relation is established only at branch scope; exact branch membership / biological row linkage is unresolved, so source and cell identifiers are intentionally not fabricated from filenames".to_string(),
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

fn is_mapping_validation_error(code: &str) -> bool {
    code == "sample_to_file_relation_unresolved"
        || code == "explicit_row_mapping_repository_scope_incomplete"
        || code.contains("channel_mapping")
        || code.contains("sample_to_file")
}

fn derive_readiness_dimensions(
    existing: bool,
    explicit_mapping_rows: usize,
    raw_file_count: usize,
    relation_mode: &str,
    relation_global_row_serialization: bool,
    repository_file_mode: &str,
    has_template_gap: bool,
    issues: &[ValidationIssue],
    design_trace: Option<&DesignAgentTrace>,
) -> (String, String, String, String, String) {
    let mapping_status = if existing {
        "existing_sdrf_mapping"
    } else if explicit_mapping_rows > 0 && explicit_mapping_rows >= raw_file_count {
        "resolved_explicit_mapping"
    } else if explicit_mapping_rows > 0 {
        "partial_explicit_mapping"
    } else if relation_mode == "one_cell_per_data_file"
        && relation_global_row_serialization
        && repository_file_mode != "generic_archives_only"
    {
        "resolved_one_cell_per_file"
    } else if relation_mode == "one_cell_per_data_file" {
        "relation_resolved_branch_mapping_unresolved"
    } else if relation_mode == "multiplexed_cells_per_data_file" {
        "unresolved_channel_mapping"
    } else if relation_mode == "mixed" {
        "unresolved_mixed_relation"
    } else {
        "unresolved"
    }
    .to_string();

    let template_status = if has_template_gap {
        "template_vocabulary_gap"
    } else {
        "compatible"
    }
    .to_string();

    let non_mapping_errors = issues
        .iter()
        .filter(|issue| issue.level == "error" && !is_mapping_validation_error(&issue.code))
        .count();
    let mapping_errors = issues
        .iter()
        .filter(|issue| issue.level == "error" && is_mapping_validation_error(&issue.code))
        .count();
    let metadata_status = if non_mapping_errors > 0 {
        "incomplete"
    } else if mapping_errors > 0 {
        "validator_complete_except_mapping"
    } else {
        "validator_complete"
    }
    .to_string();

    let agent_terminal_status = design_trace
        .map(|trace| trace.final_assessment.terminal_status.clone())
        .unwrap_or_else(|| "not_applicable".into());
    let evidence_status = agent_terminal_status.clone();

    (
        mapping_status,
        template_status,
        metadata_status,
        evidence_status,
        agent_terminal_status,
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
                agent_terminal_status: audit.agent_terminal_status,
                mapping_status: audit.mapping_status,
                template_status: audit.template_status,
                metadata_status: audit.metadata_status,
                evidence_status: audit.evidence_status,
                validation_errors: audit.validation_error_count,
                proposal_repairs: audit.proposal_repair_count,
                draft_path: audit.draft_path,
                review_path: audit.review_path,
                error: String::new(),
            });
        }
    }
    let mut evidence = build_evidence(opts, accession)?;
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

    let design_assessment_path =
        proposal_path.with_file_name(format!("{accession}.design_assessment.json"));
    let evidence_actions_path =
        proposal_path.with_file_name(format!("{accession}.evidence_actions.json"));
    let validator_repair_path =
        proposal_path.with_file_name(format!("{accession}.validator_repair.json"));
    let validator_repair_actions_path =
        proposal_path.with_file_name(format!("{accession}.validator_repair_actions.json"));
    let mut design_trace: Option<DesignAgentTrace> = None;
    let mut validator_repair_trace: Option<ValidatorRepairTrace> = None;
    let mut deterministic_validator_repairs = Vec::new();

    let (mut proposal, ollama_used) = if let Some(proposal) =
        deterministic_existing_sdrf_proposal(&evidence)
    {
        (proposal, false)
    } else {
        let initial_assessment = call_ollama_design_assessment(opts, &evidence, &[]).await?;
        let mut current_assessment = initial_assessment.clone();
        let mut evidence_action_results = Vec::new();
        let mut attempted_actions = BTreeSet::new();
        let mut rounds_completed = 0usize;
        let mut deferred_evidence_actions = Vec::new();

        for round in 1..=DESIGN_AGENT_MAX_ROUNDS {
            if current_assessment.terminal_status != "continue"
                || current_assessment.next_evidence_actions.is_empty()
            {
                break;
            }
            let round_results = execute_evidence_actions(
                &mut evidence,
                &current_assessment.next_evidence_actions,
                round,
                &mut attempted_actions,
            );
            if round_results.is_empty() {
                current_assessment.terminal_status = "partial".into();
                current_assessment.next_evidence_actions.clear();
                break;
            }
            rounds_completed = round;
            let terminal_abstain = round_results.iter().any(|r| r.outcome == "abstain");
            evidence_action_results.extend(round_results);
            if terminal_abstain {
                current_assessment.terminal_status = "abstained".into();
                current_assessment.next_evidence_actions.clear();
                break;
            }
            current_assessment =
                call_ollama_design_assessment(opts, &evidence, &evidence_action_results).await?;
        }

        if rounds_completed >= DESIGN_AGENT_MAX_ROUNDS
            && current_assessment.terminal_status == "continue"
        {
            deferred_evidence_actions =
                std::mem::take(&mut current_assessment.next_evidence_actions);
            current_assessment.terminal_status = "evidence_exhausted".into();
        }
        if current_assessment.terminal_status == "continue" {
            current_assessment.terminal_status = if current_assessment.missing_linkages.is_empty()
                && current_assessment.conflicts.is_empty()
            {
                "resolved".into()
            } else {
                "partial".into()
            };
            current_assessment.next_evidence_actions.clear();
        }

        let final_assessment = current_assessment;
        fs::write(&evidence_path, serde_json::to_string_pretty(&evidence)?)?;
        let trace = DesignAgentTrace {
            initial_assessment,
            evidence_action_results,
            final_assessment: final_assessment.clone(),
            rounds_completed,
            deferred_evidence_actions,
        };
        fs::write(
            &design_assessment_path,
            serde_json::to_string_pretty(&trace)?,
        )?;
        fs::write(
            &evidence_actions_path,
            serde_json::to_string_pretty(&trace.evidence_action_results)?,
        )?;
        design_trace = Some(trace);
        (
            call_ollama(opts, &evidence, Some(&final_assessment)).await?,
            true,
        )
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
    if let Some(trace) = design_trace.as_ref() {
        provenance_issues.extend(apply_design_assessment_guard(
            &mut proposal,
            &trace.final_assessment,
            &evidence.study_design.relation_mode_hint,
        ));
    }
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
    let relation_arbitration = relation_scaffold_arbitration(
        design_trace.as_ref().map(|trace| &trace.final_assessment),
        &evidence.study_design.relation_mode_hint,
    );
    if evidence.existing_sdrf_path.is_empty()
        && study_design_has_assertive_relation_hint(&evidence.study_design)
        && relation_arbitration_allows_scaffold(relation_arbitration)
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
                code: match relation_arbitration {
                    RelationArbitration::AllowConfirm => {
                        "agent_relation_consensus_confirms_scaffold".into()
                    }
                    RelationArbitration::Defer => {
                        "agent_relation_deferred_to_deterministic_scaffold".into()
                    }
                    _ => "relation_mode_determined_from_study_design_scaffold".into(),
                },
                row: 0,
                column: "relation_mode".into(),
                message: format!(
                    "trusted deterministic study-design evidence changed relation_mode from '{previous}' to '{hint}' (confidence={}, arbitration={:?})",
                    evidence.study_design.relation_confidence,
                    relation_arbitration
                ),
            });
        }
    } else if evidence.existing_sdrf_path.is_empty()
        && study_design_has_assertive_relation_hint(&evidence.study_design)
        && !relation_arbitration_allows_scaffold(relation_arbitration)
    {
        let relation = design_trace
            .as_ref()
            .map(|trace| &trace.final_assessment.relation_assessment);
        provenance_issues.push(ValidationIssue {
            level: "warning".into(),
            code: "agent_blocks_relation_scaffold_broadcast".into(),
            row: 0,
            column: "relation_mode".into(),
            message: match relation {
                Some(relation) => format!(
                    "design agent explicitly assessed relation mode='{}' scope='{}' (origin={}, confidence={}, refs={:?}); deterministic_hint='{}'; arbitration={:?}; deterministic relation scaffold was not broadcast; reason={}",
                    relation.mode,
                    relation.scope,
                    relation.claim_origin,
                    relation.confidence,
                    relation.evidence_refs,
                    evidence.study_design.relation_mode_hint,
                    relation_arbitration,
                    relation.reason
                ),
                None => "design agent explicitly blocked deterministic relation scope; deterministic relation scaffold was not broadcast".into(),
            },
        });
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
        design_trace.as_ref().map(|trace| &trace.final_assessment),
    ));
    provenance_issues.extend(apply_publication_compatibility_normalization(&mut proposal));
    if let Some(issue) = sanitize_nonindividual_semantic_proposal(&mut proposal) {
        deterministic_validator_repairs.push(issue.code.clone());
        provenance_issues.push(issue);
    }
    fs::write(&proposal_path, serde_json::to_string_pretty(&proposal)?)?;
    validate_proposal_refs(&proposal, &evidence)?;
    let mut proposal_repair_count = provenance_issues.len();
    let existing = !evidence.existing_sdrf_path.is_empty();

    let relation_global_row_serialization = relation_allows_global_row_serialization(
        design_trace.as_ref().map(|trace| &trace.final_assessment),
        &proposal.relation_mode,
    );
    let mut row_proposal = proposal.clone();
    if evidence.existing_sdrf_path.is_empty()
        && explicit_mappings.is_empty()
        && proposal.relation_mode == "one_cell_per_data_file"
        && !relation_global_row_serialization
    {
        // Acquisition cardinality may be resolved at branch scope while branch
        // membership / biological row linkage is still unresolved. Preserve the
        // concrete relation in the audit/proposal, but do not let it authorize
        // filename-derived source/cell identifiers across the entire repository.
        row_proposal.relation_mode = "uncertain".into();
    }
    let (mut headers, mut rows, mut generation_mode) =
        draft_rows_with_explicit_mappings(&row_proposal, &evidence, &explicit_mappings)?;
    if evidence.existing_sdrf_path.is_empty()
        && explicit_mappings.is_empty()
        && proposal.relation_mode == "one_cell_per_data_file"
        && !relation_global_row_serialization
    {
        generation_mode = "generated_relation_resolved_branch_mapping_unresolved".into();
    }
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
                "serialized {} source-grounded row(s) from explicit mapping manifest {} (refs: {})",
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
    let base_issues = issues.clone();
    let initial_validation_issues = if !existing
        && explicit_mappings.is_empty()
        && (proposal.relation_mode != "one_cell_per_data_file"
            || !relation_global_row_serialization
            || evidence.study_design.repository_file_mode == "generic_archives_only")
    {
        validate_incomplete_mapping_scaffold(
            &headers,
            &rows,
            &evidence,
            &proposal.relation_mode,
            relation_global_row_serialization,
        )
    } else {
        validate_annotation_draft(&headers, &rows, &evidence, existing)
    };
    issues.extend(initial_validation_issues);

    let repair_tasks = cluster_validator_repair_tasks(&issues);
    if !existing && ollama_used && !repair_tasks.is_empty() {
        let design_assessment = design_trace.as_ref().map(|trace| &trace.final_assessment);
        let prior_design_action_results = design_trace
            .as_ref()
            .map(|trace| trace.evidence_action_results.as_slice())
            .unwrap_or(&[]);
        let mut initial_repair = call_ollama_validator_repair(
            opts,
            &evidence,
            &proposal,
            design_assessment,
            &repair_tasks,
            "plan",
            prior_design_action_results,
            &[],
        )
        .await?;
        let mut final_repair = initial_repair.clone();
        let mut action_results = Vec::new();
        let mut rounds_completed = 0usize;
        let needs_retrieval = VALIDATOR_REPAIR_MAX_ROUNDS > 0
            && validator_repair_plan_needs_retrieval(
                &evidence,
                design_assessment,
                &repair_tasks,
                &initial_repair,
            );
        if needs_retrieval {
            if initial_repair.next_evidence_actions.is_empty() {
                initial_repair.next_evidence_actions = synthesized_validator_repair_actions(
                    &evidence,
                    design_assessment,
                    &repair_tasks,
                    &initial_repair,
                );
            }
            if !initial_repair.next_evidence_actions.is_empty() {
                initial_repair.terminal_status = "continue".into();
                let mut attempted_actions =
                    seed_attempted_actions_from_history(prior_design_action_results);
                action_results = execute_evidence_actions(
                    &mut evidence,
                    &initial_repair.next_evidence_actions,
                    1,
                    &mut attempted_actions,
                );
                rounds_completed = 1;
                if action_results
                    .iter()
                    .all(|result| result.outcome == "abstain")
                {
                    final_repair = initial_repair.clone();
                    final_repair.terminal_status = "abstained".into();
                    final_repair.next_evidence_actions.clear();
                } else {
                    final_repair = call_ollama_validator_repair(
                        opts,
                        &evidence,
                        &proposal,
                        design_assessment,
                        &repair_tasks,
                        "decide",
                        prior_design_action_results,
                        &action_results,
                    )
                    .await?;
                }
            } else {
                initial_repair.terminal_status = "partial".into();
                final_repair = initial_repair.clone();
            }
        } else {
            initial_repair.next_evidence_actions.clear();
            initial_repair.terminal_status = "resolved".into();
            final_repair = initial_repair.clone();
        }
        if final_repair.terminal_status == "continue" {
            final_repair.terminal_status = "partial".into();
            final_repair.next_evidence_actions.clear();
        }

        let mut repair_issues = apply_validator_repair_template_gaps(&mut evidence, &final_repair);
        let (decision_issues, applied_fields) = apply_validator_repair_decisions(
            &mut proposal,
            &evidence,
            design_assessment,
            &repair_tasks,
            &final_repair,
        );
        repair_issues.extend(decision_issues);
        proposal_repair_count += repair_issues.len();

        if !applied_fields.is_empty() {
            validate_proposal_refs(&proposal, &evidence)?;
            fs::write(&proposal_path, serde_json::to_string_pretty(&proposal)?)?;
            fs::write(&evidence_path, serde_json::to_string_pretty(&evidence)?)?;
            let mut repaired_row_proposal = proposal.clone();
            if evidence.existing_sdrf_path.is_empty()
                && explicit_mappings.is_empty()
                && proposal.relation_mode == "one_cell_per_data_file"
                && !relation_global_row_serialization
            {
                repaired_row_proposal.relation_mode = "uncertain".into();
            }
            let (new_headers, new_rows, new_generation_mode) = draft_rows_with_explicit_mappings(
                &repaired_row_proposal,
                &evidence,
                &explicit_mappings,
            )?;
            headers = new_headers;
            rows = new_rows;
            generation_mode = new_generation_mode;
            if evidence.existing_sdrf_path.is_empty()
                && explicit_mappings.is_empty()
                && proposal.relation_mode == "one_cell_per_data_file"
                && !relation_global_row_serialization
            {
                generation_mode = "generated_relation_resolved_branch_mapping_unresolved".into();
            }
            write_sdrf(&draft_path, &headers, &rows)?;
            issues = base_issues.clone();
            issues.extend(repair_issues.clone());
            let repaired_validation_issues = if !existing
                && explicit_mappings.is_empty()
                && (proposal.relation_mode != "one_cell_per_data_file"
                    || !relation_global_row_serialization
                    || evidence.study_design.repository_file_mode == "generic_archives_only")
            {
                validate_incomplete_mapping_scaffold(
                    &headers,
                    &rows,
                    &evidence,
                    &proposal.relation_mode,
                    relation_global_row_serialization,
                )
            } else {
                validate_annotation_draft(&headers, &rows, &evidence, existing)
            };
            issues.extend(repaired_validation_issues);
        } else {
            issues.extend(repair_issues.clone());
            fs::write(&evidence_path, serde_json::to_string_pretty(&evidence)?)?;
        }

        let trace = ValidatorRepairTrace {
            repair_agent_version: VALIDATOR_REPAIR_AGENT_VERSION.into(),
            tasks: repair_tasks.clone(),
            initial_assessment: initial_repair,
            evidence_action_results: action_results.clone(),
            final_assessment: final_repair,
            rounds_completed,
            applied_fields,
            deterministic_repairs: deterministic_validator_repairs.clone(),
        };
        fs::write(
            &validator_repair_path,
            serde_json::to_string_pretty(&trace)?,
        )?;
        fs::write(
            &validator_repair_actions_path,
            serde_json::to_string_pretty(&trace.evidence_action_results)?,
        )?;
        validator_repair_trace = Some(trace);
    } else if !deterministic_validator_repairs.is_empty() {
        let trace = ValidatorRepairTrace {
            repair_agent_version: VALIDATOR_REPAIR_AGENT_VERSION.into(),
            tasks: repair_tasks,
            initial_assessment: ValidatorRepairAssessment {
                repair_agent_version: VALIDATOR_REPAIR_AGENT_VERSION.into(),
                terminal_status: "resolved".into(),
                notes: "deterministic semantic sanitization required no LLM repair pass".into(),
                ..ValidatorRepairAssessment::default()
            },
            final_assessment: ValidatorRepairAssessment {
                repair_agent_version: VALIDATOR_REPAIR_AGENT_VERSION.into(),
                terminal_status: "resolved".into(),
                notes: "deterministic semantic sanitization required no LLM repair pass".into(),
                ..ValidatorRepairAssessment::default()
            },
            deterministic_repairs: deterministic_validator_repairs.clone(),
            ..ValidatorRepairTrace::default()
        };
        fs::write(
            &validator_repair_path,
            serde_json::to_string_pretty(&trace)?,
        )?;
        validator_repair_trace = Some(trace);
    }

    write_review(&review_path, &issues)?;
    let errors = issues.iter().filter(|x| x.level == "error").count();
    let locally_valid = errors == 0;
    let has_template_gap = !evidence.metadata_scaffold.template_gaps.is_empty();
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
    } else if explicit_scope_incomplete {
        "incomplete_explicit_row_mapping_repository_scope"
    } else if !explicit_mappings.is_empty() && has_isolation_template_gap {
        "incomplete_template_isolation_method_gap"
    } else if !explicit_mappings.is_empty() && locally_valid {
        "locally_valid_draft"
    } else if !explicit_mappings.is_empty() {
        "incomplete_required_metadata"
    } else if proposal.relation_mode != "one_cell_per_data_file"
        || !relation_global_row_serialization
    {
        "incomplete_sample_to_file_or_channel_mapping"
    } else if has_isolation_template_gap {
        "incomplete_template_isolation_method_gap"
    } else if locally_valid {
        "locally_valid_draft"
    } else {
        "incomplete_required_metadata"
    };
    let (mapping_status, template_status, metadata_status, evidence_status, agent_terminal_status) =
        derive_readiness_dimensions(
            existing,
            explicit_mappings.len(),
            evidence.raw_files.len(),
            &proposal.relation_mode,
            relation_global_row_serialization,
            &evidence.study_design.repository_file_mode,
            has_template_gap,
            &issues,
            design_trace.as_ref(),
        );
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
        design_agent_version: if design_trace.is_some() { DESIGN_AGENT_VERSION.into() } else { String::new() },
        design_assessment_path: if design_trace.is_some() { design_assessment_path.display().to_string() } else { String::new() },
        evidence_actions_path: if design_trace.is_some() { evidence_actions_path.display().to_string() } else { String::new() },
        validator_repair_agent_version: if validator_repair_trace.is_some() { VALIDATOR_REPAIR_AGENT_VERSION.into() } else { String::new() },
        validator_repair_path: if validator_repair_trace.is_some() { validator_repair_path.display().to_string() } else { String::new() },
        validator_repair_actions_path: validator_repair_trace.as_ref().filter(|trace| !trace.evidence_action_results.is_empty()).map(|_| validator_repair_actions_path.display().to_string()).unwrap_or_default(),
        validator_repair_rounds: validator_repair_trace.as_ref().map(|trace| trace.rounds_completed).unwrap_or(0),
        validator_repair_task_count: validator_repair_trace.as_ref().map(|trace| trace.tasks.len()).unwrap_or(0),
        draft_path: draft_path.display().to_string(), proposal_path: proposal_path.display().to_string(), evidence_path: evidence_path.display().to_string(),
        review_path: review_path.display().to_string(), validation_issue_count: issues.len(), validation_error_count: errors, proposal_repair_count,
        explicit_row_mapping_manifest: opts.explicit_row_mapping_manifest.as_ref().map(|p| p.display().to_string()).unwrap_or_default(),
        explicit_row_mapping_rows: explicit_mappings.len(), locally_valid,
        completeness_status: completeness.into(),
        agent_terminal_status: agent_terminal_status.clone(),
        mapping_status: mapping_status.clone(),
        template_status: template_status.clone(),
        metadata_status: metadata_status.clone(),
        evidence_status: evidence_status.clone(),
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
        agent_terminal_status,
        mapping_status,
        template_status,
        metadata_status,
        evidence_status,
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
                        "  -> {} relation={} valid={} status={} mapping={} template={} evidence={}",
                        row.status,
                        row.relation_mode,
                        row.locally_valid,
                        row.completeness_status,
                        row.mapping_status,
                        row.template_status,
                        row.evidence_status
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
                    agent_terminal_status: "error".into(),
                    mapping_status: "error".into(),
                    template_status: "error".into(),
                    metadata_status: "error".into(),
                    evidence_status: "error".into(),
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
        let annotation_idx = headers
            .iter()
            .position(|h| h == "comment[sdrf annotation tool]")
            .unwrap();
        assert_eq!(rows[0][annotation_idx], "pride-scp-sdrf v0.5.4");
        assert_eq!(sdrf_annotation_tool_value(), "pride-scp-sdrf v0.5.4");
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
    fn explicit_source_grounded_manifest_serializes_non_single_study_rows_without_cell_metadata_leakage(
    ) {
        let evidence = DatasetEvidence {
            accession: "PXD028040".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![
                RawFile {
                    file_name: "2018-08-08_SC02_reference.RAW".into(),
                    file_uri: "ftp://example/2018-08-08_SC02_reference.RAW".into(),
                    category: "RAW".into(),
                },
                RawFile {
                    file_name: "2018-08-15_SC02_tmt_reference.RAW".into(),
                    file_uri: "ftp://example/2018-08-15_SC02_tmt_reference.RAW".into(),
                    category: "RAW".into(),
                },
                RawFile {
                    file_name: "2018-08-27_SC02.RAW".into(),
                    file_uri: "ftp://example/2018-08-27_SC02.RAW".into(),
                    category: "RAW".into(),
                },
            ],
            study_design: StudyDesignScaffold::default(),
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
                "PXD028040\t2018-08-08_SC02_reference.RAW\twhole_tissue_digest_2018_08_08_SC02\tnot applicable\tnot applicable\t1\tnot available\tnot applicable\tnot available\tnot applicable\tnot applicable\tChoi_design.xlsx\tSheet2:row6\tdate_sc_run_key\thigh\n",
                "PXD028040\t2018-08-15_SC02_tmt_reference.RAW\twhole_tissue_digest_2018_08_15_SC02\tnot applicable\tnot applicable\t1\tnot available\tnot applicable\tTMT128\tTMT131\tnot applicable\tChoi_design.xlsx\tSheet2:row9\tdate_sc_run_key\thigh\n",
                "PXD028040\t2018-08-27_SC02.RAW\tDA_neuron_1\tDA_neuron_1\t1\t1\tsingle cell\t1\tTMT128\tTMT131\tnot applicable\tChoi_design.xlsx\tSheet2:row15\tdate_sc_run_key\thigh\n"
            ),
        )
        .unwrap();
        let mappings =
            load_explicit_row_mappings(&manifest, "PXD028040", &evidence.raw_files).unwrap();
        assert_eq!(mappings.len(), 3);
        let proposal = SdrfProposal {
            relation_mode: "one_cell_per_data_file".into(),
            organism: "Mus musculus".into(),
            organism_part: "substantia nigra pars compacta".into(),
            disease: "normal".into(),
            cell_type: "dopaminergic neuron".into(),
            sample_type: "single cell".into(),
            single_cell_isolation_method: "manual picking".into(),
            individual: "mouse_1".into(),
            sample_preparation_batch: "batch_1".into(),
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
        let (headers, rows, _) =
            draft_rows_with_explicit_mappings(&proposal, &evidence, &mappings).unwrap();
        let at = |name: &str| headers.iter().position(|h| h == name).unwrap();
        for row in &rows[..2] {
            assert_eq!(row[at(SC_SAMPLE_TYPE)], "not available");
            assert_eq!(row[at("characteristics[cell type]")], "not applicable");
            assert_eq!(row[at(SC_ISOLATION_METHOD)], "not applicable");
            assert_eq!(row[at(SC_CELL_IDENTIFIER)], "not applicable");
            assert_eq!(row[at(SC_INDIVIDUAL)], "not applicable");
            assert_eq!(row[at(SC_PREP_BATCH)], "not applicable");
            assert_eq!(row[at(SC_CELLS_PER_WELL)], "not applicable");
        }
        assert_eq!(rows[0][at("comment[label]")], "not available");
        assert_eq!(rows[0][at(SC_CARRIER_CHANNEL)], "not applicable");
        assert_eq!(rows[1][at("comment[label]")], "TMT128");
        assert_eq!(rows[1][at(SC_CARRIER_CHANNEL)], "TMT131");
        assert_eq!(rows[2][at(SC_SAMPLE_TYPE)], "single cell");
        assert_eq!(
            rows[2][at("characteristics[cell type]")],
            "dopaminergic neuron"
        );
        assert_eq!(rows[2][at(SC_ISOLATION_METHOD)], "manual picking");
        let issues = validate_draft(&headers, &rows, &evidence);
        assert!(issues.iter().all(|x| x.level != "error"), "{issues:?}");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn explicit_manifest_rejects_unbacked_study_sample_but_accepts_not_available() {
        let raw_files = vec![RawFile {
            file_name: "example.RAW".into(),
            file_uri: "ftp://example/example.RAW".into(),
            category: "RAW".into(),
        }];
        let root = tmp();
        fs::create_dir_all(&root).unwrap();
        let manifest = root.join("mapping.tsv");
        let header = "accession\traw_file\tsource_name\tcell_identifier\tbiological_replicate\ttechnical_replicate\tsample_type\tcells_per_well\tlabel\tcarrier_channel\treference_channel\tdesign_source\tdesign_ref\tmapping_key\tmapping_confidence\n";

        fs::write(
            &manifest,
            format!(
                "{header}PXD000001\texample.RAW\tcontrol\tnot applicable\tnot applicable\t1\tnot available\tnot applicable\tnot available\tnot applicable\tnot applicable\tdesign.tsv\trow1\texact_raw_name\thigh\n"
            ),
        )
        .unwrap();
        let accepted = load_explicit_row_mappings(&manifest, "PXD000001", &raw_files).unwrap();
        assert_eq!(accepted.len(), 1);
        assert_eq!(accepted[0].sample_type, "not available");

        fs::write(
            &manifest,
            format!(
                "{header}PXD000001\texample.RAW\tcontrol\tnot applicable\tnot applicable\t1\tstudy sample\tnot applicable\tnot available\tnot applicable\tnot applicable\tdesign.tsv\trow1\texact_raw_name\thigh\n"
            ),
        )
        .unwrap();
        let err = load_explicit_row_mappings(&manifest, "PXD000001", &raw_files)
            .unwrap_err()
            .to_string();
        assert!(err.contains("sample_type=study sample"), "{err}");
        assert!(err.contains("not currently validator-backed"), "{err}");
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
    fn existing_sdrf_merge_preserves_explicit_bulk_source_role_as_pooled() {
        let root = tmp();
        fs::create_dir_all(&root).unwrap();
        let existing = root.join("bulk-existing.sdrf.tsv");
        fs::write(
            &existing,
            "source name\tcharacteristics[organism]\tassay name\ttechnology type\tcomment[proteomics data acquisition method]\tcomment[label]\tcomment[instrument]\tcomment[cleavage agent details]\tcomment[fraction identifier]\tcomment[technical replicate]\tcomment[data file]\tcharacteristics[sample type]\tcharacteristics[cells per well]\tcharacteristics[cell identifier]\nPC3_parental_bulk_library\tHomo sapiens\trun1\tproteomic profiling by mass spectrometry\tData-independent acquisition\tlabel free sample\tOrbitrap\tNT=Trypsin;AC=MS:1001251\t1\t1\tlib.mzML\tsingle cell\tnot applicable\tnot applicable\n",
        )
        .unwrap();
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: existing.display().to_string(),
            raw_files: vec![RawFile {
                file_name: "lib.mzML".into(),
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
            ..Default::default()
        };
        let (headers, rows, _) = merge_existing_sdrf(&proposal, &evidence).unwrap();
        let sample_type = header_first_index(&headers, SC_SAMPLE_TYPE).unwrap();
        assert_eq!(rows[0][sample_type], "pooled");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn existing_sdrf_merge_normalizes_blank_nonisobaric_channels_without_inference() {
        let root = tmp();
        fs::create_dir_all(&root).unwrap();
        let existing = root.join("mixed-label-existing.sdrf.tsv");
        fs::write(
            &existing,
            "source name\tcharacteristics[organism]\tassay name\ttechnology type\tcomment[proteomics data acquisition method]\tcomment[label]\tcomment[instrument]\tcomment[cleavage agent details]\tcomment[fraction identifier]\tcomment[technical replicate]\tcomment[data file]\tcomment[sample preparation batch]\tcomment[carrier channel]\tcomment[reference channel]\ncell_A\tHomo sapiens\trun1\tproteomic profiling by mass spectrometry\tData-dependent acquisition\tNT=label free sample;AC=MS:1002038\tOrbitrap\tNT=Trypsin;AC=MS:1001251\t1\t1\tcell_A.raw\tbatch1\t\t\npool_A\tHomo sapiens\trun2\tproteomic profiling by mass spectrometry\tData-dependent acquisition\tNT=DIMETHYL0;AC=PRIDE:0000848\tOrbitrap\tNT=Trypsin;AC=MS:1001251\t1\t1\tpool_A.raw\tbatch1\t\t\n",
        )
        .unwrap();
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: existing.display().to_string(),
            raw_files: vec![],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let proposal = SdrfProposal {
            relation_mode: "one_cell_per_data_file".into(),
            ..Default::default()
        };
        let (headers, rows, _) = merge_existing_sdrf(&proposal, &evidence).unwrap();
        let carrier = header_first_index(&headers, SC_CARRIER_CHANNEL).unwrap();
        let reference = header_first_index(&headers, SC_REFERENCE_CHANNEL).unwrap();
        assert_eq!(rows[0][carrier], "not applicable");
        assert_eq!(rows[0][reference], "not applicable");
        assert_eq!(rows[1][carrier], "not applicable");
        assert_eq!(rows[1][reference], "not applicable");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn existing_sdrf_merge_unknown_channel_state_fails_closed_not_available() {
        let root = tmp();
        fs::create_dir_all(&root).unwrap();
        let existing = root.join("isobaric-existing.sdrf.tsv");
        fs::write(
            &existing,
            "source name\tcharacteristics[organism]\tassay name\ttechnology type\tcomment[proteomics data acquisition method]\tcomment[label]\tcomment[instrument]\tcomment[cleavage agent details]\tcomment[fraction identifier]\tcomment[technical replicate]\tcomment[data file]\tcomment[carrier channel]\tcomment[reference channel]\ncell_A\tHomo sapiens\trun1\tproteomic profiling by mass spectrometry\tData-dependent acquisition\tTMTpro\tOrbitrap\tNT=Trypsin;AC=MS:1001251\t1\t1\tcell_A.raw\t\t\n",
        )
        .unwrap();
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: existing.display().to_string(),
            raw_files: vec![],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let proposal = SdrfProposal {
            relation_mode: "one_cell_per_data_file".into(),
            ..Default::default()
        };
        let (headers, rows, _) = merge_existing_sdrf(&proposal, &evidence).unwrap();
        let carrier = header_first_index(&headers, SC_CARRIER_CHANNEL).unwrap();
        let reference = header_first_index(&headers, SC_REFERENCE_CHANNEL).unwrap();
        assert_eq!(rows[0][carrier], "not available");
        assert_eq!(rows[0][reference], "not available");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn dataset_wide_individual_pipe_value_is_never_accepted_as_one_individual() {
        assert!(obviously_invalid_field_value(
            "individual",
            "animal hemisphere | uterine cervix"
        ));
        assert!(!obviously_invalid_field_value("individual", "donor_1"));
    }

    #[test]
    fn native_guard_detects_dataset_semantic_individual_concatenation_and_bulk_role_conflict() {
        let headers = vec![
            "source name".into(),
            "characteristics[organism]".into(),
            "characteristics[organism part]".into(),
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
            SC_CARRIER_CHANNEL.into(),
            SC_REFERENCE_CHANNEL.into(),
        ];
        let rows = vec![
            vec![
                "Xla_cell_1".into(),
                "Xenopus laevis".into(),
                "animal hemisphere".into(),
                "single cell".into(),
                "manual picking".into(),
                "cell_1".into(),
                "animal hemisphere | uterine cervix".into(),
                "batch1".into(),
                "1".into(),
                "run1".into(),
                "proteomic profiling by mass spectrometry".into(),
                "Data-dependent acquisition".into(),
                "label free sample".into(),
                "Orbitrap".into(),
                "Trypsin".into(),
                "1".into(),
                "1".into(),
                "run1.raw".into(),
                "not applicable".into(),
                "not applicable".into(),
            ],
            vec![
                "PC3_bulk_library".into(),
                "Homo sapiens".into(),
                "uterine cervix".into(),
                "single cell".into(),
                "not applicable".into(),
                "not applicable".into(),
                "animal hemisphere | uterine cervix".into(),
                "not available".into(),
                "not applicable".into(),
                "run2".into(),
                "proteomic profiling by mass spectrometry".into(),
                "Data-dependent acquisition".into(),
                "label free sample".into(),
                "Orbitrap".into(),
                "Trypsin".into(),
                "1".into(),
                "1".into(),
                "run2.raw".into(),
                "not applicable".into(),
                "not applicable".into(),
            ],
        ];
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![
                RawFile {
                    file_name: "run1.raw".into(),
                    file_uri: String::new(),
                    category: "RAW".into(),
                },
                RawFile {
                    file_name: "run2.raw".into(),
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
        let issues = validate_draft(&headers, &rows, &evidence);
        assert!(issues
            .iter()
            .any(|x| x.code == "individual_concatenates_nonindividual_dataset_semantics"));
        assert!(issues
            .iter()
            .any(|x| x.code == "single_cell_sample_type_conflicts_with_explicit_bulk_source_role"));
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
    fn generic_aspiration_does_not_create_capillary_microsampling_template_gap() {
        let item = EvidenceItem {
            id: "E0001".into(),
            source_kind: "manuscript_text".into(),
            source_label: "sample preparation".into(),
            text:
                "Cell lysates were aspirated into the pipette tip and transferred onto the filter."
                    .into(),
        };
        let design = StudyDesignScaffold {
            relation_mode_hint: "one_cell_per_data_file".into(),
            relation_confidence: "high".into(),
            repository_file_mode: "raw_files_present".into(),
            ..Default::default()
        };
        let scaffold = infer_deterministic_metadata_scaffold(&[item], &design);
        assert!(scaffold
            .template_gaps
            .iter()
            .all(|gap| gap.observed_value != "capillary microsampling"));
    }

    #[test]
    fn capillary_microsampling_reference_title_does_not_create_template_gap() {
        let item = EvidenceItem {
            id: "E0001".into(),
            source_kind: "manuscript_text".into(),
            source_label: "references".into(),
            text: "Lombard-Banek et al. Microsampling Capillary Electrophoresis Mass Spectrometry Enables Single-Cell Proteomics in Complex Tissues. Anal. Chem. 2019.".into(),
        };
        let design = StudyDesignScaffold {
            relation_mode_hint: "one_cell_per_data_file".into(),
            relation_confidence: "high".into(),
            repository_file_mode: "raw_files_present".into(),
            ..Default::default()
        };
        let scaffold = infer_deterministic_metadata_scaffold(&[item], &design);
        assert!(scaffold
            .template_gaps
            .iter()
            .all(|gap| gap.observed_value != "capillary microsampling"));
    }

    #[test]
    fn explicit_capillary_microsampling_still_creates_template_gap() {
        let item = EvidenceItem {
            id: "E0001".into(),
            source_kind: "manuscript_text".into(),
            source_label: "methods isolation".into(),
            text:
                "Single cells were collected by capillary microsampling before proteomic analysis."
                    .into(),
        };
        let design = StudyDesignScaffold {
            relation_mode_hint: "one_cell_per_data_file".into(),
            relation_confidence: "high".into(),
            repository_file_mode: "raw_files_present".into(),
            ..Default::default()
        };
        let scaffold = infer_deterministic_metadata_scaffold(&[item], &design);
        assert!(scaffold
            .template_gaps
            .iter()
            .any(|gap| gap.observed_value == "capillary microsampling"));
    }

    #[test]
    fn eyebrow_hair_microdissection_maps_to_manual_picking() {
        let item = EvidenceItem {
            id: "E0001".into(),
            source_kind: "manuscript_text".into(),
            source_label: "methods isolation".into(),
            text: "Individual cells were cut and isolated from whole embryos using an eyebrow-hair knife and loop.".into(),
        };
        let design = StudyDesignScaffold {
            relation_mode_hint: "one_cell_per_data_file".into(),
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
    fn hydrodynamic_single_cell_loading_maps_to_manual_picking() {
        let item = EvidenceItem {
            id: "E0001".into(),
            source_kind: "manuscript_text".into(),
            source_label: "methods isolation".into(),
            text: "Single cells were loaded onto the capillary manually using low flow generated by hydrodynamic pressure.".into(),
        };
        let design = StudyDesignScaffold {
            relation_mode_hint: "one_cell_per_data_file".into(),
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
    fn explicit_manual_isolation_beats_generic_facs_metadata() {
        let evidence = vec![
            EvidenceItem {
                id: "E0001".into(),
                source_kind: "pride_project".into(),
                source_label: "project.description".into(),
                text: "The project includes FACS sorted control cells.".into(),
            },
            EvidenceItem {
                id: "E0002".into(),
                source_kind: "manuscript_semantic_evidence".into(),
                source_label: "single_cell_isolation_method".into(),
                text: "Single cells were loaded manually using hydrodynamic pressure under a microscope.".into(),
            },
        ];
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
        assert_eq!(
            scaffold.evidence_refs.get("single_cell_isolation_method"),
            Some(&vec!["E0002".to_string()])
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
        let issues =
            validate_incomplete_mapping_scaffold(&headers, &rows, &evidence, "uncertain", false);
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
        let issues =
            validate_incomplete_mapping_scaffold(&headers, &rows, &evidence, "uncertain", false);
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
            cell_type: "HeLa cell".into(),
            individual: "donor_1".into(),
            single_cell_isolation_method: "cellenONE".into(),
            ..Default::default()
        };
        let (headers, rows, mode) = draft_rows(&proposal, &evidence).unwrap();
        assert_eq!(mode, "generated_file_role_aware_one_row_per_raw_file");
        let idx = |name: &str| header_first_index(&headers, name).unwrap();
        assert_eq!(rows[0][idx(SC_SAMPLE_TYPE)], "single cell");
        assert_eq!(rows[0][idx(SC_CELLS_PER_WELL)], "1");
        assert_eq!(rows[1][idx(SC_SAMPLE_TYPE)], "not available");
        assert_eq!(rows[1][idx(SC_CELLS_PER_WELL)], "40");
        assert_eq!(rows[1][idx(SC_CELL_IDENTIFIER)], "not applicable");
        assert_eq!(rows[2][idx(SC_SAMPLE_TYPE)], "empty");
        assert_eq!(rows[2][idx(SC_ISOLATION_METHOD)], "not applicable");
        assert_eq!(rows[2][idx("characteristics[cell type]")], "not applicable");
        assert_eq!(rows[2][idx(SC_INDIVIDUAL)], "not applicable");
        assert_eq!(rows[3][idx(SC_SAMPLE_TYPE)], "bulk control");
        assert_eq!(rows[3][idx(SC_ISOLATION_METHOD)], "not applicable");
    }

    #[test]
    fn scientific_guard_rejects_cross_field_individual_and_acquisition_contradiction() {
        let headers = vec![
            "source name".into(),
            "characteristics[organism]".into(),
            "characteristics[organism part]".into(),
            "characteristics[cell type]".into(),
            "characteristics[cell line]".into(),
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
            SC_CARRIER_CHANNEL.into(),
            SC_REFERENCE_CHANNEL.into(),
        ];
        let rows = vec![vec![
            "cell1".into(),
            "Homo sapiens".into(),
            "Embryo".into(),
            "early embryonic cell".into(),
            "HeLa".into(),
            "single cell".into(),
            "manual picking".into(),
            "cell1".into(),
            "Embryo".into(),
            "not available".into(),
            "1".into(),
            "assay1".into(),
            "proteomic profiling by mass spectrometry".into(),
            "NT=Data-independent acquisition;AC=PRIDE:0000450".into(),
            "label free sample".into(),
            "Orbitrap".into(),
            "NT=Trypsin;AC=MS:1001251".into(),
            "1".into(),
            "1".into(),
            "run_DDAtop20.raw".into(),
            "not applicable".into(),
            "not applicable".into(),
        ]];
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![RawFile {
                file_name: "run_DDAtop20.raw".into(),
                file_uri: String::new(),
                category: "RAW".into(),
            }],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let issues = validate_draft(&headers, &rows, &evidence);
        assert!(issues
            .iter()
            .any(|x| x.code == "individual_duplicates_nonindividual_semantic_field"));
        assert!(issues
            .iter()
            .any(|x| x.code == "data_file_name_acquisition_contradiction"));
    }

    #[test]
    fn scientific_guard_rejects_empty_control_leakage_wwa_dia_lcm_scope_and_qe_cid() {
        let headers = vec![
            "source name".into(),
            "characteristics[organism]".into(),
            "characteristics[organism part]".into(),
            "characteristics[disease]".into(),
            "characteristics[developmental stage]".into(),
            "characteristics[sex]".into(),
            "characteristics[cell line]".into(),
            "characteristics[cellosaurus accession]".into(),
            "characteristics[material type]".into(),
            SC_SAMPLE_TYPE.into(),
            SC_CELLS_PER_WELL.into(),
            SC_CELL_IDENTIFIER.into(),
            SC_ISOLATION_METHOD.into(),
            SC_INDIVIDUAL.into(),
            SC_PREP_BATCH.into(),
            "assay name".into(),
            "technology type".into(),
            "comment[proteomics data acquisition method]".into(),
            "comment[label]".into(),
            "comment[instrument]".into(),
            "comment[dissociation method]".into(),
            "comment[lcm microscope model]".into(),
            "comment[cleavage agent details]".into(),
            "comment[fraction identifier]".into(),
            "comment[technical replicate]".into(),
            "comment[data file]".into(),
            SC_CARRIER_CHANNEL.into(),
            SC_REFERENCE_CHANNEL.into(),
        ];
        let rows = vec![vec![
            "blank".into(),
            "Homo sapiens".into(),
            "brain".into(),
            "glioblastoma".into(),
            "adult".into(),
            "male".into(),
            "HeLa".into(),
            "CVCL_0001".into(),
            "cell line".into(),
            "empty".into(),
            "0".into(),
            "blank_1".into(),
            "manual aspiration".into(),
            "not applicable".into(),
            "not available".into(),
            "assay1".into(),
            "proteomic profiling by mass spectrometry".into(),
            "NT=Data-independent acquisition;AC=PRIDE:0000450".into(),
            "label free sample".into(),
            "NT=Q Exactive Plus;AC=MS:1002634".into(),
            "NT=CID;AC=MS:1000133".into(),
            "Zeiss PALM MicroBeam".into(),
            "NT=Trypsin;AC=MS:1001251".into(),
            "1".into(),
            "1".into(),
            "run_WWA4.raw".into(),
            "not applicable".into(),
            "not applicable".into(),
        ]];
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![RawFile {
                file_name: "run_WWA4.raw".into(),
                file_uri: String::new(),
                category: "RAW".into(),
            }],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let issues = validate_draft(&headers, &rows, &evidence);
        assert!(issues
            .iter()
            .any(|x| x.code == "empty_control_has_concrete_biological_identity"));
        assert!(issues
            .iter()
            .any(|x| x.code == "data_file_name_acquisition_contradiction"));
        assert!(issues
            .iter()
            .any(|x| x.code == "lcm_microscope_model_without_lcm_isolation"));
        assert!(issues
            .iter()
            .any(|x| x.code == "q_exactive_row_uses_generic_cid_instead_of_hcd"));
    }

    #[test]
    fn scientific_guard_rejects_multiorganism_project_collapse() {
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
            SC_ISOLATION_METHOD.into(),
            SC_CELL_IDENTIFIER.into(),
            SC_PREP_BATCH.into(),
            SC_CARRIER_CHANNEL.into(),
            SC_REFERENCE_CHANNEL.into(),
        ];
        let rows = vec![vec![
            "cell1".into(),
            "Homo sapiens".into(),
            "assay1".into(),
            "proteomic profiling by mass spectrometry".into(),
            "Data-dependent acquisition".into(),
            "label free sample".into(),
            "Orbitrap".into(),
            "NT=Trypsin;AC=MS:1001251".into(),
            "1".into(),
            "1".into(),
            "cell1.raw".into(),
            "manual picking".into(),
            "cell1".into(),
            "not available".into(),
            "not applicable".into(),
            "not applicable".into(),
        ]];
        let evidence = DatasetEvidence {
            accession: "PXD999999".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![RawFile {
                file_name: "cell1.raw".into(),
                file_uri: String::new(),
                category: "RAW".into(),
            }],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![
                EvidenceItem {
                    id: "E0001".into(),
                    source_kind: "pride_project".into(),
                    source_label: "project:organisms[0].name".into(),
                    text: "Homo sapiens".into(),
                },
                EvidenceItem {
                    id: "E0002".into(),
                    source_kind: "pride_project".into(),
                    source_label: "project:organisms[1].name".into(),
                    text: "Xenopus laevis".into(),
                },
            ],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let issues = validate_draft(&headers, &rows, &evidence);
        assert!(issues
            .iter()
            .any(|x| x.code == "multiorganism_project_collapsed_to_single_candidate_organism"));
    }

    #[test]
    fn publication_compatibility_normalizes_dia_and_study_sample() {
        assert_eq!(
            validator_compatible_acquisition_method(
                "NT=Data-independent acquisition;AC=PRIDE:0000450"
            ),
            "Data-independent acquisition"
        );
        assert_eq!(
            validator_compatible_acquisition_method("NT=diaPASEF;AC=PRIDE:0000650"),
            "Data-independent acquisition"
        );
        assert_eq!(
            validator_compatible_acquisition_method("Data-dependent acquisition"),
            "Data-dependent acquisition"
        );
        let mut proposal = SdrfProposal::default();
        proposal.proteomics_data_acquisition_method =
            "NT=Data-independent acquisition;AC=PRIDE:0000450".into();
        proposal.sample_type = "study sample".into();
        let issues = apply_publication_compatibility_normalization(&mut proposal);
        assert_eq!(
            proposal.proteomics_data_acquisition_method,
            "Data-independent acquisition"
        );
        assert_eq!(proposal.sample_type, "not available");
        assert_eq!(issues.len(), 2);
    }

    #[test]
    fn design_assessment_schema_exposes_v04_contract() {
        let schema = design_assessment_schema();
        let text = schema.to_string();
        assert!(text.contains("relation_assessment"));
        assert!(text.contains("terminal_status"));
        assert!(text.contains("raw_exact"));
        assert!(text.contains("terms_all"));
        assert!(text.contains("SEARCH_STRUCTURED_DESIGN"));
        assert!(text.contains("SEARCH_EXACT_RAW_NAME"));
        assert!(text.contains("LOOKUP_KG_TERM"));
        assert!(text.contains("ABSTAIN"));
    }

    #[test]
    fn exact_raw_action_does_not_invent_filename_linkage() {
        let mut evidence = DatasetEvidence {
            accession: "PXD000001".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![RawFile {
                file_name: "sample_THX_1.raw".into(),
                file_uri: String::new(),
                category: String::new(),
            }],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_text".into(),
                source_label: "paper".into(),
                text: "THX cells were analyzed, but no raw filenames are reported.".into(),
            }],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let actions = vec![EvidenceActionRequest {
            action: "SEARCH_EXACT_RAW_NAME".into(),
            reason: "need row linkage".into(),
            target_fields: vec!["cell_type".into()],
            queries: vec![EvidenceQuery {
                match_kind: "raw_exact".into(),
                value: "sample_THX_1.raw".into(),
                terms: vec![],
                document_hint: String::new(),
            }],
        }];
        let mut attempted = BTreeSet::new();
        let results = execute_evidence_actions(&mut evidence, &actions, 1, &mut attempted);
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].outcome, "no_match");
        assert!(results[0].matched_evidence_refs.is_empty());
    }

    #[test]
    fn exact_raw_action_rejects_semantic_query() {
        let mut evidence = DatasetEvidence {
            accession: "PXD000001".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![RawFile {
                file_name: "sample_1.raw".into(),
                file_uri: String::new(),
                category: String::new(),
            }],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let actions = vec![EvidenceActionRequest {
            action: "SEARCH_EXACT_RAW_NAME".into(),
            reason: "semantic query is invalid here".into(),
            target_fields: vec!["cell_type".into()],
            queries: vec![EvidenceQuery {
                match_kind: "phrase".into(),
                value: "K562".into(),
                terms: vec![],
                document_hint: String::new(),
            }],
        }];
        let mut attempted = BTreeSet::new();
        let results = execute_evidence_actions(&mut evidence, &actions, 1, &mut attempted);
        assert_eq!(results[0].outcome, "invalid_query");
    }

    #[test]
    fn typed_publication_terms_match_atomic_evidence() {
        let mut evidence = DatasetEvidence {
            accession: "PXD000001".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_text".into(),
                source_label: "doi:10.example/test".into(),
                text: "K562 sample material was analyzed in the experiment.".into(),
            }],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let actions = vec![EvidenceActionRequest {
            action: "SEARCH_PUBLICATION".into(),
            reason: "find cell-line evidence".into(),
            target_fields: vec!["cell_type".into()],
            queries: vec![EvidenceQuery {
                match_kind: "terms_all".into(),
                value: String::new(),
                terms: vec!["K562".into(), "sample".into()],
                document_hint: "10.example/test".into(),
            }],
        }];
        let mut attempted = BTreeSet::new();
        let results = execute_evidence_actions(&mut evidence, &actions, 1, &mut attempted);
        assert_eq!(results[0].outcome, "matched");
        assert_eq!(results[0].matched_evidence_refs, vec!["E0001"]);
    }

    #[test]
    fn repeated_failed_evidence_action_is_blocked() {
        let mut evidence = DatasetEvidence {
            accession: "PXD000001".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![RawFile {
                file_name: "a.raw".into(),
                file_uri: String::new(),
                category: String::new(),
            }],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let actions = vec![EvidenceActionRequest {
            action: "SEARCH_EXACT_RAW_NAME".into(),
            reason: "need linkage".into(),
            target_fields: vec!["cell_type".into()],
            queries: vec![EvidenceQuery {
                match_kind: "raw_exact".into(),
                value: "a.raw".into(),
                terms: vec![],
                document_hint: String::new(),
            }],
        }];
        let mut attempted = BTreeSet::new();
        let first = execute_evidence_actions(&mut evidence, &actions, 1, &mut attempted);
        let second = execute_evidence_actions(&mut evidence, &actions, 2, &mut attempted);
        assert_eq!(first[0].outcome, "no_match");
        assert_eq!(second[0].outcome, "duplicate_skipped");
    }

    #[test]
    fn filename_only_group_is_normalized_to_search_hint() {
        let evidence = DatasetEvidence {
            accession: "PXD000001".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![RawFile {
                file_name: "32cell_hint.raw".into(),
                file_uri: String::new(),
                category: String::new(),
            }],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let mut assessment = DatasetDesignAssessment {
            candidate_groups: vec![CandidateDesignGroup {
                id: "g1".into(),
                description: "filename-derived group".into(),
                status: "supported".into(),
                source_basis: "source_evidence".into(),
                confidence: "high".into(),
                evidence_refs: vec![],
                linked_raw_files: vec!["32cell_hint.raw".into()],
                linkage_status: "supported".into(),
            }],
            ..DatasetDesignAssessment::default()
        };
        normalize_design_assessment(&evidence, &mut assessment);
        let group = &assessment.candidate_groups[0];
        assert_eq!(group.status, "search_hint");
        assert_eq!(group.source_basis, "filename_hint");
        assert_eq!(group.linkage_status, "unresolved");
        assert!(group.linked_raw_files.is_empty());
    }

    #[test]
    fn omitted_scope_defers_to_deterministic_project_scaffold() {
        let mut proposal = SdrfProposal::default();
        let mut scaffold = DeterministicMetadataScaffold::default();
        scaffold
            .values
            .insert("instrument".into(), "Orbitrap Fusion Lumos".into());
        scaffold
            .evidence_refs
            .insert("instrument".into(), vec!["E0001".into()]);
        let evidence = DatasetEvidence {
            accession: "PXD000001".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: scaffold,
            evidence: vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "pride_project".into(),
                source_label: "project".into(),
                text: "Orbitrap Fusion Lumos".into(),
            }],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let assessment = DatasetDesignAssessment {
            field_scopes: vec![FieldScopeClaim {
                field: "instrument".into(),
                scope: "unresolved".into(),
                evidence_refs: vec![],
                confidence: "low".into(),
                reason:
                    "field scope omitted by planner; deterministic evidence may still resolve it"
                        .into(),
                claim_origin: "default_missing".into(),
            }],
            ..DatasetDesignAssessment::default()
        };
        let issues =
            apply_deterministic_metadata_scaffold(&mut proposal, &evidence, Some(&assessment));
        assert_eq!(proposal.instrument, "Orbitrap Fusion Lumos");
        assert!(issues
            .iter()
            .any(|x| x.code == "agent_scope_deferred_to_deterministic_scaffold"));
    }

    #[test]
    fn explicit_group_scope_blocks_deterministic_project_scaffold_broadcast() {
        let mut proposal = SdrfProposal::default();
        let mut scaffold = DeterministicMetadataScaffold::default();
        scaffold
            .values
            .insert("organism".into(), "Homo sapiens (human)".into());
        scaffold
            .evidence_refs
            .insert("organism".into(), vec!["E0001".into()]);
        let evidence = DatasetEvidence {
            accession: "PXD000001".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: scaffold,
            evidence: vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "pride_project".into(),
                source_label: "project".into(),
                text: "multiple biological branches".into(),
            }],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let assessment = DatasetDesignAssessment {
            field_scopes: vec![FieldScopeClaim {
                field: "organism".into(),
                scope: "group".into(),
                evidence_refs: vec!["E0001".into()],
                confidence: "high".into(),
                reason: "organism varies by group".into(),
                claim_origin: "model_explicit".into(),
            }],
            ..DatasetDesignAssessment::default()
        };
        let issues =
            apply_deterministic_metadata_scaffold(&mut proposal, &evidence, Some(&assessment));
        assert!(proposal.organism.is_empty());
        assert!(issues
            .iter()
            .any(|x| x.code == "agent_blocks_project_scaffold_broadcast"));
    }

    #[test]
    fn design_guard_prevents_explicit_nonproject_dataset_broadcast() {
        let mut proposal = SdrfProposal {
            relation_mode: "one_cell_per_data_file".into(),
            cell_type: "HeLa cell".into(),
            disease: "monocytic leukemia".into(),
            ..SdrfProposal::default()
        };
        let assessment = DatasetDesignAssessment {
            agent_version: DESIGN_AGENT_VERSION.into(),
            design_homogeneous: false,
            relation_assessment: RelationAssessment {
                mode: "mixed".into(),
                scope: "branch".into(),
                evidence_refs: vec!["E0001".into()],
                confidence: "high".into(),
                reason: "multiple relation regimes".into(),
                claim_origin: "model_explicit".into(),
            },
            field_scopes: vec![
                FieldScopeClaim {
                    field: "cell_type".into(),
                    scope: "group".into(),
                    confidence: "high".into(),
                    reason: "multiple cell lines".into(),
                    claim_origin: "model_explicit".into(),
                    ..FieldScopeClaim::default()
                },
                FieldScopeClaim {
                    field: "disease".into(),
                    scope: "unresolved".into(),
                    confidence: "low".into(),
                    reason: "row mapping missing".into(),
                    claim_origin: "model_explicit".into(),
                    ..FieldScopeClaim::default()
                },
            ],
            missing_linkages: vec!["RAW-to-cell-line mapping unavailable".into()],
            ..DatasetDesignAssessment::default()
        };
        let issues =
            apply_design_assessment_guard(&mut proposal, &assessment, "one_cell_per_data_file");
        assert_eq!(proposal.relation_mode, "uncertain");
        assert_eq!(proposal.cell_type, "not available");
        assert_eq!(proposal.disease, "not available");
        assert!(issues
            .iter()
            .any(|x| x.code == "agent_relation_conflict_not_broadcast"));
        assert!(issues
            .iter()
            .any(|x| x.code == "agent_nonproject_value_not_broadcast"));
        assert!(issues.iter().any(|x| x.code == "agent_missing_row_linkage"));
    }

    #[test]
    fn default_missing_relation_defers_instead_of_blocking() {
        let assessment = DatasetDesignAssessment {
            relation_assessment: RelationAssessment {
                mode: "unresolved".into(),
                scope: "unresolved".into(),
                evidence_refs: vec![],
                confidence: "low".into(),
                reason: "missing planner relation".into(),
                claim_origin: "default_missing".into(),
            },
            ..DatasetDesignAssessment::default()
        };
        assert_eq!(
            relation_scaffold_arbitration(Some(&assessment), "one_cell_per_data_file"),
            RelationArbitration::Defer
        );
    }

    #[test]
    fn branch_relation_matching_deterministic_hint_confirms_scaffold() {
        let assessment = DatasetDesignAssessment {
            relation_assessment: RelationAssessment {
                mode: "one_cell_per_data_file".into(),
                scope: "branch".into(),
                evidence_refs: vec!["E0001".into()],
                confidence: "high".into(),
                reason: "one acquisition unit per file".into(),
                claim_origin: "model_explicit".into(),
            },
            ..DatasetDesignAssessment::default()
        };
        assert_eq!(
            relation_scaffold_arbitration(Some(&assessment), "one_cell_per_data_file"),
            RelationArbitration::AllowConfirm
        );
    }

    #[test]
    fn design_guard_keeps_branch_relation_when_deterministic_hint_agrees() {
        let mut proposal = SdrfProposal {
            relation_mode: "one_cell_per_data_file".into(),
            ..SdrfProposal::default()
        };
        let assessment = DatasetDesignAssessment {
            relation_assessment: RelationAssessment {
                mode: "one_cell_per_data_file".into(),
                scope: "branch".into(),
                evidence_refs: vec!["E0001".into()],
                confidence: "high".into(),
                reason: "one acquisition unit per file".into(),
                claim_origin: "model_explicit".into(),
            },
            ..DatasetDesignAssessment::default()
        };
        let issues =
            apply_design_assessment_guard(&mut proposal, &assessment, "one_cell_per_data_file");
        assert_eq!(proposal.relation_mode, "one_cell_per_data_file");
        assert!(!issues
            .iter()
            .any(|x| x.code == "agent_relation_conflict_not_broadcast"));
    }

    #[test]
    fn relation_conflict_blocks_deterministic_scaffold() {
        let assessment = DatasetDesignAssessment {
            relation_assessment: RelationAssessment {
                mode: "multiplexed_cells_per_data_file".into(),
                scope: "branch".into(),
                evidence_refs: vec!["E0001".into()],
                confidence: "high".into(),
                reason: "reporter multiplexing".into(),
                claim_origin: "model_explicit".into(),
            },
            ..DatasetDesignAssessment::default()
        };
        assert_eq!(
            relation_scaffold_arbitration(Some(&assessment), "one_cell_per_data_file"),
            RelationArbitration::BlockConflict
        );
    }

    #[test]
    fn mixed_relation_blocks_project_relation_scaffold() {
        let assessment = DatasetDesignAssessment {
            relation_assessment: RelationAssessment {
                mode: "mixed".into(),
                scope: "branch".into(),
                evidence_refs: vec!["E0001".into()],
                confidence: "high".into(),
                reason: "different acquisition cardinalities".into(),
                claim_origin: "model_explicit".into(),
            },
            ..DatasetDesignAssessment::default()
        };
        assert_eq!(
            relation_scaffold_arbitration(Some(&assessment), "one_cell_per_data_file"),
            RelationArbitration::BlockProjectRelation
        );
    }

    #[test]
    fn branch_relation_does_not_authorize_global_row_serialization() {
        let assessment = DatasetDesignAssessment {
            relation_assessment: RelationAssessment {
                mode: "one_cell_per_data_file".into(),
                scope: "branch".into(),
                evidence_refs: vec!["E0001".into()],
                confidence: "high".into(),
                reason: "branch cardinality is resolved".into(),
                claim_origin: "model_explicit".into(),
            },
            ..DatasetDesignAssessment::default()
        };
        assert!(!relation_allows_global_row_serialization(
            Some(&assessment),
            "one_cell_per_data_file"
        ));
    }

    #[test]
    fn project_relation_authorizes_global_row_serialization() {
        let assessment = DatasetDesignAssessment {
            relation_assessment: RelationAssessment {
                mode: "one_cell_per_data_file".into(),
                scope: "project".into(),
                evidence_refs: vec!["E0001".into()],
                confidence: "high".into(),
                reason: "same acquisition cardinality applies to every file".into(),
                claim_origin: "model_explicit".into(),
            },
            ..DatasetDesignAssessment::default()
        };
        assert!(relation_allows_global_row_serialization(
            Some(&assessment),
            "one_cell_per_data_file"
        ));
    }

    #[test]
    fn raw_exact_repository_request_is_normalized_to_exact_raw_action() {
        let evidence = DatasetEvidence {
            accession: "PXD000001".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![RawFile {
                file_name: "sample_01.raw".into(),
                file_uri: String::new(),
                category: "RAW".into(),
            }],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let query = EvidenceQuery {
            match_kind: "raw_exact".into(),
            value: "sample_01.raw".into(),
            terms: vec![],
            document_hint: String::new(),
        };
        let (action, from) =
            canonicalize_action_for_query(&evidence, "SEARCH_REPOSITORY_METADATA", &query);
        assert_eq!(action, "SEARCH_EXACT_RAW_NAME");
        assert_eq!(from, "SEARCH_REPOSITORY_METADATA");
        assert!(validate_evidence_query_for_action(&evidence, &action, &query).is_ok());
    }

    #[test]
    fn normalized_raw_exact_request_executes_under_exact_raw_action() {
        let mut evidence = DatasetEvidence {
            accession: "PXD000001".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![RawFile {
                file_name: "sample_01.raw".into(),
                file_uri: String::new(),
                category: "RAW".into(),
            }],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "pride_files".into(),
                source_label: "repository:file".into(),
                text: "sample_01.raw".into(),
            }],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let actions = vec![EvidenceActionRequest {
            action: "SEARCH_REPOSITORY_METADATA".into(),
            reason: "verify the exact repository acquisition".into(),
            target_fields: vec!["sample_type".into()],
            queries: vec![EvidenceQuery {
                match_kind: "raw_exact".into(),
                value: "sample_01.raw".into(),
                terms: vec![],
                document_hint: String::new(),
            }],
        }];
        let mut attempted = BTreeSet::new();
        let results = execute_evidence_actions(&mut evidence, &actions, 1, &mut attempted);
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].action, "SEARCH_EXACT_RAW_NAME");
        assert_eq!(
            results[0].normalized_from_action,
            "SEARCH_REPOSITORY_METADATA"
        );
        assert!(results[0].summary.contains("action_normalized"));
        assert_ne!(results[0].outcome, "invalid_query");
    }

    #[test]
    fn branch_relation_is_resolved_without_claiming_row_mapping_resolved() {
        let issues = vec![ValidationIssue {
            level: "error".into(),
            code: "sample_to_file_branch_mapping_unresolved".into(),
            row: 0,
            column: "characteristics[cell identifier]".into(),
            message: "branch mapping unresolved".into(),
        }];
        let (mapping, _, metadata, _, _) = derive_readiness_dimensions(
            false,
            0,
            10,
            "one_cell_per_data_file",
            false,
            "direct_acquisitions",
            false,
            &issues,
            None,
        );
        assert_eq!(mapping, "relation_resolved_branch_mapping_unresolved");
        assert_eq!(metadata, "validator_complete_except_mapping");
    }

    #[test]
    fn readiness_dimensions_keep_template_and_mapping_separate() {
        let issues = vec![ValidationIssue {
            level: "error".into(),
            code: "sample_to_file_relation_unresolved".into(),
            row: 0,
            column: "characteristics[cell identifier]".into(),
            message: "mapping unresolved".into(),
        }];
        let trace = DesignAgentTrace {
            final_assessment: DatasetDesignAssessment {
                terminal_status: "evidence_exhausted".into(),
                ..DatasetDesignAssessment::default()
            },
            ..DesignAgentTrace::default()
        };
        let (mapping, template, metadata, evidence, terminal) = derive_readiness_dimensions(
            false,
            0,
            10,
            "uncertain",
            false,
            "raw_acquisitions",
            true,
            &issues,
            Some(&trace),
        );
        assert_eq!(mapping, "unresolved");
        assert_eq!(template, "template_vocabulary_gap");
        assert_eq!(metadata, "validator_complete_except_mapping");
        assert_eq!(evidence, "evidence_exhausted");
        assert_eq!(terminal, "evidence_exhausted");
    }

    #[test]
    fn validator_repair_clusters_row_storms_by_field() {
        let mut issues = Vec::new();
        for row in 1..=7 {
            issues.push(ValidationIssue {
                level: "error".into(),
                code: "single_cell_isolation_unresolved".into(),
                row,
                column: SC_ISOLATION_METHOD.into(),
                message: "isolation unresolved".into(),
            });
        }
        for row in 1..=3 {
            issues.push(ValidationIssue {
                level: "error".into(),
                code: "individual_duplicates_nonindividual_semantic_field".into(),
                row,
                column: SC_INDIVIDUAL.into(),
                message: "individual duplicates organism part".into(),
            });
        }
        let tasks = cluster_validator_repair_tasks(&issues);
        assert_eq!(tasks.len(), 2);
        assert_eq!(tasks[0].field, "single_cell_isolation_method");
        assert_eq!(tasks[0].error_count, 7);
        assert!(tasks[0].representative_rows.len() <= 6);
        assert_eq!(tasks[1].field, "individual");
        assert_eq!(tasks[1].error_count, 3);
    }

    #[test]
    fn validator_repair_ignores_nonrepairable_validation_errors() {
        let issues = vec![ValidationIssue {
            level: "error".into(),
            code: "required_integer_invalid".into(),
            row: 1,
            column: "comment[technical replicate]".into(),
            message: "not an integer".into(),
        }];
        assert!(cluster_validator_repair_tasks(&issues).is_empty());
    }

    #[test]
    fn validator_repair_individual_semantic_sanitizer_removes_embryo_alias() {
        let mut proposal = SdrfProposal {
            organism_part: "Embryo".into(),
            individual: "Embryo".into(),
            ..SdrfProposal::default()
        };
        proposal
            .evidence_refs
            .insert("individual".into(), vec!["E0001".into()]);
        let issue = sanitize_nonindividual_semantic_proposal(&mut proposal)
            .expect("embryo alias should be sanitized");
        assert_eq!(proposal.individual, "not available");
        assert!(!proposal.evidence_refs.contains_key("individual"));
        assert_eq!(issue.code, "validator_repair_individual_semantic_sanitized");
    }

    #[test]
    fn validator_repair_individual_semantic_sanitizer_preserves_donor_identifier() {
        let mut proposal = SdrfProposal {
            organism_part: "Brain".into(),
            individual: "animal_01".into(),
            ..SdrfProposal::default()
        };
        assert!(sanitize_nonindividual_semantic_proposal(&mut proposal).is_none());
        assert_eq!(proposal.individual, "animal_01");
    }

    #[test]
    fn validator_repair_cannot_project_assign_multiorganism_mapping() {
        let evidence = DatasetEvidence {
            accession: "PXD000001".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "pride_project".into(),
                source_label: "project:organisms[0].name".into(),
                text: "Homo sapiens".into(),
            }],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let tasks = vec![ValidatorRepairTask {
            field: "organism".into(),
            error_codes: vec![
                "multiorganism_project_collapsed_to_single_candidate_organism".into(),
            ],
            error_count: 1,
            representative_rows: vec![0],
            representative_messages: vec!["multiple organisms".into()],
        }];
        let assessment = ValidatorRepairAssessment {
            repair_agent_version: VALIDATOR_REPAIR_AGENT_VERSION.into(),
            decisions: vec![ValidatorRepairDecision {
                field: "organism".into(),
                resolution: "set_project_value".into(),
                scope: "project".into(),
                value: "Homo sapiens".into(),
                evidence_refs: vec!["E0001".into()],
                confidence: "high".into(),
                reason: "one source mentions human".into(),
            }],
            terminal_status: "resolved".into(),
            ..ValidatorRepairAssessment::default()
        };
        let mut proposal = SdrfProposal::default();
        let (issues, applied) =
            apply_validator_repair_decisions(&mut proposal, &evidence, None, &tasks, &assessment);
        assert!(applied.is_empty());
        assert!(
            issues
                .iter()
                .any(|issue| issue.code
                    == "validator_repair_project_value_rejected_for_mapping_field")
        );
        assert!(proposal.organism.is_empty());
    }

    #[test]
    fn validator_repair_applies_project_isolation_with_relevant_evidence() {
        let evidence = DatasetEvidence {
            accession: "PXD000001".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "publication".into(),
                source_label: "methods:isolation".into(),
                text: "Individual cells were isolated by manual picking with a micropipette."
                    .into(),
            }],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let tasks = vec![ValidatorRepairTask {
            field: "single_cell_isolation_method".into(),
            error_codes: vec!["single_cell_isolation_unresolved".into()],
            error_count: 12,
            representative_rows: vec![1, 2, 3],
            representative_messages: vec!["isolation unresolved".into()],
        }];
        let assessment = ValidatorRepairAssessment {
            repair_agent_version: VALIDATOR_REPAIR_AGENT_VERSION.into(),
            decisions: vec![ValidatorRepairDecision {
                field: "single_cell_isolation_method".into(),
                resolution: "set_project_value".into(),
                scope: "project".into(),
                value: "manual picking".into(),
                evidence_refs: vec!["E0001".into()],
                confidence: "high".into(),
                reason: "methods explicitly state manual picking".into(),
            }],
            terminal_status: "resolved".into(),
            ..ValidatorRepairAssessment::default()
        };
        let mut proposal = SdrfProposal {
            single_cell_isolation_method: "not available".into(),
            ..SdrfProposal::default()
        };
        let (issues, applied) =
            apply_validator_repair_decisions(&mut proposal, &evidence, None, &tasks, &assessment);
        assert_eq!(applied, vec!["single_cell_isolation_method"]);
        assert_eq!(proposal.single_cell_isolation_method, "manual picking");
        assert_eq!(
            proposal.evidence_refs.get("single_cell_isolation_method"),
            Some(&vec!["E0001".into()])
        );
        assert!(issues
            .iter()
            .any(|issue| issue.code == "validator_repair_project_value_applied"));
    }

    #[test]
    fn validator_repair_respects_explicit_group_scope() {
        let evidence = DatasetEvidence {
            accession: "PXD000001".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "publication".into(),
                source_label: "methods:isolation".into(),
                text: "A subset of cells was isolated by manual picking.".into(),
            }],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let design = DatasetDesignAssessment {
            field_scopes: vec![FieldScopeClaim {
                field: "single_cell_isolation_method".into(),
                scope: "group".into(),
                evidence_refs: vec!["E0001".into()],
                confidence: "high".into(),
                reason: "only one branch uses manual picking".into(),
                claim_origin: "model_explicit".into(),
            }],
            ..DatasetDesignAssessment::default()
        };
        let tasks = vec![ValidatorRepairTask {
            field: "single_cell_isolation_method".into(),
            error_codes: vec!["single_cell_isolation_unresolved".into()],
            error_count: 5,
            representative_rows: vec![1],
            representative_messages: vec!["isolation unresolved".into()],
        }];
        let assessment = ValidatorRepairAssessment {
            repair_agent_version: VALIDATOR_REPAIR_AGENT_VERSION.into(),
            decisions: vec![ValidatorRepairDecision {
                field: "single_cell_isolation_method".into(),
                resolution: "set_project_value".into(),
                scope: "project".into(),
                value: "manual picking".into(),
                evidence_refs: vec!["E0001".into()],
                confidence: "high".into(),
                reason: "attempted project broadcast".into(),
            }],
            terminal_status: "resolved".into(),
            ..ValidatorRepairAssessment::default()
        };
        let mut proposal = SdrfProposal {
            single_cell_isolation_method: "not available".into(),
            ..SdrfProposal::default()
        };
        let (issues, applied) = apply_validator_repair_decisions(
            &mut proposal,
            &evidence,
            Some(&design),
            &tasks,
            &assessment,
        );
        assert!(applied.is_empty());
        assert_eq!(proposal.single_cell_isolation_method, "not available");
        assert!(issues.iter().any(|issue| {
            issue.code == "validator_repair_project_value_rejected_by_provenance_guard"
        }));
    }

    #[test]
    fn validator_repair_normalization_enforces_bounded_action_budget() {
        let evidence = DatasetEvidence {
            accession: "PXD000001".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let tasks = vec![ValidatorRepairTask {
            field: "single_cell_isolation_method".into(),
            error_codes: vec!["single_cell_isolation_unresolved".into()],
            error_count: 1,
            representative_rows: vec![1],
            representative_messages: vec!["isolation unresolved".into()],
        }];
        let query = EvidenceQuery {
            match_kind: "terms_any".into(),
            value: String::new(),
            terms: vec!["isolation".into()],
            document_hint: String::new(),
        };
        let action = EvidenceActionRequest {
            action: "SEARCH_PUBLICATION".into(),
            reason: "find isolation evidence".into(),
            target_fields: vec!["single_cell_isolation_method".into()],
            queries: vec![query; 8],
        };
        let mut assessment = ValidatorRepairAssessment {
            next_evidence_actions: vec![action; 8],
            terminal_status: "continue".into(),
            ..ValidatorRepairAssessment::default()
        };
        normalize_validator_repair_assessment(&evidence, &tasks, "plan", &mut assessment);
        assert_eq!(
            assessment.next_evidence_actions.len(),
            VALIDATOR_REPAIR_MAX_TASKS
        );
        assert!(assessment
            .next_evidence_actions
            .iter()
            .all(|action| action.queries.len() <= VALIDATOR_REPAIR_MAX_QUERIES_PER_TASK));
    }

    #[test]
    fn validator_repair_prompt_exposes_one_round_fail_closed_contract() {
        let evidence = DatasetEvidence {
            accession: "PXD000001".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let tasks = vec![ValidatorRepairTask {
            field: "organism".into(),
            error_codes: vec![
                "multiorganism_project_collapsed_to_single_candidate_organism".into(),
            ],
            error_count: 1,
            representative_rows: vec![0],
            representative_messages: vec!["multiple organisms".into()],
        }];
        let prompt = validator_repair_prompt(
            &evidence,
            &SdrfProposal::default(),
            None,
            &tasks,
            "plan",
            &[],
            &[],
        );
        assert!(prompt.contains("PHASE=PLAN"));
        assert!(prompt.contains("at most ONE evidence-retrieval round"));
        assert!(prompt.contains("Do not optimize for validator-green output"));
        assert!(prompt.contains("NEVER collapse to one project value"));
        assert!(
            prompt.contains("Rust, not you, owns the final controlled-vocabulary normalization")
        );
        assert!(prompt.contains(VALIDATOR_REPAIR_AGENT_VERSION));
    }

    #[test]
    fn validator_repair_normalization_clears_project_value_for_row_mapping_requirement() {
        let evidence = DatasetEvidence {
            accession: "PXD000001".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "pride_project".into(),
                source_label: "project:organism".into(),
                text: "Homo sapiens and Mus musculus".into(),
            }],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let tasks = vec![ValidatorRepairTask {
            field: "organism".into(),
            error_codes: vec![
                "multiorganism_project_collapsed_to_single_candidate_organism".into(),
            ],
            error_count: 1,
            representative_rows: vec![0],
            representative_messages: vec!["multiple organisms".into()],
        }];
        let mut assessment = ValidatorRepairAssessment {
            decisions: vec![ValidatorRepairDecision {
                field: "organism".into(),
                resolution: "row_mapping_required".into(),
                scope: "project".into(),
                value: "Homo sapiens".into(),
                evidence_refs: vec!["E0001".into()],
                confidence: "high".into(),
                reason: "rows require source linkage".into(),
            }],
            terminal_status: "resolved".into(),
            ..ValidatorRepairAssessment::default()
        };
        normalize_validator_repair_assessment(&evidence, &tasks, "plan", &mut assessment);
        assert_eq!(assessment.decisions[0].scope, "row");
        assert!(assessment.decisions[0].value.is_empty());
    }

    #[test]
    fn validator_repair_canonicalizes_hydrodynamic_loading_to_manual_picking() {
        let evidence = DatasetEvidence {
            accession: "PXD000001".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "publication".into(),
                source_label: "methods:isolation".into(),
                text: "Individual single cells were loaded hydrodynamically into the capillary before injection.".into(),
            }],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let decision = ValidatorRepairDecision {
            field: "single_cell_isolation_method".into(),
            resolution: "set_project_value".into(),
            scope: "project".into(),
            value: "capillary loading".into(),
            evidence_refs: vec!["E0001".into()],
            confidence: "high".into(),
            reason: "publication describes hydrodynamic loading".into(),
        };
        let (value, refs) = canonical_validator_repair_project_value(&evidence, &decision).unwrap();
        assert_eq!(value, "manual picking");
        assert_eq!(refs, vec!["E0001".to_string()]);
    }

    #[test]
    fn validator_repair_acquisition_canonicalization_rejects_filename_only_evidence() {
        let evidence = DatasetEvidence {
            accession: "PXD000001".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "pride_files".into(),
                source_label: "/snapshot/files/PXD000001.json".into(),
                text: "sample_DIA_rep1.RAW".into(),
            }],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let decision = ValidatorRepairDecision {
            field: "proteomics_data_acquisition_method".into(),
            resolution: "set_project_value".into(),
            scope: "project".into(),
            value: "DIA".into(),
            evidence_refs: vec!["E0001".into()],
            confidence: "high".into(),
            reason: "filename contains DIA".into(),
        };
        assert!(canonical_validator_repair_project_value(&evidence, &decision).is_none());
    }

    #[test]
    fn validator_repair_plan_requires_retrieval_for_nonterminal_isolation_mapping_decision() {
        let evidence = DatasetEvidence {
            accession: "PXD000001".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "publication".into(),
                source_label: "methods:isolation".into(),
                text: "Single-cell sampling method requires clarification.".into(),
            }],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let tasks = vec![ValidatorRepairTask {
            field: "single_cell_isolation_method".into(),
            error_codes: vec!["single_cell_isolation_unresolved".into()],
            error_count: 12,
            representative_rows: vec![1],
            representative_messages: vec!["isolation unresolved".into()],
        }];
        let assessment = ValidatorRepairAssessment {
            decisions: vec![ValidatorRepairDecision {
                field: "single_cell_isolation_method".into(),
                resolution: "row_mapping_required".into(),
                scope: "row".into(),
                value: String::new(),
                evidence_refs: vec!["E0001".into()],
                confidence: "low".into(),
                reason: "method unresolved".into(),
            }],
            terminal_status: "resolved".into(),
            ..ValidatorRepairAssessment::default()
        };
        assert!(validator_repair_plan_needs_retrieval(
            &evidence,
            None,
            &tasks,
            &assessment
        ));
        let actions = synthesized_validator_repair_actions(&evidence, None, &tasks, &assessment);
        assert_eq!(actions.len(), 1);
        assert_eq!(
            actions[0].target_fields,
            vec!["single_cell_isolation_method".to_string()]
        );
    }

    #[test]
    fn validator_repair_template_gap_propagates_only_when_deterministically_corroborated() {
        let mut evidence = DatasetEvidence {
            accession: "PXD000001".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![EvidenceItem {
                id: "E0001".into(),
                source_kind: "manuscript_text".into(),
                source_label: "methods:sampling".into(),
                text: "Cells were collected by capillary microsampling for proteomic analysis."
                    .into(),
            }],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let assessment = ValidatorRepairAssessment {
            decisions: vec![ValidatorRepairDecision {
                field: "single_cell_isolation_method".into(),
                resolution: "template_gap".into(),
                scope: "project".into(),
                value: String::new(),
                evidence_refs: vec!["E0001".into()],
                confidence: "high".into(),
                reason: "capillary microsampling is unsupported by the pinned vocabulary".into(),
            }],
            terminal_status: "resolved".into(),
            ..ValidatorRepairAssessment::default()
        };
        let issues = apply_validator_repair_template_gaps(&mut evidence, &assessment);
        assert_eq!(evidence.metadata_scaffold.template_gaps.len(), 1);
        assert_eq!(
            evidence.metadata_scaffold.template_gaps[0].observed_value,
            "capillary microsampling"
        );
        assert!(issues
            .iter()
            .any(|issue| issue.code == "validator_repair_template_gap_confirmed"));
    }

    #[test]
    fn validator_repair_decide_phase_clears_further_actions() {
        let evidence = DatasetEvidence {
            accession: "PXD000001".into(),
            project_json_path: String::new(),
            files_json_path: String::new(),
            existing_sdrf_path: String::new(),
            raw_files: vec![],
            study_design: StudyDesignScaffold::default(),
            metadata_scaffold: DeterministicMetadataScaffold::default(),
            evidence: vec![],
            manuscript_sources: vec![],
            annotation_sources: vec![],
        };
        let tasks = vec![ValidatorRepairTask {
            field: "single_cell_isolation_method".into(),
            error_codes: vec!["single_cell_isolation_unresolved".into()],
            error_count: 1,
            representative_rows: vec![1],
            representative_messages: vec!["isolation unresolved".into()],
        }];
        let mut assessment = ValidatorRepairAssessment {
            next_evidence_actions: vec![EvidenceActionRequest {
                action: "SEARCH_PUBLICATION".into(),
                reason: "extra search".into(),
                target_fields: vec!["single_cell_isolation_method".into()],
                queries: vec![],
            }],
            terminal_status: "continue".into(),
            ..ValidatorRepairAssessment::default()
        };
        normalize_validator_repair_assessment(&evidence, &tasks, "decide", &mut assessment);
        assert!(assessment.next_evidence_actions.is_empty());
        assert_eq!(assessment.terminal_status, "partial");
    }
}
