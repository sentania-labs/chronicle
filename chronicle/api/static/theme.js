// Chronicle theme choice: light or dark, remembered in the browser.
// Original to this repository. Loaded as a blocking script in <head> of every
// page, UI and Admin (ui_chrome.py says why it is not inline and not deferred):
// it must set `data-theme` on the root element before the first paint, or the
// page flashes the other theme. Lattice reads `data-theme="dark"`; light is the
// default and needs no attribute, but it is set anyway so the choice is always
// explicit and `style.css` can rely on it.
//
// resolveTheme and nextTheme are declared outside the DOM-guarded block for the
// same reason ui.js and editor.js keep their pure helpers there:
// tests/theme.test.mjs `require`s this file under `node --test`, where there is
// no `document`, so only these declarations run.

var THEME_KEY = "chronicle-theme";

// A stored choice wins; with none (or a value that is not one of the two), the
// operating system's preference decides, and light is the fallback.
function resolveTheme(stored, prefersDark) {
  if (stored === "light" || stored === "dark") {
    return stored;
  }
  return prefersDark ? "dark" : "light";
}

function nextTheme(current) {
  return current === "dark" ? "light" : "dark";
}

(function () {
  if (typeof document === "undefined") {
    return;
  }
  function readStored() {
    try {
      return window.localStorage.getItem(THEME_KEY);
    } catch (e) {
      return null; // storage blocked: the OS preference still applies
    }
  }
  function prefersDark() {
    return !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  }
  function apply(theme) {
    document.documentElement.setAttribute("data-theme", theme);
  }

  apply(resolveTheme(readStored(), prefersDark()));

  // The control is server-rendered hidden (no script, nothing to do). Its label
  // names the theme a click switches to. Delegated, so it works whether the
  // button exists yet or not when this head script runs.
  function label() {
    var button = document.getElementById("theme-toggle");
    if (!button) {
      return;
    }
    var target = nextTheme(document.documentElement.getAttribute("data-theme"));
    button.textContent = target === "dark" ? "Dark theme" : "Light theme";
    button.setAttribute("aria-label", "Switch to the " + target + " theme");
    button.hidden = false;
  }
  document.addEventListener("click", function (event) {
    var target = event.target;
    var button = target && target.closest ? target.closest("[data-theme-toggle]") : null;
    if (!button) {
      return;
    }
    var chosen = nextTheme(document.documentElement.getAttribute("data-theme"));
    apply(chosen);
    try {
      window.localStorage.setItem(THEME_KEY, chosen);
    } catch (e) {
      // not stored: the choice holds for this page only
    }
    label();
  });
  document.addEventListener("DOMContentLoaded", label);
})();

if (typeof module !== "undefined" && module.exports) {
  module.exports = { resolveTheme: resolveTheme, nextTheme: nextTheme };
}
