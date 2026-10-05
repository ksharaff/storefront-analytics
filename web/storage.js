// Storefront Storage — the storage room's pick & pack app (served at /storage).
// No build step, no framework, same approach as the dashboard's app.js.
//
// Screens, one per URL hash, so the browser's Back button works and a
// refresh stays on the same screen:
//   #/                        start: Trendyol orders | Website orders
//   #/website                 two tiles: items to collect, orders to pack
//   #/website/items           the pick list (printable)
//   #/website/orders          waiting orders in three tables: 1, 2, 3+ pieces
//   #/website/order/5007      scan one order, enter boxes, approve
//   #/website/packed          packed orders: order no. / BOL no. / boxes
//
// Scanning: a USB or Bluetooth barcode scanner "types" the code and presses
// Enter, like a keyboard. The scan box catches that; keys pressed while the
// focus is somewhere else are sent to the scan box too, so a scan is never
// lost because someone clicked a button first.
//
// The scan check happens twice: here, to tick items off as they are scanned,
// and on the server (app/storage.py, check_scans), which refuses an Approve
// that does not match the order exactly.
//
// Safety: store data (product names, SKUs) is inserted with textContent,
// never innerHTML.

import { STORAGE, STRINGS, STATUSES } from "./i18n.js";
import { code128Svg } from "./barcode.js";

const REFRESH_MS = 30_000;   // lists refresh themselves; the scan screen does not
const API = "/api/storage/website";

// ---------------------------------------------------------------------------
// Language, formatting, small helpers
// ---------------------------------------------------------------------------
const state = { lang: initialLang() };

function initialLang() {
  let saved = null;
  try { saved = localStorage.getItem("lang"); } catch { /* storage blocked */ }
  if (saved === "en" || saved === "ar") return saved;
  return (navigator.language || "").startsWith("ar") ? "ar" : "en";
}

/** Translate a key; function entries take arguments. A missing key shows
 *  the key itself, which is easy to spot on screen. */
function t(key, ...args) {
  const v = STORAGE[state.lang][key];
  if (v === undefined) return key;
  return typeof v === "function" ? v(...args) : v;
}

// Same locale rules as the dashboard: Western digits, Gregorian calendar.
const locale = () => (state.lang === "ar" ? "ar-SA-u-nu-latn-ca-gregory" : "en-GB");
const fmtNum = (n) => (n == null ? "—" : new Intl.NumberFormat(locale()).format(n));

/** A Riyadh wall-clock time from the API ("2026-09-03T11:00:00", no zone),
 *  shown as it is. Formatted "as UTC" so the viewer's own timezone cannot
 *  shift it — the same trick the dashboard uses. */
function fmtWhen(local) {
  if (!local) return "—";
  return new Intl.DateTimeFormat(locale(), {
    timeZone: "UTC", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  }).format(new Date(local + "Z"));
}
/** A real UTC timestamp from the API ("...+00:00"), shown in Riyadh time. */
const fmtUtc = (iso) => new Intl.DateTimeFormat(locale(), {
  timeZone: "Asia/Riyadh", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
}).format(new Date(iso));
/** Today's date in Riyadh, written out: "Monday, 28 September 2026". */
const todayLong = () => new Intl.DateTimeFormat(locale(), {
  timeZone: "Asia/Riyadh", weekday: "long", day: "numeric", month: "long", year: "numeric",
}).format(new Date());
/** A day ("2026-09-28") written out: "Monday, 28 September 2026". */
const dayLong = (day) => new Intl.DateTimeFormat(locale(), {
  timeZone: "UTC", weekday: "long", day: "numeric", month: "long", year: "numeric",
}).format(new Date(day + "T00:00:00Z"));
/** "now" in Riyadh, for the printed page header. */
const nowInRiyadh = () => new Intl.DateTimeFormat(locale(), {
  timeZone: "Asia/Riyadh", dateStyle: "medium", timeStyle: "short",
}).format(new Date());

const statusName = (slug) => STATUSES[state.lang][slug] || slug;

/** How a scanned code and a stored SKU are compared — the same rule as
 *  normalise() in app/storage.py: trimmed, case ignored. */
const normalise = (code) => (code || "").trim().toLowerCase();

/** el("div", {class: "x"}, "text", child, ...). Strings become text nodes. */
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

// Line icons of our own (24x24, stroked with currentColor), like the dashboard's.
const ICONS = {
  box:     ["M3 7.5l9-4.5 9 4.5v9l-9 4.5-9-4.5z", "M3 7.5l9 4.5 9-4.5", "M12 12v9"],
  list:    ["M9 6h11", "M9 12h11", "M9 18h11", "M4 6h.01", "M4 12h.01", "M4 18h.01"],
  bag:     ["M5 8h14l-1.2 12H6.2z", "M9 8V6.5a3 3 0 0 1 6 0V8"],
  truck:   ["M2 6h12v10H2z", "M14 10h4l3 3v3h-7z", "M6 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4z",
            "M17 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4z"],
  print:   ["M6 9V3h12v6", "M6 18H4v-7h16v7h-2", "M7 14h10v7H7z"],
  sync:    ["M20 11a8 8 0 0 0-14.3-4.9L4 8", "M4 4v4h4", "M4 13a8 8 0 0 0 14.3 4.9L20 16", "M20 20v-4h-4"],
  scan:    ["M4 8V5h3", "M17 5h3v3", "M20 16v3h-3", "M7 19H4v-3", "M8 9v6", "M11 9v6", "M14 9v6", "M17 9v6"],
  check:   ["M5 12.5l4.5 4.5L19 7.5"],
  cross:   ["M6.5 6.5l11 11", "M17.5 6.5l-11 11"],
  back:    ["M15 5l-7 7 7 7"],
  sun:     ["M12 16.5a4.5 4.5 0 1 0 0-9 4.5 4.5 0 0 0 0 9z", "M12 2.5v2", "M12 19.5v2",
            "M4.6 4.6l1.4 1.4", "M18 18l1.4 1.4", "M2.5 12h2", "M19.5 12h2", "M4.6 19.4l1.4-1.4", "M18 6l1.4-1.4"],
  moon:    ["M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"],
  calendar: ["M4 6h16v14H4z", "M4 10h16", "M8 3.5v4", "M16 3.5v4"],
  edit:     ["M4 20h4L19 9l-4-4L4 16z", "M13.5 6.5l4 4"],
  // The dashboard's section icons, for the shared tab bar (same drawings as app.js).
  overview:  ["M4 4h7v7H4z", "M13 4h7v4h-7z", "M13 10h7v10h-7z", "M4 13h7v7H4z"],
  sales:     ["M3 17l6-6 4 4 8-8", "M15 7h6v6"],
  products:  ["M3 7.5l9-4.5 9 4.5v9l-9 4.5-9-4.5z", "M3 7.5l9 4.5 9-4.5", "M12 12v9"],
  payments:  ["M3 6h18v12H3z", "M3 10h18", "M7 15h4"],
  warehouse: ["M2.5 6.5h11v10h-11z", "M13.5 9.5h4l3.5 3.5v3.5h-7.5", "M6.5 19.5a2 2 0 1 0 0-4 2 2 0 0 0 0 4z",
              "M17 19.5a2 2 0 1 0 0-4 2 2 0 0 0 0 4z"],
  storage:   ["M3 9l9-5 9 5v11H3z", "M7 20v-7h10v7", "M7 16h10"],
};
function icon(name, cls = "icon") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  for (const d of ICONS[name] || []) {
    const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", d);
    svg.append(p);
  }
  return svg;
}

/** Call the storage API. Errors carry the HTTP status and FastAPI's detail. */
async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(API + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    const err = new Error(typeof d === "string" ? d : d?.message || `HTTP ${res.status}`);
    err.status = res.status;
    err.problems = d?.problems || [];
    err.detail = d;
    throw err;
  }
  return data;
}

/** Short beeps, so a worker hears whether a scan counted without looking up.
 *  Harmless if sound is unavailable. */
let audio = null;
function beep(ok) {
  try {
    audio = audio || new AudioContext();
    const osc = audio.createOscillator(), gain = audio.createGain();
    osc.frequency.value = ok ? 880 : 220;
    osc.type = ok ? "sine" : "square";
    gain.gain.value = 0.08;
    osc.connect(gain).connect(audio.destination);
    osc.start();
    osc.stop(audio.currentTime + (ok ? 0.08 : 0.3));
  } catch { /* no audio: the on-screen message still says it */ }
}

// ---------------------------------------------------------------------------
// Page frame: heading, back button, actions, banners
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);

function frame({ title, sub = "", back = null, actions = [], dated = true, range = null, rangeOpts = {} }) {
  $("page-title").textContent = title;
  $("page-sub").textContent = sub;
  const b = $("back");
  b.hidden = !back;
  if (back) {
    b.href = back;
    b.replaceChildren(icon("back"), el("span", {}, t("back")));
  }
  // Today's date on every Trendyol / Website screen (2026-09-28). Worked
  // out on every draw, so a screen left open past midnight shows the new day
  // at its next 30-second refresh.
  //
  // On the list screens (tiles, pick list, orders) the chip shows the
  // chosen days instead (From 00:00 to the end of To), with two date pickers.
  const today = $("today");
  today.hidden = !dated && !range;
  if (range) today.replaceChildren(...rangeChip(range, rangeOpts).filter(Boolean));
  else if (dated) today.replaceChildren(icon("calendar"), el("span", {}, todayLong()));
  $("page-actions").replaceChildren(...actions);
  document.title = `${title} · ${t("title")}`;
}

function banner(text, kind = "error", action = null) {
  $("banners").replaceChildren(el("div", { class: `banner ${kind}`, role: "alert" }, text,
    action && el("button", { type: "button", class: "link-btn", onclick: action.run }, action.label)));
}
const clearBanner = () => $("banners").replaceChildren();

function applyStatic() {
  document.documentElement.lang = state.lang;
  document.documentElement.dir = t("dir");
  $("brand-name").textContent = t("title");
  const dark = document.documentElement.dataset.theme === "dark";
  const themeBtn = $("theme-toggle");
  themeBtn.replaceChildren(icon(dark ? "sun" : "moon"));
  themeBtn.title = t(dark ? "themeLight" : "themeDark");
  themeBtn.setAttribute("aria-label", themeBtn.title);
  const langBtn = $("lang-toggle");
  langBtn.textContent = t("switchLang");
  langBtn.setAttribute("aria-label", t("switchLangLabel"));
  langBtn.lang = state.lang === "ar" ? "en" : "ar";

  // The dashboard's tabs, with Storage selected. The other tabs are links
  // back to the dashboard, carrying the language along.
  const nav = $("nav");
  nav.setAttribute("aria-label", STRINGS[state.lang].title);
  nav.replaceChildren(
    ...DASHBOARD_SECTIONS.map((s) => el("a", {
      href: `/#section=${s}&lang=${state.lang}`, role: "tab", "aria-selected": "false",
    }, icon(s), STRINGS[state.lang][s])),
    el("a", { href: "#/", role: "tab", "aria-selected": "true" }, icon("storage"), STRINGS[state.lang].storage));
}
// Must match SECTIONS in app.js (minus Storage, which is this page): a tab
// added to the dashboard has to be added here too, or it vanishes from the
// bar while on the storage page (Warehouse did, until 2026-10-01).
const DASHBOARD_SECTIONS = ["overview", "sales", "products", "payments", "warehouse"];

// ---------------------------------------------------------------------------
// The chosen dates: From and To (2026-09-28)
// ---------------------------------------------------------------------------
// The lists show the orders placed from From 00:00 to the end of To (Riyadh
// time; both days included). No dates means the whole current month, 1st to
// last day (2026-10-01; it was yesterday before); the server decides that. Chosen dates travel in the address
// (#/website/items?from=2026-09-27&to=2026-09-28), so a refresh or a link
// keeps them.
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
function chosenDates() {
  const q = new URLSearchParams(location.hash.split("?")[1] || "");
  const from = q.get("from"), to = q.get("to");
  return DATE_RE.test(from || "") && DATE_RE.test(to || "") ? { from, to } : null;
}
/** A screen's address, keeping the chosen dates. */
const withDates = (hash) => {
  const d = chosenDates();
  return d ? `${hash}?from=${d.from}&to=${d.to}` : hash;
};
/** A screen's address keeping EVERYTHING in the current address (dates,
 *  the chosen tab `size`, and `via`: which screen the packed list was
 *  opened from), minus `drop`, plus `set`. Used where Back must return to
 *  exactly the screen the worker came from. */
