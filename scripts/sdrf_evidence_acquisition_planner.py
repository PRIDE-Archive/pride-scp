#!/usr/bin/env python3
"""Blocker-directed evidence acquisition planner for PRIDE-SCP SDRF Harness v2.2.

The planner is non-networking and non-generative. It chooses which trusted source class should
be acquired next for each evidence-limited accession. Fetching is performed by existing or future
provider adapters. Completed no-result source attempts are content-addressed and are not repeated
until the evidence identity, blocker, source strategy version or policy changes.
"""
from __future__ import annotations

import argparse
import csv
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from sdrf_annotation_state import canonical_sha256, normalize_field_name

VERSION = "pride-scp-sdrf-evidence-acquisition-planner-v2.2.0"
CATALOG_VERSION = "pride-scp-sdrf-evidence-source-catalog-v1"

NO_PROGRESS_STATUSES = {
    "source_not_found",
    "no_new_artifact",
    "source_exhausted",
    "unsupported_source",
    "provenance_invalid",
    # PRIDE may expose only a community-curated SDRF for an accession.
    # That artifact is deliberately untrusted for independent evidence and
    # therefore represents no progress for the depositor_sdrf source class.
    "found_untrusted_community_annotation",
}

# This provider result describes the state of the queried source class itself,
# not merely the current evidence bundle. Registering the untrusted community
# source may change evidence_set_sha256, but that must not immediately replay
# the same depositor-SDRF acquisition. A changed blocker, policy, or source
# strategy still permits a fresh attempt.
SOURCE_STATE_NO_PROGRESS_STATUSES = {
    "found_untrusted_community_annotation",
}


@dataclass(frozen=True)
class SourceStrategy:
    source_class: str
    priority: int
    families: tuple[str, ...]
    fields: tuple[str, ...]
    provider: str
    acquisition_method: str
    expected_trust_class: str
    requires_external_locator: bool = True

    @classmethod
    def from_dict(cls, obj: dict[str, Any]) -> "SourceStrategy":
        return cls(
            source_class=str(obj["source_class"]),
            priority=int(obj.get("priority") or 100),
            families=tuple(str(x) for x in obj.get("families") or ()),
            fields=tuple(normalize_field_name(x) for x in obj.get("fields") or ()),
            provider=str(obj.get("provider") or ""),
            acquisition_method=str(obj.get("acquisition_method") or ""),
            expected_trust_class=str(obj.get("expected_trust_class") or "untrusted_or_unknown"),
            requires_external_locator=bool(obj.get("requires_external_locator", True)),
        )


@dataclass(frozen=True)
class AcquisitionAttempt:
    stage_key: str
    accession: str
    source_class: str
    status: str
    evidence_set_sha256: str
    policy_version: str
    blocker_key: str
    strategy_version: str


