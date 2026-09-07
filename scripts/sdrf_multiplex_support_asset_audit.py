#!/usr/bin/env python3
"""Non-generative support-asset audit for residual true-multiplex PRIDE SCP datasets.

After PXD028040 demonstrated that deposited experimental-design/support files can carry the
sample/run/reporter relationships needed for deterministic SDRF reconstruction, this audit applies
that lesson to the remaining source-grounded ``multiplex_supported`` cohort without generating any
SDRF rows.

The audit:
  * derives repository file inventories from the local PRIDE snapshot;
  * identifies small public design/metadata/readme/SDRF/tabular support assets;
  * downloads only bounded non-RAW support files with explicit public URIs;
  * parses XLSX/CSV/TSV/TXT/JSON/XML/YAML-like text where possible;
  * surfaces rows/lines with single-cell, reporter-channel, carrier/reference/blank/control,
    run/file/sample, or replicate semantics;
  * never promotes filename words or chemistry alone into a reporter mapping.

GT metadata is not read and never supplies SDRF fields.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

from sdrf_reporter_run_scope_audit import (
    RepoFile,
    download_support_file,
    is_raw_file,
    repository_files,
)
from sdrf_reporter_design_semantic_audit import parse_xlsx_structured

AUDITOR_VERSION = "pride-scp-sdrf-multiplex-support-asset-auditor-v0.1"

PARSEABLE_EXTENSIONS = {".xlsx", ".csv", ".tsv", ".txt", ".json", ".xml", ".yaml", ".yml"}
DISCOVERY_EXTENSIONS = PARSEABLE_EXTENSIONS | {".xls", ".pdf", ".zip", ".gz", ".tar"}
SUPPORT_NAME_RE = re.compile(
    r"(?i)(?:experimental[_ .-]?design|experiment[_ .-]?design|design|metadata|sample|run|file|mapping|map|"
    r"annotation|sdrf|readme|description|manifest|layout|channel|reporter|label|tmt|itraq|plex|supplement)"
)
CHEMISTRY_RE = re.compile(r"(?i)(?:\bTMT(?:pro)?(?=\b|\s*[-_ ]?\d)|\btandem\s+mass\s+tag\b|\biTRAQ\b)")
SINGLE_RE = re.compile(
    r"(?i)\b(?:single[- _]?(?:cell|neuron)|individual\s+(?:cell|neuron)|single[- _]?som(?:a|al)|"
    r"somal\s+aspirate|single[- _]?cell\s+proteom|SCoPE(?:2)?|nanoPOTS|nPOP)\b"
)
ROLE_PATTERNS = {
    "carrier": re.compile(r"(?i)\bcarrier(?:\s+channel)?\b"),
    "reference": re.compile(r"(?i)\b(?:reference|bridge)(?:\s+channel)?\b"),
    "blank": re.compile(r"(?i)\b(?:blank|empty|negative\s+control)(?:\s+channel)?\b"),
    "control": re.compile(r"(?i)\bcontrol(?:\s+channel|\s+sample|\s+well)?\b"),
    "analytical": re.compile(r"(?i)\b(?:analytical|analyte|sample)(?:\s+channel)?\b"),
}
RUN_SEMANTIC_RE = re.compile(
    r"(?i)\b(?:raw\s*file|raw\s*name|file\s*name|run(?:\s*(?:id|name|number))?|acquisition|batch|set|plex|"
    r"replicate|sample\s*(?:id|name|number)|well|cell\s*(?:id|name|number))\b"
)
REPORTER_TOKEN_RE = re.compile(
    r"(?i)(?<!\d)(?:TMT(?:pro)?\s*[-_ ]*)?(126|127[NC]?|128[NC]?|129[NC]?|130[NC]?|131[NC]?|132[NC]?|133[NC]?|134[NC]?|135[NC]?)(?!\d)"
)
RAW_TOKEN_RE = re.compile(r"(?i)\b[^\s|,;]+\.RAW\b")


@dataclass
class EvidenceHit:
    accession: str
    support_file: str
    location: str
    chemistry: bool
    single_cell: bool
    roles: list[str]
    reporter_channels: list[str]
    run_semantics: bool
    raw_tokens: list[str]
    text: str


@dataclass
class AssetStatus:
    accession: str
    file_name: str
    category: str
    uri: str
    extension: str
    selected: bool
    parseable: bool
    acquisition_status: str
    parsed_units: int
    evidence_hits: int


def norm(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def reporter_channels(text: str) -> list[str]:
    out = {m.group(1).upper() for m in REPORTER_TOKEN_RE.finditer(text)}
    return sorted(out)


def role_flags(text: str) -> list[str]:
    return sorted(name for name, rx in ROLE_PATTERNS.items() if rx.search(text))


def evidence_hit(accession: str, support_file: str, location: str, text: str) -> EvidenceHit | None:
    text = norm(text)
    if not text:
        return None
    chem = bool(CHEMISTRY_RE.search(text))
    single = bool(SINGLE_RE.search(text))
    roles = role_flags(text)
    channels = reporter_channels(text)
    run = bool(RUN_SEMANTIC_RE.search(text))
    raw_tokens = sorted({m.group(0) for m in RAW_TOKEN_RE.finditer(text)}, key=str.lower)

    # Keep only evidence-bearing units. A chemistry-only line is useful for source triage, but a
    # bare generic sentence with none of these signals is not.
    if not (chem or single or roles or channels or run or raw_tokens):
        return None
    return EvidenceHit(
        accession=accession,
        support_file=support_file,
        location=location,
        chemistry=chem,
        single_cell=single,
        roles=roles,
        reporter_channels=channels,
        run_semantics=run,
        raw_tokens=raw_tokens,
        text=text[:4000],
    )


def support_candidate(row: RepoFile) -> bool:
    if is_raw_file(row):
        return False
    lower = row.name.lower()
    ext = Path(lower).suffix.lower()
    if lower.endswith(".sdrf.tsv"):
        return True
    if ext not in DISCOVERY_EXTENSIONS:
        return False
    # Readme and metadata/design-like files are preferred. Tabular files are also retained because
    # many PRIDE deposits use generic filenames such as "samples.xlsx" or "table1.csv".
    if SUPPORT_NAME_RE.search(row.name):
        return True
    return ext in {".xlsx", ".xls", ".csv", ".tsv"}


def parse_text_units(path: Path) -> list[tuple[str, str]]:
    ext = path.suffix.lower()
    if ext == ".xlsx":
        units: list[tuple[str, str]] = []
        for row in parse_xlsx_structured(path):
            units.append((f"{row.sheet}:row{row.row_number}", row.text))
        return units
    text = path.read_text(errors="replace")
    if ext == ".json":
        try:
            obj = json.loads(text)
            text = json.dumps(obj, ensure_ascii=False, indent=2)
        except Exception:
            pass
    # Keep physical line numbers; adjacent context can be recovered from the downloaded file if a
    # hit is selected for the next bounded reconstruction iteration.
    return [(f"line{idx}", line) for idx, line in enumerate(text.splitlines(), start=1) if line.strip()]


def publication_text_lookup(reporter_audit_tsv: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    with reporter_audit_tsv.open() as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            try:
                out[row["accession"]] = int(row.get("publication_text_rows") or 0)
            except ValueError:
                out[row["accession"]] = 0
    return out


def audit_accession(
    accession: str,
    files_json: Path,
    output: Path,
    max_bytes: int,
) -> tuple[dict[str, object], list[AssetStatus], list[EvidenceHit]]:
    obj = json.loads(files_json.read_text(errors="replace"))
    repo = repository_files(obj)
    raws = [x for x in repo if is_raw_file(x)]
    candidates = [x for x in repo if support_candidate(x)]
    asset_dir = output / "support_files" / accession
    statuses: list[AssetStatus] = []
    hits: list[EvidenceHit] = []

    for row in candidates:
        ext = Path(row.name).suffix.lower()
        if row.name.lower().endswith(".sdrf.tsv"):
            ext = ".tsv"
        parseable = ext in PARSEABLE_EXTENSIONS
        acquired: Path | None = None
        status = "not_downloaded_unparseable" if not parseable else "pending"
        units: list[tuple[str, str]] = []
        local_hits: list[EvidenceHit] = []
        if parseable:
            acquired, status = download_support_file(row, asset_dir, max_bytes=max_bytes)
            if acquired is not None:
                try:
                    units = parse_text_units(acquired)
                    for location, text in units:
                        hit = evidence_hit(accession, row.name, location, text)
                        if hit is not None:
                            local_hits.append(hit)
                    hits.extend(local_hits)
                except Exception as exc:
                    status = f"parse_error:{type(exc).__name__}:{exc}"
                    units = []
                    local_hits = []
        statuses.append(
            AssetStatus(
                accession=accession,
                file_name=row.name,
                category=row.category,
                uri=row.uri,
                extension=ext,
                selected=True,
                parseable=parseable,
                acquisition_status=status,
                parsed_units=len(units),
                evidence_hits=len(local_hits),
            )
        )

    explicit_role_hits = [h for h in hits if h.chemistry and h.roles and h.reporter_channels]
    run_linkage_hits = [h for h in hits if h.run_semantics and (h.raw_tokens or h.single_cell or h.reporter_channels)]
    sample_layout_hits = [h for h in hits if h.single_cell and h.chemistry and len(h.reporter_channels) >= 1]

    if explicit_role_hits and run_linkage_hits:
        cls = "support_asset_role_and_run_evidence"
    elif explicit_role_hits:
        cls = "support_asset_explicit_reporter_role_evidence"
    elif sample_layout_hits:
        cls = "support_asset_partial_single_cell_reporter_layout"
    elif hits:
        cls = "support_asset_semantic_evidence"
    elif candidates:
        cls = "support_assets_without_parseable_mapping_evidence"
    else:
        cls = "no_repository_support_assets"

    summary = {
        "accession": accession,
        "repository_files": len(repo),
        "raw_files": len(raws),
        "support_candidates": len(candidates),
        "parseable_support_candidates": sum(s.parseable for s in statuses),
        "support_files_acquired": sum(s.acquisition_status in {"downloaded", "cached"} for s in statuses),
        "evidence_hits": len(hits),
        "explicit_reporter_role_hits": len(explicit_role_hits),
        "run_linkage_hits": len(run_linkage_hits),
        "single_cell_reporter_layout_hits": len(sample_layout_hits),
        "support_class": cls,
    }
    return summary, statuses, hits


def write_tsv(path: Path, rows: Iterable[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def run(args: argparse.Namespace) -> int:
    output: Path = args.output
    output.mkdir(parents=True, exist_ok=True)
    accessions = [x.strip() for x in args.accessions_file.read_text().splitlines() if x.strip()]
    pub_rows = publication_text_lookup(args.reporter_audit_tsv)

    summaries: list[dict[str, object]] = []
    statuses: list[AssetStatus] = []
    hits: list[EvidenceHit] = []
    missing_files_json: list[str] = []

    for accession in accessions:
        files_json = args.snapshot / "files" / f"{accession}.json"
        if not files_json.is_file():
            missing_files_json.append(accession)
            summaries.append({
                "accession": accession,
                "repository_files": 0,
                "raw_files": 0,
                "support_candidates": 0,
                "parseable_support_candidates": 0,
                "support_files_acquired": 0,
                "evidence_hits": 0,
                "explicit_reporter_role_hits": 0,
                "run_linkage_hits": 0,
                "single_cell_reporter_layout_hits": 0,
                "support_class": "missing_repository_file_snapshot",
                "publication_text_rows": pub_rows.get(accession, 0),
            })
            continue
        summary, acc_statuses, acc_hits = audit_accession(accession, files_json, output, args.max_bytes)
        summary["publication_text_rows"] = pub_rows.get(accession, 0)
        summaries.append(summary)
        statuses.extend(acc_statuses)
        hits.extend(acc_hits)

    summary_fields = [
        "accession", "repository_files", "raw_files", "support_candidates", "parseable_support_candidates",
        "support_files_acquired", "publication_text_rows", "evidence_hits", "explicit_reporter_role_hits",
        "run_linkage_hits", "single_cell_reporter_layout_hits", "support_class",
    ]
    write_tsv(output / "sdrf_multiplex_support_asset_audit.tsv", summaries, summary_fields)

    status_fields = list(AssetStatus.__dataclass_fields__)
    write_tsv(output / "support_asset_status.tsv", (asdict(x) for x in statuses), status_fields)

    hit_fields = [
        "accession", "support_file", "location", "chemistry", "single_cell", "roles",
        "reporter_channels", "run_semantics", "raw_tokens", "text",
    ]
    hit_rows = []
    for h in hits:
        d = asdict(h)
        for key in ("roles", "reporter_channels", "raw_tokens"):
            d[key] = ",".join(d[key])
        hit_rows.append(d)
    write_tsv(output / "support_asset_evidence_hits.tsv", hit_rows, hit_fields)

    counts: dict[str, int] = {}
    for row in summaries:
        cls = str(row["support_class"])
        counts[cls] = counts.get(cls, 0) + 1
    overall = {
        "auditor_version": AUDITOR_VERSION,
        "accessions": len(accessions),
        "missing_repository_file_snapshots": missing_files_json,
        "support_class_counts": counts,
        "accessions_with_publication_text": sum(int(r.get("publication_text_rows", 0)) > 0 for r in summaries),
        "accessions_with_support_candidates": sum(int(r.get("support_candidates", 0)) > 0 for r in summaries),
        "accessions_with_acquired_support_files": sum(int(r.get("support_files_acquired", 0)) > 0 for r in summaries),
        "accessions_with_explicit_reporter_role_hits": sum(int(r.get("explicit_reporter_role_hits", 0)) > 0 for r in summaries),
        "accessions_with_run_linkage_hits": sum(int(r.get("run_linkage_hits", 0)) > 0 for r in summaries),
        "non_generative": True,
        "outputs": {
            "inventory": str(output / "sdrf_multiplex_support_asset_audit.tsv"),
            "asset_status": str(output / "support_asset_status.tsv"),
            "evidence_hits": str(output / "support_asset_evidence_hits.tsv"),
        },
    }
    (output / "sdrf_multiplex_support_asset_audit_summary.json").write_text(json.dumps(overall, indent=2) + "\n")
    print(json.dumps(overall, indent=2))
    for row in summaries:
        print(
            f"{row['accession']} class={row['support_class']} raw={row['raw_files']} "
            f"support={row['support_candidates']} acquired={row['support_files_acquired']} "
            f"pub_text={row['publication_text_rows']} hits={row['evidence_hits']} "
            f"role_hits={row['explicit_reporter_role_hits']} run_hits={row['run_linkage_hits']}"
        )
    return 0


def self_test() -> None:
    assert support_candidate(RepoFile("experimental_design.xlsx", "OTHER", "https://example.test/design.xlsx"))
    assert support_candidate(RepoFile("samples.tsv", "OTHER", "https://example.test/samples.tsv"))
    assert not support_candidate(RepoFile("sample01.RAW", "RAW", "https://example.test/sample01.RAW"))
    assert not support_candidate(RepoFile("search_results.mzid", "RESULT", "https://example.test/search_results.mzid"))

    hit = evidence_hit(
        "PXDTEST",
        "design.tsv",
        "line2",
        "single cell sample reporter channel TMTpro126 analytical; TMTpro127N carrier; run sample01.RAW",
    )
    assert hit is not None
    assert hit.chemistry and hit.single_cell and hit.run_semantics
    assert hit.reporter_channels == ["126", "127N"]
    assert "analytical" in hit.roles and "carrier" in hit.roles
    assert hit.raw_tokens == ["sample01.RAW"]

    # Chemistry alone is surfaced for source triage but cannot itself be classified as explicit role evidence.
    chem = evidence_hit("PXDTEST", "readme.txt", "line1", "Samples were labeled with TMTpro.")
    assert chem is not None and chem.chemistry and not chem.roles and not chem.reporter_channels

    # Bare numbers without surrounding reporter semantics may be tokenized, but without chemistry/roles/run/single
    # semantics they must not be treated as a meaningful evidence unit.
    bare = evidence_hit("PXDTEST", "notes.txt", "line1", "126 127 128")
    assert bare is not None  # retained diagnostically because reporter-like tokens are present
    assert not bare.chemistry and not bare.roles
    print("sdrf_multiplex_support_asset_audit self-test: PASS")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--accessions-file", type=Path)
    p.add_argument("--snapshot", type=Path)
    p.add_argument("--reporter-audit-tsv", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--max-bytes", type=int, default=25 * 1024 * 1024)
    p.add_argument("--self-test", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        self_test()
        return 0
    for name in ("accessions_file", "snapshot", "reporter_audit_tsv", "output"):
        if getattr(args, name) is None:
            raise SystemExit(f"ERROR: --{name.replace('_', '-')} is required")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
