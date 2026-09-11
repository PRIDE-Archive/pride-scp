#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

VERSION = "pride-scp-sdrf-scientific-guard-v0.2"
RESERVED = {"", "not available", "not applicable", "unknown", "pooled", "anonymized"}
DDA_RE = re.compile(r"(?:^|[_\-.])DDA(?:top\d+)?(?:[_\-.]|$)", re.I)
DIA_RE = re.compile(r"(?:^|[_\-.])DIA(?:[_\-.]|$)", re.I)
TRUNCATED_PROSE_RE = re.compile(r"(?:\[[0-9]+\].*\b(?:and|then|as)\b|\b(?:figure|fig\.?|prepared|described)\b)", re.I)

@dataclass
class GuardResult:
    version: str = VERSION
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)


def _norm(v: str) -> str:
    return " ".join(str(v or "").strip().split())


def _low(v: str) -> str:
    return _norm(v).lower()


def _concrete(v: str) -> bool:
    return _low(v) not in RESERVED


def _read(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        rows = [{k: str(v or "") for k, v in r.items()} for r in reader]
        return list(reader.fieldnames or []), rows


def _project_values(project: dict[str, Any], key: str) -> list[str]:
    out = []
    for x in project.get(key) or []:
        if isinstance(x, dict):
            v = _norm(x.get("name", ""))
        else:
            v = _norm(x)
        if v and _low(v) not in RESERVED:
            out.append(v)
    return sorted(set(out), key=str.lower)


def analyze(path: Path, project_json: Path | None = None) -> GuardResult:
    headers, rows = _read(path)
    result = GuardResult()

    def add(code: str, row_idx: int | None = None, **detail: Any) -> None:
        token = code if row_idx is None else f"{code}:row_{row_idx}"
        result.blockers.append(token)
        result.details.append({"code": code, "row": row_idx, **detail})

    # 1. Individual/donor semantic leakage from other biological columns.
    aliases = [
        "characteristics[organism part]",
        "characteristics[cell line]",
        "characteristics[cell type]",
        "characteristics[developmental stage]",
    ]
    if "characteristics[individual]" in headers:
        for i, row in enumerate(rows, 2):
            individual = _norm(row.get("characteristics[individual]", ""))
            if not _concrete(individual):
                continue
            matched = False
            for h in aliases:
                other = _norm(row.get(h, ""))
                if _concrete(other) and _low(individual) == _low(other):
                    add("individual_duplicates_nonindividual_semantic_field", i,
                        individual=individual, duplicate_header=h, duplicate_value=other)
                    matched = True
                    break
            if not matched:
                cell_line = _norm(row.get("characteristics[cell line]", ""))
                if _concrete(cell_line) and re.search(r"(?:^|[_ -])donor$", individual, re.I):
                    a = re.sub(r"[^a-z0-9]", "", cell_line.lower())
                    b = re.sub(r"[^a-z0-9]", "", re.sub(r"(?:[_ -]?donor)$", "", individual, flags=re.I).lower())
                    prefix = 0
                    for ca, cb in zip(a, b):
                        if ca != cb:
                            break
                        prefix += 1
                    if prefix >= 3:
                        add("individual_looks_synthesized_from_cell_line", i, individual=individual, cell_line=cell_line)

    # 2. Explicit acquisition tokens in deposited file names must not contradict metadata.
    for i, row in enumerate(rows, 2):
        fn = _norm(row.get("comment[data file]", ""))
        acq = _low(row.get("comment[proteomics data acquisition method]", ""))
        if DDA_RE.search(fn) and "independent" in acq:
            add("data_file_name_explicit_dda_conflicts_with_dia_metadata", i, data_file=fn, acquisition=acq)
        if DIA_RE.search(fn) and "dependent acquisition" in acq and "independent" not in acq:
            add("data_file_name_explicit_dia_conflicts_with_dda_metadata", i, data_file=fn, acquisition=acq)

    # 3. Empty/blank controls should not carry concrete biological identities as if they were cells.
    for i, row in enumerate(rows, 2):
        role = _low(row.get("characteristics[sample type]", ""))
        cells = _low(row.get("characteristics[cells per well]", ""))
        source = _low(row.get("source name", ""))
        is_empty = role in {"empty", "blank", "negative control"} or cells == "0" or "blank" in source
        if not is_empty:
            continue
        bad = []
        for h in ("characteristics[individual]", "characteristics[cell type]", "characteristics[cell line]"):
            if _concrete(row.get(h, "")):
                bad.append(h)
        if bad:
            add("empty_control_has_concrete_biological_identity", i, headers=bad)

    # 4. Known sample-role ontology delivery gap. The current specification advertises
    #    "study sample", but the maintained PRIDE ontology cache used by sdrf-pipelines does not
    #    currently expose it as a child of PRIDE:0000895. Do not synthesize this literal in new
    #    output. Use a source-backed ontology role where one exists; otherwise fail closed to the
    #    reserved value "not available". This guard is intentionally explicit so ontology-skipped
    #    HPC readiness runs cannot silently publish a value that current upstream CI rejects.
    if "characteristics[sample type]" in headers:
        for i, row in enumerate(rows, 2):
            if _low(row.get("characteristics[sample type]", "")) == "study sample":
                add(
                    "sample_type_study_sample_not_currently_validator_backed",
                    i,
                    value=row.get("characteristics[sample type]", ""),
                    remediation="use a source-backed PRIDE sample-role term, or 'not available' when the role is not source-resolved",
                )

    # 5. Narrow identifier/model/version fields must not contain obvious truncated protocol prose.
    for h in ("comment[nanopots chip version]", "comment[microfluidics chip type]", "comment[lcm microscope model]"):
        if h not in headers:
            continue
        for i, row in enumerate(rows, 2):
            v = _norm(row.get(h, ""))
            if not _concrete(v):
                continue
            last = v.split()[-1] if v.split() else ""
            truncated = bool(TRUNCATED_PROSE_RE.search(v) and (len(v) > 35 or len(last) == 1))
            if truncated:
                add("narrow_metadata_field_contains_truncated_protocol_prose", i, header=h, value=v)

    # 6. Repository-wide project metadata collapse checks. These are contradiction detectors only.
    if project_json and project_json.is_file():
        try:
            project = json.loads(project_json.read_text(encoding="utf-8"))
        except Exception as exc:
            result.warnings.append(f"project_metadata_unreadable:{exc}")
        else:
            project_orgs = _project_values(project, "organisms")
            candidate_orgs = sorted({_norm(r.get("characteristics[organism]", "")) for r in rows if _concrete(r.get("characteristics[organism]", ""))}, key=str.lower)
            if len(project_orgs) > 1 and len(candidate_orgs) == 1:
                add("multiorganism_project_collapsed_to_single_candidate_organism", None,
                    project_organisms=project_orgs, candidate_organisms=candidate_orgs)

            project_parts = _project_values(project, "organismParts")
            candidate_parts = sorted({_norm(r.get("characteristics[organism part]", "")) for r in rows if _concrete(r.get("characteristics[organism part]", ""))}, key=str.lower)
            if len(project_parts) > 1 and len(candidate_parts) == 1:
                # PRIDE organismParts occasionally contains cell-line/cell-type concepts, so this is
                # useful evidence of heterogeneity but not sufficiently reliable to force a candidate
                # organism-part value. Keep it diagnostic rather than encouraging category copying.
                result.warnings.append("multi_organism_part_project_collapsed_to_single_candidate_part")
                result.details.append({
                    "code": "multi_organism_part_project_collapsed_to_single_candidate_part",
                    "row": None,
                    "project_organism_parts": project_parts,
                    "candidate_organism_parts": candidate_parts,
                    "severity": "warning",
                })

    result.blockers = list(dict.fromkeys(result.blockers))
    result.warnings = list(dict.fromkeys(result.warnings))
    return result


def self_test() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        p = td / "x.tsv"
        p.write_text(
            "source name\tcharacteristics[organism]\tcharacteristics[organism part]\tcharacteristics[individual]\tcharacteristics[cell line]\tcharacteristics[cell type]\tcharacteristics[sample type]\tcharacteristics[cells per well]\tcomment[data file]\tcomment[proteomics data acquisition method]\tcomment[nanopots chip version]\n"
            "x\tHomo sapiens\tEmbryo\tEmbryo\tHeLa\tEarly embryonic cell\tstudy sample\t1\tfoo_DDAtop20.raw\tNT=Data-independent acquisition;AC=PRIDE:0000450\tnanoPOTS chip[2] (Figure S2) and then prepared f\n"
        )
        project = td / "project.json"
        project.write_text(json.dumps({"organisms": [{"name":"Homo sapiens"},{"name":"Xenopus laevis"}]}))
        r = analyze(p, project)
        text = " ".join(r.blockers)
        assert "individual_duplicates_nonindividual_semantic_field" in text
        assert "explicit_dda_conflicts" in text
        assert "truncated_protocol_prose" in text
        assert "sample_type_study_sample_not_currently_validator_backed" in text
        assert "multiorganism_project_collapsed" in text
    print("sdrf_scientific_guard self-test: PASS")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("sdrf", nargs="?")
    ap.add_argument("--project-json")
    ap.add_argument("--self-test", action="store_true")
    ns = ap.parse_args()
    if ns.self_test:
        self_test()
    else:
        if not ns.sdrf:
            ap.error("sdrf is required unless --self-test")
        r = analyze(Path(ns.sdrf), Path(ns.project_json) if ns.project_json else None)
        print(json.dumps(asdict(r), indent=2, sort_keys=True))
        raise SystemExit(1 if r.blockers else 0)
