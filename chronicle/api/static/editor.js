// Chronicle editor page: the markdown editor, save state, local backup, and
// image upload. Original to this repository; the vendored asset it drives is
// /static/vendor/easymde.min.js (see THIRD_PARTY.md). It expects ui.js to have
// loaded first (it calls the global `sanitize`) and `marked` in the page head.
//
// The pure helpers sit outside the DOM-guarded IIFEs below, for the same
// reason ui.js keeps isSafeUrl outside: tests/editor.test.mjs `require`s this
// file under `node --test`, where there is no `document`, so only these
// declarations run.

var BACKUP_PREFIX = "chronicle-backup:";

// The tab title and heading the server rendered for a page, read from a parsed
// copy of it. The heading and the tab title come from the draft title, which a
// save can change, so they are refreshed with the `data-refresh` regions
// rather than left reading the title the page loaded with (#36). Returns null
// when the parsed page has neither, so a partial page never blanks them.
function pageTitles(doc) {
  if (!doc) {
    return null;
  }
  var heading = doc.querySelector("h1");
  var title = doc.title || "";
  var text = heading ? heading.textContent : "";
  if (!title && !text) {
    return null;
  }
  return { title: title, heading: text };
}

// A field-by-field comparison of two editor states ({body, fields}), so "is
// there anything unsaved" and "does the stored backup differ from the server's
// copy" are the same question asked the same way.
function sameState(a, b) {
  if (!a || !b || a.body !== b.body) {
    return false;
  }
  var left = a.fields || {};
  var right = b.fields || {};
  var keys = Object.keys(left).concat(Object.keys(right));
  for (var i = 0; i < keys.length; i++) {
    if ((left[keys[i]] || "") !== (right[keys[i]] || "")) {
      return false;
    }
  }
  return true;
}

// What to do with a stored backup when the editor opens on a draft. `server`
// is the state the server rendered; `serverVersion` the version it is at. A
// backup that matches the server has nothing to offer and is cleared. One that
// differs is offered, and if it was written against an older version the
// server has moved on since (the 409 conflict case, where the browser copy is
// the only copy of the visitor's work).
function backupVerdict(backup, server, serverVersion) {
  if (!backup || typeof backup.body !== "string") {
    return { action: "none" };
  }
  if (sameState(backup, server)) {
    return { action: "clear" };
  }
  var based = typeof backup.baseVersion === "number" ? backup.baseVersion : null;
  return {
    action: "offer",
    serverMoved: based !== null && based !== serverVersion,
    baseVersion: based,
    serverVersion: serverVersion,
  };
}

// What a backup write should do right now. While an earlier backup is still
// on offer (the banner is showing and the visitor has chosen neither Restore
// nor Discard) the stored copy is the only copy of their work, so nothing
// may overwrite or clear it: leaving the page unanswered used to clear it,
// because the editor then matched the server.
function backupWriteAction(now, saved, offerPending) {
  if (offerPending) {
    return "skip";
  }
  return sameState(now, saved) ? "clear" : "write";
}

// One tab's handle on the browser's single backup slot for a draft. The slot
// is shared by every tab open on the post, so a tab may only remove what it is
// entitled to remove: the entry it wrote itself, or one holding exactly the
// state it has just saved. A clean tab that is switched away from or closed
// used to clear the slot unconditionally, taking another tab's unsaved work
// with it. `storage` is anything with getItem/setItem/removeItem (or null when
// the browser blocks it); every access is guarded because any of them can throw.
function makeBackupStore(storage, key) {
  var own = null;
  return {
    read: function () {
      try {
        return JSON.parse(storage.getItem(key) || "null");
      } catch (e) {
        return null;
      }
    },
    write: function (state, baseVersion) {
      var raw = JSON.stringify({
        body: state.body,
        fields: state.fields,
        baseVersion: baseVersion,
        savedAt: new Date().toISOString(),
      });
      try {
        storage.setItem(key, raw);
        own = raw;
      } catch (e) {
        // storage full or blocked: the save indicator still says what is unsaved
      }
    },
    // Clear only an entry this tab wrote, or one that matches `saved`, the
    // state this tab knows the server now holds.
    clear: function (saved) {
      try {
        var raw = storage.getItem(key);
        if (raw === null) {
          return;
        }
        var stored = null;
        try {
          stored = JSON.parse(raw);
        } catch (e) {
          // an unreadable entry is nobody's work: only this tab's own write clears it
        }
        if (raw === own || (stored && sameState(stored, saved))) {
          storage.removeItem(key);
          own = null;
        }
      } catch (e) {
        // nothing more to do client-side
      }
    },
    // The visitor's explicit Discard: theirs to decide, whoever wrote it.
    discard: function () {
      try {
        storage.removeItem(key);
      } catch (e) {
        // nothing more to do client-side
      }
      own = null;
    },
  };
}