function withQuery(hash, { drop = [], set = {} } = {}) {
  const q = new URLSearchParams(location.hash.split("?")[1] || "");
  for (const k of drop) q.delete(k);
  for (const [k, v] of Object.entries(set)) q.set(k, v);
  const qs = q.toString();
  return qs ? `${hash}?${qs}` : hash;
}

/** The API query string for the chosen dates, plus any extra parameters. */
function datesQuery(extra = {}) {
  const q = new URLSearchParams(extra);
  const d = chosenDates();
  if (d) { q.set("from", d.from); q.set("to", d.to); }
  const s = q.toString();
  return s ? `?${s}` : "";
}
/** Switch to other dates; null goes back to the default (this month). */
function goToDates(from, to) {
  const path = location.hash.split("?")[0] || "#/website";
  const q = new URLSearchParams();
  if (from && to) { q.set("from", from); q.set("to", to); }
  const size = chosenSize();          // stay on the same group tab
  if (size) q.set("size", size);
  const qs = q.toString();
  location.hash = qs ? `${path}?${qs}` : path;
}

/** "Monday, 28 September 2026", or "27 Sept – 28 Sept 2026" for a range. */
function rangeTitle(r) {
  if (r.from === r.to) return dayLong(r.from);
  const f = (day, opts) => new Intl.DateTimeFormat(locale(), { timeZone: "UTC", ...opts })
    .format(new Date(day + "T00:00:00Z"));
  return `${f(r.from, { day: "numeric", month: "short" })} – ${f(r.to, { day: "numeric", month: "short", year: "numeric" })}`;
}
const rangeWindowText = (r) => t("rangeWindow", fmtWhen(r.start_local), fmtWhen(r.last_local));

/** The heading chip on the list screens: the dates shown, their exact
 *  window, a From and a To picker, and a way back to this month. The
 *  pickers go up to the end of this month (r.max). */
function rangeChip(r, { go = goToDates, windowKey = "rangeWindow", resetLabel = t("backToThisMonth") } = {}) {
  const picker = (value, label) => el("label", { class: "date-field" }, el("span", {}, label),
    el("input", { type: "date", class: "day-picker", value, max: r.max || r.today, "aria-label": label }));
  const fromField = picker(r.from, t("dateFrom"));
  const toField = picker(r.to, t("dateTo"));
  const fromIn = fromField.querySelector("input"), toIn = toField.querySelector("input");
  // Keep From ≤ To while picking: moving one past the other moves both.
  fromIn.addEventListener("change", () => {
    if (!fromIn.value) return;
    if (!toIn.value || toIn.value < fromIn.value) toIn.value = fromIn.value;
    go(fromIn.value, toIn.value);
  });
  toIn.addEventListener("change", () => {
    if (!toIn.value) return;
    if (!fromIn.value || fromIn.value > toIn.value) fromIn.value = toIn.value;
    go(fromIn.value, toIn.value);
  });
  return [
    icon("calendar"),
    el("span", { class: "batch-text" },
      el("strong", {}, rangeTitle(r)),
      el("small", {}, t(windowKey, fmtWhen(r.start_local), fmtWhen(r.last_local)))),
    fromField, toField,
    !r.is_default && el("button", { type: "button", class: "icon-btn latest-btn",
                                    onclick: () => go(null, null) }, resetLabel),
  ];
}

// The "N older orders are still waiting" banner was removed from every
// page on request (2026-10-01). Older orders are reached by choosing an
// earlier From date; the older-orders screen (#/website/older) still works.

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------
let refreshTimer = null;
let renderSeq = 0;          // a slow response for an old screen is ignored

function route() {
  const parts = location.hash.replace(/^#\/?/, "").split("?")[0].split("/").filter(Boolean);
  if (parts.length === 0) return { view: startView };
  if (parts[0] === "trendyol" && parts.length === 1) return { view: trendyolView };
  if (parts[0] !== "website") return { view: notFound };
  if (parts.length === 1) return { view: hubView, refresh: true };
  if (parts[1] === "items" && parts.length === 2) return { view: itemsView, refresh: true };
  if (parts[1] === "orders" && parts.length === 2) return { view: ordersView, refresh: true };
  if (parts[1] === "packed" && parts.length === 2) return { view: packedView, refresh: true };
  if (parts[1] === "loading" && parts.length === 2) return { view: loadingView, refresh: true };
  if (parts[1] === "shipping" && parts.length === 2) return { view: shippingView, refresh: true };
  if (parts[1] === "older" && parts.length === 2) return { view: olderView, refresh: true };
  if (parts[1] === "order" && /^\d+$/.test(parts[2] || "")) {
    return { view: orderView, arg: Number(parts[2]) };
  }
  return { view: notFound };
}

async function render({ quiet = false } = {}) {
  const seq = ++renderSeq;
  const r = route();
  clearTimeout(refreshTimer);
  if (!quiet) {
    clearBanner();
    $("content").classList.add("loading");
    loadMsg = null;      // opening the truck screen anew: no old scan result
    justApproved = null; // a packed order opened later shows its Undo again
  }
  try {
    const node = await r.view(r.arg, () => seq === renderSeq);
    if (seq !== renderSeq) return;
    if (node) $("content").replaceChildren(node);
    if (!quiet) window.scrollTo(0, 0);
  } catch (e) {
    if (seq !== renderSeq) return;
    banner(`${t("loadError")}: ${e.message}`, "error", { label: t("retry"), run: () => render() });
  } finally {
    if (seq === renderSeq) $("content").classList.remove("loading");
  }
  // Lists refresh every 30 s while the tab is visible, keeping the counts
  // current as orders arrive or colleagues pack them.
  if (r.refresh && seq === renderSeq) scheduleRefresh();
}

function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    // Not while a names window is open or someone is typing a name: a
    // redraw would throw their typing away. Try again next round.
    const busy = document.querySelector("dialog[open]")
      || document.activeElement?.classList.contains("names-input")
      || document.activeElement?.classList.contains("order-search")
      || document.activeElement?.classList.contains("load-input")   // scanning at the truck
      || loadBusy;
    if (busy) return scheduleRefresh();
    if (document.visibilityState === "visible") render({ quiet: true });
    else document.addEventListener("visibilitychange", () => render({ quiet: true }), { once: true });
  }, REFRESH_MS);
}

// ---------------------------------------------------------------------------
// Worker names: who collects the pick list, who packs each order
// ---------------------------------------------------------------------------
// Names are typed freely ("Sam", "Ahmed"); names used before are offered
// as suggestions (one shared <datalist>, filled from /workers). The server
// trims them and drops duplicates (app/storage.py, clean_workers).

let knownWorkers = [];
async function loadKnownWorkers() {
  try { knownWorkers = (await api("/workers")).workers; } catch { /* suggestions are optional */ }
  let list = $("worker-suggestions");
  if (!list) { list = el("datalist", { id: "worker-suggestions" }); document.body.append(list); }
  list.replaceChildren(...knownWorkers.map((n) => el("option", { value: n })));
}

/** Names as small tags, or "—" when there are none. */
const nameTags = (names) => (names.length
  ? el("span", { class: "name-tags" }, names.map((n) => el("span", { class: "name-tag" }, n)))
  : el("span", { class: "muted" }, "—"));

// ---------------------------------------------------------------------------
// Colour, size and other options of an order line (from app/storage.py,
// line_options): [{kind: "color" | "size" | "other", label, value}]
// ---------------------------------------------------------------------------
const optionValues = (opts, kind) =>
  (opts || []).filter((o) => o.kind === kind).map((o) => o.value).join(" / ");
const optionLabel = (o) => (o.kind === "color" ? t("colColor") : o.kind === "size" ? t("colSize") : o.label);

/** Small tags for a line's options, e.g. [ازرق] [10 - 12]. Colour and size
 *  come first; the label is in the tooltip. Nothing for plain products. */
function optionTags(opts) {
  if (!opts || !opts.length) return null;
  const order = { color: 0, size: 1, other: 2 };
  return el("span", { class: "opt-tags" }, [...opts].sort((a, b) => order[a.kind] - order[b.kind])
    .map((o) => el("span", { class: `opt-tag opt-${o.kind}`, title: optionLabel(o) }, o.value)));
}

/** "Colour: ازرق · Size: 10 - 12", for the scanning screen. */
const optionText = (opts) => (opts || []).map((o) => `${optionLabel(o)}: ${o.value}`).join(" · ");

/** A box to type names into: each name becomes a tag with ×; Enter or a
 *  comma adds the typed name. onChange(names) runs after every change and
 *  may return a promise (a save); the box shows "Saving…" / "Saved". */
function namesEditor(initial, onChange, { label, id }) {
  let names = [...initial];
  const status = el("span", { class: "names-status muted small", "aria-live": "polite" });
  const input = el("input", {
    id, class: "names-input", type: "text", list: "worker-suggestions", autocomplete: "off",
    placeholder: t("addName"), "aria-label": label, maxlength: "40",
  });
  const tags = el("span", { class: "name-tags" });
  const drawTags = () => tags.replaceChildren(...names.map((n) => el("span", { class: "name-tag" }, n,
    el("button", { type: "button", class: "name-x", "aria-label": t("removeName", n), title: t("removeName", n),
                   onclick: (e) => { e.stopPropagation(); set(names.filter((x) => x !== n)); } }, "×"))));
  async function set(next) {
    names = next;
    drawTags();
    status.textContent = t("saving");
    try {
      const saved = await onChange(names);
      if (Array.isArray(saved)) { names = saved; drawTags(); }   // the server's cleaned list
      status.textContent = t("saved");
      loadKnownWorkers();
    } catch (e) {
      status.textContent = `${t("notSaved")}: ${e.message}`;
    }
  }
  const addTyped = () => {
    const typed = input.value.split(",").map((x) => x.trim()).filter(Boolean);
    input.value = "";
    const fresh = typed.filter((n) => !names.some((x) => x.toLowerCase() === n.toLowerCase()));
    if (fresh.length) set([...names, ...fresh]);
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") { e.preventDefault(); addTyped(); }
    else if (e.key === "Backspace" && !input.value && names.length) set(names.slice(0, -1));
  });
  input.addEventListener("change", addTyped);    // a picked suggestion, or leaving the box
  drawTags();
  return el("div", { class: "names-editor", onclick: () => input.focus() }, tags, input, status);
}

/** A small window to set the packers of one or several orders. Saves when
 *  the worker presses Save; Cancel (or Esc) changes nothing. */
function assignDialog(orderIds, current, title, onDone) {
  let names = [...current];
  const dialog = el("dialog", { class: "assign-dialog" });
  const editor = namesEditor(names, (n) => { names = n; }, { label: t("packedBy"), id: "assign-names" });
  editor.querySelector(".names-status").remove();          // nothing is saved until Save
  const msg = el("p", { class: "names-status muted small" });
  const save = el("button", { type: "button", class: "btn-accent" }, t("save"));
  save.addEventListener("click", async () => {
    const typed = dialog.querySelector(".names-input");
    if (typed.value.trim()) typed.dispatchEvent(new Event("change"));   // a name typed but not entered
    save.disabled = true;
    try {
      await api("/orders/assign", { method: "PUT", body: { order_ids: orderIds, workers: names } });
      dialog.close();
      loadKnownWorkers();
      onDone();
    } catch (e) {
      msg.textContent = `${t("notSaved")}: ${e.message}`;
      save.disabled = false;
    }
  });
  dialog.append(
    el("h2", {}, title),
    el("p", { class: "muted small" }, t("namesHint")),
    editor, msg,
    el("div", { class: "dialog-actions" },
      el("button", { type: "button", class: "icon-btn", onclick: () => dialog.close() }, t("cancel")),
      save));
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  dialog.querySelector(".names-input").focus();
}

