use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::Path;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WeightedTerm {
    pub label: String,
    pub term: String,
    pub weight: i32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DiscoveryConfig {
    pub explicit_terms: Vec<WeightedTerm>,
    pub method_terms: Vec<WeightedTerm>,
    pub biological_terms: Vec<WeightedTerm>,
    pub adjacent_terms: Vec<WeightedTerm>,
    pub negative_context_terms: Vec<WeightedTerm>,
    pub strong_score: i32,
    pub possible_score: i32,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct LaneHit {
    pub lane: String,
    pub label: String,
    pub term: String,
    pub weight: i32,
    pub source_excerpt: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct CandidateRecord {
    pub accession: String,
    pub dataset_title: String,
    pub dataset_description: String,
    pub score: i32,
    pub tier: String,
    pub positive_lanes: Vec<String>,
    pub positive_labels: Vec<String>,
    pub negative_context_labels: Vec<String>,
    pub hits: Vec<LaneHit>,
    pub project_json_path: String,
    pub files_json_path: String,
    pub sdrf_path: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct CandidateDiagnosticRecord {
    pub accession: String,
    pub dataset_title: String,
    pub dataset_description: String,
    pub discovery_score: i32,
    pub discovery_tier: String,
    pub positive_lanes: Vec<String>,
    pub specific_scp_labels: Vec<String>,
    pub method_labels: Vec<String>,
    pub biological_specific_labels: Vec<String>,
    pub broad_context_labels: Vec<String>,
    pub adjacent_labels: Vec<String>,
    pub uncategorized_positive_labels: Vec<String>,
    pub negative_context_labels: Vec<String>,
    pub evidence_profile: String,
    pub semantic_priority: String,
    pub broad_only: bool,
    pub has_negative_context: bool,
    pub project_json_path: String,
    pub files_json_path: String,
    pub sdrf_path: String,
    pub hits: Vec<LaneHit>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct CandidateAuditSummary {
    pub candidates: usize,
    pub priority_a_specific: usize,
    pub priority_b_method: usize,
    pub priority_c_broad: usize,
    pub priority_d_adjacent: usize,
    pub broad_only_candidates: usize,
    pub candidates_with_negative_context: usize,
    pub candidates_with_specific_signal: usize,
    pub candidates_with_method_signal: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct PythonBridgeSummary {
    pub candidates_exported: usize,
    pub strong_candidates: usize,
    pub possible_candidates: usize,
    pub weak_candidates: usize,
    pub priority_a_specific: usize,
    pub priority_b_method: usize,
    pub priority_c_broad: usize,
    pub priority_d_adjacent: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct DiscoverySummary {
    pub projects_scanned: usize,
    pub projects_with_positive_signal: usize,
    pub candidates_emitted: usize,
    pub strong_candidates: usize,
    pub possible_candidates: usize,
    pub weak_candidates: usize,
    pub min_score: i32,
    pub expected_positive_count: usize,
    pub candidate_count_below_expected: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct RecallAuditRow {
    pub accession: String,
    pub expected_positive: bool,
    pub recovered: bool,
    pub candidate_score: Option<i32>,
    pub candidate_tier: String,
    pub positive_lanes: String,
    pub benchmark_label: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct RecallSummary {
    pub expected_positives: usize,
    pub recovered_positives: usize,
    pub missed_positives: usize,
    pub recall: f64,
}

pub fn read_json<T: for<'de> Deserialize<'de>>(path: &Path) -> Result<T> {
    let file = File::open(path).with_context(|| format!("open {}", path.display()))?;
    serde_json::from_reader(BufReader::new(file))
        .with_context(|| format!("parse JSON {}", path.display()))
}

pub fn write_json<T: Serialize>(path: &Path, value: &T) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).with_context(|| format!("create {}", parent.display()))?;
    }
    let file = File::create(path).with_context(|| format!("create {}", path.display()))?;
    serde_json::to_writer_pretty(BufWriter::new(file), value)
        .with_context(|| format!("write JSON {}", path.display()))?;
    Ok(())
}

pub fn read_jsonl<T: for<'de> Deserialize<'de>>(path: &Path) -> Result<Vec<T>> {
    let file = File::open(path).with_context(|| format!("open {}", path.display()))?;
    let reader = BufReader::new(file);
    let mut out = Vec::new();
    for (index, line) in reader.lines().enumerate() {
        let line =
            line.with_context(|| format!("read line {} from {}", index + 1, path.display()))?;
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let row = serde_json::from_str::<T>(line)
            .with_context(|| format!("parse JSONL line {} from {}", index + 1, path.display()))?;
        out.push(row);
    }
    Ok(out)
}

pub fn write_jsonl<T: Serialize>(path: &Path, rows: &[T]) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).with_context(|| format!("create {}", parent.display()))?;
    }
    let mut writer =
        BufWriter::new(File::create(path).with_context(|| format!("create {}", path.display()))?);
    for row in rows {
        serde_json::to_writer(&mut writer, row)?;
        writer.write_all(b"\n")?;
    }
    Ok(())
}

pub fn read_nonempty_lines(path: &Path) -> Result<Vec<String>> {
    let file = File::open(path).with_context(|| format!("open {}", path.display()))?;
    let reader = BufReader::new(file);
    let mut out = Vec::new();
    for line in reader.lines() {
        let line = line?;
        let line = line.trim();
        if !line.is_empty() && !line.starts_with('#') {
            out.push(line.to_string());
        }
    }
    Ok(out)
}

pub fn flatten_json_strings(value: &serde_json::Value, out: &mut Vec<String>) {
    match value {
        serde_json::Value::String(text) => out.push(text.clone()),
        serde_json::Value::Array(items) => {
            for item in items {
                flatten_json_strings(item, out);
            }
        }
        serde_json::Value::Object(map) => {
            for value in map.values() {
                flatten_json_strings(value, out);
            }
        }
        _ => {}
    }
}

pub fn first_string_for_keys(value: &serde_json::Value, keys: &[&str]) -> String {
    let Some(map) = value.as_object() else {
        return String::new();
    };
    for key in keys {
        if let Some(serde_json::Value::String(text)) = map.get(*key) {
            if !text.trim().is_empty() {
                return text.trim().to_string();
            }
        }
    }
    String::new()
}

pub fn semicolon_join(values: &[String]) -> String {
    values.join("; ")
}

pub fn write_candidate_tsv(path: &Path, rows: &[CandidateRecord]) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut writer = csv::WriterBuilder::new().delimiter(b'\t').from_path(path)?;
    writer.write_record([
        "accession",
        "dataset_title",
        "dataset_description",
        "score",
        "tier",
        "positive_lanes",
        "positive_labels",
        "negative_context_labels",
        "project_json_path",
        "files_json_path",
        "sdrf_path",
    ])?;
    for row in rows {
        let score = row.score.to_string();
        let positive_lanes = semicolon_join(&row.positive_lanes);
        let positive_labels = semicolon_join(&row.positive_labels);
        let negative_labels = semicolon_join(&row.negative_context_labels);
        writer.write_record([
            row.accession.as_str(),
            row.dataset_title.as_str(),
            row.dataset_description.as_str(),
            score.as_str(),
            row.tier.as_str(),
            positive_lanes.as_str(),
            positive_labels.as_str(),
            negative_labels.as_str(),
            row.project_json_path.as_str(),
            row.files_json_path.as_str(),
            row.sdrf_path.as_str(),
        ])?;
    }
    writer.flush()?;
    Ok(())
}

pub fn read_candidate_tsv(path: &Path) -> Result<Vec<CandidateRecord>> {
    let mut reader = csv::ReaderBuilder::new().delimiter(b'\t').from_path(path)?;
    let headers = reader.headers()?.clone();
    let mut rows = Vec::new();
    for record in reader.records() {
        let record = record?;
        let mut map = BTreeMap::<String, String>::new();
        for (header, value) in headers.iter().zip(record.iter()) {
            map.insert(header.to_string(), value.to_string());
        }
        let split = |key: &str| -> Vec<String> {
            map.get(key)
                .map(|v| {
                    v.split(';')
                        .map(str::trim)
                        .filter(|x| !x.is_empty())
                        .map(str::to_string)
                        .collect()
                })
                .unwrap_or_default()
        };
        rows.push(CandidateRecord {
            accession: map.get("accession").cloned().unwrap_or_default(),
            dataset_title: map.get("dataset_title").cloned().unwrap_or_default(),
            dataset_description: map.get("dataset_description").cloned().unwrap_or_default(),
            score: map
                .get("score")
                .and_then(|x| x.parse::<i32>().ok())
                .unwrap_or_default(),
            tier: map.get("tier").cloned().unwrap_or_default(),
            positive_lanes: split("positive_lanes"),
            positive_labels: split("positive_labels"),
            negative_context_labels: split("negative_context_labels"),
            hits: Vec::new(),
            project_json_path: map.get("project_json_path").cloned().unwrap_or_default(),
            files_json_path: map.get("files_json_path").cloned().unwrap_or_default(),
            sdrf_path: map.get("sdrf_path").cloned().unwrap_or_default(),
        });
    }
    Ok(rows)
}

pub fn make_progress_bar(len: u64, label: &str, enabled: bool) -> indicatif::ProgressBar {
    if !enabled {
        return indicatif::ProgressBar::hidden();
    }
    let bar = indicatif::ProgressBar::new(len);
    let style = indicatif::ProgressStyle::with_template(
        "{spinner:.green} [{elapsed_precise}] [{bar:40.cyan/blue}] {pos}/{len} ({percent}%) ETA {eta_precise} {msg}",
    )
    .unwrap_or_else(|_| indicatif::ProgressStyle::default_bar())
    .progress_chars("=>-");
    bar.set_style(style);
    bar.set_message(label.to_string());
    bar
}

pub fn make_spinner(label: &str, enabled: bool) -> indicatif::ProgressBar {
    if !enabled {
        return indicatif::ProgressBar::hidden();
    }
    let spinner = indicatif::ProgressBar::new_spinner();
    let style =
        indicatif::ProgressStyle::with_template("{spinner:.green} [{elapsed_precise}] {msg}")
            .unwrap_or_else(|_| indicatif::ProgressStyle::default_spinner());
    spinner.set_style(style);
    spinner.set_message(label.to_string());
    spinner.enable_steady_tick(std::time::Duration::from_millis(120));
    spinner
}

pub fn format_duration(duration: std::time::Duration) -> String {
    let total = duration.as_secs();
    let hours = total / 3600;
    let minutes = (total % 3600) / 60;
    let seconds = total % 60;
    let millis = duration.subsec_millis();
    if hours > 0 {
        format!("{hours}h {minutes:02}m {seconds:02}s")
    } else if minutes > 0 {
        format!("{minutes}m {seconds:02}s")
    } else if total > 0 {
        format!("{seconds}.{millis:03}s")
    } else {
        format!("0.{millis:03}s")
    }
}
