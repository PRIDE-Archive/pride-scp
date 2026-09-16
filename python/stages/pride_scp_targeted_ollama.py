#!/usr/bin/env python3

"""
Hybrid PRIDE single-cell proteomics publication annotator (v18).

Design:
  PDF -> deterministic parsing/retrieval
      -> deterministic terminology extraction
      -> five small Ollama semantic extraction calls
      -> validation/merge
      -> accession-centric JSON

The script is deliberately CPU-friendly:
  - qwen2.5:3b by default
  - small per-task evidence payloads
  - compact structured outputs
  - --cpu-threads to limit Ollama inference threads
  - adaptive retry when a structured task is truncated
"""

import argparse
import html
import json
import os
import re
import time
import unicodedata
from pathlib import Path

import pymupdf
import requests


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_PDF_PATH = (
    "/home/sing/Documents/datasets/PXD049412/paper/"
    "main_s41592-024-02559-1.pdf"
)
DEFAULT_OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "qwen2.5:3b"

DEFAULT_NUM_CTX = 4096

# Small task-specific generation ceilings. Truncated structured output is
# retried automatically; the samples task may escalate to 2,000 tokens.
DEFAULT_SAMPLES_NUM_PREDICT = 630
DEFAULT_PREPARATION_NUM_PREDICT = 180
DEFAULT_LC_NUM_PREDICT = 180
DEFAULT_SINGLE_CELL_PERFORMANCE_NUM_PREDICT = 160
DEFAULT_LOW_INPUT_PERFORMANCE_NUM_PREDICT = 160

DEFAULT_TASK_EVIDENCE_CHARS = 3_200


TARGET_SECTIONS = {
    "abstract",
    "results",
    "discussion",
    "conclusion",
    "methods",
    "data_availability",
}

HEADING_ALIASES = {
    "abstract": {
        "abstract",
        "summary",
    },
    "results": {
        "results",
    },
    "discussion": {
        "discussion",
        "results and discussion",
    },
    "conclusion": {
        "conclusion",
        "conclusions",
    },
    "methods": {
        "methods",
        "materials and methods",
        "materials & methods",
        "experimental procedures",
        "experimental section",
        "online methods",
    },
    "data_availability": {
        "data availability",
        "data and code availability",
        "data availability statement",
        "availability of data and materials",
        "data availability and accession codes",
    },
}

STOP_HEADINGS = {
    "references",
    "acknowledgements",
    "acknowledgments",
    "author contributions",
    "competing interests",
    "conflict of interest",
    "rights and permissions",
    "extended data",
    "supplementary information",
}


PXD_RE = re.compile(r"\bPXD\d{6,}\b", re.I)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.I)
REPOSITORY_ACCESSION_RE = re.compile(
    r"^(?:PXD|RPXD)\d{4,}$|^MSV\d{6,}$|^MTBLS\d+$|"
    r"^JPST\d+$|^PASS\d+$",
    re.I,
)
CITATION_YEAR_RE = re.compile(r"\(\s*(?:19|20)\d{2}[a-z]?\s*\)", re.I)

EVIDENCE_ID_RE = re.compile(
    r"^(?:E|B)\d{1,5}$",
    re.I,
)

GENERIC_SAMPLE_NAME_RE = re.compile(
    r"^(?:"
    r"(?:human|mouse|murine|rat|yeast)?\s*"
    r"(?:single\s+)?(?:individual\s+)?cells?"
    r"|"
    r"(?:human|mouse|murine|rat|yeast)?\s*cell\s+lines?"
    r"|"
    r"(?:single[- ]cell|single[- ]cells|individual\s+cells)"
    r"|"
    r"(?:samples?|sample\s+types?)"
    r")$",
    re.I,
)


NON_CELL_SAMPLE_NAME_RE = re.compile(
    r"^(?:"
    r"roots?|shoots?|"
    r"(?:bulk|whole)\s+(?:tissue|lysate|digest|proteome)|"
    r"tissues?|organs?|"
    r"plasma|serum|blood|"
    r"carrier(?:\s+proteome)?|"
    r"pooled(?:\s+sample)?|"
    r"(?:protein|peptide|proteome)\s+digests?|"
    r"patients?|subjects?|donors?|"
    r"(?:healthy\s+)?controls?|"
    r"non[- ]?[A-Za-z0-9/+-]+\s+controls?"
    r")$",
    re.I,
)

BIOLOGICAL_CELL_UNIT_RE = re.compile(
    r"\b(?:"
    r"cells?|oocytes?|neurons?|blastomeres?|"
    r"bacterium|bacteria|"
    r"sperm(?:\s+cells?)?|egg\s+cells?|"
    r"hepatocytes?|lymphocytes?|monocytes?|macrophages?|"
    r"fibroblasts?|keratinocytes?|myocytes?|myofibres?|"
    r"progenitors?|blasts?"
    r")\b",
    re.I,
)


YEAR_ONLY_RE = re.compile(r"^(?:19|20)\d{2}[a-z]?$", re.I)
AUTHOR_INITIALS_RE = re.compile(
    r"^(?:[A-Z]\.?\s*){1,3}[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’-]{2,}$"
    r"|^[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’-]{2,}(?:\s+[A-Z]{1,4}\.?)$"
)
ABSTRACT_PREFIX_RE = re.compile(r"^\s*ABSTRACT\s*:\s*", re.I)
REFERENCEISH_SAMPLE_RE = re.compile(
    r"\b(?:doi|journal|vol\.?|volume|issue|pages?|et\s+al\.|"
    r"proteomics sample preparation at high sensitivity|"
    r"published|publisher)\b",
    re.I,
)


FINAL_METADATA_REFERENCE_RE = re.compile(
    r"^(?:[A-Z]\.\s+|[A-Z][a-z]+,\s+[A-Z]\.)"
    r"|\b(?:an\s+automated|workflow\s+for|method\s+for|"
    r"proteomics\s+sample\s+preparation|mass\s+spectrometry\s+workflow|"
    r"quantitative\s+multiplexed)\b",
    re.I,
)

FINAL_METADATA_PROCEDURAL_SAMPLE_RE = re.compile(
    r"^(?:"
    r"(?:FACS|flow[- ]cytometry)[- ]sorted\s+cell\s+populations?|"
    r"sorted\s+cell\s+populations?|"
    r"cell\s+populations?|"
    r"(?:stage[- ]?\d+\s+)?embryos?"
    r")$",
    re.I,
)

FINAL_METADATA_CONDITION_RE = re.compile(
    r"^(?:WT|wild[- ]type|mutant|control)\b"
    r"|\bp[A-Z0-9_.-]+:[A-Z0-9_.-]+\b"
    r"|\b(?:GFP|mCherry|RFP|YFP)\b",
    re.I,
)

NON_ORGANISM_VALUE_RE = re.compile(
    r"\b(?:cells?|cell\s+line|tissue|tumou?r|cancer|prostate|"
    r"plasma|serum|blood|control|sample|digest|proteome|patient|"
    r"oocyte|neuron|embryo|organoid)\b",
    re.I,
)

CELL_ISOLATION_ACTION_RE = re.compile(
    r"\b(?:FACS|fluorescence[- ]activated\s+cell\s+sorting|"
    r"flow\s+cytometry|CellenONE|cell\s+sorting|sorted|"
    r"single[- ]cell\s+isolation|cell\s+isolation|"
    r"manual\s+isolation|manually\s+isolated|"
    r"micromanipulation|micropipett(?:e|ing)|"
    r"laser\s+capture|microdissection|"
    r"picked|captured|deposited|dispensed|"
    r"one\s+cell\s+per\s+well|nanowell)\b",
    re.I,
)

NON_CELL_ISOLATION_METHOD_RE = re.compile(
    r"\b(?:RNA|DNA|protein)\s+(?:isolation|extraction)"
    r"|\b(?:RNA|DNA)\s+isolation\s+kit\b"
    r"|\b(?:RNA|DNA)\s+extraction\s+kit\b",
    re.I,
)



# Known manuscript/source-text aliases. Keep this deliberately tiny and
# explicit rather than attempting unsafe fuzzy correction of arbitrary sample
# names. The Nature Methods Astral manuscript contains the heading typo A459
# while the surrounding prose consistently identifies the cell line as A549.
KNOWN_SAMPLE_ALIASES = {
    "a459": "A549 cells",
}

ORGANISM_CANONICAL = {
    "human": "Homo sapiens",
    "homo sapiens": "Homo sapiens",
    "mouse": "Mus musculus",
    "murine": "Mus musculus",
    "mus musculus": "Mus musculus",
    "yeast": "Saccharomyces cerevisiae",
    "saccharomyces cerevisiae": "Saccharomyces cerevisiae",
    "c. elegans": "Caenorhabditis elegans",
    "caenorhabditis elegans": "Caenorhabditis elegans",
    "rice": "Oryza sativa",
    "oryza sativa": "Oryza sativa",
    "xenopus laevis": "Xenopus laevis",
    "arabidopsis thaliana": "Arabidopsis thaliana",
    "e. coli": "Escherichia coli",
    "escherichia coli": "Escherichia coli",
}


SINGLE_CELL_LABEL_RE = re.compile(
    r"\bsingle[- ]cell\b|\bsingle[- ]cell data\b",
    re.I,
)

LOW_INPUT_LABEL_RE = re.compile(
    r"\b(?:dilution|low[- ]input|low amount|low-amount|benchmark)\b",
    re.I,
)

# These are accession-scope hints, not biological assertions. They are only
# activated when the authors explicitly map multiple PXD accessions to named
# experiment groups in the Data Availability statement.
DATASET_SCOPE_TERM_MAP = {
    "single_cell": (
        "single-cell",
        "single cell",
        "individual single",
        "individual cells",
        "single-cell data",
        "single-cell proteomics",
        "single-cell proteome",
    ),
    "low_input": (
        "dilution",
        "low-amount",
        "low amount",
        "low-input",
        "low input",
        "picogram",
        " pg ",
    ),
    "fractionation": (
        "fractionation",
        "hph",
        "high-ph",
        "high ph",
    ),
    "clinical": (
        "clinical",
        "msa",
        "brain",
        "biops",
    ),
    "yeast": (
        "yeast",
        "knockout",
        "ko collection",
    ),
    "species_mix": (
        "three-species",
        "three species",
        "mixed species",
        "species mix",
        "lfq",
    ),
    "dda_dia": (
        "dda versus dia",
        "dda vs dia",
        "comparison",
    ),
    "window_optimization": (
        "window optimization",
        "window size",
        "isolation window",
    ),
}


# ---------------------------------------------------------------------------
# Retrieval vocabularies
# ---------------------------------------------------------------------------

EVIDENCE_GROUPS = {
    "single_cell_samples": {
        "preferred_sections": {
            "abstract": 8,
            "results": 6,
            "methods": 3,
            "discussion": 1,
        },
        "terms": {
            "single-cell": 12,
            "single cell": 12,
            "single cells": 12,
            "single-cell proteom": 16,
            "single cell proteom": 16,
            "individual cell": 12,
            "individual cells": 12,
            "single-cell samples": 16,
            "single cell samples": 16,
            "deposited into individual wells": 18,
            "individual wells": 10,
            "one cell per well": 18,
            "single cells were sorted": 18,
            "single cells were isolated": 16,
            "single cells were analyzed": 18,
            "proteins per single cell": 18,
            "protein groups per single cell": 18,
            "from a single cell": 16,
            "dataset contains": 4,
        },
        "negative_terms": {
            "single cell line": 20,
            "single-cell line": 20,
            "single cell type": 20,
            "single-cell type": 20,
            "single cell culture": 20,
            "single-cell clone": 20,
        },
    },

    "low_input_benchmarks": {
        "preferred_sections": {
            "abstract": 5,
            "results": 8,
            "methods": 4,
        },
        "terms": {
            " pg ": 12,
            "picogram": 12,
            "low-input": 11,
            "low input": 11,
            "low-amount": 10,
            "low amount": 10,
            "diluted": 10,
            "dilution": 9,
            "bulk digest": 11,
            "benchmark": 8,
            "benchmarking": 8,
            "mixture": 4,
            "diluted bulk digests": 14,
            "cell peptides": 5,
        },
    },

    "isolation_and_preparation": {
        "preferred_sections": {
            "methods": 10,
            "results": 2,
            "abstract": 1,
        },
        "terms": {
            "cellenone": 14,
            "facs": 14,
            "fluorescence-activated cell sorting": 14,
            "cell sorting": 11,
            "sorted": 5,
            "sorting": 5,
            "384-well": 9,
            "96-well": 7,
            "nanopots": 10,
            "nanop3": 10,
            "proteochip": 10,
            "one-pot": 8,
            "lysis": 7,
            "digestion": 7,
            "trypsin": 6,
            "tryptic": 5,
            "reduction": 4,
            "alkylation": 4,
            "sample preparation": 8,
            "cell isolation": 9,
            "accutase": 8,
        },
    },

    "lc": {
        "preferred_sections": {
            "methods": 11,
        },
        "terms": {
            "liquid chromatography": 10,
            "lc–ms analysis": 12,
            "lc-ms analysis": 12,
            "uhplc system": 14,
            "vanquish neo": 20,
            "nanolc": 10,
            "ultimate 3000": 9,
            "vanquish": 10,
            "aurora": 9,
            "column": 6,
            "emitter": 6,
            "gradient": 9,
            "gradient lengths": 12,
            "flow rate": 7,
            "nl min": 7,
            "nl/min": 7,
            "mobile phase": 5,
        },
    },

    "throughput_and_depth": {
        "preferred_sections": {
            "abstract": 7,
            "results": 10,
            "discussion": 2,
        },
        "terms": {
            "throughput": 12,
            "samples per day": 14,
            "cells per day": 14,
            "spd": 9,
            "proteins per cell": 14,
            "protein groups per cell": 14,
            "proteome coverage": 10,
            "proteome depth": 12,
            "protein groups": 6,
            "proteins": 2,
            "quantified": 3,
        },
    },

    "single_cell_performance_specific": {
        "preferred_sections": {
            "abstract": 10,
            "results": 12,
            "discussion": 2,
        },
        "terms": {
            "from a single cell": 24,
            "per single cell": 24,
            "single-cell dataset": 18,
            "single-cell measurements": 18,
            "individual cell": 16,
            "individual cells": 16,
            "proteins per cell": 18,
            "protein groups per cell": 18,
            "maximum coverage": 14,
            "proteome coverage": 12,
            "proteome depth": 14,
            "protein groups": 7,
            "samples per day": 14,
            "cells per day": 14,
            "spd": 8,
        },
        "negative_terms": {
            "diluted bulk": 24,
            "cell peptides": 10,
            "low-input": 12,
            "low input": 12,
        },
    },

    "low_input_performance_specific": {
        "preferred_sections": {
            "abstract": 10,
            "results": 12,
            "methods": 2,
        },
        "terms": {
            " pg ": 18,
            "picogram": 18,
            "low-input": 16,
            "low input": 16,
            "ultra-low input": 16,
            "low-amount": 14,
            "diluted bulk": 22,
            "cell peptides": 12,
            "protein groups": 10,
            "proteins": 6,
            "50 samples per day": 8,
            "50 spd": 8,
        },
        "negative_terms": {
            "from a single cell": 18,
            "individual cells": 12,
        },
    },
}


# ---------------------------------------------------------------------------
# Deterministic terminology patterns
# ---------------------------------------------------------------------------

# More specific patterns should precede broader patterns where relevant.

INSTRUMENT_PATTERNS = [
    ("Orbitrap Astral", re.compile(r"\bOrbitrap\s+Astral\b", re.I)),
    (
        "Orbitrap Exploris 480",
        re.compile(r"\bOrbitrap\s+Exploris\s+480\b", re.I),
    ),
    (
        "Orbitrap Exploris 240",
        re.compile(r"\bOrbitrap\s+Exploris\s+240\b", re.I),
    ),
    (
        "Orbitrap Eclipse",
        re.compile(r"\bOrbitrap\s+Eclipse\b", re.I),
    ),
    (
        "Orbitrap Fusion Lumos",
        re.compile(r"\b(?:Orbitrap\s+)?Fusion\s+Lumos\b", re.I),
    ),
    (
        "Q Exactive HF-X",
        re.compile(r"\bQ\s*Exactive\s+HF-?X\b", re.I),
    ),
    (
        "Q Exactive HF",
        re.compile(r"\bQ\s*Exactive\s+HF\b", re.I),
    ),
    (
        "timsTOF SCP",
        re.compile(r"\btimsTOF\s+SCP\b", re.I),
    ),
    (
        "timsTOF Ultra",
        re.compile(r"\btimsTOF\s+Ultra\b", re.I),
    ),
    (
        "timsTOF Pro 2",
        re.compile(r"\btimsTOF\s+Pro\s*2\b", re.I),
    ),
    (
        "timsTOF Pro",
        re.compile(r"\btimsTOF\s+Pro\b", re.I),
    ),
]

ACQUISITION_PATTERNS = [
    (
        "DIA",
        re.compile(
            r"\b(?:DIA|DIA-MS|data[- ]independent acquisition|"
            r"data[- ]independent acquisition mode)\b",
            re.I,
        ),
    ),
    (
        "DDA",
        re.compile(
            r"\b(?:DDA|DDA-MS|data[- ]dependent acquisition|"
            r"data[- ]dependent acquisition mode)\b",
            re.I,
        ),
    ),
    (
        "diaPASEF",
        re.compile(r"\bdiaPASEF\b", re.I),
    ),
    (
        "PASEF",
        re.compile(r"\bPASEF\b", re.I),
    ),
]

ION_MOBILITY_PATTERNS = [
    (
        "FAIMS Pro Duo",
        re.compile(r"\bFAIMS\s+Pro\s+Duo\b", re.I),
    ),
    (
        "FAIMS Pro",
        re.compile(r"\bFAIMS\s+Pro\b", re.I),
    ),
    (
        "FAIMS",
        re.compile(
            r"\bfield asymmetric waveform ion mobility spectrometry\b",
            re.I,
        ),
    ),
    (
        "TIMS",
        re.compile(
            r"\b(?:trapped ion mobility spectrometry|TIMS)\b",
            re.I,
        ),
    ),
]

SOFTWARE_PATTERNS = [
    (
        "Spectronaut",
        re.compile(
            r"\bSpectronaut(?:\s+(?:version\s*)?\d+(?:\.\d+)*)?\b",
            re.I,
        ),
    ),
    (
        "DIA-NN",
        re.compile(r"\bDIA[- ]NN\b", re.I),
    ),
    (
        "MaxQuant",
        re.compile(r"\bMaxQuant\b", re.I),
    ),
    (
        "FragPipe",
        re.compile(r"\bFragPipe\b", re.I),
    ),
    (
        "Proteome Discoverer",
        re.compile(r"\bProteome\s+Discoverer\b", re.I),
    ),
    (
        "Skyline",
        re.compile(r"\bSkyline\b", re.I),
    ),
]

LABELING_PATTERNS = [
    ("TMTpro", re.compile(r"\bTMTpro\b", re.I)),
    ("TMT", re.compile(r"\bTMT(?:\d+plex)?\b", re.I)),
    ("plexDIA", re.compile(r"\bplexDIA\b", re.I)),
    ("SILAC", re.compile(r"\bSILAC\b", re.I)),
    (
        "label-free",
        re.compile(r"\blabel[- ]free\b", re.I),
    ),
]

LIBRARY_STRATEGY_PATTERNS = [
    # Support publisher line wrapping such as "direct-\nDIA+".
    (
        "DirectDIA+",
        re.compile(
            r"(?<!\w)Direct(?:-\s*|\s*)DIA\+(?!\w)",
            re.I,
        ),
    ),
    (
        "DirectDIA",
        re.compile(
            r"(?<!\w)Direct(?:-\s*|\s*)DIA(?!\+)",
            re.I,
        ),
    ),
    ("library-free", re.compile(r"\blibrary[- ]free\b", re.I)),
    (
        "spectral library",
        re.compile(r"\bspectral librar(?:y|ies)\b", re.I),
    ),
    (
        "library-based",
        re.compile(r"\blibrary[- ]based\b", re.I),
    ),
]


# ---------------------------------------------------------------------------
# Structured schemas for the three semantic calls
# ---------------------------------------------------------------------------

