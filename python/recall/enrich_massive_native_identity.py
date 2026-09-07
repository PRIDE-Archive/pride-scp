#!/usr/bin/env python3
"""Candidate-scoped MassIVE native identity enrichment.

This stage is deliberately source-derived and GT-blind.  It resolves trusted
MSV<->PXD alias edges from structured repository metadata, caches provenance,
and can apply the resulting alias graph to normalized MassIVE project records.

Frozen GT is never accepted as an input to this script.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

import requests

PXD_RE = re.compile(r"^PXD\d+$", re.I)
MSV_RE = re.compile(r"^MSV\d+$", re.I)
NATIVE_RE = re.compile(r"^(?:MSV|IPX|JPST|PASS|PXL)\d+$", re.I)
ACCESSION_TOKEN_RE = re.compile(r"\b(?:PXD|MSV|IPX|JPST|PASS|PXL)\d+\b", re.I)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"<>\[\]{}]+", re.I)
URL_RE = re.compile(r"https?://[^\s\"<>]+", re.I)
PMID_RE = re.compile(r"\b(?:PMID\s*[:=]?\s*)?(\d{7,9})\b", re.I)

IDENTIFIER_KEYS = {
    "identifier",
    "identifiers",
    "accession",
    "accessions",
    "pxd",
    "pxdaccession",
    "datasetid",
    "datasetidentifier",
    "fulldatasetlinks",
    "fulldatasetlink",
    "dataseturi",
    "dataseturl",
    "proteomexchangeaccession",
    "proteomexchangeaccessionnumber",
}
PUBLICATION_KEYS = {"publication", "publications", "pubmed", "doi"}


def norm_key(value: str) -> str:
    return "".join(ch.lower() for ch in value if ch.isalnum())


def norm_accession(value: str) -> str:
    value = (value or "").strip().upper()
    return value if ACCESSION_TOKEN_RE.fullmatch(value) else ""


def is_pxd(value: str) -> bool:
    return bool(PXD_RE.fullmatch((value or "").strip()))


def is_msv(value: str) -> bool:
    return bool(MSV_RE.fullmatch((value or "").strip()))


def flatten_strings(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, list):
        for item in value:
            out.extend(flatten_strings(item))
    elif isinstance(value, dict):
        for item in value.values():
            out.extend(flatten_strings(item))
    return out


def all_accession_tokens(value: Any) -> set[str]:
    out: set[str] = set()
    for text in flatten_strings(value):
        out.update(m.group(0).upper() for m in ACCESSION_TOKEN_RE.finditer(text))
    return out


def ontology_term_identifier_like(value: dict[str, Any]) -> bool:
    name = str(value.get("name") or "").lower()
    accession = str(value.get("accession") or "").upper()
    return (
        "accession" in name
        or "identifier" in name
        or "dataset uri" in name
        or "dataset url" in name
        or accession == "MS:1001919"  # ProteomeXchange accession number
        or accession == "MS:1002634"  # MassIVE dataset identifier
    )


def structured_identifier_strings(value: Any) -> list[tuple[str, str]]:
    """Return (json_path, string) only from identifier-semantic fields.

    Free title/description text is intentionally excluded.
    """
    out: list[tuple[str, str]] = []

    def visit(node: Any, path: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            if ontology_term_identifier_like(node):
                for text in flatten_strings(node):
                    out.append(("/".join(path) or "$", text))
            for key, child in node.items():
                nk = norm_key(key)
                child_path = path + (key,)
                if nk in IDENTIFIER_KEYS or "identifier" in nk or "accession" in nk:
                    for text in flatten_strings(child):
                        out.append(("/".join(child_path), text))
                else:
                    visit(child, child_path)
        elif isinstance(node, list):
            for idx, child in enumerate(node):
                visit(child, path + (str(idx),))

    visit(value, ())
    # deterministic de-duplication
    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str]] = []
    for item in out:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def structured_accession_evidence(value: Any) -> dict[str, set[str]]:
    evidence: dict[str, set[str]] = defaultdict(set)
    for path, text in structured_identifier_strings(value):
        for match in ACCESSION_TOKEN_RE.finditer(text):
            evidence[match.group(0).upper()].add(path)
    return evidence


def exact_scalar_tokens(value: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(value, str):
        token = norm_accession(value)
        if token:
            out.add(token)
    elif isinstance(value, list):
        for item in value:
            out.update(exact_scalar_tokens(item))
    elif isinstance(value, dict):
        for item in value.values():
            out.update(exact_scalar_tokens(item))
    return out


def massive_detail_alias_evidence(value: Any, target_msv: str) -> dict[str, set[str]]:
    """Trusted aliases from the accession-filtered QueryDatasets response.

    Objects use identifier-semantic fields. Positional table rows are accepted
    only when the *same row* contains the exact requested MSV scalar and an exact
    PXD scalar; arbitrary free-text PXD mentions are not promoted.
    """
    target_msv = target_msv.upper()
    evidence: dict[str, set[str]] = defaultdict(set)

    structured = structured_accession_evidence(value)
    for acc, paths in structured.items():
        if is_pxd(acc):
            evidence[acc].update(f"structured:{p}" for p in paths)

    def visit(node: Any, path: tuple[str, ...]) -> None:
        if isinstance(node, list):
            scalars = exact_scalar_tokens(node)
            if target_msv in scalars:
                for token in sorted(scalars):
                    if is_pxd(token):
                        evidence[token].add(f"matched_positional_row:{'/'.join(path) or '$'}")
            for idx, child in enumerate(node):
                visit(child, path + (str(idx),))
        elif isinstance(node, dict):
            for key, child in node.items():
                visit(child, path + (key,))

    visit(value, ())
    return evidence




def dataset_entries(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        for key in ("datasets", "results", "content"):
            child = value.get(key)
            if isinstance(child, list):
                return list(child)
        data = value.get("data")
        if isinstance(data, list):
            return list(data)
        if isinstance(data, dict):
            for key in ("datasets", "results", "content"):
                child = data.get(key)
                if isinstance(child, list):
                    return list(child)
    return [value]


def proteomecentral_search_alias_evidence(value: Any, target_msv: str) -> dict[str, set[str]]:
    """Return PXD evidence only from the same returned dataset record as target_msv."""
    target_msv = target_msv.upper()
    evidence: dict[str, set[str]] = defaultdict(set)
    for idx, entry in enumerate(dataset_entries(value)):
        structured = structured_accession_evidence(entry)
        structured_tokens = set(structured)
        scalars = exact_scalar_tokens(entry) if isinstance(entry, list) else set()
        if target_msv not in structured_tokens and target_msv not in scalars:
            continue
        for accession, paths in structured.items():
            if is_pxd(accession):
                evidence[accession].update(f"record[{idx}]:{path}" for path in paths)
        if isinstance(entry, list):
            for accession in scalars:
                if is_pxd(accession):
                    evidence[accession].add(f"record[{idx}]:matched_positional_row")
    return evidence

def publication_subtrees(value: Any) -> list[Any]:
    out: list[Any] = []
    if isinstance(value, dict):
        for key, child in value.items():
            nk = norm_key(key)
            if nk in PUBLICATION_KEYS or "publication" in nk:
                out.append(child)
            out.extend(publication_subtrees(child))
    elif isinstance(value, list):
        for child in value:
            out.extend(publication_subtrees(child))
    return out


def extract_publication_metadata(*values: Any) -> tuple[set[str], set[str], set[str], set[str]]:
    dois: set[str] = set()
    urls: set[str] = set()
    pmids: set[str] = set()
    strings: set[str] = set()
    for value in values:
        if value is None:
            continue
        subtrees = publication_subtrees(value)
        # Dataset links can also contain DOI/PubMed URLs even if not nested under publications.
        if isinstance(value, dict):
            for key, child in value.items():
                if norm_key(key) in {"fulldatasetlinks", "datasetlinks", "links"}:
                    subtrees.append(child)
        for subtree in subtrees:
            for text in flatten_strings(subtree):
                clean = text.strip()
                if not clean:
                    continue
                strings.add(clean)
                for match in DOI_RE.finditer(clean):
                    doi = match.group(0).rstrip(".,;:)]}").lower()
                    dois.add(doi)
                for match in URL_RE.finditer(clean):
                    urls.add(match.group(0).rstrip(".,;:)]}"))
                lower = clean.lower()
                if "pubmed" in lower or "pmid" in lower:
                    for match in PMID_RE.finditer(clean):
                        pmids.add(match.group(1))
    return dois, urls, pmids, strings


def stable_component_key(tokens: Iterable[str]) -> str:
    canonical = ";".join(sorted({norm_accession(x) for x in tokens if norm_accession(x)}))
    if not canonical:
        return ""
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"dataset_identity:{digest}"


def add_edge(graph: dict[str, set[str]], a: str, b: str) -> None:
    a = norm_accession(a)
    b = norm_accession(b)
    if not a or not b or a == b:
        return
    graph[a].add(b)
    graph[b].add(a)


def component(graph: dict[str, set[str]], seed: str) -> set[str]:
    seed = norm_accession(seed)
    if not seed:
        return set()
    seen = {seed}
    queue: deque[str] = deque([seed])
    while queue:
        cur = queue.popleft()
        for nxt in graph.get(cur, ()):  # noqa: SIM118
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class FetchResult:
    status: str
    value: Any = None
    url: str = ""
    error: str = ""


@dataclass
class SourceMetrics:
    counts: Counter = field(default_factory=Counter)

    def note(self, source: str, status: str) -> None:
        self.counts[f"{source}_{status}"] += 1


class CachedJsonFetcher:
    def __init__(self, timeout: int, retries: int, user_agent: str, force: bool):
        self.timeout = timeout
        self.retries = retries
        self.force = force
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})

    def get(self, url: str, cache_path: Path, *, params: dict[str, Any] | None = None, allow_404: bool = True) -> FetchResult:
        not_found = cache_path.with_suffix(cache_path.suffix + ".not_found")
        cache_invalid_error = ""
        if cache_path.is_file() and not self.force:
            try:
                return FetchResult("cached", load_json(cache_path), url)
            except Exception as exc:  # corrupt cache should be refreshed
                cache_invalid_error = str(exc)
                cache_path.unlink(missing_ok=True)
        if not_found.is_file() and not self.force:
            return FetchResult("not_found_cached", None, url)

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        last_error = ""
        attempts = self.retries + 1
        for attempt in range(attempts):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                final_url = str(response.url)
                if response.status_code == 404 and allow_404:
                    not_found.write_text(final_url + "\n", encoding="utf-8")
                    cache_path.unlink(missing_ok=True)
                    return FetchResult("not_found", None, final_url)
                response.raise_for_status()
                # requests/json can represent lone surrogates; serializing with ensure_ascii=True
                # keeps them safe.  The original Rust QueryDatasets catalogue repair remains the
                # authoritative enumeration boundary and is not changed by this stage.
                value = response.json()
                atomic_write_json(cache_path, value)
                not_found.unlink(missing_ok=True)
                return FetchResult("fetched", value, final_url)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt + 1 < attempts:
                    time.sleep(min(2**attempt, 16))
        return FetchResult("error", None, url, "; ".join(x for x in [cache_invalid_error, last_error] if x))


def collect_alias_evidence(
    target_msv: str,
    source: str,
    value: Any,
    *,
    detail_mode: bool = False,
    direct_resource: bool = False,
) -> list[dict[str, str]]:
    target_msv = target_msv.upper()
    if value is None:
        return []

    if detail_mode:
        raw = massive_detail_alias_evidence(value, target_msv)
    elif not direct_resource and source.startswith("proteomecentral"):
        raw = proteomecentral_search_alias_evidence(value, target_msv)
    else:
        structured = structured_accession_evidence(value)
        raw = {acc: paths for acc, paths in structured.items() if is_pxd(acc)}

    rows: list[dict[str, str]] = []
    for pxd, paths in sorted(raw.items()):
        if not is_pxd(pxd):
            continue
        for path in sorted(paths):
            rows.append({
                "native_accession": target_msv,
                "pxd_accession": pxd.upper(),
                "source": source,
                "source_locator": path,
                "trust": "trusted_structured_repository_identity",
            })
    return rows


def existing_registry_edges(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    rows: list[dict[str, str]] = []
    for row in read_tsv(path):
        pxd = norm_accession(row.get("pxd_accession", ""))
        if not is_pxd(pxd):
            continue
        for token in ACCESSION_TOKEN_RE.findall(row.get("native_accessions", "") or ""):
            native = token.upper()
            if is_msv(native):
                rows.append({
                    "native_accession": native,
                    "pxd_accession": pxd,
                    "source": "proteomecentral_registry_crosswalk",
                    "source_locator": row.get("registry_json_path", ""),
                    "trust": "trusted_structured_repository_identity",
                })
    return rows


def project_existing_edges(project: dict[str, Any], target_msv: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for value in project.get("pxdAliases") or []:
        pxd = norm_accession(str(value))
        if is_pxd(pxd):
            rows.append({
                "native_accession": target_msv,
                "pxd_accession": pxd,
                "source": "normalized_project_existing_alias",
                "source_locator": "pxdAliases",
                "trust": "trusted_existing_runtime_identity",
            })
    raw = project.get("massiveNativeRecord")
    structured = structured_accession_evidence(raw)
    for pxd, paths in sorted(structured.items()):
        if not is_pxd(pxd):
            continue
        for path in sorted(paths):
            rows.append({
                "native_accession": target_msv,
                "pxd_accession": pxd,
                "source": "massive_native_record_structured",
                "source_locator": path,
                "trust": "trusted_structured_repository_identity",
            })
    return rows


def unique_evidence(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, ...]] = set()
    out: list[dict[str, str]] = []
    for row in rows:
        key = (
            row.get("native_accession", ""),
            row.get("pxd_accession", ""),
            row.get("source", ""),
            row.get("source_locator", ""),
            row.get("trust", ""),
        )
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def update_massive_crosswalk(path: Path, aliases_by_msv: dict[str, set[str]], backup_path: Path) -> int:
    rows = read_tsv(path)
    if not backup_path.is_file():
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_path)
    changed = 0
    for row in rows:
        msv = norm_accession(row.get("msv_accession", ""))
        if not is_msv(msv) or msv not in aliases_by_msv:
            continue
        new = "; ".join(sorted(aliases_by_msv[msv]))
        if (row.get("pxd_aliases") or "").strip() != new:
            row["pxd_aliases"] = new
            changed += 1
    fields = list(rows[0].keys()) if rows else ["msv_accession", "pxd_aliases", "project_json_path", "dataset_title"]
    write_tsv(path, rows, fields)
    return changed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--delta-tsv", type=Path, required=True,
                    help="M2-A candidate delta TSV. Used only for the 167 source candidate accessions; GT columns are ignored.")
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--massive-proxi-base", default="https://massive.ucsd.edu/ProteoSAFe/proxi/v0.1")
    ap.add_argument("--massive-query-endpoint", default="https://massive.ucsd.edu/ProteoSAFe/QueryDatasets")
    ap.add_argument("--proteomecentral-proxi-base", default="https://proteomecentral.proteomexchange.org/api/proxi/v0.1")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--retries", type=int, default=4)
    ap.add_argument("--user-agent", default="PRIDE-SCP-massive-native-identity/0.1.0")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--apply", action="store_true",
                    help="Apply trusted aliases to normalized MassIVE project records and massive_accessions.tsv.")
    ap.add_argument("--expect-candidates", type=int, default=167)
    args = ap.parse_args()

    snapshot = args.snapshot.resolve()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    massive_root = snapshot / "native" / "massive"
    projects_dir = massive_root / "projects"
    crosswalk_path = massive_root / "massive_accessions.tsv"
    registry_crosswalk = snapshot / "registry" / "registry_accessions.tsv"
    cache_root = massive_root / "identity_enrichment"

    delta_rows = read_tsv(args.delta_tsv)
    targets = sorted({norm_accession(row.get("native_msv") or row.get("candidate_accession") or "")
                      for row in delta_rows})
    targets = [x for x in targets if is_msv(x)]
    if args.expect_candidates and len(targets) != args.expect_candidates:
        raise SystemExit(f"candidate count mismatch: expected {args.expect_candidates}, observed {len(targets)}")
    if not crosswalk_path.is_file():
        raise SystemExit(f"missing MassIVE crosswalk: {crosswalk_path}")

    fetcher = CachedJsonFetcher(args.timeout, args.retries, args.user_agent, args.force)
    fetch_metrics = SourceMetrics()
    all_evidence: list[dict[str, str]] = existing_registry_edges(registry_crosswalk)
    evidence_by_target: dict[str, list[dict[str, str]]] = defaultdict(list)
    remote_records: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    input_state: dict[str, dict[str, Any]] = {}

    for index, msv in enumerate(targets, 1):
        project_path = projects_dir / f"{msv}.json"
        if not project_path.is_file():
            errors.append({"native_accession": msv, "source": "local_project", "error": f"missing {project_path}"})
            continue
        project = load_json(project_path)
        input_state[msv] = project
        local_edges = project_existing_edges(project, msv)
        evidence_by_target[msv].extend(local_edges)
        all_evidence.extend(local_edges)

        unverified_tokens = sorted(x for x in all_accession_tokens(project.get("massiveNativeRecord")) if is_pxd(x))

        proxi_url = f"{args.massive_proxi_base.rstrip('/')}/datasets/{msv}"
        proxi = fetcher.get(proxi_url, cache_root / "massive_proxi" / f"{msv}.json")
        fetch_metrics.note("massive_proxi", proxi.status)
        if proxi.status == "error":
            errors.append({"native_accession": msv, "source": "massive_proxi", "error": proxi.error})
        proxi_edges = collect_alias_evidence(msv, "massive_proxi", proxi.value, direct_resource=True)
        evidence_by_target[msv].extend(proxi_edges)
        all_evidence.extend(proxi_edges)

        current_aliases = {e["pxd_accession"] for e in evidence_by_target[msv] if is_pxd(e["pxd_accession"])}
        detail = FetchResult("skipped_alias_already_resolved")
        if not current_aliases:
            detail_query = json.dumps({"title_input": msv}, separators=(",", ":"))
            detail = fetcher.get(
                args.massive_query_endpoint,
                cache_root / "massive_detail" / f"{msv}.json",
                params={"pageSize": 30, "offset": 0, "query": detail_query},
            )
            fetch_metrics.note("massive_detail", detail.status)
            if detail.status == "error":
                errors.append({"native_accession": msv, "source": "massive_detail", "error": detail.error})
            detail_edges = collect_alias_evidence(msv, "massive_detail", detail.value, detail_mode=True)
            evidence_by_target[msv].extend(detail_edges)
            all_evidence.extend(detail_edges)
            current_aliases.update(e["pxd_accession"] for e in detail_edges)
        else:
            fetch_metrics.note("massive_detail", detail.status)

        pc_direct = FetchResult("skipped_alias_already_resolved")
        pc_search = FetchResult("skipped_alias_already_resolved")
        if not current_aliases:
            pc_url = f"{args.proteomecentral_proxi_base.rstrip('/')}/datasets/{msv}"
            pc_direct = fetcher.get(pc_url, cache_root / "proteomecentral_direct" / f"{msv}.json")
            fetch_metrics.note("proteomecentral_direct", pc_direct.status)
            if pc_direct.status == "error":
                errors.append({"native_accession": msv, "source": "proteomecentral_direct", "error": pc_direct.error})
            pc_edges = collect_alias_evidence(msv, "proteomecentral_direct", pc_direct.value, direct_resource=True)
            evidence_by_target[msv].extend(pc_edges)
            all_evidence.extend(pc_edges)
            current_aliases.update(e["pxd_accession"] for e in pc_edges)

            if not current_aliases:
                pc_collection = f"{args.proteomecentral_proxi_base.rstrip('/')}/datasets"
                pc_search = fetcher.get(
                    pc_collection,
                    cache_root / "proteomecentral_search" / f"{msv}.json",
                    params={"pageSize": 100, "pageNumber": 1, "resultType": "full", "search": msv, "repository": "MassIVE"},
                )
                fetch_metrics.note("proteomecentral_search", pc_search.status)
                if pc_search.status == "error":
                    errors.append({"native_accession": msv, "source": "proteomecentral_search", "error": pc_search.error})
                search_edges = collect_alias_evidence(msv, "proteomecentral_search", pc_search.value, direct_resource=False)
                evidence_by_target[msv].extend(search_edges)
                all_evidence.extend(search_edges)
                current_aliases.update(e["pxd_accession"] for e in search_edges)
            else:
                fetch_metrics.note("proteomecentral_search", pc_search.status)
        else:
            fetch_metrics.note("proteomecentral_direct", pc_direct.status)
            fetch_metrics.note("proteomecentral_search", pc_search.status)

        dois, urls, pmids, publication_strings = extract_publication_metadata(
            project.get("massiveNativeRecord"), proxi.value, detail.value, pc_direct.value, pc_search.value
        )
        remote_records[msv] = {
            "unverified_pxd_tokens": unverified_tokens,
            "publication_dois": sorted(dois),
            "publication_urls": sorted(urls),
            "publication_pmids": sorted(pmids),
            "publication_strings": sorted(publication_strings),
            "source_status": {
                "massive_proxi": proxi.status,
                "massive_detail": detail.status,
                "proteomecentral_direct": pc_direct.status,
                "proteomecentral_search": pc_search.status,
            },
            "source_urls": {
                "massive_proxi": proxi.url,
                "massive_detail": detail.url,
                "proteomecentral_direct": pc_direct.url,
                "proteomecentral_search": pc_search.url,
            },
        }
        print(f"[{index}/{len(targets)}] {msv}: aliases={','.join(sorted(current_aliases)) or '-'} dois={len(dois)}", flush=True)

    all_evidence = unique_evidence(all_evidence)
    for msv in list(evidence_by_target):
        evidence_by_target[msv] = unique_evidence(evidence_by_target[msv])

    # Build the source-only identity graph from trusted evidence. No GT enters this graph.
    graph: dict[str, set[str]] = defaultdict(set)
    for row in all_evidence:
        add_edge(graph, row.get("native_accession", ""), row.get("pxd_accession", ""))

    # Include existing MassIVE crosswalk edges so components remain coherent with the runtime snapshot.
    crosswalk_rows = read_tsv(crosswalk_path)
    for row in crosswalk_rows:
        msv = norm_accession(row.get("msv_accession", ""))
        if not is_msv(msv):
            continue
        for token in ACCESSION_TOKEN_RE.findall(row.get("pxd_aliases", "") or ""):
            pxd = token.upper()
            if is_pxd(pxd):
                add_edge(graph, msv, pxd)

    aliases_by_msv: dict[str, set[str]] = {}
    identity_rows: list[dict[str, Any]] = []
    promotion_rows: list[dict[str, Any]] = []
    records_with_alias_before = 0
    records_with_alias_after = 0
    records_newly_resolved = 0
    aliases_promoted = 0
    promoted_unverified = 0
    publication_doi_coverage = 0
    project_updates = 0

    for msv in targets:
        project = input_state.get(msv)
        if project is None:
            continue
        before = {norm_accession(str(x)) for x in (project.get("pxdAliases") or [])}
        before = {x for x in before if is_pxd(x)}
        if before:
            records_with_alias_before += 1
        comp = component(graph, msv)
        after = {x for x in comp if is_pxd(x)}
        # direct trusted evidence may not have been connected if target had a malformed accession; union defensively
        after.update(e["pxd_accession"] for e in evidence_by_target.get(msv, []) if is_pxd(e["pxd_accession"]))
        aliases_by_msv[msv] = after
        if after:
            records_with_alias_after += 1
        if not before and after:
            records_newly_resolved += 1
        promoted = sorted(after - before)
        aliases_promoted += len(promoted)
        unverified = set(remote_records.get(msv, {}).get("unverified_pxd_tokens", []))
        promoted_unverified += len(set(promoted) & unverified)
        if remote_records.get(msv, {}).get("publication_dois"):
            publication_doi_coverage += 1

        members = component(graph, msv) or {msv}
        canonical_id = stable_component_key(members)
        evidence = evidence_by_target.get(msv, [])
        source_names = sorted({e["source"] for e in evidence})
        identity = {
            "canonical_dataset_identity": canonical_id,
            "repository": "MassIVE",
            "native_accession": msv,
            "pxd_aliases": sorted(after),
            "secondary_accessions": sorted(x for x in members if x != msv and not is_pxd(x)),
            "source_identity_members": sorted(members),
            "publication_dois": remote_records.get(msv, {}).get("publication_dois", []),
            "publication_pmids": remote_records.get(msv, {}).get("publication_pmids", []),
            "publication_urls": remote_records.get(msv, {}).get("publication_urls", []),
            "source_evidence": evidence,
            "source_status": remote_records.get(msv, {}).get("source_status", {}),
            "source_urls": remote_records.get(msv, {}).get("source_urls", {}),
            "unverified_pxd_tokens": sorted(unverified),
            "gt_used_for_identity": False,
        }
        atomic_write_json(cache_root / "identity" / f"{msv}.json", identity)

        identity_rows.append({
            "native_accession": msv,
            "canonical_dataset_identity": canonical_id,
            "pxd_aliases_before": "; ".join(sorted(before)),
            "pxd_aliases_after": "; ".join(sorted(after)),
            "promoted_pxd_aliases": "; ".join(promoted),
            "unverified_pxd_tokens": "; ".join(sorted(unverified)),
            "promoted_unverified_token": "yes" if set(promoted) & unverified else "no",
            "source_identity_members": "; ".join(sorted(members)),
            "source_evidence": "; ".join(source_names),
            "publication_dois": "; ".join(identity["publication_dois"]),
            "publication_pmids": "; ".join(identity["publication_pmids"]),
            "publication_urls": "; ".join(identity["publication_urls"]),
            "massive_proxi_status": identity["source_status"].get("massive_proxi", ""),
            "massive_detail_status": identity["source_status"].get("massive_detail", ""),
            "proteomecentral_direct_status": identity["source_status"].get("proteomecentral_direct", ""),
            "proteomecentral_search_status": identity["source_status"].get("proteomecentral_search", ""),
        })
        for pxd in promoted:
            ev_sources = sorted({e["source"] for e in evidence if e["pxd_accession"] == pxd})
            promotion_rows.append({
                "native_accession": msv,
                "pxd_accession": pxd,
                "source_evidence": "; ".join(ev_sources),
                "was_unverified_token": "yes" if pxd in unverified else "no",
                "canonical_dataset_identity": canonical_id,
            })

        if args.apply:
            updated = dict(project)
            updated["pxdAliases"] = sorted(after)
            updated["sourceIdentity"] = identity
            if updated != project:
                atomic_write_json(projects_dir / f"{msv}.json", updated)
                project_updates += 1

    crosswalk_updates = 0
    if args.apply:
        crosswalk_updates = update_massive_crosswalk(
            crosswalk_path,
            aliases_by_msv,
            cache_root / "massive_accessions.pre_m2_identity.tsv",
        )

    identity_fields = [
        "native_accession", "canonical_dataset_identity", "pxd_aliases_before", "pxd_aliases_after",
        "promoted_pxd_aliases", "unverified_pxd_tokens", "promoted_unverified_token",
        "source_identity_members", "source_evidence", "publication_dois", "publication_pmids",
        "publication_urls", "massive_proxi_status", "massive_detail_status",
        "proteomecentral_direct_status", "proteomecentral_search_status",
    ]
    write_tsv(output / "massive_native_identity_enrichment.tsv", identity_rows, identity_fields)
    write_tsv(output / "pxd_alias_promotions.tsv", promotion_rows, [
        "native_accession", "pxd_accession", "source_evidence", "was_unverified_token", "canonical_dataset_identity"
    ])
    unresolved = [row for row in identity_rows if not row["pxd_aliases_after"]]
    write_tsv(output / "unresolved_native_candidates.tsv", unresolved, identity_fields)
    write_tsv(output / "identity_enrichment_errors.tsv", errors, ["native_accession", "source", "error"])

    summary: dict[str, Any] = {
        "candidate_accessions": len(targets),
        "records_with_pxd_alias_before": records_with_alias_before,
        "records_with_pxd_alias_after": records_with_alias_after,
        "records_newly_resolved": records_newly_resolved,
        "records_unresolved_after": len(targets) - records_with_alias_after,
        "promoted_pxd_alias_edges": aliases_promoted,
        "promoted_aliases_that_were_unverified_native_tokens": promoted_unverified,
        "publication_doi_coverage_after": publication_doi_coverage,
        "trusted_evidence_rows": len(all_evidence),
        "project_records_updated": project_updates,
        "massive_crosswalk_rows_updated": crosswalk_updates,
        "apply": bool(args.apply),
        "errors": len(errors),
        "gt_used_for_identity": False,
        "fetch_status_counts": dict(sorted(fetch_metrics.counts.items())),
    }
    atomic_write_json(output / "massive_native_identity_enrichment_summary.json", summary)

    print(json.dumps(summary, indent=2, sort_keys=True))
    if errors:
        print(f"WARNING: {len(errors)} source retrieval/local errors; inspect {output / 'identity_enrichment_errors.tsv'}", file=sys.stderr)


if __name__ == "__main__":
    main()
