#!/usr/bin/env python3
"""Bounded release20 repair4 lane for exact-hash-rejected/reviewed SDRFs.

This lane starts from the latest scientifically reviewed candidates, not from the original release20
files.  It deliberately excludes PXD062702 because that v5 projected hash is already independently
approved and must remain immutable.

Unlike the older release20 repair helper, this implementation preserves duplicate SDRF columns by
position.  Repeated headers are meaningful in SDRF (for example multiple cleavage-agent or
modification columns), so DictReader/DictWriter must not be used for scientific repair.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Callable

from sdrf_scientific_guard import analyze as analyze_guard

VERSION = "pride-scp-release20-repair4-v2"

# Inputs are intentionally bound to the exact latest reviewed artifacts.  PXD019515/PXD019958 use
# the v5 repaired *source candidates*; PXD054066 uses the exact currently submitted PR artifact.
EXPECTED_INPUT_SHA256 = {
    "PXD019515": "4d3467559b43f093fae55ace09d75c02c382618b8e1c34e6df28e9790fdf1a64",
    "PXD019958": "ca8c13f56e7fdbe802783c4b1f2c761f90228d6118495e85524a70573b8981af",
    "PXD054066": "52a5c3f27b8a4c45fc1da6b6ec236de336cb0247e0c88062fbd16fec0768f4fe",
}

EVIDENCE = {
    "PXD019515": [
        "https://www.ebi.ac.uk/pride/archive/projects/PXD019515",
        "https://doi.org/10.1039/D0SC03636F",
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC8178986/",
        "v5 exact-hash independent review",
    ],
    "PXD019958": [
        "https://www.ebi.ac.uk/pride/archive/projects/PXD019958",
        "https://doi.org/10.1038/s41467-020-19394-5",
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC7658233/",
        "v5 exact-hash independent review",
    ],
    "PXD054066": [
        "https://www.ebi.ac.uk/pride/archive/projects/PXD054066",
        "https://github.com/bigbio/sdrf-annotated-datasets/pull/472",
        "Qodo source-level review on PR #472",
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC11903336/",
    ],
}

TRYPSIN = "NT=Trypsin;AC=MS:1001251"
MODIFICATIONS = [
    "NT=Oxidation;AC=UNIMOD:35;TA=M;MT=Variable",
    "NT=Acetyl;AC=UNIMOD:1;PP=Protein N-term;MT=Variable",
    "NT=Carbamidomethyl;AC=UNIMOD:4;TA=C;MT=Fixed",
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_sdrf(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.reader(fh, delimiter="\t")
        try:
            headers = next(reader)
        except StopIteration:
            return [], []
        rows: list[list[str]] = []
        for record in reader:
            if len(record) < len(headers):
                record += [""] * (len(headers) - len(record))
            if len(record) != len(headers):
                raise ValueError(
                    f"{path}: row has {len(record)} columns but header has {len(headers)}"
                )
            if any(value.strip() for value in record):
                rows.append(record)
    return headers, rows


def write_sdrf(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)


def indices(headers: list[str], header: str) -> list[int]:
    return [i for i, value in enumerate(headers) if value == header]


def first_value(headers: list[str], row: list[str], header: str) -> str:
    idx = indices(headers, header)
    return row[idx[0]] if idx else ""


def set_index(
    row: list[str],
    headers: list[str],
    idx: int,
    value: str,
    changes: list[dict[str, object]],
    reason: str,
) -> None:
    old = row[idx]
    if old == value:
        return
    header = headers[idx]
    occurrence = indices(headers, header).index(idx) + 1
    row[idx] = value
    changes.append(
        {
            "data_file": first_value(headers, row, "comment[data file]"),
            "field": header,
            "occurrence": occurrence,
            "old": old,
            "new": value,
            "reason": reason,
        }
    )


def set_all(
    row: list[str],
    headers: list[str],
    header: str,
    value: str,
    changes: list[dict[str, object]],
    reason: str,
    *,
    required: bool = True,
) -> None:
    found = indices(headers, header)
    if required and not found:
        raise ValueError(f"required column missing: {header}")
    for idx in found:
        set_index(row, headers, idx, value, changes, reason)


def ensure_repeated_columns(
    headers: list[str],
    rows: list[list[str]],
    header: str,
    minimum_count: int,
    changes: list[dict[str, object]],
    reason: str,
) -> None:
    """Ensure a repeated SDRF field has enough positional columns without collapsing duplicates.

    Missing repeated columns are inserted immediately after the final existing occurrence so the
    surrounding SDRF column order is preserved.  This is a representation repair only: inserted
    cells start empty and must be populated by the caller from source-grounded values.
    """
    found = indices(headers, header)
    if not found:
        raise ValueError(f"required repeated column missing: {header}")
    while len(found) < minimum_count:
        insert_at = found[-1] + 1
        headers.insert(insert_at, header)
        for row in rows:
            row.insert(insert_at, "")
        occurrence = len(found) + 1
        changes.append(
            {
                "scope": "schema",
                "data_file": "",
                "field": header,
                "occurrence": occurrence,
                "old": "<column absent>",
                "new": "<repeated column inserted>",
                "reason": reason,
            }
        )
        found = indices(headers, header)


def set_repeated_values(
    row: list[str],
    headers: list[str],
    header: str,
    values: list[str],
    changes: list[dict[str, object]],
    reason: str,
    *,
    fill: str = "not applicable",
    exact_count: int | None = None,
) -> None:
    found = indices(headers, header)
    if exact_count is not None and len(found) != exact_count:
        raise ValueError(
            f"{header}: expected {exact_count} repeated columns, found {len(found)}"
        )
    if len(found) < len(values):
        raise ValueError(
            f"{header}: need at least {len(values)} repeated columns, found {len(found)}"
        )
    target = values + [fill] * (len(found) - len(values))
    for idx, value in zip(found, target):
        set_index(row, headers, idx, value, changes, reason)


def repair_019515(headers: list[str], rows: list[list[str]], changes: list[dict[str, object]]) -> None:
    ensure_repeated_columns(
        headers,
        rows,
        "comment[modification parameters]",
        len(MODIFICATIONS),
        changes,
        "source supports three distinct modification parameters; add the missing repeated SDRF column rather than collapsing or overwriting duplicate headers",
    )
    for row in rows:
        set_all(
            row,
            headers,
            "comment[precursor mass tolerance]",
            "not available",
            changes,
            "publication reports precursor mass tolerance <5 ppm, but BigBio ms-proteomics accepts only an exact numeric value plus unit; do not invent the boundary or false precision",
        )
        set_repeated_values(
            row,
            headers,
            "comment[cleavage agent details]",
            [TRYPSIN],
            changes,
            "publication describes tryptic peptides/semi-tryptic search; no second cleavage agent is source-supported",
        )
        set_repeated_values(
            row,
            headers,
            "comment[modification parameters]",
            MODIFICATIONS,
            changes,
            "publication: variable methionine oxidation, variable protein N-terminal acetylation, fixed cysteine carbamidomethylation",
        )


def repair_019958(headers: list[str], rows: list[list[str]], changes: list[dict[str, object]]) -> None:
    ensure_repeated_columns(
        headers,
        rows,
        "comment[modification parameters]",
        len(MODIFICATIONS),
        changes,
        "source supports three distinct modification parameters; add the missing repeated SDRF column rather than collapsing or overwriting duplicate headers",
    )
    for row in rows:
        set_repeated_values(
            row,
            headers,
            "comment[cleavage agent details]",
            [TRYPSIN],
            changes,
            "publication explicitly states the specific proteolytic enzyme was trypsin; Lys-C is unsupported",
        )
        set_repeated_values(
            row,
            headers,
            "comment[modification parameters]",
            MODIFICATIONS,
            changes,
            "publication: variable methionine oxidation, variable N-terminal acetylation, fixed cysteine carbamidomethylation",
        )


def is_zero_cell_control(headers: list[str], row: list[str]) -> bool:
    sample_type = first_value(headers, row, "characteristics[sample type]").strip().lower()
    cells = first_value(headers, row, "characteristics[cells per well]").strip().lower()
    cell_id = first_value(headers, row, "characteristics[cell identifier]").strip().lower()
    source = first_value(headers, row, "source name").strip().lower()
    data_file = first_value(headers, row, "comment[data file]").strip().lower()
    return (
        sample_type in {"empty", "blank", "negative control"}
        or cells == "0"
        or cell_id == "empty"
        or "blank" in source
        or "blank" in data_file
    )


def repair_054066(headers: list[str], rows: list[list[str]], changes: list[dict[str, object]]) -> None:
    blank_count = 0
    for row in rows:
        # The reviewed PR copied three May-22 HeLa run identifiers into every row as a preparation
        # batch, including later hFF acquisitions and blanks.  The source audit does not close a
        # row-specific batch mapping; filename tokens such as "Batch1" are not allowed to invent it.
        set_all(
            row,
            headers,
            "comment[sample preparation batch]",
            "not available",
            changes,
            "PR #472 review: copied HeLa run identifiers are not preparation-batch provenance; no source-closed per-row batch mapping is available",
        )
        if not is_zero_cell_control(headers, row):
            continue
        blank_count += 1
        # Clear only cell-specific biological identity that is impossible for a zero-cell control.
        # Organism/organism-part study context and the technical isolation workflow may still be
        # meaningful for a process blank, so do not erase them without an independent source finding.
        for header in (
            "characteristics[individual]",
            "characteristics[cell type]",
            "characteristics[cell line]",
            "characteristics[cellosaurus accession]",
            "characteristics[cellosaurus name]",
            "characteristics[material type]",
        ):
            set_all(
                row,
                headers,
                header,
                "not applicable",
                changes,
                "zero-cell blank cannot carry a concrete cell/individual/material identity",
                required=False,
            )
        # The single-cell template has an explicit reserved representation for a zero-cell control.
        set_all(
            row,
            headers,
            "characteristics[cell identifier]",
            "empty",
            changes,
            "zero-cell blank control",
            required=False,
        )
    if blank_count != 4:
        raise ValueError(
            f"PXD054066: expected the four independently reviewed zero-cell blank controls, found {blank_count}; refuse broad repair"
        )


REPAIRS: dict[str, Callable[[list[str], list[list[str]], list[dict[str, object]]], None]] = {
    "PXD019515": repair_019515,
    "PXD019958": repair_019958,
    "PXD054066": repair_054066,
}


def input_path(root: Path, accession: str) -> Path:
    candidates = [
        root / accession / f"{accession}.sdrf.tsv",
        root / "candidates" / accession / f"{accession}.sdrf.tsv",
        root / "datasets" / accession / f"{accession}.sdrf.tsv",
    ]
    found = [path for path in candidates if path.is_file()]
    if len(found) != 1:
        raise SystemExit(
            f"{accession}: expected exactly one input under {root}; found {[str(p) for p in found]}"
        )
    return found[0]


def run_repair(input_root: Path, repo: Path, output: Path, selected: list[str]) -> list[tuple[str, str, str, int]]:
    summary: list[tuple[str, str, str, int]] = []
    for accession in selected:
        src = input_path(input_root, accession)
        old_sha = sha256(src)
        expected = EXPECTED_INPUT_SHA256[accession]
        if old_sha != expected:
            raise SystemExit(
                f"{accession}: input SHA mismatch: expected {expected}, got {old_sha}; repair4 must start from the reviewed artifact"
            )
        headers, rows = read_sdrf(src)
        if not headers or not rows:
            raise SystemExit(f"{accession}: empty SDRF input")
        changes: list[dict[str, object]] = []
        REPAIRS[accession](headers, rows, changes)
        dst = output / "candidates" / accession / f"{accession}.sdrf.tsv"
        write_sdrf(dst, headers, rows)
        new_sha = sha256(dst)
        if new_sha == old_sha:
            raise SystemExit(f"{accession}: bounded repair made no byte change")

        project = repo / "data" / "snapshot" / "projects" / f"{accession}.json"
        guard = analyze_guard(dst, project if project.is_file() else None)
        if guard.blockers:
            raise SystemExit(
                f"{accession}: repaired candidate still fails scientific guard: {guard.blockers}"
            )
        manifest = {
            "repair_version": VERSION,
            "accession": accession,
            "input_path": str(src),
            "input_sha256": old_sha,
            "repaired_sha256": new_sha,
            "changes": changes,
            "evidence": EVIDENCE[accession],
            "scientific_guard": {
                "blockers": guard.blockers,
                "warnings": guard.warnings,
                "details": guard.details,
            },
            "compatibility_architecture_changed": False,
            "pxd062702_approved_hash_touched": False,
            "requires_new_hash_bound_independent_review": True,
        }
        mp = output / "manifests" / f"{accession}.repair.json"
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        summary.append((accession, old_sha, new_sha, len(changes)))

    sp = output / "repair_summary.tsv"
    sp.parent.mkdir(parents=True, exist_ok=True)
    with sp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        writer.writerow(["accession", "input_sha256", "repaired_sha256", "changes"])
        writer.writerows(summary)
    return summary


def self_test() -> None:
    # Regression: reviewed legacy SDRFs may have only two repeated modification columns even though
    # the source supports three distinct modifications.  Repair must add the missing repeated column
    # positionally, preserve duplicate headers, and never collapse them through dict-based CSV I/O.
    def fixture() -> tuple[list[str], list[str]]:
        headers = [
            "source name",
            "characteristics[organism]",
            "characteristics[organism part]",
            "characteristics[individual]",
            "characteristics[cell type]",
            "characteristics[cell line]",
            "characteristics[material type]",
            "characteristics[sample type]",
            "characteristics[single cell isolation protocol]",
            "characteristics[cell identifier]",
            "characteristics[cells per well]",
            "comment[data file]",
            "comment[sample preparation batch]",
            "comment[cleavage agent details]",
            "comment[cleavage agent details]",
            "comment[modification parameters]",
            "comment[modification parameters]",
            "comment[precursor mass tolerance]",
        ]
        protein = [
            "sample",
            "Homo sapiens",
            "cell culture",
            "cell",
            "cell culture",
            "HeLa",
            "cell",
            "single cell",
            "cellenONE",
            "sample",
            "1",
            "sample.raw",
            "bad batch",
            "NT=Lys-C;AC=MS:1001309",
            "NT=Lys-C;AC=MS:1001309",
            "bad mod",
            "bad mod",
            "5 ppm",
        ]
        return headers, protein

    headers, protein = fixture()
    changes: list[dict[str, object]] = []
    rows = [protein.copy()]
    assert len(indices(headers, "comment[modification parameters]")) == 2
    repair_019515(headers, rows, changes)
    assert len(indices(headers, "comment[modification parameters]")) == 3
    assert any(change.get("scope") == "schema" for change in changes)
    assert [rows[0][i] for i in indices(headers, "comment[cleavage agent details]")] == [
        TRYPSIN,
        "not applicable",
    ]
    assert [rows[0][i] for i in indices(headers, "comment[modification parameters]")] == MODIFICATIONS
    assert first_value(headers, rows[0], "comment[precursor mass tolerance]") == "not available"

    headers_019958, protein_019958 = fixture()
    rows_019958 = [protein_019958]
    changes_019958: list[dict[str, object]] = []
    repair_019958(headers_019958, rows_019958, changes_019958)
    assert len(indices(headers_019958, "comment[modification parameters]")) == 3
    assert any(change.get("scope") == "schema" for change in changes_019958)
    assert [
        rows_019958[0][i]
        for i in indices(headers_019958, "comment[cleavage agent details]")
    ] == [TRYPSIN, "not applicable"]
    assert [
        rows_019958[0][i]
        for i in indices(headers_019958, "comment[modification parameters]")
    ] == MODIFICATIONS

    headers_054066, protein_054066 = fixture()
    blank = protein_054066.copy()
    blank[0] = "Blank_04"
    blank[7] = "empty"
    blank[9] = "empty"
    blank[10] = "0"
    blank[11] = "Blank_04.raw"
    rows_054066 = [blank.copy(), blank.copy(), blank.copy(), blank.copy(), protein_054066.copy()]
    repair_054066(headers_054066, rows_054066, [])
    for row in rows_054066:
        assert first_value(headers_054066, row, "comment[sample preparation batch]") == "not available"
    for row in rows_054066[:4]:
        for header in (
            "characteristics[individual]",
            "characteristics[cell type]",
            "characteristics[cell line]",
            "characteristics[material type]",
        ):
            assert first_value(headers_054066, row, header) == "not applicable"
        assert first_value(headers_054066, row, "characteristics[organism part]") == "cell culture"
        assert (
            first_value(headers_054066, row, "characteristics[single cell isolation protocol]")
            == "cellenONE"
        )
        assert first_value(headers_054066, row, "characteristics[cell identifier]") == "empty"
    assert first_value(headers_054066, rows_054066[4], "characteristics[cell line]") == "HeLa"

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "roundtrip.tsv"
        write_sdrf(p, headers, rows)
        h2, r2 = read_sdrf(p)
        assert h2 == headers
        assert r2 == rows
        assert len(indices(h2, "comment[cleavage agent details]")) == 2
        assert len(indices(h2, "comment[modification parameters]")) == 3
    print("repair_release20_repair4_sdrfs self-test: PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-root", type=Path, help="staged reviewed inputs; one exact file per accession")
    ap.add_argument("--repo", type=Path, help="PRIDE-SCP repo root (for optional snapshot project metadata)")
    ap.add_argument("--output", type=Path)
    ap.add_argument("--accession", action="append", choices=sorted(REPAIRS))
    ap.add_argument("--self-test", action="store_true")
    ns = ap.parse_args()
    if ns.self_test:
        self_test()
        return
    if not ns.input_root or not ns.repo or not ns.output:
        ap.error("--input-root, --repo and --output are required unless --self-test")
    selected = ns.accession or sorted(REPAIRS)
    summary = run_repair(ns.input_root, ns.repo, ns.output, selected)
    for row in summary:
        print("\t".join(map(str, row)))
    print(f"summary={ns.output / 'repair_summary.tsv'}")


if __name__ == "__main__":
    main()
