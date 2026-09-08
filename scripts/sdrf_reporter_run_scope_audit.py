#!/usr/bin/env python3
"""Non-generative run/file-scope audit for narrow reporter-channel SDRF candidates.

The reporter-role audit can prove that a study uses an analytical reporter channel plus a carrier,
but that is not enough to write SDRF rows.  This helper asks the next question: which deposited RAW
files and biological samples does that layout actually describe?

The audit is deliberately evidence-first.  It reads the repository file snapshot, downloads only
small design/metadata support files when an explicit public URI is present, parses tabular/XLSX
content without external spreadsheet dependencies, and cross-references exact RAW basenames/stems.
It never writes SDRF rows and never uses GT metadata as field truth.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

AUDITOR_VERSION = "pride-scp-sdrf-run-scope-auditor-v0.1"

RAW_EXT_RE = re.compile(r"(?i)(?:\.raw|\.d|\.wiff|\.wiff\.scan|\.mzml)$")
SUPPORT_EXTENSIONS = {".xlsx", ".csv", ".tsv", ".txt", ".json"}
SUPPORT_NAME_RE = re.compile(
    r"(?i)(?:experimental[_ -]?design|experiment[_ -]?design|metadata|sample[_ -]?(?:map|mapping|sheet)|"
    r"run[_ -]?(?:map|mapping)|file[_ -]?(?:map|mapping)|annotation|design)"
)
SINGLE_BRANCH_RE = re.compile(
    r"(?i)\b(?:single[- _]?(?:cell|neuron)|single[- _]?som(?:a|al)|somal\s+aspirate|"
    r"dopaminergic\s+neurons?|DA\s+neurons?|individual\s+neurons?)\b"
)
TMT_RE = re.compile(r"(?i)\b(?:TMT(?:pro)?|tandem\s+mass\s+tag)\b")
SCOPE_RE = re.compile(
    r"(?i)\b(?:from\s+each\s+aspirate|each\s+aspirate|each\s+sample|all\s+(?:the\s+)?samples|"
    r"each\s+(?:biological\s+)?replicate|biological\s+triplicates?|individual\s+neurons?|"
    r"single\s+neuronal\s+somas?)\b"
)
REPORTER_128_RE = re.compile(r"(?i)\bTMT\s*[-_]?\s*128\b")
REPORTER_131_RE = re.compile(r"(?i)\bTMT\s*[-_]?\s*131(?:[NC])?\b")


@dataclass
class RepoFile:
    name: str
    category: str
    uri: str


@dataclass
class SupportRow:
    support_file: str
    sheet: str
    row_number: int
    cells: list[str]

    @property
    def text(self) -> str:
        return " | ".join(x for x in self.cells if x.strip())


@dataclass
class RawMatch:
    raw_file: str
    category: str
    file_uri: str
    filename_tmt: bool
    filename_single_branch: bool
    support_match_count: int
    support_refs: list[str]
    support_row_texts: list[str]


def _text(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _file_name_from_obj(obj: dict) -> str:
    for key in ("fileName", "filename", "name"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _file_category_from_obj(obj: dict) -> str:
    for key in ("fileCategory", "category"):
        value = obj.get(key)
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for inner in ("value", "name"):
                inner_value = value.get(inner)
                if isinstance(inner_value, str) and inner_value.strip():
                    return inner_value.strip()
    return ""


def _file_uri_from_obj(obj: dict) -> str:
    locations = obj.get("publicFileLocations")
    if isinstance(locations, list):
        fallback = ""
        for loc in locations:
            if not isinstance(loc, dict):
                continue
            value = loc.get("value")
            if not isinstance(value, str) or not value.strip():
                continue
            label = str(loc.get("name") or "").lower()
            if "ftp" in label or "http" in label:
                return value.strip()
            if not fallback:
                fallback = value.strip()
        if fallback:
            return fallback
    for key in ("downloadLink", "downloadUrl", "url"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def repository_files(value: object) -> list[RepoFile]:
    found: dict[str, RepoFile] = {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            name = _file_name_from_obj(node)
            if name:
                found.setdefault(
                    name,
                    RepoFile(name=name, category=_file_category_from_obj(node), uri=_file_uri_from_obj(node)),
                )
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return sorted(found.values(), key=lambda x: x.name.lower())


def is_raw_file(row: RepoFile) -> bool:
    return row.category.upper() == "RAW" or bool(RAW_EXT_RE.search(row.name))


def is_support_candidate(row: RepoFile) -> bool:
    ext = Path(row.name).suffix.lower()
    return ext in SUPPORT_EXTENSIONS and bool(SUPPORT_NAME_RE.search(row.name))


def public_http_uri(uri: str) -> str:
    uri = uri.strip()
    if uri.startswith("ftp://ftp.pride.ebi.ac.uk/"):
        return "https://ftp.pride.ebi.ac.uk/" + uri.split("ftp://ftp.pride.ebi.ac.uk/", 1)[1]
    return uri


def download_support_file(row: RepoFile, dest_dir: Path, max_bytes: int = 25 * 1024 * 1024) -> tuple[Path | None, str]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(row.name).name)
    target = dest_dir / safe
    if target.is_file() and target.stat().st_size > 0:
        return target, "cached"
    if not row.uri:
        return None, "missing_public_uri"
    uri = public_http_uri(row.uri)
    if not uri.startswith(("http://", "https://")):
        return None, f"unsupported_uri:{urlparse(uri).scheme or 'unknown'}"
    try:
        import requests
    except Exception as exc:  # pragma: no cover - project requirements include requests
        return None, f"requests_unavailable:{exc}"
    try:
        with requests.get(uri, stream=True, timeout=(20, 90)) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared and int(declared) > max_bytes:
                return None, f"too_large:{declared}"
            total = 0
            with target.open("wb") as fh:
                for chunk in response.iter_content(chunk_size=1024 * 128):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        fh.close()
                        target.unlink(missing_ok=True)
                        return None, f"too_large_streamed:{total}"
                    fh.write(chunk)
        return target, "downloaded"
    except Exception as exc:
        target.unlink(missing_ok=True)
        return None, f"download_error:{type(exc).__name__}:{exc}"


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    name = "xl/sharedStrings.xml"
    if name not in zf.namelist():
        return []
    root = ET.fromstring(zf.read(name))
    out: list[str] = []
    for si in root.findall("{*}si"):
        parts = [t.text or "" for t in si.findall(".//{*}t")]
        out.append("".join(parts))
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
        if rid and target:
            if target.startswith("/"):
                path = target.lstrip("/")
            elif target.startswith("xl/"):
                path = target
            else:
                path = "xl/" + target.lstrip("./")
            rel_map[rid] = path
    wb_root = ET.fromstring(zf.read(workbook))
    out: list[tuple[str, str]] = []
    for sheet in wb_root.findall(".//{*}sheet"):
        name = sheet.attrib.get("name", "sheet")
        rid = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
        target = rel_map.get(rid)
        if target in names:
            out.append((name, target))
    return out


def parse_xlsx(path: Path) -> list[SupportRow]:
    rows: list[SupportRow] = []
    with zipfile.ZipFile(path) as zf:
        shared = _xlsx_shared_strings(zf)
        for sheet_name, target in _xlsx_sheet_targets(zf):
            root = ET.fromstring(zf.read(target))
            for r in root.findall(".//{*}sheetData/{*}row"):
                row_number = int(r.attrib.get("r") or len(rows) + 1)
                cells: list[str] = []
                for c in r.findall("{*}c"):
                    ctype = c.attrib.get("t", "")
                    value = ""
                    if ctype == "inlineStr":
                        value = "".join(t.text or "" for t in c.findall(".//{*}t"))
                    else:
                        v = c.find("{*}v")
                        raw = v.text if v is not None and v.text is not None else ""
                        if ctype == "s" and raw.isdigit():
                            idx = int(raw)
                            value = shared[idx] if 0 <= idx < len(shared) else raw
                        elif ctype == "b":
                            value = "TRUE" if raw == "1" else "FALSE"
                        else:
                            value = raw
                    cells.append(_text(value))
                if any(cells):
                    rows.append(SupportRow(path.name, sheet_name, row_number, cells))
    return rows


def parse_support_file(path: Path) -> list[SupportRow]:
    ext = path.suffix.lower()
    if ext == ".xlsx":
        return parse_xlsx(path)
    if ext in {".csv", ".tsv", ".txt"}:
        text = path.read_text(errors="replace")
        if ext == ".tsv":
            delimiter = "\t"
        elif ext == ".csv":
            delimiter = ","
        else:
            sample = "\n".join(text.splitlines()[:20])
            try:
                delimiter = csv.Sniffer().sniff(sample, delimiters="\t,;").delimiter
            except Exception:
                delimiter = "\t"
        out: list[SupportRow] = []
        for i, row in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter), start=1):
            cells = [_text(x) for x in row]
            if any(cells):
                out.append(SupportRow(path.name, "text", i, cells))
        return out
    if ext == ".json":
        obj = json.loads(path.read_text(errors="replace"))
        out: list[SupportRow] = []
        def walk(node: object, path_tokens: list[str]) -> None:
            if isinstance(node, dict):
                scalar = [f"{k}={_text(v)}" for k, v in node.items() if not isinstance(v, (dict, list)) and _text(v)]
                if scalar:
                    out.append(SupportRow(path.name, "json", len(out) + 1, path_tokens + scalar))
                for k, v in node.items():
                    if isinstance(v, (dict, list)):
                        walk(v, path_tokens + [str(k)])
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, path_tokens + [str(i)])
        walk(obj, [])
        return out
    return []


def raw_stem(name: str) -> str:
    base = Path(name).name.lower()
    for ext in (".wiff.scan", ".raw", ".wiff", ".mzml", ".d", ".zip"):
        if base.endswith(ext):
            return base[: -len(ext)]
    return Path(base).stem


def row_mentions_raw(row: SupportRow, raw_name: str) -> bool:
    text = row.text.lower()
    base = Path(raw_name).name.lower()
    stem = raw_stem(raw_name)
    if base and base in text:
        return True
    if len(stem) >= 8 and stem in text:
        return True
    return False


def load_reporter_contract(path: Path, accession: str) -> dict[str, str]:
    with path.open(errors="replace") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            if (row.get("accession") or "").strip() == accession:
                return {k: (v or "").strip() for k, v in row.items()}
    return {}


def iter_publication_text(manifest: Path, accession: str) -> Iterator[tuple[str, str]]:
    if not manifest.is_file():
        return
    with manifest.open(errors="replace") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            if (row.get("accession") or "").strip() != accession:
                continue
            p = (row.get("publication_content_text_path") or "").strip()
            if not p:
                continue
            path = Path(p)
            if not path.is_file():
                continue
            label = (row.get("publication_doi") or row.get("doi") or path.name or "publication").strip()
            yield label, path.read_text(errors="replace")


def publication_scope_contexts(texts: Iterable[tuple[str, str]]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for label, text in texts:
        for m128 in REPORTER_128_RE.finditer(text):
            lo = max(0, m128.start() - 500)
            hi = min(len(text), m128.end() + 900)
            window = text[lo:hi]
            if not REPORTER_131_RE.search(window):
                continue
            if not SINGLE_BRANCH_RE.search(window):
                continue
            scopes = sorted({m.group(0) for m in SCOPE_RE.finditer(window)}, key=str.lower)
            if not scopes:
                continue
            out.append({
                "source": label,
                "scope_terms": scopes,
                "has_tmt128": True,
                "has_tmt131": True,
                "has_single_branch": True,
                "text": re.sub(r"\s+", " ", window).strip(),
            })
    # de-duplicate overlapping windows
    dedup: dict[str, dict[str, object]] = {}
    for item in out:
        key = str(item["text"])
        dedup.setdefault(key, item)
    return list(dedup.values())


def write_support_rows(path: Path, rows: list[SupportRow]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["support_file", "sheet", "row_number", "row_text", "cells_json"])
        for r in rows:
            w.writerow([r.support_file, r.sheet, r.row_number, r.text, json.dumps(r.cells, ensure_ascii=False)])


def write_raw_matches(path: Path, rows: list[RawMatch]) -> None:
    fields = [
        "raw_file", "category", "file_uri", "filename_tmt", "filename_single_branch",
        "support_match_count", "support_refs", "support_row_texts",
    ]
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, delimiter="\t")
        w.writeheader()
        for row in rows:
            d = asdict(row)
            d["support_refs"] = ";".join(d["support_refs"])
            d["support_row_texts"] = " || ".join(d["support_row_texts"])
            w.writerow(d)


def audit(args: argparse.Namespace) -> dict[str, object]:
    accession = args.accession.strip().upper()
    files_obj = json.loads(args.files_json.read_text(errors="replace"))
    repo_files = repository_files(files_obj)
    raws = [x for x in repo_files if is_raw_file(x)]
    support_candidates = [x for x in repo_files if is_support_candidate(x)]

    args.output.mkdir(parents=True, exist_ok=True)
    support_dir = args.output / "support_files"
    support_rows: list[SupportRow] = []
    support_status: list[dict[str, str]] = []
    for row in support_candidates:
        local, status = download_support_file(row, support_dir, max_bytes=args.max_support_bytes)
        record = {"file": row.name, "category": row.category, "uri": row.uri, "status": status, "local_path": str(local or "")}
        if local:
            try:
                parsed = parse_support_file(local)
                support_rows.extend(parsed)
                record["rows_parsed"] = str(len(parsed))
            except Exception as exc:
                record["status"] = f"parse_error:{type(exc).__name__}:{exc}"
                record["rows_parsed"] = "0"
        support_status.append(record)

    reporter = load_reporter_contract(args.reporter_audit_tsv, accession)
    reporter_ok = (
        reporter.get("mapping_class") == "single_analytical_channel_per_run"
        and reporter.get("confidence") == "high"
        and reporter.get("single_cell_channels") == "128"
        and reporter.get("carrier_channels") == "131"
        and not reporter.get("ambiguous_channels")
    )

    pub_contexts = publication_scope_contexts(iter_publication_text(args.publication_manifest, accession))

    raw_matches: list[RawMatch] = []
    for raw in raws:
        matching = [r for r in support_rows if row_mentions_raw(r, raw.name)]
        raw_matches.append(RawMatch(
            raw_file=raw.name,
            category=raw.category,
            file_uri=raw.uri,
            filename_tmt=bool(re.search(r"(?i)(?:^|[_ -])tmt(?:pro)?(?:[_ -]|$)", raw.name)),
            filename_single_branch=bool(re.search(r"(?i)(?:single[_ -]?neuron|single[_ -]?cell|single[_ -]?soma)", raw.name)),
            support_match_count=len(matching),
            support_refs=[f"{r.support_file}:{r.sheet}:row{r.row_number}" for r in matching],
            support_row_texts=[r.text for r in matching],
        ))

    tmt_single = [r for r in raw_matches if r.filename_tmt and r.filename_single_branch]
    tmt_single_mapped = [r for r in tmt_single if r.support_match_count > 0]
    raw_refs = [r for r in raw_matches if r.support_match_count > 0]

    # This audit intentionally does not auto-authorize generation.  Even complete exact filename
    # linkage still needs interpretation of the deposited design rows to establish biological
    # sample identity/replicate semantics.
    if not reporter_ok:
        status = "reporter_contract_not_met"
    elif not support_candidates:
        status = "repository_design_source_not_discovered"
    elif not support_rows:
        status = "repository_design_source_unavailable_or_unparseable"
    elif not raw_refs:
        status = "design_source_has_no_exact_raw_linkage"
    elif not pub_contexts:
        status = "publication_run_scope_not_explicit"
    else:
        status = "run_scope_inventory_ready_for_design_row_review"

    write_support_rows(args.output / "support_rows.tsv", support_rows)
    write_raw_matches(args.output / "raw_support_matches.tsv", raw_matches)
    (args.output / "publication_scope_contexts.json").write_text(json.dumps(pub_contexts, indent=2, ensure_ascii=False))
    (args.output / "support_file_status.json").write_text(json.dumps(support_status, indent=2, ensure_ascii=False))

    summary: dict[str, object] = {
        "auditor_version": AUDITOR_VERSION,
        "accession": accession,
        "status": status,
        "non_generative": True,
        "reporter_contract": {
            "valid": reporter_ok,
            "mapping_class": reporter.get("mapping_class", ""),
            "confidence": reporter.get("confidence", ""),
            "analytical_channel": reporter.get("single_cell_channels", ""),
            "carrier_channel": reporter.get("carrier_channels", ""),
            "reference_channel": reporter.get("reference_channels", ""),
            "ambiguous_channels": reporter.get("ambiguous_channels", ""),
        },
        "repository_files": len(repo_files),
        "raw_files": len(raws),
        "support_candidates": len(support_candidates),
        "support_files_parsed": sum(1 for x in support_status if int(x.get("rows_parsed", "0") or 0) > 0),
        "support_rows": len(support_rows),
        "raw_files_with_exact_support_row": len(raw_refs),
        "filename_tmt_single_branch_candidates": len(tmt_single),
        "filename_tmt_single_branch_candidates_with_support_row": len(tmt_single_mapped),
        "publication_scoped_layout_contexts": len(pub_contexts),
        "generator_gate": "manual_design_row_review_required",
        "notes": [
            "Filename heuristics are diagnostic only and never establish biological sample identity.",
            "Exact RAW/support-table linkage is necessary but not sufficient for SDRF generation.",
            "A reporter-row generator remains blocked until design rows explicitly establish the biological sample/run scope of the 128 analytical + 131 carrier layout.",
        ],
        "outputs": {
            "support_file_status": str(args.output / "support_file_status.json"),
            "support_rows": str(args.output / "support_rows.tsv"),
            "raw_support_matches": str(args.output / "raw_support_matches.tsv"),
            "publication_scope_contexts": str(args.output / "publication_scope_contexts.json"),
        },
    }
    (args.output / "sdrf_reporter_run_scope_audit_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def _minimal_xlsx(path: Path) -> None:
    shared = [
        "Raw file", "Sample", "Reporter", "Carrier",
        "cell_A_TMT_single_neuron.RAW", "DA neuron 1", "TMT-128 analyte", "TMT-131 carrier",
    ]
    si = "".join(f"<si><t>{x}</t></si>" for x in shared)
    shared_xml = f'<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(shared)}" uniqueCount="{len(shared)}">{si}</sst>'
    sheet_xml = '''<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
      <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c><c r="D1" t="s"><v>3</v></c></row>
      <row r="2"><c r="A2" t="s"><v>4</v></c><c r="B2" t="s"><v>5</v></c><c r="C2" t="s"><v>6</v></c><c r="D2" t="s"><v>7</v></c></row>
    </sheetData></worksheet>'''
    workbook_xml = '''<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Design" sheetId="1" r:id="rId1"/></sheets></workbook>'''
    rels_xml = '''<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'''
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/sharedStrings.xml", shared_xml)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", rels_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def self_test() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        xlsx = root / "experimental_design.xlsx"
        _minimal_xlsx(xlsx)
        rows = parse_xlsx(xlsx)
        assert len(rows) == 2, rows
        assert rows[1].sheet == "Design"
        assert "cell_A_TMT_single_neuron.RAW" in rows[1].text
        assert row_mentions_raw(rows[1], "cell_A_TMT_single_neuron.RAW")
        assert not row_mentions_raw(rows[1], "cell_B.RAW")

        payload = [
            {"fileName": "cell_A_TMT_single_neuron.RAW", "fileCategory": {"value": "RAW"}, "publicFileLocations": [{"name": "FTP Protocol", "value": "ftp://example/cell_A_TMT_single_neuron.RAW"}]},
            {"fileName": "Study_experimental_design.xlsx", "fileCategory": {"value": "OTHER"}, "publicFileLocations": [{"name": "FTP Protocol", "value": "ftp://example/Study_experimental_design.xlsx"}]},
        ]
        files = repository_files(payload)
        assert len([x for x in files if is_raw_file(x)]) == 1
        assert len([x for x in files if is_support_candidate(x)]) == 1

        contexts = publication_scope_contexts([(
            "synthetic",
            "From each aspirate, proteins were tagged with TMT-128 as the analyte and mixed with TMT-131-tagged tissue digest as the carrier for individual neurons.",
        )])
        assert contexts, contexts
    print("sdrf_reporter_run_scope_audit self-test: PASS")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accession")
    ap.add_argument("--files-json", type=Path)
    ap.add_argument("--publication-manifest", type=Path)
    ap.add_argument("--reporter-audit-tsv", type=Path)
    ap.add_argument("--output", type=Path)
    ap.add_argument("--max-support-bytes", type=int, default=25 * 1024 * 1024)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return 0
    for attr in ("accession", "files_json", "publication_manifest", "reporter_audit_tsv", "output"):
        if getattr(args, attr) is None:
            ap.error(f"--{attr.replace('_', '-')} is required unless --self-test is used")
    summary = audit(args)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
