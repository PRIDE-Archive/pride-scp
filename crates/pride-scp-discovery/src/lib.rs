use anyhow::{anyhow, Context, Result};
use pride_scp_core::{
    first_string_for_keys, flatten_json_strings, read_candidate_tsv, read_json, semicolon_join,
    write_candidate_tsv, write_json, write_jsonl, CandidateRecord, DiscoveryConfig,
    DiscoverySummary, LaneHit, RecallAuditRow, RecallSummary, WeightedTerm,
};
use rayon::prelude::*;
use regex::Regex;
use serde_json::Value;
use std::collections::{BTreeSet, HashMap};
use std::fs::File;
use std::io::{BufWriter, Read, Write};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
pub struct DiscoverOptions {
    pub snapshot_dir: PathBuf,
    pub output_dir: PathBuf,
    pub config_path: PathBuf,
    pub min_score: i32,
    pub expected_positive_count: usize,
}

#[derive(Debug, Clone)]
pub struct RecallAuditOptions {
    pub candidates_tsv: PathBuf,
    pub benchmark_csv: PathBuf,
    pub output_dir: PathBuf,
}

#[derive(Debug, Clone)]
pub struct ExportPythonOptions {
    pub candidates_tsv: PathBuf,
    pub output_dir: PathBuf,
    pub min_tier: String,
}

fn excerpt(text: &str, needle: &str) -> String {
    let lower = text.to_ascii_lowercase();
    let needle_lower = needle.to_ascii_lowercase();
    let Some(pos) = lower.find(&needle_lower) else {
        return text.chars().take(240).collect();
    };
    let start = pos.saturating_sub(100);
    let end = (pos + needle.len() + 140).min(text.len());
    let mut start = start;
    let mut end = end;
    while start > 0 && !text.is_char_boundary(start) {
        start -= 1;
    }
    while end < text.len() && !text.is_char_boundary(end) {
        end += 1;
    }
    text[start..end]
        .replace('\n', " ")
        .replace('\r', " ")
        .replace('\t', " ")
}

fn contains_ci(haystack: &str, needle: &str) -> bool {
    haystack
        .to_ascii_lowercase()
        .contains(&needle.to_ascii_lowercase())
}

fn scan_terms(
    lane: &str,
    text: &str,
    multiplier: i32,
    terms: &[WeightedTerm],
    seen: &mut BTreeSet<(String, String)>,
    hits: &mut Vec<LaneHit>,
) -> i32 {
    let mut score = 0;
    for item in terms {
        if contains_ci(text, &item.term) {
            let key = (lane.to_string(), item.label.clone());
            if seen.insert(key) {
                let weight = item.weight.saturating_mul(multiplier);
                score += weight;
                hits.push(LaneHit {
                    lane: lane.to_string(),
                    label: item.label.clone(),
                    term: item.term.clone(),
                    weight,
                    source_excerpt: excerpt(text, &item.term),
                });
            }
        }
    }
    score
}

fn scan_negative_terms(
    lane: &str,
    text: &str,
    terms: &[WeightedTerm],
    labels: &mut BTreeSet<String>,
    hits: &mut Vec<LaneHit>,
) {
    for item in terms {
        if contains_ci(text, &item.term) && labels.insert(item.label.clone()) {
            hits.push(LaneHit {
                lane: lane.to_string(),
                label: format!("context:{}", item.label),
                term: item.term.clone(),
                weight: 0,
                source_excerpt: excerpt(text, &item.term),
            });
        }
    }
}