SAMPLES_SCHEMA = {
    "type": "object",
    "properties": {
        "is_single_cell_proteomics": {
            "type": "string",
            "enum": ["yes", "no", "uncertain"],
        },
        "true_single_cell_samples": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "properties": {
                    "sample_type": {
                        "type": "string",
                        "maxLength": 80,
                    },
                    "organism": {
                        "type": ["string", "null"],
                        "maxLength": 60,
                    },
                    "cell_counts_reported": {
                        "type": "array",
                        "maxItems": 5,
                        "items": {
                            "type": "string",
                            "maxLength": 90,
                        },
                    },
                },
                "required": [
                    "sample_type",
                    "organism",
                    "cell_counts_reported",
                ],
            },
        },
        "low_input_benchmarks": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "sample": {
                        "type": "string",
                        "maxLength": 100,
                    },
                    "organism": {
                        "type": ["string", "null"],
                        "maxLength": 80,
                    },
                    "input_amount": {
                        "type": ["string", "null"],
                        "maxLength": 80,
                    },
                },
                "required": [
                    "sample",
                    "organism",
                    "input_amount",
                ],
            },
        },
    },
    "required": [
        "is_single_cell_proteomics",
        "true_single_cell_samples",
        "low_input_benchmarks",
    ],
}


PREPARATION_SCHEMA = {
    "type": "object",
    "properties": {
        "sample_preparation": {
            "type": ["string", "null"],
            "maxLength": 280,
        },
        "single_cell_isolation": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "samples": {
                        "type": "array",
                        "maxItems": 6,
                        "items": {
                            "type": "string",
                            "maxLength": 70,
                        },
                    },
                    "method": {
                        "type": "string",
                        "maxLength": 110,
                    },
                },
                "required": [
                    "samples",
                    "method",
                ],
            },
        },
    },
    "required": [
        "sample_preparation",
        "single_cell_isolation",
    ],
}


LC_SCHEMA = {
    "type": "object",
    "properties": {
        "lc_configuration": {
            "type": ["string", "null"],
            "maxLength": 220,
        },
        "lc_gradient": {
            "type": ["string", "null"],
            "maxLength": 220,
        },
    },
    "required": [
        "lc_configuration",
        "lc_gradient",
    ],
}


SINGLE_CELL_PERFORMANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "single_cell_throughput": {
            "type": ["string", "null"],
            "maxLength": 100,
        },
        "single_cell_proteome_depth": {
            "type": ["string", "null"],
            "maxLength": 160,
        },
    },
    "required": [
        "single_cell_throughput",
        "single_cell_proteome_depth",
    ],
}


LOW_INPUT_PERFORMANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "low_input_proteome_depth": {
            "type": ["string", "null"],
            "maxLength": 160,
        },
    },
    "required": [
        "low_input_proteome_depth",
    ],
}

NULL_LIKE_STRINGS = {
    "null",
    "none",
    "unknown",
    "not known",
    "not provided",
    "not reported",
    "not specified",
    "not stated",
    "not available",
    "n/a",
    "na",
    "useless_inference",
}


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------

def normalize_heading(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip().lower())
    text = re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", text)
    return text.rstrip(":.")


def classify_heading(line: str):
    heading = normalize_heading(line)

    if len(heading) > 80 or len(heading.split()) > 9:
        return None

    if heading in STOP_HEADINGS:
        return "__stop__"

    for canonical, aliases in HEADING_ALIASES.items():
        if heading in aliases:
            return canonical

    return None


def clean_block_text(text: str) -> str:
    lines = []

    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()

        if line:
            lines.append(line)

    return "\n".join(lines).strip()


def extract_blocks(pdf_path: str):
    doc = pymupdf.open(pdf_path)
    blocks = []

    for page_num, page in enumerate(doc, start=1):
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)

        for block_num, block in enumerate(
            page.get_text("blocks", sort=True),
            start=1,
        ):
            x0, y0, x1, y1 = map(float, block[:4])
            text = clean_block_text(block[4])

            if len(text) < 20:
                continue

            if "javascript" in text.lower():
                continue

            blocks.append(
                {
                    "block_id": f"B{len(blocks) + 1:03d}",
                    "page": page_num,
                    "page_block": block_num,
                    "text": text,
                    "bbox": (x0, y0, x1, y1),
                    "page_width": page_width,
                    "page_height": page_height,
                    "section": "other",
                }
            )

    doc.close()
    return blocks


def extract_text_blocks(text_path: str):
    """Create PDF-like evidence blocks from normalized publication text.

    v0.1.7 uses this for Europe-PMC/JATS full text when a direct PDF is not
    available.  Paragraph boundaries are preserved so the existing section
    classifier and evidence retrievers operate without a separate LLM path.
    """
    raw = Path(text_path).read_text(encoding="utf-8")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = re.split(r"\n\s*\n+", raw)
    blocks = []
    for paragraph in paragraphs:
        text = clean_block_text(paragraph)
        if len(text) < 20:
            continue
        blocks.append(
            {
                "block_id": f"B{len(blocks) + 1:03d}",
                "page": 1,
                "page_block": len(blocks) + 1,
                "text": text,
                "bbox": (0.0, float(len(blocks)), 1.0, float(len(blocks) + 1)),
                "page_width": 1.0,
                "page_height": max(1.0, float(len(paragraphs))),
                "section": "other",
            }
        )
    return blocks


def infer_text_publication_title(blocks):
    for item in blocks[:12]:
        text = normalize_publication_text(item.get("text", "")) or ""
        if is_plausible_publication_title(text):
            return text, "source_text_first_block"
    return None, None



def normalize_publication_text(text):
    """Normalize publisher/PDF title text without changing scientific meaning."""
    if not isinstance(text, str):
        return None

    value = text.strip()
    if not value:
        return None

    # Repair missing '&' before numeric entities produced by some PDF exports.
    value = re.sub(r"(?<!&)#x([0-9A-Fa-f]{2,8});", r"&#x\1;", value)
    value = re.sub(r"(?<!&)#(\d{2,7});", r"&#\1;", value)

    # Decode repeatedly because publisher metadata sometimes nests entities.
    for _ in range(3):
        decoded = html.unescape(value)
        if decoded == value:
            break
        value = decoded

    # Remove simple markup left in PRIDE/Crossref titles.
    value = re.sub(r"</?[^>]+>", " ", value)
    value = unicodedata.normalize("NFKC", value)

    # Normalize common dash variants so SCP regexes are stable.
    value = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2212]", "-", value)
    value = value.replace("⠒", "'")
    value = re.sub(r"\s+", " ", value).strip()

    return value or None


def is_plausible_publication_title(title):
    value = normalize_publication_text(title)
    if not value:
        return False

    lower = value.lower()
    if not (20 <= len(value) <= 500):
        return False

    if lower in {"untitled", "unknown", "microsoft word", "document"}:
        return False

    boilerplate = (
        "published as part of the",
        "virtual special issue",
        "focus: asilomar conference",
        "supplementary information",
        "table of contents",
    )
    if any(term in lower for term in boilerplate):
        return False

    if DOI_RE.fullmatch(value):
        return False

    return True


def infer_publication_title(pdf_path, blocks):
    """
    Return a conservative publication-title candidate.

    PDF metadata is preferred. If it is absent/useless, use a short page-1
    block near the top of the page while rejecting obvious author,
    affiliation, DOI, journal-header and correspondence blocks.
    """
    try:
        doc = pymupdf.open(pdf_path)
        metadata = doc.metadata or {}
        metadata_title = normalize_publication_text(
            str(metadata.get("title") or "")
        ) or ""
        doc.close()
    except Exception:
        metadata_title = ""

    if (
        is_plausible_publication_title(metadata_title)
        and not DOI_RE.search(metadata_title)
        and "http://" not in metadata_title.lower()
        and "https://" not in metadata_title.lower()
    ):
        return metadata_title, "pdf_metadata"

    candidates = []

    for item in blocks:
        if item.get("page") != 1:
            continue

        text = normalize_publication_text(
            item.get("text", "")
        ) or ""
        lower = text.lower()
        y0 = float(item.get("bbox", (0, 0, 0, 0))[1])
        page_height = float(item.get("page_height") or 1.0)

        if not (20 <= len(text) <= 420):
            continue
        if len(text.split()) < 4 or len(text.split()) > 55:
            continue
        if y0 > page_height * 0.38:
            continue
        if DOI_RE.search(text) or "doi.org" in lower or "http" in lower:
            continue
        if "@" in text:
            continue
        if not is_plausible_publication_title(text):
            continue
        if any(
            term in lower
            for term in (
                "correspondence",
                "department of",
                "university of",
                "received:",
                "accepted:",
                "published online",
                "author contributions",
            )
        ):
            continue

        score = 0.0
        score += max(0.0, 8.0 - (y0 / page_height) * 20.0)
        if 45 <= len(text) <= 260:
            score += 4.0
        if text.count(",") <= 3:
            score += 2.0
        if re.search(r"\b(?:proteom|mass\s+spectrom|single[- ]cell|single\s+cell)\b", lower):
            score += 3.0

        candidates.append((score, y0, text))

    if not candidates:
        return None, None

    candidates.sort(key=lambda row: (-row[0], row[1]))
    return candidates[0][2], "page1_heuristic"


def extract_sections(blocks):
    sections = {name: [] for name in TARGET_SECTIONS}
    current = None

    for block in blocks:
        lines = [
            line.strip()
            for line in block["text"].splitlines()
            if line.strip()
        ]

        if not lines:
            continue

        heading = classify_heading(lines[0])

        if heading == "__stop__":
            current = None
            continue

        if heading in TARGET_SECTIONS:
            current = heading

            remainder = " ".join(lines[1:]).strip()

            if remainder:
                item = dict(block)
                item["text"] = remainder
                item["section"] = current
                sections[current].append(item)

            continue

        if current:
            item = dict(block)
            item["section"] = current
            sections[current].append(item)

    return sections


# ---------------------------------------------------------------------------
# Abstract / lead fallback
# ---------------------------------------------------------------------------

LEAD_EXCLUDE_TERMS = {
    "received:",
    "accepted:",
    "published online",
    "author contributions",
    "competing interests",
    "correspondence",
    "rights and permissions",
    "references",
    "https://doi.org",
    "nature methods |",
}


def generic_relevance_score(text: str) -> int:
    lower = text.lower()
    score = 0

    if "single-cell" in lower or "single cell" in lower:
        score += 8

    if "proteom" in lower:
        score += 4

    if "mass spectrom" in lower:
        score += 3

    if "protein" in lower:
        score += 2

    return score


def infer_lead_abstract(blocks):
    candidates = []

    for block in blocks:
        if block["page"] != 1:
            continue

        text = block["text"]
        lower = text.lower()
        x0, y0, _, _ = block["bbox"]
        width = block["page_width"]
        height = block["page_height"]

        if len(text) < 500 or len(text) > 4_500:
            continue

        if y0 < height * 0.22 or y0 > height * 0.80:
            continue

        if any(term in lower for term in LEAD_EXCLUDE_TERMS):
            continue

        sentence_marks = text.count(".") + text.count(";")

        if sentence_marks < 3:
            continue

        score = 0.0

        if x0 >= width * 0.32:
            score += 5.0

        if height * 0.30 <= y0 <= height * 0.62:
            score += 4.0

        if 800 <= len(text) <= 2_800:
            score += 3.0

        score += min(generic_relevance_score(text), 12) * 0.3

        candidates.append((score, y0, x0, block))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))

    best = dict(candidates[0][3])
    best["section"] = "abstract"
    best["inferred_abstract"] = True

    return best


# ---------------------------------------------------------------------------
# Deterministic identifiers
# ---------------------------------------------------------------------------

def find_identifiers(blocks):
    full_text = "\n\n".join(item["text"] for item in blocks)

    pxds = sorted({
        accession.upper()
        for accession in PXD_RE.findall(full_text)
    })

    dois = sorted({
        doi.rstrip(".,;)")
        for doi in DOI_RE.findall(full_text)
    })

    primary_doi = None

    page1 = [
        item
        for item in blocks
        if item["page"] == 1
    ]

    for item in page1:
        if "doi.org" not in item["text"].lower():
            continue

        matches = DOI_RE.findall(item["text"])

        if matches:
            primary_doi = matches[0].rstrip(".,;)")
            break

    if primary_doi is None:
        for item in page1:
            matches = DOI_RE.findall(item["text"])

            if matches:
                primary_doi = matches[0].rstrip(".,;)")
                break

    return pxds, dois, primary_doi



def parse_data_availability_accession_labels(sections):
    """
    Parse author-provided experiment labels for PXD accessions.

    Example:
      "(6) single-cell data: PXD046357; (7) single-shot dilution series:
       PXD046283"

    Returns:
      {
        "PXD046357": "single-cell data",
        "PXD046283": "single-shot dilution series",
        ...
      }

    If an accession is mentioned without a useful local label, it is omitted
    from the map rather than assigned an invented description.
    """
    text = " ".join(
        item["text"]
        for item in sections.get("data_availability", [])
    )

    if not text:
        return {}

    # Normalize line wrapping while retaining punctuation boundaries.
    text = re.sub(r"\s+", " ", text)

    mapping = {}

    # Data Availability statements commonly separate experiment/accession
    # pairs with semicolons. Process each segment independently.
    for segment in re.split(r";", text):
        matches = list(PXD_RE.finditer(segment))

        if not matches:
            continue

        for match in matches:
            accession = match.group(0).upper()
            prefix = segment[:match.start()].strip()

            # If the segment contains a generic introduction followed by a
            # numbered item, retain only the numbered item's text.
            numbered = list(
                re.finditer(
                    r"(?:\(|\[)?(?:\d+|[ivx]+)(?:\)|\])[\s.)-]*",
                    prefix,
                    re.I,
                )
            )
            if numbered:
                prefix = prefix[numbered[-1].end():].strip()

            # Remove boilerplate before "identifiers:" when present.
            prefix = re.sub(
                r"^.*?\bidenti[- ]*fiers?\s*:\s*",
                "",
                prefix,
                flags=re.I,
            )

            # The useful experiment label itself is the text BEFORE the
            # final colon immediately preceding the accession.
            prefix = prefix.rstrip(" :")

            prefix = re.sub(
                r"^(?:and\s+)?(?:the\s+)?",
                "",
                prefix,
                flags=re.I,
            )
            prefix = prefix.strip(" ,:.-")

            # Reject generic repository boilerplate as a "label".
            boilerplate = (
                "proteomexchange",
                "pride",
                "dataset identifier",
                "dataset identifiers",
                "data are available",
                "data have been deposited",
            )

            if (
                prefix
                and len(prefix) <= 120
                and not any(term in prefix.lower() for term in boilerplate)
            ):
                mapping[accession] = prefix

    return mapping


def classify_explicit_dataset_label(label):
    """
    Return a strong accession-level SCP hint only when the authors themselves
    used an explicit experiment label in a multi-accession Data Availability
    statement.
    """
    if not label:
        return None

    if SINGLE_CELL_LABEL_RE.search(label):
        return "yes"

    return None


def has_separate_low_input_accession(accession_labels, target_accession):
    for accession, label in accession_labels.items():
        if accession == target_accession:
            continue

        if LOW_INPUT_LABEL_RE.search(label or ""):
            return True

    return False


def infer_scope_kind(label):
    if not label:
        return None

    lower = label.lower()

    if SINGLE_CELL_LABEL_RE.search(label):
        return "single_cell"

    if LOW_INPUT_LABEL_RE.search(label):
        return "low_input"

    if "fraction" in lower:
        return "fractionation"

    if "clinical" in lower or "msa" in lower:
        return "clinical"

    if "yeast" in lower or "knockout" in lower or " ko " in f" {lower} ":
        return "yeast"

    if "species" in lower or "mix" in lower:
        return "species_mix"

    if "dda" in lower and "dia" in lower:
        return "dda_dia"

    if "window" in lower:
        return "window_optimization"

    return None



def looks_like_reference_block(text):
    """
    Exclude bibliography blocks that were accidentally retained inside a
    preceding section because a publisher merged the 'References' heading
    into a larger PDF text block.
    """
    if not isinstance(text, str):
        return False

    stripped = text.strip()

    if re.match(r"^\d+\.\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’-]+", stripped):
        return True

    # Several numbered references in one block are also a strong signal.
    numbered_refs = re.findall(
        r"(?:^|\n)\s*\d+\.\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’-]+",
        stripped,
    )

    return len(numbered_refs) >= 2


def make_scope_snippet(item, terms, max_chars=1_250):
    """
    Clip large publisher PDF blocks around an accession-scope term.

    This matters for Nature PDFs where a single extracted block may contain a
    single-cell paragraph followed by an unrelated instrument-comparison
    paragraph. Deterministic metadata must not inherit terms from the unrelated
    tail of that block.
    """
    text = item["text"]

    if len(text) <= max_chars:
        return dict(item)

    lower = text.lower()
    hits = []

    for term in terms:
        start = lower.find(term.lower())
        if start >= 0:
            # Prefer highly specific single-cell terms over generic FAIMS/DIA.
            specificity = len(term)
            if "single" in term.lower():
                specificity += 30
            hits.append((specificity, start, len(term)))

    if not hits:
        clipped = dict(item)
        clipped["text"] = text[:max_chars].strip()
        clipped["scope_snippet"] = True
        return clipped

    hits.sort(key=lambda row: (-row[0], row[1]))
    _, hit_start, hit_len = hits[0]

    center = hit_start + hit_len // 2
    half = max_chars // 2
    start = max(0, center - half)
    end = min(len(text), start + max_chars)

    if end - start < max_chars:
        start = max(0, end - max_chars)

    snippet = text[start:end].strip()

    clipped = dict(item)
    clipped["text"] = snippet
    clipped["scope_snippet"] = True

    return clipped


def build_accession_scope_sections(sections, label):
    """
    Build a conservative section subset for a target experiment in a paper
    that maps multiple PXD accessions to different experiment groups.

    We include blocks that contain scope terms. For single-cell targets, we
    also include explicit low-amount/FAIMS blocks because many SCP papers
    describe the single-cell acquisition together with sensitivity/dilution
    optimization. We do NOT include generic bulk sample-preparation blocks
    solely because they mention the same cell line.
    """
    scope_kind = infer_scope_kind(label)

    if scope_kind is None:
        return sections, None, set()

    terms = set(DATASET_SCOPE_TERM_MAP.get(scope_kind, ()))

    if scope_kind == "single_cell":
        terms.update(
            {
                "single-cell proteomics",
                "single cell proteomics",
                "single-cell samples",
                "single cell samples",
                "individual cells",
                "individual single",
                "faims",
                "low-amount dilution",
                "low amount dilution",
            }
        )

    scoped = {name: [] for name in TARGET_SECTIONS}
    block_ids = set()

    for section_name in TARGET_SECTIONS:
        for item in sections.get(section_name, []):
            if looks_like_reference_block(item["text"]):
                continue

            lower = item["text"].lower()

            if any(term in lower for term in terms):
                # Preserve the full source block for semantic retrieval.
                # Task-specific retrieval will later extract a compact snippet
                # around its strongest term. Clipping here caused double
                # clipping and could remove the key performance sentence.
                scoped[section_name].append(dict(item))
                block_ids.add(item["block_id"])

    # Always preserve Data Availability for the explicit accession mapping.
    for item in sections.get("data_availability", []):
        if item["block_id"] not in block_ids:
            scoped["data_availability"].append(item)
            block_ids.add(item["block_id"])

    return scoped, scope_kind, block_ids


