# In-lane adversarial review: legacy preview post link (issue 61)

Reviewed the diff against `origin/main` (`git diff origin/main...HEAD`) as a
reviewer trying to break it, before any Codex round. Checked each of the
angles the dispatch named, plus a few that fell out of reading the code.

## Checked, no finding

- **A hand-set frontmatter `url` with a scheme, host, or dot segments
  reaching an href.** `resolve_run_post_url`'s derivation branch still ends
  in `preview_post_url`, the same function every existing surface already
  used, which strips scheme, host, and both literal and percent-encoded dot
  segments (ADR 015/022). The derivation adds nothing new past that
  boundary: it only ever changes what date and slug feed into `post_url`'s
  own logic before the result reaches `preview_post_url`. The "stored
  `post_url` wins outright" branch returns exactly what a prior run wrote,
  the same trust boundary the pre-issue-61 code already had.
- **Escaping.** Every surface that renders a derived link (`_draft_card`,
  `_post_info`, `preview_list_page`, `_success_result_html`) already wraps
  the value in `escape()` before writing it into an `href`; nothing here
  bypasses that.
- **A run timestamp in UTC read as the date by mistake.** This is the
  failure mode the fix exists to close. `_run_build_date` reads `.date()`
  off the stamp's own recorded offset, never converted to `PUBLISH_TZ`,
  since a pre-v0.3.3 build's date came from the builder process's own local
  zone (in practice UTC, since the images set no TZ), which the stored
  offset already records;
  `test_resolve_run_post_url_uses_the_runs_own_build_date_when_the_draft_has_none`
  covers a build stamped just past a UTC month boundary and asserts the
  recorded-offset date wins, not a Chicago-converted one.
  - **Adolin verification: build date converted to PUBLISH_TZ does not match
    what post_date used at build time; fixed to use the stamp's recorded
    offset.**
- **A malformed run timestamp.** `_run_build_date` catches `ValueError` from
  `datetime.fromisoformat` and returns None, which `resolve_run_post_url`
  treats as "no date derivable" and falls back to the root. Covered by
  `test_resolve_run_post_url_is_none_when_no_date_can_be_derived_at_all`.
- **A draft deleted after the run.** Every board/editor/previews-list call
  site already holds the `Draft` object it loaded earlier in the same
  request, so there is no second read that could race a deletion. The one
  route that reads a run on its own (`GET /runs/{id}`) fetches the draft
  fresh and wraps it in `try/except ApiError`, falling back to `draft=None`
  (root fallback) rather than 404ing the whole page over a missing draft.
- **A draft whose slug changed.** `Draft.slug` is pinned once
  (`Store._pin_slug`) and never changes afterward (AGENTS.md), so there is
  no live code path where a draft's slug differs between when a run was
  recorded and when it is read back. `resolve_run_post_url` always reads
  `draft.slug` off the current object, never anything cached on the run, so
  even a hypothetical future change here would self-correct rather than go
  stale.
- **Extra store reads on the board page per card.** `_board_row` already
  received a `Draft` object from `drafts_board`'s single per-page loop; the
  new `convert.resolve_run_post_url(last_preview, draft)` call adds no I/O,
  since both arguments are already in memory. The only new read anywhere in
  this diff is one `get_draft` call in the run-log route, once per page
  view, not per row.
- **The publish PR body.** `_pr_body` is unchanged; only what feeds its
  `post_url` argument moved from a direct dict read to
  `convert.resolve_run_post_url(preview_run, draft)`. A publish or
  unpublish run's own result (branch, PR number, commit sha) has no
  `preview_url` key, so the derivation returns None immediately for those
  kinds and the PR body's existing `preview_url`-only fallback line is what
  renders, same as before.
- **A blank stored `post_url`.** The old per-route helpers returned `""`
  unchanged (`isinstance` only, no truthiness check); every caller's own
  `post_url or preview_url` fallback already treated that as "use the
  root." The new function returns None for a blank string instead, which
  every caller's fallback logic treats identically. Not a behavior change.
- **Never writes anything back.** `resolve_run_post_url` only ever reads
  `run.result` and builds a local `dict(frontmatter)` copy when deriving a
  date; nothing is reassigned onto the `Run` or `Draft` objects, and no
  store write is ever called from this path.
  `test_resolve_run_post_url_never_modifies_the_stored_result` asserts the
  passed-in result dict is byte-identical after the call, and the
  `/v1/drafts/{id}/status` and editor-page live checks (below) each read
  the stored run back afterward and assert `post_url` is still absent.

## Disposition

No findings required a code change. The tests originally written for
ADR 022's "falls back to the root when the run predates `post_url`"
behavior asserted the specific thing issue 61 changes; they were rewritten
to assert the new derive-first behavior instead of being left to fail
(`tests/test_ui.py`, `tests/test_api_drafts.py`, `tests/test_publisher.py`),
and one straightforward genuine-fallback test was added at each layer for
the case derivation still cannot produce a link (no derivable date at all,
or no pinned slug).
