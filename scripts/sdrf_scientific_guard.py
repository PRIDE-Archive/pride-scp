#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

VERSION = "pride-scp-sdrf-scientific-guard-v0.4"
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


class DuplicateSafeRow:
    """Positional SDRF row that preserves repeated headers.

    ``get`` returns the first concrete value (or first literal when all are reserved), while
    ``values`` exposes every repeated-column value for checks that need full cardinality.
    """
    def __init__(self, headers: list[str], values: list[str]):
        self._values: dict[str, list[str]] = {}
        for idx, header in enumerate(headers):
            self._values.setdefault(header, []).append(str(values[idx] if idx < len(values) else ""))

    def values(self, header: str) -> list[str]:
        return list(self._values.get(header, []))

    def get(self, header: str, default: str = "") -> str:
        values = self._values.get(header, [])
        if not values:
            return default
        for value in values:
            if _concrete(value):
                return value
        return values[0]


def _read(path: Path) -> tuple[list[str], list[DuplicateSafeRow]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh, delimiter="\t")
        table = list(reader)
    if not table:
        return [], []
    headers = [str(x or "") for x in table[0]]
    rows = [DuplicateSafeRow(headers, [str(v or "") for v in row]) for row in table[1:]]
    return headers, rows


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

    def warn(code: str, row_idx: int | None = None, **detail: Any) -> None:
        token = code if row_idx is None else f"{code}:row_{row_idx}"
        result.warnings.append(token)
        result.details.append({"code": code, "row": row_idx, "severity": "warning", **detail})

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
        # WWA (wide-window acquisition) is a DDA strategy with wider precursor isolation, not DIA.
        # Do not infer DIA merely from the word "wide". This is an explicit literature-backed
        # acquisition semantic, not an accession-specific filename heuristic.
        if re.search(r"(?:^|[_\-.])WWA(?:\d|[_\-.]|$)", fn, re.I) and "independent" in acq:
            add("wide_window_acquisition_mislabeled_as_dia", i, data_file=fn, acquisition=acq)

    # 3. Empty/zero-cell controls must not carry concrete cell/tissue identity as if biological
    #    material were present. Keep organism/study context separate from cell-specific identity.
    for i, row in enumerate(rows, 2):
        role = _low(row.get("characteristics[sample type]", ""))
        cells = _low(row.get("characteristics[cells per well]", ""))
        source = _low(row.get("source name", ""))
        is_empty = role in {"empty", "blank", "negative control"} or cells == "0" or "blank" in source
        if not is_empty:
            continue
        bad = []
        for h in (
            "characteristics[individual]",
            "characteristics[cell type]",
            "characteristics[cell line]",
            "characteristics[cellosaurus accession]",
            "characteristics[cellosaurus name]",
        ):
            if h in headers and _concrete(row.get(h, "")):
                bad.append(h)
        contextual = [
            h for h in (
                "characteristics[organism part]",
                "characteristics[disease]",
                "characteristics[developmental stage]",
                "characteristics[sex]",
            )
            if h in headers and _concrete(row.get(h, ""))
        ]
        if contextual:
            warn("empty_control_carries_contextual_biological_metadata", i, headers=contextual)
        material = _low(row.get("characteristics[material type]", ""))
        if material in {"cell", "cell line", "tissue", "biofluid", "primary cell"}:
            bad.append("characteristics[material type]")
        cell_id = _low(row.get("characteristics[cell identifier]", ""))
        if cell_id and cell_id not in RESERVED and cell_id != "empty":
            bad.append("characteristics[cell identifier]")
        if bad:
            exact_zero_control = (
                role in {"empty", "blank", "negative control"}
                and cells == "0"
                and cell_id == "empty"
            )
            if exact_zero_control:
                warn("explicit_zero_cell_control_identity_requires_projection_normalization", i, headers=bad)
            else:
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
                warn(
                    "sample_type_study_sample_requires_projection_normalization",
                    i,
                    value=row.get("characteristics[sample type]", ""),
                    remediation="publication projection uses 'not available' until the maintained validator exposes this term",
                )

    # 5. Instrument/isolation metadata must be locally compatible with each row.
    for i, row in enumerate(rows, 2):
        lcm_model = _norm(row.get("comment[lcm microscope model]", ""))
        isolation = _low(row.get("characteristics[single cell isolation protocol]", ""))
        if _concrete(lcm_model) and not ("laser capture" in isolation or isolation == "lcm"):
            add("lcm_microscope_model_without_lcm_isolation", i,
                lcm_microscope_model=lcm_model, isolation_protocol=row.get("characteristics[single cell isolation protocol]", ""))

        instrument = _low(row.get("comment[instrument]", ""))
        dissociation = _low(row.get("comment[dissociation method]", ""))
        if "q exactive" in instrument and ("ms:1000133" in dissociation or dissociation == "cid"):
            add("q_exactive_row_uses_generic_cid_instead_of_hcd", i,
                instrument=row.get("comment[instrument]", ""), dissociation=row.get("comment[dissociation method]", ""))

    # 6. Narrow identifier/model/version fields must not contain obvious truncated protocol prose.
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

    # 7. Repeated proteomics chemistry columns are meaningful, but repeating the same concrete
    # chemistry value in multiple copies is almost always a serialization defect. Detect it before
    # independent review so repeated-header handling cannot silently collapse/duplicate chemistry.
    for i, row in enumerate(rows, 2):
        for header, code in (
            ("comment[cleavage agent details]", "duplicate_repeated_cleavage_agent"),
            ("comment[modification parameters]", "duplicate_repeated_modification_parameter"),
        ):
            concrete = [_norm(v) for v in row.values(header) if _concrete(v)]
            keys = [v.lower() for v in concrete]
            duplicates = sorted({v for v in keys if keys.count(v) > 1})
            if duplicates:
                add(code, i, header=header, duplicate_values=duplicates)

    # 8. Repository-wide project metadata collapse checks. These are contradiction detectors only.
    if project_json and project_json.is_file():
        try:
            project = json.loads(project_json.read_text(encoding="utf-8"))
        except Exception as exc:
            result.warnings.append(f"project_metadata_unreadable:{exc}")
        else:
            project_orgs = _project_values(project, "organisms")
            candidate_orgs = sorted({
                _norm(v) for r in rows for v in r.values("characteristics[organism]") if _concrete(v)
            }, key=str.lower)
            if len(project_orgs) > 1 and len(candidate_orgs) == 1:
                add("multiorganism_project_collapsed_to_single_candidate_organism", None,
                    project_organisms=project_orgs, candidate_organisms=candidate_orgs)

            project_parts = _project_values(project, "organismParts")
            candidate_parts = sorted({
                _norm(v) for r in rows for v in r.values("characteristics[organism part]") if _concrete(v)
            }, key=str.lower)
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
            "source name\tcharacteristics[organism]\tcharacteristics[organism part]\tcharacteristics[individual]\tcharacteristics[cell line]\tcharacteristics[cell type]\tcharacteristics[cellosaurus accession]\tcharacteristics[disease]\tcharacteristics[developmental stage]\tcharacteristics[sex]\tcharacteristics[material type]\tcharacteristics[sample type]\tcharacteristics[cells per well]\tcharacteristics[cell identifier]\tcharacteristics[single cell isolation protocol]\tcomment[data file]\tcomment[proteomics data acquisition method]\tcomment[instrument]\tcomment[dissociation method]\tcomment[lcm microscope model]\tcomment[nanopots chip version]\n"
            "x\tHomo sapiens\tEmbryo\tEmbryo\tHeLa\tEarly embryonic cell\tCVCL_0001\tcancer\tadult\tmale\tcell\tstudy sample\t1\tx1\tmanual aspiration\tfoo_DDAtop20.raw\tNT=Data-independent acquisition;AC=PRIDE:0000450\tNT=Q Exactive Plus;AC=MS:1002634\tNT=CID;AC=MS:1000133\tZeiss PALM MicroBeam\tnanoPOTS chip[2] (Figure S2) and then prepared f\n"
            "blank\tHomo sapiens\tbrain\tnot applicable\tHeLa\tnot applicable\tCVCL_0001\tcancer\tadult\tmale\tcell line\tempty\t0\tblank_1\tnot applicable\tfoo_WWA4.raw\tNT=Data-independent acquisition;AC=PRIDE:0000450\tNT=Orbitrap Fusion Lumos;AC=MS:1002732\tHCD\tnot applicable\tnot available\n"
        )
        project = td / "project.json"
        project.write_text(json.dumps({"organisms": [{"name":"Homo sapiens"},{"name":"Xenopus laevis"}]}))
        r = analyze(p, project)
        text = " ".join(r.blockers)
        assert "individual_duplicates_nonindividual_semantic_field" in text
        assert "explicit_dda_conflicts" in text
        assert "truncated_protocol_prose" in text
        assert "sample_type_study_sample_requires_projection_normalization" in " ".join(r.warnings)
        assert "multiorganism_project_collapsed" in text
        assert "q_exactive_row_uses_generic_cid_instead_of_hcd" in text
        assert "lcm_microscope_model_without_lcm_isolation" in text
        assert "empty_control_has_concrete_biological_identity" in text
        assert "wide_window_acquisition_mislabeled_as_dia" in text

        chem = td / "chem.tsv"
        chem.write_text(
            "source name\tcomment[cleavage agent details]\tcomment[cleavage agent details]\tcomment[modification parameters]\tcomment[modification parameters]\n"
            "x\tNT=Trypsin;AC=MS:1001251\tNT=Trypsin;AC=MS:1001251\tNT=Oxidation;AC=UNIMOD:35;TA=M;MT=Variable\tNT=Oxidation;AC=UNIMOD:35;TA=M;MT=Variable\n"
        )
        cr = analyze(chem)
        ctext = " ".join(cr.blockers)
        assert "duplicate_repeated_cleavage_agent" in ctext
        assert "duplicate_repeated_modification_parameter" in ctext

        zero = td / "zero.tsv"
        zero.write_text(
            "source name\tcharacteristics[sample type]\tcharacteristics[cells per well]\tcharacteristics[cell identifier]\tcharacteristics[individual]\tcharacteristics[cell type]\tcharacteristics[cell line]\tcharacteristics[material type]\n"
            "blank\tempty\t0\tempty\tdonor1\tHeLa\tHeLa\tcell\n"
        )
        zr = analyze(zero)
        assert not any("empty_control_has_concrete_biological_identity" in x for x in zr.blockers)
        assert any("explicit_zero_cell_control_identity_requires_projection_normalization" in x for x in zr.warnings)
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
