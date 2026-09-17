# Third-party assets

Chronicle's UI (C5) vendors exactly one third-party asset. Everything else
under `chronicle/api/static/` (`style.css`, `ui.js`) is original to this
repository.

## marked

- **File:** `chronicle/api/static/vendor/marked.min.js`
- **Version:** 12.0.2
- **Upstream:** https://github.com/markedjs/marked
- **Licence:** MIT (Copyright (c) 2018+, MarkedJS; Copyright (c) 2011-2018,
  Christopher Jeffrey), reproduced in the file's own header comment.
- **Why:** the editor's live preview pane renders the draft body as markdown
  entirely in the visitor's browser, with no server round trip and no
  network fetch at page load (AGENTS.md); `marked` is a well-known,
  single-file, dependency-free renderer that fits that bar without writing
  and maintaining a markdown parser for this round.
- **Not modified** from the upstream minified build.