def build_metadata_scope_blocks(
    blocks,
    scoped_sections,
    scope_kind,
):
    """
    Deterministic terminology should describe the target accession rather than
    every experiment in a multi-accession publication.

    Use explicitly scoped Results/Methods evidence plus a few universal
    manuscript descriptors when they are safe:
      - title/lead blocks mentioning label-free
      - LC-MS/MS method blocks for single-cell targets when they also contain
        low-amount/FAIMS context
    """
    if scope_kind is None:
        return blocks

    scoped_items = [
        item
        for section_items in scoped_sections.values()
        for item in section_items
    ]

    scoped_ids = {
        item["block_id"]
        for item in scoped_items
    }

    scope_terms = set(
        DATASET_SCOPE_TERM_MAP.get(scope_kind, ())
    )

    if scope_kind == "single_cell":
        scope_terms.update(
            {
                "single-cell proteomics",
                "single cell proteomics",
                "single-cell samples",
                "single cell samples",
                "individual cells",
                "individual single",
                "single hela",
                "faims",
                "low-amount dilution",
                "low amount dilution",
            }
        )

    # Deterministic metadata gets compact target-context snippets. Semantic
    # tasks retain the full blocks above.
    selected = [
        make_scope_snippet(
            item,
            scope_terms,
            max_chars=650,
        )
        for item in scoped_items
        if item.get("section") != "data_availability"
    ]

    if scope_kind == "single_cell":
        for item in blocks:
            lower = item["text"].lower()

            if looks_like_reference_block(item["text"]):
                continue

            if (
                len(PXD_RE.findall(item["text"])) >= 2
                or "dataset identifiers" in lower
                or "dataset identifiers:" in lower
            ):
                continue

            extra_relevant = (
                (
                    ("single-cell" in lower or "single cell" in lower)
                    and (
                        "spectronaut" in lower
                        or "orbitrap astral" in lower
                        or "faims" in lower
                        or "dia" in lower
                    )
                )
                or (
                    "low-amount dilution series" in lower
                    and (
                        "faims" in lower
                        or "directdia" in lower
                        or "spectronaut" in lower
                    )
                )
                or (
                    item["page"] == 1
                    and "label-free" in lower
                )
            )

            if extra_relevant and item["block_id"] not in scoped_ids:
                selected.append(
                    make_scope_snippet(
                        item,
                        DATASET_SCOPE_TERM_MAP["single_cell"]
                        + (
                            "faims",
                            "low-amount dilution",
                            "directdia",
                            "spectronaut",
                            "label-free",
                        ),
                        max_chars=650,
                    )
                )
                scoped_ids.add(item["block_id"])

    return selected


def make_empty_inference_stats(task_name):
    return {
        "wall_seconds": 0.0,
        "prompt_tokens": 0,
        "output_tokens": 0,
        "prompt_seconds": 0.0,
        "generation_seconds": 0.0,
        "load_seconds": 0.0,
        "num_predict": 0,
        "attempts": 0,
        "skipped": True,
        "reason": task_name,
    }


def infer_target_accession(pdf_path: str):
    matches = PXD_RE.findall(str(pdf_path))

    if not matches:
        return None

    return matches[-1].upper()


# ---------------------------------------------------------------------------
# Section-aware block collection
# ---------------------------------------------------------------------------

def all_section_blocks(sections):
    items = []

    for section_name in (
        "abstract",
        "methods",
        "results",
        "discussion",
        "conclusion",
        "data_availability",
    ):
        items.extend(sections[section_name])

    return items


def score_block_for_group(item, group_name):
    spec = EVIDENCE_GROUPS[group_name]

    score = spec["preferred_sections"].get(
        item.get("section", "other"),
        0,
    )

    lower = item["text"].lower()

    content_score = 0

    for term, weight in spec["terms"].items():
        if term.lower() in lower:
            content_score += weight

    negative_score = 0

    for term, weight in spec.get("negative_terms", {}).items():
        if term.lower() in lower:
            negative_score += weight

    return (
        score + content_score - negative_score,
        content_score,
    )


def make_relevance_snippet(item, group_name, max_chars):
    """
    Keep a compact context window around the strongest task-specific match.

    Whole PDF blocks can be >1,500 characters. Sending them whole caused one
    high-scoring block to consume most of a task's evidence budget and hid
    other experiments (for example A549/H460 behind the TE/hPS block).
    """
    if len(item["text"]) <= max_chars:
        return dict(item)

    spec = EVIDENCE_GROUPS[group_name]
    lower = item["text"].lower()

    matches = []

    for term, weight in spec["terms"].items():
        start = lower.find(term.lower())

        if start >= 0:
            matches.append((weight, start, len(term)))

    if not matches:
        clipped = dict(item)
        clipped["text"] = item["text"][:max_chars].rstrip()
        clipped["snippet"] = True
        return clipped

    matches.sort(key=lambda row: (-row[0], row[1]))
    _, center_start, term_len = matches[0]

    half = max_chars // 2
    center = center_start + term_len // 2

    start = max(0, center - half)
    end = min(len(item["text"]), start + max_chars)

    if end - start < max_chars:
        start = max(0, end - max_chars)

    snippet = item["text"][start:end]

    # Move to nearby whitespace so we do not start/end mid-word.
    if start > 0:
        first_space = snippet.find(" ")
        if 0 <= first_space < 80:
            snippet = snippet[first_space + 1:]

    if end < len(item["text"]):
        last_space = snippet.rfind(" ")
        if last_space > len(snippet) - 80:
            snippet = snippet[:last_space]

    clipped = dict(item)
    clipped["text"] = snippet.strip()
    clipped["snippet"] = True

    return clipped


def select_group_blocks(
    sections,
    group_name,
    max_chars,
    *,
    include_abstract=False,
    max_block_chars=1_000,
):
    candidates = []

    for item in all_section_blocks(sections):
        text = item.get("text", "")

        # Reference-like blocks and accession inventories are metadata, not
        # biological sample evidence. In particular, long Data Availability
        # accession lists caused small models to emit PXD IDs as sample names.
        if looks_like_reference_block(text):
            continue

        if group_name in {
            "single_cell_samples",
            "low_input_benchmarks",
            "single_cell_performance_specific",
            "low_input_performance_specific",
        }:
            pxd_count = len(PXD_RE.findall(text))
            lower_text = text.lower()

            if pxd_count >= 2:
                continue

            if (
                item.get("section") == "data_availability"
                and (
                    pxd_count >= 1
                    or "proteomexchange" in lower_text
                    or "dataset identifier" in lower_text
                )
            ):
                continue

        score, content_score = score_block_for_group(
            item,
            group_name,
        )

        if content_score <= 0:
            continue

        candidates.append(
            (
                score,
                item["page"],
                item["bbox"][1],
                item["bbox"][0],
                item,
            )
        )

    candidates.sort(
        key=lambda row: (
            -row[0],
            row[1],
            row[2],
            row[3],
        )
    )

    selected = []
    seen = set()
    used = 0

    if include_abstract:
        for item in sections["abstract"]:
            abstract_item = make_relevance_snippet(
                item,
                group_name,
                max_block_chars,
            )

            key = (
                abstract_item["block_id"],
                abstract_item["text"],
            )

            if key in seen:
                continue

            entry_cost = len(abstract_item["text"]) + 80

            if used + entry_cost <= max_chars:
                selected.append(abstract_item)
                seen.add(key)
                used += entry_cost

    for _, _, _, _, item in candidates:
        snippet = make_relevance_snippet(
            item,
            group_name,
            max_block_chars,
        )

        key = (
            snippet["block_id"],
            snippet["text"],
        )

        if key in seen:
            continue

        entry_cost = len(snippet["text"]) + 80

        if used + entry_cost > max_chars:
            continue

        selected.append(snippet)
        seen.add(key)
        used += entry_cost

    return selected


def combine_task_blocks(*groups, max_chars):
    """
    Merge already-selected group lists into one task payload while preserving
    order and enforcing a final size limit.
    """
    selected = []
    seen = set()
    used = 0

    for group in groups:
        for item in group:
            key = re.sub(
                r"\s+",
                " ",
                item["text"],
            ).strip().lower()

            if key in seen:
                continue

            entry_cost = len(item["text"]) + 100

            if used + entry_cost > max_chars:
                continue

            selected.append(item)
            seen.add(key)
            used += entry_cost

    return selected



def combine_task_blocks_balanced(*groups, max_chars):
    """
    Round-robin merger so each evidence category contributes before one
    category consumes the entire task budget.
    """
    selected = []
    seen = set()
    used = 0

    positions = [0 for _ in groups]

    while True:
        added_this_round = False

        for group_index, group in enumerate(groups):
            while positions[group_index] < len(group):
                item = group[positions[group_index]]
                positions[group_index] += 1

                key = (
                    item["block_id"],
                    item["text"],
                )

                if key in seen:
                    continue

                entry_cost = len(item["text"]) + 100

                if used + entry_cost > max_chars:
                    continue

                selected.append(item)
                seen.add(key)
                used += entry_cost
                added_this_round = True
                break

        if not added_this_round:
            break

    return selected



def combine_task_blocks_quota(
    primary_group,
    secondary_group,
    *,
    max_chars,
    primary_count=3,
    secondary_count=1,
):
    """
    Build a compact task packet with an explicit evidence quota.

    For sample classification, genuine single-cell evidence is the primary
    objective. Previously, round-robin balancing allowed several benchmark
    blocks to displace the A549/H460 single-cell block. Keep a small amount
    of benchmark evidence for contrast, but reserve most of the budget for
    genuine single-cell experiments.
    """
    selected = []
    seen = set()
    used = 0

    def try_add(item):
        nonlocal used

        key = (
            item["block_id"],
            item["text"],
        )

        if key in seen:
            return False

        entry_cost = len(item["text"]) + 100

        if used + entry_cost > max_chars:
            return False

        selected.append(item)
        seen.add(key)
        used += entry_cost

        return True

    primary_added = 0

    for item in primary_group:
        if primary_added >= primary_count:
            break

        if try_add(item):
            primary_added += 1

    secondary_added = 0

    for item in secondary_group:
        if secondary_added >= secondary_count:
            break

        if try_add(item):
            secondary_added += 1

    # Fill any remaining room with additional primary evidence first.
    for item in primary_group:
        try_add(item)

    # Only then use more secondary evidence.
    for item in secondary_group:
        try_add(item)

    return selected


def format_task_evidence(items):
    parts = []
    records = []

    for idx, item in enumerate(items, start=1):
        evidence_id = f"E{idx:02d}"

        parts.append(
            f"\n[{evidence_id} | {item['section'].upper()} | "
            f"page {item['page']}]\n"
            f"{item['text']}\n"
        )

        records.append(
            {
                "evidence_id": evidence_id,
                "block_id": item["block_id"],
                "page": item["page"],
                "section": item["section"],
                "text": item["text"],
            }
        )

    return "".join(parts), records


# ---------------------------------------------------------------------------
# Deterministic terminology extraction
# ---------------------------------------------------------------------------

def extract_pattern_values(blocks, patterns):
    """
    Return canonical values plus deterministic provenance.

    A value is emitted once, with all pages/block IDs on which its pattern
    matched. This prevents semantic category errors such as DirectDIA+ being
    reported as an MS acquisition mode.
    """
    values = []
    provenance = {}

    for canonical, pattern in patterns:
        matches = []

        for item in blocks:
            if pattern.search(item["text"]):
                matches.append(
                    {
                        "block_id": item["block_id"],
                        "page": item["page"],
                    }
                )

        if matches:
            values.append(canonical)
            provenance[canonical] = matches

    return values, provenance


def collapse_subsumed_values(values, category=None):
    """
    Remove generic/duplicate terminology while preserving meaningful
    manuscript-level distinctions.
    """
    values = list(dict.fromkeys(values))

    if category == "labeling":
        if "TMTpro" in values and "TMT" in values:
            values.remove("TMT")

    if category == "library":
        if "DirectDIA+" in values and "DirectDIA" in values:
            values.remove("DirectDIA")

        # Normalize overlapping prose variants to useful catalogue concepts.
        normalized = []
        for value in values:
            mapped = {
                "library-based": "spectral-library-based",
                "spectral library": "spectral-library-based",
            }.get(value, value)

            if mapped not in normalized:
                normalized.append(mapped)

        values = normalized

    if category == "acquisition":
        if "diaPASEF" in values and "PASEF" in values:
            values.remove("PASEF")

    if category == "ion_mobility":
        # Generic FAIMS is redundant if a specific FAIMS device is present.
        if (
            ("FAIMS Pro" in values or "FAIMS Pro Duo" in values)
            and "FAIMS" in values
        ):
            values.remove("FAIMS")

    return values


def build_ion_mobility_role_records(
    technologies,
    provenance,
    blocks,
):
    """
    Convert flat FAIMS/TIMS terminology into compact role-aware records.

    A technology is marked as a reproducibility check only when every matched
    context is explicitly secondary/reproducibility-oriented. Otherwise it is
    treated as part of the primary workflow.
    """
    block_text = {
        item["block_id"]: item["text"].lower()
        for item in blocks
    }

    secondary_markers = (
        "reproducibility",
        "second orbitrap",
        "second astral",
        "second instrument",
        "reproducibility checks",
    )

    records = []

    for technology in technologies:
        matches = provenance.get(technology, [])

        contexts = [
            block_text.get(match["block_id"], "")
            for match in matches
        ]

        nonempty = [context for context in contexts if context]

        if (
            nonempty
            and all(
                any(marker in context for marker in secondary_markers)
                for context in nonempty
            )
        ):
            role = "reproducibility check"
        else:
            role = "primary"

        records.append(
            {
                "technology": technology,
                "role": role,
            }
        )

    return records



def filter_accession_scoped_deterministic(
    deterministic,
    metadata_blocks,
    scope_kind,
):
    """
    Remove comparison/reference metadata from accession-scoped records.

    Multi-experiment instrument papers often mention comparison platforms in
    the same paragraph as the target experiment. A platform should not become
    target-accession metadata solely because it appears as a reference
    comparison or a separately described experiment.
    """
    if scope_kind is None:
        return deterministic

    block_text = {
        item["block_id"]: item["text"].lower()
        for item in metadata_blocks
    }

    comparison_markers = (
        "reference dia datasets",
        "reference dataset",
        "compared with",
        "compared against",
        "benchmark against",
        "for the lfq samples",
        "lfq samples acquired",
        "state-of-the-art",
    )

    prov = deterministic.get(
        "deterministic_provenance",
        {},
    )

    # Filter instruments whose every scoped occurrence is comparison-only.
    kept_instruments = []

    for instrument in deterministic.get("mass_spectrometers", []):
        matches = prov.get(
            "mass_spectrometers",
            {},
        ).get(instrument, [])

        contexts = [
            block_text.get(match["block_id"], "")
            for match in matches
        ]

        # Evaluate a small window around the instrument name where possible.
        relevant_contexts = []

        for context in contexts:
            idx = context.find(instrument.lower())

            if idx >= 0:
                start = max(0, idx - 180)
                end = min(len(context), idx + len(instrument) + 180)
                relevant_contexts.append(context[start:end])
            else:
                relevant_contexts.append(context)

        comparison_only = (
            relevant_contexts
            and all(
                any(marker in context for marker in comparison_markers)
                for context in relevant_contexts
            )
        )

        if not comparison_only:
            kept_instruments.append(instrument)

    deterministic["mass_spectrometers"] = kept_instruments

    return deterministic


def deterministic_metadata(blocks):
    instruments, instrument_provenance = extract_pattern_values(
        blocks,
        INSTRUMENT_PATTERNS,
    )

    acquisition, acquisition_provenance = extract_pattern_values(
        blocks,
        ACQUISITION_PATTERNS,
    )

    ion_mobility, ion_mobility_provenance = extract_pattern_values(
        blocks,
        ION_MOBILITY_PATTERNS,
    )

    software, software_provenance = extract_pattern_values(
        blocks,
        SOFTWARE_PATTERNS,
    )

    labeling, labeling_provenance = extract_pattern_values(
        blocks,
        LABELING_PATTERNS,
    )

    library_strategy, library_provenance = extract_pattern_values(
        blocks,
        LIBRARY_STRATEGY_PATTERNS,
    )

    acquisition = collapse_subsumed_values(
        acquisition,
        category="acquisition",
    )
    ion_mobility = collapse_subsumed_values(
        ion_mobility,
        category="ion_mobility",
    )
    labeling = collapse_subsumed_values(
        labeling,
        category="labeling",
    )
    library_strategy = collapse_subsumed_values(
        library_strategy,
        category="library",
    )

    # For catalogue purposes there is usually one overall labeling strategy.
    # If multiple explicit strategies are present, preserve them all rather
    # than guessing which applies to a particular subset.
    if not labeling:
        labeling_value = None
    elif len(labeling) == 1:
        labeling_value = labeling[0]
    else:
        labeling_value = labeling

    ion_mobility_records = build_ion_mobility_role_records(
        ion_mobility,
        ion_mobility_provenance,
        blocks,
    )

    return {
        "mass_spectrometers": instruments,
        "acquisition_modes": acquisition,
        "ion_mobility_or_faims": ion_mobility_records,
        "analysis_software": software,
        "labeling_strategy": labeling_value,
        "analysis_strategies": library_strategy,
        "deterministic_provenance": {
            "mass_spectrometers": instrument_provenance,
            "acquisition_modes": acquisition_provenance,
            "ion_mobility_or_faims": ion_mobility_provenance,
            "analysis_software": software_provenance,
            "labeling_strategy": labeling_provenance,
            "analysis_strategies": library_provenance,
        },
    }



# ---------------------------------------------------------------------------
# Catalogue sample normalization
# ---------------------------------------------------------------------------


NON_BIOLOGICAL_SAMPLE_RE = re.compile(
    r"""
    \b(
        gradients?|
        dilution(?:\s+series)?|
        low[- ]amount|
        low[- ]input|
        input\s+amount|
        datasets?|
        data|
        library|
        libraries|
        spectral|
        analysis|
        analyses|
        method|
        methods|
        acquisition|
        window(?:s)?|
        fraction(?:s|ation)?|
        benchmark(?:s|ing)?|
        reference|
        raw\s+files?|
        samples?\s+per\s+day
    )\b
    """,
    re.I | re.X,
)

PHYSICAL_PREP_TERMS = {
    "facs",
    "fluorescence-activated",
    "cellenone",
    "sorting",
    "sorted",
    "isolated",
    "isolation",
    "deposited",
    "individual wells",
    "single wells",
    "lysis",
    "lysed",
    "digestion",
    "digested",
    "trypsin",
    "lys-c",
    "lysc",
    "nanopots",
    "proteochip",
    "384-well",
    "96-well",
    "accutase",
    "cell sorting",
}

ANALYSIS_ONLY_PREP_TERMS = {
    "library-free",
    "directdia",
    "spectronaut",
    "dia-nn",
    "maxquant",
    "fragpipe",
    "raw files",
    "search approach",
    "spectral library",
}


def looks_like_repository_accession(value):
    if not isinstance(value, str):
        return False

    return REPOSITORY_ACCESSION_RE.fullmatch(value.strip()) is not None


def looks_like_citation(value):
    if not isinstance(value, str):
        return False

    text = re.sub(r"\s+", " ", value.strip())
    lower = text.lower()

    if not text:
        return False

    if " et al." in lower or DOI_RE.search(text):
        return True

    # Author-list style text plus a parenthesized publication year.
    if CITATION_YEAR_RE.search(text) and text.count(",") >= 2:
        return True

    return False


def looks_like_biological_sample_name(name):
    if not isinstance(name, str):
        return False

    name = ABSTRACT_PREFIX_RE.sub("", name)
    name = re.sub(r"\s+", " ", name.strip())

    if not name:
        return False

    if len(name) > 120 or len(name.split()) > 14:
        return False

    if EVIDENCE_ID_RE.fullmatch(name):
        return False

    if YEAR_ONLY_RE.fullmatch(name):
        return False

    if AUTHOR_INITIALS_RE.fullmatch(name):
        return False

    if PXD_RE.search(name) or looks_like_repository_accession(name):
        return False

    if looks_like_citation(name) or REFERENCEISH_SAMPLE_RE.search(name):
        return False

    if GENERIC_SAMPLE_NAME_RE.fullmatch(name):
        return False

    if NON_CELL_SAMPLE_NAME_RE.fullmatch(name):
        return False

    if name.count(";") >= 1:
        return False

    # Pure quantities / size ranges are experimental descriptors, not sample
    # identities. Leading biological counts (e.g. "100 oocytes") are allowed
    # here and normalized later to the underlying sample type.
    if re.match(
        r"^[~<>≈∼]?\s*\d+(?:[.,]\d+)?(?:\s*[-–]\s*\d+(?:[.,]\d+)?)?"
        r"\s*(?:pg|ng|ug|µg|μg|mg|nm|µm|μm|um|mm|cm|ml|µl|μl|ul)\b",
        name,
        re.I,
    ):
        return False

    if re.search(r"\b\d+(?:\.\d+)?\s*(?:pg|ng|ug|µg)\b", name, re.I):
        return False

    if NON_BIOLOGICAL_SAMPLE_RE.search(name):
        return False

    return True


