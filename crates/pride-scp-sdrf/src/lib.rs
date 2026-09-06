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
pub const GENERATOR_VERSION: &str = "pride-scp-sdrf-v0.1.1";
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

fn relevant_metadata_text(path: &str, text: &str) -> bool {
    let hay = format!(
        "{} {}",
        path.to_ascii_lowercase(),
        text.to_ascii_lowercase()
    );
    [
        "single",
        "cell",
        "sample",
        "organism",
        "tissue",
        "disease",
        "instrument",
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
        "nano",
        "batch",
        "replicate",
        "carrier",
        "reference",
        "channel",
        "protocol",
        "description",
        "title",
    ]
    .iter()
    .any(|term| hay.contains(term))
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
        if relevant_metadata_text(&path, &text) {
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
    add_json_evidence(
        &mut evidence,
        "pride_project",
        "project",
        &project,
        opts.max_evidence_items,
        opts.max_evidence_chars,
    );
    add_json_evidence(
        &mut evidence,
        "pride_files",
        "files",
        &files,
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

    if existing_sdrf.is_file() {
        if let Ok(text) = fs::read_to_string(&existing_sdrf) {
            for (i, line) in text.lines().take(120).enumerate() {
                push_evidence(
                    &mut evidence,
                    "existing_sdrf",
                    format!("sdrf:line={}", i + 1),
                    line,
                    opts.max_evidence_items,
                    opts.max_evidence_chars,
                );
            }
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
            "sample_type": {"type":"string","enum":["single cell","carrier","reference","empty","negative control","bulk control","not applicable","not available"]},
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
10. This is a draft annotation pass. Uncertainty is preferable to hallucination.\n\n\
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
        "comment[annotation tool]".to_string(),
    ]);
    // Factor candidates are retained in the Ollama proposal for human review,
    // but v0.1 does not serialize them because per-row factor assignments are
    // not yet reconstructed safely.
    h
}

fn draft_rows(
    proposal: &SdrfProposal,
    evidence: &DatasetEvidence,
) -> (Vec<String>, Vec<Vec<String>>, String) {
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
            "comment[annotation tool]",
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
    let valid_files: BTreeSet<&str> = evidence
        .raw_files
        .iter()
        .map(|f| f.file_name.as_str())
        .collect();
    let cell_id =
        Regex::new(r"^[A-Za-z0-9_.-]+$|^(?:carrier|reference|empty|not applicable)$").unwrap();
    let sample_types = [
        "single cell",
        "carrier",
        "reference",
        "empty",
        "negative control",
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
                issues.push(ValidationIssue {
                    level: "error".into(),
                    code: "sample_type_invalid_or_unresolved".into(),
                    row: ri + 1,
                    column: SC_SAMPLE_TYPE.into(),
                    message: format!(
                        "sample type is not in the pinned single-cell template: {}",
                        row[j]
                    ),
                });
            }
        }
        if let Some(&j) = index.get("comment[data file]") {
            let v = row[j].trim();
            if !valid_files.contains(v) {
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

    let proposal = call_ollama(opts, &evidence).await?;
    // Persist the parsed model proposal before semantic evidence-reference validation.
    // This keeps failed batch items diagnosable without weakening provenance checks.
    fs::write(&proposal_path, serde_json::to_string_pretty(&proposal)?)?;
    validate_proposal_refs(&proposal, &evidence)?;
    let existing = !evidence.existing_sdrf_path.is_empty();

    let (headers, rows, generation_mode) = draft_rows(&proposal, &evidence);
    write_sdrf(&draft_path, &headers, &rows)?;
    let issues = validate_draft(&headers, &rows, &evidence);
    write_review(&review_path, &issues)?;
    let errors = issues.iter().filter(|x| x.level == "error").count();
    let locally_valid = errors == 0;
    let completeness = if existing {
        "draft_generated_existing_sdrf_present_requires_merge_review"
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
        review_path: review_path.display().to_string(), validation_issue_count: issues.len(), validation_error_count: errors, locally_valid,
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
    fn asserted_relation_mode_still_requires_evidence_ref() {
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
            relation_mode: "one_cell_per_data_file".into(),
            ..Default::default()
        };
        let err = validate_proposal_refs(&proposal, &evidence)
            .unwrap_err()
            .to_string();
        assert!(err.contains("relation_mode"));
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