// Orders ticked in the order tables, for giving several the same packers.
// Kept while the page refreshes; cleared when leaving the screen.
const selectedOrders = new Set();

// ---------------------------------------------------------------------------
// Sortable tables (pick list and the three order tables)
// ---------------------------------------------------------------------------
// Every header is a button with an arrow: ↕ = not sorted by this column,
// ↑ = ascending, ↓ = descending. Clicking the sorted column again reverses it.
// The choice is kept per table while the app is open, so the 30-second
// refresh does not undo it. Sorting happens here, on the rows already loaded.
const sorts = {};

/** Compare text the way people expect: case ignored, and numbers inside
 *  text by value ("item 9" before "item 10", SKU "5025" before "12025"). */
const collator = () => new Intl.Collator(state.lang, { numeric: true, sensitivity: "base" });

function sortRows(rows, col, dir) {
  const cmp = collator();
  const sign = dir === "asc" ? 1 : -1;
  // Array.sort is stable, so rows that tie keep the server's order.
  return [...rows].sort((a, b) => {
    const x = col.value(a), y = col.value(b);
    const xEmpty = x == null || x === "", yEmpty = y == null || y === "";
    // Empty values (no SKU, no category) go last in both directions.
    if (xEmpty || yEmpty) return xEmpty === yEmpty ? 0 : xEmpty ? 1 : -1;
    return sign * (col.num ? x - y : cmp.compare(String(x), String(y)));
  });
}

/** A table whose headers sort it. cols: [{key, label, value(row), num?}];
 *  rowFn(row) -> <tr>; initial = the sort before anyone clicks. */
function sortableTable(id, cols, rows, rowFn, { cls = null, initial, extraHead = [], leadHead = [] }) {
  const table = el("table", { class: cls });
  const draw = () => {
    // The saved sort, unless its column is not shown right now (e.g. the
    // hidden "Orders" column); then the table's default.
    let s = sorts[id];
    if (!s || !cols.some((c) => c.key === s.key)) s = initial;
    const col = cols.find((c) => c.key === s.key);
    // A column with `hidden: true` is not drawn, but can still be the sort:
    // the order tables stay oldest first while "Date ordered" is hidden.
    const head = cols.filter((c) => !c.hidden).map((c) => {
      const active = s.key === c.key;
      return el("th", {
        scope: "col", class: [c.num ? "num" : null, c.cls].filter(Boolean).join(" ") || null,
        "aria-sort": active ? (s.dir === "asc" ? "ascending" : "descending") : "none",
      }, el("button", {
        type: "button", class: `sort-btn${active ? " active" : ""}`,
        onclick: () => {
          sorts[id] = { key: c.key, dir: active && s.dir === "asc" ? "desc" : "asc" };
          draw();
        },
      }, c.label, el("span", { class: "arrow", "aria-hidden": "true" },
        active ? (s.dir === "asc" ? "↑" : "↓") : "↕")));
    });
    table.replaceChildren(
      el("thead", {}, el("tr", {}, leadHead, head, extraHead)),
      el("tbody", {}, (col ? sortRows(rows, col, s.dir) : rows).map(rowFn)));
  };
  draw();
  return table;
}

// ---------------------------------------------------------------------------
// Screens
// ---------------------------------------------------------------------------
async function startView() {
  frame({ title: t("startHeading"), dated: false });
  return el("div", { class: "choice-grid" },
    // Trendyol has no data source yet: it opens a page that says so.
    el("a", { class: "choice", href: "#/trendyol" },
      el("span", { class: "choice-badge trendyol" }, icon("bag")),
      el("span", { class: "choice-label" }, t("trendyol")),
      el("span", { class: "tag" }, t("comingSoon"))),
    el("a", { class: "choice", href: "#/website" },
      el("span", { class: "choice-badge website" }, icon("box")),
      el("span", { class: "choice-label" }, t("website"))));
}

async function hubView() {
  const s = await api(`/summary${datesQuery()}`);
  frame({ title: t("websiteHeading"), sub: t("websiteSub"), back: "#/", range: s.range });
  return el("div", { class: "hub" },
    el("div", { class: "choice-grid" },
      el("a", { class: "choice big", href: withDates("#/website/items") },
        el("span", { class: "choice-badge items" }, icon("list")),
        el("span", { class: "choice-label" }, t("items")),
        el("span", { class: "choice-number" }, fmtNum(s.pieces))),
      el("a", { class: "choice big", href: withDates("#/website/orders") },
        el("span", { class: "choice-badge orders" }, icon("box")),
        el("span", { class: "choice-label" }, t("orders")),
        el("span", { class: "choice-number" }, fmtNum(s.orders)))),
    el("a", { class: "packed-link card", href: withDates("#/website/packed") },
      icon("box"), el("span", {}, t("packedList")),
      el("span", { class: "muted" }, t("packedToday", fmtNum(s.packed_today)))),
    el("a", { class: "packed-link card", href: withDates("#/website/shipping") },
      icon("truck"), el("span", {}, t("shippingHeading"))));
}

/** The pick list. On screen a table; on paper the same table with an empty
 *  tick column, and a header saying when it was printed (styles in
 *  storage.css, @media print). */
// "Number of orders" in the pick list: hidden unless ticked (2026-09-28:
// not needed for now, maybe later). Remembered per browser; a blocked
// storage just means it starts hidden each time.
function showOrdersCol() {
  try { return localStorage.getItem("storage.showOrders") === "1"; } catch { return false; }
}
function setShowOrdersCol(on) {
  try { localStorage.setItem("storage.showOrders", on ? "1" : "0"); } catch { /* blocked */ }
}

async function itemsView() {
  const d = await api(`/items${datesQuery()}`);
  const withOrders = showOrdersCol();
  const toggle = el("input", { type: "checkbox", id: "show-orders", checked: withOrders });
  toggle.addEventListener("change", () => { setShowOrdersCol(toggle.checked); render({ quiet: true }); });
  frame({
    title: t("itemsHeading"), sub: t("itemsSub"), back: withDates("#/website"), range: d.range,
    actions: [
      el("label", { class: "check-toggle", for: "show-orders" }, toggle, t("showOrders")),
      el("button", { type: "button", class: "btn-accent btn-icon", onclick: () => window.print() },
        icon("print"), t("print"))],
  });
  // Every column sorts; click the same header again to reverse. Opens sorted
  // by category (the server's order: category, then name), which roughly
  // follows the shelves. Printing prints the list in the order on screen.
  const cols = [
    // Column order (2026-09-28): item no., item name, category, quantity.
    { key: "sku", label: t("colItemNo"), value: (x) => x.sku },
    { key: "item", label: t("colItemNames"), value: (x) => x.name },
    // Which colour and size to take off the shelf (2026-09-28). Empty for
    // products that don't come in variations.
    { key: "color", label: t("colColor"), value: (x) => optionValues(x.options, "color") },
    { key: "size", label: t("colSize"), value: (x) => optionValues(x.options, "size") },
    { key: "category", label: t("colCategory"), value: (x) => x.category },
    { key: "qty", label: t("colQty"), value: (x) => x.quantity, num: true },
    withOrders && { key: "orders", label: t("colOrders"), value: (x) => x.orders, num: true },
  ].filter(Boolean);
  const row = (x) => el("tr", {},
    el("td", { class: "sku" }, x.sku || el("span", { class: "muted" }, t("noSku"))),
    el("td", { class: "item-name" }, x.name || `#${x.product_id}`,
      // Any other option (not colour or size) goes under the name.
      optionTags((x.options || []).filter((o) => o.kind === "other"))),
    el("td", { class: "opt-cell" }, optionValues(x.options, "color") || el("span", { class: "muted" }, "—")),
    el("td", { class: "opt-cell" }, optionValues(x.options, "size") || el("span", { class: "muted" }, "—")),
    // One short product-type category (picked on the server); every
    // category the store gives the product shows on hover.
    el("td", { title: (x.all_categories || []).join("\n") }, x.category || t("uncategorised")),
    el("td", { class: "num qty" }, fmtNum(x.quantity)),
    withOrders && el("td", { class: "num" }, fmtNum(x.orders)),
    el("td", { class: "tick print-only" }));
  // Who collects this list (2026-09-28): typed in before printing and
  // printed at the top of the sheet. Saved for these dates as it changes.
  const pickedByLine = (names) => `${t("pickedBy")}: ${names.length ? names.join(state.lang === "ar" ? "، " : ", ") : "____________________"}`;
  const printedPickers = el("span", { class: "picked-by" }, pickedByLine(d.pickers));
  const pickers = namesEditor(d.pickers, async (names) => {
    const saved = (await api(`/pickers?from=${d.range.from}&to=${d.range.to}`,
                             { method: "PUT", body: { workers: names } })).workers;
    printedPickers.textContent = pickedByLine(saved);
    return saved;
  }, { label: t("pickedBy"), id: "pickers" });
  return el("div", { class: "stack" },
    el("section", { class: "card assign-row screen-only" },
      el("label", { for: "pickers" }, t("pickedBy")), pickers),
    printHead(t("itemsHeading"), d.range,
      statLine(t("statPieces"), fmtNum(d.pieces)),
      statLine(t("statOrders"), fmtNum(d.summary.orders)),
      printedPickers),
    el("section", { class: "card" },
      d.items.length ? el("div", { class: "table-wrap" }, sortableTable("items", cols, d.items, row, {
        cls: "pick", initial: { key: "category", dir: "asc" },
        extraHead: [el("th", { scope: "col", class: "print-only" }, t("colDone"))],
      }))
      : el("p", { class: "empty" }, t("nothingWaiting")),
      d.items.length > 0 && el("p", { class: "note screen-only" },
        t("itemsTotal", fmtNum(d.pieces), fmtNum(d.summary.orders)))));
}

const SIZES = ["one", "two", "three_plus"];

// "Date ordered" in the order tables (2026-09-29): hidden unless ticked,
// and then the LAST column. Remembered per browser, like "Show number of
// orders" on the pick list.
function showDateCol() {
  try { return localStorage.getItem("storage.showDate") === "1"; } catch { return false; }
}
function setShowDateCol(on) {
  try { localStorage.setItem("storage.showDate", on ? "1" : "0"); } catch { /* blocked */ }
}
// "Show packed by" (2026-10-01): the Packed by column can be hidden the
// same way. Shown unless someone unticks it; remembered per browser.
function showWorkersCol() {
  try { return localStorage.getItem("storage.showPackedBy") !== "0"; } catch { return true; }
}
function setShowWorkersCol(on) {
  try { localStorage.setItem("storage.showPackedBy", on ? "1" : "0"); } catch { /* blocked */ }
}

/** Columns of the three order tables (2026-09-29): Order no., BOL no.,
 *  Item name, Item no., Total items, Packed by, then Date ordered when
 *  shown. Default sort: date ordered, oldest first, even while hidden. */
const ORDER_COLS = () => [
  { key: "order", label: t("colOrder"), value: (o) => o.number },
  // From the OTO sync; orders without one yet sort last.
  { key: "bol", label: t("colBol"), value: (o) => o.bol_no || "" },
  { key: "names", label: t("colItemNames"), value: (o) => o.items.map((x) => x.name || "").join(" ") },
  // On paper the item number is printed at the start of each item line
  // instead (see orderRow), so this column is screen only.
  { key: "skus", label: t("colItemNos"), value: (o) => o.items.map((x) => x.sku || "").join(" "),
    cls: "screen-only" },
  { key: "pieces", label: t("colTotalItems"), value: (o) => o.pieces, num: true },
  // Who packs it (2026-09-28). Orders nobody is assigned to sort last.
  { key: "workers", label: t("packedBy"), value: (o) => o.workers.join(", "),
    hidden: !showWorkersCol() },
  // Riyadh wall-clock "2026-09-20T08:00:00" sorts correctly as text; the id
  // breaks ties between orders placed in the same second.
  { key: "date", label: t("colOrdered"), value: (o) => `${o.created_local} ${o.id}`,
    hidden: !showDateCol() },
];

