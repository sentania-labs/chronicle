# C5 live check: rendered HTML and screenshots

The Playwright MCP server was unavailable during this round's own live
check (connection closed at startup, confirmed by trying to load its
tools before starting). Per the round's own fallback instruction, the
HTML fragments below are what that live check actually produced against
the fresh-volume compose stack, in place of PNG screenshots. Fetched with
`curl` against a running `docker compose` stack (`down -v`, `up -d
--build`, the real blog clone mounted read-only at `/blog`), not
hand-written.

- `01-drafts-board.html` / `.png`: the drafts board after the import.
- `02-editor.html` / `.png`: the editor for the imported draft, after a
  ghostwriter save, showing both authors in the version history.
- `03-conflict-view.html` / `.png`: the 409 view from saving with a stale
  `base_version`: the server's diff summary, the reloaded current-version
  form, and the visitor's own attempted text in a read-only pane.
- `04-version-diff.html` / `.png`: the unified diff between v1 (scott) and
  v2 (ghostwriter).
- `05-preview-tab.html` / `.png`: the preview tab listing the built
  preview.

Checked for internal hostnames and IPs before saving; none were present
(the UI never renders the container network's own addresses). No token
value appears in any of these pages, consistent with
`tests/test_ui.py::test_ui_token_never_appears_in_any_rendered_page`.

The `.png` files are real screenshots from an independent branch
verification pass (headless Chrome), taken separately from the HTML
fragments above and against the same rendered pages. That pass found two
defects: the imported-post feature image being dropped on save, and an
mdash HTML entity rendering instead of plain punctuation. Both are fixed
on this branch. See the PR body's "Verification evidence" section for the
fix commits.
