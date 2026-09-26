#!/usr/bin/env python3
"""Generic acquisition adapters for source-grounded external design artifacts.

This module is deliberately non-generative.  It only materializes files from source URLs that were
already discovered by the publication/evidence pipeline.  Provider-specific APIs are used solely to
enumerate public files; every acquired byte is retained with its originating URL and SHA256 by the
caller.

Supported acquisition lanes:

* Europe PMC supplementary bundles (ZIP);
* Zenodo public records;
* Figshare public articles;
* direct public structured-file URLs.

GitHub remains handled by the existing evidence-graph adapter because it already performs bounded
high-value repository selection.
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

VERSION = "pride-scp-external-artifact-acquisition-v0.1"

# Keep this aligned with parsers in sdrf_multiplex_evidence_graph.py.  Archives are accepted only as
# containers; their members still have to use a parser-supported suffix before they are materialized.
PARSER_SUFFIXES = {
    ".txt", ".tsv", ".csv", ".xlsx", ".json", ".xml", ".sky", ".pdresult", ".pdstudy", ".msf",
}
ARCHIVE_SUFFIXES = {".zip"}
SELECTABLE_SUFFIXES = PARSER_SUFFIXES | ARCHIVE_SUFFIXES
HIGH_VALUE_RE = re.compile(
    r"(?i)(?:cell|sample|input|design|metadata|annotation|characteristic|cellenone|channel|reporter|tmt|raw|run|plex|batch|well|manifest|mapping|sorted|unsorted|experimental)"
)
ZENODO_ID_RE = re.compile(r"(?:zenodo\.org/(?:records?|record)/|10\.5281/zenodo\.)(\d+)", re.I)
FIGSHARE_ID_RE = re.compile(r"figshare\.com/(?:[^?#]*/)?(?:articles|ndownloader/articles)/(?:[^/?#]+/)?(\d+)(?:/|$|[?#])", re.I)
FIGSHARE_API_ID_RE = re.compile(r"api\.figshare\.com/v\d+/articles/(\d+)", re.I)


@dataclass(frozen=True)
class AcquiredArtifact:
    provider: str
    source_url: str
    resolved_url: str
    remote_name: str
    local_path: str
    size_bytes: int
    sha256: str
    status: str


def _suffix_from_name(value: str) -> str:
    path = unquote(urlparse(value).path)
    return Path(path).suffix.lower()


def classify_external_source(url: str, link_type: str = "") -> str:
    """Classify a discovered external source without using accession-specific rules."""
    low = (url or "").strip().lower()
    link_type_low = (link_type or "").strip().lower()
    if "github.com/" in low:
        return "github"
    if "zenodo." in low or "10.5281/zenodo." in low:
        return "zenodo"
    if "figshare.com/" in low:
        return "figshare"
    if "mendeley" in low:
        return "mendeley"
    if link_type_low in {"media", "supplementary-material", "supplementary", "supplement"}:
        return "supplementary_media"
    if _suffix_from_name(low) in PARSER_SUFFIXES:
        return "direct_structured_file"
    return "other"


def europe_pmc_supplement_url(pmcid: str) -> str:
    pmcid = (pmcid or "").strip().upper()
    if not pmcid:
        return ""
    if not pmcid.startswith("PMC") and pmcid.isdigit():
        pmcid = "PMC" + pmcid
    if not re.fullmatch(r"PMC\d+", pmcid):
        return ""
    return f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/supplementaryFiles"


def _safe_name(value: str, fallback: str = "artifact") -> str:
    name = Path(unquote(urlparse(value).path)).name or fallback
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name or fallback


def _score_name(name: str) -> tuple[int, str]:
    suffix = Path(name).suffix.lower()
    if suffix not in SELECTABLE_SUFFIXES:
        return 0, "unsupported_suffix"
    if suffix in ARCHIVE_SUFFIXES:
        return 2, "archive"
    if HIGH_VALUE_RE.search(name):
        return 5, "high_value_name"
    if suffix in {".xlsx", ".csv", ".tsv", ".txt"}:
        return 3, "structured_table"
    if suffix in {".pdresult", ".pdstudy", ".msf", ".sky"}:
        return 4, "analysis_design_artifact"
    return 2, "structured_metadata"


def _select_files(rows: Iterable[dict[str, Any]], max_files: int, max_bytes: int) -> list[dict[str, Any]]:
    ranked: list[tuple[int, int, str, dict[str, Any]]] = []
    for row in rows:
        name = str(row.get("name") or row.get("key") or row.get("filename") or "")
        try:
            size = int(row.get("size") or row.get("filesize") or 0)
        except Exception:
            size = 0
        score, _ = _score_name(name)
        if score <= 0:
            continue
        if size and size > max_bytes:
            continue
        ranked.append((score, size, name.lower(), row))
    ranked.sort(key=lambda x: (-x[0], x[1], x[2]))
    return [x[3] for x in ranked[:max_files]]


def _stream_download(session: Any, url: str, target: Path, max_bytes: int, timeout: tuple[int, int] = (20, 120)) -> tuple[bool, str, int, str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with session.get(url, stream=True, timeout=timeout, allow_redirects=True) as response:
            response.raise_for_status()
            clen = response.headers.get("content-length")
            if clen and int(clen) > max_bytes:
                return False, str(getattr(response, "url", url)), 0, f"too_large:{clen}"
            n = 0
            digest = hashlib.sha256()
            with target.open("wb") as fh:
                for chunk in response.iter_content(1024 * 256):
                    if not chunk:
                        continue
                    n += len(chunk)
                    if n > max_bytes:
                        fh.close()
                        target.unlink(missing_ok=True)
                        return False, str(getattr(response, "url", url)), n, f"too_large_streamed:{n}"
                    digest.update(chunk)
                    fh.write(chunk)
            return True, str(getattr(response, "url", url)), n, digest.hexdigest()
    except Exception as exc:
        target.unlink(missing_ok=True)
        return False, url, 0, f"download_error:{type(exc).__name__}:{exc}"


def _download_selected(
    *, provider: str, source_url: str, rows: list[dict[str, Any]], dest: Path, max_files: int,
    max_bytes: int, session: Any,
) -> list[AcquiredArtifact]:
    out: list[AcquiredArtifact] = []
    for idx, row in enumerate(_select_files(rows, max_files, max_bytes), start=1):
        name = str(row.get("name") or row.get("key") or row.get("filename") or f"artifact_{idx}")
        links = row.get("links") if isinstance(row.get("links"), dict) else {}
        url = str(
            row.get("download_url")
            or row.get("downloadUrl")
            or links.get("content")
            or links.get("download")
            or links.get("self")
            or row.get("url")
            or ""
        )
        if not url.startswith(("http://", "https://")):
            continue
        safe = _safe_name(name, f"artifact_{idx}{Path(name).suffix}")
        target = dest / provider / safe
        ok, resolved, size, result = _stream_download(session, url, target, max_bytes)
        if not ok:
            out.append(AcquiredArtifact(provider, source_url, resolved, name, "", size, "", result))
            continue
        out.append(AcquiredArtifact(provider, source_url, resolved, name, str(target), size, result, "downloaded"))
    return out


def _zenodo_record_id(url: str) -> str:
    match = ZENODO_ID_RE.search(url or "")
    return match.group(1) if match else ""


def _figshare_article_id(url: str) -> str:
    match = FIGSHARE_API_ID_RE.search(url or "")
    if match:
        return match.group(1)
    parsed = urlparse(url or "")
    if "figshare.com" not in parsed.netloc.lower():
        return ""
    parts = [p for p in parsed.path.split("/") if p]
    if "articles" not in parts:
        return ""
    numeric = [p for p in parts[parts.index("articles") + 1:] if p.isdigit()]
    return numeric[-1] if numeric else ""


def acquire_zenodo(source_url: str, dest: Path, max_files: int, max_bytes: int, session: Any) -> list[AcquiredArtifact]:
    record_id = _zenodo_record_id(source_url)
    if not record_id:
        return [AcquiredArtifact("zenodo", source_url, "", "", "", 0, "", "unresolved_record_id")]
    api = f"https://zenodo.org/api/records/{record_id}"
    try:
        response = session.get(api, timeout=(20, 60), headers={"Accept": "application/json"})
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return [AcquiredArtifact("zenodo", source_url, api, "", "", 0, "", f"metadata_error:{type(exc).__name__}:{exc}")]
    rows: Any = payload.get("files") if isinstance(payload, dict) else []
    if isinstance(rows, dict):
        rows = rows.get("entries") or rows.get("items") or []
    if not isinstance(rows, list):
        rows = []
    out = _download_selected(
        provider="zenodo", source_url=source_url, rows=rows, dest=dest, max_files=max_files,
        max_bytes=max_bytes, session=session,
    )
    return out or [AcquiredArtifact(
        "zenodo", source_url, api, "", "", 0, "", "no_supported_files"
    )]


def acquire_figshare(source_url: str, dest: Path, max_files: int, max_bytes: int, session: Any) -> list[AcquiredArtifact]:
    article_id = _figshare_article_id(source_url)
    if not article_id:
        return [AcquiredArtifact("figshare", source_url, "", "", "", 0, "", "unresolved_article_id")]
    api = f"https://api.figshare.com/v2/articles/{article_id}"
    try:
        response = session.get(api, timeout=(20, 60), headers={"Accept": "application/json"})
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return [AcquiredArtifact("figshare", source_url, api, "", "", 0, "", f"metadata_error:{type(exc).__name__}:{exc}")]
    rows: Any = payload.get("files") if isinstance(payload, dict) else []
    if isinstance(rows, dict):
        rows = rows.get("entries") or rows.get("items") or []
    if not isinstance(rows, list):
        rows = []
    out = _download_selected(
        provider="figshare", source_url=source_url, rows=rows, dest=dest, max_files=max_files,
        max_bytes=max_bytes, session=session,
    )
    return out or [AcquiredArtifact(
        "figshare", source_url, api, "", "", 0, "", "no_supported_files"
    )]


def acquire_direct(source_url: str, dest: Path, max_bytes: int, session: Any, provider: str = "direct_structured_file") -> list[AcquiredArtifact]:
    if not source_url.startswith(("http://", "https://")):
        return [AcquiredArtifact(provider, source_url, "", "", "", 0, "", "unsupported_url")]
    suffix = _suffix_from_name(source_url)
    if suffix not in SELECTABLE_SUFFIXES:
        return [AcquiredArtifact(provider, source_url, source_url, "", "", 0, "", "unsupported_suffix")]
    name = _safe_name(source_url, f"artifact{suffix}")
    target = dest / provider / name
    ok, resolved, size, result = _stream_download(session, source_url, target, max_bytes)
    if not ok:
        return [AcquiredArtifact(provider, source_url, resolved, name, "", size, "", result)]
    return [AcquiredArtifact(provider, source_url, resolved, name, str(target), size, result, "downloaded")]


def acquire_europe_pmc_bundle(source_url: str, dest: Path, max_files: int, max_bytes: int, max_archive_bytes: int, session: Any) -> list[AcquiredArtifact]:
    """Download a Europe PMC supplementary ZIP and retain only parser-supported members."""
    try:
        with session.get(
            source_url, stream=True, timeout=(20, 120), headers={"Accept": "application/zip"}
        ) as response:
            response.raise_for_status()
            clen = response.headers.get("content-length")
            if clen and int(clen) > max_archive_bytes:
                return [AcquiredArtifact(
                    "europe_pmc_supplement", source_url, str(getattr(response, "url", source_url)),
                    "", "", 0, "", f"bundle_too_large:{clen}",
                )]
            buffer = io.BytesIO()
            total = 0
            for chunk in response.iter_content(1024 * 256):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_archive_bytes:
                    return [AcquiredArtifact(
                        "europe_pmc_supplement", source_url, str(getattr(response, "url", source_url)),
                        "", "", total, "", f"bundle_too_large_streamed:{total}",
                    )]
                buffer.write(chunk)
            blob = buffer.getvalue()
    except Exception as exc:
        return [AcquiredArtifact("europe_pmc_supplement", source_url, source_url, "", "", 0, "", f"bundle_error:{type(exc).__name__}:{exc}")]
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except Exception as exc:
        return [AcquiredArtifact("europe_pmc_supplement", source_url, source_url, "", "", len(blob), "", f"bundle_invalid_zip:{type(exc).__name__}:{exc}")]
    rows = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        rows.append({"name": info.filename, "size": info.file_size, "_zip_info": info})
    selected = _select_files(rows, max_files, max_bytes)
    if not selected:
        return [AcquiredArtifact(
            "europe_pmc_supplement", source_url, source_url, "", "", len(blob),
            hashlib.sha256(blob).hexdigest(), "no_supported_files_in_bundle",
        )]
    out: list[AcquiredArtifact] = []
    for idx, row in enumerate(selected, start=1):
        info = row["_zip_info"]
        name = str(row["name"])
        try:
            data = archive.read(info)
        except Exception as exc:
            out.append(AcquiredArtifact("europe_pmc_supplement", source_url, source_url, name, "", 0, "", f"member_error:{type(exc).__name__}:{exc}"))
            continue
        if len(data) > max_bytes:
            out.append(AcquiredArtifact("europe_pmc_supplement", source_url, source_url, name, "", len(data), "", f"too_large:{len(data)}"))
            continue
        safe = _safe_name(name, f"supplement_{idx}{Path(name).suffix}")
        target = dest / "europe_pmc_supplement" / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        out.append(AcquiredArtifact(
            "europe_pmc_supplement", source_url, source_url, name, str(target), len(data),
            hashlib.sha256(data).hexdigest(), "downloaded_bundle_member",
        ))
    return out


def acquire_external_artifacts(
    *, source_type: str, source_url: str, dest: Path, max_files: int, max_bytes: int,
    max_archive_bytes: int, session: Any | None = None,
) -> list[AcquiredArtifact]:
    """Materialize parser-relevant artifacts for one already-discovered public source."""
    if session is None:
        import requests
        session = requests.Session()
        session.headers.update({"User-Agent": "PRIDE-SCP-external-artifact-acquisition/0.1"})
    if source_type == "zenodo":
        return acquire_zenodo(source_url, dest, max_files, max_bytes, session)
    if source_type == "figshare":
        return acquire_figshare(source_url, dest, max_files, max_bytes, session)
    if source_type == "europe_pmc_supplement":
        return acquire_europe_pmc_bundle(source_url, dest, max_files, max_bytes, max_archive_bytes, session)
    if source_type in {"direct_structured_file", "supplementary_media"}:
        return acquire_direct(source_url, dest, max_bytes, session, provider=source_type)
    return []


def self_test() -> None:
    assert classify_external_source("https://github.com/org/repo") == "github"
    assert classify_external_source("https://zenodo.org/records/12345") == "zenodo"
    assert classify_external_source("https://figshare.com/articles/dataset/example/98765") == "figshare"
    assert classify_external_source("https://example.org/files/sample_design.xlsx") == "direct_structured_file"
    assert classify_external_source("supp1.xlsx", "media") == "supplementary_media"
    assert europe_pmc_supplement_url("PMC12345").endswith("/PMC12345/supplementaryFiles")
    assert _zenodo_record_id("https://doi.org/10.5281/zenodo.998877") == "998877"
    assert _figshare_article_id("https://figshare.com/articles/dataset/example/12345678") == "12345678"

    rows = [
        {"name": "figure.png", "size": 100},
        {"name": "sample_design.xlsx", "size": 1000},
        {"name": "metadata.tsv", "size": 200},
        {"name": "huge_mapping.csv", "size": 9999999},
    ]
    selected = _select_files(rows, 3, 100000)
    assert [r["name"] for r in selected] == ["metadata.tsv", "sample_design.xlsx"]

    # ZIP member selection never materializes unsupported image-only members.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("images/figure.png", b"x")
        zf.writestr("tables/sample_metadata.tsv", b"sample\tchannel\nA\t126\n")
    class Response:
        content = buf.getvalue()
        url = "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC1/supplementaryFiles"
        headers = {"content-length": str(len(content))}
        def raise_for_status(self) -> None: pass
        def __enter__(self) -> "Response": return self
        def __exit__(self, *args: Any) -> None: return None
        def iter_content(self, chunk_size: int) -> Iterable[bytes]: yield self.content
    class Session:
        def get(self, *args: Any, **kwargs: Any) -> Response: return Response()
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        got = acquire_europe_pmc_bundle(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC1/supplementaryFiles",
            Path(td), 5, 100000, 100000, Session(),
        )
        assert len(got) == 1 and got[0].remote_name.endswith("sample_metadata.tsv") and got[0].sha256

    class JsonResponse:
        def __init__(self, *, payload: Any = None, content: bytes = b"", url: str = "") -> None:
            self._payload = payload
            self.content = content
            self.url = url
            self.headers = {"content-length": str(len(content))} if content else {}
        def raise_for_status(self) -> None: pass
        def json(self) -> Any: return self._payload
        def __enter__(self) -> "JsonResponse": return self
        def __exit__(self, *args: Any) -> None: return None
        def iter_content(self, chunk_size: int) -> Iterable[bytes]:
            if self.content:
                yield self.content

    class ProviderSession:
        def get(self, url: str, *args: Any, **kwargs: Any) -> JsonResponse:
            if url.endswith("/api/records/998877"):
                return JsonResponse(payload={"files":[{
                    "key":"sample_design.tsv", "size":8,
                    "links":{"content":"https://files.example/zenodo-design"},
                }]}, url=url)
            if url.endswith("/v2/articles/12345678"):
                return JsonResponse(payload={"files":[{
                    "name":"channel_mapping.csv", "size":8,
                    "download_url":"https://files.example/figshare-map",
                }]}, url=url)
            if url == "https://files.example/zenodo-design":
                return JsonResponse(content=b"a\tb\n1\t2\n", url=url)
            if url == "https://files.example/figshare-map":
                return JsonResponse(content=b"a,b\n1,2\n", url=url)
            raise AssertionError(url)

    with tempfile.TemporaryDirectory() as td:
        session = ProviderSession()
        zen = acquire_zenodo("https://doi.org/10.5281/zenodo.998877", Path(td), 4, 100000, session)
        fig = acquire_figshare("https://figshare.com/articles/dataset/example/12345678", Path(td), 4, 100000, session)
        assert len(zen) == 1 and zen[0].status == "downloaded" and zen[0].sha256
        assert len(fig) == 1 and fig[0].status == "downloaded" and fig[0].sha256
    print("sdrf_external_artifact_acquisition self-test: PASS")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