/** The BOL number, as a link to the carrier's tracking page when OTO gave
 *  one (opens in a new tab; clicking it does not open the order). "—" when
 *  there is no shipment yet. */
function bolLink(o) {
  if (!o.bol_no) return el("span", { class: "muted" }, "—");
  if (!o.tracking_url) return o.bol_no;
  return el("a", { href: o.tracking_url, target: "_blank", rel: "noopener noreferrer",
                   class: "bol-link", onclick: (e) => e.stopPropagation() }, o.bol_no);
}

/** One order row. It does NOT open the order (2026-10-01): an order is
 *  opened only by typing or scanning its order no. or BOL no. into the
 *  search bar (Enter), or with "Scan BOL no.", so a worker packs the parcel
 *  actually in their hands. Order no. and BOL no. are plain text. The tick
 *  box selects orders for giving several the same packers.
 *
 *  Item names and item numbers are listed one product line per row inside
 *  their cells, so each name sits level with its number. Names stay on one
 *  line (cut with "…", full name on hover) to keep them level. Every line
 *  shows its quantity, "× 1" too, and every line of the order is listed
 *  (2026-09-29: no more "+N"). */
function orderRow(o, { onSelect, onAssigned }) {
  const lines = (render) => o.items.map((x) => el("div", { class: "line" }, render(x)));
  const tick = el("input", { type: "checkbox", checked: selectedOrders.has(o.id),
                             "aria-label": t("selectOrder", o.number) });
  tick.addEventListener("click", (e) => e.stopPropagation());
  tick.addEventListener("change", () => {
    tick.checked ? selectedOrders.add(o.id) : selectedOrders.delete(o.id);
    onSelect();
  });
  const change = el("button", {
    type: "button", class: "edit-names", title: t("assignPackers"), "aria-label": t("assignPackers"),
    onclick: (e) => {
      e.stopPropagation();
      assignDialog([o.id], o.workers, t("packersFor", o.number), onAssigned);
    },
  }, icon("edit"));
  return el("tr", { class: selectedOrders.has(o.id) ? "selected" : null },
    el("td", { class: "select-cell" }, tick),
    el("td", { class: "order-no" }, `#${o.number}`),
    el("td", { class: "bol", dir: "ltr", title: o.carrier || "" },
      o.bol_no || el("span", { class: "muted" }, "—")),
    el("td", { class: "names", title: o.items.map((x) => x.name).join("\n") },
      // The name may be cut with "…"; colour, size and quantity never are.
      // On paper (2026-09-29) names wrap onto several lines, so a separate
      // Item no. column could no longer stay level with its name: the item
      // number is printed at the start of its own line instead.
      // The number itself is isolated left-to-right (<bdi>); the span around
      // it follows the page direction, so its gap and "·" land between the
      // number and the name in Arabic too.
      lines((x) => [el("span", { class: "line-sku print-only" }, el("bdi", { dir: "ltr" }, x.sku || t("noSku"))),
                    el("span", { class: "nm" }, x.name || `#${x.product_id}`), optionTags(x.options),
                    el("span", { class: "qty-tag" }, `× ${fmtNum(x.quantity)}`)])),
    el("td", { class: "sku screen-only" }, lines((x) => x.sku || el("span", { class: "muted" }, t("noSku")))),
    el("td", { class: "num" }, fmtNum(o.pieces)),
    showWorkersCol() && el("td", { class: "workers" }, nameTags(o.workers), change),
    showDateCol() && el("td", { class: "when" }, fmtWhen(o.created_local)));
}

async function ordersView() {
  // The summary only for "N packed today" on the Packed orders button.
  const [d, s] = await Promise.all([api(`/orders${datesQuery()}`), api(`/summary${datesQuery()}`)]);
  frame({ title: t("ordersHeading"), sub: t("ordersSub"), back: withDates("#/website"), range: d.range,
          actions: [packedButton(s.packed_today, "orders"), printButton()] });
  const stats = el("span", { class: "print-stats" });   // filled by orderTables
  return el("div", { class: "stack" },
    printHead(t("ordersHeading"), d.range, stats), orderTables(d, "orders", stats));
}

/** Orders placed BEFORE From that are still waiting (the banner's link). */
async function olderView() {
  const [d, s] = await Promise.all([api(`/orders${datesQuery({ older: "true" })}`),
                                    api(`/summary${datesQuery()}`)]);
  frame({ title: t("olderHeading"), sub: t("olderSub", fmtWhen(d.range.start_local)),
          back: withDates("#/website"), range: d.range,
          actions: [packedButton(s.packed_today, "older"), printButton()] });
  const stats = el("span", { class: "print-stats" });
  return el("div", { class: "stack" }, printHead(t("olderHeading"), d.range, stats), orderTables(d, "older", stats));
}

/** Straight to the packed list from the orders screens, and back (2026-09-28: faster than going back to the Website page each time).
 *  Keeps the chosen dates, so Back from there returns to the same days. */
// `via` (2026-09-28): Back on the packed list returns to this screen,
// on the same tab, instead of the Website page.
// The address is read on click, not when the button is drawn: switching
// tabs changes it afterwards (replaceState), and the tab must come along.
const packedButton = (packedToday, via) => el("a", {
  class: "icon-btn btn-icon btn-blue", href: withQuery("#/website/packed", { set: { via } }),
  onclick: (e) => { e.preventDefault(); location.hash = withQuery("#/website/packed", { set: { via } }); },
}, icon("box"), t("packedList"),     // a box: packed, not yet on the truck (2026-10-01)
  el("span", { class: "tab-count" }, t("packedToday", fmtNum(packedToday))));
const toPackButton = () => el("a", { class: "icon-btn btn-icon",
                                     href: withQuery("#/website/orders", { drop: ["via", "pfrom", "pto"] }) },
  icon("box"), t("ordersHeading"));

const printButton = () => el("button", { type: "button", class: "btn-accent btn-icon",
                                         onclick: () => window.print() }, icon("print"), t("print"));

/** One "Label: value" line in a printed header (2026-09-30). */
const statLine = (label, value) => el("span", { class: "print-stat" }, `${label}: ${value}`);

/** The top of a printed page: what it is, the dates, when printed.
 *  Each fact sits on its own line (2026-09-30): the window, "Printed
 *  on …", then the counts ("Orders: 3", …) passed in `more`.
 *  `b` (the From/To range) may be null for lists that are not picked by
 *  date, like the packed list; then only the title and print time show. */
const printHead = (title, b, ...more) => el("div", { class: "print-only print-head" },
  el("strong", {}, b ? `${t("title")} · ${title} · ${rangeTitle(b)}` : `${t("title")} · ${title}`),
  // b.windowKey: other wording for the window, e.g. "Packed from … to …".
  b && el("span", {}, b.windowKey ? t(b.windowKey, fmtWhen(b.start_local), fmtWhen(b.last_local))
                                  : rangeWindowText(b)),
  el("span", {}, t("printedAt", nowInRiyadh())), ...more);

/** Which group tab is chosen (2026-09-28): `size=one|two|three_plus` in
 *  the address, so a refresh or a shared link keeps it. null = not chosen. */
function chosenSize() {
  const v = new URLSearchParams(location.hash.split("?")[1] || "").get("size");
  return SIZES.includes(v) ? v : null;
}
/** Put the chosen tab in the address WITHOUT a hashchange event (that would
 *  reload the data and clear the ticks); replaceState, so Back still goes
 *  to the previous screen rather than through every tab clicked. */
function setChosenSize(size) {
  const [path, qs] = location.hash.split("?");
  const q = new URLSearchParams(qs || "");
  q.set("size", size);
  history.replaceState(null, "", `${path}?${q}`);
}

// ---------------------------------------------------------------------------
// Finding one order: a search bar and "Scan BOL no." (2026-09-30)
// ---------------------------------------------------------------------------
// The search filters all three groups at once. Typed text matches the order
// number, BOL no., item names and item numbers. A BOL read by the camera
// must match a BOL EXACTLY, so the scan shows just that one order. A USB
// scanner simply types into the search box. Kept while the list refreshes;
// cleared when the screen or the dates change (see the hashchange handler).
let orderFilter = { q: "", raw: "", exact: false };

function orderMatches(o, f) {
  if (!f.q) return true;
  if (f.exact) return normalise(o.bol_no) === f.q || String(o.number) === f.q.replace(/^#/, "");
  return [o.number, `#${o.number}`, o.bol_no, ...o.items.flatMap((x) => [x.name, x.sku])]
    .some((v) => v && normalise(String(v)).includes(f.q));
}

/** Load a script once (the ZXing fallback, only when needed). */
const scriptsLoaded = {};
function loadScript(src) {
  scriptsLoaded[src] ||= new Promise((resolve, reject) => {
    const tag = el("script", { src });
    tag.addEventListener("load", resolve);
    tag.addEventListener("error", () => reject(new Error(`could not load ${src}`)));
    document.head.append(tag);
  });
  return scriptsLoaded[src];
}

// Label barcodes a BOL may use: carriers print Code 128 mostly; the others
// cost nothing to accept.
const BOL_FORMATS = ["code_128", "code_39", "code_93", "codabar", "itf", "ean_13", "qr_code", "data_matrix"];

/** A camera window that reads one barcode and hands its text to onCode.
 *  Uses the browser's own BarcodeDetector where there is one (Chrome and
 *  Edge on Android and Mac); elsewhere (Windows Chrome, iPhone, Firefox)
 *  the bundled ZXing library (web/vendor, Apache-2.0), loaded on first use.
 *  The camera only works on a secure page: https://, or this computer
 *  itself (127.0.0.1). The camera is always released on close. */
function openBolScanner(onCode) {
  const video = el("video", { class: "scan-video", autoplay: true, playsinline: true });
  video.muted = true;
  const status = el("p", { class: "muted small scan-status", role: "status" }, t("scanBolAim"));
  let stream = null, timer = null, reader = null, done = false;
  const stop = () => {
    if (done) return;
    done = true;
    clearTimeout(timer);
    try { reader?.reset(); } catch { /* already stopped */ }
    stream?.getTracks().forEach((track) => track.stop());
    dialog.close();
    dialog.remove();
  };
  const dialog = el("dialog", { class: "assign-dialog scan-dialog" },
    el("h2", {}, t("scanBol")),
    el("div", { class: "scan-video-wrap" }, video),
    status,
    el("div", { class: "dialog-actions" },
      el("button", { type: "button", class: "icon-btn", onclick: stop }, t("cancel"))));
  dialog.addEventListener("cancel", (e) => { e.preventDefault(); stop(); });   // Esc key
  document.body.append(dialog);
  dialog.showModal();

  const found = (text) => {
    const code = (text || "").trim();
    if (!code || done) return;
    beep(true);
    stop();
    onCode(code);
  };
  const fail = (e) => {
    if (done) return;
    status.textContent = t("cameraFailed", e?.name || e?.message || "?");
    status.classList.add("error");
  };

  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    status.textContent = t("cameraUnavailable");
    status.classList.add("error");
    return;
  }
  (async () => {
    try {
      if ("BarcodeDetector" in window) {
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
        if (done) { stream.getTracks().forEach((track) => track.stop()); return; }
        video.srcObject = stream;
        await video.play();
        const supported = await BarcodeDetector.getSupportedFormats();
        const formats = BOL_FORMATS.filter((f) => supported.includes(f));
        const detector = new BarcodeDetector(formats.length ? { formats } : undefined);
        const tick = async () => {           // look ~5 times a second
          if (done) return;
          try {
            const hit = (await detector.detect(video)).find((b) => b.rawValue?.trim());
            if (hit) return found(hit.rawValue);
          } catch { /* a frame that could not be read */ }
          timer = setTimeout(tick, 200);
        };
        tick();
      } else {
        await loadScript("/static/vendor/zxing-library-0.23.0.min.js");
        if (done) return;
        reader = new window.ZXing.BrowserMultiFormatReader();
        await reader.decodeFromConstraints({ video: { facingMode: "environment" }, audio: false }, video,
          (result) => { if (result) found(result.getText()); });
      }
    } catch (e) { fail(e); }
  })();
}

