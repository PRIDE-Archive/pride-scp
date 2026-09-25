#!/usr/bin/env python3
"""Fail-closed specialized structured-design ingestion for PRIDE-SCP.

Adapters:
  * tabular XLS/XLSX experimental-design sheets
  * CSV/TSV run/channel annotation tables
  * Proteome Discoverer .pdStudy XML

The script emits a common evidence graph and, when a base SDRF is supplied,
a conservative candidate SDRF.  Candidate mutation is restricted to exact
joins and placeholder/missing fields; it never invents biological identity.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "pride-scp-specialized-design-ingest-v1"
PLACEHOLDERS = {"", "not available", "not applicable", "na", "n/a", "unknown"}


def norm(v: Any) -> str:
    return " ".join(str(v if v is not None else "").strip().split())


def norm_key(v: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", norm(v).lower())


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def raw_basename(v: str) -> str:
    v = norm(v).replace("\\", "/")
    return v.rsplit("/", 1)[-1]


def raw_stem(v: str) -> str:
    name = raw_basename(v)
    low = name.lower()
    for suffix in (".d.zip", ".raw", ".wiff", ".mzml", ".d"):
        if low.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


def label_channel(v: str) -> str:
    x = norm(v).upper().replace("TMT", "").replace("PRO", "")
    return re.sub(r"[^0-9A-Z]+", "", x)


def sniff_delimiter(path: Path) -> str:
    text = path.read_text(encoding="utf-8-sig", errors="replace")[:8192]
    try:
        return csv.Sniffer().sniff(text, delimiters=",;\t").delimiter
    except csv.Error:
        counts = {d: text.count(d) for d in (";", "\t", ",")}
        return max(counts, key=counts.get)


def read_table(path: Path) -> list[dict[str, str]]:
    delim = sniff_delimiter(path)
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.DictReader(fh, delimiter=delim)
        out: list[dict[str, str]] = []
        for row in reader:
            clean = {norm(k): norm(v) for k, v in row.items() if k is not None}
            clean.pop("", None)
            if any(clean.values()):
                out.append(clean)
        return out


def parse_annotation_tables(source_root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    files = sorted([p for p in source_root.iterdir() if p.suffix.lower() in {".csv", ".tsv"}])
    candidates: list[tuple[Path, list[dict[str, str]]]] = []
    for p in files:
        rows = read_table(p)
        if not rows:
            continue
        headers = {norm_key(k) for k in rows[0]}
        if {"run", "channel"}.issubset(headers):
            candidates.append((p, rows))

    if not candidates:
        return [], {"adapter": "tabular_run_channel", "tables": 0, "rows": 0}

    canonical_fields = ("run", "channel", "cell_type", "cell_number", "sample_type", "batch")

    def get(row: dict[str, str], wanted: str) -> str:
        w = norm_key(wanted)
        for k, v in row.items():
            if norm_key(k) == w:
                return norm(v)
        return ""

    table_sets: dict[Path, set[tuple[str, ...]]] = {}
    for p, rows in candidates:
        table_sets[p] = {tuple(get(r, f) for f in canonical_fields) for r in rows}

    # Prefer a table that is exactly the union of the other annotation tables.
    aggregate: Path | None = None
    for p, values in table_sets.items():
        others: set[tuple[str, ...]] = set()
        for q, qvalues in table_sets.items():
            if q != p:
                others |= qvalues
        if others and values == others:
            aggregate = p
            break

    selected = []
    if aggregate is not None:
        selected = [(aggregate, next(rows for p, rows in candidates if p == aggregate))]
    else:
        selected = candidates

    seen: set[tuple[str, ...]] = set()
    records: list[dict[str, Any]] = []
    for p, rows in selected:
        for row in rows:
            vals = tuple(get(row, f) for f in canonical_fields)
            if vals in seen:
                continue
            seen.add(vals)
            run, channel, cell_type, cell_number, sample_type, batch = vals
            if not run or not channel:
                continue
            records.append({
                "record_type": "run_channel_annotation",
                "join": {"assay_name": run, "channel": channel},
                "attributes": {
                    "cell_type": cell_type,
                    "cells_per_well": cell_number,
                    "sample_type": sample_type,
                    "batch": batch,
                },
                "evidence": {"file": p.name, "adapter": "tabular_run_channel"},
            })

    return records, {
        "adapter": "tabular_run_channel",
        "tables": len(candidates),
        "aggregate_table": aggregate.name if aggregate else None,
        "rows": len(records),
    }


def parse_xlsx_design(source_root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    paths = sorted([p for p in source_root.iterdir() if p.suffix.lower() in {".xlsx", ".xls"}])
    if not paths:
        return [], {"adapter": "xlsx_design", "files": 0, "rows": 0}
    try:
        from openpyxl import load_workbook
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("openpyxl is required for XLSX ingestion") from exc

    records: list[dict[str, Any]] = []
    meta: list[dict[str, Any]] = []
    for path in paths:
        wb = load_workbook(path, read_only=True, data_only=True)
        for ws in wb.worksheets:
            section = ""
            data_rows = 0
            for rownum, values in enumerate(ws.iter_rows(values_only=True), 1):
                cells = [norm(v) for v in values]
                first = cells[0] if cells else ""
                second = cells[1] if len(cells) > 1 else ""
                third = cells[2] if len(cells) > 2 else ""
                if not any(cells):
                    continue
                # Section headers are one non-empty cell and are not run identifiers.
                if first and not second and not third and not re.search(r"\d{4}-\d{2}-\d{2}", first):
                    section = first
                    continue
                # Project-level metadata is retained but not promoted into row mappings.
                if first in {"Publication/Project", "Authors (Full name)", "Correspondance"}:
                    continue
                if not first:
                    continue
                records.append({
                    "record_type": "design_row",
                    "join": {"run_id": first},
                    "attributes": {
                        "design_section": section,
                        "description": second,
                        "detail": third,
                    },
                    "evidence": {"file": path.name, "sheet": ws.title, "row": rownum, "adapter": "xlsx_design"},
                })
                data_rows += 1
            meta.append({"file": path.name, "sheet": ws.title, "rows": data_rows})
    return records, {"adapter": "xlsx_design", "files": len(paths), "sheets": meta, "rows": len(records)}


def parse_pdstudy(source_root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    paths = sorted([p for p in source_root.iterdir() if p.suffix.lower() == ".pdstudy"])
    records: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for path in paths:
        root = ET.parse(path).getroot()
        filesets = {e.attrib.get("Id", ""): e for e in root.findall(".//FileSet")}
        qchannels = {e.attrib.get("Id", ""): e.attrib.get("Name", "") for e in root.findall(".//QuanChannel")}
        method = root.find(".//QuanMethod")
        method_name = method.attrib.get("Name", "") if method is not None else ""
        raw_names: set[str] = set()
        channel_names: set[str] = set()
        count = 0
        for sample in root.findall(".//Sample"):
            fs = filesets.get(sample.attrib.get("FileSetId", ""))
            if fs is None:
                continue
            file_elem = fs.find("./Files/File")
            if file_elem is None:
                file_elem = fs.find(".//File")
            if file_elem is None:
                continue
            raw = raw_basename(file_elem.attrib.get("FileName", ""))
            qci = sample.find("./QuanChannelInformation")
            channel = qchannels.get(qci.attrib.get("QuanChannelId", ""), "") if qci is not None else ""
            if not raw or not channel:
                continue
            raw_names.add(raw)
            channel_names.add(channel)
            count += 1
            records.append({
                "record_type": "pdstudy_raw_channel",
                "join": {"raw_file": raw, "channel": channel},
                "attributes": {
                    "assay_name": raw_stem(raw),
                    "sample_name": sample.attrib.get("Name", ""),
                    "sample_number": sample.attrib.get("SampleNumber", ""),
                    "quant_method": method_name,
                },
                "evidence": {"file": path.name, "adapter": "pdstudy_xml"},
            })
        summaries.append({"file": path.name, "rows": count, "raw_files": len(raw_names), "channels": len(channel_names), "quant_method": method_name})
    return records, {"adapter": "pdstudy_xml", "files": len(paths), "studies": summaries, "rows": len(records)}


def read_sdrf(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        if reader.fieldnames is None:
            raise RuntimeError(f"missing SDRF header: {path}")
        return list(reader.fieldnames), [{k: norm(v) for k, v in row.items()} for row in reader]


def write_sdrf(path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers, delimiter="\t", lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def ensure_column(headers: list[str], rows: list[dict[str, str]], col: str) -> None:
    if col in headers:
        return
    # Insert characteristics before assay name, comments after assay name.
    if col.startswith("characteristics[") and "assay name" in headers:
        headers.insert(headers.index("assay name"), col)
    else:
        headers.append(col)
    for row in rows:
        row[col] = "not available"


def is_placeholder(v: str) -> bool:
    return norm(v).lower() in PLACEHOLDERS


def apply_exact_annotations(base: Path, records: list[dict[str, Any]], out: Path) -> dict[str, Any]:
    headers, rows = read_sdrf(base)
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    ambiguous: set[tuple[str, str]] = set()
    for rec in records:
        if rec.get("record_type") != "run_channel_annotation":
            continue
        j = rec["join"]
        key = (norm(j.get("assay_name")), label_channel(j.get("channel", "")))
        if not all(key):
            continue
        if key in by_key and by_key[key]["attributes"] != rec["attributes"]:
            ambiguous.add(key)
        else:
            by_key[key] = rec
    for key in ambiguous:
        by_key.pop(key, None)

    # Source-backed columns that can be filled without changing an existing concrete value.
    source_map = {
        "cell_type": "characteristics[cell type]",
        "cells_per_well": "characteristics[cells per well]",
        "sample_type": "characteristics[sample type]",
    }
    matched = 0
    applied = 0
    conflicts: list[dict[str, str]] = []
    for row in rows:
        assay = norm(row.get("assay name", ""))
        channel = label_channel(row.get("comment[label]", ""))
        rec = by_key.get((assay, channel))
        if rec is None:
            continue
        matched += 1
        attrs = rec["attributes"]
        for attr, col in source_map.items():
            value = norm(attrs.get(attr, ""))
            if not value:
                continue
            ensure_column(headers, rows, col)
            current = norm(row.get(col, ""))
            if is_placeholder(current):
                row[col] = value
                applied += 1
            elif current != value:
                conflicts.append({"assay": assay, "channel": channel, "column": col, "existing": current, "evidence": value})

    # Add exact TMT label only if absent/placeholder.
    for row in rows:
        assay = norm(row.get("assay name", ""))
        current_label = norm(row.get("comment[label]", ""))
        channel = label_channel(current_label)
        if channel:
            continue
        # An assay can only be filled when exactly one annotation channel exists, which is rare.
        chans = sorted({k[1] for k in by_key if k[0] == assay})
        if len(chans) == 1:
            ensure_column(headers, rows, "comment[label]")
            row["comment[label]"] = "TMT" + chans[0]
            applied += 1

    write_sdrf(out, headers, rows)
    return {"matched_rows": matched, "applied_values": applied, "conflicts": conflicts, "ambiguous_join_keys": len(ambiguous)}


def correlate_design_with_base(base: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    headers, rows = read_sdrf(base)
    assays = defaultdict(list)
    raws = defaultdict(list)
    for i, row in enumerate(rows):
        assays[norm(row.get("assay name", ""))].append(i)
        raws[raw_stem(row.get("comment[data file]", ""))].append(i)
    matched = 0
    unmatched = []
    ambiguous = []
    for rec in records:
        if rec.get("record_type") != "design_row":
            continue
        run = norm(rec["join"].get("run_id", ""))
        ids = sorted(set(assays.get(run, []) + raws.get(run, [])))
        if len(ids) == 1:
            rec["join"]["sdrf_row_index"] = ids[0]
            matched += 1
        elif len(ids) == 0:
            unmatched.append(run)
        else:
            ambiguous.append(run)
    return {"matched_design_rows": matched, "unmatched_run_ids": unmatched, "ambiguous_run_ids": ambiguous}


def correlate_pdstudy_with_base(base: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    _, rows = read_sdrf(base)
    keys = defaultdict(list)
    for i, row in enumerate(rows):
        raw = raw_basename(row.get("comment[data file]", ""))
        channel = label_channel(row.get("comment[label]", ""))
        if raw and channel:
            keys[(raw, channel)].append(i)
    matched = 0
    raw_only = 0
    no_match = 0
    rawset = {raw_basename(r.get("comment[data file]", "")) for r in rows}
    for rec in records:
        if rec.get("record_type") != "pdstudy_raw_channel":
            continue
        raw = raw_basename(rec["join"].get("raw_file", ""))
        channel = label_channel(rec["join"].get("channel", ""))
        ids = keys.get((raw, channel), [])
        if len(ids) == 1:
            rec["join"]["sdrf_row_index"] = ids[0]
            matched += 1
        elif raw in rawset:
            raw_only += 1
        else:
            no_match += 1
    return {"matched_raw_channel_rows": matched, "raw_present_without_channel_match": raw_only, "unmatched_rows": no_match}


def write_graph(path: Path, accession: str, records: list[dict[str, Any]], source_summary: list[dict[str, Any]], correlations: dict[str, Any]) -> None:
    payload = {"schema": SCHEMA, "accession": accession, "source_summary": source_summary, "correlations": correlations, "records": records}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_mapping_tsv(path: Path, accession: str, records: list[dict[str, Any]]) -> None:
    fields = ["accession", "record_type", "source_file", "raw_file", "assay_name", "channel", "cell_type", "cells_per_well", "sample_type", "batch", "design_section", "description", "detail", "sample_name", "quant_method", "sdrf_row_index"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, delimiter="\t", lineterminator="\n")
        w.writeheader()
        for rec in records:
            j = rec.get("join", {})
            a = rec.get("attributes", {})
            e = rec.get("evidence", {})
            w.writerow({
                "accession": accession,
                "record_type": rec.get("record_type", ""),
                "source_file": e.get("file", ""),
                "raw_file": j.get("raw_file", ""),
                "assay_name": j.get("assay_name", a.get("assay_name", "")),
                "channel": j.get("channel", ""),
                "cell_type": a.get("cell_type", ""),
                "cells_per_well": a.get("cells_per_well", ""),
                "sample_type": a.get("sample_type", ""),
                "batch": a.get("batch", ""),
                "design_section": a.get("design_section", ""),
                "description": a.get("description", ""),
                "detail": a.get("detail", ""),
                "sample_name": a.get("sample_name", ""),
                "quant_method": a.get("quant_method", ""),
                "sdrf_row_index": j.get("sdrf_row_index", ""),
            })


def run(args: argparse.Namespace) -> int:
    accession = args.accession
    source_root = Path(args.source_root)
    output_root = Path(args.output_root)
    base = Path(args.base_sdrf) if args.base_sdrf else None
    if not source_root.is_dir():
        raise SystemExit(f"source root not found: {source_root}")
    output_root.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    correlations: dict[str, Any] = {}

    for parser in (parse_annotation_tables, parse_xlsx_design, parse_pdstudy):
        recs, summary = parser(source_root)
        if recs:
            records.extend(recs)
        if summary.get("rows") or summary.get("files") or summary.get("tables"):
            summaries.append(summary)

    if not records:
        raise SystemExit("no supported structured-design records found")

    if base is not None:
        if not base.is_file():
            raise SystemExit(f"base SDRF not found: {base}")
        correlations["xlsx"] = correlate_design_with_base(base, records)
        correlations["pdstudy"] = correlate_pdstudy_with_base(base, records)
        ann_records = [r for r in records if r.get("record_type") == "run_channel_annotation"]
        candidate = output_root / f"{accession}.specialized_candidate.sdrf.tsv"
        if ann_records:
            correlations["annotation_candidate"] = apply_exact_annotations(base, records, candidate)
        else:
            shutil.copy2(base, candidate)
            correlations["annotation_candidate"] = {"matched_rows": 0, "applied_values": 0, "conflicts": [], "copied_base_unchanged": True}
        correlations["candidate_sha256"] = sha256(candidate)
        correlations["base_sha256"] = sha256(base)

    write_graph(output_root / f"{accession}.structured_design_graph.json", accession, records, summaries, correlations)
    write_mapping_tsv(output_root / f"{accession}.structured_mapping.tsv", accession, records)
    summary = {"schema": SCHEMA, "accession": accession, "records": len(records), "source_summary": summaries, "correlations": correlations}
    (output_root / f"{accession}.ingest_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def self_test() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "src"; src.mkdir()
        # semicolon annotation table + base SDRF exact assay/channel join
        (src / "sampleAnnotations.csv").write_text(
            "run;channel;cell_type;cell_number;sample_type;batch\n"
            "RUN1;126C;carrier;50;SC;B1\n"
            "RUN1;127N;THP1;1;SC;B1\n",
            encoding="utf-8",
        )
        base = root / "base.tsv"
        base.write_text(
            "source name\tcharacteristics[cell type]\tassay name\tcomment[data file]\tcomment[label]\n"
            "s1\tnot available\tRUN1\tRUN1.raw\tTMT126C\n"
            "s2\tnot available\tRUN1\tRUN1.raw\tTMT127N\n",
            encoding="utf-8",
        )
        out = root / "out"
        args = argparse.Namespace(accession="PXDTEST", source_root=str(src), base_sdrf=str(base), output_root=str(out))
        run(args)
        headers, rows = read_sdrf(out / "PXDTEST.specialized_candidate.sdrf.tsv")
        assert "characteristics[cells per well]" in headers
        assert rows[0]["characteristics[cells per well]"] == "50"
        assert rows[1]["characteristics[cell type]"] == "THP1"
        summary = json.loads((out / "PXDTEST.ingest_summary.json").read_text())
        assert summary["correlations"]["annotation_candidate"]["matched_rows"] == 2

        # pdStudy minimal exact topology
        pdsrc = root / "pd"; pdsrc.mkdir()
        (pdsrc / "x.pdStudy").write_text(
            "<?xml version='1.0'?><Study><FileSets><FileSet Id='F1'><Files><File FileName='C:\\\\x\\\\A.raw'/></Files></FileSet></FileSets>"
            "<QuanMethods><QuanMethod Id='38' Name='TMTpro'><QuanChannels><QuanChannel Id='1' Name='126'/></QuanChannels></QuanMethod></QuanMethods>"
            "<Samples><Sample FileSetId='F1' Name='A - [126]' SampleNumber='1'><QuanChannelInformation QuanMethodId='38' QuanChannelId='1'/></Sample></Samples></Study>",
            encoding="utf-8",
        )
        recs, meta = parse_pdstudy(pdsrc)
        assert len(recs) == 1 and recs[0]["join"]["raw_file"] == "A.raw" and recs[0]["join"]["channel"] == "126"
        assert meta["rows"] == 1
    print("sdrf_specialized_design_ingest self-test: PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accession")
    ap.add_argument("--source-root")
    ap.add_argument("--base-sdrf")
    ap.add_argument("--output-root")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    missing = [x for x in ("accession", "source_root", "output_root") if not getattr(args, x)]
    if missing:
        ap.error("missing required arguments: " + ", ".join("--" + x.replace("_", "-") for x in missing))
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
