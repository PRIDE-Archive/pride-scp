use anyhow::Result;
use clap::{Parser, Subcommand};
use pride_scp_core::format_duration;
use pride_scp_discovery::{
    candidate_audit, discover, export_python, recall_audit, CandidateAuditOptions, DiscoverOptions,
    ExportPythonOptions, RecallAuditOptions,
};
use pride_scp_index::{
    compact_snapshot, massive_native_snapshot, registry_snapshot, snapshot, CompactSnapshotOptions,
    MassiveNativeSnapshotOptions, RegistrySnapshotOptions, SnapshotOptions,
    DEFAULT_MASSIVE_PAGE_SIZE, DEFAULT_MASSIVE_QUERY_DATASETS_API, DEFAULT_PRIDE_API,
    DEFAULT_PROJECT_PAGE_SIZE, DEFAULT_PROTEOMECENTRAL_PROXI_API, DEFAULT_REGISTRY_PAGE_SIZE,
};
use pride_scp_sdrf::{
    acquire_sdrf_evidence, annotate_sdrf, audit_sdrf_sources, resolve_sdrf_sources,
    run_scientific_sdrf_agent, SdrfAcquireEvidenceOptions, SdrfAnnotateOptions, SdrfAuditOptions,
    SdrfResolveOptions, SdrfScientificAgentOptions, DEFAULT_OLLAMA_URL as DEFAULT_SDRF_OLLAMA_URL,
    DEFAULT_PRIDE_V2_API as DEFAULT_SDRF_PRIDE_V2_API,
};
use pride_scp_transfer::{transfer, SlurmOptions, TransferOptions};
use std::path::PathBuf;
use std::time::Instant;

#[derive(Debug, Parser)]
#[command(
    name = "pride-scp",
    version,
    about = "PRIDE single-cell proteomics discovery, annotation, audit, and transfer tooling"
)]
struct Cli {
    /// Disable interactive progress bars/spinners. Final summaries still print to stdout.
    #[arg(long, global = true)]
    no_progress: bool,

