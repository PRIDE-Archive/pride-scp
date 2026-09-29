use anyhow::{anyhow, bail, Context, Result};
use percent_encoding::percent_decode_str;
use regex::Regex;
use reqwest::header::{CONTENT_LENGTH, CONTENT_RANGE, RANGE};
use reqwest::{Client, StatusCode};
use serde::{Deserialize, Serialize};
use std::cmp::Reverse;
use std::collections::{hash_map::DefaultHasher, HashSet, VecDeque};
use std::hash::{Hash, Hasher};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::fs::{self, OpenOptions};
use tokio::io::AsyncWriteExt;
use tokio::sync::Mutex;
use tokio::task::JoinSet;
use url::Url;

const DEFAULT_CONNECT_TIMEOUT_SECONDS: u64 = 30;
const PLAN_VERSION: &str = "pride-scp-transfer-plan-v5";

#[derive(Debug, Clone)]
pub struct TransferOptions {
    pub source: Option<String>,
    pub source_list: Option<PathBuf>,
    pub destination_dir: PathBuf,
    pub jobs: usize,
    pub metadata_jobs: usize,
    pub metadata_timeout_seconds: u64,
    pub retries: usize,
    pub user_agent: String,
    pub max_depth: Option<usize>,
    pub include_regex: Vec<String>,
    pub exclude_regex: Vec<String>,
    pub limit: usize,
    pub resume: bool,
    pub force: bool,
    pub dry_run: bool,
    pub manifest_out: Option<PathBuf>,
    pub curl_binary: String,
    /// FTP TLS policy: auto, required, or off. Auto requires explicit TLS for MassIVE.
    pub ftp_tls_mode: String,
    /// Disable TLS certificate verification for FTP(S). Explicit opt-in only.
    pub ftp_insecure_tls: bool,
    /// Optional CA certificate bundle/file passed to curl for FTP(S) verification.
    pub ftp_ca_cert: Option<PathBuf>,
    /// Force IPv4 for FTP(S). MassIVE enables this automatically.
    pub ftp_force_ipv4: bool,
    /// Disable EPSV and use PASV for FTP(S). MassIVE enables this automatically.
    pub ftp_disable_epsv: bool,
    /// FTP directory-listing strategy: auto, mlsd, or list. Auto prefers MLSD for MassIVE.
    pub ftp_listing_mode: String,
    /// Maximum concurrent FTP directory-listing control sessions. Kept separate from metadata_jobs.
    pub ftp_listing_jobs: usize,
    /// Minimum spacing between FTP directory-listing connection attempts.
    pub ftp_listing_interval_ms: u64,
    /// Base delay for exponential backoff after transient FTP listing failures.
    pub ftp_retry_backoff_seconds: u64,
    pub slurm: Option<SlurmOptions>,
    pub progress: bool,
}

