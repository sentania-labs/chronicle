# 017: digest holds published posts directly, and Chronicle's content conventions come from Hugo's own config

- **Status:** accepted
- **Date:** 2026-09-18

## Context

Two separate frictions turned out to be one design.

**The import chore.** Digest already discovers every post on main and
writes a `Post` record for it (`Store.apply_digest`), but that record is
Chronicle's internal mirror only: nothing user-facing reads or edits it.
Turning a post into something Chronicle can work with today requires
`Import as draft`, which creates a brand-new `Draft` at status `drafting`
(the model's default). At Scott's scale, 347 existing posts, that is 347
manual clicks before Chronicle considers anything tracked, and it recurs:
`reconcile.run`'s `post_on_main_without_published_draft` flag looks for a
PUBLISHED draft tracking each post, `_already_flagged` only consults
unresolved flags, and an imported draft sits at `drafting`, so the flag's
condition is still true after every single import. `ignore` does not
suppress it either, since only `content_drift` flags carry a suppression
field. Left alone, that is 347 flags re-raised on every hourly reconcile
pass, forever.

**The hardcoded directory.** `chronicle/api/digest.py`'s
`POSTS_GLOB_DIRS = ("content/posts",)` and `convert.py`'s matching
`POSTS_DIR`/`STATIC_IMAGES_DIR` constants encode a directory convention
that happens to be true of Scott's site today, but isn't what actually
governs it. His Blowfish theme classifies archive content by frontmatter
`type` through `params.mainSections`, not by directory; `hugo config
--format json` against his site reports `contentdir: "content"`,
`params.mainsections: ["post"]` (Hugo's JSON output lowercases keys),
`staticdir: ["static"]`, and `taxonomies: {category: categories, series:
series, tag: tags}`. A directory constant that happens to match today
would silently stop matching the day a post-like section moves, while
Hugo's own answer would not.

Both problems point at the same fix: digest should ask Hugo what a post
is and where things live, and once it can tell a post from a page bundle
correctly, there is no reason left to make Scott import each one by hand.

## Decision

### The `Post` record and the working record are two different things, and both stay

`Store.apply_digest` keeps writing `Post` records exactly as it does today:
one JSON file per slug under `data/repo/posts/`, idempotent, no status, no
edit surface. This is Chronicle's internal mirror of what main's own git
history says exists, and every drift check in `reconcile.py` depends on
comparing against it. It is not renamed, not merged into the working
record, and nothing about it changes in this round.

The working record (`Draft` in code, "Posts" in the UI, see below) is the
user-facing surface: what Scott opens, edits, and re-publishes through the
normal lifecycle. Today the only way a `Post` produces a working record is
`Import as draft`, landing at `drafting`. From this round on, digest
creates or updates a working record directly, at status `published`, with
its `published` dict populated from what the digest itself observed
(`post_path`, `url`, `date`, and `post_blob_sha` from the git blob sha
digest already reads; `branch`, `pr_number`, and `pr_url` stay unset,
since no Chronicle-opened PR produced this record). `Import as draft`
still exists for the case it was always for: creating a *new* editable copy
of something, which now only comes up when someone wants to fork or
reimport deliberately, not as the only path from "exists on main" to
"exists in Chronicle".

This is why `post_on_main_without_published_draft` stops recurring:
digest's own run is what puts a PUBLISHED record in place, so the flag's
condition (a post with no published record tracking it) is false the
moment digest has seen the post at all, with nobody clicking anything.

This is additive and idempotent, same as `apply_digest` today: a post
digest has already recorded gets its `published` dict left alone unless
main's content actually changed underneath it (that's `content_drift`'s
job, unchanged by this ADR), and a post whose working record a human has
since edited into `drafting` or anywhere else is never dragged back to
`published` by a later digest run. Backfilling the 347 existing posts is
one digest run against the live instance; nothing here requires a
migration script or touches an existing draft, flag, or post record
destructively.

### The rename is user-facing only, this round

"Drafts" becomes "Posts" everywhere Scott reads it: navigation, page
titles, headings, UI copy. It does not rename `data/repo/drafts/` on disk,
the `Draft` model or Python identifiers, or the `/v1/drafts` /
`/admin`-side API routes. The instance is live with real data and open PRs
referencing today's paths; a storage or route rename is a migration with a
blast radius (URLs bookmarked, any external reference to a draft ID's
path) Scott has not agreed to. Deferred deliberately, not forgotten, and
namable as its own issue if he wants it later.

The rename fits because Scott intends to hold other content types (an
About page, a Triathlon Training page) in the same place: once most
working records start life already `published` by digest rather than
hand-drafted, "Drafts" stops describing most of what's in there.

### Content type travels on the record, sourced from frontmatter `type`

`type` is already in `FRONTMATTER_ALLOWLIST` (`models.py`) and already
round-trips through save and publish; this ADR adds nothing to the model.
What changes is that digest now walks wider than `content/posts` (see
below) and needs to classify what it finds: a post (`type: post`, or
whatever `params.mainsections` names) gets a normal working record: a page
bundle (`about/index.md`, `training/index.md`, `type: page`) is handled
the same way a post's page bundle already is (`index.md` inside a
directory), since Chronicle's page-bundle handling was never specific to
posts, only untested against anything else.