def split_compound_sample_name(name):
    """
    Split model-produced grouped labels such as:
      "TE and hPS cells"
      "A459, H460, HeLa cells"

    Only split simple comma/'and' lists. Parenthetical biological labels are
    otherwise left untouched.
    """
    if not isinstance(name, str):
        return []

    normalized = re.sub(r"\s+", " ", name.strip())

    if not normalized:
        return []

    if "," not in normalized and " and " not in normalized.lower():
        return [normalized]

    parts = re.split(
        r"\s*,\s*|\s+\band\b\s+",
        normalized,
        flags=re.I,
    )

    parts = [
        part.strip(" ,;")
        for part in parts
        if part.strip(" ,;")
    ]

    return parts or [normalized]


def has_physical_preparation_evidence(evidence_text):
    if not isinstance(evidence_text, str):
        return False

    lower = evidence_text.lower()

    return any(term in lower for term in PHYSICAL_PREP_TERMS)




def has_single_cell_specific_preparation_records(records):
    """
    For accession-scoped SCP datasets, generic cell-line digestion is not
    sufficient evidence of how the deposited single cells were prepared.

    Require physical preparation language and explicit single-cell language
    in the same retrieved source block.
    """
    single_cell_terms = (
        "single-cell",
        "single cell",
        "single-cell samples",
        "single cell samples",
        "individual cell",
        "individual cells",
        "single hela",
    )

    for record in records:
        text = record.get("text", "")
        lower = text.lower()

        has_sc = any(term in lower for term in single_cell_terms)
        has_physical = any(
            term in lower
            for term in PHYSICAL_PREP_TERMS
        )

        if has_sc and has_physical:
            return True

    return False


def is_meaningful_sample_preparation(value):
    """
    Reject descriptive labels such as
      "10-ng HeLa digest dilutions and single HeLa cell preparations"
    that name sample classes but do not describe a preparation operation.
    """
    if not isinstance(value, str):
        return False

    lower = value.lower()

    action_terms = (
        "lysis",
        "lysed",
        "digestion",
        "digested",
        "trypsin",
        "lys-c",
        "lysc",
        "reduction",
        "reduced",
        "alkylation",
        "alkylated",
        "teab",
        "ddm",
        "tfa",
        "dmso",
        "facs",
        "cellenone",
        "sorted",
        "sorting",
        "384-well",
        "96-well",
        "nanopots",
        "proteochip",
        "sp3",
        "stage-tip",
        "stagetip",
    )

    return any(term in lower for term in action_terms)


def resolve_accession_scoped_lc_configuration(
    result,
    lc_records,
    scope_kind,
):
    """
    Avoid selecting a specific LC platform when the paper only gives an
    experiment-wide choice such as "Vanquish Neo or Evosep ONE" and does not
    explicitly tie one platform to the target single-cell experiment.
    """
    warnings = []

    if scope_kind != "single_cell":
        return result, warnings

    system_patterns = {
        "Vanquish Neo": re.compile(r"\bVanquish\s+Neo\b", re.I),
        "Evosep ONE": re.compile(r"\bEvosep\s+ONE\b", re.I),
        "UltiMate 3000": re.compile(r"\bUltiMate\s+3000\b", re.I),
    }

    sc_terms = (
        "single-cell",
        "single cell",
        "single hela",
        "individual cell",
        "individual cells",
    )

    systems_seen = set()
    systems_tied_to_sc = set()

    for record in lc_records:
        text = record.get("text", "")
        lower = text.lower()
        record_has_sc = any(term in lower for term in sc_terms)

        for system, pattern in system_patterns.items():
            if pattern.search(text):
                systems_seen.add(system)

                if record_has_sc:
                    systems_tied_to_sc.add(system)

    config = result.get("lc_configuration")

    if (
        isinstance(config, str)
        and len(systems_seen) > 1
        and not systems_tied_to_sc
    ):
        result["lc_configuration"] = None
        warnings.append(
            "lc_configuration rejected because multiple LC systems were "
            "described but none was explicitly tied to the target "
            "single-cell experiment."
        )

    return result, warnings

def evidence_supports_isolation_method(method, evidence_text):
    """
    High-precision cell-isolation validation.

    v18 distinguishes physical cell isolation/dispensing from downstream
    RNA/DNA/protein extraction. A product such as an RNA isolation kit is not
    a single-cell isolation method even when its name occurs in the paper.
    """
    if not isinstance(method, str) or not isinstance(evidence_text, str):
        return False

    method = re.sub(r"\s+", " ", method.strip())
    evidence = re.sub(r"\s+", " ", evidence_text)

    if not method:
        return False

    if NON_CELL_ISOLATION_METHOD_RE.search(method):
        return False

    if not CELL_ISOLATION_ACTION_RE.search(method):
        return False

    lower_method = method.lower()
    lower_evidence = evidence.lower()

    method_vocab = {
        "facs": ("facs", "fluorescence-activated"),
        "cellenone": ("cellenone",),
        "flow cytometry": ("flow cytometry", "facs"),
        "sorting": ("sorting", "sorted"),
        "sorted": ("sorting", "sorted"),
        "manual": ("manual isolation", "manually isolated"),
        "micromanip": ("micromanipulation",),
        "micropip": ("micropipette", "micropipetting"),
        "laser capture": ("laser capture",),
        "microdissection": ("microdissection",),
        "picked": ("picked",),
        "captured": ("captured",),
        "deposited": ("deposited",),
        "dispensed": ("dispensed",),
        "nanowell": ("nanowell",),
    }

    recognized = False
    for canonical, evidence_terms in method_vocab.items():
        if canonical in lower_method:
            recognized = True
            if not any(term in lower_evidence for term in evidence_terms):
                return False

    if not recognized:
        action = CELL_ISOLATION_ACTION_RE.search(method)
        if action and action.group(0).lower() not in lower_evidence:
            return False

    return True



def deterministic_single_cell_depth(evidence_text):
    """
    Conservative fallback for explicit prose such as:
      ">4,500 proteins from single HeLa cells"

    Used only if the semantic extractor returns null.
    """
    if not isinstance(evidence_text, str):
        return None

    patterns = [
        re.compile(
            r"((?:~|>|up to\s+)?\d[\d,]*\s+"
            r"(?:protein groups?|proteins)\s+from\s+"
            r"(?:a\s+)?single[- ]?[A-Za-z0-9+(). -]{0,70}cells?)",
            re.I,
        ),
        re.compile(
            r"(\d[\d,]*\s+to\s+(?:a\s+maximum\s+of\s+)?"
            r"(?:~|>)?\d[\d,]*\s+(?:protein groups?|proteins)"
            r"[^.]{0,80}single[- ]?cells?)",
            re.I,
        ),
    ]

    for pattern in patterns:
        match = pattern.search(evidence_text)

        if match:
            return re.sub(
                r"\s+",
                " ",
                match.group(1).strip(),
            )

    return None

def canonicalize_organism(value):
    if value is None or not isinstance(value, str):
        return None

    stripped = re.sub(r"\s+", " ", value.strip())
    normalized = stripped.lower()

    if looks_like_citation(stripped) or looks_like_repository_accession(stripped):
        return None

    if NON_ORGANISM_VALUE_RE.search(stripped):
        return None

    if normalized in ORGANISM_CANONICAL:
        return ORGANISM_CANONICAL[normalized]

    extra_common = {
        "drosophila melanogaster": "Drosophila melanogaster",
        "danio rerio": "Danio rerio",
        "zebrafish": "Danio rerio",
        "rattus norvegicus": "Rattus norvegicus",
        "rat": "Rattus norvegicus",
    }

    if normalized in extra_common:
        return extra_common[normalized]

    if re.fullmatch(r"[A-Z][a-z]+\s+[a-z][A-Za-z0-9._-]+", stripped):
        second = stripped.split(None, 1)[1].lower()
        if second not in {
            "cell", "cells", "tissue", "tissues", "plasma", "serum",
            "blood", "control", "sample", "samples",
        }:
            return stripped

    return None


def canonicalize_sample_name(name):
    if not isinstance(name, str):
        return name

    stripped = ABSTRACT_PREFIX_RE.sub("", name)
    stripped = re.sub(r"\s+", " ", stripped.strip())

    # Move leading biological counts out of sample identity. The count remains
    # available to the dedicated cell-count field when the model extracted it.
    count_match = re.match(
        r"^\d[\d,]*\s+(?=(?:single\s+)?(?:oocytes?|cells?|neurons?|"
        r"blastomeres?|bacteria|bacterium|sperm(?:\s+cells?)?|egg\s+cells?))",
        stripped,
        re.I,
    )
    if count_match:
        stripped = stripped[count_match.end():].strip()

    key = stripped.lower()

    if key in KNOWN_SAMPLE_ALIASES:
        return KNOWN_SAMPLE_ALIASES[key]

    # Normalize common cosmetic variants without changing biological meaning.
    if key in {"a549", "a549 cell", "a549 cells"}:
        return "A549 cells"
    if key in {"a459", "a459 cell", "a459 cells"}:
        return "A549 cells"
    if key in {"h460", "h460 cell", "h460 cells"}:
        return "H460 cells"
    if key in {"hela", "hela cell", "hela cells", "single hela cells"}:
        return "single HeLa cells" if "single" in key else "HeLa cells"
    if key in {"te", "te cells", "te-like", "te-like cells"}:
        return "TE-like cells"
    if key in {"hps", "hps cell", "hps cells"}:
        return "hPS cells"

    return stripped


def sample_search_terms(sample_name):
    """
    Return conservative text-search aliases for an already identified sample.
    These are used only to find source support, not to invent sample identity.
    """
    name = canonicalize_sample_name(sample_name)
    lower = name.lower()

    terms = {lower}

    without_cells = re.sub(
        r"\b(?:cells?|cell line)\b",
        "",
        lower,
    )
    without_cells = re.sub(r"\s+", " ", without_cells).strip()

    if without_cells:
        terms.add(without_cells)

    if "hela" in lower:
        terms.add("hela")

    if "a549" in lower:
        terms.add("a549")

    if "h460" in lower:
        terms.add("h460")

    if "naive hps" in lower or "pluripotent stem" in lower:
        terms.update(
            {
                "naive hps",
                "naive ps cells",
                "naive human pluripotent",
            }
        )

    if "te-like" in lower or "trophectoderm" in lower:
        terms.update(
            {
                "te-like",
                "trophectoderm-like",
                "single te",
            }
        )

    return {
        term
        for term in terms
        if len(term) >= 3
    }


def infer_organism_from_source(sample_name, blocks):
    """
    Conservative source-grounded organism inference.

    v17 used a broad local window and could attach an organism from a nearby
    comparison experiment. v18 only propagates an organism when it is directly
    linked to the sample name in the same short phrase/sentence. Ambiguous
    cases stay null.
    """
    if not isinstance(sample_name, str):
        return None

    name = canonicalize_sample_name(sample_name)
    lower_name = name.lower()

    embedded = (
        ("Homo sapiens", r"\b(?:human|homo sapiens)\b"),
        ("Mus musculus", r"\b(?:mouse|murine|mus musculus)\b"),
        ("Oryza sativa", r"\b(?:rice|oryza sativa)\b"),
        ("Xenopus laevis", r"\bxenopus laevis\b"),
        ("Arabidopsis thaliana", r"\barabidopsis thaliana\b"),
        ("Saccharomyces cerevisiae", r"\b(?:yeast|saccharomyces cerevisiae)\b"),
        ("Escherichia coli", r"\b(?:e\. coli|escherichia coli)\b"),
        ("Caenorhabditis elegans", r"\b(?:c\. elegans|caenorhabditis elegans)\b"),
    )

    embedded_hits = {
        organism
        for organism, pattern in embedded
        if re.search(pattern, lower_name, re.I)
    }
    if len(embedded_hits) == 1:
        return next(iter(embedded_hits))

    terms = sorted(sample_search_terms(name), key=len, reverse=True)
    aliases = (
        ("Homo sapiens", r"(?:human|homo sapiens)"),
        ("Mus musculus", r"(?:mouse|murine|mus musculus)"),
        ("Oryza sativa", r"(?:rice|oryza sativa)"),
        ("Xenopus laevis", r"(?:xenopus laevis)"),
        ("Arabidopsis thaliana", r"(?:arabidopsis thaliana)"),
        ("Saccharomyces cerevisiae", r"(?:yeast|saccharomyces cerevisiae)"),
        ("Escherichia coli", r"(?:e\. coli|escherichia coli)"),
        ("Caenorhabditis elegans", r"(?:c\. elegans|caenorhabditis elegans)"),
    )

    supported = set()

    for item in blocks:
        text = re.sub(r"\s+", " ", item.get("text", ""))
        lower = text.lower()

        for term in terms:
            term_lower = term.lower()
            start_pos = 0
            while True:
                pos = lower.find(term_lower, start_pos)
                if pos < 0:
                    break

                left = max(0, pos - 90)
                right = min(len(text), pos + len(term_lower) + 90)
                local = text[left:right]

                for organism, alias in aliases:
                    relation = re.compile(
                        rf"\b{alias}\b[^,;:.]{{0,45}}\b{re.escape(term)}\b"
                        rf"|\b{re.escape(term)}\b[^,;:.]{{0,45}}"
                        rf"(?:from|of|derived\s+from|in)\s+\b{alias}\b",
                        re.I,
                    )
                    if relation.search(local):
                        supported.add(organism)

                start_pos = pos + len(term_lower)

    if len(supported) == 1:
        return next(iter(supported))
    return None



def enrich_respective_cell_counts(samples, blocks):
    """
    Recover explicit constructions such as:
      "The dataset contains 21 and 12 individual hPS cells and TE-like cells,
       respectively."

    This is intentionally narrow: it only acts when the source explicitly uses
    "respectively", so the count-to-sample mapping is deterministic.
    """
    mappings = {}

    pattern = re.compile(
        r"(?:contains|included|comprised)\s+"
        r"(\d+)\s+and\s+(\d+)\s+"
        r"(?:individual\s+)?"
        r"([A-Za-z0-9+.-]+(?:\s+[A-Za-z0-9+.-]+){0,3})\s+cells?\s+"
        r"and\s+"
        r"([A-Za-z0-9+.-]+(?:\s+[A-Za-z0-9+.-]+){0,3})\s+cells?"
        r"\s*,?\s*respectively",
        re.I,
    )

    for item in blocks:
        text = re.sub(r"\s+", " ", item["text"])

        for match in pattern.finditer(text):
            count1, count2, name1, name2 = match.groups()

            canonical1 = canonicalize_sample_name(name1)
            canonical2 = canonicalize_sample_name(name2)

            mappings[sample_name_key(canonical1)] = (
                f"{count1} individual {canonical1}"
            )
            mappings[sample_name_key(canonical2)] = (
                f"{count2} individual {canonical2}"
            )

    enriched = []

    for sample in samples:
        item = dict(sample)
        key = sample_name_key(item.get("sample_type"))
        counts = list(item.get("cell_counts_reported", []))

        inferred = mappings.get(key)

        if inferred and count_value_key(inferred) not in {
            count_value_key(value)
            for value in counts
        }:
            counts.append(inferred)

        item["cell_counts_reported"] = counts
        enriched.append(item)

    return enriched

def count_value_key(value):
    if not isinstance(value, str):
        return ""

    return re.sub(r"\s+", " ", value.strip().lower())


def normalize_and_merge_true_samples(samples, blocks):
    """
    Apply explicit aliases, canonical organism names, source-grounded organism
    propagation, and duplicate merging.

    Cell-count strings are preserved verbatim and de-duplicated; we do not
    calculate or reconcile differing experiment counts.
    """
    merged = {}

    for sample in samples:
        if not isinstance(sample, dict):
            continue

        raw_names = split_compound_sample_name(
            sample.get("sample_type")
        )

        counts = sample.get(
            "cell_counts_reported",
            [],
        )

        if not isinstance(counts, list):
            counts = []

        counts = [
            value
            for value in counts
            if isinstance(value, str)
            and value.strip()
        ]

        for raw_name in raw_names:
            if not looks_like_biological_sample_name(raw_name):
                continue

            name = canonicalize_sample_name(raw_name)

            if not name:
                continue

            key = sample_name_key(name)

            proposed_organism = canonicalize_organism(
                sample.get("organism")
            )
            source_organism = infer_organism_from_source(
                name,
                blocks,
            )

            # High-precision metadata policy: the locally grounded source
            # relationship wins. A model-only organism is not retained.
            organism = source_organism

            if key not in merged:
                merged[key] = {
                    "sample_type": name,
                    "organism": organism,
                    "cell_counts_reported": [],
                }

            if (
                merged[key]["organism"] is None
                and organism is not None
            ):
                merged[key]["organism"] = organism

            existing_count_keys = {
                count_value_key(value)
                for value in merged[key]["cell_counts_reported"]
            }

            for value in counts:
                count_key = count_value_key(value)

                if (
                    count_key
                    and count_key not in existing_count_keys
                ):
                    merged[key]["cell_counts_reported"].append(
                        value
                    )
                    existing_count_keys.add(count_key)

    return list(merged.values())


def normalize_low_input_benchmarks(benchmarks):
    normalized = []

    for benchmark in benchmarks:
        if not isinstance(benchmark, dict):
            continue

        item = dict(benchmark)
        sample_name = item.get("sample")

        if (
            looks_like_repository_accession(sample_name)
            or looks_like_citation(sample_name)
        ):
            continue

        item["organism"] = canonicalize_organism(
            item.get("organism")
        )
        normalized.append(item)

    return normalized


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def normalize_null_strings(value):
    if isinstance(value, dict):
        return {
            key: normalize_null_strings(val)
            for key, val in value.items()
        }

    if isinstance(value, list):
        return [
            normalize_null_strings(item)
            for item in value
        ]

    if isinstance(value, str):
        if value.strip().lower() in NULL_LIKE_STRINGS:
            return None

    return value


def normalized_for_support(text):
    return re.sub(
        r"\s+",
        " ",
        text.replace(",", ""),
    ).lower()


def numeric_tokens(text):
    if not isinstance(text, str):
        return []

    normalized = text.replace(",", "")

    return re.findall(
        r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?(?![A-Za-z0-9.])",
        normalized,
    )


def _number_matches(evidence, number):
    return list(
        re.finditer(
            rf"(?<![\d.]){re.escape(number)}(?![\d.])",
            evidence,
        )
    )


def _window_has(pattern, evidence, match, radius=90):
    left = max(0, match.start() - radius)
    right = min(len(evidence), match.end() + radius)
    return re.search(pattern, evidence[left:right], re.I) is not None


def validate_numeric_string(value, evidence_text, context=None):
    """
    Require emitted quantities to be supported as whole numeric tokens and,
    when the field semantics are known, in the appropriate nearby context.

    This prevents failures such as:
      20 cells being accepted because the evidence contains 200 uL;
      250 pg being accepted because the evidence contains 250 mM;
      protein-depth numbers being borrowed from unrelated numeric statements.
    """
    if value is None or not isinstance(value, str):
        return value

    numbers = numeric_tokens(value)

    if not numbers:
        return value

    evidence = normalized_for_support(evidence_text)
    value_normalized = normalized_for_support(value)

    # Explicit quantity + unit pairs in the model value must occur as a pair
    # in the source evidence, not merely as the same naked number.
    unit_pairs = re.findall(
        r"(?<![\d.])(\d+(?:\.\d+)?)\s*"
        r"(pg|ng|ug|µg|μg|mg|mm|mmol|mm|"
        r"ul|µl|μl|ml|%|min|mins|minutes|h|hr|hrs|hours)",
        value_normalized,
        re.I,
    )

    for number, unit in unit_pairs:
        unit_variants = {
            "µg": r"(?:ug|µg|μg)",
            "μg": r"(?:ug|µg|μg)",
            "ug": r"(?:ug|µg|μg)",
            "µl": r"(?:ul|µl|μl)",
            "μl": r"(?:ul|µl|μl)",
            "ul": r"(?:ul|µl|μl)",
        }
        unit_re = unit_variants.get(
            unit.lower(),
            re.escape(unit.lower()),
        )
        if not re.search(
            rf"(?<![\d.]){re.escape(number)}(?![\d.])"
            rf"\s*{unit_re}\b",
            evidence,
            re.I,
        ):
            return None

    for number in numbers:
        matches = _number_matches(evidence, number)

        if not matches:
            return None

        if context == "cell_count":
            if not any(
                _window_has(
                    r"\b(?:single|individual)?\s*cells?\b",
                    evidence,
                    match,
                    radius=55,
                )
                for match in matches
            ):
                return None

        elif context == "input_amount":
            if not any(
                _window_has(
                    r"\b(?:pg|ng|ug|µg|μg|mg)\b",
                    evidence,
                    match,
                    radius=20,
                )
                for match in matches
            ):
                return None

        elif context in {
            "single_cell_proteome_depth",
            "low_input_proteome_depth",
        }:
            # Numbers with explicit mass units are input amounts; otherwise
            # require the number to occur near proteins/protein groups.
            number_has_mass_unit = re.search(
                rf"(?<![\d.]){re.escape(number)}(?![\d.])"
                r"\s*(?:pg|ng|ug|µg|μg|mg)\b",
                value_normalized,
                re.I,
            )
            if number_has_mass_unit:
                if not any(
                    _window_has(
                        r"\b(?:pg|ng|ug|µg|μg|mg)\b",
                        evidence,
                        match,
                        radius=20,
                    )
                    for match in matches
                ):
                    return None
            else:
                if not any(
                    _window_has(
                        r"\b(?:proteins?|protein\s+groups?)\b",
                        evidence,
                        match,
                        radius=90,
                    )
                    for match in matches
                ):
                    return None

        elif context == "single_cell_throughput":
            if not any(
                _window_has(
                    r"\b(?:samples?|cells?)\s*(?:per|/)\s*day\b"
                    r"|\bspd\b",
                    evidence,
                    match,
                    radius=70,
                )
                for match in matches
            ):
                return None

    return value


