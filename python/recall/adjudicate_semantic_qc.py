#!/usr/bin/env python3
"""Evidence-grounded independent semantic QC for recall-first PRIDE SCP.

v0.1.9 fixes the v0.1.8 failure mode where the critic/jury saw mostly
post-gating summaries and consequently returned `uncertain` for nearly every
candidate.  This version consumes `qc_evidence_packets.jsonl`, which contains
exact Stage-04 task passages (samples/preparation/performance), raw samples-task
outputs, and repository source excerpts.

The LLM reports factual axes.  A deterministic normalizer derives the catalogue
verdict from those axes so an over-cautious top-level `uncertain` cannot hide a
clear `individual cell + same-unit MS + no pooling` result, and an optimistic
`include` cannot override explicit population/pooling evidence.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

import requests

DEFAULT_OLLAMA_URL = "http://localhost:11434/api/generate"
QC_VERSION = "v0.1.9-evidence-grounded-qc-1"
VALID_DECISIONS = {"include", "exclude", "uncertain"}

SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["include", "exclude", "uncertain"]},
        "sample_unit": {
            "type": "string",
            "enum": [
                "individual_cell",
                "population_or_pool",
                "bulk_or_tissue",
                "benchmark_or_equivalent",
                "unclear",
            ],
        },
        "same_unit_ms_proteomics": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "target_dataset_scope": {
            "type": "string",
            "enum": ["supports_target", "not_target_specific", "contradicts_target", "unclear"],
        },
        "premeasurement_pooling": {"type": "string", "enum": ["present", "absent", "unclear"]},
        "benchmark_only": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "evidence_quote_cell": {"type": "string", "maxLength": 500},
        "evidence_quote_ms": {"type": "string", "maxLength": 500},
        "reason": {"type": "string", "maxLength": 900},
    },
    "required": [
        "decision",
        "sample_unit",
        "same_unit_ms_proteomics",
        "target_dataset_scope",
        "premeasurement_pooling",
        "benchmark_only",
        "evidence_quote_cell",
        "evidence_quote_ms",
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
        default="work/python/semantic_unification/qc_evidence_packets.jsonl",
    )
    p.add_argument("--output-dir", default="work/python/semantic_qc")
    p.add_argument("--critic-model", default="phi4-mini:3.8b")
    p.add_argument("--jury-model", default="gemma3:4b")
    p.add_argument("--no-jury", action="store_true")
    p.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    p.add_argument("--cpu-threads", type=int, default=4)
    p.add_argument("--num-ctx", type=int, default=8192)
    p.add_argument("--num-predict", type=int, default=420)
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--retry-backoff", type=float, default=3.0)
    p.add_argument("--critic-keep-alive", default="30m")
    p.add_argument("--jury-keep-alive", default="10m")
    p.add_argument("--evidence-chars", type=int, default=16000)
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


def system_prompt() -> str:
    return (
        "You are an independent evidence reviewer for a PRIDE single-cell mass-spectrometry "
        "proteomics catalogue. Use ONLY the PRIMARY SOURCE EVIDENCE supplied in the prompt. "
        "Previous Qwen labels, discovery routes, and titles are claims to verify, not authority. "
        "A true SCP dataset requires proteomic/mass-spectrometry measurement where the sample unit "
        "is an individual biological cell (including one egg/oocyte/blastomere/bacterium) or where "
        "individual identity is preserved by separate preparation/labeling before identity-preserving "
        "multiplexing. A population of many sorted cells, a cell-line culture, pooled cells before "
        "measurement, tissue/bulk material, or diluted-bulk/single-cell-equivalent benchmark is not "
        "true SCP. Do not confuse 'single cell type' with one biological cell. A phrase such as "
        "'10^6 root hair cells ... proteins from each sample' describes a population sample, not an "
        "individual cell. Conversely, 'five eggs were used; each egg was homogenized' can describe "
        "five separate individual-cell biological replicates. Decide the factual axes first, then the "
        "overall decision. If evidence is genuinely insufficient, use unclear/uncertain."
    )


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def evidence_text(row: dict[str, Any], max_chars: int) -> str:
    pieces = [
        f"PRIDE accession: {text(row.get('accession'))}",
        f"Dataset title: {text(row.get('dataset_title'))}",
        f"Evidence mode: {text(row.get('semantic_evidence_mode'))}",
        f"Discovery priority: {text(row.get('semantic_priority'))}",
        f"Evidence packet flags: {compact_json(row.get('qc_evidence_flags') or [])}",
        "",
        "PRIMARY SOURCE EVIDENCE:",
    ]
    primary = row.get("qc_primary_evidence") or []
    if not primary:
        pieces.append("[no primary source passage was recovered]")
    for i, block in enumerate(primary, start=1):
        if not isinstance(block, dict):
            continue
        meta = "; ".join(
            x for x in [
                f"source={text(block.get('source'))}",
                f"task={text(block.get('task'))}" if text(block.get('task')) else "",
                f"page={text(block.get('page'))}" if text(block.get('page')) else "",
                f"flags={compact_json(block.get('flags') or [])}",
            ] if x
        )
        pieces.append(f"[E{i} | {meta}]\n{text(block.get('text'))}")

    raw_samples = row.get("qc_stage04_raw_samples") or []
    if raw_samples:
        pieces.append("\nPRIOR SAMPLES-TASK OUTPUT (claim to verify against passages, not authority):")
        for item in raw_samples[:3]:
            pieces.append(compact_json(item))

    pieces.extend(
        [
            "\nSECONDARY CONTEXT (not authority):",
            f"Unified route: {text(row.get('unified_route'))}",
            f"Review flags: {compact_json(row.get('review_flags') or [])}",
            f"Stage04 final/model: {text(row.get('stage04_classification'))}/{text(row.get('stage04_model_classification'))}",
            f"Repository triage class: {text(row.get('repository_triage_class'))}",
            f"Repository structured cell/MS: {text(row.get('repository_individual_cell_evidence'))}/{text(row.get('repository_ms_proteomics_evidence'))}",
        ]
    )
    return "\n".join(pieces)[:max_chars]


def critic_prompt(row: dict[str, Any], max_chars: int) -> str:
    return (
        "Adjudicate whether THIS PRIDE accession itself contains true individual-biological-cell "
        "MS proteomics. First classify sample_unit and whether MS/proteomics is performed on that "
        "same unit. Explicit population/pooling evidence overrides superficial use of the phrase "
        "'single-cell proteomics'. Copy short exact source phrases into the evidence quote fields.\n\n"
        + evidence_text(row, max_chars)
    )


def jury_prompt(row: dict[str, Any], critic: dict[str, Any], max_chars: int) -> str:
    return (
        "Act as an independent second reviewer. Re-evaluate the factual axes from the PRIMARY SOURCE "
        "EVIDENCE. The critic result is shown only to expose the disputed interpretation. Do not defer "
        "to it. Explicit many-cell/population/pooling evidence means exclusion unless the text clearly "
        "states that cells were individually prepared/labeled and identity was preserved.\n\n"
        f"CRITIC RESULT: {compact_json(critic)}\n\n"
        + evidence_text(row, max_chars)
    )


def keep_alive_value(value: str) -> Any:
    v = text(value)
    return 0 if v in {"0", "0s", "0m", "0h"} else (v or "5m")


def transient(code: int) -> bool:
    return code in {429, 500, 502, 503, 504}


def post_generate(*, url: str, model: str, prompt: str, args: argparse.Namespace, keep_alive: str) -> tuple[dict[str, Any], dict[str, Any]]:
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
    last_exc: Exception | None = None
    for attempt in range(args.retries + 1):
        try:
            response = requests.post(url, json=payload, timeout=args.timeout)
            if response.status_code >= 400:
                body = response.text[:800]
                if response.status_code == 404:
                    raise RuntimeError(f"Ollama model {model!r} unavailable. Try: ollama pull {model}. {body}")
                if transient(response.status_code) and attempt < args.retries:
                    time.sleep(args.retry_backoff * (2**attempt))
                    continue
                response.raise_for_status()
            data = response.json()
            parsed = json.loads(text(data.get("response")) or "{}")
            validate_model_payload(parsed)
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
            last_exc = exc
            if attempt < args.retries:
                time.sleep(args.retry_backoff * (2**attempt))
                continue
            raise OllamaUnavailableError(f"Ollama unavailable for {model}: {type(exc).__name__}: {exc}") from exc
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            last_exc = exc
            if attempt < args.retries:
                time.sleep(args.retry_backoff * (2**attempt))
                continue
            raise RuntimeError(f"Invalid structured response from {model}: {exc}") from exc
    raise RuntimeError(f"Ollama request failed for {model}: {last_exc}")


def unload_model(args: argparse.Namespace, model: str) -> None:
    payload = {"model": model, "prompt": "", "stream": False, "keep_alive": 0}
    try:
        requests.post(args.ollama_url, json=payload, timeout=min(args.timeout, 60))
    except Exception:
        pass


def validate_model_payload(payload: dict[str, Any]) -> None:
    if text(payload.get("decision")).lower() not in VALID_DECISIONS:
        raise ValueError("missing/invalid decision")
    required = [
        "sample_unit",
        "same_unit_ms_proteomics",
        "target_dataset_scope",
        "premeasurement_pooling",
        "benchmark_only",
        "reason",
    ]
    missing = [k for k in required if not text(payload.get(k))]
    if missing:
        raise ValueError(f"missing required fields: {missing}")


def normalized_decision(payload: dict[str, Any]) -> tuple[str, str]:
    """Derive verdict from factual axes, using top-level decision only when coherent."""
    sample = text(payload.get("sample_unit")).lower()
    ms = text(payload.get("same_unit_ms_proteomics")).lower()
    scope = text(payload.get("target_dataset_scope")).lower()
    pooling = text(payload.get("premeasurement_pooling")).lower()
    benchmark = text(payload.get("benchmark_only")).lower()
    raw = text(payload.get("decision")).lower()

    explicit_exclude = (
        sample in {"population_or_pool", "bulk_or_tissue", "benchmark_or_equivalent"}
        or scope == "contradicts_target"
        or pooling == "present"
        or benchmark == "yes"
    )
    if explicit_exclude:
        return "exclude", "structured_exclusion_axis"

    explicit_include = (
        sample == "individual_cell"
        and ms == "yes"
        and scope in {"supports_target", "not_target_specific", "unclear"}
        and pooling == "absent"
        and benchmark == "no"
    )
    if explicit_include:
        return "include", "structured_individual_cell_plus_ms"

    # Preserve a coherent explicit model decision only if no factual axis
    # contradicts it.  Otherwise uncertainty is safer.
    if raw == "include" and sample == "individual_cell" and ms == "yes":
        return "include", "model_decision_consistent_with_axes"
    if raw == "exclude" and sample in {"population_or_pool", "bulk_or_tissue", "benchmark_or_equivalent"}:
        return "exclude", "model_decision_consistent_with_axes"
    return "uncertain", "insufficient_or_mixed_axes"


def evidence_strength(row: dict[str, Any]) -> int:
    score = 0
    primary = row.get("qc_primary_evidence") or []
    score += min(len(primary), 6)
    flags = set(row.get("qc_evidence_flags") or [])
    if "individual_cell_language" in flags:
        score += 2
    if "ms_proteomics_language" in flags:
        score += 2
    if flags & {"explicit_pooling_language", "multi_cell_count_language", "population_or_bulk_language", "benchmark_language"}:
        score += 3
    if row.get("qc_stage04_raw_samples"):
        score += 1
    return score


def jury_required(row: dict[str, Any], critic: dict[str, Any]) -> tuple[bool, str]:
    c, _ = normalized_decision(critic)
    route = text(row.get("unified_route"))
    flags = set(row.get("review_flags") or [])
    evidence_flags = set(row.get("qc_evidence_flags") or [])

    if c == "uncertain" and route in {"include_candidate", "review_high"} and evidence_strength(row) >= 4:
        return True, "high_recall_uncertain_with_direct_evidence"
    if c == "uncertain" and evidence_flags & {
        "explicit_pooling_language", "multi_cell_count_language", "population_or_bulk_language", "benchmark_language"
    }:
        return True, "uncertain_with_population_or_benchmark_risk"
    if route in {"include_candidate", "review_high"} and c == "exclude":
        return True, "high_recall_route_vs_exclude"
    if route == "review_low" and c == "include":
        return True, "weak_route_vs_include"
    if c == "include" and (
        "repository_qwen_overcall" in flags
        or "contradictory_cell_evidence" in flags
        or "negative_context_present" in flags
        or "adjacent_discovery_label" in flags
        or evidence_flags & {"explicit_pooling_language", "multi_cell_count_language", "population_or_bulk_language", "benchmark_language"}
    ):
        return True, "include_despite_risk_flag"
    return False, ""


def combine_decisions(critic: dict[str, Any], jury: dict[str, Any] | None) -> tuple[str, str, str, str]:
    c, c_basis = normalized_decision(critic)
    if jury is None:
        return c, "critic_only", c_basis, ""
    j, j_basis = normalized_decision(jury)
    if c == "uncertain" and j in {"include", "exclude"}:
        return j, "jury_resolved_critic_uncertainty", c_basis, j_basis
    if c == j and c in {"include", "exclude"}:
        return c, "critic_jury_agree", c_basis, j_basis
    return "uncertain", "critic_jury_disagree_or_unresolved", c_basis, j_basis


def packet_hash(row: dict[str, Any]) -> str:
    return text(row.get("qc_packet_hash")) or hashlib.sha256(
        json.dumps(row, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def cache_payload_valid(payload: dict[str, Any], *, row: dict[str, Any], model: str) -> bool:
    if payload.get("qc_version") != QC_VERSION:
        return False
    if text(payload.get("packet_hash")) != packet_hash(row):
        return False
    if text(payload.get("model")) != model:
        return False
    try:
        validate_model_payload(payload)
    except Exception:
        return False
    return True


def read_valid_cache(path: Path, *, row: dict[str, Any], model: str, force: bool) -> tuple[dict[str, Any] | None, bool]:
    if force or not path.is_file():
        return None, False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, True
    if isinstance(payload, dict) and cache_payload_valid(payload, row=row, model=model):
        return payload, False
    return None, True


def write_tsv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "accession", "unified_route", "semantic_evidence_mode", "semantic_priority",
        "critic_raw_decision", "critic_decision", "critic_decision_basis",
        "critic_sample_unit", "critic_same_unit_ms_proteomics", "critic_target_dataset_scope",
        "critic_premeasurement_pooling", "critic_benchmark_only", "critic_evidence_quote_cell",
        "critic_evidence_quote_ms", "critic_reason", "jury_trigger", "jury_raw_decision",
        "jury_decision", "jury_decision_basis", "jury_sample_unit", "jury_same_unit_ms_proteomics",
        "jury_target_dataset_scope", "jury_premeasurement_pooling", "jury_benchmark_only",
        "jury_evidence_quote_cell", "jury_evidence_quote_ms", "jury_reason", "final_decision",
        "final_basis", "evidence_strength", "qc_evidence_flags", "critic_model", "jury_model",
        "critic_wall_seconds", "jury_wall_seconds", "error",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            out = dict(row)
            if isinstance(out.get("qc_evidence_flags"), list):
                out["qc_evidence_flags"] = "; ".join(out["qc_evidence_flags"])
            writer.writerow({k: out.get(k, "") for k in fields})


def write_review_decisions(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = ["accession", "final_decision", "review_note"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            note = f"v0.1.9 evidence-grounded QC ({row.get('final_basis','')}): {row.get('critic_reason','')}"
            if row.get("jury_reason"):
                note += f" | jury: {row.get('jury_reason','')}"
            writer.writerow({"accession": row["accession"], "final_decision": row["final_decision"], "review_note": note[:1800]})


def main() -> None:
    args = parse_args()
    input_path = Path(args.input_jsonl)
    output_dir = Path(args.output_dir)
    critic_dir = output_dir / "critic_status"
    jury_dir = output_dir / "jury_status"
    critic_dir.mkdir(parents=True, exist_ok=True)
    jury_dir.mkdir(parents=True, exist_ok=True)

    selected = {x.upper() for x in args.accession}
    rows = [r for r in read_jsonl(input_path) if not selected or text(r.get("accession")).upper() in selected]
    if args.limit > 0:
        rows = rows[: args.limit]

    critic_results: dict[str, dict[str, Any]] = {}
    jury_results: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    invalidated_critic = 0
    invalidated_jury = 0

    print(f"Evidence-grounded semantic QC candidates: {len(rows)}")
    print(f"QC version: {QC_VERSION}")
    print(f"Critic model: {args.critic_model}")

    for i, row in enumerate(rows, start=1):
        accession = text(row.get("accession"))
        path = critic_dir / f"{accession}.json"
        cached, invalid = read_valid_cache(path, row=row, model=args.critic_model, force=args.force)
        if invalid:
            invalidated_critic += 1
        if cached is not None:
            critic_results[accession] = cached
            norm, _ = normalized_decision(cached)
            print(f"[critic {i}/{len(rows)}] {accession} -> cached/{norm}")
            continue
        try:
            parsed, stats = post_generate(
                url=args.ollama_url, model=args.critic_model, prompt=critic_prompt(row, args.evidence_chars),
                args=args, keep_alive=args.critic_keep_alive,
            )
            payload = {
                "qc_version": QC_VERSION, "packet_hash": packet_hash(row), "accession": accession,
                "model": args.critic_model, **parsed, "stats": stats,
            }
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            critic_results[accession] = payload
            norm, basis = normalized_decision(payload)
            print(f"[critic {i}/{len(rows)}] {accession} -> {norm} ({basis})")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            errors[accession] = error
            path.write_text(json.dumps({"qc_version": QC_VERSION, "packet_hash": packet_hash(row), "accession": accession, "error": error}, indent=2), encoding="utf-8")
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

    print(f"Selective jury candidates: {len(jury_targets)}")
    if jury_targets:
        print(f"Jury model: {args.jury_model}")

    for i, (row, trigger) in enumerate(jury_targets, start=1):
        accession = text(row.get("accession"))
        path = jury_dir / f"{accession}.json"
        cached, invalid = read_valid_cache(path, row=row, model=args.jury_model, force=args.force)
        if invalid:
            invalidated_jury += 1
        if cached is not None:
            cached = dict(cached)
            cached["trigger"] = trigger
            jury_results[accession] = cached
            norm, _ = normalized_decision(cached)
            print(f"[jury {i}/{len(jury_targets)}] {accession} -> cached/{norm}")
            continue
        try:
            parsed, stats = post_generate(
                url=args.ollama_url, model=args.jury_model,
                prompt=jury_prompt(row, critic_results[accession], args.evidence_chars),
                args=args, keep_alive=args.jury_keep_alive,
            )
            payload = {
                "qc_version": QC_VERSION, "packet_hash": packet_hash(row), "accession": accession,
                "model": args.jury_model, "trigger": trigger, **parsed, "stats": stats,
            }
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            jury_results[accession] = payload
            norm, basis = normalized_decision(payload)
            print(f"[jury {i}/{len(jury_targets)}] {accession} -> {norm} ({basis})")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            errors[accession] = (errors.get(accession, "") + (" | " if errors.get(accession) else "") + error)
            path.write_text(json.dumps({"qc_version": QC_VERSION, "packet_hash": packet_hash(row), "accession": accession, "trigger": trigger, "error": error}, indent=2), encoding="utf-8")
            print(f"[jury {i}/{len(jury_targets)}] {accession} -> ERROR {error}")

    if jury_targets:
        unload_model(args, args.jury_model)

    final_rows = []
    for row in rows:
        accession = text(row.get("accession"))
        critic = critic_results.get(accession, {})
        jury = jury_results.get(accession)
        if not critic or critic.get("error"):
            final, final_basis, c_basis, j_basis = "uncertain", "critic_error", "", ""
        elif jury is not None and jury.get("error"):
            final, final_basis, c_basis, j_basis = "uncertain", "jury_error", "", ""
        else:
            final, final_basis, c_basis, j_basis = combine_decisions(critic, jury)
        c_norm, _ = normalized_decision(critic) if critic and not critic.get("error") else ("uncertain", "")
        j_norm, _ = normalized_decision(jury) if jury and not jury.get("error") else ("", "")
        final_rows.append({
            "accession": accession,
            "unified_route": row.get("unified_route", ""),
            "semantic_evidence_mode": row.get("semantic_evidence_mode", ""),
            "semantic_priority": row.get("semantic_priority", ""),
            "critic_raw_decision": critic.get("decision", ""),
            "critic_decision": c_norm,
            "critic_decision_basis": c_basis,
            "critic_sample_unit": critic.get("sample_unit", ""),
            "critic_same_unit_ms_proteomics": critic.get("same_unit_ms_proteomics", ""),
            "critic_target_dataset_scope": critic.get("target_dataset_scope", ""),
            "critic_premeasurement_pooling": critic.get("premeasurement_pooling", ""),
            "critic_benchmark_only": critic.get("benchmark_only", ""),
            "critic_evidence_quote_cell": critic.get("evidence_quote_cell", ""),
            "critic_evidence_quote_ms": critic.get("evidence_quote_ms", ""),
            "critic_reason": critic.get("reason", ""),
            "jury_trigger": jury.get("trigger", "") if jury else "",
            "jury_raw_decision": jury.get("decision", "") if jury else "",
            "jury_decision": j_norm,
            "jury_decision_basis": j_basis,
            "jury_sample_unit": jury.get("sample_unit", "") if jury else "",
            "jury_same_unit_ms_proteomics": jury.get("same_unit_ms_proteomics", "") if jury else "",
            "jury_target_dataset_scope": jury.get("target_dataset_scope", "") if jury else "",
            "jury_premeasurement_pooling": jury.get("premeasurement_pooling", "") if jury else "",
            "jury_benchmark_only": jury.get("benchmark_only", "") if jury else "",
            "jury_evidence_quote_cell": jury.get("evidence_quote_cell", "") if jury else "",
            "jury_evidence_quote_ms": jury.get("evidence_quote_ms", "") if jury else "",
            "jury_reason": jury.get("reason", "") if jury else "",
            "final_decision": final,
            "final_basis": final_basis,
            "evidence_strength": evidence_strength(row),
            "qc_evidence_flags": row.get("qc_evidence_flags") or [],
            "critic_model": args.critic_model,
            "jury_model": args.jury_model if jury else "",
            "critic_wall_seconds": (critic.get("stats") or {}).get("wall_seconds", ""),
            "jury_wall_seconds": ((jury or {}).get("stats") or {}).get("wall_seconds", ""),
            "error": errors.get(accession, ""),
        })

    final_rows.sort(key=lambda r: r["accession"])
    write_tsv(output_dir / "semantic_qc_results.tsv", final_rows)
    write_review_decisions(output_dir / "review_decisions.tsv", final_rows)
    uncertain_rows = [r for r in final_rows if r["final_decision"] == "uncertain"]
    write_tsv(output_dir / "uncertain_for_manual_review.tsv", uncertain_rows)

    summary = {
        "qc_version": QC_VERSION,
        "candidates": len(final_rows),
        "critic_model": args.critic_model,
        "jury_model": "" if args.no_jury else args.jury_model,
        "invalidated_old_critic_cache_records": invalidated_critic,
        "invalidated_old_jury_cache_records": invalidated_jury,
        "critic_raw_decision_counts": dict(Counter(r["critic_raw_decision"] for r in final_rows if r["critic_raw_decision"])),
        "critic_normalized_decision_counts": dict(Counter(r["critic_decision"] for r in final_rows if r["critic_decision"])),
        "jury_calls": len(jury_targets),
        "jury_normalized_decision_counts": dict(Counter(r["jury_decision"] for r in final_rows if r["jury_decision"])),
        "final_decision_counts": dict(Counter(r["final_decision"] for r in final_rows)),
        "uncertain_for_manual_review": len(uncertain_rows),
        "errors": sum(bool(r["error"]) for r in final_rows),
        "note": (
            "v0.1.9 rehydrates direct source evidence and derives verdicts from factual axes. "
            "v0.1.8 uncertain/blank caches are invalidated automatically. Residual uncertain "
            "rows still block final Stage-05 bridge generation."
        ),
    }
    (output_dir / "semantic_qc_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
