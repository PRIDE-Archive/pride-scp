#!/usr/bin/env python3
"""Evidence-first audit for unresolved multiplex/channel mappings in PRIDE-SCP SDRF drafts.

This helper is intentionally diagnostic. It does not write SDRF rows and does not use GT
metadata. It inventories explicit reporter-channel roles from the same public evidence used by
the SDRF reconstruction pipeline plus any normalized publication text materialized upstream.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator

AUDITOR_VERSION = "pride-scp-sdrf-mapping-auditor-v0.2"

# TMTpro order is a superset useful for expanding explicit textual ranges. TMT6/10 tokens
# remain valid members of this sequence; we never infer a chemistry-specific suffix that was
# not present in evidence.
CHANNEL_ORDER = [
    "126", "127N", "127C", "128N", "128C", "129N", "129C", "130N", "130C",
    "131N", "131C", "132N", "132C", "133N", "133C", "134N", "134C", "135N",
]
CHANNEL_INDEX = {x: i for i, x in enumerate(CHANNEL_ORDER)}

CHANNEL_TOKEN_RE = re.compile(
    r"(?i)(?:\bTMT(?:pro)?\s*[-_]?\s*)?(12[6-9]|13[0-5])\s*([NC])?\b"
)
RANGE_RE = re.compile(
    r"(?i)(?:\bTMT(?:pro)?\s*[-_]?\s*)?(12[6-9]|13[0-5])\s*([NC])?\s*"
    r"(?:through|thru|to|[-–—])\s*"
    r"(?:\bTMT(?:pro)?\s*[-_]?\s*)?(12[6-9]|13[0-5])\s*([NC])?\b"
)

ROLE_PATTERNS = {
    "carrier": re.compile(r"(?i)\bcarrier(?:\s+(?:proteome|sample|channel|cells?))?\b|\bboost(?:ing)?\s+channel\b"),
    "reference": re.compile(r"(?i)\b(?:reference|bridge)(?:\s+(?:proteome|sample|channel))?\b|\bnormalization\s+channel\b"),
    "blank": re.compile(r"(?i)\b(?:blank|empty|unused|not\s+used|left\s+empty|control\s+wells?)\b"),
    "single_cell": re.compile(
        r"(?i)\b(?:single[- ]cells?(?:\s+(?:samples?|proteomes?|channels?|wells?))?|"
        r"analytical\s+channel|single[- ]cell\s+signal|single[- ]cell\s+channels?)\b"
    ),
}

CHEMISTRY_PATTERNS = [
    ("TMTpro", re.compile(r"(?i)\btmt\s*pro\b|\btmtpro\b")),
    ("TMT", re.compile(r"(?i)\btmt(?:6|10|11|16|18)?\s*(?:plex)?\b|\btandem\s+mass\s+tags?\b")),
    ("iTRAQ", re.compile(r"(?i)\bitraq\b")),
    ("plexDIA", re.compile(r"(?i)\bplexdia\b")),
]

SINGLE_BRANCH_RE = re.compile(
    r"(?i)\b(?:single[- ]cell|single[- ]cells|single[- ]neuron|single[- ]neurons|"
    r"single[- ]oocyte|single[- ]oocytes|single[- ]zygote|single[- ]zygotes|"
    r"single[- ]blastomere|single[- ]blastomeres|single[- ](?:muscle\s+)?fib(?:er|re)s?)\b"
)
ISOBARIC_RE = re.compile(
    r"(?i)\b(?:tmt(?:pro)?|tandem\s+mass\s+tag|itraq|carrier\s+(?:channel|proteome|sample)|"
    r"reference\s+channel|reporter\s+channel|isobaric)\b"
)
NONISOBARIC_RE = re.compile(
    r"(?i)\b(?:label[- ]free|data[- ]independent|dia(?:-pasef)?|ce[- ]ms(?:/ms)?|"
    r"capillary\s+electrophoresis|maldi(?:-tof)?|top[- ]down|direct\s+injection)\b"
)

ROLE_WORD = {
    "carrier": r"carrier(?:\s+(?:proteome|sample|channel|cells?))?|boost(?:ing)?\s+channel",
    "reference": r"(?:reference|bridge)(?:\s+(?:proteome|sample|channel))?|normalization\s+channel",
    "single_cell": r"(?:single[- ]cells?(?:\s+(?:samples?|proteomes?|channels?|wells?))?|analytical\s+channel|single[- ]cell\s+channel)",
    "blank": r"(?:blank|empty|unused|not\s+used|left\s+empty|control\s+wells?)",
}
ROLE_ALT = "|".join(f"(?P<{k}>{v})" for k, v in ROLE_WORD.items())
ROLE_RE = re.compile(rf"(?i)\b(?:{ROLE_ALT})\b")
RESPECTIVE_ROLE_RE = re.compile(r"(?i)\b(analytical|carrier|reference|bridge|blank|single[- ]cell)\b")
RESPECTIVE_ROLE_MAP = {"analytical":"single_cell","carrier":"carrier","reference":"reference","bridge":"reference","blank":"blank","single-cell":"single_cell","single cell":"single_cell"}

def normalize_role_match(m: re.Match[str]) -> str | None:
    for role in ROLE_WORD:
        if m.groupdict().get(role):
            return role
    return None

def _channel_pattern(name: str) -> str:
    return rf"(?P<{name}_n>12[6-9]|13[0-5])\s*(?P<{name}_s>[NC])?"

def explicit_respectively_hits(source_kind: str, source_label: str, source_ref: str, text: str) -> list[ContextHit]:
    """Recover unambiguous two-role/two-channel assignments joined by ``respectively``."""
    hits: list[ContextHit] = []
    sentence_re = re.compile(r"[^.;\n]{0,500}\brespectively\b[^.;\n]{0,120}[.;]?", re.I)
    for sm in sentence_re.finditer(text):
        sentence = sm.group(0)
        channel_matches = list(CHANNEL_TOKEN_RE.finditer(sentence))
        role_matches = list(RESPECTIVE_ROLE_RE.finditer(sentence))
        if len(channel_matches) != 2 or len(role_matches) < 2:
            continue
        roles: list[tuple[int, str]] = []
        for rm in role_matches:
            raw = re.sub(r"\s+", " ", rm.group(1).lower())
            role = RESPECTIVE_ROLE_MAP.get(raw)
            if role is not None and (not roles or roles[-1][1] != role):
                roles.append((rm.start(), role))
        if len(roles) < 2:
            continue
        cpos = [m.start() for m in channel_matches]
        rpos = [x[0] for x in roles[:2]]
        if not (max(cpos) < min(rpos) or max(rpos) < min(cpos)):
            continue
        channels = [_channel(m.group(1), m.group(2)) for m in channel_matches]
        ctx = context_window(text, sm.start(), sm.end(), 220)
        for ch, (_, role) in zip(channels, roles[:2]):
            hits.append(ContextHit(source_kind, source_label, source_ref, [ch], [role], ctx))
    return hits

def branch_context_counts(texts: Iterable[str]) -> tuple[int, int, int]:
    linked_iso = 0
    linked_noniso = 0
    dataset_iso = 0
    for text in texts:
        if not text:
            continue
        # Sentence-ish segmentation is intentionally permissive for PDF-extracted text.
        chunks = re.split(r"(?<=[.;])\s+|\n+", text)
        for i, chunk in enumerate(chunks):
            if not chunk.strip():
                continue
            neighborhood = " ".join(chunks[max(0, i-1): min(len(chunks), i+2)])
            has_single = bool(SINGLE_BRANCH_RE.search(neighborhood))
            has_iso = bool(ISOBARIC_RE.search(neighborhood))
            has_noniso = bool(NONISOBARIC_RE.search(neighborhood))
            if has_iso and has_single:
                linked_iso += 1
            elif has_iso:
                dataset_iso += 1
            if has_noniso and has_single:
                linked_noniso += 1
    return linked_iso, linked_noniso, dataset_iso

def relation_recheck(chemistry: str, roles: dict[str, set[str]], ambiguous: set[str], texts: list[str]) -> tuple[str, str, int, int, int]:
    linked_iso, linked_noniso, dataset_iso = branch_context_counts(texts)
    explicit_roles = any(rr & {"carrier", "reference", "single_cell"} for rr in roles.values())
    if explicit_roles or linked_iso > 0:
        return "multiplex_supported", "high" if explicit_roles else "medium", linked_iso, linked_noniso, dataset_iso
    if linked_noniso > 0 and (chemistry or dataset_iso > 0):
        return "substudy_scope_recheck", "high", linked_iso, linked_noniso, dataset_iso
    if linked_noniso > 0:
        return "nonisobaric_single_cell_candidate", "high", linked_iso, linked_noniso, dataset_iso
    if not chemistry and not roles:
        return "relation_source_recheck", "low", linked_iso, linked_noniso, dataset_iso
    return "multiplex_unlinked_dataset_evidence", "low", linked_iso, linked_noniso, dataset_iso


@dataclass
class ContextHit:
    source_kind: str
    source_label: str
    source_ref: str
    channels: list[str]
    roles: list[str]
    text: str


@dataclass
class AccessionAudit:
    accession: str
    evidence_path: str
    raw_file_count: int
    manuscript_source_count: int
    publication_text_rows: int
    chemistry: str
    carrier_channels: list[str]
    reference_channels: list[str]
    single_cell_channels: list[str]
    blank_channels: list[str]
    ambiguous_channels: list[str]
    observed_channels: list[str]
    mapping_class: str
    confidence: str
    candidate_generation_mode: str
    context_hits: int
    relation_recheck: str
    relation_confidence: str
    single_cell_isobaric_contexts: int
    single_cell_nonisobaric_contexts: int
    dataset_only_isobaric_contexts: int
    notes: str


def _channel(number: str, suffix: str | None) -> str:
    return f"{number}{(suffix or '').upper()}"


def extract_channels(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for m in CHANNEL_TOKEN_RE.finditer(text):
        if any(lo <= m.start() < hi for lo, hi in respectively_spans):
            continue
        ch = _channel(m.group(1), m.group(2))
        if ch not in seen:
            out.append(ch)
            seen.add(ch)
    return out


def expand_ranges(text: str) -> list[tuple[list[str], tuple[int, int]]]:
    out: list[tuple[list[str], tuple[int, int]]] = []
    for m in RANGE_RE.finditer(text):
        a = _channel(m.group(1), m.group(2))
        b = _channel(m.group(3), m.group(4))
        if a not in CHANNEL_INDEX or b not in CHANNEL_INDEX:
            continue
        ia, ib = CHANNEL_INDEX[a], CHANNEL_INDEX[b]
        if ia <= ib and ib - ia <= 17:
            out.append((CHANNEL_ORDER[ia : ib + 1], m.span()))
    return out


def detect_chemistry(texts: Iterable[str], scaffold_hint: str = "") -> str:
    joined = "\n".join(texts)
    detected = ""
    for name, pat in CHEMISTRY_PATTERNS:
        if pat.search(joined):
            detected = name
            break
    hint = scaffold_hint.strip()
    # Prefer the more specific TMTpro evidence over a generic older TMT scaffold hint.
    if detected == "TMTpro" and hint == "TMT":
        return detected
    return hint or detected


def context_window(text: str, start: int, end: int, flank: int = 260) -> str:
    lo = max(0, start - flank)
    hi = min(len(text), end + flank)
    return re.sub(r"\s+", " ", text[lo:hi]).strip()



def local_clause(text: str, start: int, end: int, max_flank: int = 260) -> tuple[str, int]:
    left_candidates = [text.rfind(sep, max(0, start - max_flank), start) for sep in [".", ";", "\n"]]
    left = max(left_candidates) + 1
    right_candidates = [text.find(sep, end, min(len(text), end + max_flank)) for sep in [".", ";", "\n"]]
    right_real = [x for x in right_candidates if x >= 0]
    right = min(right_real) if right_real else min(len(text), end + max_flank)
    return text[left:right], left

def role_set(text: str) -> set[str]:
    return {name for name, pat in ROLE_PATTERNS.items() if pat.search(text)}


def nearest_role(text: str, start: int, end: int, radius: int = 150) -> str | None:
    center = (start + end) / 2
    candidates: list[tuple[float, int, str]] = []
    priority = {"carrier": 0, "reference": 1, "blank": 2, "single_cell": 3}
    lo, hi = max(0, start - radius), min(len(text), end + radius)
    for role, pat in ROLE_PATTERNS.items():
        for m in pat.finditer(text, lo, hi):
            distance = abs(((m.start() + m.end()) / 2) - center)
            candidates.append((distance, priority[role], role))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]


def classify_role_for_context(text: str) -> str | None:
    roles = role_set(text)
    for role in ("carrier", "reference", "blank", "single_cell"):
        if role in roles:
            return role
    return None


def collect_hits(source_kind: str, source_label: str, source_ref: str, text: str) -> list[ContextHit]:
    hits: list[ContextHit] = []
    hits.extend(explicit_respectively_hits(source_kind, source_label, source_ref, text))
    respectively_spans = [m.span() for m in re.finditer(r"[^.;\n]{0,500}\brespectively\b[^.;\n]{0,120}[.;]?", text, re.I)]
    channel_context_re = re.compile(r"(?i)\b(?:tmt(?:pro)?|reporter|channel|label(?:ed|led|ing)?)\b")
    # First retain explicit ranges so the audit can distinguish a resolved set of channels
    # from an ambiguous "single-cell and control wells" range.
    for channels, span in expand_ranges(text):
        clause, _ = local_clause(text, span[0], span[1], 360)
        if not channel_context_re.search(clause):
            continue
        ctx = context_window(text, span[0], span[1], 320)
        roles = sorted(role_set(clause))
        hits.append(ContextHit(source_kind, source_label, source_ref, channels, roles, ctx))

    # Then retain individual channel mentions. De-duplicate later at the accession level.
    for m in CHANNEL_TOKEN_RE.finditer(text):
        if any(lo <= m.start() < hi for lo, hi in respectively_spans):
            continue
        ch = _channel(m.group(1), m.group(2))
        ctx = context_window(text, m.start(), m.end(), 220)
        clause, offset = local_clause(text, m.start(), m.end(), 260)
        # Bare 126-135 numbers are common in papers as citation/page/protein counts. Only
        # treat them as reporter channels when the local clause explicitly talks about a
        # TMT/reporter/channel/label context. Prefixed tokens are intrinsically safe.
        token = m.group(0).lower()
        if not token.startswith("tmt") and not channel_context_re.search(clause):
            continue
        nearest = nearest_role(clause, m.start() - offset, m.end() - offset, radius=180)
        roles = [nearest] if nearest else []
        hits.append(ContextHit(source_kind, source_label, source_ref, [ch], roles, ctx))
    return hits


def iter_manifest_text(manifest: Path, wanted: set[str]) -> Iterator[tuple[str, str, str]]:
    if not manifest.is_file():
        return
    with manifest.open(errors="replace") as fh:
        rows = csv.DictReader(fh, delimiter="\t")
        for row in rows:
            acc = (row.get("accession") or "").strip()
            if acc not in wanted:
                continue
            p = (row.get("publication_content_text_path") or "").strip()
            if not p:
                continue
            path = Path(p)
            if not path.is_file():
                continue
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            label = (row.get("publication_doi") or row.get("doi") or path.name).strip()
            yield acc, label, text


def find_evidence_path(accession: str, evidence_dirs: list[Path]) -> Path | None:
    candidates = [
        f"{accession}.evidence.json",
        f"{accession}.sdrf.evidence.json",
    ]
    for root in evidence_dirs:
        for name in candidates:
            p = root / name
            if p.is_file():
                return p
    return None


def channel_roles_from_hits(hits: list[ContextHit]) -> tuple[dict[str, set[str]], set[str]]:
    roles: dict[str, set[str]] = defaultdict(set)
    ambiguous: set[str] = set()
    for hit in hits:
        role_names = set(hit.roles)
        # A range described as "single-cell and control wells" is structurally useful but
        # does not prove which channels are cells versus blanks/controls.
        if len(hit.channels) > 1 and "single_cell" in role_names and "blank" in role_names:
            ambiguous.update(hit.channels)
            continue
        primary = hit.roles[0] if len(hit.roles) == 1 else classify_role_for_context(hit.text)
        if primary is None:
            continue
        if len(hit.channels) > 1 and primary == "single_cell":
            # Explicit "single cells were labeled with A through B" is a usable global
            # single-cell channel range unless the same context also says controls/blank.
            roles_to_apply = {"single_cell"}
        else:
            roles_to_apply = {primary}
        for ch in hit.channels:
            roles[ch].update(roles_to_apply)
    # Contradictory role assignments stay explicit instead of being silently resolved.
    for ch, rr in roles.items():
        if len(rr) > 1:
            ambiguous.add(ch)
    return roles, ambiguous


def mapping_classification(
    chemistry: str,
    roles: dict[str, set[str]],
    ambiguous: set[str],
    hits: list[ContextHit],
) -> tuple[str, str, str, str]:
    carrier = sorted(ch for ch, rr in roles.items() if rr == {"carrier"})
    reference = sorted(ch for ch, rr in roles.items() if rr == {"reference"})
    single = sorted(ch for ch, rr in roles.items() if rr == {"single_cell"})
    blank = sorted(ch for ch, rr in roles.items() if rr == {"blank"})

    # Strong special case: one analytical/single-cell reporter channel plus carrier. This
    # supports one biological row per RAW while retaining multiplex carrier metadata.
    analytical_phrase = any(
        len(h.channels) == 1
        and "single_cell" in h.roles
        and re.search(r"(?i)\banalytical\s+channel\b", h.text)
        for h in hits
    )
    if len(single) == 1 and carrier and not ambiguous and analytical_phrase:
        return (
            "single_analytical_channel_per_run",
            "high",
            "generated_one_single_cell_channel_per_raw_with_carrier",
            "one explicit analytical/single-cell reporter channel and carrier channel are directly evidenced",
        )
    if len(single) >= 2 and carrier and not ambiguous:
        return (
            "explicit_global_multiplex_layout_candidate",
            "medium",
            "candidate_per_channel_rows_requires_run_scope_confirmation",
            "multiple single-cell reporter channels plus carrier are explicit; verify that the layout applies uniformly to each RAW before row generation",
        )
    if ambiguous and carrier:
        return (
            "ambiguous_single_or_control_channel_layout",
            "medium",
            "manual_or_structured_mapping_required",
            "reporter range and carrier are explicit but single-cell versus control/blank roles are not fully assigned",
        )
    if carrier and reference:
        return (
            "carrier_reference_roles_only",
            "medium",
            "sample_channel_mapping_still_required",
            "carrier/reference channels are explicit but study-cell reporter channels are not mapped",
        )
    if carrier:
        return (
            "carrier_role_only",
            "low",
            "sample_channel_mapping_still_required",
            "carrier channel is explicit but biological reporter-channel mapping is incomplete",
        )
    if chemistry or roles:
        return (
            "chemistry_or_partial_channel_evidence",
            "low",
            "mapping_evidence_enrichment_required",
            "multiplex chemistry/channel evidence is present but not enough for deterministic row reconstruction",
        )
    return (
        "no_explicit_channel_evidence_relation_recheck",
        "low",
        "relation_mode_recheck",
        "no explicit reporter-channel role evidence was found; multiplex relation may require re-evaluation or additional source material",
    )


def audit_accession(
    accession: str,
    evidence_dirs: list[Path],
    publication_texts: list[tuple[str, str]],
) -> tuple[AccessionAudit, list[ContextHit]]:
    evidence_path = find_evidence_path(accession, evidence_dirs)
    evidence_obj: dict = {}
    if evidence_path:
        try:
            evidence_obj = json.loads(evidence_path.read_text())
        except Exception:
            evidence_obj = {}

    hits: list[ContextHit] = []
    evidence_items = evidence_obj.get("evidence", []) or []
    for item in evidence_items:
        text = str(item.get("text") or "")
        if not text:
            continue
        hits.extend(
            collect_hits(
                str(item.get("source_kind") or "evidence"),
                str(item.get("source_label") or ""),
                str(item.get("id") or ""),
                text,
            )
        )

    for label, text in publication_texts:
        hits.extend(collect_hits("publication_fulltext", label, label, text))

    # Deduplicate equivalent contexts, preferring compact deterministic order.
    dedup: dict[tuple, ContextHit] = {}
    for h in hits:
        key = (tuple(h.channels), tuple(h.roles), h.text)
        dedup.setdefault(key, h)
    hits = list(dedup.values())

    design = evidence_obj.get("study_design", {}) or {}
    chemistry = detect_chemistry(
        [str(x.get("text") or "") for x in evidence_items] + [t for _, t in publication_texts],
        str(design.get("multiplex_chemistry_hint") or ""),
    )
    roles, ambiguous = channel_roles_from_hits(hits)
    all_texts = [str(x.get("text") or "") for x in evidence_items] + [t for _, t in publication_texts]
    rel_recheck, rel_conf, linked_iso, linked_noniso, dataset_iso = relation_recheck(
        chemistry, roles, ambiguous, all_texts
    )

    carrier = sorted(ch for ch, rr in roles.items() if rr == {"carrier"})
    reference = sorted(ch for ch, rr in roles.items() if rr == {"reference"})
    single = sorted(ch for ch, rr in roles.items() if rr == {"single_cell"})
    blank = sorted(ch for ch, rr in roles.items() if rr == {"blank"})
    observed = sorted({ch for h in hits for ch in h.channels}, key=lambda c: CHANNEL_INDEX.get(c, 999))
    mapping_class, confidence, generation, note = mapping_classification(chemistry, roles, ambiguous, hits)
    if rel_recheck in {"substudy_scope_recheck", "nonisobaric_single_cell_candidate"}:
        mapping_class = "relation_false_positive_candidate"
        confidence = rel_conf
        generation = "return_to_nonisobaric_or_mixed_design_lane"
        note = (
            "single-cell evidence is locally linked to non-isobaric acquisition while isobaric evidence is absent "
            "from, or unlinked to, the single-cell branch; re-evaluate the multiplex relation before channel mapping"
        )

    raw_files = evidence_obj.get("raw_files", []) or []
    manuscript_sources = evidence_obj.get("manuscript_sources", []) or []
    audit = AccessionAudit(
        accession=accession,
        evidence_path=str(evidence_path or ""),
        raw_file_count=len(raw_files),
        manuscript_source_count=len(manuscript_sources),
        publication_text_rows=len(publication_texts),
        chemistry=chemistry,
        carrier_channels=carrier,
        reference_channels=reference,
        single_cell_channels=single,
        blank_channels=blank,
        ambiguous_channels=sorted(ambiguous, key=lambda c: CHANNEL_INDEX.get(c, 999)),
        observed_channels=observed,
        mapping_class=mapping_class,
        confidence=confidence,
        candidate_generation_mode=generation,
        context_hits=len(hits),
        relation_recheck=rel_recheck,
        relation_confidence=rel_conf,
        single_cell_isobaric_contexts=linked_iso,
        single_cell_nonisobaric_contexts=linked_noniso,
        dataset_only_isobaric_contexts=dataset_iso,
        notes=note,
    )
    return audit, hits


def write_tsv(path: Path, rows: list[AccessionAudit]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "accession", "mapping_class", "confidence", "candidate_generation_mode", "chemistry",
        "raw_file_count", "manuscript_source_count", "publication_text_rows", "carrier_channels",
        "reference_channels", "single_cell_channels", "blank_channels", "ambiguous_channels",
        "observed_channels", "context_hits", "relation_recheck", "relation_confidence",
        "single_cell_isobaric_contexts", "single_cell_nonisobaric_contexts",
        "dataset_only_isobaric_contexts", "evidence_path", "notes",
    ]
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, delimiter="\t")
        w.writeheader()
        for r in rows:
            d = asdict(r)
            for field in [
                "carrier_channels", "reference_channels", "single_cell_channels", "blank_channels",
                "ambiguous_channels", "observed_channels",
            ]:
                d[field] = ",".join(d[field])
            w.writerow({k: d[k] for k in fields})


def self_test() -> None:
    # Patch-proteomics-style: one analytical channel plus one carrier channel.
    text = (
        "The neuronal soma digest was tagged using the TMT-128 channel (analytical channel). "
        "Reference tissue was tagged using the TMT-131 channel (carrier channel, used for signal enhancement)."
    )
    hits = collect_hits("publication_fulltext", "synthetic", "S1", text)
    roles, amb = channel_roles_from_hits(hits)
    klass, conf, gen, _ = mapping_classification("TMT", roles, amb, hits)
    assert roles["128"] == {"single_cell"}, roles
    assert roles["131"] == {"carrier"}, roles
    assert klass == "single_analytical_channel_per_run", klass
    assert conf == "high" and gen == "generated_one_single_cell_channel_per_raw_with_carrier"

    # SCoPE2-style range with both single cells and control wells must remain ambiguous.
    text = (
        "The carrier and reference were labeled with 126 and 127N, respectively. "
        "Single-cell and control wells were labeled with 128C through 135N."
    )
    hits = collect_hits("publication_fulltext", "synthetic", "S2", text)
    roles, amb = channel_roles_from_hits(hits)
    # Respectively is intentionally not solved by this first auditor; the global range must
    # nevertheless remain non-final rather than being fabricated as all single cells.
    assert amb, (roles, amb)

    # Explicit "respectively" grammar must resolve role/channel pairing rather than
    # assigning both numbers to the nearest carrier/reference word.
    text = "TMT-128 and TMT-131 were used as analytical and carrier channels, respectively."
    hits = collect_hits("publication_fulltext", "synthetic", "Sresp1", text)
    roles, amb = channel_roles_from_hits(hits)
    assert roles["128"] == {"single_cell"}, (roles, hits)
    assert roles["131"] == {"carrier"}, (roles, hits)
    assert not amb

    text = "Carrier and reference channels were labeled with TMTpro126 and TMTpro127N, respectively."
    hits = collect_hits("publication_fulltext", "synthetic", "Sresp2", text)
    roles, amb = channel_roles_from_hits(hits)
    assert roles["126"] == {"carrier"}, (roles, hits)
    assert roles["127N"] == {"reference"}, (roles, hits)
    assert not amb

    # Dataset-level TMT from a separate benchmark must not automatically make the
    # single-cell branch multiplexed.
    texts = [
        "Single cells were analyzed label-free by data-independent acquisition.",
        "In a separate bulk benchmark, pooled samples were TMT labeled for method comparison.",
    ]
    rel, conf, li, ln, di = relation_recheck("TMT", {}, set(), texts)
    assert rel == "substudy_scope_recheck", (rel, conf, li, ln, di)
    assert conf == "high" and ln > 0 and di > 0

    # Explicit single-cell/carrier TMT context remains multiplex-supported.
    texts = ["Single-cell samples were multiplexed with TMTpro and analyzed with a carrier channel."]
    rel, conf, li, ln, di = relation_recheck("TMTpro", {}, set(), texts)
    assert rel == "multiplex_supported", (rel, conf, li, ln, di)
    assert li > 0

    # Bare numeric counts in single-cell prose are not reporter-channel evidence.
    text = "Single-cell proteomics quantified 128 proteins from individual cells."
    assert collect_hits("publication_fulltext", "synthetic", "Sx", text) == []

    # Explicit single-cell range without control language is structurally usable as a candidate.
    text = "Carrier channel TMTpro126; single cells were labeled with TMTpro128C through TMTpro131C."
    hits = collect_hits("publication_fulltext", "synthetic", "S3", text)
    roles, amb = channel_roles_from_hits(hits)
    klass, _, _, _ = mapping_classification("TMTpro", roles, amb, hits)
    assert not amb
    assert klass == "explicit_global_multiplex_layout_candidate", (roles, klass)
    print("sdrf_mapping_evidence_audit self-test: PASS")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accessions-file", type=Path)
    ap.add_argument("--evidence-dir", type=Path, action="append", default=[])
    ap.add_argument("--publication-manifest", type=Path)
    ap.add_argument("--output", type=Path)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return 0
    if not args.accessions_file or not args.output:
        ap.error("--accessions-file and --output are required unless --self-test is used")

    accessions = [x.strip() for x in args.accessions_file.read_text().splitlines() if x.strip()]
    accessions = list(dict.fromkeys(accessions))
    wanted = set(accessions)
    pub_by_acc: dict[str, list[tuple[str, str]]] = defaultdict(list)
    if args.publication_manifest:
        for acc, label, text in iter_manifest_text(args.publication_manifest, wanted):
            pub_by_acc[acc].append((label, text))

    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    (out / "contexts").mkdir(exist_ok=True)

    audits: list[AccessionAudit] = []
    for acc in accessions:
        audit, hits = audit_accession(acc, args.evidence_dir, pub_by_acc.get(acc, []))
        audits.append(audit)
        (out / "contexts" / f"{acc}.mapping_contexts.json").write_text(
            json.dumps([asdict(h) for h in hits], indent=2) + "\n"
        )

    write_tsv(out / "sdrf_mapping_evidence_audit.tsv", audits)
    counts = Counter(x.mapping_class for x in audits)
    relation_counts = Counter(x.relation_recheck for x in audits)
    summary = {
        "auditor_version": AUDITOR_VERSION,
        "accessions": len(audits),
        "mapping_class_counts": dict(sorted(counts.items())),
        "relation_recheck_counts": dict(sorted(relation_counts.items())),
        "publication_text_accessions": sum(1 for x in audits if x.publication_text_rows > 0),
        "high_confidence_candidates": sum(1 for x in audits if x.confidence == "high"),
        "medium_confidence_candidates": sum(1 for x in audits if x.confidence == "medium"),
        "output_tsv": str(out / "sdrf_mapping_evidence_audit.tsv"),
    }
    (out / "sdrf_mapping_evidence_audit_summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    lane_map = defaultdict(list)
    for x in audits:
        lane_map[x.mapping_class].append(x.accession)
    for lane, accs in lane_map.items():
        (out / f"{lane}.txt").write_text("\n".join(accs) + "\n")

    print(json.dumps(summary, indent=2))
    for x in audits:
        print(
            f"{x.accession} class={x.mapping_class} confidence={x.confidence} chemistry={x.chemistry or '-'} "
            f"carrier={x.carrier_channels or '-'} reference={x.reference_channels or '-'} "
            f"single={x.single_cell_channels or '-'} blank={x.blank_channels or '-'} "
            f"ambiguous={x.ambiguous_channels or '-'} relation={x.relation_recheck}/{x.relation_confidence} "
            f"linked_iso={x.single_cell_isobaric_contexts} linked_noniso={x.single_cell_nonisobaric_contexts} "
            f"dataset_iso={x.dataset_only_isobaric_contexts} raw={x.raw_file_count} pub_text={x.publication_text_rows}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
