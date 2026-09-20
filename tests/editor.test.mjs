// Node tests for the pure helpers in chronicle/api/static/editor.js: the
// backup verdict, the upload outcome, the image-reference lookup, and the
// save-state text. The DOM parts of the file are behind a `document` guard, so
// `require`ing it under node runs only these declarations. Run by
// tests/test_js.py (part of `make check` when node is on PATH), or directly:
// `node --test tests/editor.test.mjs`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const require = createRequire(import.meta.url);
const { pageTitles, sameState, backupVerdict, backupWriteAction, makeBackupStore, conflictBackupAction, backupMessage, uploadOutcome, lookupImageSrc, saveStateText } =
  require("../chronicle/api/static/editor.js");

test("sameState compares the body and every field, missing and empty alike", () => {
  assert.equal(sameState({ body: "a", fields: { title: "t" } }, { body: "a", fields: { title: "t" } }), true);
  assert.equal(sameState({ body: "a", fields: {} }, { body: "b", fields: {} }), false);
  assert.equal(sameState({ body: "a", fields: { title: "t" } }, { body: "a", fields: { title: "u" } }), false);
  assert.equal(sameState({ body: "a", fields: { tags: "" } }, { body: "a", fields: {} }), true);
  assert.equal(sameState(null, { body: "a" }), false);
});

test("a backup that matches the server is cleared, not offered", () => {
  const server = { body: "same", fields: { title: "t" } };
  assert.deepEqual(backupVerdict({ body: "same", fields: { title: "t" }, baseVersion: 3 }, server, 3), {
    action: "clear",
  });
});

test("no backup, or a malformed one, offers nothing", () => {
  const server = { body: "x", fields: {} };
  assert.deepEqual(backupVerdict(null, server, 1), { action: "none" });
  assert.deepEqual(backupVerdict({ nope: true }, server, 1), { action: "none" });
});

test("a differing backup is offered, and flags a server that moved on", () => {
  const server = { body: "server text", fields: {} };
  const current = backupVerdict({ body: "mine", fields: {}, baseVersion: 4 }, server, 4);
  assert.equal(current.action, "offer");
  assert.equal(current.serverMoved, false);

  const moved = backupVerdict({ body: "mine", fields: {}, baseVersion: 4 }, server, 6);
  assert.equal(moved.action, "offer");
  assert.equal(moved.serverMoved, true);
  assert.equal(moved.baseVersion, 4);
  assert.equal(moved.serverVersion, 6);
  assert.match(backupMessage(moved, "today"), /version 4.*version 6.*instead of overwriting/);
  assert.doesNotMatch(backupMessage(current, "today"), /version/);
});

test("a backup with no base version is offered without claiming the server moved", () => {
  const verdict = backupVerdict({ body: "mine", fields: {} }, { body: "s", fields: {} }, 2);
  assert.equal(verdict.action, "offer");
  assert.equal(verdict.serverMoved, false);
});

test("an inline upload inserts the markdown the server built", () => {
  const outcome = uploadOutcome({
    ok: true,
    filename: "rack.png",
    role: "inline",
    markdown: "![rack](rack.png)",
    url: "/content/drafts/d/images/i/file",
  });
  assert.equal(outcome.ok, true);
  assert.equal(outcome.url, "/content/drafts/d/images/i/file");
  assert.equal(outcome.insert, "![rack](rack.png)");
  assert.match(outcome.message, /rack\.png/);
});

test("a feature upload inserts nothing", () => {
  const outcome = uploadOutcome({ ok: true, filename: "hero.png", role: "feature", markdown: null });
  assert.equal(outcome.ok, true);
  assert.equal(outcome.insert, null);
});

test("a failed upload carries the server's message, or a default", () => {
  assert.deepEqual(uploadOutcome({ ok: false, message: "too large" }), { ok: false, message: "too large" });
  assert.equal(uploadOutcome(null).ok, false);
  assert.equal(uploadOutcome({ ok: false }).message, "upload failed");
});

test("image references resolve to the attached image's URL, encoded or not", () => {
  const images = [
    { filename: "rack photo.png", src: "/content/drafts/d/images/i1/file" },
    { filename: "b.png", src: "/content/drafts/d/images/i2/file" },
  ];
  assert.equal(lookupImageSrc("b.png", images), "/content/drafts/d/images/i2/file");
  assert.equal(lookupImageSrc("rack%20photo.png", images), "/content/drafts/d/images/i1/file");
  assert.equal(lookupImageSrc("https://example.com/x.png", images), "https://example.com/x.png");
  assert.equal(lookupImageSrc("%E0%A4%A", images), "%E0%A4%A");
});

