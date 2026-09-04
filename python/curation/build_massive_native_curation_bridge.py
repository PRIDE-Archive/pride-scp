#!/usr/bin/env python3
"""Build GT-blind MassIVE-native evidence sidecars for frozen v19 curation.

The bridge does not classify datasets and does not consume GT. It turns the
accepted post-identity MassIVE candidates plus cached structured repository
responses into provenance-preserving evidence sidecars and an augmented
candidate JSONL that the unchanged v19-shadow-2.7.1 decision policy can read.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

MSV_RE = re.compile(r"^MSV\d{9}$", re.I)
PXD_RE = re.compile(r"\bPXD\d{6}\b", re.I)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
FILE_EXT_RE = re.compile(
    r"\.(?:raw|mzml|mzxml|mgf|wiff|d|tdf|tsf|hdf5|h5|csv|tsv|txt|json|xml|yaml|yml|xlsx?|zip|gz|tar|sdrf)(?:\?|$|\b)",
    re.I,
)
METADATA_FILE_RE = re.compile(r"(?:readme|sdrf|metadata|sample|experimental[_ -]?design|manifest|annotation)", re.I)
SAMPLE_RE = re.compile(
    r"\b(?:single[- ]cell|individual cell|one cell|cell per|cells per|oocyte|blastomere|zygote|neuron|"
    r"hepatocyte|cardiomyocyte|macrophage|monocyte|neutrophil|lymphocyte|astrocyte|fibroblast|microglia|"
    r"myofib(?:re|er)|bacteri(?:um|a)|cell sorting|facs|isolated cells?)\b",
    re.I,
)
METHOD_RE = re.compile(
    r"\b(?:proteom|mass spectrom|lc[- ]?ms|ms/ms|dia|dda|tmt|itraq|plexdia|scp|scope2|scope[- ]?ms|"
    r"nanopots?|npop|proteochip|cellenone|mics?drop|maldi|ce[- ]?ms|top[- ]?down)\b",
    re.I,
)
NEGATIVE_RE = re.compile(
    r"\b(?:bulk|dilut(?:ed|ion)|benchmark|single[- ]cell[- ]equivalent|low[- ]input|pooled cells?|"
    r"reanalysis|reanaly[sz]ed|spatial region|near[- ]single[- ]cell|almost[- ]single[- ]cell)\b",
    re.I,
)


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def load_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
    return rows


def write_tsv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def iter_leaves(value: Any, prefix: str = "", depth: int = 0) -> Iterable[tuple[str, str]]:
    if depth > 10:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            yield from iter_leaves(child, child_prefix, depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value[:2000]):
            yield from iter_leaves(child, f"{prefix}[{index}]", depth + 1)
    elif isinstance(value, (str, int, float, bool)):
        raw = re.sub(r"\s+", " ", text(value)).strip()
        if raw:
            yield prefix, raw


def categories_for(path: str, raw: str) -> set[str]:
    probe = f"{path} {raw}".replace("_", " ")
    cats: set[str] = set()
    if PXD_RE.search(raw):
        cats.add("accession_identity")
    if DOI_RE.search(raw) or "publication" in path.lower() or "pubmed" in raw.lower() or "doi" in path.lower():
        cats.add("publication")
    if FILE_EXT_RE.search(raw) or any(token in path.lower() for token in ("datafiles", "file", "filename")):
        cats.add("file_inventory")
    if METADATA_FILE_RE.search(raw) and (FILE_EXT_RE.search(raw) or "/" in raw or "file" in path.lower()):
        cats.add("metadata_file")
    if SAMPLE_RE.search(probe):
        cats.add("sample_metadata")
    if METHOD_RE.search(probe):
        cats.add("ms_method")
    if NEGATIVE_RE.search(probe):
        cats.add("nonqualifying_context")
    return cats


def evidence_priority(categories: set[str], source: str) -> int:
    score = 0
    if "sample_metadata" in categories:
        score += 50
    if "ms_method" in categories:
        score += 40
    if "metadata_file" in categories:
        score += 35
    if "publication" in categories:
        score += 30
    if "file_inventory" in categories:
        score += 20
    if "nonqualifying_context" in categories:
        score += 25
    if "accession_identity" in categories:
        score += 10
    if source in {"massive_proxi", "massive_detail"}:
        score += 5
    return score


def collect_source_items(source: str, payload: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path, raw in iter_leaves(payload):
        if len(raw) > 8000:
            raw = raw[:8000]
        cats = categories_for(path, raw)
        if not cats:
            continue
        if cats == {"file_inventory"} and len(raw) > 500 and not FILE_EXT_RE.search(raw):
            continue
        rows.append({
            "source_kind": "repository",
            "source": source,
            "source_path": path,
            "categories": sorted(cats),
            "priority": evidence_priority(cats, source),
            "text": raw,
        })
    return rows


def unique_items(rows: list[dict[str, Any]], max_items: int = 300, max_chars: int = 180_000) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = re.sub(r"\s+", " ", text(row.get("text"))).strip().lower()
        if not key:
            continue
        old = best.get(key)
        if old is None or int(row.get("priority") or 0) > int(old.get("priority") or 0):
            best[key] = row
    ranked = sorted(best.values(), key=lambda x: (-int(x.get("priority") or 0), text(x.get("source")), text(x.get("source_path"))))
    selected: list[dict[str, Any]] = []
    used = 0
    for row in ranked:
        cost = len(text(row.get("text"))) + 100
        if selected and used + cost > max_chars:
            continue
        item = dict(row)
        item["ref"] = f"N{len(selected)+1:03d}"
        selected.append(item)
        used += cost
        if len(selected) >= max_items:
            break
    return selected


def aliases_from_project(project: dict[str, Any]) -> list[str]:
    values: set[str] = set()
    for value in project.get("pxdAliases") or []:
        for match in PXD_RE.finditer(text(value)):
            values.add(match.group(0).upper())
    identity = project.get("sourceIdentity")
    if isinstance(identity, dict):
        for value in identity.get("pxd_aliases") or []:
            for match in PXD_RE.finditer(text(value)):
                values.add(match.group(0).upper())
    return sorted(values)


def publication_ids(project: dict[str, Any], source_payloads: list[Any]) -> tuple[list[str], list[str]]:
    dois: set[str] = set()
    urls: set[str] = set()
    identity = project.get("sourceIdentity")
    if isinstance(identity, dict):
        dois.update(text(x).rstrip(".,;:)]") for x in identity.get("publication_dois") or [] if text(x))
        urls.update(text(x) for x in identity.get("publication_urls") or [] if text(x))
    for payload in [project, *source_payloads]:
        for _, raw in iter_leaves(payload):
            for match in DOI_RE.finditer(raw):
                dois.add(match.group(0).rstrip(".,;:)]"))
            for match in URL_RE.finditer(raw):
                u = match.group(0).rstrip(".,;:)]")
                if any(k in u.lower() for k in ("doi.org", "pubmed", "publication", "article")):
                    urls.add(u)
    return sorted(dois), sorted(urls)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates-jsonl", type=Path, required=True)
    ap.add_argument("--delta-tsv", type=Path, required=True)
    ap.add_argument("--candidate-diagnostics", type=Path, required=True)
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--expect-candidates", type=int, default=118)
    args = ap.parse_args()

    output = args.output_dir.resolve()
    evidence_dir = output / "evidence"
    output.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    delta_rows = read_tsv(args.delta_tsv)
    targets = sorted({text(r.get("candidate_accession")).upper() for r in delta_rows if MSV_RE.fullmatch(text(r.get("candidate_accession")).upper())})
    if args.expect_candidates and len(targets) != args.expect_candidates:
        raise SystemExit(f"MassIVE bridge candidate guard failed: expected {args.expect_candidates}, observed {len(targets)}")

    candidate_map = {text(r.get("accession")).upper(): r for r in load_jsonl(args.candidates_jsonl)}
    diagnostics = {text(r.get("accession")).upper(): r for r in read_tsv(args.candidate_diagnostics)}
    massive_root = args.snapshot.resolve() / "native" / "massive"
    projects_dir = massive_root / "projects"
    cache_root = massive_root / "identity_enrichment"

    summary_rows: list[dict[str, Any]] = []
    augmented_rows: list[dict[str, Any]] = []
    priority_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()

    for index, accession in enumerate(targets, 1):
        candidate = candidate_map.get(accession)
        if not candidate:
            raise SystemExit(f"accepted discovery candidate missing for bridge target {accession}")
        project_path = projects_dir / f"{accession}.json"
        project = load_json(project_path)
        if not isinstance(project, dict):
            raise SystemExit(f"native project JSON missing/unreadable for {accession}: {project_path}")

        source_specs = [
            ("massive_native_record", project.get("massiveNativeRecord")),
            ("massive_proxi", load_json(cache_root / "massive_proxi" / f"{accession}.json")),
            ("massive_detail", load_json(cache_root / "massive_detail" / f"{accession}.json")),
            ("proteomecentral_direct", load_json(cache_root / "proteomecentral_direct" / f"{accession}.json")),
            ("proteomecentral_search", load_json(cache_root / "proteomecentral_search" / f"{accession}.json")),
        ]
        items: list[dict[str, Any]] = []
        for source, payload in source_specs:
            if payload is not None:
                items.extend(collect_source_items(source, payload))
        items = unique_items(items)

        payloads = [payload for _, payload in source_specs if payload is not None]
        aliases = aliases_from_project(project)
        dois, pub_urls = publication_ids(project, payloads)
        category_counts: Counter[str] = Counter()
        source_counts: Counter[str] = Counter()
        file_examples: list[str] = []
        metadata_examples: list[str] = []
        for item in items:
            source_counts[text(item.get("source"))] += 1
            for cat in item.get("categories") or []:
                category_counts[text(cat)] += 1
            raw = text(item.get("text"))
            if "file_inventory" in (item.get("categories") or []) and len(file_examples) < 8:
                file_examples.append(raw[:240])
            if "metadata_file" in (item.get("categories") or []) and len(metadata_examples) < 8:
                metadata_examples.append(raw[:240])

        diag = diagnostics.get(accession, {})
        priority = text(diag.get("semantic_priority")) or "unknown"
        has_repository_detail = bool(items)
        high_value = category_counts["sample_metadata"] + category_counts["ms_method"]
        if high_value > 0:
            packet_status = "native_evidence_ready"
        elif dois or pub_urls:
            packet_status = "publication_link_only_needs_content"
        elif has_repository_detail:
            packet_status = "repository_evidence_sparse"
        else:
            packet_status = "source_evidence_missing"
        status_counts[packet_status] += 1

        if priority == "A_specific" and not aliases:
            review_priority = "P0_native_specific"
        elif priority in {"A_specific", "B_method"}:
            review_priority = "P1_high_signal"
        elif priority == "C_broad":
            review_priority = "P2_broad"
        else:
            review_priority = "P3_adjacent_or_unknown"
        priority_counts[review_priority] += 1

        canonical = ""
        identity = project.get("sourceIdentity")
        if isinstance(identity, dict):
            canonical = text(identity.get("canonical_dataset_identity"))

        sidecar = {
            "bridge_version": "massive-native-curation-bridge-v1",
            "accession": accession,
            "repository": "MassIVE",
            "canonical_dataset_identity": canonical,
            "trusted_pxd_aliases": aliases,
            "publication_dois": dois,
            "publication_urls": pub_urls,
            "source_review_status": "unreviewed",
            "packet_status": packet_status,
            "semantic_priority": priority,
            "category_counts": dict(sorted(category_counts.items())),
            "source_counts": dict(sorted(source_counts.items())),
            "evidence_items": items,
            "provenance": {
                "project_json": str(project_path.resolve()),
                "identity_cache_root": str(cache_root.resolve()),
                "gt_used": False,
            },
        }
        sidecar_path = evidence_dir / f"{accession}.json"
        sidecar_path.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        augmented = dict(candidate)
        augmented["native_evidence_path"] = str(sidecar_path.resolve())
        augmented["canonical_dataset_identity"] = canonical
        augmented["trusted_pxd_aliases"] = aliases
        augmented["publication_dois"] = dois
        augmented["publication_urls"] = pub_urls
        augmented["repository"] = "MassIVE"
        augmented_rows.append(augmented)

        row = {
            "candidate_accession": accession,
            "canonical_dataset_identity": canonical,
            "trusted_pxd_aliases": "; ".join(aliases),
            "dataset_title": candidate.get("dataset_title", ""),
            "discovery_score": candidate.get("score", ""),
            "discovery_tier": candidate.get("tier", ""),
            "semantic_priority": priority,
            "source_review_priority": review_priority,
            "source_review_status": "unreviewed",
            "packet_status": packet_status,
            "repository_evidence_items": len(items),
            "sample_metadata_items": category_counts["sample_metadata"],
            "ms_method_items": category_counts["ms_method"],
            "nonqualifying_context_items": category_counts["nonqualifying_context"],
            "file_inventory_items": category_counts["file_inventory"],
            "metadata_file_items": category_counts["metadata_file"],
            "publication_items": category_counts["publication"],
            "publication_dois": "; ".join(dois),
            "publication_urls": "; ".join(pub_urls),
            "file_examples": " | ".join(file_examples),
            "metadata_file_examples": " | ".join(metadata_examples),
            "native_evidence_path": str(sidecar_path.resolve()),
            "gt_used_for_bridge": "no",
        }
        summary_rows.append(row)
        print(f"[{index}/{len(targets)}] {accession}: aliases={len(aliases)} evidence={len(items)} status={packet_status}", flush=True)

    augmented_path = output / "massive_native_curation_candidates.jsonl"
    with augmented_path.open("w", encoding="utf-8") as handle:
        for row in augmented_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    fields = [
        "candidate_accession", "canonical_dataset_identity", "trusted_pxd_aliases", "dataset_title",
        "discovery_score", "discovery_tier", "semantic_priority", "source_review_priority",
        "source_review_status", "packet_status", "repository_evidence_items", "sample_metadata_items",
        "ms_method_items", "nonqualifying_context_items", "file_inventory_items", "metadata_file_items",
        "publication_items", "publication_dois", "publication_urls", "file_examples", "metadata_file_examples",
        "native_evidence_path", "gt_used_for_bridge",
    ]
    write_tsv(output / "massive_native_source_evidence_audit.tsv", summary_rows, fields)
    write_tsv(
        output / "massive_native_source_review_queue.tsv",
        sorted(summary_rows, key=lambda r: (text(r["source_review_priority"]), -int(r.get("discovery_score") or 0), text(r["candidate_accession"]))),
        fields,
    )

    summary = {
        "bridge_version": "massive-native-curation-bridge-v1",
        "candidate_count": len(summary_rows),
        "expected_candidate_count": args.expect_candidates,
        "gt_used_for_bridge": False,
        "trusted_pxd_alias_candidate_count": sum(bool(r["trusted_pxd_aliases"]) for r in summary_rows),
        "native_without_trusted_pxd_alias_count": sum(not bool(r["trusted_pxd_aliases"]) for r in summary_rows),
        "publication_identifier_coverage": sum(bool(r["publication_dois"] or r["publication_urls"]) for r in summary_rows),
        "metadata_file_hint_coverage": sum(int(r["metadata_file_items"]) > 0 for r in summary_rows),
        "file_inventory_coverage": sum(int(r["file_inventory_items"]) > 0 for r in summary_rows),
        "sample_or_method_evidence_coverage": sum(int(r["sample_metadata_items"]) + int(r["ms_method_items"]) > 0 for r in summary_rows),
        "packet_status_counts": dict(sorted(status_counts.items())),
        "source_review_priority_counts": dict(sorted(priority_counts.items())),
        "augmented_candidates_jsonl": str(augmented_path.resolve()),
        "note": "GT-blind source/evidence normalization only; no inclusion/exclusion decisions are made by this bridge.",
    }
    (output / "massive_native_curation_bridge_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
