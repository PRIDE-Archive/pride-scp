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


def test_generic_chemistry_context_does_not_poison_explicit_reporter_expansion(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.tsv"
    candidate.write_text(
        "source name\tcharacteristics[sample type]\tcharacteristics[organism]\t"
        "characteristics[cell identifier]\tcomment[data file]\tcomment[label]\n"
        "run_A\tsingle cell\tHomo sapiens (human)\tnot available\tA.raw\tTMTpro\n"
    )

    mapping = tmp_path / "structured_bundle_row_mappings.tsv"
    mapping.write_text(
        "accession\traw_file\treporter_channel\tsource_name\tplate\tmultiplex_sample\tplate_well\t"
        "source_population\tmapping_confidence\tmapping_key\n"
        "PXD900001\tA.raw\tTMT127N\tPlate1_S1_A1\tPlate1\t1\tA1\tBLAST\thigh\texact_raw_name_channel_well\n"
        "PXD900001\tA.raw\tTMT128N\tPlate1_S1_A2\tPlate1\t1\tA2\tPROG\thigh\texact_raw_name_channel_well\n"
    )

    # Contextual community rows may carry both generic chemistry and explicit
    # control-channel identities.  Once the dedicated bundle has an exact
    # reporter mapping, these rows remain context and must not enter expansion.
    context = tmp_path / "PXD900001_community_annotated.sdrf.tsv"
    context.write_text(
        "source name\tcharacteristics[sample type]\tcharacteristics[organism]\tcomment[data file]\tcomment[label]\n"
        "run_A\tsingle cell\tHomo sapiens\tA.raw\tTMTpro\n"
        "carrier_A\tcarrier\tHomo sapiens\tA.raw\tTMT126\n"
    )

    output = tmp_path / "resolved.tsv"
    report = tmp_path / "resolver.json"
    result = resolve(candidate, output, report, [mapping, context], "PXD900001")

    assert result["changed"] is True
    assert result["multiplex_expansion_count"] == 1
    assert result["conflicts"] == []
    rows = list(csv.DictReader(output.open(), delimiter="\t"))
    assert [r["comment[label]"] for r in rows] == ["TMT127N", "TMT128N"]
    assert all(r["characteristics[organism]"] == "Homo sapiens (human)" for r in rows)


def test_bulk_candidate_is_not_multiplex_expanded_from_reporter_mapping(tmp_path: Path) -> None:
    candidate = tmp_path / "bulk.tsv"
    candidate.write_text(
        "source name\tcharacteristics[sample type]\tcomment[data file]\tcomment[label]\n"
        "bulk_run\tbulk\tB.raw\tTMTpro\n"
    )
    mapping = tmp_path / "mapping.tsv"
    mapping.write_text(
        "accession\traw_file\treporter_channel\tsource_name\n"
        "PXD900001\tB.raw\tTMT127N\tBulk_S1_A1\n"
        "PXD900001\tB.raw\tTMT128N\tBulk_S1_A2\n"
    )

    output = tmp_path / "resolved.tsv"
    report = tmp_path / "resolver.json"
    result = resolve(candidate, output, report, [mapping], "PXD900001")

    assert result["multiplex_expansion_count"] == 0
    assert result["changed"] is False
    assert output.read_bytes() == candidate.read_bytes()


def test_explicit_bundle_bulk_population_vetoes_mislabeled_single_cell_candidate(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.tsv"
    candidate.write_text(
        "source name\tcharacteristics[sample type]\tcomment[data file]\tcomment[label]\n"
        "run_B\tsingle cell\tB.raw\tTMTpro\n"
    )
    mapping = tmp_path / "structured_bundle_row_mappings.tsv"
    mapping.write_text(
        "accession\traw_file\treporter_channel\tsource_name\tsource_population\t"
        "mapping_confidence\tmapping_key\n"
        "PXD900001\tB.raw\tTMT127N\tBulk_S1_A1\tBULK\thigh\texact_raw_name_channel_well\n"
        "PXD900001\tB.raw\tTMT128N\tBulk_S1_A2\tBULK\thigh\texact_raw_name_channel_well\n"
    )

    output = tmp_path / "resolved.tsv"
    report = tmp_path / "resolver.json"
    result = resolve(candidate, output, report, [mapping], "PXD900001")

    assert result["multiplex_expansion_count"] == 0
    assert result["changed"] is False
    assert output.read_bytes() == candidate.read_bytes()


def test_bundle_expansion_fails_closed_without_source_population_provenance(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.tsv"
    candidate.write_text(
        "source name\tcharacteristics[sample type]\tcomment[data file]\tcomment[label]\n"
        "run_C\tsingle cell\tC.raw\tTMTpro\n"
    )
    mapping = tmp_path / "structured_bundle_row_mappings.tsv"
    mapping.write_text(
        "accession\traw_file\treporter_channel\tsource_name\tsource_population\t"
        "mapping_confidence\tmapping_key\n"
        "PXD900001\tC.raw\tTMT127N\tCell_S1_A1\t\thigh\texact_raw_name_channel_well\n"
        "PXD900001\tC.raw\tTMT128N\tCell_S1_A2\t\thigh\texact_raw_name_channel_well\n"
    )

    output = tmp_path / "resolved.tsv"
    report = tmp_path / "resolver.json"
    result = resolve(candidate, output, report, [mapping], "PXD900001")

    assert result["multiplex_expansion_count"] == 0
    assert result["changed"] is False
    assert output.read_bytes() == candidate.read_bytes()


def test_organism_display_alias_does_not_block_explicit_reporter_expansion(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.tsv"
    candidate.write_text(
        "source name\tcharacteristics[sample type]\tcharacteristics[organism]\t"
        "comment[data file]\tcomment[label]\n"
        "run_A\tsingle cell\tHomo sapiens (human)\tA.raw\tTMTpro\n"
    )
    mapping = tmp_path / "mapping.tsv"
    mapping.write_text(
        "accession\traw_file\treporter_channel\tsource_name\torganism\n"
        "PXD900001\tA.raw\tTMT127N\tcell-A\tHomo sapiens\n"
        "PXD900001\tA.raw\tTMT128N\tcell-B\tHomo sapiens\n"
    )
    output = tmp_path / "resolved.tsv"
    report = tmp_path / "resolver.json"
    result = resolve(candidate, output, report, [mapping], "PXD900001")

    assert result["changed"] is True
    assert result["multiplex_expansion_count"] == 1
    assert result["conflicts"] == []
    rows = list(csv.DictReader(output.open(), delimiter="\t"))
    assert {r["comment[label]"] for r in rows} == {"TMT127N", "TMT128N"}
    assert all(r["characteristics[organism]"] == "Homo sapiens (human)" for r in rows)


def test_different_organism_still_blocks_explicit_reporter_expansion(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.tsv"
    candidate.write_text(
        "source name\tcharacteristics[sample type]\tcharacteristics[organism]\t"
        "comment[data file]\tcomment[label]\n"
        "run_A\tsingle cell\tHomo sapiens (human)\tA.raw\tTMTpro\n"
    )
    mapping = tmp_path / "mapping.tsv"
    mapping.write_text(
        "accession\traw_file\treporter_channel\tsource_name\torganism\n"
        "PXD900001\tA.raw\tTMT127N\tcell-A\tMus musculus\n"
        "PXD900001\tA.raw\tTMT128N\tcell-B\tMus musculus\n"
    )
    output = tmp_path / "resolved.tsv"
    report = tmp_path / "resolver.json"
    result = resolve(candidate, output, report, [mapping], "PXD900001")

    assert result["changed"] is False
    assert result["multiplex_expansion_count"] == 0
    assert result["conflicts"]
    assert all(
        "characteristics[organism]" in conflict["detail"]
        for conflict in result["conflicts"]
    )
    assert output.read_bytes() == candidate.read_bytes()
