#!/usr/bin/env python3
"""
Stage 06: independent LLM QC review of the curated PRIDE SCP catalogue.

v3.1 keeps the validated v2.1 retrieval/claim logic and uses a selective
three-model adjudication jury. MiniCheck remains the primary grounded fact
checker. Easy MiniCheck-supported claims PASS immediately, matching the
empirically successful v2.1 behavior. Phi and Gemma are invoked only for
MiniCheck non-support or deterministic high-risk cases. Optional anonymized
reconsideration remains available for experiments but is OFF by default.

This stage is READ ONLY with respect to the Stage-05 catalogue. It never
rewrites catalogue CSVs or Stage-04 annotation JSONs.

Default reviewer architecture:
  1. Bespoke-MiniCheck: primary binary grounded factual-support check.
  2. Easy MiniCheck Yes + no high-risk evidence: PASS immediately.
  3. Phi-4-mini + Gemma 3 4B: blind structured adjudicators only for
     MiniCheck No or deterministic high-risk cases.
  4. Optional one-round anonymized reconsideration is disabled by default.

The output is an audit layer for manual review, not an automatic correction
layer.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pymupdf
import requests


STAGE06_VERSION = "stage6-qc-v3.2"
DEFAULT_OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_FACT_MODEL = "bespoke-minicheck"
DEFAULT_CRITIC_MODEL = "phi4-mini:3.8b"
DEFAULT_JURY_MODEL = "gemma3:4b"
DEFAULT_NUM_CTX = 4096
DEFAULT_CRITIC_NUM_PREDICT = 320
DEFAULT_JURY_NUM_PREDICT = 320
DEFAULT_DELIBERATION_NUM_PREDICT = 320
DEFAULT_EVIDENCE_CHARS = 3600
DEFAULT_SNIPPET_CHARS = 850
DEFAULT_FACT_KEEP_ALIVE = "5m"
DEFAULT_CRITIC_KEEP_ALIVE = "0"
DEFAULT_JURY_KEEP_ALIVE = "0"
DEFAULT_OLLAMA_RETRIES = 2
DEFAULT_OLLAMA_RETRY_BACKOFF = 3.0


class OllamaUnavailableError(RuntimeError):
    """Raised after transient Ollama connectivity failures are exhausted."""



TECHNICAL_FIELDS = (
    "sample_preparation",
    "single_cell_isolation_json",
    "labeling_strategy",
    "mass_spectrometers",
    "acquisition_modes",
    "ion_mobility_or_faims_json",
    "lc_configuration",
    "lc_gradient",
    "single_cell_throughput",
    "single_cell_proteome_depth",
    "low_input_proteome_depth",
    "analysis_software",
    "analysis_strategies",
)

GENERIC_SCP_TERMS = (
    "single-cell proteomics",
    "single cell proteomics",
    "single-cell",
    "single cell",
    "individual cell",
    "individual cells",
    "single neuron",
    "single oocyte",
    "single bacterium",
    "proteomics",
    "mass spectrometry",
    "LC-MS",
    "MS/MS",
)

# Retrieval aliases are intentionally conservative and are used only to find
# source passages. They never rewrite curated catalogue values.
RETRIEVAL_ALIASES = {
    "escherichia coli": ("E. coli", "E coli"),
    "e. coli": ("Escherichia coli", "E coli"),
    "homo sapiens": ("human", "humans"),
    "mus musculus": ("mouse", "murine"),
    "oryza sativa": ("rice",),
    "saccharomyces cerevisiae": ("yeast",),
    "naive hps": (
        "naive human pluripotent stem cells",
        "naive hPS cells",
        "naive hPSC",
    ),
    "naive hps cells": (
        "naive human pluripotent stem cells",
        "naive hPS",
        "naive hPSC",
    ),
    "te-like": ("trophectoderm-like", "TE like"),
    "te-like cells": ("trophectoderm-like cells", "TE like cells"),
    "egg cells": ("egg cell", "female gametes", "female gamete"),
    "sperm cells": ("sperm cell", "male gametes", "male gamete"),
}

# These patterns are deliberately narrower than generic low-input/bulk
# language. A hit indicates that material from multiple biological cells may
# have been combined into one proteomic sample, which is directly relevant to
# the project's definition of a true individual-cell measurement.
MEASUREMENT_POOLING_PATTERNS = (
    # Explicit pooled/combined cell material. Context filters below decide
    # whether the pooling concerns the target proteomic measurement.
    re.compile(
        r"\b(?:pooled|combined)\s+(?:the\s+)?(?:\d+\s+)?"
        r"(?:isolated\s+)?(?:single[- ]?)?(?:cells?|egg\s+cells?|"
        r"sperm\s+cells?|oocytes?|gametes?|neurons?|bacteria|bacterium)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:single[- ]?)?(?:cells?|egg\s+cells?|sperm\s+cells?|"
        r"oocytes?|gametes?|neurons?|bacteria|bacterium)\b\s+"
        r"(?:were|was|are|is|were\s+then|was\s+then)?\s*"
        r"(?:pooled|combined)\b",
        re.I,
    ),
    # Strong multi-cell-to-one-container evidence. This is the highest-value
    # pattern for destructive pre-measurement pooling such as PXD000265.
    re.compile(
        r"\b(?:\d{2,}|twenty|thirty|forty|fifty|sixty|seventy|eighty|"
        r"ninety|hundred)(?:\s*(?:to|[-–])\s*(?:\d{2,}|twenty|thirty|"
        r"forty|fifty|sixty|seventy|eighty|ninety|hundred))?\s+"
        r"(?:isolated\s+)?(?:single[- ]?)?(?:cells?|egg\s+cells?|"
        r"sperm\s+cells?|oocytes?|gametes?|neurons?|bacteria)\b.{0,100}"
        r"\b(?:were\s+)?(?:transferred|combined|pooled)\b.{0,100}"
        r"\b(?:into|in|to)\s+(?:a|an|one|the)\s+[^.;]{0,70}"
        r"\b(?:droplet|tube|vial|well|sample|reaction|buffer|container)\b",
        re.I | re.S,
    ),
)

# Pooling is not automatically a contradiction. These contexts describe
# another assay/prior method or multiplexing that preserves single-cell
# identity after cells have already been separately processed/labeled.
POOLING_TRANSCRIPTOMICS_CONTEXT_RE = re.compile(
    r"\b(?:transcriptom(?:e|es|ics|ic)|RNA[- ]?seq|whole[- ]transcriptome|"
    r"single[- ]cell\s+RNA|sequencing\s+analysis)\b",
    re.I,
)
POOLING_PRIOR_METHOD_CONTEXT_RE = re.compile(
    r"\b(?:original|previous|previously|prior|earlier|historical|formerly)\b"
    r".{0,120}\b(?:approach|method|workflow|study|work|analysis)\b"
    r"|\b(?:approach|method|workflow|study|work)\b.{0,120}"
    r"\b(?:previous|previously|prior|earlier|original)\b",
    re.I | re.S,
)
POOLING_IDENTITY_PRESERVING_MULTIPLEX_RE = re.compile(
    r"\b(?:booster|carrier(?:\s+channel)?|reference\s+channel|TMT(?:pro)?|"
    r"isobaric|plex(?:ed|ing)?|multiplex(?:ed|ing)?|reporter\s+ion|"
    r"label(?:ed|led|ing)|tag(?:ged|ging))\b",
    re.I,
)

BENCHMARK_EXCLUSION_TERMS = (
    "single-cell-equivalent",
    "single cell equivalent",
    "single-cell-level input",
    "single cell level input",
    "diluted bulk",
    "bulk digest",
    "low-input benchmark",
    "low input benchmark",
)

STRUCTURED_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["pass", "warn", "fail"],
        },
        "evidence_support": {
            "type": "string",
            "enum": ["direct", "indirect", "partial", "absent", "contradictory"],
        },
        "reason": {
            "type": "string",
            "maxLength": 320,
        },
        "proposed_value": {
            "type": ["string", "null"],
            "maxLength": 240,
        },
        "evidence_ids": {
            "type": "array",
            "maxItems": 6,
            "items": {"type": "string", "maxLength": 20},
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
        },
    },
    "required": [
        "decision",
        "evidence_support",
        "reason",
        "proposed_value",
        "evidence_ids",
        "confidence",
    ],
}


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Stage 06: independent grounded LLM QC of the curated PRIDE SCP "
            "catalogue. Stage-05 outputs are never modified."
        )
    )
    parser.add_argument(
        "catalogue_dir",
        nargs="?",
        default="pride_scp_catalogue",
    )
    parser.add_argument(
        "--base-dir",
        default=".",
        help="Base directory for resolving relative publication PDF paths.",
    )
    parser.add_argument(
        "--output-dir",
        default="pride_scp_qc_review",
    )
    parser.add_argument(
        "--fact-model",
        default=DEFAULT_FACT_MODEL,
        help=(
            "Grounded factuality model (default: bespoke-minicheck). "
            "Any model that reliably returns Yes/No may be substituted."
        ),
    )
    parser.add_argument(
        "--critic-model",
        default=DEFAULT_CRITIC_MODEL,
        help=(
            "Blind structured adjudicator used only after MiniCheck No or "
            "deterministic high-risk evidence "
            f"(default: {DEFAULT_CRITIC_MODEL}). Use 'none' to disable."
        ),
    )
    parser.add_argument(
        "--jury-model",
        default=DEFAULT_JURY_MODEL,
        help=(
            "Second blind structured adjudicator used only after MiniCheck No "
            "or deterministic high-risk evidence "
            f"(default: {DEFAULT_JURY_MODEL}). Use 'none' to disable."
        ),
    )
    parser.add_argument(
        "--ollama-url",
        default=DEFAULT_OLLAMA_URL,
    )
    parser.add_argument(
        "--fact-keep-alive",
        default=DEFAULT_FACT_KEEP_ALIVE,
        help=(
            "Ollama residency for MiniCheck on ordinary claims (default: 5m). "
            "It is explicitly unloaded before structured adjudication unless "
            "--keep-fact-loaded-during-adjudication is set."
        ),
    )
    parser.add_argument(
        "--critic-keep-alive",
        default=DEFAULT_CRITIC_KEEP_ALIVE,
        help="Ollama residency for Phi adjudication (default: 0 = unload immediately).",
    )
    parser.add_argument(
        "--jury-keep-alive",
        default=DEFAULT_JURY_KEEP_ALIVE,
        help="Ollama residency for Gemma adjudication (default: 0 = unload immediately).",
    )
    parser.add_argument(
        "--keep-fact-loaded-during-adjudication",
        action="store_true",
        help=(
            "Do not explicitly unload the fact model before Phi/Gemma. Disabled "
            "by default for memory safety on CPU-only machines."
        ),
    )
    parser.add_argument(
        "--ollama-retries",
        type=int,
        default=DEFAULT_OLLAMA_RETRIES,
        help="Retries after transient Ollama connection/5xx failures (default: 2).",
    )
    parser.add_argument(
        "--ollama-retry-backoff",
        type=float,
        default=DEFAULT_OLLAMA_RETRY_BACKOFF,
        help="Initial exponential retry backoff in seconds (default: 3).",
    )
    parser.add_argument(
        "--continue-after-ollama-unavailable",
        action="store_true",
        help=(
            "Continue to later claims after Ollama remains unreachable after "
            "retries. By default Stage 06 stops early to avoid a cascade of "
            "identical NOT_REVIEWED rows."
        ),
    )
    parser.add_argument(
        "--cpu-threads",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--num-ctx",
        type=int,
        default=DEFAULT_NUM_CTX,
    )
    parser.add_argument(
        "--critic-num-predict",
        type=int,
        default=DEFAULT_CRITIC_NUM_PREDICT,
    )
    parser.add_argument(
        "--jury-num-predict",
        type=int,
        default=DEFAULT_JURY_NUM_PREDICT,
    )
    parser.add_argument(
        "--deliberation-rounds",
        type=int,
        choices=[0, 1],
        default=0,
        help=(
            "Experimental anonymized reconsideration rounds after an unresolved "
            "adjudication. Hard-limited to 0 or 1 and OFF by default because "
            "the v3 pilot showed model-to-model deliberation increased WARNs."
        ),
    )
    parser.add_argument(
        "--deliberation-num-predict",
        type=int,
        default=DEFAULT_DELIBERATION_NUM_PREDICT,
    )
    parser.add_argument(
        "--evidence-chars",
        type=int,
        default=DEFAULT_EVIDENCE_CHARS,
    )
    parser.add_argument(
        "--snippet-chars",
        type=int,
        default=DEFAULT_SNIPPET_CHARS,
    )
    parser.add_argument(
        "--scope",
        default="core",
        choices=["core", "technical", "all"],
        help=(
            "core = accession/SCP mapping + SCP classification + samples + "
            "organisms + counts + low-input benchmarks; technical = non-empty technical metadata; "
            "all = both (default: core)."
        ),
    )
    parser.add_argument(
        "--accession",
        action="append",
        default=[],
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
    )
    parser.add_argument(
        "--force",
        action="store_true",
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build evidence packets without calling Ollama.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
    )
    parser.add_argument(
        "--save-pdf-cache",
        action="store_true",
    )
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(
    path: Path,
    rows: list[dict[str, Any]],
    fieldnames: list[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: "" if value is None else value
                    for key, value in row.items()
                }
            )


def text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def split_joined(value: Any) -> list[str]:
    value = text(value)
    if not value:
        return []
    return [
        part.strip()
        for part in value.split(";")
        if part.strip()
    ]


def safe_json(value: Any, default):
    if isinstance(value, (dict, list)):
        return value
    value = text(value)
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def stable_hash(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def model_is_disabled(value: str | None) -> bool:
    return text(value).lower() in {"", "none", "off", "false", "0"}


def load_catalogue(catalogue_dir: Path):
    datasets = read_csv(catalogue_dir / "single_cell_dataset_catalogue.csv")
    samples = read_csv(catalogue_dir / "single_cell_samples.csv")
    benchmarks = read_csv(catalogue_dir / "low_input_benchmarks.csv")
    publications = read_csv(
        catalogue_dir / "single_cell_publication_catalogue.csv"
    )
    raw_samples = read_csv(
        catalogue_dir / "single_cell_samples_raw_candidates.csv"
    )
    raw_benchmarks = read_csv(
        catalogue_dir / "low_input_benchmarks_raw_candidates.csv"
    )

    if not datasets:
        raise FileNotFoundError(
            f"No dataset catalogue found under: {catalogue_dir}"
        )

    grouped = {
        "samples": defaultdict(list),
        "benchmarks": defaultdict(list),
        "publications": defaultdict(list),
        "raw_samples": defaultdict(list),
        "raw_benchmarks": defaultdict(list),
    }

    for row in samples:
        grouped["samples"][row.get("accession", "")].append(row)
    for row in benchmarks:
        grouped["benchmarks"][row.get("accession", "")].append(row)
    for row in publications:
        grouped["publications"][row.get("accession", "")].append(row)
    for row in raw_samples:
        grouped["raw_samples"][row.get("accession", "")].append(row)
    for row in raw_benchmarks:
        grouped["raw_benchmarks"][row.get("accession", "")].append(row)

    return datasets, grouped


def resolve_existing_path(
    raw_path: str,
    *,
    base_dir: Path,
    catalogue_dir: Path,
) -> Path | None:
    raw_path = text(raw_path)
    if not raw_path:
        return None

    path = Path(raw_path)
    candidates = []

    if path.is_absolute():
        candidates.append(path)
    else:
        candidates.extend(
            [
                base_dir / path,
                catalogue_dir.parent / path,
                catalogue_dir / path,
            ]
        )

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    return None


def current_annotation_paths(dataset_row: dict[str, str]) -> list[Path]:
    paths = []
    for raw in split_joined(dataset_row.get("source_annotation_paths")):
        path = Path(raw)
        if path.is_file():
            paths.append(path)
    return paths


def annotation_source_fragments(
    dataset_row: dict[str, str],
) -> list[dict[str, Any]]:
    fragments = []

    for path in current_annotation_paths(dataset_row):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        gate = (
            data.get("provenance", {})
            .get("scp_evidence_gate", {})
            or {}
        )
        gate_evidence = text(gate.get("evidence"))
        if gate_evidence:
            fragments.append(
                {
                    "source": f"annotation_gate:{path.name}",
                    "page": None,
                    "text": gate_evidence,
                }
            )

        evidence_file = (
            data.get("provenance", {})
            .get("semantic_evidence_file")
        )
        if evidence_file:
            evidence_path = Path(str(evidence_file))
            if evidence_path.is_file():
                try:
                    bundle = json.loads(
                        evidence_path.read_text(encoding="utf-8")
                    )
                except Exception:
                    bundle = {}
                for task_name, records in (
                    bundle.get("tasks", {}) or {}
                ).items():
                    for record in records or []:
                        snippet = text(record.get("text"))
                        if snippet:
                            fragments.append(
                                {
                                    "source": (
                                        f"annotation_evidence:{task_name}:"
                                        f"{record.get('block_id', '')}"
                                    ),
                                    "page": record.get("page"),
                                    "text": snippet,
                                }
                            )

    return fragments


def extract_pdf_pages(
    pdf_path: Path,
    *,
    cache_dir: Path,
    save_cache: bool,
) -> list[dict[str, Any]]:
    stat = pdf_path.stat()
    cache_key = stable_hash(
        f"{pdf_path.resolve()}::{stat.st_size}::{stat.st_mtime_ns}",
        24,
    )
    cache_path = cache_dir / f"{cache_key}.json"

    if cache_path.is_file():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    doc = pymupdf.open(pdf_path)
    pages = []

    for page_index, page in enumerate(doc, start=1):
        page_text = text(page.get_text("text", sort=True))
        if page_text:
            pages.append(
                {
                    "page": page_index,
                    "text": page_text,
                }
            )

    doc.close()

    if save_cache:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(pages, ensure_ascii=False),
            encoding="utf-8",
        )

    return pages


def build_source_index(
    dataset_row: dict[str, str],
    publication_rows: list[dict[str, str]],
    *,
    base_dir: Path,
    catalogue_dir: Path,
    cache_dir: Path,
    save_pdf_cache: bool,
) -> dict[str, Any]:
    sources = []
    pdf_paths = []

    title_bits = [
        text(dataset_row.get("dataset_title")),
        text(dataset_row.get("publication_titles")),
    ]
    title_bits = [value for value in title_bits if value]
    if title_bits:
        sources.append(
            {
                "source": "authoritative_titles",
                "page": None,
                "text": " | ".join(title_bits),
            }
        )

    for row in publication_rows:
        screen_evidence = text(row.get("scp_screen_evidence"))
        if screen_evidence:
            sources.append(
                {
                    "source": (
                        "stage3_screen:"
                        + text(row.get("publication_doi"))
                    ),
                    "page": None,
                    "text": screen_evidence,
                }
            )

        pdf_path = resolve_existing_path(
            row.get("pdf_path", ""),
            base_dir=base_dir,
            catalogue_dir=catalogue_dir,
        )
        if not pdf_path:
            continue

        pdf_paths.append(str(pdf_path))
        try:
            pages = extract_pdf_pages(
                pdf_path,
                cache_dir=cache_dir,
                save_cache=save_pdf_cache,
            )
        except Exception as exc:
            sources.append(
                {
                    "source": f"pdf_error:{pdf_path.name}",
                    "page": None,
                    "text": f"PDF extraction error: {type(exc).__name__}: {exc}",
                }
            )
            continue

        doi = text(row.get("publication_doi"))
        for page in pages:
            sources.append(
                {
                    "source": f"pdf:{doi or pdf_path.name}",
                    "page": page["page"],
                    "text": page["text"],
                }
            )

    sources.extend(annotation_source_fragments(dataset_row))

    return {
        "sources": sources,
        "pdf_paths": sorted(set(pdf_paths)),
    }


def useful_terms(value: str) -> list[str]:
    value = text(value)
    if not value:
        return []

    terms = [value]

    cleaned = re.sub(
        r"\b(?:single|individual|cells?|cell line|protein|proteome)\b",
        " ",
        value,
        flags=re.I,
    )
    cleaned = text(cleaned)
    if len(cleaned) >= 3:
        terms.append(cleaned)

    for token in re.findall(
        r"\b[A-Za-z][A-Za-z0-9.+/-]{2,}\b",
        value,
    ):
        if token.lower() not in {
            "single",
            "cell",
            "cells",
            "protein",
            "proteins",
            "proteome",
            "proteomics",
            "the",
            "and",
            "with",
        }:
            terms.append(token)

    return list(dict.fromkeys(terms))


def retrieval_alias_terms(value: str) -> list[str]:
    """Return conservative retrieval-only aliases for a curated value."""
    value = text(value)
    if not value:
        return []

    out = [value]
    lower = value.lower()

    for key, aliases in RETRIEVAL_ALIASES.items():
        if lower == key or key in lower:
            out.extend(aliases)

    # Very small morphology expansion helps exact sample matching without
    # turning generic SCP words into high-priority search terms.
    if lower.endswith(" cells"):
        out.append(value[:-1])
    elif lower.endswith(" cell"):
        out.append(value + "s")

    return list(dict.fromkeys(text(item) for item in out if text(item)))


def evidence_lane(
    name: str,
    *,
    terms: list[str] | tuple[str, ...] = (),
    groups: list[list[str]] | None = None,
    priority: int = 50,
    max_items: int = 1,
    pattern_group: str = "",
    high_risk: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "terms": list(dict.fromkeys(text(term) for term in terms if text(term))),
        "groups": [
            list(dict.fromkeys(text(term) for term in group if text(term)))
            for group in (groups or [])
            if any(text(term) for term in group)
        ],
        "priority": int(priority),
        "max_items": int(max_items),
        "pattern_group": pattern_group,
        "high_risk": bool(high_risk),
    }


def count_terms(value: str) -> list[str]:
    value = text(value)
    if not value:
        return []
    terms = [value]
    terms.extend(re.findall(r"\b\d[\d,]*(?:\.\d+)?\b", value))
    return list(dict.fromkeys(terms))


def claim_record(
    *,
    accession: str,
    category: str,
    record_type: str,
    record_id: str,
    field: str,
    value: str,
    claim_text: str,
    search_terms: list[str],
    evidence_lanes: list[dict[str, Any]] | None = None,
    provenance: str = "",
) -> dict[str, Any]:
    raw_id = f"{accession}|{category}|{record_type}|{record_id}|{field}|{value}"
    return {
        "claim_id": f"C_{stable_hash(raw_id, 14)}",
        "accession": accession,
        "category": category,
        "record_type": record_type,
        "record_id": record_id,
        "field": field,
        "value": value,
        "claim_text": claim_text,
        "search_terms": list(dict.fromkeys(
            [term for term in search_terms if text(term)]
        )),
        "evidence_lanes": evidence_lanes or [],
        "provenance": provenance,
    }


def build_core_claims(
    dataset_row: dict[str, str],
    samples: list[dict[str, str]],
    benchmarks: list[dict[str, str]],
) -> list[dict[str, Any]]:
    accession = dataset_row["accession"]
    claims = []

    # Keep accession scoping distinct from the biological classification. This
    # makes both review units substantially more atomic and prevents every
    # sample/count claim from having to re-prove repository mapping.
    support_scope = text(dataset_row.get("scp_support_scopes")).lower()
    target_specific_scope = "target_accession" in support_scope
    mapping_claim_text = (
        f"The publication maps its individual-cell proteomics data to PRIDE "
        f"accession {accession}."
        if target_specific_scope
        else (
            f"The publication identifies PRIDE accession {accession} as a "
            "deposited proteomics dataset."
        )
    )

    claims.append(
        claim_record(
            accession=accession,
            category="mapping",
            record_type="dataset",
            record_id=accession,
            field="scp_accession_mapping",
            value=accession,
            claim_text=mapping_claim_text,
            search_terms=[accession, "single-cell", "single cell", "proteomics"],
            evidence_lanes=[
                evidence_lane(
                    "accession_mapping",
                    terms=[accession],
                    groups=[[accession]],
                    priority=120,
                    max_items=2,
                ),
                evidence_lane(
                    "scp_context",
                    terms=[
                        "single-cell proteomics",
                        "single cell proteomics",
                        "single-cell data",
                        "single cell data",
                        "individual cells",
                    ],
                    priority=70,
                    max_items=1,
                ),
            ],
            provenance=(
                f"tier={text(dataset_row.get('scp_evidence_tiers'))}; "
                f"scope={text(dataset_row.get('scp_support_scopes'))}"
            ),
        )
    )

    claims.append(
        claim_record(
            accession=accession,
            category="classification",
            record_type="dataset",
            record_id=accession,
            field="is_single_cell_proteomics",
            value="yes",
            claim_text=(
                "The publication reports proteomic measurements of individual "
                "biological cells."
            ),
            search_terms=[*GENERIC_SCP_TERMS],
            evidence_lanes=[
                evidence_lane(
                    "individual_cell_measurement",
                    terms=[
                        "individual cells",
                        "individual cell",
                        "single cells",
                        "single cell",
                        "single bacterium",
                        "single bacteria",
                        "single neuron",
                        "single oocyte",
                        "single-cell proteomics",
                        "single cell proteomics",
                    ],
                    groups=[[
                        "individual cells",
                        "individual cell",
                        "single cells",
                        "single cell",
                        "single bacterium",
                        "single bacteria",
                        "single neuron",
                        "single oocyte",
                    ]],
                    priority=105,
                    max_items=2,
                ),
                evidence_lane(
                    "ms_proteomics",
                    terms=[
                        "proteomics",
                        "proteome",
                        "mass spectrometry",
                        "LC-MS",
                        "LC-MS/MS",
                        "MS/MS",
                    ],
                    priority=75,
                    max_items=1,
                ),
                evidence_lane(
                    "measurement_pooling_contradiction",
                    pattern_group="measurement_pooling",
                    priority=130,
                    max_items=2,
                    high_risk=True,
                ),
                evidence_lane(
                    "benchmark_exclusion_context",
                    terms=list(BENCHMARK_EXCLUSION_TERMS),
                    priority=55,
                    max_items=1,
                ),
            ],
            provenance=(
                f"tier={text(dataset_row.get('scp_evidence_tiers'))}; "
                f"scope={text(dataset_row.get('scp_support_scopes'))}"
            ),
        )
    )

    for sample in samples:
        sample_index = text(sample.get("sample_index")) or "?"
        sample_type = text(sample.get("sample_type"))
        organism = text(sample.get("organism"))
        counts = text(sample.get("cell_counts_reported"))
        record_id = f"sample:{sample_index}"
        sample_aliases = retrieval_alias_terms(sample_type)
        individual_terms = [
            "single cell",
            "single-cell",
            "individual cell",
            "individual cells",
            "single cells",
            "one cell",
            "single-cell proteomics",
            "single cell proteomics",
        ]

        claims.append(
            claim_record(
                accession=accession,
                category="sample",
                record_type="sample",
                record_id=record_id,
                field="sample_type",
                value=sample_type,
                claim_text=(
                    f"The publication reports individual-cell proteomic "
                    f"measurements of {sample_type}."
                ),
                search_terms=sample_aliases,
                evidence_lanes=[
                    evidence_lane(
                        "exact_sample_measurement",
                        terms=[*sample_aliases, *individual_terms],
                        groups=[sample_aliases, individual_terms],
                        priority=115,
                        max_items=3,
                    ),
                ],
                provenance=(
                    f"status={text(sample.get('sample_metadata_status'))}; "
                    f"sources={text(sample.get('sample_metadata_sources'))}; "
                    f"origins={text(sample.get('candidate_origins'))}"
                ),
            )
        )

        if organism:
            organism_aliases = retrieval_alias_terms(organism)
            claims.append(
                claim_record(
                    accession=accession,
                    category="sample",
                    record_type="sample",
                    record_id=record_id,
                    field="organism",
                    value=organism,
                    claim_text=(
                        f"The {sample_type} used in the individual-cell "
                        f"experiment are {organism}."
                    ),
                    search_terms=[*sample_aliases, *organism_aliases],
                    evidence_lanes=[
                        evidence_lane(
                            "sample_organism",
                            terms=[*sample_aliases, *organism_aliases],
                            groups=[sample_aliases, organism_aliases],
                            priority=120,
                            max_items=3,
                        ),
                    ],
                    provenance=(
                        f"organism_resolution="
                        f"{text(sample.get('organism_resolution'))}; "
                        f"qc_notes={text(sample.get('qc_notes'))}"
                    ),
                )
            )

        if counts:
            counts_search = count_terms(counts)
            claims.append(
                claim_record(
                    accession=accession,
                    category="sample",
                    record_type="sample",
                    record_id=record_id,
                    field="cell_counts_reported",
                    value=counts,
                    claim_text=(
                        f"The publication reports {counts} for {sample_type}."
                    ),
                    search_terms=[*sample_aliases, *counts_search],
                    evidence_lanes=[
                        evidence_lane(
                            "sample_count",
                            terms=[*sample_aliases, *counts_search],
                            groups=[sample_aliases, counts_search],
                            priority=125,
                            max_items=3,
                        ),
                    ],
                    provenance=text(sample.get("candidate_origins")),
                )
            )

    for benchmark in benchmarks:
        idx = text(benchmark.get("benchmark_index")) or "?"
        sample = text(benchmark.get("sample"))
        amount = text(benchmark.get("input_amount"))
        organism = text(benchmark.get("organism"))
        record_id = f"benchmark:{idx}"
        sample_aliases = retrieval_alias_terms(sample)
        amount_terms = count_terms(amount)

        claims.append(
            claim_record(
                accession=accession,
                category="benchmark",
                record_type="benchmark",
                record_id=record_id,
                field="sample_and_input_amount",
                value=f"{sample} | {amount}",
                claim_text=(
                    f"The publication reports a low-input proteomics benchmark "
                    f"using {sample} with total input {amount}."
                ),
                search_terms=[*sample_aliases, *amount_terms],
                evidence_lanes=[
                    evidence_lane(
                        "benchmark_sample_amount",
                        terms=[
                            *sample_aliases,
                            *amount_terms,
                            "low input",
                            "low-input",
                            "benchmark",
                            "dilution",
                            "diluted bulk",
                        ],
                        groups=[sample_aliases, amount_terms],
                        priority=115,
                        max_items=3,
                    ),
                ],
                provenance=text(benchmark.get("qc_status")),
            )
        )

        if organism:
            organism_aliases = retrieval_alias_terms(organism)
            claims.append(
                claim_record(
                    accession=accession,
                    category="benchmark",
                    record_type="benchmark",
                    record_id=record_id,
                    field="organism",
                    value=organism,
                    claim_text=(
                        f"The low-input benchmark sample {sample} is from "
                        f"{organism}."
                    ),
                    search_terms=[*sample_aliases, *organism_aliases],
                    evidence_lanes=[
                        evidence_lane(
                            "benchmark_organism",
                            terms=[*sample_aliases, *organism_aliases],
                            groups=[sample_aliases, organism_aliases],
                            priority=115,
                            max_items=3,
                        ),
                    ],
                    provenance=text(benchmark.get("qc_status")),
                )
            )

    return claims


def technical_claim_text(
    accession: str,
    field: str,
    value: str,
) -> str:
    templates = {
        "sample_preparation": (
            f"The target single-cell proteomics experiment for {accession} "
            f"used this sample-preparation protocol: {value}."
        ),
        "labeling_strategy": (
            f"The target single-cell proteomics experiment for {accession} "
            f"used the labeling strategy {value}."
        ),
        "mass_spectrometers": (
            f"The target single-cell proteomics experiment for {accession} "
            f"used the mass spectrometer {value}."
        ),
        "acquisition_modes": (
            f"The target single-cell proteomics experiment for {accession} "
            f"used the acquisition mode {value}."
        ),
        "lc_configuration": (
            f"The target single-cell proteomics experiment for {accession} "
            f"used this LC configuration: {value}."
        ),
        "lc_gradient": (
            f"The target single-cell proteomics experiment for {accession} "
            f"used this LC gradient: {value}."
        ),
        "single_cell_throughput": (
            f"The publication reports single-cell throughput for {accession} "
            f"as {value}."
        ),
        "single_cell_proteome_depth": (
            f"The publication reports genuine single-cell proteome depth for "
            f"{accession} as {value}."
        ),
        "low_input_proteome_depth": (
            f"The publication reports low-input benchmark proteome depth for "
            f"{accession} as {value}."
        ),
        "analysis_software": (
            f"The target experiment associated with {accession} was analyzed "
            f"using {value}."
        ),
        "analysis_strategies": (
            f"The target experiment associated with {accession} used the "
            f"analysis strategy {value}."
        ),
    }
    return templates.get(
        field,
        f"For accession {accession}, the field {field} has value {value}.",
    )


def build_technical_claims(
    dataset_row: dict[str, str],
) -> list[dict[str, Any]]:
    accession = dataset_row["accession"]
    claims = []

    split_fields = {
        "labeling_strategy",
        "mass_spectrometers",
        "acquisition_modes",
        "analysis_software",
        "analysis_strategies",
    }

    for field in TECHNICAL_FIELDS:
        raw = text(dataset_row.get(field))
        if not raw:
            continue

        if field == "single_cell_isolation_json":
            groups = safe_json(raw, [])
            for idx, group in enumerate(groups, start=1):
                if not isinstance(group, dict):
                    continue
                method = text(group.get("method"))
                samples = [
                    text(value)
                    for value in group.get("samples", []) or []
                    if text(value)
                ]
                if not method:
                    continue
                sample_text = ", ".join(samples) if samples else "the single cells"
                value = f"{method} | {sample_text}"
                claims.append(
                    claim_record(
                        accession=accession,
                        category="technical",
                        record_type="dataset",
                        record_id=f"isolation:{idx}",
                        field=field,
                        value=value,
                        claim_text=(
                            f"For {accession}, {sample_text} were physically "
                            f"isolated or dispensed using {method}."
                        ),
                        search_terms=[
                            method,
                            *samples,
                            "sorted",
                            "isolated",
                            "FACS",
                            "CellenONE",
                            "single cell",
                        ],
                    )
                )
            continue

        if field == "ion_mobility_or_faims_json":
            items = safe_json(raw, [])
            for idx, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    continue
                technology = text(item.get("technology"))
                role = text(item.get("role"))
                if not technology:
                    continue
                value = technology if not role else f"{technology} ({role})"
                claims.append(
                    claim_record(
                        accession=accession,
                        category="technical",
                        record_type="dataset",
                        record_id=f"ion_mobility:{idx}",
                        field=field,
                        value=value,
                        claim_text=(
                            f"The target experiment associated with {accession} "
                            f"used {technology}"
                            + (f" with role {role}." if role else ".")
                        ),
                        search_terms=[
                            technology,
                            role,
                            "ion mobility",
                            "FAIMS",
                            "TIMS",
                        ],
                    )
                )
            continue

        values = split_joined(raw) if field in split_fields else [raw]

        for idx, value in enumerate(values, start=1):
            claims.append(
                claim_record(
                    accession=accession,
                    category="technical",
                    record_type="dataset",
                    record_id=f"{field}:{idx}",
                    field=field,
                    value=value,
                    claim_text=technical_claim_text(
                        accession,
                        field,
                        value,
                    ),
                    search_terms=[
                        *useful_terms(value),
                        field.replace("_", " "),
                    ],
                )
            )

    return claims


def build_claims_for_dataset(
    dataset_row: dict[str, str],
    grouped: dict[str, Any],
    scope: str,
) -> list[dict[str, Any]]:
    accession = dataset_row["accession"]
    claims = []

    if scope in {"core", "all"}:
        claims.extend(
            build_core_claims(
                dataset_row,
                grouped["samples"].get(accession, []),
                grouped["benchmarks"].get(accession, []),
            )
        )

    if scope in {"technical", "all"}:
        claims.extend(build_technical_claims(dataset_row))

    return claims


def term_occurrences(lower: str, term: str) -> list[int]:
    term = text(term).lower()
    if len(term) < 2:
        return []
    positions = []
    start = 0
    while True:
        pos = lower.find(term, start)
        if pos < 0:
            break
        positions.append(pos)
        start = pos + max(1, len(term))
    return positions


def clip_window(
    source_text: str,
    center: int,
    *,
    snippet_chars: int,
) -> str:
    half = snippet_chars // 2
    start = max(0, center - half)
    end = min(len(source_text), start + snippet_chars)

    if end - start < snippet_chars:
        start = max(0, end - snippet_chars)

    snippet = source_text[start:end]

    if start > 0:
        boundaries = [
            pos
            for pos in (
                snippet.find(". "),
                snippet.find("; "),
            )
            if 0 <= pos <= 120
        ]
        if boundaries:
            snippet = snippet[min(boundaries) + 2:]

    if end < len(source_text):
        tails = [
            pos
            for pos in (
                snippet.rfind(". "),
                snippet.rfind("; "),
            )
            if pos >= len(snippet) - 140
        ]
        if tails:
            snippet = snippet[:max(tails) + 1]

    return text(snippet)


def _contains_term(lower: str, term: str) -> bool:
    return text(term).lower() in lower


def _pooling_context_is_excluded(
    source_text: str,
    match_start: int,
    match_end: int,
) -> bool:
    # Inspect a local window so unrelated material elsewhere on the same PDF
    # page cannot suppress a genuine pooling event.
    start = max(0, match_start - 260)
    end = min(len(source_text), match_end + 260)
    context = source_text[start:end]

    # PXD019958 regression: pooling in prior transcriptomics/RNA-seq work is
    # not evidence that the target proteomics experiment destroyed cell identity.
    if POOLING_TRANSCRIPTOMICS_CONTEXT_RE.search(context):
        return True

    # PXD038699 regression: descriptions of an original/prior pooled-cell
    # method are background comparisons, not necessarily the target experiment.
    if POOLING_PRIOR_METHOD_CONTEXT_RE.search(context):
        return True

    # PXD020586 regression: after separately prepared/labeled single cells are
    # multiplexed with a booster/carrier channel, physical pooling for a common
    # MS injection can preserve per-cell reporter identity.
    if POOLING_IDENTITY_PRESERVING_MULTIPLEX_RE.search(context):
        return True

    return False


def _measurement_pooling_matches(
    source_text: str,
) -> list[tuple[int, str]]:
    matches = []
    seen = set()
    for pattern in MEASUREMENT_POOLING_PATTERNS:
        for match in pattern.finditer(source_text):
            if _pooling_context_is_excluded(
                source_text,
                match.start(),
                match.end(),
            ):
                continue
            center = match.start() + max(1, len(match.group(0)) // 2)
            label = text(match.group(0))[:180]
            key = (center, label.lower())
            if key in seen:
                continue
            seen.add(key)
            matches.append((center, label))
    return matches[:12]


def _pattern_matches(
    source_text: str,
    pattern_group: str,
) -> list[tuple[int, str]]:
    if pattern_group != "measurement_pooling":
        return []
    return _measurement_pooling_matches(source_text)


def _lane_candidates(
    source_index: dict[str, Any],
    lane: dict[str, Any],
    *,
    snippet_chars: int,
) -> list[dict[str, Any]]:
    terms = [text(term) for term in lane.get("terms", []) if len(text(term)) >= 2]
    groups = lane.get("groups", []) or []
    priority = int(lane.get("priority", 50))
    candidates = []

    for source in source_index["sources"]:
        source_text = text(source.get("text"))
        if not source_text:
            continue
        if source.get("source", "").startswith("pdf_error:"):
            continue

        lower = source_text.lower()
        hit_centers: list[tuple[int, str]] = []

        # Cap per-term hits rather than truncating one global term-ordered list.
        # This prevents an early generic term from crowding out exact values.
        for term in terms:
            for pos in term_occurrences(lower, term)[:2]:
                hit_centers.append((pos + len(term) // 2, term))

        pattern_group = text(lane.get("pattern_group"))
        if pattern_group:
            hit_centers.extend(_pattern_matches(source_text, pattern_group))

        if not hit_centers:
            continue

        # Deduplicate nearby centers while preserving term diversity.
        compact_centers = []
        seen_centers = []
        for center, matched_term in hit_centers:
            if any(abs(center - old) < max(40, snippet_chars // 5) for old in seen_centers):
                continue
            compact_centers.append((center, matched_term))
            seen_centers.append(center)

        for center, matched_term in compact_centers:
            snippet = clip_window(
                source_text,
                center,
                snippet_chars=snippet_chars,
            )
            if not snippet:
                continue

            snippet_lower = snippet.lower()
            matched_terms = [term for term in terms if _contains_term(snippet_lower, term)]
            group_hits = []
            for group in groups:
                group_hits.append(
                    [term for term in group if _contains_term(snippet_lower, term)]
                )

            # Candidate score depends on the actual snippet, not all terms that
            # happened to occur elsewhere on the page/source.
            score = priority
            score += min(len(matched_terms), 6) * 5
            score += sum(18 for hits in group_hits if hits)
            if groups and all(group_hits):
                score += 28

            if re.search(r"\bPXD\d{6,}\b", snippet, re.I):
                score += 8
            if any(term.lower() in snippet_lower for term in GENERIC_SCP_TERMS):
                score += 6
            if re.search(r"\b(?:mass spectrometry|LC[- ]?MS(?:/MS)?|MS/MS|proteomics?)\b", snippet, re.I):
                score += 6

            pattern_hit = False
            if pattern_group == "measurement_pooling":
                pattern_hit = bool(_measurement_pooling_matches(snippet))
                if pattern_hit:
                    score += 35

            source_name = text(source.get("source"))
            if source_name.startswith("pdf:"):
                score += 4
            elif source_name == "authoritative_titles":
                # Titles are useful corroboration but should not dominate
                # methods/results passages for biological claims.
                score += 1

            candidates.append(
                {
                    "score": score,
                    "lane": lane.get("name", "general"),
                    "lane_priority": priority,
                    "high_risk": bool(lane.get("high_risk")) and pattern_hit,
                    "source": source.get("source"),
                    "page": source.get("page"),
                    "matched_term": matched_term,
                    "matched_terms": matched_terms,
                    "group_match_count": sum(1 for hits in group_hits if hits),
                    "text": snippet,
                }
            )

    candidates.sort(
        key=lambda item: (
            -item["score"],
            -item["group_match_count"],
            str(item["source"]),
            item["page"] or 0,
        )
    )
    return candidates


def _evidence_key(item: dict[str, Any]) -> str:
    return re.sub(r"\W+", " ", item["text"].lower()).strip()


def retrieve_evidence(
    source_index: dict[str, Any],
    claim: dict[str, Any],
    *,
    max_chars: int,
    snippet_chars: int,
) -> list[dict[str, Any]]:
    lanes = claim.get("evidence_lanes", []) or []

    if not lanes:
        # Technical claims and any legacy callers retain a simple lane, but use
        # the new snippet-local scoring/selection behavior.
        lanes = [
            evidence_lane(
                "general",
                terms=[
                    text(term)
                    for term in claim.get("search_terms", [])
                    if len(text(term)) >= 2
                ],
                priority=60,
                max_items=6,
            )
        ]

    lane_candidates = {
        lane["name"]: _lane_candidates(
            source_index,
            lane,
            snippet_chars=snippet_chars,
        )
        for lane in lanes
    }

    selected: list[dict[str, Any]] = []
    seen = set()
    used = 0

    def try_add(item: dict[str, Any]) -> bool:
        nonlocal used
        key = _evidence_key(item)
        if not key or key in seen:
            return False

        cost = len(item["text"]) + 115
        if used + cost > max_chars:
            return False

        selected.append(item)
        seen.add(key)
        used += cost
        return True

    # First pass: guarantee evidence-role diversity by taking the strongest
    # candidate from each lane when the evidence budget permits.
    for lane in sorted(lanes, key=lambda value: -int(value.get("priority", 50))):
        candidates = lane_candidates.get(lane["name"], [])
        if candidates:
            try_add(candidates[0])

    # Second pass: fill remaining budget by global score, respecting each
    # lane's maximum. This allows two or three direct sample passages while
    # retaining accession/contradiction coverage for classification claims.
    selected_per_lane = Counter(item["lane"] for item in selected)
    all_candidates = [item for values in lane_candidates.values() for item in values]
    all_candidates.sort(
        key=lambda item: (
            -item["score"],
            -item["lane_priority"],
            str(item["source"]),
            item["page"] or 0,
        )
    )

    lane_limits = {
        lane["name"]: max(1, int(lane.get("max_items", 1)))
        for lane in lanes
    }

    for item in all_candidates:
        lane_name = item["lane"]
        if selected_per_lane[lane_name] >= lane_limits.get(lane_name, 1):
            continue
        if try_add(item):
            selected_per_lane[lane_name] += 1
        if len(selected) >= 6:
            break

    # If the claim-specific search found nothing, retain the old generic
    # fallback so the reviewer can still see some SCP context rather than
    # receiving an empty packet.
    if not selected:
        fallback_lane = evidence_lane(
            "generic_fallback",
            terms=list(GENERIC_SCP_TERMS),
            priority=10,
            max_items=2,
        )
        for item in _lane_candidates(
            source_index,
            fallback_lane,
            snippet_chars=snippet_chars,
        )[:2]:
            try_add(item)

    output = []
    for index, item in enumerate(selected, start=1):
        output.append(
            {
                "evidence_id": f"E{index:02d}",
                "lane": item["lane"],
                "high_risk": bool(item.get("high_risk")),
                "source": item["source"],
                "page": item["page"],
                "matched_term": item["matched_term"],
                "matched_terms": item.get("matched_terms", []),
                "text": item["text"],
            }
        )

    return output


def format_document(evidence: list[dict[str, Any]]) -> str:
    parts = []
    for item in evidence:
        source = item["source"]
        if item.get("page"):
            source += f" page {item['page']}"
        lane = text(item.get("lane")) or "general"
        risk = " | possible-measurement-contradiction" if item.get("high_risk") else ""
        parts.append(
            f"[{item['evidence_id']} | lane={lane}{risk} | {source}]\n"
            f"{item['text']}"
        )
    return "\n\n".join(parts)


def ollama_options(
    *,
    num_ctx: int,
    num_predict: int,
    cpu_threads: int | None,
) -> dict[str, Any]:
    options = {
        "temperature": 0,
        "seed": 42,
        "num_ctx": num_ctx,
        "num_predict": num_predict,
    }
    if cpu_threads is not None:
        options["num_thread"] = cpu_threads
    return options


def ollama_keep_alive_value(value: Any) -> Any:
    value = text(value)
    if value in {"0", "0s", "0m", "0h"}:
        return 0
    return value or "5m"


def _transient_ollama_status(status_code: int) -> bool:
    return status_code in {429, 500, 502, 503, 504}


def post_generate(
    *,
    url: str,
    model: str,
    prompt: str,
    timeout: int,
    options: dict[str, Any],
    schema: dict[str, Any] | None = None,
    keep_alive: Any = "5m",
    retries: int = 0,
    retry_backoff: float = 1.0,
) -> tuple[str, dict[str, Any]]:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": ollama_keep_alive_value(keep_alive),
        "options": options,
    }
    if schema is not None:
        payload["format"] = schema

    retries = max(0, int(retries))
    retry_backoff = max(0.0, float(retry_backoff))
    started_all = time.perf_counter()
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        try:
            response = requests.post(url, json=payload, timeout=timeout)

            if response.status_code >= 400:
                body = response.text[:800]
                if response.status_code == 404:
                    raise RuntimeError(
                        f"Ollama model {model!r} was not available. "
                        f"Try: ollama pull {model}\nServer response: {body}"
                    )
                if (
                    _transient_ollama_status(response.status_code)
                    and attempt < retries
                ):
                    if retry_backoff:
                        time.sleep(retry_backoff * (2 ** attempt))
                    continue
                if response.status_code in {502, 503, 504}:
                    raise OllamaUnavailableError(
                        f"Ollama returned HTTP {response.status_code} for model "
                        f"{model!r} after {attempt + 1} attempt(s): {body}"
                    )
                response.raise_for_status()

            data = response.json()
            raw = text(data.get("response"))
            wall = time.perf_counter() - started_all
            stats = {
                "wall_seconds": round(wall, 3),
                "request_attempts": attempt + 1,
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "output_tokens": data.get("eval_count", 0),
                "prompt_seconds": round(
                    data.get("prompt_eval_duration", 0) / 1e9,
                    3,
                ),
                "generation_seconds": round(
                    data.get("eval_duration", 0) / 1e9,
                    3,
                ),
            }
            return raw, stats

        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = exc
            if attempt < retries:
                if retry_backoff:
                    time.sleep(retry_backoff * (2 ** attempt))
                continue
            raise OllamaUnavailableError(
                f"Ollama became unreachable while running model {model!r} "
                f"after {attempt + 1} attempt(s): {type(exc).__name__}: {exc}"
            ) from exc

    raise OllamaUnavailableError(
        f"Ollama request for model {model!r} failed: {last_error}"
    )


def unload_ollama_model(
    *,
    url: str,
    model: str,
    timeout: int,
    retries: int,
    retry_backoff: float,
) -> None:
    # Official Ollama API semantics: empty prompt + keep_alive=0 unloads the
    # model. We intentionally do not send generation options/schema here.
    payload = {
        "model": model,
        "prompt": "",
        "stream": False,
        "keep_alive": 0,
    }
    retries = max(0, int(retries))
    retry_backoff = max(0.0, float(retry_backoff))

    for attempt in range(retries + 1):
        try:
            response = requests.post(url, json=payload, timeout=timeout)
            if response.status_code < 400:
                return
            if response.status_code == 404:
                # A missing/not-loaded model is already effectively unloaded.
                return
            if _transient_ollama_status(response.status_code) and attempt < retries:
                if retry_backoff:
                    time.sleep(retry_backoff * (2 ** attempt))
                continue
            if response.status_code in {502, 503, 504}:
                raise OllamaUnavailableError(
                    f"Ollama returned HTTP {response.status_code} while unloading "
                    f"model {model!r}."
                )
            response.raise_for_status()
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt < retries:
                if retry_backoff:
                    time.sleep(retry_backoff * (2 ** attempt))
                continue
            raise OllamaUnavailableError(
                f"Ollama became unreachable while unloading model {model!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc


def run_fact_checker(
    *,
    model: str,
    document: str,
    claim: dict[str, Any],
    args,
) -> tuple[str, str, dict[str, Any]]:
    extra = ""
    if claim.get("category") == "classification":
        extra = (
            "\nFor this classification claim, a title or generic label such as "
            "'single-cell proteomics' is not sufficient if methods evidence "
            "shows that many cells were pooled or combined into one MS sample. "
            "Explicit pooling evidence conflicts with a claim of proteomic "
            "measurement on individual biological cells."
        )
    elif claim.get("category") == "mapping":
        extra = (
            "\nThe evidence excerpts come from the same publication. The "
            "accession mapping may be supported by combining a repository/data "
            "availability excerpt with an individual-cell experiment excerpt."
        )

    prompt = (
        "Document:\n"
        f"{document}\n\n"
        "Claim:\n"
        f"{claim['claim_text']}\n\n"
        "Determine whether ALL factual information in the claim is supported "
        "by the document. Evidence may be distributed across the supplied "
        "excerpts, but do not use outside knowledge."
        f"{extra}\nRespond with only Yes or No."
    )

    raw, stats = post_generate(
        url=args.ollama_url,
        model=model,
        prompt=prompt,
        timeout=args.timeout,
        options=ollama_options(
            num_ctx=args.num_ctx,
            num_predict=12,
            cpu_threads=args.cpu_threads,
        ),
        keep_alive=args.fact_keep_alive,
        retries=args.ollama_retries,
        retry_backoff=args.ollama_retry_backoff,
    )

    match = re.search(r"\b(yes|no)\b", raw, re.I)
    if not match:
        raise RuntimeError(
            f"Fact model returned neither Yes nor No: {raw!r}"
        )

    return match.group(1).lower(), raw, stats


def normalize_structured_review(
    parsed: dict[str, Any],
    claim: dict[str, Any],
) -> dict[str, Any]:
    decision = text(parsed.get("decision")).lower()
    support = text(parsed.get("evidence_support")).lower()

    if decision not in {"pass", "warn", "fail"}:
        decision = "warn"
    if support not in {
        "direct",
        "indirect",
        "partial",
        "absent",
        "contradictory",
    }:
        support = "absent"

    evidence_ids = [
        text(value)
        for value in parsed.get("evidence_ids", []) or []
        if text(value)
    ]
    valid_evidence_ids = {
        item["evidence_id"]
        for item in claim.get("evidence", [])
    }
    evidence_ids = [
        value
        for value in evidence_ids
        if value in valid_evidence_ids
    ]

    try:
        confidence = float(parsed.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    proposed = parsed.get("proposed_value")
    proposed = text(proposed) if proposed is not None else ""

    normalized = {
        "decision": decision,
        "evidence_support": support,
        "reason": text(parsed.get("reason"))[:320],
        "proposed_value": proposed,
        "evidence_ids": evidence_ids,
        "confidence": round(confidence, 3),
    }

    # PASS should correspond to affirmative source support. If a structured
    # reviewer calls partial/absent/contradictory evidence a PASS, preserve the
    # uncertainty instead of allowing a malformed response to create consensus.
    if normalized["decision"] == "pass" and normalized["evidence_support"] not in {
        "direct",
        "indirect",
    }:
        normalized["decision"] = "warn"
        normalized["reason"] = (
            "Reviewer attempted PASS without direct/indirect support; "
            "downgraded deterministically to WARN. " + normalized["reason"]
        )[:320]

    # Proposed values are dangerous when evidence is merely partial. Retain a
    # correction only for a cited explicit contradiction.
    if not (
        normalized["evidence_support"] == "contradictory"
        and normalized["evidence_ids"]
        and normalized["decision"] in {"warn", "fail"}
    ):
        normalized["proposed_value"] = ""

    # FAIL remains deliberately difficult: explicit contradictory evidence and
    # at least one valid citation are mandatory.
    if normalized["decision"] == "fail":
        if (
            normalized["evidence_support"] != "contradictory"
            or not normalized["evidence_ids"]
        ):
            normalized["decision"] = "warn"
            normalized["reason"] = (
                "Reviewer attempted FAIL without a cited explicit contradiction; "
                "downgraded deterministically to WARN. "
                + normalized["reason"]
            )[:320]

    return normalized


def run_structured_reviewer(
    *,
    model: str,
    document: str,
    claim: dict[str, Any],
    reviewer_role: str,
    num_predict: int,
    args,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    prompt = (
        "You are one blind, independent scientific metadata reviewer in a "
        "small evidence-grounded jury. Use ONLY the supplied source evidence; "
        "do not use outside knowledge and do not infer what another reviewer "
        "might decide. PASS only when the claim is supported by the supplied "
        "evidence. WARN when evidence is partial, indirect, ambiguous, or "
        "missing. FAIL only when the supplied evidence explicitly contradicts "
        "the claim. A retrieval lane labelled HIGH-RISK is a cue to inspect "
        "carefully, not an automatic contradiction. Propose a replacement only "
        "when it is directly stated in cited evidence.\n\n"
        f"Reviewer role: {reviewer_role}\n"
        f"Accession: {claim['accession']}\n"
        f"Field: {claim['field']}\n"
        f"Curated value: {claim['value']}\n\n"
        f"Claim:\n{claim['claim_text']}\n\n"
        f"Source evidence:\n{document}\n\n"
        "Return only JSON matching the requested schema. Confidence is your "
        "confidence in your own evidence-grounded decision, from 0 to 1."
    )

    raw, stats = post_generate(
        url=args.ollama_url,
        model=model,
        prompt=prompt,
        timeout=args.timeout,
        options=ollama_options(
            num_ctx=args.num_ctx,
            num_predict=num_predict,
            cpu_threads=args.cpu_threads,
        ),
        schema=STRUCTURED_REVIEW_SCHEMA,
        keep_alive=(
            args.critic_keep_alive
            if reviewer_role == "A"
            else args.jury_keep_alive
        ),
        retries=args.ollama_retries,
        retry_backoff=args.ollama_retry_backoff,
    )

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Structured reviewer returned invalid JSON: {exc}; raw={raw!r}"
        ) from exc

    return normalize_structured_review(parsed, claim), raw, stats


def structured_vote(review: dict[str, Any] | None) -> str:
    if not review:
        return "unavailable"
    if (
        review.get("decision") == "fail"
        and review.get("evidence_support") == "contradictory"
        and review.get("evidence_ids")
    ):
        return "contradict"
    if (
        review.get("decision") == "pass"
        and review.get("evidence_support") in {"direct", "indirect"}
    ):
        return "support"
    return "uncertain"


def consensus_state(
    *,
    fact_answer: str,
    critic_review: dict[str, Any] | None,
    jury_review: dict[str, Any] | None,
    high_risk_evidence: bool,
    high_risk_evidence_ids: set[str] | None = None,
) -> dict[str, Any]:
    votes = {
        "fact": "support" if fact_answer == "yes" else "not_support",
        "critic": structured_vote(critic_review),
        "jury": structured_vote(jury_review),
    }

    support_count = sum(value == "support" for value in votes.values())
    contradiction_count = sum(value == "contradict" for value in votes.values())
    uncertain_count = sum(value in {"uncertain", "not_support"} for value in votes.values())

    # FAIL requires two independent structured reviewers to cite explicit
    # contradiction. MiniCheck No is non-entailment, not contradiction.
    if contradiction_count >= 2:
        decision = "fail"
        state = "two_structured_contradictions"
    else:
        if high_risk_evidence:
            # A deterministic pooling flag can only be cleared when BOTH
            # structured reviewers independently support the claim AND each
            # explicitly cites at least one high-risk passage. This prevents a
            # reviewer from clearing the flag by attending only to a positive
            # title/abstract while ignoring the pooling methods evidence.
            high_risk_evidence_ids = set(high_risk_evidence_ids or set())
            critic_checked_risk = bool(
                critic_review
                and high_risk_evidence_ids.intersection(
                    critic_review.get("evidence_ids", [])
                )
            )
            jury_checked_risk = bool(
                jury_review
                and high_risk_evidence_ids.intersection(
                    jury_review.get("evidence_ids", [])
                )
            )
            structured_support = (
                votes["critic"] == "support"
                and votes["jury"] == "support"
                and critic_checked_risk
                and jury_checked_risk
            )
            if structured_support and contradiction_count == 0:
                decision = "pass"
                state = "high_risk_cleared_by_two_citing_structured_reviewers"
            else:
                decision = "warn"
                state = "high_risk_unresolved"
        elif support_count >= 2 and contradiction_count == 0:
            decision = "pass"
            state = "two_of_three_support"
        else:
            decision = "warn"
            state = "unresolved"

    return {
        "decision": decision,
        "state": state,
        "votes": votes,
        "support_count": support_count,
        "contradiction_count": contradiction_count,
        "uncertain_count": uncertain_count,
    }


def warn_reason_for(
    *,
    claim: dict[str, Any],
    fact_answer: str,
    critic_review: dict[str, Any] | None,
    jury_review: dict[str, Any] | None,
    high_risk_evidence: bool,
    consensus: dict[str, Any],
) -> str:
    if consensus.get("decision") != "warn":
        return ""

    provenance = text(claim.get("provenance")).lower()
    if high_risk_evidence:
        return "positive_and_contradictory_evidence"
    if claim.get("field") == "organism" and "known_cell_line" in provenance:
        return "taxonomy_inferred_not_explicit"
    if claim.get("category") == "mapping":
        return "accession_mapping_uncertain"
    if consensus.get("contradiction_count", 0) > 0:
        return "positive_and_contradictory_evidence"

    supports = {
        text((critic_review or {}).get("evidence_support")),
        text((jury_review or {}).get("evidence_support")),
    }
    if "absent" in supports:
        return "source_not_explicit"
    if supports & {"partial", "indirect"}:
        return "evidence_partial"
    if fact_answer == "no":
        return "reviewer_disagreement"
    return "reviewer_disagreement"


def review_dimension_for(claim: dict[str, Any]) -> str:
    provenance = text(claim.get("provenance")).lower()
    if claim.get("field") == "organism" and "known_cell_line" in provenance:
        return "source_entailment+deterministic_curation"
    return "source_entailment"


def compact_review_summary(label: str, review: dict[str, Any] | None) -> str:
    if not review:
        return f"{label}: unavailable"
    ids = ",".join(review.get("evidence_ids", [])) or "none"
    return (
        f"{label}: decision={review.get('decision')}; "
        f"support={review.get('evidence_support')}; evidence_ids={ids}; "
        f"reason={review.get('reason', '')}"
    )


def run_deliberation_reviewer(
    *,
    model: str,
    document: str,
    claim: dict[str, Any],
    own_label: str,
    own_review: dict[str, Any],
    fact_answer: str,
    critic_review: dict[str, Any],
    jury_review: dict[str, Any],
    num_predict: int,
    args,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    # Model names are deliberately hidden. The purpose is reconsideration of
    # evidence, not social deference to a particular model family.
    summaries = (
        f"Binary reviewer: {'SUPPORT' if fact_answer == 'yes' else 'NOT SUPPORTED'}\n"
        f"Structured reviewer A: {compact_review_summary('A', critic_review)}\n"
        f"Structured reviewer B: {compact_review_summary('B', jury_review)}"
    )
    prompt = (
        "This is the ONE permitted reconsideration round for an evidence-grounded "
        "scientific metadata jury. The first-round reviewers disagreed or could "
        "not establish consensus. Re-read the source evidence yourself. Other "
        "judgments are shown anonymously only to identify the disputed points. "
        "Do NOT change your answer merely to agree; change only when the cited "
        "evidence warrants it. Use no outside knowledge. PASS requires support; "
        "WARN means partial/ambiguous/absent evidence; FAIL requires an explicit "
        "cited contradiction.\n\n"
        f"You are structured reviewer {own_label}.\n"
        f"Your first-round judgment: {compact_review_summary(own_label, own_review)}\n\n"
        f"Anonymous first-round jury summary:\n{summaries}\n\n"
        f"Claim:\n{claim['claim_text']}\n\n"
        f"Source evidence:\n{document}\n\n"
        "Return only JSON matching the requested schema."
    )

    raw, stats = post_generate(
        url=args.ollama_url,
        model=model,
        prompt=prompt,
        timeout=args.timeout,
        options=ollama_options(
            num_ctx=args.num_ctx,
            num_predict=num_predict,
            cpu_threads=args.cpu_threads,
        ),
        schema=STRUCTURED_REVIEW_SCHEMA,
        keep_alive=(
            args.critic_keep_alive
            if own_label == "A"
            else args.jury_keep_alive
        ),
        retries=args.ollama_retries,
        retry_backoff=args.ollama_retry_backoff,
    )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Deliberation reviewer returned invalid JSON: {exc}; raw={raw!r}"
        ) from exc
    return normalize_structured_review(parsed, claim), raw, stats


def reviewer_config(args) -> dict[str, Any]:
    return {
        "stage06_version": STAGE06_VERSION,
        "fact_model": args.fact_model,
        "critic_model": (
            None if model_is_disabled(args.critic_model)
            else args.critic_model
        ),
        "jury_model": (
            None if model_is_disabled(args.jury_model)
            else args.jury_model
        ),
        "num_ctx": args.num_ctx,
        "critic_num_predict": args.critic_num_predict,
        "jury_num_predict": args.jury_num_predict,
        "deliberation_rounds": args.deliberation_rounds,
        "deliberation_num_predict": args.deliberation_num_predict,
        "evidence_chars": args.evidence_chars,
        "snippet_chars": args.snippet_chars,
        "scope": args.scope,
        "dry_run": args.dry_run,
        "fact_keep_alive": args.fact_keep_alive,
        "critic_keep_alive": args.critic_keep_alive,
        "jury_keep_alive": args.jury_keep_alive,
        "keep_fact_loaded_during_adjudication": (
            args.keep_fact_loaded_during_adjudication
        ),
        "ollama_retries": args.ollama_retries,
        "ollama_retry_backoff": args.ollama_retry_backoff,
        "continue_after_ollama_unavailable": (
            args.continue_after_ollama_unavailable
        ),
        "jury_policy": (
            "Selective jury: MiniCheck Yes+no-risk=>PASS; Phi+Gemma only on "
            "MiniCheck No/high-risk; unresolved=>WARN; FAIL requires two "
            "structured explicit contradictions; deliberation off by default"
        ),
    }


def config_fingerprint(config: dict[str, Any]) -> str:
    return stable_hash(
        json.dumps(config, sort_keys=True),
        24,
    )


def status_path_for(
    output_dir: Path,
    accession: str,
    claim_id: str,
) -> Path:
    return output_dir / "claim_status" / accession / f"{claim_id}.json"


def review_claim(
    claim: dict[str, Any],
    *,
    output_dir: Path,
    config: dict[str, Any],
    args,
) -> dict[str, Any]:
    status_path = status_path_for(
        output_dir,
        claim["accession"],
        claim["claim_id"],
    )
    status_path.parent.mkdir(parents=True, exist_ok=True)

    if status_path.is_file() and not args.force:
        try:
            existing = json.loads(
                status_path.read_text(encoding="utf-8")
            )
        except Exception:
            existing = None

        if (
            existing
            and existing.get("config_fingerprint")
            == config_fingerprint(config)
        ):
            if args.retry_errors:
                if existing.get("run_status") != "error":
                    return existing
            else:
                return existing

    evidence = claim.get("evidence", [])
    document = format_document(evidence)
    high_risk_evidence = any(
        bool(item.get("high_risk"))
        for item in evidence
    )
    high_risk_evidence_ids = {
        item.get("evidence_id")
        for item in evidence
        if item.get("high_risk") and item.get("evidence_id")
    }

    result = {
        **{
            key: claim.get(key)
            for key in (
                "claim_id",
                "accession",
                "category",
                "record_type",
                "record_id",
                "field",
                "value",
                "claim_text",
                "provenance",
            )
        },
        "stage06_version": STAGE06_VERSION,
        "fact_model": args.fact_model,
        "critic_model": (
            "" if model_is_disabled(args.critic_model) else args.critic_model
        ),
        "jury_model": (
            "" if model_is_disabled(args.jury_model) else args.jury_model
        ),
        "config_fingerprint": config_fingerprint(config),
        "review_dimension": review_dimension_for(claim),
        "evidence_ids": "; ".join(
            item["evidence_id"]
            for item in evidence
        ),
        "high_risk_evidence": "yes" if high_risk_evidence else "no",
        "evidence_count": len(evidence),
        "evidence_document_chars": len(document),
        "run_status": "success",
        "fact_answer": "",
        "critic_decision": "",
        "critic_evidence_support": "",
        "critic_confidence": "",
        "jury_trigger": "",
        "jury_decision": "",
        "jury_evidence_support": "",
        "jury_confidence": "",
        "deliberation_performed": "no",
        "deliberation_rounds_used": 0,
        "critic_final_decision": "",
        "critic_final_evidence_support": "",
        "jury_final_decision": "",
        "jury_final_evidence_support": "",
        "consensus_state": "",
        "consensus_support_votes": 0,
        "consensus_contradiction_votes": 0,
        "consensus_uncertain_votes": 0,
        "warn_reason": "",
        "final_decision": "",
        "reason": "",
        "proposed_value": "",
        "cited_evidence_ids": "",
        "fact_wall_seconds": 0.0,
        "critic_wall_seconds": 0.0,
        "jury_wall_seconds": 0.0,
        "deliberation_wall_seconds": 0.0,
        "error_kind": "",
        "error": "",
    }

    if not document.strip():
        result.update(
            {
                "final_decision": "not_reviewed",
                "reason": "No source evidence could be retrieved for this atomic claim.",
            }
        )
        status_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return result

    if args.dry_run:
        result.update(
            {
                "run_status": "dry_run",
                "final_decision": "not_reviewed",
                "reason": "Dry run: LLM reviewers were not called.",
            }
        )
        status_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return result

    raw_dir = output_dir / "raw" / claim["accession"]
    raw_dir.mkdir(parents=True, exist_ok=True)

    critic_review: dict[str, Any] | None = None
    jury_review: dict[str, Any] | None = None
    fact_answer = ""

    try:
        # Round 1A: MiniCheck. It is binary entailment only; a No never counts
        # as source contradiction in the deterministic jury.
        fact_answer, fact_raw, fact_stats = run_fact_checker(
            model=args.fact_model,
            document=document,
            claim=claim,
            args=args,
        )
        result["fact_answer"] = fact_answer
        result["fact_wall_seconds"] = fact_stats["wall_seconds"]
        (raw_dir / f"{claim['claim_id']}.fact.txt").write_text(
            fact_raw,
            encoding="utf-8",
        )

        # v3.2 selective adjudication: preserve the empirically successful
        # v2.1 primary gate. A MiniCheck-supported claim with no deterministic
        # high-risk evidence PASSes immediately. Structured LLMs are reserved
        # for claims that would otherwise require manual review.
        if fact_answer == "yes" and not high_risk_evidence:
            result["critic_decision"] = "not_run_primary_pass"
            result["critic_evidence_support"] = "not_run"
            result["jury_trigger"] = "not_needed_primary_pass"
            current = {
                "decision": "pass",
                "state": "primary_fact_supported_no_risk",
                "votes": {
                    "fact": "support",
                    "critic": "unavailable",
                    "jury": "unavailable",
                },
                "support_count": 1,
                "contradiction_count": 0,
                "uncertain_count": 0,
            }
        else:
            # Phi and Gemma are blind, independent adjudicators. Both see only
            # the claim and the same retrieved evidence; neither sees MiniCheck
            # or the other structured reviewer's answer. To avoid the OOM seen
            # in the v3.1 full-core run, explicitly unload MiniCheck before
            # bringing either structured model into memory unless the user opts
            # out. Phi/Gemma default to keep_alive=0 and unload after each call.
            if not args.keep_fact_loaded_during_adjudication:
                unload_ollama_model(
                    url=args.ollama_url,
                    model=args.fact_model,
                    timeout=args.timeout,
                    retries=args.ollama_retries,
                    retry_backoff=args.ollama_retry_backoff,
                )

            if model_is_disabled(args.critic_model):
                result["critic_decision"] = "not_run_disabled"
                result["critic_evidence_support"] = "unavailable"
                result["jury_trigger"] = "adjudication_needed_but_critic_disabled"
                current = consensus_state(
                    fact_answer=fact_answer,
                    critic_review=None,
                    jury_review=None,
                    high_risk_evidence=high_risk_evidence,
                    high_risk_evidence_ids=high_risk_evidence_ids,
                )
            else:
                claim_for_review = dict(claim)
                claim_for_review["evidence"] = evidence
                critic_review, critic_raw, critic_stats = run_structured_reviewer(
                    model=args.critic_model,
                    document=document,
                    claim=claim_for_review,
                    reviewer_role="A",
                    num_predict=args.critic_num_predict,
                    args=args,
                )
                result["critic_decision"] = critic_review["decision"]
                result["critic_evidence_support"] = critic_review["evidence_support"]
                result["critic_confidence"] = critic_review["confidence"]
                result["critic_wall_seconds"] = critic_stats["wall_seconds"]
                (raw_dir / f"{claim['claim_id']}.critic.txt").write_text(
                    critic_raw,
                    encoding="utf-8",
                )

                if model_is_disabled(args.jury_model):
                    result["jury_trigger"] = "adjudication_needed_but_jury_disabled"
                    current = consensus_state(
                        fact_answer=fact_answer,
                        critic_review=critic_review,
                        jury_review=None,
                        high_risk_evidence=high_risk_evidence,
                        high_risk_evidence_ids=high_risk_evidence_ids,
                    )
                else:
                    result["jury_trigger"] = (
                        "high_risk_evidence"
                        if high_risk_evidence
                        else "fact_not_supported"
                    )
                    if args.critic_model != args.jury_model:
                        unload_ollama_model(
                            url=args.ollama_url,
                            model=args.critic_model,
                            timeout=args.timeout,
                            retries=args.ollama_retries,
                            retry_backoff=args.ollama_retry_backoff,
                        )
                    jury_review, jury_raw, jury_stats = run_structured_reviewer(
                        model=args.jury_model,
                        document=document,
                        claim=claim_for_review,
                        reviewer_role="B",
                        num_predict=args.jury_num_predict,
                        args=args,
                    )
                    result["jury_decision"] = jury_review["decision"]
                    result["jury_evidence_support"] = jury_review["evidence_support"]
                    result["jury_confidence"] = jury_review["confidence"]
                    result["jury_wall_seconds"] = jury_stats["wall_seconds"]
                    (raw_dir / f"{claim['claim_id']}.jury.txt").write_text(
                        jury_raw,
                        encoding="utf-8",
                    )

                    current = consensus_state(
                        fact_answer=fact_answer,
                        critic_review=critic_review,
                        jury_review=jury_review,
                        high_risk_evidence=high_risk_evidence,
                        high_risk_evidence_ids=high_risk_evidence_ids,
                    )

                    # Experimental only. The v3 pilot showed that routine
                    # reviewer-to-reviewer deliberation caused anchoring and
                    # expanded the WARN queue. It is therefore OFF by default.
                    if (
                        current["decision"] == "warn"
                        and args.deliberation_rounds > 0
                        and critic_review is not None
                        and jury_review is not None
                    ):
                        result["deliberation_performed"] = "yes"
                        result["deliberation_rounds_used"] = 1

                        critic_revised, critic_delib_raw, critic_delib_stats = run_deliberation_reviewer(
                            model=args.critic_model,
                            document=document,
                            claim=claim_for_review,
                            own_label="A",
                            own_review=critic_review,
                            fact_answer=fact_answer,
                            critic_review=critic_review,
                            jury_review=jury_review,
                            num_predict=args.deliberation_num_predict,
                            args=args,
                        )
                        jury_revised, jury_delib_raw, jury_delib_stats = run_deliberation_reviewer(
                            model=args.jury_model,
                            document=document,
                            claim=claim_for_review,
                            own_label="B",
                            own_review=jury_review,
                            fact_answer=fact_answer,
                            critic_review=critic_review,
                            jury_review=jury_review,
                            num_predict=args.deliberation_num_predict,
                            args=args,
                        )
                        result["deliberation_wall_seconds"] = round(
                            critic_delib_stats["wall_seconds"]
                            + jury_delib_stats["wall_seconds"],
                            3,
                        )
                        (raw_dir / f"{claim['claim_id']}.critic.deliberation.txt").write_text(
                            critic_delib_raw,
                            encoding="utf-8",
                        )
                        (raw_dir / f"{claim['claim_id']}.jury.deliberation.txt").write_text(
                            jury_delib_raw,
                            encoding="utf-8",
                        )
                        critic_review = critic_revised
                        jury_review = jury_revised
                        current = consensus_state(
                            fact_answer=fact_answer,
                            critic_review=critic_review,
                            jury_review=jury_review,
                            high_risk_evidence=high_risk_evidence,
                            high_risk_evidence_ids=high_risk_evidence_ids,
                        )

        # Preserve both first-round and final structured judgments in the CSV.
        result["critic_final_decision"] = (
            critic_review.get("decision", "") if critic_review else ""
        )
        result["critic_final_evidence_support"] = (
            critic_review.get("evidence_support", "") if critic_review else ""
        )
        result["jury_final_decision"] = (
            jury_review.get("decision", "") if jury_review else ""
        )
        result["jury_final_evidence_support"] = (
            jury_review.get("evidence_support", "") if jury_review else ""
        )
        result["consensus_state"] = current["state"]
        result["consensus_support_votes"] = current["support_count"]
        result["consensus_contradiction_votes"] = current["contradiction_count"]
        result["consensus_uncertain_votes"] = current["uncertain_count"]
        result["final_decision"] = current["decision"]

        warn_reason = warn_reason_for(
            claim=claim,
            fact_answer=fact_answer,
            critic_review=critic_review,
            jury_review=jury_review,
            high_risk_evidence=high_risk_evidence,
            consensus=current,
        )
        result["warn_reason"] = warn_reason

        if current["decision"] == "pass":
            if current.get("state") == "primary_fact_supported_no_risk":
                result["reason"] = (
                    "Grounded MiniCheck supported the claim and deterministic "
                    "retrieval found no high-risk contradiction; selective "
                    "structured adjudication was not required."
                )
            else:
                result["reason"] = (
                    "Selective evidence jury supported the disputed claim "
                    f"({current['support_count']} support vote(s), no explicit "
                    "two-reviewer contradiction)."
                )
        elif current["decision"] == "fail":
            result["reason"] = (
                "Two independent structured reviewers cited explicit source "
                "contradictions to the curated claim."
            )
        else:
            result["reason"] = (
                f"Jury remained unresolved ({warn_reason or 'reviewer_disagreement'}); "
                "manual review is required rather than forcing consensus."
            )

        # Expose a proposed correction only when the final structured reviewers
        # independently provide the SAME explicitly contradictory replacement.
        contradictory_reviews = [
            review
            for review in (critic_review, jury_review)
            if review
            and review.get("evidence_support") == "contradictory"
            and review.get("evidence_ids")
        ]
        proposals = {
            text(review.get("proposed_value"))
            for review in contradictory_reviews
            if text(review.get("proposed_value"))
        }
        if len(proposals) == 1 and len(contradictory_reviews) >= 2:
            result["proposed_value"] = next(iter(proposals))

        cited_ids = []
        for review in contradictory_reviews:
            for evidence_id in review.get("evidence_ids", []):
                if evidence_id not in cited_ids:
                    cited_ids.append(evidence_id)
        result["cited_evidence_ids"] = "; ".join(cited_ids)

    except OllamaUnavailableError as exc:
        result.update(
            {
                "run_status": "error",
                "final_decision": "not_reviewed",
                "error_kind": "ollama_unavailable",
                "error": f"{type(exc).__name__}: {exc}",
                "reason": (
                    "Ollama became unavailable after bounded retries; the run "
                    "should be resumed rather than treating this as a QC result."
                ),
            }
        )
    except Exception as exc:
        result.update(
            {
                "run_status": "error",
                "final_decision": "not_reviewed",
                "error_kind": "reviewer_error",
                "error": f"{type(exc).__name__}: {exc}",
                "reason": "Reviewer execution failed.",
            }
        )

    status_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return result


REVIEW_FIELDS = [
    "claim_id",
    "accession",
    "category",
    "record_type",
    "record_id",
    "field",
    "value",
    "claim_text",
    "provenance",
    "stage06_version",
    "review_dimension",
    "fact_model",
    "critic_model",
    "jury_model",
    "run_status",
    "fact_answer",
    "critic_decision",
    "critic_evidence_support",
    "critic_confidence",
    "jury_trigger",
    "jury_decision",
    "jury_evidence_support",
    "jury_confidence",
    "deliberation_performed",
    "deliberation_rounds_used",
    "critic_final_decision",
    "critic_final_evidence_support",
    "jury_final_decision",
    "jury_final_evidence_support",
    "consensus_state",
    "consensus_support_votes",
    "consensus_contradiction_votes",
    "consensus_uncertain_votes",
    "warn_reason",
    "final_decision",
    "reason",
    "proposed_value",
    "high_risk_evidence",
    "evidence_count",
    "evidence_document_chars",
    "evidence_ids",
    "cited_evidence_ids",
    "fact_wall_seconds",
    "critic_wall_seconds",
    "jury_wall_seconds",
    "deliberation_wall_seconds",
    "error_kind",
    "error",
]


DATASET_REVIEW_FIELDS = [
    "accession",
    "dataset_title",
    "publication_titles",
    "claims_reviewed",
    "pass_count",
    "warn_count",
    "fail_count",
    "not_reviewed_count",
    "error_count",
    "high_risk_count",
    "jury_invoked_count",
    "deliberation_count",
    "overall_qc_decision",
    "manual_review_required",
]


MANUAL_REVIEW_FIELDS = [
    "priority",
    *REVIEW_FIELDS,
]


def overall_decision(rows: list[dict[str, Any]]) -> str:
    decisions = {
        row.get("final_decision")
        for row in rows
    }
    if "fail" in decisions:
        return "fail"
    if "warn" in decisions:
        return "warn"
    if "pass" in decisions and decisions <= {"pass", "not_reviewed"}:
        return "pass"
    return "not_reviewed"


def aggregate_outputs(
    *,
    output_dir: Path,
    dataset_rows: list[dict[str, str]],
    results: list[dict[str, Any]],
    config: dict[str, Any],
    selected_accessions: list[str],
) -> None:
    results = sorted(
        results,
        key=lambda row: (
            row.get("accession", ""),
            row.get("category", ""),
            row.get("record_id", ""),
            row.get("field", ""),
        ),
    )

    write_csv(
        output_dir / "annotation_qc_review.csv",
        results,
        REVIEW_FIELDS,
    )

    by_accession = defaultdict(list)
    for row in results:
        by_accession[row.get("accession", "")].append(row)

    dataset_lookup = {
        row["accession"]: row
        for row in dataset_rows
    }

    dataset_summaries = []
    for accession in selected_accessions:
        rows = by_accession.get(accession, [])
        decisions = Counter(
            row.get("final_decision", "not_reviewed")
            for row in rows
        )
        errors = sum(
            1
            for row in rows
            if row.get("run_status") == "error"
        )
        overall = overall_decision(rows)
        drow = dataset_lookup.get(accession, {})
        dataset_summaries.append(
            {
                "accession": accession,
                "dataset_title": drow.get("dataset_title", ""),
                "publication_titles": drow.get("publication_titles", ""),
                "claims_reviewed": len(rows),
                "pass_count": decisions.get("pass", 0),
                "warn_count": decisions.get("warn", 0),
                "fail_count": decisions.get("fail", 0),
                "not_reviewed_count": decisions.get("not_reviewed", 0),
                "error_count": errors,
                "high_risk_count": sum(
                    1
                    for row in rows
                    if row.get("high_risk_evidence") == "yes"
                ),
                "jury_invoked_count": sum(
                    1
                    for row in rows
                    if row.get("jury_decision")
                ),
                "deliberation_count": sum(
                    1
                    for row in rows
                    if row.get("deliberation_performed") == "yes"
                ),
                "overall_qc_decision": overall,
                "manual_review_required": (
                    "yes" if overall in {"warn", "fail"} or errors else "no"
                ),
            }
        )

    write_csv(
        output_dir / "annotation_qc_dataset_summary.csv",
        dataset_summaries,
        DATASET_REVIEW_FIELDS,
    )

    manual = []
    for row in results:
        if row.get("final_decision") not in {"warn", "fail"}:
            continue
        manual.append(
            {
                "priority": (
                    "1-fail"
                    if row.get("final_decision") == "fail"
                    else (
                        "1-high-risk"
                        if row.get("high_risk_evidence") == "yes"
                        else "2-warn"
                    )
                ),
                **row,
            }
        )

    manual.sort(
        key=lambda row: (
            row["priority"],
            row.get("accession", ""),
            row.get("field", ""),
        )
    )
    write_csv(
        output_dir / "manual_review_queue.csv",
        manual,
        MANUAL_REVIEW_FIELDS,
    )

    failures = [
        row
        for row in results
        if row.get("run_status") == "error"
    ]
    write_csv(
        output_dir / "annotation_qc_failures.csv",
        failures,
        REVIEW_FIELDS,
    )

    decision_counts = Counter(
        row.get("final_decision", "not_reviewed")
        for row in results
    )
    category_counts = Counter(
        row.get("category", "")
        for row in results
    )
    warn_reason_counts = Counter(
        row.get("warn_reason", "")
        for row in results
        if row.get("final_decision") == "warn"
        and row.get("warn_reason")
    )
    jury_invocations = sum(1 for row in results if row.get("jury_decision"))
    deliberations = sum(
        1 for row in results if row.get("deliberation_performed") == "yes"
    )

    summary = {
        "stage06_version": STAGE06_VERSION,
        "reviewer_config": config,
        "selected_accessions": selected_accessions,
        "accessions_reviewed": len(selected_accessions),
        "atomic_claims": len(results),
        "decision_counts": dict(decision_counts),
        "category_counts": dict(category_counts),
        "warn_reason_counts": dict(warn_reason_counts),
        "jury_invocations": jury_invocations,
        "deliberations": deliberations,
        "manual_review_rows": len(manual),
        "reviewer_errors": len(failures),
        "read_only_note": (
            "Stage 06 is audit-only. No Stage-05 catalogue value or Stage-04 "
            "annotation JSON is modified."
        ),
    }

    (output_dir / "qc_run_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def main():
    args = parse_args()

    if args.cpu_threads is not None and args.cpu_threads < 1:
        raise ValueError("--cpu-threads must be >= 1")
    if args.evidence_chars < 1000:
        raise ValueError("--evidence-chars must be >= 1000")
    if args.snippet_chars < 300:
        raise ValueError("--snippet-chars must be >= 300")

    catalogue_dir = Path(args.catalogue_dir).resolve()
    base_dir = Path(args.base_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    datasets, grouped = load_catalogue(catalogue_dir)

    requested = {
        value.upper()
        for value in args.accession
        if value
    }

    if requested:
        datasets = [
            row
            for row in datasets
            if row.get("accession", "").upper() in requested
        ]

    datasets.sort(key=lambda row: row.get("accession", ""))

    if args.limit > 0:
        datasets = datasets[:args.limit]

    selected_accessions = [
        row["accession"]
        for row in datasets
    ]

    if not datasets:
        raise RuntimeError("No datasets selected for Stage-06 QC.")

    config = reviewer_config(args)
    config["config_fingerprint"] = config_fingerprint(config)

    config_path = output_dir / "run_config.json"
    if config_path.is_file() and not args.force:
        try:
            previous = json.loads(
                config_path.read_text(encoding="utf-8")
            )
        except Exception:
            previous = {}

        if (
            previous.get("config_fingerprint")
            and previous.get("config_fingerprint")
            != config["config_fingerprint"]
        ):
            raise RuntimeError(
                "The output directory contains reviews from a different "
                "Stage-06 configuration/model. Use a different --output-dir "
                "or --force."
            )

    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    cache_dir = output_dir / "pdf_text_cache"
    packet_dir = output_dir / "evidence_packets"
    packet_dir.mkdir(parents=True, exist_ok=True)

    all_results = []
    total_claims = 0
    prepared = []

    for index, dataset_row in enumerate(datasets, start=1):
        accession = dataset_row["accession"]
        publications = grouped["publications"].get(accession, [])

        source_index = build_source_index(
            dataset_row,
            publications,
            base_dir=base_dir,
            catalogue_dir=catalogue_dir,
            cache_dir=cache_dir,
            save_pdf_cache=args.save_pdf_cache,
        )

        claims = build_claims_for_dataset(
            dataset_row,
            grouped,
            args.scope,
        )

        for claim in claims:
            claim["evidence"] = retrieve_evidence(
                source_index,
                claim,
                max_chars=args.evidence_chars,
                snippet_chars=args.snippet_chars,
            )

        packet = {
            "stage06_version": STAGE06_VERSION,
            "accession": accession,
            "dataset_title": dataset_row.get("dataset_title", ""),
            "publication_titles": dataset_row.get("publication_titles", ""),
            "publication_dois": dataset_row.get("publication_dois", ""),
            "pdf_paths_found": source_index["pdf_paths"],
            "claims": claims,
        }

        (packet_dir / f"{accession}.json").write_text(
            json.dumps(packet, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        prepared.append((dataset_row, claims))
        total_claims += len(claims)

        print(
            f"[packet {index}/{len(datasets)}] {accession}: "
            f"{len(claims)} claims, "
            f"{len(source_index['pdf_paths'])} PDF(s)"
        )

    print(
        f"\nPrepared {total_claims} atomic claims across "
        f"{len(datasets)} accessions."
    )

    if args.dry_run:
        print("Dry run: no Ollama calls will be made.")

    completed = 0
    aborted_due_to_ollama = False
    abort_error = ""

    for dataset_row, claims in prepared:
        accession = dataset_row["accession"]

        for claim in claims:
            completed += 1
            print(
                f"[{completed}/{total_claims}] "
                f"{accession} {claim['field']} "
                f"({claim['claim_id']})"
            )

            result = review_claim(
                claim,
                output_dir=output_dir,
                config=config,
                args=args,
            )
            all_results.append(result)

            print(
                "  -> "
                f"{result.get('run_status')} / "
                f"{result.get('final_decision')}"
                + (
                    f" / fact={result.get('fact_answer')}"
                    if result.get("fact_answer")
                    else ""
                )
            )

            if (
                result.get("error_kind") == "ollama_unavailable"
                and not args.continue_after_ollama_unavailable
            ):
                aborted_due_to_ollama = True
                abort_error = text(result.get("error"))
                print(
                    "  !! Ollama remained unavailable after retries; stopping "
                    "early to avoid a cascade of identical reviewer errors."
                )
                break

        if aborted_due_to_ollama:
            break

    runtime_state = {
        "stage06_version": STAGE06_VERSION,
        "planned_claims": total_claims,
        "processed_claims": len(all_results),
        "aborted_due_to_ollama": aborted_due_to_ollama,
        "abort_error": abort_error,
    }
    (output_dir / "runtime_state.json").write_text(
        json.dumps(runtime_state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    aggregate_outputs(
        output_dir=output_dir,
        dataset_rows=datasets,
        results=all_results,
        config=config,
        selected_accessions=selected_accessions,
    )

    decisions = Counter(
        row.get("final_decision", "not_reviewed")
        for row in all_results
    )
    errors = sum(
        1
        for row in all_results
        if row.get("run_status") == "error"
    )

    print("\nStage-06 QC complete")
    print(f"Accessions: {len(datasets):,}")
    print(f"Atomic claims: {len(all_results):,}")
    print(
        "Decisions: "
        + ", ".join(
            f"{key}={decisions.get(key, 0)}"
            for key in ("pass", "warn", "fail", "not_reviewed")
        )
    )
    jury_invocations = sum(1 for row in all_results if row.get("jury_decision"))
    deliberations = sum(
        1 for row in all_results if row.get("deliberation_performed") == "yes"
    )
    print(f"Reviewer errors: {errors:,}")
    print(f"Third-reviewer invocations: {jury_invocations:,}")
    print(f"Deliberation rounds used: {deliberations:,}")
    print(
        "Manual review queue: "
        f"{sum(decisions.get(k, 0) for k in ('warn', 'fail')):,}"
    )
    print(f"Saved under: {output_dir}")
    if aborted_due_to_ollama:
        print(
            "Run stopped early because Ollama was unavailable after retries. "
            "Restart/verify Ollama and rerun the same command with "
            "--retry-errors to resume completed claim-level state."
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
