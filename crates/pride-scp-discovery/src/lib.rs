use anyhow::{anyhow, Context, Result};
use pride_scp_core::{
    first_string_for_keys, flatten_json_strings, make_progress_bar, make_spinner,
    read_candidate_tsv, read_json, read_jsonl, semicolon_join, write_candidate_tsv, write_json,
    write_jsonl, CandidateAuditSummary, CandidateDiagnosticRecord, CandidateRecord,
    DiscoveryConfig, DiscoverySummary, LaneHit, PythonBridgeSummary, RecallAuditRow, RecallSummary,
    WeightedTerm,
};
use rayon::prelude::*;
use regex::Regex;
use serde_json::Value;
use std::collections::{BTreeSet, HashMap, HashSet};
use std::fs::File;
use std::io::{BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::time::Instant;

#[derive(Debug, Clone)]
pub struct DiscoverOptions {
    pub snapshot_dir: PathBuf,
    pub output_dir: PathBuf,
    pub config_path: PathBuf,
    pub min_score: i32,
    pub expected_positive_count: usize,
    /// Repository-source scope. Supported values: `all`, `pride-primary`,
    /// `registry-supplement`, `native-massive`. Production PRIDE catalogue
    /// generation uses `pride-primary` so cross-repository registry/native
    /// records cannot enter the candidate cohort.
    pub source_scope: String,
    pub progress: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DiscoverySourceScope {
    All,
    PridePrimary,
    RegistrySupplement,
    NativeMassive,
}

impl DiscoverySourceScope {
    fn parse(value: &str) -> Result<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "all" => Ok(Self::All),
            "pride-primary" | "pride" | "primary" => Ok(Self::PridePrimary),
            "registry-supplement" | "registry" => Ok(Self::RegistrySupplement),
            "native-massive" | "massive" => Ok(Self::NativeMassive),
            other => Err(anyhow!(
                "unsupported discovery source scope {other:?}; expected all, pride-primary, registry-supplement, or native-massive"
            )),
        }
    }

    fn includes_primary(self) -> bool {
        matches!(self, Self::All | Self::PridePrimary)
    }

    fn includes_registry(self) -> bool {
        matches!(self, Self::All | Self::RegistrySupplement)
    }

    fn includes_native_massive(self) -> bool {
        matches!(self, Self::All | Self::NativeMassive)
    }

    fn label(self) -> &'static str {
        match self {
            Self::All => "all",
            Self::PridePrimary => "pride-primary",
            Self::RegistrySupplement => "registry-supplement",
            Self::NativeMassive => "native-massive",
        }
    }
}

#[derive(Debug, Clone)]
pub struct CandidateAuditOptions {
    pub candidates_jsonl: PathBuf,
    pub config_path: PathBuf,
    pub output_dir: PathBuf,
    pub progress: bool,
}

#[derive(Debug, Clone)]
pub struct RecallAuditOptions {
    pub candidates_tsv: PathBuf,
    pub benchmark_csv: PathBuf,
    pub output_dir: PathBuf,
    pub repository_filter: Option<String>,
    pub progress: bool,
}

