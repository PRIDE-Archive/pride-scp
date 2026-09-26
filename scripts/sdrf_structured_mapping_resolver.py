#!/usr/bin/env python3
"""Fail-closed resolver for explicit structured RAW/sample/channel mapping evidence.

The resolver is deliberately non-generative.  It scans trusted structured evidence
(JSON/JSONL/TSV/CSV/XLSX) for records that explicitly contain a repository RAW/data-file
identity plus one or more SDRF fields.  Candidate rows are updated only when the mapping
is unique at the strongest exact identity available (RAW+label/source/assay before RAW
alone).  Existing concrete SDRF values are never overwritten with a different value.

A one-row-per-RAW candidate may be expanded into multiple rows only when one structured
evidence source explicitly contains multiple unique reporter/channel records for that RAW
and each record carries a concrete label plus at least one concrete biological/sample
identity field.  Row order and filename semantics are never used as evidence.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

SCHEMA_VERSION = "pride-scp-structured-row-mapping-resolver-v2"
PLACEHOLDERS = {"", "not available", "not applicable", "unknown", "na", "n/a", "none"}


def norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", norm_text(value))


def norm_file(value: str) -> str:
    value = str(value or "").strip().replace("\\", "/")
    value = value.rsplit("/", 1)[-1]
    return value.casefold()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "source name": ("source name", "source_name", "sourcename", "sample name", "sample_name", "sample"),
    "assay name": ("assay name", "assay_name", "assayname"),
    "characteristics[cell identifier]": (
        "characteristics[cell identifier]", "cell identifier", "cell_identifier", "cell id", "cell_id",
    ),
    "characteristics[biological replicate]": (
        "characteristics[biological replicate]", "biological replicate", "biological_replicate", "bio replicate", "bio_replicate",
    ),
    "characteristics[cell line]": ("characteristics[cell line]", "cell line", "cell_line"),
    "characteristics[cellosaurus accession]": (
        "characteristics[cellosaurus accession]", "cellosaurus accession", "cellosaurus_accession",
        "characteristics[cell line accession]", "cell line accession", "cell_line_accession",
    ),
    "characteristics[organism]": ("characteristics[organism]", "organism", "species"),
    "characteristics[organism part]": ("characteristics[organism part]", "organism part", "organism_part", "tissue"),
    "characteristics[sample type]": ("characteristics[sample type]", "sample type", "sample_type"),
    "characteristics[individual]": ("characteristics[individual]", "individual", "donor identifier", "donor_identifier", "subject identifier", "subject_identifier"),
    "comment[label]": (
        "comment[label]", "label", "channel", "channel name", "channel_name", "channelname",
        "reporter channel", "reporter_channel", "reporter channel name", "reporter_channel_name",
        "tmt channel", "tmt_channel", "quan channel", "quan_channel",
    ),
}

RAW_ALIASES = (
    "comment[data file]", "data file", "data_file", "raw file", "raw_file", "raw filename", "raw_filename",
    "repository file", "repository_file", "repository raw", "repository_raw", "file name", "file_name",
)

IDENTITY_FIELDS = ("comment[label]", "source name", "assay name")
BIOLOGICAL_FIELDS = (
    "source name",
    "characteristics[cell identifier]",
    "characteristics[biological replicate]",
    "characteristics[cell line]",
    "characteristics[organism]",
    "characteristics[organism part]",
    "characteristics[sample type]",
    "characteristics[individual]",
)
NARROWABLE_COMPOSITE_FIELDS = {
    "characteristics[cell line]",
    "characteristics[organism]",
    "characteristics[organism part]",
    "characteristics[sample type]",
    "characteristics[individual]",
}

ALIAS_TO_FIELD = {
    norm_key(alias): field
    for field, aliases in FIELD_ALIASES.items()
    for alias in aliases
}
RAW_KEYS = {norm_key(x) for x in RAW_ALIASES}
ACCESSION_KEYS = {norm_key(x) for x in ("accession", "project_accession", "project accession", "pxd")}


def is_concrete(value: str) -> bool:
    return norm_text(value) not in PLACEHOLDERS


def row_specific_value(field: str, value: str) -> bool:
    """Return whether a structured value can resolve one SDRF row.

    Project-level composite biological values such as ``HeLa; Jurkat`` are useful
    context but are not row-scoped mapping evidence and must never be applied as if
    they selected one member.
    """
    if not is_concrete(value):
        return False
    if field in NARROWABLE_COMPOSITE_FIELDS:
        parts = [x.strip() for x in re.split(r"[;|]", value) if x.strip()]
        if len(parts) > 1:
            return False
    return True


def scalar(value: Any) -> str:
    if isinstance(value, (str, int, float, bool)):
        return str(value).strip()
    return ""


def canonical_record(row: dict[str, Any], source: Path) -> dict[str, str] | None:
    raw = ""
    accession_value = ""
    fields: dict[str, str] = {}
    for key, value in row.items():
        sval = scalar(value)
        if not sval:
            continue
        nk = norm_key(str(key))
        if nk in RAW_KEYS and not raw:
            raw = sval
        if nk in ACCESSION_KEYS and not accession_value and re.fullmatch(r"PXD\d{6,}", sval, re.I):
            accession_value = sval.upper()
        field = ALIAS_TO_FIELD.get(nk)
        if field and row_specific_value(field, sval):
            prev = fields.get(field)
            if prev and norm_text(prev) != norm_text(sval):
                return None
            fields[field] = sval
    if not raw or not fields:
        return None
    payload = {"raw_file": raw, "source_path": str(source)}
    if accession_value:
        payload["accession"] = accession_value
    payload.update(fields)
    return payload


def walk_json_records(obj: Any, source: Path, inherited: dict[str, Any] | None = None) -> Iterable[dict[str, str]]:
    inherited = dict(inherited or {})
    if isinstance(obj, dict):
        merged = dict(inherited)
        for k, v in obj.items():
            if isinstance(v, (str, int, float, bool)):
                merged[k] = v
        rec = canonical_record(merged, source)
        if rec:
            yield rec
        for value in obj.values():
            if isinstance(value, (dict, list)):
                yield from walk_json_records(value, source, merged)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk_json_records(item, source, inherited)


def read_delimited(path: Path, delimiter: str) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
            return [dict(x) for x in csv.DictReader(fh, delimiter=delimiter)]
    except Exception:
        return []


def row_with_evidence_text(row: dict[str, Any]) -> dict[str, Any] | None:
    text = str(row.get("evidence_text") or "").strip()
    if not text or "=" not in text:
        return None
    parsed: dict[str, Any] = {}
    for part in re.split(r"\s*\|\s*", text):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip(); value = value.strip()
        if key and value:
            parsed[key] = value
    for source_key, target_key in (("accession", "accession"), ("repository_raw", "raw_file")):
        value = row.get(source_key)
        if value:
            parsed[target_key] = value
    return parsed or None


def xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    name = "xl/sharedStrings.xml"
    if name not in zf.namelist():
        return []
    root = ET.fromstring(zf.read(name))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    out = []
    for si in root.findall("m:si", ns):
        out.append("".join(t.text or "" for t in si.findall(".//m:t", ns)))
    return out


def xlsx_cell_value(cell: ET.Element, shared: list[str], ns: dict[str, str]) -> str:
    typ = cell.attrib.get("t", "")
    if typ == "inlineStr":
        return "".join(t.text or "" for t in cell.findall(".//m:t", ns))
    node = cell.find("m:v", ns)
    if node is None or node.text is None:
        return ""
    value = node.text
    if typ == "s":
        try:
            return shared[int(value)]
        except Exception:
            return ""
    return value


def read_xlsx(path: Path) -> list[dict[str, str]]:
    rows_out: list[dict[str, str]] = []
    try:
        with zipfile.ZipFile(path) as zf:
            shared = xlsx_shared_strings(zf)
            ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            sheets = sorted(x for x in zf.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", x))
            for sheet in sheets:
                root = ET.fromstring(zf.read(sheet))
                matrix: list[list[str]] = []
                for row in root.findall(".//m:sheetData/m:row", ns):
                    values: dict[int, str] = {}
                    for c in row.findall("m:c", ns):
                        ref = c.attrib.get("r", "")
                        m = re.match(r"([A-Z]+)", ref)
                        if not m:
                            continue
                        col = 0
                        for ch in m.group(1):
                            col = col * 26 + (ord(ch) - 64)
                        values[col - 1] = xlsx_cell_value(c, shared, ns)
                    if values:
                        max_col = max(values)
                        matrix.append([values.get(i, "") for i in range(max_col + 1)])
                if len(matrix) < 2:
                    continue
                headers = [x.strip() for x in matrix[0]]
                for row in matrix[1:]:
                    padded = row + [""] * max(0, len(headers) - len(row))
                    rows_out.append({headers[i]: padded[i] for i in range(len(headers)) if headers[i]})
    except Exception:
        return []
    return rows_out


def structured_records(path: Path) -> list[dict[str, str]]:
    suffix = path.suffix.lower()
    rows: list[dict[str, Any]] = []
    if suffix in {".tsv", ".txt"}:
        rows = read_delimited(path, "\t")
    elif suffix == ".csv":
        rows = read_delimited(path, ",")
    elif suffix == ".xlsx":
        rows = read_xlsx(path)
    elif suffix in {".json", ".jsonl"}:
        try:
            if suffix == ".jsonl":
                objs = [json.loads(line) for line in path.read_text(errors="replace").splitlines() if line.strip()]
            else:
                objs = [json.loads(path.read_text(errors="replace"))]
            out: list[dict[str, str]] = []
            for obj in objs:
                out.extend(walk_json_records(obj, path))
            return out
        except Exception:
            return []
    out = []
    for row in rows:
        rec = canonical_record(row, path)
        if rec:
            out.append(rec)
        recovered = row_with_evidence_text(row)
        if recovered:
            rec2 = canonical_record(recovered, path)
            if rec2:
                out.append(rec2)
    return out


def collect_records(roots: list[Path], target_accession: str = "") -> list[dict[str, str]]:
    allowed = {".json", ".jsonl", ".tsv", ".csv", ".xlsx"}
    out: list[dict[str, str]] = []
    seen_files: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        paths = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in allowed)
        for path in paths:
            key = str(path)
            if key in seen_files:
                continue
            seen_files.add(key)
            path_pxds = {x.upper() for x in re.findall(r"PXD\d{6,}", str(path), re.I)}
            for rec in structured_records(path):
                rec_acc = (rec.get("accession") or "").upper()
                if target_accession:
                    target = target_accession.upper()
                    if rec_acc:
                        if rec_acc != target:
                            continue
                    elif path_pxds:
                        if target not in path_pxds:
                            continue
                    else:
                        # With a cohort evidence root, an unscoped record carrying neither
                        # an explicit accession nor an accession-scoped source path is not
                        # safe to bind to a project even when its RAW basename happens to match.
                        continue
                rec["source_sha256"] = sha256_file(path)
                out.append(rec)
    return out


def identity_match_score(candidate: dict[str, str], rec: dict[str, str]) -> int | None:
    if norm_file(candidate.get("comment[data file]", "")) != norm_file(rec.get("raw_file", "")):
        return None
    score = 1
    for field in IDENTITY_FIELDS:
        rv = rec.get(field, "")
        if not is_concrete(rv):
            continue
        cv = candidate.get(field, "")
        if is_concrete(cv):
            if norm_text(cv) != norm_text(rv):
                return None
            score += 2
    return score


def unique_value(records: list[dict[str, str]], field: str) -> tuple[str, dict[str, str] | None]:
    values: dict[str, tuple[str, dict[str, str]]] = {}
    for rec in records:
        value = rec.get(field, "")
        if not is_concrete(value):
            continue
        values[norm_text(value)] = (value, rec)
    if len(values) != 1:
        return "", None
    return next(iter(values.values()))


def mapping_candidates(candidate: dict[str, str], records_by_raw: dict[str, list[dict[str, str]]]) -> list[dict[str, str]]:
    pool = records_by_raw.get(norm_file(candidate.get("comment[data file]", "")), [])
    scored: list[tuple[int, dict[str, str]]] = []
    for rec in pool:
        score = identity_match_score(candidate, rec)
        if score is not None:
            scored.append((score, rec))
    if not scored:
        return []
    best = max(score for score, _ in scored)
    return [rec for score, rec in scored if score == best]




def dedupe_records(records: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[tuple[str, str], ...]] = set()
    out: list[dict[str, str]] = []
    for rec in records:
        sig = tuple(sorted(
            (k, norm_text(v))
            for k, v in rec.items()
            if k not in {"source_path", "source_sha256"} and is_concrete(v)
        ))
        if sig in seen:
            continue
        seen.add(sig)
        out.append(rec)
    return out

def can_expand(base: dict[str, str], records: list[dict[str, str]]) -> bool:
    if len(records) < 2:
        return False
    if is_concrete(base.get("comment[label]", "")):
        return False
    labels = [rec.get("comment[label]", "") for rec in records]
    if not all(is_concrete(x) for x in labels):
        return False
    if len({norm_text(x) for x in labels}) != len(labels):
        return False
    # A reporter label alone is not enough to define a biological/sample row.
    return all(any(is_concrete(rec.get(field, "")) for field in BIOLOGICAL_FIELDS) for rec in records)




def safe_composite_narrowing(field: str, current: str, value: str) -> bool:
    """Allow exact row evidence to select one member of an existing explicit set.

    This is not a generic overwrite rule.  It applies only to selected biological fields,
    only when the current value explicitly contains two or more semicolon/pipe-delimited
    alternatives, and only when the proposed value exactly equals one existing member.
    """
    if field not in NARROWABLE_COMPOSITE_FIELDS:
        return False
    if not is_concrete(current) or not is_concrete(value):
        return False
    parts = [x.strip() for x in re.split(r"[;|]", current) if x.strip()]
    if len(parts) < 2:
        return False
    target = norm_text(value)
    return sum(norm_text(x) == target for x in parts) == 1


def apply_record(
    base: dict[str, str],
    rec: dict[str, str],
    headers: list[str],
    *,
    allow_expansion_identity_replace: bool = False,
) -> tuple[dict[str, str], list[dict[str, str]], str]:
    row = dict(base)
    edges: list[dict[str, str]] = []
    for field in headers:
        if field == "comment[data file]":
            continue
        value = rec.get(field, "")
        if not row_specific_value(field, value):
            continue
        current = row.get(field, "")
        if is_concrete(current) and norm_text(current) != norm_text(value):
            if allow_expansion_identity_replace and field in {"source name", "assay name"}:
                row[field] = value
                edges.append({
                    "field": field,
                    "old": current,
                    "new": value,
                    "source_path": rec.get("source_path", ""),
                    "source_sha256": rec.get("source_sha256", ""),
                    "explicit_multiplex_identity_split": "true",
                })
                continue
            if not safe_composite_narrowing(field, current, value):
                return base, [], f"concrete_conflict:{field}:{current}!={value}"
            row[field] = value
            edges.append({
                "field": field,
                "old": current,
                "new": value,
                "source_path": rec.get("source_path", ""),
                "source_sha256": rec.get("source_sha256", ""),
                "narrowed_composite": "true",
            })
        elif not is_concrete(current):
            row[field] = value
            edges.append({
                "field": field,
                "old": current,
                "new": value,
                "source_path": rec.get("source_path", ""),
                "source_sha256": rec.get("source_sha256", ""),
            })
    return row, edges, ""


def resolve(candidate: Path, output: Path, report: Path, roots: list[Path], accession: str = "") -> dict[str, Any]:
    with candidate.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        headers = list(reader.fieldnames or [])
        source_rows = [dict(row) for row in reader]
    if "comment[data file]" not in headers:
        raise ValueError("candidate has no comment[data file] column")

    records = collect_records(roots, accession)
    by_raw: dict[str, list[dict[str, str]]] = defaultdict(list)
    for rec in records:
        by_raw[norm_file(rec.get("raw_file", ""))].append(rec)

    # A resolver may add a recognized SDRF column only when structured evidence
    # contains at least one concrete row-scoped value for that field.  Empty/all-
    # placeholder shape-only columns are never added.  Rows without exact evidence
    # remain blank and are caught by the immediate validator gate.
    added_columns: list[str] = []
    for field in FIELD_ALIASES:
        if field in headers:
            continue
        if any(row_specific_value(field, rec.get(field, "")) for rec in records):
            headers.append(field)
            added_columns.append(field)

    candidate_by_raw: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in source_rows:
        candidate_by_raw[norm_file(row.get("comment[data file]", ""))].append(row)

    output_rows: list[dict[str, str]] = []
    applied: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    expansions: list[dict[str, Any]] = []

    for raw_key, group in candidate_by_raw.items():
        explicit_records = dedupe_records(by_raw.get(raw_key, []))
        # Safe explicit multiplex expansion only for one-row-per-RAW candidates.
        if len(group) == 1 and can_expand(group[0], explicit_records):
            expanded_rows = []
            expanded_edges: list[dict[str, Any]] = []
            ok = True
            for rec in sorted(explicit_records, key=lambda r: norm_text(r.get("comment[label]", ""))):
                new_row, edges, err = apply_record(
                    group[0], rec, headers, allow_expansion_identity_replace=True
                )
                if err:
                    ok = False
                    conflicts.append({"data_file": group[0].get("comment[data file]", ""), "status": "multiplex_expansion_conflict", "detail": err, "source_path": rec.get("source_path", "")})
                    break
                expanded_rows.append(new_row)
                expanded_edges.extend(edges)
            if ok and len(expanded_rows) == len(explicit_records):
                output_rows.extend(expanded_rows)
                expansions.append({"data_file": group[0].get("comment[data file]", ""), "rows_before": 1, "rows_after": len(expanded_rows), "labels": [x.get("comment[label]", "") for x in expanded_rows]})
                for edge in expanded_edges:
                    applied.append({"data_file": group[0].get("comment[data file]", ""), **edge, "mode": "explicit_multiplex_expansion"})
                continue

        for row in group:
            matches = mapping_candidates(row, by_raw)
            updated = dict(row)
            for field in headers:
                if field == "comment[data file]":
                    continue
                current = updated.get(field, "")
                value, source = unique_value(matches, field)
                if not value or source is None:
                    continue
                if is_concrete(current):
                    if norm_text(current) == norm_text(value):
                        continue
                    if not safe_composite_narrowing(field, current, value):
                        continue
                    mode = "exact_structured_composite_narrowing"
                else:
                    mode = "exact_structured_mapping"
                updated[field] = value
                applied.append({
                    "data_file": row.get("comment[data file]", ""),
                    "field": field,
                    "old": current,
                    "new": value,
                    "source_path": source.get("source_path", ""),
                    "source_sha256": source.get("source_sha256", ""),
                    "mode": mode,
                })
            output_rows.append(updated)

    # Preserve original row order across RAW groups as much as possible by rebuilding using
    # first appearance order rather than sorted keys.
    if len(candidate_by_raw) > 1:
        rebuilt: list[dict[str, str]] = []
        grouped_out: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in output_rows:
            grouped_out[norm_file(row.get("comment[data file]", ""))].append(row)
        seen: set[str] = set()
        for row in source_rows:
            key = norm_file(row.get("comment[data file]", ""))
            if key in seen:
                continue
            seen.add(key)
            rebuilt.extend(grouped_out.get(key, []))
        output_rows = rebuilt

    output.parent.mkdir(parents=True, exist_ok=True)
    substantive_change = bool(applied or expansions)
    if substantive_change:
        with output.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=headers, delimiter="\t", lineterminator="\n", extrasaction="ignore")
            writer.writeheader()
            writer.writerows(output_rows)
    else:
        # Do not turn formatting/newline/BOM differences into false scientific
        # progress.  With no accepted mapping edge and no explicit expansion the
        # resolver must preserve the candidate byte-for-byte.
        shutil.copy2(candidate, output)
        added_columns = []

    changed = substantive_change and sha256_file(candidate) != sha256_file(output)
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "changed" if changed else "unchanged",
        "changed": changed,
        "input": str(candidate),
        "output": str(output),
        "input_sha256": sha256_file(candidate),
        "output_sha256": sha256_file(output),
        "structured_records": len(records),
        "added_columns": added_columns,
        "applied_edges": applied,
        "applied_edge_count": len(applied),
        "multiplex_expansions": expansions,
        "multiplex_expansion_count": len(expansions),
        "conflicts": conflicts,
        "evidence_roots": [str(x) for x in roots],
        "safety": {
            "row_order_inference": False,
            "filename_semantic_inference": False,
            "concrete_value_overwrite": False,
            "explicit_multiplex_source_assay_split_only": True,
            "project_level_composite_not_row_mapping_evidence": True,
            "explicit_composite_member_narrowing_only": True,
            "structured_explicit_mapping_only": True,
        },
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def self_test() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "candidate.tsv"
        evidence = root / "mapping.tsv"
        out = root / "out.tsv"
        rep = root / "report.json"
        src.write_text(
            "source name\tcharacteristics[cell identifier]\tcharacteristics[biological replicate]\tcomment[data file]\tcomment[label]\n"
            "S1\tnot available\tnot available\ta.raw\tlabel free sample\n",
            encoding="utf-8",
        )
        evidence.write_text(
            "raw_file\tcell_identifier\tbiological_replicate\n"
            "a.raw\tcell-001\t1\n",
            encoding="utf-8",
        )
        result = resolve(src, out, rep, [evidence])
        assert result["changed"] and result["applied_edge_count"] == 2
        assert "cell-001" in out.read_text()

        # Conflicting values remain unresolved.
        evidence.write_text(
            "raw_file\tcell_identifier\n"
            "a.raw\tcell-001\n"
            "a.raw\tcell-002\n",
            encoding="utf-8",
        )
        result = resolve(src, out, rep, [evidence])
        text = out.read_text()
        assert "cell-001" not in text and "cell-002" not in text

        # Exact structured evidence may narrow a project-level composite value only to
        # one member already explicitly present in that composite.
        src_comp = root / "candidate_composite.tsv"
        src_comp.write_text(
            "source name\tcharacteristics[cell line]\tcomment[data file]\tcomment[label]\n"
            "S1\tHeLa; K562\tc.raw\tlabel free sample\n", encoding="utf-8"
        )
        evidence.write_text(
            "raw_file\tcell_line\n"
            "c.raw\tK562\n", encoding="utf-8"
        )
        result = resolve(src_comp, out, rep, [evidence])
        assert result["changed"]
        assert "\tK562\t" in out.read_text()
        assert any(x["mode"] == "exact_structured_composite_narrowing" for x in result["applied_edges"])

        # Explicit multiplex records may expand one RAW only when reporter labels and
        # biological identities are both explicitly present.
        src2 = root / "candidate2.tsv"
        src2.write_text(
            "source name\tcharacteristics[cell identifier]\tcomment[data file]\tcomment[label]\n"
            "not available\tnot available\tm.raw\tnot available\n",
            encoding="utf-8",
        )
        evidence.write_text(
            "raw_file\treporter_channel\tcell_identifier\tsource_name\n"
            "m.raw\tTMT126\tcell-A\tcell-A\n"
            "m.raw\tTMT127N\tcell-B\tcell-B\n",
            encoding="utf-8",
        )
        result = resolve(src2, out, rep, [evidence])
        assert result["multiplex_expansion_count"] == 1
        with out.open(newline="") as fh:
            rows = list(csv.DictReader(fh, delimiter="\t"))
        assert len(rows) == 2 and {x["characteristics[cell identifier]"] for x in rows} == {"cell-A", "cell-B"}

        # Recognized missing columns may be added only when explicit structured
        # evidence supplies a concrete value.
        src4 = root / "missing_column.tsv"
        ev4 = root / "missing_column_evidence.tsv"
        out4 = root / "missing_column_out.tsv"
        rep4 = root / "missing_column_report.json"
        src4.write_text("source name\tcomment[data file]\nS1\ta.raw\n")
        ev4.write_text("raw_file\tcell identifier\na.raw\tcell-1\n")
        r4 = resolve(src4, out4, rep4, [ev4], "")
        assert r4["changed"] is True
        assert "characteristics[cell identifier]" in r4["added_columns"]
        assert "cell-1" in out4.read_text()

        # Explicit multiplex expansion may replace the pre-expansion technical
        # source identity with source-backed channel identities.
        src5 = root / "multiplex_source.tsv"
        ev5 = root / "multiplex_source_evidence.tsv"
        out5 = root / "multiplex_source_out.tsv"
        rep5 = root / "multiplex_source_report.json"
        src5.write_text("source name\tcomment[data file]\nrun_0001\tm.raw\n")
        ev5.write_text(
            "raw_file\tlabel\tsource name\tcell identifier\n"
            "m.raw\tTMT126\tcell-A\tA\n"
            "m.raw\tTMT127N\tcell-B\tB\n"
        )
        r5 = resolve(src5, out5, rep5, [ev5], "")
        assert r5["multiplex_expansion_count"] == 1
        rows5 = list(csv.DictReader(out5.open(), delimiter="\t"))
        assert {x["source name"] for x in rows5} == {"cell-A", "cell-B"}

        # Composite project-level identity must not be treated as row-scoped
        # structured mapping evidence.
        src6 = root / "composite.tsv"
        ev6 = root / "composite_evidence.tsv"
        out6 = root / "composite_out.tsv"
        rep6 = root / "composite_report.json"
        src6.write_text("source name\tcharacteristics[cell line]\tcomment[data file]\nS1\tnot applicable\tc.raw\n")
        ev6.write_text("raw_file\tcell line\nc.raw\tHeLa; Jurkat\n")
        r6 = resolve(src6, out6, rep6, [ev6], "")
        assert r6["applied_edge_count"] == 0
        assert "HeLa; Jurkat" not in out6.read_text()
        assert out6.read_bytes() == src6.read_bytes()

        # Generalized graph join rows from structured SQLite evidence preserve source column names
        # as field=value assertions.  The resolver may project them only through its normal alias and
        # uniqueness rules; high-level join status alone is never treated as a value.
        src8 = root / "join_bridge.tsv"
        ev8 = root / "join_evidence.tsv"
        out8 = root / "join_bridge_out.tsv"
        rep8 = root / "join_bridge_report.json"
        src8.write_text(
            "source name\tcomment[data file]\tcomment[label]\n"
            "not available\tm.raw\tnot available\n"
        )
        ev8.write_text(
            "accession\trepository_raw\tjoin_confidence\tevidence_text\n"
            "PXD900001\tm.raw\thigh\tRawFile=m.raw | SampleName=cell-A | ChannelName=TMT126\n"
        )
        r8 = resolve(src8, out8, rep8, [ev8], "PXD900001")
        assert r8["changed"] is True
        rows8 = list(csv.DictReader(out8.open(), delimiter="\t"))
        assert rows8[0]["source name"] == "cell-A"
        assert rows8[0]["comment[label]"] == "TMT126"

        noev = root / "noev.tsv"; noev_out = root / "noev_out.tsv"; noev_rep = root / "noev.json"
        noev.write_bytes(b"source name\tcomment[data file]\r\nS1\ta.raw\r\n")
        r7 = resolve(noev, noev_out, noev_rep, [], "")
        assert r7["changed"] is False
        assert noev_out.read_bytes() == noev.read_bytes()

    print("sdrf_structured_mapping_resolver self-test: PASS")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--candidate", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--report", type=Path)
    p.add_argument("--evidence-root", type=Path, action="append", default=[])
    p.add_argument("--accession", default="")
    return p


def main() -> int:
    args = parser().parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.candidate is None or args.output is None or args.report is None:
        raise SystemExit("--candidate, --output and --report are required")
    result = resolve(args.candidate, args.output, args.report, args.evidence_root, args.accession)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
