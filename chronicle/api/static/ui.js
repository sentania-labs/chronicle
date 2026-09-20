// Chronicle UI: minimal hand-written client-side behaviour.
// Not vendored: this file is original to Chronicle. The vendored assets are
// /static/vendor/marked.min.js and the EasyMDE pair (see THIRD_PARTY.md); the
// editor page's own behaviour is in editor.js, which calls sanitize below.
//
// isSafeUrl and sanitize are declared outside the DOM-guarded IIFE below,
// not because they run outside the browser, but so tests/ui_sanitize.test.mjs
// can `require` this same file under `node --test` (no global `document`
// makes the IIFE's own guard return immediately, leaving only these two
// function declarations, which node's own `URL` can exercise directly).
// A literal-string check against "javascript:" is not enough: a browser
// strips ASCII whitespace (including a literal newline written as the
// entity `&#x0A;`) while parsing a URL before deciding its scheme, so
// `java&#x0A;script:alert(1)` parses to an attribute value that starts
// with `java\nscript:`, sails past a literal check, and still runs when
// clicked (found in a round C5 review, after the review that added the
// literal check). Bypass payloads this guards against, for both `href`
// and `src`: a raw newline or tab inside the scheme, a NUL byte, mixed
// case (`jAvAsCrIpT:`), `data:text/html`, and `vbscript:`. Resolving the
// value through the URL parser (same normalisation a browser's navigation
// uses) and comparing `.protocol` catches all of these in one place,
// instead of pattern-matching each one by hand.
function isSafeUrl(value) {
  var trimmed = (value || "").trim();
  if (trimmed === "") {
    return true;
  }
  var base = typeof document !== "undefined" ? document.baseURI : "http://localhost/";
  var resolved;
  try {
    resolved = new URL(trimmed, base);
  } catch (e) {
    return false;
  }
  return (
    resolved.protocol === "http:" ||
    resolved.protocol === "https:" ||
    resolved.protocol === "mailto:"
  );
}

function sanitize(html, resolveImageSrc) {
  var doc = new DOMParser().parseFromString(html, "text/html");
  // marked renders raw HTML in the markdown source through unchanged (that
  // is standard GFM behaviour, not a bug in marked); a draft's body is not
  // trusted input here, because a different consumer token can PUT one and
  // the editor's own browser is what later opens this editor and renders it. A
  // round C5 review confirmed this without sanitising: a body containing
  // `<img src=x onerror=...>` executes on load, same-origin, with reach to
  // every reserved action. This strips the handful of constructs that
  // matter (script-bearing tags, event handler attributes, unsafe URLs,
  // `srcset`) rather than pulling in a full third-party sanitiser for one
  // preview pane.
  //
  // The preview pane sits inside the editor's own <form>, next to controls
  // that carry reserved actions. Nothing a body contains may become, join,
  // or point at a control: form-owning elements are removed outright, and the
  // attributes that submit somewhere else (`formaction` and its siblings),
  // attach an element to a form (`form`), or name an element the page's own
  // script and labels look up (`id`, `name`, `for`) are stripped. That is the
  // boundary; it does not lean on the parser ignoring a nested <form> or on
  // editor.js cancelling the submit.
  [
    "script",
    "style",
    "iframe",
    "object",
    "embed",
    "link",
    "meta",
    "base",
    "template",
    "form",
    "button",
    "input",
    "select",
    "option",
    "optgroup",
    "datalist",
    "textarea",
    "fieldset",
    "output",
  ].forEach(function (tag) {
    doc.querySelectorAll(tag).forEach(function (el) {
      el.remove();
    });
  });
  var stripped = [
    "srcset",
    "form",
    "formaction",
    "formmethod",
    "formtarget",
    "formenctype",
    "formnovalidate",
    "id",
    "name",
    "for",
  ];
  doc.querySelectorAll("*").forEach(function (el) {
    stripped.forEach(function (name) {
      el.removeAttribute(name);
    });
    Array.prototype.slice.call(el.attributes).forEach(function (attr) {
      var name = attr.name.toLowerCase();
      var isUrlAttr = name === "href" || name === "src" || name === "xlink:href";
      if (name.indexOf("on") === 0 || (isUrlAttr && !isSafeUrl(attr.value))) {
        el.removeAttribute(attr.name);
      } else if (name === "src" && el.tagName === "IMG" && resolveImageSrc) {
        // Applied after the safety check, on the same parsed document, so the
        // one parse that was sanitised is the one that is serialised. The
        // resolver only ever returns the original value or a URL the server
        // built for an image attached to this draft.
        el.setAttribute("src", resolveImageSrc(attr.value));
      }
    });
  });
  return doc.body.innerHTML;
}

(function () {
  if (typeof document === "undefined") {
    return;
  }
  // The CSP's default-src 'self' has no script-src exception for inline
  // event handlers, so the status filter's auto-submit-on-change lives
  // here instead of an onchange="" attribute; a plain <select> with no
  // submit button degrades to doing nothing without this.
  var status = document.getElementById("status");
  if (!status || !status.form) {
    return;
  }
  status.addEventListener("change", function () {
    status.form.submit();
  });
})();

if (typeof module !== "undefined" && module.exports) {
  module.exports = { isSafeUrl: isSafeUrl, sanitize: sanitize };
}