test("every save state has its own text, and unknown states read as idle", () => {
  assert.equal(saveStateText("dirty"), "Unsaved changes");
  assert.equal(saveStateText("saving"), "Saving...");
  assert.equal(saveStateText("saved", "3:04 PM"), "Saved at 3:04 PM");
  assert.match(saveStateText("conflict"), /changed underneath you/);
  assert.equal(saveStateText("error", "nope"), "Not saved: nope");
  assert.equal(saveStateText("idle"), "No unsaved changes");
  assert.equal(saveStateText("whatever"), "No unsaved changes");
});

test("an unanswered backup offer is never overwritten or cleared", () => {
  const server = { body: "server", fields: {} };
  const typed = { body: "typed", fields: {} };
  // Leaving the page with the banner up, the editor matches the server: that
  // must not clear the stored copy the banner is offering.
  assert.equal(backupWriteAction(server, server, true), "skip");
  assert.equal(backupWriteAction(typed, server, true), "skip");
  assert.equal(backupWriteAction(server, server, false), "clear");
  assert.equal(backupWriteAction(typed, server, false), "write");
});

// A stand-in for window.localStorage shared by two "tabs".
function fakeStorage() {
  const data = new Map();
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => void data.set(k, String(v)),
    removeItem: (k) => void data.delete(k),
  };
}

test("a clean second tab does not clear the first tab's unsaved-work backup", () => {
  const storage = fakeStorage();
  const key = "chronicle-backup:d1";
  const server = { body: "server text", fields: { title: "t" } };
  const tabA = makeBackupStore(storage, key);
  const tabB = makeBackupStore(storage, key);

  // Tab A has unsaved edits and its backup is written.
  tabA.write({ body: "A's edits", fields: { title: "t" } }, 3);
  assert.equal(tabA.read().body, "A's edits");

  // Tab B is clean (its state is the server's) and is switched away from or
  // closed: the flush asks to clear. It did not write this entry and the entry
  // is not the state it holds, so it stays.
  tabB.clear(server);
  assert.equal(tabB.read().body, "A's edits");

  // Tab A then saves: the entry is its own and matches what it just saved.
  tabA.clear({ body: "A's edits", fields: { title: "t" } });
  assert.equal(tabA.read(), null);
});

test("a tab clears an entry that matches the state it has just saved, even if another tab wrote it", () => {
  const storage = fakeStorage();
  const key = "chronicle-backup:d1";
  const tabA = makeBackupStore(storage, key);
  const tabB = makeBackupStore(storage, key);
  tabA.write({ body: "same text", fields: {} }, 2);
  // Tab B saved that same text: the backup is redundant and B may clear it.
  tabB.clear({ body: "same text", fields: {} });
  assert.equal(tabB.read(), null);
});

test("a tab does not clear a backup another tab overwrote after its own write", () => {
  const storage = fakeStorage();
  const key = "chronicle-backup:d1";
  const tabA = makeBackupStore(storage, key);
  const tabB = makeBackupStore(storage, key);
  tabA.write({ body: "A", fields: {} }, 1);
  tabB.write({ body: "B", fields: {} }, 1);
  tabA.clear({ body: "A", fields: {} });
  assert.equal(tabA.read().body, "B");
});

test("discard is the visitor's own decision and clears whoever wrote the entry", () => {
  const storage = fakeStorage();
  const key = "chronicle-backup:d1";
  const tabA = makeBackupStore(storage, key);
  const tabB = makeBackupStore(storage, key);
  tabA.write({ body: "A", fields: {} }, 1);
  tabB.discard();
  assert.equal(tabB.read(), null);
});

test("a blocked or throwing storage never throws out of the backup store", () => {
  const throwing = {
    getItem() {
      throw new Error("blocked");
    },
    setItem() {
      throw new Error("blocked");
    },
    removeItem() {
      throw new Error("blocked");
    },
  };
  for (const store of [makeBackupStore(throwing, "k"), makeBackupStore(null, "k")]) {
    assert.equal(store.read(), null);
    store.write({ body: "x", fields: {} }, 1);
    store.clear({ body: "x", fields: {} });
    store.discard();
  }
});

test("the conflict page never overwrites earlier unanswered work, and stores only when nothing carries the state", () => {
  const attempted = { body: "EDIT TWO", fields: { title: "two", tags: "b" } };
  // The offered backup the visitor ignored: a different body must survive.
  assert.equal(conflictBackupAction({ body: "EDIT ONE", fields: { title: "one" } }, attempted), "keep");
  // The editor's own copy of this very state is left as it is.
  assert.equal(conflictBackupAction({ body: "EDIT TWO", fields: { title: "two", tags: "b" } }, attempted), "same");
  // Same body but older title or tags: the attempted field values are not stored yet.
  assert.equal(conflictBackupAction({ body: "EDIT TWO", fields: { title: "one", tags: "b" } }, attempted), "keep");
  assert.equal(conflictBackupAction({ body: "EDIT TWO", fields: { title: "two", tags: "a" } }, attempted), "keep");
  assert.equal(conflictBackupAction({ body: "EDIT TWO", fields: { title: "two" } }, attempted), "keep");
  assert.equal(conflictBackupAction({ body: "EDIT TWO" }, attempted), "keep");
  // A missing field and an empty one are the same, as everywhere else.
  assert.equal(
    conflictBackupAction({ body: "EDIT TWO", fields: { title: "two", tags: "b", summary: "" } }, attempted),
    "same"
  );
  // Nothing stored, or an unreadable entry: the attempted state goes in.
  assert.equal(conflictBackupAction(null, attempted), "write");
  assert.equal(conflictBackupAction({ nope: true }, attempted), "write");
});


