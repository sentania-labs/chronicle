// Node tests for the pure helpers in chronicle/api/static/theme.js. The DOM
// part of the file is behind a `document` guard, so `require`ing it under node
// runs only the declarations. Run by tests/test_js.py (part of `make check`
// when node is on PATH), or directly: `node --test tests/theme.test.mjs`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { resolveTheme, nextTheme } = require("../chronicle/api/static/theme.js");

test("a stored choice wins over the operating system preference", () => {
  assert.equal(resolveTheme("light", true), "light");
  assert.equal(resolveTheme("dark", false), "dark");
});

test("with no stored choice the operating system preference decides, light by default", () => {
  assert.equal(resolveTheme(null, true), "dark");
  assert.equal(resolveTheme(null, false), "light");
});

test("a stored value that is not a theme is ignored, not applied", () => {
  assert.equal(resolveTheme("purple", false), "light");
  assert.equal(resolveTheme("", true), "dark");
  assert.equal(resolveTheme("javascript:alert(1)", false), "light");
});

test("the toggle flips between the two themes", () => {
  assert.equal(nextTheme("light"), "dark");
  assert.equal(nextTheme("dark"), "light");
  assert.equal(nextTheme(null), "dark");
});