/** The three order groups (1 / 2 / 3+ pieces) as TABS (2026-09-28): a
 *  pill bar with every group's count, and one group shown at a time, full
 *  width. Before, the three tables sat side by side and a long first table
 *  pushed the third far down the page.
 *
 *  Printing prints the group on screen (the owner's choice); its heading and count
 *  are on the sheet. Ticked orders (for giving several the same packers)
 *  are cleared when switching tabs, so nothing ticked is ever out of sight.
 *  `key` keeps the sort choice separate for the main and the older screens. */
function orderTables(d, key, stats = null) {
  const all = SIZES.flatMap((size) => d.tables[size]);
  // Forget ticks on orders no longer listed (packed meanwhile, other day).
  for (const id of [...selectedOrders]) if (!all.some((o) => o.id === id)) selectedOrders.delete(id);

  // The chosen tab; with none chosen, the first group that has orders.
  let current = chosenSize() || SIZES.find((size) => d.tables[size].length) || SIZES[0];

  const bar = el("div", { class: "select-bar", role: "region", "aria-label": t("assignPackers") });
  const drawBar = () => {
    bar.hidden = selectedOrders.size === 0;
    bar.replaceChildren(
      el("strong", {}, t("selectedCount", selectedOrders.size)),
      el("button", { type: "button", class: "btn-accent btn-icon", onclick: () => {
        const ids = [...selectedOrders];
        const first = all.find((o) => o.id === ids[0]);
        // Start from the first ticked order's names when they all share them.
        const same = ids.every((id) => all.find((o) => o.id === id)?.workers.join() === first?.workers.join());
        assignDialog(ids, same && first ? first.workers : [], t("packersForMany", ids.length), () => {
          selectedOrders.clear(); render({ quiet: true });
        });
      } }, icon("edit"), t("assignPackers")),
      el("button", { type: "button", class: "icon-btn", onclick: () => {
        selectedOrders.clear(); render({ quiet: true });
      } }, t("clearSelection")));
  };
  drawBar();
  const rowOpts = { onSelect: drawBar, onAssigned: () => render({ quiet: true }) };

  // The tab bar: one pill per group, with its count. Screen only.
  const tabs = el("div", { class: "segmented size-tabs", role: "tablist",
                           "aria-label": t("sizeTabs") });

  // OTO sync (2026-09-29/30): fetch the BOL number of every order on this
  // screen (all three groups) from OTO, the shipping platform, then redraw.
  // TEST mode until OTO is connected.
  const syncBtn = el("button", { type: "button", class: "icon-btn btn-icon btn-oto", onclick: async () => {
    syncBtn.disabled = true;
    syncBtn.lastChild.textContent = t("otoSyncing");
    try {
      const r = await api(`/oto/sync${datesQuery(key === "older" ? { older: "true" } : {})}`,
                          { method: "POST" });
      banner(t(r.mode === "test" ? "otoSyncedTest" : "otoSynced", r.updated, r.with_bol, r.orders)
             + (r.failed ? ` ${t("otoSomeFailed", r.failed)}` : ""), r.failed ? "warn" : "info");
      render({ quiet: true });
    } catch (e) {
      banner(`${t("otoFailed")}: ${e.message}`);
      syncBtn.disabled = false;
      syncBtn.lastChild.textContent = t("otoSync");
    }
  } }, icon("sync"), el("span", {}, t("otoSync")));

  const dateTick = el("input", { type: "checkbox", id: `show-date-${key}`, checked: showDateCol() });
  dateTick.addEventListener("change", () => { setShowDateCol(dateTick.checked); render({ quiet: true }); });
  const workersTick = el("input", { type: "checkbox", id: `show-workers-${key}`, checked: showWorkersCol() });
  workersTick.addEventListener("change", () => { setShowWorkersCol(workersTick.checked); render({ quiet: true }); });

  // Search bar and "Scan BOL no." (2026-09-30); see orderFilter above.
  const search = el("input", { type: "search", class: "order-search", value: orderFilter.raw,
                               placeholder: t("searchOrders"), "aria-label": t("searchOrders"),
                               autocomplete: "off", spellcheck: "false" });
  const clearBtn = el("button", { type: "button", class: "search-clear", title: t("clearSearch"),
                                  "aria-label": t("clearSearch"), onclick: () => setFilter("", false) }, "✕");
  search.addEventListener("input", () => setFilter(search.value, false));
  search.addEventListener("keydown", (e) => {
    if (e.key === "Escape") setFilter("", false);
    // Enter (typed, or the end of a USB scanner's code) opens the order
    // whose order no. or BOL no. is exactly what was entered.
    if (e.key === "Enter") { e.preventDefault(); openExact(search.value, { byNumber: true }); }
  });
  const scanBtn = el("button", { type: "button", class: "icon-btn btn-icon",
                                 onclick: () => openBolScanner((code) => openExact(code, { byNumber: false })) },
    icon("scan"), t("scanBol"));

  /** Open the one order on this screen (any group) whose BOL no. — or, when
   *  typed, order no. ("10044" or "#10044") — is exactly `text`. With
   *  none, the list is filtered and says so; with several (a shared BOL),
   *  they are listed to choose from by scanning again. */
  function openExact(text, { byNumber }) {
    const raw = (text || "").trim();
    if (!raw) return;
    const q = normalise(raw), num = q.replace(/^#/, "");
    const all = SIZES.flatMap((size) => d.tables[size]);
    const hits = all.filter((o) => normalise(o.bol_no) === q || (byNumber && String(o.number) === num));
    if (hits.length === 1) {
      location.hash = withQuery(`#/website/order/${hits[0].id}`, { drop: ["via"] });
      return;
    }
    search.value = raw;
    setFilter(raw, true);         // none: the panel says so; several: listed
    if (!hits.length) beep(false);
  }

  // Row 1: what workers use all the time. Row 2 (2026-10-01): the column
  // tickboxes, view settings that change rarely, on a quieter line of their
  // own under the tabs.
  const columnToggles = el("div", { class: "column-toggles" },
    el("label", { class: "check-toggle", for: `show-date-${key}` }, dateTick, t("showDate")),
    el("label", { class: "check-toggle", for: `show-workers-${key}` }, workersTick, t("showPackedBy")));
  const toolbar = el("div", { class: "orders-toolbar" }, tabs,
    el("div", { class: "order-search-wrap" }, search, clearBtn),
    scanBtn,
    syncBtn);                                  // pushed to the far right (CSS)

  /** Set the search; show the first group with matches when the one on
   *  screen has none. */
  function setFilter(text, exact) {
    const raw = (text || "").trim();
    orderFilter = { q: normalise(raw), raw, exact: exact && !!raw };
    if (search.value !== raw && (exact || !raw)) search.value = raw;
    const count = (size) => d.tables[size].filter((o) => orderMatches(o, orderFilter)).length;
    if (orderFilter.q && !count(current)) {
      const other = SIZES.find((size) => count(size));
      if (other) { current = other; setChosenSize(other); }
    }
    selectedOrders.clear(); drawBar();
    draw();
  }
  const panel = el("div", { role: "tabpanel" });
  const draw = () => {
    tabs.replaceChildren(...SIZES.map((size) => el("button", {
      type: "button", role: "tab", "aria-selected": String(size === current),
      "aria-pressed": String(size === current),   // the shared .segmented style
      onclick: () => {
        if (size === current) return;
        current = size;
        setChosenSize(size);
        selectedOrders.clear(); drawBar();
        draw();
      },
    }, t(size), el("span", { class: "tab-count" },
         fmtNum(d.tables[size].filter((o) => orderMatches(o, orderFilter)).length)))));

    clearBtn.hidden = !orderFilter.q;
    const list = d.tables[current].filter((o) => orderMatches(o, orderFilter));
    // The printed header counts what is printed: this group, as searched.
    stats?.replaceChildren(statLine(t("statOrders"), fmtNum(list.length)),
      statLine(t("statPieces"), fmtNum(list.reduce((n, o) => n + o.pieces, 0))));
    panel.replaceChildren(el("section", { class: "card" },
      el("div", { class: "card-head" }, el("h2", {}, t(current)),
        el("span", { class: "count-pill" }, fmtNum(list.length))),
      list.length ? el("div", { class: "table-wrap" },
        sortableTable(`${key}-${current}`, ORDER_COLS(), list, (o) => orderRow(o, rowOpts), {
          cls: "order-table", initial: { key: "date", dir: "asc" },
          leadHead: [el("th", { scope: "col", class: "select-cell" },
                        el("span", { class: "sr-only" }, t("select")))] }))
      : el("p", { class: "empty" }, !orderFilter.q ? t("nothingWaiting")
          : t(orderFilter.exact ? "exactNotFound" : "noMatch", orderFilter.raw))));
  };
  draw();
  if (focusSearchNext) {
    focusSearchNext = false;
    setTimeout(() => search.focus({ preventScroll: true }), 0);
  }
  return el("div", { class: "stack" },
    el("div", { class: "orders-controls screen-only" }, toolbar, columnToggles), bar, panel);
}

// The packed list's own dates (2026-09-30): the days orders were PACKED,
// kept as pfrom/pto in the address so they don't disturb the order screens'
// from/to (which Back returns to). No dates: this month so far.
function packedDates() {
  const q = new URLSearchParams(location.hash.split("?")[1] || "");
  const from = q.get("pfrom"), to = q.get("pto");
  return DATE_RE.test(from || "") && DATE_RE.test(to || "") ? { from, to } : null;
}
function goToPackedDates(from, to) {
  location.hash = from && to ? withQuery("#/website/packed", { set: { pfrom: from, pto: to } })
                             : withQuery("#/website/packed", { drop: ["pfrom", "pto"] });
}

async function packedView() {
  const pd = packedDates();
  const d = await api(`/packed${pd ? `?from=${pd.from}&to=${pd.to}` : ""}`);
  // Print (2026-09-28), same as the pick list: only when there is
  // something to print.
  // Opened from Orders to pack or the older orders: Back returns there (same
  // dates, same tab). Opened from the Website page: Back goes there. The
  // "Orders to pack" button is left out when Back already does the same.
  const via = new URLSearchParams(location.hash.split("?")[1] || "").get("via");
  const cameFrom = ["orders", "older"].includes(via) ? via : null;
  frame({ title: t("packedHeading"),
          back: cameFrom ? withQuery(`#/website/${cameFrom}`, { drop: ["via", "pfrom", "pto"] })
                         : withDates("#/website"),
          actions: [cameFrom !== "orders" && toPackButton(),
                    // Loading onto the truck (2026-09-30)
                    el("a", { class: "icon-btn btn-icon btn-oto", href: withQuery("#/website/loading") },
                      icon("truck"), t("loadTruck")),
                    el("a", { class: "icon-btn btn-icon", href: withQuery("#/website/shipping", { drop: ["pfrom", "pto"] }) },
                      t("shippingHeading")),
                    d.packed.length && printButton()].filter(Boolean),
          range: d.range,
          rangeOpts: { go: goToPackedDates, windowKey: "packedWindow", resetLabel: t("backToThisMonth") } });
  if (!d.packed.length) return el("section", { class: "card" }, el("p", { class: "empty" }, t("nothingPacked")));
  // Totals for the printed sheet: handy when handing the boxes over.
  const sum = (k) => d.packed.reduce((n, p) => n + (p[k] || 0), 0);
  const totals = t("packedTotal", fmtNum(d.packed.length), fmtNum(sum("boxes")), fmtNum(sum("pieces")));
  return el("div", { class: "stack" },
    printHead(t("packedHeading"), { ...d.range, windowKey: "packedWindow" },
      statLine(t("statOrders"), fmtNum(d.packed.length)),
      statLine(t("statBoxes"), fmtNum(sum("boxes"))),
      statLine(t("statPieces"), fmtNum(sum("pieces")))),
    d.truncated && el("p", { class: "banner warn" }, t("packedTruncated", fmtNum(d.packed.length))),
    el("section", { class: "card" }, el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {},
        el("th", { scope: "col" }, t("colOrder")),
        el("th", { scope: "col" }, t("colBol")),
        el("th", { scope: "col", class: "num" }, t("colBoxes")),
        el("th", { scope: "col", class: "num" }, t("colLoaded")),
        el("th", { scope: "col", class: "num" }, t("colPieces")),
        el("th", { scope: "col" }, t("colPackedAt")),
        el("th", { scope: "col" }, t("packedBy")),
        // Empty box on paper, ticked by pen (e.g. when the courier collects).
        el("th", { scope: "col", class: "print-only" }, t("colDone")))),
      el("tbody", {}, d.packed.map((p) => el("tr", {},
        el("td", {}, el("a", { href: withQuery(`#/website/order/${p.id}`) }, `#${p.number}`)),
        el("td", {}, p.bol_no || el("span", { class: "muted" }, "—")),
        el("td", { class: "num" }, fmtNum(p.boxes)),
        // Boxes already scanned onto the truck: "1 / 2" is a box still missing.
        el("td", { class: `num${p.loaded ? " partly-loaded" : ""}` },
          p.loaded ? el("span", { dir: "ltr" }, `${fmtNum(p.loaded)} / ${fmtNum(p.boxes)}`)
                   : el("span", { class: "muted" }, "—")),
        el("td", { class: "num" }, fmtNum(p.pieces)),
        el("td", {}, fmtWhen(p.packed_local)),
        el("td", {}, nameTags(p.packed_by)),
        el("td", { class: "tick print-only" })))))),
      el("p", { class: "note screen-only" }, totals)));
}

