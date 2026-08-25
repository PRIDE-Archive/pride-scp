use anyhow::{anyhow, Context, Result};
use pride_scp_core::{read_nonempty_lines, write_json};
use reqwest::{Client, StatusCode};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::Semaphore;
use tokio::task::JoinSet;
use tokio::time::sleep;

pub const DEFAULT_PRIDE_API: &str = "https://www.ebi.ac.uk/pride/ws/archive/v3";

#[derive(Debug, Clone)]
pub struct SnapshotOptions {
    pub output_dir: PathBuf,
    pub api_base: String,
    pub concurrency: usize,
    pub timeout_seconds: u64,
    pub retries: usize,
    pub user_agent: String,
    pub include_files: bool,
    pub include_sdrf: bool,
    pub force: bool,
    pub limit: usize,
    pub accessions_file: Option<PathBuf>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct SnapshotSummary {
    pub accessions_planned: usize,
    pub projects_fetched: usize,
    pub projects_cached: usize,
    pub files_fetched: usize,
    pub files_cached: usize,
    pub sdrf_fetched: usize,
    pub sdrf_cached: usize,
    pub sdrf_not_found: usize,
    pub project_errors: usize,
    pub file_errors: usize,
    pub sdrf_errors: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ErrorRecord {
    accession: String,
    stage: String,
    url: String,
    error: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum FetchState {
    Fetched,
    Cached,
    NotFound,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct ProjectSnapshotResult {
    project: Option<String>,
    files: Option<String>,
    sdrf: Option<String>,
}

fn is_pxd(accession: &str) -> bool {
    let upper = accession.trim().to_ascii_uppercase();
    upper.len() >= 9 && upper.starts_with("PXD") && upper[3..].chars().all(|c| c.is_ascii_digit())
}

fn extract_accessions(value: &Value) -> Vec<String> {
    let entries: Vec<&Value> = if let Some(items) = value.as_array() {
        items.iter().collect()
    } else if let Some(items) = value.get("projects").and_then(Value::as_array) {
        items.iter().collect()
    } else if let Some(items) = value.get("content").and_then(Value::as_array) {
        items.iter().collect()
    } else if let Some(items) = value
        .get("_embedded")
        .and_then(|v| v.get("projects"))
        .and_then(Value::as_array)
    {
        items.iter().collect()
    } else {
        Vec::new()
    };

    let mut out = BTreeSet::new();
    for item in entries {
        if let Some(accession) = item.get("accession").and_then(Value::as_str) {
            let accession = accession.trim().to_ascii_uppercase();
            if is_pxd(&accession) {
                out.insert(accession);
            }
        }
    }
    out.into_iter().collect()
}

async fn fetch_cached(
    client: &Client,
    url: &str,
    path: &Path,
    force: bool,
    retries: usize,
    allow_not_found: bool,
    validate_json: bool,
) -> Result<FetchState> {
    if path.is_file() && !force {
        return Ok(FetchState::Cached);
    }

    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .with_context(|| format!("create {}", parent.display()))?;
    }

    let attempts = retries.saturating_add(1);
    let mut last_error = String::new();

    for attempt in 0..attempts {
        match client.get(url).send().await {
            Ok(response) => {
                let status = response.status();
                if allow_not_found && status == StatusCode::NOT_FOUND {
                    return Ok(FetchState::NotFound);
                }
                if !status.is_success() {
                    last_error = format!("HTTP {status}");
                } else {
                    let bytes = response.bytes().await.context("read response body")?;
                    if validate_json {
                        serde_json::from_slice::<Value>(&bytes)
                            .with_context(|| format!("validate JSON from {url}"))?;
                    }
                    let tmp = path.with_extension(format!(
                        "{}.part",
                        path.extension().and_then(|x| x.to_str()).unwrap_or("tmp")
                    ));
                    tokio::fs::write(&tmp, &bytes)
                        .await
                        .with_context(|| format!("write {}", tmp.display()))?;
                    tokio::fs::rename(&tmp, path).await.with_context(|| {
                        format!("rename {} -> {}", tmp.display(), path.display())
                    })?;
                    return Ok(FetchState::Fetched);
                }
            }
            Err(error) => {
                last_error = format!("{error:#}");
            }
        }

        if attempt + 1 < attempts {
            let seconds = 1_u64 << attempt.min(5);
            sleep(Duration::from_secs(seconds)).await;
        }
    }

    Err(anyhow!(
        "request failed after {attempts} attempt(s): {last_error}"
    ))
}

async fn write_error(output_dir: &Path, record: &ErrorRecord) -> Result<()> {
    let path = output_dir
        .join("errors")
        .join(&record.stage)
        .join(format!("{}.json", record.accession));
    write_json(&path, record)
}

async fn snapshot_one(
    client: Client,
    opts: SnapshotOptions,
    accession: String,
) -> (String, Vec<(String, Result<FetchState>)>) {
    let mut outcomes = Vec::new();

    let project_url = format!(
        "{}/projects/{}",
        opts.api_base.trim_end_matches('/'),
        accession
    );
    let project_path = opts
        .output_dir
        .join("projects")
        .join(format!("{accession}.json"));
    let project = fetch_cached(
        &client,
        &project_url,
        &project_path,
        opts.force,
        opts.retries,
        false,
        true,
    )
    .await;
    outcomes.push(("project".to_string(), project));

    if opts.include_files {
        let url = format!(
            "{}/projects/{}/files/all",
            opts.api_base.trim_end_matches('/'),
            accession
        );
        let path = opts
            .output_dir
            .join("files")
            .join(format!("{accession}.json"));
        let result = fetch_cached(&client, &url, &path, opts.force, opts.retries, true, true).await;
        outcomes.push(("files".to_string(), result));
    }

    if opts.include_sdrf {
        let url = format!(
            "{}/files/sdrf/{}",
            opts.api_base.trim_end_matches('/'),
            accession
        );
        let path = opts
            .output_dir
            .join("sdrf")
            .join(format!("{accession}.sdrf.tsv"));
        let result =
            fetch_cached(&client, &url, &path, opts.force, opts.retries, true, false).await;
        outcomes.push(("sdrf".to_string(), result));
    }

    (accession, outcomes)
}

pub async fn snapshot(opts: SnapshotOptions) -> Result<SnapshotSummary> {
    tokio::fs::create_dir_all(&opts.output_dir)
        .await
        .with_context(|| format!("create {}", opts.output_dir.display()))?;

    let client = Client::builder()
        .timeout(Duration::from_secs(opts.timeout_seconds))
        .user_agent(&opts.user_agent)
        .build()
        .context("build HTTP client")?;

    let mut accessions = if let Some(path) = &opts.accessions_file {
        read_nonempty_lines(path)?
            .into_iter()
            .map(|x| x.to_ascii_uppercase())
            .filter(|x| is_pxd(x))
            .collect::<Vec<_>>()
    } else {
        let all_url = format!("{}/projects/all", opts.api_base.trim_end_matches('/'));
        let all_path = opts.output_dir.join("projects_all.json");
        fetch_cached(
            &client,
            &all_url,
            &all_path,
            opts.force,
            opts.retries,
            false,
            true,
        )
        .await?;
        let value: Value = pride_scp_core::read_json(&all_path)?;
        extract_accessions(&value)
    };

    accessions.sort();
    accessions.dedup();
    if opts.limit > 0 && accessions.len() > opts.limit {
        accessions.truncate(opts.limit);
    }

    let semaphore = Arc::new(Semaphore::new(opts.concurrency.max(1)));
    let mut join_set = JoinSet::new();
    for accession in accessions.iter().cloned() {
        let permit = semaphore.clone().acquire_owned().await?;
        let client = client.clone();
        let opts = opts.clone();
        join_set.spawn(async move {
            let _permit = permit;
            snapshot_one(client, opts, accession).await
        });
    }

    let mut summary = SnapshotSummary {
        accessions_planned: accessions.len(),
        ..SnapshotSummary::default()
    };
    let mut completed = 0usize;

    while let Some(joined) = join_set.join_next().await {
        let (accession, outcomes) = joined.context("snapshot worker panicked")?;
        completed += 1;
        for (stage, outcome) in outcomes {
            match (stage.as_str(), outcome) {
                ("project", Ok(FetchState::Fetched)) => summary.projects_fetched += 1,
                ("project", Ok(FetchState::Cached)) => summary.projects_cached += 1,
                ("files", Ok(FetchState::Fetched)) => summary.files_fetched += 1,
                ("files", Ok(FetchState::Cached)) => summary.files_cached += 1,
                ("sdrf", Ok(FetchState::Fetched)) => summary.sdrf_fetched += 1,
                ("sdrf", Ok(FetchState::Cached)) => summary.sdrf_cached += 1,
                ("sdrf", Ok(FetchState::NotFound)) => summary.sdrf_not_found += 1,
                (stage, Err(error)) => {
                    match stage {
                        "project" => summary.project_errors += 1,
                        "files" => summary.file_errors += 1,
                        "sdrf" => summary.sdrf_errors += 1,
                        _ => {}
                    }
                    let url = match stage {
                        "project" => format!(
                            "{}/projects/{}",
                            opts.api_base.trim_end_matches('/'),
                            accession
                        ),
                        "files" => format!(
                            "{}/projects/{}/files/all",
                            opts.api_base.trim_end_matches('/'),
                            accession
                        ),
                        "sdrf" => format!(
                            "{}/files/sdrf/{}",
                            opts.api_base.trim_end_matches('/'),
                            accession
                        ),
                        _ => String::new(),
                    };
                    let record = ErrorRecord {
                        accession: accession.clone(),
                        stage: stage.to_string(),
                        url,
                        error: format!("{error:#}"),
                    };
                    if let Err(write_error) = write_error(&opts.output_dir, &record).await {
                        eprintln!("warning: could not write error record: {write_error:#}");
                    }
                }
                _ => {}
            }
        }
        if completed % 100 == 0 || completed == summary.accessions_planned {
            eprintln!("snapshot {completed}/{}", summary.accessions_planned);
        }
    }

    write_json(&opts.output_dir.join("snapshot_summary.json"), &summary)?;
    Ok(summary)
}
