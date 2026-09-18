#!/usr/bin/env bash
# Fresh-volume compose smoke test.
#
# Proves the whole preview path against brand-new named volumes, the exact
# shape that exposed the /data/preview permission-denied defect: `down -v`
# then `up -d --build`, so `data` and `preview` are re-initialised from the
# images (Dockerfile's chown), not inherited from a previous run. Once the
# stack is up it digests a tiny fixture git repo built in a temp dir, finds
# the published record digest already recorded for that post (ADR 017),
# revises it to claim a preview, and confirms the built page answers 200
# through the preview container.
#
# Usage: make compose-smoke
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

API="${API:-http://127.0.0.1:8080}"
PREVIEW="${PREVIEW:-http://127.0.0.1:8090}"

# A project name distinct from docker-compose.yml's own `name: chronicle`:
# otherwise this script's `down -v` would tear down and delete the volumes
# of a developer's own `make compose-up` stack, drafts, tokens, and instance
# key included, if one happened to be running under the same project name.
compose() {
    docker compose -p chronicle-smoke \
        -f docker-compose.yml -f ci/compose-smoke.override.yml "$@"
}

step() { printf '\n=== %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
ok()   { printf 'ok: %s\n' "$1"; }

status_of() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
field()     { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"; }

wait_for_status() {
    local want="$1" url="$2" tries="${3:-60}" got=""
    for _ in $(seq 1 "$tries"); do
        got="$(status_of "$url" || true)"
        [ "$got" = "$want" ] && return 0
        sleep 1
    done
    fail "$url answered $got, expected $want"
}

fixture_dir="$(mktemp -d)"
export CHRONICLE_SMOKE_FIXTURE_DIR="$fixture_dir"

cleanup() {
    compose down -v || true
    rm -rf "$fixture_dir"
}
trap cleanup EXIT

step "build a tiny fixture blog repo"
mkdir -p "$fixture_dir/content/posts" "$fixture_dir/layouts/_default"
cat > "$fixture_dir/hugo.toml" <<'EOF'
baseURL = "/"
title = "Fixture"
EOF
cat > "$fixture_dir/layouts/_default/single.html" <<'EOF'
<html><body><h1>{{ .Title }}</h1>{{ .Content }}</body></html>
EOF
cat > "$fixture_dir/layouts/_default/list.html" <<'EOF'
<html><body>list</body></html>
EOF
cat > "$fixture_dir/content/posts/hello-world.md" <<'EOF'
---
title: Hello World
date: 2026-01-01
url: /posts/hello-world/
---
A tiny fixture post for the compose smoke test.
EOF
git -C "$fixture_dir" init -q -b main
git -C "$fixture_dir" -c user.email=smoke@example.com -c user.name=smoke add -A
git -C "$fixture_dir" -c user.email=smoke@example.com -c user.name=smoke commit -q -m 'fixture post'
# The api container reads this bind mount as uid 1000, which will not match
# whatever uid built the fixture on the host, so it has to be world-readable
# for the read-only mount to actually be readable.
chmod -R a+rX "$fixture_dir"
ok "fixture repo built at $fixture_dir"

step "fresh volumes: down -v, then up -d --build"
compose down -v || true
compose up -d --build
ok "stack started against fresh named volumes"

step "wait for health"
wait_for_status 200 "$API/healthz"
wait_for_status 200 "$PREVIEW/healthz"
wait_for_status 200 "$API/readyz"
ok "api, builder and preview are up"

step "digest the fixture repo"
compose exec -T api chronicle digest
ok "digest ran against CHRONICLE_DIGEST_REPO_URL=/fixture"

step "issue a consumer token"
token="$(compose exec -T api chronicle token issue smoke | tr -d '\r\n')"
[ -n "$token" ] || fail "token issue produced no output"
AUTH=(-H "Authorization: Bearer $token")
ok "issued a consumer token"

step "confirm the fixture post landed in the catalog"
posts="$(mktemp -p "$fixture_dir")"
code="$(curl -sS -o "$posts" -w '%{http_code}' "${AUTH[@]}" "$API/v1/posts")"
[ "$code" = "200" ] || { cat "$posts"; fail "listing posts returned $code, expected 200"; }
python3 -c 'import json,sys
posts = json.load(open(sys.argv[1]))["posts"]
raise SystemExit(0 if any(p["slug"] == "hello-world" for p in posts) else 1)' "$posts" \
    || fail "digest did not record the fixture post hello-world"
ok "digest recorded hello-world"

step "find the published record digest created for hello-world"
# ADR 017: digest lands a published post as a working record directly, so
# there is already a draft at status "published" for hello-world here.
# Importing it again with from_post would collide (409 image_dir_collision)
# with the record digest itself just created, so this proves the same
# record digest wrote rather than a duplicate import of it.
published="$(mktemp -p "$fixture_dir")"
code="$(curl -sS -o "$published" -w '%{http_code}' "${AUTH[@]}" "$API/v1/drafts?status=published")"
[ "$code" = "200" ] || { cat "$published"; fail "listing published drafts returned $code, expected 200"; }
draft_id="$(python3 -c 'import json,sys
drafts = json.load(open(sys.argv[1]))["drafts"]
match = next((d for d in drafts if d["slug"] == "hello-world"), None)
print(match["id"] if match else "")' "$published")"
[ -n "$draft_id" ] || fail "digest did not leave a published draft for hello-world"
ok "found published draft $draft_id for hello-world"

step "revise the published record so it can be previewed"
# The preview action only exists from drafting/in_review/previewed, never
# from published (chronicle/api/transitions.py), so a save is the same step
# a real edit would take: it moves the record to drafting (the `revise`
# transition) without touching what digest already recorded on `published`.
draft="$(mktemp -p "$fixture_dir")"
curl -sS -o "$draft" "${AUTH[@]}" "$API/v1/drafts/$draft_id"
base_version="$(field "$draft" version_no)"
save_body="$(python3 -c 'import json,sys
draft = json.load(open(sys.argv[1]))
print(json.dumps({
    "base_version": draft["version_no"],
    "frontmatter": draft["frontmatter"],
    "body": draft["body"],
    "message": "smoke: revise to claim a preview",
}))' "$draft")"
saved="$(mktemp -p "$fixture_dir")"
code="$(curl -sS -o "$saved" -w '%{http_code}' -X PUT "$API/v1/drafts/$draft_id" \
    "${AUTH[@]}" -H 'Content-Type: application/json' -d "$save_body")"
[ "$code" = "200" ] || { cat "$saved"; fail "revising the published draft returned $code, expected 200"; }
status="$(field "$saved" status)"
[ "$status" = "drafting" ] || fail "revise left status $status, expected drafting"
ok "revised draft $draft_id ($base_version -> drafting)"

step "claim a preview"
action="$(mktemp -p "$fixture_dir")"
code="$(curl -sS -o "$action" -w '%{http_code}' -X POST \
    "$API/v1/drafts/$draft_id/actions/preview" "${AUTH[@]}")"
[ "$code" = "200" ] || { cat "$action"; fail "the preview action returned $code, expected 200"; }
run_id="$(field "$action" run_id)"
[ -n "$run_id" ] && [ "$run_id" != "None" ] || fail "the preview action did not queue a run"
ok "queued preview run $run_id"

step "wait for the builder to finish the run"
status_file="$(mktemp -p "$fixture_dir")"
run_status=""
for _ in $(seq 1 60); do
    curl -sS -o "$status_file" "${AUTH[@]}" "$API/v1/runs/$run_id"
    run_status="$(field "$status_file" status)"
    [ "$run_status" = "succeeded" ] && break
    [ "$run_status" = "failed" ] && { cat "$status_file"; fail "the preview run failed"; }
    sleep 1
done
[ "$run_status" = "succeeded" ] || { cat "$status_file"; fail "the preview run did not finish in time"; }
ok "run $run_id succeeded"

step "the built page is reachable through the preview container"
wait_for_status 200 "$PREVIEW/preview/hello-world/"
ok "the preview page returned 200"

printf '\ncompose smoke passed\n'
