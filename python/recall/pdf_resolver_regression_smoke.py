#!/usr/bin/env python3
"""Offline regressions for the v0.1.5 publication/PDF resolver."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STAGES = ROOT / "python" / "stages"
sys.path.insert(0, str(STAGES))

import pride_scp_pipeline_common as common  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "pdf_stage", STAGES / "02_download_publication_pdfs.py"
)
assert spec and spec.loader
pdf_stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pdf_stage)


def fake_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.7\n" + b"0" * 2048)


def test_pmcid_does_not_require_haspdf() -> None:
    urls = common.europe_pmc_pdf_urls({"pmcid": "PMC10557376"})
    assert urls, "PMCID should produce fallback PDF URLs even without hasPDF"
    assert any("PMC10557376" in url for _, url in urls)


def test_title_only_lookup_requires_exact_title() -> None:
    original = common._europe_pmc_search
    try:
        common._europe_pmc_search = lambda *a, **k: [
            {"title": "Different paper", "pmcid": "PMC1"}
        ]
        assert common.europe_pmc_lookup(object(), title="Wanted paper") is None
        common._europe_pmc_search = lambda *a, **k: [
            {"title": "Wanted paper", "pmcid": "PMC2"}
        ]
        result = common.europe_pmc_lookup(object(), title="Wanted paper")
        assert result and result["pmcid"] == "PMC2"
    finally:
        common._europe_pmc_search = original


def test_manual_accession_pdf_is_reused() -> None:
    with tempfile.TemporaryDirectory() as tmp_text:
        tmp = Path(tmp_text)
        manual = tmp / "manual"
        source = manual / "PXD123456.pdf"
        fake_pdf(source)
        row = {
            "accession": "PXD123456",
            "publication_doi": "10.1234/example",
            "publication_title": "Example publication",
        }
        result = pdf_stage.external_pdf_result(
            row,
            pdf_dir=tmp / "work_pdfs",
            manual_manifest={},
            manual_index=pdf_stage.build_pdf_index([manual]),
            reuse_index={},
            reuse_mode="symlink",
        )
        assert result is not None
        assert result["pdf_status"] == "already_exists"
        assert result["pdf_source"] == "manual_pdf"
        assert common.validate_pdf_path(Path(result["pdf_path"]))


def test_old_cache_schema_is_invalidated() -> None:
    with tempfile.TemporaryDirectory() as tmp_text:
        tmp = Path(tmp_text)
        row = {
            "accession": "PXD123456",
            "publication_doi": "10.1234/example",
            "publication_title": "Example publication",
        }
        path = pdf_stage.cache_file_for(row, tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "cache_schema_version": 1,
                    "publication_identity": pdf_stage.publication_identity(row),
                    "result": {"pdf_status": "no_open_access_pdf"},
                }
            ),
            encoding="utf-8",
        )
        assert pdf_stage.read_cache(row, tmp) is None


def test_unresolved_has_diagnostics() -> None:
    with tempfile.TemporaryDirectory() as tmp_text:
        tmp = Path(tmp_text)
        row = {
            "accession": "PXD123456",
            "publication_doi": "10.1234/example",
            "publication_title": "Example publication",
            "publication_pmid": "",
            "publication_pmcid": "",
        }
        original_lookup = pdf_stage.europe_pmc_lookup
        original_session = pdf_stage.session_for_thread
        try:
            pdf_stage.europe_pmc_lookup = lambda *a, **k: None
            pdf_stage.session_for_thread = lambda *a, **k: object()
            result = pdf_stage.resolve_uncached(
                row,
                pdf_dir=tmp / "pdfs",
                manual_manifest={},
                manual_index={},
                reuse_index={},
                reuse_mode="symlink",
                unpaywall_email="",
                user_agent="test",
                timeout=1,
            )
        finally:
            pdf_stage.europe_pmc_lookup = original_lookup
            pdf_stage.session_for_thread = original_session
        assert result["pdf_status"] == "no_open_access_pdf"
        assert result["pdf_error"], "unresolved results must explain why"
        trace = json.loads(result["pdf_resolution_trace"])
        assert any(item.get("source") == "EuropePMC" for item in trace)
        assert any(item.get("source") == "Unpaywall" for item in trace)


def main() -> None:
    test_pmcid_does_not_require_haspdf()
    test_title_only_lookup_requires_exact_title()
    test_manual_accession_pdf_is_reused()
    test_old_cache_schema_is_invalidated()
    test_unresolved_has_diagnostics()
    print("All v0.1.5 PDF resolver regression tests passed.")
    print("PMCID fallback no longer requires hasPDF=Y.")
    print("Manual/reused PDFs override unresolved cache paths.")
    print("Old no_open_access_pdf cache schema is invalidated.")
    print("Unresolved rows now retain structured diagnostics.")


if __name__ == "__main__":
    main()