#[derive(Debug, Clone)]
pub struct SlurmOptions {
    pub submit: bool,
    pub jobs: usize,
    pub script_dir: Option<PathBuf>,
    pub job_name: String,
    pub time: String,
    pub mem: String,
    pub cpus_per_task: usize,
    pub partition: Option<String>,
    pub account: Option<String>,
    pub qos: Option<String>,
    pub constraint: Option<String>,
    pub sbatch_args: Vec<String>,
    pub sbatch_binary: String,
    pub curl_binary: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct TransferItem {
    pub url: String,
    pub relative_path: String,
    pub remote_bytes: Option<u64>,
    pub local_bytes: u64,
    pub bytes_remaining: Option<u64>,
    pub complete: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TransferPlan {
    pub plan_version: String,
    pub source: String,
    pub destination: String,
    pub files: Vec<TransferItem>,
}

#[derive(Debug, Clone, Serialize)]
pub struct TransferSummary {
    pub plan_version: String,
    pub mode: String,
    pub source: String,
    pub destination: String,
    pub discovered_files: usize,
    pub selected_files: usize,
    pub files_to_transfer: usize,
    pub already_complete_files: usize,
    pub known_remote_bytes: u64,
    pub known_remote_size: String,
    pub unknown_remote_size_files: usize,
    pub existing_local_bytes: u64,
    pub existing_local_size: String,
    pub known_bytes_to_transfer: u64,
    pub known_size_to_transfer: String,
    pub downloaded_files: usize,
    pub downloaded_bytes: u64,
    pub manifest: Option<String>,
    pub slurm_scripts: Vec<String>,
    pub slurm_job_ids: Vec<String>,
}

#[derive(Debug, Clone)]
struct DiscoveredFile {
    url: Url,
    relative_path: String,
    remote_bytes_hint: Option<u64>,
}

#[derive(Debug, Clone)]
struct FtpEntry {
    name: String,
    is_dir: bool,
    size: Option<u64>,
}

#[derive(Debug, Clone, Default)]
struct DownloadTotals {
    files: usize,
    bytes: u64,
}

pub async fn transfer(options: TransferOptions) -> Result<TransferSummary> {
    validate_options(&options)?;
    let client = Client::builder()
        .connect_timeout(Duration::from_secs(DEFAULT_CONNECT_TIMEOUT_SECONDS))
        .user_agent(options.user_agent.clone())
        .build()
        .context("build HTTP transfer client")?;

    if options.progress {
        log::info!("discovering transfer source {}", transfer_source_label(&options));
    }
    let (discovered_count, mut files) = discover_files(&client, &options).await?;
    files.sort_by(|a, b| a.relative_path.cmp(&b.relative_path));
    if options.limit > 0 && files.len() > options.limit {
        files.truncate(options.limit);
    }

    if options.progress {
        log::info!(
            "probing remote sizes for {} selected files with {} metadata workers",
            files.len(),
            options.metadata_jobs
        );
    }
    let mut plan = build_plan(&client, &options, files).await?;
    plan.files.sort_by(|a, b| a.relative_path.cmp(&b.relative_path));

    if !options.dry_run && !options.force {
        if let Some(item) = plan.files.iter().find(|item| {
            item.remote_bytes
                .map_or(false, |remote| item.local_bytes > remote)
        }) {
            bail!(
                "local file {} is larger than remote ({} > {}); rerun with --force to replace oversized local files",
                safe_destination(&options.destination_dir, &item.relative_path)?.display(),
                item.local_bytes,
                item.remote_bytes.unwrap_or(0)
            );
        }
    }

    let mut manifest = None;
    if let Some(path) = options.manifest_out.as_ref() {
        write_manifest(path, &plan).await?;
        manifest = Some(path.display().to_string());
    }

    let mut downloaded = DownloadTotals::default();
    let mut slurm_scripts = Vec::new();
    let mut slurm_job_ids = Vec::new();
    let mode;

    if options.dry_run {
        mode = "dry-run".to_owned();
    } else if let Some(slurm) = options.slurm.as_ref() {
        let result = prepare_slurm(&options, &plan, slurm).await?;
        if manifest.is_none() {
            manifest = Some(result.manifest.display().to_string());
        }
        slurm_scripts = result
            .scripts
            .iter()
            .map(|path| path.display().to_string())
            .collect();
        slurm_job_ids = result.job_ids;
        mode = if slurm.submit {
            "slurm-submit".to_owned()
        } else {
            "slurm-generate".to_owned()
        };
    } else {
        mode = "direct".to_owned();
        downloaded = download_direct(&client, &options, &plan).await?;
    }

    let known_remote_bytes = plan.files.iter().filter_map(|f| f.remote_bytes).sum();
    let unknown_remote_size_files = plan.files.iter().filter(|f| f.remote_bytes.is_none()).count();
    let existing_local_bytes = plan.files.iter().map(|f| f.local_bytes).sum();
    let known_bytes_to_transfer = plan.files.iter().filter_map(|f| f.bytes_remaining).sum();
    let already_complete_files = plan.files.iter().filter(|f| f.complete).count();
    let files_to_transfer = plan.files.len().saturating_sub(already_complete_files);

    Ok(TransferSummary {
        plan_version: PLAN_VERSION.to_owned(),
        mode,
        source: plan.source,
        destination: plan.destination,
        discovered_files: discovered_count,
        selected_files: plan.files.len(),
        files_to_transfer,
        already_complete_files,
        known_remote_bytes,
        known_remote_size: format_bytes(known_remote_bytes),
        unknown_remote_size_files,
        existing_local_bytes,
        existing_local_size: format_bytes(existing_local_bytes),
        known_bytes_to_transfer,
        known_size_to_transfer: format_bytes(known_bytes_to_transfer),
        downloaded_files: downloaded.files,
        downloaded_bytes: downloaded.bytes,
        manifest,
        slurm_scripts,
        slurm_job_ids,
    })
}

fn format_bytes(bytes: u64) -> String {
    const UNITS: [&str; 6] = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    if bytes < 1024 {
        return format!("{bytes} B");
    }
    let mut value = bytes as f64;
    let mut unit = 0usize;
    while value >= 1024.0 && unit + 1 < UNITS.len() {
        value /= 1024.0;
        unit += 1;
    }
    format!("{value:.2} {}", UNITS[unit])
}

fn validate_options(options: &TransferOptions) -> Result<()> {
    if options.jobs == 0 {
        bail!("--jobs must be at least 1");
    }
    if options.metadata_jobs == 0 {
        bail!("--metadata-jobs must be at least 1");
    }
    if options.metadata_timeout_seconds == 0 {
        bail!("--metadata-timeout must be at least 1 second");
    }
    match (&options.source, &options.source_list) {
        (Some(source), None) => {
            validate_source_url(source)?;
        }
        (None, Some(_)) => {}
        (Some(_), Some(_)) => bail!("use either --source or --source-list, not both"),
        (None, None) => bail!("one of --source or --source-list is required"),
    }
    if options.curl_binary.trim().is_empty() {
        bail!("--curl-binary must not be empty");
    }
    if !matches!(options.ftp_tls_mode.as_str(), "auto" | "required" | "off") {
        bail!("--ftp-tls must be one of: auto, required, off");
    }
    if options.ftp_insecure_tls && options.ftp_ca_cert.is_some() {
        bail!("--ftp-insecure-tls cannot be combined with --ftp-ca-cert");
    }
    if !matches!(options.ftp_listing_mode.as_str(), "auto" | "mlsd" | "list") {
        bail!("--ftp-listing must be one of: auto, mlsd, list");
    }
    if options.ftp_listing_jobs == 0 {
        bail!("--ftp-listing-jobs must be at least 1");
    }
    if options.ftp_retry_backoff_seconds == 0 {
        bail!("--ftp-retry-backoff-seconds must be at least 1");
    }
    compile_patterns(&options.include_regex, "--include-regex")?;
    compile_patterns(&options.exclude_regex, "--exclude-regex")?;
    if options.dry_run && options.slurm.is_some() {
        bail!("--dry-run cannot be combined with Slurm generation/submission");
    }
    if let Some(slurm) = options.slurm.as_ref() {
        if slurm.jobs == 0 {
            bail!("--slurm-jobs must be at least 1");
        }
        if slurm.cpus_per_task == 0 {
            bail!("--slurm-cpus-per-task must be at least 1");
        }
        if slurm.job_name.is_empty()
            || !slurm
                .job_name
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'))
        {
            bail!("--slurm-job-name may contain only letters, numbers, '.', '_' and '-'");
        }
    }
    Ok(())
}

fn validate_source_url(source: &str) -> Result<Url> {
    let parsed = Url::parse(source).with_context(|| format!("invalid transfer source URL: {source}"))?;
    if !matches!(parsed.scheme(), "http" | "https" | "ftp" | "ftps") {
        bail!("transfer sources support http://, https://, ftp:// and ftps:// URLs");
    }
    Ok(parsed)
}

fn compile_patterns(patterns: &[String], label: &str) -> Result<Vec<Regex>> {
    patterns
        .iter()
        .map(|pattern| {
            Regex::new(pattern).with_context(|| format!("invalid {label} pattern {pattern:?}"))
        })
        .collect()
}

async fn discover_files(
    client: &Client,
    options: &TransferOptions,
) -> Result<(usize, Vec<DiscoveredFile>)> {
    let sources = load_sources(options).await?;
    let include = compile_patterns(&options.include_regex, "--include-regex")?;
    let exclude = compile_patterns(&options.exclude_regex, "--exclude-regex")?;
    let prefix_http_sources = options.source_list.is_some() || sources.len() > 1;

    let mut discovered_count = 0usize;
    let mut discovered = Vec::new();
    let mut target_paths = HashSet::new();

    for source_text in sources {
        let source = validate_source_url(&source_text)?;
        let (source_count, source_files) = match source.scheme() {
            "ftp" | "ftps" => {
                discover_ftp_files(options, source, &include, &exclude).await?
            }
            "http" | "https" if is_massive_dataset_page(&source) => {
                let ftp_root = resolve_massive_ftp_root(
                    client,
                    &source,
                    options.metadata_timeout_seconds,
                    options.retries,
                )
                .await?;
                if options.progress {
                    log::info!("resolved MassIVE dataset page {} -> {}", source, ftp_root);
                }
                discover_ftp_files(options, ftp_root, &include, &exclude).await?
            }
            "http" | "https" => {
                let prefix = prefix_http_sources.then(|| source_prefix_from_url(&source));
                discover_http_files(client, options, source_text, prefix, &include, &exclude).await?
            }
            _ => unreachable!("source scheme validated above"),
        };
        discovered_count = discovered_count.saturating_add(source_count);
        for file in source_files {
            if !target_paths.insert(file.relative_path.clone()) {
                bail!(
                    "multiple sources map to the same destination-relative path {:?}; use distinct dataset roots",
                    file.relative_path
                );
            }
            discovered.push(file);
        }
    }

    Ok((discovered_count, discovered))
}

async fn load_sources(options: &TransferOptions) -> Result<Vec<String>> {
    if let Some(source) = options.source.as_ref() {
        return Ok(vec![source.clone()]);
    }
    let path = options
        .source_list
        .as_ref()
        .ok_or_else(|| anyhow!("one of --source or --source-list is required"))?;
    let text = fs::read_to_string(path)
        .await
        .with_context(|| format!("read source list {}", path.display()))?;
    let mut sources = Vec::new();
    let mut seen = HashSet::new();
    for (index, line) in text.lines().enumerate() {
        let source = line.trim();
        if source.is_empty() || source.starts_with('#') {
            continue;
        }
        validate_source_url(source)
            .with_context(|| format!("invalid source-list entry at {}:{}", path.display(), index + 1))?;
        if seen.insert(source.to_owned()) {
            sources.push(source.to_owned());
        }
    }
    if sources.is_empty() {
        bail!("source list {} contains no transfer sources", path.display());
    }
    Ok(sources)
}

fn transfer_source_label(options: &TransferOptions) -> String {
    options
        .source
        .clone()
        .or_else(|| options.source_list.as_ref().map(|path| format!("@{}", path.display())))
        .unwrap_or_else(|| "<missing>".to_owned())
}

async fn discover_http_files(
    client: &Client,
    options: &TransferOptions,
    source_text: String,
    relative_prefix: Option<String>,
    include: &[Regex],
    exclude: &[Regex],
) -> Result<(usize, Vec<DiscoveredFile>)> {
    let source = resolve_source_url(
        client,
        &source_text,
        options.metadata_timeout_seconds,
        options.retries,
    )
    .await?;

    if !source.path().ends_with('/') {
        let relative_path = prefixed_path(relative_prefix.as_deref(), &file_name_from_url(&source)?);
        if selected(&relative_path, include, exclude) {
            return Ok((
                1,
                vec![DiscoveredFile {
                    url: source,
                    relative_path,
                    remote_bytes_hint: None,
                }],
            ));
        }
        return Ok((1, Vec::new()));
    }

    let root = source;
    let mut queue = VecDeque::from([(root.clone(), 0usize)]);
    let mut visited_dirs = HashSet::new();
    let mut seen_files = HashSet::new();
    let mut discovered = Vec::new();
    let href_re = Regex::new(r#"(?i)href\s*=\s*["']([^"'#]+)["']"#).expect("href regex");

    while let Some((directory, depth)) = queue.pop_front() {
        if !visited_dirs.insert(directory.as_str().to_owned()) {
            continue;
        }
        let html = get_text_with_retry(
            client,
            directory.clone(),
            options.metadata_timeout_seconds,
            options.retries,
        )
        .await
        .with_context(|| format!("read directory listing {directory}"))?;

        for caps in href_re.captures_iter(&html) {
            let Some(raw_href) = caps.get(1).map(|m| m.as_str().trim()) else {
                continue;
            };
            if raw_href.is_empty()
                || raw_href == "../"
                || raw_href.starts_with('?')
                || raw_href.starts_with('#')
            {
                continue;
            }
            let Ok(mut link) = directory.join(raw_href) else {
                continue;
            };
            link.set_fragment(None);
            link.set_query(None);
            if !same_origin_under_root(&root, &link) || link == root {
                continue;
            }
            let relative = match relative_path_from_url(&root, &link) {
                Ok(path) => path,
                Err(_) => continue,
            };
            if relative.is_empty() {
                continue;
            }
            let relative = prefixed_path(relative_prefix.as_deref(), &relative);
            if link.path().ends_with('/') || raw_href.ends_with('/') {
                let directory_key = format!("{relative}/");
                if matches_any(&directory_key, exclude) {
                    continue;
                }
                let next_depth = depth + 1;
                if options.max_depth.map_or(true, |max_depth| next_depth <= max_depth) {
                    queue.push_back((link, next_depth));
                }
                continue;
            }
            if !seen_files.insert(link.as_str().to_owned()) {
                continue;
            }
            if selected(&relative, include, exclude) {
                discovered.push(DiscoveredFile {
                    url: link,
                    relative_path: relative,
                    remote_bytes_hint: None,
                });
            }
        }
    }

    Ok((seen_files.len(), discovered))
}

fn is_massive_dataset_page(url: &Url) -> bool {
    let host = url.host_str().unwrap_or_default().to_ascii_lowercase();
    (host == "massive.ucsd.edu"
        || host == "proteomics.ucsd.edu"
        || host.ends_with(".massive.ucsd.edu"))
        && url.path().ends_with("/ProteoSAFe/dataset.jsp")
}

async fn resolve_massive_ftp_root(
    client: &Client,
    dataset_page: &Url,
    timeout_seconds: u64,
    retries: usize,
) -> Result<Url> {
    let html = get_text_with_retry(client, dataset_page.clone(), timeout_seconds, retries)
        .await
        .with_context(|| format!("read MassIVE dataset page {dataset_page}"))?;
    let ftp_re = Regex::new(
        r#"(?i)ftps?://massive-ftp\.ucsd\.edu/[A-Za-z0-9._~%/+-]*/MSV\d{9}/?"#,
    )
    .expect("MassIVE FTP URL regex");
    if let Some(found) = ftp_re.find(&html) {
        let mut text = found.as_str().to_owned();
        if !text.ends_with('/') {
            text.push('/');
        }
        return Url::parse(&text).context("parse MassIVE FTP root from dataset page");
    }
    let accession_re = Regex::new(r"MSV\d{9}").expect("MassIVE accession regex");
    let accession = accession_re
        .find(&html)
        .map(|m| m.as_str())
        .unwrap_or("unknown accession");
    bail!(
        "MassIVE dataset page {} identifies {} but does not expose a parseable FTP root; put its ftp://massive-ftp.ucsd.edu/.../MSV.../ root in --source-list",
        dataset_page,
        accession
    )
}

#[derive(Debug)]
struct FtpListingGate {
    min_interval: Duration,
    next_allowed: Mutex<Instant>,
}

impl FtpListingGate {
    fn new(min_interval: Duration) -> Self {
        Self {
            min_interval,
            next_allowed: Mutex::new(Instant::now()),
        }
    }

    async fn wait(&self) {
        let mut next_allowed = self.next_allowed.lock().await;
        let now = Instant::now();
        if *next_allowed > now {
            tokio::time::sleep(*next_allowed - now).await;
        }
        *next_allowed = Instant::now() + self.min_interval;
    }
}

async fn discover_ftp_files(
    options: &TransferOptions,
    mut source: Url,
    include: &[Regex],
    exclude: &[Regex],
) -> Result<(usize, Vec<DiscoveredFile>)> {
    if !source.path().ends_with('/') {
        let last = file_name_from_url(&source)?;
        let accession_re = Regex::new(r"^MSV\d{9}$").expect("MassIVE accession regex");
        if accession_re.is_match(&last) {
            let mut path = source.path().to_owned();
            path.push('/');
            source.set_path(&path);
        } else {
            if selected(&last, include, exclude) {
                return Ok((
                    1,
                    vec![DiscoveredFile {
                        url: source,
                        relative_path: last,
                        remote_bytes_hint: None,
                    }],
                ));
            }
            return Ok((1, Vec::new()));
        }
    }

    let prefix = source_prefix_from_url(&source);
    let root = source;
    let listing_gate = Arc::new(FtpListingGate::new(Duration::from_millis(
        options.ftp_listing_interval_ms,
    )));
    if options.progress {
        log::info!(
            "FTP recursive discovery using {} listing worker(s), {} ms minimum connection spacing, {} s retry backoff base",
            options.ftp_listing_jobs,
            options.ftp_listing_interval_ms,
            options.ftp_retry_backoff_seconds
        );
    }
    let mut queue = VecDeque::from([(root.clone(), 0usize)]);
    let mut visited_dirs = HashSet::new();
    let mut seen_files = HashSet::new();
    let mut discovered = Vec::new();

    while !queue.is_empty() {
        let mut batch = Vec::new();
        while batch.len() < options.ftp_listing_jobs {
            let Some((directory, depth)) = queue.pop_front() else {
                break;
            };
            if visited_dirs.insert(directory.as_str().to_owned()) {
                batch.push((directory, depth));
            }
        }
        if batch.is_empty() {
            continue;
        }

        let mut listings = JoinSet::new();
        for (directory, depth) in batch {
            let worker_options = options.clone();
            let listing_gate = Arc::clone(&listing_gate);
            listings.spawn(async move {
                let result = ftp_list_directory(&worker_options, &directory, listing_gate).await;
                (directory, depth, result)
            });
        }

        while let Some(joined) = listings.join_next().await {
            let (directory, depth, entries) = joined.context("join FTP discovery worker")?;
            let entries = entries?;
            for entry in entries {
                if entry.name == "."
                    || entry.name == ".."
                    || entry.name.contains('/')
                    || entry.name.contains('\\')
                {
                    continue;
                }
                let child = join_url_path_segment(&directory, &entry.name, entry.is_dir)?;
                let suffix = relative_path_from_url(&root, &child)?;
                if suffix.is_empty() {
                    continue;
                }
                let relative = prefixed_path(Some(&prefix), &suffix);
                if entry.is_dir {
                    let directory_key = format!("{relative}/");
                    if matches_any(&directory_key, exclude) {
                        continue;
                    }
                    let next_depth = depth + 1;
                    if options
                        .max_depth
                        .map_or(true, |max_depth| next_depth <= max_depth)
                    {
                        queue.push_back((child, next_depth));
                    }
                    continue;
                }
                if !seen_files.insert(child.as_str().to_owned()) {
                    continue;
                }
                if selected(&relative, include, exclude) {
                    discovered.push(DiscoveredFile {
                        url: child,
                        relative_path: relative,
                        remote_bytes_hint: entry.size,
                    });
                }
            }
        }
    }

    Ok((seen_files.len(), discovered))
}

fn is_massive_ftp(url: &Url) -> bool {
    url.host_str()
        .map(|host| host.eq_ignore_ascii_case("massive-ftp.ucsd.edu"))
        .unwrap_or(false)
}

fn ftp_tls_required(options: &TransferOptions, url: &Url) -> bool {
    match options.ftp_tls_mode.as_str() {
        "required" => true,
        "off" => false,
        _ => is_massive_ftp(url) || url.scheme().eq_ignore_ascii_case("ftps"),
    }
}

fn configure_ftp_curl(command: &mut Command, options: &TransferOptions, url: &Url) {
    if ftp_tls_required(options, url) {
        // MassIVE currently requires explicit FTP-over-TLS on port 21.
        command.arg("--ssl-reqd");
    }
    if options.ftp_insecure_tls {
        command.arg("--insecure");
    } else if let Some(ca_cert) = options.ftp_ca_cert.as_ref() {
        command.arg("--cacert").arg(ca_cert);
    }
    if options.ftp_force_ipv4 || is_massive_ftp(url) {
        command.arg("-4");
    }
    if options.ftp_disable_epsv || is_massive_ftp(url) {
        command.args(["--disable-epsv", "--ftp-pasv"]);
    }
}

fn ftp_failure_message(url: &Url, options: &TransferOptions, stderr: &str) -> String {
    let stderr = stderr.trim();
    if stderr.contains("421 TLS is required") {
        return format!(
            "{stderr}\nserver requires explicit FTP-over-TLS; use --ftp-tls required (MassIVE uses this automatically in --ftp-tls auto)"
        );
    }
    if stderr.contains("SSL certificate problem") && !options.ftp_insecure_tls {
        return format!(
            "{stderr}\nTLS reached the FTP server but certificate verification failed. Prefer --ftp-ca-cert PATH with the appropriate CA chain; for an explicitly accepted public-data compatibility workaround, rerun with --ftp-insecure-tls"
        );
    }
    stderr.to_owned()
}

fn slurm_ftp_curl_args(options: &TransferOptions, url_text: &str) -> Result<String> {
    let url = Url::parse(url_text).with_context(|| format!("parse transfer URL {url_text}"))?;
    if !matches!(url.scheme(), "ftp" | "ftps") {
        return Ok(String::new());
    }
    let mut args = Vec::<String>::new();
    if ftp_tls_required(options, &url) {
        args.push("--ssl-reqd".to_owned());
    }
    if options.ftp_insecure_tls {
        args.push("--insecure".to_owned());
    } else if let Some(ca_cert) = options.ftp_ca_cert.as_ref() {
        args.push("--cacert".to_owned());
        args.push(shell_quote(&ca_cert.display().to_string()));
    }
    if options.ftp_force_ipv4 || is_massive_ftp(&url) {
        args.push("-4".to_owned());
    }
    if options.ftp_disable_epsv || is_massive_ftp(&url) {
        args.push("--disable-epsv".to_owned());
        args.push("--ftp-pasv".to_owned());
    }
    if args.is_empty() {
        Ok(String::new())
    } else {
        Ok(format!("{} ", args.join(" ")))
    }
}

async fn ftp_list_directory(
    options: &TransferOptions,
    directory: &Url,
    listing_gate: Arc<FtpListingGate>,
) -> Result<Vec<FtpEntry>> {
    let methods: Vec<&str> = match options.ftp_listing_mode.as_str() {
        "mlsd" => vec!["MLSD"],
        "list" => vec!["LIST"],
        _ if is_massive_ftp(directory) => vec!["MLSD", "LIST"],
        _ => vec!["LIST", "MLSD"],
    };
    let mut failures = Vec::new();

    for method in methods {
        let mut exhausted_transient = false;
        let mut method_failure = None;

        for attempt in 0..=options.retries {
            listing_gate.wait().await;

            let curl_binary = options.curl_binary.clone();
            let curl_label = curl_binary.clone();
            let url = directory.to_string();
            let timeout = options.metadata_timeout_seconds.to_string();
            let ftp_options = options.clone();
            let directory_url = directory.clone();
            let method_owned = method.to_owned();
            let output = tokio::task::spawn_blocking(move || {
                let mut command = Command::new(&curl_binary);
                command.args([
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--connect-timeout",
                    "30",
                    "--max-time",
                    &timeout,
                ]);
                configure_ftp_curl(&mut command, &ftp_options, &directory_url);
                if method_owned == "MLSD" {
                    // RFC 3659 machine-readable listing. SFTPGo advertises MLSD/MLST support.
                    command.args(["--request", "MLSD"]);
                }
                command.arg(&url).output()
            })
            .await
            .context("join FTP directory-listing worker")?
            .with_context(|| {
                format!("run {curl_label} for FTP {method} directory listing {directory}")
            })?;

            if !output.status.success() {
                let code = output.status.code();
                let failure = ftp_failure_message(
                    directory,
                    options,
                    &String::from_utf8_lossy(&output.stderr),
                );
                let transient = is_transient_ftp_listing_exit(code);
                method_failure = Some(format!(
                    "{method}: curl exit {}: {}",
                    code.map_or_else(|| "signal".to_owned(), |value| value.to_string()),
                    failure
                ));

                if transient && attempt < options.retries {
                    let delay = ftp_listing_retry_delay(options, directory, method, attempt);
                    log::warn!(
                        "transient FTP {} listing failure for {} (attempt {}/{}); retrying after {:.2}s",
                        method,
                        directory,
                        attempt + 1,
                        options.retries + 1,
                        delay.as_secs_f64()
                    );
                    tokio::time::sleep(delay).await;
                    continue;
                }
                exhausted_transient = transient;
                break;
            }

            let listing = match String::from_utf8(output.stdout) {
                Ok(listing) => listing,
                Err(error) => {
                    method_failure = Some(format!(
                        "{method}: directory listing is not UTF-8: {error}"
                    ));
                    break;
                }
            };
            match parse_ftp_listing(&listing) {
                Ok(entries) if !entries.is_empty() || listing.trim().is_empty() => {
                    return Ok(entries)
                }
                Ok(_) => {
                    method_failure = Some(format!(
                        "{method}: no entries parsed; first response line: {:?}",
                        listing.lines().next().unwrap_or_default()
                    ));
                    break;
                }
                Err(error) => {
                    method_failure = Some(format!("{method}: {error:#}"));
                    break;
                }
            }
        }

        if let Some(failure) = method_failure {
            failures.push(failure);
        }

        // LIST will not repair a server that is refusing/throttling control connections.
        // Avoid immediately doubling the connection burst after MLSD exhausts transient retries.
        if exhausted_transient {
            break;
        }
    }

    let rate_limit_hint = if is_massive_ftp(directory) {
        " MassIVE appears to be refusing or dropping FTP control/data connections. Keep discovery conservative (default: --ftp-listing-jobs 1); if needed increase --ftp-listing-interval-ms or --ftp-retry-backoff-seconds."
    } else {
        ""
    };
    bail!(
        "FTP directory listing failed for {} after {}: {}{}",
        directory,
        options.ftp_listing_mode,
        failures.join(" | "),
        rate_limit_hint
    )
}

fn is_transient_ftp_listing_exit(code: Option<i32>) -> bool {
    matches!(code, Some(7 | 28 | 56))
}

fn ftp_listing_retry_delay(
    options: &TransferOptions,
    directory: &Url,
    method: &str,
    attempt: usize,
) -> Duration {
    let shift = (attempt as u32).min(4);
    let base = options
        .ftp_retry_backoff_seconds
        .saturating_mul(1u64 << shift)
        .min(60);
    let mut hasher = DefaultHasher::new();
    directory.as_str().hash(&mut hasher);
    method.hash(&mut hasher);
    attempt.hash(&mut hasher);
    let jitter_ms = hasher.finish() % 1_000;
    Duration::from_millis(base.saturating_mul(1_000).saturating_add(jitter_ms))
}

fn parse_ftp_listing(listing: &str) -> Result<Vec<FtpEntry>> {
    let unix_re = Regex::new(
        r"^([bcdlps-]\S*)\s+\d+\s+\S+\s+\S+\s+(\d+)\s+\S+\s+\S+\s+\S+\s+(.+)$",
    )?;
    let dos_re = Regex::new(
        r"^\d{2}-\d{2}-\d{2,4}\s+\d{2}:\d{2}(?:AM|PM)\s+(<DIR>|\d+)\s+(.+)$",
    )?;
    let mut entries = Vec::new();
    let mut unparsed = Vec::new();

    for raw_line in listing.lines() {
        let line = raw_line.trim_end_matches('\r');
        if line.trim().is_empty() || line.starts_with("total ") {
            continue;
        }

        // RFC 3659 MLSD facts can appear in any order; do not require `type=` first.
        if line.contains(';') {
            if let Some((facts, name)) = line.split_once(' ') {
                let name = name.trim().to_owned();
                let mut kind = None;
                let mut size = None;
                for fact in facts.split(';').filter(|fact| !fact.is_empty()) {
                    if let Some((key, value)) = fact.split_once('=') {
                        match key.to_ascii_lowercase().as_str() {
                            "type" => kind = Some(value.to_ascii_lowercase()),
                            "size" => size = value.parse::<u64>().ok(),
                            _ => {}
                        }
                    }
                }
                if let Some(kind) = kind.as_deref() {
                    match kind {
                        "dir" => entries.push(FtpEntry { name, is_dir: true, size: None }),
                        "file" => entries.push(FtpEntry { name, is_dir: false, size }),
                        "cdir" | "pdir" => {}
                        _ => {}
                    }
                    continue;
                }
            }
        }

        if let Some(caps) = unix_re.captures(line) {
            let mode = caps.get(1).map(|m| m.as_str()).unwrap_or_default();
            if mode.starts_with('l') {
                bail!("FTP directory listing contains a symbolic link, which is not followed implicitly: {line:?}");
            }
            if !mode.starts_with('d') && !mode.starts_with('-') {
                bail!("unsupported FTP directory-entry type: {line:?}");
            }
            let name = caps.get(3).map(|m| m.as_str()).unwrap_or_default().to_owned();
            let is_dir = mode.starts_with('d');
            let size = if is_dir {
                None
            } else {
                caps.get(2).and_then(|m| m.as_str().parse::<u64>().ok())
            };
            entries.push(FtpEntry { name, is_dir, size });
            continue;
        }

        if let Some(caps) = dos_re.captures(line) {
            let marker = caps.get(1).map(|m| m.as_str()).unwrap_or_default();
            let name = caps.get(2).map(|m| m.as_str()).unwrap_or_default().to_owned();
            let is_dir = marker.eq_ignore_ascii_case("<DIR>");
            let size = if is_dir { None } else { marker.parse::<u64>().ok() };
            entries.push(FtpEntry { name, is_dir, size });
            continue;
        }

        unparsed.push(line.to_owned());
    }
    if let Some(line) = unparsed.first() {
        bail!("unrecognized FTP directory-listing line: {line:?}");
    }
    Ok(entries)
}

fn join_url_path_segment(directory: &Url, name: &str, is_dir: bool) -> Result<Url> {
    let mut child = directory.clone();
    child.set_query(None);
    child.set_fragment(None);
    {
        let mut segments = child
            .path_segments_mut()
            .map_err(|_| anyhow!("URL cannot be a directory base: {directory}"))?;
        segments.pop_if_empty();
        segments.push(name);
        if is_dir {
            segments.push("");
        }
    }
    Ok(child)
}

fn source_prefix_from_url(source: &Url) -> String {
    source
        .path_segments()
        .and_then(|segments| segments.filter(|segment| !segment.is_empty()).last())
        .and_then(|segment| decode_path_segment(segment).ok())
        .filter(|segment| !segment.is_empty())
        .unwrap_or_else(|| source.host_str().unwrap_or("source").to_owned())
}

fn prefixed_path(prefix: Option<&str>, relative: &str) -> String {
    match prefix {
        Some(prefix) if !prefix.is_empty() => format!("{prefix}/{relative}"),
        _ => relative.to_owned(),
    }
}

async fn resolve_source_url(
    client: &Client,
    source: &str,
    timeout_seconds: u64,
    retries: usize,
) -> Result<Url> {
    let source_url = Url::parse(source)?;
    if source_url.path().ends_with('/') {
        return Ok(source_url);
    }

    let mut last_url = source_url.clone();
    for attempt in 0..=retries {
        match client
            .head(source_url.clone())
            .timeout(Duration::from_secs(timeout_seconds))
            .send()
            .await
        {
            Ok(response) => {
                last_url = response.url().clone();
                if last_url.path().ends_with('/') {
                    return Ok(last_url);
                }
                if response.status().is_success() {
                    return Ok(last_url);
                }
            }
            Err(error) => {
                log::debug!("HEAD source resolution failed for {}: {}", source, error);
            }
        }

        match client
            .get(source_url.clone())
            .header(RANGE, "bytes=0-0")
            .timeout(Duration::from_secs(timeout_seconds))
            .send()
            .await
        {
            Ok(response) => {
                last_url = response.url().clone();
                if last_url.path().ends_with('/') || response.status().is_success() {
                    return Ok(last_url);
                }
            }
            Err(error) => {
                log::debug!("GET source resolution failed for {}: {}", source, error);
            }
        }
        retry_delay(attempt).await;
    }
    Ok(last_url)
}

fn selected(path: &str, include: &[Regex], exclude: &[Regex]) -> bool {
    if matches_any(path, exclude) {
        return false;
    }
    include.is_empty() || matches_any(path, include)
}

fn matches_any(path: &str, patterns: &[Regex]) -> bool {
    patterns.iter().any(|pattern| pattern.is_match(path))
}

fn same_origin_under_root(root: &Url, candidate: &Url) -> bool {
    if root.scheme() != candidate.scheme()
        || root.host_str() != candidate.host_str()
        || root.port_or_known_default() != candidate.port_or_known_default()
    {
        return false;
    }
    candidate.path().starts_with(root.path())
}

fn file_name_from_url(url: &Url) -> Result<String> {
    let segment = url
        .path_segments()
        .and_then(|segments| segments.filter(|s| !s.is_empty()).last())
        .ok_or_else(|| anyhow!("source URL has no file name: {url}"))?;
    decode_path_segment(segment)
}

fn relative_path_from_url(root: &Url, candidate: &Url) -> Result<String> {
    let suffix = candidate
        .path()
        .strip_prefix(root.path())
        .ok_or_else(|| anyhow!("URL is outside source root: {candidate}"))?;
    let mut decoded = Vec::new();
    for segment in suffix.split('/') {
        if segment.is_empty() {
            continue;
        }
        let segment = decode_path_segment(segment)?;
        if segment == "." || segment == ".." || segment.contains('/') || segment.contains('\\') {
            bail!("unsafe URL path segment in {candidate}");
        }
        decoded.push(segment);
    }
    Ok(decoded.join("/"))
}

fn decode_path_segment(segment: &str) -> Result<String> {
    percent_decode_str(segment)
        .decode_utf8()
        .map(|s| s.into_owned())
        .map_err(|_| anyhow!("URL path segment is not valid UTF-8: {segment:?}"))
}

async fn get_text_with_retry(
    client: &Client,
    url: Url,
    timeout_seconds: u64,
    retries: usize,
) -> Result<String> {
    let mut last_error = None;
    for attempt in 0..=retries {
        let result = client
            .get(url.clone())
            .timeout(Duration::from_secs(timeout_seconds))
            .send()
            .await;
        match result {
            Ok(response) => match response.error_for_status() {
                Ok(response) => match response.text().await {
                    Ok(text) => return Ok(text),
                    Err(error) => last_error = Some(anyhow!(error)),
                },
                Err(error) => last_error = Some(anyhow!(error)),
            },
            Err(error) => last_error = Some(anyhow!(error)),
        }
        retry_delay(attempt).await;
    }
    Err(last_error.unwrap_or_else(|| anyhow!("HTTP request failed for {url}")))
}

async fn build_plan(
    client: &Client,
    options: &TransferOptions,
    files: Vec<DiscoveredFile>,
) -> Result<TransferPlan> {
    let resume = options.resume;
    let mut tasks = JoinSet::new();
    let mut plan_files = Vec::new();

    for file in files {
        while tasks.len() >= options.metadata_jobs {
            if let Some(result) = tasks.join_next().await {
                plan_files.push(result.context("metadata worker task failed")??);
            }
        }
        let client = client.clone();
        let destination = options.destination_dir.clone();
        let retries = options.retries;
        let timeout = options.metadata_timeout_seconds;
        let transfer_options = options.clone();
        tasks.spawn(async move {
            let remote_bytes = match file.remote_bytes_hint {
                Some(bytes) => Some(bytes),
                None => remote_size(&client, &transfer_options, &file.url, timeout, retries).await?,
            };
            let target = safe_destination(&destination, &file.relative_path)?;
            let local_bytes = match fs::metadata(&target).await {
                Ok(metadata) => metadata.len(),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => 0,
                Err(error) => {
                    return Err(error).with_context(|| format!("stat {}", target.display()))
                }
            };
            let complete = remote_bytes.map_or(false, |remote| remote == local_bytes);
            let bytes_remaining = remote_bytes.map(|remote| {
                if complete {
                    0
                } else if resume && local_bytes < remote {
                    remote - local_bytes
                } else {
                    remote
                }
            });
            Ok::<TransferItem, anyhow::Error>(TransferItem {
                url: file.url.to_string(),
                relative_path: file.relative_path,
                remote_bytes,
                local_bytes,
                bytes_remaining,
                complete,
            })
        });
    }

    while let Some(result) = tasks.join_next().await {
        plan_files.push(result.context("metadata worker task failed")??);
    }

    Ok(TransferPlan {
        plan_version: PLAN_VERSION.to_owned(),
        source: transfer_source_label(options),
        destination: options.destination_dir.display().to_string(),
        files: plan_files,
    })
}

async fn remote_size(
    client: &Client,
    options: &TransferOptions,
    url: &Url,
    timeout_seconds: u64,
    retries: usize,
) -> Result<Option<u64>> {
    if matches!(url.scheme(), "ftp" | "ftps") {
        return remote_size_curl(options, url, timeout_seconds, retries).await;
    }
    let mut last_error = None;
    for attempt in 0..=retries {
        match client
            .head(url.clone())
            .timeout(Duration::from_secs(timeout_seconds))
            .send()
            .await
        {
            Ok(response) if response.status().is_success() => {
                if let Some(size) = header_u64(response.headers().get(CONTENT_LENGTH)) {
                    return Ok(Some(size));
                }
            }
            Ok(response) if response.status() == StatusCode::METHOD_NOT_ALLOWED => {}
            Ok(response) => {
                last_error = Some(anyhow!("HEAD {} returned {}", url, response.status()));
            }
            Err(error) => last_error = Some(anyhow!(error)),
        }

        match client
            .get(url.clone())
            .header(RANGE, "bytes=0-0")
            .timeout(Duration::from_secs(timeout_seconds))
            .send()
            .await
        {
            Ok(response) if response.status().is_success() => {
                if let Some(total) = total_from_content_range(response.headers().get(CONTENT_RANGE)) {
                    return Ok(Some(total));
                }
                if let Some(size) = header_u64(response.headers().get(CONTENT_LENGTH)) {
                    return Ok(Some(size));
                }
                return Ok(None);
            }
            Ok(response) => {
                last_error = Some(anyhow!("size probe {} returned {}", url, response.status()));
            }
            Err(error) => last_error = Some(anyhow!(error)),
        }
        retry_delay(attempt).await;
    }
    if let Some(error) = last_error {
        log::warn!("could not determine remote size for {}: {:#}", url, error);
    }
    Ok(None)
}

async fn remote_size_curl(
    options: &TransferOptions,
    url: &Url,
    timeout_seconds: u64,
    retries: usize,
) -> Result<Option<u64>> {
    let curl_binary = options.curl_binary.clone();
    let curl_label = curl_binary.clone();
    let ftp_options = options.clone();
    let url_for_curl = url.clone();
    let url_text = url.to_string();
    let timeout = timeout_seconds.to_string();
    let retries = retries.to_string();
    let output = tokio::task::spawn_blocking(move || {
        let mut command = Command::new(&curl_binary);
        command.args([
            "--fail",
            "--silent",
            "--show-error",
            "--head",
            "--retry",
            &retries,
            "--connect-timeout",
            "30",
            "--max-time",
            &timeout,
        ]);
        configure_ftp_curl(&mut command, &ftp_options, &url_for_curl);
        command.arg(&url_text).output()
    })
    .await
    .context("join curl remote-size worker")?
    .with_context(|| format!("run {curl_label} size probe for {url}"))?;
    if !output.status.success() {
        log::warn!(
            "could not determine remote size for {} with curl: {}",
            url,
            ftp_failure_message(url, options, &String::from_utf8_lossy(&output.stderr))
        );
        return Ok(None);
    }
    let headers = String::from_utf8_lossy(&output.stdout);
    for line in headers.lines() {
        if let Some((name, value)) = line.split_once(':') {
            if name.trim().eq_ignore_ascii_case("content-length") {
                if let Ok(size) = value.trim().parse::<u64>() {
                    return Ok(Some(size));
                }
            }
        }
    }
    Ok(None)
}

fn header_u64(value: Option<&reqwest::header::HeaderValue>) -> Option<u64> {
    value?.to_str().ok()?.parse().ok()
}

fn total_from_content_range(value: Option<&reqwest::header::HeaderValue>) -> Option<u64> {
    let text = value?.to_str().ok()?;
    text.rsplit_once('/')?.1.parse().ok()
}

async fn retry_delay(attempt: usize) {
    let millis = ((attempt + 1) as u64 * 250).min(5_000);
    tokio::time::sleep(Duration::from_millis(millis)).await;
}

fn safe_destination(destination: &Path, relative_path: &str) -> Result<PathBuf> {
    let mut path = destination.to_path_buf();
    for segment in relative_path.split('/') {
        if segment.is_empty() || segment == "." || segment == ".." {
            bail!("unsafe relative transfer path: {relative_path:?}");
        }
        path.push(segment);
    }
    Ok(path)
}

async fn write_manifest(path: &Path, plan: &TransferPlan) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .await
            .with_context(|| format!("create manifest directory {}", parent.display()))?;
    }
    let extension = path.extension().and_then(|x| x.to_str()).unwrap_or_default();
    if extension.eq_ignore_ascii_case("json") {
        let bytes = serde_json::to_vec_pretty(plan)?;
        fs::write(path, bytes)
            .await
            .with_context(|| format!("write transfer manifest {}", path.display()))?;
        return Ok(());
    }

    let mut text = String::from("url\trelative_path\tremote_bytes\tlocal_bytes\tbytes_remaining\tcomplete\n");
    for item in &plan.files {
        text.push_str(&format!(
            "{}\t{}\t{}\t{}\t{}\t{}\n",
            sanitize_tsv(&item.url),
            sanitize_tsv(&item.relative_path),
            item.remote_bytes.map(|x| x.to_string()).unwrap_or_default(),
            item.local_bytes,
            item.bytes_remaining.map(|x| x.to_string()).unwrap_or_default(),
            item.complete
        ));
    }
    fs::write(path, text)
        .await
        .with_context(|| format!("write transfer manifest {}", path.display()))?;
    Ok(())
}

fn sanitize_tsv(value: &str) -> String {
    value
        .chars()
        .map(|c| if matches!(c, '\t' | '\n' | '\r') { ' ' } else { c })
        .collect()
}

async fn download_direct(
    client: &Client,
    options: &TransferOptions,
    plan: &TransferPlan,
) -> Result<DownloadTotals> {
    fs::create_dir_all(&options.destination_dir)
        .await
        .with_context(|| format!("create destination {}", options.destination_dir.display()))?;
    let mut tasks = JoinSet::new();
    let mut totals = DownloadTotals::default();

    for item in plan.files.iter().filter(|item| !item.complete).cloned() {
        while tasks.len() >= options.jobs {
            if let Some(result) = tasks.join_next().await {
                let downloaded = result.context("download worker task failed")??;
                totals.files += 1;
                totals.bytes += downloaded;
            }
        }
        let client = client.clone();
        let destination = options.destination_dir.clone();
        let retries = options.retries;
        let resume = options.resume;
        let force = options.force;
        let transfer_options = options.clone();
        tasks.spawn(async move {
            download_one(
                &client,
                &transfer_options,
                &destination,
                &item,
                retries,
                resume,
                force,
            )
            .await
        });
    }

    while let Some(result) = tasks.join_next().await {
        let downloaded = result.context("download worker task failed")??;
        totals.files += 1;
        totals.bytes += downloaded;
    }
    Ok(totals)
}

async fn download_one(
    client: &Client,
    options: &TransferOptions,
    destination: &Path,
    item: &TransferItem,
    retries: usize,
    resume: bool,
    force: bool,
) -> Result<u64> {
    let url = Url::parse(&item.url)?;
    if matches!(url.scheme(), "ftp" | "ftps") {
        return download_one_curl(options, destination, item, retries, resume, force).await;
    }
    let target = safe_destination(destination, &item.relative_path)?;
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent)
            .await
            .with_context(|| format!("create directory {}", parent.display()))?;
    }

    let before = fs::metadata(&target).await.map(|m| m.len()).unwrap_or(0);
    if let Some(remote) = item.remote_bytes {
        if before == remote {
            return Ok(0);
        }
        if before > remote && !force {
            bail!(
                "local file {} is larger than remote ({} > {}); use --force to replace it",
                target.display(),
                before,
                remote
            );
        }
    }

    let mut last_error = None;
    for attempt in 0..=retries {
        let current = fs::metadata(&target).await.map(|m| m.len()).unwrap_or(0);
        let can_resume = resume && current > 0 && item.remote_bytes.map_or(true, |remote| current < remote);
        let mut request = client.get(url.clone());
        if can_resume {
            request = request.header(RANGE, format!("bytes={current}-"));
        }

        match request.send().await {
            Ok(response) if response.status().is_success() => {
                let append = can_resume && response.status() == StatusCode::PARTIAL_CONTENT;
                let mut file = if append {
                    OpenOptions::new()
                        .create(true)
                        .append(true)
                        .open(&target)
                        .await
                } else {
                    OpenOptions::new()
                        .create(true)
                        .write(true)
                        .truncate(true)
                        .open(&target)
                        .await
                }
                .with_context(|| format!("open transfer target {}", target.display()))?;

                let mut response = response;
                let write_result: Result<()> = async {
                    while let Some(chunk) = response.chunk().await.context("read HTTP body chunk")? {
                        file.write_all(&chunk).await.context("write transfer chunk")?;
                    }
                    file.flush().await.context("flush transfer file")?;
                    Ok(())
                }
                .await;

                match write_result {
                    Ok(()) => {
                        let after = fs::metadata(&target)
                            .await
                            .with_context(|| format!("stat completed transfer {}", target.display()))?
                            .len();
                        if let Some(remote) = item.remote_bytes {
                            if after != remote {
                                last_error = Some(anyhow!(
                                    "size mismatch after downloading {}: local={} remote={}",
                                    target.display(),
                                    after,
                                    remote
                                ));
                            } else {
                                return Ok(if append { after.saturating_sub(before) } else { after });
                            }
                        } else {
                            return Ok(if append { after.saturating_sub(before) } else { after });
                        }
                    }
                    Err(error) => last_error = Some(error),
                }
            }
            Ok(response) => {
                last_error = Some(anyhow!("GET {} returned {}", url, response.status()));
            }
            Err(error) => last_error = Some(anyhow!(error)),
        }
        retry_delay(attempt).await;
    }

    Err(last_error.unwrap_or_else(|| anyhow!("download failed for {url}")))
}

