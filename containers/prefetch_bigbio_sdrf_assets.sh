#!/usr/bin/env bash
set -euo pipefail

# Clone sdrf-skills with its specification/template submodules and freeze the exact commit state into
# an external, checksummed bundle.  PRIDE-SCP does not silently update this bundle at runtime.
#
# Usage:
#   ./containers/prefetch_bigbio_sdrf_assets.sh OUT_DIR [REF]
#
# REF defaults to main.  The resolved commit SHA is written into provenance.json so deployment is
# reproducible even when a branch name was used for initial acquisition.

OUT="${1:?output directory required}"
REF="${2:-main}"
REPO="${SDRF_SKILLS_REPO:-https://github.com/bigbio/sdrf-skills.git}"
TMP="${OUT}.tmp.$$"

command -v git >/dev/null || { echo "git is required" >&2; exit 2; }
rm -rf "$TMP"
mkdir -p "$TMP"

git clone --recurse-submodules "$REPO" "$TMP/sdrf-skills"
git -C "$TMP/sdrf-skills" checkout "$REF"
git -C "$TMP/sdrf-skills" submodule update --init --recursive

skills_sha="$(git -C "$TMP/sdrf-skills" rev-parse HEAD)"
spec_sha=""
templates_sha=""
if [[ -d "$TMP/sdrf-skills/spec/.git" || -f "$TMP/sdrf-skills/spec/.git" ]]; then
  spec_sha="$(git -C "$TMP/sdrf-skills/spec" rev-parse HEAD)"
fi
TEMPLATES="$TMP/sdrf-skills/spec/sdrf-proteomics/sdrf-templates"
if [[ -d "$TEMPLATES/.git" || -f "$TEMPLATES/.git" ]]; then
  templates_sha="$(git -C "$TEMPLATES" rev-parse HEAD)"
fi

python - "$TMP/provenance.json" "$REPO" "$REF" "$skills_sha" "$spec_sha" "$templates_sha" <<'PY'
import json, sys
path, repo, requested, skills, spec, templates = sys.argv[1:]
with open(path, "w") as fh:
    json.dump({
        "sdrf_skills_repo": repo,
        "requested_ref": requested,
        "sdrf_skills_commit": skills,
        "proteomics_metadata_standard_commit": spec,
        "sdrf_templates_commit": templates,
        "runtime_update_allowed": False,
    }, fh, indent=2, sort_keys=True)
    fh.write("\n")
PY

# Remove VCS object databases from the deployable bundle; provenance retains exact SHAs.
find "$TMP" -name .git -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$TMP" -name .git -type f -delete 2>/dev/null || true
(
  cd "$TMP"
  find . -type f ! -name bundle.sha256 -print0 | sort -z | xargs -0 sha256sum > bundle.sha256
  sha256sum -c bundle.sha256
)

rm -rf "$OUT"
mv "$TMP" "$OUT"
echo "BigBio SDRF assets frozen at: $OUT"
cat "$OUT/provenance.json"
