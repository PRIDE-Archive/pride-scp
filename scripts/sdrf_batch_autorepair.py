#!/usr/bin/env python3
"""Bounded cohort-level PRIDE-SCP SDRF repair-to-readiness controller.

The controller does not invent scientific state. It orchestrates the existing Rust
scientific workspace agent and the frozen BigBio readiness policy:

  candidate/de-novo baseline -> readiness-task bridge -> bounded scientific agent
  -> audited candidate -> frozen readiness -> at most N repair rounds -> queue

Independent review remains a distinct exact-hash step. This controller's job is to
eliminate accession-by-accession debugging and return machine-readable terminal queues.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import sdrf_autorepair_tasks as task_bridge

POLICY_VERSION = "pride-scp-bigbio-readiness-v0.5.14.8"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def run(cmd: list[str], *, env: dict[str, str] | None = None, log: Path | None = None) -> int:
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(proc.stdout or "", encoding="utf-8")
    else:
        sys.stdout.write(proc.stdout or "")
    return proc.returncode


def read_json(path: Path) -> dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"expected JSON object: {path}")
    return obj


def parse_accessions(path: Path) -> list[str]:
    return task_bridge.parse_accessions(path)


def discover_candidate(accession: str, roots: list[Path]) -> tuple[Path | None, str]:
    found: list[Path] = []
    for root in roots:
        if root.is_dir():
            found.extend(p for p in root.rglob(f"{accession}.sdrf.tsv") if p.is_file())
    found = sorted(set(found))
    by_hash: dict[str, list[Path]] = defaultdict(list)
    for path in found:
        by_hash[sha256_file(path)].append(path)
    if not by_hash:
        return None, ""
    if len(by_hash) != 1:
        return None, "multiple_nonidentical_candidates"
    digest, paths = next(iter(by_hash.items()))
    chosen = sorted(paths, key=lambda x: (len(str(x)), str(x)))[0]
    return chosen, digest


def write_accessions(path: Path, accessions: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{x}\n" for x in accessions), encoding="utf-8")


def prepare_resolved(accessions: list[str], roots: list[Path], out: Path) -> dict[str, dict[str, str]]:
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    info: dict[str, dict[str, str]] = {}
    for accession in accessions:
        source, status = discover_candidate(accession, roots)
        if source is None:
            info[accession] = {"source": "", "sha256": "", "discovery_status": status or "missing"}
            continue
        dest = out / f"{accession}.sdrf.tsv"
        shutil.copy2(source, dest)
        info[accession] = {
            "source": str(source),
            "sha256": sha256_file(dest),
            "discovery_status": "unique",
        }
    return info


def wrap_agent_candidates(accessions: list[str], agent_root: Path, out: Path) -> dict[str, dict[str, Any]]:
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    result: dict[str, dict[str, Any]] = {}
    for accession in accessions:
        sdrf = agent_root / "sdrf" / f"{accession}.sdrf.tsv"
        audit = agent_root / "audit" / f"{accession}.scientific_agent.json"
        if not sdrf.is_file() or not audit.is_file():
            result[accession] = {"status": "missing_agent_output"}
            continue
        obj = read_json(audit)
        dest_dir = out / accession
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{accession}.sdrf.tsv"
        shutil.copy2(sdrf, dest)
        wrapper = {
            "accession": accession,
            "locally_valid": bool(obj.get("locally_valid")),
            "validation_error_count": int(obj.get("validation_errors") or 0),
            "completeness_status": "locally_valid_draft" if obj.get("locally_valid") else "incomplete_draft",
            "relation_mode": str(obj.get("relation_mode") or ""),
            "generation_mode": str(obj.get("generation_mode") or "scientific_workspace_agent"),
            "source_audit_path": str(audit),
            "source_audit": obj,
            "controller_policy": "bounded_scientific_agent_then_frozen_readiness",
        }
        audit_dest = dest_dir / f"{accession}.sdrf_audit.json"
        audit_dest.write_text(json.dumps(wrapper, indent=2) + "\n", encoding="utf-8")
        result[accession] = {
            "status": "candidate_written",
            "sha256": sha256_file(dest),
            "candidate": str(dest),
            "locally_valid": bool(obj.get("locally_valid")),
            "audit": str(audit),
        }
    return result


def run_readiness(args: argparse.Namespace, accessions_file: Path, candidates: Path, out: Path, log: Path) -> int:
    prefix = (
        [args.singularity, "exec", str(args.readiness_sif), args.python]
        if args.readiness_sif is not None
        else [args.python]
    )
    cmd = prefix + [
        str(args.readiness_script),
        "--accessions-file", str(accessions_file),
        "--snapshot", str(args.snapshot),
        "--candidate-root", str(candidates),
        "--output", str(out),
        "--validator-mode", "required",
        "--ontology-mode", "skip",
        "--sdrf-skills-root", str(args.skills_root),
        "--skills-mode", "required",
    ]
    return run(cmd, log=log)


def build_tasks(
    accessions_file: Path,
    readiness_dir: Path | None,
    candidate_root: Path,
    stage1_root: Path | None,
    out: Path,
) -> dict[str, Any]:
    ns = argparse.Namespace(
        accessions_file=accessions_file,
        readiness_dir=readiness_dir,
        candidate_root=[candidate_root],
        stage1_root=stage1_root,
        output=out,
    )
    return task_bridge.build(ns)


def run_agent(
    args: argparse.Namespace,
    accessions_file: Path,
    resolved: Path,
    tasks: Path,
    out: Path,
    log: Path,
) -> int:
    prefix = (
        [args.singularity, "exec", str(args.agent_sif), args.pride_scp]
        if args.agent_sif is not None
        else [args.pride_scp]
    )
    cmd = prefix + [
        "sdrf-agent",
        "--accessions-file", str(accessions_file),
        "--snapshot", str(args.snapshot),
        "--annotations-dir", str(args.annotations_dir),
        "--resolved-sdrf-dir", str(resolved),
        "--output", str(out),
        "--model", args.model,
        "--ollama-url", args.ollama_url,
        "--max-agent-turns", str(args.max_agent_turns),
        "--max-tool-actions", str(args.max_tool_actions),
        "--max-validator-cycles", str(args.max_validator_cycles),
        "--timeout", str(args.timeout),
    ]
    if args.publication_manifest and args.publication_manifest.is_file():
        cmd.extend(["--publication-manifest", str(args.publication_manifest)])
    env = os.environ.copy()
    env.pop("PRIDE_SCP_SCIENTIFIC_AGENT_MODE", None)
    env["PRIDE_SCP_SCIENTIFIC_AGENT_READINESS_TASKS_DIR"] = str(tasks)
    return run(cmd, env=env, log=log)


def load_task_manifest(task_dir: Path, accession: str) -> list[dict[str, Any]]:
    path = task_dir / f"{accession}.json"
    if not path.is_file():
        return []
    return list(read_json(path).get("tasks") or [])


def agent_terminal_class(agent_root: Path, accession: str) -> tuple[str, dict[str, Any]]:
    path = agent_root / "audit" / f"{accession}.scientific_agent.json"
    if not path.is_file():
        return "", {}
    audit = read_json(path)
    tasks = list(audit.get("readiness_tasks") or [])
    for t in tasks:
        if str(t.get("status") or "") == "template_gap":
            return "template_gap", audit
    for t in tasks:
        if str(t.get("status") or "") != "human_review":
            continue
        codes = " ".join(str(x) for x in t.get("error_codes") or []).lower()
        concept = str(t.get("concept_type") or "")
        if "provenance" in codes or "scope_conflict" in codes or concept == "study_structure":
            return "provenance_conflict", audit
        if "multiplex" in codes or concept == "labeling":
            return "row_mapping_required", audit
    return "", audit


def readiness_result(readiness_root: Path, accession: str) -> dict[str, Any]:
    path = readiness_root / "accessions" / f"{accession}.readiness.json"
    return read_json(path) if path.is_file() else {}


def classify_after_round(
    accession: str,
    readiness_root: Path,
    task_dir: Path,
    agent_root: Path,
    input_sha: str,
    output_sha: str,
    round_no: int,
    max_rounds: int,
) -> tuple[bool, str, str]:
    readiness = readiness_result(readiness_root, accession)
    state = str(readiness.get("state") or "")
    special, audit = agent_terminal_class(agent_root, accession)
    if special:
        return True, special, special
    tasks = load_task_manifest(task_dir, accession)
    if state == "submission_ready":
        return True, "submission_ready", "frozen_readiness_submission_ready"
    if state == "needs_independent_review" and not tasks:
        return True, "needs_independent_review", "frozen_readiness_needs_independent_review"
    if input_sha and output_sha and input_sha == output_sha and tasks:
        concepts = {str(x.get("concept_type") or "") for x in tasks}
        if "labeling" in concepts:
            return True, "row_mapping_required", "no_progress_after_source_grounded_labeling_attempt"
        if "study_structure" in concepts:
            return True, "provenance_conflict", "no_progress_after_source_scope_attempt"
        return True, "unsupported_or_exhausted", "no_candidate_fingerprint_progress"
    if round_no >= max_rounds:
        if state.startswith("blocked_"):
            return True, "unsupported_or_exhausted", f"repair_round_budget_exhausted:{state}"
        return True, state or "unsupported_or_exhausted", "repair_round_budget_exhausted"
    if state.startswith("blocked_") or tasks:
        return False, "", "continue_repair"
    if not bool(audit.get("locally_valid", True)):
        return False, "", "continue_local_validation_repair"
    return True, state or "unsupported_or_exhausted", "no_repairable_task"


def write_queues(root: Path, ledger: list[dict[str, str]]) -> None:
    by_state: dict[str, list[str]] = defaultdict(list)
    for row in ledger:
        by_state[row["final_state"]].append(row["accession"])
    queues = root / "queues"
    shutil.rmtree(queues, ignore_errors=True)
    queues.mkdir(parents=True, exist_ok=True)
    for state, accessions in sorted(by_state.items()):
        (queues / f"{state}.txt").write_text("".join(f"{x}\n" for x in sorted(accessions)), encoding="utf-8")


def controller(args: argparse.Namespace) -> dict[str, Any]:
    accessions = parse_accessions(args.accessions_file)
    args.output.mkdir(parents=True, exist_ok=True)
    current_roots = [x.resolve() for x in args.candidate_root]
    active = list(accessions)
    final: dict[str, dict[str, str]] = {}
    cumulative: dict[str, dict[str, int]] = defaultdict(lambda: {"turns": 0, "tools": 0, "validators": 0})
    previous_readiness = args.seed_readiness_dir if args.seed_readiness_dir and args.seed_readiness_dir.is_dir() else None

    for round_no in range(1, args.max_repair_rounds + 1):
        if not active:
            break
        round_root = args.output / f"round{round_no:02d}"
        round_accessions = round_root / "accessions.txt"
        write_accessions(round_accessions, active)
        resolved = round_root / "resolved"
        discovery = prepare_resolved(active, current_roots, resolved)
        task_dir = round_root / "readiness_tasks"
        build_tasks(round_accessions, previous_readiness, resolved, args.stage1_root, task_dir)

        agent_root = round_root / "agent"
        rc = run_agent(args, round_accessions, resolved, task_dir, agent_root, round_root / "agent.log")
        if rc != 0:
            # Per-accession errors are normally represented in the agent summary;
            # a nonzero process is an infrastructure failure and stops the cohort.
            raise SystemExit(f"scientific agent process failed in round {round_no}: rc={rc}; see {round_root / 'agent.log'}")

        summary_path = agent_root / "scientific_agent_summary.json"
        if summary_path.is_file():
            summary = read_json(summary_path)
            # Totals are retained at round level; per-accession exact counts come from audits below.
            (round_root / "agent_summary.snapshot.json").write_text(json.dumps(summary, indent=2) + "\n")

        candidate_root = round_root / "candidates"
        wrapped = wrap_agent_candidates(active, agent_root, candidate_root)
        readiness_root = round_root / "readiness"
        rc = run_readiness(args, round_accessions, candidate_root, readiness_root, round_root / "readiness.log")
        if rc != 0:
            # Readiness may return nonzero for missing required tools. Treat that as
            # infrastructure-fatal instead of silently classifying scientific blockers.
            summary = readiness_root / "sdrf_readiness_summary.json"
            if not summary.is_file():
                raise SystemExit(f"readiness process failed in round {round_no}: rc={rc}; see {round_root / 'readiness.log'}")

        # Build tasks from the *new* readiness state for stopping/classification.
        next_tasks = round_root / "next_readiness_tasks"
        build_tasks(round_accessions, readiness_root, candidate_root, args.stage1_root, next_tasks)

        next_active: list[str] = []
        for accession in active:
            input_sha = str(discovery.get(accession, {}).get("sha256") or "")
            output_sha = str(wrapped.get(accession, {}).get("sha256") or "")
            audit_path = agent_root / "audit" / f"{accession}.scientific_agent.json"
            audit = read_json(audit_path) if audit_path.is_file() else {}
            cumulative[accession]["turns"] += int(audit.get("turns_completed") or 0)
            cumulative[accession]["tools"] += int(audit.get("tool_actions_completed") or 0)
            cumulative[accession]["validators"] += int(audit.get("validator_cycles_completed") or 0)
            done, final_state, reason = classify_after_round(
                accession,
                readiness_root,
                next_tasks,
                agent_root,
                input_sha,
                output_sha,
                round_no,
                args.max_repair_rounds,
            )
            if done:
                rr = readiness_result(readiness_root, accession)
                final[accession] = {
                    "accession": accession,
                    "initial_candidate_sha256": input_sha,
                    "final_candidate_sha256": output_sha,
                    "projected_sha256": str(rr.get("projected_sha256") or ""),
                    "final_state": final_state,
                    "reason_code": reason,
                    "review_status": "pending" if final_state == "needs_independent_review" else "not_applicable",
                    "submission_ready": str(bool(rr.get("submission_ready"))).lower(),
                    "candidate_path": str((candidate_root / accession / f"{accession}.sdrf.tsv") if output_sha else ""),
                    "readiness_path": str(readiness_root / "accessions" / f"{accession}.readiness.json"),
                    "rounds": str(round_no),
                }
            else:
                next_active.append(accession)

        active = next_active
        current_roots = [candidate_root]
        previous_readiness = readiness_root

    for accession in active:
        # Defensive fallback; normal flow exhausts at max rounds in classify_after_round.
        final[accession] = {
            "accession": accession,
            "initial_candidate_sha256": "",
            "final_candidate_sha256": "",
            "projected_sha256": "",
            "final_state": "unsupported_or_exhausted",
            "reason_code": "controller_ended_with_active_accession",
            "review_status": "not_applicable",
            "submission_ready": "false",
            "candidate_path": "",
            "readiness_path": "",
            "rounds": str(args.max_repair_rounds),
        }

    ledger: list[dict[str, str]] = []
    for accession in accessions:
        row = final[accession]
        row["model_turns"] = str(cumulative[accession]["turns"])
        row["tool_actions"] = str(cumulative[accession]["tools"])
        row["validator_cycles"] = str(cumulative[accession]["validators"])
        ledger.append(row)

    ledger_path = args.output / "autorepair_ledger.tsv"
    fields = [
        "accession", "rounds", "model_turns", "tool_actions", "validator_cycles",
        "initial_candidate_sha256", "final_candidate_sha256", "projected_sha256",
        "final_state", "reason_code", "review_status", "submission_ready",
        "candidate_path", "readiness_path",
    ]
    with ledger_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, delimiter="\t", fieldnames=fields)
        writer.writeheader()
        writer.writerows(ledger)
    write_queues(args.output, ledger)
    counts = Counter(row["final_state"] for row in ledger)
    summary = {
        "controller": "pride-scp-batch-autorepair-v1",
        "readiness_policy_required": POLICY_VERSION,
        "agent_runtime": {
            "sif": str(args.agent_sif or ""),
            "sif_sha256": sha256_file(args.agent_sif) if args.agent_sif is not None and args.agent_sif.is_file() else "",
        },
        "readiness_runtime": {
            "sif": str(args.readiness_sif or ""),
            "sif_sha256": sha256_file(args.readiness_sif) if args.readiness_sif is not None and args.readiness_sif.is_file() else "",
        },
        "accessions": len(accessions),
        "state_counts": dict(sorted(counts.items())),
        "submission_ready": sum(row["submission_ready"] == "true" for row in ledger),
        "needs_independent_review": counts.get("needs_independent_review", 0),
        "ledger": str(ledger_path),
        "queues": str(args.output / "queues"),
    }
    (args.output / "autorepair_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return summary



def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="pride-scp-batch-autorepair-") as td:
        root = Path(td)
        readiness = root / "readiness" / "accessions"
        tasks = root / "tasks"
        agent = root / "agent" / "audit"
        readiness.mkdir(parents=True)
        tasks.mkdir(parents=True)
        agent.mkdir(parents=True)

        def write_case(accession: str, state: str, next_tasks: list[dict[str, Any]], audit_tasks: list[dict[str, Any]]) -> None:
            (readiness / f"{accession}.readiness.json").write_text(
                json.dumps({"accession": accession, "state": state, "submission_ready": state == "submission_ready"}),
                encoding="utf-8",
            )
            (tasks / f"{accession}.json").write_text(
                json.dumps({"accession": accession, "tasks": next_tasks}), encoding="utf-8"
            )
            (agent / f"{accession}.scientific_agent.json").write_text(
                json.dumps({"accession": accession, "locally_valid": True, "readiness_tasks": audit_tasks}),
                encoding="utf-8",
            )

        a1 = "PXD000011"
        write_case(a1, "submission_ready", [], [])
        assert classify_after_round(a1, root / "readiness", tasks, root / "agent", "a", "b", 1, 2)[:2] == (True, "submission_ready")

        a2 = "PXD000012"
        write_case(a2, "needs_independent_review", [], [])
        assert classify_after_round(a2, root / "readiness", tasks, root / "agent", "a", "b", 1, 2)[:2] == (True, "needs_independent_review")

        a3 = "PXD000013"
        labeling = {"id": "readiness:label", "concept_type": "labeling", "status": "human_review", "error_codes": ["multiplex_reporter_channel_mapping_unresolved"]}
        write_case(a3, "blocked_metadata_incomplete", [labeling], [labeling])
        assert classify_after_round(a3, root / "readiness", tasks, root / "agent", "a", "a", 1, 2)[:2] == (True, "row_mapping_required")

        a4 = "PXD000014"
        gap = {"id": "readiness:gap", "concept_type": "isolation_method", "status": "template_gap", "error_codes": ["all_rows_placeholder_in_required_single_cell_column"]}
        write_case(a4, "blocked_metadata_incomplete", [gap], [gap])
        assert classify_after_round(a4, root / "readiness", tasks, root / "agent", "a", "a", 1, 2)[:2] == (True, "template_gap")

        a5 = "PXD000015"
        provenance = {"id": "readiness:scope", "concept_type": "study_structure", "status": "human_review", "error_codes": ["trusted_source_scope_conflict"]}
        write_case(a5, "blocked_scientific_conflict", [provenance], [provenance])
        assert classify_after_round(a5, root / "readiness", tasks, root / "agent", "a", "a", 1, 2)[:2] == (True, "provenance_conflict")

        a6 = "PXD000016"
        acquisition = {"id": "readiness:acq", "concept_type": "acquisition_mode", "status": "repair_attempted", "error_codes": ["readiness_acquisition_conflict"]}
        write_case(a6, "blocked_scientific_conflict", [acquisition], [acquisition])
        done, state, reason = classify_after_round(a6, root / "readiness", tasks, root / "agent", "same", "same", 1, 2)
        assert done and state == "unsupported_or_exhausted" and reason == "no_candidate_fingerprint_progress"

    print("sdrf_batch_autorepair self-test: PASS")

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--accessions-file", type=Path)
    p.add_argument("--snapshot", type=Path)
    p.add_argument("--annotations-dir", type=Path)
    p.add_argument("--candidate-root", type=Path, action="append", default=[])
    p.add_argument("--seed-readiness-dir", type=Path)
    p.add_argument("--stage1-root", type=Path)
    p.add_argument("--publication-manifest", type=Path)
    p.add_argument("--skills-root", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--model", default="qwen3.6:27b")
    p.add_argument("--ollama-url", default="http://127.0.0.1:11434/api/generate")
    p.add_argument("--timeout", type=int, default=1200)
    p.add_argument("--max-repair-rounds", type=int, default=2)
    p.add_argument("--max-agent-turns", type=int, default=4)
    p.add_argument("--max-tool-actions", type=int, default=6)
    p.add_argument("--max-validator-cycles", type=int, default=2)
    p.add_argument("--agent-sif", type=Path)
    p.add_argument("--readiness-sif", type=Path)
    p.add_argument("--singularity", default="singularity")
    p.add_argument("--pride-scp", default="pride-scp")
    p.add_argument("--python", default="python")
    p.add_argument("--readiness-script", type=Path, default=Path("/opt/pride-scp/scripts/sdrf_bigbio_readiness.py"))
    return p


def main() -> int:
    args = parser().parse_args()
    if args.self_test:
        self_test()
        return 0
    required = ["accessions_file", "snapshot", "annotations_dir", "skills_root", "output"]
    missing = [name for name in required if getattr(args, name) is None]
    if missing:
        raise SystemExit("missing required arguments: " + ", ".join("--" + x.replace("_", "-") for x in missing))
    for name in ("agent_sif", "readiness_sif"):
        path = getattr(args, name)
        if path is not None and not path.is_file():
            raise SystemExit(f"--{name.replace('_', '-')} not found: {path}")
    if args.max_repair_rounds < 1 or args.max_repair_rounds > 3:
        raise SystemExit("--max-repair-rounds must be between 1 and 3")
    if not args.candidate_root:
        # Missing candidates are allowed because the scientific agent can reconstruct
        # de novo, but an explicit empty root list should be visible in provenance.
        args.candidate_root = []
    controller(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
