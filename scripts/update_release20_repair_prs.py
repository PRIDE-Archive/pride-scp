#!/usr/bin/env python3
"""Safely publish independently re-approved repairs for revoked release20 SDRFs.

The script never chooses scientific values. It only moves exact, already-approved projected bytes into
Git branches/PRs. PXD019958 and PXD062702 update their existing draft PR branches. PXD019515 opens a
new corrective PR because the defective #462 is already merged.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

PUBLIC_EVIDENCE = {
    "PXD019515": [
        "https://www.ebi.ac.uk/pride/archive/projects/PXD019515",
        "https://doi.org/10.1039/D0SC03636F",
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC8178986/",
    ],
    "PXD019958": [
        "https://www.ebi.ac.uk/pride/archive/projects/PXD019958",
        "https://doi.org/10.1038/s41467-020-19394-5",
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC7658233/",
    ],
    "PXD062702": [
        "https://www.ebi.ac.uk/pride/archive/projects/PXD062702",
        "https://doi.org/10.1002/anie.202510692",
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC12582007/",
    ],
}

REPAIR_NOTES = {
    "PXD019515": "Separates HeLa, spinal-neuron, and blank biological metadata; removes truncated nanoPOTS version prose; restores source-supported HCD.",
    "PXD019958": "Removes synthetic donor/cell-type enrichment, clears biological identity from zero-cell controls, and uses explicit reserved words for label-free single-cell metadata.",
    "PXD062702": "Separates HeLa method-development and Xenopus single-cell rows and reconstructs DDA/DIA plus instrument metadata from file/publication evidence.",
}

TARGETS = {
    "PXD019515": {
        "mode": "corrective",
        "branch": "pride-scp/pxd019515-corrective-source-review-v1",
        "prior_pr": "462",
        "title": "Correct SDRF source metadata for PXD019515 after #462",
    },
    "PXD019958": {
        "mode": "existing_draft",
        "branch": "pride-scp/pxd019958-reviewed-v0514_3",
        "prior_pr": "463",
        "title": "Update SDRF annotation for PXD019958",
    },
    "PXD062702": {
        "mode": "existing_draft",
        "branch": "pride-scp/pxd062702-reviewed-v0514_3",
        "prior_pr": "476",
        "title": "Update SDRF annotation for PXD062702",
    },
}


def run(args, cwd=None, capture=False, check=True):
    print("+", " ".join(map(str, args)))
    cp = subprocess.run(
        list(map(str, args)), cwd=cwd, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )
    if check and cp.returncode:
        msg = (cp.stderr or cp.stdout or "").strip()
        raise RuntimeError(f"command failed ({cp.returncode}): {' '.join(map(str,args))}\n{msg}")
    return cp


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_approved(path: Path) -> dict[str, dict[str, str]]:
    out = {}
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            acc = (row.get("accession") or "").strip()
            status = (row.get("status") or "").strip().lower()
            digest = (row.get("sha256") or row.get("projected_sha256") or "").strip().lower()
            if acc in TARGETS and status in {"approved", "approved_exact_hash"} and len(digest) == 64:
                out[acc] = {**row, "sha256": digest}
    return out


def remote_owner(repo: Path) -> str:
    url = run(["git", "remote", "get-url", "origin"], cwd=repo, capture=True).stdout.strip()
    tail = url.rsplit(":", 1)[-1] if url.startswith("git@") else url.rsplit("github.com/", 1)[-1]
    return tail.split("/", 1)[0].strip()


def changed_paths(repo: Path) -> list[str]:
    cp = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repo, stdout=subprocess.PIPE, check=True,
    )
    raw = cp.stdout
    paths = []
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        text = rec.decode("utf-8", "surrogateescape")
        if len(text) < 4:
            raise RuntimeError(f"malformed porcelain record: {text!r}")
        paths.append(text[3:])
    return paths


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--projected-root", required=True, type=Path,
                    help="Readiness projected root containing <PXD>/<PXD>.sdrf.tsv")
    ap.add_argument("--approved-manifest", required=True, type=Path,
                    help="Fresh independent hash-bound approval manifest")
    ap.add_argument("--repo", required=True, type=Path,
                    help="Local fork of bigbio/sdrf-annotated-datasets")
    ap.add_argument("--pride-repo", type=Path, default=Path.cwd(),
                    help="PRIDE-SCP repo; used for PR body output")
    ap.add_argument("--accession", action="append", choices=sorted(TARGETS))
    ap.add_argument("--submit", action="store_true", help="Push branch/open or update PR. Default is dry-run.")
    ap.add_argument("--results", type=Path, required=True)
    ns = ap.parse_args()

    selected = ns.accession or sorted(TARGETS)
    approved = read_approved(ns.approved_manifest)
    missing = [a for a in selected if a not in approved]
    if missing:
        raise SystemExit("missing fresh independent exact-hash approval for: " + ", ".join(missing))

    repo = ns.repo.resolve()
    if not (repo / ".git").exists():
        raise SystemExit(f"not a Git repository: {repo}")
    run(["gh", "auth", "status"])
    run(["git", "fetch", "upstream", "--prune"], cwd=repo)
    run(["git", "fetch", "origin", "--prune"], cwd=repo)
    owner = remote_owner(repo)
    if owner.lower() == "bigbio":
        raise SystemExit("origin points to bigbio; automated pushes must target a personal fork")

    base = "upstream/main"
    cp = run(["git", "show-ref", "--verify", "--quiet", "refs/remotes/upstream/dev"], cwd=repo, check=False)
    if cp.returncode == 0:
        base = "upstream/dev"

    ns.results.parent.mkdir(parents=True, exist_ok=True)
    body_dir = ns.pride_repo / "work" / "repair_pr_bodies"
    body_dir.mkdir(parents=True, exist_ok=True)
    results = []

    with tempfile.TemporaryDirectory(prefix="pride_scp_sdrf_repairs_") as td:
        for acc in selected:
            meta = TARGETS[acc]
            expected = approved[acc]["sha256"]
            src = ns.projected_root / acc / f"{acc}.sdrf.tsv"
            if not src.is_file():
                raise SystemExit(f"{acc}: projected file missing: {src}")
            actual = sha256(src)
            if actual != expected:
                raise SystemExit(f"{acc}: projected hash {actual} != approved {expected}")

            work = Path(td) / acc
            branch = meta["branch"]
            if meta["mode"] == "existing_draft":
                remote_ref = f"origin/{branch}"
                if run(["git", "show-ref", "--verify", "--quiet", f"refs/remotes/{remote_ref}"], cwd=repo, check=False).returncode:
                    raise SystemExit(f"{acc}: expected existing PR branch missing: {remote_ref}")
                start = remote_ref
            else:
                start = base

            run(["git", "worktree", "add", "--detach", work, start], cwd=repo)
            local_branch = f"repair-work-{acc.lower()}-{os.getpid()}"
            try:
                run(["git", "switch", "-c", local_branch], cwd=work)
                dst = work / "datasets" / acc / f"{acc}.sdrf.tsv"
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                paths = changed_paths(work)
                allowed = f"datasets/{acc}/{acc}.sdrf.tsv"
                if paths not in ([allowed], []):
                    raise RuntimeError(f"{acc}: unexpected changed paths: {paths}")
                if not paths:
                    status = "no_change_branch"
                    results.append([acc, status, branch, "", actual, "existing branch already contains approved bytes"])
                    continue

                run(["parse_sdrf", "validate-sdrf", "--sdrf_file", dst, "--use_ols_cache_only"], cwd=work)
                run(["git", "add", allowed], cwd=work)
                staged = run(["git", "diff", "--cached", "--name-only"], cwd=work, capture=True).stdout.splitlines()
                if staged != [allowed]:
                    raise RuntimeError(f"{acc}: staged paths are not accession-scoped: {staged}")

                commit_msg = (
                    f"Correct SDRF source metadata for {acc}"
                    if meta["mode"] == "corrective"
                    else f"Repair SDRF source metadata for {acc}"
                )
                run(["git", "commit", "-m", commit_msg], cwd=work)

                body = body_dir / f"{acc}.md"
                evidence_lines = "\n".join(f"- {url}" for url in PUBLIC_EVIDENCE[acc])
                body.write_text(
                    f"## Summary\n\nSource-grounded corrective update for `{acc}` after post-submission adversarial review.\n\n"
                    f"{REPAIR_NOTES[acc]}\n\n"
                    f"- Fresh independently approved SHA-256: `{actual}`\n"
                    f"- Prior PR: #{meta['prior_pr']}\n"
                    f"- PRIDE-SCP compatibility architecture remains frozen; this is a scientific curation repair.\n"
                    f"- The repaired file was revalidated locally with `parse_sdrf validate-sdrf --use_ols_cache_only`.\n\n"
                    f"## Public evidence\n\n{evidence_lines}\n\n"
                    f"## Curation / assistance disclosure\n\n"
                    f"Agent assistance was used for evidence synthesis and deterministic repair. The exact new hash was independently reviewed before publication and is not claimed as human-reviewed.\n\n"
                    "No unrelated files are changed.\n"
                )

                if not ns.submit:
                    results.append([acc, "dry_run_validated", branch, "", actual, f"body={body}"])
                    continue

                run(["git", "push", "--force-with-lease", "origin", f"HEAD:{branch}"], cwd=work)
                if meta["mode"] == "existing_draft":
                    run([
                        "gh", "pr", "edit", meta["prior_pr"], "--repo", "bigbio/sdrf-annotated-datasets", "--body-file", body
                    ])
                    pr_url = run([
                        "gh", "pr", "view", meta["prior_pr"], "--repo", "bigbio/sdrf-annotated-datasets", "--json", "url", "--jq", ".url"
                    ], capture=True).stdout.strip()
                    results.append([acc, "draft_pr_updated", branch, pr_url, actual, "approved repair pushed; keep draft until CI/review passes"])
                else:
                    existing = run([
                        "gh", "pr", "list", "--repo", "bigbio/sdrf-annotated-datasets", "--state", "open",
                        "--head", f"{owner}:{branch}", "--json", "url", "--jq", ".[0].url // \"\""
                    ], capture=True).stdout.strip()
                    if existing:
                        pr_url = existing
                        status = "corrective_pr_updated"
                    else:
                        pr_url = run([
                            "gh", "pr", "create", "--repo", "bigbio/sdrf-annotated-datasets", "--base", base.split("/",1)[1],
                            "--head", f"{owner}:{branch}", "--title", meta["title"], "--body-file", body
                        ], capture=True).stdout.strip()
                        status = "corrective_pr_opened"
                    results.append([acc, status, branch, pr_url, actual, f"corrects merged #{meta['prior_pr']}"])
            finally:
                run(["git", "worktree", "remove", "--force", work], cwd=repo, check=False)
                run(["git", "branch", "-D", local_branch], cwd=repo, check=False)

    with ns.results.open("w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["accession", "status", "branch", "pr_url", "sha256", "message"])
        w.writerows(results)
    print(f"results={ns.results}")
    for row in results:
        print("\t".join(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
