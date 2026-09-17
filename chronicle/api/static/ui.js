// Chronicle UI: minimal hand-written client-side behaviour.
// Not vendored: this file is original to Chronicle. The only vendored asset
// is /static/vendor/marked.min.js (see THIRD_PARTY.md).
//
// isSafeUrl and sanitize are declared outside the DOM-guarded IIFE below,
// not because they run outside the browser, but so tests/ui_sanitize.test.mjs
// can `require` this same file under `node --test` (a global `document` with
// no `getElementById("body")` makes the IIFE's own guard return immediately,
// leaving only these two function declarations, which node's own `URL` and
// (in the sanitize case) a jsdom-free DOMParser stub can exercise directly).
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

function sanitize(html) {
  var doc = new DOMParser().parseFromString(html, "text/html");
  // marked renders raw HTML in the markdown source through unchanged (that
  // is standard GFM behaviour, not a bug in marked); a draft's body is not
  // trusted input here, because a different consumer token can PUT one and
  // Scott's own browser is what later opens this editor and renders it. A
  // round C5 review confirmed this without sanitising: a body containing
  // `<img src=x onerror=...>` executes on load, same-origin, with reach to
  // every reserved action. This strips the handful of constructs that
  // matter (script-bearing tags, event handler attributes, unsafe URLs,
  // `srcset`) rather than pulling in a full third-party sanitiser for one
  // preview pane.
  ["script", "style", "iframe", "object", "embed", "link", "meta", "base"].forEach(function (tag) {
    doc.querySelectorAll(tag).forEach(function (el) {
      el.remove();
    });
  });
  doc.querySelectorAll("*").forEach(function (el) {
    el.removeAttribute("srcset");
    Array.prototype.slice.call(el.attributes).forEach(function (attr) {
      var name = attr.name.toLowerCase();
      var isUrlAttr = name === "href" || name === "src" || name === "xlink:href";
      if (name.indexOf("on") === 0 || (isUrlAttr && !isSafeUrl(attr.value))) {
        el.removeAttribute(attr.name);
      }
    });
  });
  return doc.body.innerHTML;
}

(function () {
  if (typeof document === "undefined") {
    return;
  }
  var textarea = document.getElementById("body");
  var preview = document.getElementById("preview-pane");
  if (!textarea || !preview || typeof marked === "undefined") {
    return;
  }

  function render() {
    preview.innerHTML = sanitize(marked.parse(textarea.value || ""));
  }
  textarea.addEventListener("input", render);
  render();
})();

if (typeof module !== "undefined" && module.exports) {
  module.exports = { isSafeUrl: isSafeUrl, sanitize: sanitize };
}
