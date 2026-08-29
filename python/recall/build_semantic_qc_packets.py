#!/usr/bin/env python3
"""Build evidence-grounded semantic-QC packets from the v0.1.8 unified manifest.

v0.1.8's independent QC received mostly *post-gating summaries*.  That hid the
actual Stage-04 passages that made genuine SCP cases such as single eggs look
ambiguous and it also hid population-size contradictions present in preparation
passages.  This helper rehydrates the exact Stage-04 task evidence selected from
the manuscript and the raw samples-task response before independent QC.

No candidate is classified or deleted here.  v0.1.12 also adds conservative
passage-scoped sample-unit anchors that distinguish explicit one-cell target
passages, many-cell target population passages, and separate multi-cell
controls/libraries. The output remains a versioned evidence packet for the
critic/jury stage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

PACKET_VERSION = "v0.1.12-evidence-packet-2"

INDIVIDUAL_RE = re.compile(
    r"\b(?:individual|single)[- ](?:cell|cells|egg|eggs|oocyte|oocytes|"
    r"blastomere|blastomeres|bacterium|bacteria|neuron|neurons|zygote|zygotes)\b"
    r"|\b(?:one|a single)\s+(?:cell|egg|oocyte|blastomere|bacterium|neuron|zygote)\b",
    re.I,
)
MS_RE = re.compile(
    r"\b(?:proteomics?|proteome|mass spectrometr(?:y|ic)|LC[- ]?MS(?:/MS)?|"
    r"MS/MS|Orbitrap|timsTOF|Q Exactive|Astral)\b",
    re.I,
)
POOLING_RE = re.compile(
    r"\b(?:pooled|combined)\s+(?:the\s+)?(?:\d+\s+)?(?:isolated\s+)?"
    r"(?:single[- ]?)?(?:cells?|eggs?|oocytes?|blastomeres?|bacteria|neurons?)\b"
    r"|\b(?:cells?|eggs?|oocytes?|blastomeres?|bacteria|neurons?)\b.{0,80}"
    r"\b(?:were\s+)?(?:pooled|combined)\b",
    re.I | re.S,
)
# Deliberately a *risk* signal, not an automatic contradiction.  Examples like
# "five eggs were each processed" are legitimate SCP; the LLM must use context.
MULTICELL_RE = re.compile(
    r"\b(?:10\s*\^\s*\d+|10[⁰¹²³⁴⁵⁶⁷⁸⁹]+|\d{2,}|hundreds?|thousands?|millions?)"
    r"\s+(?:sorted\s+|isolated\s+|FACS[- ]?sorted\s+)?"
    r"(?:[A-Za-z][A-Za-z-]*\s+){0,3}(?:cells?|protoplasts?)\b",
    re.I,
)
POPULATION_RE = re.compile(
    r"\b(?:cell\s+lines?|cell\s+population|sorted\s+population|bulk\s+(?:cells?|tissue|digest)|"
    r"whole[- ]cell\s+lysates?|pellet\s+of\s+cells?|FACS[- ]?sorted\s+cells?)\b",
    re.I,
)
BENCHMARK_RE = re.compile(
    r"\b(?:single[- ]cell[- ]equivalent|single cell equivalent|diluted bulk|bulk digest|"
    r"low[- ]input benchmark|single[- ]cell[- ]level input|single cell level input)\b",
    re.I,
)


ANCHOR_CELL_COUNT_RE = re.compile(
    r"\b(?:10\s*\^\s*\d+|10[⁰¹²³⁴⁵⁶⁷⁸⁹]+|\d{2,}|hundreds?|thousands?|millions?)"
    r"(?!\s*(?:pg|ng|[µμu]g|mg)\b)\s+"
    r"(?:sorted\s+|isolated\s+|FACS[- ]?sorted\s+)?"
    r"(?:[A-Za-z][A-Za-z-]*\s+){0,3}(?:cells?|protoplasts?)\b",
    re.I,
)
MULTICELL_CONTROL_RE = re.compile(
    r"\b(?:\d{2,}[- ]?cell|\d+\s*(?:/|or)\s*\d+[- ]?cell).{0,80}"
    r"\b(?:librar(?:y|ies)|control|carrier|reference|benchmark)\b",
    re.I | re.S,
)

CONTROL_NEAR_COUNT_RE = re.compile(
    r"\b(?:librar(?:y|ies)|control|carrier|reference|benchmark|blank|bulk digest|diluted bulk)\b",
    re.I,
)
SAMPLE_LINK_RE = re.compile(
    r"\b(?:replicate|sample|well)s?\b",
    re.I,
)
PROTEOMIC_LINK_RE = re.compile(
    r"\b(?:protein(?:s)?|peptide(?:s)?|extract(?:ed|ion)?|digest(?:ed|ion)?|"
    r"LC[-– ]?MS(?:/MS)?|mass spectrometr(?:y|ic))\b",
    re.I,
)
SINGLE_TARGET_PATTERNS = [
    re.compile(r"\bfor single[- ]cell samples?.{0,220}\bindividual wells?\b", re.I | re.S),
    re.compile(r"\bindividual cells?.{0,220}\b(?:digested|analy[sz]ed|LC[-– ]?MS|mass spectrometr)", re.I | re.S),
    re.compile(r"\b(?:one|1)\s+cell\s+(?:per|into)\s+(?:well|sample)\b", re.I),
    re.compile(r"\beach\s+(?:egg|embryo|oocyte|blastomere|cell|bacterium).{0,760}\bmass spectrometr", re.I | re.S),
    re.compile(r"\bproteome\s+of\s+individual\s+.{0,80}?(?:eggs?|oocytes?|blastomeres?|cells?|bacteria)\b", re.I | re.S),
]

def _context(text_value: str, start: int, end: int, flank: int = 320) -> str:
    return text_value[max(0, start - flank): min(len(text_value), end + flank)]

def sample_unit_hints_for(text_value: str) -> list[str]:
    """Return conservative passage-scoped sample-unit hints.

    These are lexical anchors used to stop a many-cell count from a separate
    library/control from being misapplied to genuine one-cell target samples.
    They are evidence hints, not catalogue decisions.
    """
    value = text_value or ""
    hints: set[str] = set()
    if any(p.search(value) for p in SINGLE_TARGET_PATTERNS):
        hints.add("single_cell_target_anchor")

    if MULTICELL_CONTROL_RE.search(value):
        hints.add("multi_cell_control_anchor")

    for match in ANCHOR_CELL_COUNT_RE.finditer(value):
        ctx = _context(value, match.start(), match.end())
        if CONTROL_NEAR_COUNT_RE.search(ctx):
            hints.add("multi_cell_control_anchor")
            continue
        if SAMPLE_LINK_RE.search(ctx) and PROTEOMIC_LINK_RE.search(ctx):
            hints.add("population_target_anchor")
    return sorted(hints)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--unified-manifest",
        default="work/python/semantic_unification/unified_semantic_manifest.jsonl",
    )
    p.add_argument(
        "--annotations-dir",
        default="work/python/pride_scp_annotations/annotations",
    )
    p.add_argument(
        "--output",
        default="work/python/semantic_unification/qc_evidence_packets.jsonl",
    )
    p.add_argument(
        "--summary",
        default="work/python/semantic_unification/qc_evidence_packet_summary.json",
    )
    p.add_argument(
        "--include-likely-non-scp",
        action="store_true",
        help="Also create packets for likely_non_scp rows (default: only the 219 QC/review candidates).",
    )
    p.add_argument("--max-evidence-blocks", type=int, default=12)
    return p.parse_args()


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    out = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def find_annotation_jsons(root: Path, accession: str) -> list[Path]:
    d = root / accession
    if not d.is_dir():
        return []
    return [p for p in sorted(d.glob("*.json")) if ".work" not in p.parts]


def parse_raw_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        # Salvage a JSON object from occasional wrapper prose.
        start, end = raw.find("{"), raw.rfind("}")
        if 0 <= start < end:
            try:
                obj = json.loads(raw[start : end + 1])
                return obj if isinstance(obj, dict) else None
            except Exception:
                pass
    return None


def flags_for(text_value: str) -> list[str]:
    flags = []
    if INDIVIDUAL_RE.search(text_value):
        flags.append("individual_cell_language")
    if MS_RE.search(text_value):
        flags.append("ms_proteomics_language")
    if POOLING_RE.search(text_value):
        flags.append("explicit_pooling_language")
    if MULTICELL_RE.search(text_value):
        flags.append("multi_cell_count_language")
    if POPULATION_RE.search(text_value):
        flags.append("population_or_bulk_language")
    if BENCHMARK_RE.search(text_value):
        flags.append("benchmark_language")
    return flags


def stage04_source_evidence(
    annotations_root: Path,
    accession: str,
    *,
    max_blocks: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Return exact task evidence blocks, raw sample outputs, and source paths."""
    blocks: list[dict[str, Any]] = []
    raw_samples: list[dict[str, Any]] = []
    source_paths: list[str] = []
    seen = set()

    # Order matters: sample/preparation passages are most diagnostic for the
    # unit of measurement; performance passages are supporting evidence.
    task_order = ("samples", "preparation", "single_cell_performance", "low_input_performance")

    for annotation_path in find_annotation_jsons(annotations_root, accession):
        annotation = load_json(annotation_path) or {}
        src = text(annotation.get("publication_source_path"))
        if src and src not in source_paths:
            source_paths.append(src)
        work = annotation_path.with_suffix("")
        work = work.parent / f"{work.name}.work"
        evidence = load_json(work / "evidence.json") or {}
        tasks = evidence.get("tasks") if isinstance(evidence.get("tasks"), dict) else {}
        for task in task_order:
            items = tasks.get(task) or []
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                passage = text(item.get("text"))
                if not passage:
                    continue
                key = (task, text(item.get("block_id")), passage[:200])
                if key in seen:
                    continue
                seen.add(key)
                blocks.append(
                    {
                        "source": "stage04_task_evidence",
                        "task": task,
                        "evidence_id": text(item.get("evidence_id")),
                        "block_id": text(item.get("block_id")),
                        "page": item.get("page"),
                        "section": text(item.get("section")),
                        "text": passage,
                        "flags": flags_for(passage),
                        "sample_unit_hints": sample_unit_hints_for(passage),
                    }
                )
                if len(blocks) >= max_blocks:
                    break
            if len(blocks) >= max_blocks:
                break

        for raw_path in sorted(work.glob("samples.attempt*.raw.txt")):
            obj = parse_raw_json(raw_path)
            if not obj:
                continue
            raw_samples.append(
                {
                    "source": "stage04_samples_raw",
                    "path": str(raw_path),
                    "is_single_cell_proteomics": text(obj.get("is_single_cell_proteomics")).lower(),
                    "true_single_cell_samples": obj.get("true_single_cell_samples") or [],
                    "low_input_benchmarks": obj.get("low_input_benchmarks") or [],
                }
            )
            break

    return blocks[:max_blocks], raw_samples, source_paths


