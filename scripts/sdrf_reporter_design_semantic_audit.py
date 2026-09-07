#!/usr/bin/env python3
"""Non-generative semantic audit of deposited reporter experimental-design workbooks.

v0.4.3a established that PXD028040 has a public experimental-design workbook but that exact RAW
basenames are not the workbook's primary linkage scheme.  This audit preserves workbook cell/column
structure and asks a narrower question: can deposited RAW acquisitions be linked to workbook rows by
explicit structured repository identifiers such as acquisition date + SC run code?

No SDRF rows are generated.  Filename words such as TMT or single_neuron are diagnostics only and
never create biological identity.  A structured candidate is emitted only when explicit identifiers
from the deposited RAW basename are also present in the deposited workbook row; ambiguity remains
ambiguity.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import re
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
import xml.etree.ElementTree as ET

# Reuse repository parsing and reporter-contract logic from the already accepted v0.4.3a audit.
from sdrf_reporter_run_scope_audit import (
    RepoFile,
    is_raw_file,
    load_reporter_contract,
    repository_files,
    raw_stem,
)

AUDITOR_VERSION = "pride-scp-sdrf-design-semantic-auditor-v0.1"

DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-_/](\d{2})[-_/](\d{2})(?!\d)")
SC_RE = re.compile(r"(?i)(?<![A-Za-z0-9])SC\s*[-_ ]?0*(\d{1,3})(?!\d)")
TMT_RE = re.compile(r"(?i)(?<![A-Za-z0-9])TMT(?:pro)?(?![A-Za-z0-9])")
SINGLE_RE = re.compile(
    r"(?i)(?:single\s*[-_ ]?(?:neuron|cell|soma)|individual\s+(?:neuron|cell)|"
    r"somal\s+aspirate|dopaminergic\s+neuron|\bDA\s+neuron)"
)
SAMPLE_RE = re.compile(r"(?i)\b(?:sample|neuron|cell|soma|aspirate|biological\s+replicate)\b")
REPLICATE_RE = re.compile(r"(?i)\b(?:biological|technical)?\s*(?:replicate|rep)\s*[-_ ]?[A-Za-z0-9]+\b")
REPORTER_128_RE = re.compile(r"(?i)(?:TMT\s*[-_ ]?)?128\b")
REPORTER_131_RE = re.compile(r"(?i)(?:TMT\s*[-_ ]?)?131(?:[NC])?\b")
ANALYTE_RE = re.compile(r"(?i)\b(?:analyte|analytical|single[- ]?cell|single[- ]?neuron)\b")
CARRIER_RE = re.compile(r"(?i)\bcarrier\b")
HEADER_RE = re.compile(
    r"(?i)\b(?:raw|file|run|sample|cell|neuron|date|experiment|replicate|tmt|channel|"
    r"carrier|analyte|analytical|label|condition|description|id)\b"
)


@dataclass
class Cell:
    ref: str
    column: str
    value: str
    raw_value: str
    style_index: int | None


@dataclass
class DesignRow:
    sheet: str
    row_number: int
    cells: list[Cell]

    @property
    def values(self) -> list[str]:
        return [c.value for c in self.cells if c.value.strip()]

    @property
    def text(self) -> str:
        return " | ".join(self.values)

    @property
    def cell_map(self) -> dict[str, str]:
        return {c.column: c.value for c in self.cells if c.value.strip()}


@dataclass
class RowFeatures:
    dates: list[str]
    sc_codes: list[str]
    has_tmt: bool
    has_single_branch: bool
    has_sample_semantics: bool
    replicate_terms: list[str]
    has_128: bool
    has_131: bool
    has_analyte_role: bool
    has_carrier_role: bool


@dataclass
class RawCandidate:
    raw_file: str
    raw_date: str
    raw_sc_code: str
    filename_tmt: bool
    filename_single_branch: bool
    match_type: str
    confidence: str
    unique: bool
    candidate_count: int
    support_refs: list[str]
    support_rows: list[str]
    explicit_row_tmt: bool
    explicit_row_single_branch: bool
    explicit_row_sample_semantics: bool
    explicit_row_128: bool
    explicit_row_131: bool
    explicit_row_analyte_role: bool
    explicit_row_carrier_role: bool


BUILTIN_DATE_FORMAT_IDS = {
    14, 15, 16, 17, 18, 19, 20, 21, 22,
    27, 30, 36, 45, 46, 47, 50, 57,
}


def _text(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def excel_col(ref: str) -> str:
    m = re.match(r"([A-Za-z]+)", ref)
    return m.group(1).upper() if m else ""


def excel_serial_to_iso(value: str) -> str:
    try:
        serial = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(serial) or serial < 1 or serial > 80000:
        return ""
    # Excel's 1900 date system includes the fictitious 1900-02-29; 1899-12-30 reproduces Excel.
    day = dt.datetime(1899, 12, 30) + dt.timedelta(days=serial)
    if day.year < 2000 or day.year > 2100:
        return ""
    return day.date().isoformat()


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    name = "xl/sharedStrings.xml"
    if name not in zf.namelist():
        return []
    root = ET.fromstring(zf.read(name))
    out: list[str] = []
    for si in root.findall("{*}si"):
        out.append("".join(t.text or "" for t in si.findall(".//{*}t")))
    return out


def _xlsx_sheet_targets(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    names = set(zf.namelist())
    workbook = "xl/workbook.xml"
    rels = "xl/_rels/workbook.xml.rels"
    if workbook not in names or rels not in names:
        sheets = sorted(x for x in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", x))
        return [(Path(x).stem, x) for x in sheets]
    rel_root = ET.fromstring(zf.read(rels))
    rel_map: dict[str, str] = {}
    for rel in rel_root.findall("{*}Relationship"):
        rid = rel.attrib.get("Id", "")
        target = rel.attrib.get("Target", "")
        if not rid or not target:
            continue
        if target.startswith("/"):
            path = target.lstrip("/")
        elif target.startswith("xl/"):
            path = target
        else:
            path = "xl/" + target.lstrip("./")
        rel_map[rid] = path
    wb_root = ET.fromstring(zf.read(workbook))
    out: list[tuple[str, str]] = []
    rid_key = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    for sheet in wb_root.findall(".//{*}sheet"):
        name = sheet.attrib.get("name", "sheet")
        target = rel_map.get(sheet.attrib.get(rid_key, ""))
        if target in names:
            out.append((name, target))
    return out


def _xlsx_date_styles(zf: zipfile.ZipFile) -> set[int]:
    if "xl/styles.xml" not in zf.namelist():
        return set()
    root = ET.fromstring(zf.read("xl/styles.xml"))
    custom_date_ids: set[int] = set()
    for fmt in root.findall(".//{*}numFmts/{*}numFmt"):
        try:
            fmt_id = int(fmt.attrib.get("numFmtId", "-1"))
        except ValueError:
            continue
        code = fmt.attrib.get("formatCode", "")
        # Remove quoted literals and bracketed conditions before checking date/time tokens.
        scrub = re.sub(r'"[^"]*"', "", code)
        scrub = re.sub(r"\[[^\]]*\]", "", scrub)
        if re.search(r"(?i)(?:y{2,4}|m{1,4}|d{1,4})", scrub):
            custom_date_ids.add(fmt_id)
    out: set[int] = set()
    cell_xfs = root.find("{*}cellXfs")
    if cell_xfs is None:
        return out
    for idx, xf in enumerate(cell_xfs.findall("{*}xf")):
        try:
            fmt_id = int(xf.attrib.get("numFmtId", "0"))
        except ValueError:
            fmt_id = 0
        if fmt_id in BUILTIN_DATE_FORMAT_IDS or fmt_id in custom_date_ids:
            out.add(idx)
    return out


def parse_xlsx_structured(path: Path) -> list[DesignRow]:
    rows: list[DesignRow] = []
    with zipfile.ZipFile(path) as zf:
        shared = _xlsx_shared_strings(zf)
        date_styles = _xlsx_date_styles(zf)
        for sheet_name, target in _xlsx_sheet_targets(zf):
            root = ET.fromstring(zf.read(target))
            for r in root.findall(".//{*}sheetData/{*}row"):
                row_number = int(r.attrib.get("r") or len(rows) + 1)
                cells: list[Cell] = []
                for c in r.findall("{*}c"):
                    ref = c.attrib.get("r", "")
                    ctype = c.attrib.get("t", "")
                    style: int | None = None
                    if c.attrib.get("s", "").isdigit():
                        style = int(c.attrib["s"])
                    raw = ""
                    value = ""
                    if ctype == "inlineStr":
                        raw = "".join(t.text or "" for t in c.findall(".//{*}t"))
                        value = raw
                    else:
                        v = c.find("{*}v")
                        raw = v.text if v is not None and v.text is not None else ""
                        if ctype == "s" and raw.isdigit():
                            idx = int(raw)
                            value = shared[idx] if 0 <= idx < len(shared) else raw
                        elif ctype == "b":
                            value = "TRUE" if raw == "1" else "FALSE"
                        elif style is not None and style in date_styles:
                            value = excel_serial_to_iso(raw) or raw
                        else:
                            value = raw
                    cells.append(Cell(ref=ref, column=excel_col(ref), value=_text(value), raw_value=_text(raw), style_index=style))
                if any(c.value for c in cells):
                    rows.append(DesignRow(sheet=sheet_name, row_number=row_number, cells=cells))
    return rows


def normalize_date_tokens(text: str) -> list[str]:
    out: list[str] = []
    for m in DATE_RE.finditer(text):
        candidate = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        try:
            dt.date.fromisoformat(candidate)
        except ValueError:
            continue
        out.append(candidate)
    return sorted(set(out))


def normalize_sc_tokens(text: str) -> list[str]:
    return sorted({f"SC{int(m.group(1)):02d}" for m in SC_RE.finditer(text)})


def features(text: str) -> RowFeatures:
    return RowFeatures(
        dates=normalize_date_tokens(text),
        sc_codes=normalize_sc_tokens(text),
        has_tmt=bool(TMT_RE.search(text)),
        has_single_branch=bool(SINGLE_RE.search(text)),
        has_sample_semantics=bool(SAMPLE_RE.search(text)),
        replicate_terms=sorted({m.group(0) for m in REPLICATE_RE.finditer(text)}, key=str.lower),
        has_128=bool(REPORTER_128_RE.search(text)),
        has_131=bool(REPORTER_131_RE.search(text)),
        has_analyte_role=bool(ANALYTE_RE.search(text)),
        has_carrier_role=bool(CARRIER_RE.search(text)),
    )


def raw_features(name: str) -> RowFeatures:
    return features(raw_stem(name).replace("_", " "))


def row_ref(row: DesignRow) -> str:
    return f"{row.sheet}:row{row.row_number}"


def candidate_rows_for_raw(raw: RepoFile, rows: list[DesignRow]) -> tuple[str, str, list[DesignRow]]:
    rf = raw_features(raw.name)
    exact = [r for r in rows if Path(raw.name).name.lower() in r.text.lower() or raw_stem(raw.name) in r.text.lower()]
    if exact:
        return "exact_raw_name", "high", exact

    # A date + SC identifier is accepted as a structured *run-key candidate* only when both sides
    # are explicit.  It does not establish biological identity by itself.
    if len(rf.dates) == 1 and len(rf.sc_codes) == 1:
        date = rf.dates[0]
        sc = rf.sc_codes[0]
        matches: list[DesignRow] = []
        for r in rows:
            ff = features(r.text)
            if date in ff.dates and sc in ff.sc_codes:
                matches.append(r)
        if matches:
            return "date_sc_run_key", "high" if len(matches) == 1 else "ambiguous", matches

    return "none", "none", []


def detect_header_candidates(rows: list[DesignRow]) -> list[dict[str, object]]:
    by_sheet: dict[str, list[DesignRow]] = {}
    for row in rows:
        by_sheet.setdefault(row.sheet, []).append(row)
    out: list[dict[str, object]] = []
    for sheet, sheet_rows in by_sheet.items():
        scored: list[tuple[int, DesignRow]] = []
        for row in sheet_rows[:15]:
            score = len({m.group(0).lower() for m in HEADER_RE.finditer(row.text)})
            if score:
                scored.append((score, row))
        if scored:
            score, row = sorted(scored, key=lambda x: (-x[0], x[1].row_number))[0]
            out.append({
                "sheet": sheet,
                "row_number": row.row_number,
                "score": score,
                "cells": row.cell_map,
                "text": row.text,
            })
        else:
            out.append({"sheet": sheet, "row_number": None, "score": 0, "cells": {}, "text": ""})
    return out


def context_rows(target: DesignRow, all_rows: list[DesignRow], radius: int = 1) -> list[dict[str, object]]:
    same = [r for r in all_rows if r.sheet == target.sheet]
    idx = next((i for i, r in enumerate(same) if r.row_number == target.row_number), None)
    if idx is None:
        return []
    out: list[dict[str, object]] = []
    for r in same[max(0, idx - radius): min(len(same), idx + radius + 1)]:
        out.append({"sheet": r.sheet, "row_number": r.row_number, "cells": r.cell_map, "text": r.text})
    return out


def write_rows(path: Path, rows: list[DesignRow]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow([
            "sheet", "row_number", "row_text", "cells_json", "dates", "sc_codes", "has_tmt",
            "has_single_branch", "has_sample_semantics", "replicate_terms", "has_128", "has_131",
            "has_analyte_role", "has_carrier_role",
        ])
        for r in rows:
            f = features(r.text)
            w.writerow([
                r.sheet, r.row_number, r.text, json.dumps(r.cell_map, ensure_ascii=False),
                ";".join(f.dates), ";".join(f.sc_codes), str(f.has_tmt).lower(),
                str(f.has_single_branch).lower(), str(f.has_sample_semantics).lower(),
                ";".join(f.replicate_terms), str(f.has_128).lower(), str(f.has_131).lower(),
                str(f.has_analyte_role).lower(), str(f.has_carrier_role).lower(),
            ])


def write_candidates(path: Path, candidates: list[RawCandidate]) -> None:
    fields = list(RawCandidate.__dataclass_fields__)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, delimiter="\t")
        w.writeheader()
        for row in candidates:
            d = asdict(row)
            d["support_refs"] = ";".join(d["support_refs"])
            d["support_rows"] = " || ".join(d["support_rows"])
            w.writerow(d)


def audit(args: argparse.Namespace) -> dict[str, object]:
    accession = args.accession.strip().upper()
    repo = repository_files(json.loads(args.files_json.read_text(errors="replace")))
    raws = [x for x in repo if is_raw_file(x)]
    rows = parse_xlsx_structured(args.support_xlsx)
    headers = detect_header_candidates(rows)

    reporter = load_reporter_contract(args.reporter_audit_tsv, accession)
    reporter_ok = (
        reporter.get("mapping_class") == "single_analytical_channel_per_run"
        and reporter.get("confidence") == "high"
        and reporter.get("single_cell_channels") == "128"
        and reporter.get("carrier_channels") == "131"
        and not reporter.get("ambiguous_channels")
    )

    candidates: list[RawCandidate] = []
    context_payload: dict[str, object] = {}
    for raw in raws:
        rf = raw_features(raw.name)
        match_type, confidence, matched = candidate_rows_for_raw(raw, rows)
        combined = " ".join(r.text for r in matched)
        mf = features(combined)
        unique = len(matched) == 1
        candidates.append(RawCandidate(
            raw_file=raw.name,
            raw_date=rf.dates[0] if len(rf.dates) == 1 else ";".join(rf.dates),
            raw_sc_code=rf.sc_codes[0] if len(rf.sc_codes) == 1 else ";".join(rf.sc_codes),
            filename_tmt=rf.has_tmt,
            filename_single_branch=rf.has_single_branch,
            match_type=match_type,
            confidence=confidence,
            unique=unique,
            candidate_count=len(matched),
            support_refs=[row_ref(r) for r in matched],
            support_rows=[r.text for r in matched],
            explicit_row_tmt=mf.has_tmt,
            explicit_row_single_branch=mf.has_single_branch,
            explicit_row_sample_semantics=mf.has_sample_semantics,
            explicit_row_128=mf.has_128,
            explicit_row_131=mf.has_131,
            explicit_row_analyte_role=mf.has_analyte_role,
            explicit_row_carrier_role=mf.has_carrier_role,
        ))
        if matched:
            context_payload[raw.name] = [
                {
                    "candidate": {"sheet": r.sheet, "row_number": r.row_number, "cells": r.cell_map, "text": r.text},
                    "context": context_rows(r, rows),
                }
                for r in matched
            ]

    tmt_single = [x for x in candidates if x.filename_tmt and x.filename_single_branch]
    unique_structured = [x for x in tmt_single if x.unique and x.match_type in {"exact_raw_name", "date_sc_run_key"}]
    with_single_semantics = [x for x in unique_structured if x.explicit_row_single_branch or x.explicit_row_sample_semantics]
    with_tmt_semantics = [x for x in unique_structured if x.explicit_row_tmt]
    with_reporter_layout = [
        x for x in unique_structured
        if x.explicit_row_128 and x.explicit_row_131 and x.explicit_row_analyte_role and x.explicit_row_carrier_role
    ]

    if not reporter_ok:
        status = "reporter_contract_not_met"
    elif not rows:
        status = "support_workbook_empty_or_unparseable"
    elif not tmt_single:
        status = "no_repository_tmt_single_branch_raws"
    elif len(unique_structured) == len(tmt_single):
        status = "all_tmt_single_runs_have_unique_structured_design_candidates"
    elif unique_structured:
        status = "partial_tmt_single_structured_design_linkage"
    else:
        status = "no_tmt_single_structured_design_linkage"

    args.output.mkdir(parents=True, exist_ok=True)
    write_rows(args.output / "design_rows_structured.tsv", rows)
    write_candidates(args.output / "raw_design_candidates.tsv", candidates)
    (args.output / "header_candidates.json").write_text(json.dumps(headers, indent=2, ensure_ascii=False))
    (args.output / "candidate_row_contexts.json").write_text(json.dumps(context_payload, indent=2, ensure_ascii=False))

    summary: dict[str, object] = {
        "auditor_version": AUDITOR_VERSION,
        "accession": accession,
        "status": status,
        "non_generative": True,
        "reporter_contract_valid": reporter_ok,
        "support_file": str(args.support_xlsx),
        "sheets": sorted({r.sheet for r in rows}),
        "design_rows": len(rows),
        "raw_files": len(raws),
        "tmt_single_branch_raws": len(tmt_single),
        "tmt_single_unique_structured_candidates": len(unique_structured),
        "tmt_single_unique_candidates_with_explicit_sample_or_single_semantics": len(with_single_semantics),
        "tmt_single_unique_candidates_with_explicit_tmt_semantics": len(with_tmt_semantics),
        "tmt_single_unique_candidates_with_explicit_128_131_role_layout": len(with_reporter_layout),
        "generator_gate": "manual_candidate_row_review_required",
        "linkage_contract": {
            "allowed_candidate_keys": ["exact_raw_name", "unique_date_plus_sc_code"],
            "filename_tmt_or_single_terms_create_linkage": False,
            "structured_run_key_establishes_biological_identity": False,
            "generation_authorized": False,
        },
        "notes": [
            "A unique date+SC match is a source-grounded run-row candidate, not a biological sample identity by itself.",
            "TMT/single-neuron words in a RAW filename are diagnostic only and never create a workbook linkage.",
            "Multiple workbook rows with the same date+SC key remain ambiguous and are never auto-selected.",
            "Reporter-row generation remains blocked until candidate rows expose explicit sample/replicate semantics for the relevant acquisitions.",
        ],
        "outputs": {
            "design_rows_structured": str(args.output / "design_rows_structured.tsv"),
            "raw_design_candidates": str(args.output / "raw_design_candidates.tsv"),
            "header_candidates": str(args.output / "header_candidates.json"),
            "candidate_row_contexts": str(args.output / "candidate_row_contexts.json"),
        },
    }
    (args.output / "sdrf_reporter_design_semantic_audit_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def _minimal_xlsx(path: Path) -> None:
    # Synthetic workbook: one run is linked indirectly by date+SC, another is deliberately ambiguous.
    shared = [
        "Date", "Run", "Sample", "Experiment",
        "2018-08-27", "SC03", "DA neuron 1", "TMT single neuron",
        "2018-09-05", "SC02", "DA neuron 2", "TMT single neuron",
        "DA neuron duplicate",
    ]
    si = "".join(f"<si><t>{x}</t></si>" for x in shared)
    shared_xml = f'<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(shared)}" uniqueCount="{len(shared)}">{si}</sst>'
    sheet_xml = '''<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
      <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c><c r="D1" t="s"><v>3</v></c></row>
      <row r="2"><c r="A2" t="s"><v>4</v></c><c r="B2" t="s"><v>5</v></c><c r="C2" t="s"><v>6</v></c><c r="D2" t="s"><v>7</v></c></row>
      <row r="3"><c r="A3" t="s"><v>8</v></c><c r="B3" t="s"><v>9</v></c><c r="C3" t="s"><v>10</v></c><c r="D3" t="s"><v>7</v></c></row>
      <row r="4"><c r="A4" t="s"><v>8</v></c><c r="B4" t="s"><v>9</v></c><c r="C4" t="s"><v>6</v></c><c r="D4" t="s"><v>7</v></c></row>
    </sheetData></worksheet>'''
    workbook_xml = '''<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Design" sheetId="1" r:id="rId1"/></sheets></workbook>'''
    rels_xml = '''<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'''
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/sharedStrings.xml", shared_xml)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", rels_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def self_test() -> None:
    assert excel_serial_to_iso("43339") == "2018-08-27"
    assert normalize_sc_tokens("SC3 sc03 SC004") == ["SC03", "SC04"]
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "design.xlsx"
        _minimal_xlsx(path)
        rows = parse_xlsx_structured(path)
        assert len(rows) == 4, rows
        assert detect_header_candidates(rows)[0]["row_number"] == 1

        raw = RepoFile("2018-08-27_SC03_tmt_single_neurons.RAW", "RAW", "")
        kind, confidence, matched = candidate_rows_for_raw(raw, rows)
        assert kind == "date_sc_run_key", (kind, matched)
        assert confidence == "high"
        assert len(matched) == 1
        assert features(matched[0].text).has_tmt
        assert features(matched[0].text).has_single_branch

        ambiguous = RepoFile("2018-09-05_SC02_10_nl_TMT_single_neuron.RAW", "RAW", "")
        kind, confidence, matched = candidate_rows_for_raw(ambiguous, rows)
        assert kind == "date_sc_run_key"
        assert confidence == "ambiguous"
        assert len(matched) == 2

        # SC alone can never create a candidate because it is not unique enough as a source key.
        sc_only = RepoFile("SC03_tmt_single_neurons.RAW", "RAW", "")
        kind, confidence, matched = candidate_rows_for_raw(sc_only, rows)
        assert kind == "none" and confidence == "none" and not matched
    print("sdrf_reporter_design_semantic_audit self-test: PASS")


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
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
