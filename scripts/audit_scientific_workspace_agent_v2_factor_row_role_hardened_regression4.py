#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

EXPECTED_HARNESS = "pride-scp-scientific-workspace-agent-v2-factor-row-role-hardened"
EXPECTED = ["PXD006182", "PXD046863", "PXD057685", "PXD063566"]


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def normalize(name: str) -> str:
    return name.strip().lower().replace("-", "_").replace(".", "_").replace(" ", "_")


def deterministic_role(name: str) -> str:
    n = normalize(name)
    if any(term in n for term in ("blank", "buffer", "wash", "empty", "solvent")):
        return "blank"
    if any(term in n for term in ("quality_control", "qualitycontrol", "_qc_", "qc_", "_qc", "irt", "standard", "std_")):
        return "quality_control"
    m = re.search(r"(?:^|[_-])(\d{1,4})(?:[_-]?cells?)(?:[_-]|$)", n)
    if m:
        count = int(m.group(1))
        return "single_cell" if count == 1 else "few_cell"
    if any(term in n for term in ("singlecell", "single_cell", "1cell", "1_cell")):
        return "single_cell"
    if (
        "bulk" in n
        or re.search(r"(?:^|[_-])\d+(?:p|n)g(?:[_-]|$)", n)
        or any(term in n for term in ("hela_digest", "proteomix", "reference_digest"))
        or re.search(r"(?:^|_)\d+(?:_\d+)?(?:pg|ng)[a-z]", n)
    ):
        return "bulk"
    return "unknown"


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def result_paths(output: Path, accession: str):
    root = output / "row_role_hardened" / accession
    return (
        root,
        root / "result.json",
        root / "evidence.json",
        root / f"{accession}.row_role_hardened.sdrf.tsv",
    )


def acquisition_branch_ok(result: dict, branch: str, expected: str) -> bool:
    for record in result.get("adjudications", []):
        if record.get("concept_type") != "acquisition_mode" or record.get("branch_id") != branch:
            continue
        adj = record.get("adjudication", {})
        value = str(adj.get("value", "")).lower()
        if expected == "DIA":
            return adj.get("outcome") == "canonical" and "data-independent" in value
        if expected == "DDA":
            return adj.get("outcome") == "canonical" and ("data-dependent" in value or "pride:0000627" in value)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--json-out", required=True, type=Path)
    args = ap.parse_args()

    summary_path = args.output / "factor_row_role_hardened_summary.json"
    if not summary_path.is_file():
        raise SystemExit(f"missing summary: {summary_path}")
    summary = load_json(summary_path)

    missing = []
    unknown_single_cell_violations = []
    per_accession = {}

    for accession in EXPECTED:
        root, result_path, evidence_path, draft_path = result_paths(args.output, accession)
        if not (result_path.is_file() and evidence_path.is_file() and draft_path.is_file()):
            missing.append(accession)
            continue

        result = load_json(result_path)
        evidence = load_json(evidence_path)
        rows = read_rows(draft_path)
        rows_by_file = {row.get("comment[data file]", ""): row for row in rows}

        for raw in evidence.get("raw_files", []):
            file_name = raw.get("file_name", "")
            if deterministic_role(file_name) != "unknown":
                continue
            row = rows_by_file.get(file_name)
            if row and row.get("characteristics[sample type]", "").strip().lower() == "single cell":
                unknown_single_cell_violations.append({
                    "accession": accession,
                    "file": file_name,
                })

        validation = result.get("validation", {})
        per_accession[accession] = {
            "terminal_status": result.get("terminal_status"),
            "validation_errors": validation.get("validation_errors"),
            "error_counts": validation.get("error_counts", {}),
            "rows": rows,
        }

    p = per_accession.get("PXD057685", {})
    p_rows = {row.get("comment[data file]", ""): row for row in p.get("rows", [])}

    def field(file_name: str, header: str) -> str:
        return p_rows.get(file_name, {}).get(header, "")

    p57685_hela_200_bulk = (
        field("200pgHeLa_raw.zip", "characteristics[sample type]") == "bulk control"
        and field("200pgHeLa_raw.zip", "characteristics[single cell isolation protocol]") == "not applicable"
    )
    p57685_hela_500_bulk = (
        field("500pgHeLa_raw.zip", "characteristics[sample type]") == "bulk control"
        and field("500pgHeLa_raw.zip", "characteristics[single cell isolation protocol]") == "not applicable"
    )
    p57685_xenopus_unresolved = (
        field("xenopus.zip", "characteristics[sample type]") != "single cell"
        and field("xenopus.zip", "characteristics[single cell isolation protocol]") != "manual picking"
    )
    p57685_errors = p.get("error_counts", {})
    p57685_old_isolation_errors_removed = p57685_errors.get("single_cell_isolation_unresolved", 0) == 0
    p57685_row_role_error_retained = p57685_errors.get("scientific_agent_raw_file_role_unresolved", 0) == 1

    p46863 = per_accession.get("PXD046863", {})
    p46863_result = None
    if "PXD046863" not in missing:
        p46863_result = load_json(result_paths(args.output, "PXD046863")[1])

    gates = {
        "expected_regression_size": summary.get("accessions_requested") == 4,
        "all_outputs_present": not missing,
        "all_process_rows_successful": summary.get("successful") == 4 and summary.get("errors") == 0,
        "zero_model_calls": summary.get("total_agent_turns") == 0,
        "zero_tool_actions": summary.get("total_tool_actions") == 0,
        "one_validator_pass_per_accession": summary.get("total_validator_cycles") == 4,
        "row_role_hardened_harness_identity_held": summary.get("harness_version") == EXPECTED_HARNESS,
        "unknown_file_roles_never_coerced_to_single_cell": not unknown_single_cell_violations,
        "p57685_200pg_hela_is_bulk_reference": p57685_hela_200_bulk,
        "p57685_500pg_hela_is_bulk_reference": p57685_hela_500_bulk,
        "p57685_xenopus_role_remains_unresolved": p57685_xenopus_unresolved,
        "p57685_false_isolation_errors_removed": p57685_old_isolation_errors_removed,
        "p57685_unresolved_row_role_remains_explicit_error": p57685_row_role_error_retained,
        "p46863_a1_remains_dia": bool(p46863_result and acquisition_branch_ok(p46863_result, "A001", "DIA")),
        "p46863_a2_remains_dda": bool(p46863_result and acquisition_branch_ok(p46863_result, "A002", "DDA")),
    }
    safety = all(gates.values())
    gates["row_role_regression4_safety_gate_passed"] = safety
    gates["stop_before_accepted22_rerun"] = not safety

    audit = {
        "summary": summary,
        "decision_observables": gates,
        "missing_accessions": missing,
        "unknown_single_cell_violations": unknown_single_cell_violations,
        "per_accession": per_accession,
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("===== PRIDE-SCP v2 ROW-ROLE HARDENING — REGRESSION4 =====")
    print(json.dumps(gates, indent=2, sort_keys=True))
    print(f"full_audit_json={args.json_out}")
    return 0 if safety else 2


if __name__ == "__main__":
    raise SystemExit(main())
