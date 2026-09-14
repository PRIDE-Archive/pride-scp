#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = REPO / "scripts" / "sdrf_bigbio_readiness.py"
sys.path.insert(0, str(REPO / "scripts"))
SPEC = importlib.util.spec_from_file_location("sdrf_bigbio_readiness", MODULE_PATH)
assert SPEC and SPEC.loader
readiness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = readiness
SPEC.loader.exec_module(readiness)


def failed_check() -> readiness.CommandResult:
    return readiness.CommandResult(
        "python -m tools",
        ["python", "-m", "tools", "check", "x.sdrf.tsv"],
        1,
        "Status: ISSUES FOUND\n",
        "",
    )


class SkillsCompatibilityTests(unittest.TestCase):
    def test_pride_label_wrong_ontology_is_known_generic_drift(self):
        check = readiness.apply_known_skills_drift_override(
            failed_check(),
            {
                "is_clean": False,
                "hallucinated": [],
                "mismatched": [],
                "wrong_ontology": [
                    {
                        "column": "comment[label]",
                        "accession": "PRIDE:0000848",
                        "label": "DIMETHYL0",
                        "expected_ontologies": ["MS"],
                        "actual_ontology": "PRIDE",
                    }
                ],
                "unimod_swaps": [],
            },
        )
        self.assertTrue(check.passed)
        self.assertTrue(check.compatibility_override)
        self.assertIn("comment_label_pride", check.compatibility_reason)

    def test_clo_cell_type_wrong_ontology_is_known_generic_drift(self):
        check = readiness.apply_known_skills_drift_override(
            failed_check(),
            {
                "is_clean": False,
                "hallucinated": [],
                "mismatched": [],
                "wrong_ontology": [
                    {
                        "column": "factor value[cell type]",
                        "accession": "CLO:0003684",
                        "label": "HeLa cell",
                        "expected_ontologies": ["CL", "BTO"],
                        "actual_ontology": "CLO",
                    }
                ],
                "unimod_swaps": [],
            },
        )
        self.assertTrue(check.passed)
        self.assertIn("cell_type_clo", check.compatibility_reason)

    def test_exact_unimod199_static_map_defect_is_known_drift(self):
        check = readiness.apply_known_skills_drift_override(
            failed_check(),
            {
                "is_clean": False,
                "hallucinated": [],
                "mismatched": [],
                "wrong_ontology": [],
                "unimod_swaps": [
                    {
                        "wrong_accession": "UNIMOD:199",
                        "wrong_name_for_accession": "Dimethyl:2H(4)",
                        "correct_accession": "UNIMOD:199",
                        "correct_name": "Label:13C(6)15N(2)",
                    }
                ],
            },
        )
        self.assertTrue(check.passed)
        self.assertIn("unimod199", check.compatibility_reason)

    def test_mixed_known_issues_are_all_required_to_match(self):
        check = readiness.apply_known_skills_drift_override(
            failed_check(),
            {
                "is_clean": False,
                "hallucinated": [],
                "mismatched": [],
                "wrong_ontology": [
                    {
                        "column": "comment[label]",
                        "accession": "PRIDE:0000850",
                        "label": "DIMETHYL4",
                        "expected_ontologies": ["MS"],
                        "actual_ontology": "PRIDE",
                    }
                ],
                "unimod_swaps": [
                    {
                        "wrong_accession": "UNIMOD:199",
                        "wrong_name_for_accession": "Dimethyl:2H(4)",
                        "correct_accession": "UNIMOD:199",
                        "correct_name": "Label:13C(6)15N(2)",
                    }
                ],
            },
        )
        self.assertTrue(check.passed)
        self.assertIn("comment_label_pride", check.compatibility_reason)
        self.assertIn("unimod199", check.compatibility_reason)

    def test_hallucination_is_never_waived(self):
        check = readiness.apply_known_skills_drift_override(
            failed_check(),
            {
                "is_clean": False,
                "hallucinated": [{"column": "characteristics[cell line]", "label": "KPC"}],
                "mismatched": [],
                "wrong_ontology": [],
                "unimod_swaps": [],
            },
        )
        self.assertFalse(check.passed)
        self.assertFalse(check.compatibility_override)

    def test_unknown_wrong_ontology_is_never_waived(self):
        check = readiness.apply_known_skills_drift_override(
            failed_check(),
            {
                "is_clean": False,
                "hallucinated": [],
                "mismatched": [],
                "wrong_ontology": [
                    {
                        "column": "characteristics[organism]",
                        "accession": "NCBITaxon:9606",
                        "expected_ontologies": ["NCBITaxon"],
                        "actual_ontology": "OTHER",
                    }
                ],
                "unimod_swaps": [],
            },
        )
        self.assertFalse(check.passed)

    def test_unknown_unimod_swap_is_never_waived(self):
        check = readiness.apply_known_skills_drift_override(
            failed_check(),
            {
                "is_clean": False,
                "hallucinated": [],
                "mismatched": [],
                "wrong_ontology": [],
                "unimod_swaps": [
                    {
                        "wrong_accession": "UNIMOD:4",
                        "wrong_name_for_accession": "Oxidation",
                        "correct_accession": "UNIMOD:35",
                        "correct_name": "Oxidation",
                    }
                ],
            },
        )
        self.assertFalse(check.passed)


if __name__ == "__main__":
    unittest.main()
