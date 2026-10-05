#!/usr/bin/env python3
"""Extract fail-closed RAW/reporter/well mappings from structured analysis bundles.

This adapter is deliberately non-generative.  It recognizes a reusable relational
bundle schema used by analysis packages that contain:

* ``file_sample_mapping.txt`` mapping an explicit file name to Plate/Sample;
* ``plate_layout_mapping.txt`` mapping Plate to sort/label/sample layout matrices;
* one or more ``*_InputFiles.txt`` tables mapping File ID to File Name; and
* one or more ``*_Proteins.txt`` tables whose abundance headers explicitly name
  ``(File ID, reporter channel)`` pairs.

For every observed File-ID/channel pair the adapter follows only explicit table
relations to one unique plate well and its source population.  It never uses RAW
filename order, numeric filename semantics, worksheet order, or inferred channel
cardinality.  Duplicate analysis roots are collapsed only when they agree exactly;
conflicting roots fail closed for the affected RAW/channel key.

The flattened TSV emitted by :func:`write_mapping_tsv` intentionally uses column
aliases consumed by ``sdrf_structured_mapping_resolver.py``.  This lets the existing
validator-gated closure harness expand one-RAW skeleton rows into source-backed
multiplex rows without accession-specific production code.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import ntpath
import re
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = "pride-scp-structured-multiplex-bundle-v1"
REPORTER_RE = re.compile(
    r"^(?:TMT(?:PRO)?|ITRAQ)?\s*[-_ ]*"
    r"(11[3-9]|12[0-6]|127[NC]?|128[NC]?|129[NC]?|130[NC]?|131[NC]?|132[NC]?|133[NC]?|134[NC]?|135[NC]?)$",
    re.I,
)
CONTROL_POPULATION_RE = re.compile(
    r"(?i)^\s*(?:empty|blank|unused|carrier(?:\s+channel)?|booster(?:\s+channel)?|reference(?:\s+channel)?|ref(?:erence)?|na|n/a|none)\s*$"
)
SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class BundleMappingRecord:
    accession: str
    raw_file: str
    reporter_channel: str
    source_name: str
    cell_identifier: str
    sample_type: str
    plate: str
    multiplex_sample: str
    plate_well: str
    source_population: str
    source_archive: str
    archive_root: str
    input_files_table: str
    file_sample_mapping_table: str
    plate_layout_mapping_table: str
    sort_layout_table: str
    label_layout_table: str
    sample_layout_table: str
    raw_file_id: str
    raw_present_in_pride_manifest: str
    mapping_confidence: str
    mapping_key: str


@dataclass(frozen=True)
class RawBundleRecord:
    archive_root: str
    input_files_table: str
    file_sample_mapping_table: str
    plate_layout_mapping_table: str
    sort_layout_table: str
    label_layout_table: str
    sample_layout_table: str
    raw_file_id: str
    raw_file_name: str
    plate: str
    sample: str
    reporter_channel: str
    plate_row: str
    plate_column: str
    plate_well: str
    source_population: str
    raw_present_in_pride_manifest: bool


def sval(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def normalize_reporter(value: Any) -> str:
    text = sval(value).upper().replace("TMT PRO", "TMTPRO")
    match = REPORTER_RE.fullmatch(text)
    return match.group(1).upper() if match else ""


def reporter_label(channel: str) -> str:
    channel = normalize_reporter(channel)
    return f"TMT{channel}" if channel else ""


def normalize_plate_col(value: Any) -> str:
    text = sval(value)
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        return text
    return str(int(number)) if number.is_integer() else text


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_tsv_bytes(data: bytes) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(decode_text(data)), delimiter="\t")
    rows = [
        {str(k): sval(v) for k, v in row.items() if k is not None}
        for row in reader
    ]
    return list(reader.fieldnames or []), rows


def read_matrix_bytes(data: bytes) -> tuple[list[str], dict[tuple[str, str], str]]:
    rows = list(csv.reader(io.StringIO(decode_text(data)), delimiter="\t"))
    if not rows:
        return [], {}
    header = [sval(x) for x in rows[0]]
    matrix: dict[tuple[str, str], str] = {}
    for row in rows[1:]:
        if not row:
            continue
        row_label = sval(row[0]).upper()
        if not row_label:
            continue
        for idx, col_label in enumerate(header[1:], start=1):
            value = sval(row[idx]) if idx < len(row) else ""
            if value:
                matrix[(row_label, normalize_plate_col(col_label))] = value
    return header[1:], matrix


def key_lookup(row: Mapping[str, str], *names: str) -> str:
    lowered = {
        re.sub(r"\s+", " ", k.strip().lower()): v
        for k, v in row.items()
    }
    for name in names:
        value = lowered.get(re.sub(r"\s+", " ", name.strip().lower()))
        if value is not None:
            return sval(value)
    return ""


def portable_basename(value: Any) -> str:
    return ntpath.basename(sval(value).replace("/", "\\"))


def resolve_zip_member(
    names: Sequence[str], base_dir: PurePosixPath, referenced_name: str
) -> tuple[str | None, str | None]:
    ref = referenced_name.strip().replace("\\", "/")
    if not ref:
        return None, "empty_reference"
    direct = str(base_dir / ref)
    if direct in names:
        return direct, None
    basename = PurePosixPath(ref).name.lower()
    candidates = [
        name
        for name in names
        if PurePosixPath(name).name.lower() == basename
        and str(PurePosixPath(name).parent).startswith(str(base_dir))
    ]
    if len(candidates) == 1:
        return candidates[0], None
    if not candidates:
        candidates = [
            name for name in names if PurePosixPath(name).name.lower() == basename
        ]
    if len(candidates) == 1:
        return candidates[0], None
    if not candidates:
        return None, f"referenced_member_not_found:{referenced_name}"
    return None, f"referenced_member_ambiguous:{referenced_name}:{'|'.join(candidates)}"


def members_with_suffix(
    names: Sequence[str], base_dir: PurePosixPath, suffix: str
) -> list[str]:
    prefix = str(base_dir)
    return sorted(
        name
        for name in names
        if str(PurePosixPath(name).parent) == prefix
        and PurePosixPath(name).name.lower().endswith(suffix.lower())
    )


def abundance_file_channel_pairs(
    zf: zipfile.ZipFile, members: Sequence[str]
) -> tuple[set[tuple[str, str]], list[str]]:
    """Recover explicit File-ID/reporter pairs from abundance column headers.

    The recognized schema is the Proteome Discoverer/SCeptre header convention
    ``Abundance ... <File ID> <reporter>``.  We deliberately require exactly two
    tokens after the first two whitespace-separated header tokens because that is
    the author-pipeline contract; unrecognized headers fail closed.
    """
    pairs: set[tuple[str, str]] = set()
    issues: list[str] = []
    for member in members:
        with zf.open(member) as handle:
            header_line = decode_text(handle.readline()).rstrip("\r\n")
        headers = next(csv.reader([header_line], delimiter="\t"), [])
        abundance_headers = [h for h in headers if "abundance" in h.lower()]
        if not abundance_headers:
            issues.append(f"{member}:no_abundance_columns")
            continue
        for column in abundance_headers:
            parts = column.split(" ")[2:]
            if len(parts) != 2:
                issues.append(f"{member}:unexpected_abundance_header:{column}")
                continue
            file_id, channel_raw = parts
            channel = normalize_reporter(channel_raw)
            if not channel:
                issues.append(f"{member}:unrecognized_reporter_in_header:{column}")
                continue
            pairs.add((sval(file_id), channel))
    return pairs, issues


def parse_bundle(
    archive_path: Path,
    *,
    accession: str,
    pride_raw_names: Iterable[str] = (),
) -> tuple[list[RawBundleRecord], list[str], dict[str, Any]]:
    pride_raws = {Path(x).name.casefold() for x in pride_raw_names}
    issues: list[str] = []
    records: list[RawBundleRecord] = []
    roots: list[dict[str, Any]] = []

    with zipfile.ZipFile(archive_path) as zf:
        names = [name for name in zf.namelist() if not name.endswith("/")]
        fsm_members = [
            name
            for name in names
            if PurePosixPath(name).name.lower() == "file_sample_mapping.txt"
        ]
        plm_members = {
            str(PurePosixPath(name).parent): name
            for name in names
            if PurePosixPath(name).name.lower() == "plate_layout_mapping.txt"
        }

        for fsm_member in sorted(fsm_members):
            base_dir = PurePosixPath(fsm_member).parent
            root = str(base_dir)
            root_issue_start = len(issues)
            root_records: list[RawBundleRecord] = []
            plm_member = plm_members.get(root)
            if plm_member is None:
                issues.append(f"{root}:plate_layout_mapping.txt_missing")
                continue
            input_members = members_with_suffix(names, base_dir, "_inputfiles.txt")
            proteins_members = members_with_suffix(names, base_dir, "_proteins.txt")
            if not input_members:
                issues.append(f"{root}:*_InputFiles.txt_missing")
                continue
            if not proteins_members:
                issues.append(f"{root}:*_Proteins.txt_missing")
                continue

            _, fsm_rows = read_tsv_bytes(zf.read(fsm_member))
            _, plm_rows = read_tsv_bytes(zf.read(plm_member))

            plate_assets: dict[str, dict[str, dict[tuple[str, str], str]]] = {}
            plate_members: dict[str, dict[str, str]] = {}
            for row in plm_rows:
                plate = key_lookup(row, "Plate")
                if not plate:
                    continue
                refs = {
                    "sort": key_lookup(row, "Sort Layout"),
                    "label": key_lookup(row, "Label Layout"),
                    "sample": key_lookup(row, "Sample Layout"),
                }
                assets: dict[str, dict[tuple[str, str], str]] = {}
                members: dict[str, str] = {}
                failed = False
                for kind, ref in refs.items():
                    member, error = resolve_zip_member(names, base_dir, ref)
                    if error or member is None:
                        issues.append(f"{root}:plate={plate}:{kind}:{error}")
                        failed = True
                        break
                    _, matrix = read_matrix_bytes(zf.read(member))
                    assets[kind] = matrix
                    members[kind] = member
                if not failed:
                    plate_assets[plate] = assets
                    plate_members[plate] = members

            file_sample_by_name: dict[str, list[tuple[str, str]]] = defaultdict(list)
            for row in fsm_rows:
                fname = portable_basename(key_lookup(row, "File Name", "File", "Filename"))
                plate = key_lookup(row, "Plate")
                sample = key_lookup(row, "Sample")
                if fname and plate and sample:
                    file_sample_by_name[fname].append((plate, sample))

            file_id_to_raw: dict[str, str] = {}
            file_id_source: dict[str, str] = {}
            for input_member in input_members:
                _, input_rows = read_tsv_bytes(zf.read(input_member))
                for input_row in input_rows:
                    file_id = key_lookup(input_row, "File ID", "FileID")
                    raw_file = portable_basename(
                        key_lookup(input_row, "File Name", "File", "Filename")
                    )
                    if not file_id or not raw_file:
                        continue
                    prior = file_id_to_raw.get(file_id)
                    if prior is not None and prior.casefold() != raw_file.casefold():
                        issues.append(
                            f"{root}:file_id={file_id}:maps_to_multiple_files:{prior}|{raw_file}"
                        )
                        continue
                    file_id_to_raw[file_id] = raw_file
                    file_id_source[file_id] = input_member

            abundance_pairs, pair_issues = abundance_file_channel_pairs(zf, proteins_members)
            issues.extend(f"{root}:{issue}" for issue in pair_issues)

            for file_id, channel in sorted(abundance_pairs):
                raw_file = file_id_to_raw.get(file_id, "")
                if not raw_file:
                    issues.append(f"{root}:file_id={file_id}:missing_InputFiles_mapping")
                    continue
                mappings = sorted(set(file_sample_by_name.get(raw_file, [])))
                if not mappings:
                    issues.append(f"{root}:{raw_file}:missing_file_sample_mapping")
                    continue
                if len(mappings) != 1:
                    issues.append(
                        f"{root}:{raw_file}:file_sample_mapping_not_one_to_one:"
                        + "|".join(f"{plate},{sample}" for plate, sample in mappings)
                    )
                    continue
                plate, sample = mappings[0]
                assets = plate_assets.get(plate)
                members = plate_members.get(plate)
                if assets is None or members is None:
                    issues.append(f"{root}:{raw_file}:plate={plate}:layout_assets_missing")
                    continue
                wells: list[tuple[str, str]] = []
                for well, sample_value in assets["sample"].items():
                    if sval(sample_value) != sval(sample):
                        continue
                    if normalize_reporter(assets["label"].get(well, "")) == channel:
                        wells.append(well)
                if len(wells) > 1:
                    issues.append(
                        f"{root}:{raw_file}:plate={plate}:sample={sample}:channel={channel}:"
                        f"multiple_wells={'|'.join(a+b for a,b in wells)}"
                    )
                    continue
                if not wells:
                    # The author workflow treats this as an unassigned/NA well.  Do not invent one.
                    continue
                plate_row, plate_col = wells[0]
                population = sval(assets["sort"].get((plate_row, plate_col), ""))
                root_records.append(
                    RawBundleRecord(
                        archive_root=root,
                        input_files_table=file_id_source.get(file_id, ""),
                        file_sample_mapping_table=fsm_member,
                        plate_layout_mapping_table=plm_member,
                        sort_layout_table=members["sort"],
                        label_layout_table=members["label"],
                        sample_layout_table=members["sample"],
                        raw_file_id=file_id,
                        raw_file_name=raw_file,
                        plate=plate,
                        sample=sample,
                        reporter_channel=channel,
                        plate_row=plate_row,
                        plate_column=plate_col,
                        plate_well=f"{plate_row}{plate_col}",
                        source_population=population,
                        raw_present_in_pride_manifest=(
                            raw_file.casefold() in pride_raws if pride_raws else False
                        ),
                    )
                )

            root_issues = issues[root_issue_start:]
            root_status = "mapping_ready" if not root_issues and root_records else "unresolved"
            if not root_issues:
                records.extend(root_records)
            roots.append(
                {
                    "archive_root": root,
                    "file_sample_mapping": fsm_member,
                    "plate_layout_mapping": plm_member,
                    "input_files_tables": input_members,
                    "proteins_tables": proteins_members,
                    "plates": sorted(plate_assets),
                    "raw_record_count": len(root_records) if not root_issues else 0,
                    "status": root_status,
                    "issues": root_issues,
                }
            )

    summary = {
        "schema_version": SCHEMA_VERSION,
        "accession": accession,
        "archive": str(archive_path),
        "archive_sha256": sha256_file(archive_path),
        "mapping_roots": roots,
        "raw_record_count": len(records),
        "issues": issues,
        "non_generative": True,
        "row_order_inference": False,
        "filename_semantic_inference": False,
    }
    return records, issues, summary


def biological_population(value: str) -> bool:
    value = sval(value)
    return bool(value) and not CONTROL_POPULATION_RE.fullmatch(value)


def safe_identifier(*parts: str) -> str:
    text = "_".join(sval(x) for x in parts if sval(x))
    text = SAFE_ID_RE.sub("_", text).strip("_")
    return text or "cell"


def flatten_mapping_records(
    raw_records: Sequence[RawBundleRecord],
    *,
    accession: str,
    archive_path: Path,
) -> tuple[list[BundleMappingRecord], list[str]]:
    """Collapse duplicate analysis roots and emit only conflict-free biological rows."""
    grouped: dict[tuple[str, str], list[RawBundleRecord]] = defaultdict(list)
    for record in raw_records:
        grouped[(record.raw_file_name.casefold(), record.reporter_channel)].append(record)

    output: list[BundleMappingRecord] = []
    conflicts: list[str] = []
    for (raw_key, channel), rows in sorted(grouped.items()):
        signatures = {
            (
                row.raw_file_name.casefold(),
                row.plate,
                row.sample,
                row.plate_well,
                row.source_population,
                row.raw_present_in_pride_manifest,
            )
            for row in rows
        }
        if len(signatures) != 1:
            conflicts.append(
                f"raw={raw_key}:channel={channel}:cross_root_conflict:"
                + "|".join(sorted(repr(sig) for sig in signatures))
            )
            continue
        row = rows[0]
        if not row.raw_present_in_pride_manifest:
            conflicts.append(
                f"raw={row.raw_file_name}:channel={channel}:absent_from_pride_manifest"
            )
            continue
        if not biological_population(row.source_population):
            # Carrier/blank/empty/reference wells are set-level evidence, not biological rows.
            continue
        identity = safe_identifier(row.plate, f"S{row.sample}", row.plate_well)
        output.append(
            BundleMappingRecord(
                accession=accession,
                raw_file=row.raw_file_name,
                reporter_channel=reporter_label(channel),
                source_name=identity,
                cell_identifier="",
                sample_type="",
                plate=row.plate,
                multiplex_sample=row.sample,
                plate_well=row.plate_well,
                source_population=row.source_population,
                source_archive=archive_path.name,
                archive_root=row.archive_root,
                input_files_table=row.input_files_table,
                file_sample_mapping_table=row.file_sample_mapping_table,
                plate_layout_mapping_table=row.plate_layout_mapping_table,
                sort_layout_table=row.sort_layout_table,
                label_layout_table=row.label_layout_table,
                sample_layout_table=row.sample_layout_table,
                raw_file_id=row.raw_file_id,
                raw_present_in_pride_manifest="true",
                mapping_confidence="high",
                mapping_key="exact_raw_name_channel_well",
            )
        )

    # Enforce one biological identity per RAW/reporter and one reporter per derived cell identity.
    seen_raw_channel: set[tuple[str, str]] = set()
    seen_raw_identity: set[tuple[str, str]] = set()
    valid: list[BundleMappingRecord] = []
    for row in output:
        rk = (row.raw_file.casefold(), row.reporter_channel.casefold())
        ik = (row.raw_file.casefold(), row.source_name.casefold())
        if rk in seen_raw_channel:
            conflicts.append(f"raw={row.raw_file}:duplicate_reporter={row.reporter_channel}")
            continue
        if row.source_name and ik in seen_raw_identity:
            conflicts.append(f"raw={row.raw_file}:duplicate_source_identity={row.source_name}")
            continue
        seen_raw_channel.add(rk)
        if row.source_name:
            seen_raw_identity.add(ik)
        valid.append(row)
    return valid, conflicts


def write_mapping_tsv(path: Path, rows: Sequence[BundleMappingRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(BundleMappingRecord.__dataclass_fields__)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def self_test() -> None:
    def matrix(rows: list[list[str]]) -> bytes:
        return ("\n".join("\t".join(x) for x in rows) + "\n").encode()

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        archive = root / "analysis.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            base = "analysis/data"
            zf.writestr(
                f"{base}/file_sample_mapping.txt",
                "File Name\tPlate\tSample\nA.raw\tP1\t1\n",
            )
            zf.writestr(
                f"{base}/plate_layout_mapping.txt",
                "Plate\tSort Layout\tLabel Layout\tSample Layout\n"
                "P1\tsort_layout.txt\tlabel_layout.txt\tsample_layout.txt\n",
            )
            zf.writestr(
                f"{base}/run_InputFiles.txt",
                "File ID\tFile Name\nF1\tC:\\data\\A.raw\n",
            )
            zf.writestr(
                f"{base}/run_Proteins.txt",
                "Protein\tAbundance X F1 127N\tAbundance X F1 128N\tAbundance X F1 126\n",
            )
            zf.writestr(
                f"{base}/sample_layout.txt",
                matrix([["", "1", "2", "3"], ["A", "1", "1", "1"]]),
            )
            zf.writestr(
                f"{base}/label_layout.txt",
                matrix([["", "1", "2", "3"], ["A", "127N", "128N", "126"]]),
            )
            zf.writestr(
                f"{base}/sort_layout.txt",
                matrix([["", "1", "2", "3"], ["A", "blast", "prog", "booster"]]),
            )

        raw, issues, summary = parse_bundle(
            archive, accession="PXD900001", pride_raw_names=["A.raw"]
        )
        assert not issues, issues
        assert summary["raw_record_count"] == 3
        flat, conflicts = flatten_mapping_records(
            raw, accession="PXD900001", archive_path=archive
        )
        assert not conflicts, conflicts
        assert len(flat) == 2
        assert {x.reporter_channel for x in flat} == {"TMT127N", "TMT128N"}
        assert {x.source_name for x in flat} == {"P1_S1_A1", "P1_S1_A2"}
        assert all(not x.cell_identifier and not x.sample_type for x in flat)

        # Repeated analysis roots are acceptable only when they agree exactly.
        doubled = raw + [
            RawBundleRecord(**{**asdict(x), "archive_root": "analysis/integrated"})
            for x in raw
        ]
        flat2, conflicts2 = flatten_mapping_records(
            doubled, accession="PXD900001", archive_path=archive
        )
        assert not conflicts2 and len(flat2) == 2

        biological = next(x for x in raw if x.reporter_channel == "127N")
        conflict = RawBundleRecord(
            **{
                **asdict(biological),
                "archive_root": "analysis/conflict",
                "plate_well": "B9",
            }
        )
        flat3, conflicts3 = flatten_mapping_records(
            raw + [conflict], accession="PXD900001", archive_path=archive
        )
        assert conflicts3
        assert all(x.reporter_channel != "TMT127N" for x in flat3)

        out = root / "mapping.tsv"
        write_mapping_tsv(out, flat)
        rows = list(csv.DictReader(out.open(), delimiter="\t"))
        assert rows[0]["raw_file"] == "A.raw"
        assert rows[0]["mapping_key"] == "exact_raw_name_channel_well"
        assert rows[0]["multiplex_sample"] == "1"
        assert "sample" not in rows[0]

        bad_archive = root / "bad_analysis.zip"
        with zipfile.ZipFile(bad_archive, "w") as zf:
            base = "analysis/data"
            zf.writestr(f"{base}/file_sample_mapping.txt", "File Name\tPlate\tSample\nA.raw\tP1\t1\n")
            zf.writestr(
                f"{base}/plate_layout_mapping.txt",
                "Plate\tSort Layout\tLabel Layout\tSample Layout\n"
                "P1\tsort_layout.txt\tlabel_layout.txt\tsample_layout.txt\n",
            )
            zf.writestr(f"{base}/run_InputFiles.txt", "File ID\tFile Name\nF1\tA.raw\n")
            zf.writestr(
                f"{base}/run_Proteins.txt",
                "Protein\tAbundance X F1 127N\tAbundance malformed header\n",
            )
            zf.writestr(f"{base}/sample_layout.txt", matrix([["", "1"], ["A", "1"]]))
            zf.writestr(f"{base}/label_layout.txt", matrix([["", "1"], ["A", "127N"]]))
            zf.writestr(f"{base}/sort_layout.txt", matrix([["", "1"], ["A", "cell"]]))
        bad_raw, bad_issues, bad_summary = parse_bundle(
            bad_archive, accession="PXD900001", pride_raw_names=["A.raw"]
        )
        assert bad_issues and not bad_raw
        assert bad_summary["mapping_roots"][0]["status"] == "unresolved"

    print("sdrf_structured_multiplex_bundle self-test: PASS")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive", type=Path)
    p.add_argument("--accession")
    p.add_argument("--raw-name", action="append", default=[])
    p.add_argument("--output", type=Path)
    p.add_argument("--report", type=Path)
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()
    if args.self_test:
        self_test()
        return 0
    if not args.archive or not args.archive.is_file():
        p.error("--archive is required")
    if not args.accession:
        p.error("--accession is required")
    if not args.output or not args.report:
        p.error("--output and --report are required")
    raw, issues, summary = parse_bundle(
        args.archive, accession=args.accession.upper(), pride_raw_names=args.raw_name
    )
    flat, conflicts = flatten_mapping_records(
        raw, accession=args.accession.upper(), archive_path=args.archive
    )
    write_mapping_tsv(args.output, flat)
    summary.update(
        {
            "mapping_rows": len(flat),
            "parse_issues": issues,
            "mapping_conflicts": conflicts,
            "status": "mapping_ready" if flat and not conflicts else "unresolved",
            "output": str(args.output),
        }
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
