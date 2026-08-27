#!/usr/bin/env python3
"""Evidence-grounded independent semantic QC for recall-first PRIDE SCP.

v0.1.11 keeps the direct-source-evidence architecture and makes the measured
MS sample unit explicit.  The reviewer must distinguish one biological cell per
target MS sample from many cells contributing to one population sample, even
when FACS is used.  It also distinguishes genuine one-cell target samples from
separate multi-cell libraries/benchmarks.  Deterministic normalization gives
true target-sample composition and destructive pooling precedence over optimistic
model labels, while a complete one-cell-to-MS chain takes precedence over an
internally inconsistent benchmark-only flag.

Critic/jury arbitration is evidence-aware: direct many-cell target-sample facts
can force exclusion, while a complete single-cell target-MS chain can resolve a
weaker contradictory label.  Structured-output retry/repair remains enabled.
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
QC_VERSION = "v0.1.11-ms-sample-unit-qc-1"
VALID_DECISIONS = {"include", "exclude", "uncertain"}

SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["include", "exclude", "uncertain"]},
        "individual_cell_samples_present": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "target_single_cell_ms_samples_present": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "cells_per_target_ms_sample": {
            "type": "string",
            "enum": ["one", "multiple", "mixed_design", "unclear"],
        },
        "individual_identity_preserved_to_ms": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "destructive_pooling_before_identity": {
            "type": "string",
            "enum": ["present", "absent", "unclear"],
        },
        "population_samples_only": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "benchmark_only": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "separate_multi_cell_controls_present": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "evidence_quote_cell": {"type": "string", "maxLength": 280},
        "evidence_quote_sample_unit": {"type": "string", "maxLength": 360},
        "evidence_quote_chain": {"type": "string", "maxLength": 360},
        "evidence_quote_ms": {"type": "string", "maxLength": 280},
        "reason": {"type": "string", "maxLength": 520},
    },
    "required": [
        "decision",
        "individual_cell_samples_present",
        "target_single_cell_ms_samples_present",
        "cells_per_target_ms_sample",
        "individual_identity_preserved_to_ms",
        "destructive_pooling_before_identity",
        "population_samples_only",
        "benchmark_only",
        "separate_multi_cell_controls_present",
        "evidence_quote_cell",
        "evidence_quote_sample_unit",
        "evidence_quote_chain",
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
    p.add_argument(
        "--num-predict",
        type=int,
        default=0,
        help="Legacy override: when >0, use this output-token budget for critic and jury.",
    )
    p.add_argument("--critic-num-predict", type=int, default=520)
    p.add_argument("--jury-num-predict", type=int, default=800)
    p.add_argument(
        "--structured-retries",
        type=int,
        default=1,
        help="Corrective retries after a syntactically/structurally invalid model response.",
    )
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
        "Previous Qwen labels, discovery routes, titles, and prior decisions are claims to verify, "
        "not authority. A true SCP dataset contains at least one TARGET MS sample whose proteomic "
        "material comes from ONE individual biological cell (including one egg/oocyte/blastomere/"
        "bacterium), or from one cell whose identity is preserved by a unique label through "
        "identity-preserving multiplexing. The key question is the composition of each TARGET MS "
        "sample, not whether individual cells appeared anywhere in the workflow. "
        "If 10^6 cells, 106 cells (a superscript may be lost in extracted text), 100 cells, or another "
        "stated many-cell count are sorted into each replicate/sample and proteins are extracted from "
        "that sample, cells_per_target_ms_sample=multiple and this is population proteomics. FACS does "
        "not imply single-cell MS by itself. By contrast, one-cell-per-well sorting followed by digestion "
        "and LC-MS/MS of those well-derived samples is true SCP. Multiple individual cells measured as "
        "separate biological replicates are also true SCP. Separate 20/40-cell libraries, carrier/reference "
        "channels, blanks, diluted-bulk benchmarks, or method controls do NOT make the accession benchmark-only "
        "if genuine one-cell target MS samples are also present; mark mixed_design / separate controls instead. "
        "Physical combination AFTER unique identity-preserving labeling is compatible with SCP. Destructive "
        "combination of unlabeled cells before identity is established is not. Decide the factual axes first. "
        "Use unclear only when the source evidence truly cannot establish the axis."
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
        "Adjudicate whether THIS PRIDE accession itself contains true individual-biological-cell MS "
        "proteomics. First determine the TARGET MS sample unit. Do not equate FACS-isolated cells with "
        "single-cell MS unless one cell contributes to each target proteomic sample (or identity-preserving "
        "labels preserve individual cells after multiplexing). Explicitly distinguish: "
        "(A) one cell per target MS sample; (B) many cells contributing to each replicate/sample; and "
        "(C) mixed designs where genuine one-cell target samples coexist with separate multi-cell libraries, "
        "carriers, or benchmarks. A phrase such as '10^6 root hair cells' or extracted '106 root hair cells' "
        "isolated from each replicate and followed by 'proteins from each sample' means MULTIPLE cells per "
        "target MS sample, not 106 independently measured single cells. Conversely, 'individual cells were "
        "sorted into individual wells; digested cells were analyzed by LC-MS/MS' establishes one-cell target "
        "samples even if 20/40-cell libraries or 250-pg benchmarks also exist. If genuine one-cell target MS "
        "samples exist, benchmark_only must be no. Copy short exact source phrases into the evidence fields.\n\n"
        + evidence_text(row, max_chars)
    )



def jury_prompt(row: dict[str, Any], critic: dict[str, Any], max_chars: int) -> str:
    return (
        "Act as an independent second reviewer. Re-evaluate the PRIMARY SOURCE EVIDENCE with special "
        "attention to the TARGET MS sample composition. The critic result is shown only to expose the "
        "disputed interpretation; do not defer to it. FACS can sort either one cell per well or a many-cell "
        "population. A stated count such as 10^6/106 cells feeding each replicate/sample means multiple "
        "cells per target MS sample unless the source explicitly says those cells were kept as separate "
        "single-cell samples. Separate 20/40-cell libraries, carriers, blanks, two-proteome mixes, or low-input "
        "benchmarks do not negate genuine one-cell samples. If one-cell target MS samples are directly present, "
        "benchmark_only cannot be yes. If many unlabeled cells contribute to each target proteomic sample, "
        "that population-sample fact overrides generic single-cell wording. Return only the requested structured "
        "object with short quotes and reason.\n\n"
        f"CRITIC RESULT: {compact_json(critic)}\n\n"
        + evidence_text(row, max_chars)
    )



def keep_alive_value(value: str) -> Any:
    v = text(value)
    return 0 if v in {"0", "0s", "0m", "0h"} else (v or "5m")


def transient(code: int) -> bool:
    return code in {429, 500, 502, 503, 504}


def parse_structured_response(raw: str) -> dict[str, Any]:
    """Parse a structured Ollama response, tolerating wrappers/code fences."""
    value = text(raw)
    if not value:
        raise ValueError("empty model response")
    candidates = [value]
    if value.startswith("```"):
        stripped = value.strip("`").strip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].lstrip()
        candidates.append(stripped)
    start, end = value.find("{"), value.rfind("}")
    if 0 <= start < end:
        candidates.append(value[start : end + 1])
    last: Exception | None = None
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
            if not isinstance(obj, dict):
                raise ValueError("structured response was not a JSON object")
            validate_model_payload(obj)
            return obj
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            last = exc
    raise ValueError(str(last or "invalid structured response"))


def corrective_prompt(prompt: str) -> str:
    return (
        prompt
        + "\n\nCORRECTIVE OUTPUT INSTRUCTION: Your previous response could not be parsed or did not "
        "match the required schema. Re-evaluate the same evidence and return ONLY one complete valid "
        "JSON object matching the requested schema. No markdown and no prose outside JSON. Keep each "
        "evidence quote under 180 characters and the reason under 300 characters so the JSON completes."
    )


def post_generate(
    *,
    url: str,
    model: str,
    prompt: str,
    args: argparse.Namespace,
    keep_alive: str,
    num_predict: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.perf_counter()
    total_http_attempts = 0
    last_exc: Exception | None = None
    last_raw = ""

    for structured_attempt in range(args.structured_retries + 1):
        this_prompt = prompt if structured_attempt == 0 else corrective_prompt(prompt)
        # Corrective retries get additional room, particularly for Gemma jury output.
        this_num_predict = num_predict if structured_attempt == 0 else max(num_predict, 900)
        payload = {
            "model": model,
            "system": system_prompt(),
            "prompt": this_prompt,
            "stream": False,
            "format": SCHEMA,
            "keep_alive": keep_alive_value(keep_alive),
            "options": {
                "temperature": 0,
                "seed": 42,
                "num_ctx": args.num_ctx,
                "num_predict": this_num_predict,
                "num_thread": args.cpu_threads,
            },
        }

        for attempt in range(args.retries + 1):
            total_http_attempts += 1
            try:
                response = requests.post(url, json=payload, timeout=args.timeout)
                if response.status_code >= 400:
                    body = response.text[:800]
                    if response.status_code == 404:
                        raise RuntimeError(
                            f"Ollama model {model!r} unavailable. Try: ollama pull {model}. {body}"
                        )
                    if transient(response.status_code) and attempt < args.retries:
                        time.sleep(args.retry_backoff * (2**attempt))
                        continue
                    response.raise_for_status()
                data = response.json()
                last_raw = text(data.get("response"))
                try:
                    parsed = parse_structured_response(last_raw)
                except (ValueError, KeyError) as exc:
                    last_exc = exc
                    break  # corrective structured retry, not another identical HTTP retry
                stats = {
                    "wall_seconds": round(time.perf_counter() - started, 3),
                    "request_attempts": total_http_attempts,
                    "structured_attempts": structured_attempt + 1,
                    "prompt_tokens": data.get("prompt_eval_count", 0),
                    "output_tokens": data.get("eval_count", 0),
                    "prompt_seconds": round(data.get("prompt_eval_duration", 0) / 1e9, 3),
                    "generation_seconds": round(data.get("eval_duration", 0) / 1e9, 3),
                    "num_predict": this_num_predict,
                }
                return parsed, stats
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt < args.retries:
                    time.sleep(args.retry_backoff * (2**attempt))
                    continue
                raise OllamaUnavailableError(
                    f"Ollama unavailable for {model}: {type(exc).__name__}: {exc}"
                ) from exc
            except (json.JSONDecodeError, ValueError, KeyError) as exc:
                last_exc = exc
                break

    snippet = last_raw[:500].replace("\n", " ")
    raise RuntimeError(
        f"Invalid structured response from {model} after "
        f"{args.structured_retries + 1} structured attempt(s): {last_exc}; raw={snippet!r}"
    )


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
        "individual_cell_samples_present",
        "target_single_cell_ms_samples_present",
        "cells_per_target_ms_sample",
        "individual_identity_preserved_to_ms",
        "destructive_pooling_before_identity",
        "population_samples_only",
        "benchmark_only",
        "separate_multi_cell_controls_present",
        "reason",
    ]
    missing = [k for k in required if not text(payload.get(k))]
    if missing:
        raise ValueError(f"missing required fields: {missing}")


def sample_unit_hard_exclusion(payload: dict[str, Any]) -> tuple[bool, str]:
    composition = text(payload.get("cells_per_target_ms_sample")).lower()
    destructive_pooling = text(payload.get("destructive_pooling_before_identity")).lower()
    population_only = text(payload.get("population_samples_only")).lower()
    if composition == "multiple":
        return True, "target_ms_sample_contains_multiple_cells"
    if destructive_pooling == "present":
        return True, "destructive_pooling_before_identity"
    if population_only == "yes":
        return True, "population_samples_only"
    return False, ""


def complete_single_cell_ms_chain(payload: dict[str, Any]) -> tuple[bool, str]:
    sc = text(payload.get("individual_cell_samples_present")).lower()
    target_sc = text(payload.get("target_single_cell_ms_samples_present")).lower()
    composition = text(payload.get("cells_per_target_ms_sample")).lower()
    identity = text(payload.get("individual_identity_preserved_to_ms")).lower()
    destructive_pooling = text(payload.get("destructive_pooling_before_identity")).lower()
    population_only = text(payload.get("population_samples_only")).lower()

    if (
        sc == "yes"
        and target_sc == "yes"
        and composition in {"one", "mixed_design"}
        and identity == "yes"
        and destructive_pooling == "absent"
        and population_only == "no"
    ):
        return True, "complete_target_single_cell_ms_chain"
    return False, ""


def normalized_decision(payload: dict[str, Any]) -> tuple[str, str]:
    """Derive verdict from the target MS sample unit plus identity-preserved chain."""
    hard_exclude, exclude_basis = sample_unit_hard_exclusion(payload)
    positive, positive_basis = complete_single_cell_ms_chain(payload)
    benchmark = text(payload.get("benchmark_only")).lower()
    target_sc = text(payload.get("target_single_cell_ms_samples_present")).lower()
    sc = text(payload.get("individual_cell_samples_present")).lower()
    raw = text(payload.get("decision")).lower()

    # Direct target-sample composition has highest precedence.  This prevents
    # '106 cells isolated by FACS' from being converted into an optimistic
    # single-cell chain merely because individual cells were involved upstream.
    if hard_exclude:
        return "exclude", f"structured_exclusion_axis:{exclude_basis}"

    # Absence of individual-cell material is an exclusion, but it is considered
    # softer than a direct many-cell target-sample statement during critic/jury
    # arbitration because the second reviewer may recover an explicit one-cell chain.
    if sc == "no" and target_sc != "yes":
        return "exclude", "structured_exclusion_axis:no_individual_cell_samples"

    # A complete direct one-cell-to-MS chain is stronger than an internally
    # inconsistent benchmark_only=yes flag.  Genuine target single-cell samples
    # plus separate low-input/multi-cell controls make a mixed study, not a
    # benchmark-only study.
    if positive:
        if benchmark == "yes":
            return "include", "structured_single_cell_chain_overrides_inconsistent_benchmark_only"
        return "include", "structured_target_single_cell_ms_chain"

    # benchmark_only is a hard exclusion only when no direct target single-cell
    # MS samples were established.
    if benchmark == "yes" and target_sc != "yes":
        return "exclude", "structured_exclusion_axis:benchmark_only_without_single_cell_targets"

    if raw == "include" and positive:
        return "include", positive_basis
    if raw == "exclude" and (hard_exclude or (benchmark == "yes" and target_sc != "yes")):
        return "exclude", "model_decision_consistent_with_exclusion_axes"
    return "uncertain", "insufficient_or_mixed_target_sample_axes"



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

    if c == j and c in {"include", "exclude"}:
        return c, "critic_jury_agree", c_basis, j_basis
    if c == "uncertain" and j in {"include", "exclude"}:
        return j, "jury_resolved_critic_uncertainty", c_basis, j_basis
    if j == "uncertain" and c in {"include", "exclude"}:
        return c, "critic_resolved_jury_uncertainty", c_basis, j_basis

    # Evidence-aware disagreement resolution.  Direct many-cell target-sample
    # composition / destructive pooling is stronger than a generic optimistic
    # chain from the other reviewer.
    c_hard, c_hard_basis = sample_unit_hard_exclusion(critic)
    j_hard, j_hard_basis = sample_unit_hard_exclusion(jury)
    c_pos, _ = complete_single_cell_ms_chain(critic)
    j_pos, _ = complete_single_cell_ms_chain(jury)

    if c_hard and not j_hard:
        return "exclude", "critic_hard_sample_unit_exclusion", c_basis, j_basis
    if j_hard and not c_hard:
        return "exclude", "jury_hard_sample_unit_exclusion", c_basis, j_basis

    # If one reviewer has a complete target one-cell-to-MS chain and the other
    # reviewer only excluded because of benchmark-only/mixed-control confusion,
    # accept the complete chain.  Hard sample-unit exclusions above still win.
    if c_pos and j == "exclude" and not j_hard:
        return "include", "critic_complete_chain_overrides_soft_jury_exclusion", c_basis, j_basis
    if j_pos and c == "exclude" and not c_hard:
        return "include", "jury_complete_chain_overrides_soft_critic_exclusion", c_basis, j_basis

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
        "critic_individual_cell_samples_present", "critic_target_single_cell_ms_samples_present",
        "critic_cells_per_target_ms_sample", "critic_individual_identity_preserved_to_ms",
        "critic_destructive_pooling_before_identity", "critic_population_samples_only",
        "critic_benchmark_only", "critic_separate_multi_cell_controls_present",
        "critic_evidence_quote_cell", "critic_evidence_quote_sample_unit",
        "critic_evidence_quote_chain", "critic_evidence_quote_ms", "critic_reason",
        "jury_trigger", "jury_raw_decision", "jury_decision", "jury_decision_basis",
        "jury_individual_cell_samples_present", "jury_target_single_cell_ms_samples_present",
        "jury_cells_per_target_ms_sample", "jury_individual_identity_preserved_to_ms",
        "jury_destructive_pooling_before_identity", "jury_population_samples_only",
        "jury_benchmark_only", "jury_separate_multi_cell_controls_present",
        "jury_evidence_quote_cell", "jury_evidence_quote_sample_unit",
        "jury_evidence_quote_chain", "jury_evidence_quote_ms", "jury_reason",
        "final_decision", "final_basis", "evidence_strength", "qc_evidence_flags",
        "critic_model", "jury_model", "critic_wall_seconds", "jury_wall_seconds", "error",
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
            note = f"v0.1.11 MS-sample-unit QC ({row.get('final_basis','')}): {row.get('critic_reason','')}"
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
                num_predict=(args.num_predict if args.num_predict > 0 else args.critic_num_predict),
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
                num_predict=(args.num_predict if args.num_predict > 0 else args.jury_num_predict),
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
            "critic_individual_cell_samples_present": critic.get("individual_cell_samples_present", ""),
            "critic_target_single_cell_ms_samples_present": critic.get("target_single_cell_ms_samples_present", ""),
            "critic_cells_per_target_ms_sample": critic.get("cells_per_target_ms_sample", ""),
            "critic_individual_identity_preserved_to_ms": critic.get("individual_identity_preserved_to_ms", ""),
            
            "critic_destructive_pooling_before_identity": critic.get("destructive_pooling_before_identity", ""),
            "critic_population_samples_only": critic.get("population_samples_only", ""),
            "critic_benchmark_only": critic.get("benchmark_only", ""),
            "critic_separate_multi_cell_controls_present": critic.get("separate_multi_cell_controls_present", ""),
            "critic_evidence_quote_cell": critic.get("evidence_quote_cell", ""),
            "critic_evidence_quote_sample_unit": critic.get("evidence_quote_sample_unit", ""),
            "critic_evidence_quote_chain": critic.get("evidence_quote_chain", ""),
            "critic_evidence_quote_ms": critic.get("evidence_quote_ms", ""),
            "critic_reason": critic.get("reason", ""),
            "jury_trigger": jury.get("trigger", "") if jury else "",
            "jury_raw_decision": jury.get("decision", "") if jury else "",
            "jury_decision": j_norm,
            "jury_decision_basis": j_basis,
            "jury_individual_cell_samples_present": jury.get("individual_cell_samples_present", "") if jury else "",
            "jury_target_single_cell_ms_samples_present": jury.get("target_single_cell_ms_samples_present", "") if jury else "",
            "jury_cells_per_target_ms_sample": jury.get("cells_per_target_ms_sample", "") if jury else "",
            "jury_individual_identity_preserved_to_ms": jury.get("individual_identity_preserved_to_ms", "") if jury else "",
            
            "jury_destructive_pooling_before_identity": jury.get("destructive_pooling_before_identity", "") if jury else "",
            "jury_population_samples_only": jury.get("population_samples_only", "") if jury else "",
            "jury_benchmark_only": jury.get("benchmark_only", "") if jury else "",
            "jury_separate_multi_cell_controls_present": jury.get("separate_multi_cell_controls_present", "") if jury else "",
            "jury_evidence_quote_cell": jury.get("evidence_quote_cell", "") if jury else "",
            "jury_evidence_quote_sample_unit": jury.get("evidence_quote_sample_unit", "") if jury else "",
            "jury_evidence_quote_chain": jury.get("evidence_quote_chain", "") if jury else "",
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
            "v0.1.11 makes the target MS sample unit explicit, gives many-cell/destructive-pooling "
            "facts precedence over optimistic labels, and lets a complete one-cell-to-MS chain override "
            "an inconsistent benchmark-only flag. Evidence-aware critic/jury arbitration and structured "
            "output retries remain enabled. Residual uncertain rows still block Stage-05."
        ),
    }
    (output_dir / "semantic_qc_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
