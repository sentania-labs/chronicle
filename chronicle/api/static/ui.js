// Chronicle UI: minimal hand-written client-side behaviour.
// Not vendored: this file is original to Chronicle. The only vendored asset
// is /static/vendor/marked.min.js (see THIRD_PARTY.md).

(function () {
  var textarea = document.getElementById("body");
  var preview = document.getElementById("preview-pane");
  if (!textarea || !preview || typeof marked === "undefined") {
    return;
  }
  function render() {
    preview.innerHTML = marked.parse(textarea.value || "");
  }
  textarea.addEventListener("input", render);
  render();
})();
