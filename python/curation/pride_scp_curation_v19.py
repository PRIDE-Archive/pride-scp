#!/usr/bin/env python3
"""Evidence-first PRIDE_SCP curation v19 shadow lane.

This module is intentionally separate from the historical Stage04 final
classification.  It combines raw repository discovery evidence with the raw
semantic evidence blocks saved by Stage04, asks a small local model to extract
GT196-aligned biological facts, validates every cited evidence reference, and
then applies a deterministic accession-level decision policy.

GT196 is never read by this production/shadow curation script.  Frozen GT is
used only by the separate evaluation module.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import requests

DEFAULT_OLLAMA_URL = "http://localhost:11434/api/generate"
PIPELINE_VERSION = "v19-shadow-2.7.1"
PXD_RE = re.compile(r"^PXD\d{6}$", re.I)
MSV_RE = re.compile(r"^MSV\d{9}$", re.I)
ACCESSION_RE = re.compile(r"^(?:PXD\d{6}|MSV\d{9})$", re.I)

YES_NO_UNCERTAIN = ["yes", "no", "uncertain"]
POOLING_STAGES = [
    "none",
    "before_identity_preserving_processing",
    "after_identity_preserving_labeling",
    "both_or_mixed",
    "uncertain",
]
UNIT_CLASSES = [
    "single_cell",
    "single_multinucleated_cell",
    "single_oocyte",
    "single_blastomere",
    "single_cell_stage_embryo_or_zygote",
    "multi_cell_embryo",
    "pooled_cells",
    "cell_population",
    "bulk_or_diluted_digest",
    "spatial_region_or_multicell_area",
    "subcellular_sample_from_one_identified_cell",
    "other",
    "uncertain",
]

FACT_FIELDS = [
    "biological_sample_unit",
    "biological_unit_class",
    "true_single_cell_ms_samples_present",
    "cells_per_target_ms_sample",
    "individual_identity_preserved_to_ms",
    "destructive_pooling_before_ms",
    "pooling_stage",
    "benchmark_only",
    "adjacent_single_cell_only",
    "reanalysis_only",
    "mixed_design",
]

CURATION_SCHEMA = {
    "type": "object",
    "properties": {
        "biological_sample_unit": {"type": ["string", "null"], "maxLength": 180},
        "biological_unit_class": {"type": "string", "enum": UNIT_CLASSES},
        "true_single_cell_ms_samples_present": {
            "type": "string",
            "enum": YES_NO_UNCERTAIN,
        },
        "cells_per_target_ms_sample": {"type": ["string", "null"], "maxLength": 120},
        "individual_identity_preserved_to_ms": {
            "type": "string",
            "enum": YES_NO_UNCERTAIN,
        },
        "destructive_pooling_before_ms": {
            "type": "string",
            "enum": YES_NO_UNCERTAIN,
        },
        "pooling_stage": {"type": "string", "enum": POOLING_STAGES},
        "benchmark_only": {"type": "string", "enum": YES_NO_UNCERTAIN},
        "adjacent_single_cell_only": {"type": "string", "enum": YES_NO_UNCERTAIN},
        "reanalysis_only": {"type": "string", "enum": YES_NO_UNCERTAIN},
        "mixed_design": {"type": "string", "enum": YES_NO_UNCERTAIN},
        "evidence_sufficiency": {
            "type": "string",
            "enum": ["sufficient", "partial", "insufficient"],
        },
        "evidence_refs": {
            "type": "object",
            "properties": {
                field: {
                    "type": "array",
                    "maxItems": 6,
                    "items": {"type": "string", "maxLength": 12},
                }
                for field in FACT_FIELDS
            },
            "required": FACT_FIELDS,
            "additionalProperties": False,
        },
        "reason": {"type": "string", "maxLength": 650},
    },
    "required": [
        *FACT_FIELDS,
        "evidence_sufficiency",
        "evidence_refs",
        "reason",
    ],
    "additionalProperties": False,
}


def text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def write_tsv(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write heterogeneous summary rows to TSV using a stable union of keys."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for field in row:
            if field not in seen:
                seen.add(field)
                fields.append(field)
    if not fields:
        fields = ["accession", "run_status", "curation_decision", "decision_reason", "error"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            delimiter="\t",
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                payload = json.loads(line)
                if isinstance(payload, dict):
                    rows.append(payload)
    return rows


def _annotation_publication_linkage(annotation: dict[str, Any]) -> tuple[list[str], str]:
    """Return explicit publication PXD aliases and the semantic-evidence path.

    Stage04 annotations historically lived under a target-accession directory, but that
    directory is not authoritative publication provenance.  When the saved semantic bundle
    explicitly names one or more PXD accessions, those explicit mentions are authoritative
    for routing.  Bundles without an explicit PXD remain attached to their declared target.
    """
    prov = annotation.get("provenance")
    if not isinstance(prov, dict):
        return [], ""
    evidence_path_text = text(prov.get("semantic_evidence_file"))
    if not evidence_path_text:
        return [], ""
    path = Path(evidence_path_text)
    if not path.is_file():
        return [], evidence_path_text
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], evidence_path_text
    if not isinstance(bundle, dict):
        return [], evidence_path_text
    pxds: list[str] = []
    raw_pxds = bundle.get("publication_pride_accessions")
    if isinstance(raw_pxds, list):
        values = raw_pxds
    elif raw_pxds is None:
        values = []
    else:
        values = [raw_pxds]
    for value in values:
        for match in re.findall(r"\bPXD\d{6}\b", text(value), re.I):
            acc = match.upper()
            if acc not in pxds:
                pxds.append(acc)
    return pxds, evidence_path_text


def load_annotations_with_linkage(root: Path) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Load Stage04 annotations through a provenance-safe publication→PXD router.

    Rules are GT-blind:
      * an explicit publication PXD list routes the bundle only to those PXD accessions;
      * a declared directory target absent from an explicit list is quarantined for that target;
      * an explicitly named PXD may receive a cross-directory publication route;
      * an unscoped bundle (no explicit PXD) remains attached to its declared target.
    """
    by_accession: dict[str, list[dict[str, Any]]] = defaultdict(list)
    diagnostics: list[dict[str, Any]] = []
    if not root.exists():
        return by_accession, diagnostics
    for path in sorted(root.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        declared = text(payload.get("target_accession")).upper()
        if not PXD_RE.fullmatch(declared):
            continue
        payload = dict(payload)
        payload["_annotation_path"] = str(path.resolve())
        explicit_pxds, evidence_path = _annotation_publication_linkage(payload)
        payload["_annotation_declared_target"] = declared
        payload["_annotation_explicit_pxds"] = list(explicit_pxds)
        payload["_annotation_semantic_evidence_file"] = evidence_path

        if explicit_pxds:
            routes = explicit_pxds
            declared_matches = declared in explicit_pxds
        else:
            routes = [declared]
            declared_matches = True

        diagnostics.append({
            "annotation_path": str(path.resolve()),
            "declared_target_accession": declared,
            "explicit_publication_pxds": list(explicit_pxds),
            "declared_target_matches_explicit": declared_matches,
            "semantic_evidence_file": evidence_path,
        })

        for routed in routes:
            routed_payload = dict(payload)
            routed_payload["_annotation_route_kind"] = (
                "declared_unscoped" if not explicit_pxds
                else "explicit_match" if routed == declared
                else "explicit_cross_directory_route"
            )
            routed_payload["_annotation_routed_accession"] = routed
            by_accession[routed].append(routed_payload)
    return by_accession, diagnostics


def load_annotations(root: Path) -> dict[str, list[dict[str, Any]]]:
    """Compatibility wrapper returning provenance-safe accession-routed annotations."""
    return load_annotations_with_linkage(root)[0]


def _clip(value: Any, limit: int = 1400) -> str:
    s = re.sub(r"\s+", " ", text(value)).strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def _iter_evidence_records(annotation: dict[str, Any]) -> Iterable[tuple[str, str, str]]:
    """Yield (source_kind, source_label, raw_text) from saved Stage04 evidence."""
    prov = annotation.get("provenance")
    if not isinstance(prov, dict):
        return
    evidence_path = text(prov.get("semantic_evidence_file"))
    if not evidence_path:
        return
    path = Path(evidence_path)
    if not path.is_file():
        return
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    tasks = bundle.get("tasks") if isinstance(bundle, dict) else None
    if not isinstance(tasks, dict):
        return
    for task_name in (
        "samples",
        "preparation",
        "single_cell_performance",
        "low_input_performance",
    ):
        records = tasks.get(task_name)
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            raw = text(record.get("text"))
            if not raw:
                continue
            section = text(record.get("section")) or "unknown_section"
            page = text(record.get("page"))
            label = f"publication:{task_name}:{section}"
            if page:
                label += f":page={page}"
            yield "publication", label, raw




DETERMINISTIC_RELEVANCE_RE = re.compile(
    r"\b(?:single[- ]cell|individual|one\s+(?:cell|oocyte|blastomere|zygote|neuron|fibre|fiber)|"
    r"oocyte|blastomere|zygote|neuron|fibre|fiber|myofibre|myofiber|hepatocyte|cardiomyocyte|"
    r"macrophage|monocyte|neutrophil|lymphocyte|astrocyte|fibroblast|bacterium|bacteria|microglia|"
    r"proteom|mass\s+spectrom|lc[- ]?ms|ms/ms|maldi|lysis|digest|dispens|isolat|sort|collect|"
    r"nanowell|well|droplet|sample|replicate|bulk|dilut|benchmark|pool|single[- ]cell[- ]like|"
    r"near[- ]single[- ]cell|almost[- ]single[- ]cell)\b",
    re.I,
)


def _iter_json_string_leaves(value: Any, prefix: str = "", depth: int = 0) -> Iterable[tuple[str, str]]:
    """Yield path/value pairs from a JSON structure without assuming a repository schema."""
    if depth > 8:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            yield from _iter_json_string_leaves(child, child_prefix, depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value[:500]):
            child_prefix = f"{prefix}[{index}]"
            yield from _iter_json_string_leaves(child, child_prefix, depth + 1)
    elif isinstance(value, (str, int, float)):
        raw = text(value)
        if raw:
            yield prefix, raw


def _load_json_file(path_text: Any) -> Any:
    path = Path(text(path_text)) if text(path_text) else Path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _candidate_identity_metadata(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return source-derived identity metadata without consulting GT."""
    aliases: set[str] = set()
    raw_aliases = candidate.get("trusted_pxd_aliases") or []
    if isinstance(raw_aliases, str):
        raw_aliases = [raw_aliases]
    for value in raw_aliases:
        for match in re.finditer(r"\bPXD\d{6}\b", text(value), re.I):
            aliases.add(match.group(0).upper())
    canonical = text(candidate.get("canonical_dataset_identity"))
    project = _load_json_file(candidate.get("project_json_path"))
    if isinstance(project, dict):
        for value in project.get("pxdAliases") or []:
            for match in re.finditer(r"\bPXD\d{6}\b", text(value), re.I):
                aliases.add(match.group(0).upper())
        identity = project.get("sourceIdentity")
        if isinstance(identity, dict):
            canonical = canonical or text(identity.get("canonical_dataset_identity"))
            for value in identity.get("pxd_aliases") or []:
                for match in re.finditer(r"\bPXD\d{6}\b", text(value), re.I):
                    aliases.add(match.group(0).upper())
    return {"canonical_dataset_identity": canonical, "trusted_pxd_aliases": sorted(aliases), "gt_used": False}


def _native_bridge_evidence(candidate: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Load GT-blind MassIVE native evidence sidecar records, when present."""
    payload = _load_json_file(candidate.get("native_evidence_path"))
    if not isinstance(payload, dict):
        return []
    rows: list[tuple[str, str, str]] = []
    for item in payload.get("evidence_items") or []:
        if not isinstance(item, dict):
            continue
        raw = text(item.get("text"))
        if not raw:
            continue
        source = text(item.get("source")) or "massive_native"
        source_path = text(item.get("source_path")) or "unknown"
        categories = ",".join(str(x) for x in (item.get("categories") or []))
        label = f"repository:native_bridge:{source}:{source_path}"
        if categories:
            label += f":categories={categories}"
        rows.append(("repository", label, raw))
    return rows


def _candidate_annotations(
    annotations: dict[str, list[dict[str, Any]]], candidate: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Route publication annotations through source-derived canonical identity."""
    accession = text(candidate.get("accession")).upper()
    identity = _candidate_identity_metadata(candidate)
    aliases = identity["trusted_pxd_aliases"]
    docs: list[dict[str, Any]] = []
    seen: set[str] = set()
    routes = [accession] if PXD_RE.fullmatch(accession) else aliases
    for route in routes:
        for annotation in annotations.get(route, []):
            key = text(annotation.get("_annotation_path")) or json.dumps(annotation, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            routed = dict(annotation)
            routed["_annotation_identity_route"] = route
            routed["_annotation_target_accession"] = accession
            routed["_annotation_allowed_pxds"] = list(aliases if MSV_RE.fullmatch(accession) else [accession])
            docs.append(routed)
    return docs, identity


def _snapshot_extra_evidence(candidate: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Collect deterministic-only evidence from raw project/files/SDRF snapshot artifacts.

    These records are never sent to the small model in shadow-2.6.  They are used only by
    deterministic evidence detectors and therefore may be broader than the bounded model packet.
    """
    out: list[tuple[str, str, str]] = []
    for field, kind in (("project_json_path", "project_json"), ("files_json_path", "files_json")):
        payload = _load_json_file(candidate.get(field))
        if payload is None:
            continue
        for path, raw in _iter_json_string_leaves(payload):
            collapsed = re.sub(r"\s+", " ", raw).strip()
            match_text = collapsed.replace("_", " ")
            if not collapsed or not DETERMINISTIC_RELEVANCE_RE.search(match_text):
                continue
            out.append(("repository", f"repository:{kind}:{path}", collapsed))

    sdrf_path_text = text(candidate.get("sdrf_path"))
    sdrf_path = Path(sdrf_path_text) if sdrf_path_text else Path()
    if sdrf_path.is_file():
        try:
            lines = sdrf_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            lines = []
        header = lines[0] if lines else ""
        for row_index, line in enumerate(lines[1:401], start=1):
            collapsed = re.sub(r"\s+", " ", line).strip()
            match_text = collapsed.replace("_", " ")
            if not collapsed or not DETERMINISTIC_RELEVANCE_RE.search(match_text):
                continue
            text_row = f"SDRF columns: {header} | row {row_index}: {collapsed}" if header else collapsed
            out.append(("repository", f"repository:sdrf:row={row_index}", text_row))
    return out


def _annotation_extra_evidence(
    annotation: dict[str, Any], accession: str, allowed_pxds: set[str] | None = None
) -> tuple[list[tuple[str, str, str]], dict[str, Any]]:
    """Return all saved Stage04 semantic records plus publication-link diagnostics.

    If the evidence bundle explicitly lists PXD accessions and the target accession is absent,
    the bundle is treated as publication-linkage-incompatible for deterministic enrichment.
    This does not rewrite the historical model facts; it prevents adding more evidence from a
    demonstrably mismatched publication bundle.
    """
    diag: dict[str, Any] = {
        "annotation_path": text(annotation.get("_annotation_path")),
        "semantic_evidence_file": "",
        "publication_source_path": "",
        "publication_title_candidate": "",
        "publication_pride_accessions": [],
        "explicit_accession_mismatch": False,
        "declared_target_accession": text(annotation.get("_annotation_declared_target") or annotation.get("target_accession")).upper(),
        "route_kind": text(annotation.get("_annotation_route_kind")) or "declared_unscoped",
        "routed_accession": text(annotation.get("_annotation_routed_accession") or accession).upper(),
    }
    prov = annotation.get("provenance")
    if not isinstance(prov, dict):
        return [], diag
    evidence_path_text = text(prov.get("semantic_evidence_file"))
    diag["semantic_evidence_file"] = evidence_path_text
    if not evidence_path_text:
        return [], diag
    path = Path(evidence_path_text)
    if not path.is_file():
        return [], diag
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], diag
    if not isinstance(bundle, dict):
        return [], diag
    diag["publication_source_path"] = text(bundle.get("publication_source_path"))
    diag["publication_title_candidate"] = text(bundle.get("publication_title_candidate"))
    pxds = []
    raw_pxds = bundle.get("publication_pride_accessions")
    if isinstance(raw_pxds, list):
        for value in raw_pxds:
            for match in re.findall(r"\bPXD\d{6}\b", text(value), re.I):
                pxds.append(match.upper())
    diag["publication_pride_accessions"] = sorted(set(pxds))
    allowed = {x.upper() for x in (allowed_pxds or set()) if PXD_RE.fullmatch(x)}
    if PXD_RE.fullmatch(accession):
        allowed.add(accession.upper())
    if pxds and not (set(pxds) & allowed):
        diag["explicit_accession_mismatch"] = True
        return [], diag

    rows: list[tuple[str, str, str]] = []
    tasks = bundle.get("tasks")
    if isinstance(tasks, dict):
        for task_name, records in tasks.items():
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                raw = text(record.get("text"))
                if not raw or not DETERMINISTIC_RELEVANCE_RE.search(raw):
                    continue
                section = text(record.get("section")) or "unknown_section"
                page = text(record.get("page"))
                label = f"publication:{task_name}:{section}"
                if page:
                    label += f":page={page}"
                rows.append(("publication", label, raw))
    return rows, diag


def build_deterministic_evidence_items(
    candidate: dict[str, Any],
    annotations: list[dict[str, Any]],
    base_items: list[dict[str, str]],
    *,
    max_items: int = 180,
    max_chars: int = 120_000,
    quarantine_base_publication: bool = False,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Build a broader evidence pool for deterministic replay without changing the model packet."""
    accession = text(candidate.get("accession")).upper()
    raw: list[tuple[str, str, str]] = []
    quarantined_base_publication_items = 0
    # Historical model packets can contain publication evidence selected before publication↔PXD
    # linkage auditing existed.  During a linkage-clean replay, packet publication rows are
    # quarantined only for a target with a proven explicit publication/PXD mismatch.  Other
    # historical publication rows are retained because some old annotation bundles are no
    # longer available on disk.  Fresh v19-shadow-2.7 packets are linkage-clean upstream.
    for item in base_items:
        if quarantine_base_publication and text(item.get("source_kind")).lower() == "publication":
            quarantined_base_publication_items += 1
            continue
        raw.append((text(item.get("source_kind")), text(item.get("source_label")), text(item.get("text"))))
    snapshot_rows = _snapshot_extra_evidence(candidate)
    raw.extend(snapshot_rows)
    native_bridge_rows = _native_bridge_evidence(candidate)
    raw.extend(native_bridge_rows)
    identity_metadata = _candidate_identity_metadata(candidate)
    allowed_pxds = set(identity_metadata.get("trusted_pxd_aliases") or [])

    diagnostics = {
        "accession": accession,
        "base_item_count": len(base_items),
        "snapshot_extra_record_count": len(snapshot_rows),
        "native_bridge_extra_record_count": len(native_bridge_rows),
        "trusted_pxd_aliases": sorted(allowed_pxds),
        "canonical_dataset_identity": identity_metadata.get("canonical_dataset_identity", ""),
        "annotation_extra_record_count": 0,
        "publication_bundle_count": len(annotations),
        "publication_explicit_mismatch_count": 0,
        "publication_diagnostics": [],
        "quarantined_base_publication_item_count": quarantined_base_publication_items,
        "publication_route_kind_counts": {},
    }
    for annotation in annotations:
        rows, diag = _annotation_extra_evidence(annotation, accession, allowed_pxds)
        raw.extend(rows)
        diagnostics["annotation_extra_record_count"] += len(rows)
        diagnostics["publication_explicit_mismatch_count"] += int(bool(diag.get("explicit_accession_mismatch")))
        diagnostics["publication_diagnostics"].append(diag)
        route_kind = text(diag.get("route_kind")) or "declared_unscoped"
        diagnostics["publication_route_kind_counts"][route_kind] = diagnostics["publication_route_kind_counts"].get(route_kind, 0) + 1

    dedup: dict[str, tuple[str, str, str]] = {}
    for source_kind, source_label, raw_text in raw:
        collapsed = re.sub(r"\s+", " ", text(raw_text)).strip()
        if not collapsed:
            continue
        key = collapsed.lower()
        if key not in dedup:
            dedup[key] = (source_kind or "unknown", source_label or "unknown", collapsed)

    selected: list[dict[str, str]] = []
    used = 0
    base_texts = {re.sub(r"\s+", " ", text(x.get("text"))).strip().lower(): text(x.get("ref")) for x in base_items}
    extra_index = 1
    for source_kind, source_label, raw_text in dedup.values():
        clipped = _clip(raw_text, 1800)
        cost = len(clipped) + 80
        if selected and used + cost > max_chars:
            continue
        key = re.sub(r"\s+", " ", raw_text).strip().lower()
        ref = base_texts.get(key)
        if not ref:
            ref = f"D{extra_index:03d}"
            extra_index += 1
        selected.append({"ref": ref, "source_kind": source_kind, "source_label": source_label, "text": clipped})
        used += cost
        if len(selected) >= max_items:
            break
    diagnostics["deterministic_item_count"] = len(selected)
    diagnostics["deterministic_extra_item_count"] = sum(1 for x in selected if text(x.get("ref")).startswith("D"))
    return selected, diagnostics

def build_evidence_items(
    candidate: dict[str, Any],
    annotations: list[dict[str, Any]],
    *,
    max_items: int,
    max_chars: int,
) -> list[dict[str, str]]:
    raw_items: list[tuple[int, str, str, str]] = []

    title = text(candidate.get("dataset_title"))
    if title:
        raw_items.append((100, "repository", "dataset_title", title))
    description = text(candidate.get("dataset_description"))
    if description:
        raw_items.append((95, "repository", "dataset_description", description))

    for hit in candidate.get("hits", []) or []:
        if not isinstance(hit, dict):
            continue
        excerpt = text(hit.get("source_excerpt"))
        if not excerpt:
            continue
        lane = text(hit.get("lane")) or "discovery"
        label = text(hit.get("label"))
        term = text(hit.get("term"))
        source_label = f"discovery_hit:{lane}"
        if label:
            source_label += f":{label}"
        if term:
            source_label += f":term={term}"
        weight = int(hit.get("weight") or 0)
        raw_items.append((70 + min(max(weight, 0), 25), "repository", source_label, excerpt))

    for source_kind, source_label, raw in _native_bridge_evidence(candidate):
        priority = 88
        lower = source_label.lower()
        if "sample_metadata" in lower or "ms_method" in lower:
            priority = 94
        elif "publication" in lower or "metadata_file" in lower:
            priority = 90
        raw_items.append((priority, source_kind, source_label, raw))

    for annotation in annotations:
        pub_title = text(annotation.get("publication_title_candidate"))
        if pub_title:
            raw_items.append((90, "publication", "publication_title", pub_title))
        for source_kind, source_label, raw in _iter_evidence_records(annotation) or []:
            # Sample/preparation evidence is intentionally weighted above generic
            # discovery excerpts because biological-unit/pooling decisions depend
            # on these source passages.
            priority = 92 if ":samples:" in source_label or ":preparation:" in source_label else 82
            raw_items.append((priority, source_kind, source_label, raw))

    # Deduplicate by normalized text while preserving the best priority.
    dedup: dict[str, tuple[int, str, str, str]] = {}
    for row in raw_items:
        key = re.sub(r"\s+", " ", row[3]).strip().lower()
        if not key:
            continue
        previous = dedup.get(key)
        if previous is None or row[0] > previous[0]:
            dedup[key] = row

    ranked = sorted(dedup.values(), key=lambda x: -x[0])
    selected: list[dict[str, str]] = []
    used = 0
    for _, source_kind, source_label, raw in ranked:
        clipped = _clip(raw)
        cost = len(clipped) + 80
        if selected and used + cost > max_chars:
            continue
        ref = f"E{len(selected) + 1:03d}"
        selected.append(
            {
                "ref": ref,
                "source_kind": source_kind,
                "source_label": source_label,
                "text": clipped,
            }
        )
        used += cost
        if len(selected) >= max_items or used >= max_chars:
            break
    return selected


def format_evidence(accession: str, items: list[dict[str, str]]) -> str:
    lines = [f"Target accession: {accession}", "Evidence items:"]
    for item in items:
        lines.append(
            f"[{item['ref']}] source={item['source_kind']} label={item['source_label']}\n"
            f"{item['text']}"
        )
    return "\n\n".join(lines)


def system_prompt() -> str:
    return (
        "You are a strict evidence extractor for mass-spectrometry single-cell proteomics. "
        "Your job is to describe accession-specific biological facts, not to maximize positive calls. "
        "Use only the numbered evidence items supplied. Do not use outside knowledge and do not copy "
        "a previous classifier's judgment. A true qualifying target MS sample is one biological cell "
        "whose individual identity remains meaningful through MS interpretation. Post-label multiplex "
        "pooling is allowed. Destructive pooling of multiple biological cells before identity-preserving "
        "labeling/processing is not a qualifying single-cell sample. Diluted bulk digest or picogram bulk "
        "benchmarks are not cells. A sorted population, cell type, tissue region, or multi-cell spatial "
        "region is not one cell. A single skeletal muscle fibre/myofibre is one multinucleated biological "
        "cell when one fibre is processed as an individual target sample. One oocyte or one blastomere "
        "is one cell; a multi-cell embryo is not automatically one cell. A subcellular aspirate can qualify "
        "only when it is explicitly taken from one identified cell and that cell identity is preserved. "
        "Single-cell transcriptomics or imaging paired with bulk proteomics is adjacent_single_cell_only. "
        "A processed-data mirror/reanalysis without qualifying independent source MS data is reanalysis_only. "
        "Mixed-design means the accession contains both a genuine qualifying one-cell MS branch and one or "
        "more nonqualifying bulk/pooled/benchmark branches. biological_unit_class and cells_per_target_ms_sample "
        "must describe the qualifying branch when one exists; otherwise they describe the target proteomic sample. "
        "IMPORTANT: cell_population means that ONE target proteomic/MS sample contains multiple biological cells. "
        "Do NOT use cell_population merely because the study contains many separate single-cell samples, replicates, "
        "or cell types. If each target MS sample is one biological cell, use single_cell (or the appropriate specific "
        "single-cell class). Keep biological_sample_unit faithful to the cited organism/cell type and never substitute "
        "an example cell line such as HeLa when the evidence describes another organism or cell type. "
        "If an MS sample explicitly contains multiple biological cells before any identity-preserving label/barcode, "
        "true_single_cell_ms_samples_present must be no for that branch. Do not infer destructive proteomic pooling from "
        "fertilization, embryo culture, mating, eggs mixed with sperm/testes, or other biological-development steps that occur "
        "before individual target cells are later isolated. Do not infer identity loss merely because the workflow is label-free: "
        "a physically separate one-cell sample preserves identity even without a barcode. Set pooling_stage="
        "before_identity_preserving_processing only when evidence explicitly supports multiple biological cells contributing to "
        "the same target proteomic/MS sample before identity preservation. Use uncertain whenever accession-specific evidence "
        "does not establish a field. Before returning, ensure the categorical fields agree with the free-text reason. "
        "Every non-uncertain assertion must cite one or more evidence_refs."
    )


def user_prompt(accession: str, items: list[dict[str, str]]) -> str:
    return (
        "Extract the requested fields for this accession.\n"
        "For biological_sample_unit, use only the accession-specific biological unit literally supported by cited evidence. "
        "Do not copy organism or cell-type examples from these instructions; there are intentionally no sample-identity examples. "
        "Never name a cell type or organism absent from the cited evidence. Use null when unresolved. "
        "Use null if the accession-specific biological unit is unresolved.\n"
        "For cells_per_target_ms_sample, describe the number of biological cells contributing to ONE target MS sample, "
        "not the number of samples in the experiment. Prefer concise values such as 'one', 'mixed_design', an explicit "
        "count, or null when unresolved. Do not calculate counts. If evidence says 1 x 10^6 cells were isolated for a "
        "proteomic replicate/sample, that is a multi-cell target sample, not one cell.\n"
        "benchmark_only=yes only when no qualifying biological one-cell MS branch is present.\n"
        "adjacent_single_cell_only=yes only when the single-cell component is not itself qualifying MS proteomics.\n"
        "reanalysis_only=yes only when the accession is merely a reanalysis/processed-data mirror.\n"
        "If evidence supports a genuine one-cell branch plus benchmarks or pooled controls, set mixed_design=yes "
        "and true_single_cell_ms_samples_present=yes.\n\n"
        + format_evidence(accession, items)
    )


def call_ollama(accession: str, items: list[dict[str, str]], args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = {
        "model": args.model,
        "system": system_prompt(),
        "prompt": user_prompt(accession, items),
        "stream": False,
        "format": CURATION_SCHEMA,
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
    stats = {
        "wall_seconds": round(time.perf_counter() - started, 3),
        "prompt_tokens": data.get("prompt_eval_count", 0),
        "output_tokens": data.get("eval_count", 0),
    }
    return parsed, stats


DIRECT_SAMPLE_LABELS = (":samples:", ":preparation:")
NONQUALIFYING_UNIT_CLASSES = {
    "multi_cell_embryo",
    "pooled_cells",
    "cell_population",
    "bulk_or_diluted_digest",
    "spatial_region_or_multicell_area",
}
QUALIFYING_UNIT_CLASSES = {
    "single_cell",
    "single_multinucleated_cell",
    "single_oocyte",
    "single_blastomere",
    "single_cell_stage_embryo_or_zygote",
    "subcellular_sample_from_one_identified_cell",
}


def _number_phrase_is_multiple(value: str) -> bool:
    s = re.sub(r"[,~≈]", "", text(value).lower())
    if re.search(r"\b\d+\s*[x×]\s*10(?:\^?\d+|\d+)\b", s):
        return True
    numbers = [int(x) for x in re.findall(r"\b\d+\b", s)]
    if any(n > 1 for n in numbers):
        return True
    return bool(re.search(r"\b(two|three|four|five|six|seven|eight|nine|ten|dozen|tens|hundred|hundreds|thousand|thousands|million|millions)\b", s))


def cells_per_sample_is_multiple(value: Any) -> bool:
    s = text(value).lower()
    if not s or s in {"one", "1", "1 cell", "single", "uncertain", "unknown", "mixed_design"}:
        return False
    return _number_phrase_is_multiple(s) or "pooled" in s or "multiple" in s


def deterministic_multicell_evidence(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Find only high-specificity direct evidence that one target MS sample contains >1 cells.

    The grammar intentionally avoids broad phrases such as "20 or 40 cells is not enough",
    prior/background pooled-cell descriptions, and post-label TMT/booster pooling.  It is a
    guardrail against model-internal contradictions, not a general semantic classifier.
    """
    out: list[dict[str, str]] = []
    unit = r"(?:cells?|oocytes?|egg cells?|blastomeres?|neurons?|fibres?|fibers?|myofibres?|myofibers?|protoplasts?)"
    scientific = r"\d+(?:\s*[x×]\s*10(?:\^?\d+|\d+))"
    count = rf"(?:~|approximately |about |roughly )?(?:{scientific}|\d[\d,]*(?:\s*[-–]\s*\d[\d,]*)?|one million|[a-z]+(?:\s+to\s+[a-z]+)?)"
    per_sample = re.compile(
        rf"(?P<count>{count})\s+(?:isolated |sorted |microdissected |facs[- ]isolated )?(?:[a-z0-9-]+\s+){{0,3}}{unit}.{{0,320}}"
        r"(?:per|for each|in each|from each|proteins? from each)\s+(?:proteomic |ms |mass[- ]spectrometry )?(?:sample|replicate|measurement)",
        re.I,
    )
    into_one = re.compile(
        rf"(?P<count>{count})\s+(?:isolated |sorted |microdissected |facs[- ]isolated )?(?:[a-z0-9-]+\s+){{0,3}}{unit}.{{0,180}}"
        r"(?:were |was |are |is )?(?:transferred|pooled|combined|collected|placed|loaded).{0,120}"
        r"(?:into|to|in)\s+(?:a |one |single )?.{0,40}(?:droplet|tube|vial|well|sample|reaction|buffer|container)",
        re.I,
    )
    prior_context = re.compile(r"\b(previous|previously|prior|earlier|original approach|background|transcriptom|single-cell rna|scrna)\b", re.I)
    post_label_context = re.compile(r"\b(tmt|tmtpro|isobaric|barcode|barcoded|labelled|labeled|booster|carrier|plex)\b", re.I)

    for item in items:
        label = text(item.get("source_label")).lower()
        direct = item.get("source_kind") == "repository" or any(token in label for token in DIRECT_SAMPLE_LABELS)
        if not direct:
            continue
        raw = text(item.get("text"))
        if not raw:
            continue
        sentences = re.split(r"(?<=[.!?;])\s+", raw)
        windows = list(sentences) + [" ".join(sentences[i : i + 2]) for i in range(max(0, len(sentences) - 1))]
        for sentence in windows:
            if prior_context.search(sentence):
                continue
            if post_label_context.search(sentence) and re.search(r"\b(pool|pooled|combine|combined)\b", sentence, re.I):
                continue
            match = per_sample.search(sentence) or into_one.search(sentence)
            if match and _number_phrase_is_multiple(match.group("count")):
                out.append({"ref": text(item.get("ref")), "text": _clip(sentence, 500)})
                break
    return out



GENERIC_SAMPLE_UNIT_TOKENS = {
    "single", "individual", "one", "biological", "target", "sample", "samples",
    "cell", "cells", "proteomic", "proteomics", "protein", "ms", "mass", "spectrometry",
    "the", "a", "an", "of", "from", "and", "or", "like",
}


def _lex_tokens(value: Any) -> list[str]:
    return re.findall(r"[a-z0-9]+", text(value).lower())


def _token_stem(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _referenced_evidence_text(result: dict[str, Any], items: list[dict[str, str]], field: str) -> str:
    refs = result.get("evidence_refs") if isinstance(result.get("evidence_refs"), dict) else {}
    wanted = set(refs.get(field) or [])
    return " ".join(text(item.get("text")) for item in items if text(item.get("ref")) in wanted)


def validate_literal_value_grounding(result: dict[str, Any], items: list[dict[str, str]]) -> list[str]:
    """Reject obvious copied/hallucinated sample identities or counts.

    This is deliberately lexical and conservative. It does not infer biology; it only asks whether
    content-bearing words/numbers emitted by the model occur in the evidence it cited.
    """
    warnings: list[str] = []
    unit = text(result.get("biological_sample_unit"))
    if unit:
        evidence = _referenced_evidence_text(result, items, "biological_sample_unit")
        ev_tokens = {_token_stem(t) for t in _lex_tokens(evidence)}
        unit_tokens = [
            _token_stem(t) for t in _lex_tokens(unit)
            if t not in GENERIC_SAMPLE_UNIT_TOKENS and not t.isdigit()
        ]
        supported = sum(1 for token in unit_tokens if token in ev_tokens)
        needed = 1 if unit_tokens else 0
        if unit_tokens and supported < needed:
            result["biological_sample_unit"] = None
            warnings.append(
                "biological_sample_unit: removed because content words were not supported by the cited evidence"
            )

    cell_count = text(result.get("cells_per_target_ms_sample"))
    if cells_per_sample_is_multiple(cell_count):
        evidence = _referenced_evidence_text(result, items, "cells_per_target_ms_sample")
        # A multiple-cell assertion must itself be visible in the cited text, rather than being invented
        # from a study-wide sample count or prompt example.
        if not _number_phrase_is_multiple(evidence) and not re.search(r"\b(pool|pooled|multiple|several|many)\b", evidence, re.I):
            result["cells_per_target_ms_sample"] = None
            warnings.append(
                "cells_per_target_ms_sample: removed because the cited evidence did not contain a grounded multi-cell count"
            )
    return warnings


def deterministic_benchmark_only_evidence(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Find high-specificity direct evidence for benchmark-only / diluted-bulk MS designs.

    This intentionally requires benchmark language plus lysate/dilution/defined-ratio context,
    or explicit single-cell-equivalent wording. A generic method benchmark is not sufficient.
    """
    out: list[dict[str, str]] = []
    benchmark = re.compile(r"\b(?:benchmark(?:ing)?|technical benchmark|method benchmark|serve[sd]? as a benchmark)\b", re.I)
    noncell_input = re.compile(
        r"\b(?:lysates?|bulk digest|bulk proteom\w*|defined ratios?|dilut(?:e|ed|ion)|single[- ]cell equivalent|cell[- ]equivalent|proteome standard)\b",
        re.I,
    )
    explicit_equivalent = re.compile(r"\b(?:single[- ]cell|one[- ]cell)\s+equivalent\b", re.I)
    for item in items:
        raw = text(item.get("text"))
        if not raw:
            continue
        label = text(item.get("source_label")).lower()
        direct = item.get("source_kind") == "repository" or any(token in label for token in DIRECT_SAMPLE_LABELS)
        if not direct:
            continue
        if explicit_equivalent.search(raw) or (benchmark.search(raw) and noncell_input.search(raw)):
            out.append({"ref": text(item.get("ref")), "text": _clip(raw, 500)})
    return out



def deterministic_qualifying_branch_evidence(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Find high-specificity direct evidence for a real identity-preserved one-cell MS branch.

    This is intentionally stricter than deterministic_onecell_support(): generic phrases such
    as "single-cell proteomics", "single-cell resolution", or "single-cell-like amounts" do
    not qualify. The evidence must describe handling/processing of an actual single/individual
    biological cell, or subcellular sampling from one identified cell.
    """
    out: list[dict[str, str]] = []
    unit = r"(?:cells?|oocytes?|egg cells?|blastomeres?|neurons?|fibres?|fibers?|myofibres?|myofibers?|protoplasts?|sperm cells?|zygotes?|hepatocytes?|cardiomyocytes?|macrophages?|monocytes?|neutrophils?|lymphocytes?|astrocytes?|fibroblasts?|bacteria|microglia)"
    explicit_unit = re.compile(
        rf"\b(?:single|individual|one)[- ]+(?:[a-z0-9+./-]+\s+){{0,6}}{unit}\b"
        r"(?![- ]?(?:proteom|resolution|analysis|technology|like|equivalent|amount))",
        re.I,
    )
    handling = re.compile(
        r"\b(?:isolat(?:e|ed|ion)|sort(?:ed|ing)?|dispens(?:e|ed|ing)|pick(?:ed|ing)?|"
        r"collect(?:ed|ing)?|place(?:d|ing)?|transfer(?:red|ring)?|seed(?:ed|ing)?)\b",
        re.I,
    )
    container = re.compile(r"\b(?:well|nanowell|tube|vial|chip|proteochip|droplet|reaction|container)\b", re.I)
    proteomic = re.compile(
        r"\b(?:proteom\w*|protein\s+(?:digestion|extraction)|mass spectrom\w*|"
        r"lc[- ]?ms|ms/ms|maldi|cze[- ]?ms)\b",
        re.I,
    )
    individual_prep = re.compile(r"\bindividual\s+cell\s+(?:isolation|lysis|digestion|sample preparation)\b", re.I)
    direct_cell_processing = re.compile(
        r"\b(?:was|were)\s+(?:lysed|digested|denatured|processed|prepared)\b",
        re.I,
    )
    subcellular_from_one = re.compile(
        r"(?:"
        r"\bsubcellular\s+proteom\w*.{0,220}\b(?:single|individual|one)?\s*(?:[a-z0-9-]+\s+){0,4}cell\b"
        r"|\bcapillary\s+microsampling.{0,220}\b(?:identified|single|individual)\s+cells?\b"
        r"|\bcapillary\s+microsampling.{0,260}\bsingle[- ]cell\s+proteome\b"
        r")",
        re.I,
    )
    proteins_from_single = re.compile(
        r"\b(?:quantif(?:y|ied)|identif(?:y|ied)|detect(?:ed)?|measur(?:e|ed))\b.{0,140}"
        r"\b(?:proteins?|proteom\w*)\b.{0,120}\bfrom\s+single[- ]+[^.;]{0,120}"
        r"\b(?:cells?|bacterium|bacteria)\b",
        re.I,
    )
    one_in_count_series = re.compile(
        rf"\b(?:1|one)\s*(?:to|through|[-–])\s*\d+\s+(?:[a-z0-9+./-]+\s+){{0,4}}{unit}\b"
        r".{0,100}\b(?:per\s+(?:experiment|sample|replicate)|were\s+(?:analy[sz]ed|processed|measured))\b",
        re.I,
    )

    for item in items:
        label = text(item.get("source_label")).lower()
        direct = item.get("source_kind") == "repository" or any(token in label for token in DIRECT_SAMPLE_LABELS)
        if not direct:
            continue
        raw = text(item.get("text"))
        if not raw:
            continue
        if (
            individual_prep.search(raw)
            or subcellular_from_one.search(raw)
            or proteins_from_single.search(raw)
            or one_in_count_series.search(raw)
        ):
            out.append({"ref": text(item.get("ref")), "text": _clip(raw, 500)})
            continue
        sentences = re.split(r"(?<=[.!?;])\s+", raw)
        windows = list(sentences) + [" ".join(sentences[i:i+2]) for i in range(max(0, len(sentences)-1))]
        for window in windows:
            units = list(explicit_unit.finditer(window))
            handlers = list(handling.finditer(window))
            if units and direct_cell_processing.search(window):
                # High-specificity procedural branch such as "the single cells were lysed".
                # This deliberately requires a finite processing verb after the literal cell unit,
                # so noun phrases such as "single HeLa cell digest" remain nonqualifying.
                out.append({"ref": text(item.get("ref")), "text": _clip(window, 500)})
                break
            if (
                units
                and handlers
                and any(abs(u.start() - h.start()) <= 180 for u in units for h in handlers)
                and (container.search(window) or proteomic.search(window))
            ):
                out.append({"ref": text(item.get("ref")), "text": _clip(window, 500)})
                break
    return out


def deterministic_nonqualifying_input_evidence(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Find high-specificity direct evidence for non-one-cell target inputs.

    This covers diluted-bulk/single-cell-like inputs and near/almost-single-cell spatial
    measurements. Generic low-input controls and bulk spectral-library material are not enough.
    """
    out: list[dict[str, str]] = []
    single_cell_like = re.compile(
        r"\b(?:single[- ]cell|one[- ]cell)\s*[- ]?like\s+(?:amounts?|levels?|input)\b",
        re.I,
    )
    diluted_to_single = re.compile(
        r"\b(?:bulk\s+proteome\s+digests?|bulk\s+digests?|proteome\s+digests?).{0,220}"
        r"\bdilut(?:e|ed|ion).{0,160}\bsingle[- ]cell(?:\s+levels?)?\b",
        re.I,
    )
    commercial_digest = re.compile(
        r"\bcommercial\s+(?:[a-z0-9-]+\s+){0,4}(?:cell\s+)?digest\b",
        re.I,
    )
    digest_standard = re.compile(r"\b(?:protein|proteome|cell)\s+digest\s+standard\b", re.I)
    spatial_near = re.compile(r"\b(?:near|almost)[- ]single[- ]cell\s+(?:resolution|scale)\b", re.I)
    spatial_context = re.compile(r"\b(?:spatial|maldi|imaging|ims|lcm|laser capture|region|pixel|tissue)\b", re.I)

    for item in items:
        label = text(item.get("source_label")).lower()
        direct = item.get("source_kind") == "repository" or any(token in label for token in DIRECT_SAMPLE_LABELS)
        if not direct:
            continue
        raw = text(item.get("text"))
        if not raw:
            continue
        reason = ""
        if single_cell_like.search(raw):
            reason = "single_cell_like_input"
        elif diluted_to_single.search(raw):
            reason = "diluted_bulk_to_single_cell_level"
        elif (commercial_digest.search(raw) or digest_standard.search(raw)) and re.search(
            r"\b(?:pg|ng|injected|sample vial|single[- ]cell|low[- ]input)\b", raw, re.I
        ):
            reason = "commercial_or_standard_digest_input"
        elif spatial_near.search(raw) and spatial_context.search(raw):
            reason = "near_single_cell_spatial_resolution"
        if reason:
            out.append({"ref": text(item.get("ref")), "reason": reason, "text": _clip(raw, 500)})
    return out


CELL_LIKE_UNIT_NOUN = (
    r"(?:cells?|oocytes?|egg(?:\s+cells?)?|blastomeres?|neurons?|"
    r"fibres?|fibers?|myofibres?|myofibers?|sperm(?:\s+cells?)?|"
    r"protoplasts?|zygotes?|gametes?|hepatocytes?|cardiomyocytes?|"
    r"macrophages?|monocytes?|neutrophils?|lymphocytes?|astrocytes?|"
    r"fibroblasts?|bacterium|bacteria|microglia)"
)
SPECIFIC_CELL_LIKE_UNIT_NOUN = (
    r"(?:oocytes?|egg(?:\s+cells?)?|blastomeres?|neurons?|fibres?|fibers?|"
    r"myofibres?|myofibers?|sperm(?:\s+cells?)?|protoplasts?|zygotes?|"
    r"gametes?|hepatocytes?|cardiomyocytes?|macrophages?|monocytes?|"
    r"neutrophils?|lymphocytes?|astrocytes?|fibroblasts?|bacterium|bacteria|microglia)"
)
SAMPLE_UNIT_FALSE_FRIEND_RE = re.compile(
    r"\b(?:proteom\w*|experiment\w*|analysis|resolution|workflow\w*|amounts?|"
    r"pool(?:ed|s|ing)?|technology|measurement\w*|benchmark\w*|disaggregat\w*)\b",
    re.I,
)
SAMPLE_UNIT_BAD_MODIFIERS = {
    "or", "and", "small", "pooled", "pool", "multiple", "several", "many",
    "trace", "low-input", "low", "input", "depending", "including", "from",
    "each", "per", "of", "sample", "samples", "experiment", "experiments",
    "proteomic", "proteomics", "study", "exploring", "how", "option", "universal",
    "approach", "method", "workflow", "protocol", "using", "with", "only",
}


def sample_unit_has_cell_noun(value: Any) -> bool:
    """Return whether a literal biological sample-unit string names a cell-like unit, not only a taxon.

    The lexicon includes common cell-type nouns that do not literally contain the token
    ``cell`` (for example hepatocyte, cardiomyocyte, macrophage and neutrophil).  This is
    a lexical type check only; accession inclusion still requires the independent one-cell/MS,
    identity-preservation and pooling evidence gates.
    """
    return bool(re.search(rf"\b{CELL_LIKE_UNIT_NOUN}\b", text(value), re.I))


def deterministic_sample_unit_evidence(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Extract literal one-cell biological sample-unit phrases from direct evidence.

    This deliberately does not treat method phrases such as ``single-cell proteomics`` or
    ``single-cell resolution`` as a biological sample unit.  Specific units (for example
    ``single HeLa cells``, ``individual neutrophils`` or ``single hepatocyte``) may come from
    repository or publication sample/preparation evidence.  Generic ``single cell(s)`` is
    retained only from publication Methods evidence and requires repeated support before it
    can be used for normalization.
    """
    specific_re = re.compile(
        rf"\b(?:single|individual|one)[- ]+(?:[A-Za-z0-9+./\-/]+\s+){{0,6}}"
        rf"{SPECIFIC_CELL_LIKE_UNIT_NOUN}\b",
        re.I,
    )
    named_cell_re = re.compile(
        r"\b(?:single|individual|one)\s+"
        r"(?P<modifiers>(?:[A-Za-z0-9+\-/]+\s+){1,4})cells?\b",
        re.I,
    )
    generic_re = re.compile(r"\b(?:single|individual|one)[- ]+cells?\b", re.I)
    procedural_generic_re = re.compile(
        r"(?:"
        r"\bsingle[- ]cell\s+(?:isolation|sorting|seeding|deposition|dispensing|collection|lysis|digestion|sample(?:s)?|sample preparation)\b"
        r"|\b(?:single|individual|one)[- ]+cells?\b.{0,160}\b(?:isolat(?:e|ed|ion)|sort(?:ed|ing)?|dispens(?:e|ed|ing)|deposit(?:ed|ion)|collect(?:ed|ing)?|seed(?:ed|ing)?|lyse[ds]?|digested|processed|prepared)\b"
        r"|\b(?:cells?|neurons?|oocytes?|blastomeres?|hepatocytes?|cardiomyocytes?|astrocytes?|bacteria)\b.{0,180}\b(?:sorted|isolated|collected|deposited|dispensed)\b.{0,100}\bsingle[- ]cell\b"
        r")",
        re.I,
    )
    count_one_named_re = re.compile(
        r"\b(?:0\s*[,/]\s*)?1\s*(?:,|and|/)\s*\d+(?:\s*[,/]\s*\d+)*\s+"
        r"(?P<name>[A-Za-z0-9][A-Za-z0-9+./-]*(?:\s+[A-Za-z0-9][A-Za-z0-9+./-]*){0,3})\s+cells?\b",
        re.I,
    )

    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        raw = text(item.get("text"))
        if not raw:
            continue
        label = text(item.get("source_label")).lower()
        is_repository = item.get("source_kind") == "repository"
        is_publication_direct = any(token in label for token in DIRECT_SAMPLE_LABELS)
        if not (is_repository or is_publication_direct):
            continue
        is_methods = ":methods" in label
        ref = text(item.get("ref"))

        candidates: list[tuple[str, str]] = []
        for match in specific_re.finditer(raw):
            phrase = re.sub(r"\s+", " ", match.group(0)).strip(" ,.;:()")
            tokens = {token.lower().strip(".,;:()") for token in phrase.split()}
            tail = raw[match.end():match.end() + 40]
            if (
                not SAMPLE_UNIT_FALSE_FRIEND_RE.search(phrase)
                and not (tokens & SAMPLE_UNIT_BAD_MODIFIERS)
                and not re.match(r"[- ]?(?:proteom|resolution|analysis|technology|like|equivalent|amount)", tail, re.I)
            ):
                candidates.append(("specific", phrase))

        for match in named_cell_re.finditer(raw):
            phrase = re.sub(r"\s+", " ", match.group(0)).strip(" ,.;:()")
            modifiers = {token.lower() for token in match.group("modifiers").split()}
            if SAMPLE_UNIT_FALSE_FRIEND_RE.search(phrase) or modifiers & SAMPLE_UNIT_BAD_MODIFIERS:
                continue
            candidates.append(("specific", phrase))

        # Contextual cell-line/type extraction is allowed only when the same direct evidence
        # explicitly links those cells to single-cell handling, or when a count series contains
        # an actual one-cell branch (for example 0/1/5 U87 cells).
        for match in count_one_named_re.finditer(raw):
            name = re.sub(r"\s+", " ", match.group("name")).strip(" ,.;:()")
            tokens = {tok.lower() for tok in name.split()}
            if tokens and not (tokens & SAMPLE_UNIT_BAD_MODIFIERS):
                candidates.append(("specific", f"{name} cell"))

        if procedural_generic_re.search(raw):
            candidates.append(("procedural_generic", "single cell"))

        # Generic cell wording is much easier to encounter in method-development/benchmark prose.
        # Keep it from direct publication Methods evidence, or from repository text only when the
        # literal cells are immediately described as physically processed (for example "single
        # cells were lysed"). The normalizer still requires repeated support unless the same ref
        # is independently recognized as a high-specificity qualifying branch.
        if is_publication_direct and is_methods:
            for match in generic_re.finditer(raw):
                tail = raw[match.end():match.end() + 40]
                if re.match(r"[- ]?(?:proteom|resolution|analysis|technology|like|equivalent|amount)", tail, re.I):
                    continue
                candidates.append(("generic", re.sub(r"\s+", " ", match.group(0))))
        elif is_repository and re.search(
            r"\b(?:single|individual|one)\s+cells?\s+(?:was|were)\s+"
            r"(?:lysed|digested|denatured|processed|prepared)\b",
            raw,
            re.I,
        ):
            for match in generic_re.finditer(raw):
                candidates.append(("generic", re.sub(r"\s+", " ", match.group(0))))

        for specificity, phrase in candidates:
            key = (ref, phrase.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "ref": ref,
                    "sample_unit": phrase,
                    "specificity": specificity,
                    "source_label": text(item.get("source_label")),
                    "text": _clip(raw, 500),
                }
            )
    return out


def normalize_biological_sample_unit(
    result: dict[str, Any],
    sample_unit_signals: list[dict[str, str]],
    onecell_support: list[dict[str, str]] | None = None,
    risk_signals: list[dict[str, str]] | None = None,
    ambiguity_signals: list[dict[str, str]] | None = None,
    nonqualifying_signals: list[dict[str, str]] | None = None,
    qualifying_branch_signals: list[dict[str, str]] | None = None,
) -> list[str]:
    """Replace a missing/taxonomic model unit only when direct packet evidence supplies one.

    This is intentionally one-way and conservative: an already cell-like model unit is kept;
    direct ambiguity always blocks normalization, while multi-cell/nonqualifying evidence blocks
    normalization unless a separate high-specificity qualifying one-cell branch is also present.
    Generic ``single cell`` wording needs two direct Methods refs plus existing deterministic
    one-cell support. The selected evidence ref is added to the field provenance.
    """
    warnings: list[str] = []
    current = text(result.get("biological_sample_unit"))
    if current and sample_unit_has_cell_noun(current):
        return warnings
    qualifying_branch_signals = qualifying_branch_signals or []
    if ambiguity_signals:
        return warnings
    if (risk_signals or nonqualifying_signals) and not qualifying_branch_signals:
        return warnings

    onecell_support = onecell_support or []
    specific = [signal for signal in sample_unit_signals if signal.get("specificity") == "specific"]
    procedural_generic = [signal for signal in sample_unit_signals if signal.get("specificity") == "procedural_generic"]
    generic = [signal for signal in sample_unit_signals if signal.get("specificity") == "generic"]

    chosen: dict[str, str] | None = None
    if specific:
        # Prefer publication sample/preparation evidence over repository metadata, then the
        # more descriptive literal phrase.  No biological ontology inference is performed.
        specific.sort(
            key=lambda signal: (
                1 if any(token in text(signal.get("source_label")).lower() for token in DIRECT_SAMPLE_LABELS) else 0,
                len(text(signal.get("sample_unit"))),
            ),
            reverse=True,
        )
        chosen = specific[0]
    elif procedural_generic and (qualifying_branch_signals or len(onecell_support) >= 2):
        # A literal procedural one-cell handling statement is strong enough to ground the
        # biological unit as one cell even when the exact cell subtype is not named.
        procedural_generic.sort(
            key=lambda signal: (
                1 if any(token in text(signal.get("source_label")).lower() for token in DIRECT_SAMPLE_LABELS) else 0,
                len(text(signal.get("text"))),
            ),
            reverse=True,
        )
        chosen = procedural_generic[0]
    elif qualifying_branch_signals and generic:
        qualifying_refs = {text(signal.get("ref")) for signal in qualifying_branch_signals}
        branch_generic = [signal for signal in generic if text(signal.get("ref")) in qualifying_refs]
        if branch_generic:
            chosen = branch_generic[0]
    elif len({text(signal.get("ref")) for signal in generic}) >= 2 and len(onecell_support) >= 2:
        chosen = generic[0]

    if chosen is None:
        return warnings

    old = current or "<missing>"
    result["biological_sample_unit"] = text(chosen.get("sample_unit"))
    refs = result.setdefault("evidence_refs", {})
    refs.setdefault("biological_sample_unit", [])
    ref = text(chosen.get("ref"))
    if ref and ref not in refs["biological_sample_unit"]:
        refs["biological_sample_unit"].append(ref)
    warnings.append(
        "biological_sample_unit: normalized from "
        f"{old!r} to {result['biological_sample_unit']!r} using direct evidence {ref}"
    )
    return warnings


def deterministic_onecell_support(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Find accession-specific evidence that a target proteomics/MS branch is genuinely one-cell."""
    out: list[dict[str, str]] = []
    unit = r"(?:cells?|oocytes?|egg cells?|blastomeres?|neurons?|fibres?|fibers?|myofibres?|myofibers?|protoplasts?|sperm cells?|zygotes?|hepatocytes?|cardiomyocytes?|macrophages?|monocytes?|neutrophils?|lymphocytes?|astrocytes?|fibroblasts?|bacteria|microglia)"
    explicit_unit = re.compile(
        rf"\b(?:single|individual|one)[- ]+(?:[a-z0-9+./-]+\s+){{0,6}}{unit}\b"
        r"(?![- ]?(?:proteom|resolution|analysis|technology|like|equivalent|amount))",
        re.I,
    )
    # Generic title-only wording such as ``single-cell proteomics`` is useful recall evidence but
    # is not a literal biological sample unit.  A *specific* named biological unit is different:
    # ``single-oocyte proteomics`` or ``single-neuron proteomics`` directly identifies one
    # biological cell type.  Keep those phrases as one-cell support while retaining the generic
    # anti-title safeguard above.
    specific_named_unit = (
        r"(?:oocytes?|egg cells?|blastomeres?|neurons?|fibres?|fibers?|myofibres?|myofibers?|"
        r"protoplasts?|sperm cells?|zygotes?|hepatocytes?|cardiomyocytes?|macrophages?|monocytes?|"
        r"neutrophils?|lymphocytes?|astrocytes?|fibroblasts?|bacterium|bacteria|microglia)"
    )
    explicit_specific_unit = re.compile(
        rf"\b(?:single|individual|one)[- ]+(?:[a-z0-9+./-]+\s+){{0,6}}{specific_named_unit}\b",
        re.I,
    )
    proteomic = re.compile(r"\b(?:proteom\w*|mass spectrom\w*|lc[- ]?ms|ms/ms|maldi|nanopots|sample prep\w*|protein extraction|digestion)\b", re.I)
    repo_single_cell = re.compile(r"\bsingle[- ]cell\s+(?:proteom\w*|mass[- ]spectrom\w*|ms\b)", re.I)
    prior_context = re.compile(r"\b(previous|previously|prior|earlier|background|review|single-cell rna|scrna|transcriptom)\b", re.I)

    for item in items:
        raw = text(item.get("text"))
        if not raw:
            continue
        label = text(item.get("source_label")).lower()
        is_repo = item.get("source_kind") == "repository"
        is_direct_pub = any(token in label for token in DIRECT_SAMPLE_LABELS)
        if is_repo and (repo_single_cell.search(raw) or ((explicit_unit.search(raw) or explicit_specific_unit.search(raw)) and proteomic.search(raw))):
            out.append({"ref": text(item.get("ref")), "text": _clip(raw, 500)})
            continue
        if not is_direct_pub:
            continue
        sentences = re.split(r"(?<=[.!?;])\s+", raw)
        windows = list(sentences) + [" ".join(sentences[i:i+2]) for i in range(max(0, len(sentences)-1))]
        for window in windows:
            if prior_context.search(window):
                continue
            if (explicit_unit.search(window) or explicit_specific_unit.search(window)) and proteomic.search(window):
                out.append({"ref": text(item.get("ref")), "text": _clip(window, 500)})
                break
    return out


def deterministic_sample_linkage_ambiguity(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Flag direct method wording where the MS sample is a cell-enriched aggregate rather than clearly one cell.

    This is review-only evidence, never exclusion-authoritative.
    """
    out: list[dict[str, str]] = []
    pattern = re.compile(
        r"\b(?:each|the|a)\s+(?:proteomic\s+|ms\s+)?sample\s+(?:was\s+|is\s+)?enriched\s+in\s+[^.;]{0,100}"
        r"(?:cells?|neurons?|oocytes?|blastomeres?|fibres?|fibers?|protoplasts?|structures?)\b",
        re.I,
    )
    for item in items:
        label = text(item.get("source_label")).lower()
        if item.get("source_kind") != "repository" and not any(token in label for token in DIRECT_SAMPLE_LABELS):
            continue
        raw = text(item.get("text"))
        match = pattern.search(raw)
        if match:
            out.append({"ref": text(item.get("ref")), "text": _clip(match.group(0), 500)})
    return out


def normalize_fact_consistency(
    result: dict[str, Any],
    risk_signals: list[dict[str, str]],
    qualifying_branch_signals: list[dict[str, str]] | None = None,
    nonqualifying_signals: list[dict[str, str]] | None = None,
) -> list[str]:
    """Apply deterministic cross-field consistency constraints without using GT labels."""
    warnings: list[str] = []
    qualifying_branch_signals = qualifying_branch_signals or []
    nonqualifying_signals = nonqualifying_signals or []
    has_separate_qualifying_branch = bool(qualifying_branch_signals) and bool(risk_signals or nonqualifying_signals)
    unit_class = text(result.get("biological_unit_class")).lower()
    mixed = text(result.get("mixed_design")).lower()
    count_multiple = cells_per_sample_is_multiple(result.get("cells_per_target_ms_sample"))
    pooling_stage = text(result.get("pooling_stage")).lower()

    if has_separate_qualifying_branch:
        result["mixed_design"] = "yes"
        mixed = "yes"
        warnings.append(
            "consistency: direct one-cell MS branch coexists with a separate nonqualifying branch; normalized mixed_design=yes"
        )

    if pooling_stage == "before_identity_preserving_processing":
        if risk_signals and not has_separate_qualifying_branch:
            if result.get("destructive_pooling_before_ms") != "yes":
                result["destructive_pooling_before_ms"] = "yes"
                warnings.append("consistency: direct pre-identity pooling evidence implies destructive_pooling_before_ms=yes")
            if mixed != "yes":
                result["true_single_cell_ms_samples_present"] = "no"
                result["individual_identity_preserved_to_ms"] = "no"
                warnings.append("consistency: direct pre-identity pooling evidence blocks a qualifying one-cell branch")
        else:
            if result.get("destructive_pooling_before_ms") == "no":
                result["pooling_stage"] = "none"
                warnings.append(
                    "consistency: pooling_stage=before_identity_preserving_processing contradicted destructive_pooling_before_ms=no and lacked exclusion-authoritative direct corroboration; normalized to none"
                )
            elif not has_separate_qualifying_branch:
                warnings.append("consistency: model-only pre-identity pooling assertion lacks deterministic multi-cell corroboration; review required")

    if pooling_stage == "after_identity_preserving_labeling" and result.get("destructive_pooling_before_ms") == "yes":
        result["destructive_pooling_before_ms"] = "uncertain"
        warnings.append("consistency: post-label pooling conflicts with destructive_pooling_before_ms=yes; downgraded")

    if mixed != "yes" and unit_class in NONQUALIFYING_UNIT_CLASSES:
        warnings.append(
            f"consistency: biological_unit_class={unit_class} conflicts with a possible one-cell branch; class alone cannot exclude"
        )

    if mixed != "yes" and count_multiple:
        result["true_single_cell_ms_samples_present"] = "no"
        result["individual_identity_preserved_to_ms"] = "no"
        warnings.append("consistency: cells_per_target_ms_sample explicitly indicates multiple cells")

    if mixed != "yes" and risk_signals and not has_separate_qualifying_branch:
        result["true_single_cell_ms_samples_present"] = "no"
        result["individual_identity_preserved_to_ms"] = "no"
        result["destructive_pooling_before_ms"] = "yes"
        if pooling_stage in {"", "none", "uncertain"}:
            result["pooling_stage"] = "before_identity_preserving_processing"
        warnings.append("consistency: direct multi-cell-to-one-MS-sample evidence overrides contradictory one-cell assertions")

    return warnings


def validate_evidence_refs(result: dict[str, Any], items: list[dict[str, str]]) -> list[str]:
    valid_refs = {item["ref"] for item in items}
    warnings: list[str] = []
    refs = result.get("evidence_refs")
    if not isinstance(refs, dict):
        result["evidence_refs"] = {field: [] for field in FACT_FIELDS}
        return ["evidence_refs missing or invalid"]

    for field in FACT_FIELDS:
        values = refs.get(field)
        if not isinstance(values, list):
            values = []
        cleaned: list[str] = []
        for value in values:
            ref = text(value)
            if ref in valid_refs and ref not in cleaned:
                cleaned.append(ref)
            elif ref:
                warnings.append(f"{field}: removed unknown evidence ref {ref}")
        refs[field] = cleaned[:6]

    # Non-uncertain categorical claims must have grounded references.  Rather
    # than fabricating support, downgrade unsupported claims to uncertain.
    categorical = [
        "true_single_cell_ms_samples_present",
        "individual_identity_preserved_to_ms",
        "destructive_pooling_before_ms",
        "pooling_stage",
        "benchmark_only",
        "adjacent_single_cell_only",
        "reanalysis_only",
        "mixed_design",
    ]
    for field in categorical:
        value = text(result.get(field)).lower()
        if value in {"yes", "no"} and not refs.get(field):
            result[field] = "uncertain"
            warnings.append(f"{field}: downgraded to uncertain because no valid evidence_refs were supplied")

    if text(result.get("pooling_stage")) not in POOLING_STAGES:
        result["pooling_stage"] = "uncertain"
        warnings.append("pooling_stage: invalid value normalized to uncertain")
    if result.get("pooling_stage") != "uncertain" and not refs.get("pooling_stage"):
        result["pooling_stage"] = "uncertain"
        warnings.append("pooling_stage: downgraded to uncertain because no valid evidence_refs were supplied")

    if text(result.get("biological_unit_class")) not in UNIT_CLASSES:
        result["biological_unit_class"] = "uncertain"
        warnings.append("biological_unit_class: invalid value normalized to uncertain")
    if result.get("biological_unit_class") != "uncertain" and not refs.get("biological_unit_class"):
        result["biological_unit_class"] = "uncertain"
        warnings.append("biological_unit_class: downgraded to uncertain because no valid evidence_refs were supplied")

    if text(result.get("biological_sample_unit")) and not refs.get("biological_sample_unit"):
        result["biological_sample_unit"] = None
        warnings.append("biological_sample_unit: removed because no valid evidence_refs were supplied")
    if text(result.get("cells_per_target_ms_sample")) and not refs.get("cells_per_target_ms_sample"):
        result["cells_per_target_ms_sample"] = None
        warnings.append("cells_per_target_ms_sample: removed because no valid evidence_refs were supplied")

    return warnings


def deterministic_decision(
    result: dict[str, Any],
    risk_signals: list[dict[str, str]] | None = None,
    onecell_support: list[dict[str, str]] | None = None,
    ambiguity_signals: list[dict[str, str]] | None = None,
    benchmark_signals: list[dict[str, str]] | None = None,
    qualifying_branch_signals: list[dict[str, str]] | None = None,
    nonqualifying_signals: list[dict[str, str]] | None = None,
) -> tuple[str, str]:
    true_sc = text(result.get("true_single_cell_ms_samples_present")).lower()
    identity = text(result.get("individual_identity_preserved_to_ms")).lower()
    pooling = text(result.get("destructive_pooling_before_ms")).lower()
    pooling_stage = text(result.get("pooling_stage")).lower()
    benchmark = text(result.get("benchmark_only")).lower()
    adjacent = text(result.get("adjacent_single_cell_only")).lower()
    reanalysis = text(result.get("reanalysis_only")).lower()
    mixed = text(result.get("mixed_design")).lower()
    unit_class = text(result.get("biological_unit_class")).lower()
    sample_unit = text(result.get("biological_sample_unit"))
    count_multiple = cells_per_sample_is_multiple(result.get("cells_per_target_ms_sample"))
    risk_signals = risk_signals or []
    onecell_support = onecell_support or []
    ambiguity_signals = ambiguity_signals or []
    benchmark_signals = benchmark_signals or []
    qualifying_branch_signals = qualifying_branch_signals or []
    nonqualifying_signals = nonqualifying_signals or []

    # Branch-aware mixed-design rule: a separate high-specificity one-cell MS branch keeps
    # an accession positive even if another branch is bulk/multi-cell or a diluted/benchmark
    # input. Direct ambiguity still prevents using this shortcut.
    if qualifying_branch_signals and (risk_signals or nonqualifying_signals) and not ambiguity_signals:
        return "include", "direct evidence establishes a qualifying one-cell MS branch alongside a separate nonqualifying branch"

    if mixed != "yes" and risk_signals:
        return "exclude", "direct evidence shows multiple biological cells combined into one target MS sample before identity preservation"
    if mixed != "yes" and count_multiple:
        return "exclude", "cells_per_target_ms_sample explicitly indicates more than one biological cell"

    # High-specificity nonqualifying inputs are exclusion-authoritative unless a real one-cell
    # branch is directly demonstrated in the same accession.
    if nonqualifying_signals and not qualifying_branch_signals:
        reasons = {text(x.get("reason")) for x in nonqualifying_signals}
        if "near_single_cell_spatial_resolution" in reasons:
            return "exclude", "direct evidence supports near/almost-single-cell spatial resolution rather than one biological cell per target MS sample"
        return "exclude", "direct evidence supports diluted-bulk/single-cell-like input without a qualifying one-cell MS branch"

    # The older benchmark detector remains conservative because some true studies contain
    # explicit benchmark controls alongside real one-cell measurements.
    if benchmark_signals and not onecell_support:
        return "exclude", "direct evidence supports benchmark-only or diluted-bulk MS design without a qualifying one-cell branch"

    # A high-specificity procedural one-cell branch is stronger than an internally conflicting
    # model-only benchmark/pooling/identity assertion. Direct negative evidence above still wins.
    if (
        qualifying_branch_signals
        and not risk_signals
        and not nonqualifying_signals
        and not ambiguity_signals
        and sample_unit_has_cell_noun(sample_unit)
    ):
        return "include", "direct procedural evidence establishes a qualifying one-cell MS branch; model-only negative/conflict fields are non-authoritative"

    if true_sc == "no":
        if benchmark == "yes":
            return "review", "model reports benchmark-only design but deterministic benchmark-only evidence did not establish the exclusion"
        if adjacent == "yes":
            return "exclude", "single-cell component is adjacent to, not itself, qualifying MS proteomics"
        if reanalysis == "yes":
            return "exclude", "accession is reanalysis/processed-only without qualifying independent source MS data"
        return "review", "model reports no qualifying one-cell branch but no exclusion-authoritative direct evidence corroborates that assertion"

    if pooling_stage == "before_identity_preserving_processing" and not risk_signals:
        return "review", "model reports pre-identity pooling without corroborating direct multi-cell-to-one-MS evidence"

    if true_sc == "yes" and identity == "yes" and pooling == "no":
        if not sample_unit:
            return "review", "positive one-cell booleans lack a literally grounded biological sample unit"
        if not sample_unit_has_cell_noun(sample_unit):
            return "review", "biological_sample_unit is grounded text but does not identify a cell-like biological unit"
        if unit_class not in QUALIFYING_UNIT_CLASSES and len(onecell_support) < 2:
            return "review", "one-cell booleans are positive but the biological-unit class is contradictory or not explicitly qualifying"
        if benchmark == "yes":
            return "review", "qualifying one-cell facts conflict with a model-only benchmark_only flag"
        if adjacent == "yes" or reanalysis == "yes":
            return "review", "qualifying one-cell facts conflict with an accession-level *_only flag"
        if ambiguity_signals:
            return "review", "direct methods describe a cell-enriched proteomic sample without proving one biological cell per target MS sample"
        if not onecell_support and not qualifying_branch_signals:
            return "review", "model reports a qualifying one-cell branch but no high-specificity accession-specific one-cell/MS linkage was recovered"
        return "include", "grounded qualifying one-cell MS branch with identity preserved and no destructive pre-MS pooling"

    if pooling == "yes" or identity == "no":
        return "review", "model reports destructive pooling or identity loss without exclusion-authoritative direct evidence"

    return "review", "insufficient or internally incomplete evidence for deterministic inclusion/exclusion"


def curation_cache_key(packet: dict[str, Any], model: str) -> str:
    payload = {"pipeline_version": PIPELINE_VERSION, "model": model, "packet": packet}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def cache_is_compatible(cached: dict[str, Any], expected_key: str, model: str) -> bool:
    return (
        cached.get("curation_pipeline_version") == PIPELINE_VERSION
        and cached.get("model") == model
        and cached.get("curation_cache_key") == expected_key
        and isinstance(cached.get("summary"), dict)
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="version", version=PIPELINE_VERSION)
    parser.add_argument("candidates_jsonl")
    parser.add_argument("--annotations-dir", default="work/python/pride_scp_annotations/annotations")
    parser.add_argument("--output-dir", default="work/python/curation_v19")
    parser.add_argument("--model", default="qwen2.5:3b")
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--num-ctx", type=int, default=8192)
    parser.add_argument("--num-predict", type=int, default=900)
    parser.add_argument("--keep-alive", default="5m")
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--max-evidence-items", type=int, default=18)
    parser.add_argument("--evidence-chars", type=int, default=14000)
    parser.add_argument("--accession", action="append", default=[])
    parser.add_argument("--accession-file", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--packets-only", action="store_true")
    return parser.parse_args()


def load_selected_accessions(args: argparse.Namespace) -> set[str]:
    selected = {text(x).upper() for x in args.accession if text(x)}
    if args.accession_file:
        for line in Path(args.accession_file).read_text(encoding="utf-8").splitlines():
            value = line.strip().upper()
            if ACCESSION_RE.fullmatch(value):
                selected.add(value)
    return selected


def main() -> None:
    args = parse_args()
    candidates = load_jsonl(Path(args.candidates_jsonl))
    annotations = load_annotations(Path(args.annotations_dir))
    selected = load_selected_accessions(args)
    if selected:
        candidates = [row for row in candidates if text(row.get("accession")).upper() in selected]
    if args.limit > 0:
        candidates = candidates[: args.limit]

    output_dir = Path(args.output_dir)
    status_dir = output_dir / "status"
    packets_dir = output_dir / "packets"
    output_dir.mkdir(parents=True, exist_ok=True)
    status_dir.mkdir(parents=True, exist_ok=True)
    packets_dir.mkdir(parents=True, exist_ok=True)

    summary_rows: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        accession = text(candidate.get("accession")).upper()
        if not ACCESSION_RE.fullmatch(accession):
            continue
        docs, identity_metadata = _candidate_annotations(annotations, candidate)
        items = build_evidence_items(
            candidate,
            docs,
            max_items=args.max_evidence_items,
            max_chars=args.evidence_chars,
        )
        deterministic_items, deterministic_evidence_diagnostics = build_deterministic_evidence_items(
            candidate,
            docs,
            items,
        )
        deterministic_evidence_sha256 = hashlib.sha256(
            json.dumps(deterministic_items, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        packet = {
            "accession": accession,
            "candidate_origin": {
                "tier": candidate.get("tier", candidate.get("discovery_tier", "")),
                "score": candidate.get("score", ""),
                "project_json_path": candidate.get("project_json_path", ""),
                "files_json_path": candidate.get("files_json_path", ""),
                "sdrf_path": candidate.get("sdrf_path", ""),
                "repository": candidate.get("repository", ""),
                "canonical_dataset_identity": identity_metadata.get("canonical_dataset_identity", ""),
                "trusted_pxd_aliases": identity_metadata.get("trusted_pxd_aliases", []),
                "native_evidence_path": candidate.get("native_evidence_path", ""),
            },
            "publication_annotation_count": len(docs),
            "evidence_items": items,
            "deterministic_evidence_sha256": deterministic_evidence_sha256,
            "deterministic_evidence_item_count": len(deterministic_items),
        }
        packet_path = packets_dir / f"{accession}.json"
        packet_path.write_text(json.dumps(packet, indent=2, ensure_ascii=False), encoding="utf-8")
        cache_key = curation_cache_key(packet, args.model)

        status_path = status_dir / f"{accession}.json"
        if args.packets_only:
            row = {
                "accession": accession,
                "run_status": "packet_only",
                "curation_decision": "",
                "decision_reason": "",
                "evidence_item_count": len(items),
                "deterministic_evidence_item_count": len(deterministic_items),
                "deterministic_extra_item_count": deterministic_evidence_diagnostics.get("deterministic_extra_item_count", 0),
                "publication_explicit_mismatch_count": deterministic_evidence_diagnostics.get("publication_explicit_mismatch_count", 0),
                "publication_annotation_count": len(docs),
                "model": args.model,
                "result_path": str(status_path.resolve()),
            }
            summary_rows.append(row)
            print(f"[{index}/{len(candidates)}] {accession} -> packet_only evidence={len(items)}")
            continue

        if status_path.is_file() and not args.force:
            cached = json.loads(status_path.read_text(encoding="utf-8"))
            if cache_is_compatible(cached, cache_key, args.model):
                summary_rows.append(cached.get("summary", {}))
                print(f"[{index}/{len(candidates)}] {accession} -> cached/{cached.get('curation_decision','')}")
                continue
            print(
                f"[{index}/{len(candidates)}] {accession} -> stale_cache "
                f"stored_version={cached.get('curation_pipeline_version','<missing>')} expected={PIPELINE_VERSION}; recomputing"
            )

        try:
            if not items:
                result = {
                    "biological_sample_unit": None,
                    "biological_unit_class": "uncertain",
                    "true_single_cell_ms_samples_present": "uncertain",
                    "cells_per_target_ms_sample": None,
                    "individual_identity_preserved_to_ms": "uncertain",
                    "destructive_pooling_before_ms": "uncertain",
                    "pooling_stage": "uncertain",
                    "benchmark_only": "uncertain",
                    "adjacent_single_cell_only": "uncertain",
                    "reanalysis_only": "uncertain",
                    "mixed_design": "uncertain",
                    "evidence_sufficiency": "insufficient",
                    "evidence_refs": {field: [] for field in FACT_FIELDS},
                    "reason": "No accession-specific repository or publication evidence was available in the packet.",
                }
                stats = {"wall_seconds": 0.0, "prompt_tokens": 0, "output_tokens": 0}
            else:
                result, stats = call_ollama(accession, items, args)
            warnings = validate_evidence_refs(result, items)
            warnings.extend(validate_literal_value_grounding(result, items))
            risk_signals = deterministic_multicell_evidence(deterministic_items)
            benchmark_signals = deterministic_benchmark_only_evidence(deterministic_items)
            onecell_support = deterministic_onecell_support(deterministic_items)
            qualifying_branch_signals = deterministic_qualifying_branch_evidence(deterministic_items)
            nonqualifying_signals = deterministic_nonqualifying_input_evidence(deterministic_items)
            ambiguity_signals = deterministic_sample_linkage_ambiguity(deterministic_items)
            warnings.extend(normalize_fact_consistency(result, risk_signals, qualifying_branch_signals, nonqualifying_signals))
            sample_unit_signals = deterministic_sample_unit_evidence(deterministic_items)
            warnings.extend(
                normalize_biological_sample_unit(
                    result,
                    sample_unit_signals,
                    onecell_support,
                    risk_signals,
                    ambiguity_signals,
                    nonqualifying_signals,
                    qualifying_branch_signals,
                )
            )
            decision, decision_reason = deterministic_decision(
                result,
                risk_signals,
                onecell_support,
                ambiguity_signals,
                benchmark_signals,
                qualifying_branch_signals,
                nonqualifying_signals,
            )
            payload = {
                "accession": accession,
                "curation_pipeline_version": PIPELINE_VERSION,
                "curation_cache_key": cache_key,
                "model": args.model,
                "curation_decision": decision,
                "decision_reason": decision_reason,
                "facts": result,
                "validation_warnings": warnings,
                "deterministic_multicell_risk_signals": risk_signals,
                "deterministic_onecell_support_signals": onecell_support,
                "deterministic_sample_linkage_ambiguity_signals": ambiguity_signals,
                "deterministic_benchmark_only_signals": benchmark_signals,
                "deterministic_qualifying_branch_signals": qualifying_branch_signals,
                "deterministic_nonqualifying_signals": nonqualifying_signals,
                "deterministic_sample_unit_signals": sample_unit_signals,
                "deterministic_evidence_diagnostics": deterministic_evidence_diagnostics,
                "deterministic_evidence_sha256": deterministic_evidence_sha256,
                "evidence_packet": str(packet_path.resolve()),
                "inference_stats": stats,
            }
            summary = {
                "accession": accession,
                "run_status": "success",
                "curation_decision": decision,
                "decision_reason": decision_reason,
                **{field: result.get(field, "") for field in FACT_FIELDS},
                "evidence_sufficiency": result.get("evidence_sufficiency", ""),
                "evidence_item_count": len(items),
                "deterministic_evidence_item_count": len(deterministic_items),
                "deterministic_extra_item_count": deterministic_evidence_diagnostics.get("deterministic_extra_item_count", 0),
                "publication_explicit_mismatch_count": deterministic_evidence_diagnostics.get("publication_explicit_mismatch_count", 0),
                "publication_annotation_count": len(docs),
                "model": args.model,
                "wall_seconds": stats.get("wall_seconds", 0),
                "validation_warning_count": len(warnings),
                "deterministic_risk_signal_count": len(risk_signals),
                "deterministic_onecell_support_count": len(onecell_support),
                "deterministic_sample_linkage_ambiguity_count": len(ambiguity_signals),
                "deterministic_benchmark_only_signal_count": len(benchmark_signals),
                "deterministic_qualifying_branch_signal_count": len(qualifying_branch_signals),
                "deterministic_nonqualifying_signal_count": len(nonqualifying_signals),
                "deterministic_sample_unit_signal_count": len(sample_unit_signals),
                "result_path": str(status_path.resolve()),
                "error": "",
            }
            payload["summary"] = summary
            status_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            summary_rows.append(summary)
            print(f"[{index}/{len(candidates)}] {accession} -> {decision} evidence={len(items)} warnings={len(warnings)}")
        except Exception as exc:
            summary = {
                "accession": accession,
                "run_status": "error",
                "curation_decision": "",
                "decision_reason": "",
                "evidence_item_count": len(items),
                "deterministic_evidence_item_count": len(deterministic_items),
                "deterministic_extra_item_count": deterministic_evidence_diagnostics.get("deterministic_extra_item_count", 0),
                "publication_explicit_mismatch_count": deterministic_evidence_diagnostics.get("publication_explicit_mismatch_count", 0),
                "publication_annotation_count": len(docs),
                "model": args.model,
                "wall_seconds": 0,
                "validation_warning_count": 0,
                "result_path": str(status_path.resolve()),
                "error": f"{type(exc).__name__}: {exc}",
            }
            status_path.write_text(json.dumps({"accession": accession, "summary": summary}, indent=2), encoding="utf-8")
            summary_rows.append(summary)
            print(f"[{index}/{len(candidates)}] {accession} -> error: {exc}")

    summary_rows.sort(key=lambda row: text(row.get("accession")))
    write_tsv(output_dir / "curation_v19_summary.tsv", summary_rows)
    metrics = {
        "curation_pipeline_version": PIPELINE_VERSION,
        "candidates_selected": len(summary_rows),
        "successful": sum(row.get("run_status") == "success" for row in summary_rows),
        "errors": sum(row.get("run_status") == "error" for row in summary_rows),
        "decision_counts": {},
        "rows_with_publication_annotations": sum(int(row.get("publication_annotation_count") or 0) > 0 for row in summary_rows),
        "rows_repository_only_in_packet": sum(int(row.get("publication_annotation_count") or 0) == 0 for row in summary_rows),
        "note": "Shadow curation only. Does not alter Stage04/Stage05 outputs or frozen GT196.",
    }
    for row in summary_rows:
        decision = text(row.get("curation_decision"))
        if decision:
            metrics["decision_counts"][decision] = metrics["decision_counts"].get(decision, 0) + 1
    (output_dir / "curation_v19_metrics.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(metrics, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