fn scan_regex_signal(
    lane: &str,
    text: &str,
    multiplier: i32,
    seen: &mut BTreeSet<(String, String)>,
    hits: &mut Vec<LaneHit>,
) -> i32 {
    let patterns = [
        (
            "individual_biological_cell_proteomics",
            r"(?i)\b(?:single|individual)\s+(?:cells?|oocytes?|blastomeres?|neurons?|bacteri(?:um|a)|hepatocytes?|zygotes?|sperm(?:\s+cells?)?|egg\s+cells?)\b.{0,100}\b(?:proteom\w*|mass\s+spectrom\w*|LC[- ]?MS|MS/MS)\b",
            12,
        ),
        (
            "proteomics_individual_biological_cell",
            r"(?i)\b(?:proteom\w*|mass\s+spectrom\w*|LC[- ]?MS|MS/MS)\b.{0,100}\b(?:single|individual)\s+(?:cells?|oocytes?|blastomeres?|neurons?|bacteri(?:um|a)|hepatocytes?|zygotes?|sperm(?:\s+cells?)?|egg\s+cells?)\b",
            12,
        ),
        (
            "one_cell_sample",
            r"(?i)\b(?:1|one)[- ]cell\s+(?:sample|well|proteom\w*)s?\b",
            9,
        ),
    ];
    let mut score = 0;
    for (label, pattern, base_weight) in patterns {
        let Ok(regex) = Regex::new(pattern) else {
            continue;
        };
        if let Some(matched) = regex.find(text) {
            let key = (lane.to_string(), label.to_string());
            if seen.insert(key) {
                let weight = base_weight * multiplier;
                score += weight;
                hits.push(LaneHit {
                    lane: lane.to_string(),
                    label: label.to_string(),
                    term: matched.as_str().to_string(),
                    weight,
                    source_excerpt: excerpt(text, matched.as_str()),
                });
            }
        }
    }
    score
}

fn read_optional_text(path: &Path) -> String {
    if !path.is_file() {
        return String::new();
    }
    let mut text = String::new();
    if let Ok(mut file) = File::open(path) {
        let _ = file.read_to_string(&mut text);
    }
    text
}

fn discover_project(
    project_path: &Path,
    opts: &DiscoverOptions,
    cfg: &DiscoveryConfig,
) -> Result<CandidateRecord> {
    let project: Value = read_json(project_path)?;
    let accession = first_string_for_keys(&project, &["accession", "projectAccession"]);
    if accession.is_empty() {
        return Err(anyhow!("missing accession in {}", project_path.display()));
    }
    let title = first_string_for_keys(&project, &["title", "projectTitle", "name"]);
    let description = first_string_for_keys(
        &project,
        &["projectDescription", "description", "datasetDescription"],
    );

    let mut project_strings = Vec::new();
    flatten_json_strings(&project, &mut project_strings);
    let metadata_text = project_strings.join("\n");

    let files_path = opts
        .snapshot_dir
        .join("files")
        .join(format!("{accession}.json"));
    let files_text = if files_path.is_file() {
        let value: Value = read_json(&files_path)?;
        let mut strings = Vec::new();
        flatten_json_strings(&value, &mut strings);
        strings.join("\n")
    } else {
        String::new()
    };

    let sdrf_path = opts
        .snapshot_dir
        .join("sdrf")
        .join(format!("{accession}.sdrf.tsv"));
    let sdrf_text = read_optional_text(&sdrf_path);

    let mut hits = Vec::new();
    let mut seen = BTreeSet::new();
    let mut negative_labels = BTreeSet::new();
    let mut score = 0;

    let lanes = [
        ("repository_title", title.as_str(), 3),
        ("repository_description", description.as_str(), 2),
        ("repository_metadata", metadata_text.as_str(), 1),
        ("file_manifest", files_text.as_str(), 2),
        ("sdrf", sdrf_text.as_str(), 2),
    ];

    for (lane, text, multiplier) in lanes {
        if text.trim().is_empty() {
            continue;
        }
        score += scan_terms(
            lane,
            text,
            multiplier,
            &cfg.explicit_terms,
            &mut seen,
            &mut hits,
        );
        score += scan_terms(
            lane,
            text,
            multiplier,
            &cfg.method_terms,
            &mut seen,
            &mut hits,
        );
        score += scan_terms(
            lane,
            text,
            multiplier,
            &cfg.biological_terms,
            &mut seen,
            &mut hits,
        );
        score += scan_terms(
            lane,
            text,
            multiplier,
            &cfg.adjacent_terms,
            &mut seen,
            &mut hits,
        );
        score += scan_regex_signal(lane, text, multiplier, &mut seen, &mut hits);
        scan_negative_terms(
            lane,
            text,
            &cfg.negative_context_terms,
            &mut negative_labels,
            &mut hits,
        );
    }

    let positive_hits: Vec<&LaneHit> = hits.iter().filter(|hit| hit.weight > 0).collect();
    let positive_lanes = positive_hits
        .iter()
        .map(|hit| hit.lane.clone())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();
    let positive_labels = positive_hits
        .iter()
        .map(|hit| hit.label.clone())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();

    let tier = if score >= cfg.strong_score {
        "strong"
    } else if score >= cfg.possible_score {
        "possible"
    } else if score > 0 {
        "weak"
    } else {
        "none"
    }
    .to_string();

    Ok(CandidateRecord {
        accession,
        dataset_title: title,
        dataset_description: description,
        score,
        tier,
        positive_lanes,
        positive_labels,
        negative_context_labels: negative_labels.into_iter().collect(),
        hits,
        project_json_path: project_path.display().to_string(),
        files_json_path: if files_path.is_file() {
            files_path.display().to_string()
        } else {
            String::new()
        },
        sdrf_path: if sdrf_path.is_file() {
            sdrf_path.display().to_string()
        } else {
            String::new()
        },
    })
}