def sample_name_has_single_cell_support(sample_name, evidence_text):
    """
    Require a model-proposed sample to be tied locally to BOTH individual-cell
    language and an actual proteomics/MS measurement context.

    Merely occurring near the words "single-cell proteomics" is insufficient;
    this avoids pulling roots/shoots, reference authors, cell-line inventories,
    or unrelated samples from the same paragraph into the catalogue.
    """
    if not isinstance(sample_name, str) or not isinstance(evidence_text, str):
        return False

    sample_name = canonicalize_sample_name(sample_name)

    if not looks_like_biological_sample_name(sample_name):
        return False

    evidence = re.sub(r"\s+", " ", evidence_text)
    lower = evidence.lower()
    terms = sample_search_terms(sample_name)

    individual_re = re.compile(
        r"\b(?:single[- ]cell|single\s+[A-Za-z0-9+()./-]{0,45}\s+cell|"
        r"individual\s+(?:single\s+)?cells?|one\s+cell\s+per\s+well|"
        r"single\s+(?:oocyte|neuron|blastomere|bacterium|bacterial\s+cell|"
        r"sperm\s+cell|egg\s+cell))\b",
        re.I,
    )
    measurement_re = re.compile(
        r"\b(?:analy[sz](?:ed|ing)|measur(?:ed|ing|ement)|profiled|"
        r"quantif(?:ied|ication)|identified|subjected\s+to|injected|loaded|"
        r"acquired|proteomic\s+analysis|mass\s+spectrom(?:etry|etric)|"
        r"LC[- ]?MS(?:/MS)?|MS/MS)\b",
        re.I,
    )
    proteomics_re = re.compile(
        r"\b(?:proteom(?:e|es|ics|ic)|mass\s+spectrom(?:etry|etric)|"
        r"LC[- ]?MS(?:/MS)?|MS/MS|Orbitrap|timsTOF|Astral)\b",
        re.I,
    )

    for term in terms:
        term_lower = term.lower()
        start_pos = 0

        while True:
            pos = lower.find(term_lower, start_pos)
            if pos < 0:
                break

            left = max(0, pos - 180)
            right = min(len(evidence), pos + len(term_lower) + 180)
            window = evidence[left:right]

            sample_has_cell_unit = (
                BIOLOGICAL_CELL_UNIT_RE.search(sample_name) is not None
            )

            escaped_term = re.escape(term)
            local_unit_link = re.search(
                rf"\b{escaped_term}\b[^.!?]{{0,35}}\bcells?\b"
                rf"|\b(?:single|individual)\s+{escaped_term}\b",
                window,
                re.I,
            )

            if (
                sample_has_cell_unit or local_unit_link
            ) and (
                individual_re.search(window)
                and proteomics_re.search(window)
                and measurement_re.search(window)
            ):
                # Require the individual-cell phrase to be reasonably close
                # to the sample occurrence, not merely elsewhere in a long
                # evidence block.
                local_pos = pos - left
                for sc_match in individual_re.finditer(window):
                    if abs(sc_match.start() - local_pos) <= 120:
                        return True

            start_pos = pos + len(term_lower)

    return False


def validate_sample_result(result, evidence_text):
    valid_true_samples = []

    for sample in result.get("true_single_cell_samples", []):
        sample_name = sample.get("sample_type", "")

        counts = sample.get("cell_counts_reported", [])

        if not isinstance(counts, list):
            counts = []

        validated_counts = []

        for count in counts:
            validated = validate_numeric_string(
                count,
                evidence_text,
                context="cell_count",
            )

            if validated is not None:
                validated_counts.append(validated)

        for expanded_name in split_compound_sample_name(sample_name):
            if not looks_like_biological_sample_name(expanded_name):
                continue

            if not sample_name_has_single_cell_support(
                expanded_name,
                evidence_text,
            ):
                continue

            organism = sample.get("organism")
            if looks_like_citation(organism):
                organism = None

            valid_true_samples.append(
                {
                    "sample_type": expanded_name,
                    "organism": organism,
                    "cell_counts_reported": list(validated_counts),
                }
            )

    valid_benchmarks = []

    for benchmark in result.get("low_input_benchmarks", []):
        sample_name = benchmark.get("sample", "")

        if (
            isinstance(sample_name, str)
            and (
                PXD_RE.search(sample_name)
                or looks_like_repository_accession(sample_name)
                or looks_like_citation(sample_name)
            )
        ):
            continue

        if looks_like_citation(benchmark.get("organism")):
            benchmark["organism"] = None

        benchmark["input_amount"] = validate_numeric_string(
            benchmark.get("input_amount"),
            evidence_text,
            context="input_amount",
        )

        valid_benchmarks.append(benchmark)

    result["true_single_cell_samples"] = valid_true_samples
    result["low_input_benchmarks"] = valid_benchmarks

    return result


def validate_performance_result(result, evidence_text):
    for key in (
        "single_cell_throughput",
        "single_cell_proteome_depth",
        "low_input_proteome_depth",
    ):
        result[key] = validate_numeric_string(
            result.get(key),
            evidence_text,
            context=key,
        )

    return result


def post_ollama_json(
    *,
    task_name,
    system,
    prompt,
    schema,
    ollama_url,
    model,
    num_ctx,
    num_predict,
    cpu_threads,
    timeout,
    auto_retry,
    raw_output_dir,
):
    logical_cpus = os.cpu_count()

    options = {
        "num_ctx": num_ctx,
        "num_predict": num_predict,
        "temperature": 0,
        "seed": 42,
    }

    if cpu_threads is not None:
        options["num_thread"] = cpu_threads

    attempt = 1
    current_num_predict = num_predict

    while True:
        options["num_predict"] = current_num_predict

        print(f"\n[{task_name}] model: {model}")
        print(f"[{task_name}] max generated tokens: {current_num_predict}")

        if cpu_threads is None:
            cpu_text = "Ollama automatic"
        else:
            cpu_text = str(cpu_threads)

        if logical_cpus:
            cpu_text += f" / {logical_cpus} logical CPUs"

        print(f"[{task_name}] CPU threads: {cpu_text}")

        started = time.perf_counter()

        response = requests.post(
            ollama_url,
            json={
                "model": model,
                "system": system,
                "prompt": prompt,
                "format": schema,
                "stream": False,
                "think": False,
                "keep_alive": "30m",
                "options": options,
            },
            timeout=timeout,
        )

        response.raise_for_status()
        data = response.json()

        wall = time.perf_counter() - started

        prompt_tokens = data.get("prompt_eval_count", 0)
        output_tokens = data.get("eval_count", 0)

        prompt_seconds = (
            data.get("prompt_eval_duration", 0) / 1e9
        )
        generation_seconds = (
            data.get("eval_duration", 0) / 1e9
        )
        load_seconds = (
            data.get("load_duration", 0) / 1e9
        )

        print(
            f"[{task_name}] wall: {wall:.1f} s; "
            f"load: {load_seconds:.1f} s"
        )

        if prompt_seconds:
            print(
                f"[{task_name}] prompt: {prompt_tokens} tokens in "
                f"{prompt_seconds:.1f} s "
                f"({prompt_tokens / prompt_seconds:.1f} tok/s)"
            )

        if generation_seconds:
            print(
                f"[{task_name}] output: {output_tokens} tokens in "
                f"{generation_seconds:.1f} s "
                f"({output_tokens / generation_seconds:.1f} tok/s)"
            )

        raw = data.get("response", "")

        raw_path = raw_output_dir / (
            f"{task_name}.attempt{attempt}.raw.txt"
        )
        raw_path.write_text(raw, encoding="utf-8")

        hit_limit = output_tokens >= current_num_predict

        try:
            parsed = json.loads(raw)

            if hit_limit:
                print(
                    f"[{task_name}] note: output reached the token limit "
                    "but still formed valid JSON."
                )

            return normalize_null_strings(parsed), {
                "wall_seconds": round(wall, 3),
                "prompt_tokens": prompt_tokens,
                "output_tokens": output_tokens,
                "prompt_seconds": round(prompt_seconds, 3),
                "generation_seconds": round(
                    generation_seconds,
                    3,
                ),
                "load_seconds": round(load_seconds, 3),
                "num_predict": current_num_predict,
                "attempts": attempt,
            }

        except json.JSONDecodeError:
            if task_name == "samples":
                max_attempts = 3
                retry_ceiling = 2_000
            else:
                max_attempts = 2
                retry_ceiling = None

            can_retry = (
                auto_retry
                and attempt < max_attempts
                and hit_limit
            )

            if not can_retry:
                raise RuntimeError(
                    f"Ollama task '{task_name}' returned invalid JSON. "
                    f"Raw response saved to {raw_path}. "
                    f"output_tokens={output_tokens}, "
                    f"num_predict={current_num_predict}."
                )

            if task_name == "samples":
                # With the default this yields 630 -> 1260 -> 2000.
                next_limit = min(
                    retry_ceiling,
                    max(current_num_predict + 320, current_num_predict * 2),
                )
            else:
                next_limit = max(
                    current_num_predict + 160,
                    int(current_num_predict * 1.5),
                )

            if next_limit <= current_num_predict:
                raise RuntimeError(
                    f"Ollama task '{task_name}' returned invalid JSON at "
                    f"the retry ceiling ({current_num_predict} tokens). "
                    f"Raw response saved to {raw_path}."
                )

            print(
                f"[{task_name}] output was truncated at "
                f"{current_num_predict} tokens; automatically retrying "
                f"with {next_limit} (attempt {attempt + 1}/{max_attempts})."
            )

            current_num_predict = next_limit
            attempt += 1


# ---------------------------------------------------------------------------
# Three semantic extraction tasks
# ---------------------------------------------------------------------------

def run_samples_task(
    evidence_text,
    *,
    target_accession,
    ollama_args,
):
    system = (
        "You are a precise scientific metadata extractor. Identify only "
        "genuine single-cell proteomics samples and low-input benchmark "
        "samples explicitly supported by the supplied evidence. Never "
        "perform arithmetic or infer a quantity. Copy numerical quantities "
        "verbatim from the evidence. A diluted bulk digest is never a true "
        "single cell. Preserve named cell types rather than collapsing them "
        "to a generic 'single cells' label. If the same sample name appears "
        "in both diluted-digest and cell contexts, classify it as a true "
        "single-cell sample only when the evidence explicitly states that "
        "an individual/single cell of that named type was measured. PRIDE "
        "or other repository accessions are never biological samples, and "
        "reference-list author/year strings are never organisms."
    )

    prompt = (
        f"Target PRIDE accession: {target_accession or 'not specified'}\n\n"
        "Return only the structured fields requested by the schema.\n"
        "Repository identifiers (for example PXD, RPXD, MSV, MTBLS, JPST, "
        "or PASS accessions) are metadata and must NEVER be emitted as a "
        "sample_type or low-input sample. Bibliographic citations, author "
        "names, years, and reference-list text must NEVER be emitted as an "
        "organism. Only report a biological sample when its name and its "
        "single-cell proteomics context are both present in the supplied "
        "evidence. If target-accession-local biological sample evidence is "
        "absent, return an empty sample list rather than extrapolating from "
        "other datasets or references in the publication.\n"
        "For true_single_cell_samples, cell_counts_reported is a list of "
        "distinct experiment-count phrases copied from the evidence for that "
        "named sample type. Do not invent example values and do not calculate "
        "totals. Use an empty list when no count is explicitly supported.\n"
        "For low_input_benchmarks, input_amount must be copied exactly from "
        "the evidence; do not add, subtract, normalize, or reinterpret "
        "amounts. The target PXD accession is metadata, never a biological "
        "sample name.\n\n"
        f"Evidence:\n{evidence_text}"
    )

    return post_ollama_json(
        task_name="samples",
        system=system,
        prompt=prompt,
        schema=SAMPLES_SCHEMA,
        **ollama_args,
    )


def run_preparation_task(
    evidence_text,
    *,
    ollama_args,
):
    system = (
        "You extract concise single-cell sample-preparation metadata from a "
        "proteomics paper. Use only explicit evidence. Do not infer missing "
        "steps. Focus on cell isolation/sorting, lysis, digestion, handling, "
        "and sample-preparation platforms. Do not report LC columns, LC "
        "gradients, mass-spectrometer settings, or data-analysis software."
    )

    prompt = (
        "Return only sample_preparation and single_cell_isolation. "
        "single_cell_isolation is an array grouping only genuine "
        "individual-cell samples that used the same explicitly stated "
        "isolation method. Do not list pooled, carrier, library, multi-cell, "
        "or bulk samples. Preserve only sample names and isolation methods "
        "that literally occur in the evidence. "
        "Keep sample_preparation concise and catalogue-like. If the evidence "
        "mixes biological sample handling with LC/MS details, ignore the "
        "LC/MS details.\n\n"
        f"Evidence:\n{evidence_text}"
    )

    return post_ollama_json(
        task_name="preparation",
        system=system,
        prompt=prompt,
        schema=PREPARATION_SCHEMA,
        **ollama_args,
    )


def run_lc_task(
    evidence_text,
    *,
    ollama_args,
):
    system = (
        "You extract only liquid-chromatography metadata from a proteomics "
        "paper. Use explicit LC evidence only. Sample-preparation reagents, "
        "plates, cell sorters, lysis buffers, trypsin, TEAB, DDM, and "
        "digestion conditions are not LC configuration or LC gradient."
    )

    prompt = (
        "Return only lc_configuration and lc_gradient. "
        "For lc_configuration, include the LC/UHPLC system AND analytical "
        "column when both are explicitly present. For lc_gradient, report "
        "the tested gradient-duration range and, when explicit, the selected "
        "single-cell operating condition; keep it "
        "concise and do not copy "
        "an entire explanatory sentence. If the evidence does not explicitly "
        "support an LC field, return null.\n\n"
        f"Evidence:\n{evidence_text}"
    )

    return post_ollama_json(
        task_name="lc",
        system=system,
        prompt=prompt,
        schema=LC_SCHEMA,
        **ollama_args,
    )


def run_single_cell_performance_task(
    evidence_text,
    *,
    ollama_args,
):
    system = (
        "You extract quantitative performance reported specifically for "
        "genuine individual-cell proteomics measurements. Use only explicit "
        "evidence and copy quantities verbatim. Never do arithmetic. Never "
        "use a value reported for picogram input, diluted bulk digest, "
        "peptide mixtures, or other low-input benchmarks."
    )

    prompt = (
        "Return only single_cell_throughput and "
        "single_cell_proteome_depth. A valid proteome-depth value must be "
        "explicitly tied to an individual/single cell or named genuine "
        "single-cell experiment. When the evidence reports both a RANGE and "
        "a maximum for genuine single cells, preserve the informative range "
        "rather than returning only the maximum. Copy the complete "
        "source-supported range verbatim. If a value "
        "is tied to 'pg', peptide input, or diluted material, return null for "
        "the single-cell field.\n\n"
        f"Evidence:\n{evidence_text}"
    )

    return post_ollama_json(
        task_name="single_cell_performance",
        system=system,
        prompt=prompt,
        schema=SINGLE_CELL_PERFORMANCE_SCHEMA,
        **ollama_args,
    )


def run_low_input_performance_task(
    evidence_text,
    *,
    ollama_args,
):
    system = (
        "You extract quantitative proteome depth reported specifically for "
        "low-input or diluted bulk proteomics benchmarks. Use only explicit "
        "evidence and copy quantities verbatim. Never do arithmetic. Never "
        "use values reported for genuine individual cells."
    )

    prompt = (
        "Return only low_input_proteome_depth. This field must report a "
        "NUMBER OF PROTEINS OR PROTEIN GROUPS, together with the associated "
        "low-input amount when available. An input amount by itself is NOT "
        "proteome depth. Prefer a directly stated source phrase containing "
        "both the protein/protein-group count and its low-input amount. "
        "The value must be "
        "explicitly associated with a picogram input, diluted bulk digest, "
        "peptide amount, or low-input benchmark. If no protein/protein-group "
        "count is supported, return null.\n\n"
        f"Evidence:\n{evidence_text}"
    )

    return post_ollama_json(
        task_name="low_input_performance",
        system=system,
        prompt=prompt,
        schema=LOW_INPUT_PERFORMANCE_SCHEMA,
        **ollama_args,
    )

# ---------------------------------------------------------------------------
# Post-merge semantic sanity checks
# ---------------------------------------------------------------------------

LC_FORBIDDEN_TERMS = {
    "trypsin",
    "teab",
    "ddm",
    "lysis",
    "digestion",
    "cellenone",
    "facs",
    "384-well",
    "96-well",
    "accutase",
}

SAMPLE_PREP_FORBIDDEN_TERMS = {
    "aurora",
    "nanolc",
    "ultimate 3000",
    "vanquish",
    "emitter",
    "analytical column",
}


def contains_any(text, terms):
    if not isinstance(text, str):
        return False

    lower = text.lower()
    return any(term in lower for term in terms)


def sanitize_lc_fields(result):
    warnings = []

    for key in ("lc_configuration", "lc_gradient"):
        value = result.get(key)

        if contains_any(value, LC_FORBIDDEN_TERMS):
            warnings.append(
                f"{key} rejected because it contained sample-preparation "
                "terminology."
            )
            result[key] = None

    return result, warnings


def sanitize_preparation_fields(result, evidence_text):
    warnings = []

    value = result.get("sample_preparation")

    if (
        contains_any(value, SAMPLE_PREP_FORBIDDEN_TERMS)
        or contains_any(value, ANALYSIS_ONLY_PREP_TERMS)
        or (
            value is not None
            and not is_meaningful_sample_preparation(value)
        )
    ):
        warnings.append(
            "sample_preparation rejected because it did not describe a "
            "supported physical sample-preparation protocol."
        )
        result["sample_preparation"] = None

    isolation = result.get("single_cell_isolation", [])

    if not isinstance(isolation, list):
        isolation = []

    cleaned_isolation = []

    for entry in isolation:
        if not isinstance(entry, dict):
            continue

        method = entry.get("method")
        samples = entry.get("samples", [])

        if (
            contains_any(method, SAMPLE_PREP_FORBIDDEN_TERMS)
            or contains_any(method, ANALYSIS_ONLY_PREP_TERMS)
        ):
            warnings.append(
                "single_cell_isolation entry rejected because its method "
                "contained LC/data-analysis terminology."
            )
            continue

        if not evidence_supports_isolation_method(
            method,
            evidence_text,
        ):
            warnings.append(
                "single_cell_isolation entry rejected because the stated "
                "isolation method was not present in the selected evidence."
            )
            continue

        if not isinstance(samples, list):
            samples = []

        normalized_samples = []

        for sample in samples:
            for expanded in split_compound_sample_name(sample):
                if looks_like_biological_sample_name(expanded):
                    normalized_samples.append(
                        canonicalize_sample_name(expanded)
                    )

        normalized_samples = list(
            dict.fromkeys(normalized_samples)
        )

        if method and normalized_samples:
            cleaned_isolation.append(
                {
                    "samples": normalized_samples,
                    "method": method,
                }
            )

    result["single_cell_isolation"] = cleaned_isolation

    return result, warnings


