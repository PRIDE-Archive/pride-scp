#!/usr/bin/env python3
from __future__ import annotations

import csv
import importlib.util
import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = REPO / "scripts" / "sdrf_bigbio_readiness.py"
sys.path.insert(0, str(REPO / "scripts"))
SPEC = importlib.util.spec_from_file_location("sdrf_bigbio_readiness", MODULE_PATH)
assert SPEC and SPEC.loader
readiness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = readiness
SPEC.loader.exec_module(readiness)


def write_table(path: pathlib.Path, headers: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)


class SemanticRepairTests(unittest.TestCase):
    def test_dataset_semantic_concatenation_in_individual_fails_closed_by_row_material(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            src = root / "source.tsv"
            out = root / "projected.tsv"
            headers = [
                "source name",
                "characteristics[organism part]",
                "characteristics[individual]",
                "characteristics[material type]",
                "characteristics[sample type]",
                "characteristics[cells per well]",
                "characteristics[cell identifier]",
            ]
            rows = [
                ["Xla_cell_1", "animal hemisphere", "animal hemisphere | uterine cervix", "cell", "single cell", "1", "cell_1"],
                ["HeLa_standard", "uterine cervix", "animal hemisphere | uterine cervix", "cell line", "standard", "not applicable", "not applicable"],
            ]
            write_table(src, headers, rows)
            h, r, info = readiness.normalize_bigbio_projection(src, out, [])
            individual = h.index("characteristics[individual]")
            self.assertEqual(r[0][individual], "not available")
            self.assertEqual(r[1][individual], "not applicable")
            self.assertIn("normalized_individual_semantic_leakage_fail_closed:2_cells", info.actions)
            self.assertIn("individual_false_identity_removed_without_donor_inference", info.warnings)

    def test_row_local_cell_line_leakage_is_not_applicable(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            src = root / "source.tsv"
            out = root / "projected.tsv"
            headers = [
                "source name",
                "characteristics[individual]",
                "characteristics[cell line]",
                "characteristics[material type]",
            ]
            rows = [["HeLa", "HeLa", "HeLa", "cell line"]]
            write_table(src, headers, rows)
            h, r, info = readiness.normalize_bigbio_projection(src, out, [])
            individual = h.index("characteristics[individual]")
            self.assertEqual(r[0][individual], "not applicable")
            self.assertTrue(any(x.startswith("normalized_individual_semantic_leakage_fail_closed") for x in info.actions))

    def test_explicit_bulk_source_role_replaces_false_single_cell_with_pooled(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            src = root / "source.tsv"
            out = root / "projected.tsv"
            headers = [
                "source name",
                "characteristics[sample type]",
                "characteristics[cells per well]",
                "characteristics[cell identifier]",
                "comment[data file]",
            ]
            rows = [[
                "PC3_parental_bulk_library",
                "single cell",
                "not applicable",
                "not applicable",
                "20240510_PC3_10ng_Lib_01.mzML",
            ]]
            write_table(src, headers, rows)
            h, r, info = readiness.normalize_bigbio_projection(src, out, [])
            sample_type = h.index("characteristics[sample type]")
            self.assertEqual(r[0][sample_type], "pooled")
            self.assertIn("normalized_explicit_bulk_source_role_to_pooled:1_cells", info.actions)
            self.assertIn("bulk_sample_role_corrected_without_data_file_filename_inference", info.warnings)

    def test_data_file_bulk_token_alone_never_changes_sample_role(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            src = root / "source.tsv"
            out = root / "projected.tsv"
            headers = [
                "source name",
                "characteristics[sample type]",
                "characteristics[cells per well]",
                "characteristics[cell identifier]",
                "comment[data file]",
            ]
            rows = [["cell_A01", "single cell", "1", "A01", "bulk_named_file.raw"]]
            write_table(src, headers, rows)
            h, r, info = readiness.normalize_bigbio_projection(src, out, [])
            sample_type = h.index("characteristics[sample type]")
            self.assertEqual(r[0][sample_type], "single cell")
            self.assertFalse(any(x.startswith("normalized_explicit_bulk_source_role") for x in info.actions))

    def test_bulk_source_with_concrete_single_cell_identity_is_not_auto_repaired(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            src = root / "source.tsv"
            out = root / "projected.tsv"
            headers = [
                "source name",
                "characteristics[sample type]",
                "characteristics[cells per well]",
                "characteristics[cell identifier]",
            ]
            rows = [["bulk_sample", "single cell", "1", "cell_1"]]
            write_table(src, headers, rows)
            h, r, info = readiness.normalize_bigbio_projection(src, out, [])
            sample_type = h.index("characteristics[sample type]")
            self.assertEqual(r[0][sample_type], "single cell")
            self.assertFalse(any(x.startswith("normalized_explicit_bulk_source_role") for x in info.actions))

    def test_mixed_label_free_and_dimethyl_rows_fill_blank_channels_not_applicable(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            src = root / "source.tsv"
            out = root / "projected.tsv"
            headers = [
                "source name",
                "comment[label]",
                "comment[carrier channel]",
                "comment[reference channel]",
            ]
            rows = [
                ["cell_A", "NT=label free sample;AC=MS:1002038", "", ""],
                ["pool_A", "NT=DIMETHYL0;AC=PRIDE:0000848", "", ""],
                ["pool_B", "NT=DIMETHYL8;AC=PRIDE:0000852", "", ""],
            ]
            write_table(src, headers, rows)
            h, r, info = readiness.normalize_bigbio_projection(src, out, ["single-cell"])
            carrier = h.index("comment[carrier channel]")
            reference = h.index("comment[reference channel]")
            self.assertEqual([row[carrier] for row in r], ["not applicable"] * 3)
            self.assertEqual([row[reference] for row in r], ["not applicable"] * 3)
            self.assertTrue(any("filled_blank_comment[carrier channel]_not_applicable:3_cells" == x for x in info.actions))
            self.assertTrue(any("filled_blank_comment[reference channel]_not_applicable:3_cells" == x for x in info.actions))

    def test_unknown_or_isobaric_blank_channels_fail_closed_not_available(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            src = root / "source.tsv"
            out = root / "projected.tsv"
            headers = [
                "source name",
                "comment[label]",
                "comment[carrier channel]",
                "comment[reference channel]",
            ]
            rows = [
                ["cell_A", "NT=TMTpro 16plex;AC=PRIDE:0000898", "", ""],
                ["cell_B", "not available", "", ""],
            ]
            write_table(src, headers, rows)
            h, r, _ = readiness.normalize_bigbio_projection(src, out, ["single-cell"])
            carrier = h.index("comment[carrier channel]")
            reference = h.index("comment[reference channel]")
            self.assertEqual([row[carrier] for row in r], ["not available", "not available"])
            self.assertEqual([row[reference] for row in r], ["not available", "not available"])

    def test_existing_nonblank_channel_values_are_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            src = root / "source.tsv"
            out = root / "projected.tsv"
            headers = [
                "source name",
                "comment[label]",
                "comment[carrier channel]",
                "comment[reference channel]",
            ]
            rows = [["cell_A", "NT=TMTpro 16plex;AC=PRIDE:0000898", "126N", "127N"]]
            write_table(src, headers, rows)
            h, r, _ = readiness.normalize_bigbio_projection(src, out, ["single-cell"])
            carrier = h.index("comment[carrier channel]")
            reference = h.index("comment[reference channel]")
            self.assertEqual(r[0][carrier], "126N")
            self.assertEqual(r[0][reference], "127N")


if __name__ == "__main__":
    unittest.main()
