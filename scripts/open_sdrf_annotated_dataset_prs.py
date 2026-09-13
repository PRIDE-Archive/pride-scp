#!/usr/bin/env python3
"""Prepare/validate and optionally open one sdrf-annotated-datasets PR per accession."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

UPSTREAM_URL = 'https://github.com/bigbio/sdrf-annotated-datasets.git'
UPSTREAM_REPO = 'bigbio/sdrf-annotated-datasets'
RULES_COMMIT = '1b96296a1dbcf4cd4034440e5f83415a71eeadc5'


def run(cmd: list[str], *, cwd: pathlib.Path | None = None, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    print('+', ' '.join(cmd))
    return subprocess.run(cmd, cwd=cwd, check=check, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def output(cmd: list[str], *, cwd: pathlib.Path | None = None) -> str:
    return run(cmd, cwd=cwd, capture=True).stdout.strip()


def changed_paths(repo: pathlib.Path) -> list[str]:
    """Return worktree paths from git porcelain without corrupting leading status spaces.

    `output()` intentionally strips surrounding whitespace for scalar command output, but
    porcelain status uses a leading space as one of its two status columns.  Parsing status
    through `output()` therefore turns ` M path` into `M path` and shifts the pathname.
    Use NUL-delimited porcelain directly and preserve the raw bytes here instead.
    """
    cp = run(
        ['git', 'status', '--porcelain=v1', '-z', '--untracked-files=all'],
        cwd=repo,
        capture=True,
    )
    raw = cp.stdout
    if not raw:
        return []

    records = raw.split('\0')
    paths: list[str] = []
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if not rec:
            continue
        if len(rec) < 4 or rec[2] != ' ':
            raise RuntimeError(f'unexpected git porcelain record: {rec!r}')
        status = rec[:2]
        path = rec[3:]
        # In -z mode, rename/copy records are followed by the second pathname.  The
        # destination path in the first record is the path that would be committed;
        # consume the companion pathname so it is not mistaken for a new record.
        if 'R' in status or 'C' in status:
            if i >= len(records) or not records[i]:
                raise RuntimeError(f'incomplete rename/copy porcelain record: {rec!r}')
            i += 1
        paths.append(path.replace('\\', '/'))
    return paths


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256(path.read_bytes()).hexdigest()
    return h


def remote_owner(url: str) -> str:
    m = re.search(r'github\.com[:/]([^/]+)/[^/]+?(?:\.git)?$', url)
    if not m:
        raise SystemExit(f'cannot parse GitHub owner from origin URL: {url}')
    return m.group(1)


def read_tsv(path: pathlib.Path) -> list[dict[str, str]]:
    with path.open(newline='') as fh:
        return list(csv.DictReader(fh, delimiter='\t'))


def ensure_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise SystemExit(f'required executable not found on PATH: {name}')


def ensure_remote(repo: pathlib.Path, name: str, url: str, add: bool) -> None:
    remotes = output(['git', 'remote'], cwd=repo).splitlines()
    if name not in remotes:
        if not add:
            raise SystemExit(f'missing git remote {name}; add it or pass --add-upstream-remote')
        run(['git', 'remote', 'add', name, url], cwd=repo)


def detect_base(repo: pathlib.Path, upstream_remote: str, requested: str) -> str:
    if requested != 'auto':
        ref = f'refs/remotes/{upstream_remote}/{requested}'
        if run(['git', 'show-ref', '--verify', '--quiet', ref], cwd=repo, check=False).returncode != 0:
            raise SystemExit(f'base branch not found: {upstream_remote}/{requested}')
        return requested
    # CONTRIBUTING at RULES_COMMIT says dev, or main if main is integration branch.
    for candidate in ('dev', 'main'):
        ref = f'refs/remotes/{upstream_remote}/{candidate}'
        if run(['git', 'show-ref', '--verify', '--quiet', ref], cwd=repo, check=False).returncode == 0:
            return candidate
    raise SystemExit('neither upstream/dev nor upstream/main exists')


def branch_exists(repo: pathlib.Path, branch: str) -> bool:
    return run(['git', 'show-ref', '--verify', '--quiet', f'refs/heads/{branch}'], cwd=repo, check=False).returncode == 0


def current_pr(upstream_repo: str, head: str) -> dict[str, object] | None:
    cp = run(['gh', 'pr', 'list', '--repo', upstream_repo, '--state', 'open', '--head', head,
              '--json', 'number,url,title'], check=False, capture=True)
    if cp.returncode != 0 or not cp.stdout.strip():
        return None
    try:
        items = json.loads(cp.stdout)
    except Exception:
        return None
    return items[0] if items else None


def make_pr_body(meta: dict[str, str], acc: str, hash_: str, action: str) -> str:
    sources = [
        f'- PRIDE project: {meta.get("pride_url") or f"https://www.ebi.ac.uk/pride/archive/projects/{acc}"}',
        f'- ProteomeXchange record: {meta.get("proteomexchange_url") or f"https://proteomecentral.proteomexchange.org/cgi/GetDataset?ID={acc}"}',
    ]
    for doi in [x.strip() for x in (meta.get('dois') or '').split('|') if x.strip()]:
        sources.append(f'- Publication DOI: https://doi.org/{doi}')
    for pmid in [x.strip() for x in (meta.get('pubmed_ids') or '').split('|') if x.strip()]:
        sources.append(f'- PubMed: https://pubmed.ncbi.nlm.nih.gov/{pmid}/')
    title = meta.get('title', '').strip()
    summary_line = f'**Dataset:** {title}' if title else f'**Dataset accession:** {acc}'
    return f'''## Summary\n\n{action.capitalize()} the reviewed SDRF annotation for `{acc}`.\n\n{summary_line}\n\n- Rows: {meta.get('rows', '')}\n- Unique data files: {meta.get('unique_data_files', '')}\n- Organism(s): {meta.get('organisms', '')}\n- Declared template(s): {meta.get('templates', '')}\n- Approved exact SHA-256: `{hash_}`\n\n## Public evidence\n\n{chr(10).join(sources)}\n\nThe sample/file relationships and scientific metadata were checked against public repository and associated publication evidence before this exact hash was promoted to `submission_ready`.\n\n## Validation\n\nValidated locally with the same command required by this repository:\n\n```bash\nparse_sdrf validate-sdrf --sdrf_file datasets/{acc}/{acc}.sdrf.tsv --use_ols_cache_only\n```\n\nResult: **PASS**.\n\n## Curation / assistance disclosure\n\nThis annotation was produced with the PRIDE-SCP source-grounded curation pipeline and agent assistance, then independently agent-reviewed against public evidence with exact-hash binding. **It has not been claimed as human-reviewed.** No sample identifiers, raw-file names, or sample-to-file relationships were invented during the submission-readiness step.\n\nContribution workflow checked against `bigbio/sdrf-annotated-datasets` rules at `{RULES_COMMIT}`.\n'''




def declared_template_names(path: pathlib.Path) -> list[str]:
    """Return declared leaf-template names while preserving repeated SDRF headers."""
    with path.open(newline='', encoding='utf-8-sig') as fh:
        rows = list(csv.reader(fh, delimiter='\t'))
    if not rows:
        return []
    headers = rows[0]
    indices = [i for i, h in enumerate(headers) if h.strip().lower() == 'comment[sdrf template]']
    names: set[str] = set()
    for row in rows[1:]:
        for i in indices:
            if i >= len(row):
                continue
            value = row[i].strip()
            if not value:
                continue
            name = value.split()[0].strip().lower()
            if name:
                names.add(name)
    preferred = ['single-cell', 'dia-acquisition', 'human', 'vertebrates', 'invertebrates', 'plants', 'cell-lines']
    return sorted(names, key=lambda x: (preferred.index(x) if x in preferred else len(preferred), x))


def validate_publication_templates(path: pathlib.Path, cwd: pathlib.Path) -> None:
    """Mirror the repository review gate before a branch is ever pushed."""
    run(['parse_sdrf', 'validate-sdrf', '--sdrf_file', str(path), '--skip-ontology'], cwd=cwd)
    templates = ['ms-proteomics'] + declared_template_names(path)
    seen: set[str] = set()
    for template in templates:
        if template in seen:
            continue
        seen.add(template)
        run(['parse_sdrf', 'validate-sdrf', '--sdrf_file', str(path), '--template', template, '--skip-ontology'], cwd=cwd)


def verify_release_checksums(release: pathlib.Path) -> None:
    sums = release / 'SHA256SUMS'
    if not sums.is_file():
        raise SystemExit(f'missing frozen release checksum file: {sums}')
    for raw in sums.read_text().splitlines():
        if not raw.strip():
            continue
        digest, rel = raw.split('  ', 1)
        p = release / rel
        if not p.is_file():
            raise SystemExit(f'frozen release file missing: {rel}')
        actual = sha256(p)
        if actual != digest:
            raise SystemExit(f'frozen release checksum mismatch: {rel}')


def self_test() -> None:
    assert remote_owner('git@github.com:alice/sdrf-annotated-datasets.git') == 'alice'
    assert remote_owner('https://github.com/alice/sdrf-annotated-datasets.git') == 'alice'
    body = make_pr_body({'rows':'1','unique_data_files':'1','templates':'single-cell v1.0.0'}, 'PXD000001', 'a'*64, 'add')
    assert 'PXD000001' in body and 'human-reviewed' in body and 'parse_sdrf' in body
    t = pathlib.Path(tempfile.gettempdir()) / 'pride_scp_templates_selftest.tsv'
    t.write_text('source name\tcomment[sdrf template]\tcomment[sdrf template]\na\tsingle-cell v1.0.0\thuman v1.1.0\n')
    assert declared_template_names(t) == ['single-cell', 'human']
    t.unlink(missing_ok=True)

    # Regression: porcelain status for a tracked modification starts with a space.
    # Ensure changed_paths() preserves the pathname rather than shifting it by one
    # character through whitespace trimming.
    with tempfile.TemporaryDirectory(prefix='pride_scp_pr_selftest_') as td:
        repo = pathlib.Path(td)
        run(['git', 'init', '-q'], cwd=repo)
        run(['git', 'config', 'user.email', 'selftest@example.invalid'], cwd=repo)
        run(['git', 'config', 'user.name', 'PRIDE-SCP self-test'], cwd=repo)
        target = repo / 'datasets' / 'PXD000001' / 'PXD000001.sdrf.tsv'
        target.parent.mkdir(parents=True)
        target.write_text('header\nold\n')
        run(['git', 'add', '.'], cwd=repo)
        run(['git', 'commit', '-q', '-m', 'seed'], cwd=repo)
        target.write_text('header\nnew\n')
        assert changed_paths(repo) == ['datasets/PXD000001/PXD000001.sdrf.tsv']

    print('self-test: PASS')


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--release-dir', type=pathlib.Path, required=False)
    ap.add_argument('--repo', type=pathlib.Path, required=False)
    ap.add_argument('--origin-remote', default='origin')
    ap.add_argument('--upstream-remote', default='upstream')
    ap.add_argument('--upstream-url', default=UPSTREAM_URL)
    ap.add_argument('--upstream-repo', default=UPSTREAM_REPO)
    ap.add_argument('--base', default='auto', help='auto, dev, or main')
    ap.add_argument('--branch-prefix', default='pride-scp')
    ap.add_argument('--branch-suffix', default='reviewed-v0514_4')
    ap.add_argument('--results', type=pathlib.Path)
    ap.add_argument('--accession', action='append', default=[])
    ap.add_argument('--max-prs', type=int, default=0, help='0 = no limit')
    ap.add_argument('--submit', action='store_true', help='push branches and open GitHub PRs')
    ap.add_argument('--add-upstream-remote', action='store_true')
    ap.add_argument('--continue-on-error', action='store_true')
    ap.add_argument('--self-test', action='store_true')
    args = ap.parse_args()

    if args.self_test:
        self_test(); return
    if not args.release_dir or not args.repo:
        raise SystemExit('--release-dir and --repo are required')

    ensure_tool('git'); ensure_tool('parse_sdrf')
    if args.submit:
        ensure_tool('gh')
        run(['gh', 'auth', 'status'])

    release = args.release_dir.resolve()
    repo = args.repo.resolve()
    verify_release_checksums(release)
    manifest_path = release / 'release_manifest.tsv'
    metadata_path = release / 'pr_metadata.tsv'
    if not manifest_path.is_file():
        raise SystemExit(f'missing {manifest_path}')
    rows = read_tsv(manifest_path)
    metadata_rows = read_tsv(metadata_path) if metadata_path.is_file() else []
    metadata = {r['accession']: r for r in metadata_rows}
    approved = {r['accession']: r for r in rows}
    selected = args.accession or sorted(approved)
    if args.max_prs > 0:
        selected = selected[:args.max_prs]

    if output(['git', 'rev-parse', '--is-inside-work-tree'], cwd=repo) != 'true':
        raise SystemExit(f'not a git repository: {repo}')
    ensure_remote(repo, args.upstream_remote, args.upstream_url, args.add_upstream_remote)
    run(['git', 'fetch', args.upstream_remote, '--prune'], cwd=repo)
    run(['git', 'fetch', args.origin_remote, '--prune'], cwd=repo)
    base = detect_base(repo, args.upstream_remote, args.base)
    origin_url = output(['git', 'remote', 'get-url', args.origin_remote], cwd=repo)
    owner = remote_owner(origin_url)

    if args.submit:
        # Explicitly assert fork owner is not upstream organization.
        if owner.lower() == 'bigbio':
            raise SystemExit('origin appears to be bigbio upstream, not a personal fork; refusing automated push')

    result_path = (args.results or (release / 'pr_submission_results.tsv')).resolve()
    result_path.parent.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, str]] = []

    with tempfile.TemporaryDirectory(prefix='pride_scp_sdrf_prs_') as td:
        tdroot = pathlib.Path(td)
        for acc in selected:
            try:
                if acc not in approved:
                    raise RuntimeError(f'{acc}: not present in approved release manifest')
                entry = approved[acc]
                expected = entry['sha256'].lower()
                source = release / 'datasets' / acc / f'{acc}.sdrf.tsv'
                if not source.is_file():
                    raise RuntimeError(f'{acc}: release file missing')
                actual = sha256(source)
                if actual != expected:
                    raise RuntimeError(f'{acc}: release SHA mismatch')

                branch = f'{args.branch_prefix}/{acc.lower()}-{args.branch_suffix}'
                head = f'{owner}:{branch}'
                if args.submit:
                    existing = current_pr(args.upstream_repo, head)
                    if existing:
                        results.append({'accession':acc,'status':'existing_open_pr','branch':branch,'pr_url':str(existing.get('url','')),'message':'reused existing open PR'})
                        continue

                # Ensure stale local worktree/branch is not reused.
                if branch_exists(repo, branch):
                    run(['git', 'branch', '-D', branch], cwd=repo)
                wt = tdroot / acc
                run(['git', 'worktree', 'add', '--detach', str(wt), f'{args.upstream_remote}/{base}'], cwd=repo)
                try:
                    run(['git', 'switch', '-c', branch], cwd=wt)
                    target = wt / 'datasets' / acc / f'{acc}.sdrf.tsv'
                    existed = target.exists()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    if sha256(target) != expected:
                        raise RuntimeError(f'{acc}: copied hash changed')

                    changed = changed_paths(wt)
                    if not changed:
                        results.append({'accession':acc,'status':'no_change_upstream','branch':branch,'pr_url':'','message':'upstream already contains identical file'})
                        continue
                    allowed_path = f'datasets/{acc}/{acc}.sdrf.tsv'
                    bad = [path for path in changed if path != allowed_path]
                    if bad:
                        raise RuntimeError(f'{acc}: unexpected changed paths: {bad}')

                    run(['parse_sdrf', 'validate-sdrf', '--sdrf_file', str(target), '--use_ols_cache_only'], cwd=wt)
                    validate_publication_templates(target, wt)
                    run(['git', 'add', f'datasets/{acc}/{acc}.sdrf.tsv'], cwd=wt)
                    staged = output(['git', 'diff', '--cached', '--name-only'], cwd=wt).splitlines()
                    if staged != [f'datasets/{acc}/{acc}.sdrf.tsv']:
                        raise RuntimeError(f'{acc}: staged paths are not accession-scoped: {staged}')

                    action = 'update' if existed else 'add'
                    msg = f'{action.capitalize()} SDRF annotation for {acc}'
                    run(['git', 'commit', '-m', msg], cwd=wt)
                    meta = metadata.get(acc, {})
                    body = make_pr_body(meta, acc, expected, action)
                    body_dir = result_path.parent / 'pr_bodies'
                    body_dir.mkdir(parents=True, exist_ok=True)
                    body_file = body_dir / f'{acc}.md'
                    body_file.write_text(body)

                    if not args.submit:
                        results.append({'accession':acc,'status':'dry_run_validated','branch':branch,'pr_url':'','message':f'{action}; validation PASS; PR body={body_file}'})
                        continue

                    run(['git', 'push', '--force-with-lease', '--set-upstream', args.origin_remote, branch], cwd=wt)
                    title = f'{action.capitalize()} SDRF annotation for {acc}'
                    cp = run(['gh','pr','create','--repo',args.upstream_repo,'--base',base,'--head',head,
                              '--title',title,'--body-file',str(body_file)], cwd=wt, capture=True)
                    pr_url = cp.stdout.strip().splitlines()[-1] if cp.stdout.strip() else ''
                    results.append({'accession':acc,'status':'pr_opened','branch':branch,'pr_url':pr_url,'message':f'{action}; validation PASS'})
                finally:
                    run(['git', 'worktree', 'remove', '--force', str(wt)], cwd=repo, check=False)
                    if branch_exists(repo, branch):
                        run(['git', 'branch', '-D', branch], cwd=repo, check=False)
            except Exception as exc:
                results.append({'accession':acc,'status':'error','branch':'','pr_url':'','message':str(exc)})
                if not args.continue_on_error:
                    break

    with result_path.open('w', newline='') as fh:
        fields = ['accession','status','branch','pr_url','message']
        w=csv.DictWriter(fh,delimiter='\t',fieldnames=fields); w.writeheader(); w.writerows(results)
    print(f'results={result_path}')
    for r in results:
        print(r['accession'], r['status'], r['pr_url'], r['message'], sep='\t')
    if any(r['status']=='error' for r in results):
        raise SystemExit(2)


if __name__ == '__main__':
    main()
