#!/usr/bin/env python3
"""Freeze an exact-hash PRIDE-SCP submission-ready cohort into a release bundle."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import pathlib
import shutil
import tarfile
from datetime import datetime, timezone


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def read_manifest(path: pathlib.Path) -> list[dict[str, str]]:
    with path.open(newline='') as fh:
        rows = list(csv.DictReader(fh, delimiter='\t'))
    required = {'accession', 'sha256', 'status'}
    if not rows or not required <= set(rows[0]):
        raise SystemExit(f'{path}: manifest must contain {sorted(required)}')
    approved = [r for r in rows if r.get('status', '').strip().lower() == 'approved']
    if not approved:
        raise SystemExit(f'{path}: no approved rows')
    return approved


def parse_sdrf(path: pathlib.Path) -> dict[str, object]:
    with path.open(newline='') as fh:
        reader = csv.reader(fh, delimiter='\t')
        rows = list(reader)
    if len(rows) < 2:
        raise SystemExit(f'{path}: SDRF is empty')
    header = rows[0]
    data = rows[1:]
    def unique(col: str) -> list[str]:
        indices = [i for i, h in enumerate(header) if h == col]
        if not indices:
            return []
        values = set()
        for r in data:
            for i in indices:
                if i < len(r) and r[i]:
                    values.add(r[i])
        return sorted(values)
    return {
        'rows': len(data),
        'unique_data_files': len(unique('comment[data file]')),
        'templates': unique('comment[sdrf template]'),
        'organisms': unique('characteristics[organism]'),
    }


def load_project_metadata(root: pathlib.Path | None, acc: str) -> dict[str, object]:
    result: dict[str, object] = {
        'title': '',
        'pride_url': f'https://www.ebi.ac.uk/pride/archive/projects/{acc}',
        'px_url': f'https://proteomecentral.proteomexchange.org/cgi/GetDataset?ID={acc}',
        'dois': [],
        'pubmed_ids': [],
    }
    if root is None:
        return result
    p = root / f'{acc}.json'
    if not p.is_file():
        return result
    try:
        obj = json.loads(p.read_text())
    except Exception:
        return result
    result['title'] = obj.get('title') or ''
    dois: list[str] = []
    pmids: list[str] = []
    if obj.get('doi'):
        dois.append(str(obj['doi']))
    for ref in obj.get('references') or []:
        if isinstance(ref, dict):
            if ref.get('doi'):
                dois.append(str(ref['doi']))
            if ref.get('pubmedID'):
                pmids.append(str(ref['pubmedID']))
    result['dois'] = sorted(set(dois))
    result['pubmed_ids'] = sorted(set(pmids))
    return result


def copy_if_present(src: pathlib.Path | None, dst_dir: pathlib.Path) -> None:
    if src and src.is_file():
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst_dir / src.name)


def write_checksums(root: pathlib.Path) -> None:
    entries: list[tuple[str, str]] = []
    for p in sorted(root.rglob('*')):
        if not p.is_file() or p.name == 'SHA256SUMS':
            continue
        entries.append((sha256(p), p.relative_to(root).as_posix()))
    (root / 'SHA256SUMS').write_text(''.join(f'{d}  {r}\n' for d, r in entries))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--submission-root', type=pathlib.Path, required=True,
                    help='Directory containing <ACC>/<ACC>.sdrf.tsv')
    ap.add_argument('--approved-manifest', type=pathlib.Path, required=True)
    ap.add_argument('--output-dir', type=pathlib.Path, required=True)
    ap.add_argument('--archive', type=pathlib.Path)
    ap.add_argument('--readiness-summary', type=pathlib.Path)
    ap.add_argument('--review-adjudication', type=pathlib.Path)
    ap.add_argument('--review-summary', type=pathlib.Path)
    ap.add_argument('--spec-contract', type=pathlib.Path)
    ap.add_argument('--project-snapshot-root', type=pathlib.Path)
    ap.add_argument('--expected-count', type=int, default=20)
    args = ap.parse_args()

    approved = read_manifest(args.approved_manifest)
    if len(approved) != args.expected_count:
        raise SystemExit(f'expected {args.expected_count} approved rows, got {len(approved)}')

    out = args.output_dir.resolve()
    if out.exists():
        shutil.rmtree(out)
    (out / 'datasets').mkdir(parents=True)
    (out / 'provenance').mkdir(parents=True)

    release_rows: list[dict[str, str]] = []
    pr_rows: list[dict[str, str]] = []

    for row in sorted(approved, key=lambda r: r['accession']):
        acc = row['accession'].strip()
        expected = row['sha256'].strip().lower()
        src = args.submission_root / acc / f'{acc}.sdrf.tsv'
        if not src.is_file():
            raise SystemExit(f'{acc}: missing submission artifact {src}')
        actual = sha256(src)
        if actual != expected:
            raise SystemExit(f'{acc}: approved hash mismatch: expected={expected} actual={actual}')

        dst_dir = out / 'datasets' / acc
        dst_dir.mkdir(parents=True)
        dst = dst_dir / f'{acc}.sdrf.tsv'
        shutil.copy2(src, dst)
        stats = parse_sdrf(dst)
        meta = load_project_metadata(args.project_snapshot_root, acc)

        release_rows.append({
            'accession': acc,
            'sha256': actual,
            'rows': str(stats['rows']),
            'unique_data_files': str(stats['unique_data_files']),
            'templates': ' | '.join(stats['templates']),
            'organisms': ' | '.join(stats['organisms']),
            'reviewer': row.get('reviewer', ''),
            'review_status': 'approved_exact_hash',
        })
        pr_rows.append({
            'accession': acc,
            'title': str(meta['title']),
            'sha256': actual,
            'rows': str(stats['rows']),
            'unique_data_files': str(stats['unique_data_files']),
            'templates': ' | '.join(stats['templates']),
            'organisms': ' | '.join(stats['organisms']),
            'pride_url': str(meta['pride_url']),
            'proteomexchange_url': str(meta['px_url']),
            'dois': ' | '.join(meta['dois']),
            'pubmed_ids': ' | '.join(meta['pubmed_ids']),
            'human_reviewed': 'no',
            'independent_review': 'agent-assisted exact-hash scientific review',
        })

    shutil.copy2(args.approved_manifest, out / 'provenance' / args.approved_manifest.name)
    copy_if_present(args.readiness_summary, out / 'provenance')
    copy_if_present(args.review_adjudication, out / 'provenance')
    copy_if_present(args.review_summary, out / 'provenance')
    copy_if_present(args.spec_contract, out / 'provenance')

    with (out / 'release_manifest.tsv').open('w', newline='') as fh:
        w = csv.DictWriter(fh, delimiter='\t', fieldnames=list(release_rows[0]))
        w.writeheader(); w.writerows(release_rows)
    with (out / 'pr_metadata.tsv').open('w', newline='') as fh:
        w = csv.DictWriter(fh, delimiter='\t', fieldnames=list(pr_rows[0]))
        w.writeheader(); w.writerows(pr_rows)

    release_meta = {
        'release_name': out.name,
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'accessions': len(release_rows),
        'readiness_policy': 'pride-scp-bigbio-readiness-v0.5.14.3',
        'review_policy': 'independent exact-hash scientific review',
        'submission_state': 'submission_ready',
        'upstream_contribution_rules_commit': '1b96296a1dbcf4cd4034440e5f83415a71eeadc5',
    }
    (out / 'release_metadata.json').write_text(json.dumps(release_meta, indent=2, sort_keys=True) + '\n')

    readme = f'''# PRIDE-SCP submission-ready release cohort\n\nThis bundle freezes **{len(release_rows)}** exact-hash SDRF files promoted to `submission_ready` by PRIDE-SCP v0.5.14.3.\n\nEvery `datasets/<ACCESSION>/<ACCESSION>.sdrf.tsv` byte sequence matches the approved independent-review SHA-256 in `release_manifest.tsv`.\n\n## Contribution target\n\nThe files are laid out for `bigbio/sdrf-annotated-datasets` as `datasets/{{ACCESSION}}/{{ACCESSION}}.sdrf.tsv`. The automation must still validate each file with the current upstream `sdrf-pipelines` before opening a pull request.\n\n## Review disclosure\n\nThe cohort was produced with agent-assisted curation and independently agent-reviewed against public repository/publication evidence with exact-hash binding. It has **not** been claimed as human-reviewed; upstream maintainers should be told this explicitly in each PR.\n'''
    (out / 'README.md').write_text(readme)
    write_checksums(out)

    # Verify our own checksum file before archiving.
    for line in (out / 'SHA256SUMS').read_text().splitlines():
        digest, rel = line.split('  ', 1)
        actual = sha256(out / rel)
        if actual != digest:
            raise SystemExit(f'checksum self-verification failed for {rel}')

    if args.archive:
        archive = args.archive.resolve()
        archive.parent.mkdir(parents=True, exist_ok=True)
        if archive.exists(): archive.unlink()
        with tarfile.open(archive, 'w:gz') as tf:
            tf.add(out, arcname=out.name)
        print(f'archive={archive}')
        print(f'archive_sha256={sha256(archive)}')

    print(f'release_dir={out}')
    print(f'accessions={len(release_rows)}')
    print('release_manifest=release_manifest.tsv')
    print('checksums=SHA256SUMS')


if __name__ == '__main__':
    main()