def sanitize_single_cell_performance(result):
    warnings = []

    depth = result.get("single_cell_proteome_depth")
    throughput = result.get("single_cell_throughput")

    if isinstance(depth, str):
        lower = depth.lower()

        if not re.search(
            r"\b(?:protein|proteins|protein groups?|pgs?)\b",
            lower,
        ):
            warnings.append(
                "single_cell_proteome_depth rejected because it contained "
                "no protein/protein-group count."
            )
            result["single_cell_proteome_depth"] = None

        elif (
            re.search(r"\b\d+(?:\.\d+)?\s*(?:pg|ng)\b", lower)
            or "peptide" in lower
            or "dilut" in lower
            or "bulk" in lower
        ):
            warnings.append(
                "single_cell_proteome_depth rejected because it looked like "
                "a low-input/bulk benchmark value."
            )
            result["single_cell_proteome_depth"] = None

    if isinstance(throughput, str):
        if "pg" in throughput.lower():
            warnings.append(
                "single_cell_throughput rejected because it contained a "
                "picogram input amount."
            )
            result["single_cell_throughput"] = None

    return result, warnings


def sanitize_low_input_performance(result):
    warnings = []
    depth = result.get("low_input_proteome_depth")

    if isinstance(depth, str):
        lower = depth.lower()

        if not re.search(
            r"\b(?:protein|proteins|protein groups?|pgs?)\b",
            lower,
        ):
            warnings.append(
                "low_input_proteome_depth rejected because it contained no "
                "protein/protein-group count."
            )
            result["low_input_proteome_depth"] = None

        elif (
            "single cell" in lower
            or "single-cell" in lower
            or "per cell" in lower
        ) and not re.search(
            r"\b\d+(?:\.\d+)?\s*(?:pg|ng)\b",
            lower,
        ):
            warnings.append(
                "low_input_proteome_depth rejected because it looked like "
                "genuine single-cell performance."
            )
            result["low_input_proteome_depth"] = None

    return result, warnings