pub fn discover(opts: DiscoverOptions) -> Result<DiscoverySummary> {
    let cfg: DiscoveryConfig = read_json(&opts.config_path)?;
    let project_dir = opts.snapshot_dir.join("projects");
    let mut project_paths = std::fs::read_dir(&project_dir)
        .with_context(|| format!("read {}", project_dir.display()))?
        .filter_map(|entry| entry.ok().map(|x| x.path()))
        .filter(|path| path.extension().and_then(|x| x.to_str()) == Some("json"))
        .collect::<Vec<_>>();
    project_paths.sort();

    let mut rows = project_paths
        .par_iter()
        .filter_map(|path| match discover_project(path, &opts, &cfg) {
            Ok(row) => Some(row),
            Err(error) => {
                eprintln!("warning: {}: {error:#}", path.display());
                None
            }
        })
        .collect::<Vec<_>>();
    rows.sort_by(|a, b| a.accession.cmp(&b.accession));

    let positive_count = rows.iter().filter(|row| row.score > 0).count();
    std::fs::create_dir_all(&opts.output_dir)?;
    // Keep a complete scored audit table, including score=0 projects, so a
    // missed known positive can be traced without repeating the snapshot.
    write_candidate_tsv(&opts.output_dir.join("project_discovery_audit.tsv"), &rows)?;
    let emitted = rows
        .into_iter()
        .filter(|row| row.score >= opts.min_score)
        .collect::<Vec<_>>();

    write_candidate_tsv(&opts.output_dir.join("candidates.tsv"), &emitted)?;
    write_jsonl(&opts.output_dir.join("candidates.jsonl"), &emitted)?;
    let mut accessions = BufWriter::new(File::create(
        opts.output_dir.join("candidate_accessions.txt"),
    )?);
    for row in &emitted {
        writeln!(accessions, "{}", row.accession)?;
    }

    let summary = DiscoverySummary {
        projects_scanned: project_paths.len(),
        projects_with_positive_signal: positive_count,
        candidates_emitted: emitted.len(),
        strong_candidates: emitted.iter().filter(|x| x.tier == "strong").count(),
        possible_candidates: emitted.iter().filter(|x| x.tier == "possible").count(),
        weak_candidates: emitted.iter().filter(|x| x.tier == "weak").count(),
        min_score: opts.min_score,
        expected_positive_count: opts.expected_positive_count,
        candidate_count_below_expected: opts.expected_positive_count > 0
            && emitted.len() < opts.expected_positive_count,
    };
    write_json(&opts.output_dir.join("discovery_summary.json"), &summary)?;
    Ok(summary)
}

fn truthy(value: &str) -> bool {
    matches!(
        value.trim().to_ascii_lowercase().as_str(),
        "yes" | "true" | "1" | "y" | "positive"
    )
}

