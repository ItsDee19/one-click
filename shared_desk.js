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

function deskMoney(value, digits) {
  if (typeof value !== "number" || !Number.isFinite(value)) { return "—"; }
  digits = digits === undefined ? 2 : digits;
  return "₹" + value.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

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
    var btn = el("theme");
    if (btn) {
      var label = document.documentElement.getAttribute("data-theme") === "light"
        ? "Switch to dark theme" : "Switch to light theme";
      btn.setAttribute("aria-label", label);
      btn.title = label;
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

/* One request boundary for all desks: failed HTTP responses never masquerade
   as empty datasets. Timers and abort listeners are released on every path. */
function deskRequest(path, options) {
  options = options || {};
  var controller = new AbortController();
  var callerSignal = options.signal;
  var timedOut = false;
  // Cold research endpoints parse filings or scan a live universe. Their
  // existing server work can take minutes; status and actions stay bounded.
  var timeout = path === "/ipos" || path === "/intraday" ? 180000 : 20000;
  var timer = setTimeout(function () { timedOut = true; controller.abort(); }, timeout);
  function cancel() { controller.abort(); }
  if (callerSignal) {
    if (callerSignal.aborted) { cancel(); }
    else { callerSignal.addEventListener("abort", cancel, { once: true }); }
  }
  var requestOptions = Object.assign({}, options, { signal: controller.signal, cache: "no-store" });
  return fetch(api(path), requestOptions).then(function (response) {
    return response.json().catch(function () {
      throw new Error("The server returned an unreadable response. Try again.");
    }).then(function (data) {
      if (!response.ok) {
        var error = new Error(data.error || data.message || "The server could not complete the request. Try again.");
        error.status = response.status;
        throw error;
      }
      return data;
    });
  }).catch(function (error) {
    if (timedOut) { throw new Error("The request took too long. Check your connection and try again."); }
    throw error;
  }).finally(function () {
    clearTimeout(timer);
    if (callerSignal) { callerSignal.removeEventListener("abort", cancel); }
  });
}

/* Reusable read-only loading and retry. Previously rendered evidence is kept
   in place while a refresh is pending or fails. Superseded results are ignored. */
var deskLoads = new WeakMap();
function deskLoad(path, render, errorElement) {
  var region = typeof errorElement === "string" ? el(errorElement) : errorElement;
  if (!region) { return Promise.reject(new Error("Missing loading status region")); }
  var previous = deskLoads.get(region);
  if (previous) { previous.abort(); }
  var controller = new AbortController();
  deskLoads.set(region, controller);
  region.setAttribute("role", "status");
  region.setAttribute("aria-live", "polite");
  region.setAttribute("aria-busy", "true");
  region.hidden = false;
  region.classList.remove("err");
  region.classList.add("banner", "show");
  region.replaceChildren();
  var spinner = document.createElement("span");
  spinner.className = "spinner";
  spinner.setAttribute("aria-hidden", "true");
  region.append(spinner, document.createTextNode(" Loading research…"));
  return deskRequest(path, { signal: controller.signal }).then(function (data) {
    if (deskLoads.get(region) !== controller) { return; }
    render(data);
    region.hidden = true;
    region.replaceChildren();
  }).catch(function (error) {
    if (deskLoads.get(region) !== controller || error.name === "AbortError") { return; }
    region.classList.add("err");
    region.replaceChildren();
    var message = document.createElement("span");
    message.textContent = "Research could not be loaded. " + (error.message === "Failed to fetch" ? "Check your connection and try again." : error.message);
    var retry = document.createElement("button");
    retry.type = "button";
    retry.className = "ghost";
    retry.textContent = "Retry loading";
    retry.addEventListener("click", function () { deskLoad(path, render, region); });
    region.append(message, retry);
  }).finally(function () {
    if (deskLoads.get(region) === controller) { region.setAttribute("aria-busy", "false"); }
  });
}

/* Local list searches stay immediate and shareable, without disrupting IME. */
function deskSearch(inputId, clearId, onChange) {
  var input = el(inputId), clear = el(clearId), composing = false;
  if (!input || !clear) { return ""; }
  var initial = new URLSearchParams(location.search).get("q") || "";
  input.value = initial;
  function paint() { clear.hidden = !input.value; }
  function commit() {
    if (composing) { return; }
    paint();
    var url = new URL(location.href);
    if (input.value) { url.searchParams.set("q", input.value); }
    else { url.searchParams.delete("q"); }
    history.replaceState(null, "", url);
    onChange(input.value);
  }
  input.addEventListener("compositionstart", function () { composing = true; });
  input.addEventListener("compositionend", function () { composing = false; commit(); });
  input.addEventListener("input", commit);
  input.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !event.isComposing) { input.value = ""; commit(); }
  });
  clear.addEventListener("click", function () { input.value = ""; composing = false; commit(); input.focus(); });
  window.addEventListener("popstate", function () {
    input.value = new URLSearchParams(location.search).get("q") || "";
    paint(); onChange(input.value);
  });
  paint();
  return initial;
}

(function () {
  var path = location.pathname.replace(/\.html$/, "").replace(/\/$/, "");
  var active = path.indexOf("intraday") !== -1 ? "intraday" : path.indexOf("quality") !== -1 ? "quality" : path.indexOf("ipo") !== -1 ? "ipo" : "overview";
  var labels = { overview: "Overview", intraday: "Intraday desk", quality: "Quality screen", ipo: "IPO desk" };
  document.querySelectorAll("[data-desk]").forEach(function (link) {
    if (link.dataset.desk === active) { link.setAttribute("aria-current", "page"); }
  });
  text(el("desk-current"), labels[active]);
  var skip = el("desk-skip");
  if (skip) { skip.href = active === "overview" ? "#board" : "#main-content"; }
  text(el("desk-date"), new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "Asia/Kolkata" }).format(new Date()));
})();