test("pageTitles reads the tab title and heading the server rendered (#36)", () => {
  const doc = { title: "Post: New name", querySelector: (sel) => (sel === "h1" ? { textContent: "Post: New name" } : null) };
  assert.deepEqual(pageTitles(doc), { title: "Post: New name", heading: "Post: New name" });
});

test("pageTitles gives nothing for a page with neither, so a partial page never blanks them", () => {
  assert.equal(pageTitles({ title: "", querySelector: () => null }), null);
  assert.equal(pageTitles(null), null);
});

// --- The wiring: run the real file against a stub page --------------------
// The pure helpers above are declarations; the lines that call them after a
// save are inside the file's DOM block. This runs the whole file in a vm
// context whose `document` is just enough for the editor page, so removing the
// title or heading update after an in-place save fails here (#36).
const EDITOR_SOURCE = readFileSync(new URL("../chronicle/api/static/editor.js", import.meta.url), "utf8");

function stubElement(props = {}) {
  const handlers = {};
  return Object.assign(
    {
      handlers,
      value: "",
      textContent: "",
      hidden: false,
      disabled: false,
      elements: [],
      attrs: {},
      addEventListener(type, fn) {
        (handlers[type] = handlers[type] || []).push(fn);
      },
      setAttribute(name, value) {
        this.attrs[name] = value;
      },
      getAttribute(name) {
        return name in this.attrs ? this.attrs[name] : null;
      },
    },
    props
  );
}

function loadEditorPage(savedPage) {
  const ids = {
    "editor-app": stubElement({ attrs: { "data-draft-id": "d1" } }),
    "edit-form": stubElement({ action: "/content/drafts/d1" }),
    body: stubElement({ value: "text" }),
    base_version: stubElement({ value: "1" }),
    "save-btn": stubElement(),
    "save-state": stubElement(),
  };
  const heading = stubElement({ textContent: "Old heading" });
  const document = {
    title: "Old heading | Chronicle",
    body: { classList: { add() {} } },
    visibilityState: "visible",
    getElementById: (id) => ids[id] || null,
    querySelector: (sel) => (sel === "h1" ? heading : null),
    querySelectorAll: () => [],
    addEventListener() {},
  };
  const store = new Map();
  const sandbox = {
    document,
    window: {
      localStorage: {
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, String(v)),
        removeItem: (k) => store.delete(k),
      },
      addEventListener() {},
      location: { pathname: "/content/drafts/d1" },
      confirm: () => true,
    },
    fetch: () => Promise.resolve({ status: 200, text: () => Promise.resolve("<html>") }),
    FormData: class {
      *[Symbol.iterator]() {}
    },
    URLSearchParams,
    DOMParser: class {
      parseFromString() {
        return savedPage;
      }
    },
    setTimeout,
    clearTimeout,
  };
  vm.runInNewContext(EDITOR_SOURCE, sandbox);
  return { document, heading, form: ids["edit-form"], ids };
}

function serverPage({ title, heading }) {
  const h1 = heading === null ? null : { textContent: heading };
  return {
    title,
    getElementById: (id) => (id === "base_version" ? { value: "2" } : null),
    querySelectorAll: () => [],
    querySelector: (sel) => (sel === "h1" ? h1 : null),
  };
}

async function submitSave(page) {
  page.form.handlers.submit[0]({ preventDefault() {} });
  await new Promise((resolve) => setTimeout(resolve, 20));
}

test("an in-place save refreshes the tab title and the page heading (#36)", async () => {
  const page = loadEditorPage(serverPage({ title: "New heading | Chronicle", heading: "New heading" }));
  await submitSave(page);
  assert.equal(page.document.title, "New heading | Chronicle");
  assert.equal(page.heading.textContent, "New heading");
  assert.equal(page.ids.base_version.value, "2");
});

test("a save whose page carries neither title nor heading leaves both as they were", async () => {
  const page = loadEditorPage(serverPage({ title: "", heading: null }));
  await submitSave(page);
  assert.equal(page.document.title, "Old heading | Chronicle");
  assert.equal(page.heading.textContent, "Old heading");
});
