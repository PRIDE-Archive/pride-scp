#!/usr/bin/env python3
"""Final bounded release20 Repair4 v4 cleavage-agent correction.

This lane starts only from the exact Repair4-v3 candidates that passed deterministic
readiness but were rejected by fresh exact-hash review for incomplete digestion chemistry.
It changes only `comment[cleavage agent details]` for PXD019515 and PXD019958.

Repeated SDRF columns are preserved positionally.  The existing Trypsin value is retained
and a second source-supported Lys-C value is added/populated; repeated-column order is not
used to encode temporal digestion order.
"""
from __future__ import annotations

import argparse
import csv
import json
import tempfile
from pathlib import Path

from repair_release20_repair4_sdrfs import (
    analyze_guard,
    ensure_repeated_columns,
    indices,
    read_sdrf,
    set_repeated_values,
    sha256,
    write_sdrf,
)

VERSION = "pride-scp-release20-repair4-v4-cleavage-agent-completeness"

EXPECTED_INPUT_SHA256 = {
    "PXD019515": "37e14990aa7efb213a0ffb716fdd20c9ebbf0baae209bd73bf2e77f36460ac17",
    "PXD019958": "d1931cedb30491fa7710b689b2856ca262cefe7b4b4a0b304fb15073b8d35e6d",
}

TRYPSIN = "NT=Trypsin;AC=MS:1001251"
LYS_C = "NT=Lys-C;AC=MS:1001309"
TARGETS = tuple(sorted(EXPECTED_INPUT_SHA256))

EVIDENCE = {
    "PXD019515": [
        "Repair4-v3 exact-hash independent review dated 2026-09-13",
        "Cong et al., Chemical Science 2021, DOI 10.1039/D0SC03636F",
        "nanoPOTS workflow uses both Lys-C and Trypsin digestion; preserve both enzymes",
    ],
    "PXD019958": [
        "Repair4-v3 exact-hash independent review dated 2026-09-13",
        "Lamanna et al., Nature Communications 2020, DOI 10.1038/s41467-020-19394-5",
        "paper explicitly reports Lys-C digestion followed by Trypsin digestion",
    ],
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
            f"{accession}: expected exactly one input under {root}; "
            f"found {[str(p) for p in found]}"
        )
    return found[0]


def repair_cleavage_agents(
    headers: list[str], rows: list[list[str]], changes: list[dict[str, object]]
) -> None:
    ensure_repeated_columns(
        headers,
        rows,
        "comment[cleavage agent details]",
        2,
        changes,
        "independent exact-hash review established two source-supported digestion enzymes; "
        "insert the missing repeated SDRF column without collapsing duplicate headers",
    )
    cleavage_columns = indices(headers, "comment[cleavage agent details]")
    if len(cleavage_columns) != 2:
        raise ValueError(
            "final cleavage repair requires exactly two cleavage-agent columns after normalization; "
            f"found {len(cleavage_columns)}"
        )
    for row in rows:
        # Preserve the already-correct Trypsin representation in the first slot and replace/add only
        # the missing second enzyme. Repeated-column order is not used as a temporal workflow model.
        set_repeated_values(
            row,
            headers,
            "comment[cleavage agent details]",
            [TRYPSIN, LYS_C],
            changes,
            "fresh exact-hash review: digestion used both Trypsin and Lys-C; retain Trypsin and add Lys-C",
            exact_count=2,
        )


