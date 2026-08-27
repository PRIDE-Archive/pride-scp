#!/usr/bin/env python3
"""Unify publication-backed and repository-only semantic results.

This is a recall-preserving *routing* layer, not a final classifier.  It keeps
all discovered candidates and assigns each one to one of five transparent
routes:

  include_candidate   high-confidence evidence candidate; still requires QC
  review_high         strong discovery evidence conflicts with semantic output
  review_medium       plausible but weaker/conflicting evidence
  review_low          weak evidence worth retaining for recall
  likely_non_scp      currently unsupported/adjacent; retained, never deleted

The purpose is to stop treating Stage-04 ``yes/no`` and repository-triage
``possible_true_scp`` as if they were calibrated equivalents.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

ROUTES = (
    "include_candidate",
    "review_high",
    "review_medium",
    "review_low",
    "likely_non_scp",
)
REVIEW_ROUTES = {"review_high", "review_medium", "review_low"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--candidate-diagnostics",
        default="data/candidate_audit/candidate_diagnostics.jsonl",
    )
    p.add_argument(
        "--publication-candidates",
        default="work/python/semantic_partition/publication_backed_candidates.jsonl",
    )
    p.add_argument(
        "--repository-candidates",
        default="work/python/semantic_partition/repository_only_candidates.jsonl",
    )
    p.add_argument(
        "--annotations-dir",
        default="work/python/pride_scp_annotations/annotations",
    )
    p.add_argument(
        "--repository-triage",
        default="work/python/repository_triage/repository_semantic_triage.tsv",
    )
    p.add_argument(
        "--known-positives",
        default="",
        help="Optional historical/known-positive CSV/TSV for route audit only.",
    )
    p.add_argument(
        "--output-dir",
        default="work/python/semantic_unification",
    )
    return p.parse_args()


def text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def truthy(value: Any) -> bool:
    return text(value).lower() in {"1", "true", "yes", "y", "positive"}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def sniff_delimiter(path: Path) -> str:
    sample = path.read_text(encoding="utf-8", errors="replace")[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="\t,")
        return dialect.delimiter
    except csv.Error:
        return "\t" if "\t" in sample.splitlines()[0] else ","


def read_table(path: Path) -> list[dict[str, str]]:
    delim = sniff_delimiter(path)
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter=delim))


def write_tsv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            delimiter="\t",
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({k: serialize_cell(row.get(k, "")) for k in fields})


def serialize_cell(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        return "; ".join(text(v) for v in value if text(v))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def discover_annotations(root: Path) -> dict[str, list[dict[str, Any]]]:
    by_accession: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in sorted(root.glob("PXD*/*.json")):
        if ".work" in path.parts:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Invalid Stage-04 annotation JSON: {path}: {exc}") from exc
        accession = text(payload.get("target_accession")) or path.parent.name
        payload = dict(payload)
        payload["_annotation_path"] = str(path)
        by_accession[accession].append(payload)
    return by_accession


def summarize_publication_annotations(
    annotations: list[dict[str, Any]],
) -> dict[str, Any]:
    if not annotations:
        return {}
    finals = [text(a.get("is_single_cell_proteomics")).lower() for a in annotations]
    gates = [
        (a.get("provenance", {}).get("scp_evidence_gate", {}) or {})
        for a in annotations
    ]
    model_classes = [text(g.get("model_classification")).lower() for g in gates]
    if "yes" in finals:
        final_class = "yes"
    elif "uncertain" in finals:
        final_class = "uncertain"
    else:
        final_class = "no"

    if "yes" in model_classes:
        model_class = "yes"
    elif "uncertain" in model_classes:
        model_class = "uncertain"
    else:
        model_class = "no"

    gate_tiers = [text(g.get("evidence_tier")) for g in gates if text(g.get("evidence_tier"))]
    gate_reasons = [text(g.get("reason")) for g in gates if text(g.get("reason"))]
    gate_evidence = [text(g.get("evidence")) for g in gates if text(g.get("evidence"))]
    gate_scopes = [text(g.get("support_scope")) for g in gates if text(g.get("support_scope"))]
    source_kinds = [text(a.get("publication_source_kind")) for a in annotations if text(a.get("publication_source_kind"))]
    titles = [text(a.get("publication_title_candidate")) for a in annotations if text(a.get("publication_title_candidate"))]
    dois = [text(a.get("publication_doi")) for a in annotations if text(a.get("publication_doi"))]
    mentioned_values = [a.get("target_accession_mentioned_in_publication") for a in annotations]
    sample_count = sum(len(a.get("true_single_cell_samples") or []) for a in annotations)
    sample_summaries: list[str] = []
    validation_warnings: list[str] = []
    for a in annotations:
        for sample in a.get("true_single_cell_samples") or []:
            if not isinstance(sample, dict):
                continue
            bits = [
                text(sample.get("sample_type")),
                text(sample.get("organism")),
                ", ".join(text(x) for x in (sample.get("cell_counts_reported") or []) if text(x)),
            ]
            summary = " | ".join(x for x in bits if x)
            if summary:
                sample_summaries.append(summary)
        validation_warnings.extend(
            text(x) for x in (a.get("validation_warnings") or []) if text(x)
        )

    return {
        "publication_annotation_count": len(annotations),
        "stage04_classification": final_class,
        "stage04_model_classification": model_class,
        "stage04_gate_supported": any(bool(g.get("supported")) for g in gates),
        "stage04_gate_tiers": gate_tiers,
        "stage04_gate_reasons": gate_reasons,
        "stage04_gate_evidence": gate_evidence,
        "stage04_support_scopes": gate_scopes,
        "stage04_publication_titles": titles,
        "stage04_publication_dois": dois,
        "stage04_source_kinds": source_kinds,
        "stage04_accession_mentioned": any(v is True for v in mentioned_values),
        "stage04_grounded_sample_count": sample_count,
        "stage04_sample_summaries": sample_summaries,
        "stage04_validation_warnings": validation_warnings,
        "stage04_annotation_paths": [a["_annotation_path"] for a in annotations],
    }


def normalize_repository_evidence(cell: str, ms: str) -> str:
    cell = cell.lower()
    ms = ms.lower()
    if cell == "direct" and ms == "direct":
        return "direct_cell_and_ms"
    if cell in {"direct", "implied"} and ms == "direct":
        return "plausible_cell_and_direct_ms"
    if cell in {"direct", "implied"} and ms == "implied":
        return "plausible_but_both_not_direct"
    if cell == "contradictory":
        return "contradictory_cell_evidence"
    if cell == "absent" and ms == "direct":
        return "ms_without_individual_cell_evidence"
    if ms == "absent":
        return "insufficient_cell_or_ms_evidence"
    return "uncertain_repository_evidence"


def repository_triage_consistency(triage_class: str, cell: str, ms: str) -> str:
    triage_class = triage_class.lower()
    cell = cell.lower()
    ms = ms.lower()
    if triage_class in {"likely_true_scp", "possible_true_scp"} and (
        cell in {"absent", "contradictory"} or ms == "absent"
    ):
        return "overcall_vs_structured_evidence"
    return "consistent_or_conservative"


def route_publication(priority: str, final_class: str, model_class: str) -> tuple[str, str]:
    if final_class == "yes":
        return (
            "include_candidate",
            "Stage-04 final classification is yes; retain for downstream QC, not as an unquestioned final positive.",
        )
    if priority == "A_specific":
        return (
            "review_high",
            "Specific SCP discovery evidence conflicts with a Stage-04 non-positive decision; historical true SCP controls occur in this pattern.",
        )
    if priority == "B_method":
        return (
            "review_medium",
            "Method-level SCP evidence is insufficient for inclusion but should be independently adjudicated.",
        )
    if priority == "C_broad" and model_class == "yes":
        return (
            "review_medium",
            "Broad discovery evidence plus a positive Stage-04 model opinion was vetoed by deterministic gating; preserve for secondary review.",
        )
    if priority == "C_broad" and model_class == "uncertain":
        return (
            "review_low",
            "Broad discovery evidence with uncertain Stage-04 model support; retain as low-priority recall review.",
        )
    return (
        "likely_non_scp",
        "Publication-backed candidate has only broad/adjacent discovery support and no positive Stage-04 semantic support.",
    )


def route_repository(priority: str, cell: str, ms: str) -> tuple[str, str]:
    cell = cell.lower()
    ms = ms.lower()
    if priority == "A_specific":
        if cell == "direct" and ms == "direct":
            return (
                "include_candidate",
                "Specific SCP discovery evidence is corroborated by direct individual-cell and direct MS/proteomics repository evidence.",
            )
        return (
            "review_high",
            "Specific SCP discovery evidence is present, but repository evidence does not independently establish both individual-cell identity and MS/proteomics.",
        )
    if priority == "B_method":
        if cell == "implied" and ms == "direct":
            return (
                "review_medium",
                "Method evidence plus direct MS/proteomics and implied individual-cell evidence requires independent adjudication.",
            )
        if ms == "direct":
            return (
                "review_low",
                "Method evidence collides with direct MS/proteomics but lacks reliable individual-cell evidence.",
            )
        return (
            "likely_non_scp",
            "Method-name evidence is not corroborated by individual-cell MS/proteomics evidence.",
        )
    if priority == "C_broad":
        if cell == "direct" and ms == "direct":
            return (
                "review_medium",
                "Broad discovery evidence was interpreted as direct cell+MS evidence by Qwen, but broad-language overcalls were common and need independent review.",
            )
        if cell == "implied" and ms in {"direct", "implied"}:
            return (
                "review_low",
                "Broad evidence has only implied individual-cell support; retain for low-priority recall review.",
            )
        return (
            "likely_non_scp",
            "Broad repository evidence lacks a reliable individual-cell MS/proteomics conjunction or is contradictory.",
        )
    return (
        "likely_non_scp",
        "Adjacent-only discovery evidence is not sufficient for true SCP inclusion.",
    )


def review_flags(candidate: dict[str, Any], row: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if candidate.get("broad_only") is True:
        flags.append("broad_only_discovery")
    if candidate.get("has_negative_context") is True:
        flags.append("negative_context_present")
    if candidate.get("adjacent_labels"):
        flags.append("adjacent_discovery_label")
    if row.get("semantic_evidence_mode") == "publication_backed":
        if row.get("stage04_classification") == "yes" and row.get("stage04_grounded_sample_count", 0) == 0:
            flags.append("stage04_positive_without_grounded_sample")
        if row.get("stage04_classification") != "yes" and candidate.get("semantic_priority") == "A_specific":
            flags.append("specific_discovery_vs_stage04_conflict")
        if row.get("stage04_classification") == "yes" and candidate.get("semantic_priority") != "A_specific":
            flags.append("stage04_positive_from_non_specific_discovery")
    else:
        if row.get("repository_triage_consistency") == "overcall_vs_structured_evidence":
            flags.append("repository_qwen_overcall")
        if row.get("repository_individual_cell_evidence") == "contradictory":
            flags.append("contradictory_cell_evidence")
    return flags


def row_for_candidate(
    candidate: dict[str, Any],
    publication_summary: dict[str, Any] | None,
    repository_triage: dict[str, str] | None,
) -> dict[str, Any]:
    accession = text(candidate.get("accession"))
    priority = text(candidate.get("semantic_priority"))
    mode = text(candidate.get("semantic_evidence_mode"))
    base: dict[str, Any] = {
        "accession": accession,
        "dataset_title": candidate.get("dataset_title", ""),
        "dataset_description": candidate.get("dataset_description", ""),
        "semantic_evidence_mode": mode,
        "discovery_score": candidate.get("discovery_score", ""),
        "discovery_tier": candidate.get("discovery_tier", ""),
        "semantic_priority": priority,
        "evidence_profile": candidate.get("evidence_profile", ""),
        "broad_only": candidate.get("broad_only", False),
        "has_negative_context": candidate.get("has_negative_context", False),
        "specific_scp_labels": candidate.get("specific_scp_labels", []),
        "method_labels": candidate.get("method_labels", []),
        "broad_context_labels": candidate.get("broad_context_labels", []),
        "adjacent_labels": candidate.get("adjacent_labels", []),
        "negative_context_labels": candidate.get("negative_context_labels", []),
        "discovery_hits": candidate.get("hits", []),
    }

    if mode == "publication_backed":
        if not publication_summary:
            raise RuntimeError(f"Missing Stage-04 annotation for publication-backed candidate {accession}")
        base.update(publication_summary)
        route, reason = route_publication(
            priority,
            text(publication_summary.get("stage04_classification")).lower(),
            text(publication_summary.get("stage04_model_classification")).lower(),
        )
        base.update(
            {
                "repository_triage_class": "",
                "repository_individual_cell_evidence": "",
                "repository_ms_proteomics_evidence": "",
                "repository_normalized_evidence": "",
                "repository_triage_consistency": "",
                "repository_triage_reason": "",
            }
        )
    elif mode == "repository_only":
        if not repository_triage:
            raise RuntimeError(f"Missing repository triage result for repository-only candidate {accession}")
        cell = text(repository_triage.get("individual_cell_measurement_evidence")).lower()
        ms = text(repository_triage.get("mass_spectrometry_proteomics_evidence")).lower()
        triage_class = text(repository_triage.get("triage_class")).lower()
        normalized = normalize_repository_evidence(cell, ms)
        consistency = repository_triage_consistency(triage_class, cell, ms)
        route, reason = route_repository(priority, cell, ms)
        base.update(
            {
                "publication_annotation_count": 0,
                "stage04_classification": "",
                "stage04_model_classification": "",
                "stage04_gate_supported": "",
                "stage04_gate_tiers": [],
                "stage04_gate_reasons": [],
                "stage04_gate_evidence": [],
                "stage04_support_scopes": [],
                "stage04_publication_titles": [],
                "stage04_publication_dois": [],
                "stage04_source_kinds": [],
                "stage04_accession_mentioned": "",
                "stage04_grounded_sample_count": 0,
                "stage04_sample_summaries": [],
                "stage04_validation_warnings": [],
                "stage04_annotation_paths": [],
                "repository_triage_class": triage_class,
                "repository_individual_cell_evidence": cell,
                "repository_ms_proteomics_evidence": ms,
                "repository_normalized_evidence": normalized,
                "repository_triage_consistency": consistency,
                "repository_triage_reason": repository_triage.get("reason", ""),
            }
        )
    else:
        raise RuntimeError(f"Unknown semantic evidence mode for {accession}: {mode!r}")

    base["unified_route"] = route
    base["unified_route_reason"] = reason
    base["secondary_review_required"] = route in REVIEW_ROUTES
    base["review_flags"] = review_flags(candidate, base)
    return base


TSV_FIELDS = [
    "accession",
    "dataset_title",
    "semantic_evidence_mode",
    "discovery_score",
    "discovery_tier",
    "semantic_priority",
    "evidence_profile",
    "broad_only",
    "has_negative_context",
    "specific_scp_labels",
    "method_labels",
    "broad_context_labels",
    "adjacent_labels",
    "negative_context_labels",
    "stage04_classification",
    "stage04_model_classification",
    "stage04_gate_supported",
    "stage04_gate_tiers",
    "stage04_gate_reasons",
    "stage04_gate_evidence",
    "stage04_support_scopes",
    "stage04_publication_titles",
    "stage04_publication_dois",
    "stage04_source_kinds",
    "stage04_accession_mentioned",
    "stage04_grounded_sample_count",
    "stage04_sample_summaries",
    "stage04_validation_warnings",
    "stage04_annotation_paths",
    "repository_triage_class",
    "repository_individual_cell_evidence",
    "repository_ms_proteomics_evidence",
    "repository_normalized_evidence",
    "repository_triage_consistency",
    "repository_triage_reason",
    "unified_route",
    "secondary_review_required",
    "unified_route_reason",
    "review_flags",
]


def known_positive_rows(path: Path, unified: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = read_table(path)
    out: list[dict[str, Any]] = []
    positive_count = 0
    routed_count = 0
    likely_non_count = 0
    for raw in rows:
        accession = text(raw.get("pxd_accession") or raw.get("accession")).upper()
        marker = raw.get("contains_true_single_cell_ms")
        if marker is None:
            marker = raw.get("expected_positive")
        if marker is not None and text(marker) and not truthy(marker):
            continue
        if not accession:
            continue
        positive_count += 1
        u = unified.get(accession)
        route = u.get("unified_route", "missing") if u else "missing"
        if u:
            routed_count += 1
        if route == "likely_non_scp":
            likely_non_count += 1
        out.append(
            {
                "accession": accession,
                "benchmark_title": raw.get("dataset_title", ""),
                "unified_route": route,
                "semantic_priority": u.get("semantic_priority", "") if u else "",
                "semantic_evidence_mode": u.get("semantic_evidence_mode", "") if u else "",
                "route_reason": u.get("unified_route_reason", "") if u else "candidate not present",
            }
        )
    summary = {
        "known_positive_rows": positive_count,
        "present_in_unified_candidates": routed_count,
        "likely_non_scp_routes": likely_non_count,
        "route_recall_non_likely_non_scp": (
            (routed_count - likely_non_count) / positive_count if positive_count else None
        ),
    }
    return out, summary


def main() -> None:
    args = parse_args()
    candidate_diag = {r["accession"]: r for r in read_jsonl(Path(args.candidate_diagnostics))}
    pub_candidates = read_jsonl(Path(args.publication_candidates))
    repo_candidates = read_jsonl(Path(args.repository_candidates))
    annotations = discover_annotations(Path(args.annotations_dir))
    triage_rows = read_table(Path(args.repository_triage))
    triage = {text(r.get("accession")): r for r in triage_rows}

    pub_accessions = {text(r.get("accession")) for r in pub_candidates}
    repo_accessions = {text(r.get("accession")) for r in repo_candidates}
    overlap = pub_accessions & repo_accessions
    if overlap:
        raise RuntimeError(f"Candidates occur in both semantic lanes: {sorted(overlap)[:10]}")
    all_semantic = pub_accessions | repo_accessions
    diag_accessions = set(candidate_diag)
    if all_semantic != diag_accessions:
        missing_semantic = sorted(diag_accessions - all_semantic)
        extra_semantic = sorted(all_semantic - diag_accessions)
        raise RuntimeError(
            "Candidate coverage mismatch: "
            f"missing_semantic={missing_semantic[:10]} extra_semantic={extra_semantic[:10]}"
        )

    rows: list[dict[str, Any]] = []
    candidate_lookup = {text(r.get("accession")): r for r in pub_candidates + repo_candidates}
    for accession in sorted(diag_accessions):
        candidate = candidate_lookup[accession]
        # Prefer the canonical candidate-audit fields when available, while retaining hits/publication metadata.
        merged = dict(candidate)
        for key, value in candidate_diag[accession].items():
            if key not in {"hits", "publication_contents", "publication_pdfs"}:
                merged[key] = value
        if accession in pub_accessions:
            merged["semantic_evidence_mode"] = "publication_backed"
            pub_summary = summarize_publication_annotations(annotations.get(accession, []))
            row = row_for_candidate(merged, pub_summary, None)
        else:
            merged["semantic_evidence_mode"] = "repository_only"
            row = row_for_candidate(merged, None, triage.get(accession))
        rows.append(row)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_tsv(output_dir / "unified_semantic_manifest.tsv", rows, TSV_FIELDS)
    write_jsonl(output_dir / "unified_semantic_manifest.jsonl", rows)

    for route in ROUTES:
        subset = [r for r in rows if r["unified_route"] == route]
        write_tsv(output_dir / f"{route}.tsv", subset, TSV_FIELDS)

    review_rows = [r for r in rows if r["secondary_review_required"]]
    write_tsv(output_dir / "secondary_review_queue.tsv", review_rows, TSV_FIELDS)
    write_jsonl(output_dir / "secondary_review_queue.jsonl", review_rows)

    # Independent-QC queue: provisional includes plus all review routes.  The
    # v0.1.7 full run yields 219 rows here.  likely_non_scp remains retained in
    # the unified manifest and can be reviewed later if recall auditing demands it.
    qc_rows = [r for r in rows if r["unified_route"] != "likely_non_scp"]
    write_tsv(output_dir / "qc_candidate_queue.tsv", qc_rows, TSV_FIELDS)
    write_jsonl(output_dir / "qc_candidate_queue.jsonl", qc_rows)

    # Explicit audit slices for the two calibration failures observed in the
    # v0.1.7 full run. These are diagnostics only and never mutate routing.
    repository_overcalls = [
        r for r in rows
        if r["semantic_evidence_mode"] == "repository_only"
        and r["repository_triage_consistency"] == "overcall_vs_structured_evidence"
    ]
    write_tsv(
        output_dir / "repository_triage_overcalls.tsv",
        repository_overcalls,
        TSV_FIELDS,
    )
    stage04_specific_conflicts = [
        r for r in rows
        if r["semantic_evidence_mode"] == "publication_backed"
        and r["semantic_priority"] == "A_specific"
        and r["stage04_classification"] != "yes"
    ]
    write_tsv(
        output_dir / "stage04_specific_conflicts.tsv",
        stage04_specific_conflicts,
        TSV_FIELDS,
    )
    stage04_non_specific_positives = [
        r for r in rows
        if r["semantic_evidence_mode"] == "publication_backed"
        and r["semantic_priority"] != "A_specific"
        and r["stage04_classification"] == "yes"
    ]
    write_tsv(
        output_dir / "stage04_non_specific_positives.tsv",
        stage04_non_specific_positives,
        TSV_FIELDS,
    )
    write_tsv(
        output_dir / "review_decisions.template.tsv",
        [
            {
                "accession": r["accession"],
                "unified_route": r["unified_route"],
                "semantic_evidence_mode": r["semantic_evidence_mode"],
                "semantic_priority": r["semantic_priority"],
                "final_decision": "",
                "review_note": "",
            }
            for r in review_rows
        ],
        [
            "accession",
            "unified_route",
            "semantic_evidence_mode",
            "semantic_priority",
            "final_decision",
            "review_note",
        ],
    )

    route_counts = Counter(r["unified_route"] for r in rows)
    mode_route_counts: dict[str, dict[str, int]] = {}
    for mode in ("publication_backed", "repository_only"):
        mode_route_counts[mode] = dict(
            Counter(r["unified_route"] for r in rows if r["semantic_evidence_mode"] == mode)
        )

    stage04_counts = Counter(
        r["stage04_classification"]
        for r in rows
        if r["semantic_evidence_mode"] == "publication_backed"
    )
    stage04_priority_counts: dict[str, dict[str, int]] = {}
    for priority in ("A_specific", "B_method", "C_broad", "D_adjacent"):
        stage04_priority_counts[priority] = dict(
            Counter(
                r["stage04_classification"]
                for r in rows
                if r["semantic_evidence_mode"] == "publication_backed"
                and r["semantic_priority"] == priority
            )
        )

    repo_triage_counts = Counter(
        r["repository_triage_class"]
        for r in rows
        if r["semantic_evidence_mode"] == "repository_only"
    )
    repo_normalized_counts = Counter(
        r["repository_normalized_evidence"]
        for r in rows
        if r["semantic_evidence_mode"] == "repository_only"
    )
    repo_overcalls = sum(
        r["repository_triage_consistency"] == "overcall_vs_structured_evidence"
        for r in rows
        if r["semantic_evidence_mode"] == "repository_only"
    )

    summary: dict[str, Any] = {
        "candidates": len(rows),
        "candidate_loss": 0,
        "publication_backed": len(pub_accessions),
        "repository_only": len(repo_accessions),
        "route_counts": dict(route_counts),
        "mode_route_counts": mode_route_counts,
        "secondary_review_candidates": len(review_rows),
        "independent_qc_candidates": len(qc_rows),
        "stage04_class_counts": dict(stage04_counts),
        "stage04_class_counts_by_priority": stage04_priority_counts,
        "repository_original_triage_counts": dict(repo_triage_counts),
        "repository_normalized_evidence_counts": dict(repo_normalized_counts),
        "repository_triage_overcalls_vs_own_structured_evidence": repo_overcalls,
        "stage04_specific_signal_conflicts": len(stage04_specific_conflicts),
        "stage04_non_specific_positives": len(stage04_non_specific_positives),
        "note": (
            "Routing is recall-preserving and provisional. likely_non_scp rows are retained; "
            "include_candidate rows still require downstream QC."
        ),
    }

    if args.known_positives:
        kp_path = Path(args.known_positives)
        if kp_path.is_file():
            unified_lookup = {r["accession"]: r for r in rows}
            kp_rows, kp_summary = known_positive_rows(kp_path, unified_lookup)
            write_tsv(
                output_dir / "known_positive_route_audit.tsv",
                kp_rows,
                [
                    "accession",
                    "benchmark_title",
                    "unified_route",
                    "semantic_priority",
                    "semantic_evidence_mode",
                    "route_reason",
                ],
            )
            summary["known_positive_route_audit"] = kp_summary
        else:
            summary["known_positive_route_audit"] = {"error": f"not found: {kp_path}"}

    (output_dir / "semantic_unification_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    print(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
