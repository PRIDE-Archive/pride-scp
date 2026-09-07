#!/usr/bin/env bash
set -euo pipefail

APPLY=0
INCLUDE_EXPERIMENTS=0
ARCHIVE_ROOT=""

usage() {
  cat <<'USAGE'
Usage: scripts/repository_cleanup.sh [options]

Safely move UNTRACKED historical/local artifacts out of the Git worktree.
Tracked paths are always skipped. Nothing is deleted.

Options:
  --apply                 perform moves (default is dry-run)
  --include-experiments   also archive untracked evaluation/experiment helpers
  --archive-root PATH     archive root (default: sibling pride-scp.local-archive)
  -h, --help              show this help
USAGE
}

while (($#)); do
  case "$1" in
    --apply)
      APPLY=1
      shift
      ;;
    --include-experiments)
      INCLUDE_EXPERIMENTS=1
      shift
      ;;
    --archive-root)
      [[ $# -ge 2 ]] || { echo "--archive-root requires a path" >&2; exit 2; }
      ARCHIVE_ROOT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "$REPO_ROOT" ]] || { echo "not inside a Git worktree" >&2; exit 1; }
cd "$REPO_ROOT"

if [[ -z "$ARCHIVE_ROOT" ]]; then
  ARCHIVE_ROOT="${REPO_ROOT}.local-archive"
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$ARCHIVE_ROOT/$STAMP"

is_tracked() {
  git ls-files --error-unmatch -- "$1" >/dev/null 2>&1
}

move_candidate() {
  local path="$1"
  [[ -e "$path" || -L "$path" ]] || return 0

  if is_tracked "$path"; then
    printf 'SKIP tracked: %s\n' "$path"
    return 0
  fi

  if (( APPLY )); then
    local target="$DEST/$path"
    mkdir -p "$(dirname "$target")"
    mv -- "$path" "$target"
    printf 'MOVED: %s -> %s\n' "$path" "$target"
  else
    printf 'WOULD MOVE: %s\n' "$path"
  fi
}

shopt -s nullglob

# Top-level historical notes, handoffs, delivery archives and run reports.
safe_candidates=(
  V0.1.*_NOTES.md
  PRIDE_SCP_HANDOFF*.md
  PRIDE_SCP_NEW_CONVERSATION*.txt
  RUN_*.md
  *_REPORT.md
  PATCH_MANIFEST.sha256
  archive_*.sh
  pride_scp_*.tar.gz
  pride_scp_*.tar.gz.part*
  pride_scp_*.part*
  pride_scp_gpt_reference_mapped
)

for path in "${safe_candidates[@]}"; do
  move_candidate "$path"
done

if (( INCLUDE_EXPERIMENTS )); then
  experimental_candidates=(
    benchmarks
    docs/annotation_evaluation.md
    docs/curation_v19.md
    docs/massive_native_discovery.md
    python/evaluation
    scripts/run_gt196_*.sh
    scripts/run_massive_native_m2_*.sh
    scripts/extract_pride_pdf_review_bundle.py
    python/recall/*publication_content*_smoke.py
  )
  for path in "${experimental_candidates[@]}"; do
    move_candidate "$path"
  done
fi

if (( APPLY )); then
  mkdir -p "$DEST"
  {
    echo "repository=$REPO_ROOT"
    echo "created=$(date --iso-8601=seconds 2>/dev/null || date)"
    echo "include_experiments=$INCLUDE_EXPERIMENTS"
    echo
    echo "git_head=$(git rev-parse HEAD 2>/dev/null || true)"
    echo
    echo "git_status_after_cleanup:"
    git status --short --ignored
  } > "$DEST/CLEANUP_MANIFEST.txt"

  echo
  echo "Archive written under: $DEST"
else
  echo
  echo "Dry run only. Re-run with --apply to move the listed untracked paths."
  echo "Default archive root: $ARCHIVE_ROOT"
fi
