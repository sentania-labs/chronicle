# C5 live check: rendered HTML, not screenshots

The Playwright MCP server was unavailable in this session (connection
closed at startup, confirmed by trying to load its tools before the live
check). Per the round's own fallback instruction, these are the rendered
HTML fragments the live check actually produced against the fresh-volume
compose stack, in place of PNG screenshots. Fetched with `curl` against a
running `docker compose` stack (`down -v`, `up -d --build`, the real blog
clone mounted read-only at `/blog`), not hand-written.

- `01-drafts-board.html`: the drafts board after the import.
- `02-editor.html`: the editor for the imported draft, after a ghostwriter
  save, showing both authors in the version history.
- `03-conflict-view.html`: the 409 view from saving with a stale
  `base_version`: the server's diff summary, the reloaded current-version
  form, and the visitor's own attempted text in a read-only pane.
- `04-version-diff.html`: the unified diff between v1 (scott) and v2
  (ghostwriter).
- `05-preview-tab.html`: the preview tab listing the built preview.

Checked for internal hostnames and IPs before saving; none were present
(the UI never renders the container network's own addresses). No token
value appears in any of these five pages, consistent with
`tests/test_ui.py::test_ui_token_never_appears_in_any_rendered_page`.
