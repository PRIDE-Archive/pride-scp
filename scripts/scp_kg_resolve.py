#!/usr/bin/env python3
"""Canonicalize and resolve provenance-bearing claims in the global SCP knowledge graph.

The resolver is intentionally accession-agnostic.  It converts raw source observations into
canonical claim signatures, collapses aliases/duplicate evidence, models source independence,
marks contradictions, and promotes only sufficiently supported claims to canonical accepted edges.

It does *not* generate SDRFs and it does not treat community repetition or runtime hypotheses as
independent truth.  High-risk sample/channel/file mappings require primary or structured evidence.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from scp_knowledge_graph import GraphStore, NodeRef, json_text, norm, stable_id

VERSION = "pride-scp-kg-resolver-v0.2.2"

# Trust weight is evidence quality, not a probability.  Contributions are capped per source family
# so dozens of pages from one community site cannot manufacture independent corroboration.
TRUST_WEIGHTS = {
    "repository_primary": 1.00,
    "publication_association": 0.95,
    "peer_reviewed": 0.92,
    "peer_reviewed_model_extraction": 0.78,
    "repository_model_extraction": 0.80,
    "primary_structured_evidence": 0.90,
    "derived_structured_evidence": 0.82,
    "analysis_repository": 0.72,
    "community_resource": 0.50,
    "derived_hypothesis": 0.30,
    "unclassified": 0.45,
}

SAFE_SOURCE_RELATION_PREDICATES = {
    "MENTIONS_ACCESSION",
    "MENTIONS_MASSIVE_ACCESSION",
    "MENTIONS_PUBLICATION",
    "LINKS_TO_RESOURCE",
    "HAS_PROJECT_WEBSITE",
    "DESCRIBES_TECHNOLOGY",
}

BIBLIOGRAPHIC_PREDICATES = {
    "HAS_PROJECT_TITLE",
    "HAS_REPOSITORY_DATE",
    "HAS_RAW_FILE",
    "HAS_PUBLICATION",
    "HAS_DOI",
    "HAS_TITLE",
}

MAPPING_PREDICATES = {
    "BELONGS_TO_BRANCH",
    "HAS_REPORTER_CHANNEL_EVIDENCE",
    "HAS_SAMPLE_TOKEN_EVIDENCE",
    "MAPS_TO_SAMPLE",
    "MAPS_TO_CELL",
    "MAPS_TO_CHANNEL",
}

RELATIONSHIP_TOKENS = (
    "REDEPOSIT",
    "PREDECESSOR",
    "SAME_STUDY",
    "RELATED_DEPOSITION",
    "SUPERSED",
    "DUPLICATE",
)

FUNCTIONAL_PREDICATES = {
    "HAS_PROJECT_TITLE",
    "HAS_REPOSITORY_DATE",
    "HAS_DOI",
    "HAS_TITLE",
    "HAS_MODALITY",
    "BELONGS_TO_BRANCH",
}

# Canonical modality families used only for contradiction diagnostics.  They do not infer modality.
LABEL_FREE_MODALITIES = {"label_free", "label_free_single_cell"}
REPORTER_MODALITIES = {
    "reporter_multiplexed_single_cell",
    "targeted_reporter",
    "targeted_reporter_single_cell",
}
UNRESOLVED_MODALITIES = {"modality_unresolved", "single_cell_modality_unresolved"}

# Positive semantic facts must carry informative values.  These tokens represent absence/unknown
# state rather than a biological or experimental fact and therefore remain in raw claim provenance
# only; the resolver will never promote them.
NONINFORMATIVE_SEMANTIC_VALUES = {
    "none", "na", "null", "unknown", "unspecified", "notspecified", "notreported",
    "unreported", "notavailable", "notapplicable", "missing", "undetermined",
}

# Common ontology namespace labels that can appear in repository JSON `cvLabel` fields.  They name
# the vocabulary, not the organism/tissue/cell/disease value.  This is a generic type check, not an
# accession-specific rule.
ONTOLOGY_NAMESPACE_SENTINELS = {
    "HAS_ORGANISM": {"NEWT", "NCBITAXON"},
    "HAS_ORGANISM_PART": {"BTO", "UBERON"},
    "HAS_CELL_TYPE": {"CL"},
    "HAS_DISEASE": {"DOID", "MONDO"},
}

REPORTER_ROLE_PREDICATES = {
    "HAS_ANALYTICAL_CHANNEL", "HAS_CARRIER_CHANNEL", "HAS_BLANK_CHANNEL", "HAS_REFERENCE_CHANNEL"
}


def _simple_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", html.unescape(norm(value)).lower())


def canonical_technology(value: str) -> tuple[str, str]:
    """Return canonical key + display label for reusable SCP technology/chemistry aliases."""
    raw = html.unescape(norm(value))
    k = _simple_key(raw)
    patterns = [
        (r"^tmtpro18(?:plex)?$", "TMTpro18"),
        (r"^tmtpro16(?:plex)?$", "TMTpro16"),
        (r"^tmtpro$", "TMTpro"),
        (r"^tmt11(?:plex)?$", "TMT11"),
        (r"^tmt10(?:plex)?$", "TMT10"),
        (r"^tmt8(?:plex)?$", "TMT8"),
        (r"^tmt6(?:plex)?$", "TMT6"),
        (r"^tmt$", "TMT"),
        (r"^plexdia$", "plexDIA"),
        (r"^scope2$", "SCoPE2"),
        (r"^scopems$", "SCoPE-MS"),
        (r"^pscope$", "pSCoPE"),
        (r"^npop$", "nPOP"),
        (r"^mpop$", "mPOP"),
        (r"^surequant$", "SureQuant"),
        (r"^cellenone$", "CellenONE"),
        (r"^dia$", "DIA"),
        (r"^dda$", "DDA"),
        (r"^prm$", "PRM"),
    ]
    for pattern, label in patterns:
        if re.match(pattern, k):
            return label.lower(), label
    # Preserve unknown technologies rather than guessing.
    return raw.lower(), raw


def canonical_reporter_channel(value: str) -> tuple[str, str]:
    raw = html.unescape(norm(value)).upper().replace("TMT", "")
    raw = re.sub(r"\s+", "", raw)
    m = re.fullmatch(r"(12[6-9]|13[0-5])([NC])?", raw)
    if m:
        label = m.group(1) + (m.group(2) or "")
        return label, label
    return raw, raw


def canonical_modality(value: str) -> tuple[str, str]:
    raw = norm(value)
    k = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    aliases = {
        "labelfree": "label_free",
        "label_free": "label_free",
        "label_free_single_cell": "label_free_single_cell",
        "reporter_multiplexed_single_cell": "reporter_multiplexed_single_cell",
        "targeted_reporter": "targeted_reporter",
        "targeted_reporter_single_cell": "targeted_reporter_single_cell",
        "single_cell_modality_unresolved": "single_cell_modality_unresolved",
        "modality_unresolved": "modality_unresolved",
        "comparator_non_primary_scp": "comparator_non_primary_scp",
        "targeted_method_development": "targeted_method_development",
    }
    key = aliases.get(k, k)
    return key, key


def canonical_acquisition(value: str) -> tuple[str, str]:
    raw = norm(value)
    k = _simple_key(raw)
    for name in ("DIA", "DDA", "PRM"):
        if k == name.lower():
            return name.lower(), name
    return raw.lower(), raw


def canonical_node(row: sqlite3.Row | dict[str, Any]) -> NodeRef:
    node_type = row["node_type"]
    key = norm(row["canonical_key"])
    label = norm(row["label"]) or key
    attrs = json.loads(row["attrs_json"] or "{}")
    if node_type in {"Technology", "ReporterChemistry"}:
        ckey, clabel = canonical_technology(label or key)
        return NodeRef("Technology", ckey, clabel, attrs)
    if node_type == "ReporterChannel":
        ckey, clabel = canonical_reporter_channel(label or key)
        return NodeRef("ReporterChannel", ckey, clabel, attrs)
    if node_type == "Modality":
        ckey, clabel = canonical_modality(label or key)
        return NodeRef("Modality", ckey, clabel, attrs)
    if node_type == "AcquisitionMethod":
        ckey, clabel = canonical_acquisition(label or key)
        return NodeRef("AcquisitionMethod", ckey, clabel, attrs)
    if node_type in {"Accession", "RepositoryAccession"}:
        return NodeRef(node_type, key.upper(), label.upper(), attrs)
    if node_type == "Publication" and key.lower().startswith("doi:"):
        doi = key[4:].strip().lower()
        return NodeRef("Publication", f"doi:{doi}", label, attrs)
    return NodeRef(node_type, key, label, attrs)


def canonical_predicate(predicate: str, attrs: dict[str, Any]) -> str:
    p = norm(predicate).upper()
    mapping = {
        "HAS_BRANCH_HYPOTHESIS": "HAS_BRANCH",
        "HAS_MODALITY_HYPOTHESIS": "HAS_MODALITY",
        "HAS_ACQUISITION_HYPOTHESIS": "HAS_ACQUISITION",
        "USES_CHEMISTRY_HYPOTHESIS": "USES_CHEMISTRY",
        "BELONGS_TO_BRANCH_HYPOTHESIS": "BELONGS_TO_BRANCH",
        "HAS_CHANNEL_EVIDENCE": "HAS_REPORTER_CHANNEL_EVIDENCE",
    }
    if p == "HAS_REPORTER_CHANNEL_HYPOTHESIS":
        role = norm(attrs.get("role")).lower()
        return {
            "analytical": "HAS_ANALYTICAL_CHANNEL",
            "carrier": "HAS_CARRIER_CHANNEL",
            "blank": "HAS_BLANK_CHANNEL",
            "reference": "HAS_REFERENCE_CHANNEL",
        }.get(role, "HAS_REPORTER_CHANNEL")
    return mapping.get(p, p)


def canonical_literal(value: str, datatype: str) -> str:
    value = html.unescape(norm(value))
    if datatype.lower() == "doi" or value.lower().startswith("10."):
        return value.lower().rstrip(".,;)")
    return value


def source_family(source: sqlite3.Row | dict[str, Any]) -> str:
    trust = norm(source["trust_class"]) or "unclassified"
    return trust


def source_lineage(source: sqlite3.Row | dict[str, Any]) -> str:
    metadata = json.loads(source["metadata_json"] or "{}")
    explicit = norm(metadata.get("lineage_uri") or metadata.get("parent_source_uri"))
    if explicit:
        low = explicit.lower()
        if low.startswith(("doi:", "pmid:", "repository:")):
            return low
        return f"explicit:{low}"
    uri = norm(source["uri"])
    trust = norm(source["trust_class"]) or "unclassified"
    # Publications are independent by DOI/source URI; repository evidence is independent per
    # accession/repository record; community pages share a domain family but retain per-page lineage.
    if uri.lower().startswith("doi:"):
        return f"doi:{uri[4:].lower()}"
    if trust == "repository_primary":
        return f"repository:{norm(source['scope_accession']).upper()}:{uri}"
    parsed = urlparse(uri if "://" in uri else "")
    if parsed.netloc:
        return f"{trust}:{parsed.netloc.lower()}:{parsed.path.rstrip('/').lower()}"
    return f"{trust}:{uri.lower()}"


def risk_class(predicate: str, subject_type: str) -> str:
    if predicate in SAFE_SOURCE_RELATION_PREDICATES and subject_type in {
        "CommunityResourcePage", "AnalysisRepository", "RepositoryResource", "ExternalResource"
    }:
        return "source_relation"
    if predicate in BIBLIOGRAPHIC_PREDICATES:
        return "bibliographic"
    if predicate in MAPPING_PREDICATES:
        return "mapping"
    if any(tok in predicate for tok in RELATIONSHIP_TOKENS):
        return "relationship"
    return "semantic"


def hypothesis_penalty(status: str) -> float:
    return 0.20 if status == "hypothesis" else 1.0


def trust_weight(trust_class: str) -> float:
    return TRUST_WEIGHTS.get(trust_class, TRUST_WEIGHTS["unclassified"])


def technology_relation(a: str, b: str) -> str:
    """Return equal/parent/child/incompatible/unknown for reporter technologies."""
    if a == b:
        return "equal"
    parent = {
        "tmtpro18": {"tmtpro", "tmt"},
        "tmtpro16": {"tmtpro", "tmt"},
        "tmtpro": {"tmt"},
        "tmt11": {"tmt"},
        "tmt10": {"tmt"},
        "tmt8": {"tmt"},
        "tmt6": {"tmt"},
    }
    if b in parent.get(a, set()):
        return "child"
    if a in parent.get(b, set()):
        return "parent"
    tmt_specific = {"tmtpro18","tmtpro16","tmt11","tmt10","tmt8","tmt6"}
    if a in tmt_specific and b in tmt_specific:
        return "incompatible"
    return "unknown"


@dataclass
class CanonicalClaim:
    claim_id: str
    subject: NodeRef
    subject_node_id: str
    predicate: str
    object_ref: NodeRef | None
    object_node_id: str | None
    literal: str
    datatype: str
    scope_accession: str
    branch_scope: str
    source_id: str
    source_family: str
    source_lineage: str
    trust_class: str
    confidence: float
    contribution: float
    raw_status: str
    attrs: dict[str, Any]
    subject_type: str

    @property
    def signature(self) -> tuple[str, str, str, str, str, str, str]:
        return (
            self.subject.node_type + "\x1f" + self.subject.canonical_key,
            self.predicate,
            (self.object_ref.node_type + "\x1f" + self.object_ref.canonical_key) if self.object_ref else "",
            self.literal,
            self.datatype,
            self.scope_accession,
            self.branch_scope,
        )


@dataclass
class ResolutionGroup:
    signature: tuple[str, str, str, str, str, str, str]
    claims: list[CanonicalClaim] = field(default_factory=list)
    risk: str = "semantic"
    status: str = "unresolved"
    support_score: float = 0.0
    independent_lineages: int = 0
    independent_families: int = 0
    edge_id: str | None = None
    conflict_key: str = ""
    method: str = ""
    attrs: dict[str, Any] = field(default_factory=dict)



def _semantic_object_value(claim: CanonicalClaim) -> str:
    if claim.object_ref is not None:
        return norm(claim.object_ref.label or claim.object_ref.canonical_key)
    return norm(claim.literal)


def _semantic_hygiene_reason(claim: CanonicalClaim) -> str:
    value = _semantic_object_value(claim)
    if not value:
        return "empty_semantic_value"
    if _simple_key(value) in NONINFORMATIVE_SEMANTIC_VALUES:
        return "noninformative_semantic_value"
    sentinels = ONTOLOGY_NAMESPACE_SENTINELS.get(claim.predicate, set())
    if value.upper() in sentinels:
        return "ontology_namespace_not_entity_value"
    return ""


def _is_reporter_chemistry(value: str) -> bool:
    key, _ = canonical_technology(value)
    return key.startswith("tmt")


def _modality_family(value: str) -> str:
    key, _ = canonical_modality(value)
    if key in LABEL_FREE_MODALITIES or key == "label_free":
        return "label_free"
    if key in REPORTER_MODALITIES or "reporter" in key:
        return "reporter"
    if key in UNRESOLVED_MODALITIES:
        return "unresolved"
    return key


def _chemistry_compatible(a: str, b: str) -> bool:
    ka, _ = canonical_technology(a)
    kb, _ = canonical_technology(b)
    if ka == kb:
        return True
    # Generic TMT/TMTpro observations are compatible with a more specific member of the same family.
    if ka == "tmt" and kb.startswith("tmt"):
        return True
    if kb == "tmt" and ka.startswith("tmt"):
        return True
    if ka == "tmtpro" and kb.startswith("tmtpro"):
        return True
    if kb == "tmtpro" and ka.startswith("tmtpro"):
        return True
    return False


def _feature_json(feature: dict[str, Any]) -> dict[str, Any]:
    return {
        "modalities": sorted(feature["modalities"]),
        "chemistries": sorted(feature["chemistries"]),
        "acquisitions": sorted(feature["acquisitions"]),
        "reporter_roles": {k: sorted(v) for k, v in sorted(feature["roles"].items())},
    }


class Resolver:
    def __init__(self, store: GraphStore):
        self.store = store
        self.conn = store.conn
        self.node_rows = {r["node_id"]: r for r in self.conn.execute("SELECT * FROM node")}
        self.source_rows = {r["source_id"]: r for r in self.conn.execute("SELECT * FROM source")}
        self.node_ref_cache: dict[str, NodeRef] = {}

    def _canonical_ref(self, node_id: str) -> NodeRef:
        if node_id not in self.node_ref_cache:
            row = self.node_rows[node_id]
            ref = canonical_node(row)
            self.node_ref_cache[node_id] = ref
            canonical_id = self.store.node(ref)
            if canonical_id != node_id:
                self.conn.execute(
                    """INSERT OR REPLACE INTO node_canonicalization(node_id,canonical_node_id,canonicalization_method,confidence,attrs_json)
                       VALUES(?,?,?,?,?)""",
                    (node_id, canonical_id, "generic_alias_normalization", 1.0,
                     json_text({"original_type": row["node_type"], "original_key": row["canonical_key"]})),
                )
        return self.node_ref_cache[node_id]

    def canonical_claims(self) -> list[CanonicalClaim]:
        out: list[CanonicalClaim] = []
        for row in self.conn.execute("SELECT * FROM claim ORDER BY claim_id"):
            attrs = json.loads(row["attrs_json"] or "{}")
            subj = self._canonical_ref(row["subject_id"])
            obj = self._canonical_ref(row["object_id"]) if row["object_id"] else None
            src = self.source_rows[row["source_id"]]
            pred = canonical_predicate(row["predicate"], attrs)
            conf = max(0.0, min(1.0, float(row["confidence"])))
            contribution = conf * trust_weight(norm(src["trust_class"])) * hypothesis_penalty(row["status"])
            out.append(CanonicalClaim(
                claim_id=row["claim_id"], subject=subj, subject_node_id=row["subject_id"], predicate=pred,
                object_ref=obj, object_node_id=row["object_id"], literal=canonical_literal(row["literal_value"], row["literal_datatype"]),
                datatype=row["literal_datatype"], scope_accession=row["scope_accession"], branch_scope=row["branch_scope"],
                source_id=row["source_id"], source_family=source_family(src), source_lineage=source_lineage(src),
                trust_class=norm(src["trust_class"]) or "unclassified", confidence=conf, contribution=contribution,
                raw_status=row["status"], attrs=attrs, subject_type=subj.node_type,
            ))
        return out

    def _existing_edge(self, claim: CanonicalClaim) -> str | None:
        sid = self.store.node(claim.subject)
        oid = self.store.node(claim.object_ref) if claim.object_ref else None
        row = self.conn.execute(
            """SELECT edge_id FROM edge WHERE subject_id=? AND predicate=? AND COALESCE(object_id,'')=COALESCE(?, '')
               AND literal_value=? AND literal_datatype=? AND scope_accession=? AND branch_scope=? AND status='accepted' LIMIT 1""",
            (sid, claim.predicate, oid, claim.literal, claim.datatype, claim.scope_accession, claim.branch_scope),
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _support(group: ResolutionGroup) -> None:
        # Multiple claims from the same source lineage collapse to their maximum contribution.
        by_lineage: dict[str, CanonicalClaim] = {}
        for c in group.claims:
            old = by_lineage.get(c.source_lineage)
            if old is None or c.contribution > old.contribution:
                by_lineage[c.source_lineage] = c
        # Evidence families have bounded total influence.  Community/runtime repetition is tightly
        # capped, while genuinely independent peer-reviewed or structured source lineages can add
        # corroboration without growing without bound.
        family_caps = {
            "community_resource": 0.50,
            "derived_hypothesis": 0.10,
            "publication_association": 0.95,
            "analysis_repository": 0.90,
            "peer_reviewed": 1.80,
            "peer_reviewed_model_extraction": 1.25,
            "repository_model_extraction": 1.20,
            "primary_structured_evidence": 1.80,
            "derived_structured_evidence": 1.60,
            "repository_primary": 1.20,
            "unclassified": 0.60,
        }
        by_family_values: dict[str, list[float]] = defaultdict(list)
        for c in by_lineage.values():
            by_family_values[c.source_family].append(c.contribution)
        by_family: dict[str, float] = {}
        for family, values in by_family_values.items():
            by_family[family] = min(family_caps.get(family, 0.75), sum(sorted(values, reverse=True)[:3]))
        group.support_score = sum(by_family.values())
        group.independent_lineages = len(by_lineage)
        group.independent_families = len(by_family)
        group.attrs.update({
            "source_family_contributions": dict(sorted(by_family.items())),
            "source_lineages": sorted(by_lineage),
            "raw_claim_count": len(group.claims),
        })

    def _base_decision(self, group: ResolutionGroup) -> None:
        self._support(group)
        first = group.claims[0]
        group.risk = risk_class(first.predicate, first.subject_type)
        existing = self._existing_edge(first)
        if existing:
            group.status = "accepted_existing"
            group.edge_id = existing
            group.method = "existing_canonical_edge"
            return

        asserted = [c for c in group.claims if c.raw_status != "hypothesis"]
        strong_families = {
            "repository_primary", "peer_reviewed", "primary_structured_evidence", "derived_structured_evidence",
            "peer_reviewed_model_extraction", "repository_model_extraction",
        }
        families = {c.source_family for c in asserted}
        max_asserted = max((c.contribution for c in asserted), default=0.0)

        if not asserted:
            group.status = "hypothesis_only"
            group.method = "no_non_hypothesis_support"
            return

        if group.risk == "source_relation":
            # This edge says *the source page/repository mentions or links something*; it does not
            # assert that the mentioned object is true for an accession's SDRF.
            if max_asserted >= 0.35:
                group.status = "accepted_resolved"
                group.method = "source_relation_observation"
            else:
                group.status = "source_asserted"
                group.method = "weak_source_relation"
            return

        if group.risk == "bibliographic":
            if any(c.source_family == "repository_primary" and c.contribution >= 0.90 for c in asserted):
                group.status = "accepted_resolved"; group.method = "primary_repository_evidence"; return
            if any(c.source_family in {"publication_association", "peer_reviewed"} and c.contribution >= 0.84 for c in asserted):
                group.status = "accepted_resolved"; group.method = "publication_identity_evidence"; return

        if group.risk == "semantic":
            # One explicit peer-reviewed or primary source can close a method-level semantic fact;
            # community/resource repetition alone cannot.
            if any(c.source_family in {"repository_primary", "peer_reviewed", "primary_structured_evidence"} and c.contribution >= 0.82 for c in asserted):
                group.status = "accepted_resolved"; group.method = "explicit_primary_semantic_evidence"; return
            if group.support_score >= 1.35 and len(families & strong_families) >= 2:
                group.status = "accepted_resolved"; group.method = "independent_semantic_corroboration"; return

        if group.risk == "mapping":
            # High-risk SDRF mappings require an exact structured/primary source.  Runtime hypotheses,
            # community pages and publication prose cannot independently close RAW->cell/channel joins.
            exact_structured = False
            for c in asserted:
                if c.source_family not in {"primary_structured_evidence", "derived_structured_evidence", "repository_primary"}:
                    continue
                method = norm(c.attrs.get("join_method")).lower()
                if c.contribution >= 0.78 and ("exact" in method or "source" in method or c.source_family == "primary_structured_evidence"):
                    exact_structured = True
            if exact_structured:
                group.status = "accepted_resolved"; group.method = "exact_structured_mapping_evidence"; return
            mapping_strong = families & {"primary_structured_evidence","derived_structured_evidence","repository_primary","peer_reviewed"}
            if group.support_score >= 1.55 and len(mapping_strong) >= 2:
                group.status = "corroborated_not_accepted"; group.method = "mapping_requires_source_closure"; return

        if group.risk == "relationship":
            if group.support_score >= 1.65 and len(families & {"repository_primary","peer_reviewed","primary_structured_evidence"}) >= 2:
                group.status = "corroborated_not_accepted"; group.method = "relationship_requires_explicit_review"; return

        if group.independent_families >= 2 and group.support_score >= 1.0:
            group.status = "corroborated_not_accepted"
            group.method = "corroborated_below_acceptance_gate"
        else:
            group.status = "source_asserted"
            group.method = "single_lineage_or_low_authority"

    def groups(self, claims: Iterable[CanonicalClaim]) -> list[ResolutionGroup]:
        grouped: dict[tuple[str, str, str, str, str, str, str], ResolutionGroup] = {}
        for c in claims:
            g = grouped.setdefault(c.signature, ResolutionGroup(c.signature))
            g.claims.append(c)
        groups = list(grouped.values())
        for g in groups:
            self._base_decision(g)
        self._apply_semantic_hygiene(groups)
        self._apply_functional_conflicts(groups)
        self._apply_semantic_consistency_conflicts(groups)
        return groups

    def _apply_semantic_hygiene(self, groups: list[ResolutionGroup]) -> None:
        for g in groups:
            if g.risk != "semantic" or g.status == "accepted_existing":
                continue
            reason = _semantic_hygiene_reason(g.claims[0])
            if not reason:
                continue
            g.status = "rejected_semantic_hygiene"
            g.method = reason
            g.attrs["semantic_hygiene_reason"] = reason
            g.attrs["semantic_hygiene_value"] = _semantic_object_value(g.claims[0])

    def _apply_semantic_consistency_conflicts(self, groups: list[ResolutionGroup]) -> None:
        # Reporter role exclusivity: within one canonical source-local context/branch, a reporter
        # channel cannot simultaneously be analytical, carrier, blank and/or reference.  Do not pick
        # a winner from weak model/hypothesis evidence; expose the contradiction instead.
        by_channel: dict[tuple[str, str, str, str], list[ResolutionGroup]] = defaultdict(list)
        for g in groups:
            first = g.claims[0]
            if first.predicate not in REPORTER_ROLE_PREDICATES or first.object_ref is None:
                continue
            if g.status == "rejected_semantic_hygiene":
                continue
            key = (g.signature[0], first.scope_accession, first.branch_scope, first.object_ref.canonical_key)
            by_channel[key].append(g)
        for key, variants in by_channel.items():
            roles = {g.claims[0].predicate for g in variants}
            if len(roles) <= 1:
                continue
            conflict_id = stable_id("semantic_conflict", "reporter_role_exclusivity", *key)
            strong_existing = [g for g in variants if g.status == "accepted_existing"]
            for g in variants:
                g.conflict_key = conflict_id
                g.attrs["semantic_conflict"] = "reporter_role_exclusivity"
                g.attrs["conflicting_roles"] = sorted(roles)
                if g.status == "accepted_existing":
                    continue
                if len(strong_existing) == 1:
                    g.status = "rejected_by_stronger_evidence"
                    g.method = "reporter_role_conflict_with_existing_truth"
                else:
                    g.status = "conflicted"
                    g.method = "reporter_role_exclusivity_conflict"

        # A source-local context cannot coherently be label-free while also carrying reporter
        # chemistry or reporter-role facts.  This is a diagnostic consistency rule, not modality
        # inference; it never manufactures a replacement modality.
        by_context: dict[tuple[str, str, str], list[ResolutionGroup]] = defaultdict(list)
        for g in groups:
            if g.status == "rejected_semantic_hygiene":
                continue
            first = g.claims[0]
            if g.risk != "semantic":
                continue
            by_context[(g.signature[0], first.scope_accession, first.branch_scope)].append(g)
        for key, variants in by_context.items():
            label_free = [g for g in variants if g.claims[0].predicate == "HAS_MODALITY" and g.claims[0].object_ref and _modality_family(g.claims[0].object_ref.canonical_key) == "label_free"]
            reporter = []
            for g in variants:
                c = g.claims[0]
                if c.predicate in REPORTER_ROLE_PREDICATES:
                    reporter.append(g)
                elif c.predicate == "HAS_MODALITY" and c.object_ref and _modality_family(c.object_ref.canonical_key) == "reporter":
                    reporter.append(g)
                elif c.predicate == "USES_CHEMISTRY" and c.object_ref and _is_reporter_chemistry(c.object_ref.canonical_key):
                    reporter.append(g)
            if not label_free or not reporter:
                continue
            involved = []
            seen_group_ids = set()
            for candidate in label_free + reporter:
                marker = id(candidate)
                if marker not in seen_group_ids:
                    seen_group_ids.add(marker)
                    involved.append(candidate)
            conflict_id = stable_id("semantic_conflict", "label_free_reporter_context", *key)
            for g in involved:
                g.conflict_key = conflict_id
                g.attrs["semantic_conflict"] = "label_free_with_reporter_evidence"
                if g.status == "accepted_existing":
                    continue
                g.status = "conflicted"
                g.method = "label_free_reporter_context_conflict"

    def _apply_functional_conflicts(self, groups: list[ResolutionGroup]) -> None:
        by_key: dict[tuple[str,str,str,str], list[ResolutionGroup]] = defaultdict(list)
        for g in groups:
            first = g.claims[0]
            if first.predicate not in FUNCTIONAL_PREDICATES:
                continue
            key = (g.signature[0], first.predicate, first.scope_accession, first.branch_scope)
            by_key[key].append(g)
        for key, variants in by_key.items():
            if len(variants) <= 1:
                continue
            # BELONGS_TO_BRANCH hypotheses with multiple branch targets are mapping conflicts unless a
            # primary accepted edge is already present.  Semantic modality conflicts are similarly
            # retained instead of choosing a winner from weak derived evidence.
            accepted = [g for g in variants if g.status in {"accepted_existing","accepted_resolved"}]
            strong_existing = [g for g in accepted if g.status == "accepted_existing"]
            conflict_id = stable_id("conflict", *key)
            if len(accepted) == 1:
                winner = accepted[0]
                for g in variants:
                    g.conflict_key = conflict_id
                    if g is winner:
                        continue
                    if g.status != "accepted_existing":
                        g.status = "rejected_by_stronger_evidence"
                        g.method = "functional_predicate_single_accepted_winner"
                continue
            # If all variants are weak/hypothetical, or more than one independently reaches the
            # acceptance gate, expose the contradiction rather than choosing by score.
            if len(accepted) != 1:
                for g in variants:
                    g.conflict_key = conflict_id
                    if g.status not in {"accepted_existing"}:
                        g.status = "conflicted"
                        g.method = "functional_predicate_conflict"

    def _promote(self, group: ResolutionGroup) -> None:
        if group.status != "accepted_resolved":
            return
        first = group.claims[0]
        conf = min(0.999, max(c.confidence for c in group.claims if c.raw_status != "hypothesis") if any(c.raw_status != "hypothesis" for c in group.claims) else 0.5)
        eid = self.store.resolved_edge(
            [c.claim_id for c in group.claims], subject=first.subject, predicate=first.predicate,
            object_ref=first.object_ref, literal_value=first.literal, literal_datatype=first.datatype,
            scope_accession=first.scope_accession, branch_scope=first.branch_scope,
            confidence=conf, resolution_method=f"kg_resolver:{group.method}",
            attrs={"resolver_version":VERSION,"support_score":group.support_score,"independent_families":group.independent_families,"risk_class":group.risk},
        )
        group.edge_id = eid

    def persist_groups(self, groups: list[ResolutionGroup]) -> None:
        for g in groups:
            self._promote(g)
            first = g.claims[0]
            sid = self.store.node(first.subject)
            oid = self.store.node(first.object_ref) if first.object_ref else None
            rid = stable_id("resolution", *g.signature)
            self.conn.execute(
                """INSERT INTO resolution_group(
                   resolution_id,canonical_subject_id,canonical_predicate,canonical_object_id,canonical_literal_value,canonical_literal_datatype,
                   scope_accession,branch_scope,risk_class,status,support_score,independent_lineages,independent_families,claim_count,
                   winning_edge_id,conflict_key,resolution_method,attrs_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid,sid,first.predicate,oid,first.literal,first.datatype,first.scope_accession,first.branch_scope,g.risk,g.status,g.support_score,
                 g.independent_lineages,g.independent_families,len(g.claims),g.edge_id,g.conflict_key,g.method,json_text(g.attrs)),
            )
            for c in g.claims:
                self.conn.execute(
                    """INSERT INTO resolution_claim(resolution_id,claim_id,source_lineage,source_family,normalized_confidence,contribution)
                       VALUES(?,?,?,?,?,?)""",
                    (rid,c.claim_id,c.source_lineage,c.source_family,c.confidence,c.contribution),
                )

    def resolve_branches(self, claims: list[CanonicalClaim]) -> dict[str, Any]:
        # Branch resolution is intentionally diagnostic at this stage.  Runtime hypotheses are not
        # promoted to accepted branch facts until primary/publication/structured claims corroborate them.
        branch_nodes = {
            r["node_id"]: r for r in self.conn.execute("SELECT * FROM node WHERE node_type='ExperimentalBranchHypothesis'")
        }
        feature: dict[str, dict[str, Any]] = {
            nid: {"modalities":set(),"chemistries":set(),"acquisitions":set(),"roles":defaultdict(set),"files":set(),"accession":""}
            for nid in branch_nodes
        }
        for c in claims:
            raw_subj = c.subject_node_id
            raw_obj = c.object_node_id
            if raw_subj in feature:
                f=feature[raw_subj]
                f["accession"] = c.scope_accession or f["accession"]
                if c.predicate=="HAS_MODALITY" and c.object_ref: f["modalities"].add(c.object_ref.canonical_key)
                if c.predicate=="USES_CHEMISTRY" and c.object_ref: f["chemistries"].add(c.object_ref.canonical_key)
                if c.predicate=="HAS_ACQUISITION" and c.object_ref: f["acquisitions"].add(c.object_ref.canonical_key)
                if c.predicate in {"HAS_ANALYTICAL_CHANNEL","HAS_CARRIER_CHANNEL","HAS_BLANK_CHANNEL","HAS_REFERENCE_CHANNEL"} and c.object_ref:
                    f["roles"][c.predicate].add(c.object_ref.canonical_key)
            if c.predicate=="BELONGS_TO_BRANCH" and raw_obj in feature:
                feature[raw_obj]["accession"] = c.scope_accession or feature[raw_obj]["accession"]
                feature[raw_obj]["files"].add(c.subject.canonical_key)

        contradictory: set[str] = set()
        diagnostics: dict[str, list[str]] = defaultdict(list)
        for nid,f in feature.items():
            mods=set(f["modalities"]); chems=set(f["chemistries"])
            if mods & LABEL_FREE_MODALITIES and any(x.startswith("tmt") for x in chems):
                contradictory.add(nid); diagnostics[nid].append("label_free_modality_with_reporter_chemistry")
            resolved_mods={m for m in mods if m not in UNRESOLVED_MODALITIES}
            if len(resolved_mods & LABEL_FREE_MODALITIES) and len(resolved_mods & REPORTER_MODALITIES):
                contradictory.add(nid); diagnostics[nid].append("mutually_exclusive_modality_hypotheses")
            specific=[x for x in chems if x in {"tmtpro18","tmtpro16","tmt11","tmt10","tmt8","tmt6"}]
            if len(specific)>1:
                incompatible=False
                for i,a in enumerate(specific):
                    for b in specific[i+1:]:
                        if technology_relation(a,b)=="incompatible": incompatible=True
                if incompatible:
                    contradictory.add(nid); diagnostics[nid].append("incompatible_reporter_chemistries")

        # Conservative union-find: merge only coherent hypotheses with same accession and compatible
        # modality/chemistry plus strong feature or file agreement.
        parent={nid:nid for nid in branch_nodes}
        def find(x):
            while parent[x]!=x:
                parent[x]=parent[parent[x]]; x=parent[x]
            return x
        def union(a,b):
            ra,rb=find(a),find(b)
            if ra!=rb: parent[rb]=ra
        def modality_family(mods:set[str])->set[str]:
            out=set()
            for m in mods:
                if m in LABEL_FREE_MODALITIES: out.add("label_free")
                elif m in REPORTER_MODALITIES: out.add("reporter")
                elif m in UNRESOLVED_MODALITIES: out.add("unresolved")
                else: out.add(m)
            return out
        def chemistry_compatible(a:set[str],b:set[str])->bool:
            if not a or not b: return True
            for x in a:
                for y in b:
                    if technology_relation(x,y)=="incompatible": return False
            return True
        nids=list(branch_nodes)
        for i,a in enumerate(nids):
            if a in contradictory: continue
            fa=feature[a]
            for b in nids[i+1:]:
                if b in contradictory: continue
                fb=feature[b]
                if not fa["accession"] or fa["accession"]!=fb["accession"]: continue
                ma,mb=modality_family(fa["modalities"]),modality_family(fb["modalities"])
                if ("label_free" in ma and "reporter" in mb) or ("reporter" in ma and "label_free" in mb): continue
                if not chemistry_compatible(fa["chemistries"],fb["chemistries"]): continue
                score=0.0
                if (ma & mb) - {"unresolved"}: score += 0.35
                elif "unresolved" in ma or "unresolved" in mb: score += 0.15
                if fa["chemistries"] and fb["chemistries"]: score += 0.30
                elif fa["chemistries"] or fb["chemistries"]: score += 0.10
                if fa["acquisitions"] and fb["acquisitions"]:
                    inter=len(fa["acquisitions"] & fb["acquisitions"]); union_n=len(fa["acquisitions"] | fb["acquisitions"])
                    score += 0.15*(inter/union_n if union_n else 0)
                elif not fa["acquisitions"] or not fb["acquisitions"]: score += 0.05
                if fa["files"] and fb["files"]:
                    inter=len(fa["files"] & fb["files"]); union_n=len(fa["files"] | fb["files"])
                    score += 0.20*(inter/union_n if union_n else 0)
                if score >= 0.72:
                    union(a,b)

        clusters: dict[str,list[str]]=defaultdict(list)
        for nid in nids:
            clusters[find(nid)].append(nid)
        counts=defaultdict(int)
        for root,members in clusters.items():
            acc=next((feature[x]["accession"] for x in members if feature[x]["accession"]),"")
            if any(x in contradictory for x in members):
                status="contradictory_hypothesis"; blocker=";".join(sorted({d for x in members for d in diagnostics[x]})); conf=0.0
            else:
                mods=set().union(*(feature[x]["modalities"] for x in members))
                chems=set().union(*(feature[x]["chemistries"] for x in members))
                acqs=set().union(*(feature[x]["acquisitions"] for x in members))
                resolved_mods=mods-UNRESOLVED_MODALITIES
                if not resolved_mods and not chems:
                    status="unresolved_hypothesis"; blocker="no coherent modality/chemistry evidence"; conf=0.25
                else:
                    status="canonical_branch_candidate"; blocker="requires source-grounded corroboration before promotion"; conf=min(0.75,0.45+0.05*(len(members)-1))
            mods=set().union(*(feature[x]["modalities"] for x in members))
            chems=set().union(*(feature[x]["chemistries"] for x in members))
            acqs=set().union(*(feature[x]["acquisitions"] for x in members))
            # Prefer the most specific compatible reporter chemistry for display.
            specific_order=["tmtpro18","tmtpro16","tmt11","tmt10","tmt8","tmt6","tmtpro","tmt"]
            chemistry=next((x for x in specific_order if x in chems), sorted(chems)[0] if chems else "")
            resolved_mods=sorted(mods-UNRESOLVED_MODALITIES)
            modality="|".join(resolved_mods or sorted(mods))
            acquisition="|".join(sorted(acqs))
            member_keys=sorted(branch_nodes[x]["canonical_key"] for x in members)
            canonical_key=stable_id("branch",acc,"|".join(member_keys))
            brid=stable_id("branch_resolution",acc,canonical_key)
            attrs={
                "member_branch_keys":member_keys,
                "member_count":len(members),
                "file_hypotheses":sorted(set().union(*(feature[x]["files"] for x in members))),
                "reporter_roles":{role:sorted(set().union(*(feature[x]["roles"].get(role,set()) for x in members))) for role in ("HAS_ANALYTICAL_CHANNEL","HAS_CARRIER_CHANNEL","HAS_BLANK_CHANNEL","HAS_REFERENCE_CHANNEL")},
                "diagnostics":sorted({d for x in members for d in diagnostics[x]}),
            }
            self.conn.execute(
                """INSERT INTO branch_resolution(branch_resolution_id,scope_accession,canonical_branch_key,status,modality,chemistry,acquisition,confidence,blocker,attrs_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (brid,acc,canonical_key,status,modality,chemistry,acquisition,conf,blocker,json_text(attrs)),
            )
            for x in members:
                relation="contradictory_member" if x in contradictory else ("merged_member" if len(members)>1 else "single_member")
                self.conn.execute(
                    "INSERT INTO branch_resolution_member(branch_resolution_id,branch_node_id,relation,similarity) VALUES(?,?,?,?)",
                    (brid,x,relation,1.0 if len(members)==1 else 0.8),
                )
            counts[status]+=1
        return {"branch_clusters":len(clusters),"status_counts":dict(sorted(counts.items()))}

    def resolve_context_branch_alignment(self, claims: list[CanonicalClaim]) -> dict[str, Any]:
        """Diagnose compatibility between source-local semantic contexts and branch hypotheses.

        This layer is deliberately non-generative and non-promoting.  It never asserts that a
        context *is* a branch; it only records whether independently extracted semantic features are
        compatible with, ambiguous among, or contradictory to existing branch hypotheses.
        """
        context_nodes = {
            r["node_id"]: r for r in self.conn.execute("SELECT * FROM node WHERE node_type='ExperimentalContext'")
        }
        branch_nodes = {
            r["node_id"]: r for r in self.conn.execute("SELECT * FROM node WHERE node_type='ExperimentalBranchHypothesis'")
        }
        def blank() -> dict[str, Any]:
            return {"modalities": set(), "chemistries": set(), "acquisitions": set(), "roles": defaultdict(set), "accession": "", "lineages": set()}
        contexts = {nid: blank() for nid in context_nodes}
        branches = {nid: blank() for nid in branch_nodes}
        for c in claims:
            for target, feature in ((contexts, contexts.get(c.subject_node_id)), (branches, branches.get(c.subject_node_id))):
                if feature is None:
                    continue
                feature["accession"] = c.scope_accession or feature["accession"]
                feature["lineages"].add(c.source_lineage)
                if c.predicate == "HAS_MODALITY" and c.object_ref:
                    feature["modalities"].add(c.object_ref.canonical_key)
                elif c.predicate == "USES_CHEMISTRY" and c.object_ref:
                    feature["chemistries"].add(c.object_ref.canonical_key)
                elif c.predicate == "HAS_ACQUISITION" and c.object_ref:
                    feature["acquisitions"].add(c.object_ref.canonical_key)
                elif c.predicate in REPORTER_ROLE_PREDICATES and c.object_ref:
                    feature["roles"][c.predicate].add(c.object_ref.canonical_key)

        candidate_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for context_id, cf in contexts.items():
            if not (cf["modalities"] or cf["chemistries"] or cf["acquisitions"] or cf["roles"]):
                continue
            acc = cf["accession"]
            if not acc:
                continue
            for branch_id, bf in branches.items():
                if bf["accession"] != acc:
                    continue
                matched: list[str] = []
                conflicts: list[str] = []
                score = 0.0
                conflict_score = 0.0

                cm = {_modality_family(x) for x in cf["modalities"] if _modality_family(x) != "unresolved"}
                bm = {_modality_family(x) for x in bf["modalities"] if _modality_family(x) != "unresolved"}
                if cm and bm:
                    inter = cm & bm
                    if inter:
                        score += 0.35
                        matched.append("modality:" + ",".join(sorted(inter)))
                    elif {"label_free", "reporter"}.issubset(cm | bm):
                        conflict_score += 1.0
                        conflicts.append("label_free_vs_reporter_modality")

                cc = set(cf["chemistries"])
                bc = set(bf["chemistries"])
                if cc and bc:
                    compat = {(a, b) for a in cc for b in bc if _chemistry_compatible(a, b)}
                    if compat:
                        score += 0.35
                        matched.append("chemistry:" + ",".join(sorted({a for a, _ in compat} | {b for _, b in compat})))
                    elif any(_is_reporter_chemistry(x) for x in cc) and any(_is_reporter_chemistry(x) for x in bc):
                        conflict_score += 0.60
                        conflicts.append("incompatible_reporter_chemistry")

                ca = set(cf["acquisitions"])
                ba = set(bf["acquisitions"])
                if ca and ba:
                    inter = ca & ba
                    if inter:
                        score += 0.15
                        matched.append("acquisition:" + ",".join(sorted(inter)))
                    else:
                        conflict_score += 0.15
                        conflicts.append("different_acquisition")

                role_matches = 0
                role_conflicts = 0
                for role in REPORTER_ROLE_PREDICATES:
                    overlap = set(cf["roles"].get(role, set())) & set(bf["roles"].get(role, set()))
                    role_matches += len(overlap)
                    if overlap:
                        matched.append(role + ":" + ",".join(sorted(overlap)))
                crole_by_channel: dict[str, set[str]] = defaultdict(set)
                brole_by_channel: dict[str, set[str]] = defaultdict(set)
                for role in REPORTER_ROLE_PREDICATES:
                    for ch in cf["roles"].get(role, set()): crole_by_channel[ch].add(role)
                    for ch in bf["roles"].get(role, set()): brole_by_channel[ch].add(role)
                for ch in set(crole_by_channel) & set(brole_by_channel):
                    if crole_by_channel[ch].isdisjoint(brole_by_channel[ch]):
                        role_conflicts += 1
                        conflicts.append("reporter_role:" + ch)
                if role_matches:
                    score += min(0.15, 0.05 * role_matches)
                if role_conflicts:
                    conflict_score += min(0.60, 0.30 * role_conflicts)

                row = {
                    "context_id": context_id,
                    "branch_id": branch_id,
                    "accession": acc,
                    "score": round(score, 4),
                    "conflict_score": round(conflict_score, 4),
                    "matched": matched,
                    "conflicts": conflicts,
                    "context": cf,
                    "branch": bf,
                }
                candidate_rows[context_id].append(row)

        status_counts: dict[str, int] = defaultdict(int)
        inserted = 0
        for context_id, rows in candidate_rows.items():
            viable = [r for r in rows if r["score"] >= 0.55 and r["conflict_score"] < 0.50]
            for r in rows:
                if r["conflict_score"] >= 0.50:
                    status = "contradictory"
                elif r in viable:
                    status = "unique_compatible_candidate" if len(viable) == 1 else "ambiguous_compatible_candidate"
                elif r["score"] >= 0.30:
                    status = "weak_candidate"
                else:
                    status = "insufficient_evidence"
                aid = stable_id("context_branch_alignment", r["accession"], r["context_id"], r["branch_id"])
                attrs = {
                    "matched": r["matched"],
                    "conflicts": r["conflicts"],
                    "context_features": _feature_json(r["context"]),
                    "branch_features": _feature_json(r["branch"]),
                    "non_promoting": True,
                }
                self.conn.execute(
                    """INSERT INTO context_branch_alignment(
                       alignment_id,scope_accession,context_node_id,branch_node_id,status,compatibility_score,
                       conflict_score,matched_features,conflicting_features,context_source_lineages,evidence_summary,attrs_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (aid, r["accession"], r["context_id"], r["branch_id"], status, r["score"], r["conflict_score"],
                     len(r["matched"]), len(r["conflicts"]), len(r["context"]["lineages"]),
                     ";".join(r["matched"] + r["conflicts"]), json_text(attrs)),
                )
                inserted += 1
                status_counts[status] += 1
        return {"alignments": inserted, "status_counts": dict(sorted(status_counts.items()))}

    def run(self) -> dict[str, Any]:
        self.store.clear_resolution()
        claims=self.canonical_claims()
        groups=self.groups(claims)
        self.persist_groups(groups)
        branch_summary=self.resolve_branches(claims)
        context_alignment_summary=self.resolve_context_branch_alignment(claims)
        self.conn.commit()
        status_counts={r[0]:r[1] for r in self.conn.execute("SELECT status,COUNT(*) FROM resolution_group GROUP BY status ORDER BY status")}
        risk_counts={r[0]:r[1] for r in self.conn.execute("SELECT risk_class,COUNT(*) FROM resolution_group GROUP BY risk_class ORDER BY risk_class")}
        accepted_by_risk={r[0]:r[1] for r in self.conn.execute(
            "SELECT risk_class,COUNT(*) FROM resolution_group WHERE status='accepted_resolved' GROUP BY risk_class ORDER BY risk_class"
        )}
        return {
            "resolver_version":VERSION,
            "claims_read":len(claims),
            "canonical_groups":len(groups),
            "node_aliases":self.conn.execute("SELECT COUNT(*) FROM node_canonicalization").fetchone()[0],
            "resolution_status_counts":status_counts,
            "risk_class_counts":risk_counts,
            "accepted_resolved_by_risk":accepted_by_risk,
            "resolver_promoted_edges":self.conn.execute("SELECT COUNT(*) FROM edge WHERE resolution_method LIKE 'kg_resolver:%'").fetchone()[0],
            "resolver_promoted_accession_scoped_edges":self.conn.execute("SELECT COUNT(*) FROM edge WHERE resolution_method LIKE 'kg_resolver:%' AND scope_accession<>''").fetchone()[0],
            "resolver_promoted_mapping_edges":accepted_by_risk.get("mapping",0),
            "branch_resolution":branch_summary,
            "semantic_hygiene":{
                "rejected_groups":sum(1 for g in groups if g.status=="rejected_semantic_hygiene"),
                "conflicted_groups":sum(1 for g in groups if g.status=="conflicted" and g.method in {"reporter_role_exclusivity_conflict","label_free_reporter_context_conflict"}),
                "method_counts":dict(sorted(Counter(g.method for g in groups if g.method in {"noninformative_semantic_value","ontology_namespace_not_entity_value","reporter_role_exclusivity_conflict","label_free_reporter_context_conflict","reporter_role_conflict_with_existing_truth"}).items())),
            },
            "context_branch_alignment":context_alignment_summary,
            "policy":{
                "community_repetition_capped_per_source_family":True,
                "hypothesis_claims_cannot_self_promote":True,
                "mapping_requires_primary_or_structured_source_closure":True,
                "cross_accession_relationships_remain_review_gated":True,
                "noninformative_semantic_values_never_promote":True,
                "reporter_role_conflicts_remain_review_gated":True,
                "context_branch_alignment_is_non_promoting":True,
            },
        }


def export_resolution_report(store: GraphStore, output: Path, summary: dict[str, Any]) -> None:
    output.mkdir(parents=True,exist_ok=True)
    (output/"kg_resolution_summary.json").write_text(json.dumps(summary,indent=2)+"\n")
    # Human-review queues are explicit and generic.
    queries={
        "conflicts.tsv":"SELECT * FROM resolution_group WHERE status='conflicted' ORDER BY scope_accession,canonical_predicate,resolution_id",
        "corroborated_not_accepted.tsv":"SELECT * FROM resolution_group WHERE status='corroborated_not_accepted' ORDER BY scope_accession,canonical_predicate,resolution_id",
        "hypothesis_only.tsv":"SELECT * FROM resolution_group WHERE status='hypothesis_only' ORDER BY scope_accession,canonical_predicate,resolution_id",
        "semantic_hygiene.tsv":"SELECT * FROM resolution_group WHERE status='rejected_semantic_hygiene' OR resolution_method IN ('reporter_role_exclusivity_conflict','label_free_reporter_context_conflict','reporter_role_conflict_with_existing_truth') ORDER BY scope_accession,branch_scope,canonical_predicate,resolution_id",
        "branch_resolution.tsv":"SELECT * FROM branch_resolution ORDER BY scope_accession,canonical_branch_key",
        "context_branch_alignment.tsv":"SELECT * FROM context_branch_alignment ORDER BY scope_accession,context_node_id,compatibility_score DESC,branch_node_id",
    }
    import csv
    for name,q in queries.items():
        rows=[dict(r) for r in store.conn.execute(q)]
        path=output/name
        if rows:
            with path.open("w",newline="",encoding="utf-8") as fh:
                w=csv.DictWriter(fh,fieldnames=list(rows[0]),delimiter="\t"); w.writeheader(); w.writerows(rows)
        else:
            path.write_text("")


def self_test() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root=Path(td); db=root/"kg.sqlite"
        with GraphStore(db) as g:
            # Primary repository truth.
            repo=g.source("pride_project_metadata","fixture:repo",trust_class="repository_primary",scope_accession="PXD900101")
            acc=NodeRef("Accession","PXD900101","PXD900101")
            raw=NodeRef("RawFile","PXD900101::run1.raw","run1.raw",{"accession":"PXD900101"})
            c=g.claim(acc,"HAS_RAW_FILE",repo,object_ref=raw,scope_accession="PXD900101",extractor="fixture",confidence=1.0,evidence_locator="inventory",evidence_text="run1.raw")
            g.promote_edge([c],confidence=1.0,resolution_method="repository_primary")

            # Technology aliases from independent sources should normalize to one fact, but community
            # repetition alone should count only once per family.
            pub=g.source("publication","doi:10.0000/test",trust_class="peer_reviewed",scope_accession="PXD900101")
            comm1=g.source("scp_slavovlab_web","https://scp.example/a",trust_class="community_resource")
            comm2=g.source("scp_slavovlab_web","https://scp.example/b",trust_class="community_resource")
            branch=NodeRef("ExperimentalBranch","PXD900101::b1","b1")
            g.claim(branch,"USES_CHEMISTRY",pub,object_ref=NodeRef("Technology","TMT pro 18-plex","TMT pro 18-plex"),scope_accession="PXD900101",branch_scope="b1",extractor="llm",confidence=0.96,evidence_locator="Methods",evidence_text="TMT pro 18-plex")
            g.claim(branch,"USES_CHEMISTRY",comm1,object_ref=NodeRef("ReporterChemistry","TMTpro18","TMTpro18"),scope_accession="PXD900101",branch_scope="b1",extractor="community",confidence=0.9,evidence_locator="page",evidence_text="TMTpro18")
            g.claim(branch,"USES_CHEMISTRY",comm2,object_ref=NodeRef("Technology","TMTpro 18 plex","TMTpro 18 plex"),scope_accession="PXD900101",branch_scope="b1",extractor="community",confidence=0.9,evidence_locator="page",evidence_text="TMTpro18")
            page=NodeRef("CommunityResourcePage","https://scp.example/a","A")
            g.claim(page,"MENTIONS_ACCESSION",comm1,object_ref=acc,extractor="community",confidence=0.9,evidence_locator="page",evidence_text="PXD fixture")

            # A single model interpretation of a publication must not self-promote, but matching
            # model interpretations from independent publication + repository source lineages may
            # corroborate a semantic fact.
            model_pub=g.source("llm_publication_semantic","doi:10.0000/model",trust_class="peer_reviewed_model_extraction",scope_accession="PXD900101",metadata={"lineage_uri":"doi:10.0000/model"})
            model_repo=g.source("llm_pride_metadata_semantic","pride-snapshot:PXD900101",trust_class="repository_model_extraction",scope_accession="PXD900101",metadata={"lineage_uri":"repository:PXD900101:pride-snapshot:PXD900101"})
            tech=NodeRef("Technology","TMTpro18","TMTpro18")
            g.claim(acc,"USES_CHEMISTRY",model_pub,object_ref=tech,scope_accession="PXD900101",extractor="small_llm:fixture",confidence=.98,evidence_locator="Methods",evidence_text="TMTpro18")
            g.claim(acc,"USES_CHEMISTRY",model_repo,object_ref=tech,scope_accession="PXD900101",extractor="small_llm:fixture",confidence=.98,evidence_locator="description",evidence_text="TMTpro18")

            # Semantic hygiene: source-local model contexts retain provenance but placeholder/CV-label
            # values and internally contradictory reporter roles must never become accepted truth.
            ctx=NodeRef("ExperimentalContext","PXD900101::ctx:reporter","reporter context",{"accession":"PXD900101"})
            g.claim(ctx,"HAS_MODALITY",model_pub,object_ref=NodeRef("Modality","reporter_multiplexed_single_cell","reporter_multiplexed_single_cell"),scope_accession="PXD900101",branch_scope="PXD900101::ctx:reporter",extractor="small_llm:fixture",confidence=.98,evidence_locator="Methods",evidence_text="single-cell TMTpro18")
            g.claim(ctx,"USES_CHEMISTRY",model_pub,object_ref=NodeRef("ReporterChemistry","TMTpro18","TMTpro18"),scope_accession="PXD900101",branch_scope="PXD900101::ctx:reporter",extractor="small_llm:fixture",confidence=.98,evidence_locator="Methods",evidence_text="TMTpro18")
            g.claim(ctx,"HAS_CARRIER_CHANNEL",model_pub,object_ref=NodeRef("ReporterChannel","126","126"),scope_accession="PXD900101",branch_scope="PXD900101::ctx:reporter",extractor="small_llm:fixture",confidence=.98,evidence_locator="Methods",evidence_text="126 carrier")
            g.claim(ctx,"HAS_ANALYTICAL_CHANNEL",model_pub,object_ref=NodeRef("ReporterChannel","126","126"),scope_accession="PXD900101",branch_scope="PXD900101::ctx:reporter",extractor="small_llm:fixture",confidence=.90,evidence_locator="Methods",evidence_text="126")
            badctx=NodeRef("ExperimentalContext","PXD900101::ctx:metadata","metadata context",{"accession":"PXD900101"})
            g.claim(badctx,"HAS_ORGANISM",model_repo,object_ref=NodeRef("Organism","NEWT","NEWT"),scope_accession="PXD900101",branch_scope="PXD900101::ctx:metadata",extractor="small_llm:fixture",confidence=1.0,evidence_locator="project.organisms[0].cvLabel",evidence_text="NEWT")
            g.claim(badctx,"HAS_DISEASE",model_repo,object_ref=NodeRef("Disease","not_specified","not_specified"),scope_accession="PXD900101",branch_scope="PXD900101::ctx:metadata",extractor="small_llm:fixture",confidence=1.0,evidence_locator="description",evidence_text="not specified")

            # Contradictory branch hypotheses must remain diagnostic, never canonical truth.
            hyp=g.source("runtime_inference","fixture:v053",trust_class="derived_hypothesis")
            bh=NodeRef("ExperimentalBranchHypothesis","PXD900101::h1","h1",{"accession":"PXD900101"})
            g.claim(acc,"HAS_BRANCH_HYPOTHESIS",hyp,object_ref=bh,scope_accession="PXD900101",branch_scope="h1",extractor="fixture",confidence=.55,status="hypothesis",evidence_locator="row1",evidence_text="h1")
            g.claim(bh,"HAS_MODALITY_HYPOTHESIS",hyp,object_ref=NodeRef("Modality","label_free","label_free"),scope_accession="PXD900101",branch_scope="h1",extractor="fixture",confidence=.55,status="hypothesis",evidence_locator="row1",evidence_text="label free")
            g.claim(bh,"USES_CHEMISTRY_HYPOTHESIS",hyp,object_ref=NodeRef("ReporterChemistry","TMT6plex","TMT6plex"),scope_accession="PXD900101",branch_scope="h1",extractor="fixture",confidence=.55,status="hypothesis",evidence_locator="row1",evidence_text="TMT6")
            bh2=NodeRef("ExperimentalBranchHypothesis","PXD900101::h2","h2",{"accession":"PXD900101"})
            g.claim(acc,"HAS_BRANCH_HYPOTHESIS",hyp,object_ref=bh2,scope_accession="PXD900101",branch_scope="h2",extractor="fixture",confidence=.55,status="hypothesis",evidence_locator="row2",evidence_text="h2")
            g.claim(bh2,"HAS_MODALITY_HYPOTHESIS",hyp,object_ref=NodeRef("Modality","reporter_multiplexed_single_cell","reporter_multiplexed_single_cell"),scope_accession="PXD900101",branch_scope="h2",extractor="fixture",confidence=.55,status="hypothesis",evidence_locator="row2",evidence_text="reporter multiplexed single cell")
            g.claim(bh2,"USES_CHEMISTRY_HYPOTHESIS",hyp,object_ref=NodeRef("ReporterChemistry","TMTpro18","TMTpro18"),scope_accession="PXD900101",branch_scope="h2",extractor="fixture",confidence=.55,status="hypothesis",evidence_locator="row2",evidence_text="TMTpro18")
            g.claim(bh2,"HAS_CARRIER_CHANNEL",hyp,object_ref=NodeRef("ReporterChannel","126","126"),scope_accession="PXD900101",branch_scope="h2",extractor="fixture",confidence=.55,status="hypothesis",evidence_locator="row2",evidence_text="126 carrier")

            r=Resolver(g); summary=r.run()
            assert summary["node_aliases"] >= 2
            assert g.conn.execute("SELECT COUNT(*) FROM resolution_group WHERE canonical_predicate='USES_CHEMISTRY' AND status='accepted_resolved'").fetchone()[0] >= 1
            # Existing repository edge survives.
            assert g.conn.execute("SELECT COUNT(*) FROM edge WHERE predicate='HAS_RAW_FILE' AND status='accepted'").fetchone()[0]==1
            # Alias-normalized semantic fact should resolve from peer-reviewed + community evidence.
            assert g.conn.execute("SELECT COUNT(*) FROM edge WHERE predicate='USES_CHEMISTRY' AND status='accepted' AND branch_scope='b1'").fetchone()[0]==1
            # Community family is capped: two pages are not two independent families.
            rg=g.conn.execute("SELECT independent_families,support_score FROM resolution_group WHERE canonical_predicate='USES_CHEMISTRY' AND branch_scope='b1'").fetchone()
            assert rg[0]==2 and rg[1] < 1.5
            assert g.conn.execute("SELECT COUNT(*) FROM edge WHERE predicate='MENTIONS_ACCESSION' AND status='accepted'").fetchone()[0]==1
            br=g.conn.execute("SELECT status FROM branch_resolution WHERE scope_accession='PXD900101'").fetchall()
            assert any(x[0]=="contradictory_hypothesis" for x in br)
            # Namespace/placeholder values are retained only as rejected resolution groups.
            hygiene=dict(g.conn.execute("SELECT resolution_method,COUNT(*) FROM resolution_group WHERE status='rejected_semantic_hygiene' GROUP BY resolution_method"))
            assert hygiene.get("ontology_namespace_not_entity_value",0) >= 1
            assert hygiene.get("noninformative_semantic_value",0) >= 1
            # Same reporter channel in multiple roles is exposed as a semantic conflict.
            assert g.conn.execute("SELECT COUNT(*) FROM resolution_group WHERE resolution_method='reporter_role_exclusivity_conflict'").fetchone()[0] >= 2
            # The reporter context should uniquely match the compatible reporter/TMTpro18 branch and
            # contradict the label-free/TMT6 branch.  These alignments are diagnostic only.
            aligns=dict(g.conn.execute("SELECT status,COUNT(*) FROM context_branch_alignment GROUP BY status"))
            assert aligns.get("unique_compatible_candidate",0) >= 1
            assert aligns.get("contradictory",0) >= 1
            assert summary["context_branch_alignment"]["alignments"] >= 2
            report_dir=root/"resolution"
            export_resolution_report(g, report_dir, summary)
            assert (report_dir/"semantic_hygiene.tsv").is_file()
            assert (report_dir/"context_branch_alignment.tsv").is_file()
            assert (report_dir/"semantic_hygiene.tsv").read_text().strip()
            assert (report_dir/"context_branch_alignment.tsv").read_text().strip()

            # Regression: a resolver rerun must be idempotent/foreign-key safe even when
            # resolution_group.winning_edge_id points at edges created by the previous resolver run.
            first_counts = (
                g.conn.execute("SELECT COUNT(*) FROM resolution_group").fetchone()[0],
                g.conn.execute("SELECT COUNT(*) FROM edge WHERE resolution_method LIKE 'kg_resolver:%'").fetchone()[0],
            )
            summary2=Resolver(g).run()
            second_counts = (
                g.conn.execute("SELECT COUNT(*) FROM resolution_group").fetchone()[0],
                g.conn.execute("SELECT COUNT(*) FROM edge WHERE resolution_method LIKE 'kg_resolver:%'").fetchone()[0],
            )
            assert first_counts == second_counts
            assert summary2["resolver_promoted_mapping_edges"] == 0
    print("scp_kg_resolve self-test: PASS")


def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--db",type=Path)
    p.add_argument("--output",type=Path)
    p.add_argument("--self-test",action="store_true")
    args=p.parse_args()
    if args.self_test:
        self_test(); return 0
    if not args.db or not args.output:
        raise SystemExit("--db and --output are required")
    with GraphStore(args.db) as store:
        store.add_run("scp_kg_resolve",VERSION)
        summary=Resolver(store).run()
        export_resolution_report(store,args.output,summary)
        print(json.dumps(summary,indent=2))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