pub fn recall_audit(opts: RecallAuditOptions) -> Result<RecallSummary> {
    let candidates = read_candidate_tsv(&opts.candidates_tsv)?;
    let candidate_map: HashMap<String, CandidateRecord> = candidates
        .into_iter()
        .map(|row| (row.accession.to_ascii_uppercase(), row))
        .collect();

    let mut reader = csv::Reader::from_path(&opts.benchmark_csv)?;
    let headers = reader.headers()?.clone();
    let accession_col = headers
        .iter()
        .position(|x| x == "pxd_accession")
        .or_else(|| headers.iter().position(|x| x == "accession"))
        .ok_or_else(|| anyhow!("benchmark needs pxd_accession or accession column"))?;
    let positive_col = headers
        .iter()
        .position(|x| x == "contains_true_single_cell_ms")
        .or_else(|| headers.iter().position(|x| x == "expected_positive"));
    let label_col = headers
        .iter()
        .position(|x| x == "dataset_title")
        .or_else(|| headers.iter().position(|x| x == "benchmark_label"));

    let mut rows = Vec::new();
    for record in reader.records() {
        let record = record?;
        if let Some(index) = positive_col {
            if !truthy(record.get(index).unwrap_or_default()) {
                continue;
            }
        }
        let accession = record
            .get(accession_col)
            .unwrap_or_default()
            .trim()
            .to_ascii_uppercase();
        if accession.is_empty() {
            continue;
        }
        let candidate = candidate_map.get(&accession);
        rows.push(RecallAuditRow {
            accession: accession.clone(),
            expected_positive: true,
            recovered: candidate.is_some(),
            candidate_score: candidate.map(|x| x.score),
            candidate_tier: candidate.map(|x| x.tier.clone()).unwrap_or_default(),
            positive_lanes: candidate
                .map(|x| semicolon_join(&x.positive_lanes))
                .unwrap_or_default(),
            benchmark_label: label_col
                .and_then(|index| record.get(index))
                .unwrap_or_default()
                .to_string(),
        });
    }
    rows.sort_by(|a, b| a.accession.cmp(&b.accession));

    std::fs::create_dir_all(&opts.output_dir)?;
    let mut writer = csv::WriterBuilder::new()
        .delimiter(b'\t')
        .from_path(opts.output_dir.join("recall_audit.tsv"))?;
    writer.serialize((
        "accession",
        "expected_positive",
        "recovered",
        "candidate_score",
        "candidate_tier",
        "positive_lanes",
        "benchmark_label",
    ))?;
    for row in &rows {
        writer.serialize(row)?;
    }
    writer.flush()?;

    let missed = rows
        .iter()
        .filter(|x| !x.recovered)
        .cloned()
        .collect::<Vec<_>>();
    let mut missed_writer = csv::WriterBuilder::new()
        .delimiter(b'\t')
        .from_path(opts.output_dir.join("missed_known_positives.tsv"))?;
    missed_writer.serialize((
        "accession",
        "expected_positive",
        "recovered",
        "candidate_score",
        "candidate_tier",
        "positive_lanes",
        "benchmark_label",
    ))?;
    for row in &missed {
        missed_writer.serialize(row)?;
    }
    missed_writer.flush()?;

    let expected = rows.len();
    let recovered = rows.iter().filter(|x| x.recovered).count();
    let summary = RecallSummary {
        expected_positives: expected,
        recovered_positives: recovered,
        missed_positives: expected.saturating_sub(recovered),
        recall: if expected > 0 {
            recovered as f64 / expected as f64
        } else {
            0.0
        },
    };
    write_json(&opts.output_dir.join("recall_summary.json"), &summary)?;
    Ok(summary)
}

fn tier_rank(tier: &str) -> i32 {
    match tier.to_ascii_lowercase().as_str() {
        "strong" => 3,
        "possible" => 2,
        "weak" => 1,
        _ => 0,
    }
}

