use anyhow::{anyhow, Context, Result};
use pride_scp_core::{
    first_string_for_keys, flatten_json_strings, make_progress_bar, make_spinner,
    read_nonempty_lines, write_json,
};
use reqwest::{header::RETRY_AFTER, Client, StatusCode};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeSet, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Semaphore;
use tokio::task::JoinSet;
use tokio::time::sleep;

pub const DEFAULT_PRIDE_API: &str = "https://www.ebi.ac.uk/pride/ws/archive/v3";
pub const DEFAULT_PROJECT_PAGE_SIZE: usize = 100;
pub const DEFAULT_PROTEOMECENTRAL_PROXI_API: &str =
    "https://proteomecentral.proteomexchange.org/api/proxi/v0.1";
pub const DEFAULT_REGISTRY_PAGE_SIZE: usize = 100;

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
    /// Stop catalogue enumeration after this many consecutive pages add no new accessions.
    pub max_stagnant_pages: usize,
    /// Maximum number of concurrent HTTP requests across project/files/SDRF stages.
    pub request_concurrency: usize,
    /// Seed per-project metadata from the catalogue payload when possible.
    pub seed_projects_from_catalogue: bool,
    /// Refresh the catalogue payload even when project-page cache files exist.
    pub refresh_catalogue: bool,
    pub accessions_file: Option<PathBuf>,
    pub progress: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct SnapshotSummary {
    pub accessions_planned: usize,
    pub project_pages_fetched: usize,
    pub project_pages_cached: usize,
    pub project_pages_duplicate_only: usize,
    pub unique_accessions_enumerated: usize,
    pub catalogue_projects_seeded: usize,
    pub enumeration_termination: String,
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

#[derive(Debug, Clone)]
pub struct RegistrySnapshotOptions {
    /// Existing PRIDE snapshot that will receive a supplemental ProteomeCentral index.
    pub snapshot_dir: PathBuf,
    pub api_base: String,
    pub timeout_seconds: u64,
    pub retries: usize,
    pub user_agent: String,
    /// PROXI datasets page size. The public API currently documents a maximum of 100.
    pub page_size: usize,
    /// Safety cap. Zero means no user cap beyond the internal implausibility guard.
    pub max_pages: usize,
    /// Refresh cached registry pages and normalized records.
    pub force: bool,
    pub progress: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct RegistrySnapshotSummary {
    pub api_base: String,
    pub page_size: usize,
    pub pages_fetched: usize,
    pub pages_cached: usize,
    pub datasets_seen: usize,
    pub datasets_with_pxd_alias: usize,
    pub unique_pxd_accessions: usize,
    pub primary_snapshot_overlaps: usize,
    pub supplemental_pxd_accessions: usize,
    pub normalized_records_written: usize,
    pub stale_normalized_records_removed: usize,
    pub native_aliases_observed: usize,
    pub enumeration_termination: String,
}

#[derive(Debug, Clone)]
pub struct CompactSnapshotOptions {
    pub snapshot_dir: PathBuf,
    /// Keep the newest completed catalogue page as a compact provenance copy.
    pub keep_latest_project_page: bool,
    /// Optional accession list used only when pruning non-candidate file/SDRF evidence.
    pub retain_accessions_file: Option<PathBuf>,
    /// Remove file-manifest and SDRF payloads for accessions outside retain_accessions_file.
    pub prune_noncandidate_evidence: bool,
    pub dry_run: bool,
    pub progress: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct CompactSnapshotSummary {
    pub snapshot_dir: String,
    pub accessions_indexed: usize,
    pub project_records: usize,
    pub project_pages_found: usize,
    pub project_pages_removed: usize,
    pub project_pages_kept: usize,
    pub project_page_bytes_removed: u64,
    pub file_manifests_removed: usize,
    pub sdrf_payloads_removed: usize,
    pub evidence_bytes_removed: u64,
    pub dry_run: bool,
    pub validation_ok: bool,
    pub note: String,
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

fn registry_dataset_entries(value: &Value) -> Vec<&Value> {
    if let Some(items) = value.as_array() {
        return items.iter().collect();
    }
    for key in ["datasets", "results", "data", "content"] {
        if let Some(items) = value.get(key).and_then(Value::as_array) {
            return items.iter().collect();
        }
    }
    if let Some(data) = value.get("data") {
        for key in ["datasets", "results", "content"] {
            if let Some(items) = data.get(key).and_then(Value::as_array) {
                return items.iter().collect();
            }
        }
    }
    Vec::new()
}

fn accession_tokens(text: &str) -> impl Iterator<Item = String> + '_ {
    text.split(|c: char| !c.is_ascii_alphanumeric())
        .map(str::trim)
        .filter(|token| !token.is_empty())
        .map(str::to_ascii_uppercase)
}

fn registry_identifier_strings(dataset: &Value) -> Vec<String> {
    let mut strings = Vec::new();
    if let Some(value) = dataset.get("identifiers") {
        flatten_json_strings(value, &mut strings);
    }
    if let Some(value) = dataset.get("accession") {
        flatten_json_strings(value, &mut strings);
    }
    // Some registry providers expose the PX accession only in a dataset link
    // rather than the identifiers array. Restrict this fallback to structured
    // links so PXD mentions in free-text descriptions are not mistaken for aliases.
    if let Some(value) = dataset.get("fullDatasetLinks") {
        flatten_json_strings(value, &mut strings);
    }
    strings
}

fn registry_pxd_accessions(dataset: &Value) -> Vec<String> {
    registry_identifier_strings(dataset)
        .iter()
        .flat_map(|text| accession_tokens(text))
        .filter(|token| is_pxd(token))
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

fn registry_native_accessions(dataset: &Value) -> Vec<String> {
    let mut strings = Vec::new();
    flatten_json_strings(dataset, &mut strings);
    strings
        .iter()
        .flat_map(|text| accession_tokens(text))
        .filter(|token| {
            ["MSV", "IPX", "JPST", "PASS", "PXL"].iter().any(|prefix| {
                token
                    .strip_prefix(prefix)
                    .map(|rest| !rest.is_empty() && rest.chars().all(|c| c.is_ascii_digit()))
                    .unwrap_or(false)
            })
        })
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

fn infer_registry_repository(dataset: &Value, native_accessions: &[String]) -> String {
    if native_accessions.iter().any(|x| x.starts_with("MSV")) {
        return "MassIVE".to_string();
    }
    if native_accessions.iter().any(|x| x.starts_with("IPX")) {
        return "iProX".to_string();
    }
    if native_accessions.iter().any(|x| x.starts_with("JPST")) {
        return "jPOST".to_string();
    }
    if native_accessions.iter().any(|x| x.starts_with("PASS")) {
        return "PeptideAtlas/PASSEL".to_string();
    }

    let mut strings = Vec::new();
    flatten_json_strings(dataset, &mut strings);
    let joined = strings.join("\n").to_ascii_lowercase();
    for (needle, label) in [
        ("massive", "MassIVE"),
        ("iprox", "iProX"),
        ("jpost", "jPOST"),
        ("panorama public", "Panorama Public"),
        ("panorama", "Panorama Public"),
        ("pride", "PRIDE"),
    ] {
        if joined.contains(needle) {
            return label.to_string();
        }
    }
    "unknown".to_string()
}

fn normalized_registry_dataset(accession: &str, dataset: &Value) -> Value {
    let native_accessions = registry_native_accessions(dataset);
    let hosting_repository = infer_registry_repository(dataset, &native_accessions);
    let title = first_string_for_keys(dataset, &["title", "datasetTitle", "name"]);
    let description = first_string_for_keys(
        dataset,
        &["description", "projectDescription", "datasetDescription"],
    );
    serde_json::json!({
        "accession": accession,
        "projectAccession": accession,
        "title": title,
        "description": description,
        "registrySource": "ProteomeCentral PROXI",
        "registryHostingRepository": hosting_repository,
        "registryNativeAccessions": native_accessions,
        "registryDataset": dataset,
    })
}

fn has_pagination_metadata(value: &Value) -> bool {
    value.get("last").is_some()
        || value.get("totalPages").is_some()
        || value.get("totalElements").is_some()
        || value
            .get("page")
            .and_then(Value::as_object)
            .map(|page| {
                page.contains_key("totalPages")
                    || page.contains_key("totalElements")
                    || page.contains_key("number")
            })
            .unwrap_or(false)
        || value
            .get("_links")
            .and_then(Value::as_object)
            .map(|links| links.contains_key("next") || links.contains_key("last"))
            .unwrap_or(false)
}

fn catalogue_response_is_monolithic(value: &Value, requested_page_size: usize) -> bool {
    let entries = project_entries(value).len();
    // Live PRIDE v3 currently returns the complete project catalogue from /projects/all
    // even when page/pageSize are supplied. Detect that shape explicitly so a full crawl
    // does not request the same ~40k-project payload hundreds or thousands of times.
    entries
        > requested_page_size
            .saturating_mul(2)
            .max(requested_page_size + 1)
        && !has_pagination_metadata(value)
}

fn seed_catalogue_projects(value: &Value, output_dir: &Path, force: bool) -> Result<usize> {
    let projects_dir = output_dir.join("projects");
    std::fs::create_dir_all(&projects_dir)
        .with_context(|| format!("create {}", projects_dir.display()))?;
    let mut seeded = 0usize;
    for item in project_entries(value) {
        let Some(accession) = item.get("accession").and_then(Value::as_str) else {
            continue;
        };
        let accession = accession.trim().to_ascii_uppercase();
        if !is_pxd(&accession) {
            continue;
        }
        let path = projects_dir.join(format!("{accession}.json"));
        if path.is_file() && !force {
            continue;
        }
        write_json(&path, item)
            .with_context(|| format!("seed catalogue project {}", path.display()))?;
        seeded += 1;
    }
    Ok(seeded)
}

fn latest_cached_project_page(pages_dir: &Path) -> Option<PathBuf> {
    let entries = std::fs::read_dir(pages_dir).ok()?;
    entries
        .filter_map(|entry| entry.ok())
        .filter_map(|entry| {
            let path = entry.path();
            if path.extension().and_then(|x| x.to_str()) != Some("json") {
                return None;
            }
            let stem = path.file_stem()?.to_str()?;
            let page = stem.strip_prefix("page_")?.parse::<usize>().ok()?;
            Some((page, path))
        })
        .max_by_key(|(page, _)| *page)
        .map(|(_, path)| path)
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

async fn fetch_cached_bounded(
    request_semaphore: &Arc<Semaphore>,
    client: &Client,
    url: &str,
    path: &Path,
    force: bool,
    retries: usize,
    allow_not_found: bool,
    validate_json: bool,
) -> Result<FetchState> {
    // Cache hits do not consume an HTTP slot.
    if path.is_file() && !force {
        return Ok(FetchState::Cached);
    }
    let _permit = request_semaphore
        .acquire()
        .await
        .map_err(|_| anyhow!("HTTP request semaphore closed"))?;
    fetch_cached(
        client,
        url,
        path,
        force,
        retries,
        allow_not_found,
        validate_json,
    )
    .await
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
    let mut stagnant_pages = 0usize;
    let max_stagnant_pages = opts.max_stagnant_pages.max(1);
    let spinner = make_spinner("enumerating PRIDE project catalogue", opts.progress);

    // Recover efficiently from a previously interrupted v0.1.2 runaway enumeration.
    // The highest completed cached page is also the newest live catalogue snapshot. If
    // it already contains the complete monolithic catalogue, use it directly instead of
    // replaying page_000000, page_000001, ... from the beginning.
    if !opts.force && !opts.refresh_catalogue {
        if let Some(latest_path) = latest_cached_project_page(&pages_dir) {
            let latest_value: Value = pride_scp_core::read_json(&latest_path)
                .with_context(|| format!("read cached project page {}", latest_path.display()))?;
            if catalogue_response_is_monolithic(&latest_value, page_size) {
                let mut accessions = extract_accessions(&latest_value);
                summary.project_pages_cached += 1;
                summary.unique_accessions_enumerated = accessions.len();
                summary.enumeration_termination = "latest_cached_monolithic_catalogue".to_string();
                if opts.seed_projects_from_catalogue {
                    spinner.set_message(format!(
                        "seeding {} project records from newest cached catalogue",
                        accessions.len()
                    ));
                    let seeded =
                        seed_catalogue_projects(&latest_value, &opts.output_dir, opts.force)?;
                    summary.catalogue_projects_seeded += seeded;
                    log::info!("seeded {} project records from cached catalogue", seeded);
                }
                spinner.finish_with_message(format!(
                    "catalogue enumeration complete | {} accessions | latest cached monolithic catalogue",
                    accessions.len()
                ));
                accessions.sort();
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
                return Ok(accessions);
            }
        }
    }

    loop {
        let url = format!(
            "{}/projects/all?page={page}&pageSize={page_size}",
            opts.api_base.trim_end_matches('/')
        );
        let path = pages_dir.join(format!("page_{page:06}.json"));
        let state = fetch_cached(
            client,
            &url,
            &path,
            opts.force || opts.refresh_catalogue,
            opts.retries,
            false,
            true,
        )
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
        let before = accessions.len();
        for accession in &page_accessions {
            accessions.insert(accession.clone());
        }
        let added = accessions.len().saturating_sub(before);

        if opts.seed_projects_from_catalogue {
            let seeded = seed_catalogue_projects(&value, &opts.output_dir, opts.force)?;
            summary.catalogue_projects_seeded += seeded;
            if seeded > 0 {
                log::info!(
                    "seeded {} project records from catalogue page {}",
                    seeded,
                    page
                );
            }
        }

        if added == 0 {
            stagnant_pages += 1;
            summary.project_pages_duplicate_only += 1;
        } else {
            stagnant_pages = 0;
        }

        spinner.inc(1);
        spinner.set_message(format!(
            "enumerating PRIDE projects | page {} | {} unique | +{} new | stagnant {}/{}",
            page + 1,
            accessions.len(),
            added,
            stagnant_pages,
            max_stagnant_pages
        ));

        // A bounded pilot should never download the complete PRIDE project catalogue first.
        if opts.limit > 0 && accessions.len() >= opts.limit {
            summary.enumeration_termination = "limit_reached".to_string();
            break;
        }

        // The live v3 /projects/all endpoint has been observed returning the entire catalogue
        // (~40k projects) even when page/pageSize are supplied. In that case the first response
        // is authoritative enough for enumeration and requesting page 1, 2, ... merely repeats
        // the same huge payload.
        if catalogue_response_is_monolithic(&value, page_size) {
            summary.enumeration_termination = "monolithic_catalogue_response".to_string();
            log::info!(
                "catalogue endpoint returned {} projects for requested pageSize={}; treating response as complete",
                page_accessions.len(),
                page_size
            );
            break;
        }

        if page_is_last(&value, page, page_size) {
            summary.enumeration_termination = "api_last_page".to_string();
            break;
        }

        if stagnant_pages >= max_stagnant_pages {
            summary.enumeration_termination = format!("stagnant_after_{max_stagnant_pages}_pages");
            log::warn!(
                "stopping PRIDE project enumeration after {} consecutive duplicate-only pages ({} unique accessions)",
                stagnant_pages,
                accessions.len()
            );
            break;
        }

        page = page.saturating_add(1);
        if page > 1_000_000 {
            return Err(anyhow!(
                "aborting project enumeration after implausibly many pages"
            ));
        }
    }

    if summary.enumeration_termination.is_empty() {
        summary.enumeration_termination = "unknown".to_string();
    }
    summary.unique_accessions_enumerated = accessions.len();

    spinner.finish_with_message(format!(
        "catalogue enumeration complete | {} accessions | {}",
        accessions.len(),
        summary.enumeration_termination
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
    request_semaphore: Arc<Semaphore>,
    opts: SnapshotOptions,
    accession: String,
) -> (String, Vec<(String, Result<FetchState>)>) {
    let project_url = format!(
        "{}/projects/{}",
        opts.api_base.trim_end_matches('/'),
        accession
    );
    let project_path = opts
        .output_dir
        .join("projects")
        .join(format!("{accession}.json"));

    let files_url = format!(
        "{}/projects/{}/files/all",
        opts.api_base.trim_end_matches('/'),
        accession
    );
    let files_path = opts
        .output_dir
        .join("files")
        .join(format!("{accession}.json"));

    let sdrf_url = format!(
        "{}/files/sdrf/{}",
        opts.api_base.trim_end_matches('/'),
        accession
    );
    let sdrf_path = opts
        .output_dir
        .join("sdrf")
        .join(format!("{accession}.sdrf.tsv"));

    // These resources are independent. Fetch them concurrently, while a separate global
    // request semaphore keeps aggregate pressure on PRIDE bounded.
    let project_future = fetch_cached_bounded(
        &request_semaphore,
        &client,
        &project_url,
        &project_path,
        opts.force,
        opts.retries,
        false,
        true,
    );
    let files_future = async {
        if opts.include_files {
            fetch_cached_bounded(
                &request_semaphore,
                &client,
                &files_url,
                &files_path,
                opts.force,
                opts.retries,
                true,
                true,
            )
            .await
        } else {
            Ok(FetchState::NotFound)
        }
    };
    let sdrf_future = async {
        if opts.include_sdrf {
            fetch_cached_bounded(
                &request_semaphore,
                &client,
                &sdrf_url,
                &sdrf_path,
                opts.force,
                opts.retries,
                true,
                false,
            )
            .await
        } else {
            Ok(FetchState::NotFound)
        }
    };

    let (project, files, sdrf) = tokio::join!(project_future, files_future, sdrf_future);
    let mut outcomes = vec![("project".to_string(), project)];
    if opts.include_files {
        outcomes.push(("files".to_string(), files));
    }
    if opts.include_sdrf {
        outcomes.push(("sdrf".to_string(), sdrf));
    }
    (accession, outcomes)
}

pub async fn snapshot(opts: SnapshotOptions) -> Result<SnapshotSummary> {
    let started = Instant::now();
    log::info!(
        "snapshot start: output={} project_concurrency={} request_concurrency={} files={} sdrf={}",
        opts.output_dir.display(),
        opts.concurrency.max(1),
        opts.request_concurrency.max(1),
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
    if opts.accessions_file.is_some() {
        summary.unique_accessions_enumerated = accessions.len();
        summary.enumeration_termination = "accessions_file".to_string();
    }
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

    let project_concurrency = opts.concurrency.max(1);
    let request_semaphore = Arc::new(Semaphore::new(opts.request_concurrency.max(1)));
    let mut join_set = JoinSet::new();
    let mut accession_iter = accessions.into_iter();

    for _ in 0..project_concurrency {
        let Some(accession) = accession_iter.next() else {
            break;
        };
        let worker_client = client.clone();
        let worker_opts = opts.clone();
        let worker_progress = progress.clone();
        let worker_request_semaphore = request_semaphore.clone();
        join_set.spawn(async move {
            let result = snapshot_one(
                worker_client,
                worker_request_semaphore,
                worker_opts,
                accession,
            )
            .await;
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

        if let Some(next_accession) = accession_iter.next() {
            let worker_client = client.clone();
            let worker_opts = opts.clone();
            let worker_progress = progress.clone();
            let worker_request_semaphore = request_semaphore.clone();
            join_set.spawn(async move {
                let result = snapshot_one(
                    worker_client,
                    worker_request_semaphore,
                    worker_opts,
                    next_accession,
                )
                .await;
                worker_progress.inc(1);
                worker_progress.set_message(format!("completed {}", result.0));
                result
            });
        }
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

pub async fn registry_snapshot(opts: RegistrySnapshotOptions) -> Result<RegistrySnapshotSummary> {
    let started = Instant::now();
    let registry_dir = opts.snapshot_dir.join("registry");
    let pages_dir = registry_dir.join("pages");
    let projects_dir = registry_dir.join("projects");
    tokio::fs::create_dir_all(&pages_dir)
        .await
        .with_context(|| format!("create {}", pages_dir.display()))?;
    tokio::fs::create_dir_all(&projects_dir)
        .await
        .with_context(|| format!("create {}", projects_dir.display()))?;

    let client = Client::builder()
        .connect_timeout(Duration::from_secs(20))
        .timeout(Duration::from_secs(opts.timeout_seconds.max(1)))
        .user_agent(&opts.user_agent)
        .build()
        .context("build ProteomeCentral HTTP client")?;

    let primary_projects_dir = opts.snapshot_dir.join("projects");
    let primary_accessions = if primary_projects_dir.is_dir() {
        std::fs::read_dir(&primary_projects_dir)?
            .filter_map(|entry| entry.ok().map(|x| x.path()))
            .filter_map(|path| accession_from_named_file(&path))
            .collect::<HashSet<_>>()
    } else {
        HashSet::new()
    };

    let page_size = opts.page_size.clamp(1, DEFAULT_REGISTRY_PAGE_SIZE);
    let mut summary = RegistrySnapshotSummary {
        api_base: opts.api_base.clone(),
        page_size,
        ..Default::default()
    };
    let mut seen_pxd = BTreeSet::new();
    let mut supplemental_pxd = BTreeSet::new();
    let mut native_aliases = BTreeSet::new();
    let mut alias_rows = vec![
        "pxd_accession\thosting_repository\tnative_accessions\tpresent_in_primary_snapshot\tregistry_json_path\tdataset_title".to_string(),
    ];
    let spinner = make_spinner("enumerating ProteomeCentral PROXI datasets", opts.progress);
    let mut page_number = 1usize;

    loop {
        if opts.max_pages > 0 && page_number > opts.max_pages {
            return Err(anyhow!(
                "ProteomeCentral enumeration incomplete: reached --max-pages {} before an end-of-list response",
                opts.max_pages
            ));
        }
        if page_number > 1_000_000 {
            return Err(anyhow!(
                "aborting ProteomeCentral enumeration after implausibly many pages"
            ));
        }

        let url = format!(
            "{}/datasets?pageSize={page_size}&pageNumber={page_number}&resultType=full",
            opts.api_base.trim_end_matches('/')
        );
        let page_path = pages_dir.join(format!("page_{page_number:06}.json"));
        let state = fetch_cached(
            &client,
            &url,
            &page_path,
            opts.force,
            opts.retries,
            true,
            true,
        )
        .await
        .with_context(|| format!("enumerate ProteomeCentral page {page_number} from {url}"))?;
        match state {
            FetchState::Fetched => summary.pages_fetched += 1,
            FetchState::Cached => summary.pages_cached += 1,
            FetchState::NotFound => {
                summary.enumeration_termination = "not_found_page".to_string();
                break;
            }
        }

        let value: Value = pride_scp_core::read_json(&page_path)
            .with_context(|| format!("read ProteomeCentral page {}", page_path.display()))?;
        let entries = registry_dataset_entries(&value);
        if entries.is_empty() {
            summary.enumeration_termination = "empty_page".to_string();
            break;
        }

        summary.datasets_seen += entries.len();
        let mut page_pxd_count = 0usize;
        for dataset in &entries {
            let pxd_accessions = registry_pxd_accessions(dataset);
            if pxd_accessions.is_empty() {
                continue;
            }
            summary.datasets_with_pxd_alias += 1;
            page_pxd_count += pxd_accessions.len();
            let natives = registry_native_accessions(dataset);
            native_aliases.extend(natives.iter().cloned());
            let repository = infer_registry_repository(dataset, &natives);
            let title = first_string_for_keys(dataset, &["title", "datasetTitle", "name"])
                .replace('\t', " ")
                .replace('\n', " ");
            let native_joined = natives.join("; ");

            for accession in pxd_accessions {
                let is_new = seen_pxd.insert(accession.clone());
                if !is_new {
                    continue;
                }
                let present_primary = primary_accessions.contains(&accession);
                if present_primary {
                    summary.primary_snapshot_overlaps += 1;
                } else {
                    summary.supplemental_pxd_accessions += 1;
                    supplemental_pxd.insert(accession.clone());
                }

                let path = projects_dir.join(format!("{accession}.json"));
                let registry_json_path = if present_primary {
                    String::new()
                } else {
                    let normalized = normalized_registry_dataset(&accession, dataset);
                    if opts.force || !path.is_file() {
                        write_json(&path, &normalized).with_context(|| {
                            format!("write normalized registry record {}", path.display())
                        })?;
                        summary.normalized_records_written += 1;
                    }
                    path.display().to_string()
                };
                alias_rows.push(format!(
                    "{}\t{}\t{}\t{}\t{}\t{}",
                    accession,
                    repository,
                    native_joined.replace('\t', " "),
                    present_primary,
                    registry_json_path,
                    title,
                ));
            }
        }

        spinner.set_message(format!(
            "ProteomeCentral page {} | datasets={} | PXD aliases={} | unique PXD={} | supplements={}",
            page_number,
            entries.len(),
            page_pxd_count,
            seen_pxd.len(),
            summary.supplemental_pxd_accessions,
        ));

        if entries.len() < page_size {
            summary.enumeration_termination = "short_page".to_string();
            break;
        }
        page_number = page_number.saturating_add(1);
    }

    // Reconcile generated supplement files only after a complete enumeration.
    // This prevents a removed/reclassified registry alias from surviving as a
    // stale discovery candidate across --force refreshes. Primary overlaps are
    // intentionally never materialized in this directory.
    for entry in std::fs::read_dir(&projects_dir)? {
        let path = entry?.path();
        if path.extension().and_then(|x| x.to_str()) != Some("json") {
            continue;
        }
        let Some(accession) = accession_from_named_file(&path) else {
            continue;
        };
        if !supplemental_pxd.contains(&accession) {
            std::fs::remove_file(&path)
                .with_context(|| format!("remove stale registry record {}", path.display()))?;
            summary.stale_normalized_records_removed += 1;
        }
    }

    summary.unique_pxd_accessions = seen_pxd.len();
    summary.native_aliases_observed = native_aliases.len();
    if summary.enumeration_termination.is_empty() {
        summary.enumeration_termination = "unknown".to_string();
    }

    let accessions_text = if seen_pxd.is_empty() {
        String::new()
    } else {
        format!(
            "{}\n",
            seen_pxd.iter().cloned().collect::<Vec<_>>().join("\n")
        )
    };
    tokio::fs::write(registry_dir.join("accessions.txt"), accessions_text)
        .await
        .context("write registry accessions.txt")?;
    tokio::fs::write(
        registry_dir.join("registry_accessions.tsv"),
        format!("{}\n", alias_rows.join("\n")),
    )
    .await
    .context("write registry_accessions.tsv")?;
    write_json(&registry_dir.join("registry_summary.json"), &summary)?;

    spinner.finish_with_message(format!(
        "ProteomeCentral registry complete | {} PXD aliases | {} supplemental",
        summary.unique_pxd_accessions, summary.supplemental_pxd_accessions
    ));
    log::info!(
        "registry snapshot complete in {:.1}s: datasets={} unique_pxd={} overlaps={} supplements={} pages_fetched={} cached={}",
        started.elapsed().as_secs_f64(),
        summary.datasets_seen,
        summary.unique_pxd_accessions,
        summary.primary_snapshot_overlaps,
        summary.supplemental_pxd_accessions,
        summary.pages_fetched,
        summary.pages_cached,
    );
    Ok(summary)
}

fn file_size(path: &Path) -> u64 {
    std::fs::metadata(path).map(|meta| meta.len()).unwrap_or(0)
}

fn accession_from_named_file(path: &Path) -> Option<String> {
    let name = path.file_name()?.to_str()?;
    let accession = name.split('.').next()?.trim().to_ascii_uppercase();
    is_pxd(&accession).then_some(accession)
}

pub fn compact_snapshot(opts: CompactSnapshotOptions) -> Result<CompactSnapshotSummary> {
    let started = Instant::now();
    let snapshot_dir = opts.snapshot_dir.clone();
    let accessions_path = snapshot_dir.join("accessions.txt");
    let projects_dir = snapshot_dir.join("projects");
    let pages_dir = snapshot_dir.join("project_pages");
    let files_dir = snapshot_dir.join("files");
    let sdrf_dir = snapshot_dir.join("sdrf");

    let accessions = read_nonempty_lines(&accessions_path)
        .with_context(|| {
            format!(
                "validate snapshot accession index {}",
                accessions_path.display()
            )
        })?
        .into_iter()
        .map(|x| x.to_ascii_uppercase())
        .collect::<BTreeSet<_>>();
    let project_records = if projects_dir.is_dir() {
        std::fs::read_dir(&projects_dir)?
            .filter_map(|entry| entry.ok())
            .filter(|entry| entry.path().extension().and_then(|x| x.to_str()) == Some("json"))
            .count()
    } else {
        0
    };

    if project_records < accessions.len() {
        return Err(anyhow!(
            "snapshot compaction refused: only {} materialized project records for {} indexed accessions",
            project_records,
            accessions.len()
        ));
    }

    let mut page_files = if pages_dir.is_dir() {
        std::fs::read_dir(&pages_dir)?
            .filter_map(|entry| entry.ok().map(|x| x.path()))
            .filter(|path| path.is_file())
            .collect::<Vec<_>>()
    } else {
        Vec::new()
    };
    page_files.sort();
    let latest_page = page_files
        .iter()
        .filter(|path| path.extension().and_then(|x| x.to_str()) == Some("json"))
        .max()
        .cloned();

    let retain_accessions: HashSet<String> = if opts.prune_noncandidate_evidence {
        let path = opts.retain_accessions_file.as_ref().ok_or_else(|| {
            anyhow!("--prune-noncandidate-evidence requires --retain-accessions-file")
        })?;
        read_nonempty_lines(path)?
            .into_iter()
            .map(|x| x.to_ascii_uppercase())
            .collect()
    } else {
        HashSet::new()
    };

    let total_items = page_files.len()
        + if opts.prune_noncandidate_evidence && files_dir.is_dir() {
            std::fs::read_dir(&files_dir)?.count()
        } else {
            0
        }
        + if opts.prune_noncandidate_evidence && sdrf_dir.is_dir() {
            std::fs::read_dir(&sdrf_dir)?.count()
        } else {
            0
        };
    let progress = make_progress_bar(
        total_items as u64,
        "compacting snapshot cache",
        opts.progress,
    );

    let mut summary = CompactSnapshotSummary {
        snapshot_dir: snapshot_dir.display().to_string(),
        accessions_indexed: accessions.len(),
        project_records,
        project_pages_found: page_files.len(),
        dry_run: opts.dry_run,
        validation_ok: true,
        note: if opts.keep_latest_project_page {
            "Materialized projects/accessions retained; newest catalogue page retained for provenance.".to_string()
        } else {
            "Materialized projects/accessions retained; redundant catalogue pages removed."
                .to_string()
        },
        ..Default::default()
    };

    for path in &page_files {
        let keep = opts.keep_latest_project_page
            && latest_page
                .as_ref()
                .map(|latest| latest == path)
                .unwrap_or(false);
        if keep {
            summary.project_pages_kept += 1;
        } else {
            summary.project_pages_removed += 1;
            summary.project_page_bytes_removed += file_size(path);
            if !opts.dry_run {
                std::fs::remove_file(path).with_context(|| {
                    format!("remove redundant catalogue page {}", path.display())
                })?;
            }
        }
        progress.inc(1);
    }

    if opts.prune_noncandidate_evidence {
        for (dir, is_sdrf) in [(&files_dir, false), (&sdrf_dir, true)] {
            if !dir.is_dir() {
                continue;
            }
            for entry in std::fs::read_dir(dir)? {
                let path = entry?.path();
                if !path.is_file() {
                    continue;
                }
                let keep = accession_from_named_file(&path)
                    .map(|accession| retain_accessions.contains(&accession))
                    .unwrap_or(true);
                if !keep {
                    let bytes = file_size(&path);
                    summary.evidence_bytes_removed += bytes;
                    if is_sdrf {
                        summary.sdrf_payloads_removed += 1;
                    } else {
                        summary.file_manifests_removed += 1;
                    }
                    if !opts.dry_run {
                        std::fs::remove_file(&path).with_context(|| {
                            format!("remove non-candidate evidence {}", path.display())
                        })?;
                    }
                }
                progress.inc(1);
            }
        }
    }

    progress.finish_with_message("snapshot compaction complete");
    if !opts.dry_run {
        write_json(
            &snapshot_dir.join("snapshot_compaction_summary.json"),
            &summary,
        )?;
    }
    log::info!(
        "snapshot compaction complete in {:.1}s: pages_removed={} page_bytes_removed={} evidence_bytes_removed={}",
        started.elapsed().as_secs_f64(),
        summary.project_pages_removed,
        summary.project_page_bytes_removed,
        summary.evidence_bytes_removed,
    );
    Ok(summary)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn compact_snapshot_removes_redundant_pages_but_keeps_materialized_projects() {
        let root = std::env::temp_dir().join(format!(
            "pride_scp_compact_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(root.join("projects")).unwrap();
        std::fs::create_dir_all(root.join("project_pages")).unwrap();
        std::fs::write(root.join("accessions.txt"), "PXD000001\nPXD000002\n").unwrap();
        std::fs::write(root.join("projects/PXD000001.json"), "{}").unwrap();
        std::fs::write(root.join("projects/PXD000002.json"), "{}").unwrap();
        std::fs::write(root.join("project_pages/page_000000.json"), "[1]").unwrap();
        std::fs::write(root.join("project_pages/page_000001.json"), "[2]").unwrap();

        let summary = compact_snapshot(CompactSnapshotOptions {
            snapshot_dir: root.clone(),
            keep_latest_project_page: true,
            retain_accessions_file: None,
            prune_noncandidate_evidence: false,
            dry_run: false,
            progress: false,
        })
        .expect("snapshot compaction should succeed");
        assert_eq!(summary.project_pages_found, 2);
        assert_eq!(summary.project_pages_removed, 1);
        assert_eq!(summary.project_pages_kept, 1);
        assert!(root.join("project_pages/page_000001.json").is_file());
        assert!(!root.join("project_pages/page_000000.json").exists());
        assert!(root.join("projects/PXD000001.json").is_file());
        assert!(root.join("projects/PXD000002.json").is_file());

        let _ = std::fs::remove_dir_all(root);
    }

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

    #[test]
    fn detects_monolithic_catalogue_response_without_pagination_metadata() {
        let projects = (0..250)
            .map(|i| json!({"accession": format!("PXD{i:06}"), "title": "example"}))
            .collect::<Vec<_>>();
        let value = Value::Array(projects);
        assert!(catalogue_response_is_monolithic(&value, 100));
    }

    #[test]
    fn does_not_call_normal_paginated_response_monolithic() {
        let projects = (0..100)
            .map(|i| json!({"accession": format!("PXD{i:06}")}))
            .collect::<Vec<_>>();
        let value = json!({
            "content": projects,
            "totalPages": 400,
            "last": false
        });
        assert!(!catalogue_response_is_monolithic(&value, 100));
    }
    #[test]
    fn proteomecentral_dataset_extracts_secondary_pxd_and_native_massive_alias() {
        let dataset = json!({
            "title": "Single cell proteomics and epiproteomics",
            "description": "Single cells were isolated by FACS and analyzed by timsTOF SCP.",
            "identifiers": [
                {"accession": "MS:1001919", "name": "ProteomeXchange accession", "value": "PXD047101"},
                {"accession": "MS:1002634", "name": "MassIVE dataset identifier", "value": "MSV000093434"}
            ],
            "fullDatasetLinks": [
                {"accession": "MS:1002846", "name": "MassIVE dataset URI", "value": "ftp://massive.ucsd.edu/MSV000093434/"}
            ]
        });
        assert_eq!(registry_pxd_accessions(&dataset), vec!["PXD047101"]);
        assert!(registry_native_accessions(&dataset).contains(&"MSV000093434".to_string()));
        assert_eq!(
            infer_registry_repository(&dataset, &registry_native_accessions(&dataset)),
            "MassIVE"
        );

        let normalized = normalized_registry_dataset("PXD047101", &dataset);
        assert_eq!(normalized["accession"], "PXD047101");
        assert_eq!(normalized["registryHostingRepository"], "MassIVE");
        assert_eq!(normalized["registryNativeAccessions"][0], "MSV000093434");
    }

    #[test]
    fn proteomecentral_dataset_entries_accept_array_and_common_wrappers() {
        let a = json!([{"title": "a"}, {"title": "b"}]);
        assert_eq!(registry_dataset_entries(&a).len(), 2);

        let wrapped = json!({"datasets": [{"title": "a"}]});
        assert_eq!(registry_dataset_entries(&wrapped).len(), 1);

        let nested = json!({"data": {"results": [{"title": "a"}]}});
        assert_eq!(registry_dataset_entries(&nested).len(), 1);
    }
}
