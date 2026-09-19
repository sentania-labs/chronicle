# Third-party assets

Chronicle's UI vendors two third-party libraries, `marked` and `EasyMDE`
(a JS file and a stylesheet), and one first-party stylesheet pair shared with
Scott's other tools, `Lattice` (`tokens.css` and `lattice.css`). Everything
else under `chronicle/api/static/` (`style.css`, `ui.js`, `editor.js`) is
original to this repository. Nothing is loaded from a CDN, ever.

The full licence text of everything the vendored files contain is under
`chronicle/api/static/vendor/LICENSES/`, one file per component. The minified
files carry only their own top-level header comment (minification strips the
others), so those files are how the notices travel with the copies.
`tests/test_third_party.py` checks the checksums below against the files.

## Checksums

The sha256 of each vendored file as committed. "Not modified" below is checked
against these and against the upstream npm package, not asserted.

| File | sha256 |
| --- | --- |
| `chronicle/api/static/vendor/marked.min.js` | `15fabce5b65898b32b03f5ed25e9f891a729ad4c0d6d877110a7744aa847a894` |
| `chronicle/api/static/vendor/easymde.min.js` | `aed84bf922d57dfc6a0f65b30eb534dfaab509c74d1f5ccad19ea8775a09de13` |
| `chronicle/api/static/vendor/easymde.min.css` | `8a148c947f7e63250d8fb8d97e030b6fef6e02480ea08c0acfacb11618ac11f6` |
| `chronicle/api/static/vendor/tokens.css` | `d9b98f9729dcb61ded4e57fe00d1ce996acf0e217b942944b06fccf4dedce3d4` |
| `chronicle/api/static/vendor/lattice.css` | `179ce17c5ff9c55963abd82f27b7c6e49b20196e4c23fe7c6ff6a7d7c7d1bdb4` |

## marked

- **File:** `chronicle/api/static/vendor/marked.min.js`
- **Version:** 12.0.2
- **Upstream:** https://github.com/markedjs/marked
- **Licence:** MIT (Copyright (c) 2018+, MarkedJS; Copyright (c) 2011-2018,
  Christopher Jeffrey). The file's own header names the licence; the full text
  is `vendor/LICENSES/marked.txt`.
- **Why:** the editor's live preview pane renders the draft body as markdown
  entirely in the visitor's browser, with no server round trip and no
  network fetch at page load (AGENTS.md); `marked` is a well-known,
  single-file, dependency-free renderer that fits that bar without writing
  and maintaining a markdown parser for this round.
- **Not modified.** Byte-identical to `marked.min.js` in the `marked@12.0.2`
  npm package (same sha256 as above).

## EasyMDE

- **Files:** `chronicle/api/static/vendor/easymde.min.js` and
  `chronicle/api/static/vendor/easymde.min.css`
- **Version:** 2.18.0
- **Upstream:** https://github.com/Ionaru/easy-markdown-editor
- **Licence:** MIT (Copyright (c) 2015 Sparksuite, Inc.; Copyright (c) 2017
  Jeroen Akkerman); the file's own header says `@license MIT` and nothing more.
- **What the JS bundle contains besides EasyMDE.** It is a browserify bundle,
  so it carries other libraries whose notices minification removed. Their
  licences are in `vendor/LICENSES/`:
  - CodeMirror 5.65.9 and the addons and modes EasyMDE requires (MIT,
    Copyright (C) 2017 Marijn Haverbeke and others): `codemirror.txt`.
  - `marked` 4.x, a second, older copy of the parser that EasyMDE uses for
    its own preview. It is a different copy from the top-level `marked` 12.0.2
    above; both are in the editor page. Same licence: `marked.txt`. The
    editor's live render uses the top-level 12.0.2 through `previewRender`.
  - `codemirror-spell-checker` 1.1.2 (MIT, Copyright (c) 2015 Wes Cossick):
    `codemirror-spell-checker.txt`.
  - `typo-js` 1.x (Modified BSD, Copyright (c) 2011, Christopher Finke):
    `typo-js.txt`.
  The spell checker is present in the bundle but never runs: `editor.js` sets
  `spellChecker: false`.
- **Why:** the post editor needs a markdown toolbar and a side-by-side live
  render on top of a plain textarea, with markdown staying the stored text
  (no rich-text WYSIWYG). EasyMDE is a single-file, maintained editor that
  does this and degrades to the plain textarea without script. 2.18.0 is the
  version the sibling dashboard app uses (there from a CDN; here it is
  vendored).
- **`easymde.min.css` is not modified:** byte-identical to `dist/easymde.min.css`
  in the `easymde@2.18.0` npm package.
- **`easymde.min.js` has one edit.** The upstream file (npm `dist/easymde.min.js`,
  sha256 `42c578c29ae613807f43c292e23365f2f676071450a8f09314668a27720ccee3`)
  contains one literal em-dash character, inside the smart-punctuation regular
  expression of its embedded `marked`. This repository forbids that character
  everywhere (`make prose-check`), so it is written as the JavaScript escape
  `\u2014`, which is the same character to the engine. Nothing else differs:
  replacing that one character in the upstream file reproduces the committed
  file byte for byte.
- **Network:** the bundle contains jsdelivr and bootstrapcdn URLs it would fetch
  a spelling dictionary and FontAwesome from; `editor.js` sets
  `spellChecker: false` and `autoDownloadFontAwesome: false`, so neither request
  is ever made, and the toolbar glyphs are Chronicle's own CSS
  (`.editor-toolbar` in `style.css`). The Content-Security-Policy
  (`default-src 'self'`) would block them anyway.

## Lattice

- **Files:** `chronicle/api/static/vendor/tokens.css` and
  `chronicle/api/static/vendor/lattice.css`
- **Version:** v0.1.0
- **Upstream:** https://github.com/sentania-labs/lattice, release `v0.1.0`
  (the two release assets of those names, downloaded with `gh release
  download`, not copied from a working tree).
- **Licence:** none stated upstream. `sentania-labs/lattice` carries no LICENSE
  file of its own. It is a first-party asset shared between Scott's own
  repositories (extracted from `sentania-labs/vcf-cf-migrator`), and Chronicle
  itself is MIT. No licence is invented for it here, and there is nothing to
  put under `vendor/LICENSES/`.
- **Why:** Chronicle's UI and Admin were unstyled browser defaults. Lattice is
  the shared look for these tools: a stylesheet and design tokens with no
  build step and no runtime, which is what lets it be served from the app the
  same way the other vendored files are. Both themes come from the tokens
  (`data-theme="dark"` on the root element).
- **Not modified.** Byte-identical to the `v0.1.0` release assets (same sha256
  as above). Chronicle's own `style.css` is linked after them and carries only
  what Lattice does not cover.