### Hugo config is read once per digest, from the environment Chronicle names explicitly

`digest.py` invokes `hugo config --format json --environment
<CHRONICLE_HUGO_ENVIRONMENT, default "production">` against the freshly
cloned or fetched `data/site/` working tree. Scott's own site's
content-shaped keys (`contentdir`, `params.mainsections`, `staticdir`,
`taxonomies`) are identical across `_default`, `devel`, and `production`;
only `baseURL` differs. `production` is the default because it is the
environment whose config is what main's own live build actually uses;
`CHRONICLE_HUGO_ENVIRONMENT` exists for a site where that is not true.
Chronicle states which environment it read in the same toolchain state the
admin status page already surfaces (`admin.write_toolchain`), so this is
never a silent choice.

All four are computed together, in one `HugoConventions` value
(`digest.read_hugo_conventions`), so a digest run and the admin status
page both see the same answer for the same run. Three of the four are
wired into behavior this round; `taxonomies` is computed and reported
only, deliberately, since Chronicle never rejects or rewrites on it:

- `contentdir` replaces `POSTS_GLOB_DIRS`'s hardcoded `"content/posts"` as
  digest's walk root (still walked recursively for bundles; `mainsections`
  decides what counts as a post from there, so the walk itself covers
  everything under `contentdir`, not just what happens to sit under a
  `posts/` subdirectory). Wired into `digest.discover_posts` this round.
  It is also, as of issue #18, wired into `convert.post_path`: a
  digest-created record's republish now matches its source path against
  the site's real `contentdir` (read from the last digest's state,
  `digest.read_content_dir_from_state`, the same shape as `staticdir`
  below), not the hardcoded `content/posts` prefix. A site whose archive
  moves outside `content/posts` used to fall through that check and land
  a duplicate file on republish instead of updating the one already on
  main; that gap is closed, not deferred.
- `params.mainsections` decides which walked files are posts (archive
  content) versus a page bundle or anything else. Hugo's JSON output
  lowercases every key, `mainsections` included; this is read that way,
  not as `mainSections`. Wired into `digest.discover_posts` this round.
- `staticdir` is where images live on this site (`static` on Scott's own
  site, matching `convert.py`'s hardcoded `static/images` prefix exactly,
  so nothing observable changes for him). Derived and surfaced on the
  admin status page this round, and wired into `convert.py`,
  `publisher.py`, `builder/runner.py`, and `cli.py` (`digest.
  read_static_dir_from_state`), each reading the last digest's state
  rather than a live `HugoConventions` value.
- `taxonomies` validates that the `categories`/`tags`/`series` keys
  Chronicle's frontmatter allowlist already carries are taxonomies this
  site's config actually defines, rather than assuming English defaults
  that happen to match Hugo's own. Computed and reported (which allowlist
  keys have no matching taxonomy) on the admin status page this round;
  nothing rejects or rewrites a draft's frontmatter over a mismatch, the
  same "flags, not corrections" posture as everything else here.

Stays convention, not derived, each documented as such in the code that
uses it:

