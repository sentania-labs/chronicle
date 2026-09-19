// Node-only regression test for the pure URL check in
// chronicle/api/static/ui.js (isSafeUrl). `sanitize` itself needs a DOM, which
// node lacks; tests/test_js_dom.py runs it in headless Chrome. Run by tests/test_js.py as part of
// `make check` when node is on PATH (skipped, with the reason, when it is
// not), or directly with `node --test tests/ui_sanitize.test.mjs`. The payload
// list itself is also documented in ui.js's own comment so the coverage is
// legible without running node at all.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { isSafeUrl } = require("../chronicle/api/static/ui.js");

const bypassPayloads = [
  "java\nscript:alert(1)",
  "java\tscript:alert(1)",
  "jAvAsCrIpT:alert(1)",
  "data:text/html,<script>alert(1)</script>",
  "vbscript:msgbox(1)",
];

test("known javascript: bypass payloads are rejected", () => {
  for (const payload of bypassPayloads) {
    assert.equal(isSafeUrl(payload), false, `expected unsafe: ${JSON.stringify(payload)}`);
  }
});

test("a NUL byte breaks the scheme, so the URL parser treats it as a relative path", () => {
  // "java\x00script:..." has no valid scheme character at the NUL, so the
  // WHATWG URL parser never reads a "javascript:" scheme out of it at all;
  // it resolves relative to the page and lands back on http/https, which is
  // exactly the safe outcome isSafeUrl exists to allow. This is not a
  // bypass, it is the parser already doing the neutralising.
  assert.equal(isSafeUrl("java\x00script:alert(1)"), true);
});

test("ordinary safe urls are accepted", () => {
  assert.equal(isSafeUrl("http://example.com/x"), true);
  assert.equal(isSafeUrl("https://example.com/x"), true);
  assert.equal(isSafeUrl("mailto:scott@example.com"), true);
  assert.equal(isSafeUrl("/relative/path"), true);
  assert.equal(isSafeUrl("#hash"), true);
  assert.equal(isSafeUrl(""), true);
});