async fn download_one_curl(
    options: &TransferOptions,
    destination: &Path,
    item: &TransferItem,
    retries: usize,
    resume: bool,
    force: bool,
) -> Result<u64> {
    let target = safe_destination(destination, &item.relative_path)?;
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent)
            .await
            .with_context(|| format!("create directory {}", parent.display()))?;
    }

    let before = fs::metadata(&target).await.map(|m| m.len()).unwrap_or(0);
    if let Some(remote) = item.remote_bytes {
        if before == remote {
            return Ok(0);
        }
        if before > remote && !force {
            bail!(
                "local file {} is larger than remote ({} > {}); use --force to replace it",
                target.display(),
                before,
                remote
            );
        }
    }

    if force && item.remote_bytes.map_or(false, |remote| before > remote) {
        match fs::remove_file(&target).await {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(error).with_context(|| format!("remove oversized {}", target.display()))
            }
        }
    }

    let current = fs::metadata(&target).await.map(|m| m.len()).unwrap_or(0);
    let can_resume = resume
        && current > 0
        && item
            .remote_bytes
            .map_or(true, |remote| current < remote);
    let first = run_curl_download(
        options,
        &item.url,
        &target,
        retries,
        can_resume,
    )
    .await;
    let resumed_successfully = if first.is_err() && can_resume {
        log::warn!(
            "resume failed for {}; retrying from byte zero",
            item.relative_path
        );
        run_curl_download(options, &item.url, &target, retries, false).await?;
        false
    } else {
        first?;
        can_resume
    };

    let after = fs::metadata(&target)
        .await
        .with_context(|| format!("stat completed transfer {}", target.display()))?
        .len();
    if let Some(remote) = item.remote_bytes {
        if after != remote {
            bail!(
                "size mismatch after downloading {}: local={} remote={}",
                target.display(),
                after,
                remote
            );
        }
    }
    Ok(if resumed_successfully {
        after.saturating_sub(before)
    } else {
        after
    })
}

