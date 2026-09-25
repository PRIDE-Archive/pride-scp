#!/usr/bin/env python3
"""Fail-closed exact Cellosaurus resolver for SDRF cell-line annotations.

Only a unique exact recommended-name or synonym match is accepted. The resolver never
splits composite values and never guesses from fuzzy similarity. By default it queries
the public Cellosaurus API; a JSON fixture/cache may be supplied for reproducible tests.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API = "https://api.cellosaurus.org/search/cell-line"
PLACEHOLDERS = {"", "not available", "not applicable", "unknown", "na", "n/a"}


def norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def parse_records(obj: Any) -> list[dict[str, Any]]:
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if not isinstance(obj, dict):
        return []
    for key in ("cell-line-list", "cell_lines", "results", "response", "docs"):
        value = obj.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            for nested in ("docs", "results", "cell-line-list"):
                v = value.get(nested)
                if isinstance(v, list):
                    return [x for x in v if isinstance(x, dict)]
    return []


def names(record: dict[str, Any]) -> list[str]:
    vals: list[str] = []
    for key in ("id", "name", "recommendedName", "recommended_name"):
        v = record.get(key)
        if isinstance(v, str) and v.strip():
            vals.append(v.strip())
    for key in ("sy", "synonyms", "synonym"):
        v = record.get(key)
        if isinstance(v, str):
            vals.extend(x.strip() for x in re.split(r"[|;]", v) if x.strip())
        elif isinstance(v, list):
            vals.extend(str(x).strip() for x in v if str(x).strip())
    return vals


def accession(record: dict[str, Any]) -> str:
    for key in ("ac", "accession", "primaryAccession", "primary_accession"):
        v = record.get(key)
        if isinstance(v, str) and re.fullmatch(r"CVCL_[A-Z0-9]+", v.strip(), re.I):
            return v.strip().upper()
    return ""


def recommended(record: dict[str, Any]) -> str:
    for key in ("id", "recommendedName", "recommended_name", "name"):
        v = record.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def api_query(term: str, endpoint: str, timeout: int) -> list[dict[str, Any]]:
    q = urllib.parse.quote(f'idsy:"{term}"')
    url = f"{endpoint}?q={q}&fields=id,ac,sy&format=json&rows=50"
    req = urllib.request.Request(url, headers={"User-Agent": "PRIDE-SCP/validator-gated-closure"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return parse_records(json.load(response))


def unique_exact(term: str, records: list[dict[str, Any]]) -> dict[str, str] | None:
    target = norm(term)
    matched: dict[str, dict[str, Any]] = {}
    for record in records:
        ac = accession(record)
        if not ac:
            continue
        if any(norm(name) == target for name in names(record)):
            matched[ac] = record
    if len(matched) != 1:
        return None
    ac, record = next(iter(matched.items()))
    return {"accession": ac, "recommended_name": recommended(record), "query": term}


def detect_columns(headers: list[str]) -> tuple[int | None, int | None]:
    lower = [h.strip().lower() for h in headers]
    line = next((i for i, h in enumerate(lower) if h == "characteristics[cell line]"), None)
    canonical = next((i for i, h in enumerate(lower) if h == "characteristics[cellosaurus accession]"), None)
    if canonical is not None:
        return line, canonical
    legacy = next((i for i, h in enumerate(lower) if h == "characteristics[cell line accession]"), None)
    return line, legacy


def resolve_file(candidate: Path, output: Path, report: Path, endpoint: str, timeout: int, fixture: Path | None) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    with candidate.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        matrix = list(csv.reader(fh, delimiter="\t"))
    if not matrix:
        raise ValueError("empty SDRF")
    headers = matrix[0]
    rows = matrix[1:]
    line_idx, acc_idx = detect_columns(headers)
    if line_idx is None:
        shutil.copy2(candidate, output)
        result = {
            "schema_version": "pride-scp-cellosaurus-exact-resolver-v2",
            "status": "no_cell_line_column",
            "changed": False,
            "input": str(candidate),
            "output": str(output),
            "input_sha256": sha256_file(candidate),
            "output_sha256": sha256_file(output),
            "resolutions": [],
        }
        report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return result

    changed = False
    # Renaming a populated legacy alias is representation-only and preserves all
    # existing accession values.  A missing canonical column, however, is created
    # lazily only after at least one exact ontology resolution succeeds; adding an
    # empty column must not consume a closure pass.
    if acc_idx is not None and headers[acc_idx].strip().lower() == "characteristics[cell line accession]":
        headers[acc_idx] = "characteristics[cellosaurus accession]"
        changed = True

    fixture_obj = json.loads(fixture.read_text()) if fixture and fixture.is_file() else None
    cache: dict[str, dict[str, str] | None] = {}
    resolutions: list[dict[str, Any]] = []

    for row_no, row in enumerate(rows, start=2):
        while len(row) < len(headers):
            row.append("")
        term = row[line_idx].strip()
        current = row[acc_idx].strip() if acc_idx is not None and acc_idx < len(row) else ""
        if norm(term) in PLACEHOLDERS or re.fullmatch(r"CVCL_[A-Z0-9]+", current, re.I):
            continue
        # Composite cell-line identity is a row-mapping problem, not an ontology lookup.
        if ";" in term or "|" in term:
            resolutions.append({"row": row_no, "query": term, "status": "composite_unresolved"})
            continue
        if term not in cache:
            if fixture_obj is not None:
                recs = fixture_obj.get(term, []) if isinstance(fixture_obj, dict) else []
            else:
                try:
                    recs = api_query(term, endpoint, timeout)
                except Exception as exc:
                    resolutions.append({"row": row_no, "query": term, "status": "api_error", "detail": repr(exc)})
                    cache[term] = None
                    continue
            cache[term] = unique_exact(term, recs)
        hit = cache[term]
        if hit is None:
            resolutions.append({"row": row_no, "query": term, "status": "not_unique_exact"})
            continue
        if acc_idx is None:
            headers.append("characteristics[cellosaurus accession]")
            acc_idx = len(headers) - 1
            for existing in rows:
                while len(existing) < len(headers):
                    existing.append("")
        row[acc_idx] = hit["accession"]
        changed = True
        resolutions.append({"row": row_no, "query": term, "status": "resolved", **hit})

    if changed:
        with output.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, delimiter="\t", lineterminator="\n")
            w.writerow(headers)
            w.writerows(rows)
    else:
        shutil.copy2(candidate, output)

    result = {
        "schema_version": "pride-scp-cellosaurus-exact-resolver-v2",
        "status": "changed" if changed else "unchanged",
        "changed": changed,
        "input": str(candidate),
        "output": str(output),
        "input_sha256": sha256_file(candidate),
        "output_sha256": sha256_file(output),
        "resolutions": resolutions,
        "composite_unresolved": sum(x.get("status") == "composite_unresolved" for x in resolutions),
        "exact_resolved": sum(x.get("status") == "resolved" for x in resolutions),
    }
    report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def self_test() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root=Path(td)
        src=root/"x.tsv"; out=root/"y.tsv"; rep=root/"r.json"; fixture=root/"f.json"
        src.write_text("source name\tcharacteristics[cell line]\nS1\tHeLa\n",encoding="utf-8")
        fixture.write_text(json.dumps({"HeLa":[{"id":"HeLa","ac":"CVCL_0030","sy":["He-La"]}]}),encoding="utf-8")
        res=resolve_file(src,out,rep,API,5,fixture)
        assert res["changed"] is True
        rendered = out.read_text()
        assert "CVCL_0030" in rendered
        assert "characteristics[cellosaurus accession]" in rendered
        assert "characteristics[cell line accession]" not in rendered

        legacy = root/"legacy.tsv"; legacy_out=root/"legacy_out.tsv"; legacy_rep=root/"legacy_report.json"
        legacy.write_text(
            "source name\tcharacteristics[cell line]\tcharacteristics[cell line accession]\n"
            "S1\tUnknownLine\tCVCL_9999\n", encoding="utf-8"
        )
        legacy_res=resolve_file(legacy,legacy_out,legacy_rep,API,5,root/"missing_fixture.json")
        assert legacy_res["changed"] is True
        legacy_text=legacy_out.read_text()
        assert "characteristics[cellosaurus accession]" in legacy_text
        assert "characteristics[cell line accession]" not in legacy_text
        assert "CVCL_9999" in legacy_text

        composite = root/"composite.tsv"; composite_out=root/"composite_out.tsv"; composite_rep=root/"composite_report.json"
        composite.write_text("source name\tcharacteristics[cell line]\nS1\tHeLa; A549\n", encoding="utf-8")
        composite_res=resolve_file(composite,composite_out,composite_rep,API,5,fixture)
        assert composite_res["changed"] is False
        assert composite_out.read_bytes() == composite.read_bytes()
        assert "characteristics[cellosaurus accession]" not in composite_out.read_text()
        assert composite_res["composite_unresolved"] == 1
    print("sdrf_cellosaurus_resolver self-test: PASS")


def parser() -> argparse.ArgumentParser:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test",action="store_true")
    p.add_argument("--candidate",type=Path)
    p.add_argument("--output",type=Path)
    p.add_argument("--report",type=Path)
    p.add_argument("--endpoint",default=API)
    p.add_argument("--timeout",type=int,default=30)
    p.add_argument("--fixture",type=Path)
    return p


def main() -> int:
    args=parser().parse_args()
    if args.self_test:
        self_test(); return 0
    if args.candidate is None or args.output is None or args.report is None:
        raise SystemExit("--candidate, --output and --report are required")
    result=resolve_file(args.candidate,args.output,args.report,args.endpoint,args.timeout,args.fixture)
    print(json.dumps(result,indent=2))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
