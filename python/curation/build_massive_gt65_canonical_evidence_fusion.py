#!/usr/bin/env python3
"""Build the bounded M2-C2 canonical evidence-fusion benchmark.

This stage is intentionally narrow.  It starts from the accepted M2-C1 GT65
benchmark and augments only PXD representatives with repository-native MassIVE
evidence reached through trusted source-derived MSV<->PXD aliases already stored
in the normalized MassIVE snapshot.  GT is not consulted to create or validate
identity edges and no curation labels are written into the candidate JSONL.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import build_massive_native_curation_bridge as native_bridge


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
    return rows


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def split_aliases(value: str) -> list[str]:
    return sorted({part.strip().upper() for part in (value or "").split(";") if part.strip().upper().startswith("PXD")})


def load_massive_alias_index(crosswalk: Path) -> tuple[dict[str, list[str]], dict[str, dict[str, str]]]:
    """Return PXD->MSV from the source-derived normalized MassIVE crosswalk."""
    pxd_to_msv: dict[str, list[str]] = defaultdict(list)
    by_msv: dict[str, dict[str, str]] = {}
    for row in read_tsv(crosswalk):
        msv = text(row.get("msv_accession")).upper()
        if not msv.startswith("MSV"):
            continue
        by_msv[msv] = row
        for pxd in split_aliases(text(row.get("pxd_aliases"))):
            pxd_to_msv[pxd].append(msv)
    return {pxd: sorted(set(msvs)) for pxd, msvs in pxd_to_msv.items()}, by_msv


def build_fused_sidecar(pxd: str, msvs: list[str], snapshot: Path, output_path: Path) -> dict[str, Any]:
    massive_root = snapshot / "native" / "massive"
    projects_dir = massive_root / "projects"
    cache_root = massive_root / "identity_enrichment"

    all_items: list[dict[str, Any]] = []
    all_aliases: set[str] = {pxd}
    all_dois: set[str] = set()
    all_urls: set[str] = set()
    canonical_ids: set[str] = set()
    source_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    native_sources: list[dict[str, Any]] = []

    for msv in sorted(msvs):
        project_path = projects_dir / f"{msv}.json"
        project = native_bridge.load_json(project_path)
        if not isinstance(project, dict):
            raise ValueError(f"missing/unreadable trusted native project {project_path}")

        aliases = native_bridge.aliases_from_project(project)
        if pxd not in aliases:
            raise ValueError(f"trusted crosswalk edge {msv}<->{pxd} is not present in normalized project identity")
        all_aliases.update(aliases)

        identity = project.get("sourceIdentity")
        if isinstance(identity, dict):
            canonical = text(identity.get("canonical_dataset_identity"))
            if canonical:
                canonical_ids.add(canonical)

        source_specs = [
            ("massive_native_record", project.get("massiveNativeRecord")),
            ("massive_proxi", native_bridge.load_json(cache_root / "massive_proxi" / f"{msv}.json")),
            ("massive_detail", native_bridge.load_json(cache_root / "massive_detail" / f"{msv}.json")),
            ("proteomecentral_direct", native_bridge.load_json(cache_root / "proteomecentral_direct" / f"{msv}.json")),
            ("proteomecentral_search", native_bridge.load_json(cache_root / "proteomecentral_search" / f"{msv}.json")),
        ]
        payloads: list[Any] = []
        per_source_counts: Counter[str] = Counter()
        for source, payload in source_specs:
            if payload is None:
                continue
            payloads.append(payload)
            items = native_bridge.collect_source_items(source, payload)
            for item in items:
                enriched = dict(item)
                enriched["native_accession"] = msv
                # Include the native accession in the source path so downstream
                # provenance remains explicit after canonical fusion.
                enriched["source_path"] = f"{msv}:{text(item.get('source_path')) or 'unknown'}"
                all_items.append(enriched)
                source_counts[source] += 1
                per_source_counts[source] += 1
                for category in item.get("categories") or []:
                    category_counts[str(category)] += 1

        dois, urls = native_bridge.publication_ids(project, payloads)
        all_dois.update(dois)
        all_urls.update(urls)
        native_sources.append({
            "native_accession": msv,
            "project_json": str(project_path.resolve()),
            "trusted_pxd_aliases": aliases,
            "source_item_counts": dict(sorted(per_source_counts.items())),
        })

    if len(canonical_ids) > 1:
        raise ValueError(
            f"canonical identity conflict for {pxd}: trusted linked MSVs resolve to {sorted(canonical_ids)}"
        )

    items = native_bridge.unique_items(all_items, max_items=300, max_chars=180_000)
    canonical = next(iter(canonical_ids), "")
    sidecar = {
        "bridge_version": "massive-canonical-evidence-fusion-v1",
        "accession": pxd,
        "repository": "canonical:ProteomeXchange+MassIVE",
        "canonical_dataset_identity": canonical,
        "trusted_pxd_aliases": sorted(all_aliases),
        "linked_native_accessions": sorted(msvs),
        "publication_dois": sorted(all_dois),
        "publication_urls": sorted(all_urls),
        "category_counts": dict(sorted(category_counts.items())),
        "source_counts": dict(sorted(source_counts.items())),
        "evidence_items": items,
        "native_sources": native_sources,
        "provenance": {
            "snapshot": str(snapshot.resolve()),
            "identity_source": "normalized MassIVE pxdAliases/sourceIdentity",
            "gt_used_for_identity": False,
            "gt_used_for_fusion": False,
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return sidecar


def sanitized_candidate(row: dict[str, Any]) -> dict[str, Any]:
    banned = ("gt_", "evaluation_", "matched_frozen_gt")
    return {
        key: value for key, value in row.items()
        if not any(str(key).lower().startswith(prefix) for prefix in banned)
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--m2c1-candidates-jsonl", type=Path, required=True)
    ap.add_argument("--m2c1-manifest", type=Path, required=True)
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--expect-gt-rows", type=int, default=65)
    ap.add_argument("--expect-pxd-gt-rows", type=int, default=26)
    ap.add_argument("--expect-unique-pxd-representatives", type=int, default=25)
    args = ap.parse_args()

    snapshot = args.snapshot.resolve()
    crosswalk = snapshot / "native" / "massive" / "massive_accessions.tsv"
    if not crosswalk.is_file():
        raise SystemExit(f"missing normalized MassIVE crosswalk: {crosswalk}")

    manifest = read_tsv(args.m2c1_manifest)
    if len(manifest) != args.expect_gt_rows:
        raise SystemExit(f"M2-C2 guard failed: expected {args.expect_gt_rows} GT manifest rows, observed {len(manifest)}")

    candidates = {text(row.get("accession")).upper(): row for row in load_jsonl(args.m2c1_candidates_jsonl)}
    pxd_manifest_rows = [row for row in manifest if text(row.get("representation_class")) == "pxd_candidate"]
    if len(pxd_manifest_rows) != args.expect_pxd_gt_rows:
        raise SystemExit(
            f"M2-C2 guard failed: expected {args.expect_pxd_gt_rows} PXD GT rows, observed {len(pxd_manifest_rows)}"
        )
    pxd_reps = sorted({text(row.get("representative_accession")).upper() for row in pxd_manifest_rows})
    if len(pxd_reps) != args.expect_unique_pxd_representatives:
        raise SystemExit(
            f"M2-C2 guard failed: expected {args.expect_unique_pxd_representatives} unique PXD representatives, observed {len(pxd_reps)}"
        )

    pxd_to_msv, _ = load_massive_alias_index(crosswalk)
    output = args.output_dir.resolve()
    evidence_dir = output / "evidence"
    output.mkdir(parents=True, exist_ok=True)

    fused_candidates: dict[str, dict[str, Any]] = {acc: sanitized_candidate(row) for acc, row in candidates.items()}
    fusion_rows: list[dict[str, Any]] = []
    missing_edges: list[str] = []

    for pxd in pxd_reps:
        base = fused_candidates.get(pxd)
        if base is None:
            raise SystemExit(f"M2-C2 PXD representative missing from M2-C1 candidates: {pxd}")
        msvs = pxd_to_msv.get(pxd, [])
        if not msvs:
            missing_edges.append(pxd)
            continue
        sidecar_path = evidence_dir / f"{pxd}.canonical_massive_fusion.json"
        try:
            sidecar = build_fused_sidecar(pxd, msvs, snapshot, sidecar_path)
        except ValueError as exc:
            raise SystemExit(f"M2-C2 source identity/evidence fusion failed for {pxd}: {exc}") from exc

        fused = dict(base)
        fused["native_evidence_path"] = str(sidecar_path.resolve())
        fused["canonical_dataset_identity"] = text(sidecar.get("canonical_dataset_identity"))
        fused["trusted_pxd_aliases"] = sidecar.get("trusted_pxd_aliases") or [pxd]
        fused["canonical_fused_native_accessions"] = sidecar.get("linked_native_accessions") or []
        fused["canonical_evidence_fusion_version"] = "massive-canonical-evidence-fusion-v1"
        # Keep accession/repository/project paths of the PXD representative: the
        # native sidecar supplements rather than replaces its repository packet.
        fused_candidates[pxd] = sanitized_candidate(fused)
        fusion_rows.append({
            "representative_accession": pxd,
            "linked_native_accessions": "; ".join(sidecar.get("linked_native_accessions") or []),
            "canonical_dataset_identity": text(sidecar.get("canonical_dataset_identity")),
            "trusted_pxd_aliases": "; ".join(sidecar.get("trusted_pxd_aliases") or []),
            "fused_native_evidence_items": len(sidecar.get("evidence_items") or []),
            "sample_metadata_items": int((sidecar.get("category_counts") or {}).get("sample_metadata", 0)),
            "ms_method_items": int((sidecar.get("category_counts") or {}).get("ms_method", 0)),
            "publication_items": int((sidecar.get("category_counts") or {}).get("publication", 0)),
            "publication_identifier_count": len(sidecar.get("publication_dois") or []) + len(sidecar.get("publication_urls") or []),
            "native_evidence_path": str(sidecar_path.resolve()),
            "gt_used_for_identity": "no",
            "gt_used_for_fusion": "no",
        })

    if missing_edges:
        raise SystemExit(
            "M2-C2 source-derived alias coverage failed for PXD representatives: " + ", ".join(sorted(missing_edges))
        )

    # Write all 63 M2-C1 representatives for reproducibility, plus a 25-accession
    # file so the runner can rerun only the affected PXD candidates.
    candidates_path = output / "massive_gt65_canonical_fusion_candidates.jsonl"
    with candidates_path.open("w", encoding="utf-8") as handle:
        for accession in sorted(fused_candidates):
            handle.write(json.dumps(fused_candidates[accession], ensure_ascii=False) + "\n")
    affected_path = output / "affected_pxd_representatives.txt"
    affected_path.write_text("\n".join(pxd_reps) + "\n", encoding="utf-8")

    fields = [
        "representative_accession", "linked_native_accessions", "canonical_dataset_identity",
        "trusted_pxd_aliases", "fused_native_evidence_items", "sample_metadata_items",
        "ms_method_items", "publication_items", "publication_identifier_count",
        "native_evidence_path", "gt_used_for_identity", "gt_used_for_fusion",
    ]
    write_tsv(output / "canonical_evidence_fusion_audit.tsv", fusion_rows, fields)

    # Preserve the 65-row evaluation mapping while clearly indicating the new
    # representation class for affected rows.  GT fields live only here.
    fused_manifest: list[dict[str, Any]] = []
    fusion_by_pxd = {row["representative_accession"]: row for row in fusion_rows}
    for row in manifest:
        out_row = dict(row)
        rep = text(row.get("representative_accession")).upper()
        fusion = fusion_by_pxd.get(rep)
        if fusion:
            out_row["representation_class"] = "pxd_candidate_with_native_fusion"
            out_row["native_evidence_attached"] = "yes"
            out_row["canonical_fusion_applied"] = "yes"
            out_row["fused_native_accessions"] = fusion["linked_native_accessions"]
            out_row["fused_native_evidence_items"] = fusion["fused_native_evidence_items"]
        else:
            out_row["canonical_fusion_applied"] = "no_existing_native_representation"
            out_row["fused_native_accessions"] = ""
            out_row["fused_native_evidence_items"] = "0"
        fused_manifest.append(out_row)

    manifest_fields = list(fused_manifest[0].keys()) if fused_manifest else []
    write_tsv(output / "massive_gt65_canonical_fusion_manifest.tsv", fused_manifest, manifest_fields)

    summary = {
        "phase": "M2-C2_canonical_evidence_fusion_build",
        "evaluation_only": True,
        "gt_rows": len(fused_manifest),
        "unique_curation_candidates": len(fused_candidates),
        "pxd_gt_rows": len(pxd_manifest_rows),
        "unique_pxd_representatives": len(pxd_reps),
        "pxd_representatives_with_source_derived_native_aliases": len(fusion_rows),
        "fusion_coverage_complete": len(fusion_rows) == len(pxd_reps),
        "linked_native_accession_count": len({msv for row in fusion_rows for msv in text(row["linked_native_accessions"]).split("; ") if msv}),
        "fused_native_evidence_item_total": sum(int(row["fused_native_evidence_items"]) for row in fusion_rows),
        "pxd_representatives_with_sample_or_method_evidence": sum(
            int(row["sample_metadata_items"]) + int(row["ms_method_items"]) > 0 for row in fusion_rows
        ),
        "gt_labels_written_to_curation_candidates": False,
        "gt_used_for_identity": False,
        "gt_used_for_fusion": False,
        "affected_accessions_file": str(affected_path.resolve()),
        "fused_candidates_jsonl": str(candidates_path.resolve()),
        "note": "Only trusted source-derived MassIVE aliases are fused. GT defines the evaluation cohort but never creates identity/evidence edges or enters v19 candidate JSONL.",
    }
    (output / "massive_gt65_canonical_fusion_build_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
