/* ===========================================================================
   shared_desk.js — the plumbing every page needs, defined once.

   Theme handling and backend resolution were copied into all four pages and
   had already drifted apart: same intent, four slightly different versions.
   This is the single copy. page_render.py inlines it, so the pages stay
   self-contained single files with no extra request and no flash of the
   wrong theme on load.

   Provides, as globals: el, text, esc, API, api(), and an auto-wired theme
   toggle for any page carrying #theme.
   =========================================================================== */

function el(id) { return document.getElementById(id); }

function text(node, value) {
  if (node && node.textContent !== String(value)) { node.textContent = value; }
}

function esc(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* Where the backend lives. `?api=` wins and is remembered, so one deployed
   frontend can be pointed at a local backend while developing; otherwise the
   value build_web.py baked in is used. */
var API = (function () {
  function clean(u) { return u ? String(u).trim().replace(/\/+$/, "") : ""; }
  try {
    var fromUrl = clean(new URLSearchParams(location.search).get("api"));
    if (fromUrl) { localStorage.setItem("dalal.api", fromUrl); return fromUrl; }
    var saved = clean(localStorage.getItem("dalal.api"));
    if (saved) { return saved; }
  } catch (e) { /* private mode: fall through to the baked default */ }
  return clean(window.__API_BASE__);
})();

function api(path) { return API + path; }

/* The toggle. The theme itself is applied before first paint by the snippet
   in <head>; this only handles switching it afterwards. */
(function () {
  var SUN = "M12 17a5 5 0 100-10 5 5 0 000 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4"
          + "M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4";
  var MOON = "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z";

  function paint() {
    var icon = el("themeicon");
    if (icon) {
      icon.setAttribute("d",
        document.documentElement.getAttribute("data-theme") !== "light" ? MOON : SUN);
    }
  }

  function wire() {
    var btn = el("theme");
    paint();
    if (!btn) { return; }
    btn.addEventListener("click", function () {
      var next = document.documentElement.getAttribute("data-theme") === "light"
        ? "dark" : "light";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("dalal.theme", next); } catch (e) { /* private mode */ }
      paint();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
