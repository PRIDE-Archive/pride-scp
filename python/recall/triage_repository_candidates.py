#!/usr/bin/env python3
"""
Non-destructive semantic triage for Rust-discovered PRIDE SCP candidates.

This helper is intentionally NOT a final classifier and never removes a
candidate. It exists to prioritize candidates that lack a usable publication
PDF, using only repository/file/SDRF evidence emitted by the Rust discovery
layer.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path
from typing import Any

import requests

DEFAULT_OLLAMA_URL = "http://localhost:11434/api/generate"

SCHEMA = {
    "type": "object",
    "properties": {
        "triage_class": {
            "type": "string",
            "enum": [
                "likely_true_scp",
                "possible_true_scp",
                "adjacent_or_benchmark",
                "unlikely_true_scp",
                "uncertain",
            ],
        },
        "individual_cell_measurement_evidence": {
            "type": "string",
            "enum": ["direct", "implied", "absent", "contradictory"],
        },
        "mass_spectrometry_proteomics_evidence": {
            "type": "string",
            "enum": ["direct", "implied", "absent"],
        },
        "reason": {"type": "string", "maxLength": 500},
    },
    "required": [
        "triage_class",
        "individual_cell_measurement_evidence",
        "mass_spectrometry_proteomics_evidence",
        "reason",
    ],
    "additionalProperties": False,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidates_jsonl")
    parser.add_argument("--output-dir", default="work/repository_triage")
    parser.add_argument("--model", default="qwen2.5:3b")
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--num-ctx", type=int, default=4096)
    parser.add_argument("--num-predict", type=int, default=260)
    parser.add_argument("--keep-alive", default="5m")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--evidence-chars", type=int, default=6000)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--accession", action="append", default=[])
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def load_candidates(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def evidence_text(candidate: dict[str, Any], max_chars: int) -> str:
    pieces = [
        f"PRIDE accession: {candidate.get('accession', '')}",
        f"Dataset title: {candidate.get('dataset_title', '')}",
        f"Dataset description: {candidate.get('dataset_description', '')}",
        f"Discovery tier: {candidate.get('tier', '')}",
        "Discovery evidence:",
    ]
    for hit in candidate.get("hits", []) or []:
        if not isinstance(hit, dict):
            continue
        pieces.append(
            "- lane={lane}; label={label}; term={term}; excerpt={excerpt}".format(
                lane=hit.get("lane", ""),
                label=hit.get("label", ""),
                term=hit.get("term", ""),
                excerpt=hit.get("source_excerpt", ""),
            )
        )
    text = "\n".join(pieces)
    return text[:max_chars]


def call_ollama(candidate: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    system = (
        "You triage public PRIDE proteomics datasets for possible true "
        "single-cell mass-spectrometry proteomics. Be recall-oriented but "
        "evidence-grounded. True SCP means proteomic measurement preserving "
        "the identity of an individual biological cell. Low-input bulk or "
        "single-cell-equivalent digests are adjacent benchmarks, not true "
        "single cells. Sorted populations containing many cells are not true "
        "SCP. Multiplexing individually prepared/labeled cells can still be "
        "true SCP. Repository evidence can be incomplete, so use 'possible' "
        "or 'uncertain' instead of guessing. This is triage only."
    )
    prompt = (
        "Classify the candidate using only the supplied repository evidence. "
        "Do not use outside knowledge. Method names alone may justify a "
        "possible candidate but not direct individual-cell evidence.\n\n"
        + evidence_text(candidate, args.evidence_chars)
    )
    payload = {
        "model": args.model,
        "system": system,
        "prompt": prompt,
        "stream": False,
        "format": SCHEMA,
        "keep_alive": args.keep_alive,
        "options": {
            "temperature": 0,
            "num_ctx": args.num_ctx,
            "num_predict": args.num_predict,
            "num_thread": args.cpu_threads,
        },
    }
    started = time.perf_counter()
    response = requests.post(args.ollama_url, json=payload, timeout=args.timeout)
    response.raise_for_status()
    data = response.json()
    parsed = json.loads(data.get("response", "{}"))
    return {
        "accession": candidate.get("accession", ""),
        "run_status": "success",
        "model": args.model,
        "wall_seconds": round(time.perf_counter() - started, 3),
        **parsed,
    }


def write_summary(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "accession",
        "run_status",
        "triage_class",
        "individual_cell_measurement_evidence",
        "mass_spectrometry_proteomics_evidence",
        "reason",
        "model",
        "wall_seconds",
        "error",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fields})


def main() -> None:
    args = parse_args()
    input_path = Path(args.candidates_jsonl)
    output_dir = Path(args.output_dir)
    status_dir = output_dir / "status"
    output_dir.mkdir(parents=True, exist_ok=True)
    status_dir.mkdir(parents=True, exist_ok=True)

    selected = {x.upper() for x in args.accession}
    candidates = [
        row
        for row in load_candidates(input_path)
        if not selected or str(row.get("accession", "")).upper() in selected
    ]
    if args.limit > 0:
        candidates = candidates[: args.limit]

    rows: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        accession = str(candidate.get("accession", ""))
        status_path = status_dir / f"{accession}.json"
        if status_path.is_file() and not args.force:
            row = json.loads(status_path.read_text(encoding="utf-8"))
            rows.append(row)
            print(f"[{index}/{len(candidates)}] {accession} -> cached/{row.get('triage_class','')}")
            continue
        try:
            row = call_ollama(candidate, args)
        except Exception as exc:
            row = {
                "accession": accession,
                "run_status": "error",
                "model": args.model,
                "error": f"{type(exc).__name__}: {exc}",
            }
        status_path.write_text(
            json.dumps(row, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        rows.append(row)
        print(
            f"[{index}/{len(candidates)}] {accession} -> "
            f"{row.get('run_status')}/{row.get('triage_class','')}"
        )

    rows.sort(key=lambda x: str(x.get("accession", "")))
    write_summary(output_dir / "repository_semantic_triage.tsv", rows)
    summary = {
        "candidates": len(rows),
        "successful": sum(x.get("run_status") == "success" for x in rows),
        "errors": sum(x.get("run_status") == "error" for x in rows),
        "class_counts": {},
        "note": "Non-destructive triage only; no candidate is removed automatically.",
    }
    for row in rows:
        cls = row.get("triage_class", "")
        if cls:
            summary["class_counts"][cls] = summary["class_counts"].get(cls, 0) + 1
    (output_dir / "triage_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
