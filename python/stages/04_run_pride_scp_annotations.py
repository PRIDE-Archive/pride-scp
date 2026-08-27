#!/usr/bin/env python3
"""
Stage 4: run pride_scp_targeted_ollama.py for screened accession/PDF pairs.

Two execution modes:
  * Local batch: --workers N
  * Cluster/SLURM array: --row-index N
    If --row-index is omitted and SLURM_ARRAY_TASK_ID is present, that value
    is used automatically.

Each job writes an independent status JSON, avoiding shared-file write races.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from pride_scp_pipeline_common import (
    publication_key,
    read_tsv,
    safe_slug,
    text_value,
    validate_pdf_path,
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest_tsv")
    parser.add_argument(
        "--targeted-script",
        required=True,
        help="Path to your existing pride_scp_targeted_ollama.py.",
    )
    parser.add_argument(
        "--output-dir",
        default="pride_scp_annotations",
    )
    parser.add_argument(
        "--model",
        default="qwen2.5:3b",
    )
    parser.add_argument(
        "--cpu-threads",
        type=int,
        default=4,
    )
    parser.add_argument(
        "--ollama-url",
        default="http://localhost:11434/api/generate",
    )
    parser.add_argument(
        "--screen-decisions",
        default="candidate,uncertain",
        help=(
            "Comma-separated screening decisions to annotate when the "
            "manifest contains scp_screen_decision. Default: "
            "candidate,uncertain. Use --all-valid-content to bypass screening."
        ),
    )
    parser.add_argument(
        "--all-valid-pdfs",
        action="store_true",
        help="Backward-compatible alias: annotate every valid publication PDF.",
    )
    parser.add_argument(
        "--all-valid-content",
        action="store_true",
        help=(
            "Ignore scp_screen_decision and annotate every valid publication "
            "content artifact (PDF or normalized full text)."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Local concurrent annotations. Keep at 1 for one local CPU/Ollama "
            "server; use SLURM arrays for real parallelism."
        ),
    )
    parser.add_argument(
        "--row-index",
        type=int,
        default=None,
        help="Run one zero-based valid job index.",
    )
    parser.add_argument(
        "--print-job-count",
        action="store_true",
        help="Print the number of valid accession/PDF jobs and exit.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
    )
    parser.add_argument(
        "--no-save-evidence",
        action="store_true",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
    )
    parser.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        help="Additional argument passed verbatim to the targeted script.",
    )
    return parser.parse_args()


def valid_text_path(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 500:
        return False
    try:
        return len(path.read_text(encoding="utf-8").strip()) >= 500
    except (OSError, UnicodeError):
        return False


def make_jobs(
    rows: list[dict[str, str]],
    *,
    allowed_screen_decisions: set[str],
    all_valid_content: bool,
) -> list[dict[str, Any]]:
    jobs = []

    for row_number, row in enumerate(rows):
        content_kind = text_value(row.get("publication_content_kind")).lower()
        content_path_text = text_value(row.get("publication_content_path"))
        content_status = text_value(row.get("publication_content_status")).lower()

        # Backward-compatible PDF-only manifests remain valid.
        if not content_kind:
            pdf_status = text_value(row.get("pdf_status")).lower()
            pdf_path = Path(text_value(row.get("pdf_path")))
            if pdf_status in {"downloaded", "already_exists"} and validate_pdf_path(pdf_path):
                content_kind = "pdf"
                content_path_text = str(pdf_path)
                content_status = "available"

        content_path = Path(content_path_text) if content_path_text else Path()
        if content_status != "available":
            continue
        if content_kind == "pdf":
            if not content_path_text or not validate_pdf_path(content_path):
                continue
        elif content_kind in {"fulltext_xml", "fulltext_html", "text", "fulltext_text"}:
            if not content_path_text or not valid_text_path(content_path):
                continue
        else:
            continue

        accession = text_value(row.get("accession")).upper()
        if not accession.startswith("PXD"):
            continue

        screen_decision = text_value(row.get("scp_screen_decision")).lower()

        if (
            not all_valid_content
            and "scp_screen_decision" in row
            and screen_decision not in allowed_screen_decisions
        ):
            continue

        pub_index = text_value(row.get("publication_index")) or "0"
        pubkey = safe_slug(publication_key(row), max_len=80)
        job_id = f"{accession}__pub{pub_index}__{pubkey}"

        jobs.append(
            {
                "job_index": len(jobs),
                "manifest_row_number": row_number,
                "job_id": job_id,
                "accession": accession,
                "content_kind": content_kind,
                "content_path": str(content_path.resolve()),
                "content_source": text_value(row.get("publication_content_source")),
                "publication_doi": text_value(row.get("publication_doi")),
                "publication_title": text_value(row.get("publication_title")),
                "publication_index": pub_index,
                "scp_screen_decision": screen_decision,
                "scp_screen_score": text_value(row.get("scp_screen_score")),
                "scp_screen_reason": text_value(row.get("scp_screen_reason")),
            }
        )

    return jobs


def run_job(job: dict[str, Any], args) -> dict[str, Any]:
    output_root = Path(args.output_dir)
    annotation_dir = output_root / "annotations" / job["accession"]
    log_dir = output_root / "logs"
    status_dir = output_root / "status"
    annotation_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    status_dir.mkdir(parents=True, exist_ok=True)

    annotation_path = annotation_dir / f"{job['job_id']}.json"
    log_path = log_dir / f"{job['job_id']}.log"
    status_path = status_dir / f"{job['job_id']}.json"

    if annotation_path.exists() and not args.force:
        status = {
            **job,
            "status": "already_exists",
            "annotation_path": str(annotation_path.resolve()),
            "log_path": str(log_path.resolve()),
            "returncode": 0,
            "wall_seconds": 0.0,
        }
        status_path.write_text(
            json.dumps(status, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return status

    cmd = [
        args.python,
        str(Path(args.targeted_script).resolve()),
    ]
    if job["content_kind"] == "pdf":
        cmd.extend(["--pdf", job["content_path"]])
    else:
        cmd.extend(["--source-text", job["content_path"]])
    cmd.extend([
        "--target-accession",
        job["accession"],
        "--publication-title",
        job["publication_title"],
        "--publication-doi",
        job["publication_doi"],
        "--model",
        args.model,
        "--cpu-threads",
        str(args.cpu_threads),
        "--ollama-url",
        args.ollama_url,
        "--output",
        str(annotation_path.resolve()),
    ])

    if not args.no_save_evidence:
        cmd.append("--save-evidence")

    cmd.extend(args.extra_arg)

    started = time.perf_counter()

    with log_path.open("w", encoding="utf-8") as log_handle:
        log_handle.write("COMMAND:\n")
        log_handle.write(" ".join(cmd) + "\n\n")
        log_handle.flush()
        proc = subprocess.run(
            cmd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )

    wall = time.perf_counter() - started

    status = {
        **job,
        "status": "success" if proc.returncode == 0 and annotation_path.exists() else "error",
        "annotation_path": str(annotation_path.resolve()),
        "log_path": str(log_path.resolve()),
        "returncode": proc.returncode,
        "wall_seconds": round(wall, 3),
        "command": cmd,
    }

    status_path.write_text(
        json.dumps(status, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return status


def main():
    args = parse_args()
    rows = read_tsv(Path(args.manifest_tsv))

    allowed_screen_decisions = {
        value.strip().lower()
        for value in args.screen_decisions.split(",")
        if value.strip()
    }

    jobs = make_jobs(
        rows,
        allowed_screen_decisions=allowed_screen_decisions,
        all_valid_content=(args.all_valid_content or args.all_valid_pdfs),
    )

    if args.print_job_count:
        print(len(jobs))
        return

    print(f"Valid publication-content annotation jobs: {len(jobs):,}")
    if not (args.all_valid_content or args.all_valid_pdfs) and rows and "scp_screen_decision" in rows[0]:
        print(
            "Accepted screen decisions: "
            + ",".join(sorted(allowed_screen_decisions))
        )

    row_index = args.row_index
    if row_index is None and os.environ.get("SLURM_ARRAY_TASK_ID") is not None:
        row_index = int(os.environ["SLURM_ARRAY_TASK_ID"])

    if row_index is not None:
        if row_index < 0 or row_index >= len(jobs):
            raise SystemExit(
                f"--row-index {row_index} is out of range for {len(jobs)} jobs"
            )
        job = jobs[row_index]
        print(
            f"Running job {row_index}: {job['accession']} "
            f"{job['publication_doi'] or job['publication_title']}"
        )
        status = run_job(job, args)
        print(json.dumps(status, indent=2))
        raise SystemExit(0 if status["status"] in {"success", "already_exists"} else 1)

    # Local mode. Multiple local Ollama calls can contend for the same CPU/GPU;
    # default is deliberately 1.
    if args.workers <= 1:
        for i, job in enumerate(jobs, start=1):
            print(f"[{i}/{len(jobs)}] {job['accession']} {job['publication_doi']}")
            status = run_job(job, args)
            print(f"  -> {status['status']} ({status['wall_seconds']} s)")
        return

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_job, job, args): job
            for job in jobs
        }
        completed = 0
        for future in as_completed(futures):
            job = futures[future]
            try:
                status = future.result()
            except Exception as exc:
                status = {
                    "status": "runner_exception",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            completed += 1
            print(
                f"[{completed}/{len(jobs)}] {job['accession']} "
                f"-> {status.get('status')}"
            )


if __name__ == "__main__":
    main()