async fn run_curl_download(
    options: &TransferOptions,
    url: &str,
    target: &Path,
    retries: usize,
    resume: bool,
) -> Result<()> {
    let curl_binary = options.curl_binary.clone();
    let curl_label = curl_binary.clone();
    let ftp_options = options.clone();
    let parsed_url = Url::parse(url).with_context(|| format!("parse FTP URL {url}"))?;
    let url = url.to_owned();
    let url_label = url.clone();
    let target = target.to_path_buf();
    let retries = retries.to_string();
    let output = tokio::task::spawn_blocking(move || {
        let mut command = Command::new(&curl_binary);
        command.args([
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--retry",
            &retries,
            "--connect-timeout",
            "30",
        ]);
        configure_ftp_curl(&mut command, &ftp_options, &parsed_url);
        if resume {
            command.args(["--continue-at", "-"]);
        }
        command.arg("--output").arg(&target).arg(&url).output()
    })
    .await
    .context("join curl download worker")?
    .with_context(|| format!("run {curl_label} for {url_label}"))?;
    if !output.status.success() {
        bail!(
            "curl download failed for {}: {}",
            url_label,
            ftp_failure_message(
                &Url::parse(&url_label).context("parse failed FTP URL for diagnostic")?,
                options,
                &String::from_utf8_lossy(&output.stderr),
            )
        );
    }
    Ok(())
}

