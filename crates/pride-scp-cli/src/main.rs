use anyhow::Result;
use clap::{Parser, Subcommand};
use pride_scp_discovery::{
    discover, export_python, recall_audit, DiscoverOptions, ExportPythonOptions, RecallAuditOptions,
};
use pride_scp_index::{snapshot, SnapshotOptions, DEFAULT_PRIDE_API, DEFAULT_PROJECT_PAGE_SIZE};
use std::path::PathBuf;

#[derive(Debug, Parser)]
#[command(
    name = "pride-scp",
    version,
    about = "Recall-first PRIDE single-cell proteomics discovery and audit tooling"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Snapshot PRIDE project metadata, file manifests, and SDRF where available.
    Snapshot {
        #[arg(long, default_value = "data/snapshot")]
        output: PathBuf,
        #[arg(long, default_value = DEFAULT_PRIDE_API)]
        api_base: String,
        #[arg(long, default_value_t = 8)]
        concurrency: usize,
        /// Per-request timeout, including response-body transfer.
        #[arg(long, default_value_t = 120)]
        timeout: u64,
        /// Number of retries after the initial HTTP attempt.
        #[arg(long, default_value_t = 4)]
        retries: usize,
        /// Page size used while enumerating PRIDE projects.
        #[arg(long, default_value_t = DEFAULT_PROJECT_PAGE_SIZE)]
        project_page_size: usize,
        #[arg(long, default_value = "PRIDE-SCP-recall-index/0.1.1")]
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
    },
    /// Measure candidate recall against a known-positive CSV benchmark.
    RecallAudit {
        #[arg(long, default_value = "data/discovery/candidates.tsv")]
        candidates: PathBuf,
        #[arg(long)]
        known_positives: PathBuf,
        #[arg(long, default_value = "data/recall_audit")]
        output: PathBuf,
    },
    /// Export a candidate accession list and manifest for the existing Python stages.
    ExportPython {
        #[arg(long, default_value = "data/discovery/candidates.tsv")]
        candidates: PathBuf,
        #[arg(long, default_value = "data/python_bridge")]
        output: PathBuf,
        #[arg(long, default_value = "weak")]
        min_tier: String,
    },
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::Snapshot {
            output,
            api_base,
            concurrency,
            timeout,
            retries,
            project_page_size,
            user_agent,
            no_files,
            no_sdrf,
            force,
            limit,
            accessions_file,
        } => {
            let summary = snapshot(SnapshotOptions {
                output_dir: output,
                api_base,
                concurrency,
                timeout_seconds: timeout,
                retries,
                project_page_size,
                user_agent,
                include_files: !no_files,
                include_sdrf: !no_sdrf,
                force,
                limit,
                accessions_file,
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
        } => {
            let summary = discover(DiscoverOptions {
                snapshot_dir: snapshot,
                output_dir: output,
                config_path: config,
                min_score,
                expected_positive_count,
            })?;
            println!("{}", serde_json::to_string_pretty(&summary)?);
        }
        Command::RecallAudit {
            candidates,
            known_positives,
            output,
        } => {
            let summary = recall_audit(RecallAuditOptions {
                candidates_tsv: candidates,
                benchmark_csv: known_positives,
                output_dir: output,
            })?;
            println!("{}", serde_json::to_string_pretty(&summary)?);
        }
        Command::ExportPython {
            candidates,
            output,
            min_tier,
        } => {
            let count = export_python(ExportPythonOptions {
                candidates_tsv: candidates,
                output_dir: output,
                min_tier,
            })?;
            println!("exported_candidates\t{count}");
        }
    }
    Ok(())
}
