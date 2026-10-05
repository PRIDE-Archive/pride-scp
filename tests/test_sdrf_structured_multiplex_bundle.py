from __future__ import annotations

import csv
import json
import sys
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from sdrf_generalized_evidence_graph import mapping_archive_priority
from sdrf_reporter_run_scope_audit import RepoFile
from sdrf_structured_mapping_resolver import resolve
from sdrf_structured_multiplex_bundle import (
    flatten_mapping_records,
    parse_bundle,
    write_mapping_tsv,
)


def write_bundle(path: Path, *, malformed_header: bool = False) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        base = "analysis/data"
        zf.writestr(
            f"{base}/file_sample_mapping.txt",
            "File Name\tPlate\tSample\nA.raw\tPlate1\t1\n",
        )
        zf.writestr(
            f"{base}/plate_layout_mapping.txt",
            "Plate\tSort Layout\tLabel Layout\tSample Layout\n"
            "Plate1\tsort_layout.txt\tlabel_layout.txt\tsample_layout.txt\n",
        )
        zf.writestr(
            f"{base}/run_InputFiles.txt",
            "File ID\tFile Name\nF1\tA.raw\n",
        )
        bad = "\tAbundance malformed header" if malformed_header else ""
        zf.writestr(
            f"{base}/run_Proteins.txt",
            "Protein\tAbundance X F1 128N\tAbundance X F1 127N"
            f"\tAbundance X F1 126{bad}\n",
        )
        zf.writestr(
            f"{base}/sample_layout.txt",
            "\t1\t2\t3\nA\t1\t1\t1\n",
        )
        zf.writestr(
            f"{base}/label_layout.txt",
            "\t1\t2\t3\nA\t128N\t127N\t126\n",
        )
        zf.writestr(
            f"{base}/sort_layout.txt",
            "\t1\t2\t3\nA\tprog\tblast\tbooster\n",
        )


def test_schema_adapter_expands_generic_tmtpro_without_filename_order(tmp_path: Path) -> None:
    archive = tmp_path / "SCeptre_FINAL.zip"
    write_bundle(archive)

    raw, issues, summary = parse_bundle(
        archive,
        accession="PXD900001",
        pride_raw_names=["A.raw"],
    )
    assert not issues
    assert summary["raw_record_count"] == 3

    flat, conflicts = flatten_mapping_records(
        raw,
        accession="PXD900001",
        archive_path=archive,
    )
    assert not conflicts
    assert [(x.reporter_channel, x.source_name) for x in flat] == [
        ("TMT127N", "Plate1_S1_A2"),
        ("TMT128N", "Plate1_S1_A1"),
    ]
    assert all(x.multiplex_sample == "1" for x in flat)

    mapping = tmp_path / "structured_bundle_row_mappings.tsv"
    write_mapping_tsv(mapping, flat)
    mapping_rows = list(csv.DictReader(mapping.open(), delimiter="\t"))
    assert "multiplex_sample" in mapping_rows[0]
    assert "sample" not in mapping_rows[0]

    candidate = tmp_path / "candidate.tsv"
    candidate.write_text(
        "source name\tcharacteristics[sample type]\t"
        "characteristics[cell identifier]\tcomment[data file]\tcomment[label]\n"
        "run_A\tsingle cell\tnot available\tA.raw\tTMTpro\n"
    )
    output = tmp_path / "resolved.tsv"
    report = tmp_path / "resolver.json"
    result = resolve(candidate, output, report, [mapping], "PXD900001")

    assert result["changed"] is True
    assert result["multiplex_expansion_count"] == 1
    assert not result["conflicts"]
    resolved = list(csv.DictReader(output.open(), delimiter="\t"))
    assert [(r["comment[label]"], r["source name"]) for r in resolved] == [
        ("TMT127N", "Plate1_S1_A2"),
        ("TMT128N", "Plate1_S1_A1"),
    ]
    assert all(
        r["characteristics[cell identifier]"] == r["source name"] for r in resolved
    )


def test_malformed_bundle_root_fails_closed_instead_of_emitting_partial_rows(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "analysis.zip"
    write_bundle(archive, malformed_header=True)

    raw, issues, summary = parse_bundle(
        archive,
        accession="PXD900001",
        pride_raw_names=["A.raw"],
    )
    assert issues
    assert raw == []
    assert summary["mapping_roots"][0]["status"] == "unresolved"


def test_mapping_archive_selection_is_schema_triage_not_accession_specific() -> None:
    priority, reason = mapping_archive_priority(
        RepoFile("analysis_source_bundle.zip", "OTHER", "")
    )
    assert priority == 5
    assert reason == "mapping_or_analysis_archive_name"

    priority, reason = mapping_archive_priority(
        RepoFile("random_backup.zip", "OTHER", "")
    )
    assert priority == 0
    assert reason == "archive_without_mapping_hint"