pub fn export_python(opts: ExportPythonOptions) -> Result<usize> {
    let min_rank = tier_rank(&opts.min_tier);
    if min_rank == 0 {
        return Err(anyhow!("--min-tier must be strong, possible, or weak"));
    }
    let mut rows = read_candidate_tsv(&opts.candidates_tsv)?
        .into_iter()
        .filter(|row| tier_rank(&row.tier) >= min_rank)
        .collect::<Vec<_>>();
    rows.sort_by(|a, b| a.accession.cmp(&b.accession));
    std::fs::create_dir_all(&opts.output_dir)?;

    let mut accessions = BufWriter::new(File::create(
        opts.output_dir.join("candidate_accessions.txt"),
    )?);
    for row in &rows {
        writeln!(accessions, "{}", row.accession)?;
    }

    let mut writer = csv::WriterBuilder::new()
        .delimiter(b'\t')
        .from_path(opts.output_dir.join("candidate_manifest.tsv"))?;
    writer.write_record([
        "accession",
        "discovery_score",
        "discovery_tier",
        "discovery_positive_lanes",
        "discovery_positive_labels",
        "discovery_negative_context_labels",
        "dataset_title",
        "dataset_description",
    ])?;
    for row in &rows {
        let score = row.score.to_string();
        let positive_lanes = semicolon_join(&row.positive_lanes);
        let positive_labels = semicolon_join(&row.positive_labels);
        let negative_labels = semicolon_join(&row.negative_context_labels);
        writer.write_record([
            row.accession.as_str(),
            score.as_str(),
            row.tier.as_str(),
            positive_lanes.as_str(),
            positive_labels.as_str(),
            negative_labels.as_str(),
            row.dataset_title.as_str(),
            row.dataset_description.as_str(),
        ])?;
    }
    writer.flush()?;
    Ok(rows.len())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tier_order_is_monotonic() {
        assert!(tier_rank("strong") > tier_rank("possible"));
        assert!(tier_rank("possible") > tier_rank("weak"));
    }

    #[test]
    fn fixture_union_recovers_known_positives() {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let snapshot = root.join("tests/fixtures/snapshot");
        let config = root.join("config/discovery_terms.json");
        let benchmark = root.join("tests/fixtures/known_positives.csv");
        let output = std::env::temp_dir().join(format!(
            "pride_scp_fixture_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let discovery_out = output.join("discovery");
        let recall_out = output.join("recall");

        let summary = discover(DiscoverOptions {
            snapshot_dir: snapshot,
            output_dir: discovery_out.clone(),
            config_path: config,
            min_score: 1,
            expected_positive_count: 0,
        })
        .expect("fixture discovery should succeed");
        assert!(summary.candidates_emitted >= 3);

        let candidates = read_candidate_tsv(&discovery_out.join("candidates.tsv"))
            .expect("candidate TSV should parse");
        assert!(candidates.iter().any(|x| x.accession == "PXD900001"));
        assert!(candidates.iter().any(|x| x.accession == "PXD900003"));
        assert!(!candidates.iter().any(|x| x.accession == "PXD900002"));

        let recall = recall_audit(RecallAuditOptions {
            candidates_tsv: discovery_out.join("candidates.tsv"),
            benchmark_csv: benchmark,
            output_dir: recall_out,
        })
        .expect("fixture recall audit should succeed");
        assert_eq!(recall.expected_positives, 2);
        assert_eq!(recall.recovered_positives, 2);
        assert!((recall.recall - 1.0).abs() < f64::EPSILON);

        let _ = std::fs::remove_dir_all(output);
    }

    #[test]
    fn negative_context_is_not_a_rejection() {
        let cfg = DiscoveryConfig {
            explicit_terms: vec![WeightedTerm {
                label: "scp".into(),
                term: "single-cell proteomics".into(),
                weight: 10,
            }],
            method_terms: vec![],
            biological_terms: vec![],
            adjacent_terms: vec![],
            negative_context_terms: vec![WeightedTerm {
                label: "bulk".into(),
                term: "diluted bulk".into(),
                weight: 0,
            }],
            strong_score: 20,
            possible_score: 8,
        };
        let mut seen = BTreeSet::new();
        let mut hits = Vec::new();
        let score = scan_terms(
            "title",
            "Single-cell proteomics with diluted bulk controls",
            1,
            &cfg.explicit_terms,
            &mut seen,
            &mut hits,
        );
        assert_eq!(score, 10);
        let mut negatives = BTreeSet::new();
        scan_negative_terms(
            "title",
            "Single-cell proteomics with diluted bulk controls",
            &cfg.negative_context_terms,
            &mut negatives,
            &mut hits,
        );
        assert!(negatives.contains("bulk"));
        assert_eq!(score, 10);
    }
}