def read_tsv(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        return [dict(row) for row in csv.DictReader(fh, delimiter="\t")]


def load_catalog(path: Path) -> tuple[str, list[SourceStrategy]]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if obj.get("schema_version") != CATALOG_VERSION:
        raise ValueError(f"unsupported evidence source catalog: {obj.get('schema_version')!r}")
    version = str(obj.get("catalog_revision") or obj["schema_version"])
    return version, [SourceStrategy.from_dict(x) for x in obj.get("strategies") or []]


def load_attempts(path: Path | None) -> list[AcquisitionAttempt]:
    out = []
    for row in read_tsv(path):
        if not row.get("stage_key"):
            continue
        out.append(
            AcquisitionAttempt(
                stage_key=row["stage_key"],
                accession=row.get("accession", ""),
                source_class=row.get("source_class", ""),
                status=row.get("status", ""),
                evidence_set_sha256=row.get("evidence_set_sha256", ""),
                policy_version=row.get("policy_version", ""),
                blocker_key=row.get("blocker_key", ""),
                strategy_version=row.get("strategy_version", ""),
            )
        )
    return out


def strategy_supports(strategy: SourceStrategy, family: str, fields: Iterable[str]) -> bool:
    normalized = {normalize_field_name(x) for x in fields if normalize_field_name(x)}
    if normalized and set(strategy.fields).intersection(normalized):
        return True
    return family in set(strategy.families)


def acquisition_stage_key(
    *,
    accession: str,
    source_class: str,
    evidence_set_sha256: str,
    policy_version: str,
    blocker_key: str,
    strategy_version: str,
) -> str:
    return canonical_sha256(
        {
            "accession": accession.upper(),
            "source_class": source_class,
            "evidence_set_sha256": evidence_set_sha256.lower(),
            "policy_version": policy_version,
            "blocker_key": blocker_key,
            "strategy_version": strategy_version,
        }
    )


def plan_acquisition_for_case(
    *,
    accession: str,
    blocker_family: str,
    blocker_fields: Iterable[str],
    blocker_key: str,
    evidence_set_sha256: str,
    policy_version: str,
    strategies: Iterable[SourceStrategy],
    attempts: Iterable[AcquisitionAttempt],
    strategy_version: str,
    max_source_classes: int,
) -> list[dict[str, Any]]:
    attempt_rows = list(attempts)
    attempts_by_key = {x.stage_key: x for x in attempt_rows}
    relevant_attempts = [
        x for x in attempt_rows
        if x.blocker_key == blocker_key and x.strategy_version == strategy_version
    ]
    attempted_classes = {x.source_class for x in relevant_attempts}
    if len(attempted_classes) >= max_source_classes:
        return []
    rows = []
    for strategy in sorted(strategies, key=lambda x: (x.priority, x.source_class)):
        if not strategy_supports(strategy, blocker_family, blocker_fields):
            continue
        source_state_exhausted = any(
            attempt.accession.upper() == accession.upper()
            and attempt.source_class == strategy.source_class
            and attempt.policy_version == policy_version
            and attempt.status in SOURCE_STATE_NO_PROGRESS_STATUSES
            for attempt in relevant_attempts
        )
        if source_state_exhausted:
            continue
        skey = acquisition_stage_key(
            accession=accession,
            source_class=strategy.source_class,
            evidence_set_sha256=evidence_set_sha256,
            policy_version=policy_version,
            blocker_key=blocker_key,
            strategy_version=strategy_version,
        )
        prior = attempts_by_key.get(skey)
        if prior and prior.status in NO_PROGRESS_STATUSES:
            continue
        rows.append(
            {
                "stage_key": skey,
                "accession": accession.upper(),
                "source_class": strategy.source_class,
                "priority": strategy.priority,
                "provider": strategy.provider,
                "acquisition_method": strategy.acquisition_method,
                "expected_trust_class": strategy.expected_trust_class,
                "requires_external_locator": str(strategy.requires_external_locator).lower(),
                "blocker_family": blocker_family,
                "blocker_fields": ";".join(sorted({normalize_field_name(x) for x in blocker_fields if normalize_field_name(x)})),
                "blocker_key": blocker_key,
                "evidence_set_sha256": evidence_set_sha256,
                "policy_version": policy_version,
                "strategy_version": strategy_version,
                "status": "pending",
            }
        )
        # Only the highest-priority unexhausted source class is scheduled in one
        # planning cycle. The accession is replanned after acquisition so the
        # harness can stop immediately if the blocker becomes source-closed.
        break
    return rows


def self_test() -> None:
    strategy = SourceStrategy(
        source_class="deposited_sdrf",
        priority=10,
        families=("single_cell_isolation",),
        fields=(),
        provider="PRIDE",
        acquisition_method="deposited_metadata",
        expected_trust_class="trusted_deposited",
    )
    kwargs = dict(
        accession="PXD900001",
        blocker_family="single_cell_isolation",
        blocker_fields=["characteristics[single cell isolation protocol]"],
        blocker_key="b" * 64,
        evidence_set_sha256="e" * 64,
        policy_version="p",
        strategies=[strategy],
        strategy_version="catalog-v1",
        max_source_classes=4,
    )
    rows = plan_acquisition_for_case(attempts=[], **kwargs)
    assert len(rows) == 1
    attempt = AcquisitionAttempt(
        stage_key=rows[0]["stage_key"],
        accession="PXD900001",
        source_class="deposited_sdrf",
        status="source_exhausted",
        evidence_set_sha256="e" * 64,
        policy_version="p",
        blocker_key="b" * 64,
        strategy_version="catalog-v1",
    )
    assert plan_acquisition_for_case(attempts=[attempt], **kwargs) == []

    community_only_attempt = AcquisitionAttempt(
        stage_key=rows[0]["stage_key"],
        accession="PXD900001",
        source_class="deposited_sdrf",
        status="found_untrusted_community_annotation",
        evidence_set_sha256="e" * 64,
        policy_version="p",
        blocker_key="b" * 64,
        strategy_version="catalog-v1",
    )
    assert (
        plan_acquisition_for_case(
            attempts=[community_only_attempt],
            **kwargs,
        )
        == []
    )

    # Ordinary content-addressed no-progress work can be retried after the
    # evidence identity changes.
    kwargs["evidence_set_sha256"] = "f" * 64
    assert len(plan_acquisition_for_case(attempts=[attempt], **kwargs)) == 1

    # A community-only result is different: it describes the queried source
    # class itself. Registering that untrusted source may change the evidence
    # hash, but must not immediately replay the same depositor-SDRF lookup.
    assert (
        plan_acquisition_for_case(
            attempts=[community_only_attempt],
            **kwargs,
        )
        == []
    )

    # A policy change deliberately reopens the source-state decision.
    kwargs["policy_version"] = "p2"
    assert len(plan_acquisition_for_case(attempts=[community_only_attempt], **kwargs)) == 1
    print("sdrf_evidence_acquisition_planner self-test: PASS")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        self_test()
        return 0
    raise SystemExit("This module is used by sdrf_annotation_harness.py; standalone execution currently supports --self-test only")


if __name__ == "__main__":
    raise SystemExit(main())