    /// Log level for stderr diagnostics (error, warn, info, debug, trace).
    /// RUST_LOG overrides this when set.
    #[arg(long, global = true, default_value = "info")]
    log_level: String,

    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Plan or transfer files from HTTP(S), FTP(S), or a list of dataset roots.
    /// Supports byte-accurate dry runs, resumable direct downloads, and size-balanced Slurm shards.
    Transfer {
        /// Single HTTP(S)/FTP(S) file or directory URL. Use --source-list for multiple dataset roots.
        #[arg(long, conflicts_with = "source_list")]
        source: Option<String>,
        /// Text file containing one HTTP(S)/FTP(S) source or MassIVE dataset root per line.
        /// Blank lines and lines beginning with '#' are ignored. Alias: --dataset-list.
        #[arg(long, visible_alias = "dataset-list", conflicts_with = "source")]
        source_list: Option<PathBuf>,
        /// Local destination root. The source directory hierarchy is preserved below this path.
        #[arg(long)]
        destination: PathBuf,
        /// Concurrent direct-download workers. Ignored for Slurm execution.
        #[arg(long, default_value_t = 4)]
        jobs: usize,
        /// Concurrent HEAD/range probes used to calculate remote sizes.
        #[arg(long, default_value_t = 16)]
        metadata_jobs: usize,
        /// Timeout for directory-listing and size-probe requests. Large file bodies are not subject to this timeout.
        #[arg(long, default_value_t = 120)]
        metadata_timeout: u64,
        /// Number of retries after the initial HTTP attempt.
        #[arg(long, default_value_t = 4)]
        retries: usize,
        #[arg(long, default_value = "PRIDE-SCP-transfer/0.1.12")]
        user_agent: String,
        /// Maximum recursive directory depth below the source root. Omit for unlimited recursion.
        #[arg(long)]
        max_depth: Option<usize>,
        /// Keep only relative file paths matching at least one regex. Repeat as needed.
        #[arg(long = "include-regex")]
        include_regex: Vec<String>,
        /// Exclude relative file/directory paths matching any regex. Repeat as needed.
        #[arg(long = "exclude-regex")]
        exclude_regex: Vec<String>,
        /// Limit the selected file set after filtering. Zero means unlimited.
        #[arg(long, default_value_t = 0)]
        limit: usize,
        /// Report discovered files and transfer size without downloading or creating Slurm scripts.
        #[arg(long, conflicts_with_all = ["generate_slurm", "submit_slurm"])]
        dry_run: bool,
        /// Do not resume partially present files; replace them from byte zero.
        #[arg(long)]
        no_resume: bool,
        /// Replace local files that are larger than their remote counterpart.
        #[arg(long)]
        force: bool,
        /// Optional JSON (extension `.json`) or TSV transfer manifest.
        #[arg(long)]
        manifest_out: Option<PathBuf>,
        /// Generate size-balanced Slurm transfer scripts but do not submit them.
        #[arg(long, conflicts_with = "submit_slurm")]
        generate_slurm: bool,
        /// Generate and submit size-balanced Slurm transfer jobs with `sbatch`.
        #[arg(long, conflicts_with = "generate_slurm")]
        submit_slurm: bool,
        /// Maximum number of independent Slurm transfer shards.
        #[arg(long, default_value_t = 8)]
        slurm_jobs: usize,
        /// Directory for generated Slurm scripts, logs, and the transfer manifest.
        #[arg(long)]
        slurm_script_dir: Option<PathBuf>,
        #[arg(long, default_value = "pride-scp-transfer")]
        slurm_job_name: String,
        #[arg(long, default_value = "12:00:00")]
        slurm_time: String,
        #[arg(long, default_value = "4G")]
        slurm_mem: String,
        #[arg(long, default_value_t = 1)]
        slurm_cpus_per_task: usize,
        #[arg(long)]
        slurm_partition: Option<String>,
        #[arg(long)]
        slurm_account: Option<String>,
        #[arg(long)]
        slurm_qos: Option<String>,
        #[arg(long)]
        slurm_constraint: Option<String>,
        /// Extra argument passed directly to every `sbatch` invocation. Repeat as needed.
        #[arg(long = "sbatch-arg", allow_hyphen_values = true)]
        sbatch_args: Vec<String>,
        /// sbatch executable used for --submit-slurm. Useful when Slurm is exposed at a non-default path.
        #[arg(long, default_value = "sbatch")]
        sbatch_binary: String,
        /// Curl executable used for FTP(S) discovery/direct downloads and generated Slurm workers.
        #[arg(long, default_value = "curl")]
        curl_binary: String,
        /// FTP TLS policy: auto, required, or off. Auto requires explicit TLS for MassIVE.
        #[arg(long, default_value = "auto", value_parser = ["auto", "required", "off"])]
        ftp_tls: String,
        /// Disable FTP(S) TLS certificate verification. Use only as an explicit compatibility workaround.
        #[arg(long, conflicts_with = "ftp_ca_cert")]
        ftp_insecure_tls: bool,
        /// CA certificate bundle/file passed to curl for FTP(S) certificate verification.
        #[arg(long, value_name = "PATH", conflicts_with = "ftp_insecure_tls")]
        ftp_ca_cert: Option<PathBuf>,
        /// Force IPv4 for FTP(S). MassIVE does this automatically.
        #[arg(long)]
        ftp_ipv4: bool,
        /// Disable EPSV and use PASV for FTP(S). MassIVE does this automatically.
        #[arg(long)]
        ftp_disable_epsv: bool,
        /// FTP directory-listing strategy. Auto prefers MLSD for MassIVE and falls back to LIST only for non-transient protocol/parse failures.
        #[arg(long, default_value = "auto", value_parser = ["auto", "mlsd", "list"])]
        ftp_listing: String,
        /// Maximum concurrent FTP directory-listing sessions. Kept separate from --metadata-jobs to avoid overwhelming FTP servers.
        #[arg(long, default_value_t = 1)]
        ftp_listing_jobs: usize,
        /// Minimum milliseconds between new FTP directory-listing connection attempts.
        #[arg(long, default_value_t = 750)]
        ftp_listing_interval_ms: u64,
        /// Base seconds for exponential backoff after transient FTP listing failures (curl 7/28/56).
        #[arg(long, default_value_t = 5)]
        ftp_retry_backoff_seconds: u64,
    },
    /// Snapshot PRIDE project metadata, file manifests, and SDRF where available.
    Snapshot {
        #[arg(long, default_value = "data/snapshot")]
        output: PathBuf,
        #[arg(long, default_value = DEFAULT_PRIDE_API)]
        api_base: String,
        /// Maximum number of accessions being processed concurrently.
        #[arg(long, default_value_t = 8)]
        concurrency: usize,
        /// Global cap on concurrent HTTP requests across project/files/SDRF retrieval.
        #[arg(long, default_value_t = 16)]
        request_concurrency: usize,
        /// Per-request timeout, including response-body transfer.
        #[arg(long, default_value_t = 120)]
        timeout: u64,
        /// Number of retries after the initial HTTP attempt.
        #[arg(long, default_value_t = 4)]
        retries: usize,
        /// Page size requested while enumerating PRIDE projects. The live v3 endpoint
        /// may return a monolithic catalogue; v0.1.3 detects and stops on that shape.
        #[arg(long, default_value_t = DEFAULT_PROJECT_PAGE_SIZE)]
        project_page_size: usize,
        /// Safety stop if consecutive catalogue pages contribute no new PXD accessions.
        #[arg(long, default_value_t = 3)]
        max_stagnant_pages: usize,
        /// Do not seed project metadata from the /projects/all catalogue payload.
        /// Seeding avoids tens of thousands of redundant per-project detail requests.
        #[arg(long)]
        no_catalogue_project_seed: bool,
        /// Refresh the PRIDE catalogue response instead of reusing cached project pages.
        /// This does not force re-download of per-project files/SDRF.
        #[arg(long)]
        refresh_catalogue: bool,
        #[arg(long, default_value = "PRIDE-SCP-recall-index/0.1.4")]
        user_agent: String,
        /// Skip per-project file-manifest retrieval.
        #[arg(long)]
        no_files: bool,
        /// Skip SDRF retrieval.
        #[arg(long)]
        no_sdrf: bool,
        #[arg(long)]
        force: bool,
        #[arg(long, default_value_t = 0)]
        limit: usize,
        #[arg(long)]
        accessions_file: Option<PathBuf>,
    },
    /// Snapshot ProteomeCentral/PROXI metadata to recover cross-repository PXD aliases
    /// that are absent from the primary PRIDE project catalogue.
    RegistrySnapshot {
        /// Existing PRIDE snapshot directory to supplement.
        #[arg(long, default_value = "data/snapshot")]
        snapshot: PathBuf,
        #[arg(long, default_value = DEFAULT_PROTEOMECENTRAL_PROXI_API)]
        api_base: String,
        /// PROXI datasets page size (public API maximum: 100).
        #[arg(long, default_value_t = DEFAULT_REGISTRY_PAGE_SIZE)]
        page_size: usize,
        /// Per-request timeout, including response-body transfer.
        #[arg(long, default_value_t = 120)]
        timeout: u64,
        /// Number of retries after the initial HTTP attempt.
        #[arg(long, default_value_t = 4)]
        retries: usize,
        /// Safety page cap; hitting the cap is an error because a partial registry
        /// snapshot is unsafe for recall benchmarking. Zero disables the user cap.
        #[arg(long, default_value_t = 10_000)]
        max_pages: usize,
        #[arg(long, default_value = "PRIDE-SCP-registry-index/0.1.12")]
        user_agent: String,
        /// Refresh cached ProteomeCentral pages and normalized registry records.
        #[arg(long)]
        force: bool,
    },
    /// Snapshot the native public MassIVE dataset catalogue using MassIVE's own
    /// QueryDatasets endpoint. This lane can discover MSV datasets that have no PXD alias.
    MassiveNativeSnapshot {
        /// Existing unified snapshot directory.
        #[arg(long, default_value = "data/snapshot")]
        snapshot: PathBuf,
        #[arg(long, default_value = DEFAULT_MASSIVE_QUERY_DATASETS_API)]
        query_endpoint: String,
        /// JSON query sent to MassIVE QueryDatasets. `{}` requests the public table unfiltered.
        #[arg(long, default_value = "{}")]
        query_json: String,
        #[arg(long, default_value_t = DEFAULT_MASSIVE_PAGE_SIZE)]
        page_size: usize,
        /// Per-request timeout, including response-body transfer.
        #[arg(long, default_value_t = 120)]
        timeout: u64,
        /// Number of retries after the initial HTTP attempt.
        #[arg(long, default_value_t = 4)]
        retries: usize,
        /// Safety page cap. Reaching it is an error because a partial native catalogue
        /// is unsafe for recall benchmarking. Zero disables the user cap.
        #[arg(long, default_value_t = 10_000)]
        max_pages: usize,
        /// Fail if this many populated pages add no new MSV accessions.
        #[arg(long, default_value_t = 3)]
        max_stagnant_pages: usize,
        #[arg(long, default_value = "PRIDE-SCP-massive-native-index/0.2.0")]
        user_agent: String,
        #[arg(long)]
        force: bool,
    },
    /// Discover SCP candidates from the union of repository/file/SDRF signals.
    Discover {
        #[arg(long, default_value = "data/snapshot")]
        snapshot: PathBuf,
        #[arg(long, default_value = "data/discovery")]
        output: PathBuf,
        #[arg(long, default_value = "config/discovery_terms.json")]
        config: PathBuf,
        /// High-recall default: retain any project with a positive signal.
        #[arg(long, default_value_t = 1)]
        min_score: i32,
        /// Grant-informed scale guardrail; 0 disables the alarm.
        #[arg(long, default_value_t = 208)]
        expected_positive_count: usize,
        /// Repository lane(s) to scan. `pride-primary` is the GT-independent
        /// production scope for the PRIDE-only catalogue.
        #[arg(
            long,
            default_value = "all",
            value_parser = ["all", "pride-primary", "registry-supplement", "native-massive"]
        )]
        source_scope: String,
    },
    /// Categorize high-recall discovery evidence into semantic-review profiles.
    CandidateAudit {
        #[arg(long, default_value = "data/discovery/candidates.jsonl")]
        candidates: PathBuf,
        #[arg(long, default_value = "config/discovery_terms.json")]
        config: PathBuf,
        #[arg(long, default_value = "data/candidate_audit")]
        output: PathBuf,
    },
    /// Reclaim redundant snapshot cache after projects have been materialized.
    CompactSnapshot {
        #[arg(long, default_value = "data/snapshot")]
        snapshot: PathBuf,
        /// Remove every project-page cache. By default the newest page is retained.
        #[arg(long)]
        delete_all_project_pages: bool,
        /// Optional accession list to retain when pruning file/SDRF evidence.
        #[arg(long)]
        retain_accessions_file: Option<PathBuf>,
        /// Also remove file/SDRF payloads for accessions not in --retain-accessions-file.
        #[arg(long)]
        prune_noncandidate_evidence: bool,
        /// Report what would be removed without deleting anything.
        #[arg(long)]
        dry_run: bool,
    },
    /// Measure candidate recall against a known-positive CSV benchmark.
    RecallAudit {
        #[arg(long, default_value = "data/discovery/candidates.tsv")]
        candidates: PathBuf,
        #[arg(long)]
        known_positives: PathBuf,
        /// Optional case-insensitive hosting_repository filter for benchmark CSVs.
        /// This allows the frozen multi-repository GT master to be evaluated directly
        /// without copying PRIDE rows into a production lookup list.
        #[arg(long)]
        repository_filter: Option<String>,
        #[arg(long, default_value = "data/recall_audit")]
        output: PathBuf,
    },
    /// Export a candidate accession list and manifest for the existing Python stages.
    ExportPython {
        #[arg(long, default_value = "data/discovery/candidates.tsv")]
        candidates: PathBuf,
        /// Optional full JSONL candidate records. Defaults to candidates.jsonl beside the TSV.
        #[arg(long)]
        candidates_jsonl: Option<PathBuf>,
        #[arg(long, default_value = "config/discovery_terms.json")]
        config: PathBuf,
        #[arg(long, default_value = "data/python_bridge")]
        output: PathBuf,
        #[arg(long, default_value = "weak")]
        min_tier: String,
    },
    /// Resolve usable SDRF sources for PRIDE accessions into a provenance-labelled local cache.
    /// Community-curated BigBio SDRFs are preferred over original repository SDRFs;
    /// header-only PRIDE API cache responses are never selected.
    SdrfResolve {
        #[arg(long = "accession")]
        accessions: Vec<String>,
        #[arg(long)]
        accessions_file: Option<PathBuf>,
        #[arg(long, default_value = "data/snapshot")]
        snapshot: PathBuf,
        #[arg(long, default_value = "data/sdrf_sources")]
        output: PathBuf,
        #[arg(long, default_value_t = 120)]
        timeout: u64,
        #[arg(long)]
        force: bool,
    },
    /// Execute blocker-directed PRIDE evidence acquisition tasks from the SDRF harness.
    /// Community-annotated SDRFs are recorded separately and are never promoted to depositor evidence.
    SdrfAcquireEvidence {
        /// `evidence_acquisition_plan.tsv` produced by the SDRF annotation harness.
        #[arg(long)]
        acquisition_plan: PathBuf,
        /// Output directory for acquisition attempts, provenance manifest, and downloaded candidates.
        #[arg(long, default_value = "data/sdrf_evidence_acquisition")]
        output: PathBuf,
        /// PRIDE Archive v2 API base used for project file inventories.
        #[arg(long, default_value = DEFAULT_SDRF_PRIDE_V2_API)]
        api_base: String,
        /// Request timeout in seconds.
        #[arg(long, default_value_t = 120)]
        timeout: u64,
        /// Maximum non-community SDRF candidates downloaded for one accession.
        #[arg(long, default_value_t = 4)]
        max_candidates_per_accession: usize,
        /// Re-download already materialized provider artifacts.
        #[arg(long)]
        force: bool,
    },
    /// Execute blocker-directed PRIDE evidence acquisition tasks from the SDRF harness.
    /// Community-annotated SDRFs are recorded separately and are never promoted to depositor evidence.
    /// Deterministically audit already-resolved SDRFs without invoking Ollama.
    /// Reports validator errors/warnings and target fields that would still need enrichment.
    SdrfAudit {
        #[arg(long = "accession")]
        accessions: Vec<String>,
        #[arg(long)]
        accessions_file: Option<PathBuf>,
        #[arg(long, default_value = "data/snapshot")]
        snapshot: PathBuf,
        /// `resolved/` directory produced by `pride-scp sdrf-resolve`.
        #[arg(long)]
        resolved_sdrf_dir: PathBuf,
        #[arg(long, default_value = "data/sdrf_audit")]
        output: PathBuf,
    },
    /// Generate provenance-tracked SDRF-Proteomics single-cell drafts from PRIDE
    /// repository metadata, existing annotations, and manuscript-derived evidence.
    SdrfAnnotate {
        /// One or more PRIDE accessions to annotate. May be combined with --accessions-file.
        #[arg(long = "accession")]
        accessions: Vec<String>,
        /// File containing PRIDE accessions (one per line; CSV/TSV first column also accepted).
        #[arg(long)]
        accessions_file: Option<PathBuf>,
        /// Existing PRIDE snapshot containing projects/, files/, and sdrf/.
        #[arg(long, default_value = "data/snapshot")]
        snapshot: PathBuf,
        /// Existing Stage04 annotation tree. Manuscript-derived semantic evidence is reused
        /// when provenance files are available.
        #[arg(long, default_value = "work/python/pride_scp_annotations/annotations")]
        annotations_dir: PathBuf,
        /// Publication-content manifest from the existing pipeline. Text/HTML/XML content
        /// paths are read directly; PDFs continue to use upstream extraction/semantic evidence.
        #[arg(
            long,
            default_value = "work/python/pride_candidate_publications_with_content.tsv"
        )]
        publication_manifest: PathBuf,
        /// Additional extracted manuscript text/HTML/XML file(s). Repeat as needed.
        #[arg(long = "manuscript-text")]
        manuscript_text: Vec<PathBuf>,
        /// Optional `resolved/` directory produced by `pride-scp sdrf-resolve`.
        /// Usable files here take precedence over the PRIDE SDRF API cache.
        #[arg(long)]
        resolved_sdrf_dir: Option<PathBuf>,
        /// Optional source-grounded explicit row-mapping manifest. Rows in this manifest
        /// replace generic de-novo row construction only for matching accessions.
        #[arg(long)]
        explicit_row_mapping_manifest: Option<PathBuf>,
        #[arg(long, default_value = "data/sdrf_annotation")]
        output: PathBuf,
        #[arg(long, default_value = "qwen2.5:3b")]
        model: String,
        #[arg(long, default_value = DEFAULT_SDRF_OLLAMA_URL)]
        ollama_url: String,
        /// Ollama request timeout. Large manuscript evidence packets may require several minutes.
        #[arg(long, default_value_t = 1200)]
        timeout: u64,
        /// Maximum evidence records passed to the model after provenance-aware extraction.
        #[arg(long, default_value_t = 128)]
        max_evidence_items: usize,
        /// Maximum total evidence characters retained for one dataset.
        #[arg(long, default_value_t = 60_000)]
        max_evidence_chars: usize,
        /// Maximum RAW filenames shown to Ollama. The complete inventory is still used
        /// deterministically when the SDRF draft is serialized.
        #[arg(long, default_value_t = 64)]
        max_files_in_prompt: usize,
        #[arg(long)]
        force: bool,
    },
    /// Run a persistent scientific-agent harness that reconstructs study design before SDRF rows are compiled.
    SdrfAgent {
        #[arg(long = "accession")]
        accessions: Vec<String>,
        #[arg(long)]
        accessions_file: Option<PathBuf>,
        #[arg(long, default_value = "data/snapshot")]
        snapshot: PathBuf,
        #[arg(long, default_value = "work/python/pride_scp_annotations/annotations")]
        annotations_dir: PathBuf,
        #[arg(
            long,
            default_value = "work/python/pride_candidate_publications_with_content.tsv"
        )]
        publication_manifest: PathBuf,
        #[arg(long = "manuscript-text")]
        manuscript_text: Vec<PathBuf>,
        #[arg(long)]
        resolved_sdrf_dir: Option<PathBuf>,
        #[arg(long)]
        explicit_row_mapping_manifest: Option<PathBuf>,
        #[arg(long, default_value = "data/sdrf_scientific_agent")]
        output: PathBuf,
        #[arg(long, default_value = "qwen2.5:3b")]
        model: String,
        #[arg(long, default_value = DEFAULT_SDRF_OLLAMA_URL)]
        ollama_url: String,
        #[arg(long, default_value_t = 1200)]
        timeout: u64,
        #[arg(long, default_value_t = 128)]
        max_evidence_items: usize,
        #[arg(long, default_value_t = 60_000)]
        max_evidence_chars: usize,
        #[arg(long, default_value_t = 64)]
        max_files_in_prompt: usize,
        #[arg(long, default_value_t = 12)]
        max_agent_turns: usize,
        #[arg(long, default_value_t = 20)]
        max_tool_actions: usize,
        #[arg(long, default_value_t = 3)]
        max_validator_cycles: usize,
        #[arg(long)]
        force: bool,
    },
}

