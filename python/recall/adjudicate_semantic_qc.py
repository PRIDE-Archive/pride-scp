#!/usr/bin/env python3
"""Independent small-LLM QC for the v0.1.8 unified semantic queue.

The deterministic unifier intentionally does not turn the two Qwen lanes into
final catalogue labels.  This helper supplies an *independent* strict reviewer
(default Phi-4-mini) and an optional selective jury (default Gemma 3 4B).

It is designed to be resumable, CPU-friendly, and memory-safe: the critic is
run as one phase, explicitly unloaded, then the jury is loaded only for
selected conflicts/uncertainties.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

import requests

DEFAULT_OLLAMA_URL = "http://localhost:11434/api/generate"

SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["include", "exclude", "uncertain"],
        },
        "individual_cell_measurement": {
            "type": "string",
            "enum": ["direct", "implied", "absent", "contradictory"],
        },
        "ms_proteomics_measurement": {
            "type": "string",
            "enum": ["direct", "implied", "absent"],
        },
        "population_or_bulk_contradiction": {
            "type": "string",
            "enum": ["present", "absent", "uncertain"],
        },
        "adjacent_only": {
            "type": "string",
            "enum": ["yes", "no", "uncertain"],
        },
        "evidence_quote": {"type": "string", "maxLength": 400},
        "reason": {"type": "string", "maxLength": 700},
    },
    "required": [
        "decision",
        "individual_cell_measurement",
        "ms_proteomics_measurement",
        "population_or_bulk_contradiction",
        "adjacent_only",
        "evidence_quote",
        "reason",
    ],
    "additionalProperties": False,
}


class OllamaUnavailableError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument(
        "input_jsonl",
        nargs="?",
        default="work/python/semantic_unification/qc_candidate_queue.jsonl",
        help=(
            "Unified candidate JSONL. Default is provisional includes + all review routes. "
            "Pass unified_semantic_manifest.jsonl to QC all 321 candidates."
        ),
    )
    p.add_argument("--output-dir", default="work/python/semantic_qc")
    p.add_argument("--critic-model", default="phi4-mini:3.8b")
    p.add_argument("--jury-model", default="gemma3:4b")
    p.add_argument("--no-jury", action="store_true")
    p.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    p.add_argument("--cpu-threads", type=int, default=4)
    p.add_argument("--num-ctx", type=int, default=8192)
    p.add_argument("--num-predict", type=int, default=360)
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--retry-backoff", type=float, default=3.0)
    p.add_argument("--critic-keep-alive", default="30m")
    p.add_argument("--jury-keep-alive", default="10m")
    p.add_argument("--evidence-chars", type=int, default=12000)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--accession", action="append", default=[])
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def as_lines(value: Any) -> list[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, list):
        return [text(x) for x in value if text(x)]
    return [text(value)]


def evidence_text(row: dict[str, Any], max_chars: int) -> str:
    pieces = [
        f"PRIDE accession: {text(row.get('accession'))}",
        f"Dataset title: {text(row.get('dataset_title'))}",
        f"Dataset description: {text(row.get('dataset_description'))}",
        f"Evidence mode: {text(row.get('semantic_evidence_mode'))}",
        f"Discovery tier: {text(row.get('discovery_tier'))}",
        f"Discovery semantic priority: {text(row.get('semantic_priority'))}",
        f"Discovery evidence profile: {text(row.get('evidence_profile'))}",
        f"Specific SCP labels: {row.get('specific_scp_labels', [])}",
        f"Method labels: {row.get('method_labels', [])}",
        f"Broad-context labels: {row.get('broad_context_labels', [])}",
        f"Adjacent labels: {row.get('adjacent_labels', [])}",
        f"Negative-context labels: {row.get('negative_context_labels', [])}",
        f"Deterministic unification route: {text(row.get('unified_route'))}",
        f"Unification reason: {text(row.get('unified_route_reason'))}",
        f"Review flags: {row.get('review_flags', [])}",
    ]

    if text(row.get("semantic_evidence_mode")) == "publication_backed":
        pieces.extend(
            [
                f"Stage-04 final classification: {text(row.get('stage04_classification'))}",
                f"Stage-04 model classification before gating: {text(row.get('stage04_model_classification'))}",
                f"Stage-04 gate tiers: {row.get('stage04_gate_tiers', [])}",
                f"Publication title(s): {row.get('stage04_publication_titles', [])}",
                f"Target accession mentioned in publication: {row.get('stage04_accession_mentioned', '')}",
                f"Grounded Stage-04 sample count: {row.get('stage04_grounded_sample_count', 0)}",
            ]
        )
        for reason in as_lines(row.get("stage04_gate_reasons")):
            pieces.append(f"Stage-04 gate reason: {reason}")
        for evidence in as_lines(row.get("stage04_gate_evidence")):
            pieces.append(f"Stage-04 retained evidence: {evidence}")
        for sample in as_lines(row.get("stage04_sample_summaries")):
            pieces.append(f"Stage-04 grounded sample: {sample}")
        for warning in as_lines(row.get("stage04_validation_warnings")):
            pieces.append(f"Stage-04 validation warning: {warning}")
    else:
        pieces.extend(
            [
                f"Repository Qwen triage class: {text(row.get('repository_triage_class'))}",
                f"Repository individual-cell evidence field: {text(row.get('repository_individual_cell_evidence'))}",
                f"Repository MS/proteomics evidence field: {text(row.get('repository_ms_proteomics_evidence'))}",
                f"Repository normalized evidence: {text(row.get('repository_normalized_evidence'))}",
                f"Repository triage consistency: {text(row.get('repository_triage_consistency'))}",
                f"Repository triage reason: {text(row.get('repository_triage_reason'))}",
            ]
        )

    for hit in row.get("discovery_hits", []) or []:
        if not isinstance(hit, dict):
            continue
        pieces.append(
            "Discovery hit: lane={lane}; label={label}; term={term}; excerpt={excerpt}".format(
                lane=text(hit.get("lane")),
                label=text(hit.get("label")),
                term=text(hit.get("term")),
                excerpt=text(hit.get("source_excerpt")),
            )
        )

    return "\n".join(pieces)[:max_chars]


def system_prompt() -> str:
    return (
        "You are an independent strict QC reviewer for a PRIDE single-cell proteomics catalogue. "
        "Use ONLY the supplied evidence; do not use outside knowledge. True SCP requires mass-"
        "spectrometry proteomic measurement in which the identity of an individual biological cell "
        "is preserved through measurement or through individual preparation/labeling before valid "
        "multiplexing. Exclude bulk tissue, bulk/cell-line proteomics, sorted populations containing "
        "many cells, pooled cells before measurement, mini-bulk, single-cell-equivalent/diluted bulk "
        "benchmarks without actual biological single cells, 'single cell type' or cell-type-resolved "
        "bulk/spatial samples, scRNA-seq or imaging combined with bulk proteomics, and method names "
        "that merely could support SCP. A paper/workflow may be about SCP but a particular PXD can "
        "still be a benchmark or companion bulk dataset. If the evidence does not establish both an "
        "individual biological cell and MS proteomics, choose uncertain rather than guessing."
    )


def critic_prompt(row: dict[str, Any], max_chars: int) -> str:
    return (
        "Independently adjudicate whether this PRIDE accession itself should be included as a true "
        "single-cell mass-spectrometry proteomics dataset. Treat previous Qwen classifications and "
        "deterministic routes only as claims to verify, not authority. The evidence_quote must be a "
        "short exact phrase copied from the supplied evidence that most directly supports your "
        "decision; use an empty string if there is no direct phrase.\n\n"
        + evidence_text(row, max_chars)
    )


def jury_prompt(row: dict[str, Any], critic: dict[str, Any], max_chars: int) -> str:
    return (
        "Act as a second independent jury reviewer. Re-evaluate the accession from the supplied "
        "evidence and the strict true-SCP definition. The critic result is shown only so you can "
        "identify the disputed point; do not defer to it. If the evidence is insufficient, return "
        "uncertain.\n\n"
        f"CRITIC RESULT: {json.dumps(critic, ensure_ascii=False, sort_keys=True)}\n\n"
        + evidence_text(row, max_chars)
    )


def keep_alive_value(value: str) -> Any:
    v = text(value)
    return 0 if v in {"0", "0s", "0m", "0h"} else (v or "5m")


def transient(code: int) -> bool:
    return code in {429, 500, 502, 503, 504}


def post_generate(
    *,
    url: str,
    model: str,
    prompt: str,
    args: argparse.Namespace,
    keep_alive: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = {
        "model": model,
        "system": system_prompt(),
        "prompt": prompt,
        "stream": False,
        "format": SCHEMA,
        "keep_alive": keep_alive_value(keep_alive),
        "options": {
            "temperature": 0,
            "seed": 42,
            "num_ctx": args.num_ctx,
            "num_predict": args.num_predict,
            "num_thread": args.cpu_threads,
        },
    }
    started = time.perf_counter()
    for attempt in range(args.retries + 1):
        try:
            response = requests.post(url, json=payload, timeout=args.timeout)
            if response.status_code >= 400:
                body = response.text[:800]
                if response.status_code == 404:
                    raise RuntimeError(
                        f"Ollama model {model!r} is unavailable. Try: ollama pull {model}. {body}"
                    )
                if transient(response.status_code) and attempt < args.retries:
                    time.sleep(args.retry_backoff * (2**attempt))
                    continue
                response.raise_for_status()
            data = response.json()
            parsed = json.loads(text(data.get("response")) or "{}")
            stats = {
                "wall_seconds": round(time.perf_counter() - started, 3),
                "request_attempts": attempt + 1,
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "output_tokens": data.get("eval_count", 0),
                "prompt_seconds": round(data.get("prompt_eval_duration", 0) / 1e9, 3),
                "generation_seconds": round(data.get("eval_duration", 0) / 1e9, 3),
            }
            return parsed, stats
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt < args.retries:
                time.sleep(args.retry_backoff * (2**attempt))
                continue
            raise OllamaUnavailableError(
                f"Ollama unavailable for {model}: {type(exc).__name__}: {exc}"
            ) from exc
    raise RuntimeError(f"Ollama request failed for {model}")


def unload_model(args: argparse.Namespace, model: str) -> None:
    payload = {"model": model, "prompt": "", "stream": False, "keep_alive": 0}
    for attempt in range(args.retries + 1):
        try:
            response = requests.post(args.ollama_url, json=payload, timeout=args.timeout)
            if response.status_code < 400 or response.status_code == 404:
                return
            if transient(response.status_code) and attempt < args.retries:
                time.sleep(args.retry_backoff * (2**attempt))
                continue
            response.raise_for_status()
        except (requests.ConnectionError, requests.Timeout):
            if attempt < args.retries:
                time.sleep(args.retry_backoff * (2**attempt))
                continue
            return


def jury_required(row: dict[str, Any], critic: dict[str, Any]) -> tuple[bool, str]:
    decision = text(critic.get("decision")).lower()
    route = text(row.get("unified_route"))
    flags = set(row.get("review_flags") or [])
    if decision == "uncertain":
        return True, "critic_uncertain"
    if route in {"include_candidate", "review_high"} and decision == "exclude":
        return True, "high_recall_route_vs_exclude"
    if route == "review_low" and decision == "include":
        return True, "weak_route_vs_include"
    if decision == "include" and (
        "repository_qwen_overcall" in flags
        or "contradictory_cell_evidence" in flags
        or "negative_context_present" in flags
        or "adjacent_discovery_label" in flags
    ):
        return True, "include_despite_contradiction_flag"
    return False, ""


def combine_decisions(
    critic: dict[str, Any], jury: dict[str, Any] | None
) -> tuple[str, str]:
    c = text(critic.get("decision")).lower()
    if jury is None:
        return c, "critic_only"
    j = text(jury.get("decision")).lower()
    if c == "uncertain" and j in {"include", "exclude"}:
        return j, "jury_resolved_critic_uncertainty"
    if c == j and c in {"include", "exclude"}:
        return c, "critic_jury_agree"
    return "uncertain", "critic_jury_disagree_or_jury_uncertain"


def write_tsv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "accession",
        "unified_route",
        "semantic_evidence_mode",
        "semantic_priority",
        "critic_decision",
        "critic_individual_cell_measurement",
        "critic_ms_proteomics_measurement",
        "critic_population_or_bulk_contradiction",
        "critic_adjacent_only",
        "critic_evidence_quote",
        "critic_reason",
        "jury_trigger",
        "jury_decision",
        "jury_individual_cell_measurement",
        "jury_ms_proteomics_measurement",
        "jury_population_or_bulk_contradiction",
        "jury_adjacent_only",
        "jury_evidence_quote",
        "jury_reason",
        "final_decision",
        "final_basis",
        "critic_model",
        "jury_model",
        "critic_wall_seconds",
        "jury_wall_seconds",
        "error",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def write_review_decisions(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = ["accession", "final_decision", "review_note"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "accession": row.get("accession", ""),
                    "final_decision": row.get("final_decision", "uncertain"),
                    "review_note": (
                        f"independent semantic QC ({row.get('final_basis','')}): "
                        f"{row.get('critic_reason','')}"
                        + (
                            f" | jury: {row.get('jury_reason','')}"
                            if row.get("jury_reason")
                            else ""
                        )
                    )[:1500],
                }
            )


def main() -> None:
    args = parse_args()
    input_path = Path(args.input_jsonl)
    output_dir = Path(args.output_dir)
    critic_dir = output_dir / "critic_status"
    jury_dir = output_dir / "jury_status"
    critic_dir.mkdir(parents=True, exist_ok=True)
    jury_dir.mkdir(parents=True, exist_ok=True)

    selected = {x.upper() for x in args.accession}
    rows = [
        r for r in read_jsonl(input_path)
        if not selected or text(r.get("accession")).upper() in selected
    ]
    if args.limit > 0:
        rows = rows[: args.limit]

    critic_results: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    print(f"Independent semantic QC candidates: {len(rows)}")
    print(f"Critic model: {args.critic_model}")

    for i, row in enumerate(rows, start=1):
        accession = text(row.get("accession"))
        path = critic_dir / f"{accession}.json"
        if path.is_file() and not args.force:
            payload = json.loads(path.read_text(encoding="utf-8"))
            critic_results[accession] = payload
            print(f"[critic {i}/{len(rows)}] {accession} -> cached/{payload.get('decision','')}")
            continue
        try:
            parsed, stats = post_generate(
                url=args.ollama_url,
                model=args.critic_model,
                prompt=critic_prompt(row, args.evidence_chars),
                args=args,
                keep_alive=args.critic_keep_alive,
            )
            payload = {
                "accession": accession,
                "model": args.critic_model,
                **parsed,
                "stats": stats,
            }
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            critic_results[accession] = payload
            print(f"[critic {i}/{len(rows)}] {accession} -> {parsed.get('decision','')}")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            errors[accession] = error
            path.write_text(json.dumps({"accession": accession, "error": error}, indent=2), encoding="utf-8")
            print(f"[critic {i}/{len(rows)}] {accession} -> ERROR {error}")

    unload_model(args, args.critic_model)

    jury_targets: list[tuple[dict[str, Any], str]] = []
    if not args.no_jury and args.jury_model:
        for row in rows:
            accession = text(row.get("accession"))
            critic = critic_results.get(accession)
            if not critic or critic.get("error"):
                continue
            needed, trigger = jury_required(row, critic)
            if needed:
                jury_targets.append((row, trigger))

    jury_results: dict[str, dict[str, Any]] = {}
    print(f"Selective jury candidates: {len(jury_targets)}")
    if jury_targets:
        print(f"Jury model: {args.jury_model}")
    for i, (row, trigger) in enumerate(jury_targets, start=1):
        accession = text(row.get("accession"))
        path = jury_dir / f"{accession}.json"
        if path.is_file() and not args.force:
            payload = json.loads(path.read_text(encoding="utf-8"))
            jury_results[accession] = payload
            print(f"[jury {i}/{len(jury_targets)}] {accession} -> cached/{payload.get('decision','')}")
            continue
        try:
            parsed, stats = post_generate(
                url=args.ollama_url,
                model=args.jury_model,
                prompt=jury_prompt(row, critic_results[accession], args.evidence_chars),
                args=args,
                keep_alive=args.jury_keep_alive,
            )
            payload = {
                "accession": accession,
                "model": args.jury_model,
                "trigger": trigger,
                **parsed,
                "stats": stats,
            }
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            jury_results[accession] = payload
            print(f"[jury {i}/{len(jury_targets)}] {accession} -> {parsed.get('decision','')}")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            errors[accession] = errors.get(accession, "") + (" | " if errors.get(accession) else "") + error
            path.write_text(json.dumps({"accession": accession, "trigger": trigger, "error": error}, indent=2), encoding="utf-8")
            print(f"[jury {i}/{len(jury_targets)}] {accession} -> ERROR {error}")

    if jury_targets:
        unload_model(args, args.jury_model)

    final_rows: list[dict[str, Any]] = []
    for row in rows:
        accession = text(row.get("accession"))
        critic = critic_results.get(accession, {})
        jury = jury_results.get(accession)
        if not critic or critic.get("error"):
            final_decision, basis = "uncertain", "critic_error"
        elif jury is not None and jury.get("error"):
            final_decision, basis = "uncertain", "jury_error"
        else:
            final_decision, basis = combine_decisions(critic, jury)
        final_rows.append(
            {
                "accession": accession,
                "unified_route": row.get("unified_route", ""),
                "semantic_evidence_mode": row.get("semantic_evidence_mode", ""),
                "semantic_priority": row.get("semantic_priority", ""),
                "critic_decision": critic.get("decision", ""),
                "critic_individual_cell_measurement": critic.get("individual_cell_measurement", ""),
                "critic_ms_proteomics_measurement": critic.get("ms_proteomics_measurement", ""),
                "critic_population_or_bulk_contradiction": critic.get("population_or_bulk_contradiction", ""),
                "critic_adjacent_only": critic.get("adjacent_only", ""),
                "critic_evidence_quote": critic.get("evidence_quote", ""),
                "critic_reason": critic.get("reason", ""),
                "jury_trigger": jury.get("trigger", "") if jury else "",
                "jury_decision": jury.get("decision", "") if jury else "",
                "jury_individual_cell_measurement": jury.get("individual_cell_measurement", "") if jury else "",
                "jury_ms_proteomics_measurement": jury.get("ms_proteomics_measurement", "") if jury else "",
                "jury_population_or_bulk_contradiction": jury.get("population_or_bulk_contradiction", "") if jury else "",
                "jury_adjacent_only": jury.get("adjacent_only", "") if jury else "",
                "jury_evidence_quote": jury.get("evidence_quote", "") if jury else "",
                "jury_reason": jury.get("reason", "") if jury else "",
                "final_decision": final_decision,
                "final_basis": basis,
                "critic_model": args.critic_model,
                "jury_model": args.jury_model if jury else "",
                "critic_wall_seconds": (critic.get("stats") or {}).get("wall_seconds", ""),
                "jury_wall_seconds": ((jury or {}).get("stats") or {}).get("wall_seconds", ""),
                "error": errors.get(accession, ""),
            }
        )

    final_rows.sort(key=lambda r: r["accession"])
    write_tsv(output_dir / "semantic_qc_results.tsv", final_rows)
    write_review_decisions(output_dir / "review_decisions.tsv", final_rows)
    uncertain_rows = [r for r in final_rows if r["final_decision"] == "uncertain"]
    write_tsv(output_dir / "uncertain_for_manual_review.tsv", uncertain_rows)

    summary = {
        "candidates": len(final_rows),
        "critic_model": args.critic_model,
        "jury_model": "" if args.no_jury else args.jury_model,
        "critic_decision_counts": dict(Counter(r["critic_decision"] for r in final_rows if r["critic_decision"])),
        "jury_calls": len(jury_targets),
        "jury_decision_counts": dict(Counter(r["jury_decision"] for r in final_rows if r["jury_decision"])),
        "final_decision_counts": dict(Counter(r["final_decision"] for r in final_rows)),
        "uncertain_for_manual_review": len(uncertain_rows),
        "errors": sum(bool(r["error"]) for r in final_rows),
        "note": (
            "Independent QC decisions are overrides for the semantic-unification bridge. "
            "Uncertain decisions intentionally block final Stage-05 bridge generation."
        ),
    }
    (output_dir / "semantic_qc_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