// ---------------------------------------------------------------------------
// Loading onto the truck, and Orders being shipped (2026-09-30)
// ---------------------------------------------------------------------------
// Packed is not shipped: at handover every BOX is scanned (its BOL label)
// on this screen. An order with 2 boxes needs 2 scans; after the last one it
// leaves Packed orders and shows under Orders being shipped. The list below
// the scan box is everything still to load, OLDEST first, so a box that was
// forgotten last time is at the top.
let loadMsg = null;            // {kind, text} of the last scan; cleared when the
                               // next scan starts or the screen is opened again
let loadBusy = false;          // a scan is being sent; don't redraw meanwhile
let loadQueue = Promise.resolve();

function loadScan(raw) {
  const code = (raw || "").trim();
  if (!code) return;
  loadQueue = loadQueue.then(async () => {
    loadBusy = true;
    try {
      const r = await api("/load", { method: "POST", body: { code } });
      loadMsg = { kind: "ok", text: r.shipped ? t("loadDone", r.number, r.boxes)
                                              : t("loadOk", r.number, r.loaded, r.boxes) };
      beep(true);
    } catch (e) {
      const d = e.detail || {};
      loadMsg = { kind: "bad", text:
        d.reason === "not_packed" ? t("loadNotPacked", d.number)
        : d.reason === "all_loaded" ? t("loadAllLoaded", d.number, d.boxes)
        : d.reason === "unknown" ? t("loadUnknown", d.code || code)
        : `${t("loadError")}: ${e.message}` };
      beep(false);
    } finally {
      loadBusy = false;
    }
    await render({ quiet: true });
  });
}

// Columns of "Still to load". Riyadh wall-clock "2026-09-30T10:09:00" sorts
// correctly as text; the id breaks ties between orders packed the same second.
const LOAD_COLS = () => [
  { key: "order", label: t("colOrder"), value: (o) => o.number },
  { key: "bol", label: t("colBol"), value: (o) => o.bol_no || "" },
  { key: "loaded", label: t("colLoaded"), value: (o) => o.loaded, num: true },
  { key: "packed", label: t("colPackedAt"), value: (o) => `${o.packed_local} ${o.id}` },
  { key: "by", label: t("packedBy"), value: (o) => (o.packed_by || []).join(", ") },
];

async function loadingView() {
  const d = await api("/loading");
  frame({ title: t("loadingHeading"), sub: t("loadingSub"),
          back: withQuery("#/website/packed") });
  const input = el("input", { id: "load-input", class: "scan-input load-input", type: "text",
                              autocomplete: "off", autocapitalize: "off", spellcheck: "false",
                              enterkeyhint: "done", "aria-label": t("loadScanLabel") });
  // The previous order's result disappears as soon as the next code starts
  // arriving (typed, or from the scanner), so it is never mistaken for the
  // new box's result.
  const clearMsg = () => {
    if (!loadMsg) return;
    loadMsg = null;
    const m = document.querySelector(".scan-card .scan-msg");
    if (m) { m.textContent = ""; m.className = "scan-msg"; }
  };
  input.addEventListener("input", clearMsg);
  input.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const v = input.value;
    input.value = "";
    loadScan(v);
  });
  setTimeout(() => $("load-input")?.focus({ preventScroll: true }), 0);
  const waitingBoxes = d.orders.reduce((n, o) => n + (o.boxes - o.loaded), 0);
  return el("div", { class: "stack" },
    el("section", { class: "card scan-card" },
      el("label", { for: "load-input", class: "scan-label" }, icon("truck"), t("loadScanLabel")),
      el("div", { class: "load-row" }, input,
        el("button", { type: "button", class: "icon-btn btn-icon",
                       onclick: () => { clearMsg(); openBolScanner((code) => loadScan(code)); } },
          icon("scan"), t("scanBol"))),
      el("p", { class: `scan-msg ${loadMsg ? loadMsg.kind : ""}`, "aria-live": "assertive" },
        loadMsg ? loadMsg.text : "")),
    el("section", { class: "card" },
      el("div", { class: "card-head" }, el("h2", {}, t("toLoadHeading")),
        el("span", { class: "count-pill" }, t("toLoadCount", fmtNum(d.orders.length), fmtNum(waitingBoxes)))),
      d.orders.length ? el("div", { class: "table-wrap" },
        // Every column sorts (↕ / ↑ / ↓, 2026-10-01); oldest packed first
        // until someone clicks, so a box forgotten last time stays on top.
        sortableTable("to-load", LOAD_COLS(), d.orders, (o) => el("tr", { class: o.loaded ? "partly-loaded-row" : null },
          el("td", {}, el("a", { href: withQuery(`#/website/order/${o.id}`) }, `#${o.number}`)),
          el("td", { class: "bol", dir: "ltr" }, o.bol_no || el("span", { class: "muted" }, t("noBolYet"))),
          el("td", { class: `num${o.loaded ? " partly-loaded" : ""}` },
            el("span", { dir: "ltr" }, `${fmtNum(o.loaded)} / ${fmtNum(o.boxes)}`)),
          el("td", {}, fmtWhen(o.packed_local)),
          el("td", {}, nameTags(o.packed_by))),
          { initial: { key: "packed", dir: "asc" } }))
      : el("p", { class: "empty" }, t("nothingToLoad"))));
}

// Orders being shipped: by LOADING day, this month by default. Its dates
// are sfrom/sto in the address (like the packed list's pfrom/pto).
function shippingDates() {
  const q = new URLSearchParams(location.hash.split("?")[1] || "");
  const from = q.get("sfrom"), to = q.get("sto");
  return DATE_RE.test(from || "") && DATE_RE.test(to || "") ? { from, to } : null;
}
function goToShippingDates(from, to) {
  location.hash = from && to ? withQuery("#/website/shipping", { set: { sfrom: from, sto: to } })
                             : withQuery("#/website/shipping", { drop: ["sfrom", "sto"] });
}

// Shipped orders, one delivery company at a time (2026-10-01): the
// printed sheet is signed by that company's driver to confirm the boxes went
// onto their truck, so a sheet never mixes companies. A tab per company
// (counts included); the chosen one is kept in the address as &carrier=.
// Names are grouped ignoring case and spaces ("Aymakan" = "aymakan ");
// orders OTO gave no company get their own tab.
const carrierKey = (x) => normalise(x.carrier || "");
function chosenCarrier() {
  return new URLSearchParams(location.hash.split("?")[1] || "").get("carrier");
}
function setChosenCarrier(key) {
  const [path, qs] = location.hash.split("?");
  const q = new URLSearchParams(qs || "");
  q.set("carrier", key);
  history.replaceState(null, "", `${path}?${q}`);
}
// "Show packed by" here has its own remembered choice (shown by default).
function showShippedWorkers() {
  try { return localStorage.getItem("storage.shipped.showPackedBy") !== "0"; } catch { return true; }
}
function setShowShippedWorkers(on) {
  try { localStorage.setItem("storage.shipped.showPackedBy", on ? "1" : "0"); } catch { /* blocked */ }
}

/** Columns of the shipped list; every one sorts (2026-10-01). */
const SHIPPED_COLS = () => [
  { key: "order", label: t("colOrder"), value: (x) => x.number },
  { key: "bol", label: t("colBol"), value: (x) => x.bol_no || "" },
  { key: "boxes", label: t("colBoxes"), value: (x) => x.boxes, num: true },
  { key: "pieces", label: t("colPieces"), value: (x) => x.pieces, num: true },
  // Riyadh wall-clock text sorts correctly; the id breaks ties.
  { key: "packed", label: t("colPackedAt"), value: (x) => `${x.packed_local} ${x.id}` },
  { key: "loaded", label: t("colLoadedAt"), value: (x) => `${x.shipped_local} ${x.id}` },
  { key: "by", label: t("packedBy"), value: (x) => (x.packed_by || []).join(", "),
    hidden: !showShippedWorkers() },
];

