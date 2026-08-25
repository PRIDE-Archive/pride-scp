use anyhow::{anyhow, Context, Result};
use pride_scp_core::{make_progress_bar, make_spinner, read_nonempty_lines, write_json};
use reqwest::{header::RETRY_AFTER, Client, StatusCode};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Semaphore;
use tokio::task::JoinSet;
use tokio::time::sleep;

pub const DEFAULT_PRIDE_API: &str = "https://www.ebi.ac.uk/pride/ws/archive/v3";
pub const DEFAULT_PROJECT_PAGE_SIZE: usize = 100;

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
    pub project_page_size: usize,
    pub accessions_file: Option<PathBuf>,
    pub progress: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct SnapshotSummary {
    pub accessions_planned: usize,
    pub project_pages_fetched: usize,
    pub project_pages_cached: usize,
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

fn is_pxd(accession: &str) -> bool {
    let upper = accession.trim().to_ascii_uppercase();
    upper.len() >= 9 && upper.starts_with("PXD") && upper[3..].chars().all(|c| c.is_ascii_digit())
}

fn project_entries(value: &Value) -> Vec<&Value> {
    if let Some(items) = value.as_array() {
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
    }
}

fn extract_accessions(value: &Value) -> Vec<String> {
    let mut out = BTreeSet::new();
    for item in project_entries(value) {
        if let Some(accession) = item.get("accession").and_then(Value::as_str) {
            let accession = accession.trim().to_ascii_uppercase();
            if is_pxd(&accession) {
                out.insert(accession);
            }
        }
    }
    out.into_iter().collect()
}

fn nested_usize(value: &Value, keys: &[&str]) -> Option<usize> {
    let mut current = value;
    for key in keys {
        current = current.get(*key)?;
    }
    current.as_u64().and_then(|x| usize::try_from(x).ok())
}

fn page_is_last(value: &Value, page: usize, page_size: usize) -> bool {
    if let Some(last) = value.get("last").and_then(Value::as_bool) {
        if last {
            return true;
        }
    }

    let total_pages = value
        .get("totalPages")
        .and_then(Value::as_u64)
        .and_then(|x| usize::try_from(x).ok())
        .or_else(|| nested_usize(value, &["page", "totalPages"]));
    if let Some(total_pages) = total_pages {
        return page.saturating_add(1) >= total_pages;
    }

    project_entries(value).len() < page_size
}

fn retry_after_seconds(response: &reqwest::Response) -> Option<u64> {
    response
        .headers()
        .get(RETRY_AFTER)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.trim().parse::<u64>().ok())
}

