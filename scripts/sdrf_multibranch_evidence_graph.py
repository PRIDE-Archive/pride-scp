#!/usr/bin/env python3
"""Build source-grounded multi-branch/sub-study contracts for difficult PRIDE SCP deposits.

v0.5.1 established that the remaining hard accessions cannot be represented safely by a single
accession-wide modality.  This stage makes sub-study branches first-class evidence objects.  It is
non-generative: it segments repository/source evidence and records generation eligibility, but never
writes SDRF rows.

The bounded real-data targets are:
  * PXD041399: label-free DDA/DIA plus TMT6/TMTpro single-cell branches;
  * PXD041328/PXD048347: related gastruloid SCP deposits with a closed TMTpro18 design and a
    publication-linked analysis repository containing CellenOne/input files;
  * PXD069039/PXD073405: same-title, high-overlap deposits requiring accession-integrity review and
    targeted SureQuant/TMT branch segmentation before generation.

No GT/reference files are read.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from sdrf_multiplex_evidence_graph import (  # noqa: E402
    DesignContract,
    ExternalSource,
    channels_in,
    extract_design_contracts,
    github_repo_parts,
    manifest_texts,
    norm,
    project_json,
    project_title,
    raw_files,
    read_tsv,
    repo_rows,
    title_similarity,
    write_tsv,
)

VERSION = "pride-scp-sdrf-multibranch-evidence-graph-v0.1"
RAW_RE = re.compile(r"(?i)([^\s\t,;|]+\.(?:raw|d|wiff|wiff2|mzml|mzxml))")
CHANNEL_CONTEXT_RE = re.compile(r"(?i)(?:TMT(?:pro)?|reporter|channel|label).{0,40}(12[6-9]|13[0-5])\s*([NC])?")
HIGH_EXT_RE = re.compile(r"(?i)(?:cell|sample|input|design|metadata|annotation|characteristic|cellenone|channel|tmt|raw|report|sorted|unsorted)")


@dataclass
class BranchContract:
    accession: str
    branch_id: str
    branch_label: str
    modality: str
    confidence: str
    source_kind: str
    source_ref: str
    design_contract_id: str
    analytical_channels: list[str]
    carrier_channels: list[str]
    blank_channels: list[str]
    reference_channels: list[str]
    expected_analytical_count: int | None
    generation_eligibility: str
    blocker: str
    reason: str


@dataclass
class FileMembership:
    accession: str
    branch_id: str
    file_name: str
    file_category: str
    membership: str
    confidence: str
    evidence_source: str
    rule: str
    reason: str


@dataclass
class ExternalFileEvidence:
    accession: str
    source_url: str
    repository_path: str
    local_path: str
    archive_member: str
    evidence_type: str
    raw_tokens: list[str]
    channels: list[str]
    sample_tokens: list[str]
    cell_types: list[str]
    header_fields: list[str]
    row_count: int
    text_preview: str


@dataclass
class AccessionIntegrity:
    accession_a: str
    accession_b: str
    title_similarity: float
    raw_a: int
    raw_b: int
    shared_raws: int
    a_fraction_shared: float
    b_fraction_shared: float
    raw_jaccard: float
    announced_a: str
    announced_b: str
    classification: str
    confidence: str
    generation_policy: str
    reason: str


def write_rows(path: Path, objects: Iterable[Any], fields: list[str]) -> None:
    rows = []
    for obj in objects:
        row = asdict(obj) if hasattr(obj, "__dataclass_fields__") else dict(obj)
        rows.append(row)
    write_tsv(path, rows, fields)


def snapshot_file_rows(snapshot: Path, accession: str) -> list[dict[str, str]]:
    out = []
    for r in repo_rows(snapshot, accession):
        out.append({"name": r.name, "category": r.category, "uri": r.uri})
    return out


def find_contract(contracts: list[DesignContract], accession: str, chemistry: str, required: dict[str, set[str]] | None = None) -> DesignContract | None:
    candidates = [c for c in contracts if c.accession == accession and c.chemistry.lower() == chemistry.lower()]
    if required:
        filtered = []
        for c in candidates:
            ok = True
            for attr, vals in required.items():
                if not vals.issubset(set(getattr(c, attr))):
                    ok = False
                    break
            if ok:
                filtered.append(c)
        candidates = filtered
    candidates.sort(key=lambda c: (not c.complete_global_layout, c.confidence != "high", c.contract_id))
    return candidates[0] if candidates else None


def branch_contract(
    accession: str,
    branch_id: str,
    label: str,
    modality: str,
    confidence: str,
    source_kind: str,
    source_ref: str,
    contract: DesignContract | None,
    eligibility: str,
    blocker: str,
    reason: str,
) -> BranchContract:
    return BranchContract(
        accession=accession,
        branch_id=branch_id,
        branch_label=label,
        modality=modality,
        confidence=confidence,
        source_kind=source_kind,
        source_ref=source_ref,
        design_contract_id=contract.contract_id if contract else "",
        analytical_channels=list(contract.analytical_channels) if contract else [],
        carrier_channels=list(contract.carrier_channels) if contract else [],
        blank_channels=list(contract.blank_channels) if contract else [],
        reference_channels=list(contract.reference_channels) if contract else [],
        expected_analytical_count=contract.expected_analytical_count if contract else None,
        generation_eligibility=eligibility,
        blocker=blocker,
        reason=reason,
    )




def source_specific_041399_contracts(texts: list[tuple[str, str]]) -> list[DesignContract]:
    """Recover branch-scoped reporter designs from the PXD041399 publication.

    v0.5.1's generic block parser can merge neighboring TMT6 and TMTpro sentences into one design
    block.  Here we deliberately scope each branch to its own explicit sentence family.  This is not
    accession metadata hard-coding: every emitted channel role must be present in the supplied
    publication text itself.
    """
    out: list[DesignContract] = []
    for ref, text in texts:
        flat = re.sub(r"\s+", " ", text)
        # TMT6: require all role clauses in a bounded local window.
        m = re.search(r"(?is)For the TMT6plex set.{0,1800}?single zygotes? were labeled with\s*(126\s*,\s*127\s*,\s*128\s*,?\s*(?:and\s*)?129)\s*channels?.{0,1800}?labeled with the TMT\s*131\s*channel.{0,1800}?labeled with the TMT\s*130\s*channel", flat)
        if m:
            out.append(DesignContract(
                accession="PXD041399", contract_id="PXD041399:publication:tmt6_branch", source_kind="publication_branch", source_ref=ref, chemistry="TMT6plex",
                universe=["126","127","128","129","130","131"], analytical_channels=["126","127","128","129"], carrier_channels=["131"], blank_channels=["130"], reference_channels=[], excluded_channels=[], expected_analytical_count=4, confidence="high", complete_global_layout=True, evidence_text=norm(m.group(0))[:2000]
            ))
        # TMTpro/TMT8: the paper explicitly defines eight analytical reporters.  It does not state a
        # carrier/blank role in the same sentence, so preserve a high-confidence partial branch design.
        m2 = re.search(r"(?is)For the TMT8plex set.{0,900}?single zygotes? were labeled with eight channels from 16plex of TMTpro.{0,300}?(126\s*,\s*127N\s*,\s*128C\s*,\s*129N\s*,\s*130C\s*,\s*131N\s*,\s*132C\s*,?\s*(?:and\s*)?133N)", flat)
        if m2:
            out.append(DesignContract(
                accession="PXD041399", contract_id="PXD041399:publication:tmtpro8_branch", source_kind="publication_branch", source_ref=ref, chemistry="TMTpro8",
                universe=[], analytical_channels=["126","127N","128C","129N","130C","131N","132C","133N"], carrier_channels=[], blank_channels=[], reference_channels=[], excluded_channels=[], expected_analytical_count=8, confidence="high", complete_global_layout=False, evidence_text=norm(m2.group(0))[:1200]
            ))
    return out

def classify_041399(contracts: list[DesignContract]) -> list[BranchContract]:
    six = find_contract(
        contracts,
        "PXD041399",
        "TMT6plex",
        {"analytical_channels": {"126", "127", "128", "129"}, "blank_channels": {"130"}, "carrier_channels": {"131"}},
    )
    # Some publication grammars describe the TMTpro 8-channel analytical set without a complete
    # carrier/blank universe. Preserve that as a branch design even when not globally complete.
    tmtpro = next((c for c in contracts if c.accession == "PXD041399" and "tmtpro" in c.chemistry.lower() and len(c.analytical_channels) >= 8), None)
    return [
        branch_contract("PXD041399", "dda_label_free", "DDA label-free optimization", "label_free", "high", "repository+publication", "PXD041399 DDAgradient files and publication label-free DDA methods", None, "branch_segmentable", "exact RAW membership still required", "Repository names and publication methods independently identify a label-free DDA branch."),
        branch_contract("PXD041399", "dia_label_free", "DIA label-free optimization", "label_free", "high", "repository+publication", "PXD041399 DIAgradient/DIAmethodtest files and publication directDIA methods", None, "branch_segmentable", "exact RAW membership still required", "Repository names and publication methods independently identify label-free DIA branches."),
        branch_contract("PXD041399", "tmt6_single_cell", "TMT6 single-zygote branch", "reporter_multiplexed_single_cell", "high" if six else "medium", "publication+repository", "10.1016/j.jpha.2023.05.003 + TMT6plexSingleCell PG report", six, "design_closed_file_mapping_open" if six else "design_partial", "branch RAW/plex mapping required", "The publication specifies four single-zygote channels, one blank and one carrier; the repository has a dedicated TMT6plexSingleCell report."),
        branch_contract("PXD041399", "tmtpro_single_cell", "TMTpro/TMT8 single-zygote branch", "reporter_multiplexed_single_cell", "high" if tmtpro else "medium", "publication+repository", "10.1016/j.jpha.2023.05.003 + TMT8plexSingleCell PG report", tmtpro, "design_closed_file_mapping_open" if tmtpro and tmtpro.analytical_channels else "design_partial", "branch RAW/plex mapping required", "A distinct TMTpro/TMT8 single-cell branch is explicitly represented by publication reporter channels and a dedicated repository PG report."),
    ]


def classify_gastruloid(contracts: list[DesignContract]) -> list[BranchContract]:
    out = []
    for acc, branch_id, label, reason in [
        ("PXD041328", "gastruloid_single_cell_primary", "Gastruloid single-cell TMTpro18 branch", "Primary gastruloid SCP deposit; source analysis repository contains CellenOne characteristics and unsorted/sorted input bundles."),
        ("PXD048347", "bra_gfp_single_cell", "BRA-GFP+ gastruloid single-cell TMTpro18 branch", "The study data-availability statement identifies PXD048347 as BRA-GFP+ single-cell proteomics data; repository RAW names also contain GFP."),
    ]:
        c = find_contract(contracts, acc, "TMTpro18", {"carrier_channels": {"126"}, "blank_channels": {"127C"}})
        out.append(branch_contract(acc, branch_id, label, "reporter_multiplexed_single_cell", "high" if c else "medium", "publication+analysis_repository", "10.1016/j.stem.2024.04.017 + github.com/PSobrevalsAlcaraz/SCP_Stelloo.et.al.2023", c, "design_closed_external_mapping_open" if c else "design_partial", "RAW -> cell -> reporter mapping from published analysis inputs", reason))
    return out


def classify_pandey(contracts: list[DesignContract]) -> list[BranchContract]:
    # Do not force a reporter channel set until the targeted branch source is structurally closed.
    return [
        branch_contract("PXD069039", "surequant_targeted_predecessor", "SureQuant/shTMT targeted single-cell branch (predecessor deposit)", "targeted_reporter_single_cell", "medium", "repository+project_metadata", "PXD069039 SureQuant/Skyline/PD artifacts", None, "blocked_integrity_review", "probable predecessor/expanded redeposit relation to PXD073405", "The project title and structured artifacts match the later published targeted SureQuant/TMT study, but accession identity must be resolved before generation."),
        branch_contract("PXD073405", "h3_single_cell_surequant", "Histone H3 single-cell SureQuant/shTMT branch", "targeted_reporter_single_cell", "high", "publication+repository", "10.1021/acs.jproteome.5c00972 + H3_SingleCell_PTM_SureQuant.sky", None, "integrity_review_then_mapping", "resolve PXD069039 relationship and acquire Skyline branch mapping", "The publication and PX record explicitly define a targeted single-cell TMT/SureQuant experiment with super-heavy TMT trigger peptides."),
        branch_contract("PXD073405", "hela_dda_comparator", "HeLa DDA comparator/method-development branch", "comparator_non_primary_scp", "high", "repository_filename", "HeLa_*DDA*.pdResult/.raw", None, "non_primary_branch", "none", "Repository filenames explicitly identify DDA comparison runs distinct from the targeted H3 single-cell branch."),
        branch_contract("PXD073405", "hela_prm_comparator", "HeLa PRM comparator/method-development branch", "comparator_non_primary_scp", "high", "repository_filename", "HeLa_*PRM*.sky/.csv/.raw", None, "non_primary_branch", "none", "Repository filenames explicitly identify standard PRM comparison runs."),
        branch_contract("PXD073405", "hela_surequant_methoddev", "HeLa SureQuant method-development branch", "targeted_method_development", "high", "repository_filename", "HeLa_*SureQuant*", None, "non_primary_branch", "none", "Repository filenames explicitly identify HeLa SureQuant method-development material distinct from H3 single-cell PTM runs."),
    ]


def membership_rule(accession: str, file_name: str, branches: list[BranchContract]) -> list[FileMembership]:
    low = file_name.lower()
    category = "RAW" if re.search(r"(?i)\.(?:raw|d|wiff|wiff2|mzml|mzxml)$", file_name) else "SUPPORT"
    hits: list[tuple[str, str, str, str]] = []

    if accession == "PXD041399":
        rules = [
            ("tmt6_single_cell", r"tmt6(?:plex)?singlecell|tmt6plex", "filename_branch_token", "Explicit TMT6plexSingleCell repository naming."),
            ("tmtpro_single_cell", r"tmt8(?:plex)?singlecell|tmt8plex|tmtpro", "filename_branch_token", "Explicit TMT8plex/TMTpro single-cell repository naming."),
            ("dda_label_free", r"ddagradient|(?:^|[_-])dda(?:[_-]|$)", "filename_branch_token", "Explicit DDA-gradient repository naming."),
            ("dia_label_free", r"diagradient|diamethodtest|(?:^|[_-])dia(?:[_-]|$)", "filename_branch_token", "Explicit DIA-gradient/method-test repository naming."),
        ]
        for bid, pattern, rule, reason in rules:
            if re.search(pattern, low, re.I):
                hits.append((bid, "member", rule, reason))

    elif accession == "PXD041328":
        # Do not infer a biological class from an opaque run name.  We can, however, assign the SCP
        # acquisition family itself because publication says this accession is SCP and the RAW prefix is
        # internally consistent. External mapping evidence may later resolve individual cells/channels.
        if category == "RAW" and re.search(r"20221222_e1_rm_cvg_exp55|scp", low):
            hits.append(("gastruloid_single_cell_primary", "member", "accession_scp_family", "Publication defines PXD041328 as single-cell proteomics; matching repository acquisition family retained without inventing cell identity."))
    elif accession == "PXD048347":
        if category == "RAW" and ("scp_gastruloids_gfp" in low or re.search(r"_gfp_?c\d", low)):
            hits.append(("bra_gfp_single_cell", "member", "bra_gfp_filename_plus_publication", "Publication identifies PXD048347 as BRA-GFP+ SCP and RAW name independently contains the GFP SCP acquisition family."))
    elif accession in {"PXD069039", "PXD073405"}:
        target = "surequant_targeted_predecessor" if accession == "PXD069039" else "h3_single_cell_surequant"
        if "h3_singlecell_ptm_surequant" in low:
            hits.append((target, "member", "targeted_branch_filename", "Explicit H3_SingleCell_PTM_SureQuant filename."))
        elif "hela" in low and "dda" in low:
            if accession == "PXD073405": hits.append(("hela_dda_comparator", "member", "comparator_filename", "Explicit HeLa DDA comparator filename."))
            else: hits.append((target, "candidate", "predecessor_shared_method_file", "HeLa DDA method-development file in predecessor accession; not primary single-cell branch."))
        elif "hela" in low and "prm" in low:
            if accession == "PXD073405": hits.append(("hela_prm_comparator", "member", "comparator_filename", "Explicit HeLa PRM comparator filename."))
            else: hits.append((target, "candidate", "predecessor_shared_method_file", "HeLa PRM method-development file in predecessor accession; not primary single-cell branch."))
        elif "hela" in low and "surequant" in low:
            if accession == "PXD073405": hits.append(("hela_surequant_methoddev", "member", "methoddev_filename", "Explicit HeLa SureQuant method-development filename."))
            else: hits.append((target, "candidate", "predecessor_shared_method_file", "HeLa SureQuant method-development file in predecessor accession."))
        elif "surequant" in low or "singlecell" in low or "single_cell" in low:
            hits.append((target, "candidate", "targeted_semantic_filename", "Filename is compatible with targeted single-cell/SureQuant branch but lacks the strongest H3 branch token."))

    out = []
    known = {b.branch_id for b in branches if b.accession == accession}
    for bid, membership, rule, reason in hits:
        if bid in known:
            out.append(FileMembership(accession, bid, file_name, category, membership, "high" if membership == "member" else "medium", "repository_file_name+source_branch_contract", rule, reason))
    if not out:
        out.append(FileMembership(accession, "", file_name, category, "unassigned", "low", "repository_inventory", "no_source_grounded_branch_rule", "No branch membership asserted from filename alone."))
    return out


def recursively_find_value(obj: Any, keys: set[str]) -> list[str]:
    out: list[str] = []
    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                if str(k).lower() in keys and isinstance(v, (str, int, float)) and norm(v): out.append(norm(v))
                walk(v)
        elif isinstance(x, list):
            for v in x: walk(v)
    walk(obj)
    return out


def project_announce(snapshot: Path, accession: str) -> str:
    obj = project_json(snapshot, accession)
    vals = recursively_find_value(obj, {"announcedate", "announce_date", "publicationdate", "submissiondate", "created", "createdat"})
    return vals[0] if vals else ""


def integrity_pair(snapshot: Path, a: str, b: str) -> tuple[AccessionIntegrity, list[str], list[str], list[str]]:
    ra, rb = set(raw_files(snapshot, a)), set(raw_files(snapshot, b))
    shared = sorted(ra & rb); ua = sorted(ra - rb); ub = sorted(rb - ra)
    sim = title_similarity(project_title(snapshot, a), project_title(snapshot, b))
    af = len(shared) / len(ra) if ra else 0.0; bf = len(shared) / len(rb) if rb else 0.0
    jac = len(shared) / len(ra | rb) if (ra | rb) else 0.0
    da, db = project_announce(snapshot, a), project_announce(snapshot, b)
    if sim >= 0.95 and af >= 0.85 and len(rb) >= len(ra):
        cls = "probable_predecessor_expanded_redeposit"
        conf = "high"
        policy = f"block_generation_for_{a}_until_relationship_resolved;prefer_later_published_accession_for_source_reconstruction"
        reason = "Near-identical project identity plus most earlier RAWs are contained in the larger companion accession; classify as probable predecessor/expanded redeposit, not independent studies."
    elif sim >= 0.9 and jac >= 0.5:
        cls = "same_study_companion_or_redeposit_review"
        conf = "high"
        policy = "block_both_until_relationship_resolved"
        reason = "Same project identity and substantial exact RAW overlap require accession-integrity review before SDRF generation."
    else:
        cls = "related_accessions_review"
        conf = "medium"
        policy = "review_before_generation"
        reason = "Related project identity/file evidence is insufficient for a stronger relation class."
    return AccessionIntegrity(a,b,sim,len(ra),len(rb),len(shared),af,bf,jac,da,db,cls,conf,policy,reason), shared, ua, ub


def load_v051_contracts(path: Path) -> list[DesignContract]:
    out = []
    for r in read_tsv(path):
        try:
            out.append(DesignContract(
                accession=r.get("accession", ""), contract_id=r.get("contract_id", ""), source_kind=r.get("source_kind", ""), source_ref=r.get("source_ref", ""), chemistry=r.get("chemistry", ""),
                universe=[x for x in r.get("universe", "").split(",") if x], analytical_channels=[x for x in r.get("analytical_channels", "").split(",") if x], carrier_channels=[x for x in r.get("carrier_channels", "").split(",") if x], blank_channels=[x for x in r.get("blank_channels", "").split(",") if x], reference_channels=[x for x in r.get("reference_channels", "").split(",") if x], excluded_channels=[x for x in r.get("excluded_channels", "").split(",") if x], expected_analytical_count=int(r["expected_analytical_count"]) if r.get("expected_analytical_count", "").isdigit() else None,
                confidence=r.get("confidence", ""), complete_global_layout=r.get("complete_global_layout", "").lower() == "true", evidence_text=r.get("evidence_text", ""),
            ))
        except Exception:
            continue
    return out


def github_sources(path: Path, wanted: set[str]) -> list[ExternalSource]:
    out = []
    for r in read_tsv(path):
        acc = r.get("accession", "").upper()
        if acc not in wanted or r.get("source_type", "").lower() != "github": continue
        out.append(ExternalSource(acc, "github", r.get("url", ""), r.get("source_ref", ""), r.get("confidence", "")))
    return out


def _request_session():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": "PRIDE-SCP-multibranch/0.1", "Accept": "application/vnd.github+json"})
    return s


def fetch_github_branch_sources(source: ExternalSource, dest: Path, max_files: int, max_bytes: int, max_archive_bytes: int) -> list[Path]:
    parts = github_repo_parts(source.url)
    if not parts: return []
    owner, repo = parts
    try:
        sess = _request_session()
        meta = sess.get(f"https://api.github.com/repos/{owner}/{repo}", timeout=45); meta.raise_for_status()
        branch = meta.json().get("default_branch") or "main"
        tree = sess.get(f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1", timeout=60); tree.raise_for_status()
        candidates = []
        for e in tree.json().get("tree") or []:
            if e.get("type") != "blob": continue
            p = str(e.get("path") or ""); size = int(e.get("size") or 0); suff = Path(p).suffix.lower()
            if not HIGH_EXT_RE.search(p): continue
            if suff in {".zip", ".7z"}:
                if size and size > max_archive_bytes: continue
                score = 8 if re.search(r"(?i)(?:inputfiles|sorted|unsorted|sample|cell)", p) else 4
            elif suff in {".txt", ".tsv", ".csv", ".xlsx", ".json", ".yaml", ".yml", ".r", ".py"}:
                if size and size > max_bytes: continue
                score = 10 if re.search(r"(?i)(?:cellenone|characteristic|input|sample|design|annotation|metadata)", p) else 5
            else:
                continue
            candidates.append((score, size, p))
        candidates.sort(key=lambda x: (-x[0], x[1], x[2]))
        out = []
        for _, _, p in candidates[:max_files]:
            target = dest / owner / repo / p; target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file(): out.append(target); continue
            r = sess.get(f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{p}", timeout=90)
            limit = max_archive_bytes if target.suffix.lower() in {".zip", ".7z"} else max_bytes
            if not r.ok or len(r.content) > limit: continue
            target.write_bytes(r.content); out.append(target)
        return out
    except Exception:
        return []


def parse_text_table_bytes(data: bytes, name: str, accession: str, source_url: str, local_path: str, archive_member: str = "") -> ExternalFileEvidence | None:
    try: text = data.decode("utf-8-sig", errors="replace")
    except Exception: return None
    lines = [x for x in text.splitlines() if x.strip()]
    if not lines: return None
    sample = "\n".join(lines[:200])
    raw = sorted({m.group(1) for m in RAW_RE.finditer(sample)})
    chans = channels_in(sample) if re.search(r"(?i)(?:TMT|reporter|channel|label)", sample) else []
    cells = sorted(set(re.findall(r"(?i)\b(?:ectoderm|mesoderm|endoderm|mESCs?|unsorted|BRA[-_ ]?GFP\+?|GFP\+?)\b", sample)))
    delim = "\t" if "\t" in lines[0] else "," if "," in lines[0] else None
    headers = [norm(x) for x in lines[0].split(delim)] if delim else []
    samples = []
    for h in headers:
        if re.search(r"(?i)(?:sample|cell|source|type|group|well|plex|batch|replicate|file|raw)", h): samples.append(h)
    ev_type = "cell_characteristics" if re.search(r"(?i)cellenone|characteristic", name) else "analysis_input_table"
    if not (raw or chans or cells or samples or ev_type == "cell_characteristics"): return None
    return ExternalFileEvidence(accession, source_url, name, local_path, archive_member, ev_type, raw, chans, sorted(set(samples)), cells, headers, max(0, len(lines)-1), norm(sample)[:1200])


def parse_external_file(path: Path, accession: str, source_url: str) -> list[ExternalFileEvidence]:
    out: list[ExternalFileEvidence] = []
    suff = path.suffix.lower()
    if suff in {".txt", ".tsv", ".csv", ".r", ".py", ".json", ".yaml", ".yml"}:
        ev = parse_text_table_bytes(path.read_bytes(), path.name, accession, source_url, str(path))
        if ev: out.append(ev)
    elif suff == ".zip":
        try:
            with zipfile.ZipFile(path) as z:
                for info in z.infolist()[:1000]:
                    if info.is_dir() or info.file_size > 25*1024*1024: continue
                    member_suff = Path(info.filename).suffix.lower()
                    if member_suff not in {".txt", ".tsv", ".csv", ".r", ".py", ".json", ".yaml", ".yml"}: continue
                    ev = parse_text_table_bytes(z.read(info), info.filename, accession, source_url, str(path), info.filename)
                    if ev: out.append(ev)
        except Exception:
            pass
    elif suff == ".7z":
        # Optional local extraction. The audit remains useful if py7zr/7z are unavailable: the archive
        # itself is still inventoried and its mapping blocker stays explicit rather than fabricated.
        with tempfile.TemporaryDirectory(prefix="pride_scp_7z_") as td:
            tmp = Path(td); ok = False
            try:
                import py7zr  # type: ignore
                with py7zr.SevenZipFile(path, mode="r") as z: z.extractall(path=tmp)
                ok = True
            except Exception:
                exe = shutil.which("7z") or shutil.which("7za")
                if exe:
                    p = subprocess.run([exe, "x", "-y", f"-o{tmp}", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    ok = p.returncode == 0
            if ok:
                for child in list(tmp.rglob("*"))[:1500]:
                    if not child.is_file() or child.stat().st_size > 25*1024*1024: continue
                    if child.suffix.lower() not in {".txt", ".tsv", ".csv", ".r", ".py", ".json", ".yaml", ".yml"}: continue
                    ev = parse_text_table_bytes(child.read_bytes(), child.relative_to(tmp).as_posix(), accession, source_url, str(path), child.relative_to(tmp).as_posix())
                    if ev: out.append(ev)
    return out


def summarize_external_mapping(evidence: list[ExternalFileEvidence], snapshot: Path, accession: str) -> tuple[int, int, bool, str]:
    repo_raw = {x.lower(): x for x in raw_files(snapshot, accession)}
    exact_raw = set(); channel_rows = 0; cell_rows = 0
    for e in evidence:
        for r in e.raw_tokens:
            if Path(r).name.lower() in repo_raw: exact_raw.add(repo_raw[Path(r).name.lower()])
        if e.channels: channel_rows += 1
        if e.cell_types: cell_rows += 1
    closed = bool(exact_raw and channel_rows and cell_rows)
    reason = f"exact_repository_raws={len(exact_raw)} channel_evidence_rows={channel_rows} cell_annotation_rows={cell_rows}"
    return len(exact_raw), channel_rows, closed, reason


def self_test() -> None:
    # PXD041399 must remain multi-branch rather than collapse to one modality.
    pub = """DIA label-free data were processed with Spectronaut. For a TMT6plex set, channels 126, 127, 128 and 129 contained single zygotes, channel 130 was blank, and channel 131 was carrier. For the TMT8plex set single zygotes were labeled with TMTpro channels 126, 127N, 128C, 129N, 130C, 131N, 132C and 133N."""
    cs = extract_design_contracts("PXD041399", "paper", pub)
    bs = classify_041399(cs)
    assert {b.branch_id for b in bs} == {"dda_label_free","dia_label_free","tmt6_single_cell","tmtpro_single_cell"}
    six = next(b for b in bs if b.branch_id == "tmt6_single_cell")
    assert six.analytical_channels == ["126","127","128","129"] and six.blank_channels == ["130"] and six.carrier_channels == ["131"]
    ms = membership_rule("PXD041399", "2022-mouseZygote-TMT6plexSingleCell-4samples-PGreports.xlsx", bs)
    assert ms[0].branch_id == "tmt6_single_cell" and ms[0].membership == "member"
    ms = membership_rule("PXD041399", "2022-mouseZygote-DIAgradient-10samples-PGreports.xlsx", bs)
    assert ms[0].branch_id == "dia_label_free"

    # Pandey relation classification must block predecessor generation when most earlier RAWs are in later accession.
    with tempfile.TemporaryDirectory(prefix="pride_scp_v052_test_") as td:
        root = Path(td); (root/"files").mkdir(); (root/"projects").mkdir()
        def proj(acc, title): (root/"projects"/f"{acc}.json").write_text(json.dumps({"title":title}))
        title = "Multiplexed quantitation of post-translational modified peptides in single cells using triggered MS/MS combined with super heavy tandem mass tags"
        proj("PXD069039", title); proj("PXD073405", title)
        # repository_files() accepts recursive lists/dicts containing name/category values.
        a = [{"fileName":f"R{i}.raw","fileCategory":{"value":"RAW"}} for i in range(9)]
        b = [{"fileName":f"R{i}.raw","fileCategory":{"value":"RAW"}} for i in range(10)]
        (root/"files"/"PXD069039.json").write_text(json.dumps(a)); (root/"files"/"PXD073405.json").write_text(json.dumps(b))
        rel, shared, _, _ = integrity_pair(root,"PXD069039","PXD073405")
        assert rel.classification == "probable_predecessor_expanded_redeposit" and len(shared) == 9

    t = b"Diameter\tElongation\tCellType\n17\t1.2\tEctoderm\n16\t1.3\tMesoderm\n"
    ev = parse_text_table_bytes(t, "CellsCharacteristics_CellenOne.txt", "PXD041328", "github", "/tmp/x")
    assert ev and ev.evidence_type == "cell_characteristics" and set(ev.cell_types) >= {"Ectoderm","Mesoderm"}
    print("sdrf_multibranch_evidence_graph self-test: PASS")


def run(args: argparse.Namespace) -> int:
    out = args.output; out.mkdir(parents=True, exist_ok=True)
    v051 = args.v051_audit
    contracts = load_v051_contracts(v051/"design_contracts.tsv")

    # Re-derive publication contracts as a safety net: branch association is new in v0.5.2 and should
    # not depend on whether v0.5.1 marked an accession-wide contract complete.
    pub = manifest_texts(args.publication_manifest, {"PXD041399","PXD041328","PXD048347","PXD073405","PXD069039"})
    for acc, rows in pub.items():
        for ref, text in rows:
            contracts.extend(extract_design_contracts(acc, ref, text))
    contracts.extend(source_specific_041399_contracts(pub.get("PXD041399", [])))

    # Deduplicate equivalent contracts.
    uniq = {}
    for c in contracts:
        key = (c.accession,c.chemistry,tuple(c.analytical_channels),tuple(c.carrier_channels),tuple(c.blank_channels),tuple(c.reference_channels))
        old = uniq.get(key)
        if old is None or (not old.complete_global_layout and c.complete_global_layout): uniq[key] = c
    contracts = list(uniq.values())

    branches: list[BranchContract] = []
    branches.extend(classify_041399(contracts))
    branches.extend(classify_gastruloid(contracts))
    branches.extend(classify_pandey(contracts))

    memberships: list[FileMembership] = []
    target_accs = sorted({b.accession for b in branches})
    for acc in target_accs:
        for row in snapshot_file_rows(args.snapshot, acc):
            memberships.extend(membership_rule(acc, row["name"], branches))

    # Accession-integrity audit for the Pandey pair.
    integrity, shared, unique_a, unique_b = integrity_pair(args.snapshot,"PXD069039","PXD073405")
    shared_rows = [{"relation":"shared","accession_a":"PXD069039","accession_b":"PXD073405","file_name":x} for x in shared]
    unique_rows = [{"relation":"unique_a","accession_a":"PXD069039","accession_b":"PXD073405","file_name":x} for x in unique_a] + [{"relation":"unique_b","accession_a":"PXD069039","accession_b":"PXD073405","file_name":x} for x in unique_b]

    # Fetch/parse the publication-linked gastruloid analysis repository with archives included.  URLs
    # remain source-grounded because they came from the publication in v0.5.1.
    wanted_external = {"PXD041328","PXD048347"}
    sources = github_sources(v051/"external_analysis_sources.tsv", wanted_external)
    # Same repository can support both accessions; retain accession provenance separately.
    ext_evidence: list[ExternalFileEvidence] = []
    fetched_inventory = []
    if args.fetch_external_analysis:
        for src in sources:
            if "PSobrevalsAlcaraz/SCP_Stelloo.et.al.2023" not in src.url: continue
            files = fetch_github_branch_sources(src, out/"external_analysis", args.max_external_files, args.max_external_bytes, args.max_archive_bytes)
            for p in files:
                fetched_inventory.append({"accession":src.accession,"source_url":src.url,"file_name":p.name,"local_path":str(p),"size_bytes":p.stat().st_size,"sha256":hashlib.sha256(p.read_bytes()).hexdigest()})
                ext_evidence.extend(parse_external_file(p, src.accession, src.url))

    # Summaries of whether the external analysis has actually closed a mapping. Avoid declaring
    # success from CellenOne characteristics alone: row order without a source-grounded RAW/channel
    # join is not sufficient.
    external_status = []
    for acc in sorted(wanted_external):
        ev = [e for e in ext_evidence if e.accession == acc]
        exact_raw, channel_rows, closed, reason = summarize_external_mapping(ev, args.snapshot, acc)
        external_status.append({"accession":acc,"evidence_files":len(ev),"exact_repository_raw_tokens":exact_raw,"channel_evidence_rows":channel_rows,"mapping_closed":closed,"status":"explicit_row_mapping_candidate" if closed else "external_mapping_partial","reason":reason})
        for b in branches:
            if b.accession == acc and b.branch_id in {"gastruloid_single_cell_primary","bra_gfp_single_cell"}:
                if closed:
                    b.generation_eligibility = "explicit_row_mapping_candidate"
                    b.blocker = "manual inspection of exact external mapping rows before serialization"
                elif ev:
                    b.generation_eligibility = "external_mapping_partial"
                    b.blocker = "join external cell/input rows to repository RAW and reporter channels"

    # Branch-level counts and conflict-free accession state.
    mem_by = defaultdict(list)
    for m in memberships:
        if m.branch_id: mem_by[(m.accession,m.branch_id)].append(m)
    branch_summary = []
    for b in branches:
        ms = mem_by[(b.accession,b.branch_id)]
        branch_summary.append({
            **asdict(b),
            "member_files":sum(m.membership=="member" for m in ms),
            "candidate_files":sum(m.membership=="candidate" for m in ms),
            "member_raw_files":sum(m.membership=="member" and m.file_category=="RAW" for m in ms),
            "candidate_raw_files":sum(m.membership=="candidate" and m.file_category=="RAW" for m in ms),
        })

    accession_summary = []
    for acc in target_accs:
        bs = [b for b in branches if b.accession==acc]
        assigned = {m.file_name for m in memberships if m.accession==acc and m.branch_id and m.membership in {"member","candidate"}}
        rawset = set(raw_files(args.snapshot,acc)); assigned_raw = {m.file_name for m in memberships if m.accession==acc and m.file_category=="RAW" and m.branch_id and m.membership in {"member","candidate"}}
        modalities = sorted({b.modality for b in bs})
        status = "multi_branch_model_ready" if len(bs)>=2 else "single_branch_model"
        if acc == "PXD069039": status = "generation_blocked_accession_integrity"
        elif acc == "PXD073405": status = "multi_branch_integrity_review"
        elif acc in wanted_external:
            est = next((x for x in external_status if x["accession"]==acc), None)
            status = est["status"] if est else "external_mapping_open"
        accession_summary.append({"accession":acc,"branches":len(bs),"modalities":"|".join(modalities),"repository_raw_files":len(rawset),"assigned_raw_files":len(assigned_raw),"unassigned_raw_files":len(rawset-assigned_raw),"assigned_all_files":len(assigned),"accession_status":status})

    write_rows(out/"branch_contracts.tsv", branches, list(BranchContract.__dataclass_fields__))
    write_rows(out/"branch_file_membership.tsv", memberships, list(FileMembership.__dataclass_fields__))
    bfields = list(BranchContract.__dataclass_fields__) + ["member_files","candidate_files","member_raw_files","candidate_raw_files"]
    write_tsv(out/"branch_summary.tsv", branch_summary, bfields)
    write_rows(out/"external_analysis_mapping_evidence.tsv", ext_evidence, list(ExternalFileEvidence.__dataclass_fields__))
    write_tsv(out/"external_analysis_fetch_inventory.tsv", fetched_inventory, ["accession","source_url","file_name","local_path","size_bytes","sha256"])
    write_tsv(out/"external_mapping_status.tsv", external_status, ["accession","evidence_files","exact_repository_raw_tokens","channel_evidence_rows","mapping_closed","status","reason"])
    write_rows(out/"accession_integrity.tsv", [integrity], list(AccessionIntegrity.__dataclass_fields__))
    write_tsv(out/"shared_raw_files.tsv", shared_rows, ["relation","accession_a","accession_b","file_name"])
    write_tsv(out/"unique_raw_files.tsv", unique_rows, ["relation","accession_a","accession_b","file_name"])
    write_tsv(out/"accession_branch_summary.tsv", accession_summary, ["accession","branches","modalities","repository_raw_files","assigned_raw_files","unassigned_raw_files","assigned_all_files","accession_status"])

    summary = {
        "auditor_version": VERSION,
        "accessions": len(target_accs),
        "branch_contracts": len(branches),
        "branches_by_accession": dict(sorted(Counter(b.accession for b in branches).items())),
        "modalities": dict(sorted(Counter(b.modality for b in branches).items())),
        "PXD041399_branch_ids": [b.branch_id for b in branches if b.accession=="PXD041399"],
        "PXD041328_PXD048347_closed_TMTpro18": [b.accession for b in branches if b.accession in wanted_external and b.design_contract_id],
        "PXD069039_PXD073405_relation": asdict(integrity),
        "external_mapping_status": external_status,
        "non_generative": True,
        "gt_metadata_used": False,
        "outputs": {
            "branches": str(out/"branch_contracts.tsv"),
            "membership": str(out/"branch_file_membership.tsv"),
            "branch_summary": str(out/"branch_summary.tsv"),
            "external_mapping": str(out/"external_analysis_mapping_evidence.tsv"),
            "external_status": str(out/"external_mapping_status.tsv"),
            "integrity": str(out/"accession_integrity.tsv"),
            "shared_raws": str(out/"shared_raw_files.tsv"),
            "unique_raws": str(out/"unique_raw_files.tsv"),
            "accession_summary": str(out/"accession_branch_summary.tsv"),
        },
    }
    (out/"multibranch_evidence_graph_summary.json").write_text(json.dumps(summary,indent=2)+"\n")
    print(json.dumps(summary,indent=2))
    for r in accession_summary:
        print(f"{r['accession']} branches={r['branches']} modalities={r['modalities']} raw={r['repository_raw_files']} assigned_raw={r['assigned_raw_files']} unassigned_raw={r['unassigned_raw_files']} status={r['accession_status']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--snapshot", type=Path)
    p.add_argument("--v051-audit", type=Path)
    p.add_argument("--publication-manifest", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--fetch-external-analysis", action="store_true")
    p.add_argument("--max-external-files", type=int, default=24)
    p.add_argument("--max-external-bytes", type=int, default=25*1024*1024)
    p.add_argument("--max-archive-bytes", type=int, default=200*1024*1024)
    p.add_argument("--self-test", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        self_test(); return 0
    required = [args.snapshot,args.v051_audit,args.publication_manifest,args.output]
    if any(x is None for x in required):
        raise SystemExit("--snapshot, --v051-audit, --publication-manifest and --output are required")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
