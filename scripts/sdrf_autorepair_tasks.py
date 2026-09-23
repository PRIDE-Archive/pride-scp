#!/usr/bin/env python3
"""Build source-grounded scientific-agent tasks from frozen readiness diagnostics.

This bridge is deliberately non-generative. It never edits SDRF rows. It converts
machine-readable readiness/scientific-guard findings into bounded scientific tasks
that the existing Rust scientific workspace agent must resolve from trusted evidence.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

PLACEHOLDERS = {
    "", "not available", "not applicable", "unknown", "unspecified", "na", "n/a"
}


def parse_accessions(path: Path) -> list[str]:
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        acc = raw.strip().upper()
        if not acc or acc.startswith("#"):
            continue
        if not re.fullmatch(r"PXD\d{6}", acc):
            raise SystemExit(f"invalid accession in {path}: {raw!r}")
        if acc not in out:
            out.append(acc)
    return out


def read_json(path: Path) -> dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"expected JSON object: {path}")
    return obj


def read_tsv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        rows = [dict(row) for row in reader]
        return list(reader.fieldnames or []), rows


def read_tsv_matrix(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh, delimiter="\t")
        table = list(reader)
    if not table:
        return [], []
    headers = table[0]
    rows = [row + [""] * (len(headers) - len(row)) for row in table[1:]]
    return headers, [row[:len(headers)] for row in rows]


def locate_candidate(accession: str, readiness: dict[str, Any] | None, roots: list[Path]) -> Path | None:
    if readiness:
        candidate = readiness.get("candidate") or {}
        path = Path(str(candidate.get("path") or ""))
        if path.is_file():
            return path
    found: list[Path] = []
    for root in roots:
        if root.is_dir():
            found.extend(p for p in root.rglob(f"{accession}.sdrf.tsv") if p.is_file())
    found = sorted(set(found))
    if len(found) == 1:
        return found[0]
    return None


def task(
    task_id: str,
    concept: str,
    field: str,
    code: str,
    objective: str,
    messages: list[str] | None = None,
    rows: list[int] | None = None,
    error_count: int = 1,
) -> dict[str, Any]:
    return {
        "id": f"readiness:{task_id}",
        "concept_type": concept,
        "sdrf_field": field,
        "error_codes": [code],
        "error_count": max(1, error_count),
        "representative_rows": (rows or [])[:24],
        "representative_messages": (messages or [])[:24],
        "objective": objective,
    }


def placeholder_tasks(readiness: dict[str, Any]) -> list[dict[str, Any]]:
    cols = [str(x) for x in readiness.get("placeholder_required_columns") or []]
    blockers = [str(x) for x in readiness.get("blockers") or []]
    if "all_rows_placeholder_in_required_single_cell_column" not in blockers:
        return []
    out: list[dict[str, Any]] = []
    for col in cols:
        lower = col.lower()
        if "single cell isolation protocol" in lower:
            out.append(task(
                "isolation_template_or_value",
                "isolation_method",
                "single_cell_isolation_method",
                "all_rows_placeholder_in_required_single_cell_column",
                "Determine the real single-cell isolation/sampling method from trusted sources. Record the source-faithful method; if the pinned SDRF template cannot represent it faithfully, preserve a template_gap rather than substituting another allowed method.",
            ))
        elif "organism part" in lower:
            out.append(task(
                "organism_part",
                "organism_part",
                "organism_part",
                "all_rows_placeholder_in_required_single_cell_column",
                "Recover the organism-part context from trusted sources without collapsing distinct biological materials.",
            ))
    return out


def scientific_guard_tasks(readiness: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    findings = [str(x) for x in (readiness.get("blockers") or []) + (readiness.get("warnings") or [])]
    for finding in findings:
        lower = finding.lower()
        if "multi_organism_part_project_collapsed_to_single_candidate_part" in lower:
            out.append(task(
                "organism_part_scope",
                "organism_part",
                "organism_part",
                "scientific_guard_multi_organism_part_project_collapsed_to_single_candidate_part",
                "Reconstruct source-grounded organism-part scope. Preserve distinct biological materials/parts as branches when the project contains more than one supported context; do not broadcast one part across the project.",
                [finding],
            ))
        if "acquisition" in lower and ("contradict" in lower or "mapping" in lower):
            out.append(task(
                "acquisition_mode",
                "acquisition_mode",
                "proteomics_data_acquisition_method",
                "readiness_acquisition_conflict",
                "Resolve acquisition mode from direct trusted publication/repository evidence. Filename tokens are hints only and cannot authorize DDA/DIA replacement.",
                [finding],
            ))
    return out


def parse_sdrf_tasks(readiness: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    messages: list[str] = []
    for result in readiness.get("parse_sdrf") or []:
        if not isinstance(result, dict) or result.get("passed") is True:
            continue
        messages.extend(str(result.get(key) or "") for key in ("stderr", "stdout"))
    joined = "\n".join(messages).lower()
    if "acquisition" in joined or "data-dependent" in joined or "data-independent" in joined:
        out.append(task(
            "parse_acquisition",
            "acquisition_mode",
            "proteomics_data_acquisition_method",
            "parse_sdrf_acquisition_failure",
            "Resolve the acquisition regime from trusted source evidence and let Rust canonicalize the SDRF representation.",
            [x for x in messages if x][:12],
        ))
    if "isolation" in joined:
        out.append(task(
            "parse_isolation",
            "isolation_method",
            "single_cell_isolation_method",
            "parse_sdrf_isolation_failure",
            "Resolve the source-faithful isolation method or explicit template gap from trusted evidence.",
            [x for x in messages if x][:12],
        ))
    return out


def multiplex_mapping_task(candidate: Path | None) -> list[dict[str, Any]]:
    if candidate is None or not candidate.is_file():
        return []
    headers, rows = read_tsv_matrix(candidate)
    if not rows or "comment[data file]" not in headers:
        return []
    first_index = {header: headers.index(header) for header in set(headers)}
    data_idx = first_index["comment[data file]"]
    sample_idx = first_index.get("characteristics[sample type]")
    cells_idx = first_index.get("characteristics[cells per well]")
    label_indices = [i for i, h in enumerate(headers) if h == "comment[label]"]
    if not label_indices:
        return []

    data_groups: dict[str, list[list[str]]] = defaultdict(list)
    for row in rows:
        raw = row[data_idx].strip().lower()
        if raw:
            data_groups[raw].append(row)
    multiplex_groups = [group for group in data_groups.values() if len(group) > 1]
    if len(multiplex_groups) < 2:
        return []

    uninformative = {
        "", "not available", "not applicable", "label free sample", "label-free sample", "label free"
    }
    # Duplicate label columns are preserved positionally. If any label column
    # contains a concrete non-label-free channel/chemistry value, the SDRF
    # already carries multiplex identity and must not be flagged merely because
    # another repeated label column is unresolved.
    informative_label = any(
        row[idx].strip().lower() not in uninformative
        for row in rows
        for idx in label_indices
        if idx < len(row)
    )
    if informative_label:
        return []

    has_multiplex_roles = False
    for group in multiplex_groups:
        for row in group:
            sample = row[sample_idx].strip().lower() if sample_idx is not None else ""
            cells = row[cells_idx].strip() if cells_idx is not None else ""
            if sample in {"carrier", "reference"} or (cells.isdigit() and int(cells) > 1):
                has_multiplex_roles = True
                break
        if has_multiplex_roles:
            break
    if not has_multiplex_roles:
        return []

    repeated = sum(len(group) for group in multiplex_groups)
    return [task(
        "multiplex_reporter_mapping",
        "labeling",
        "label",
        "multiplex_reporter_channel_mapping_unresolved",
        "Determine the multiplex chemistry and exact reporter-channel/sample mapping from trusted publication, supplement, or structured-design evidence. Never infer reporter identity from SDRF row order. If exact channel mapping is unavailable after bounded search, escalate as row_mapping_required rather than inventing labels.",
        [f"{len(multiplex_groups)} repeated acquisition groups contain {repeated} SDRF rows while every repeated comment[label] column is label-free/unresolved"],
        error_count=repeated,
    )]


def stage1_provenance_task(accession: str, stage1_root: Path | None) -> list[dict[str, Any]]:
    if stage1_root is None:
        return []
    path = stage1_root / "study_factor_graphs" / accession / "accepted_graph.json"
    if not path.is_file():
        return []
    try:
        graph = read_json(path)
    except Exception:
        return []
    if str(graph.get("status") or "") != "human_review":
        return []
    reason = str(graph.get("reason") or "")
    lower = reason.lower()
    conflict = (
        "does not contain evidence for single-cell proteomics" in lower
        or "does not contain evidence for single cell proteomics" in lower
        or ("single-cell proteomics" in lower and "hallucinat" in lower)
        or ("single cell proteomics" in lower and "hallucinat" in lower)
    )
    if not conflict:
        return []
    return [task(
        "provenance_scope_conflict",
        "study_structure",
        "study_structure",
        "trusted_source_scope_conflict",
        "Reconcile trusted-source identity and study scope. Verify accession/publication/repository linkage, reject mismatched evidence branches, and retain only sources that demonstrably describe this proteomics accession. If the conflict cannot be resolved from registered trusted sources, escalate as provenance_conflict.",
        [reason],
    )]


def dedup_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in tasks:
        key = (str(item["concept_type"]), str(item["sdrf_field"]))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def build_for_accession(
    accession: str,
    readiness_dir: Path | None,
    candidate_roots: list[Path],
    stage1_root: Path | None,
) -> dict[str, Any]:
    readiness: dict[str, Any] | None = None
    if readiness_dir is not None:
        path = readiness_dir / "accessions" / f"{accession}.readiness.json"
        if path.is_file():
            readiness = read_json(path)
    candidate = locate_candidate(accession, readiness, candidate_roots)
    tasks: list[dict[str, Any]] = []
    if readiness:
        tasks.extend(placeholder_tasks(readiness))
        tasks.extend(scientific_guard_tasks(readiness))
        tasks.extend(parse_sdrf_tasks(readiness))
    tasks.extend(multiplex_mapping_task(candidate))
    tasks.extend(stage1_provenance_task(accession, stage1_root))
    tasks = dedup_tasks(tasks)
    return {
        "accession": accession,
        "readiness_state": (readiness or {}).get("state", ""),
        "candidate_path": str(candidate) if candidate else "",
        "tasks": tasks,
    }


def build(args: argparse.Namespace) -> dict[str, Any]:
    accessions = parse_accessions(args.accessions_file)
    args.output.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    with_tasks = 0
    for accession in accessions:
        manifest = build_for_accession(
            accession,
            args.readiness_dir,
            args.candidate_root,
            args.stage1_root,
        )
        (args.output / f"{accession}.json").write_text(
            json.dumps({"accession": accession, "tasks": manifest["tasks"]}, indent=2) + "\n",
            encoding="utf-8",
        )
        if manifest["tasks"]:
            with_tasks += 1
        rows.append({
            "accession": accession,
            "readiness_state": str(manifest["readiness_state"]),
            "candidate_path": str(manifest["candidate_path"]),
            "task_count": str(len(manifest["tasks"])),
            "task_ids": ";".join(str(x["id"]) for x in manifest["tasks"]),
        })
    ledger = args.output / "readiness_task_manifest.tsv"
    with ledger.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            delimiter="\t",
            fieldnames=["accession", "readiness_state", "candidate_path", "task_count", "task_ids"],
        )
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "accessions": len(accessions),
        "accessions_with_tasks": with_tasks,
        "task_count": sum(int(row["task_count"]) for row in rows),
        "manifest_tsv": str(ledger),
    }
    (args.output / "readiness_task_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="pride-scp-autorepair-tasks-") as td:
        root = Path(td)
        accessions = root / "accessions.txt"
        accessions.write_text("PXD000001\nPXD000002\nPXD000003\n", encoding="utf-8")
        readiness = root / "readiness" / "accessions"
        readiness.mkdir(parents=True)
        candidate_root = root / "candidates"
        stage1 = root / "stage1"
        out = root / "tasks"

        # Template gap / organism-part scope fixture.
        a1 = "PXD000001"
        c1 = candidate_root / a1 / f"{a1}.sdrf.tsv"
        c1.parent.mkdir(parents=True)
        c1.write_text(
            "source name\tcomment[data file]\tcharacteristics[sample type]\tcharacteristics[single cell isolation protocol]\n"
            "c1\tc1.raw\tsingle cell\tnot applicable\n",
            encoding="utf-8",
        )
        (readiness / f"{a1}.readiness.json").write_text(json.dumps({
            "state": "blocked_metadata_incomplete",
            "candidate": {"path": str(c1)},
            "blockers": ["all_rows_placeholder_in_required_single_cell_column"],
            "warnings": ["scientific_guard:multi_organism_part_project_collapsed_to_single_candidate_part"],
            "placeholder_required_columns": ["characteristics[single cell isolation protocol]"],
        }), encoding="utf-8")

        # Multiplex mapping fixture.
        a2 = "PXD000002"
        c2 = candidate_root / a2 / f"{a2}.sdrf.tsv"
        c2.parent.mkdir(parents=True)
        c2.write_text(
            "source name\tcomment[data file]\tcomment[label]\tcharacteristics[sample type]\tcharacteristics[cells per well]\n"
            "s1\tr1.raw\tLabel free sample\tsingle cell\t1\n"
            "s2\tr1.raw\tLabel free sample\tcarrier\t250\n"
            "s3\tr2.raw\tLabel free sample\tsingle cell\t1\n"
            "s4\tr2.raw\tLabel free sample\tcarrier\t250\n",
            encoding="utf-8",
        )
        (readiness / f"{a2}.readiness.json").write_text(json.dumps({
            "state": "needs_independent_review", "candidate": {"path": str(c2)},
            "blockers": [], "warnings": [], "placeholder_required_columns": [],
        }), encoding="utf-8")

        # Provenance conflict fixture.
        a3 = "PXD000003"
        c3 = candidate_root / a3 / f"{a3}.sdrf.tsv"
        c3.parent.mkdir(parents=True)
        c3.write_text("source name\tcomment[data file]\nx\tx.raw\n", encoding="utf-8")
        g3 = stage1 / "study_factor_graphs" / a3
        g3.mkdir(parents=True)
        (g3 / "accepted_graph.json").write_text(json.dumps({
            "status": "human_review",
            "reason": "trusted sources do not contain evidence for single-cell proteomics and constructing it would require hallucinating data",
        }), encoding="utf-8")

        ns = argparse.Namespace(
            accessions_file=accessions,
            readiness_dir=root / "readiness",
            candidate_root=[candidate_root],
            stage1_root=stage1,
            output=out,
        )
        summary = build(ns)
        assert summary["accessions"] == 3
        assert summary["accessions_with_tasks"] == 3
        m1 = read_json(out / f"{a1}.json")
        assert {x["concept_type"] for x in m1["tasks"]} == {"isolation_method", "organism_part"}
        m2 = read_json(out / f"{a2}.json")
        assert any(x["error_codes"] == ["multiplex_reporter_channel_mapping_unresolved"] for x in m2["tasks"])
        m3 = read_json(out / f"{a3}.json")
        assert any(x["concept_type"] == "study_structure" for x in m3["tasks"])
    print("sdrf_autorepair_tasks self-test: PASS")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--accessions-file", type=Path)
    p.add_argument("--readiness-dir", type=Path)
    p.add_argument("--candidate-root", type=Path, action="append", default=[])
    p.add_argument("--stage1-root", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--self-test", action="store_true")
    return p


def main() -> int:
    args = parser().parse_args()
    if args.self_test:
        self_test()
        return 0
    missing = [name for name in ("accessions_file", "output") if getattr(args, name) is None]
    if missing:
        raise SystemExit("missing required arguments: " + ", ".join("--" + x.replace("_", "-") for x in missing))
    summary = build(args)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