// What the conflict page does with the browser's backup slot. The attempted
// state ({body, fields}) is stored only when the slot holds nothing that
// carries it: an entry with the same body and the same fields already is that
// work, and an entry that differs in either (a body, or just an older title or
// tags) is earlier work nobody has answered for (an offered backup the visitor
// ignored, or another tab's), which this page must not overwrite. The
// attempted state stays in the page's own pane instead.
function conflictBackupAction(existing, attempted) {
  if (!existing || typeof existing.body !== "string") {
    return "write";
  }
  return sameState(existing, attempted) ? "same" : "keep";
}

function backupMessage(verdict, whenText) {
  var text = "Unsaved edits from " + whenText + " are stored in this browser.";
  if (verdict.serverMoved) {
    text +=
      " They were written against version " +
      verdict.baseVersion +
      ", and the server is now at version " +
      verdict.serverVersion +
      ": saving them will show the differences instead of overwriting anything.";
  }
  return text;
}

// The server's answer to an image upload -> what the editor does with it.
// `markdown` is the reference to put at the cursor (null for a feature image,
// which the body does not reference); the filename is always the server's.
function uploadOutcome(result) {
  if (!result || result.ok !== true) {
    return {
      ok: false,
      message: (result && result.message) || "upload failed",
    };
  }
  return {
    ok: true,
    insert: typeof result.markdown === "string" ? result.markdown : null,
    filename: result.filename,
    role: result.role,
    url: result.url,
    message:
      result.markdown !== null && result.markdown !== undefined
        ? "Uploaded " + result.filename + " and inserted it at the cursor."
        : "Uploaded " + result.filename + " as the feature image option.",
  };
}

// Bare-filename references in the body point at attached images; the live
// render swaps them for the URL the server serves that image from.
function lookupImageSrc(src, images) {
  var candidates = [src];
  try {
    candidates.push(decodeURIComponent(src));
  } catch (e) {
    // an undecodable reference simply matches only literally
  }
  for (var i = 0; i < images.length; i++) {
    if (candidates.indexOf(images[i].filename) !== -1) {
      return images[i].src;
    }
  }
  return src;
}

function saveStateText(state, detail) {
  switch (state) {
    case "dirty":
      return "Unsaved changes";
    case "saving":
      return "Saving...";
    case "saved":
      return "Saved" + (detail ? " at " + detail : "");
    case "conflict":
      return "Not saved: the post changed underneath you";
    case "error":
      return "Not saved" + (detail ? ": " + detail : "");
    default:
      return "No unsaved changes";
  }
}

// --- The conflict page: keep the attempted text in this browser -------------
(function () {
  if (typeof document === "undefined") {
    return;
  }
  var attempted = document.getElementById("attempted-body");
  if (!attempted) {
    return;
  }
  var key = BACKUP_PREFIX + attempted.getAttribute("data-draft-id");
  var body = attempted.textContent || "";
  var outcome = "unavailable";
  try {
    var fields = {};
    try {
      fields = JSON.parse(attempted.getAttribute("data-fields") || "{}") || {};
    } catch (e) {
      // a malformed attribute leaves the body-only backup
    }
    var existing = JSON.parse(window.localStorage.getItem(key) || "null");
    var action = conflictBackupAction(existing, { body: body, fields: fields });
    if (action === "write") {
      window.localStorage.setItem(
        key,
        JSON.stringify({
          body: body,
          fields: fields,
          baseVersion: Number(attempted.getAttribute("data-base-version")),
          savedAt: new Date().toISOString(),
        })
      );
    }
    outcome = action === "keep" ? "kept-other" : "stored";
  } catch (e) {
    // localStorage unavailable: the page's own attempted-text pane remains
  }
  var note = document.getElementById("backup-note-" + outcome);
  if (note) {
    note.hidden = false;
  }
})();