def repository_source_evidence(row: dict[str, Any], max_blocks: int) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    title = text(row.get("dataset_title"))
    description = text(row.get("dataset_description"))
    for source, passage in (("repository_title", title), ("repository_description", description)):
        if passage:
            blocks.append({"source": source, "text": passage, "flags": flags_for(passage), "sample_unit_hints": sample_unit_hints_for(passage)})
    for hit in row.get("discovery_hits") or []:
        if not isinstance(hit, dict):
            continue
        passage = text(hit.get("source_excerpt"))
        if not passage:
            continue
        blocks.append(
            {
                "source": "discovery_hit",
                "lane": text(hit.get("lane")),
                "label": text(hit.get("label")),
                "term": text(hit.get("term")),
                "text": passage,
                "flags": flags_for(passage),
                "sample_unit_hints": sample_unit_hints_for(passage),
            }
        )
        if len(blocks) >= max_blocks:
            break
    return blocks[:max_blocks]


def packet_hash(packet: dict[str, Any]) -> str:
    copy = dict(packet)
    copy.pop("qc_packet_hash", None)
    raw = json.dumps(copy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def main() -> None:
    args = parse_args()
    manifest_path = Path(args.unified_manifest)
    annotations_root = Path(args.annotations_dir)
    output = Path(args.output)
    summary_path = Path(args.summary)
    output.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    rows = read_jsonl(manifest_path)
    if not args.include_likely_non_scp:
        rows = [r for r in rows if text(r.get("unified_route")) != "likely_non_scp"]

    packets = []
    mode_counts: dict[str, int] = {}
    flag_counts: dict[str, int] = {}
    anchor_counts: dict[str, int] = {}
    with_primary = 0
    with_raw_samples = 0

    for row in rows:
        packet = dict(row)
        packet["qc_packet_version"] = PACKET_VERSION
        accession = text(row.get("accession"))
        mode = text(row.get("semantic_evidence_mode"))
        mode_counts[mode] = mode_counts.get(mode, 0) + 1

        if mode == "publication_backed":
            blocks, raw_samples, source_paths = stage04_source_evidence(
                annotations_root,
                accession,
                max_blocks=max(1, args.max_evidence_blocks),
            )
            packet["qc_primary_evidence"] = blocks
            packet["qc_stage04_raw_samples"] = raw_samples
            packet["qc_publication_source_paths"] = source_paths
            if raw_samples:
                with_raw_samples += 1
        else:
            packet["qc_primary_evidence"] = repository_source_evidence(
                row, max(1, args.max_evidence_blocks)
            )
            packet["qc_stage04_raw_samples"] = []
            packet["qc_publication_source_paths"] = []

        if packet["qc_primary_evidence"]:
            with_primary += 1
        all_flags = sorted(
            {
                flag
                for block in packet["qc_primary_evidence"]
                if isinstance(block, dict)
                for flag in (block.get("flags") or [])
            }
        )
        packet["qc_evidence_flags"] = all_flags
        for flag in all_flags:
            flag_counts[flag] = flag_counts.get(flag, 0) + 1

        sample_unit_anchors = {
            "single_cell_target_anchor": [],
            "population_target_anchor": [],
            "multi_cell_control_anchor": [],
        }
        for block in packet["qc_primary_evidence"]:
            if not isinstance(block, dict):
                continue
            for hint in block.get("sample_unit_hints") or []:
                if hint not in sample_unit_anchors:
                    continue
                sample_unit_anchors[hint].append({
                    "source": block.get("source", ""),
                    "task": block.get("task", ""),
                    "page": block.get("page"),
                    "text": text(block.get("text"))[:700],
                })
        packet["qc_sample_unit_anchors"] = sample_unit_anchors
        for anchor, items in sample_unit_anchors.items():
            if items:
                anchor_counts[anchor] = anchor_counts.get(anchor, 0) + 1
        packet["qc_packet_hash"] = packet_hash(packet)
        packets.append(packet)

    with output.open("w", encoding="utf-8") as handle:
        for packet in packets:
            handle.write(json.dumps(packet, ensure_ascii=False, sort_keys=True) + "\n")

    summary = {
        "packet_version": PACKET_VERSION,
        "candidates": len(packets),
        "mode_counts": mode_counts,
        "candidates_with_primary_source_evidence": with_primary,
        "publication_candidates_with_raw_samples_response": with_raw_samples,
        "evidence_flag_candidate_counts": dict(sorted(flag_counts.items())),
        "sample_unit_anchor_candidate_counts": dict(sorted(anchor_counts.items())),
        "output": str(output),
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