#[derive(Debug)]
struct SlurmPreparation {
    manifest: PathBuf,
    scripts: Vec<PathBuf>,
    job_ids: Vec<String>,
}

async fn prepare_slurm(
    options: &TransferOptions,
    plan: &TransferPlan,
    slurm: &SlurmOptions,
) -> Result<SlurmPreparation> {
    let script_dir = slurm.script_dir.clone().unwrap_or_else(|| {
        options
            .destination_dir
            .join(".pride-scp-transfer")
            .join("slurm")
    });
    fs::create_dir_all(&script_dir)
        .await
        .with_context(|| format!("create Slurm script directory {}", script_dir.display()))?;

    let manifest = script_dir.join("transfer-plan.tsv");
    write_manifest(&manifest, plan).await?;

    let pending: Vec<_> = plan.files.iter().filter(|item| !item.complete).cloned().collect();
    if pending.is_empty() {
        return Ok(SlurmPreparation {
            manifest,
            scripts: Vec::new(),
            job_ids: Vec::new(),
        });
    }

    let shard_count = slurm.jobs.min(pending.len()).max(1);
    let shards = balance_shards(pending, shard_count);
    let mut scripts = Vec::new();
    for (index, shard) in shards.iter().enumerate() {
        let script = script_dir.join(format!("transfer-shard-{index:03}.sbatch"));
        let contents = render_slurm_script(options, slurm, index, shard, &script_dir)?;
        fs::write(&script, contents)
            .await
            .with_context(|| format!("write Slurm script {}", script.display()))?;
        scripts.push(script);
    }

    let mut job_ids = Vec::new();
    if slurm.submit {
        for script in &scripts {
            let mut command = Command::new(&slurm.sbatch_binary);
            for arg in &slurm.sbatch_args {
                command.arg(arg);
            }
            command.arg(script);
            let output = command
                .output()
                .with_context(|| format!("run sbatch for {}", script.display()))?;
            if !output.status.success() {
                bail!(
                    "sbatch failed for {}: {}",
                    script.display(),
                    String::from_utf8_lossy(&output.stderr).trim()
                );
            }
            let stdout = String::from_utf8_lossy(&output.stdout);
            let job_id = stdout
                .split_whitespace()
                .rev()
                .find(|token| token.chars().all(|c| c.is_ascii_digit()))
                .unwrap_or_else(|| stdout.trim())
                .to_owned();
            job_ids.push(job_id);
        }
    }

    Ok(SlurmPreparation {
        manifest,
        scripts,
        job_ids,
    })
}

