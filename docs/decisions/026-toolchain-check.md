# 026: an admin Toolchain page that checks upstream and opens blog PRs

- **Status:** accepted
- **Date:** 2026-09-25

## Context

Hugo is pinned twice: in Chronicle's image (`ARG HUGO_VERSION`) and in the
blog's deploy workflow. The blog's themes are git submodules. Nothing told
Scott when any of these fell behind, and the blog still carries
`themes/hugo-clarity` as a submodule although the site config sets
`theme = "blowfish"`, so it is cloned on every build for nothing (issue #67).

## Decision

- **Where the numbers come from:** `git ls-remote` against each upstream, and
  nothing else. No GitHub API token, no clone. Only https upstreams are
  queried; an ssh GitHub URL is rewritten to https first, and anything else
  is shown as not checked. Every query has a 30 second timeout.
  - Hugo: the highest `vX.Y.Z` tag on gohugoio/hugo, set against the version
    the builder heartbeat reports (the same source the Status page uses) and
    the blog workflow's `HUGO_VERSION`.
  - Theme and other submodules: the pinned gitlink from the site checkout,
    set against upstream `HEAD` and its highest version-shaped tag (peeled,
    so an annotated tag names its commit).
  - Hugo modules: each `require` in the site's `go.mod` on a github.com path,
    set against its highest tag.
- **Unused themes:** a submodule directly under `themesdir` that nothing
  loads. Loaded means named by the site's `theme` or a `module.imports`
  path (full path or last segment), or imported the same way by a loaded
  theme's own `theme.toml`, `hugo.toml` or `config.*`, transitively. These
  come from the site's own `hugo config`, in the environment digest uses.
  When that read fails, names no theme at all, or puts `themesdir` outside
  the site, no theme is ever marked unused, so a broken or unexpected
  config can never offer to delete the theme the site depends on.
- **When:** a background thread in the api (ADR 013's pattern) runs one
  check a day, never in its first five minutes after start, and never
  before a digest has left a site checkout. "Check now" on the page runs one
  in the background. The page itself never queries upstream; it renders
  `state/toolchain_check.json`. Local reads happen under the site clone lock
  and every upstream query after it is released.
- **Actions open PRs, never push.** Each goes through the same
  `GitHubRepoOps` the publisher uses (the GitHub App, or test-token mode) and
  builds one commit off the default branch's head on a
  `chronicle/toolchain/<action>-<path>-<hash>` branch (`bump-tag`,
  `bump-head` or `remove`; the hash of the exact path keeps two paths that
  slug alike apart). A second click refreshes the same PR.
  - Move a submodule: a gitlink tree entry at the latest tag's commit or at
    upstream head. Only those two commits, as found by the last check, can
    be chosen; the form names `tag` or `head`, never a sha. Refused when
    the default branch's gitlink or the submodule's url no longer matches
    the check's, so a stale check never builds a PR that moves a theme
    backwards or points a new upstream at a commit it may not have.
  - Remove an unused theme: deletes the gitlink and its `.gitmodules`
    section (read from the default branch through the API, every other line
    kept byte for byte), or `.gitmodules` itself when nothing is left.
    Refused unless the default branch is still at the commit the check read
    (`site_commit`), since "unused" was decided against that commit's config.
  - The page links the opened PR until the pinned commit changes, and
    keeps the buttons, so a closed PR never leaves a row with nothing to
    press.
- **Hugo bumps stay a Chronicle release.** Hugo is in the image, so the page
  reports the gap, links the release notes, and offers a prefilled new-issue
  link on Chronicle's repo. It does not file the issue itself.
- **Hugo modules are reported only.** Moving one means `hugo mod get` and a
  Go toolchain, which Chronicle does not carry.

## Consequences

- A merged bump or removal reaches Chronicle on its next digest, like any
  other change to main.
- No preview build of a toolchain PR's branch: the builder builds drafts,
  not arbitrary branches. The issue called this ideal, not required.
