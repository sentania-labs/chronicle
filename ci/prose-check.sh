#!/usr/bin/env bash
# House rule, enforced rather than remembered: no em-dashes anywhere in the
# tree. Chat, docs, code comments and commit messages all use a comma, a
# colon, parentheses or a period instead.
#
# Untracked but not ignored files are scanned too, so a developer sees the
# failure before the commit rather than in CI.
#
# The character and its HTML entity forms are assembled from parts so this
# file does not contain the thing it is looking for. uv.lock is skipped
# because its content comes from package metadata upstream, not from
# anyone here. docs/screenshots/*/*.html is skipped for the same reason:
# those are byte-for-byte HTTP responses captured verbatim during a live
# check, not authored prose, so a defect they document (like the C5
# rendering bug this check now also catches, fixed on the branch that
# captured them) stays legible as evidence instead of being edited after
# the fact.
set -euo pipefail

amp='&'
emdash=$(printf '\xe2\x80\x94')
entity_named="${amp}mdash;"
# HTML numeric character references allow leading zeros and, for the hex
# form, either case for both the x and the digits, so the pattern accepts
# all of that rather than only the one canonical spelling.
entity_dec="${amp}#0*8212;"
entity_hex="${amp}#[xX]0*2014;"
pattern="${emdash}|${entity_named}|${entity_dec}|${entity_hex}"
failed=0

while IFS= read -r -d '' file; do
    case "$file" in
        uv.lock) continue ;;
        docs/screenshots/*/*.html) continue ;;
    esac
    [ -f "$file" ] || continue
    if grep -n -E -i -- "$pattern" "$file" 2>/dev/null; then
        echo "  ^ em-dash in $file" >&2
        failed=1
    fi
done < <(git ls-files -z --cached --others --exclude-standard)

if [ "$failed" -ne 0 ]; then
    echo "em-dashes found; use a comma, a colon, parentheses or a period" >&2
    exit 1
fi
echo "prose check passed: no em-dashes"
