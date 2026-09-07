#!/usr/bin/env python3
"""Build a source-grounded explicit SDRF row-mapping manifest from a deposited design workbook.

This v0.4.4 helper is intentionally narrow.  It accepts only the PXD028040 design architecture
observed in the deposited workbook after v0.4.3b manual/source review:

* a worksheet section explicitly headed "Application for single neuron analysis";
* rows with one explicit acquisition date and one explicit SC run code;
* an explicit biological identity of the form "DA neuron #N";
* an explicit "technical replicate measurement M";
* an explicit neuron digest tagged with TMT 128; and
* an explicit tissue digest tagged with TMT 131.

Each design row must map uniquely to one PRIDE RAW acquisition by acquisition-date + SC code.
Filename words such as ``TMT`` or ``single_neuron`` are never used to decide whether a RAW belongs
to the single-neuron branch.  No SDRF is written here; the output is a provenance-rich TSV consumed
by the Rust serializer through ``--explicit-row-mapping-manifest``.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sdrf_reporter_design_semantic_audit import (
    Cell,
    DesignRow,
    features,
    parse_xlsx_structured,
    row_ref,
)
from sdrf_reporter_run_scope_audit import (
    RepoFile,
    is_raw_file,
    load_reporter_contract,
    repository_files,
    raw_stem,
)

AUDITOR_VERSION = "pride-scp-sdrf-explicit-mapping-manifest-v0.1"
SECTION_RE = re.compile(r"(?i)^\s*Application\s+for\s+single\s+neuron\s+analysis\s*$")
SAMPLE_RE = re.compile(
    r"(?i)\bDA\s+neuron\s*#\s*(\d+)\s+technical\s+replicate\s+measurement\s+(\d+)\b"
)
ANALYTICAL_LAYOUT_RE = re.compile(
    r"(?i)\b(?:~\s*)?\d+(?:\.\d+)?\s*pg\s+of\s+neuron\s+digest\s+tagged\s+with\s+TMT\s*[-_ ]?128\b"
)
CARRIER_LAYOUT_RE = re.compile(
    r"(?i)\b(?:~\s*)?\d+(?:\.\d+)?\s*ng\s+of\s+(?:diluted\s+)?tissue\s+digest\s+tagged\s+with\s+TMT\s*[-_ ]?131\b"
)

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


@dataclass
class MappingRow:
    accession: str
    raw_file: str
    source_name: str
    cell_identifier: str
    biological_replicate: str
    technical_replicate: str
    sample_type: str
    cells_per_well: str
    label: str
    carrier_channel: str
    reference_channel: str
    design_source: str
    design_ref: str
    mapping_key: str
    mapping_confidence: str


def raw_date_sc(raw: RepoFile) -> tuple[str, str] | None:
    f = features(raw_stem(raw.name).replace("_", " "))
    if len(f.dates) != 1 or len(f.sc_codes) != 1:
        return None
    return f.dates[0], f.sc_codes[0]


def single_neuron_section_rows(rows: list[DesignRow]) -> tuple[DesignRow, list[DesignRow]]:
    section_rows = [r for r in rows if SECTION_RE.match(r.text)]
    if len(section_rows) != 1:
        raise ValueError(
            f"expected exactly one 'Application for single neuron analysis' section row; found {len(section_rows)}"
        )
    section = section_rows[0]
    same_sheet = [r for r in rows if r.sheet == section.sheet and r.row_number > section.row_number]
    selected: list[DesignRow] = []
    for row in same_sheet:
        m = SAMPLE_RE.search(row.text)
        if not m:
            # Do not use proximity alone to absorb unrelated sections.  Once the explicit single-neuron
            # rows have started, a later non-empty semantic section breaks the block.
            if selected and row.text.strip():
                break
            continue
        rf = features(row.text)
        if len(rf.dates) != 1 or len(rf.sc_codes) != 1:
            raise ValueError(f"{row_ref(row)} sample row lacks one explicit date+SC key: {row.text}")
        if not ANALYTICAL_LAYOUT_RE.search(row.text):
            raise ValueError(f"{row_ref(row)} lacks explicit neuron-digest TMT128 analytical layout: {row.text}")
        if not CARRIER_LAYOUT_RE.search(row.text):
            raise ValueError(f"{row_ref(row)} lacks explicit tissue-digest TMT131 carrier layout: {row.text}")
        selected.append(row)
    if not selected:
        raise ValueError("single-neuron design section contained no explicit sample rows")
    return section, selected


def derive_mapping(
    accession: str,
    design_source: str,
    rows: list[DesignRow],
    raws: list[RepoFile],
    reporter: dict[str, str],
) -> tuple[list[MappingRow], dict[str, object]]:
    reporter_ok = (
        reporter.get("mapping_class") == "single_analytical_channel_per_run"
        and reporter.get("confidence") == "high"
        and reporter.get("single_cell_channels") == "128"
        and reporter.get("carrier_channels") == "131"
        and not reporter.get("ambiguous_channels")
    )
    if not reporter_ok:
        raise ValueError("accepted PXD028040 reporter contract is not present in reporter audit")

    section, design_rows = single_neuron_section_rows(rows)

    raw_by_key: dict[tuple[str, str], list[RepoFile]] = {}
    for raw in raws:
        key = raw_date_sc(raw)
        if key:
            raw_by_key.setdefault(key, []).append(raw)

    mapping: list[MappingRow] = []
    seen_raws: set[str] = set()
    sample_to_reps: dict[int, set[int]] = {}
    for row in design_rows:
        rf = features(row.text)
        key = (rf.dates[0], rf.sc_codes[0])
        matches = raw_by_key.get(key, [])
        if len(matches) != 1:
            raise ValueError(
                f"{row_ref(row)} date+SC key {key[0]}+{key[1]} maps to {len(matches)} repository RAWs"
            )
        raw = matches[0]
        raw_key = raw.name.lower()
        if raw_key in seen_raws:
            raise ValueError(f"repository RAW assigned twice: {raw.name}")
        seen_raws.add(raw_key)

        sm = SAMPLE_RE.search(row.text)
        assert sm is not None
        biological = int(sm.group(1))
        technical = int(sm.group(2))
        sample_to_reps.setdefault(biological, set()).add(technical)
        source_name = f"DA_neuron_{biological}"
        mapping.append(
            MappingRow(
                accession=accession,
                raw_file=raw.name,
                source_name=source_name,
                cell_identifier=source_name,
                biological_replicate=str(biological),
                technical_replicate=str(technical),
                sample_type="single cell",
                cells_per_well="1",
                label="TMT128",
                carrier_channel="TMT131",
                reference_channel="not applicable",
                design_source=design_source,
                design_ref=row_ref(row),
                mapping_key="date_sc_run_key",
                mapping_confidence="high",
            )
        )

    # The deposited workbook explicitly describes three neurons with three technical measurements
    # each.  Treat a different shape as a source-contract change requiring manual re-review.
    expected = {1: {1, 2, 3}, 2: {1, 2, 3}, 3: {1, 2, 3}}
    if sample_to_reps != expected:
        raise ValueError(f"unexpected biological/technical replicate design: {sample_to_reps!r}")
    if len(mapping) != 9:
        raise ValueError(f"expected 9 explicit single-neuron acquisition rows, found {len(mapping)}")

    summary: dict[str, object] = {
        "auditor_version": AUDITOR_VERSION,
        "accession": accession,
        "status": "explicit_mapping_manifest_ready",
        "non_generative": True,
        "reporter_contract_valid": True,
        "section_ref": row_ref(section),
        "section_text": section.text,
        "design_source": design_source,
        "single_neuron_design_rows": len(design_rows),
        "repository_raw_files": len(raws),
        "mapped_single_neuron_raw_files": len(mapping),
        "biological_samples": 3,
        "technical_replicates_per_sample": 3,
        "analytical_label": "TMT128",
        "carrier_channel": "TMT131",
        "reference_channel": "not applicable",
        "mapping_key": "unique acquisition date + SC code",
        "filename_branch_words_used_for_membership": False,
        "generator_authorized_for_manifest_rows_only": True,
        "unmapped_repository_raw_files": [r.name for r in raws if r.name.lower() not in seen_raws],
        "sample_groups": {
            f"DA_neuron_{sample}": [m.raw_file for m in mapping if m.biological_replicate == str(sample)]
            for sample in (1, 2, 3)
        },
        "notes": [
            "Membership in the single-neuron branch comes from the deposited workbook section, not RAW filename words.",
            "Each workbook row explicitly supplies DA neuron identity, technical replicate measurement, TMT128 neuron digest, and TMT131 tissue digest.",
            "Only the nine manifest rows are authorized for deterministic SDRF row serialization; unrelated development/control RAWs remain outside this reconstructed single-neuron branch.",
        ],
    }
    return mapping, summary


def write_manifest(path: Path, rows: list[MappingRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=MANIFEST_FIELDS, delimiter="\t")
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)


def audit(args: argparse.Namespace) -> dict[str, object]:
    accession = args.accession.strip().upper()
    repo = repository_files(json.loads(args.files_json.read_text(errors="replace")))
    raws = [x for x in repo if is_raw_file(x)]
    workbook_rows = parse_xlsx_structured(args.support_xlsx)
    reporter = load_reporter_contract(args.reporter_audit_tsv, accession)
    mapping, summary = derive_mapping(
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
    summary["mapping_rows"] = [m.__dict__ for m in mapping]
    (args.output / "sdrf_reporter_design_manifest_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False)
    )
    return summary


def _cell(ref: str, value: str) -> Cell:
    return Cell(ref=ref, column=ref[0], value=value, raw_value=value, style_index=None)


def self_test() -> None:
    rows = [
        DesignRow("Sheet2", 14, [_cell("A14", "Application for single neuron analysis")]),
    ]
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
    raws: list[RepoFile] = []
    for row_no, key, sample, tech in specs:
        rows.append(
            DesignRow(
                "Sheet2",
                row_no,
                [
                    _cell(f"A{row_no}", key),
                    _cell(f"B{row_no}", f"DA neuron #{sample} technical replicate measurement {tech}"),
                    _cell(
                        f"C{row_no}",
                        "~100 pg of neuron digest tagged with TMT 128 + ~10 ng of diluted tissue digest tagged with TMT 131",
                    ),
                ],
            )
        )
        # Two names deliberately lack single-neuron wording, matching the real source-layout lesson.
        if key == "2018-08-27_SC02":
            name = f"{key}.RAW"
        elif key == "2018-09-04_SC05":
            name = f"{key}_10_ng_tmt.RAW"
        else:
            name = f"{key}_TMT_single_neuron.RAW"
        raws.append(RepoFile(name=name, category="RAW", uri=f"ftp://example/{name}"))
    reporter = {
        "mapping_class": "single_analytical_channel_per_run",
        "confidence": "high",
        "single_cell_channels": "128",
        "carrier_channels": "131",
        "ambiguous_channels": "",
    }
    mapping, summary = derive_mapping("PXD028040", "design.xlsx", rows, raws, reporter)
    assert len(mapping) == 9
    assert summary["biological_samples"] == 3
    assert mapping[0].raw_file == "2018-08-27_SC02.RAW"
    assert mapping[4].raw_file == "2018-09-04_SC05_10_ng_tmt.RAW"
    assert mapping[0].source_name == "DA_neuron_1"
    assert mapping[4].source_name == "DA_neuron_2"
    assert mapping[-1].source_name == "DA_neuron_3"
    assert {m.label for m in mapping} == {"TMT128"}
    assert {m.carrier_channel for m in mapping} == {"TMT131"}
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "mapping.tsv"
        write_manifest(path, mapping)
        with path.open() as fh:
            parsed = list(csv.DictReader(fh, delimiter="\t"))
        assert len(parsed) == 9
        assert parsed[0]["design_ref"] == "Sheet2:row15"
    print("sdrf_reporter_design_manifest self-test: PASS")


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