#[derive(Debug, Clone)]
pub struct ExportPythonOptions {
    pub candidates_tsv: PathBuf,
    pub candidates_jsonl: Option<PathBuf>,
    pub config_path: PathBuf,
    pub output_dir: PathBuf,
    pub min_tier: String,
    pub progress: bool,
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
        // A skeletal muscle fibre/myofibre is a single multinucleated biological cell.
        // This vocabulary is common in muscle proteomics and was a dominant measured
        // false-negative class in the frozen recall baseline. Keep the signal specific
        // by requiring nearby proteomics/MS language rather than matching "fiber" alone.
        (
            "single_muscle_fibre_proteomics",
            r"(?i)\b(?:single|individual)\s+(?:(?:skeletal|cardiac)\s+)?(?:(?:muscle|myo)\s*)?(?:fibers?|fibres?|myofibers?|myofibres?)\b.{0,120}\b(?:proteom\w*|mass\s+spectrom\w*|LC[- ]?MS|MS/MS)\b",
            12,
        ),
        (
            "single_muscle_fibre_proteomics",
            r"(?i)\b(?:proteom\w*|mass\s+spectrom\w*|LC[- ]?MS|MS/MS)\b.{0,120}\b(?:single|individual)\s+(?:(?:skeletal|cardiac)\s+)?(?:(?:muscle|myo)\s*)?(?:fibers?|fibres?|myofibers?|myofibres?)\b",
            12,
        ),
        // MALDI/MSI studies often describe the biological resolution separately from
        // the acquisition modality. Requiring both concepts in the same evidence lane
        // avoids turning generic MALDI imaging or generic "single-cell resolution"
        // spatial studies into positive discovery signals.
        (
            "single_cell_maldi_msi",
            r"(?is)\b(?:MALDI(?:[- ]?MSI)?|mass\s+spectrom(?:etry|etric)\s+imaging)\b.{0,300}\bsingle[- ]cell(?:ular)?(?:\s+resolution)?\b",
            10,
        ),
        (
            "single_cell_maldi_msi",
            r"(?is)\bsingle[- ]cell(?:ular)?(?:\s+resolution)?\b.{0,300}\b(?:MALDI(?:[- ]?MSI)?|mass\s+spectrom(?:etry|etric)\s+imaging)\b",
            10,
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

fn project_pxd_aliases(path: &Path) -> Vec<String> {
    let Ok(value) = read_json::<Value>(path) else {
        return Vec::new();
    };
    let Some(items) = value.get("pxdAliases").and_then(Value::as_array) else {
        return Vec::new();
    };
    items
        .iter()
        .filter_map(Value::as_str)
        .map(str::trim)
        .filter(|x| x.to_ascii_uppercase().starts_with("PXD"))
        .map(str::to_ascii_uppercase)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

pub fn discover(opts: DiscoverOptions) -> Result<DiscoverySummary> {
    let started = Instant::now();
    log::info!(
        "discovery start: snapshot={} output={} min_score={}",
        opts.snapshot_dir.display(),
        opts.output_dir.display(),
        opts.min_score
    );
    let cfg: DiscoveryConfig = read_json(&opts.config_path)?;
    let source_scope = DiscoverySourceScope::parse(&opts.source_scope)?;
    let project_dir = opts.snapshot_dir.join("projects");
    let mut all_primary_project_paths = std::fs::read_dir(&project_dir)
        .with_context(|| format!("read {}", project_dir.display()))?
        .filter_map(|entry| entry.ok().map(|x| x.path()))
        .filter(|path| path.extension().and_then(|x| x.to_str()) == Some("json"))
        .collect::<Vec<_>>();
    all_primary_project_paths.sort();
    let primary_accessions = all_primary_project_paths
        .iter()
        .filter_map(|path| path.file_stem().and_then(|x| x.to_str()))
        .map(str::to_ascii_uppercase)
        .collect::<HashSet<_>>();
    let primary_project_paths = if source_scope.includes_primary() {
        all_primary_project_paths.clone()
    } else {
        Vec::new()
    };

    // ProteomeCentral is a supplemental registry lane, not a replacement for the
    // primary PRIDE snapshot. Only scan registry PXD aliases that are absent from
    // snapshot/projects so duplicate registry metadata cannot perturb the accepted
    // score/tier of existing PRIDE candidates.
    let registry_project_dir = opts.snapshot_dir.join("registry").join("projects");
    let mut registry_project_paths = if source_scope.includes_registry() && registry_project_dir.is_dir() {
        std::fs::read_dir(&registry_project_dir)
            .with_context(|| format!("read {}", registry_project_dir.display()))?
            .filter_map(|entry| entry.ok().map(|x| x.path()))
            .filter(|path| path.extension().and_then(|x| x.to_str()) == Some("json"))
            .filter(|path| {
                path.file_stem()
                    .and_then(|x| x.to_str())
                    .map(str::to_ascii_uppercase)
                    .map(|accession| !primary_accessions.contains(&accession))
                    .unwrap_or(false)
            })
            .collect::<Vec<_>>()
    } else {
        Vec::new()
    };
    registry_project_paths.sort();

    // Scan every native MassIVE record, including records with a trusted PXD alias.
    // Alias existence alone is not sufficient evidence that the PXD-side metadata is
    // discovery-equivalent: ProteomeCentral/registry supplements can be sparse stubs
    // while the native repository record carries the title/description that contains
    // the actual SCP evidence. Identity-aware duplicate suppression therefore happens
    // only *after* both representations have been scored below.
    let native_massive_dir = opts
        .snapshot_dir
        .join("native")
        .join("massive")
        .join("projects");
    let mut native_massive_paths = if source_scope.includes_native_massive() && native_massive_dir.is_dir() {
        std::fs::read_dir(&native_massive_dir)
            .with_context(|| format!("read {}", native_massive_dir.display()))?
            .filter_map(|entry| entry.ok().map(|x| x.path()))
            .filter(|path| path.extension().and_then(|x| x.to_str()) == Some("json"))
            .collect::<Vec<_>>()
    } else {
        Vec::new()
    };
    native_massive_paths.sort();

    let primary_projects_scanned = primary_project_paths.len();
    let registry_supplements_scanned = registry_project_paths.len();
    let native_massive_projects_scanned = native_massive_paths.len();
    let mut project_paths = primary_project_paths;
    project_paths.extend(registry_project_paths);
    project_paths.extend(native_massive_paths);

    log::info!(
        "discovery scan: {} project records (scope={} primary={} registry_supplements={} native_massive={})",
        project_paths.len(),
        source_scope.label(),
        primary_projects_scanned,
        registry_supplements_scanned,
        native_massive_projects_scanned,
    );
    let progress = make_progress_bar(
        project_paths.len() as u64,
        "scanning repository/file/SDRF discovery signals",
        opts.progress,
    );
    let mut rows = project_paths
        .par_iter()
        .filter_map(|path| {
            let result = match discover_project(path, &opts, &cfg) {
                Ok(row) => Some(row),
                Err(error) => {
                    log::warn!("discovery read failed: {}: {error:#}", path.display());
                    None
                }
            };
            progress.inc(1);
            result
        })
        .collect::<Vec<_>>();
    progress.finish_with_message("discovery scan complete");
    rows.sort_by(|a, b| a.accession.cmp(&b.accession));

    let positive_count = rows.iter().filter(|row| row.score > 0).count();
    std::fs::create_dir_all(&opts.output_dir)?;
    let output_progress = make_spinner("writing discovery outputs", opts.progress);
    // Keep a complete scored audit table, including score=0 projects and native
    // alias duplicates, so identity/dedup decisions can be traced without
    // repeating the snapshot.
    write_candidate_tsv(&opts.output_dir.join("project_discovery_audit.tsv"), &rows)?;

    // A native MSV representation is suppressed only when at least one trusted
    // PXD alias is itself a positive candidate. This is deliberately evidence-aware:
    // a sparse registry PXD stub with score=0 must never shadow a positive native
    // MassIVE record merely because the source-derived alias edge is valid.
    let positive_pxd_accessions = rows
        .iter()
        .filter(|row| row.score >= opts.min_score)
        .map(|row| row.accession.trim().to_ascii_uppercase())
        .filter(|accession| accession.starts_with("PXD"))
        .collect::<HashSet<_>>();

    let mut identity_dedup_rows = Vec::<(String, String, i32, String, String)>::new();
    let emitted = rows
        .into_iter()
        .filter(|row| row.score >= opts.min_score)
        .filter(|row| {
            if !row.accession.to_ascii_uppercase().starts_with("MSV") {
                return true;
            }
            let aliases = project_pxd_aliases(Path::new(&row.project_json_path));
            let represented = aliases
                .iter()
                .filter(|alias| positive_pxd_accessions.contains(*alias))
                .cloned()
                .collect::<Vec<_>>();
            let suppress = !represented.is_empty();
            identity_dedup_rows.push((
                row.accession.clone(),
                aliases.join("; "),
                row.score,
                row.tier.clone(),
                represented.join("; "),
            ));
            !suppress
        })
        .collect::<Vec<_>>();

    let mut dedup_writer = BufWriter::new(File::create(
        opts.output_dir.join("native_identity_dedup_audit.tsv"),
    )?);
    writeln!(
        dedup_writer,
        "native_accession\ttrusted_pxd_aliases\tnative_score\tnative_tier\tpositive_pxd_aliases\tdecision"
    )?;
    for (native, aliases, score, tier, represented) in &identity_dedup_rows {
        let decision = if represented.is_empty() {
            "retain_native_no_positive_pxd_representation"
        } else {
            "suppress_native_positive_pxd_representation"
        };
        writeln!(
            dedup_writer,
            "{}\t{}\t{}\t{}\t{}\t{}",
            native, aliases, score, tier, represented, decision
        )?;
    }

    write_candidate_tsv(&opts.output_dir.join("candidates.tsv"), &emitted)?;
    write_jsonl(&opts.output_dir.join("candidates.jsonl"), &emitted)?;
    let mut accessions = BufWriter::new(File::create(
        opts.output_dir.join("candidate_accessions.txt"),
    )?);
    for row in &emitted {
        writeln!(accessions, "{}", row.accession)?;
    }

    let summary = DiscoverySummary {
        source_scope: source_scope.label().to_string(),
        projects_scanned: project_paths.len(),
        primary_projects_scanned,
        registry_supplements_scanned,
        native_massive_projects_scanned,
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
    output_progress.finish_with_message(format!(
        "discovery outputs complete | {} candidates",
        summary.candidates_emitted
    ));
    log::info!(
        "discovery complete in {:.1}s: scanned={} candidates={} strong={} possible={} weak={}",
        started.elapsed().as_secs_f64(),
        summary.projects_scanned,
        summary.candidates_emitted,
        summary.strong_candidates,
        summary.possible_candidates,
        summary.weak_candidates
    );
    Ok(summary)
}

fn label_set(terms: &[WeightedTerm]) -> HashSet<String> {
    terms.iter().map(|item| item.label.clone()).collect()
}

fn sorted_unique(values: impl IntoIterator<Item = String>) -> Vec<String> {
    values
        .into_iter()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

fn categorize_candidate(row: &CandidateRecord, cfg: &DiscoveryConfig) -> CandidateDiagnosticRecord {
    let explicit = label_set(&cfg.explicit_terms);
    let methods = label_set(&cfg.method_terms);
    let biological = label_set(&cfg.biological_terms);
    let adjacent = label_set(&cfg.adjacent_terms);

    // These labels are deliberately retained for recall but are broad enough to
    // match studies that mention a cell type or nearby single-cell context without
    // measuring an individual biological cell by MS.
    let broad_overrides: HashSet<&'static str> = HashSet::from([
        "single_cells",
        "individual_cells",
        "single_bacteria",
        "proteins_per_cell",
        "protein_groups_per_cell",
        "individual_biological_cell_proteomics",
        "proteomics_individual_biological_cell",
    ]);
    let regex_specific: HashSet<&'static str> =
        HashSet::from(["one_cell_sample", "single_muscle_fibre_proteomics"]);
    // MALDI/MSI plus single-cell-resolution wording is a useful acquisition signal
    // but is not, by itself, proof that one biological cell is the target MS sample.
    // Keep it in the method-priority lane so downstream evidence adjudication still
    // has to establish the biological unit and identity-preservation chain.
    let regex_methods: HashSet<&'static str> = HashSet::from(["single_cell_maldi_msi"]);

    let mut specific_scp_labels = Vec::new();
    let mut method_labels = Vec::new();
    let mut biological_specific_labels = Vec::new();
    let mut broad_context_labels = Vec::new();
    let mut adjacent_labels = Vec::new();
    let mut uncategorized_positive_labels = Vec::new();

    for label in &row.positive_labels {
        if broad_overrides.contains(label.as_str()) {
            broad_context_labels.push(label.clone());
        } else if methods.contains(label) || regex_methods.contains(label.as_str()) {
            method_labels.push(label.clone());
        } else if explicit.contains(label) || regex_specific.contains(label.as_str()) {
            specific_scp_labels.push(label.clone());
        } else if biological.contains(label) {
            biological_specific_labels.push(label.clone());
            specific_scp_labels.push(label.clone());
        } else if adjacent.contains(label) {
            adjacent_labels.push(label.clone());
        } else {
            uncategorized_positive_labels.push(label.clone());
        }
    }

    specific_scp_labels = sorted_unique(specific_scp_labels);
    method_labels = sorted_unique(method_labels);
    biological_specific_labels = sorted_unique(biological_specific_labels);
    broad_context_labels = sorted_unique(broad_context_labels);
    adjacent_labels = sorted_unique(adjacent_labels);
    uncategorized_positive_labels = sorted_unique(uncategorized_positive_labels);

    let evidence_profile = if !specific_scp_labels.is_empty() && !method_labels.is_empty() {
        "specific_plus_method"
    } else if !specific_scp_labels.is_empty() {
        "specific"
    } else if !method_labels.is_empty() && !broad_context_labels.is_empty() {
        "method_plus_context"
    } else if !method_labels.is_empty() {
        "method_only"
    } else if !broad_context_labels.is_empty() {
        "broad_only"
    } else if !adjacent_labels.is_empty() {
        "adjacent_only"
    } else {
        "other"
    }
    .to_string();

    let semantic_priority = if !specific_scp_labels.is_empty() {
        "A_specific"
    } else if !method_labels.is_empty() {
        "B_method"
    } else if !broad_context_labels.is_empty() {
        "C_broad"
    } else {
        "D_adjacent"
    }
    .to_string();

    CandidateDiagnosticRecord {
        accession: row.accession.clone(),
        dataset_title: row.dataset_title.clone(),
        dataset_description: row.dataset_description.clone(),
        discovery_score: row.score,
        discovery_tier: row.tier.clone(),
        positive_lanes: row.positive_lanes.clone(),
        specific_scp_labels,
        method_labels,
        biological_specific_labels,
        broad_context_labels,
        adjacent_labels,
        uncategorized_positive_labels,
        negative_context_labels: row.negative_context_labels.clone(),
        evidence_profile: evidence_profile.clone(),
        semantic_priority,
        broad_only: evidence_profile == "broad_only",
        has_negative_context: !row.negative_context_labels.is_empty(),
        project_json_path: row.project_json_path.clone(),
        files_json_path: row.files_json_path.clone(),
        sdrf_path: row.sdrf_path.clone(),
        hits: row.hits.clone(),
    }
}

fn candidate_audit_summary(rows: &[CandidateDiagnosticRecord]) -> CandidateAuditSummary {
    CandidateAuditSummary {
        candidates: rows.len(),
        priority_a_specific: rows
            .iter()
            .filter(|x| x.semantic_priority == "A_specific")
            .count(),
        priority_b_method: rows
            .iter()
            .filter(|x| x.semantic_priority == "B_method")
            .count(),
        priority_c_broad: rows
            .iter()
            .filter(|x| x.semantic_priority == "C_broad")
            .count(),
        priority_d_adjacent: rows
            .iter()
            .filter(|x| x.semantic_priority == "D_adjacent")
            .count(),
        broad_only_candidates: rows.iter().filter(|x| x.broad_only).count(),
        candidates_with_negative_context: rows.iter().filter(|x| x.has_negative_context).count(),
        candidates_with_specific_signal: rows
            .iter()
            .filter(|x| !x.specific_scp_labels.is_empty())
            .count(),
        candidates_with_method_signal: rows.iter().filter(|x| !x.method_labels.is_empty()).count(),
    }
}

fn write_candidate_diagnostics_tsv(path: &Path, rows: &[CandidateDiagnosticRecord]) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut writer = csv::WriterBuilder::new().delimiter(b'\t').from_path(path)?;
    writer.write_record([
        "accession",
        "dataset_title",
        "dataset_description",
        "discovery_score",
        "discovery_tier",
        "semantic_priority",
        "evidence_profile",
        "broad_only",
        "has_negative_context",
        "positive_lanes",
        "specific_scp_labels",
        "method_labels",
        "biological_specific_labels",
        "broad_context_labels",
        "adjacent_labels",
        "uncategorized_positive_labels",
        "negative_context_labels",
        "project_json_path",
        "files_json_path",
        "sdrf_path",
    ])?;
    for row in rows {
        let score = row.discovery_score.to_string();
        let broad_only = row.broad_only.to_string();
        let has_negative = row.has_negative_context.to_string();
        let positive_lanes = semicolon_join(&row.positive_lanes);
        let specific = semicolon_join(&row.specific_scp_labels);
        let methods = semicolon_join(&row.method_labels);
        let biological = semicolon_join(&row.biological_specific_labels);
        let broad = semicolon_join(&row.broad_context_labels);
        let adjacent = semicolon_join(&row.adjacent_labels);
        let uncategorized = semicolon_join(&row.uncategorized_positive_labels);
        let negative = semicolon_join(&row.negative_context_labels);
        writer.write_record([
            row.accession.as_str(),
            row.dataset_title.as_str(),
            row.dataset_description.as_str(),
            score.as_str(),
            row.discovery_tier.as_str(),
            row.semantic_priority.as_str(),
            row.evidence_profile.as_str(),
            broad_only.as_str(),
            has_negative.as_str(),
            positive_lanes.as_str(),
            specific.as_str(),
            methods.as_str(),
            biological.as_str(),
            broad.as_str(),
            adjacent.as_str(),
            uncategorized.as_str(),
            negative.as_str(),
            row.project_json_path.as_str(),
            row.files_json_path.as_str(),
            row.sdrf_path.as_str(),
        ])?;
    }
    writer.flush()?;
    Ok(())
}

pub fn candidate_audit(opts: CandidateAuditOptions) -> Result<CandidateAuditSummary> {
    let started = Instant::now();
    let cfg: DiscoveryConfig = read_json(&opts.config_path)?;
    let mut candidates: Vec<CandidateRecord> = read_jsonl(&opts.candidates_jsonl)?;
    candidates.sort_by(|a, b| a.accession.cmp(&b.accession));
    log::info!(
        "candidate audit start: candidates={} input={}",
        candidates.len(),
        opts.candidates_jsonl.display()
    );
    let progress = make_progress_bar(
        candidates.len() as u64,
        "classifying discovery evidence profiles",
        opts.progress,
    );
    let mut rows = Vec::with_capacity(candidates.len());
    for candidate in &candidates {
        rows.push(categorize_candidate(candidate, &cfg));
        progress.inc(1);
    }
    progress.finish_with_message("candidate evidence profiles complete");

    std::fs::create_dir_all(&opts.output_dir)?;
    write_candidate_diagnostics_tsv(&opts.output_dir.join("candidate_diagnostics.tsv"), &rows)?;
    write_jsonl(&opts.output_dir.join("candidate_diagnostics.jsonl"), &rows)?;
    let broad_only = rows
        .iter()
        .filter(|x| x.broad_only)
        .cloned()
        .collect::<Vec<_>>();
    write_candidate_diagnostics_tsv(
        &opts.output_dir.join("broad_only_candidates.tsv"),
        &broad_only,
    )?;
    let summary = candidate_audit_summary(&rows);
    write_json(
        &opts.output_dir.join("candidate_audit_summary.json"),
        &summary,
    )?;
    log::info!(
        "candidate audit complete in {:.1}s: A={} B={} C={} D={} broad_only={}",
        started.elapsed().as_secs_f64(),
        summary.priority_a_specific,
        summary.priority_b_method,
        summary.priority_c_broad,
        summary.priority_d_adjacent,
        summary.broad_only_candidates,
    );
    Ok(summary)
}

fn truthy(value: &str) -> bool {
    matches!(
        value.trim().to_ascii_lowercase().as_str(),
        "yes" | "true" | "1" | "y" | "positive" | "include" | "included"
    )
}

pub fn recall_audit(opts: RecallAuditOptions) -> Result<RecallSummary> {
    let started = Instant::now();
    log::info!(
        "recall audit start: candidates={} benchmark={}",
        opts.candidates_tsv.display(),
        opts.benchmark_csv.display()
    );
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
        .or_else(|| headers.iter().position(|x| x == "expected_positive"))
        .or_else(|| headers.iter().position(|x| x == "reference_decision"));
    let repository_col = headers.iter().position(|x| x == "hosting_repository");
    if opts.repository_filter.is_some() && repository_col.is_none() {
        return Err(anyhow!(
            "--repository-filter requires hosting_repository in the benchmark"
        ));
    }
    let label_col = headers
        .iter()
        .position(|x| x == "dataset_title")
        .or_else(|| headers.iter().position(|x| x == "benchmark_label"));

    let records = reader
        .records()
        .collect::<std::result::Result<Vec<_>, _>>()?;
    let progress = make_progress_bar(
        records.len() as u64,
        "checking known-positive recovery",
        opts.progress,
    );
    let mut rows = Vec::new();
    for record in records {
        progress.inc(1);
        if let (Some(filter), Some(index)) = (&opts.repository_filter, repository_col) {
            let repository = record.get(index).unwrap_or_default().trim();
            if !repository.eq_ignore_ascii_case(filter.trim()) {
                continue;
            }
        }
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
    progress.finish_with_message("known-positive recall scan complete");
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
    log::info!(
        "recall audit complete in {:.1}s: recovered={}/{} ({:.2}%)",
        started.elapsed().as_secs_f64(),
        summary.recovered_positives,
        summary.expected_positives,
        summary.recall * 100.0
    );
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

pub fn export_python(opts: ExportPythonOptions) -> Result<PythonBridgeSummary> {
    let started = Instant::now();
    log::info!(
        "python bridge export start: candidates={} min_tier={}",
        opts.candidates_tsv.display(),
        opts.min_tier
    );
    let min_rank = tier_rank(&opts.min_tier);
    if min_rank == 0 {
        return Err(anyhow!("--min-tier must be strong, possible, or weak"));
    }
    let cfg: DiscoveryConfig = read_json(&opts.config_path)?;

    let jsonl_path = opts.candidates_jsonl.clone().unwrap_or_else(|| {
        opts.candidates_tsv
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .join("candidates.jsonl")
    });
    let source_rows: Vec<CandidateRecord> = if jsonl_path.is_file() {
        read_jsonl(&jsonl_path)?
    } else {
        log::warn!(
            "candidate JSONL not found at {}; bridge will omit hit excerpts",
            jsonl_path.display()
        );
        read_candidate_tsv(&opts.candidates_tsv)?
    };

    let mut rows = source_rows
        .into_iter()
        .filter(|row| tier_rank(&row.tier) >= min_rank)
        .collect::<Vec<_>>();
    rows.sort_by(|a, b| a.accession.cmp(&b.accession));
    std::fs::create_dir_all(&opts.output_dir)?;

    let progress = make_progress_bar(
        rows.len() as u64,
        "exporting semantic Python bridge",
        opts.progress,
    );
    let mut accessions = BufWriter::new(File::create(
        opts.output_dir.join("candidate_accessions.txt"),
    )?);
    let mut diagnostics = Vec::with_capacity(rows.len());
    for row in &rows {
        writeln!(accessions, "{}", row.accession)?;
        diagnostics.push(categorize_candidate(row, &cfg));
        progress.inc(1);
    }

    write_candidate_diagnostics_tsv(
        &opts.output_dir.join("candidate_manifest.tsv"),
        &diagnostics,
    )?;
    write_jsonl(
        &opts.output_dir.join("semantic_candidates.jsonl"),
        &diagnostics,
    )?;

    let audit = candidate_audit_summary(&diagnostics);
    let summary = PythonBridgeSummary {
        candidates_exported: rows.len(),
        strong_candidates: rows.iter().filter(|x| x.tier == "strong").count(),
        possible_candidates: rows.iter().filter(|x| x.tier == "possible").count(),
        weak_candidates: rows.iter().filter(|x| x.tier == "weak").count(),
        priority_a_specific: audit.priority_a_specific,
        priority_b_method: audit.priority_b_method,
        priority_c_broad: audit.priority_c_broad,
        priority_d_adjacent: audit.priority_d_adjacent,
    };
    write_json(
        &opts.output_dir.join("python_bridge_summary.json"),
        &summary,
    )?;
    progress.finish_with_message(format!(
        "semantic Python bridge complete | {} candidates",
        rows.len()
    ));
    log::info!(
        "python bridge export complete in {:.1}s: {} candidates A={} B={} C={} D={}",
        started.elapsed().as_secs_f64(),
        rows.len(),
        summary.priority_a_specific,
        summary.priority_b_method,
        summary.priority_c_broad,
        summary.priority_d_adjacent,
    );
    Ok(summary)
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
    fn candidate_profiles_keep_broad_and_specific_signals_distinct() {
        let cfg = DiscoveryConfig {
            explicit_terms: vec![WeightedTerm {
                label: "single_cell_proteomics".into(),
                term: "single-cell proteomics".into(),
                weight: 14,
            }],
            method_terms: vec![WeightedTerm {
                label: "nanopots".into(),
                term: "nanopots".into(),
                weight: 8,
            }],
            biological_terms: vec![WeightedTerm {
                label: "single_cells".into(),
                term: "single cells".into(),
                weight: 5,
            }],
            adjacent_terms: vec![],
            negative_context_terms: vec![],
            strong_score: 36,
            possible_score: 14,
        };
        let row = CandidateRecord {
            accession: "PXD999999".into(),
            dataset_title: "test".into(),
            dataset_description: String::new(),
            score: 40,
            tier: "strong".into(),
            positive_lanes: vec!["repository_title".into()],
            positive_labels: vec![
                "single_cell_proteomics".into(),
                "single_cells".into(),
                "nanopots".into(),
            ],
            ..Default::default()
        };
        let diag = categorize_candidate(&row, &cfg);
        assert!(diag
            .specific_scp_labels
            .contains(&"single_cell_proteomics".to_string()));
        assert!(diag.method_labels.contains(&"nanopots".to_string()));
        assert!(diag
            .broad_context_labels
            .contains(&"single_cells".to_string()));
        assert_eq!(diag.semantic_priority, "A_specific");
        assert_eq!(diag.evidence_profile, "specific_plus_method");
        assert!(!diag.broad_only);
    }

    #[test]
    fn maldi_single_cell_resolution_signal_is_method_priority_not_cell_proof() {
        let cfg = DiscoveryConfig {
            explicit_terms: vec![],
            method_terms: vec![],
            biological_terms: vec![],
            adjacent_terms: vec![],
            negative_context_terms: vec![],
            strong_score: 36,
            possible_score: 14,
        };
        let row = CandidateRecord {
            accession: "PXD999998".into(),
            dataset_title: "test".into(),
            score: 10,
            tier: "weak".into(),
            positive_labels: vec!["single_cell_maldi_msi".into()],
            ..Default::default()
        };
        let diag = categorize_candidate(&row, &cfg);
        assert!(diag
            .method_labels
            .contains(&"single_cell_maldi_msi".to_string()));
        assert!(diag.specific_scp_labels.is_empty());
        assert_eq!(diag.semantic_priority, "B_method");
        assert_eq!(diag.evidence_profile, "method_only");
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
            source_scope: "all".into(),
            progress: false,
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
            repository_filter: None,
            progress: false,
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

    #[test]
    fn regex_signal_recognizes_single_muscle_fibre_proteomics() {
        let mut seen = BTreeSet::new();
        let mut hits = Vec::new();
        let score = scan_regex_signal(
            "repository_description",
            "We performed paired proteomic analysis on individual skeletal muscle fibres from each mouse.",
            1,
            &mut seen,
            &mut hits,
        );
        assert!(score >= 12);
        assert!(hits
            .iter()
            .any(|hit| hit.label == "single_muscle_fibre_proteomics"));

        let mut seen = BTreeSet::new();
        let mut hits = Vec::new();
        let score = scan_regex_signal(
            "repository_title",
            "Single skeletal muscle fibers profiled by LC-MS/MS",
            1,
            &mut seen,
            &mut hits,
        );
        assert!(score >= 12);
        assert!(hits
            .iter()
            .any(|hit| hit.label == "single_muscle_fibre_proteomics"));
    }

    #[test]
    fn regex_signal_requires_joint_maldi_and_single_cell_context() {
        let mut seen = BTreeSet::new();
        let mut hits = Vec::new();
        let score = scan_regex_signal(
            "repository_metadata",
            "Bottom-up spatial proteomics MALDI MSI.\nNew workflow for spatial bottom-up proteomics at single-cell resolution.",
            1,
            &mut seen,
            &mut hits,
        );
        assert!(score >= 10);
        assert!(hits.iter().any(|hit| hit.label == "single_cell_maldi_msi"));

        let mut seen = BTreeSet::new();
        let mut hits = Vec::new();
        let maldi_only = scan_regex_signal(
            "repository_title",
            "MALDI MSI atlas of tissue sections",
            1,
            &mut seen,
            &mut hits,
        );
        assert_eq!(maldi_only, 0);

        let mut seen = BTreeSet::new();
        let mut hits = Vec::new();
        let resolution_only = scan_regex_signal(
            "repository_description",
            "Spatial transcriptomics at single-cell resolution",
            1,
            &mut seen,
            &mut hits,
        );
        assert_eq!(resolution_only, 0);
    }

    #[test]
    fn recall_audit_accepts_frozen_gt_style_decision_and_repository_filter() {
        let output = std::env::temp_dir().join(format!(
            "pride_scp_gt_recall_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&output).unwrap();
        let candidates = output.join("candidates.tsv");
        write_candidate_tsv(
            &candidates,
            &[CandidateRecord {
                accession: "PXD900001".into(),
                score: 12,
                tier: "possible".into(),
                positive_lanes: vec!["repository_title".into()],
                ..Default::default()
            }],
        )
        .unwrap();

        let benchmark = output.join("gt.csv");
        std::fs::write(
            &benchmark,
            concat!(
                "accession,reference_decision,hosting_repository,dataset_title\n",
                "PXD900001,include,PRIDE,recovered\n",
                "PXD900002,include,MassIVE,other repository\n",
                "PXD900003,exclude,PRIDE,strict negative\n"
            ),
        )
        .unwrap();

        let summary = recall_audit(RecallAuditOptions {
            candidates_tsv: candidates,
            benchmark_csv: benchmark,
            output_dir: output.join("recall"),
            repository_filter: Some("pride".into()),
            progress: false,
        })
        .unwrap();
        assert_eq!(summary.expected_positives, 1);
        assert_eq!(summary.recovered_positives, 1);
        assert_eq!(summary.missed_positives, 0);

        let _ = std::fs::remove_dir_all(output);
    }
    #[test]
    fn registry_supplements_only_add_pxd_aliases_missing_from_primary_snapshot() {
        let root = std::env::temp_dir().join(format!(
            "pride_scp_registry_discovery_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let projects = root.join("projects");
        let registry_projects = root.join("registry/projects");
        std::fs::create_dir_all(&projects).unwrap();
        std::fs::create_dir_all(&registry_projects).unwrap();

        // Primary metadata wins for an accession that exists in both sources. The
        // registry duplicate contains a strong SCP phrase but must not perturb score.
        write_json(
            &projects.join("PXD900010.json"),
            &serde_json::json!({
                "accession": "PXD900010",
                "title": "ordinary bulk proteomics",
                "description": "bulk tissue proteomics"
            }),
        )
        .unwrap();
        write_json(
            &registry_projects.join("PXD900010.json"),
            &serde_json::json!({
                "accession": "PXD900010",
                "title": "single-cell proteomics duplicate registry metadata"
            }),
        )
        .unwrap();

        // A PXD alias absent from PRIDE is admitted as a registry supplement.
        write_json(
            &registry_projects.join("PXD900011.json"),
            &serde_json::json!({
                "accession": "PXD900011",
                "title": "Single-cell proteomics of isolated cancer cells",
                "description": "Single cells analyzed by LC-MS/MS",
                "registryHostingRepository": "MassIVE",
                "registryNativeAccessions": ["MSV000000011"]
            }),
        )
        .unwrap();

        let config = root.join("terms.json");
        write_json(
            &config,
            &DiscoveryConfig {
                explicit_terms: vec![WeightedTerm {
                    label: "single_cell_proteomics".into(),
                    term: "single-cell proteomics".into(),
                    weight: 14,
                }],
                method_terms: vec![],
                biological_terms: vec![],
                adjacent_terms: vec![],
                negative_context_terms: vec![],
                strong_score: 36,
                possible_score: 14,
            },
        )
        .unwrap();

        let out = root.join("out");
        let summary = discover(DiscoverOptions {
            snapshot_dir: root.clone(),
            output_dir: out.clone(),
            config_path: config,
            min_score: 1,
            expected_positive_count: 0,
            source_scope: "all".into(),
            progress: false,
        })
        .unwrap();
        assert_eq!(summary.primary_projects_scanned, 1);
        assert_eq!(summary.registry_supplements_scanned, 1);
        assert_eq!(summary.projects_scanned, 2);

        let candidates = read_candidate_tsv(&out.join("candidates.tsv")).unwrap();
        assert!(candidates.iter().any(|row| row.accession == "PXD900011"));
        assert!(!candidates.iter().any(|row| row.accession == "PXD900010"));
        let registry = candidates
            .iter()
            .find(|row| row.accession == "PXD900011")
            .unwrap();
        assert!(registry
            .project_json_path
            .contains("registry/projects/PXD900011.json"));

        // Production PRIDE scope must never admit registry-only aliases.
        let pride_out = root.join("pride-only-out");
        let pride_summary = discover(DiscoverOptions {
            snapshot_dir: root.clone(),
            output_dir: pride_out.clone(),
            config_path: root.join("terms.json"),
            min_score: 1,
            expected_positive_count: 0,
            source_scope: "pride-primary".into(),
            progress: false,
        })
        .unwrap();
        assert_eq!(pride_summary.primary_projects_scanned, 1);
        assert_eq!(pride_summary.registry_supplements_scanned, 0);
        assert_eq!(pride_summary.native_massive_projects_scanned, 0);
        assert_eq!(pride_summary.projects_scanned, 1);
        let pride_candidates = read_candidate_tsv(&pride_out.join("candidates.tsv")).unwrap();
        assert!(pride_candidates.is_empty());

        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn native_massive_discovery_adds_msv_only_records_but_suppresses_positive_pxd_aliases() {
        let root = std::env::temp_dir().join(format!(
            "pride_scp_native_massive_discovery_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let projects = root.join("projects");
        let registry_projects = root.join("registry/projects");
        let native_projects = root.join("native/massive/projects");
        std::fs::create_dir_all(&projects).unwrap();
        std::fs::create_dir_all(&registry_projects).unwrap();
        std::fs::create_dir_all(&native_projects).unwrap();

        write_json(
            &registry_projects.join("PXD900021.json"),
            &serde_json::json!({
                "accession": "PXD900021",
                "title": "Single-cell proteomics represented by PXD",
                "registryNativeAccessions": ["MSV000900021"]
            }),
        )
        .unwrap();
        write_json(
            &native_projects.join("MSV000900021.json"),
            &serde_json::json!({
                "accession": "MSV000900021",
                "title": "Single-cell proteomics duplicate native record",
                "pxdAliases": ["PXD900021"],
                "sourceRepository": "MassIVE"
            }),
        )
        .unwrap();
        write_json(
            &native_projects.join("MSV000900022.json"),
            &serde_json::json!({
                "accession": "MSV000900022",
                "title": "Single-cell proteomics of native-only MassIVE cells",
                "description": "Single cells analyzed by LC-MS/MS",
                "pxdAliases": [],
                "sourceRepository": "MassIVE"
            }),
        )
        .unwrap();

        let config = root.join("terms.json");
        write_json(
            &config,
            &DiscoveryConfig {
                explicit_terms: vec![WeightedTerm {
                    label: "single_cell_proteomics".into(),
                    term: "single-cell proteomics".into(),
                    weight: 14,
                }],
                method_terms: vec![],
                biological_terms: vec![],
                adjacent_terms: vec![],
                negative_context_terms: vec![],
                strong_score: 36,
                possible_score: 14,
            },
        )
        .unwrap();

        let out = root.join("out");
        let summary = discover(DiscoverOptions {
            snapshot_dir: root.clone(),
            output_dir: out.clone(),
            config_path: config,
            min_score: 1,
            expected_positive_count: 0,
            source_scope: "all".into(),
            progress: false,
        })
        .unwrap();
        assert_eq!(summary.registry_supplements_scanned, 1);
        assert_eq!(summary.native_massive_projects_scanned, 2);
        let candidates = read_candidate_tsv(&out.join("candidates.tsv")).unwrap();
        assert!(candidates.iter().any(|row| row.accession == "PXD900021"));
        assert!(candidates.iter().any(|row| row.accession == "MSV000900022"));
        assert!(!candidates.iter().any(|row| row.accession == "MSV000900021"));

        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn native_massive_alias_does_not_hide_positive_native_evidence_when_pxd_stub_scores_zero() {
        let root = std::env::temp_dir().join(format!(
            "pride_scp_native_massive_sparse_pxd_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let projects = root.join("projects");
        let registry_projects = root.join("registry/projects");
        let native_projects = root.join("native/massive/projects");
        std::fs::create_dir_all(&projects).unwrap();
        std::fs::create_dir_all(&registry_projects).unwrap();
        std::fs::create_dir_all(&native_projects).unwrap();

        // The trusted identity edge is valid, but the PXD-side registry record is
        // intentionally too sparse to carry discovery evidence.
        write_json(
            &registry_projects.join("PXD900031.json"),
            &serde_json::json!({
                "accession": "PXD900031",
                "title": "Repository dataset",
                "registryNativeAccessions": ["MSV000900031"]
            }),
        )
        .unwrap();
        write_json(
            &native_projects.join("MSV000900031.json"),
            &serde_json::json!({
                "accession": "MSV000900031",
                "title": "Single-cell proteomics of isolated cells",
                "description": "Single cells analyzed by LC-MS/MS",
                "pxdAliases": ["PXD900031"],
                "sourceRepository": "MassIVE"
            }),
        )
        .unwrap();

        let config = root.join("terms.json");
        write_json(
            &config,
            &DiscoveryConfig {
                explicit_terms: vec![WeightedTerm {
                    label: "single_cell_proteomics".into(),
                    term: "single-cell proteomics".into(),
                    weight: 14,
                }],
                method_terms: vec![],
                biological_terms: vec![],
                adjacent_terms: vec![],
                negative_context_terms: vec![],
                strong_score: 36,
                possible_score: 14,
            },
        )
        .unwrap();

        let out = root.join("out");
        let summary = discover(DiscoverOptions {
            snapshot_dir: root.clone(),
            output_dir: out.clone(),
            config_path: config,
            min_score: 1,
            expected_positive_count: 0,
            source_scope: "all".into(),
            progress: false,
        })
        .unwrap();
        assert_eq!(summary.registry_supplements_scanned, 1);
        assert_eq!(summary.native_massive_projects_scanned, 1);

        let candidates = read_candidate_tsv(&out.join("candidates.tsv")).unwrap();
        assert!(candidates.iter().any(|row| row.accession == "MSV000900031"));
        assert!(!candidates.iter().any(|row| row.accession == "PXD900031"));

        let audit = read_candidate_tsv(&out.join("project_discovery_audit.tsv")).unwrap();
        let pxd = audit.iter().find(|row| row.accession == "PXD900031").unwrap();
        let msv = audit.iter().find(|row| row.accession == "MSV000900031").unwrap();
        assert_eq!(pxd.score, 0);
        assert!(msv.score > 0);

        let dedup = std::fs::read_to_string(out.join("native_identity_dedup_audit.tsv")).unwrap();
        assert!(dedup.contains("MSV000900031"));
        assert!(dedup.contains("retain_native_no_positive_pxd_representation"));

        let _ = std::fs::remove_dir_all(root);
    }
}
