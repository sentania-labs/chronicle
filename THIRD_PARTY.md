# Third-party assets

Chronicle's UI vendors two third-party libraries: `marked` and `EasyMDE`
(a JS file and a stylesheet). Everything else under `chronicle/api/static/`
(`style.css`, `ui.js`, `editor.js`) is original to this repository. Nothing
is loaded from a CDN, ever.

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

## EasyMDE

- **Files:** `chronicle/api/static/vendor/easymde.min.js` and
  `chronicle/api/static/vendor/easymde.min.css`
- **Version:** 2.18.0
- **Upstream:** https://github.com/Ionaru/easy-markdown-editor
- **Licence:** MIT (Copyright Jeroen Akkerman), stated in the file's own
  header comment. The bundle also carries CodeMirror 5 (MIT, Marijn
  Haverbeke and contributors) and its own dependencies, under their licences
  as bundled upstream.
- **Why:** the post editor needs a markdown toolbar and a side-by-side live
  render on top of a plain textarea, with markdown staying the stored text
  (no rich-text WYSIWYG). EasyMDE is a single-file, maintained editor that
  does this and degrades to the plain textarea without script. It is the same
  build the sibling dashboard app already runs.
- **Not modified** from the upstream minified build. The bundle contains
  jsdelivr and bootstrapcdn URLs it would fetch a spelling dictionary and
  FontAwesome from; `editor.js` sets `spellChecker: false` and
  `autoDownloadFontAwesome: false`, so neither request is ever made, and the
  toolbar glyphs are Chronicle's own CSS (`.editor-toolbar` in `style.css`).
  The Content-Security-Policy (`default-src 'self'`) would block them anyway.