async function shippingView() {
  const sd = shippingDates();
  const d = await api(`/shipping${sd ? `?from=${sd.from}&to=${sd.to}` : ""}`);
  frame({ title: t("shippingHeading"),
          back: withQuery("#/website/packed", { drop: ["sfrom", "sto", "carrier"] }),
          actions: [d.shipped.length && printButton()].filter(Boolean),
          range: d.range,
          rangeOpts: { go: goToShippingDates, windowKey: "shippingWindow", resetLabel: t("backToThisMonth") } });
  if (!d.shipped.length) return el("section", { class: "card" }, el("p", { class: "empty" }, t("nothingShipped")));

  // One group per company, the busiest first; "no company" last.
  const groups = new Map();
  for (const x of d.shipped) {
    const k = carrierKey(x);
    if (!groups.has(k)) groups.set(k, { key: k, spellings: new Map(), rows: [] });
    const g = groups.get(k);
    g.rows.push(x);
    const sp = (x.carrier || "").trim();
    g.spellings.set(sp, (g.spellings.get(sp) || 0) + 1);
  }
  // The name shown: the most used spelling, a capitalised one on a tie
  // ("Aymakan" over "aymakan").
  for (const g of groups.values()) {
    g.name = [...g.spellings].sort((a, b) => b[1] - a[1]
      || /^\p{Lu}/u.test(b[0]) - /^\p{Lu}/u.test(a[0]))[0][0];
  }
  const list = [...groups.values()].sort((a, b) => (!a.key) - (!b.key)
    || b.rows.length - a.rows.length || a.name.localeCompare(b.name));
  const label = (g) => g.key ? g.name : t("noCarrier");
  let current = list.find((g) => g.key === chosenCarrier()) || list[0];

  const tabs = el("div", { class: "segmented size-tabs carrier-tabs", role: "tablist",
                           "aria-label": t("colCarrier") });
  const workersTick = el("input", { type: "checkbox", id: "show-shipped-workers", checked: showShippedWorkers() });
  workersTick.addEventListener("change", () => { setShowShippedWorkers(workersTick.checked); draw(); });
  const body = el("div", { class: "stack" });

  const draw = () => {
    tabs.replaceChildren(...list.map((g) => el("button", {
      type: "button", role: "tab", "aria-selected": String(g === current), "aria-pressed": String(g === current),
      onclick: () => { if (g !== current) { current = g; setChosenCarrier(g.key); draw(); } },
    }, label(g), el("span", { class: "tab-count" }, fmtNum(g.rows.length)))));
    const rows = current.rows;
    const sum = (k) => rows.reduce((n, x) => n + (x[k] || 0), 0);
    const showBy = showShippedWorkers();
    body.replaceChildren(...[
      printHead(t("shippingHeading"), { ...d.range, windowKey: "shippingWindow" },
        statLine(t("colCarrier"), label(current)),
        statLine(t("statOrders"), fmtNum(rows.length)),
        statLine(t("statBoxes"), fmtNum(sum("boxes"))),
        statLine(t("statPieces"), fmtNum(sum("pieces")))),
      d.truncated && el("p", { class: "banner warn" }, t("packedTruncated", fmtNum(d.shipped.length))),
      el("section", { class: "card" },
        el("div", { class: "card-head screen-only" }, el("h2", {}, label(current)),
          el("span", { class: "count-pill" }, t("toLoadCount", fmtNum(rows.length), fmtNum(sum("boxes"))))),
        el("div", { class: "table-wrap" },
          sortableTable("shipped", SHIPPED_COLS(), rows, (x) => el("tr", {},
            el("td", {}, el("a", { href: withQuery(`#/website/order/${x.id}`) }, `#${x.number}`)),
            el("td", { class: "bol", dir: "ltr" }, bolLink(x)),
            el("td", { class: "num" }, fmtNum(x.boxes)),
            el("td", { class: "num" }, fmtNum(x.pieces)),
            el("td", {}, fmtWhen(x.packed_local)),
            el("td", {}, fmtWhen(x.shipped_local)),
            showBy && el("td", {}, nameTags(x.packed_by))),
            { initial: { key: "loaded", dir: "desc" } }))),
      // Printed only: the company's driver signs for what is on this sheet.
      el("div", { class: "print-only sign-block" },
        el("p", {}, t("signConfirm", label(current), fmtNum(rows.length), fmtNum(sum("boxes")))),
        ...["signDriver", "signSignature", "signDateTime"].map((k) =>
          el("div", { class: "sign-line" }, el("span", {}, t(k)), el("span", { class: "sign-blank" })))),
    ].filter(Boolean));
  };
  draw();
  return el("div", { class: "stack" },
    el("div", { class: "orders-controls screen-only" },
      el("div", { class: "orders-toolbar" }, tabs),
      el("div", { class: "column-toggles" },
        el("label", { class: "check-toggle", for: "show-shipped-workers" }, workersTick, t("showPackedBy")))),
    body);
}

/** Trendyol: no data source yet (it needs Trendyol seller API access). */
async function trendyolView() {
  frame({ title: t("trendyolHeading"), back: "#/" });
  return el("section", { class: "card" }, el("p", { class: "empty" }, t("trendyolNotYet")));
}

async function notFound() {
  frame({ title: t("notFound"), back: "#/", dated: false });
  return el("div");
}

// ---------------------------------------------------------------------------
// One order: scan, boxes, approve
// ---------------------------------------------------------------------------
// The scan session for the order on screen. Kept in memory only: leaving the
// screen (or reloading) starts the order over, which is the safe direction.
let session = null;

function newSession(order) {
  return {
    order,
    credited: new Map(order.items.map((x) => [x.line_item_id, 0])),  // line -> scans
    history: [],          // [{code, line}] in scan order, for "undo last scan"
    manual: new Set(),    // lines without SKU confirmed by hand
    labels: new Set(),    // unique piece labels scanned (normalised codes)
    testLabels: null,     // the Test labels shown, once asked for
    boxes: "",
    message: null,        // {kind: "ok" | "bad", text}
    saving: false,
  };
}

/** Lines still missing something: a scan, or a by-hand confirmation. */
function remaining(s) {
  return s.order.items.reduce((n, x) => n + (x.sku
    ? x.quantity - s.credited.get(x.line_item_id)
    : (s.manual.has(x.line_item_id) ? 0 : x.quantity)), 0);
}

/** One scan. A scan is either an ITEM NUMBER (as before), or a unique PIECE
 *  label (2026-09-29: every piece has its own barcode, so two identical
 *  bedsheets have two different ones). A label is looked up on the server
 *  to learn its item; the same label cannot count twice, and a label
 *  already packed into another order is refused.
 *
 *  Scans are handled one after another, in order: a scanner types fast,
 *  and a label scan waits for the server's answer. */
let scanQueue = Promise.resolve();
function handleScan(raw) {
  scanQueue = scanQueue.then(() => scanOne(raw)).catch((e) => banner(`${t("loadError")}: ${e.message}`));
}

async function scanOne(raw) {
  const s = session;
  const code = normalise(raw);
  if (!s || !code || s.saving) return;
  const shown = raw.trim();
  const bad = (text) => { s.message = { kind: "bad", text }; beep(false); drawOrder(); };

  // 1. An item number in this order: counts as before.
  const byItemNo = s.order.items.filter((x) => x.sku && normalise(x.sku) === code);
  if (byItemNo.length) return credit(s, byItemNo, shown, null);

  // 2. A label: the same one twice in this order?
  if (s.labels.has(code)) return bad(t("labelAgain", shown));

  // 3. Ask the server which item the label is. 404 = not a label either.
  let label = null;
  try {
    label = await api(`/labels/${encodeURIComponent(shown)}`);
  } catch (e) {
    if (e.status !== 404) throw e;
  }
  if (session !== s) return;              // the worker left this order meanwhile
  if (!label) return bad(t("scanUnknown", shown));
  if (label.packed_order_id != null) return bad(t("labelPacked", shown, label.packed_number));
  if (s.labels.has(code)) return bad(t("labelAgain", shown));   // scanned twice while waiting
  const byLabel = s.order.items.filter((x) => x.sku && normalise(x.sku) === normalise(label.sku));
  if (!byLabel.length) return bad(t("labelOtherItem", shown, label.sku));
  return credit(s, byLabel, shown, code);
}

/** Credit one piece to the first of `lines` that still needs one. */
function credit(s, lines, shown, labelCode) {
  const line = lines.find((x) => s.credited.get(x.line_item_id) < x.quantity);
  if (line) {
    s.credited.set(line.line_item_id, s.credited.get(line.line_item_id) + 1);
    s.history.push({ code: shown, line: line.line_item_id, label: labelCode });
    if (labelCode) s.labels.add(labelCode);
    s.message = { kind: "ok", text: t("scanOk", line.name, s.credited.get(line.line_item_id), line.quantity) };
    beep(true);
  } else {
    s.message = { kind: "bad", text: t("scanDone", lines[0].name) };
    beep(false);
  }
  drawOrder();
}

function undoScan() {
  const s = session;
  const last = s.history.pop();
  if (!last) return;
  s.credited.set(last.line, s.credited.get(last.line) - 1);
  if (last.label) s.labels.delete(last.label);
  s.message = null;
  drawOrder();
}

// ---- Test labels (2026-09-29) -------------------------------------------
// Until real piece labels exist (from an ERP or the label printer), the server
// makes test ones: one unique label per piece of the order. Each shows as a
// real Code 128 barcode. Click one to scan it, or print the sheet and use
// the actual scanner on the paper.

async function showTestLabels() {
  const s = session;
  const d = await api(`/orders/${s.order.id}/test-labels`, { method: "POST" });
  if (session !== s) return;
  s.testLabels = d.labels;
  drawOrder();
}

/** The barcode picture: our own SVG markup (bars and numbers only). */
function barcodeNode(code, doc = document) {
  const box = doc.createElement("span");
  box.className = "barcode";
  box.innerHTML = code128Svg(code);
  return box;
}

function testLabelsCard(s) {
  const tiles = s.testLabels.map((l) => {
    const used = s.labels.has(normalise(l.barcode)) || l.packed;
    return el("button", {
      type: "button", class: `label-tile${used ? " used" : ""}`, disabled: used,
      title: t("clickToScan"), "aria-label": t("scanThis", l.barcode, l.name || l.sku),
      onclick: () => handleScan(l.barcode),
    }, barcodeNode(l.barcode),
      el("span", { class: "label-code", dir: "ltr" }, l.barcode),
      el("span", { class: "label-item" }, l.name || l.sku),
      l.options && l.options.length > 0 && el("span", { class: "label-opts" }, optionText(l.options)),
      used && el("span", { class: "label-used" }, icon("check"), t("scanned")));
  });
  return el("section", { class: "card test-labels" },
    el("div", { class: "card-head" }, el("h2", {}, t("testLabels")),
      el("button", { type: "button", class: "icon-btn btn-icon",
                     onclick: () => printLabels(s.order, s.testLabels) }, icon("print"), t("printLabels"))),
    el("p", { class: "muted small" }, t("testLabelsHint")),
    s.testLabels.length ? el("div", { class: "label-grid" }, tiles)
                        : el("p", { class: "empty" }, t("noLabelsNeeded")));
}

/** A sheet of the labels in a new window, to print and scan on paper. Store
 *  text (item names) goes in with textContent, never as markup. */
function printLabels(order, labels) {
  const w = window.open("", "_blank");
  if (!w) return;
  const doc = w.document;
  doc.title = `${t("testLabels")} · #${order.number}`;
  const style = doc.createElement("style");
  style.textContent = `
    body { font: 12px system-ui, sans-serif; margin: 10mm; color: #000; }
    h1 { font-size: 15px; margin: 0 0 6mm; }
    .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6mm; }
    .l { border: 1px dashed #999; padding: 3mm; text-align: center; break-inside: avoid; }
    .barcode svg { width: 100%; height: 16mm; display: block; }
    .code { font: 700 13px ui-monospace, monospace; letter-spacing: .08em; margin-top: 1mm; }
    .name { margin-top: 1mm; }`;
  doc.head.append(style);
  const h1 = doc.createElement("h1");
  h1.textContent = `${t("title")} · ${t("testLabels")} · #${order.number}`;
  const grid = doc.createElement("div");
  grid.className = "grid";
  for (const l of labels) {
    const tile = doc.createElement("div");
    tile.className = "l";
    const code = doc.createElement("div");
    code.className = "code";
    code.textContent = l.barcode;
    const name = doc.createElement("div");
    name.className = "name";
    name.textContent = [l.name || l.sku, l.options?.length ? optionText(l.options) : ""].filter(Boolean).join(" · ");
    tile.append(barcodeNode(l.barcode, doc), code, name);
    grid.append(tile);
  }
  doc.body.append(h1, grid);
  w.focus();
  w.print();
}

async function orderView(id, stillCurrent) {
  const order = await api(`/orders/${id}`);
  if (!stillCurrent()) return null;
  // Keep the scans when the same order is simply redrawn; start fresh otherwise.
  if (!session || session.order.id !== order.id || order.state !== "waiting") {
    session = newSession(order);
  } else {
    session.order = order;
  }
  // render() puts the node on the page; then the scan box gets the focus.
  setTimeout(focusScan, 0);
  return drawOrder(false);
}

/** Draws the scan screen from `session`. With replace=false it returns the
 *  node instead (first render goes through render()). */
function drawOrder(replace = true) {
  const s = session, o = s.order;
  frame({
    title: t("orderHeading", o.number),
    sub: [t("placed", fmtWhen(o.created_local)), t("pieces", o.pieces),
          o.bol_no && t("bolLine", o.bol_no, o.oto?.carrier)].filter(Boolean).join(" · "),
    back: o.state === "packed" ? withQuery("#/website/packed")
                               : withQuery("#/website/orders", { drop: ["via"] }),
  });

  let node;
  if (o.state === "packed") node = packedPanel(o);
  else if (o.state !== "waiting") {
    node = el("section", { class: "card" }, el("p", {}, t("notWaiting", statusName(o.status))));
  } else node = scanPanel(s);

  if (!replace) return node;
  $("content").replaceChildren(node);
  if (o.state === "waiting") focusScan();
  return node;
}

