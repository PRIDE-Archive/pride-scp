#!/usr/bin/env python3
"""
Stage 3: fast, accession-aware screening of publication PDFs for genuine
single-cell proteomics (SCP) evidence.

Why this stage exists
---------------------
A publication can contain genuine SCP while depositing several PRIDE datasets,
only one of which may actually contain the single-cell experiment. Therefore
the screening unit is:

    publication PDF x target PXD accession

The screen is deliberately conservative/high-recall, but it distinguishes
publication-level SCP evidence from accession-local evidence.

Performance
-----------
* Rows are grouped by unique PDF path.
* Each PDF is opened/extracted only once per run.
* Extracted PDF text is persistently cached as gzip text.
* The default executor is ProcessPoolExecutor for parallel PDF extraction and
  regex/context scoring.
* Re-running the screening logic over an existing text cache avoids PDF
  extraction almost entirely.

No LLM is used.

Decision meanings
-----------------
candidate
    Strong evidence that the target PXD itself contains genuine individual-cell
    proteomics, or the publication contains strong SCP evidence and this is the
    only associated PXD in the manifest.

uncertain
    The publication contains plausible/strong SCP evidence, but the target PXD
    cannot be assigned confidently, especially in a multi-PXD publication.

reject
    No genuine individual-cell proteomics evidence, or the publication contains
    SCP but another PXD is explicitly mapped to it while the target PXD is not.

not_applicable
    No valid downloaded PDF.

screen_error
    PDF text extraction/processing failed.

Important exclusions
--------------------
The following are NOT sufficient SCP evidence by themselves:
* single-cell RNA-seq / transcriptomics / genomics / ATAC-seq
* single-nucleus omics
* single-cell resolution
* single-cell-derived clones/organoids
* cell-type-specific or cell-specific proteomics
* single cell line / cell type
* single-shot / single-sample
* near-single-cell / single-cell-equivalent
* low-input analyses of many cells
* one tissue structure such as a single tubule/islet/worm/fibre
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
from concurrent.futures import (
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
)
from pathlib import Path
from typing import Any

try:
    import pymupdf as fitz
except ImportError:  # compatibility with older PyMuPDF installations
    import fitz

from pride_scp_pipeline_common import (
    read_tsv,
    text_value,
    validate_pdf_path,
    write_tsv,
)


SCREEN_ALGORITHM_VERSION = "scp-screen-v5-accession-aware-quality-2026-08-22"
TEXT_CACHE_VERSION = 2


# ---------------------------------------------------------------------------
# Core vocabulary
# ---------------------------------------------------------------------------

PXD_RE = re.compile(r"\bPXD\d{6,}\b", re.I)

MS_CONTEXT_RE = re.compile(
    r"\b(?:"
    r"proteom(?:e|es|ics|ic)|"
    r"mass\s+spectrom(?:etry|etric)|"
    r"LC[- ]?MS(?:/MS)?|"
    r"nanoLC[- ]?MS|"
    r"MS/MS|"
    r"Orbitrap|"
    r"timsTOF|"
    r"Astral|"
    r"mass\s+spectrometer"
    r")\b",
    re.I,
)

# These phrases often created false positives in the PRIDE-wide crawl.
# They are masked before generic single-cell matching. They do not reject a
# whole paper; they simply do not count as positive SCP evidence.
FALSE_FRIEND_PATTERNS = [
    # Other single-cell omics
    (
        "single_cell_transcriptomics",
        re.compile(
            r"\bsingle[- ]cell(?:ular)?\s+"
            r"(?:RNA[- ]?seq(?:uencing)?|transcriptom(?:e|es|ics|ic)|"
            r"genom(?:e|es|ics|ic)|ATAC[- ]?seq|epigenom(?:e|es|ics|ic))\b",
            re.I,
        ),
    ),
    (
        "single_nucleus_omics",
        re.compile(
            r"\bsingle[- ]nucle(?:us|i)\b.{0,80}"
            r"\b(?:RNA|transcriptom|sequenc|omics?|ATAC)\b",
            re.I | re.S,
        ),
    ),
    # Resolution/derivation language
    (
        "single_cell_resolution",
        re.compile(
            r"\bsingle[- ]cell(?:ular)?\s+resolution\b",
            re.I,
        ),
    ),
    (
        "single_cell_derived",
        re.compile(
            r"\bsingle[- ]cell[- ]derived\b",
            re.I,
        ),
    ),
    (
        "single_cell_line",
        re.compile(
            r"\bsingle[- ]cell[- ]line(?:s)?\b",
            re.I,
        ),
    ),
    (
        "single_cell_type",
        re.compile(
            r"\bsingle[- ]cell[- ]type(?:s)?\b",
            re.I,
        ),
    ),
    (
        "single_cell_culture",
        re.compile(
            r"\bsingle[- ]cell[- ]culture(?:s)?\b",
            re.I,
        ),
    ),
    (
        "single_cell_clone",
        re.compile(
            r"\bsingle[- ]cell[- ]clone(?:s)?\b",
            re.I,
        ),
    ),
    # Population/cell-type specificity is not individual-cell proteomics.
    (
        "cell_type_specific",
        re.compile(
            r"\bcell[- ]type[- ]specific\b",
            re.I,
        ),
    ),
    (
        "cell_specific_proteomics",
        re.compile(
            r"\bcell[- ]specific\s+proteom(?:e|es|ics|ic)\b",
            re.I,
        ),
    ),
    # Acquisition/sample terminology
    (
        "single_shot",
        re.compile(
            r"\bsingle[- ]shot\b",
            re.I,
        ),
    ),
    (
        "single_sample",
        re.compile(
            r"\bsingle[- ]sample\b",
            re.I,
        ),
    ),
    (
        "single_cell_equivalent",
        re.compile(
            r"\bsingle[- ]cell[- ]equivalent(?:s)?\b",
            re.I,
        ),
    ),
    (
        "near_single_cell",
        re.compile(
            r"\bnear[- ]single[- ]cell\b",
            re.I,
        ),
    ),
    # A single multicellular biological structure is not an individual cell.
    (
        "single_multicellular_structure",
        re.compile(
            r"\bsingle\s+(?:"
            r"muscle\s+fib(?:er|re)|"
            r"tubule|islet|worm|organoid|follicle|crypt|glomerulus|"
            r"embryo|spheroid|colony"
            r")s?\b",
            re.I,
        ),
    ),
]


# Strong direct SCP statements.
STRONG_SCP_PATTERNS = [
    (
        "explicit_single_cell_proteomics",
        re.compile(
            r"\bsingle[- ]cell\s+proteom(?:e|es|ics|ic)\b",
            re.I,
        ),
        10,
    ),
    (
        "explicit_single_cell_mass_spectrometry",
        re.compile(
            r"\bsingle[- ]cell(?:ular)?\s+"
            r"(?:mass\s+spectrom(?:etry|etric)|LC[- ]?MS(?:/MS)?)\b",
            re.I,
        ),
        10,
    ),
    (
        "proteins_per_single_cell",
        re.compile(
            r"\b(?:\d[\d,]*|>\s*\d[\d,]*|~\s*\d[\d,]*)\s+"
            r"(?:proteins?|protein\s+groups?)\b.{0,120}"
            r"\b(?:per|from|in)\s+(?:an?\s+)?"
            r"(?:individual\s+)?single[- ]cell\b",
            re.I | re.S,
        ),
        10,
    ),
    (
        "single_cell_protein_count",
        re.compile(
            r"\b(?:individual\s+)?single[- ]cell\b.{0,140}"
            r"\b(?:\d[\d,]*|>\s*\d[\d,]*|~\s*\d[\d,]*)\s+"
            r"(?:proteins?|protein\s+groups?)\b",
            re.I | re.S,
        ),
        10,
    ),
    (
        "one_cell_per_well_or_sample",
        re.compile(
            r"\b(?:one|1)\s+(?:single\s+)?cell\s+per\s+"
            r"(?:well|reaction|sample|injection)\b",
            re.I,
        ),
        9,
    ),
]

# Publication-level evidence is intentionally tiered. A bare mention of
# "single-cell proteomics" somewhere in a long paper may be related work,
# background, or a cited method. It is not enough by itself to mark the
# publication as high-confidence SCP.
DIRECT_TERM_REASON_CLASSES = {
    "explicit_single_cell_proteomics",
    "explicit_single_cell_mass_spectrometry",
}

HIGH_PRECISION_PROCEDURAL_REASON_CLASSES = {
    "proteins_per_single_cell",
    "single_cell_protein_count",
    "one_cell_per_well_or_sample",
    "individual_cells_analyzed_by_ms_with_ms_context",
    "ms_analysis_of_individual_cells_with_ms_context",
    "single_cells_prepared_for_ms_with_ms_context",
    "single_named_cell_proteomics_with_ms_context",
}

TITLE_DIRECT_SCP_RE = re.compile(
    r"\b(?:"
    r"single[- ]cell.{0,80}proteom(?:e|es|ics|ic)|"
    r"single[- ]cell.{0,80}mass\s+spectrom(?:etry|etric)|"
    r"proteom(?:e|es|ics|ic).{0,80}single[- ]cell"
    r")\b",
    re.I | re.S,
)

SEVERE_MUPDF_WARNING_RE = re.compile(
    r"(?:"
    r"cannot find page|"
    r"zlib error|"
    r"invalid key in dict|"
    r"cannot load object|"
    r"object out of range|"
    r"cannot parse xref|"
    r"damaged pdf|"
    r"syntax error.*xref"
    r")",
    re.I,
)

MIN_REASONABLE_PAPER_TEXT_CHARS = 1000

# These require nearby MS/proteomics context.
INDIVIDUAL_CELL_PATTERNS = [
    (
        "individual_cells_analyzed_by_ms",
        re.compile(
            r"\\bindividual\\s+(?:single\\s+)?"
            r"(?:[A-Za-z0-9+._-]+\\s+){0,3}"
            r"(?:cells?|oocytes?|neurons?|blastomeres?|bacteria|bacterium)\\b"
            r".{0,140}"
            r"\\b(?:analy[sz](?:ed|ing)?|measure(?:d|ment)?|profile(?:d|ing)?|"
            r"process(?:ed|ing)?)\\b"
            r".{0,180}"
            r"\\b(?:proteom(?:e|es|ics|ic)|mass\\s+spectrom(?:etry|etric)|LC[- ]?MS(?:/MS)?|MS/MS)\\b",
            re.I | re.S,
        ),
        9,
    ),
    (
        "ms_analysis_of_individual_cells",
        re.compile(
            r"\\b(?:proteom(?:e|es|ics|ic)|mass\\s+spectrom(?:etry|etric)|LC[- ]?MS(?:/MS)?|MS/MS)\\b"
            r".{0,180}"
            r"\\bindividual\\s+(?:single\\s+)?"
            r"(?:[A-Za-z0-9+._-]+\\s+){0,3}"
            r"(?:cells?|oocytes?|neurons?|blastomeres?|bacteria|bacterium)\\b",
            re.I | re.S,
        ),
        9,
    ),
    (
        "single_cells_prepared_for_ms",
        re.compile(
            r"\\bsingle[- ](?:[A-Za-z0-9+._-]+\\s+){0,3}"
            r"(?:cells?|oocytes?|neurons?|blastomeres?|bacteria|bacterium)\\b"
            r".{0,120}"
            r"\\b(?:sort(?:ed|ing)?|isolate(?:d|ion)?|pick(?:ed|ing)?|"
            r"dispens(?:ed|ing)?|deposit(?:ed|ing)?|lyse(?:d|lysis)|digest(?:ed|ion)?)\\b"
            r".{0,220}"
            r"\\b(?:proteom(?:e|es|ics|ic)|mass\\s+spectrom(?:etry|etric)|LC[- ]?MS(?:/MS)?|MS/MS)\\b",
            re.I | re.S,
        ),
        9,
    ),
    (
        "single_named_cell_proteomics",
        re.compile(
            r"\\bsingle\\s+(?:[A-Za-z0-9+._-]+\\s+){0,3}"
            r"(?:cell|oocyte|neuron|blastomere|bacterium)\\b"
            r".{0,180}"
            r"\\b(?:proteom(?:e|es|ics|ic)|mass\\s+spectrom|LC[- ]?MS)\\b",
            re.I | re.S,
        ),
        9,
    ),
]

GENERIC_SINGLE_CELL_RE = re.compile(
    r"\b(?:single[- ]cells?|individual\s+cells?)\b",
    re.I,
)

LOW_INPUT_RE = re.compile(
    r"\b(?:"
    r"\d+(?:\.\d+)?\s*(?:pg|picograms?|ng|nanograms?)|"
    r"low[- ]input|low[- ]amount|ultra[- ]low[- ]input|"
    r"dilution\s+series|diluted\s+(?:digest|peptides?)|"
    r"\b\d+\s*(?:to|-)\s*\d+\s+cells?\b|"
    r"\b\d+\s+cells?\b"
    r")\b",
    re.I,
)

# Target-PXD local descriptors that strongly suggest the target accession is a
# benchmark/optimization/bulk dataset rather than the single-cell dataset.
NON_SCP_ACCESSION_SCOPE_RE = re.compile(
    r"\b(?:"
    r"bulk|dilution|diluted|benchmark|benchmarking|"
    r"fractionation|fractionated|spectral\s+library|library\s+generation|"
    r"window\s+(?:optimization|design)|optimization|"
    r"carrier|reference\s+channel|QC|quality\s+control|"
    r"multi[- ]cell|pooled|pooling|"
    r"\d+(?:\.\d+)?\s*(?:pg|ng)|"
    r"\d+\s*(?:to|-)\s*\d+\s+cells?"
    r")\b",
    re.I,
)

# Especially useful for Data Availability / repository-mapping prose.
TARGET_SCP_LABEL_RE_TEMPLATE = (
    r"(?:"
    # Preferred repository-list form:
    #   single-cell data: PXD046357
    r"single[- ]cell(?:\s+(?:data|dataset|proteomics?|samples?))?"
    r"[^;\n]{{0,160}}\b{pxd}\b"
    r"|"
    # Conservative reverse form:
    #   PXD046357: single-cell data
    # Do not cross semicolons, which separate neighboring dataset labels.
    r"\b{pxd}\b\s*(?::|-)\s*"
    r"single[- ]cell(?:\s+(?:data|dataset|proteomics?|samples?))?"
    r")"
)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "manifest_tsv",
    )

    parser.add_argument(
        "--output",
        default="pride_publications_screened.tsv",
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=min(8, max(1, os.cpu_count() or 1)),
        help=(
            "Parallel unique-PDF workers. Default: min(8, CPU count). "
            "On a 14-thread workstation, 8-10 is a good starting point."
        ),
    )

    parser.add_argument(
        "--executor",
        choices=("process", "thread"),
        default="process",
        help=(
            "Parallel executor. 'process' is the default and generally "
            "best for PDF extraction + regex scoring."
        ),
    )

    parser.add_argument(
        "--cache-dir",
        default=".scp_screen_cache",
        help=(
            "Persistent extracted-text cache directory. "
            "Default: .scp_screen_cache"
        ),
    )

    parser.add_argument(
        "--refresh-text-cache",
        action="store_true",
        help="Ignore cached extracted PDF text and re-extract PDFs.",
    )

    parser.add_argument(
        "--max-evidence-chars",
        type=int,
        default=3000,
        help="Maximum retained audit evidence per manifest row.",
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Text extraction and persistent cache
# ---------------------------------------------------------------------------

def normalize_pdf_text(text: str) -> str:
    text = text.replace("\u00ad", "")

    # Rejoin words split by line-end hyphenation.
    text = re.sub(
        r"(?<=\w)-\s*\n\s*(?=\w)",
        "",
        text,
    )

    # Preserve paragraph-ish newlines while collapsing noisy spaces.
    lines = [
        re.sub(r"[ \t]+", " ", line).strip()
        for line in text.splitlines()
    ]

    out = []
    blank = False

    for line in lines:
        if not line:
            if out and not blank:
                out.append("")
            blank = True
            continue

        out.append(line)
        blank = False

    return "\n".join(out).strip()


def strip_reference_section(text: str) -> tuple[str, bool]:
    """
    Remove conventional reference-list blocks without discarding later online
    Methods/Data Availability content.

    Nature-family PDFs commonly place the main article References before
    appended Methods/reporting-summary pages. Therefore truncating the whole
    document at the first "References" heading loses exactly the accession
    mapping needed for PRIDE screening.

    We remove a References/Bibliography block until the next recognizable
    major heading. If no later major heading exists, only then is the
    references tail truncated.
    """
    if not text:
        return text, False

    reference_heading = re.compile(
        r"(?im)^[ \t]*(?:references|bibliography|literature cited)[ \t]*$"
    )

    resume_heading = re.compile(
        r"(?im)^[ \t]*(?:"
        r"methods|online methods|materials and methods|"
        r"data availability|code availability|reporting summary|"
        r"acknowledgements?|author contributions?|"
        r"extended data|supplementary information|"
        r"additional information|ethics declaration|"
        r"inclusion and ethics statement|online content"
        r")[ \t]*$"
    )

    matches = list(reference_heading.finditer(text))

    if not matches:
        return text, False

    pieces = []
    cursor = 0
    removed = False

    for ref_match in matches:
        # Ignore accidental early occurrences in running text.
        if ref_match.start() < int(len(text) * 0.20):
            continue

        pieces.append(
            text[cursor:ref_match.start()]
        )

        next_heading = resume_heading.search(
            text,
            ref_match.end(),
        )

        if next_heading is None:
            cursor = len(text)
            removed = True
            break

        cursor = next_heading.start()
        removed = True

    if not removed:
        return text, False

    if cursor < len(text):
        pieces.append(
            text[cursor:]
        )

    cleaned = "\n".join(
        piece.rstrip()
        for piece in pieces
        if piece
    )

    return cleaned.strip(), True


def pdf_cache_key(path: Path) -> str:
    stat = path.stat()

    identity = (
        f"{path.resolve()}\n"
        f"{stat.st_size}\n"
        f"{stat.st_mtime_ns}\n"
        f"{TEXT_CACHE_VERSION}"
    )

    return hashlib.sha1(
        identity.encode("utf-8")
    ).hexdigest()


def text_cache_path(
    pdf_path: Path,
    cache_dir: Path,
) -> Path:
    return (
        cache_dir
        / "text"
        / f"{pdf_cache_key(pdf_path)}.txt.gz"
    )


def read_text_cache(
    pdf_path: Path,
    cache_dir: Path,
) -> str | None:
    path = text_cache_path(
        pdf_path,
        cache_dir,
    )

    if not path.is_file():
        return None

    try:
        with gzip.open(
            path,
            "rt",
            encoding="utf-8",
        ) as handle:
            return handle.read()
    except Exception:
        return None


def write_text_cache(
    pdf_path: Path,
    cache_dir: Path,
    text: str,
) -> None:
    path = text_cache_path(
        pdf_path,
        cache_dir,
    )

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = path.with_suffix(
        path.suffix + ".tmp"
    )

    with gzip.open(
        tmp,
        "wt",
        encoding="utf-8",
        compresslevel=3,
    ) as handle:
        handle.write(text)

    tmp.replace(path)


def text_cache_meta_path(
    pdf_path: Path,
    cache_dir: Path,
) -> Path:
    text_path = text_cache_path(
        pdf_path,
        cache_dir,
    )

    return text_path.with_suffix(
        ".meta.json"
    )


def read_text_cache_meta(
    pdf_path: Path,
    cache_dir: Path,
) -> dict[str, Any] | None:
    path = text_cache_meta_path(
        pdf_path,
        cache_dir,
    )

    if not path.is_file():
        return None

    try:
        value = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

        return value if isinstance(
            value,
            dict,
        ) else None

    except Exception:
        return None


def write_text_cache_meta(
    pdf_path: Path,
    cache_dir: Path,
    metadata: dict[str, Any],
) -> None:
    path = text_cache_meta_path(
        pdf_path,
        cache_dir,
    )

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = path.with_suffix(
        path.suffix + ".tmp"
    )

    tmp.write_text(
        json.dumps(
            metadata,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    tmp.replace(path)


def extraction_quality(
    *,
    text: str,
    page_count: int,
    pages_with_text: int,
    page_errors: list[str],
    warnings: str,
) -> tuple[str, bool, str]:
    """
    Return (quality, suspicious, reason).

    MuPDF often emits harmless font/color warnings while still extracting
    excellent text. We therefore use objective extraction completeness as the
    main criterion and treat severe structural warnings as an additional
    signal rather than automatically failing the PDF.
    """
    chars = len(
        text.strip()
    )

    reasons = []

    if page_count <= 0:
        reasons.append(
            "no_pages"
        )

    if chars < MIN_REASONABLE_PAPER_TEXT_CHARS:
        reasons.append(
            f"low_text_chars={chars}"
        )

    if (
        page_count > 0
        and pages_with_text == 0
    ):
        reasons.append(
            "no_pages_with_text"
        )

    if (
        page_count > 0
        and len(page_errors)
        / page_count
        > 0.20
    ):
        reasons.append(
            "many_page_errors="
            f"{len(page_errors)}/{page_count}"
        )

    if (
        warnings
        and SEVERE_MUPDF_WARNING_RE.search(
            warnings
        )
    ):
        reasons.append(
            "severe_mupdf_warning"
        )

    suspicious = bool(
        reasons
    )

    if not suspicious:
        quality = "good"
    elif chars >= MIN_REASONABLE_PAPER_TEXT_CHARS:
        quality = "warning"
    elif chars > 0:
        quality = "poor"
    else:
        quality = "failed"

    return (
        quality,
        suspicious,
        ";".join(
            reasons
        ),
    )


def extract_with_pdftotext(
    path: Path,
) -> tuple[str, dict[str, Any]]:
    executable = shutil.which(
        "pdftotext"
    )

    if not executable:
        return (
            "",
            {
                "available": False,
                "success": False,
                "error": (
                    "pdftotext executable not found"
                ),
            },
        )

    try:
        proc = subprocess.run(
            [
                executable,
                "-layout",
                "-enc",
                "UTF-8",
                str(path),
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )

        raw = proc.stdout or ""

        text = normalize_pdf_text(
            raw
        )

        text, references_removed = (
            strip_reference_section(
                text
            )
        )

        success = (
            proc.returncode == 0
            and bool(
                text.strip()
            )
        )

        return (
            text,
            {
                "available": True,
                "success": success,
                "returncode": (
                    proc.returncode
                ),
                "stderr": (
                    proc.stderr or ""
                )[:4000],
                "references_removed": (
                    references_removed
                ),
                "text_chars": len(
                    text
                ),
            },
        )

    except Exception as exc:
        return (
            "",
            {
                "available": True,
                "success": False,
                "error": (
                    f"{type(exc).__name__}: "
                    f"{exc}"
                ),
            },
        )


def extract_pdf_text(
    path: Path,
) -> tuple[str, str, dict[str, Any]]:
    """
    Extract publication text while capturing MuPDF diagnostics.

    MuPDF parser messages are suppressed from stderr and stored in metadata.
    A Poppler pdftotext fallback is attempted only when primary extraction
    fails or looks objectively suspicious.
    """
    metadata: dict[str, Any] = {
        "backend": "pymupdf",
        "fallback_attempted": False,
        "fallback_used": False,
        "page_count": 0,
        "pages_with_text": 0,
        "page_errors": [],
        "mupdf_warnings": "",
        "text_chars": 0,
        "quality": "",
        "quality_reason": "",
        "references_removed": False,
    }

    primary_text = ""
    primary_error = ""

    # Clear diagnostics left by a previous document in the same worker.
    try:
        fitz.TOOLS.mupdf_warnings(
            reset=True
        )
    except Exception:
        pass

    # Suppress MuPDF's direct stderr chatter. Diagnostics are collected below.
    try:
        fitz.TOOLS.mupdf_display_errors(
            False
        )
    except Exception:
        pass

    try:
        fitz.TOOLS.mupdf_display_warnings(
            False
        )
    except Exception:
        pass

    doc = None

    try:
        doc = fitz.open(
            path
        )

        metadata[
            "page_count"
        ] = int(
            getattr(
                doc,
                "page_count",
                0,
            )
            or 0
        )

        pages = []
        pages_with_text = 0
        page_errors = []

        for page_index in range(
            metadata[
                "page_count"
            ]
        ):
            try:
                page = doc.load_page(
                    page_index
                )

                page_text = (
                    page.get_text(
                        "text"
                    )
                    or ""
                )

                if page_text.strip():
                    pages_with_text += 1

                pages.append(
                    page_text
                )

            except Exception as exc:
                page_errors.append(
                    f"page {page_index}: "
                    f"{type(exc).__name__}: "
                    f"{exc}"
                )

        metadata[
            "pages_with_text"
        ] = pages_with_text

        metadata[
            "page_errors"
        ] = page_errors[:100]

        primary_text = normalize_pdf_text(
            "\n\n".join(
                pages
            )
        )

        (
            primary_text,
            references_removed,
        ) = strip_reference_section(
            primary_text
        )

        metadata[
            "references_removed"
        ] = references_removed

    except Exception as exc:
        primary_error = (
            f"{type(exc).__name__}: "
            f"{exc}"
        )

    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass

    try:
        warnings = (
            fitz.TOOLS.mupdf_warnings(
                reset=True
            )
            or ""
        )
    except Exception:
        warnings = ""

    metadata[
        "mupdf_warnings"
    ] = warnings[:8000]

    metadata[
        "text_chars"
    ] = len(
        primary_text
    )

    (
        quality,
        suspicious,
        quality_reason,
    ) = extraction_quality(
        text=primary_text,
        page_count=int(
            metadata[
                "page_count"
            ]
        ),
        pages_with_text=int(
            metadata[
                "pages_with_text"
            ]
        ),
        page_errors=list(
            metadata[
                "page_errors"
            ]
        ),
        warnings=warnings,
    )

    metadata[
        "quality"
    ] = quality

    metadata[
        "quality_reason"
    ] = quality_reason

    should_fallback = (
        bool(
            primary_error
        )
        or suspicious
    )

    if should_fallback:
        metadata[
            "fallback_attempted"
        ] = True

        (
            fallback_text,
            fallback_meta,
        ) = extract_with_pdftotext(
            path
        )

        metadata[
            "pdftotext"
        ] = fallback_meta

        # Prefer the fallback if the primary failed, or if Poppler recovered
        # materially more text from a suspicious PDF.
        fallback_is_better = (
            fallback_meta.get(
                "success"
            )
            and (
                not primary_text.strip()
                or len(
                    fallback_text
                )
                > max(
                    len(
                        primary_text
                    )
                    * 1.10,
                    len(
                        primary_text
                    )
                    + 500,
                )
            )
        )

        if fallback_is_better:
            primary_text = (
                fallback_text
            )

            metadata[
                "backend"
            ] = "pdftotext"

            metadata[
                "fallback_used"
            ] = True

            metadata[
                "text_chars"
            ] = len(
                primary_text
            )

            # The fallback recovered usable source text, so do not treat a
            # MuPDF parser warning as a fatal screen error.
            metadata[
                "quality"
            ] = (
                "good"
                if len(
                    primary_text
                )
                >= MIN_REASONABLE_PAPER_TEXT_CHARS
                else "warning"
            )

            metadata[
                "quality_reason"
            ] = (
                "pdftotext_fallback_recovered_text"
            )

            primary_error = ""

    if not primary_text.strip():
        error = (
            primary_error
            or (
                "No usable text could be "
                "extracted from PDF"
            )
        )

        return (
            "",
            error,
            metadata,
        )

    # A structurally odd PDF with substantial recovered text remains
    # screenable; its quality/warnings are retained for audit.
    return (
        primary_text,
        "",
        metadata,
    )


# ---------------------------------------------------------------------------
# Evidence helpers
# ---------------------------------------------------------------------------

def compact_whitespace(text: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        text,
    ).strip()


def context_window(
    text: str,
    start: int,
    end: int,
    radius: int = 420,
) -> str:
    left = max(
        0,
        start - radius,
    )
    right = min(
        len(text),
        end + radius,
    )

    return compact_whitespace(
        text[left:right]
    )


def mask_false_friends(
    text: str,
) -> tuple[str, list[dict[str, str]]]:
    masked = text
    hits = []

    for reason_class, pattern in FALSE_FRIEND_PATTERNS:
        matches = list(
            pattern.finditer(masked)
        )

        for match in matches:
            hits.append(
                {
                    "reason_class": reason_class,
                    "text": compact_whitespace(
                        match.group(0)
                    ),
                }
            )

        masked = pattern.sub(
            lambda m: " " * (
                m.end() - m.start()
            ),
            masked,
        )

    return masked, hits


def collect_publication_scp_hits(
    text: str,
    publication_title: str = "",
) -> dict[str, Any]:
    masked, false_friend_hits = mask_false_friends(
        text
    )

    title_masked, title_false_friends = (
        mask_false_friends(
            publication_title
            or ""
        )
    )

    title_direct_scp = bool(
        TITLE_DIRECT_SCP_RE.search(
            title_masked
        )
    )

    strong_hits: list[
        dict[str, Any]
    ] = []

    for reason_class, pattern, score in STRONG_SCP_PATTERNS:
        for match in pattern.finditer(
            masked
        ):
            strong_hits.append(
                {
                    "reason_class": reason_class,
                    "score": score,
                    "match": compact_whitespace(
                        match.group(0)
                    ),
                    "context": context_window(
                        masked,
                        match.start(),
                        match.end(),
                    ),
                }
            )

    for reason_class, pattern, score in INDIVIDUAL_CELL_PATTERNS:
        for match in pattern.finditer(
            masked
        ):
            local = context_window(
                masked,
                match.start(),
                match.end(),
                radius=500,
            )

            if MS_CONTEXT_RE.search(
                local
            ):
                strong_hits.append(
                    {
                        "reason_class": (
                            reason_class
                            + "_with_ms_context"
                        ),
                        "score": score,
                        "match": compact_whitespace(
                            match.group(0)
                        ),
                        "context": local,
                    }
                )

    weak_hits = []

    for match in GENERIC_SINGLE_CELL_RE.finditer(
        masked
    ):
        local = context_window(
            masked,
            match.start(),
            match.end(),
            radius=320,
        )

        if MS_CONTEXT_RE.search(
            local
        ):
            weak_hits.append(
                {
                    "reason_class": (
                        "generic_single_cell_with_ms_context"
                    ),
                    "score": 3,
                    "match": compact_whitespace(
                        match.group(0)
                    ),
                    "context": local,
                }
            )

    def dedupe(hits):
        out = []
        seen = set()

        for hit in hits:
            key = (
                hit[
                    "reason_class"
                ],
                hit[
                    "match"
                ].lower(),
                hit[
                    "context"
                ][:220].lower(),
            )

            if key in seen:
                continue

            seen.add(
                key
            )

            out.append(
                hit
            )

        return out

    strong_hits = dedupe(
        strong_hits
    )

    weak_hits = dedupe(
        weak_hits
    )

    direct_body_hits = [
        hit
        for hit in strong_hits
        if hit[
            "reason_class"
        ] in DIRECT_TERM_REASON_CLASSES
    ]

    procedural_hits = [
        hit
        for hit in strong_hits
        if hit[
            "reason_class"
        ] in HIGH_PRECISION_PROCEDURAL_REASON_CLASSES
    ]

    contextual_hits = [
        hit
        for hit in strong_hits
        if (
            hit[
                "reason_class"
            ]
            not in DIRECT_TERM_REASON_CLASSES
            and hit[
                "reason_class"
            ]
            not in HIGH_PRECISION_PROCEDURAL_REASON_CLASSES
        )
    ]

    # Evidence tiers:
    #
    # high:
    #   explicit SCP terminology in the paper title OR strong procedural /
    #   quantitative evidence that individual cells were the MS input.
    #
    # moderate:
    #   repeated direct SCP terminology in body text, or a direct term plus a
    #   separate contextual individual-cell/MS observation.
    #
    # weak:
    #   isolated body-text SCP terminology / generic single-cell+MS wording.
    #
    # This prevents one related-work mention of "single-cell proteomics" from
    # converting an otherwise conventional bulk proteomics paper into a
    # high-confidence publication.
    if (
        title_direct_scp
        or procedural_hits
    ):
        evidence_tier = "high"

    elif (
        len(
            direct_body_hits
        )
        >= 2
        or (
            direct_body_hits
            and contextual_hits
        )
    ):
        evidence_tier = "moderate"

    elif (
        strong_hits
        or weak_hits
    ):
        evidence_tier = "weak"

    else:
        evidence_tier = "none"

    if evidence_tier == "high":
        publication_has_scp = "yes"

    elif evidence_tier == "moderate":
        publication_has_scp = (
            "uncertain"
        )

    elif evidence_tier == "weak":
        publication_has_scp = (
            "weak"
        )

    else:
        publication_has_scp = "no"

    max_score = max(
        [
            h["score"]
            for h in strong_hits
        ],
        default=0,
    )

    if title_direct_scp:
        max_score = max(
            max_score,
            12,
        )

    return {
        "strong_hits": strong_hits,
        "weak_hits": weak_hits,
        "false_friend_hits": (
            false_friend_hits
            + title_false_friends
        ),
        "direct_body_hits": (
            direct_body_hits
        ),
        "procedural_hits": (
            procedural_hits
        ),
        "contextual_hits": (
            contextual_hits
        ),
        "title_direct_scp": (
            title_direct_scp
        ),
        "publication_evidence_tier": (
            evidence_tier
        ),
        "max_score": max_score,
        "publication_has_scp": (
            publication_has_scp
        ),
    }


def find_accession_segments(
    text: str,
) -> dict[str, list[str]]:
    """
    Build local accession segments with boundaries limited by neighboring PXD
    mentions. This reduces evidence leakage from one PXD's description to the
    next PXD in a Data Availability list.
    """
    matches = list(
        PXD_RE.finditer(text)
    )

    result: dict[
        str,
        list[str],
    ] = {}

    if not matches:
        return result

    for index, match in enumerate(
        matches
    ):
        accession = match.group(
            0
        ).upper()

        previous_end = (
            matches[index - 1].end()
            if index > 0
            else 0
        )

        next_start = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(text)
        )

        # Bound by neighboring PXD mentions, but still allow enough local
        # prose before/after the accession for a label such as:
        # "single-cell data are deposited under PXD..."
        left = max(
            previous_end,
            match.start() - 500,
        )

        right = min(
            next_start,
            match.end() + 500,
        )

        segment = compact_whitespace(
            text[left:right]
        )

        result.setdefault(
            accession,
            [],
        ).append(segment)

    return result


def target_explicit_scp_mapping(
    segments: list[str],
    accession: str,
) -> list[str]:
    """
    Detect explicit single-cell labels only inside the target accession's
    neighbor-bounded local segment. This prevents:

        single-cell data: PXD_A; window optimization: PXD_B

    from incorrectly marking PXD_B as single-cell merely because the phrase
    "single-cell" occurs nearby in the same Data Availability paragraph.
    """
    if not accession:
        return []

    pattern = re.compile(
        TARGET_SCP_LABEL_RE_TEMPLATE.format(
            pxd=re.escape(
                accession
            )
        ),
        re.I | re.S,
    )

    hits = []

    for segment in segments:
        for match in pattern.finditer(
            segment
        ):
            hits.append(
                compact_whitespace(
                    segment
                )
            )

    return hits


def score_accession_segments(
    segments: list[str],
) -> dict[str, Any]:
    strong = []
    weak = []
    non_scp = []
    false_friends = []

    for segment in segments:
        pub = collect_publication_scp_hits(
            segment
        )

        strong.extend(
            pub["strong_hits"]
        )

        weak.extend(
            pub["weak_hits"]
        )

        false_friends.extend(
            pub["false_friend_hits"]
        )

        if NON_SCP_ACCESSION_SCOPE_RE.search(
            segment
        ):
            non_scp.append(
                segment
            )

    direct_term_hits = [
        hit
        for hit in strong
        if hit.get("reason_class")
        in DIRECT_TERM_REASON_CLASSES
    ]

    procedural_hits = [
        hit
        for hit in strong
        if hit.get("reason_class")
        in HIGH_PRECISION_PROCEDURAL_REASON_CLASSES
    ]

    contextual_hits = [
        hit
        for hit in strong
        if (
            hit.get("reason_class")
            not in DIRECT_TERM_REASON_CLASSES
            and hit.get("reason_class")
            not in HIGH_PRECISION_PROCEDURAL_REASON_CLASSES
        )
    ]

    return {
        "strong_hits": strong,
        "direct_term_hits": direct_term_hits,
        "procedural_hits": procedural_hits,
        "contextual_hits": contextual_hits,
        "weak_hits": weak,
        "non_scp_segments": non_scp,
        "false_friend_hits": false_friends,
    }


def compact_hit_evidence(
    hits: list[dict[str, Any]],
    max_chars: int,
) -> str:
    pieces = []

    for hit in sorted(
        hits,
        key=lambda h: h.get(
            "score",
            0,
        ),
        reverse=True,
    ):
        piece = (
            f"[{hit.get('reason_class','evidence')}; "
            f"score={hit.get('score',0)}] "
            f"{hit.get('context','')}"
        )

        if piece not in pieces:
            pieces.append(
                piece
            )

    joined = "\n\n".join(
        pieces
    )

    if len(joined) > max_chars:
        return (
            joined[:max_chars].rstrip()
            + " ..."
        )

    return joined


def compact_text_evidence(
    values: list[str],
    max_chars: int,
) -> str:
    pieces = []

    for value in values:
        value = compact_whitespace(
            value
        )

        if value and value not in pieces:
            pieces.append(
                value
            )

    joined = "\n\n".join(
        pieces
    )

    if len(joined) > max_chars:
        return (
            joined[:max_chars].rstrip()
            + " ..."
        )

    return joined


# ---------------------------------------------------------------------------
# Accession-aware decision logic
# ---------------------------------------------------------------------------

def classify_accession(
    *,
    accession: str,
    publication_title: str,
    group_accessions: list[str],
    text: str,
    publication_result: dict[str, Any],
    accession_segments: dict[str, list[str]],
    max_evidence_chars: int,
) -> dict[str, Any]:

    target_segments = accession_segments.get(
        accession,
        [],
    )

    target_scored = score_accession_segments(
        target_segments
    )

    explicit_target_mapping = target_explicit_scp_mapping(
        target_segments,
        accession,
    )

    mentioned_group_accessions = {
        value
        for value in group_accessions
        if accession_segments.get(
            value
        )
    }

    other_explicit_scp = {}

    for other in group_accessions:
        if other == accession:
            continue

        hits = target_explicit_scp_mapping(
            accession_segments.get(
                other,
                [],
            ),
            other,
        )

        if hits:
            other_explicit_scp[
                other
            ] = hits

    publication_has_scp = publication_result[
        "publication_has_scp"
    ]

    publication_score = publication_result[
        "max_score"
    ]

    publication_evidence_tier = publication_result[
        "publication_evidence_tier"
    ]

    title_direct_scp = bool(
        publication_result[
            "title_direct_scp"
        ]
    )

    target_has_strong = bool(
        target_scored["strong_hits"]
        or explicit_target_mapping
    )

    target_has_weak = bool(
        target_scored["weak_hits"]
    )

    target_has_non_scp_scope = bool(
        target_scored[
            "non_scp_segments"
        ]
    )

    target_evidence = (
        explicit_target_mapping
        + [
            h["context"]
            for h in target_scored[
                "strong_hits"
            ]
        ]
        + [
            h["context"]
            for h in target_scored[
                "weak_hits"
            ]
        ]
    )

    # 1. Strong accession-local mapping wins.
    if explicit_target_mapping:
        decision = "candidate"
        score = max(
            12,
            publication_score,
        )
        reason_class = (
            "target_pxd_explicitly_single_cell"
        )
        reason = (
            "The target PXD is explicitly mapped to single-cell data/"
            "proteomics in the publication text."
        )
        target_scope = "single_cell"

    elif target_scored["procedural_hits"]:
        decision = "candidate"
        score = max(
            publication_score,
            max(
                h["score"]
                for h in target_scored[
                    "procedural_hits"
                ]
            ),
        )
        reason_class = (
            "target_pxd_local_individual_cell_proteomics"
        )
        reason = (
            "High-precision procedural or per-cell proteomics evidence occurs "
            "in the target PXD's local source-text context."
        )
        target_scope = "single_cell"

    # 2. If a strong-SCP publication names only one of the manifest-linked
    #    PXDs in its own Data Availability/source text, that named accession
    #    is the most defensible dataset assignment. This is especially useful
    #    when PRIDE metadata associates the same publication with additional
    #    later/reused accessions not named by the paper itself.
    elif (
        publication_has_scp == "yes"
        and publication_evidence_tier == "high"
        and len(mentioned_group_accessions) == 1
        and accession in mentioned_group_accessions
    ):
        decision = "candidate"
        score = max(
            publication_score,
            9,
        )
        reason_class = (
            "target_pxd_only_accession_mentioned_in_high_confidence_scp_publication"
        )
        reason = (
            "The publication has high-confidence SCP evidence (title or "
            "procedural/per-cell evidence), and the target PXD is the only "
            "manifest-linked accession explicitly mentioned in the paper."
        )
        target_scope = "likely_single_cell"

    elif (
        publication_evidence_tier == "moderate"
        and len(mentioned_group_accessions) == 1
        and accession in mentioned_group_accessions
    ):
        decision = "uncertain"
        score = max(
            publication_score,
            5,
        )
        reason_class = (
            "target_pxd_only_accession_in_moderate_scp_publication"
        )
        reason = (
            "The target PXD is the only associated accession mentioned, but "
            "the publication-level SCP evidence is moderate rather than "
            "high-confidence."
        )
        target_scope = "possibly_single_cell"

    elif (
        publication_has_scp == "yes"
        and len(mentioned_group_accessions) == 1
        and accession not in mentioned_group_accessions
    ):
        decision = "reject"
        score = 0
        reason_class = (
            "publication_scp_but_other_pxd"
        )
        reason = (
            "The publication contains genuine SCP, but a different associated "
            "PXD is the only accession actually named in the publication text."
        )
        target_scope = (
            "non_single_cell_or_secondary_association"
        )

    # 3. If another PXD is explicitly mapped to SCP and this one is not, do
    #    not propagate publication-level SCP status to every accession.
    elif (
        publication_has_scp == "yes"
        and other_explicit_scp
    ):
        decision = "reject"
        score = 0
        reason_class = (
            "publication_scp_but_other_pxd"
        )
        reason = (
            "The publication contains genuine SCP, but another associated "
            "PXD is explicitly mapped to the single-cell experiment while "
            "the target PXD is not."
        )
        target_scope = (
            "non_single_cell_or_other_experiment"
        )

    # 4. A target-local benchmark/bulk/optimization descriptor is useful when
    #    the publication itself contains SCP elsewhere.
    elif (
        publication_has_scp == "yes"
        and target_has_non_scp_scope
        and len(group_accessions) > 1
    ):
        decision = "reject"
        score = 0
        reason_class = (
            "target_pxd_non_scp_scope"
        )
        reason = (
            "The publication contains SCP, but the target PXD's local "
            "context describes a benchmark, bulk, dilution, fractionation, "
            "library, optimization, carrier, or other non-single-cell scope."
        )
        target_scope = (
            "non_single_cell_or_other_experiment"
        )

    # 5. A single-PXD paper with strong publication evidence is a reasonable
    #    candidate even when the accession is mentioned only in Data
    #    Availability without a detailed local label.
    elif (
        publication_has_scp == "yes"
        and len(group_accessions) == 1
    ):
        decision = "candidate"
        score = publication_score
        reason_class = (
            "single_pxd_publication_with_strong_scp"
        )
        reason = (
            "The publication has strong genuine SCP evidence and only one "
            "associated PXD is represented for this PDF."
        )
        target_scope = "likely_single_cell"

    # 6. Multi-PXD papers with genuine SCP but no resolvable accession mapping
    #    remain uncertain rather than multiplying all PXDs into candidates.
    elif publication_has_scp == "yes":
        decision = "uncertain"
        score = publication_score
        reason_class = (
            "ambiguous_target_pxd_in_scp_publication"
        )
        reason = (
            "The publication contains genuine SCP, but the target PXD cannot "
            "be assigned confidently from the available accession-local text."
        )
        target_scope = "ambiguous"

    # 7. Moderate publication evidence remains uncertain and is appropriate
    #    for LLM adjudication.
    elif publication_has_scp == "uncertain":
        decision = "uncertain"
        score = max(
            publication_score,
            4,
        )
        reason_class = (
            "moderate_publication_scp_evidence"
        )
        reason = (
            "Repeated/direct single-cell proteomics wording or supporting "
            "individual-cell/MS context is present, but neither the title nor "
            "high-precision procedural evidence establishes SCP confidently."
        )
        target_scope = (
            "ambiguous"
            if len(group_accessions) > 1
            else "possibly_single_cell"
        )

    # 8. A single isolated body-text SCP mention is now treated as a likely
    #    background/related-work false positive unless the target accession
    #    had its own local evidence (handled above).
    elif publication_has_scp == "weak":
        decision = "reject"
        score = 0
        reason_class = (
            "isolated_or_weak_scp_mention_only"
        )
        reason = (
            "Only weak or isolated publication-level single-cell wording was "
            "found, with no target-PXD-local, title-level, procedural, or "
            "per-cell proteomics evidence."
        )
        target_scope = "not_established_as_single_cell"

    else:
        decision = "reject"
        score = 0
        reason_class = (
            "no_individual_cell_proteomics_evidence"
        )
        reason = (
            "No source-text evidence was found that proteomic/MS measurements "
            "were performed on individual biological cells."
        )
        target_scope = "not_single_cell"

    publication_evidence = compact_hit_evidence(
        publication_result[
            "strong_hits"
        ]
        + publication_result[
            "weak_hits"
        ],
        max_evidence_chars,
    )

    target_evidence_text = compact_text_evidence(
        target_evidence
        + target_scored[
            "non_scp_segments"
        ],
        max_evidence_chars,
    )

    combined_evidence = compact_text_evidence(
        [
            target_evidence_text,
            publication_evidence,
        ],
        max_evidence_chars,
    )

    return {
        "scp_screen_decision": decision,
        "scp_screen_score": score,
        "scp_screen_reason_class": (
            reason_class
        ),
        "scp_screen_reason": reason,
        "scp_screen_evidence": combined_evidence,
        "scp_screen_publication_has_scp": (
            publication_has_scp
        ),
        "scp_screen_publication_score": (
            publication_score
        ),
        "scp_screen_publication_evidence_tier": (
            publication_evidence_tier
        ),
        "scp_screen_title_direct_scp": (
            "yes"
            if title_direct_scp
            else "no"
        ),
        "scp_screen_target_pxd_scope": (
            target_scope
        ),
        "scp_screen_target_pxd_evidence": (
            target_evidence_text
        ),
        "scp_screen_other_explicit_scp_pxds": (
            ";".join(
                sorted(
                    other_explicit_scp
                )
            )
        ),
        "scp_screen_positive_hits_json": json.dumps(
            publication_result[
                "strong_hits"
            ]
            + publication_result[
                "weak_hits"
            ],
            ensure_ascii=False,
        ),
        "scp_screen_negative_hits_json": json.dumps(
            publication_result[
                "false_friend_hits"
            ],
            ensure_ascii=False,
        ),
        "scp_screen_low_input_hits_json": json.dumps(
            LOW_INPUT_RE.findall(
                text
            )[:50],
            ensure_ascii=False,
        ),
        "scp_screen_pdf_error": "",
        "scp_screen_algorithm_version": (
            SCREEN_ALGORITHM_VERSION
        ),
    }


# ---------------------------------------------------------------------------
# Unique-PDF worker
# ---------------------------------------------------------------------------

def process_pdf_group(
    task: dict[str, Any],
) -> dict[str, Any]:
    pdf_path = Path(
        task["pdf_path"]
    )

    cache_dir = Path(
        task["cache_dir"]
    )

    refresh = bool(
        task["refresh_text_cache"]
    )

    max_evidence_chars = int(
        task["max_evidence_chars"]
    )

    row_targets = task[
        "row_targets"
    ]

    group_accessions = sorted(
        {
            accession
            for _, accession, _publication_title
            in row_targets
            if accession
        }
    )

    if not validate_pdf_path(
        pdf_path
    ):
        return {
            "pdf_path": str(pdf_path),
            "cache_hit": False,
            "error": (
                "Manifest PDF path is missing or invalid."
            ),
            "rows": {},
        }

    cache_hit = False

    text = None

    extraction_meta: dict[str, Any] = {}

    if not refresh:
        text = read_text_cache(
            pdf_path,
            cache_dir,
        )

        if text is not None:
            cache_hit = True

            extraction_meta = (
                read_text_cache_meta(
                    pdf_path,
                    cache_dir,
                )
                or {
                    "backend": "cached_text",
                    "quality": (
                        "good"
                        if len(
                            text.strip()
                        )
                        >= MIN_REASONABLE_PAPER_TEXT_CHARS
                        else "poor"
                    ),
                    "quality_reason": (
                        "legacy_cache_without_metadata"
                    ),
                    "text_chars": len(
                        text
                    ),
                    "page_count": "",
                    "pages_with_text": "",
                    "page_errors": [],
                    "mupdf_warnings": "",
                    "fallback_attempted": False,
                    "fallback_used": False,
                }
            )

            # The previous run's cache predates extraction diagnostics.
            # Re-check only objectively suspicious cached text, not all
            # 18k PDFs.
            if (
                len(
                    text.strip()
                )
                < MIN_REASONABLE_PAPER_TEXT_CHARS
            ):
                (
                    fallback_text,
                    fallback_meta,
                ) = extract_with_pdftotext(
                    pdf_path
                )

                extraction_meta[
                    "fallback_attempted"
                ] = True

                extraction_meta[
                    "pdftotext"
                ] = fallback_meta

                if (
                    fallback_meta.get(
                        "success"
                    )
                    and len(
                        fallback_text
                    )
                    > len(
                        text
                    )
                ):
                    text = (
                        fallback_text
                    )

                    extraction_meta[
                        "backend"
                    ] = "pdftotext"

                    extraction_meta[
                        "fallback_used"
                    ] = True

                    extraction_meta[
                        "quality"
                    ] = (
                        "good"
                        if len(
                            text
                        )
                        >= MIN_REASONABLE_PAPER_TEXT_CHARS
                        else "warning"
                    )

                    extraction_meta[
                        "quality_reason"
                    ] = (
                        "suspicious_cached_text_recovered_by_pdftotext"
                    )

                    extraction_meta[
                        "text_chars"
                    ] = len(
                        text
                    )

                    write_text_cache(
                        pdf_path,
                        cache_dir,
                        text,
                    )

                    write_text_cache_meta(
                        pdf_path,
                        cache_dir,
                        extraction_meta,
                    )

    if text is None:
        (
            text,
            error,
            extraction_meta,
        ) = extract_pdf_text(
            pdf_path
        )

        if error:
            return {
                "pdf_path": str(pdf_path),
                "cache_hit": False,
                "error": error,
                "extraction_meta": extraction_meta,
                "rows": {},
            }

        write_text_cache(
            pdf_path,
            cache_dir,
            text,
        )

        write_text_cache_meta(
            pdf_path,
            cache_dir,
            extraction_meta,
        )

    publication_title = (
        row_targets[0][2]
        if row_targets
        else ""
    )

    publication_result = (
        collect_publication_scp_hits(
            text,
            publication_title=(
                publication_title
            ),
        )
    )

    accession_segments = (
        find_accession_segments(
            text
        )
    )

    row_results = {}

    for (
        row_index,
        accession,
        publication_title,
    ) in row_targets:
        row_results[
            row_index
        ] = classify_accession(
            accession=accession,
            publication_title=(
                publication_title
            ),
            group_accessions=group_accessions,
            text=text,
            publication_result=(
                publication_result
            ),
            accession_segments=(
                accession_segments
            ),
            max_evidence_chars=(
                max_evidence_chars
            ),
        )

        row_results[
            row_index
        ].update(
            {
                "scp_screen_pdf_backend": (
                    extraction_meta.get(
                        "backend",
                        ""
                    )
                ),
                "scp_screen_pdf_quality": (
                    extraction_meta.get(
                        "quality",
                        ""
                    )
                ),
                "scp_screen_pdf_quality_reason": (
                    extraction_meta.get(
                        "quality_reason",
                        ""
                    )
                ),
                "scp_screen_pdf_text_chars": (
                    extraction_meta.get(
                        "text_chars",
                        len(
                            text
                        ),
                    )
                ),
                "scp_screen_pdf_page_count": (
                    extraction_meta.get(
                        "page_count",
                        ""
                    )
                ),
                "scp_screen_pdf_pages_with_text": (
                    extraction_meta.get(
                        "pages_with_text",
                        ""
                    )
                ),
                "scp_screen_pdf_fallback_attempted": (
                    "yes"
                    if extraction_meta.get(
                        "fallback_attempted"
                    )
                    else "no"
                ),
                "scp_screen_pdf_fallback_used": (
                    "yes"
                    if extraction_meta.get(
                        "fallback_used"
                    )
                    else "no"
                ),
                "scp_screen_pdf_mupdf_warnings": (
                    text_value(
                        extraction_meta.get(
                            "mupdf_warnings"
                        )
                    )[:4000]
                ),
                "scp_screen_pdf_page_errors_json": json.dumps(
                    extraction_meta.get(
                        "page_errors",
                        [],
                    ),
                    ensure_ascii=False,
                ),
            }
        )

    return {
        "pdf_path": str(pdf_path),
        "cache_hit": cache_hit,
        "error": "",
        "extraction_meta": extraction_meta,
        "rows": row_results,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    manifest_path = Path(
        args.manifest_tsv
    )

    rows = read_tsv(
        manifest_path
    )

    cache_dir = Path(
        args.cache_dir
    )

    print(
        f"Manifest rows: {len(rows):,}"
    )

    print(
        f"Executor: {args.executor}"
    )

    print(
        f"Workers: {args.workers}"
    )

    print(
        f"Text cache: {cache_dir}"
    )

    row_results: dict[
        int,
        dict[str, Any],
    ] = {}

    pdf_groups: dict[
        str,
        list[tuple[int, str]],
    ] = {}

    not_applicable = 0

    for row_index, row in enumerate(
        rows
    ):
        pdf_status = text_value(
            row.get("pdf_status")
        )

        pdf_path_text = text_value(
            row.get("pdf_path")
        )

        if (
            pdf_status
            not in {
                "downloaded",
                "already_exists",
            }
            or not pdf_path_text
        ):
            row_results[
                row_index
            ] = {
                "scp_screen_decision": (
                    "not_applicable"
                ),
                "scp_screen_score": 0,
                "scp_screen_reason_class": (
                    "no_valid_downloaded_pdf"
                ),
                "scp_screen_reason": (
                    "No valid downloaded publication PDF."
                ),
                "scp_screen_evidence": "",
                "scp_screen_publication_has_scp": "",
                "scp_screen_publication_score": 0,
                "scp_screen_publication_evidence_tier": "",
                "scp_screen_title_direct_scp": "",
                "scp_screen_target_pxd_scope": "",
                "scp_screen_target_pxd_evidence": "",
                "scp_screen_other_explicit_scp_pxds": "",
                "scp_screen_positive_hits_json": "[]",
                "scp_screen_negative_hits_json": "[]",
                "scp_screen_low_input_hits_json": "[]",
                "scp_screen_pdf_error": "",
                "scp_screen_pdf_backend": "",
                "scp_screen_pdf_quality": "",
                "scp_screen_pdf_quality_reason": "",
                "scp_screen_pdf_text_chars": "",
                "scp_screen_pdf_page_count": "",
                "scp_screen_pdf_pages_with_text": "",
                "scp_screen_pdf_fallback_attempted": "",
                "scp_screen_pdf_fallback_used": "",
                "scp_screen_pdf_mupdf_warnings": "",
                "scp_screen_pdf_page_errors_json": "[]",
                "scp_screen_algorithm_version": (
                    SCREEN_ALGORITHM_VERSION
                ),
            }

            not_applicable += 1
            continue

        accession = text_value(
            row.get("accession")
        ).upper()

        # Resolve the path for stable grouping where possible. Do not require
        # existence here; worker validation produces screen_error if missing.
        path = Path(
            pdf_path_text
        )

        try:
            group_key = str(
                path.resolve()
            )
        except Exception:
            group_key = str(
                path
            )

        publication_title = text_value(
            row.get(
                "publication_title"
            )
        )

        pdf_groups.setdefault(
            group_key,
            [],
        ).append(
            (
                row_index,
                accession,
                publication_title,
            )
        )

    print(
        "Rows without screenable PDF: "
        f"{not_applicable:,}"
    )

    print(
        "Screenable manifest rows: "
        f"{len(rows) - not_applicable:,}"
    )

    print(
        "Unique PDFs to process: "
        f"{len(pdf_groups):,}"
    )

    duplicate_savings = (
        (len(rows) - not_applicable)
        - len(pdf_groups)
    )

    print(
        "Repeated PDF opens avoided this run: "
        f"{max(0, duplicate_savings):,}"
    )

    tasks = [
        {
            "pdf_path": pdf_path,
            "cache_dir": str(
                cache_dir
            ),
            "refresh_text_cache": (
                args.refresh_text_cache
            ),
            "max_evidence_chars": (
                args.max_evidence_chars
            ),
            "row_targets": targets,
        }
        for pdf_path, targets
        in pdf_groups.items()
    ]

    executor_cls = (
        ProcessPoolExecutor
        if args.executor == "process"
        else ThreadPoolExecutor
    )

    cache_hits = 0
    processed_rows = 0
    screen_errors = 0
    pdf_quality_warnings = 0
    pdf_fallback_attempts = 0
    pdf_fallback_uses = 0

    if tasks:
        with executor_cls(
            max_workers=max(
                1,
                args.workers,
            )
        ) as executor:

            future_map = {
                executor.submit(
                    process_pdf_group,
                    task,
                ): task
                for task in tasks
            }

            completed_pdfs = 0
            total_pdfs = len(
                tasks
            )

            for future in as_completed(
                future_map
            ):
                task = future_map[
                    future
                ]

                try:
                    result = (
                        future.result()
                    )
                except Exception as exc:
                    result = {
                        "pdf_path": task[
                            "pdf_path"
                        ],
                        "cache_hit": False,
                        "error": (
                            f"{type(exc).__name__}: "
                            f"{exc}"
                        ),
                        "rows": {},
                    }

                if result.get(
                    "cache_hit"
                ):
                    cache_hits += 1

                extraction_meta = result.get(
                    "extraction_meta",
                    {},
                )

                if extraction_meta.get(
                    "quality"
                ) in {
                    "warning",
                    "poor",
                    "failed",
                }:
                    pdf_quality_warnings += 1

                if extraction_meta.get(
                    "fallback_attempted"
                ):
                    pdf_fallback_attempts += 1

                if extraction_meta.get(
                    "fallback_used"
                ):
                    pdf_fallback_uses += 1

                error = result.get(
                    "error",
                    "",
                )

                if error:
                    screen_errors += 1

                    for (
                        row_index,
                        _accession,
                        _publication_title,
                    ) in task[
                        "row_targets"
                    ]:
                        row_results[
                            row_index
                        ] = {
                            "scp_screen_decision": (
                                "screen_error"
                            ),
                            "scp_screen_score": 0,
                            "scp_screen_reason_class": (
                                "pdf_screen_error"
                            ),
                            "scp_screen_reason": (
                                "Could not extract/process "
                                "publication PDF."
                            ),
                            "scp_screen_evidence": "",
                            "scp_screen_publication_has_scp": "",
                            "scp_screen_publication_score": 0,
                            "scp_screen_target_pxd_scope": "",
                            "scp_screen_target_pxd_evidence": "",
                            "scp_screen_other_explicit_scp_pxds": "",
                            "scp_screen_positive_hits_json": "[]",
                            "scp_screen_negative_hits_json": "[]",
                            "scp_screen_low_input_hits_json": "[]",
                            "scp_screen_pdf_error": error,
                            "scp_screen_pdf_backend": text_value(
                                result.get(
                                    "extraction_meta",
                                    {},
                                ).get(
                                    "backend",
                                    ""
                                )
                            ),
                            "scp_screen_pdf_quality": text_value(
                                result.get(
                                    "extraction_meta",
                                    {},
                                ).get(
                                    "quality",
                                    ""
                                )
                            ),
                            "scp_screen_pdf_quality_reason": text_value(
                                result.get(
                                    "extraction_meta",
                                    {},
                                ).get(
                                    "quality_reason",
                                    ""
                                )
                            ),
                            "scp_screen_pdf_text_chars": result.get(
                                "extraction_meta",
                                {},
                            ).get(
                                "text_chars",
                                "",
                            ),
                            "scp_screen_pdf_page_count": result.get(
                                "extraction_meta",
                                {},
                            ).get(
                                "page_count",
                                "",
                            ),
                            "scp_screen_pdf_pages_with_text": result.get(
                                "extraction_meta",
                                {},
                            ).get(
                                "pages_with_text",
                                "",
                            ),
                            "scp_screen_pdf_fallback_attempted": (
                                "yes"
                                if result.get(
                                    "extraction_meta",
                                    {},
                                ).get(
                                    "fallback_attempted"
                                )
                                else "no"
                            ),
                            "scp_screen_pdf_fallback_used": (
                                "yes"
                                if result.get(
                                    "extraction_meta",
                                    {},
                                ).get(
                                    "fallback_used"
                                )
                                else "no"
                            ),
                            "scp_screen_pdf_mupdf_warnings": text_value(
                                result.get(
                                    "extraction_meta",
                                    {},
                                ).get(
                                    "mupdf_warnings",
                                    ""
                                )
                            )[:4000],
                            "scp_screen_pdf_page_errors_json": json.dumps(
                                result.get(
                                    "extraction_meta",
                                    {},
                                ).get(
                                    "page_errors",
                                    [],
                                ),
                                ensure_ascii=False,
                            ),
                            "scp_screen_algorithm_version": (
                                SCREEN_ALGORITHM_VERSION
                            ),
                        }

                        processed_rows += 1

                else:
                    for (
                        row_index,
                        row_result,
                    ) in result[
                        "rows"
                    ].items():
                        row_results[
                            int(row_index)
                        ] = row_result

                        processed_rows += 1

                completed_pdfs += 1

                if (
                    completed_pdfs % 100 == 0
                    or completed_pdfs
                    == total_pdfs
                ):
                    print(
                        "Processed PDFs "
                        f"{completed_pdfs:,}/"
                        f"{total_pdfs:,} "
                        "| screenable rows "
                        f"{processed_rows:,}/"
                        f"{len(rows) - not_applicable:,} "
                        "| text-cache hits "
                        f"{cache_hits:,}"
                    )

    output_rows = []

    counts: dict[
        str,
        int,
    ] = {}

    reason_counts: dict[
        str,
        int,
    ] = {}

    extra_fields = [
        "scp_screen_decision",
        "scp_screen_score",
        "scp_screen_reason_class",
        "scp_screen_reason",
        "scp_screen_evidence",
        "scp_screen_publication_has_scp",
        "scp_screen_publication_score",
        "scp_screen_publication_evidence_tier",
        "scp_screen_title_direct_scp",
        "scp_screen_target_pxd_scope",
        "scp_screen_target_pxd_evidence",
        "scp_screen_other_explicit_scp_pxds",
        "scp_screen_positive_hits_json",
        "scp_screen_negative_hits_json",
        "scp_screen_low_input_hits_json",
        "scp_screen_pdf_error",
        "scp_screen_pdf_backend",
        "scp_screen_pdf_quality",
        "scp_screen_pdf_quality_reason",
        "scp_screen_pdf_text_chars",
        "scp_screen_pdf_page_count",
        "scp_screen_pdf_pages_with_text",
        "scp_screen_pdf_fallback_attempted",
        "scp_screen_pdf_fallback_used",
        "scp_screen_pdf_mupdf_warnings",
        "scp_screen_pdf_page_errors_json",
        "scp_screen_algorithm_version",
    ]

    for row_index, row in enumerate(
        rows
    ):
        out = dict(
            row
        )

        result = row_results.get(
            row_index
        )

        if result is None:
            result = {
                "scp_screen_decision": (
                    "screen_error"
                ),
                "scp_screen_score": 0,
                "scp_screen_reason_class": (
                    "internal_missing_screen_result"
                ),
                "scp_screen_reason": (
                    "No screening result was produced "
                    "for this row."
                ),
                "scp_screen_evidence": "",
                "scp_screen_publication_has_scp": "",
                "scp_screen_publication_score": 0,
                "scp_screen_publication_evidence_tier": "",
                "scp_screen_title_direct_scp": "",
                "scp_screen_target_pxd_scope": "",
                "scp_screen_target_pxd_evidence": "",
                "scp_screen_other_explicit_scp_pxds": "",
                "scp_screen_positive_hits_json": "[]",
                "scp_screen_negative_hits_json": "[]",
                "scp_screen_low_input_hits_json": "[]",
                "scp_screen_pdf_error": (
                    "internal_missing_screen_result"
                ),
                "scp_screen_pdf_backend": "",
                "scp_screen_pdf_quality": "",
                "scp_screen_pdf_quality_reason": "",
                "scp_screen_pdf_text_chars": "",
                "scp_screen_pdf_page_count": "",
                "scp_screen_pdf_pages_with_text": "",
                "scp_screen_pdf_fallback_attempted": "",
                "scp_screen_pdf_fallback_used": "",
                "scp_screen_pdf_mupdf_warnings": "",
                "scp_screen_pdf_page_errors_json": "[]",
                "scp_screen_algorithm_version": (
                    SCREEN_ALGORITHM_VERSION
                ),
            }

        out.update(
            result
        )

        decision = out[
            "scp_screen_decision"
        ]

        reason_class = out[
            "scp_screen_reason_class"
        ]

        counts[
            decision
        ] = (
            counts.get(
                decision,
                0,
            )
            + 1
        )

        reason_counts[
            reason_class
        ] = (
            reason_counts.get(
                reason_class,
                0,
            )
            + 1
        )

        output_rows.append(
            out
        )

    fieldnames = (
        list(rows[0].keys())
        if rows
        else []
    )

    fieldnames.extend(
        field
        for field in extra_fields
        if field not in fieldnames
    )

    write_tsv(
        Path(
            args.output
        ),
        output_rows,
        fieldnames,
    )

    print(
        f"Saved: {args.output}"
    )

    print(
        "\nScreen decisions:"
    )

    for decision, count in sorted(
        counts.items()
    ):
        print(
            f"{decision:18s} "
            f"{count:8,d}"
        )

    print(
        "\nReason classes:"
    )

    for reason_class, count in sorted(
        reason_counts.items(),
        key=lambda item: (
            -item[1],
            item[0],
        ),
    ):
        print(
            f"{reason_class:48s} "
            f"{count:8,d}"
        )

    print(
        "\nPerformance summary:"
    )

    print(
        f"  unique PDFs processed: {len(pdf_groups):,}"
    )

    print(
        f"  repeated PDF opens avoided: "
        f"{max(0, duplicate_savings):,}"
    )

    print(
        f"  extracted-text cache hits: {cache_hits:,}"
    )

    print(
        f"  PDF processing errors: {screen_errors:,}"
    )

    print(
        f"  PDF quality warnings: {pdf_quality_warnings:,}"
    )

    print(
        f"  pdftotext fallback attempts: {pdf_fallback_attempts:,}"
    )

    print(
        f"  pdftotext fallbacks used: {pdf_fallback_uses:,}"
    )


if __name__ == "__main__":
    main()
