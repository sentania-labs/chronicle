# Posts board workflow: live verification screenshots

Real PNG screenshots from headless Chrome (`chrome-devtools-axi`, 1280 px
wide) driven by clicking through a running `docker compose` stack, not
curl. Taken on 2026-09-18 (America/Chicago).

## How it was produced

- Branch stack: `feat/posts-board-workflow`, compose project
  `chronicle-laneb`, clean volume, api on `127.0.0.1:8085`.
- Baseline stack: `origin/main` (b5fc00a), compose project
  `chronicle-lanebbase`, clean volume, api on `127.0.0.1:8086`.
- Both stacks loaded the same digest: the local clone of the real blog
  copied into the api container and `chronicle digest` run with
  `CHRONICLE_DIGEST_REPO_URL` pointed at it (see the top of
  `docker-compose.yml`). Result on both: 346 records created, 346 published
  as working records, Hugo 0.164.0, 2 theme submodules.
- The branch stack then got three in-flight posts made through the UI's
  own New post button (two drafting, one submitted to in_review), so the
  board has both sections to show.
- Times read `16:03 CDT` because the stack's clock was 21:03 UTC when it
  was taken and the UI renders `America/Chicago` (`CHRONICLE_UI_TIMEZONE`
  default).

## Index

- `01-posts-board.png`: the Posts board on the branch. "In flight (3)" on
  top, newest first, with local clock times. "Published archive (346)" is
  collapsed below it.
- `02-posts-board-archive-expanded.png`: the same board with the archive
  opened. The archive is newest first by the post's own date (2026-07-27,
  07-24, 07-24, 07-23, 07-06, ...), times shown as `16:01 CDT`.
- `03-posts-board-search-archive.png`: search term `vcf`. In flight drops
  to 0 ("nothing in flight."), the archive opens by itself and shows the 8
  matching posts.
- `04-posts-board-search-in-flight.png`: search term `dns`. One in-flight
  hit, an empty archive ("no published posts.").
- `05-new-post-editor.png`: the editor page the New post button lands on: a
  blank, unclaimed "(untitled)" post in drafting with every field empty.
- `06-import-nothing-left.png`: the Import tab after the digest. It says all
  346 posts are already on the Posts tab and explains that the tab only lists
  a post with no record yet.
- `07-archive-order-before-fix.png`: a defect this pass found, kept as
  evidence (see below). The archive as first built: 2026 posts, then 2011
  onward, out of publication order.
- `08-before-main-posts-board.png`: BEFORE, from `origin/main`. One flat list,
  oldest first (2005 posts on top), every record published, no search box, no
  New post button, and raw UTC stamps
  (`updated: 2026-09-18T21:02:57+00:00`). 7 pages of 50.

## Defect found and fixed in this pass

The archive was sorted by `updated_at`. A digest stamps hundreds of
records within a few seconds of each other, so on a freshly loaded instance
"newest first" meant "digest processing order": the four 2026 posts, then
2011 ascending (`07`). The archive now sorts by the date the post carries,
falling back to `updated_at` when a record has none (`02`, `03`).
Regression test:
`tests/test_ui.py::test_archive_orders_by_post_date_not_by_when_the_record_was_touched`.

## Noticed, not fixed (restyle or copy, for the next pass)

- A search with no archive hits still opens the archive and shows
  "no published posts." plus a pager reading "previous page 1 of 1 (0 total)
  next" (`04`). The pager is noise when there is nothing to page.
- The whole UI is unstyled browser defaults: default-blue links, no visual
  separation between statuses, an editor that is one long form (`05`).
- A saved post's slug shows as `-` until it is first previewed or approved
  (`01`, `04`); accurate, but it reads as missing data.
- A save returns the editor at `/content/drafts/<id>/save` (a POST render,
  not a redirect), so a browser refresh re-submits the form.

Checked for tokens, credentials and internal addresses before saving: none
are visible in any image (the pages render only titles, slugs, statuses and
local times, and the browser chrome is not in the captures).