def sample_name_key(name):
    if not isinstance(name, str):
        return ""

    name = name.lower()
    name = re.sub(
        r"\b(?:single|cells?|cell line|individual)\b",
        " ",
        name,
    )
    name = re.sub(r"[^a-z0-9]+", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def merge_isolation_samples_into_true_samples(
    true_samples,
    isolation_groups,
):
    """
    The preparation task sees high-quality Methods evidence that explicitly
    states which samples were deposited as single cells. Use it as an
    independent recall source instead of asking one LLM call to recover every
    named sample type.

    Existing sample-classifier entries win because they may carry cell counts.
    Missing samples from the isolation groups are added with organism/cell_count
    left null rather than inferred.
    """
    merged = [
        dict(sample)
        for sample in true_samples
        if isinstance(sample, dict)
    ]

    existing_keys = {
        sample_name_key(sample.get("sample_type"))
        for sample in merged
    }

    for group in isolation_groups:
        if not isinstance(group, dict):
            continue

        for raw_sample_name in group.get("samples", []):
            for sample_name in split_compound_sample_name(
                raw_sample_name
            ):
                if not looks_like_biological_sample_name(sample_name):
                    continue

                sample_name = canonicalize_sample_name(sample_name)
                key = sample_name_key(sample_name)

                if not key or key in existing_keys:
                    continue

                merged.append(
                    {
                        "sample_type": sample_name,
                        "organism": None,
                        "cell_counts_reported": [],
                    }
                )
                existing_keys.add(key)

    return merged


def is_final_catalogue_sample_name(name):
    """Metadata-only sample filter applied after SCP classification."""
    if not isinstance(name, str):
        return False

    value = canonicalize_sample_name(name)
    value = re.sub(r"\s+", " ", value.strip())
    if not value:
        return False

    if FINAL_METADATA_REFERENCE_RE.search(value):
        return False
    if FINAL_METADATA_PROCEDURAL_SAMPLE_RE.fullmatch(value):
        return False
    if (
        FINAL_METADATA_CONDITION_RE.search(value)
        and BIOLOGICAL_CELL_UNIT_RE.search(value) is None
    ):
        return False
    if len(value) > 80 or len(value.split()) > 9:
        return False
    return True


def finalize_catalogue_samples(samples):
    """Final metadata-only sample cleanup/de-duplication."""
    cleaned = {}
    warnings = []

    for sample in samples:
        if not isinstance(sample, dict):
            continue

        name = canonicalize_sample_name(sample.get("sample_type"))
        if not is_final_catalogue_sample_name(name):
            if name:
                warnings.append(
                    f"true_single_cell_sample removed by final metadata QC: {name!r}."
                )
            continue

        key = sample_name_key(name)
        if not key:
            continue

        organism = canonicalize_organism(sample.get("organism"))
        counts = [
            value for value in (sample.get("cell_counts_reported") or [])
            if isinstance(value, str) and value.strip()
        ]

        if key not in cleaned:
            cleaned[key] = {
                "sample_type": name,
                "organism": organism,
                "cell_counts_reported": [],
            }
        elif cleaned[key]["organism"] is None and organism is not None:
            cleaned[key]["organism"] = organism

        seen = {count_value_key(v) for v in cleaned[key]["cell_counts_reported"]}
        for value in counts:
            ckey = count_value_key(value)
            if ckey and ckey not in seen:
                cleaned[key]["cell_counts_reported"].append(value)
                seen.add(ckey)

    return list(cleaned.values()), warnings


def reconcile_isolation_with_final_samples(isolation_groups, true_samples):
    """Keep isolation only for surviving samples and physical cell actions."""
    valid_names = {
        sample_name_key(sample.get("sample_type"))
        for sample in true_samples if isinstance(sample, dict)
    }
    cleaned = []

    for group in isolation_groups or []:
        if not isinstance(group, dict):
            continue
        method = group.get("method")
        if (
            not isinstance(method, str)
            or NON_CELL_ISOLATION_METHOD_RE.search(method)
            or not CELL_ISOLATION_ACTION_RE.search(method)
        ):
            continue

        names = []
        for name in group.get("samples", []) or []:
            canonical = canonicalize_sample_name(name)
            if sample_name_key(canonical) in valid_names:
                names.append(canonical)
        names = list(dict.fromkeys(names))

        if names:
            cleaned.append({"samples": names, "method": method})

    return cleaned


# ---------------------------------------------------------------------------
# Explicit SCP evidence gate
# ---------------------------------------------------------------------------

SCP_FALSE_FRIEND_PATTERNS = (
    re.compile(r"\bsingle[- ]cell[- ]line(?:s)?\b", re.I),
    re.compile(r"\bsingle[- ]cell[- ]type(?:s)?\b", re.I),
    re.compile(r"\bsingle[- ]cell[- ]culture(?:s)?\b", re.I),
    re.compile(r"\bsingle[- ]cell[- ]clone(?:s)?\b", re.I),
    re.compile(r"\bsingle[- ]cell[- ]resolution\b", re.I),
    re.compile(r"\bsingle[- ]cell[- ]equivalent(?:s|\s+samples?)?\b", re.I),
    re.compile(r"\bsingle[- ]cell[- ]level\b", re.I),
    re.compile(r"\bcell[- ]type[- ]resolved\b", re.I),
    re.compile(
        r"\bsingle[- ]cell(?:[- ]sized)?[- ]"
        r"(?:contours?|regions?|areas?|pixels?|voxels?)\b",
        re.I,
    ),
    re.compile(
        r"\bsingle[- ]cell\s+(?:RNA[- ]?seq|RNA\s+sequencing|"
        r"transcriptom(?:e|es|ics|ic)|genom(?:e|es|ics|ic)|ATAC(?:-seq)?|"
        r"epigenom(?:e|es|ics|ic))\b",
        re.I,
    ),
)


SCP_DIRECT_SUPPORT_PATTERNS = (
    re.compile(r"\bsingle[- ]cell\s+proteom(?:e|es|ics|ic)\b", re.I),
    re.compile(
        r"\bsingle[- ]cell(?:ular)?\s+"
        r"(?:mass\s+spectrom(?:etry|etric)|LC[- ]?MS(?:/MS)?)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:proteins?|protein\s+groups?)\b.{0,120}"
        r"\b(?:per|from|in)\s+(?:an?\s+)?"
        r"(?:individual\s+)?single[- ]cell\b",
        re.I | re.S,
    ),
    re.compile(
        r"\b(?:individual\s+)?single[- ]cell\b.{0,140}"
        r"\b(?:proteins?|protein\s+groups?)\b",
        re.I | re.S,
    ),
)

SPATIAL_PSEUDOCELL_CONTEXT_RE = re.compile(
    r"\b(?:single[- ]cell(?:[- ]sized)?[- ](?:contours?|regions?|areas?|"
    r"pixels?|voxels?)|single[- ]cell[- ]resolution|"
    r"single[- ]cell[- ]equivalent|"
    r"(?:laser\s+capture|laser\s+microdissection|microdissected)[^.!?]{0,100}"
    r"(?:contours?|regions?|areas?|ROIs?))\b",
    re.I,
)

DIRECT_CELL_ISOLATION_RE = re.compile(
    r"\b(?:isolated|sorted|deposited|dispensed|picked|captured)\b[^.!?]{0,90}"
    r"\b(?:single[- ]cells?|individual\s+cells?|single\s+"
    r"(?:oocytes?|neurons?|blastomeres?|bacteri(?:um|a)|"
    r"sperm\s+cells?|egg\s+cells?))\b"
    r"|\b(?:single[- ]cells?|individual\s+cells?|single\s+"
    r"(?:oocytes?|neurons?|blastomeres?|bacteri(?:um|a)|"
    r"sperm\s+cells?|egg\s+cells?))\b[^.!?]{0,90}"
    r"\b(?:isolated|sorted|deposited|dispensed|picked|captured)\b",
    re.I,
)

PHYSICAL_SINGLE_CELL_HANDLING_RE = re.compile(
    r"\b(?:FACS|fluorescence[- ]activated\s+cell\s+sorting|"
    r"CellenONE|cell\s+sorting|one\s+cell\s+per\s+well|"
    r"single[- ]cell\s+sorting|single\s+cells?\s+(?:were\s+)?"
    r"(?:isolated|sorted|deposited|dispensed|picked|captured)|"
    r"(?:isolated|sorted|deposited|dispensed|picked|captured)\s+"
    r"(?:individual\s+)?single\s+cells?)\b",
    re.I,
)

SYNTHETIC_SCP_BENCHMARK_RE = re.compile(
    r"\b(?:"
    r"diluted\s+bulk(?:\s+(?:digest|proteome|peptides?))?|"
    r"bulk\s+(?:HeLa\s+)?(?:digest|proteome|peptides?)|"
    r"mixed[- ]species(?:\s+(?:proteome|protein|peptide))?\s+samples?|"
    r"(?:two|three)[- ]species(?:\s+(?:proteome|protein|peptide))?\s+samples?|"
    r"(?:100|150|200|250|300)\s*pg[^.!?]{0,90}"
    r"(?:per\s+(?:TMT(?:pro)?\s+)?(?:channel|label)|"
    r"single[- ]cell(?:\s+equivalent)?|cell[- ]equivalent)|"
    r"(?:refer(?:red)?\s+to|denot(?:ed|ing)|called)\s+as\s+"
    r"['\"“”]?single[- ]cell|"
    r"synthetic\s+single[- ]cell|"
    r"single[- ]cell[- ]equivalent\s+(?:digest|sample|input)"
    r")\b",
    re.I,
)

NON_MS_SINGLE_CELL_MODALITY_RE = re.compile(
    r"\b(?:CosMx(?:\s+Spatial\s+Molecular\s+Imager)?|"
    r"spatial\s+transcriptom(?:e|ics)|"
    r"single[- ]cell\s+RNA[- ]?seq|scRNA[- ]?seq|"
    r"single[- ]cell\s+transcriptom(?:e|ics)|"
    r"imaging[- ]based\s+single[- ]cell)\b",
    re.I,
)

BULK_MS_MATERIAL_RE = re.compile(
    r"\b(?:plasma|serum|bulk\s+(?:tissue|proteom|sample)|"
    r"tissue\s+homogenate|whole\s+tissue|pooled\s+samples?)\b",
    re.I,
)

SCP_MS_CONTEXT_RE = re.compile(
    r"\b(?:proteom(?:e|es|ics|ic)|mass\s+spectrom(?:etry|etric)|"
    r"LC[- ]?MS(?:/MS)?|MS/MS|Orbitrap|timsTOF|Astral|"
    r"mass\s+spectrometer)\b",
    re.I,
)

SCP_STRONG_MEASUREMENT_PATTERNS = (
    re.compile(
        r"\b(?:proteins?|protein\s+groups?|proteom(?:e|es))\b[^.!?]{0,120}"
        r"\b(?:from|per|in)\s+(?:an?\s+)?(?:individual\s+)?"
        r"single[- ](?:cell|oocyte|neuron|blastomere|bacterium)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:single[- ]cells?|individual\s+(?:single\s+)?cells?|"
        r"single\s+(?:oocytes?|neurons?|blastomeres?|bacteri(?:um|a)|"
        r"sperm\s+cells?|egg\s+cells?))\b[^.!?]{0,140}"
        r"\b(?:analy[sz](?:ed|ing)|measur(?:ed|ing)|profiled|quantif(?:ied|ication)|"
        r"subjected\s+to|injected|loaded|acquired)\b[^.!?]{0,120}"
        r"\b(?:proteom(?:e|es|ics|ic)|mass\s+spectrom(?:etry|etric)|"
        r"LC[- ]?MS(?:/MS)?|MS/MS)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:proteom(?:e|es|ics|ic)|mass\s+spectrom(?:etry|etric)|"
        r"LC[- ]?MS(?:/MS)?|MS/MS)\b[^.!?]{0,120}"
        r"\b(?:analy[sz](?:ed|ing)|measur(?:ed|ing)|profiled|quantif(?:ied|ication)|"
        r"subjected\s+to|acquired)\b[^.!?]{0,140}"
        r"\b(?:single[- ]cells?|individual\s+(?:single\s+)?cells?|"
        r"single\s+(?:oocytes?|neurons?|blastomeres?|bacteri(?:um|a)|"
        r"sperm\s+cells?|egg\s+cells?))\b",
        re.I,
    ),
    re.compile(
        r"\b(?:single[- ]cell|single\s+cell)\s+"
        r"(?:quantitative\s+)?proteomic\s+(?:analysis|profiling|measurement)\b",
        re.I,
    ),
)


TITLE_FALSE_FRIEND_RE = re.compile(
    r"\b(?:single[- ]cell[- ]resolution|single[- ]cell[- ]equivalent|"
    r"single[- ]cell[- ]level|cell[- ]type[- ]resolved|"
    r"single[- ]cell\s+(?:RNA[- ]?seq|RNA\s+sequencing|transcriptom|genom|ATAC)|"
    r"single[- ]cell(?:[- ]sized)?[- ](?:contours?|regions?|areas?|pixels?|voxels?))\b",
    re.I,
)


TITLE_METHOD_ONLY_RE = re.compile(
    r"\b(?:toward(?:s)?|for|enable(?:s|d|ing)?|workflow\s+for|"
    r"platform\s+for|separations?\s+for)\b.{0,90}"
    r"\bsingle[- ]cell\s+proteom",
    re.I,
)

TITLE_DIRECT_BIOLOGICAL_UNIT_RE = re.compile(
    r"\bsingle\s+(?:"
    r"bacterium|bacteria|oocytes?|neurons?|blastomeres?|"
    r"sperm(?:\s+cells?)?|egg\s+cells?"
    r")\b[^.!?]{0,140}"
    r"\b(?:proteom(?:e|es|ics|ic)|mass\s+spectrom(?:etry|etric)|"
    r"LC[- ]?MS(?:/MS)?|MS/MS)\b"
    r"|\b(?:proteom(?:e|es|ics|ic)|mass\s+spectrom(?:etry|etric)|"
    r"LC[- ]?MS(?:/MS)?|MS/MS)\b[^.!?]{0,140}"
    r"\bsingle\s+(?:"
    r"bacterium|bacteria|oocytes?|neurons?|blastomeres?|"
    r"sperm(?:\s+cells?)?|egg\s+cells?"
    r")\b",
    re.I,
)

TITLE_EXPLICIT_MS_RE = re.compile(
    r"\b(?:mass\s+spectrom(?:etry|etric)|LC[- ]?MS(?:/MS)?|MS/MS)\b",
    re.I,
)

TITLE_HIGH_SCP_RE = re.compile(
    r"\bsingle[- ]cell\s+(?:quantitative\s+)?proteom(?:e|es|ics|ic)\b"
    r"|\bsingle[- ]cell\s+proteom(?:e|es|ics|ic)\s+(?:profiling|analysis)\b"
    r"|\b(?:proteins?|protein\s+groups?)\b.{0,70}\bper\s+single\s+cell\b"
    r"|\bproteom(?:e|es|ics|ic)\b.{0,90}\bper\s+single\s+cell\b"
    r"|\b(?:spatial\s+)?single[- ]cell(?:ular)?\s+"
    r"mass\s+spectrom(?:etry|etric)\b"
    r"|\bsingle\s+cell(?:s)?\b.{0,100}"
    r"\bmass\s+spectrom(?:etry|etric)\b"
    r"|\bmass\s+spectrom(?:etry|etric)\b.{0,100}"
    r"\bsingle\s+cell(?:s)?\b"
    r"|\b(?:peptides?|proteins?|protein\s+groups?)\b.{0,100}"
    r"\bsingle\s+cells?\b.{0,100}\b(?:MS/MS|mass\s+spectrom(?:etry|etric))\b"
    r"|\bsingle\s+cells?\b.{0,100}\b(?:MS/MS|mass\s+spectrom(?:etry|etric))\b"
    r".{0,100}\b(?:peptides?|proteins?|protein\s+groups?)\b"
    r"|\bsingle\s+(?:oocytes?|neurons?|blastomeres?|bacteri(?:um|a)|"
    r"sperm\s+cells?|egg\s+cells?)\b.{0,120}"
    r"\b(?:proteom(?:e|es|ics|ic)|mass\s+spectrom(?:etry|etric))\b"
    r"|\b(?:proteom(?:e|es|ics|ic)|mass\s+spectrom(?:etry|etric))\b.{0,120}"
    r"\bsingle\s+(?:oocytes?|neurons?|blastomeres?|bacteri(?:um|a)|"
    r"sperm\s+cells?|egg\s+cells?)\b",
    re.I | re.S,
)



def _mask_scp_false_friends(text):
    masked = text
    for pattern in SCP_FALSE_FRIEND_PATTERNS:
        masked = pattern.sub(
            lambda m: " " * (m.end() - m.start()),
            masked,
        )
    return masked


def _first_match_window(text, pattern, radius=260):
    if not isinstance(text, str):
        return None

    match = pattern.search(text)
    if not match:
        return None

    left = max(0, match.start() - radius)
    right = min(len(text), match.end() + radius)

    return re.sub(r"\s+", " ", text[left:right]).strip()


def _synthetic_benchmark_only_support(evidence_text):
    """
    Detect trace-input/synthetic benchmark experiments described with
    single-cell terminology but lacking physical individual-cell handling.
    """
    if not isinstance(evidence_text, str) or not evidence_text.strip():
        return None

    normalized = normalize_publication_text(evidence_text) or evidence_text

    if not SYNTHETIC_SCP_BENCHMARK_RE.search(normalized):
        return None

    if (
        DIRECT_CELL_ISOLATION_RE.search(normalized)
        or PHYSICAL_SINGLE_CELL_HANDLING_RE.search(normalized)
    ):
        return None

    return _first_match_window(
        normalized,
        SYNTHETIC_SCP_BENCHMARK_RE,
        radius=320,
    )


def _non_ms_single_cell_modality_conflict(evidence_text):
    """
    Detect a single-cell imaging/transcriptomic component paired with
    bulk/plasma/tissue proteomics/MS evidence.
    """
    if not isinstance(evidence_text, str) or not evidence_text.strip():
        return None

    normalized = normalize_publication_text(evidence_text) or evidence_text

    if not NON_MS_SINGLE_CELL_MODALITY_RE.search(normalized):
        return None

    if not BULK_MS_MATERIAL_RE.search(normalized):
        return None

    if (
        DIRECT_CELL_ISOLATION_RE.search(normalized)
        or PHYSICAL_SINGLE_CELL_HANDLING_RE.search(normalized)
    ):
        return None

    return _first_match_window(
        normalized,
        NON_MS_SINGLE_CELL_MODALITY_RE,
        radius=420,
    )


def _title_direct_override_kind(publication_title):
    """
    Strong title evidence that can override model=no.

    Generic "single-cell proteomics" wording deliberately does not qualify.
    """
    title = normalize_publication_text(publication_title)

    if not title:
        return None

    if TITLE_DIRECT_BIOLOGICAL_UNIT_RE.search(title):
        return "direct_biological_unit"

    if TITLE_EXPLICIT_MS_RE.search(title):
        return "explicit_ms"

    return None


def _strong_measurement_support(evidence_text):
    if not isinstance(evidence_text, str) or not evidence_text.strip():
        return None

    masked = _mask_scp_false_friends(evidence_text)

    for pattern in SCP_STRONG_MEASUREMENT_PATTERNS:
        for match in pattern.finditer(masked):
            left = max(0, match.start() - 220)
            right = min(len(masked), match.end() + 260)
            window = re.sub(r"\s+", " ", masked[left:right]).strip()

            # Spatial tissue contours/ROIs can be described as "single-cell"
            # without representing an isolated individual biological cell.
            # Such wording cannot establish core SCP unless the same local
            # evidence explicitly describes isolation/sorting/capture of cells.
            if (
                SPATIAL_PSEUDOCELL_CONTEXT_RE.search(window)
                and not DIRECT_CELL_ISOLATION_RE.search(window)
            ):
                continue

            if (
                SYNTHETIC_SCP_BENCHMARK_RE.search(window)
                and not (
                    DIRECT_CELL_ISOLATION_RE.search(window)
                    or PHYSICAL_SINGLE_CELL_HANDLING_RE.search(window)
                )
            ):
                continue

            return window

    return None


def _publication_title_scp_tier(publication_title):
    title = normalize_publication_text(publication_title)

    if not title:
        return "none"

    if not is_plausible_publication_title(title):
        return "none"

    if TITLE_FALSE_FRIEND_RE.search(title):
        return "false_friend"

    if TITLE_METHOD_ONLY_RE.search(title):
        return "method_only"

    if TITLE_HIGH_SCP_RE.search(title):
        return "high"

    return "none"


def _target_accession_local_scp_support(evidence_text, target_accession):
    if (
        not isinstance(evidence_text, str)
        or not evidence_text.strip()
        or not target_accession
    ):
        return None

    target_pattern = re.compile(rf"\b{re.escape(target_accession)}\b", re.I)
    masked = _mask_scp_false_friends(evidence_text)

    for match in target_pattern.finditer(masked):
        left = max(0, match.start() - 300)
        right = min(len(masked), match.end() + 300)
        window = masked[left:right]

        local_pxds = {value.upper() for value in PXD_RE.findall(window)}
        if local_pxds != {target_accession.upper()}:
            continue

        strong = _strong_measurement_support(window)
        if strong:
            return strong

        for pattern in SCP_DIRECT_SUPPORT_PATTERNS:
            direct = pattern.search(window)
            if direct and SCP_MS_CONTEXT_RE.search(window):
                return re.sub(r"\s+", " ", window).strip()

    return None


def explicit_scp_evidence_gate(
    evidence_text,
    *,
    target_accession=None,
    target_dataset_label=None,
    publication_pxds=None,
    publication_title=None,
    model_classification=None,
    true_samples=None,
):
    """
    v17 high-precision accession-level SCP gate.

    Core-catalogue policy:
      * explicit target-PXD single-cell labels remain decisive;
      * multi-PXD papers require target-local accession evidence;
      * synthetic/trace-input single-cell benchmarks do not count without
        independent physical individual-cell handling;
      * non-MS single-cell modalities do not make a bulk PRIDE MS accession
        an SCP dataset;
      * generic SCP title wording cannot by itself override model=no;
      * model=no title overrides require either a direct biological unit or
        explicit mass-spectrometry wording;
      * explicit source-grounded individual-cell MS measurements remain
        decisive regardless of model classification.
    """
    publication_pxds = [
        str(value).upper()
        for value in (publication_pxds or [])
        if value
    ]

    true_samples = [
        item
        for item in (true_samples or [])
        if isinstance(item, dict)
        and looks_like_biological_sample_name(item.get("sample_type", ""))
    ]

    label = (
        target_dataset_label.strip()
        if isinstance(target_dataset_label, str)
        else ""
    )

    if re.search(r"\bsingle[- ]cell\b", label, re.I):
        return {
            "supported": True,
            "reason": "explicit target-accession single-cell dataset label",
            "evidence": target_dataset_label,
            "support_scope": "target_accession",
            "evidence_tier": "explicit_accession_label",
        }

    label_scope = infer_scope_kind(label) if label else None

    if label_scope is not None and label_scope != "single_cell":
        return {
            "supported": False,
            "reason": (
                "target accession has an explicit non-single-cell "
                f"dataset label ({label_scope})"
            ),
            "evidence": label,
            "support_scope": "target_accession",
            "evidence_tier": "explicit_non_scp_label",
        }

    if not isinstance(evidence_text, str) or not evidence_text.strip():
        return {
            "supported": False,
            "reason": "no retained single-cell evidence text",
            "evidence": "",
            "support_scope": "none",
            "evidence_tier": "none",
        }

    benchmark_only = _synthetic_benchmark_only_support(evidence_text)

    if benchmark_only:
        return {
            "supported": False,
            "reason": (
                "single-cell terminology is associated with a synthetic/"
                "trace-input benchmark and no physical individual-cell "
                "isolation/handling evidence was found"
            ),
            "evidence": benchmark_only,
            "support_scope": "publication",
            "evidence_tier": "synthetic_benchmark_only",
        }

    modality_conflict = _non_ms_single_cell_modality_conflict(evidence_text)

    if modality_conflict:
        return {
            "supported": False,
            "reason": (
                "the single-cell component is a non-MS imaging/transcriptomic "
                "modality while the retained proteomics/MS evidence describes "
                "bulk/plasma/tissue material"
            ),
            "evidence": modality_conflict,
            "support_scope": "publication",
            "evidence_tier": "non_ms_single_cell_modality",
        }

    local_support = _target_accession_local_scp_support(
        evidence_text,
        target_accession,
    )

    if local_support:
        return {
            "supported": True,
            "reason": (
                "target accession has unambiguous local individual-cell "
                "proteomics/MS evidence"
            ),
            "evidence": local_support,
            "support_scope": "target_accession",
            "evidence_tier": "target_local_measurement",
        }

    if len(set(publication_pxds)) > 1:
        return {
            "supported": False,
            "reason": (
                "publication contains multiple PXD accessions and no "
                "target-accession-specific SCP mapping/evidence was found"
            ),
            "evidence": "",
            "support_scope": "publication_only",
            "evidence_tier": "publication_only",
        }

    strong_measurement = _strong_measurement_support(evidence_text)

    if strong_measurement:
        return {
            "supported": True,
            "reason": (
                "explicit source evidence shows proteomics/MS measurement "
                "of individual biological cells"
            ),
            "evidence": strong_measurement,
            "support_scope": "publication",
            "evidence_tier": "individual_cell_measurement",
        }

    title_tier = _publication_title_scp_tier(publication_title)
    title_override_kind = _title_direct_override_kind(publication_title)

    if title_tier == "high":
        if model_classification == "yes":
            return {
                "supported": True,
                "reason": (
                    "publication title explicitly defines an SCP/MS study "
                    "and the semantic SCP classifier agrees"
                ),
                "evidence": normalize_publication_text(publication_title) or "",
                "support_scope": "publication_title",
                "evidence_tier": "high_title_plus_model",
            }

        if title_override_kind == "direct_biological_unit":
            return {
                "supported": True,
                "reason": (
                    "publication title explicitly identifies proteomics/MS "
                    "of a direct individual biological unit"
                ),
                "evidence": normalize_publication_text(publication_title) or "",
                "support_scope": "publication_title",
                "evidence_tier": "high_title_direct_biological_unit",
            }

        if title_override_kind == "explicit_ms":
            return {
                "supported": True,
                "reason": (
                    "publication title explicitly connects single cells with "
                    "mass spectrometry"
                ),
                "evidence": normalize_publication_text(publication_title) or "",
                "support_scope": "publication_title",
                "evidence_tier": "high_title_explicit_ms",
            }

    if title_tier == "method_only" and model_classification == "yes":
        return {
            "supported": True,
            "reason": (
                "single-cell proteomics method-development title is "
                "corroborated by the semantic SCP classifier"
            ),
            "evidence": normalize_publication_text(publication_title) or "",
            "support_scope": "publication_title",
            "evidence_tier": "method_title_plus_model",
        }

    masked = _mask_scp_false_friends(evidence_text)
    direct_match = None

    for pattern in SCP_DIRECT_SUPPORT_PATTERNS:
        direct_match = pattern.search(masked)
        if direct_match:
            break

    if (
        model_classification == "yes"
        and true_samples
        and direct_match is not None
    ):
        left = max(0, direct_match.start() - 160)
        right = min(len(masked), direct_match.end() + 200)

        return {
            "supported": True,
            "reason": (
                "model classification is corroborated by a grounded "
                "individual-cell sample and direct SCP source text"
            ),
            "evidence": re.sub(r"\s+", " ", masked[left:right]).strip(),
            "support_scope": "publication",
            "evidence_tier": "model_plus_grounded_sample",
        }

    if title_tier == "high" and model_classification != "yes":
        reason = (
            "publication title contains strong SCP wording, but the semantic "
            "classifier did not confirm SCP and the title lacks a direct "
            "biological-unit or explicit-MS override"
        )
    elif title_tier in {"false_friend", "method_only"}:
        reason = (
            "publication title contains single-cell-adjacent wording but does "
            "not by itself establish individual-cell MS/proteomics"
        )
    elif direct_match is not None:
        reason = (
            "only publication-level SCP wording was found; no individual-cell "
            "measurement evidence or grounded SCP sample supported the dataset"
        )
    else:
        reason = (
            "no explicit target-supported evidence that proteomic/MS "
            "measurements were performed on individual biological cells"
        )

    return {
        "supported": False,
        "reason": reason,
        "evidence": "",
        "support_scope": "none",
        "evidence_tier": title_tier,
    }


# ---------------------------------------------------------------------------
# Catalogue note
# ---------------------------------------------------------------------------

def build_catalogue_note(
    is_single_cell_proteomics,
    true_samples,
    low_input_benchmarks,
):
    if is_single_cell_proteomics == "yes":
        if true_samples and low_input_benchmarks:
            return (
                "Publication contains genuine single-cell proteomics "
                "experiments alongside low-input bulk benchmark experiments."
            )

        if true_samples:
            return (
                "Publication contains genuine single-cell proteomics "
                "experiments."
            )

        return (
            "Publication is classified as single-cell proteomics, but the "
            "selected evidence did not yield a named true single-cell sample."
        )

    if is_single_cell_proteomics == "no":
        if low_input_benchmarks:
            return (
                "Publication contains low-input proteomics benchmarks but no "
                "genuine single-cell proteomics experiment was identified."
            )

        return (
            "No genuine single-cell proteomics experiment was identified in "
            "the selected publication evidence."
        )

    return (
        "The selected publication evidence was insufficient to classify the "
        "study confidently as single-cell proteomics."
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Hybrid local annotation pipeline for PRIDE single-cell "
            "proteomics publications."
        )
    )

    parser.add_argument(
        "--pdf",
        default=DEFAULT_PDF_PATH,
        help=f"Publication PDF (default: {DEFAULT_PDF_PATH})",
    )

    parser.add_argument(
        "--source-text",
        default=None,
        help=(
            "Normalized publication full text (for example Europe-PMC JATS "
            "text) used when a direct PDF is unavailable. When supplied, "
            "this takes precedence over --pdf."
        ),
    )

    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Ollama model (default: {DEFAULT_MODEL})",
    )

    parser.add_argument(
        "--target-accession",
        default=None,
        help=(
            "PRIDE accession being curated. If omitted, infer the last PXD "
            "accession present in the PDF path."
        ),
    )

    parser.add_argument(
        "--publication-title",
        default=None,
        help=(
            "Authoritative publication title from the PRIDE/Stage-1 manifest. "
            "When supplied, this is preferred over PDF title inference."
        ),
    )

    parser.add_argument(
        "--publication-doi",
        default=None,
        help=(
            "Authoritative publication DOI from the PRIDE/Stage-1 manifest. "
            "When supplied, this is preferred in the output metadata."
        ),
    )

    parser.add_argument(
        "--ollama-url",
        default=DEFAULT_OLLAMA_URL,
    )

    parser.add_argument(
        "--num-ctx",
        type=int,
        default=DEFAULT_NUM_CTX,
        help=f"Ollama context length (default: {DEFAULT_NUM_CTX}).",
    )

    parser.add_argument(
        "--cpu-threads",
        type=int,
        default=None,
        help=(
            "Limit Ollama to this many CPU threads. This is a thread limit, "
            "not an exact CPU-percentage cap. Lower values usually reduce "
            "heat/fan noise and increase runtime somewhat."
        ),
    )

    parser.add_argument(
        "--task-evidence-chars",
        type=int,
        default=DEFAULT_TASK_EVIDENCE_CHARS,
        help=(
            "Maximum evidence characters supplied to each semantic LLM task "
            f"(default: {DEFAULT_TASK_EVIDENCE_CHARS})."
        ),
    )

    parser.add_argument(
        "--samples-num-predict",
        type=int,
        default=DEFAULT_SAMPLES_NUM_PREDICT,
        help=(
            "Initial generation ceiling for sample classification "
            f"(default: {DEFAULT_SAMPLES_NUM_PREDICT})."
        ),
    )

    parser.add_argument(
        "--preparation-num-predict",
        type=int,
        default=DEFAULT_PREPARATION_NUM_PREDICT,
        help=(
            "Initial generation ceiling for sample-preparation extraction "
            f"(default: {DEFAULT_PREPARATION_NUM_PREDICT})."
        ),
    )

    parser.add_argument(
        "--lc-num-predict",
        type=int,
        default=DEFAULT_LC_NUM_PREDICT,
        help=(
            "Initial generation ceiling for LC extraction "
            f"(default: {DEFAULT_LC_NUM_PREDICT})."
        ),
    )

    parser.add_argument(
        "--single-cell-performance-num-predict",
        type=int,
        default=DEFAULT_SINGLE_CELL_PERFORMANCE_NUM_PREDICT,
        help=(
            "Initial generation ceiling for genuine single-cell performance "
            f"(default: {DEFAULT_SINGLE_CELL_PERFORMANCE_NUM_PREDICT})."
        ),
    )

    parser.add_argument(
        "--low-input-performance-num-predict",
        type=int,
        default=DEFAULT_LOW_INPUT_PERFORMANCE_NUM_PREDICT,
        help=(
            "Initial generation ceiling for low-input performance "
            f"(default: {DEFAULT_LOW_INPUT_PERFORMANCE_NUM_PREDICT})."
        ),
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Ollama request timeout in seconds (default: 1800).",
    )

    parser.add_argument(
        "--no-auto-retry",
        action="store_true",
        help=(
            "Disable the automatic one-time retry when structured output is "
            "truncated at num_predict."
        ),
    )

    parser.add_argument(
        "--save-evidence",
        action="store_true",
        help=(
            "Save the evidence supplied to each semantic extraction task."
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run PDF parsing, retrieval and deterministic extraction only; "
            "do not call Ollama."
        ),
    )

    parser.add_argument(
        "--publication-wide",
        action="store_true",
        help=(
            "Disable accession-specific scoping in publications that map "
            "multiple PXD accessions to separate experiments. Useful for "
            "diagnostics; accession-specific scoping is the default."
        ),
    )

    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON path. Default: <pdf>.pride_annotation.json",
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    if args.cpu_threads is not None and args.cpu_threads < 1:
        raise ValueError("--cpu-threads must be >= 1")

    if args.task_evidence_chars < 1_000:
        raise ValueError(
            "--task-evidence-chars should be at least 1000"
        )

    source_kind = "text" if args.source_text else "pdf"
    source_path = Path(args.source_text) if args.source_text else Path(args.pdf)

    if not source_path.exists():
        label = "publication text" if source_kind == "text" else "PDF"
        raise FileNotFoundError(f"{label} not found: {source_path}")

    target_accession = (
        args.target_accession.upper()
        if args.target_accession
        else infer_target_accession(str(source_path))
    )

    out_path = (
        Path(args.output)
        if args.output
        else source_path.with_suffix(".pride_annotation.json")
    )

    work_dir = out_path.parent / (
        out_path.stem + ".work"
    )
    work_dir.mkdir(parents=True, exist_ok=True)

    print(f"Publication source ({source_kind}): {source_path}")
    print(f"Target accession: {target_accession or 'not inferred'}")
    print(f"Extracting {source_kind} blocks...")

    blocks = (
        extract_text_blocks(str(source_path))
        if source_kind == "text"
        else extract_blocks(str(source_path))
    )

    full_text_chars = sum(
        len(item["text"])
        for item in blocks
    )

    print(
        f"Extracted {len(blocks):,} blocks / "
        f"{full_text_chars:,} characters"
    )

    manifest_publication_title = normalize_publication_text(
        args.publication_title
    )

    if is_plausible_publication_title(manifest_publication_title):
        publication_title_candidate = manifest_publication_title
        publication_title_source = "manifest"
    elif source_kind == "text":
        publication_title_candidate, publication_title_source = (
            infer_text_publication_title(blocks)
        )
    else:
        publication_title_candidate, publication_title_source = (
            infer_publication_title(str(source_path), blocks)
        )

    print(
        "Publication title candidate:",
        publication_title_candidate or "not inferred",
    )
    if publication_title_source:
        print("Publication title source:", publication_title_source)

    sections = extract_sections(blocks)

    if not sections["abstract"]:
        inferred_abstract = infer_lead_abstract(blocks)

        if inferred_abstract is not None:
            sections["abstract"] = [inferred_abstract]

            print(
                "Abstract heading not found; inferred a page-1 lead "
                f"paragraph ({len(inferred_abstract['text']):,} chars)."
            )
        else:
            print(
                "Abstract heading not found and no page-1 lead paragraph "
                "passed the fallback heuristic."
            )

    print("\nDetected target sections:")

    for name in sorted(TARGET_SECTIONS):
        chars = sum(
            len(item["text"])
            for item in sections[name]
        )

        print(
            f"  {name:18s}: "
            f"{len(sections[name]):4d} blocks / "
            f"{chars:8,d} chars"
        )

    publication_pxds, all_dois, detected_primary_doi = find_identifiers(
        blocks
    )

    manifest_publication_doi = (
        args.publication_doi.strip()
        if isinstance(args.publication_doi, str)
        and args.publication_doi.strip()
        else None
    )
    primary_doi = manifest_publication_doi or detected_primary_doi
    publication_doi_source = (
        "manifest" if manifest_publication_doi else "pdf"
    )

    print("\nPXD accessions mentioned in PDF:", publication_pxds)
    print("Primary DOI:", primary_doi)
    print("Primary DOI source:", publication_doi_source)
    print("PDF-detected DOI candidate:", detected_primary_doi)
    print("All DOIs found:", all_dois[:10])

    accession_labels = parse_data_availability_accession_labels(
        sections
    )
    target_dataset_label = (
        accession_labels.get(target_accession)
        if target_accession
        else None
    )

    explicit_dataset_scp_hint = None
    scoped_sections = sections
    scope_kind = None
    scope_block_ids = set()

    if (
        not args.publication_wide
        and target_dataset_label
        and len(accession_labels) > 1
    ):
        explicit_dataset_scp_hint = classify_explicit_dataset_label(
            target_dataset_label
        )

        scoped_sections, scope_kind, scope_block_ids = (
            build_accession_scope_sections(
                sections,
                target_dataset_label,
            )
        )

        print(
            f"Target dataset label: {target_dataset_label!r} "
            "(from Data Availability)"
        )
        print(
            f"Accession-specific scope: {scope_kind or 'label-only'} "
            f"({len(scope_block_ids)} source blocks)"
        )
    elif target_dataset_label:
        print(
            f"Target dataset label: {target_dataset_label!r} "
            "(from Data Availability)"
        )

    separate_low_input_accession = (
        scope_kind == "single_cell"
        and has_separate_low_input_accession(
            accession_labels,
            target_accession,
        )
    )

    # ---------------------------------------------------------------------
    # Deterministic terminology extraction
    # ---------------------------------------------------------------------

    metadata_blocks = build_metadata_scope_blocks(
        blocks,
        scoped_sections,
        scope_kind,
    )

    deterministic = deterministic_metadata(metadata_blocks)
    deterministic = filter_accession_scoped_deterministic(
        deterministic,
        metadata_blocks,
        scope_kind,
    )

    print("\nDeterministic metadata:")
    print(
        "  mass spectrometers:",
        deterministic["mass_spectrometers"],
    )
    print(
        "  acquisition modes:",
        deterministic["acquisition_modes"],
    )
    print(
        "  ion mobility / FAIMS:",
        deterministic["ion_mobility_or_faims"],
    )
    print(
        "  analysis software:",
        deterministic["analysis_software"],
    )
    print(
        "  labeling strategy:",
        deterministic["labeling_strategy"],
    )
    print(
        "  analysis strategies:",
        deterministic["analysis_strategies"],
    )

    # ---------------------------------------------------------------------
    # Build small task-specific evidence payloads
    # ---------------------------------------------------------------------

    samples_group = select_group_blocks(
        scoped_sections,
        "single_cell_samples",
        max_chars=args.task_evidence_chars,
        include_abstract=True,
        max_block_chars=850,
    )

    low_input_group = select_group_blocks(
        scoped_sections,
        "low_input_benchmarks",
        max_chars=args.task_evidence_chars,
        include_abstract=False,
        max_block_chars=800,
    )

    preparation_group = select_group_blocks(
        scoped_sections,
        "isolation_and_preparation",
        max_chars=args.task_evidence_chars,
        include_abstract=False,
        max_block_chars=1_450,
    )

    lc_group = select_group_blocks(
        scoped_sections,
        "lc",
        max_chars=args.task_evidence_chars,
        include_abstract=False,
        max_block_chars=750,
    )

    single_cell_performance_group = select_group_blocks(
        scoped_sections,
        "single_cell_performance_specific",
        max_chars=args.task_evidence_chars,
        include_abstract=True,
        max_block_chars=900,
    )

    low_input_performance_group = select_group_blocks(
        scoped_sections,
        "low_input_performance_specific",
        max_chars=args.task_evidence_chars,
        include_abstract=True,
        max_block_chars=900,
    )

    # Sample classification needs both genuine single-cell evidence and one
    # low-input contrast block. Give only this task a modest extra budget so
    # the contrast block is not displaced by three ~850-character SC snippets.
    sample_task_blocks = combine_task_blocks_quota(
        samples_group,
        low_input_group,
        max_chars=args.task_evidence_chars + 800,
        primary_count=3,
        secondary_count=1,
    )

    preparation_task_blocks = combine_task_blocks(
        preparation_group,
        max_chars=args.task_evidence_chars,
    )

    lc_task_blocks = combine_task_blocks(
        lc_group,
        max_chars=args.task_evidence_chars,
    )

    # These are now independently retrieved categories rather than mixtures
    # of the generic sample/performance pools.
    single_cell_performance_blocks = combine_task_blocks(
        single_cell_performance_group,
        max_chars=args.task_evidence_chars,
    )

    low_input_performance_blocks = combine_task_blocks(
        low_input_performance_group,
        max_chars=args.task_evidence_chars,
    )

    sample_evidence, sample_records = format_task_evidence(
        sample_task_blocks
    )
    preparation_evidence, preparation_records = format_task_evidence(
        preparation_task_blocks
    )
    lc_evidence, lc_records = format_task_evidence(
        lc_task_blocks
    )
    single_cell_performance_evidence, single_cell_performance_records = (
        format_task_evidence(single_cell_performance_blocks)
    )
    low_input_performance_evidence, low_input_performance_records = (
        format_task_evidence(low_input_performance_blocks)
    )

    print("\nSemantic task evidence:")
    print(
        f"  samples:                 {len(sample_records):2d} blocks / "
        f"{len(sample_evidence):5,d} chars"
    )
    print(
        f"  preparation:             {len(preparation_records):2d} blocks / "
        f"{len(preparation_evidence):5,d} chars"
    )
    print(
        f"  LC:                      {len(lc_records):2d} blocks / "
        f"{len(lc_evidence):5,d} chars"
    )
    print(
        f"  single-cell performance: {len(single_cell_performance_records):2d} blocks / "
        f"{len(single_cell_performance_evidence):5,d} chars"
    )
    print(
        f"  low-input performance:   {len(low_input_performance_records):2d} blocks / "
        f"{len(low_input_performance_evidence):5,d} chars"
    )
    evidence_bundle = {
        "target_accession": target_accession,
        "publication_source_kind": source_kind,
        "publication_source_path": str(source_path.resolve()),
        "target_dataset_label": target_dataset_label,
        "accession_scope_kind": scope_kind,
        "publication_doi": primary_doi,
        "publication_doi_source": publication_doi_source,
        "pdf_detected_primary_doi": detected_primary_doi,
        "publication_title_candidate": publication_title_candidate,
        "publication_title_source": publication_title_source,
        "publication_pride_accessions": publication_pxds,
        "publication_accession_labels": accession_labels,
        "tasks": {
            "samples": sample_records,
            "preparation": preparation_records,
            "lc": lc_records,
            "single_cell_performance": single_cell_performance_records,
            "low_input_performance": low_input_performance_records,
        },
    }

    evidence_json_path = work_dir / "evidence.json"

    if args.save_evidence or args.dry_run:
        evidence_json_path.write_text(
            json.dumps(
                evidence_bundle,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        (work_dir / "samples.evidence.txt").write_text(
            sample_evidence,
            encoding="utf-8",
        )

        (work_dir / "preparation.evidence.txt").write_text(
            preparation_evidence,
            encoding="utf-8",
        )

        (work_dir / "lc.evidence.txt").write_text(
            lc_evidence,
            encoding="utf-8",
        )

        (work_dir / "single_cell_performance.evidence.txt").write_text(
            single_cell_performance_evidence,
            encoding="utf-8",
        )

        (work_dir / "low_input_performance.evidence.txt").write_text(
            low_input_performance_evidence,
            encoding="utf-8",
        )

        print(f"\nSaved task evidence under: {work_dir}")

    if args.dry_run:
        dry_result = {
            "target_accession": target_accession,
            "target_accession_mentioned_in_publication": (
                target_accession in publication_pxds
                if target_accession
                else None
            ),
            "publication_doi": primary_doi,
            "publication_doi_source": publication_doi_source,
            "pdf_detected_primary_doi": detected_primary_doi,
            "publication_title_candidate": publication_title_candidate,
            "publication_title_source": publication_title_source,
            "publication_pride_accessions": publication_pxds,
            "target_dataset_label": target_dataset_label,
            "accession_scope_kind": scope_kind,
            "publication_accession_labels": accession_labels,
            "deterministic_metadata": deterministic,
            "semantic_task_evidence_counts": {
                "samples": len(sample_records),
                "preparation": len(preparation_records),
                "lc": len(lc_records),
                "single_cell_performance": len(
                    single_cell_performance_records
                ),
                "low_input_performance": len(
                    low_input_performance_records
                ),
            },
        }

        dry_path = work_dir / "dry_run.json"
        dry_path.write_text(
            json.dumps(
                dry_result,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        print("Dry run complete; Ollama was not called.")
        return

    # ---------------------------------------------------------------------
    # Five compact semantic calls
    # ---------------------------------------------------------------------

    common_ollama = {
        "ollama_url": args.ollama_url,
        "model": args.model,
        "num_ctx": args.num_ctx,
        "cpu_threads": args.cpu_threads,
        "timeout": args.timeout,
        "auto_retry": not args.no_auto_retry,
        "raw_output_dir": work_dir,
    }

    if sample_evidence.strip():
        samples_result, samples_stats = run_samples_task(
            sample_evidence,
            target_accession=target_accession,
            ollama_args={
                **common_ollama,
                "num_predict": args.samples_num_predict,
            },
        )

        samples_result = validate_sample_result(
            samples_result,
            sample_evidence,
        )
    else:
        print(
            "\n[samples] skipped: no single-cell sample evidence "
            "was retrieved."
        )
        samples_result = {
            "is_single_cell_proteomics": "no",
            "true_single_cell_samples": [],
            "low_input_benchmarks": [],
        }
        samples_stats = make_empty_inference_stats(
            "no single-cell sample evidence"
        )

    preparation_supported = has_physical_preparation_evidence(
        preparation_evidence
    )

    if scope_kind == "single_cell":
        preparation_supported = (
            preparation_supported
            and has_single_cell_specific_preparation_records(
                preparation_records
            )
        )

    if preparation_supported:
        preparation_result, preparation_stats = run_preparation_task(
            preparation_evidence,
            ollama_args={
                **common_ollama,
                "num_predict": args.preparation_num_predict,
            },
        )

        preparation_result, preparation_warnings = (
            sanitize_preparation_fields(
                preparation_result,
                preparation_evidence,
            )
        )
    else:
        print(
            "\n[preparation] skipped: no explicit target-specific physical "
            "single-cell preparation/isolation evidence was retrieved."
        )
        preparation_result = {
            "sample_preparation": None,
            "single_cell_isolation": [],
        }
        preparation_stats = make_empty_inference_stats(
            "no physical preparation evidence"
        )
        preparation_warnings = []

    if lc_evidence.strip():
        lc_result, lc_stats = run_lc_task(
            lc_evidence,
            ollama_args={
                **common_ollama,
                "num_predict": args.lc_num_predict,
            },
        )

        lc_result, lc_warnings = sanitize_lc_fields(lc_result)

        lc_result, scoped_lc_warnings = (
            resolve_accession_scoped_lc_configuration(
                lc_result,
                lc_records,
                scope_kind,
            )
        )
        lc_warnings.extend(scoped_lc_warnings)
    else:
        print("\n[lc] skipped: no LC evidence was retrieved.")
        lc_result = {
            "lc_configuration": None,
            "lc_gradient": None,
        }
        lc_stats = make_empty_inference_stats("no LC evidence")
        lc_warnings = []

    if single_cell_performance_evidence.strip():
        single_cell_performance_result, single_cell_performance_stats = (
            run_single_cell_performance_task(
                single_cell_performance_evidence,
                ollama_args={
                    **common_ollama,
                    "num_predict": args.single_cell_performance_num_predict,
                },
            )
        )

        single_cell_performance_result = validate_performance_result(
            {
                "single_cell_throughput": (
                    single_cell_performance_result.get(
                        "single_cell_throughput"
                    )
                ),
                "single_cell_proteome_depth": (
                    single_cell_performance_result.get(
                        "single_cell_proteome_depth"
                    )
                ),
                "low_input_proteome_depth": None,
            },
            single_cell_performance_evidence,
        )

        single_cell_performance_result, sc_perf_warnings = (
            sanitize_single_cell_performance(
                single_cell_performance_result
            )
        )
    else:
        print(
            "\n[single_cell_performance] skipped: no single-cell "
            "performance evidence was retrieved."
        )
        single_cell_performance_result = {
            "single_cell_throughput": None,
            "single_cell_proteome_depth": None,
            "low_input_proteome_depth": None,
        }
        single_cell_performance_stats = make_empty_inference_stats(
            "no single-cell performance evidence"
        )
        sc_perf_warnings = []

    explicit_depth = deterministic_single_cell_depth(
        single_cell_performance_evidence
    )

    if explicit_depth is not None:
        semantic_depth = single_cell_performance_result.get(
            "single_cell_proteome_depth"
        )

        if semantic_depth != explicit_depth:
            sc_perf_warnings.append(
                "single_cell_proteome_depth replaced by an explicit "
                "source-text match to avoid combining unrelated numeric "
                "values from the same paragraph."
            )

        single_cell_performance_result[
            "single_cell_proteome_depth"
        ] = explicit_depth

    if separate_low_input_accession:
        print(
            "\n[low_input_performance] skipped: Data Availability maps "
            "low-input/dilution data to a separate PXD accession."
        )
        low_input_performance_result = {
            "low_input_proteome_depth": None,
        }
        low_input_performance_stats = make_empty_inference_stats(
            "separate low-input accession"
        )
        low_input_perf_warnings = []
    elif not low_input_performance_evidence.strip():
        print(
            "\n[low_input_performance] skipped: no low-input "
            "performance evidence was retrieved."
        )
        low_input_performance_result = {
            "low_input_proteome_depth": None,
        }
        low_input_performance_stats = make_empty_inference_stats(
            "no low-input performance evidence"
        )
        low_input_perf_warnings = []
    else:
        low_input_performance_result, low_input_performance_stats = (
            run_low_input_performance_task(
                low_input_performance_evidence,
                ollama_args={
                    **common_ollama,
                    "num_predict": args.low_input_performance_num_predict,
                },
            )
        )

        low_input_performance_result = validate_performance_result(
            {
                "single_cell_throughput": None,
                "single_cell_proteome_depth": None,
                "low_input_proteome_depth": (
                    low_input_performance_result.get(
                        "low_input_proteome_depth"
                    )
                ),
            },
            low_input_performance_evidence,
        )

        low_input_performance_result, low_input_perf_warnings = (
            sanitize_low_input_performance(
                low_input_performance_result
            )
        )

    validation_warnings = (
        preparation_warnings
        + lc_warnings
        + sc_perf_warnings
        + low_input_perf_warnings
    )

    if validation_warnings:
        print("\nValidation warnings:")
        for warning in validation_warnings:
            print(f"  - {warning}")
    # ---------------------------------------------------------------------
    # Merge
    # ---------------------------------------------------------------------

    model_scp = samples_result.get(
        "is_single_cell_proteomics",
        "uncertain",
    )

    scp_gate_text = "\n\n".join(
        value
        for value in (
            sample_evidence,
            preparation_evidence,
            single_cell_performance_evidence,
        )
        if isinstance(value, str) and value.strip()
    )

    scp_evidence_gate = explicit_scp_evidence_gate(
        scp_gate_text,
        target_accession=target_accession,
        target_dataset_label=target_dataset_label,
        publication_pxds=publication_pxds,
        publication_title=publication_title_candidate,
        model_classification=model_scp,
        true_samples=samples_result.get("true_single_cell_samples", []),
    )

    if explicit_dataset_scp_hint == "yes":
        is_scp = "yes"
    elif scp_evidence_gate["supported"]:
        is_scp = "yes"
    else:
        is_scp = "no"

    if model_scp != is_scp:
        validation_warnings.append(
            "Model SCP classification "
            f"{model_scp!r} was overridden by the explicit source-evidence "
            f"gate ({scp_evidence_gate['reason']})."
        )

    true_samples = samples_result.get(
        "true_single_cell_samples",
        [],
    )

    if is_scp == "yes":
        # Methods evidence can recover genuine single-cell sample types
        # omitted by the results/abstract-oriented sample extractor.
        true_samples = merge_isolation_samples_into_true_samples(
            true_samples,
            preparation_result.get("single_cell_isolation", []),
        )
    else:
        true_samples = []
        preparation_result["single_cell_isolation"] = []
        single_cell_performance_result[
            "single_cell_throughput"
        ] = None
        single_cell_performance_result[
            "single_cell_proteome_depth"
        ] = None

    true_samples = normalize_and_merge_true_samples(
        true_samples,
        blocks,
    )

    true_samples = enrich_respective_cell_counts(
        true_samples,
        blocks,
    )

    # v18 metadata-only QC. The SCP yes/no decision above is already final.
    true_samples, final_sample_warnings = finalize_catalogue_samples(
        true_samples
    )
    validation_warnings.extend(final_sample_warnings)

    preparation_result["single_cell_isolation"] = (
        reconcile_isolation_with_final_samples(
            preparation_result.get("single_cell_isolation", []),
            true_samples,
        )
    )

    low_input_benchmarks = normalize_low_input_benchmarks(
        samples_result.get(
            "low_input_benchmarks",
            [],
        )
    )

    if separate_low_input_accession:
        if low_input_benchmarks:
            validation_warnings.append(
                "Low-input benchmark records were removed because the "
                "publication maps dilution/low-input data to a separate "
                "PXD accession."
            )
        low_input_benchmarks = []

    catalogue_note = build_catalogue_note(
        is_scp,
        true_samples,
        low_input_benchmarks,
    )

    result = {
        "target_accession": target_accession,
        "publication_source_kind": source_kind,
        "publication_source_path": str(source_path.resolve()),
        "target_accession_mentioned_in_publication": (
            target_accession in publication_pxds
            if target_accession
            else None
        ),
        "publication_doi": primary_doi,
        "publication_doi_source": publication_doi_source,
        "pdf_detected_primary_doi": detected_primary_doi,
        "publication_title_candidate": publication_title_candidate,
        "publication_title_source": publication_title_source,
        "publication_pride_accessions": publication_pxds,
        "target_dataset_label": target_dataset_label,
        "accession_scope_kind": scope_kind,
        "publication_accession_labels": accession_labels,

        "annotation_pipeline_version": "v18",
        "classification_policy_version": "v17",
        "metadata_qc_version": "v18",
        "annotation_model": args.model,
        "cpu_threads": args.cpu_threads,

        "is_single_cell_proteomics": is_scp,
        "true_single_cell_samples": true_samples,
        "low_input_benchmarks": low_input_benchmarks,

        "sample_preparation": preparation_result.get(
            "sample_preparation"
        ),
        "single_cell_isolation": preparation_result.get(
            "single_cell_isolation"
        ),

        "labeling_strategy": deterministic[
            "labeling_strategy"
        ],

        "mass_spectrometers": deterministic[
            "mass_spectrometers"
        ],
        "acquisition_modes": deterministic[
            "acquisition_modes"
        ],
        "ion_mobility_or_faims": deterministic[
            "ion_mobility_or_faims"
        ],

        "lc_configuration": lc_result.get(
            "lc_configuration"
        ),
        "lc_gradient": lc_result.get(
            "lc_gradient"
        ),

        "single_cell_throughput": (
            single_cell_performance_result.get(
                "single_cell_throughput"
            )
        ),
        "single_cell_proteome_depth": (
            single_cell_performance_result.get(
                "single_cell_proteome_depth"
            )
        ),
        "low_input_proteome_depth": (
            low_input_performance_result.get(
                "low_input_proteome_depth"
            )
        ),

        "analysis_software": deterministic[
            "analysis_software"
        ],
        "analysis_strategies": deterministic[
            "analysis_strategies"
        ],

        "catalogue_note": catalogue_note,
        "validation_warnings": validation_warnings,

        "provenance": {
            "scp_evidence_gate": {
                "model_classification": model_scp,
                "final_classification": is_scp,
                "supported": scp_evidence_gate["supported"],
                "reason": scp_evidence_gate["reason"],
                "evidence": scp_evidence_gate["evidence"],
                "support_scope": scp_evidence_gate.get(
                    "support_scope"
                ),
                "evidence_tier": scp_evidence_gate.get(
                    "evidence_tier"
                ),
            },
            "accession_scope": {
                "enabled": (
                    scope_kind is not None
                    and not args.publication_wide
                ),
                "target_dataset_label": target_dataset_label,
                "scope_kind": scope_kind,
                "scope_block_count": len(scope_block_ids),
            },
            "deterministic": deterministic[
                "deterministic_provenance"
            ],
            "semantic_evidence_file": (
                str(evidence_json_path)
                if evidence_json_path.exists()
                else None
            ),
        },

        "inference_stats": {
            "samples": samples_stats,
            "preparation": preparation_stats,
            "lc": lc_stats,
            "single_cell_performance": single_cell_performance_stats,
            "low_input_performance": low_input_performance_stats,
            "total_wall_seconds": round(
                samples_stats["wall_seconds"]
                + preparation_stats["wall_seconds"]
                + lc_stats["wall_seconds"]
                + single_cell_performance_stats["wall_seconds"]
                + low_input_performance_stats["wall_seconds"],
                3,
            ),
            "total_output_tokens": (
                samples_stats["output_tokens"]
                + preparation_stats["output_tokens"]
                + lc_stats["output_tokens"]
                + single_cell_performance_stats["output_tokens"]
                + low_input_performance_stats["output_tokens"]
            ),
        },
    }

    out_path.write_text(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(f"\nSaved: {out_path}")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
