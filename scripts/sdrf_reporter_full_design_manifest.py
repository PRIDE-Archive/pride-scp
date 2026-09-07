#!/usr/bin/env python3
"""Build a source-grounded full-repository SDRF row manifest for PXD028040.

v0.4.5 extends the accepted v0.4.4 nine-row single-neuron manifest with the seven
remaining repository RAW acquisitions.  The additional rows are not inferred from
RAW filename words.  They are selected from the deposited experimental-design
workbook by explicit acquisition-date + SC run keys and by their workbook material
descriptions.

The workbook supports two bounded pre-single-neuron material classes:

* three 100-pg diluted whole-tissue digest measurements with no explicit reporter
  assignment in the workbook row; and
* four 100-pg diluted whole-tissue digest analyte measurements explicitly tagged
  with TMT128 and mixed with 10-ng TMT131-tagged tissue digest.

These seven rows are represented as non-single-cell ``study sample`` rows.  Their
source identifiers are deterministic aliases derived from the explicit workbook run
key, not claims of biological identity.  ``technical_replicate=1`` is a structural
single-measurement value for each unique source alias; no replicate grouping is
invented for these development/reference rows.

The nine single-neuron rows are delegated to the already accepted v0.4.4 parser and
retain the 3-neuron x 3-technical-replicate design.  No SDRF is written here; the
result is a provenance-rich TSV consumed by Rust through
``--explicit-row-mapping-manifest``.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import tempfile
from dataclasses import asdict
from pathlib import Path

from sdrf_reporter_design_manifest import (
    AUDITOR_VERSION as V044_AUDITOR_VERSION,
    MappingRow,
    derive_mapping as derive_single_neuron_mapping,
    raw_date_sc,
    single_neuron_section_rows,
)
from sdrf_reporter_design_semantic_audit import DesignRow, features, parse_xlsx_structured, row_ref
from sdrf_reporter_run_scope_audit import is_raw_file, load_reporter_contract, repository_files

AUDITOR_VERSION = "pride-scp-sdrf-full-design-manifest-v0.1"
WHOLE_TISSUE_RE = re.compile(r"(?i)\b100\s*pg\s+of\s+protein\s+digest\b.*\bdiluted\s+whole\s+tissue\b")
TMT128_RE = re.compile(r"(?i)\bTMT\s*[-_ ]?128\b")
TMT131_RE = re.compile(r"(?i)\bTMT\s*[-_ ]?131\b")

MANIFEST_FIELDS = [
    "accession",
    "raw_file",
    "source_name",
    "cell_identifier",
    "biological_replicate",
    "technical_replicate",
    "sample_type",
    "cells_per_well",
    "label",
    "carrier_channel",
    "reference_channel",
    "design_source",
    "design_ref",
    "mapping_key",
    "mapping_confidence",
]


def _safe_run_source(date: str, sc: str) -> str:
    return f"whole_tissue_digest_{date.replace('-', '_')}_{sc}"


def pre_single_neuron_rows(rows: list[DesignRow]) -> tuple[DesignRow, list[DesignRow]]:
    section, _ = single_neuron_section_rows(rows)
    selected: list[DesignRow] = []
    for row in rows:
        if row.sheet != section.sheet or row.row_number >= section.row_number:
            continue
        f = features(row.text)
        if len(f.dates) != 1 or len(f.sc_codes) != 1:
            continue
        if WHOLE_TISSUE_RE.search(row.text):
            selected.append(row)
    if len(selected) != 7:
        raise ValueError(
            f"expected exactly seven pre-single-neuron whole-tissue design rows; found {len(selected)}"
        )
    no_tmt = [r for r in selected if not TMT128_RE.search(r.text) and not TMT131_RE.search(r.text)]
    tmt = [r for r in selected if TMT128_RE.search(r.text) and TMT131_RE.search(r.text)]
    partial = [r for r in selected if r not in no_tmt and r not in tmt]
    if len(no_tmt) != 3 or len(tmt) != 4 or partial:
        raise ValueError(
            "unexpected pre-single-neuron design shape; expected 3 whole-tissue rows without explicit reporter assignments and 4 with explicit TMT128+TMT131 layout"
        )
    return section, selected


def derive_full_mapping(
    accession: str,
    design_source: str,
    rows: list[DesignRow],
    raws,
    reporter: dict[str, str],
) -> tuple[list[MappingRow], dict[str, object]]:
    single_rows, single_summary = derive_single_neuron_mapping(
        accession=accession,
        design_source=design_source,
        rows=rows,
        raws=raws,
        reporter=reporter,
    )
    section, development_rows = pre_single_neuron_rows(rows)

    raw_by_key: dict[tuple[str, str], list] = {}
    for raw in raws:
        key = raw_date_sc(raw)
        if key:
            raw_by_key.setdefault(key, []).append(raw)

    support_rows: list[MappingRow] = []
    support_classes: dict[str, str] = {}
    for row in development_rows:
        f = features(row.text)
        key = (f.dates[0], f.sc_codes[0])
        matches = raw_by_key.get(key, [])
        if len(matches) != 1:
            raise ValueError(
                f"{row_ref(row)} date+SC key {key[0]}+{key[1]} maps to {len(matches)} repository RAWs"
            )
        raw = matches[0]
        has_128 = bool(TMT128_RE.search(row.text))
        has_131 = bool(TMT131_RE.search(row.text))
        if has_128 != has_131:
            raise ValueError(f"{row_ref(row)} contains only one side of the expected TMT128/TMT131 layout")
        source_name = _safe_run_source(*key)
        support_rows.append(
            MappingRow(
                accession=accession,
                raw_file=raw.name,
                source_name=source_name,
                cell_identifier="not applicable",
                biological_replicate="not applicable",
                technical_replicate="1",
                sample_type="study sample",
                cells_per_well="not applicable",
                label="TMT128" if has_128 else "not available",
                carrier_channel="TMT131" if has_131 else "not applicable",
                reference_channel="not applicable",
                design_source=design_source,
                design_ref=row_ref(row),
                mapping_key="date_sc_run_key",
                mapping_confidence="high",
            )
        )
        support_classes[raw.name] = "whole_tissue_tmt128_tmt131_reference" if has_128 else "whole_tissue_reference"

    full = support_rows + single_rows
    raw_names = {r.name.lower() for r in raws}
    mapped_names = {m.raw_file.lower() for m in full}
    if len(full) != 16 or mapped_names != raw_names:
        missing = sorted(raw_names - mapped_names)
        extra = sorted(mapped_names - raw_names)
        raise ValueError(
            f"full repository manifest does not cover exactly the 16 RAW files: rows={len(full)} missing={missing} extra={extra}"
        )
    if len(mapped_names) != len(full):
        raise ValueError("full repository manifest contains duplicate RAW assignments")

    summary: dict[str, object] = {
        "auditor_version": AUDITOR_VERSION,
        "accession": accession,
        "status": "full_repository_explicit_mapping_manifest_ready",
        "non_generative": True,
        "reporter_contract_valid": True,
        "v044_single_neuron_parser": V044_AUDITOR_VERSION,
        "design_source": design_source,
        "single_neuron_section_ref": row_ref(section),
        "repository_raw_files": len(raws),
        "mapped_repository_raw_files": len(full),
        "single_neuron_rows": len(single_rows),
        "whole_tissue_reference_rows": sum(1 for x in support_classes.values() if x == "whole_tissue_reference"),
        "whole_tissue_tmt_reference_rows": sum(1 for x in support_classes.values() if x == "whole_tissue_tmt128_tmt131_reference"),
        "single_neuron_biological_samples": single_summary["biological_samples"],
        "single_neuron_technical_replicates_per_sample": single_summary["technical_replicates_per_sample"],
        "single_neuron_analytical_label": "TMT128",
        "single_neuron_carrier_channel": "TMT131",
        "repository_scope_complete": True,
        "filename_branch_words_used_for_membership": False,
        "support_row_semantics": {
            raw: cls for raw, cls in sorted(support_classes.items())
        },
        "notes": [
            "All 16 repository RAWs are mapped by one explicit workbook acquisition-date + SC key each.",
            "The seven pre-single-neuron rows are whole-tissue method-development/reference material, not biological single-neuron identities.",
            "No reporter label is invented for the three workbook rows without explicit TMT assignments; they retain 'not available'.",
            "The four pre-single-neuron TMT rows explicitly retain TMT128 analyte and TMT131 carrier from the workbook.",
            "technical_replicate=1 on the seven unique-source support rows is a structural single-measurement value and does not assert an unrecorded replicate grouping.",
            "The nine single-neuron rows retain the accepted DA-neuron identity and technical-replicate structure from v0.4.4.",
        ],
        "mapping_rows": [asdict(m) for m in full],
    }
    return full, summary


def write_manifest(path: Path, rows: list[MappingRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=MANIFEST_FIELDS, delimiter="\t")
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def audit(args: argparse.Namespace) -> dict[str, object]:
    accession = args.accession.strip().upper()
    repo = repository_files(json.loads(args.files_json.read_text(errors="replace")))
    raws = [x for x in repo if is_raw_file(x)]
    workbook_rows = parse_xlsx_structured(args.support_xlsx)
    reporter = load_reporter_contract(args.reporter_audit_tsv, accession)
    mapping, summary = derive_full_mapping(
        accession=accession,
        design_source=args.support_xlsx.name,
        rows=workbook_rows,
        raws=raws,
        reporter=reporter,
    )
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "explicit_row_mapping_manifest.tsv"
    write_manifest(manifest_path, mapping)
    summary["manifest"] = str(manifest_path)
    (args.output / "sdrf_reporter_full_design_manifest_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False)
    )
    return summary


def self_test() -> None:
    # Reuse the v0.4.4 helper's own data classes through the structured parser module.
    from sdrf_reporter_design_manifest import _cell
    from sdrf_reporter_design_semantic_audit import DesignRow
    from sdrf_reporter_run_scope_audit import RepoFile

    rows = []
    development = [
        (6, "2018-08-08_SC02", "100 pg of protein digest (diluted whole tissue)"),
        (7, "2018-08-08_SC03", "100 pg of protein digest (diluted whole tissue)"),
        (8, "2018-08-08_SC04", "100 pg of protein digest (diluted whole tissue)"),
        (9, "2018-08-15_SC02", "100 pg of protein digest (diluted whole tissue, tagged with TMT 128) with 10 ng of protein digest (diluted whole tissue tagged with TMT 131)"),
        (10, "2018-08-15_SC03", "100 pg of protein digest (diluted whole tissue, tagged with TMT 128) with 10 ng of protein digest (diluted whole tissue tagged with TMT 131)"),
        (11, "2018-08-15_SC04", "100 pg of protein digest (diluted whole tissue, tagged with TMT 128) with 10 ng of protein digest (diluted whole tissue tagged with TMT 131)"),
        (12, "2018-08-16_SC04", "100 pg of protein digest (diluted whole tissue, tagged with TMT 128) with 10 ng of protein digest (diluted whole tissue tagged with TMT 131)"),
    ]
    raws = []
    for row_no, key, text in development:
        rows.append(DesignRow("Sheet2", row_no, [_cell(f"A{row_no}", key), _cell(f"B{row_no}", text)]))
        raws.append(RepoFile(name=f"{key}_development.RAW", category="RAW", uri=f"ftp://example/{key}.RAW"))
    rows.append(DesignRow("Sheet2", 14, [_cell("A14", "Application for single neuron analysis")]))
    specs = [
        (15, "2018-08-27_SC02", 1, 1),
        (16, "2018-08-27_SC03", 1, 2),
        (17, "2018-08-27_SC04", 1, 3),
        (18, "2018-08-30_SC02", 2, 1),
        (19, "2018-09-04_SC05", 2, 2),
        (20, "2018-09-05_SC02", 2, 3),
        (21, "2018-09-05_SC03", 3, 1),
        (22, "2018-09-05_SC04", 3, 2),
        (23, "2018-09-06_SC01", 3, 3),
    ]
    for row_no, key, sample, tech in specs:
        rows.append(
            DesignRow(
                "Sheet2",
                row_no,
                [
                    _cell(f"A{row_no}", key),
                    _cell(f"B{row_no}", f"DA neuron #{sample} technical replicate measurement {tech}"),
                    _cell(f"C{row_no}", "~100 pg of neuron digest tagged with TMT 128 + ~10 ng of diluted tissue digest tagged with TMT 131"),
                ],
            )
        )
        raws.append(RepoFile(name=f"{key}_single.RAW", category="RAW", uri=f"ftp://example/{key}.RAW"))
    reporter = {
        "mapping_class": "single_analytical_channel_per_run",
        "confidence": "high",
        "single_cell_channels": "128",
        "carrier_channels": "131",
        "ambiguous_channels": "",
    }
    mapping, summary = derive_full_mapping("PXD028040", "design.xlsx", rows, raws, reporter)
    assert len(mapping) == 16
    assert summary["repository_scope_complete"] is True
    assert summary["whole_tissue_reference_rows"] == 3
    assert summary["whole_tissue_tmt_reference_rows"] == 4
    support = mapping[:7]
    assert all(m.sample_type == "study sample" for m in support)
    assert all(m.cell_identifier == "not applicable" for m in support)
    assert all(m.cells_per_well == "not applicable" for m in support)
    assert {m.label for m in support[:3]} == {"not available"}
    assert {m.carrier_channel for m in support[:3]} == {"not applicable"}
    assert {m.label for m in support[3:]} == {"TMT128"}
    assert {m.carrier_channel for m in support[3:]} == {"TMT131"}
    assert all(m.sample_type == "single cell" for m in mapping[7:])
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "full.tsv"
        write_manifest(path, mapping)
        with path.open() as fh:
            parsed = list(csv.DictReader(fh, delimiter="\t"))
        assert len(parsed) == 16
    print("sdrf_reporter_full_design_manifest self-test: PASS")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accession", default="PXD028040")
    ap.add_argument("--files-json", type=Path)
    ap.add_argument("--support-xlsx", type=Path)
    ap.add_argument("--reporter-audit-tsv", type=Path)
    ap.add_argument("--output", type=Path)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return 0
    for attr in ("files_json", "support_xlsx", "reporter_audit_tsv", "output"):
        value = getattr(args, attr)
        if value is None:
            ap.error(f"--{attr.replace('_', '-')} is required unless --self-test is used")
        if attr != "output" and not value.is_file():
            ap.error(f"input not found: {value}")
    summary = audit(args)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
