#!/usr/bin/env python3
"""High-specificity non-generative support-asset audit for residual multiplex PRIDE SCP datasets.

v0.4.6 proved that repository support assets can be discovered, but it also showed why a permissive
semantic scan is unsafe: peptide/protein result tables can contain words such as ``carrier``, ``run``
and ``control`` as protein-name vocabulary, while bare integers such as 128/131/132 occur for many
unrelated reasons.  This maintained v0.2 auditor therefore separates *asset triage* from *semantic
mapping evidence* and requires local reporter/run/sample syntax before a unit can influence the next
SDRF reconstruction decision.

The audit remains non-generative.  GT metadata is never read and never supplies SDRF fields.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from sdrf_reporter_run_scope_audit import (
    RepoFile,
    download_support_file,
    is_raw_file,
    repository_files,
)
from sdrf_reporter_design_semantic_audit import parse_xlsx_structured

AUDITOR_VERSION = "pride-scp-sdrf-multiplex-support-asset-auditor-v0.2"

PARSEABLE_EXTENSIONS = {".xlsx", ".csv", ".tsv", ".txt", ".json", ".xml", ".yaml", ".yml"}
DISCOVERY_EXTENSIONS = PARSEABLE_EXTENSIONS | {".xls", ".pdf", ".zip", ".gz", ".tar"}

# High-value names describe design/sample relationships rather than downstream search results.
STRONG_SUPPORT_NAME_RE = re.compile(
    r"(?i)(?:experimental[_ .-]?design|experiment[_ .-]?design|sample[_ .-]?(?:sheet|map|mapping|metadata)|"
    r"samplesheet|run[_ .-]?(?:map|mapping|manifest)|file[_ .-]?(?:map|mapping|manifest)|"
    r"channel[_ .-]?(?:map|mapping|layout)|reporter[_ .-]?(?:map|mapping|layout)|"
    r"metadata|annotation|sdrf|manifest|design)"
)
TEXT_SUPPORT_NAME_RE = re.compile(r"(?i)(?:^|[_ .-])(?:readme|description|notes?|methods?|supplement(?:ary)?)(?:[_ .-]|$)")

# Search-engine / quantitative result tables are not experimental-design sources.  Strong design
# names override this list, but generic result filenames do not.
RESULT_LIKE_NAME_RE = re.compile(
    r"(?i)(?:realtimesearch|real[_ .-]?time[_ .-]?search|search[_ .-]?(?:results?|output)|"
    r"(?:^|[_ .-])results?(?:[_ .-]|$)|peptide|protein[_ .-]?(?:groups?|results?|quant)|"
    r"psm|percolator|mokapot|msms|spectra|spectrum|feature[_ .-]?(?:table|quant)|"
    r"quant(?:ification|ified)?|abundance|intensity|identification|mzid|mzidentml|"
    r"maxquant|fragpipe|proteome[_ .-]?discoverer|spectronaut|diann|dia-nn|skyline)"
)
RESULT_LIKE_CATEGORY_RE = re.compile(r"(?i)(?:RESULT|SEARCH|PEPTIDE|PROTEIN|SPECTR|IDENTIFICATION|QUANT)")
DESIGN_CATEGORY_RE = re.compile(r"(?i)(?:DESIGN|METADATA|SAMPLE|ANNOTATION|SDRF|OTHER)")

CHEMISTRY_RE = re.compile(r"(?i)(?:\bTMT(?:pro)?(?=\b|\s*[-_ ]?\d)|\btandem\s+mass\s+tag\b|\biTRAQ\b)")
SINGLE_RE = re.compile(
    r"(?i)\b(?:single[- _]?(?:cell|neuron)|individual\s+(?:cell|neuron)|single[- _]?som(?:a|al)|"
    r"somal\s+aspirate|single[- _]?cell\s+proteom|SCoPE(?:2)?|nanoPOTS|nPOP)\b"
)
REPORTER_CONTEXT_RE = re.compile(r"(?i)\b(?:reporter|channel|tag(?:ged|ging)?|label(?:ed|led|ing)?)\b")

PREFIXED_REPORTER_RE = re.compile(
    r"(?i)\bTMT(?:pro)?\s*[-_ ]*"
    r"(126|127[NC]?|128[NC]?|129[NC]?|130[NC]?|131[NC]?|132[NC]?|133[NC]?|134[NC]?|135[NC]?)\b"
)
BARE_REPORTER_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(126|127[NC]?|128[NC]?|129[NC]?|130[NC]?|131[NC]?|132[NC]?|133[NC]?|134[NC]?|135[NC]?)"
    r"(?![A-Za-z0-9])",
    re.I,
)
RAW_TOKEN_RE = re.compile(r"(?i)(?<![A-Za-z0-9_.-])[^\s|,;]+\.RAW\b")

EXPLICIT_RUN_RE = re.compile(
    r"(?i)\b(?:raw\s*(?:file|name)|file\s*(?:name|id)|run\s*(?:id|name|number)|"
    r"acquisition\s*(?:id|name|number)|sample\s*(?:id|name|number)|batch\s*(?:id|name|number)?|"
    r"set\s*(?:id|name|number)|well\s*(?:id|name|number)?|technical\s+replicate|biological\s+replicate|replicate\s*(?:id|name|number))\b"
)

PROTEIN_CARRIER_RE = re.compile(
    r"(?i)\b(?:solute\s+carrier|mitochondrial\s+(?:\w+\s+){0,3}carrier|carrier\s+protein|"
    r"carrier-associated\s+membrane\s+protein)\b"
)
PROTEIN_CONTROL_RE = re.compile(r"(?i)\b(?:cell\s+(?:cycle|division)\s+control|control\s+protein)\b")


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
    evidence_tier: str
    text: str


@dataclass
class AssetStatus:
    accession: str
    file_name: str
    category: str
    uri: str
    extension: str
    selected: bool
    priority: str
    triage_reason: str
    parseable: bool
    acquisition_status: str
    parsed_units: int
    evidence_hits: int
    credible_evidence_hits: int


def norm(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _channel_context(text: str, start: int, end: int, radius: int = 56) -> str:
    return text[max(0, start - radius): min(len(text), end + radius)]


def reporter_channels(text: str) -> list[str]:
    """Return only reporter numbers with explicit TMT or reporter/channel/label context."""
    found = {m.group(1).upper() for m in PREFIXED_REPORTER_RE.finditer(text)}
    for match in BARE_REPORTER_RE.finditer(text):
        window = _channel_context(text, match.start(), match.end())
        if CHEMISTRY_RE.search(window) or REPORTER_CONTEXT_RE.search(window):
            found.add(match.group(1).upper())
    return sorted(found)


def role_flags(text: str, chemistry: bool, channels: list[str]) -> list[str]:
    """Recognize mapping roles while rejecting common protein-name homonyms."""
    roles: set[str] = set()
    local_reporter_context = chemistry or bool(channels) or bool(REPORTER_CONTEXT_RE.search(text))

    # Explicit table/layout syntax such as ``channel 126 = blank`` or
    # ``reporter 131N: carrier`` is strong role evidence even when the role follows the token.
    reporter_role_after = re.compile(
        r"(?i)\b(?:channel|reporter)\s*(?:126|127[NC]?|128[NC]?|129[NC]?|130[NC]?|131[NC]?|132[NC]?|133[NC]?|134[NC]?|135[NC]?)"
        r"\s*(?:=|:|->|-)\s*(carrier|reference|bridge|blank|control|analytical|analyte)\b"
    )
    for match in reporter_role_after.finditer(text):
        role = match.group(1).lower()
        if role == "bridge":
            role = "reference"
        elif role == "analyte":
            role = "analytical"
        roles.add(role)

    if re.search(r"(?i)\bcarrier\s+(?:channel|reporter|sample|proteome|material|digest)\b", text):
        roles.add("carrier")
    elif local_reporter_context and re.search(r"(?i)\bcarrier\b", text) and not PROTEIN_CARRIER_RE.search(text):
        roles.add("carrier")

    if re.search(r"(?i)\b(?:reference|bridge)\s+(?:channel|reporter|sample|proteome|material)\b", text):
        roles.add("reference")
    elif local_reporter_context and re.search(r"(?i)\b(?:reference|bridge)\b", text):
        roles.add("reference")

    if re.search(r"(?i)\b(?:blank|empty|negative\s+control)\s+(?:channel|reporter|sample|well)\b", text):
        roles.add("blank")

    if re.search(r"(?i)\bcontrol\s+(?:channel|reporter|sample|well)\b", text):
        roles.add("control")
    elif local_reporter_context and re.search(r"(?i)\bcontrol\b", text) and not PROTEIN_CONTROL_RE.search(text):
        roles.add("control")

    if re.search(r"(?i)\b(?:analytical|analyte)\s+(?:channel|reporter|sample|digest|material)\b", text):
        roles.add("analytical")
    elif local_reporter_context and re.search(r"(?i)\b(?:analytical|analyte)\b", text):
        roles.add("analytical")
    if re.search(r"(?i)\bsample\s+(?:channel|reporter)\b", text):
        roles.add("analytical")

    return sorted(roles)


def evidence_hit(accession: str, support_file: str, location: str, text: str) -> EvidenceHit | None:
    text = norm(text)
    if not text:
        return None
    chem = bool(CHEMISTRY_RE.search(text))
    single = bool(SINGLE_RE.search(text))
    channels = reporter_channels(text)
    roles = role_flags(text, chem, channels)
    raw_tokens = sorted({m.group(0) for m in RAW_TOKEN_RE.finditer(text)}, key=str.lower)
    run = bool(raw_tokens or EXPLICIT_RUN_RE.search(text))

    # Do not retain arbitrary protein/result lines just because they contain a bare channel-sized
    # integer or a homonymous English word.  Run semantics alone are also insufficient unless tied
    # to relevant chemistry/sample/channel context.
    relevant_run = run and bool(chem or single or channels or roles or raw_tokens)
    if not (chem or single or roles or channels or raw_tokens or relevant_run):
        return None

    explicit_role = bool(roles and channels)
    run_linked = bool(raw_tokens or (run and (chem or single or channels or roles)))
    if explicit_role and run_linked:
        tier = "reporter_role_and_run"
    elif explicit_role:
        tier = "explicit_reporter_role"
    elif single and chem and channels:
        tier = "single_cell_reporter_layout"
    elif run_linked and (single or chem or channels):
        tier = "run_or_sample_linkage"
    else:
        tier = "source_triage_only"

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
        evidence_tier=tier,
        text=text[:4000],
    )


def asset_triage(row: RepoFile) -> tuple[bool, str, str]:
    """Return selected, priority, reason for a non-RAW repository asset."""
    if is_raw_file(row):
        return False, "excluded", "raw_file"
    lower = row.name.lower()
    ext = Path(lower).suffix.lower()
    if lower.endswith(".sdrf.tsv"):
        ext = ".tsv"
    if ext not in DISCOVERY_EXTENSIONS:
        return False, "excluded", "unsupported_extension"

    strong_name = bool(STRONG_SUPPORT_NAME_RE.search(row.name)) or lower.endswith(".sdrf.tsv")
    text_name = bool(TEXT_SUPPORT_NAME_RE.search(row.name))
    result_like = bool(RESULT_LIKE_NAME_RE.search(row.name)) or bool(RESULT_LIKE_CATEGORY_RE.search(row.category))

    if strong_name:
        return True, "high", "explicit_design_or_metadata_name"
    if result_like:
        return False, "excluded", "result_like_asset"
    if text_name and ext in {".txt", ".json", ".xml", ".yaml", ".yml", ".pdf"}:
        return True, "medium", "readme_or_methods_name"
    if ext in {".xlsx", ".xls"}:
        return True, "medium", "non_result_workbook_fallback"
    if ext in {".csv", ".tsv"} and DESIGN_CATEGORY_RE.search(row.category):
        return True, "low", "non_result_tabular_repository_asset"
    return False, "excluded", "no_design_signal"


def support_candidate(row: RepoFile) -> bool:
    return asset_triage(row)[0]


def parse_text_units(path: Path) -> list[tuple[str, str]]:
    ext = path.suffix.lower()
    if ext == ".xlsx":
        return [(f"{row.sheet}:row{row.row_number}", row.text) for row in parse_xlsx_structured(path)]
    text = path.read_text(errors="replace")
    if ext == ".json":
        try:
            obj = json.loads(text)
            text = json.dumps(obj, ensure_ascii=False, indent=2)
        except Exception:
            pass
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


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name)


def acquire_selected_asset(
    row: RepoFile,
    asset_dir: Path,
    max_bytes: int,
    reuse_root: Path | None,
    accession: str,
) -> tuple[Path | None, str]:
    if reuse_root is not None:
        prior = reuse_root / accession / _safe_name(row.name)
        if prior.is_file() and prior.stat().st_size > 0:
            asset_dir.mkdir(parents=True, exist_ok=True)
            target = asset_dir / _safe_name(row.name)
            if not target.exists():
                shutil.copy2(prior, target)
            return target, "reused_v046_cache"
    return download_support_file(row, asset_dir, max_bytes=max_bytes)


def audit_accession(
    accession: str,
    files_json: Path,
    output: Path,
    max_bytes: int,
    reuse_root: Path | None,
) -> tuple[dict[str, object], list[AssetStatus], list[EvidenceHit]]:
    obj = json.loads(files_json.read_text(errors="replace"))
    repo = repository_files(obj)
    raws = [x for x in repo if is_raw_file(x)]
    auditable_assets = [
        x for x in repo
        if not is_raw_file(x)
        and (Path(x.name.lower()).suffix.lower() in DISCOVERY_EXTENSIONS or x.name.lower().endswith(".sdrf.tsv"))
    ]
    asset_dir = output / "support_files" / accession
    statuses: list[AssetStatus] = []
    hits: list[EvidenceHit] = []

    for row in auditable_assets:
        ext = Path(row.name).suffix.lower()
        if row.name.lower().endswith(".sdrf.tsv"):
            ext = ".tsv"
        selected, priority, triage_reason = asset_triage(row)
        parseable = ext in PARSEABLE_EXTENSIONS
        units: list[tuple[str, str]] = []
        local_hits: list[EvidenceHit] = []
        status = "excluded_by_triage"

        if selected and not parseable:
            status = "selected_unparseable"
        elif selected:
            acquired, status = acquire_selected_asset(row, asset_dir, max_bytes, reuse_root, accession)
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

        credible = [h for h in local_hits if h.evidence_tier != "source_triage_only"]
        statuses.append(
            AssetStatus(
                accession=accession,
                file_name=row.name,
                category=row.category,
                uri=row.uri,
                extension=ext,
                selected=selected,
                priority=priority,
                triage_reason=triage_reason,
                parseable=parseable,
                acquisition_status=status,
                parsed_units=len(units),
                evidence_hits=len(local_hits),
                credible_evidence_hits=len(credible),
            )
        )

    selected_statuses = [s for s in statuses if s.selected]
    credible_hits = [h for h in hits if h.evidence_tier != "source_triage_only"]
    explicit_role_hits = [h for h in hits if h.evidence_tier in {"explicit_reporter_role", "reporter_role_and_run"}]
    role_and_run_hits = [h for h in hits if h.evidence_tier == "reporter_role_and_run"]
    run_linkage_hits = [h for h in hits if h.evidence_tier in {"run_or_sample_linkage", "reporter_role_and_run"}]
    sample_layout_hits = [h for h in hits if h.evidence_tier == "single_cell_reporter_layout"]

    if role_and_run_hits:
        cls = "support_asset_reconstruction_candidate"
    elif explicit_role_hits and run_linkage_hits:
        cls = "support_asset_role_plus_separate_run_evidence"
    elif explicit_role_hits:
        cls = "support_asset_explicit_reporter_role_evidence"
    elif sample_layout_hits or run_linkage_hits:
        cls = "support_asset_partial_mapping_evidence"
    elif credible_hits:
        cls = "support_asset_semantic_evidence"
    elif selected_statuses:
        cls = "selected_support_assets_without_mapping_evidence"
    else:
        cls = "no_high_value_repository_support_assets"

    summary = {
        "accession": accession,
        "repository_files": len(repo),
        "raw_files": len(raws),
        "auditable_non_raw_assets": len(statuses),
        "support_candidates": len(selected_statuses),
        "high_priority_support_candidates": sum(s.priority == "high" for s in selected_statuses),
        "medium_priority_support_candidates": sum(s.priority == "medium" for s in selected_statuses),
        "low_priority_support_candidates": sum(s.priority == "low" for s in selected_statuses),
        "result_like_assets_excluded": sum(s.triage_reason == "result_like_asset" for s in statuses),
        "support_files_acquired": sum(
            s.acquisition_status in {"downloaded", "cached", "reused_v046_cache"} for s in selected_statuses
        ),
        "evidence_hits": len(hits),
        "credible_evidence_hits": len(credible_hits),
        "explicit_reporter_role_hits": len(explicit_role_hits),
        "reporter_role_and_run_hits": len(role_and_run_hits),
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
                "auditable_non_raw_assets": 0,
                "support_candidates": 0,
                "high_priority_support_candidates": 0,
                "medium_priority_support_candidates": 0,
                "low_priority_support_candidates": 0,
                "result_like_assets_excluded": 0,
                "support_files_acquired": 0,
                "evidence_hits": 0,
                "credible_evidence_hits": 0,
                "explicit_reporter_role_hits": 0,
                "reporter_role_and_run_hits": 0,
                "run_linkage_hits": 0,
                "single_cell_reporter_layout_hits": 0,
                "support_class": "missing_repository_file_snapshot",
                "publication_text_rows": pub_rows.get(accession, 0),
            })
            continue
        summary, acc_statuses, acc_hits = audit_accession(
            accession, files_json, output, args.max_bytes, args.reuse_support_root
        )
        summary["publication_text_rows"] = pub_rows.get(accession, 0)
        summaries.append(summary)
        statuses.extend(acc_statuses)
        hits.extend(acc_hits)

    summary_fields = [
        "accession", "repository_files", "raw_files", "auditable_non_raw_assets", "support_candidates",
        "high_priority_support_candidates", "medium_priority_support_candidates", "low_priority_support_candidates",
        "result_like_assets_excluded", "support_files_acquired", "publication_text_rows", "evidence_hits",
        "credible_evidence_hits", "explicit_reporter_role_hits", "reporter_role_and_run_hits",
        "run_linkage_hits", "single_cell_reporter_layout_hits", "support_class",
    ]
    write_tsv(output / "sdrf_multiplex_support_asset_audit.tsv", summaries, summary_fields)

    status_fields = list(AssetStatus.__dataclass_fields__)
    write_tsv(output / "support_asset_status.tsv", (asdict(x) for x in statuses), status_fields)

    hit_fields = [
        "accession", "support_file", "location", "chemistry", "single_cell", "roles",
        "reporter_channels", "run_semantics", "raw_tokens", "evidence_tier", "text",
    ]
    hit_rows: list[dict[str, object]] = []
    for h in hits:
        d = asdict(h)
        for key in ("roles", "reporter_channels", "raw_tokens"):
            d[key] = ",".join(d[key])
        hit_rows.append(d)
    write_tsv(output / "support_asset_evidence_hits.tsv", hit_rows, hit_fields)

    credible_rows = [r for r in hit_rows if r["evidence_tier"] != "source_triage_only"]
    write_tsv(output / "support_asset_credible_evidence_hits.tsv", credible_rows, hit_fields)

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
        "accessions_with_selected_support_assets": sum(int(r.get("support_candidates", 0)) > 0 for r in summaries),
        "accessions_with_credible_support_evidence": sum(int(r.get("credible_evidence_hits", 0)) > 0 for r in summaries),
        "accessions_with_explicit_reporter_role_hits": sum(int(r.get("explicit_reporter_role_hits", 0)) > 0 for r in summaries),
        "accessions_with_reporter_role_and_run_hits": sum(int(r.get("reporter_role_and_run_hits", 0)) > 0 for r in summaries),
        "result_like_assets_excluded": sum(int(r.get("result_like_assets_excluded", 0)) for r in summaries),
        "non_generative": True,
        "outputs": {
            "inventory": str(output / "sdrf_multiplex_support_asset_audit.tsv"),
            "asset_status": str(output / "support_asset_status.tsv"),
            "evidence_hits": str(output / "support_asset_evidence_hits.tsv"),
            "credible_evidence_hits": str(output / "support_asset_credible_evidence_hits.tsv"),
        },
    }
    (output / "sdrf_multiplex_support_asset_audit_summary.json").write_text(json.dumps(overall, indent=2) + "\n")
    print(json.dumps(overall, indent=2))
    for row in summaries:
        print(
            f"{row['accession']} class={row['support_class']} raw={row['raw_files']} "
            f"selected={row['support_candidates']} excluded_results={row['result_like_assets_excluded']} "
            f"pub_text={row['publication_text_rows']} credible={row['credible_evidence_hits']} "
            f"role_hits={row['explicit_reporter_role_hits']} role_run={row['reporter_role_and_run_hits']} "
            f"run_hits={row['run_linkage_hits']}"
        )
    return 0


def self_test() -> None:
    assert support_candidate(RepoFile("experimental_design.xlsx", "OTHER", "https://example.test/design.xlsx"))
    assert support_candidate(RepoFile("sample_annotation.tsv", "OTHER", "https://example.test/sample.tsv"))
    assert support_candidate(RepoFile("README.txt", "OTHER", "https://example.test/readme.txt"))
    assert not support_candidate(RepoFile("sample01.RAW", "RAW", "https://example.test/sample01.RAW"))
    assert not support_candidate(RepoFile("20210316_scMS_realtimesearch.csv", "RESULT", "https://example.test/result.csv"))
    assert not support_candidate(RepoFile("protein_results.csv", "RESULT", "https://example.test/proteins.csv"))
    assert support_candidate(RepoFile("table1.xlsx", "OTHER", "https://example.test/table1.xlsx"))

    hit = evidence_hit(
        "PXDTEST",
        "design.tsv",
        "line2",
        "single cell sample; TMTpro126 analytical channel; TMTpro127N carrier channel; RAW file sample01.RAW",
    )
    assert hit is not None
    assert hit.chemistry and hit.single_cell and hit.run_semantics
    assert hit.reporter_channels == ["126", "127N"]
    assert "analytical" in hit.roles and "carrier" in hit.roles
    assert hit.raw_tokens == ["sample01.RAW"]
    assert hit.evidence_tier == "reporter_role_and_run"

    # Bare channel-sized integers are no longer evidence without TMT/reporter/channel/label context.
    assert evidence_hit("PXDTEST", "notes.txt", "line1", "126 127 128") is None
    assert evidence_hit("PXDTEST", "results.csv", "line1", "TMEM131 transmembrane protein 131") is None

    # Real v0.4.6 false positives from protein-result vocabulary must stay non-evidence.
    assert evidence_hit(
        "PXDTEST", "results.csv", "line2", "sp|Q9UBX3|DIC_HUMAN Mitochondrial dicarboxylate carrier OS=Homo sapiens"
    ) is None
    assert evidence_hit(
        "PXDTEST", "results.csv", "line3", "sp|Q9BVN2|RUSC1_HUMAN RUN and SH3 domain-containing protein 1"
    ) is None
    assert evidence_hit(
        "PXDTEST", "results.csv", "line4", "sp|Q99741|CDC6_HUMAN Cell division control protein 6 homolog"
    ) is None

    # Chemistry-only prose is retained strictly as source triage, never as mapping evidence.
    chem = evidence_hit("PXDTEST", "readme.txt", "line1", "Samples were labeled with TMTpro.")
    assert chem is not None and chem.chemistry and not chem.roles and not chem.reporter_channels
    assert chem.evidence_tier == "source_triage_only"

    # A bare number may be accepted when an explicit reporter/channel phrase anchors it locally.
    contextual = evidence_hit(
        "PXDTEST", "design.tsv", "line5", "reporter channel 126 = blank; channel 127N = single cell sample"
    )
    assert contextual is not None and contextual.reporter_channels == ["126", "127N"]
    assert "blank" in contextual.roles

    print("sdrf_multiplex_support_asset_audit self-test: PASS")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--accessions-file", type=Path)
    p.add_argument("--snapshot", type=Path)
    p.add_argument("--reporter-audit-tsv", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--reuse-support-root", type=Path)
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