fn init_logging(default_level: &str) {
    let mut builder = env_logger::Builder::new();
    if let Ok(filter) = std::env::var("RUST_LOG") {
        builder.parse_filters(&filter);
    } else {
        builder.parse_filters(default_level);
    }
    let _ = builder.format_timestamp_secs().try_init();
}

#[tokio::main]
async fn main() -> Result<()> {
    let Cli {
        no_progress,
        log_level,
        command,
    } = Cli::parse();
    init_logging(&log_level);
    let progress = !no_progress;
    let started = Instant::now();

    match command {
        Command::Transfer {
            source,
            source_list,
            destination,
            jobs,
            metadata_jobs,
            metadata_timeout,
            retries,
            user_agent,
            max_depth,
            include_regex,
            exclude_regex,
            limit,
            dry_run,
            no_resume,
            force,
            manifest_out,
            generate_slurm,
            submit_slurm,
            slurm_jobs,
            slurm_script_dir,
            slurm_job_name,
            slurm_time,
            slurm_mem,
            slurm_cpus_per_task,
            slurm_partition,
            slurm_account,
            slurm_qos,
            slurm_constraint,
            sbatch_args,
            sbatch_binary,
            curl_binary,
            ftp_tls,
            ftp_insecure_tls,
            ftp_ca_cert,
            ftp_ipv4,
            ftp_disable_epsv,
            ftp_listing,
            ftp_listing_jobs,
            ftp_listing_interval_ms,
            ftp_retry_backoff_seconds,
        } => {
            let source_label = source
                .as_deref()
                .map(str::to_owned)
                .or_else(|| {
                    source_list
                        .as_ref()
                        .map(|path| format!("@{}", path.display()))
                })
                .unwrap_or_else(|| "<missing>".to_owned());
            log::info!(
                "command=transfer source={} destination={} dry_run={} generate_slurm={} submit_slurm={}",
                source_label,
                destination.display(),
                dry_run,
                generate_slurm,
                submit_slurm
            );
            let slurm = (generate_slurm || submit_slurm).then(|| SlurmOptions {
                submit: submit_slurm,
                jobs: slurm_jobs,
                script_dir: slurm_script_dir,
                job_name: slurm_job_name,
                time: slurm_time,
                mem: slurm_mem,
                cpus_per_task: slurm_cpus_per_task,
                partition: slurm_partition,
                account: slurm_account,
                qos: slurm_qos,
                constraint: slurm_constraint,
                sbatch_args,
                sbatch_binary,
                curl_binary: curl_binary.clone(),
            });
            let summary = transfer(TransferOptions {
                source,
                source_list,
                destination_dir: destination,
                jobs,
                metadata_jobs,
                metadata_timeout_seconds: metadata_timeout,
                retries,
                user_agent,
                max_depth,
                include_regex,
                exclude_regex,
                limit,
                resume: !no_resume,
                force,
                dry_run,
                manifest_out,
                curl_binary: curl_binary.clone(),
                ftp_tls_mode: ftp_tls,
                ftp_insecure_tls,
                ftp_ca_cert,
                ftp_force_ipv4: ftp_ipv4,
                ftp_disable_epsv,
                ftp_listing_mode: ftp_listing,
                ftp_listing_jobs,
                ftp_listing_interval_ms,
                ftp_retry_backoff_seconds,
                slurm,
                progress,
            })
            .await?;
            println!("{}", serde_json::to_string_pretty(&summary)?);
        }
        Command::Snapshot {
            output,
            api_base,
            concurrency,
            request_concurrency,
            timeout,
            retries,
            project_page_size,
            max_stagnant_pages,
            no_catalogue_project_seed,
            refresh_catalogue,
            user_agent,
            no_files,
            no_sdrf,
            force,
            limit,
            accessions_file,
        } => {
            log::info!("command=snapshot output={}", output.display());
            let summary = snapshot(SnapshotOptions {
                output_dir: output,
                api_base,
                concurrency,
                request_concurrency,
                timeout_seconds: timeout,
                retries,
                project_page_size,
                max_stagnant_pages,
                seed_projects_from_catalogue: !no_catalogue_project_seed,
                refresh_catalogue,
                user_agent,
                include_files: !no_files,
                include_sdrf: !no_sdrf,
                force,
                limit,
                accessions_file,
                progress,
            })
            .await?;
            println!("{}", serde_json::to_string_pretty(&summary)?);
        }
        Command::RegistrySnapshot {
            snapshot,
            api_base,
            page_size,
            timeout,
            retries,
            max_pages,
            user_agent,
            force,
        } => {
            log::info!("command=registry-snapshot snapshot={}", snapshot.display());
            let summary = registry_snapshot(RegistrySnapshotOptions {
                snapshot_dir: snapshot,
                api_base,
                timeout_seconds: timeout,
                retries,
                user_agent,
                page_size,
                max_pages,
                force,
                progress,
            })
            .await?;
            println!("{}", serde_json::to_string_pretty(&summary)?);
        }
        Command::MassiveNativeSnapshot {
            snapshot,
            query_endpoint,
            query_json,
            page_size,
            timeout,
            retries,
            max_pages,
            max_stagnant_pages,
            user_agent,
            force,
        } => {
            log::info!(
                "command=massive-native-snapshot snapshot={}",
                snapshot.display()
            );
            let summary = massive_native_snapshot(MassiveNativeSnapshotOptions {
                snapshot_dir: snapshot,
                query_endpoint,
                query_json,
                timeout_seconds: timeout,
                retries,
                user_agent,
                page_size,
                max_pages,
                max_stagnant_pages,
                force,
                progress,
            })
            .await?;
            println!("{}", serde_json::to_string_pretty(&summary)?);
        }
        Command::Discover {
            snapshot,
            output,
            config,
            min_score,
            expected_positive_count,
            source_scope,
        } => {
            log::info!(
                "command=discover snapshot={} output={}",
                snapshot.display(),
                output.display()
            );
            let summary = discover(DiscoverOptions {
                snapshot_dir: snapshot,
                output_dir: output,
                config_path: config,
                min_score,
                expected_positive_count,
                source_scope,
                progress,
            })?;
            println!("{}", serde_json::to_string_pretty(&summary)?);
        }
        Command::CandidateAudit {
            candidates,
            config,
            output,
        } => {
            log::info!(
                "command=candidate-audit candidates={} output={}",
                candidates.display(),
                output.display()
            );
            let summary = candidate_audit(CandidateAuditOptions {
                candidates_jsonl: candidates,
                config_path: config,
                output_dir: output,
                progress,
            })?;
            println!("{}", serde_json::to_string_pretty(&summary)?);
        }
        Command::CompactSnapshot {
            snapshot,
            delete_all_project_pages,
            retain_accessions_file,
            prune_noncandidate_evidence,
            dry_run,
        } => {
            log::info!("command=compact-snapshot snapshot={}", snapshot.display());
            let summary = compact_snapshot(CompactSnapshotOptions {
                snapshot_dir: snapshot,
                keep_latest_project_page: !delete_all_project_pages,
                retain_accessions_file,
                prune_noncandidate_evidence,
                dry_run,
                progress,
            })?;
            println!("{}", serde_json::to_string_pretty(&summary)?);
        }
        Command::RecallAudit {
            candidates,
            known_positives,
            repository_filter,
            output,
        } => {
            log::info!(
                "command=recall-audit candidates={} benchmark={}",
                candidates.display(),
                known_positives.display()
            );
            let summary = recall_audit(RecallAuditOptions {
                candidates_tsv: candidates,
                benchmark_csv: known_positives,
                output_dir: output,
                repository_filter,
                progress,
            })?;
            println!("{}", serde_json::to_string_pretty(&summary)?);
        }
        Command::ExportPython {
            candidates,
            candidates_jsonl,
            config,
            output,
            min_tier,
        } => {
            log::info!(
                "command=export-python candidates={} output={}",
                candidates.display(),
                output.display()
            );
            let summary = export_python(ExportPythonOptions {
                candidates_tsv: candidates,
                candidates_jsonl,
                config_path: config,
                output_dir: output,
                min_tier,
                progress,
            })?;
            println!("{}", serde_json::to_string_pretty(&summary)?);
        }
        Command::SdrfResolve {
            accessions,
            accessions_file,
            snapshot,
            output,
            timeout,
            force,
        } => {
            log::info!(
                "command=sdrf-resolve snapshot={} output={}",
                snapshot.display(),
                output.display()
            );
            let summary = resolve_sdrf_sources(SdrfResolveOptions {
                snapshot_dir: snapshot,
                output_dir: output,
                accessions,
                accessions_file,
                timeout_seconds: timeout,
                force,
                progress,
            })
            .await?;
            println!("{}", serde_json::to_string_pretty(&summary)?);
        }
        Command::SdrfAcquireEvidence {
            acquisition_plan,
            output,
            api_base,
            timeout,
            max_candidates_per_accession,
            force,
        } => {
            log::info!(
                "command=sdrf-acquire-evidence plan={} output={} api={}",
                acquisition_plan.display(),
                output.display(),
                api_base
            );
            let summary = acquire_sdrf_evidence(SdrfAcquireEvidenceOptions {
                acquisition_plan,
                output_dir: output,
                api_base,
                timeout_seconds: timeout,
                max_candidates_per_accession,
                force,
                progress,
            })
            .await?;
            println!("{}", serde_json::to_string_pretty(&summary)?);
        }
        Command::SdrfAudit {
            accessions,
            accessions_file,
            snapshot,
            resolved_sdrf_dir,
            output,
        } => {
            log::info!(
                "command=sdrf-audit snapshot={} resolved={} output={}",
                snapshot.display(),
                resolved_sdrf_dir.display(),
                output.display()
            );
            let summary = audit_sdrf_sources(SdrfAuditOptions {
                snapshot_dir: snapshot,
                resolved_sdrf_dir,
                output_dir: output,
                accessions,
                accessions_file,
                progress,
            })?;
            println!("{}", serde_json::to_string_pretty(&summary)?);
        }
        Command::SdrfAnnotate {
            accessions,
            accessions_file,
            snapshot,
            annotations_dir,
            publication_manifest,
            manuscript_text,
            resolved_sdrf_dir,
            explicit_row_mapping_manifest,
            output,
            model,
            ollama_url,
            timeout,
            max_evidence_items,
            max_evidence_chars,
            max_files_in_prompt,
            force,
        } => {
            log::info!(
                "command=sdrf-annotate snapshot={} output={} model={}",
                snapshot.display(),
                output.display(),
                model
            );
            let summary = annotate_sdrf(SdrfAnnotateOptions {
                snapshot_dir: snapshot,
                annotations_dir,
                publication_manifest: publication_manifest
                    .is_file()
                    .then_some(publication_manifest),
                manuscript_text_paths: manuscript_text,
                output_dir: output,
                accessions,
                accessions_file,
                resolved_sdrf_dir,
                explicit_row_mapping_manifest,
                model,
                ollama_url,
                timeout_seconds: timeout,
                max_evidence_items,
                max_evidence_chars,
                max_files_in_prompt,
                force,
                progress,
            })
            .await?;
            println!("{}", serde_json::to_string_pretty(&summary)?);
        }
        Command::SdrfAgent {
            accessions,
            accessions_file,
            snapshot,
            annotations_dir,
            publication_manifest,
            manuscript_text,
            resolved_sdrf_dir,
            explicit_row_mapping_manifest,
            output,
            model,
            ollama_url,
            timeout,
            max_evidence_items,
            max_evidence_chars,
            max_files_in_prompt,
            max_agent_turns,
            max_tool_actions,
            max_validator_cycles,
            force,
        } => {
            log::info!(
                "command=sdrf-agent snapshot={} output={} model={} turns={} actions={} validators={}",
                snapshot.display(),
                output.display(),
                model,
                max_agent_turns,
                max_tool_actions,
                max_validator_cycles
            );
            let summary = run_scientific_sdrf_agent(SdrfScientificAgentOptions {
                snapshot_dir: snapshot,
                annotations_dir,
                publication_manifest: publication_manifest
                    .is_file()
                    .then_some(publication_manifest),
                manuscript_text_paths: manuscript_text,
                output_dir: output,
                accessions,
                accessions_file,
                resolved_sdrf_dir,
                explicit_row_mapping_manifest,
                model,
                ollama_url,
                timeout_seconds: timeout,
                max_evidence_items,
                max_evidence_chars,
                max_files_in_prompt,
                max_agent_turns,
                max_tool_actions,
                max_validator_cycles,
                force,
                progress,
            })
            .await?;
            println!("{}", serde_json::to_string_pretty(&summary)?);
        }
    }

    log::info!("command finished in {}", format_duration(started.elapsed()));
    Ok(())
}
