// Node tests for the pure helpers in chronicle/api/static/editor.js: the
// backup verdict, the upload outcome, the image-reference lookup, and the
// save-state text. The DOM parts of the file are behind a `document` guard, so
// `require`ing it under node runs only these declarations. Run by
// tests/test_js.py (part of `make check` when node is on PATH), or directly:
// `node --test tests/editor.test.mjs`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { sameState, backupVerdict, backupMessage, uploadOutcome, lookupImageSrc, saveStateText } =
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
  });
  assert.equal(outcome.ok, true);
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
