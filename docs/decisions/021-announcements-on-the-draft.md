# 021: suggested announcements live on the draft, not in the post

- **Status:** accepted
- **Date:** 2026-09-20

## Context

The ghostwriter writes a post and, in the same pass, knows what it should say
on social media. Today it has nowhere to put that. Frontmatter is a closed
Hugo allowlist (ADR 007), so a `x_announcement` key is refused, and the body
is the post: anything written there publishes to the blog. The result is that
the suggested wording lives in a chat log, which is the one place Scott
cannot find it when he is actually about to post the link.

## Decision

A draft carries an `announcements` mapping: at most three entries, keyed
exactly `x`, `bluesky`, `linkedin`. Each value is plain text. A channel may be
absent; no other key is accepted, and no value type other than a string is.
`Store.check_announcements` is the one check, and it raises the same 422
`ApiError` envelope every other domain refusal uses.

Three properties follow from where the field sits:

- **Never published.** `convert.convert` reads the frontmatter allowlist and
  the body, and neither can reach this field. A test asserts a draft with
  announcements converts to output byte-identical to the same draft without
  them.
- **Versioned.** `Version` carries the same field, so the history of a post's
  announcements moves with the history of its text, and a restore of an older
  version restores what that version said.
- **Backwards compatible.** The field defaults to an empty mapping, so a
  record written before it existed (on disk, or inside a backup bundle made
  before this release, ADR 016) loads unchanged. Rolling back is safe in the
  same way: older code ignores the extra key.

`PUT /v1/drafts/{id}` reuses the save path rather than getting an endpoint of
its own. Save is already the only place that bumps the version, checks
`base_version` for a conflict, and refuses a write while a publish run or PR
is in flight. A separate `/announcements` endpoint would have to repeat all
three, and the first time one of them changed, the two paths would drift.
Omitting `announcements` from a `PUT` body keeps what the draft has; sending a
mapping, `{}` included, replaces it in full. The edit page's form always
submits all three textareas, so a UI save is always a full replacement, with a
blank or whitespace-only textarea meaning the channel is absent rather than
present and empty.

The edit page shows the three fields in a collapsed sidebar panel with a Copy
button each. They are plain form fields: no markdown rendering, no `innerHTML`
anywhere on this path. The text is escaped server-side like every other value
a UI template interpolates, because another consumer token can write it and
Scott's browser is what renders it (the same reasoning as the editor's
preview sanitiser).

No length limit. The API's existing request body size guard is the only
ceiling, and it is the same one the draft body lives under; a per-channel
character count would encode each network's current limit into Chronicle and
be wrong the next time one of them changes it.

The published post's URL is shown in the panel as the site-relative path
`convert.post_url` wrote. Chronicle has no configured public base URL for the
blog anywhere in its settings, and this feature is not a good enough reason to
add an environment variable that every deployment would then have to set
correctly.

## Not in scope

Chronicle does not post to any social network. It holds no credentials for
one, makes no outbound call on this path, and has no scheduler for one. This
is a text field with a copy button; the human does the posting.

## Consequences

- Every draft record and version record grows an `announcements` key. Empty
  for everything written before this change, and for everything nobody fills
  in.
- A new channel is a code change here and in `ui_templates`'s label list, not
  a caller's choice of key. That is the point: the edit page renders a fixed
  set of fields, and an open mapping would render nothing for a key it had
  never heard of.
- Announcement text is not reconciled against anything on main, because
  nothing on main ever carries it.

## Amendment (issue #71): the published link is filled in on merge

The "no configured public base URL" premise above was cheaper to lift than
this ADR assumed: digest already runs `hugo config` against the site's
production environment, and that output carries `baseURL`. Digest now keeps it
(`HugoConventions.baseurl`, stored in `data/state/toolchain.json`), and only
an absolute http(s) value counts; still no new setting.

When the watcher observes a publish PR's merge, after the draft is
`published`, it joins that base with `published.url` and asks
`Store.fill_announcement_links` to put the link in (`announce.fill_links`):

- every `{link}` becomes the URL; an announcement without one gets the URL on
  its own last line; an empty announcement stays empty;
- the link the last fill wrote is kept on the draft (`announcement_link`), so
  a later publish at a different url swaps it instead of adding a second;
- a second observation of the same merge changes nothing.

The write is a new version authored `chronicle` with the same frontmatter and
body. It never goes through `save_draft`, never changes the status, and a
failure is logged without blocking the merge. A site with no usable `baseURL`
leaves the announcements alone, and the panel keeps showing the site-relative
path. Chronicle still posts nothing anywhere.