fn balance_shards(mut items: Vec<TransferItem>, shard_count: usize) -> Vec<Vec<TransferItem>> {
    let known_sum: u64 = items.iter().filter_map(|item| item.bytes_remaining).sum();
    let known_count = items.iter().filter(|item| item.bytes_remaining.is_some()).count() as u64;
    let unknown_weight = if known_count > 0 {
        (known_sum / known_count).max(1)
    } else {
        1
    };
    items.sort_by_key(|item| Reverse(item.bytes_remaining.unwrap_or(unknown_weight)));

    let mut shards = vec![Vec::new(); shard_count];
    let mut weights = vec![0u64; shard_count];
    for item in items {
        let (index, _) = weights
            .iter()
            .enumerate()
            .min_by_key(|(_, weight)| **weight)
            .expect("at least one shard");
        weights[index] = weights[index].saturating_add(item.bytes_remaining.unwrap_or(unknown_weight));
        shards[index].push(item);
    }
    shards
}

fn render_slurm_script(
    options: &TransferOptions,
    slurm: &SlurmOptions,
    index: usize,
    items: &[TransferItem],
    script_dir: &Path,
) -> Result<String> {
    let stdout_path = script_dir.join(format!("{}-shard-{index:03}-%j.out", slurm.job_name));
    let stderr_path = script_dir.join(format!("{}-shard-{index:03}-%j.err", slurm.job_name));
    let mut text = String::from("#!/usr/bin/env bash\n");
    text.push_str(&format!("#SBATCH --job-name={}-{:03}\n", slurm.job_name, index));
    text.push_str(&format!("#SBATCH --time={}\n", slurm.time));
    text.push_str(&format!("#SBATCH --mem={}\n", slurm.mem));
    text.push_str(&format!("#SBATCH --cpus-per-task={}\n", slurm.cpus_per_task));
    text.push_str(&format!("#SBATCH --output={}\n", stdout_path.display()));
    text.push_str(&format!("#SBATCH --error={}\n", stderr_path.display()));
    if let Some(partition) = slurm.partition.as_ref() {
        text.push_str(&format!("#SBATCH --partition={}\n", partition));
    }
    if let Some(account) = slurm.account.as_ref() {
        text.push_str(&format!("#SBATCH --account={}\n", account));
    }
    if let Some(qos) = slurm.qos.as_ref() {
        text.push_str(&format!("#SBATCH --qos={}\n", qos));
    }
    if let Some(constraint) = slurm.constraint.as_ref() {
        text.push_str(&format!("#SBATCH --constraint={}\n", constraint));
    }
    text.push('\n');
    text.push_str("FAILED=0\n");
    text.push_str("echo \"transfer shard started: $(date -Is) host=$(hostname) job=${SLURM_JOB_ID:-none}\"\n");

    for item in items {
        let target = safe_destination(&options.destination_dir, &item.relative_path)?;
        let parent = target.parent().unwrap_or(&options.destination_dir);
        let curl = shell_quote(&slurm.curl_binary);
        let ftp_args = slurm_ftp_curl_args(options, &item.url)?;
        let url = shell_quote(&item.url);
        let target_q = shell_quote(&target.display().to_string());
        let parent_q = shell_quote(&parent.display().to_string());
        text.push_str(&format!("mkdir -p {parent_q}\n"));
        if options.resume {
            text.push_str(&format!(
                "if [ -s {target_q} ]; then\n  {curl} {ftp_args}--fail --silent --show-error --location --retry {} --retry-all-errors --connect-timeout {} --continue-at - --output {target_q} {url}\n  RC=$?\n  if [ \"$RC\" -ne 0 ]; then\n    echo \"resume failed for {}; retrying from byte 0\" >&2\n    {curl} {ftp_args}--fail --silent --show-error --location --retry {} --retry-all-errors --connect-timeout {} --output {target_q} {url}\n    RC=$?\n  fi\nelse\n  {curl} {ftp_args}--fail --silent --show-error --location --retry {} --retry-all-errors --connect-timeout {} --output {target_q} {url}\n  RC=$?\nfi\nif [ \"$RC\" -ne 0 ]; then FAILED=1; fi\n",
                options.retries,
                DEFAULT_CONNECT_TIMEOUT_SECONDS,
                item.relative_path.replace('"', "\\\""),
                options.retries,
                DEFAULT_CONNECT_TIMEOUT_SECONDS,
                options.retries,
                DEFAULT_CONNECT_TIMEOUT_SECONDS,
            ));
        } else {
            text.push_str(&format!(
                "{curl} {ftp_args}--fail --silent --show-error --location --retry {} --retry-all-errors --connect-timeout {} --output {target_q} {url}\nRC=$?\nif [ \"$RC\" -ne 0 ]; then FAILED=1; fi\n",
                options.retries, DEFAULT_CONNECT_TIMEOUT_SECONDS
            ));
        }
        if let Some(remote_bytes) = item.remote_bytes {
            text.push_str(&format!(
                "if [ \"$RC\" -eq 0 ]; then\n  LOCAL_BYTES=$(stat -c '%s' {target_q} 2>/dev/null || echo -1)\n  if [ \"$LOCAL_BYTES\" != \"{remote_bytes}\" ]; then\n    echo \"size mismatch: {} local=$LOCAL_BYTES remote={remote_bytes}\" >&2\n    FAILED=1\n  fi\nfi\n",
                item.relative_path.replace('"', "\\\"")
            ));
        }
        text.push('\n');
    }
    text.push_str("echo \"transfer shard finished: $(date -Is) failed=$FAILED\"\nexit \"$FAILED\"\n");
    Ok(text)
}

fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn massive_ftp_auto_requires_tls_and_compat_transport() {
        let url = Url::parse("ftp://massive-ftp.ucsd.edu/v10/MSV000098940/").unwrap();
        let options = transfer_options_for_test();
        assert!(ftp_tls_required(&options, &url));
        assert!(is_massive_ftp(&url));
    }

    #[test]
    fn ftp_certificate_diagnostic_is_actionable() {
        let url = Url::parse("ftp://massive-ftp.ucsd.edu/v10/MSV000098940/").unwrap();
        let options = transfer_options_for_test();
        let message = ftp_failure_message(
            &url,
            &options,
            "curl: (60) SSL certificate problem: unable to get local issuer certificate",
        );
        assert!(message.contains("--ftp-ca-cert"));
        assert!(message.contains("--ftp-insecure-tls"));
    }

    fn transfer_options_for_test() -> TransferOptions {
        TransferOptions {
            source: Some("ftp://massive-ftp.ucsd.edu/v10/MSV000098940/".to_owned()),
            source_list: None,
            destination_dir: PathBuf::from("/tmp/pride-scp-transfer-test"),
            jobs: 1,
            metadata_jobs: 1,
            metadata_timeout_seconds: 30,
            retries: 0,
            user_agent: "test".to_owned(),
            max_depth: None,
            include_regex: Vec::new(),
            exclude_regex: Vec::new(),
            limit: 0,
            resume: true,
            force: false,
            dry_run: true,
            manifest_out: None,
            curl_binary: "curl".to_owned(),
            ftp_tls_mode: "auto".to_owned(),
            ftp_insecure_tls: false,
            ftp_ca_cert: None,
            ftp_force_ipv4: false,
            ftp_disable_epsv: false,
            ftp_listing_mode: "auto".to_owned(),
            ftp_listing_jobs: 1,
            ftp_listing_interval_ms: 750,
            ftp_retry_backoff_seconds: 5,
            slurm: None,
            progress: false,
        }
    }

    #[test]
    fn root_relative_paths_are_decoded_and_safe() {
        let root = Url::parse("https://example.org/data/").unwrap();
        let file = Url::parse("https://example.org/data/run%2001/sample.raw").unwrap();
        assert_eq!(
            relative_path_from_url(&root, &file).unwrap(),
            "run 01/sample.raw"
        );
    }

    #[test]
    fn paths_outside_root_are_rejected() {
        let root = Url::parse("https://example.org/data/").unwrap();
        let file = Url::parse("https://example.org/other/sample.raw").unwrap();
        assert!(!same_origin_under_root(&root, &file));
    }

    #[test]
    fn include_and_exclude_filters_compose() {
        let include = compile_patterns(&[String::from(r"(?i)\.(raw|mzml)$")], "include").unwrap();
        let exclude = compile_patterns(&[String::from(r"(?i)/tmp/")], "exclude").unwrap();
        assert!(selected("study/a.raw", &include, &exclude));
        assert!(!selected("study/a.txt", &include, &exclude));
        assert!(!selected("study/tmp/a.raw", &include, &exclude));
    }

    #[test]
    fn sharding_balances_largest_files_first() {
        let item = |name: &str, bytes: u64| TransferItem {
            url: format!("https://example.org/{name}"),
            relative_path: name.to_owned(),
            remote_bytes: Some(bytes),
            local_bytes: 0,
            bytes_remaining: Some(bytes),
            complete: false,
        };
        let shards = balance_shards(
            vec![item("a", 10), item("b", 9), item("c", 2), item("d", 1)],
            2,
        );
        let weights: Vec<u64> = shards
            .iter()
            .map(|shard| shard.iter().filter_map(|item| item.bytes_remaining).sum())
            .collect();
        assert_eq!(weights, vec![11, 11]);
    }

    #[test]
    fn shell_quote_handles_single_quotes() {
        assert_eq!(shell_quote("a'b"), "'a'\\''b'");
    }
    #[test]
    fn byte_format_is_human_readable() {
        assert_eq!(format_bytes(1024), "1.00 KiB");
        assert_eq!(format_bytes(5 * 1024 * 1024), "5.00 MiB");
    }

    #[test]
    fn transient_ftp_listing_exit_codes_are_classified() {
        assert!(is_transient_ftp_listing_exit(Some(7)));
        assert!(is_transient_ftp_listing_exit(Some(28)));
        assert!(is_transient_ftp_listing_exit(Some(56)));
        assert!(!is_transient_ftp_listing_exit(Some(60)));
        assert!(!is_transient_ftp_listing_exit(Some(67)));
    }

    #[test]
    fn ftp_unix_listing_preserves_spaces_and_sizes() {
        let listing = "drwxr-xr-x 2 ftp ftp 4096 Sep 20 12:00 raw\n-rw-r--r-- 1 ftp ftp 123456 Sep 20 12:00 sample 01.raw\n";
        let entries = parse_ftp_listing(listing).unwrap();
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0].name, "raw");
        assert!(entries[0].is_dir);
        assert_eq!(entries[1].name, "sample 01.raw");
        assert!(!entries[1].is_dir);
        assert_eq!(entries[1].size, Some(123456));
    }

    #[test]
    fn ftp_mlsd_listing_is_supported() {
        let listing = "type=dir;modify=20260920120000; raw\r\ntype=file;size=42;modify=20260920120000; result.tsv\r\n";
        let entries = parse_ftp_listing(listing).unwrap();
        assert_eq!(entries.len(), 2);
        assert!(entries[0].is_dir);
        assert_eq!(entries[1].size, Some(42));
    }

    #[test]
    fn ftp_mlsd_facts_can_appear_in_any_order() {
        let listing = "modify=20260921120000;size=42;type=file; sample.raw\ntype=dir;modify=20260921120000; raw\n";
        let entries = parse_ftp_listing(listing).unwrap();
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0].name, "sample.raw");
        assert_eq!(entries[0].size, Some(42));
        assert!(!entries[0].is_dir);
        assert_eq!(entries[1].name, "raw");
        assert!(entries[1].is_dir);
    }

    #[test]
    fn ftp_dataset_root_uses_accession_as_destination_prefix() {
        let root = Url::parse("ftp://massive-ftp.ucsd.edu/v10/MSV000098940/").unwrap();
        assert_eq!(source_prefix_from_url(&root), "MSV000098940");
        let raw = join_url_path_segment(&root, "raw", true).unwrap();
        let file = join_url_path_segment(&raw, "sample 01.d", false).unwrap();
        assert_eq!(relative_path_from_url(&root, &file).unwrap(), "raw/sample 01.d");
        assert_eq!(
            prefixed_path(Some(&source_prefix_from_url(&root)), "raw/sample 01.d"),
            "MSV000098940/raw/sample 01.d"
        );
    }

    #[test]
    fn massive_dataset_page_is_detected() {
        let url = Url::parse(
            "https://massive.ucsd.edu/ProteoSAFe/dataset.jsp?task=4b22e2f4fcde424daf16857507045a4c",
        )
        .unwrap();
        assert!(is_massive_dataset_page(&url));
    }

}