def run_repair(
    input_root: Path, repo: Path, output: Path, selected: list[str]
) -> list[tuple[str, str, str, int]]:
    summary: list[tuple[str, str, str, int]] = []
    for accession in selected:
        src = input_path(input_root, accession)
        old_sha = sha256(src)
        expected = EXPECTED_INPUT_SHA256[accession]
        if old_sha != expected:
            raise SystemExit(
                f"{accession}: input SHA mismatch: expected {expected}, got {old_sha}; "
                "Repair4-v4 must start from the exact reviewed Repair4-v3 candidate"
            )
        headers, rows = read_sdrf(src)
        if not headers or not rows:
            raise SystemExit(f"{accession}: empty SDRF input")

        before_headers = list(headers)
        before_rows = [list(row) for row in rows]
        changes: list[dict[str, object]] = []
        repair_cleavage_agents(headers, rows, changes)

        # Fail closed if any pre-existing non-cleavage field changed. A newly inserted cleavage
        # column is allowed, but every original column outside the cleavage field must be byte-value
        # identical row-for-row.
        for old_idx, header in enumerate(before_headers):
            if header == "comment[cleavage agent details]":
                continue
            new_positions = [i for i, value in enumerate(headers) if value == header]
            old_positions = [i for i, value in enumerate(before_headers) if value == header]
            occurrence = old_positions.index(old_idx)
            if occurrence >= len(new_positions):
                raise SystemExit(f"{accession}: non-cleavage column disappeared: {header}")
            new_idx = new_positions[occurrence]
            for row_no, (old_row, new_row) in enumerate(zip(before_rows, rows), start=1):
                if old_row[old_idx] != new_row[new_idx]:
                    raise SystemExit(
                        f"{accession}: non-cleavage field changed at row {row_no}: {header}"
                    )

        dst = output / "candidates" / accession / f"{accession}.sdrf.tsv"
        write_sdrf(dst, headers, rows)
        new_sha = sha256(dst)
        if new_sha == old_sha:
            raise SystemExit(f"{accession}: cleavage repair made no byte change")

        project = repo / "data" / "snapshot" / "projects" / f"{accession}.json"
        guard = analyze_guard(dst, project if project.is_file() else None)
        if guard.blockers:
            raise SystemExit(
                f"{accession}: repaired candidate fails scientific guard: {guard.blockers}"
            )

        changed_fields = sorted(
            {str(change.get("field") or "") for change in changes if change.get("field")}
        )
        if changed_fields != ["comment[cleavage agent details]"]:
            raise SystemExit(
                f"{accession}: v4 changed fields outside cleavage-agent scope: {changed_fields}"
            )

        manifest = {
            "repair_version": VERSION,
            "accession": accession,
            "input_path": str(src),
            "input_sha256": old_sha,
            "repaired_sha256": new_sha,
            "changes": changes,
            "changed_fields": changed_fields,
            "evidence": EVIDENCE[accession],
            "scientific_guard": {
                "blockers": guard.blockers,
                "warnings": guard.warnings,
                "details": guard.details,
            },
            "repair_scope": "cleavage-agent completeness only",
            "non_cleavage_fields_preserved": True,
            "compatibility_architecture_changed": False,
            "pxd054066_approved_hash_touched": False,
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
    base_headers = [
        "source name",
        "characteristics[organism]",
        "characteristics[sample type]",
        "characteristics[single cell isolation protocol]",
        "characteristics[cell identifier]",
        "characteristics[cells per well]",
        "comment[data file]",
        "comment[cleavage agent details]",
        "comment[modification parameters]",
    ]
    base_row = [
        "sample",
        "Homo sapiens",
        "single cell",
        "manual aspiration",
        "cell-1",
        "1",
        "sample.raw",
        TRYPSIN,
        "NT=Oxidation;AC=UNIMOD:35;TA=M;MT=Variable",
    ]

    # PXD019515-shaped legacy layout: one cleavage column. Add exactly one repeated cleavage column
    # and keep all other values unchanged.
    headers = list(base_headers)
    rows = [list(base_row)]
    changes: list[dict[str, object]] = []
    repair_cleavage_agents(headers, rows, changes)
    assert len(indices(headers, "comment[cleavage agent details]")) == 2
    assert [rows[0][i] for i in indices(headers, "comment[cleavage agent details]")] == [
        TRYPSIN,
        LYS_C,
    ]
    assert rows[0][headers.index("comment[modification parameters]")] == base_row[-1]
    assert any(change.get("scope") == "schema" for change in changes)

    # PXD019958-shaped layout: two cleavage columns, where only the second scientific value changes.
    headers2 = list(base_headers)
    insert_at = headers2.index("comment[cleavage agent details]") + 1
    headers2.insert(insert_at, "comment[cleavage agent details]")
    row2 = list(base_row)
    row2.insert(insert_at, "not applicable")
    rows2 = [row2]
    changes2: list[dict[str, object]] = []
    repair_cleavage_agents(headers2, rows2, changes2)
    assert [rows2[0][i] for i in indices(headers2, "comment[cleavage agent details]")] == [
        TRYPSIN,
        LYS_C,
    ]
    assert not any(change.get("scope") == "schema" for change in changes2)
    cell_changes = [c for c in changes2 if c.get("data_file") == "sample.raw"]
    assert len(cell_changes) == 1
    assert cell_changes[0]["old"] == "not applicable"
    assert cell_changes[0]["new"] == LYS_C

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "roundtrip.tsv"
        write_sdrf(p, headers, rows)
        h2, r2 = read_sdrf(p)
        assert h2 == headers
        assert r2 == rows
        assert len(indices(h2, "comment[cleavage agent details]")) == 2

    print("repair_release20_repair4_v4_cleavage_agents self-test: PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-root", type=Path)
    ap.add_argument("--repo", type=Path)
    ap.add_argument("--output", type=Path)
    ap.add_argument("--accession", action="append", choices=list(TARGETS))
    ap.add_argument("--self-test", action="store_true")
    ns = ap.parse_args()
    if ns.self_test:
        self_test()
        return
    if not ns.input_root or not ns.repo or not ns.output:
        ap.error("--input-root, --repo and --output are required unless --self-test")
    selected = ns.accession or list(TARGETS)
    summary = run_repair(ns.input_root, ns.repo, ns.output, selected)
    for row in summary:
        print("\t".join(map(str, row)))
    print(f"summary={ns.output / 'repair_summary.tsv'}")


if __name__ == "__main__":
    main()
