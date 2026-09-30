#!/usr/bin/env python3
"""Extract provenance-preserving, blocker-scoped claims from registered publications.

Only explicit text matches are eligible.  The extractor never infers an isolation or
mass-spectrometry dissociation method from instrument type, file names, row order, or
publication-wide context.  Accepted claim artifacts retain the exact parent publication
SHA256 and bounded supporting spans so the evidence registry can preserve independent
source lineage without treating the whole manuscript as field-scoped evidence.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VERSION = "pride-scp-sdrf-publication-claims-v0.1.0"

ISOLATION_FIELD = "characteristics[single cell isolation protocol]"
DISSOCIATION_FIELD = "comment[dissociation method]"

SOURCE_FIELDS = [
    "accession",
    "source_kind",
    "source_provider",
    "source_locator",
    "source_identity",
    "publication_doi",
    "publication_pmid",
    "publication_pmcid",
    "publication_identity_status",
    "local_path",
    "trust_class",
    "blocker_field",
    "parent_artifact_sha256",
    "derivation_operation",
    "retrieval_method",
    "original_filename",
    "media_type",
    "claim_value",
    "claim_status",
    "claim_rule_id",
    "claim_text",
    "claim_source_start",
    "claim_source_end",
    "claim_extractor_version",
]

AUDIT_FIELDS = [
    "accession",
    "source_identity",
    "parent_artifact_sha256",
    "blocker_field",
    "claim_value",
    "claim_status",
    "claim_rule_id",
    "claim_text",
    "claim_source_start",
    "claim_source_end",
    "accepted",
    "reason",
]


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_field_name(value: str) -> str:
    return norm(value).lower()


def read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        return [dict(row) for row in csv.DictReader(fh, delimiter="\t")]


def split_fields(raw: str) -> list[str]:
    value = str(raw or "").strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [norm(x) for x in parsed if norm(x)]
    return [norm(x) for x in value.split(";") if norm(x)]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def stable_id(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True)
class ClaimRule:
    rule_id: str
    blocker_field: str
    claim_value: str
    claim_status: str
    pattern: re.Pattern[str]


def _rule(
    rule_id: str,
    field: str,
    value: str,
    status: str,
    pattern: str,
) -> ClaimRule:
    return ClaimRule(
        rule_id=rule_id,
        blocker_field=field,
        claim_value=value,
        claim_status=status,
        pattern=re.compile(pattern, re.IGNORECASE | re.MULTILINE),
    )


RULES: tuple[ClaimRule, ...] = (
    # Pinned single-cell isolation vocabulary.  These require explicit method language;
    # generic mentions of "single cell" or an instrument are intentionally insufficient.
    _rule(
        "isolation_manual_picking_v1",
        ISOLATION_FIELD,
        "manual picking",
        "supported_vocabulary",
        r"\bmanual(?:ly)?\s+(?:pick(?:ed|ing)?|isolat(?:ed|ion)|dissect(?:ed|ion|ing)?)\b|"
        r"\b(?:individual|single)\s+cells?\s+(?:were\s+)?(?:picked|transferred)\b|"
        r"\bmicropipett(?:e|ing)\b.*\b(?:single|individual)\s+cells?\b",
    ),
    _rule(
        "isolation_facs_v1",
        ISOLATION_FIELD,
        "FACS",
        "supported_vocabulary",
        r"\bFACS\b|\bfluorescence[- ]activated\s+cell\s+sort(?:ing|ed)?\b",
    ),
    _rule(
        "isolation_cellenone_v1",
        ISOLATION_FIELD,
        "cellenONE",
        "supported_vocabulary",
        r"\bcellenONE\b",
    ),
    _rule(
        "isolation_lcm_v1",
        ISOLATION_FIELD,
        "laser capture microdissection",
        "supported_vocabulary",
        r"\blaser\s+capture\s+microdissection\b|\bLCM\b.{0,80}\b(?:cell|tissue)\b",
    ),
    _rule(
        "isolation_nanopots_v1",
        ISOLATION_FIELD,
        "nanoPOTS",
        "supported_vocabulary",
        r"\bnanoPOTS\b",
    ),
    _rule(
        "isolation_droplet_microfluidics_v1",
        ISOLATION_FIELD,
        "droplet microfluidics",
        "supported_vocabulary",
        r"\bdroplet\s+microfluidic(?:s)?\b",
    ),
    _rule(
        "isolation_acoustic_droplet_v1",
        ISOLATION_FIELD,
        "acoustic droplet ejection",
        "supported_vocabulary",
        r"\bacoustic\s+droplet\s+ejection\b",
    ),
    _rule(
        "isolation_microfluidics_v1",
        ISOLATION_FIELD,
        "microfluidics",
        "supported_vocabulary",
        r"(?<!droplet )\bmicrofluidic(?:s)?\b",
    ),
    # Explicit collection/isolation methods outside the current pinned vocabulary are useful
    # blocker evidence, but must not be silently coerced to a supported term.
    _rule(
        "isolation_transvaginal_puncture_v1",
        ISOLATION_FIELD,
        "transvaginal puncture",
        "unsupported_vocabulary",
        r"\boocytes?\s+(?:were\s+)?obtained\s+by\s+transvaginal\s+puncture\b|"
        r"\btransvaginal\s+puncture\b.{0,120}\boocytes?\b",
    ),
    _rule(
        "isolation_microaspiration_v1",
        ISOLATION_FIELD,
        "microaspiration",
        "unsupported_vocabulary",
        r"\bmicroaspirat(?:e|ed|ion|ing)\b",
    ),
    _rule(
        "isolation_patch_clamp_aspiration_v1",
        ISOLATION_FIELD,
        "patch-clamp aspiration",
        "unsupported_vocabulary",
        r"\bpatch[- ]clamp\b.{0,120}\baspirat(?:e|ed|ion|ing)\b",
    ),
    _rule(
        "isolation_capillary_microsampling_v1",
        ISOLATION_FIELD,
        "capillary microsampling",
        "unsupported_vocabulary",
        r"\bcapillary\s+microsampl(?:e|ed|ing)\b",
    ),
    # Proteomics dissociation claims.  Instrument family alone must never imply one of these.
    _rule(
        "dissociation_hcd_v1",
        DISSOCIATION_FIELD,
        "HCD",
        "supported_vocabulary",
        r"\bHCD\b|\bhigher[- ]energy\s+collisional\s+dissociation\b|"
        r"\bbeam[- ]type\s+collision[- ]induced\s+dissociation\b",
    ),
    _rule(
        "dissociation_cid_v1",
        DISSOCIATION_FIELD,
        "CID",
        "supported_vocabulary",
        r"\bCID\b|(?<!beam-type )\bcollision[- ]induced\s+dissociation\b",
    ),
    _rule(
        "dissociation_etd_v1",
        DISSOCIATION_FIELD,
        "ETD",
        "supported_vocabulary",
        r"\bETD\b|\belectron\s+transfer\s+dissociation\b",
    ),
    _rule(
        "dissociation_ecd_v1",
        DISSOCIATION_FIELD,
        "ECD",
        "supported_vocabulary",
        r"\bECD\b|\belectron\s+capture\s+dissociation\b",
    ),
    _rule(
        "dissociation_uvpd_v1",
        DISSOCIATION_FIELD,
        "UVPD",
        "supported_vocabulary",
        r"\bUVPD\b|\bultraviolet\s+photodissociation\b",
    ),
)

RULES_BY_FIELD: dict[str, tuple[ClaimRule, ...]] = {}
for _field in {normalize_field_name(rule.blocker_field) for rule in RULES}:
    RULES_BY_FIELD[_field] = tuple(
        rule for rule in RULES if normalize_field_name(rule.blocker_field) == _field
    )


def _body_before_references(text: str) -> str:
    match = re.search(r"(?im)^\s*REFERENCES\s*$", text)
    return text[: match.start()] if match else text


def _bounded_context(text: str, start: int, end: int, radius: int = 240) -> str:
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    return norm(text[lo:hi])


def _blockers_by_accession(path: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for row in read_tsv(path):
        accession = norm(row.get("accession")).upper()
        if not accession:
            continue
        fields = split_fields(row.get("blocker_fields", ""))
        if not fields and row.get("blocker_field"):
            fields = split_fields(row["blocker_field"])
        out[accession] = fields
    return out


def _publication_rows(path: Path) -> list[dict[str, str]]:
    rows = []
    for row in read_tsv(path):
        if norm(row.get("source_kind")) != "publication_fulltext":
            continue
        if norm(row.get("is_independent")).lower() not in {"true", "1", "yes"}:
            continue
        if norm(row.get("trust_class")) != "trusted_independent":
            continue
        local_path = Path(norm(row.get("local_path")))
        if not local_path.is_file():
            continue
        declared = norm(row.get("artifact_sha256")).lower()
        actual = sha256_file(local_path)
        if declared and declared != actual:
            raise ValueError(
                f"registered publication SHA mismatch for {local_path}: {declared} != {actual}"
            )
        item = dict(row)
        item["artifact_sha256"] = actual
        item["local_path"] = str(local_path)
        rows.append(item)
    return rows


def _write_claim_artifact(
    artifact_dir: Path,
    *,
    publication: dict[str, str],
    blocker_field: str,
    claim_value: str,
    claim_status: str,
    matches: list[dict[str, Any]],
) -> Path:
    payload = {
        "schema_version": "pride-scp-sdrf-publication-claim-v1",
        "extractor_version": VERSION,
        "accession": norm(publication.get("accession")).upper(),
        "source_identity": norm(publication.get("source_identity")),
        "parent_artifact_sha256": norm(publication.get("artifact_sha256")).lower(),
        "blocker_field": blocker_field,
        "claim_value": claim_value,
        "claim_status": claim_status,
        "supporting_spans": matches,
    }
    claim_id = stable_id(payload)
    acc = payload["accession"]
    field_slug = re.sub(r"[^a-z0-9]+", "_", blocker_field.lower()).strip("_")
    path = artifact_dir / acc / field_slug / f"claim_{claim_id[:20]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def build_publication_claim_rows(
    evidence_registry: Path,
    blocker_manifest: Path,
    artifact_dir: Path,
) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, Any]]:
    blockers = _blockers_by_accession(blocker_manifest)
    publications = _publication_rows(evidence_registry)
    source_rows: list[dict[str, str]] = []
    audit_rows: list[dict[str, str]] = []
    skipped_empty_blocker_fields = 0
    unsupported_blocker_fields = 0
    no_claim_fields = 0
    conflicting_fields = 0

    for publication in publications:
        accession = norm(publication.get("accession")).upper()
        fields = blockers.get(accession, [])
        if not fields:
            skipped_empty_blocker_fields += 1
            continue
        text_path = Path(publication["local_path"])
        text = _body_before_references(text_path.read_text(encoding="utf-8", errors="replace"))

        for blocker_field in fields:
            normalized_field = normalize_field_name(blocker_field)
            rules = RULES_BY_FIELD.get(normalized_field, ())
            if not rules:
                unsupported_blocker_fields += 1
                continue

            detections: dict[str, dict[str, Any]] = {}
            for rule in rules:
                matches = []
                for match in rule.pattern.finditer(text):
                    matches.append(
                        {
                            "rule_id": rule.rule_id,
                            "start": match.start(),
                            "end": match.end(),
                            "matched_text": match.group(0),
                            "matched_text_normalized": norm(match.group(0)),
                            "matched_text_sha256": hashlib.sha256(
                                match.group(0).encode("utf-8")
                            ).hexdigest(),
                            "context": _bounded_context(text, match.start(), match.end()),
                        }
                    )
                    if len(matches) >= 3:
                        break
                if not matches:
                    continue
                existing = detections.get(rule.claim_value)
                if existing is None:
                    detections[rule.claim_value] = {
                        "claim_status": rule.claim_status,
                        "rule_ids": [rule.rule_id],
                        "matches": matches,
                    }
                else:
                    if rule.rule_id not in existing["rule_ids"]:
                        existing["rule_ids"].append(rule.rule_id)
                    existing["matches"].extend(matches)
                    existing["matches"] = existing["matches"][:3]

            if not detections:
                no_claim_fields += 1
                continue

            if len(detections) != 1:
                conflicting_fields += 1
                for value, detected in sorted(detections.items()):
                    first_match = detected["matches"][0]
                    audit_rows.append(
                        {
                            "accession": accession,
                            "source_identity": norm(publication.get("source_identity")),
                            "parent_artifact_sha256": publication["artifact_sha256"],
                            "blocker_field": blocker_field,
                            "claim_value": value,
                            "claim_status": "conflict_unresolved",
                            "claim_rule_id": ";".join(sorted(detected["rule_ids"])),
                            "claim_text": first_match["context"],
                            "claim_source_start": str(first_match["start"]),
                            "claim_source_end": str(first_match["end"]),
                            "accepted": "false",
                            "reason": "multiple_distinct_values_for_same_publication_and_blocker_field",
                        }
                    )
                continue

            claim_value, detected = next(iter(detections.items()))
            claim_status = str(detected["claim_status"])
            matches = list(detected["matches"])
            artifact = _write_claim_artifact(
                artifact_dir,
                publication=publication,
                blocker_field=blocker_field,
                claim_value=claim_value,
                claim_status=claim_status,
                matches=matches,
            )
            first_match = matches[0]
            rule_ids = ";".join(sorted(detected["rule_ids"]))
            source_row = {
                "accession": accession,
                "source_kind": "publication_field_claim",
                "source_provider": norm(publication.get("source_provider")),
                "source_locator": norm(publication.get("source_locator")),
                "source_identity": norm(publication.get("source_identity")),
                "publication_doi": norm(publication.get("publication_doi")),
                "publication_pmid": norm(publication.get("publication_pmid")),
                "publication_pmcid": norm(publication.get("publication_pmcid")),
                "publication_identity_status": norm(
                    publication.get("publication_identity_status")
                ),
                "local_path": str(artifact),
                # Independence is inherited through the exact registered parent SHA and the
                # approved deterministic claim-extraction derivation; this row is not a new
                # primary source.
                "trust_class": "derived_publication_claim",
                "blocker_field": blocker_field,
                "parent_artifact_sha256": publication["artifact_sha256"],
                "derivation_operation": "extract_publication_claim",
                "retrieval_method": VERSION,
                "original_filename": artifact.name,
                "media_type": "application/json",
                "claim_value": claim_value,
                "claim_status": claim_status,
                "claim_rule_id": rule_ids,
                "claim_text": first_match["context"],
                "claim_source_start": str(first_match["start"]),
                "claim_source_end": str(first_match["end"]),
                "claim_extractor_version": VERSION,
            }
            source_rows.append(source_row)
            audit_rows.append(
                {
                    "accession": accession,
                    "source_identity": source_row["source_identity"],
                    "parent_artifact_sha256": source_row["parent_artifact_sha256"],
                    "blocker_field": blocker_field,
                    "claim_value": claim_value,
                    "claim_status": claim_status,
                    "claim_rule_id": rule_ids,
                    "claim_text": first_match["context"],
                    "claim_source_start": str(first_match["start"]),
                    "claim_source_end": str(first_match["end"]),
                    "accepted": "true",
                    "reason": "single_explicit_value_for_publication_and_blocker_field",
                }
            )

    source_rows.sort(
        key=lambda row: (
            row["accession"],
            normalize_field_name(row["blocker_field"]),
            row["source_identity"],
            row["claim_value"],
        )
    )
    audit_rows.sort(
        key=lambda row: (
            row["accession"],
            normalize_field_name(row["blocker_field"]),
            row["source_identity"],
            row["claim_value"],
        )
    )
    summary = {
        "version": VERSION,
        "publication_records_considered": len(publications),
        "publication_accessions_considered": len({row["accession"] for row in publications}),
        "claims_emitted": len(source_rows),
        "claim_accessions": len({row["accession"] for row in source_rows}),
        "supported_vocabulary_claims": sum(
            row["claim_status"] == "supported_vocabulary" for row in source_rows
        ),
        "unsupported_vocabulary_claims": sum(
            row["claim_status"] == "unsupported_vocabulary" for row in source_rows
        ),
        "conflicting_fields": conflicting_fields,
        "no_claim_fields": no_claim_fields,
        "unsupported_blocker_fields": unsupported_blocker_fields,
        "skipped_empty_blocker_fields": skipped_empty_blocker_fields,
    }
    return source_rows, audit_rows, summary


def write_tsv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=fields,
            delimiter="\t",
            lineterminator="\n",
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-registry", type=Path, required=True)
    parser.add_argument("--blocker-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    source_rows, audit_rows, summary = build_publication_claim_rows(
        args.evidence_registry,
        args.blocker_manifest,
        args.output / "artifacts",
    )
    write_tsv(args.output / "publication_claim_source_manifest.tsv", source_rows, SOURCE_FIELDS)
    write_tsv(args.output / "publication_claim_audit.tsv", audit_rows, AUDIT_FIELDS)
    (args.output / "publication_claim_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
