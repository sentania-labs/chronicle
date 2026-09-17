// Chronicle UI: minimal hand-written client-side behaviour.
// Not vendored: this file is original to Chronicle. The only vendored asset
// is /static/vendor/marked.min.js (see THIRD_PARTY.md).

(function () {
  var textarea = document.getElementById("body");
  var preview = document.getElementById("preview-pane");
  if (!textarea || !preview || typeof marked === "undefined") {
    return;
  }

  // marked renders raw HTML in the markdown source through unchanged (that
  // is standard GFM behaviour, not a bug in marked); a draft's body is not
  // trusted input here, because a different consumer token can PUT one and
  // Scott's own browser is what later opens this editor and renders it. A
  // round C5 review confirmed this without sanitising: a body containing
  // `<img src=x onerror=...>` executes on load, same-origin, with reach to
  // every reserved action. This strips the handful of constructs that
  // matter (script-bearing tags, event handler attributes, javascript:
  // URLs) rather than pulling in a full third-party sanitiser for one
  // preview pane.
  function sanitize(html) {
    var doc = new DOMParser().parseFromString(html, "text/html");
    ["script", "style", "iframe", "object", "embed", "link", "meta", "base"].forEach(function (tag) {
      doc.querySelectorAll(tag).forEach(function (el) {
        el.remove();
      });
    });
    doc.querySelectorAll("*").forEach(function (el) {
      Array.prototype.slice.call(el.attributes).forEach(function (attr) {
        var name = attr.name.toLowerCase();
        var value = attr.value.trim().toLowerCase();
        var isUrlAttr = name === "href" || name === "src" || name === "xlink:href";
        if (name.indexOf("on") === 0 || (isUrlAttr && value.indexOf("javascript:") === 0)) {
          el.removeAttribute(attr.name);
        }
      });
    });
    return doc.body.innerHTML;
  }

  function render() {
    preview.innerHTML = sanitize(marked.parse(textarea.value || ""));
  }
  textarea.addEventListener("input", render);
  render();
})();
