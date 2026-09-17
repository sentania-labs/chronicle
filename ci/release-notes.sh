#!/usr/bin/env bash
# Release notes built from merged pull request titles since the previous
# tag. A squash-merged PR's commit subject ends in "(#123)"; that is the
# one shape this greps for. Falls back to GitHub's own generated notes when
# there is no previous tag (the first release).
set -euo pipefail

: "${VERSION:?release version is required}"
: "${GITHUB_REPOSITORY:?repository is required}"
: "${GH_TOKEN:?a token for gh api is required}"

previous_tag="$(git tag --list 'v*' --sort=-v:refname | grep -v "^${VERSION}\$" | head -n1 || true)"

if [ -z "$previous_tag" ]; then
  gh api -X POST "repos/${GITHUB_REPOSITORY}/releases/generate-notes" -f tag_name="$VERSION" -q .body
  exit 0
fi

echo "## Changes since $previous_tag"
echo
# grep exits 1 when nothing matches (no squash-merge commit in range, e.g.
# a tag cut straight off unsquashed merge commits); under set -o pipefail
# that would fail the whole script after the images are already built,
# signed, and pushed by the job before this one, so a merge-free range is
# reported as "no listed changes" instead of crashing the release.
notes="$(git log --pretty=format:'- %s' "${previous_tag}..${VERSION}" \
  | grep -E '\(#[0-9]+\)$' | sort -u || true)"
if [ -z "$notes" ]; then
  echo "(no merged pull requests found in this range)"
else
  echo "$notes"
fi
