# Lane F, part A: four status words, and no personal name in the product

Branch `feat/ui-four-words`. Presentation layer only: `transitions.py`, API
status values, `?status=` values and stored records are unchanged.

## 1. Four words (closes #32 and #37)

A reader sees Draft, In review, Published, Rejected. `ui_status.STATUS_LABELS`
maps all eight statuses onto them (previewed, revision_requested, unpublished
are Draft; approved is In review). `tests/test_ui_status.py` still fails until a
new state-machine status is given a label, and now also asserts the label set
is exactly the four words.

The finer facts are detail badges beside the status badge (`status_details`):

| Fact | Where it shows |
| --- | --- |
| Came back from review (warn tone) | board card, editor bar |
| Preview built / Preview out of date | board (built only), editor (knows if it is current) |
| Was published (`unpublished`) | board, editor |
| Approved (`approved`) | board, editor |
| Publishing / Publish PR open / Unpublish PR open | editor only (uses the flags it already computes) |

The editor's detail sits in its own `data-refresh` region (`#status-detail`)
so a save or action swaps it like the pill.

### How the came-back-from-review signal survives a preview (#37)

The status was the only carrier, so a preview's staged `revise` erased it. It
is now read from the feedback log, which a revise never touches
(`ui_status.came_back_from_review`): the draft came back if its status is
`revision_requested`, or if it is `drafting` and the newest review verdict in
its feedback log (`request_revision` or `reject`) is `request_revision`. A
later `reject` settles it too (reject then restore does not show it). The warn
tone moved from the status badge to this detail badge. The board reads each
row's feedback log (`_board_row`); nothing is stored and no record changes.

**Updated in the fix round (2026-09-19, see `docs/notes/lane-f-part-b.md`):**
this original write-up said the badge also fired for `previewed`, on the same
"newest verdict" read. That was wrong: `previewed` is reachable both straight
from a fresh `revise` (still unanswered, the case this was meant to catch)
and from a full resubmit round trip (`revise`, `preview`, `submit` back to
`in_review`, a further preview success lands back on `previewed`) once the
author has already answered the request, and `submit`/`approve` write no
feedback entry, so the log cannot order the two. The reviewer caught this
live: request_revision, preview, submit, preview success still showed the
badge. The fix stops the badge firing for `previewed` at all, so a draft that
is previewed once, straight out of `revision_requested`, and never
resubmitted, now loses the badge as soon as that first preview succeeds,
before the author submits it again. That known gap is accepted; fixing it
properly needs the store to order a resubmit against a request, which is out
of this lane's files.

Known limit, unchanged by the fix round: a draft with a `published` record
that is back in `drafting` never shows the badge from the log, because the
log cannot order an old request against a publish (no timestamp for the
publish is stored, and submit and approve leave no feedback entry). It shows
only while the status is `revision_requested`. A publish, revise, sent-back
post loses the badge. Fixing that needs a per-draft event lookup or a
recorded submit, both in `store.py`.

### The filter

The dropdown lists All, Draft, In review, Published, Rejected. Each option's
value is the raw statuses it covers as a comma list, for example Draft is
`drafting,revision_requested,previewed,unpublished` (DRAFT_STATUSES order) and
In review is `in_review,approved`. `?status=` on the board accepts one raw
status exactly as before or a comma list; nothing on the API side takes a list.
A hand-typed single raw status (`?status=previewed`) still filters correctly and
shows as its own selected option with its raw text, never as the wider word.

The admin status page had the same duplicate problem (it printed `Draft` for
several rows), so it now sums per-status counts under the four words
(`label_counts`).

## 2. Scott's name out of the product (closes #12)

- `(Scott only)` on reserved-action buttons now reads `(editor only)`.
- `UI_COMMIT_AUTHOR` is `editor`. Every UI write (version author, git commit
  author, claim holder, event actor) says `editor`. Nothing reads the old value
  back: a claim is set without comparing holders, so existing records that say
  `scott` stay as history and cause no lockout. New history in the same repo
  will read `editor`.
- `lint.py` docstring no longer cites `/home/scott/...`; it says the module
  was ported from the vault's blog lint script.
- `docs/backup.md` example records use `editor`.
- Comments in my files that named Scott casually (`deps.py`, `ui_deps.py`,
  `ui_templates.py`, `ui.js`) now say the editor. ADRs, the spec and
  `docs/screenshots/lattice-skin/README.md` are untouched. **Missed at the
  time, fixed in the fix round (2026-09-19):** `routes/ui.py`'s own module
  docstring still stated the author contract as "authored `scott`", not a
  casual mention; the reviewer caught it alongside the same stale statement
  in `AGENTS.md` and the spec. See `docs/notes/lane-f-part-b.md`.

## Where the instructions were wrong or incomplete

- `previewed` is also reachable from `in_review` (a successful preview of a
  draft in review moves it to `previewed`; `RUN_OUTCOME_TRANSITIONS`). Mapping
  `previewed` to Draft therefore shows Draft for a post that is under review
  once anyone previews it. That is what the state machine says (it treats
  `previewed` as draft-family and stages a submit before approve), so I
  followed the mapping as instructed, but it is a real ambiguity for the
  later status-collapse decision.
- The instruction said the UI author lands "into an operator's own blog repo
  history". It lands in Chronicle's internal `data/repo/` git history, which is
  never pushed (AGENTS.md); the published blog PR is authored through the
  GitHub App. The naming problem is real but its blast radius is the internal
  history and the record fields, not the blog repo.
- `?status=` values did not have to change but had to gain a comma list form
  for the dropdown to be usable; single values behave as before.
- ADR 019 still says "Needs revision is `warn`". Left alone per instruction; it
  is now stale (the warn tone belongs to the `Came back from review` detail).
- `docs/screenshots/editor/README.md` mentions Previewed as a status; it is
  evidence of a past run and was not touched.
- `deps.py` and `ui_deps.py` were not on the file list; I edited only their
  docstrings, which named the old identity.

## Broken in the other lane's files

Nothing broken found. Residual naming only: `store.py` (around lines 1800,
1818, 1834, 1951), `convert.py:164`, `digest.py`, `github_client.py:263` have
comments that say "Scott". Comments only, not product output; left alone.

## Seen working

Ran the api against a scratch data dir with one draft in each status and looked
at `/content/drafts` and a sent-back draft's editor in a browser: four word
badges, the amber Came back from review badge, Was published, Approved and
Preview built beside them, and the filter listing five options. The scratch
directory is deleted.
