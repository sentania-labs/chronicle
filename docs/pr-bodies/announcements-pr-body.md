# Announcements: a per-draft field for suggested social posts

## What changes for someone running it

A draft gains a fourth thing it can carry alongside title, body, and
frontmatter: suggested social announcements, one entry each for X,
Bluesky, and LinkedIn. It shows up as a new "Announcements" sidebar panel
on the edit page, three textareas with a Copy button each, collapsed by
default like the other panels. Nothing else about the running service
changes: no new environment variable, no new endpoint, no new background
process, no outbound call anywhere. Every existing draft and version JSON
on disk loads unchanged (the field defaults to an empty mapping), and a
backup taken before this release restores the same way.

## Blast radius

Small and additive. The only paths touched are `PUT /v1/drafts/{id}`
(reused, not replaced) and the edit page's own render and save. Every
other route, the publish and watch background threads, digest, and
reconciliation are untouched: none of them can see this field, because
`convert.py` never reads it (proven by a test that converts the same
draft with and without announcements and asserts byte-identical output).
A consumer that only ever wrote `frontmatter` and `body` before this
release keeps writing them exactly the same way; omitting `announcements`
from a `PUT` body keeps whatever the draft already has.

## Recovery

Revert the PR. Drafts saved with announcements after this lands keep an
extra `announcements` key that older code (pre-revert) simply ignores;
nothing about reverting requires a data migration in either direction.

## Why the save path was reused, not a new endpoint

`PUT /v1/drafts/{id}` is already the only place that bumps a draft's
version, checks `base_version` for a conflict, and refuses a write while
a publish run or PR is open. A separate `/announcements` endpoint would
have had to repeat all three of those, and the first time one of them
changed, the two paths would drift out of step with each other. Omitting
`announcements` keeps the mapping; sending one, `{}` included, replaces
it in full. The edit page's form always submits all three textareas, so
a UI save is always a full replace, with a blank or whitespace-only
textarea meaning that channel is absent rather than present and empty.

## The published-URL prefix decision

The panel shows the published post's URL once `draft.published` is set,
as the exact site-relative path `convert.post_url` wrote (for example
`/2026/08/announcing-things/`). Chronicle has no configured public base
URL for the blog anywhere in its settings today, and this feature is not
reason enough to add an environment variable every deployment would then
have to set correctly. The path is shown as-is.

## Not in scope

No posting to any social network, no credentials for one, no outbound
call of any kind. This is a text field with a Copy button; a human does
the actual posting by hand.

## What was observed live

Full write-up and reasoning in
`docs/screenshots/announcements/README.md`. Short version: brought up an
isolated compose stack (project `chronicle-lanek`, api on
`127.0.0.1:8089`, preview on `127.0.0.1:8099`), minted a token inside it,
created a draft over the API, `PUT` all three announcement channels,
opened the edit page in headless Chrome at 1280px, confirmed the panel
rendered the saved text, clicked Copy and confirmed the "Copied"
confirmation, edited one channel's text in the browser, clicked Save, and
confirmed over `GET /v1/drafts/{id}` (not just the rendered page) that
the edited text came back with `version_no` bumped from 1 to 2. A
baseline screenshot from `origin/main` (af14f39), built as a standalone
container in a throwaway worktree and removed afterward, confirms the
Announcements panel does not exist there at all.

Screenshots: `docs/screenshots/announcements/before-no-panel.png`,
`after-panel-filled.png`, `after-copy-confirmed.png`. The lanek compose
stack, its volumes, and the throwaway worktree were all torn down after
the check; nothing from this pass was left running.

## Adversarial review findings and disposition

Full write-up in `docs/pr-bodies/announcements-review.md`. One real
finding: the conflict page's reload form (shown on a stale `base_version`
409) rendered the frontmatter fields and the body but no announcement
fields at all, so its normal resubmit path would have silently cleared
whatever a draft's announcements held. Fixed with hidden inputs carrying
the current draft's announcements through that form
(`ui_templates._announcement_hidden_fields`), with a regression test.
Every other scenario tried (an unknown channel key, a non-string value, a
huge string, HTML or quotes in the text on the edit page, a PUT omitting
the field, a stale base_version, the convert output, a pre-field backup
restore, the copy button with no clipboard API) was already handled
correctly and is covered by an existing test; see the review file for the
detail on each.

## One noted gap, not fixed here (out of scope for this pass)

`tests/editor.test.mjs`'s fake DOM harness stubs a form's `.elements` as
a plain array with no real form-association behaviour, so the
announcement textareas joining the local-backup Restore flow through the
standard `form="edit-form"` attribute (the same mechanism the
frontmatter panel already uses) is not independently unit tested at that
layer; a test against the stub would only prove the stub, not the real
browser behaviour. The server-rendered-HTML test that the three fields
carry `form="edit-form"` is what actually holds this contract. See
`docs/pr-bodies/announcements-review.md` for the full reasoning.
