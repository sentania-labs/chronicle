// Node tests for the pure helpers in chronicle/api/static/theme.js. The DOM
// part of the file is behind a `document` guard, so `require`ing it under node
// runs only the declarations. Run by tests/test_js.py (part of `make check`
// when node is on PATH), or directly: `node --test tests/theme.test.mjs`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import vm from "node:vm";

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

// --- The wiring: run the real file against a stub page --------------------
// resolveTheme and nextTheme are declarations; the click handler, the
// localStorage write and the reveal of the control are inside the file's DOM
// block. This runs the whole file in a vm context so removing any of them fails.
const THEME_SOURCE = readFileSync(new URL("../chronicle/api/static/theme.js", import.meta.url), "utf8");

function loadThemePage({ stored = null, prefersDark = false, storageThrows = false } = {}) {
  const root = {
    attrs: {},
    setAttribute(name, value) {
      this.attrs[name] = value;
    },
    getAttribute(name) {
      return name in this.attrs ? this.attrs[name] : null;
    },
  };
  const button = {
    hidden: true,
    textContent: "",
    attrs: {},
    setAttribute(name, value) {
      this.attrs[name] = value;
    },
  };
  const handlers = {};
  const writes = [];
  const document = {
    documentElement: root,
    getElementById: (id) => (id === "theme-toggle" ? button : null),
    addEventListener(type, fn) {
      (handlers[type] = handlers[type] || []).push(fn);
    },
  };
  const window = {
    localStorage: {
      getItem: () => stored,
      setItem(key, value) {
        if (storageThrows) {
          throw new Error("blocked");
        }
        writes.push([key, value]);
      },
    },
    matchMedia: () => ({ matches: prefersDark }),
  };
  vm.runInNewContext(THEME_SOURCE, { document, window });
  const click = () =>
    handlers.click[0]({ target: { closest: (sel) => (sel === "[data-theme-toggle]" ? button : null) } });
  return { root, button, handlers, writes, click };
}

test("the stored or preferred theme is applied to the root before paint", () => {
  assert.equal(loadThemePage({ stored: "dark" }).root.attrs["data-theme"], "dark");
  assert.equal(loadThemePage({ prefersDark: true }).root.attrs["data-theme"], "dark");
  assert.equal(loadThemePage().root.attrs["data-theme"], "light");
});

test("the control is revealed on load and names the theme a click switches to", () => {
  const page = loadThemePage();
  assert.equal(page.button.hidden, true);
  page.handlers.DOMContentLoaded[0]();
  assert.equal(page.button.hidden, false);
  assert.equal(page.button.textContent, "Dark theme");
  assert.equal(page.button.attrs["aria-label"], "Switch to the dark theme");
});

test("a click flips the theme, remembers it, and relabels the control", () => {
  const page = loadThemePage();
  page.click();
  assert.equal(page.root.attrs["data-theme"], "dark");
  assert.deepEqual(JSON.parse(JSON.stringify(page.writes)), [["chronicle-theme", "dark"]]);
  assert.equal(page.button.textContent, "Light theme");
  page.click();
  assert.equal(page.root.attrs["data-theme"], "light");
  assert.equal(page.writes[1][1], "light");
});

test("a click on something that is not the control does nothing", () => {
  const page = loadThemePage();
  page.handlers.click[0]({ target: { closest: () => null } });
  assert.equal(page.root.attrs["data-theme"], "light");
  assert.equal(page.writes.length, 0);
});

test("blocked storage still switches the theme for this page", () => {
  const page = loadThemePage({ storageThrows: true });
  page.click();
  assert.equal(page.root.attrs["data-theme"], "dark");
});
