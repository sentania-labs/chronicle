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
# anyone here. Nothing else is skipped: a captured screenshot fragment
# under docs/screenshots/ used to get a pass here on the theory that it was
# a byte-for-byte HTTP response, not authored prose. That let a real
# rendering defect (the C5 "last run" em-dash) sit undetected in a
# committed file. Once a bug like that is fixed, the fragment documenting
# it is edited to match, same as any other tracked file, so the exemption
# bought nothing but a blind spot.
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
