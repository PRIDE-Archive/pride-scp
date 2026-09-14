#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = REPO / "scripts" / "sdrf_bigbio_readiness.py"
sys.path.insert(0, str(REPO / "scripts"))
SPEC = importlib.util.spec_from_file_location("sdrf_bigbio_readiness", MODULE_PATH)
assert SPEC and SPEC.loader
readiness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = readiness
SPEC.loader.exec_module(readiness)


class ExistingSdrfLinkagePolicyTests(unittest.TestCase):
    def _snapshot(self, root: pathlib.Path, accession: str, raw_name: str) -> pathlib.Path:
        files = root / "files"
        files.mkdir(parents=True, exist_ok=True)
        (files / f"{accession}.json").write_text(
            json.dumps(
                {
                    "files": [
                        {
                            "fileName": raw_name,
                            "fileCategory": {"value": "RAW"},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return root

    def _table(self, data_file: str) -> tuple[list[str], list[list[str]]]:
        headers = [
            "source name",
            "assay name",
            "technology type",
            "comment[data file]",
            "characteristics[single cell isolation protocol]",
            "characteristics[cell identifier]",
        ]
        rows = [[
            "S1",
            "A1",
            "proteomic profiling by mass spectrometry",
            data_file,
            "manual cell picking",
            "cell-1",
        ]]
        return headers, rows

    def _candidate(self, generation_mode: str, locally_valid: bool = True):
        return readiness.CandidateInfo(
            locally_valid=locally_valid,
            internal_validation_errors=0,
            completeness_status="existing_sdrf_enriched_locally_valid",
            generation_mode=generation_mode,
        )

    def test_locally_valid_enriched_existing_sdrf_linkage_mismatch_is_warning(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            snapshot = self._snapshot(root / "snapshot", "PXDTEST", "bundle.zip")
            headers, rows = self._table("logical-acquisition.raw")
            blockers, warnings, *_ = readiness.internal_candidate_checks(
                self._candidate("enriched_existing_sdrf"),
                root / "candidate.sdrf.tsv",
                headers,
                rows,
                snapshot,
                "PXDTEST",
            )
            self.assertNotIn("sdrf_data_file_not_in_pride_raw_inventory", blockers)
            self.assertIn(
                "resolved_existing_sdrf_data_file_not_in_pride_raw_inventory",
                warnings,
            )

    def test_generated_explicit_mapping_linkage_mismatch_remains_blocking(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            snapshot = self._snapshot(root / "snapshot", "PXDTEST", "bundle.zip")
            headers, rows = self._table("logical-acquisition.raw")
            blockers, warnings, *_ = readiness.internal_candidate_checks(
                self._candidate("source_grounded_explicit_mapping"),
                root / "candidate.sdrf.tsv",
                headers,
                rows,
                snapshot,
                "PXDTEST",
            )
            self.assertIn("sdrf_data_file_not_in_pride_raw_inventory", blockers)
            self.assertNotIn(
                "resolved_existing_sdrf_data_file_not_in_pride_raw_inventory",
                warnings,
            )

    def test_existing_sdrf_with_failed_internal_audit_remains_blocking(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            snapshot = self._snapshot(root / "snapshot", "PXDTEST", "bundle.zip")
            headers, rows = self._table("logical-acquisition.raw")
            blockers, warnings, *_ = readiness.internal_candidate_checks(
                self._candidate("enriched_existing_sdrf", locally_valid=False),
                root / "candidate.sdrf.tsv",
                headers,
                rows,
                snapshot,
                "PXDTEST",
            )
            self.assertIn("sdrf_data_file_not_in_pride_raw_inventory", blockers)
            self.assertNotIn(
                "resolved_existing_sdrf_data_file_not_in_pride_raw_inventory",
                warnings,
            )

    def test_existing_sdrf_exact_inventory_match_has_no_linkage_warning(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            snapshot = self._snapshot(root / "snapshot", "PXDTEST", "logical-acquisition.raw")
            headers, rows = self._table("logical-acquisition.raw")
            blockers, warnings, *_ = readiness.internal_candidate_checks(
                self._candidate("enriched_existing_sdrf"),
                root / "candidate.sdrf.tsv",
                headers,
                rows,
                snapshot,
                "PXDTEST",
            )
            self.assertNotIn("sdrf_data_file_not_in_pride_raw_inventory", blockers)
            self.assertNotIn(
                "resolved_existing_sdrf_data_file_not_in_pride_raw_inventory",
                warnings,
            )


if __name__ == "__main__":
    unittest.main()