- **The `/YYYY/MM/slug/` post URL default** (`convert.post_url`'s
  fallback when a post has no `url` of its own). Scott's site has
  `permalinks: null`, so there is nothing in Hugo's config to derive this
  from; it remains an observed pattern read off the real posts, and an
  author who wants something else sets `url` by hand, exactly as today.
- **The `<YYYY-MM-DD>-<slug>.md` filename** for a new post
  (`convert.post_filename`). Hugo does not govern content filenames at
  all; there is no config key that could replace this.

### Failure mode: fall back, never fail the digest, and say so where it's seen

If the `hugo config` call fails (non-zero exit, timeout, missing binary),
or its stdout does not parse as the JSON shape expected, digest falls back
to today's hardcoded conventions (`content/posts` walk root, `static/
images` destination, no taxonomy validation) rather than failing the run.
This is reported, not swallowed: it lands in the same toolchain/digest
status the admin page already reads (`admin.write_toolchain` /
`admin.write_digest_status`), the one place an operator (Scott) actually
looks after a digest, as a field an admin can see fell back and why,
distinct from the normal case where Hugo answered.

### Hugo now runs in the api image too

AGENTS.md stated that the publisher, watcher, and reconciler need no Hugo
toolchain, so Hugo lives only in the `builder` stage. That was true until
now: `digest.py` runs in the api process, and deriving conventions this
way means the api process runs `hugo config` itself. The `Dockerfile`'s
`api` stage now installs the same pinned Hugo the `builder` stage
installs, reusing the existing `HUGO_VERSION` ARG and the same install
block, so there is exactly one pinned Hugo version in the file, not two
that could drift apart. AGENTS.md's sentence claiming the api needs no
Hugo toolchain is corrected to say why it now does.

This is a narrow addition, not a reversal of the underlying reasoning in
`chronicle/api/background.py`'s ADR 013: the publisher, watcher, and
reconciler still never invoke Hugo to *build* anything (no template
rendering, no theme, no `hugo` build command); the only new call is
`hugo config`, which reads configuration and touches no content. The
builder remains the only place a Hugo *build* runs.

This does not touch `digest.py`'s existing `hugo_version` toolchain-drift
field, which continues to come from parsing the target repo's own GitHub
Actions workflow (`parse_toolchain`/`_hugo_version_from_workflow`), never
from the locally installed binary. Conflating the two would make drift
detection compare Chronicle's own pinned Hugo against main's workflow
instead of comparing main's workflow against nothing meaningful; the
config-reading call and the drift-reporting field stay unrelated uses of
two different Hugo installations that happen to be pinned to the same
version by construction (bumping `HUGO_VERSION` in the Dockerfile still
never changes what `hugo_version` reports).

### Against ADR 005 (reconciliation flags only, never corrects)

Digest recording what main's own history already says, as a working
record instead of only a `Post` mirror, is not a correction: it is digest
doing exactly what it already does (write down what main says), just to a
record type that is now user-visible. ADR 005's line stays intact and
unchanged by this decision: reconciliation itself still never changes a
status or writes a working record on its own conclusion; every flag
`reconcile.run` raises is still resolved by an admin picking a resolution,
one at a time. What changes is upstream of reconciliation entirely, digest
populating a record before reconciliation ever gets a chance to flag its
absence, not reconciliation deciding to fix anything.

## Consequences

- Scott's 347 existing posts become tracked, editable working records
  from one digest run against the live instance, with no import chore and
  no manual per-post action.
- `post_on_main_without_published_draft` stops recurring for anything
  digest has already seen; it still fires correctly for a post that
  somehow lands on main outside digest's own write path (a hand-pushed
  commit before the next scheduled digest catches up), which is exactly
  the case the flag exists for.
- A site whose Hugo config `hugo config` cannot read (no config file
  parseable at all, a Hugo binary that fails on this version) still
  digests correctly, using the same conventions Chronicle already had,
  with the fallback visible on the admin page rather than hidden.
- The api image grows a Hugo install it didn't have before; image size and
  the api stage's build time both increase slightly. Reusing the
  builder's install block and the single `HUGO_VERSION` ARG keeps this
  from becoming a second version to track by hand.
- Renaming storage or API routes to match the UI's "Posts" label is
  explicitly out of scope this round and left for a future issue if Scott
  decides he wants it.