// --- The editor page ----------------------------------------------------
(function () {
  if (typeof document === "undefined") {
    return;
  }
  var app = document.getElementById("editor-app");
  var form = document.getElementById("edit-form");
  var textarea = document.getElementById("body");
  if (!app || !form || !textarea) {
    return;
  }
  document.body.classList.add("js");

  var draftId = app.getAttribute("data-draft-id");
  var baseInput = document.getElementById("base_version");
  var stateEl = document.getElementById("save-state");
  var uploadEl = document.getElementById("upload-state");
  var backupKey = BACKUP_PREFIX + draftId;
  var editor = null;

  // Images uploaded on this page, known the moment the server answers. The
  // images panel is refreshed only after the reference is in the body, so the
  // render that insertion triggers cannot find the new image in the panel yet.
  var uploaded = [];

  function attachedImages() {
    var rows = document.querySelectorAll("#images-panel [data-image-filename]");
    return uploaded.concat(
      Array.prototype.map.call(rows, function (row) {
        return {
          filename: row.getAttribute("data-image-filename"),
          src: row.getAttribute("data-image-src"),
        };
      })
    );
  }

  function renderMarkdown(text) {
    // marked passes raw HTML through, and a body is not trusted input (a
    // different consumer token can PUT one): everything reaching the render
    // goes through ui.js's sanitize, which also swaps attached-image
    // references for the URL the server serves them from.
    var images = attachedImages();
    return sanitize(marked.parse(text || ""), function (src) {
      return lookupImageSrc(src, images);
    });
  }

  if (typeof EasyMDE !== "undefined") {
    editor = new EasyMDE({
      element: textarea,
      forceSync: true,
      // EasyMDE would otherwise fetch FontAwesome, and a spelling dictionary,
      // from a CDN. Nothing here reaches the network: toolbar glyphs are
      // Chronicle's own CSS (style.css, .editor-toolbar), and there is no
      // spell checker.
      autoDownloadFontAwesome: false,
      spellChecker: false,
      sideBySideFullscreen: false,
      status: ["lines", "words"],
      minHeight: "26rem",
      previewRender: renderMarkdown,
      toolbar: [
        "bold",
        "italic",
        "heading",
        "|",
        "quote",
        "unordered-list",
        "ordered-list",
        "|",
        "link",
        "image",
        "code",
        "horizontal-rule",
        "table",
        "|",
        "preview",
        "side-by-side",
        "fullscreen",
      ],
    });
    editor.toggleSideBySide();
  }

  function getBody() {
    return editor ? editor.value() : textarea.value;
  }

  function setBody(text) {
    if (editor) {
      editor.value(text);
    } else {
      textarea.value = text;
    }
  }

  // Every named control except the body and the version marker: the title, and
  // the frontmatter panel's inputs that join the form by its id.
  function readFields() {
    var out = {};
    Array.prototype.forEach.call(form.elements, function (el) {
      if (!el.name || el.name === "body" || el.name === "base_version" || el.type === "file") {
        return;
      }
      out[el.name] = el.value;
    });
    return out;
  }

  function writeFields(fields) {
    Object.keys(fields || {}).forEach(function (name) {
      var el = form.elements[name];
      if (el && !el.readOnly && el.type !== "file") {
        el.value = fields[name];
      }
    });
  }

  function currentState() {
    return { body: getBody(), fields: readFields() };
  }

  var saved = currentState();
  var serverVersion = Number(baseInput.value);

  // --- Save state ------------------------------------------------------
  function setState(state, detail) {
    stateEl.setAttribute("data-state", state);
    stateEl.textContent = saveStateText(state, detail);
  }

  function isDirty() {
    return !sameState(currentState(), saved);
  }

  // --- Local backup ---------------------------------------------------
  // Deliberately not EasyMDE's own autosave: its restore can silently put
  // stale text back over newer server content with no prompt. This keeps the
  // browser's copy, offers it on the next visit, and clears it only when the
  // server has the same text.
  var storage = null;
  try {
    storage = window.localStorage;
  } catch (e) {
    // blocked outright: every store call below is then a no-op
  }
  var backup = makeBackupStore(storage, backupKey);

  // True while a stored backup is on offer and unanswered (see
  // backupWriteAction).
  var offerPending = false;

  function writeBackup() {
    var now = currentState();
    var action = backupWriteAction(now, saved, offerPending);
    if (action === "skip") {
      return;
    }
    if (action === "clear") {
      backup.clear(saved);
      return;
    }
    backup.write(now, Number(baseInput.value));
  }

  var banner = document.getElementById("backup-banner");
  var bannerText = document.getElementById("backup-banner-text");
  var pending = backup.read();
  var verdict = backupVerdict(pending, saved, serverVersion);
  if (verdict.action === "clear") {
    backup.clear(saved);
  } else if (verdict.action === "offer" && banner && bannerText) {
    var when;
    try {
      when = new Date(pending.savedAt).toLocaleString();
    } catch (e) {
      when = "an earlier visit";
    }
    bannerText.textContent = backupMessage(verdict, when);
    banner.hidden = false;
    offerPending = true;
  }

  function hideBanner() {
    if (banner) {
      banner.hidden = true;
    }
  }

  var restoreBtn = document.getElementById("backup-restore");
  var discardBtn = document.getElementById("backup-discard");
  if (restoreBtn) {
    restoreBtn.addEventListener("click", function () {
      if (!pending) {
        return;
      }
      offerPending = false;
      setBody(pending.body);
      writeFields(pending.fields);
      // Restoring puts the text back at the version it was written against,
      // so if the server has moved on the next save is refused with the
      // differences shown rather than overwriting what landed meanwhile.
      if (typeof pending.baseVersion === "number") {
        baseInput.value = String(pending.baseVersion);
      }
      hideBanner();
      onChange();
    });
  }
  if (discardBtn) {
    discardBtn.addEventListener("click", function () {
      offerPending = false;
      backup.discard();
      pending = null;
      hideBanner();
    });
  }

  var backupTimer = null;
  function onChange() {
    setState(isDirty() ? "dirty" : "idle");
    if (backupTimer) {
      clearTimeout(backupTimer);
    }
    backupTimer = setTimeout(writeBackup, 800);
  }
  if (editor) {
    editor.codemirror.on("change", onChange);
  } else {
    textarea.addEventListener("input", onChange);
  }
  form.addEventListener("input", onChange);

  function flushBackup() {
    if (backupTimer) {
      clearTimeout(backupTimer);
      backupTimer = null;
    }
    writeBackup();
  }
  window.addEventListener("pagehide", flushBackup);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") {
      flushBackup();
    }
  });

  // --- Regions the server owns ---------------------------------------------
  // After a save or an upload the page swaps in the server's fresh copy of each
  // `data-refresh` region (status, actions, feedback, versions, images) taken
  // from the page `_editor_response` rendered, so none of it is computed twice.
  function refreshRegions(doc) {
    doc.querySelectorAll("[data-refresh]").forEach(function (fresh) {
      var current = document.getElementById(fresh.id);
      if (current) {
        current.replaceWith(fresh);
      }
    });
    var titles = pageTitles(doc);
    var heading = document.querySelector("h1");
    if (titles && titles.title) {
      document.title = titles.title;
    }
    if (titles && titles.heading && heading) {
      heading.textContent = titles.heading;
    }
  }

  // --- Saving ------------------------------------------------------------
  var saving = false;

  function timeText() {
    return new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  function noticeText(doc) {
    var notice = doc.querySelector(".notice");
    return notice ? notice.textContent.trim() : "";
  }

  // The Save button sits in a `data-refresh` region, so a swap replaces the
  // element: always look it up, never hold one. The server marks it
  // `data-locked` while it would refuse a save (a publish run or PR in flight);
  // Ctrl+S must honour that the same as the disabled button does.
  function saveButton() {
    return document.getElementById("save-btn");
  }

  function saveLockReason() {
    var button = saveButton();
    if (button && button.getAttribute("data-locked") !== null) {
      return button.getAttribute("data-locked") || "saving is refused right now";
    }
    return null;
  }

  function setSaveBusy(busy) {
    var button = saveButton();
    if (button && saveLockReason() === null) {
      button.disabled = busy;
    }
  }

  function save() {
    if (saving) {
      return;
    }
    var locked = saveLockReason();
    if (locked !== null) {
      setState("error", locked);
      return;
    }
    saving = true;
    if (editor) {
      editor.codemirror.save();
    }
    var sent = currentState();
    flushBackup();
    setState("saving");
    setSaveBusy(true);
    // The same POST the form makes: same URL, same fields, same origin, the
    // browser adds Origin, and the server authenticates the ui token itself.
    fetch(form.action, {
      method: "POST",
      credentials: "same-origin",
      body: new URLSearchParams(new FormData(form)),
    })
      .then(function (resp) {
        return resp.text().then(function (html) {
          return { status: resp.status, html: html };
        });
      })
      .then(function (result) {
        var doc = new DOMParser().parseFromString(result.html, "text/html");
        if (result.status === 200) {
          var fresh = doc.getElementById("base_version");
          if (fresh) {
            baseInput.value = fresh.value;
          }
          refreshRegions(doc);
          saved = sent;
          if (sameState(currentState(), saved)) {
            if (!offerPending) {
              backup.clear(saved);
            }
            setState("saved", timeText());
          } else {
            writeBackup();
            setState("dirty");
          }
          return;
        }
        if (result.status === 409 && doc.getElementById("attempted-body")) {
          // A stale save. The backup was written above, so the visitor's text
          // survives this page being replaced by the conflict view.
          setState("conflict");
          document.open();
          document.write(result.html);
          document.close();
          return;
        }
        setState("error", noticeText(doc) || "the server refused the save");
      })
      .catch(function () {
        setState("error", "could not reach the server; your text is kept in this browser");
      })
      .then(function () {
        saving = false;
        setSaveBusy(false);
      });
  }

  form.addEventListener("submit", function (event) {
    if (typeof fetch === "undefined") {
      return;
    }
    event.preventDefault();
    save();
  });
  document.addEventListener("keydown", function (event) {
    if ((event.ctrlKey || event.metaKey) && event.key === "s") {
      event.preventDefault();
      save();
    }
  });
  // An action button (Preview, Publish and the rest) works on the last saved
  // version, not on what is in the editor.
  document.addEventListener("submit", function (event) {
    var target = event.target;
    if (target && target.classList && target.classList.contains("action-form") && isDirty()) {
      flushBackup();
      if (!window.confirm("You have unsaved changes. This works on the last saved version. Continue?")) {
        event.preventDefault();
      }
    }
  });

  // --- Images --------------------------------------------------------------
  function say(message, isError) {
    if (uploadEl) {
      uploadEl.textContent = message;
      uploadEl.setAttribute("data-error", isError ? "1" : "0");
    }
  }

  function addFeatureOption(filename, select) {
    var featureSelect = document.getElementById("featureImage");
    if (!featureSelect) {
      return;
    }
    var found = Array.prototype.some.call(featureSelect.options, function (option) {
      return option.value === filename;
    });
    if (!found) {
      var option = document.createElement("option");
      option.value = filename;
      option.textContent = filename;
      featureSelect.appendChild(option);
    }
    if (select) {
      featureSelect.value = filename;
    }
  }

  function refreshFromServer() {
    return fetch(window.location.pathname, { credentials: "same-origin" })
      .then(function (resp) {
        return resp.text();
      })
      .then(function (html) {
        refreshRegions(new DOMParser().parseFromString(html, "text/html"));
      });
  }

  function uploadOne(file, position) {
    var roleEl = document.getElementById("role");
    var body = new FormData();
    body.append("file", file);
    body.append("role", roleEl ? roleEl.value : "inline");
    say("Uploading " + file.name + "...", false);
    return fetch("/content/drafts/" + encodeURIComponent(draftId) + "/images", {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      body: body,
    })
      .then(function (resp) {
        return resp.json().catch(function () {
          return { ok: false, message: "upload failed (" + resp.status + ")" };
        });
      })
      .then(function (result) {
        var outcome = uploadOutcome(result);
        if (!outcome.ok) {
          say(outcome.message, true);
          return;
        }
        if (outcome.url) {
          uploaded.push({ filename: outcome.filename, src: outcome.url });
        }
        if (outcome.insert !== null) {
          if (editor) {
            if (position) {
              editor.codemirror.setCursor(position);
            }
            editor.codemirror.replaceSelection(outcome.insert);
            editor.codemirror.focus();
          } else {
            textarea.value += "\n" + outcome.insert + "\n";
          }
          addFeatureOption(outcome.filename, false);
        } else {
          addFeatureOption(outcome.filename, true);
        }
        onChange();
        say(outcome.message, false);
        return refreshFromServer();
      })
      .catch(function () {
        say("Upload failed: could not reach the server.", true);
      });
  }

  function uploadAll(files, position) {
    var images = Array.prototype.filter.call(files, function (file) {
      return file.type.indexOf("image/") === 0;
    });
    if (images.length === 0) {
      say("That is not an image.", true);
      return;
    }
    images.reduce(function (chain, file) {
      return chain.then(function () {
        return uploadOne(file, position);
      });
    }, Promise.resolve());
  }

  function hasFiles(event) {
    var types = (event.dataTransfer && event.dataTransfer.types) || [];
    return Array.prototype.indexOf.call(types, "Files") !== -1;
  }

  var container = document.querySelector(".EasyMDEContainer") || form;
  ["dragenter", "dragover"].forEach(function (name) {
    container.addEventListener(
      name,
      function (event) {
        if (hasFiles(event)) {
          event.preventDefault();
          container.classList.add("drop-active");
        }
      },
      true
    );
  });
  container.addEventListener(
    "dragleave",
    function () {
      container.classList.remove("drop-active");
    },
    true
  );
  container.addEventListener(
    "drop",
    function (event) {
      container.classList.remove("drop-active");
      if (!hasFiles(event)) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      var position = editor
        ? editor.codemirror.coordsChar({ left: event.clientX, top: event.clientY }, "window")
        : null;
      uploadAll(event.dataTransfer.files, position);
    },
    true
  );
  if (editor) {
    editor.codemirror.on("paste", function (cm, event) {
      var files = event.clipboardData && event.clipboardData.files;
      if (files && files.length > 0) {
        event.preventDefault();
        uploadAll(files, null);
      }
    });
  }

  // The images panel is swapped after every upload, so its controls are
  // reached through the document rather than bound once.
  document.addEventListener("change", function (event) {
    var target = event.target;
    if (target && target.id === "file" && target.files && target.files.length > 0) {
      uploadAll(target.files, null);
      target.value = "";
    }
  });
  ["dragenter", "dragover"].forEach(function (name) {
    document.addEventListener(name, function (event) {
      var zone = event.target.closest && event.target.closest("#dropzone");
      if (zone && hasFiles(event)) {
        event.preventDefault();
        zone.classList.add("drop-active");
      }
    });
  });
  document.addEventListener("dragleave", function (event) {
    var zone = event.target.closest && event.target.closest("#dropzone");
    if (zone) {
      zone.classList.remove("drop-active");
    }
  });
  document.addEventListener("drop", function (event) {
    var zone = event.target.closest && event.target.closest("#dropzone");
    if (zone && hasFiles(event)) {
      event.preventDefault();
      zone.classList.remove("drop-active");
      uploadAll(event.dataTransfer.files, null);
    }
  });
  document.addEventListener("submit", function (event) {
    if (event.target && event.target.id === "image-form" && typeof fetch !== "undefined") {
      event.preventDefault();
      var input = document.getElementById("file");
      if (input && input.files && input.files.length > 0) {
        uploadAll(input.files, null);
        input.value = "";
      }
    }
  });
})();

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    sameState: sameState,
    pageTitles: pageTitles,
    backupVerdict: backupVerdict,
    backupWriteAction: backupWriteAction,
    makeBackupStore: makeBackupStore,
    conflictBackupAction: conflictBackupAction,
    backupMessage: backupMessage,
    uploadOutcome: uploadOutcome,
    lookupImageSrc: lookupImageSrc,
    saveStateText: saveStateText,
  };
}
