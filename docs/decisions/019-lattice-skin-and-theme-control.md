# 019: Lattice on both surfaces, and a theme control served as a head script

- **Status:** accepted
- **Date:** 2026-09-18

## Context

The UI and Admin rendered as browser defaults, and Admin carried its own inline
`<style>` block, so the two surfaces were not even the same default. Lattice
(`sentania-labs/lattice`) is the shared look for Scott's tools: a stylesheet and
design tokens, no build step, no runtime, both themes.

## Decision

**Vendor the two release assets, link them in the documented order, on both
surfaces.** `tokens.css` and `lattice.css` are the `v0.1.0` release assets,
byte-identical, under `chronicle/api/static/vendor/`, recorded in
`THIRD_PARTY.md` with their checksums. Every page links tokens, then Lattice,
then (on the editor only) EasyMDE's own sheet, then Chronicle's `style.css`, and
`ui_chrome.py` is the one place that decides that order, so the two `page()`
functions cannot drift. Admin's inline block is gone.

**`style.css` keeps only what Lattice does not cover, and only in tokens.** The
page shell, the editor grid and sticky bar, the dropzone, the diff colours and
the EasyMDE and CodeMirror chrome. It uses `var(--...)` for every colour, space
and radius, never a hex value: a hardcoded `#fff` is what breaks when the theme
flips. `tests/test_ui_skin.py` fails on a hex or `rgb()` value and on a token
that does not exist. EasyMDE's stylesheet is never edited; `style.css` wins over
it on order and specificity.

**A state colour carries state and nothing else.** `ui_status.STATUS_TONES`
maps a draft status to a Lattice badge tone: Published is `ok`, Needs revision is
`warn`, Rejected is `bad`, and every in-progress status stays neutral. Notices
map by kind (`ok`, `error` to `bad`, `conflict` and `warning` to `warn`), the
internal-only banner is `warn` (it warns about exposure), and an empty state is
the neutral banner.

**One primary per screen.** Publish or Preview, whichever `offers_for` marks
primary, is the editor's; Save is a plain button, because a Save that was
primary would be a second primary whenever an offer is, and the offers are
swapped in place after a save while the Save button is not.

**A theme control, the one behaviour addition.** A button in the header writes
`light` or `dark` to `localStorage` under `chronicle-theme`; with nothing stored,
`prefers-color-scheme` decides. It is served as `static/theme.js`, a synchronous
script in `<head>` ahead of the stylesheets, not as an inline script and not
deferred. Inline is out because the Content-Security-Policy is
`default-src 'self'` (an inline script would be refused), and deferred is out
because the theme has to be set before first paint or every dark page flashes
light. A same-origin blocking script in `<head>` is the one form that satisfies
both. The button ships `hidden` and the script reveals it, since without script
it would do nothing.

## Consequences

- The CSP is unchanged. Nothing is loaded from a CDN.
- A future page that needs a new look adds a rule to `style.css` in tokens, or
  asks Lattice for a component; it does not add a second stylesheet.
- Lattice has no licence file upstream. `THIRD_PARTY.md` says so and does not
  invent one.
- Static assets carry no `Cache-Control` (Starlette's `StaticFiles` sends only
  `Last-Modified` and `ETag`), so a browser may keep an old `style.css` for a
  while after an upgrade. Not addressed here; see the pull request body.