fn exponential_backoff_seconds(attempt: usize) -> u64 {
    1_u64 << attempt.min(5)
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
        let mut retry_after = None;
        match client.get(url).send().await {
            Ok(response) => {
                let status = response.status();
                retry_after = retry_after_seconds(&response);
                if allow_not_found && status == StatusCode::NOT_FOUND {
                    return Ok(FetchState::NotFound);
                }
                if !status.is_success() {
                    last_error = format!("HTTP {status}");
                } else {
                    match response.bytes().await {
                        Ok(bytes) => {
                            if validate_json {
                                match serde_json::from_slice::<Value>(&bytes) {
                                    Ok(_) => {}
                                    Err(error) => {
                                        last_error = format!("invalid JSON response: {error}");
                                        if attempt + 1 < attempts {
                                            let seconds = exponential_backoff_seconds(attempt);
                                            sleep(Duration::from_secs(seconds)).await;
                                            continue;
                                        }
                                        break;
                                    }
                                }
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
                        Err(error) => {
                            // A response body can time out after the headers were received. This is
                            // transient and must participate in the same retry policy as send().
                            last_error = format!("read response body: {error:#}");
                        }
                    }
                }
            }
            Err(error) => {
                last_error = format!("send request: {error:#}");
            }
        }

        if attempt + 1 < attempts {
            let seconds = retry_after.unwrap_or_else(|| exponential_backoff_seconds(attempt));
            log::warn!(
                "HTTP retry {}/{} in {}s: {} ({})",
                attempt + 1,
                attempts - 1,
                seconds,
                url,
                last_error
            );
            sleep(Duration::from_secs(seconds)).await;
        }
    }

    Err(anyhow!(
        "request failed after {attempts} attempt(s): {last_error}"
    ))
}

async fn enumerate_project_accessions(
    client: &Client,
    opts: &SnapshotOptions,
    summary: &mut SnapshotSummary,
) -> Result<Vec<String>> {
    let page_size = opts.project_page_size.max(1);
    let pages_dir = opts.output_dir.join("project_pages");
    tokio::fs::create_dir_all(&pages_dir)
        .await
        .with_context(|| format!("create {}", pages_dir.display()))?;

    let mut accessions = BTreeSet::new();
    let mut page = 0usize;
    let spinner = make_spinner("enumerating PRIDE project catalogue", opts.progress);

    loop {
        let url = format!(
            "{}/projects/all?page={page}&pageSize={page_size}",
            opts.api_base.trim_end_matches('/')
        );
        let path = pages_dir.join(format!("page_{page:06}.json"));
        let state = fetch_cached(client, &url, &path, opts.force, opts.retries, false, true)
            .await
            .with_context(|| format!("enumerate PRIDE projects page {page} from {url}"))?;

        match state {
            FetchState::Fetched => summary.project_pages_fetched += 1,
            FetchState::Cached => summary.project_pages_cached += 1,
            FetchState::NotFound => {}
        }

        let value: Value = pride_scp_core::read_json(&path)
            .with_context(|| format!("read project page {}", path.display()))?;
        let page_accessions = extract_accessions(&value);
        for accession in page_accessions {
            accessions.insert(accession);
        }
        spinner.inc(1);
        spinner.set_message(format!(
            "enumerating PRIDE projects | page {} | {} accessions",
            page + 1,
            accessions.len()
        ));

        // A bounded pilot should never download the complete PRIDE project catalogue first.
        // Stop enumeration as soon as we have enough accessions to satisfy --limit.
        if opts.limit > 0 && accessions.len() >= opts.limit {
            break;
        }
        if page_is_last(&value, page, page_size) {
            break;
        }

        page = page.saturating_add(1);
        if page > 1_000_000 {
            return Err(anyhow!(
                "aborting project enumeration after implausibly many pages"
            ));
        }
    }

    spinner.finish_with_message(format!(
        "catalogue enumeration complete | {} accessions",
        accessions.len()
    ));

    let mut accessions = accessions.into_iter().collect::<Vec<_>>();
    if opts.limit > 0 && accessions.len() > opts.limit {
        accessions.truncate(opts.limit);
    }

    let accession_text = if accessions.is_empty() {
        String::new()
    } else {
        format!("{}\n", accessions.join("\n"))
    };
    tokio::fs::write(opts.output_dir.join("accessions.txt"), accession_text)
        .await
        .context("write accessions.txt")?;

    Ok(accessions)
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
    let started = Instant::now();
    log::info!(
        "snapshot start: output={} concurrency={} files={} sdrf={}",
        opts.output_dir.display(),
        opts.concurrency.max(1),
        opts.include_files,
        opts.include_sdrf
    );
    tokio::fs::create_dir_all(&opts.output_dir)
        .await
        .with_context(|| format!("create {}", opts.output_dir.display()))?;

    let client = Client::builder()
        .connect_timeout(Duration::from_secs(20))
        .timeout(Duration::from_secs(opts.timeout_seconds.max(1)))
        .user_agent(&opts.user_agent)
        .build()
        .context("build HTTP client")?;

    let mut summary = SnapshotSummary::default();
    let mut accessions = if let Some(path) = &opts.accessions_file {
        read_nonempty_lines(path)?
            .into_iter()
            .map(|x| x.to_ascii_uppercase())
            .filter(|x| is_pxd(x))
            .collect::<Vec<_>>()
    } else {
        enumerate_project_accessions(&client, &opts, &mut summary).await?
    };

    accessions.sort();
    accessions.dedup();
    if opts.limit > 0 && accessions.len() > opts.limit {
        accessions.truncate(opts.limit);
    }
    log::info!("snapshot plan: {} accessions", accessions.len());

    summary.accessions_planned = accessions.len();
    let progress = make_progress_bar(
        summary.accessions_planned as u64,
        "snapshotting project metadata/files/SDRF",
        opts.progress,
    );

    let semaphore = Arc::new(Semaphore::new(opts.concurrency.max(1)));
    let mut join_set = JoinSet::new();
    for accession in accessions.iter().cloned() {
        let permit = semaphore.clone().acquire_owned().await?;
        let client = client.clone();
        let opts = opts.clone();
        let worker_progress = progress.clone();
        join_set.spawn(async move {
            let _permit = permit;
            let result = snapshot_one(client, opts, accession).await;
            worker_progress.inc(1);
            worker_progress.set_message(format!("completed {}", result.0));
            result
        });
    }

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
        progress.set_message(format!(
            "processed={} | last={} | fetched p/f/s={}/{}/{} | cached={}/{}/{} | errors={}",
            completed,
            accession,
            summary.projects_fetched,
            summary.files_fetched,
            summary.sdrf_fetched,
            summary.projects_cached,
            summary.files_cached,
            summary.sdrf_cached,
            summary.project_errors + summary.file_errors + summary.sdrf_errors
        ));
    }

    progress.finish_with_message(format!(
        "snapshot complete | {} projects",
        summary.accessions_planned
    ));
    write_json(&opts.output_dir.join("snapshot_summary.json"), &summary)?;
    log::info!(
        "snapshot complete in {:.1}s: {} projects, {} total errors",
        started.elapsed().as_secs_f64(),
        summary.accessions_planned,
        summary.project_errors + summary.file_errors + summary.sdrf_errors
    );
    Ok(summary)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn extracts_projects_from_content_wrapper() {
        let value = json!({
            "content": [
                {"accession": "PXD000001"},
                {"accession": "PXD000002"},
                {"accession": "NOT_A_PXD"}
            ],
            "totalPages": 4,
            "last": false
        });
        assert_eq!(
            extract_accessions(&value),
            vec!["PXD000001".to_string(), "PXD000002".to_string()]
        );
        assert!(!page_is_last(&value, 0, 200));
    }

    #[test]
    fn detects_last_page_from_spring_metadata() {
        let value = json!({
            "content": [{"accession": "PXD000001"}],
            "totalPages": 3,
            "last": true
        });
        assert!(page_is_last(&value, 2, 200));
    }

    #[test]
    fn detects_last_page_from_short_page_fallback() {
        let value = json!([
            {"accession": "PXD000001"},
            {"accession": "PXD000002"}
        ]);
        assert!(page_is_last(&value, 0, 200));
        assert!(!page_is_last(&value, 0, 2));
    }
}
