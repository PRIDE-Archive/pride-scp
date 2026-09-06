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
pub const GENERATOR_VERSION: &str = "pride-scp-sdrf-v0.2.0";
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
pub struct SdrfAnnotateOptions {
    pub snapshot_dir: PathBuf,
    pub annotations_dir: PathBuf,
    pub publication_manifest: Option<PathBuf>,
    pub manuscript_text_paths: Vec<PathBuf>,
    pub output_dir: PathBuf,
    pub accessions: Vec<String>,
    pub accessions_file: Option<PathBuf>,
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
    pub existing_sdrf_detected: usize,
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

fn collect_accessions(opts: &SdrfAnnotateOptions) -> Result<Vec<String>> {
    let mut out = BTreeSet::new();
    for raw in &opts.accessions {
        let acc = norm_accession(raw).ok_or_else(|| anyhow!("invalid PRIDE accession: {raw}"))?;
        out.insert(acc);
    }
    if let Some(path) = &opts.accessions_file {
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
    let existing_sdrf = opts
        .snapshot_dir
        .join("sdrf")
        .join(format!("{accession}.sdrf.tsv"));
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
    // Existing SDRF is the highest-value source because it already encodes the
    // sample-to-file relationship. Parse it structurally; never flatten its raw
    // TSV lines into an LLM prompt.
    if existing_sdrf.is_file() {
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
        existing_sdrf_path: existing_sdrf
            .is_file()
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
    let ev = evidence
        .evidence
        .iter()
        .map(|e| {
            format!(
                "[{}] {} / {}: {}",
                e.id, e.source_kind, e.source_label, e.text
            )
        })
        .collect::<Vec<_>>()
        .join("\n");
    format!(
        "You are extracting metadata for an SDRF-Proteomics single-cell draft for {acc}.\n\n\
RULES:\n\
1. Use ONLY the evidence records below. Never invent sample metadata, channel assignments, instruments, enzymes, conditions, or cell identifiers.\n\
2. If a value is not evidenced, return exactly 'not available' unless the concept genuinely does not apply, in which case use 'not applicable'.\n\
3. relation_mode means the biological sample-to-RAW-file design: one_cell_per_data_file, multiplexed_cells_per_data_file, mixed, or uncertain. Be conservative.\n\
4. sample_type is the dominant target sample class, not a claim that every row has that type.\n\
5. For label, acquisition method, instrument and cleavage agent, prefer terminology already present in PRIDE/manuscript evidence.\n\
6. Every non-reserved proposed value must cite one or more E#### refs in evidence_refs. Reserved values may have an empty ref list. relation_mode=uncertain is explicitly non-assertive and may have no evidence refs.\n\
7. Do not infer a per-cell identifier from a filename here; Rust may do that deterministically only when relation_mode is one_cell_per_data_file.\n\
8. Dataset-level fields (organism part, disease, cell type, individual, batch, factors) may vary across samples. Return a concrete value only if the evidence supports that the same value applies to all target single-cell samples; otherwise use 'not available'.\n\
9. Factor proposals are review hints only in v0.1 because per-row factor assignments are not yet reconstructed safely.\n\
10. Repository provenance labels (for example PRIDE, fileCategory, publicFileLocations, FTP/HTTP locations) are NEVER biological or SDRF values. Do not copy provenance/source labels into metadata fields.\n\
11. Existing SDRF structured evidence has highest priority for values already deposited by submitters. Preserve it unless stronger evidence demonstrates it is missing, not that it is wrong.\n\
12. Factors must describe biological/experimental study variables only. Never propose repository/file bookkeeping fields as factors.\n\
13. This is a draft annotation pass. Uncertainty is preferable to hallucination.\n\n\
RAW FILE COUNT: {nfiles}\nRAW FILE SAMPLE (max {max_files}):\n{files}\n\nEVIDENCE:\n{ev}",
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

fn repair_proposal_provenance(
    proposal: &mut SdrfProposal,
    evidence: &DatasetEvidence,
) -> Vec<ValidationIssue> {
    let valid: BTreeSet<&str> = evidence.evidence.iter().map(|e| e.id.as_str()).collect();
    let mut issues = Vec::new();

    for (field, refs) in &mut proposal.evidence_refs {
        let before = refs.clone();
        refs.retain(|r| valid.contains(r.as_str()));
        for removed in before.iter().filter(|r| !refs.iter().any(|x| x == *r)) {
            issues.push(ValidationIssue {
                level: "warning".into(),
                code: "proposal_unknown_evidence_ref_removed".into(),
                row: 0,
                column: field.clone(),
                message: format!("removed unknown model evidence reference {removed}"),
            });
        }
    }

    fn repair_field(
        field: &str,
        value: &mut String,
        refs: &BTreeMap<String, Vec<String>>,
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
                    "model proposed '{original}' without a valid evidence reference; downgraded to '{}'",
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
        &mut issues,
    );
    repair_field("organism", &mut proposal.organism, refs, &mut issues);
    repair_field(
        "organism_part",
        &mut proposal.organism_part,
        refs,
        &mut issues,
    );
    repair_field("disease", &mut proposal.disease, refs, &mut issues);
    repair_field("cell_type", &mut proposal.cell_type, refs, &mut issues);
    repair_field("sample_type", &mut proposal.sample_type, refs, &mut issues);
    repair_field(
        "single_cell_isolation_method",
        &mut proposal.single_cell_isolation_method,
        refs,
        &mut issues,
    );
    repair_field("individual", &mut proposal.individual, refs, &mut issues);
    repair_field(
        "sample_preparation_batch",
        &mut proposal.sample_preparation_batch,
        refs,
        &mut issues,
    );
    repair_field(
        "cells_per_well",
        &mut proposal.cells_per_well,
        refs,
        &mut issues,
    );
    repair_field(
        "proteomics_data_acquisition_method",
        &mut proposal.proteomics_data_acquisition_method,
        refs,
        &mut issues,
    );
    repair_field("label", &mut proposal.label, refs, &mut issues);
    repair_field("instrument", &mut proposal.instrument, refs, &mut issues);
    repair_field(
        "cleavage_agent_details",
        &mut proposal.cleavage_agent_details,
        refs,
        &mut issues,
    );
    repair_field(
        "fraction_identifier",
        &mut proposal.fraction_identifier,
        refs,
        &mut issues,
    );
    repair_field(
        "technical_replicate",
        &mut proposal.technical_replicate,
        refs,
        &mut issues,
    );
    repair_field(
        "carrier_channel",
        &mut proposal.carrier_channel,
        refs,
        &mut issues,
    );
    repair_field(
        "reference_channel",
        &mut proposal.reference_channel,
        refs,
        &mut issues,
    );

    for factor in &mut proposal.factors {
        let before = factor.evidence_refs.clone();
        factor.evidence_refs.retain(|r| valid.contains(r.as_str()));
        for removed in before
            .iter()
            .filter(|r| !factor.evidence_refs.iter().any(|x| x == *r))
        {
            issues.push(ValidationIssue {
                level: "warning".into(),
                code: "factor_unknown_evidence_ref_removed".into(),
                row: 0,
                column: format!("factor[{}]", factor.name),
                message: format!("removed unknown model evidence reference {removed}"),
            });
        }
        if let Some(canonical) = canonical_reserved_alias(&factor.value) {
            factor.value = canonical.to_string();
        } else if !factor.name.trim().is_empty() && factor.evidence_refs.is_empty() {
            let original = factor.value.clone();
            factor.value = "not available".into();
            issues.push(ValidationIssue {
                level: "warning".into(),
                code: "factor_downgraded_missing_provenance".into(),
                row: 0,
                column: format!("factor[{}]", factor.name),
                message: format!("factor value '{original}' lacked a valid evidence reference; downgraded to 'not available'"),
            });
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
) -> (Vec<String>, Vec<Vec<String>>, String) {
    if !evidence.existing_sdrf_path.is_empty() {
        if let Ok(merged) = merge_existing_sdrf(proposal, evidence) {
            return merged;
        }
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
    (headers, rows, mode.to_string())
}

fn validate_draft(
    headers: &[String],
    rows: &[Vec<String>],
    evidence: &DatasetEvidence,
) -> Vec<ValidationIssue> {
    let mut issues = Vec::new();
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

    let mut proposal = call_ollama(opts, &evidence).await?;
    // Preserve the model response verbatim, then construct a provenance-safe proposal.
    // Unsupported assertions are downgraded to reserved values instead of aborting
    // the entire accession; every repair is surfaced in the review/audit outputs.
    let raw_proposal_path = proposal_path.with_file_name(format!("{accession}.ollama.raw.json"));
    fs::write(&raw_proposal_path, serde_json::to_string_pretty(&proposal)?)?;
    let mut provenance_issues = repair_proposal_provenance(&mut proposal, &evidence);
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

    let (headers, rows, generation_mode) = draft_rows(&proposal, &evidence);
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
        annotation_source_count: evidence.annotation_sources.len(), ollama_model: opts.model.clone(), ollama_used: true,
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
    let mut existing = 0usize;
    for accession in &accessions {
        if opts
            .snapshot_dir
            .join("sdrf")
            .join(format!("{accession}.sdrf.tsv"))
            .is_file()
        {
            existing += 1;
        }
    }
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
        let (headers, rows, _) = draft_rows(&proposal, &evidence);
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
        let (headers, rows, _) = draft_rows(&proposal, &evidence);
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
}