function scanPanel(s) {
  const left = remaining(s);
  const input = el("input", {
    id: "scan-input", class: "scan-input", type: "text", autocomplete: "off",
    autocapitalize: "off", spellcheck: "false", enterkeyhint: "done",
    "aria-describedby": "scan-hint",
  });
  input.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const v = input.value;
    input.value = "";
    handleScan(v);
  });

  const lines = s.order.items.map((x) => {
    const got = s.credited.get(x.line_item_id);
    const done = x.sku ? got >= x.quantity : s.manual.has(x.line_item_id);
    return el("tr", { class: done ? "done" : null },
      el("td", { class: "item-name" }, x.name || `#${x.product_id}`,
        x.options && x.options.length > 0 && el("div", { class: "opt-line" }, optionText(x.options)),
        !x.sku && el("div", { class: "muted small" }, t("noBarcodeNote"))),
      el("td", { class: "num qty" }, fmtNum(x.quantity)),
      el("td", { class: "sku" }, x.sku || el("span", { class: "muted" }, t("noSku"))),
      // dir="ltr": "1 / 2" must not turn into "2 / 1" in the Arabic layout.
      el("td", { class: "num" }, x.sku ? el("span", { dir: "ltr", class: "count" },
        `${fmtNum(got)} / ${fmtNum(x.quantity)}`) : "—"),
      el("td", { class: "state" }, x.sku
        ? el("span", { class: `mark ${done ? "ok" : "no"}`, title: t(done ? "ok" : "missing") },
            icon(done ? "check" : "cross"), el("span", { class: "sr-only" }, t(done ? "ok" : "missing")))
        : el("button", {
            type: "button", class: `hand-btn${done ? " on" : ""}`, "aria-pressed": String(done),
            onclick: () => {
              done ? s.manual.delete(x.line_item_id) : s.manual.add(x.line_item_id);
              drawOrder();
            },
          }, icon(done ? "check" : "cross"), t(done ? "confirmedByHand" : "confirmByHand"))));
  });

  const boxes = el("input", {
    id: "boxes", class: "boxes-input", type: "number", inputmode: "numeric",
    min: "1", max: "99", step: "1", value: s.boxes, "aria-describedby": "approve-msg",
  });
  boxes.addEventListener("input", () => {
    s.boxes = boxes.value;
    blocked.classList.remove("error");     // a new number clears the OTO error
    updateApprove();
  });
  // A scan that lands in the boxes field by mistake (a number too long to be
  // a box count, ended by the scanner's Enter) is treated as a scan instead.
  boxes.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    if (boxes.value.length > 2) {
      const v = boxes.value;
      s.boxes = "";
      handleScan(v);
    } else if (left === 0) approve();
  });

  const approveBtn = el("button", { id: "approve", type: "button", class: "approve-btn", onclick: approve },
    icon("check"), t("approve"));
  const blocked = el("p", { id: "approve-msg", class: "approve-msg", role: "status" });

  // Approve only APPEARS when every item is ticked (as drawn on the board);
  // before that, a line says how many are left. The box count is checked on
  // click, with its own message.
  function updateApprove() {
    approveBtn.hidden = left > 0;
    blocked.textContent = left > 0 ? t("approveBlocked", left) : "";
  }
  updateApprove();

  return el("div", { class: "scan-layout" },
    el("section", { class: "card scan-card" },
      el("label", { for: "scan-input", class: "scan-label" }, icon("scan"), t("scanLabel")),
      input,
      el("p", { id: "scan-hint", class: "muted small" }, t("scanHint")),
      el("p", { class: `scan-msg ${s.message ? s.message.kind : ""}`, "aria-live": "assertive" },
        s.message ? s.message.text : ""),
      el("div", { class: "scan-tools" },
        el("button", { type: "button", class: "icon-btn", disabled: !s.history.length, onclick: undoScan },
          t("undoScan")),
        el("button", { type: "button", class: "icon-btn",
                       disabled: !s.history.length && !s.manual.size,
                       onclick: () => {
                         const shown = s.testLabels;   // keep the label sheet open
                         session = newSession(s.order);
                         session.testLabels = shown;
                         drawOrder();
                       } },
          t("startOver")),
        // TEST ONLY: unique piece labels to scan (see showTestLabels).
        !s.testLabels && el("button", { type: "button", class: "icon-btn btn-icon",
                                        onclick: () => showTestLabels().catch((e) => banner(e.message)) },
          icon("scan"), t("testLabels")))),
    s.testLabels && testLabelsCard(s),
    el("section", { class: "card" }, el("div", { class: "table-wrap" }, el("table", { class: "scan-table" },
      el("thead", {}, el("tr", {},
        el("th", { scope: "col" }, t("colItem")),
        el("th", { scope: "col", class: "num" }, t("colQty")),
        el("th", { scope: "col" }, t("colItemNo")),
        el("th", { scope: "col", class: "num" }, t("colScanned")),
        el("th", { scope: "col" }, el("span", { class: "sr-only" }, t("colScanned"))))),
      el("tbody", {}, lines)))),
    el("section", { class: "card finish" },
      // Who packs it: filled in from the orders page if assigned there, and
      // changeable here. Saved as it changes; recorded with Approve.
      el("div", { class: "boxes-field packers-field" },
        el("label", { for: "packers" }, t("packedBy")),
        namesEditor(s.order.workers, async (names) => {
          const saved = (await api("/orders/assign", { method: "PUT",
            body: { order_ids: [s.order.id], workers: names } })).workers;
          s.order.workers = saved;
          return saved;
        }, { label: t("packedBy"), id: "packers" })),
      el("div", { class: "boxes-field" },
        // No hint text under the field (2026-09-30): only an error when
        // the number does not match OTO (see approve()).
        el("label", { for: "boxes" }, t("boxes")), boxes),
      el("div", { class: "approve-box" }, approveBtn, blocked)));
}

async function approve() {
  const s = session;
  if (!s || s.saving || remaining(s) > 0) return;
  const boxes = Number(s.boxes);
  if (!Number.isInteger(boxes) || boxes < 1 || boxes > 99) {
    $("approve-msg").textContent = t("needBoxes");
    $("boxes").focus();
    return;
  }
  // Must match OTO's box count, from the OTO sync (2026-09-30). A wrong
  // number is refused with a short error; no override. When OTO has no box
  // count for the order (not synced, or no shipment yet), nothing to check.
  const otoBoxes = s.order.oto?.boxes;
  if (otoBoxes && boxes !== otoBoxes) {
    const msg = $("approve-msg");
    msg.textContent = t("boxesMismatch");
    msg.classList.add("error");
    beep(false);
    $("boxes").focus();
    return;
  }
  s.saving = true;
  const btn = $("approve");
  btn.disabled = true;
  btn.lastChild.textContent = t("approving");
  try {
    const packed = await api(`/orders/${s.order.id}/approve`, {
      method: "POST",
      body: { boxes, scans: s.history.map((h) => h.code), manual: [...s.manual],
              workers: s.order.workers },
    });
    beep(true);
    session = newSession(packed);
    justApproved = packed.id;
    clearBanner();
    drawOrder();
  } catch (e) {
    beep(false);
    s.saving = false;
    // The server's reasons (e.g. "barcode '5025': 1 of 2 scanned") help the
    // person fixing it; the order is reloaded in case it changed in the store.
    banner([e.message, ...e.problems].join(" · "));
    render({ quiet: true });
  }
}

// The order just approved on this screen (2026-10-01): its panel offers
// only "Back to orders", which returns with the cursor in the search bar,
// ready for the next scan. Opened later (from Packed orders), a packed order
// still offers Undo packing / Undo loading.
let justApproved = null;
let focusSearchNext = false;   // put the cursor in the orders search bar on arrival

/** After Approve, or when opening an order that is already packed. */
function packedPanel(o) {
  const p = o.packing;
  const justNow = justApproved === o.id;
  const back = el("a", { class: justNow ? "btn-accent btn-icon" : "icon-btn",
                         href: withDates("#/website/orders"),
                         onclick: () => { focusSearchNext = true; } }, t("backToOrders"));
  return el("section", { class: "card packed-panel" },
    el("div", { class: "packed-icon" }, icon("check")),
    el("p", { class: "packed-text" }, justNow && Date.now() - Date.parse(p.packed_at_utc) < 60_000
      ? t("packedDone", o.number, p.boxes)
      : t("alreadyPacked", fmtUtc(p.packed_at_utc), p.boxes)),
    p.packed_by && p.packed_by.length > 0 && el("p", { class: "packed-by-line" },
      t("packedByLine", p.packed_by.join(state.lang === "ar" ? "، " : ", "))),
    // Which exact pieces went into it (unique labels), when labels were used.
    p.labels && p.labels.length > 0 && el("p", { class: "packed-labels" },
      el("strong", {}, t("labelsPacked")), " ",
      el("span", { dir: "ltr" }, p.labels.map((l) => l.barcode).join(", "))),
    // On the truck? (2026-09-30)
    p.shipped_at_utc ? el("p", { class: "packed-by-line" }, t("shippedLine", fmtUtc(p.shipped_at_utc)))
      : p.loaded > 0 && el("p", { class: "packed-by-line" }, t("loadedLine", p.loaded, p.boxes)),
    el("div", { class: "packed-actions" },
      back,
      !justNow && (p.loaded > 0
        ? el("button", { type: "button", class: "icon-btn danger", onclick: () => undoLoading(o) },
            t("undoLoading"))
        : el("button", { type: "button", class: "icon-btn danger", onclick: () => undoPacking(o) },
            t("undoPacking")))));
}

async function undoPacking(o) {
  if (!window.confirm(t("undoConfirm"))) return;
  try {
    const back = await api(`/orders/${o.id}/packing`, { method: "DELETE" });
    session = newSession(back);
    drawOrder();
  } catch (e) {
    banner(e.message);
  }
}

async function undoLoading(o) {
  if (!window.confirm(t("undoLoadingConfirm"))) return;
  try {
    const back = await api(`/orders/${o.id}/loading`, { method: "DELETE" });
    session = newSession(back);
    drawOrder();
  } catch (e) {
    banner(e.message);
  }
}

function focusScan() {
  const input = $("scan-input");
  if (input && document.activeElement !== $("boxes")) input.focus({ preventScroll: true });
}

// Keys typed while the focus is NOT in a text field (say, on a button just
// clicked) go to the scan box, so the scanner's digits and its Enter are
// never lost or turned into a button press.
document.addEventListener("keydown", (e) => {
  const input = $("scan-input");
  if (!input || e.ctrlKey || e.metaKey || e.altKey) return;
  const a = document.activeElement;
  if (a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.tagName === "SELECT")) return;
  if (e.key.length === 1 || e.key === "Enter") {
    input.focus({ preventScroll: true });
    if (e.key === "Enter") { e.preventDefault(); const v = input.value; input.value = ""; handleScan(v); }
  }
}, true);

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------
function init() {
  applyStatic();
  $("lang-toggle").addEventListener("click", () => {
    state.lang = state.lang === "ar" ? "en" : "ar";
    try { localStorage.setItem("lang", state.lang); } catch { /* storage blocked */ }
    applyStatic();
    // The scan screen redraws from memory so no scans are lost; others reload.
    if (route().view === orderView && session) drawOrder(); else render();
  });
  $("theme-toggle").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme !== "dark";
    if (dark) document.documentElement.dataset.theme = "dark";
    else delete document.documentElement.dataset.theme;
    try { localStorage.setItem("theme", dark ? "dark" : "light"); } catch { /* storage blocked */ }
    applyStatic();
  });
  window.addEventListener("hashchange", () => {
    selectedOrders.clear();
    orderFilter = { q: "", raw: "", exact: false };   // a new screen starts unfiltered
    render();
  });
  loadKnownWorkers();
  render();
}

init();
