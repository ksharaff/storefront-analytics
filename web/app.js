// Storefront Analytics dashboard — the whole front end, no build step.
//
// Flow: the URL hash holds the state (section, period, language, ...), so a
// link or a refresh reopens the same view. refresh() fetches the section's
// data from the metrics API and draw() turns it into DOM. Every 30 seconds
// the current view is fetched again quietly, so new orders appear without
// anyone pressing refresh.
//
// Safety: anything that came from the store (product names, payment titles)
// is inserted with textContent, never innerHTML, so a product named
// "<script>…" is displayed, not executed.

import { STRINGS, GROUPS, STATUSES, GATEWAYS } from "./i18n.js";

// The Customers tab was removed on request (2026-10-01); an old link with
// section=customers opens the Overview. Purchasing customers stays an Overview KPI.
const SECTIONS = ["overview", "sales", "products", "payments", "warehouse"];
const PERIODS = ["today", "yesterday", "last_7_days", "current_month",
                 "previous_month", "this_year", "custom"];
const LIMITS = [10, 25, 50, 100, 500];
const REFRESH_MS = 30_000;          // live refresh ("polling every ~30 s")
const STALE_MS = 30 * 60_000;       // warn if the last sync is older than this
const TZ = "Asia/Riyadh";           // all dates are shown in store time

// ---------------------------------------------------------------------------
// State, mirrored in the URL hash: #section=sales&period=this_year&lang=ar
// ---------------------------------------------------------------------------
const state = readState();
let lastData = null;       // last successful response, redrawn on language switch
let lastHealth = null;
let requestSeq = 0;        // ignore responses that arrive after a newer request
let chart = null;          // the one Chart.js instance on screen
let pendingChart = null;   // builds the chart once its canvas is on the page
const openTables = new Set();   // which "show as table" panels are open

function readState() {
  const h = new URLSearchParams(location.hash.slice(1));
  let savedLang = null;
  try { savedLang = localStorage.getItem("lang"); } catch { /* storage blocked */ }
  const browserLang = (navigator.language || "").startsWith("ar") ? "ar" : "en";
  const pick = (value, allowed, fallback) => (allowed.includes(value) ? value : fallback);
  return {
    lang: pick(h.get("lang") || savedLang, ["en", "ar"], browserLang),
    section: pick(h.get("section"), SECTIONS, "overview"),
    period: pick(h.get("period"), PERIODS, "current_month"),
    start: h.get("start") || "",
    end: h.get("end") || "",
    limit: pick(Number(h.get("limit")), LIMITS, 10),
    // Page of the "Today" orders list. Kept out of the URL on purpose: the
    // list moves as orders arrive, so a saved page 3 means little later.
    ordersPage: 1,
  };
}

function writeState() {
  const h = new URLSearchParams({ section: state.section, period: state.period,
                                  lang: state.lang });
  if (state.period === "custom") { h.set("start", state.start); h.set("end", state.end); }
  if (state.section === "products" && state.limit !== 10) h.set("limit", state.limit);
  history.replaceState(null, "", "#" + h.toString());
}

// ---------------------------------------------------------------------------
// Language and number formatting
// ---------------------------------------------------------------------------
const rtl = () => state.lang === "ar";

/** Translate a key; function entries take arguments. A missing key shows
 *  the key itself, which is easy to spot on screen. */
function t(key, ...args) {
  const v = STRINGS[state.lang][key];
  if (v === undefined) return key;
  return typeof v === "function" ? v(...args) : v;
}

// Arabic labels with Western digits (0-9) and the GREGORIAN calendar.
//  * nu-latn: Western digits, the usual choice in Saudi business software.
//    Change to "nu-arab" for Arabic-Indic digits (٠-٩).
//  * ca-gregory: without it, "ar-SA" defaults to the Hijri (Umm al-Qura)
//    calendar, and dates come out as "12 رجب 1447 هـ" — wrong for sales
//    reporting, which follows the Gregorian calendar like the store does.
const locale = () => (rtl() ? "ar-SA-u-nu-latn-ca-gregory" : "en-US");

const fmtNum = (n, digits = 0) =>
  n == null ? "—" : new Intl.NumberFormat(locale(), { maximumFractionDigits: digits }).format(n);

/** The Saudi Riyal sign, U+20C1 (Unicode 17). Drawn by the bundled
 *  "Saudi Riyal" font (styles.css), since most systems can't show it yet. */
const RIYAL = "\u20C1";

/** SAR amounts with the riyal sign. `compact` gives "⃁ 6.98M" for tiles;
 *  full otherwise. Intl still does the work (grouping, compact "M"/"مليون",
 *  and where the currency goes in each language); only the "SAR" / "ر.س."
 *  text it produces is swapped for the sign. The sign lands before the
 *  number in English and after it in Arabic, which on screen is the left
 *  side of the number in both. */
function fmtMoney(n, compact = false) {
  if (n == null) return "—";
  return new Intl.NumberFormat(locale(), {
    style: "currency", currency: "SAR",
    notation: compact && Math.abs(n) >= 10_000 ? "compact" : "standard",
    maximumFractionDigits: compact && Math.abs(n) >= 10_000 ? 2 : 0,
  }).formatToParts(n)
    .map((part) => (part.type === "currency" ? RIYAL : part.value))
    .join("");
}

/** The API sends percentages as numbers like 12.3 (meaning 12.3%). */
const fmtPct = (p, sign = false) =>
  p == null ? "—" : new Intl.NumberFormat(locale(), {
    style: "percent", maximumFractionDigits: 1, signDisplay: sign ? "exceptZero" : "auto",
  }).format(p / 100);

/** "2026-09-01T00:00:00" — a LOCAL (Riyadh) wall-clock time from the API.
 *  Parsed as if UTC and formatted in UTC, so the viewer's own computer
 *  timezone can never shift it. */
function bucketLabel(bucket, hourly) {
  const [d, time] = bucket.split("T");
  const [y, m, day] = d.split("-").map(Number);
  const [hh] = time.split(":").map(Number);
  const date = new Date(Date.UTC(y, m - 1, day, hh));
  const opts = hourly ? { hour: "2-digit", minute: "2-digit", hourCycle: "h23" }
                      : { month: "short", day: "numeric" };
  return new Intl.DateTimeFormat(locale(), { ...opts, timeZone: "UTC" }).format(date);
}

/** "2026-03-15T00:30:00", an order time in Riyadh wall-clock, → "Mar 15, 00:30".
 *  Same trick as bucketLabel: parsed and formatted as UTC, so the viewer's
 *  own computer timezone can't move it. */
function orderTime(local) {
  const [d, time] = local.split("T");
  const [y, m, day] = d.split("-").map(Number);
  const [hh, mm] = time.split(":").map(Number);
  return new Intl.DateTimeFormat(locale(), {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    hourCycle: "h23", timeZone: "UTC",
  }).format(new Date(Date.UTC(y, m - 1, day, hh, mm)));
}

/** "just now", "5 min ago", "3 hours ago", and from 24 hours on days AND
 *  hours (2026-09-29): 45 hours is "1 day and 21 hours ago", not
 *  "45 hour(s) ago". Whole days drop the hours: "2 days ago". */
function agoText(iso) {
  const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60_000);
  if (mins < 1) return t("justNow");
  if (mins < 60) return t("minutesAgo", mins);
  const hours = Math.floor(mins / 60);
  if (hours < 24) return t("hoursAgo", hours);
  return t("daysAgo", Math.floor(hours / 24), hours % 24);
}

const statusName = (slug) => STATUSES[state.lang][slug] || slug;
const groupName = (g) => GROUPS[state.lang][g] || g;
/** "Tamara", or "Tamara · PoS" for Point of Sale orders paid that way. */
const gatewayName = (m) => (GATEWAYS[state.lang][m.gateway] || m.gateway) + (m.pos ? ` · ${t("pos")}` : "");
const todayInRiyadh = () => new Intl.DateTimeFormat("en-CA", { timeZone: TZ }).format(new Date());

// ---------------------------------------------------------------------------
// Tiny DOM helper: el("div", {class: "x"}, "text", childNode, ...)
// Strings become text nodes (safe); nothing is ever parsed as HTML.
// ---------------------------------------------------------------------------
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "style") Object.assign(node.style, v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

// ---------------------------------------------------------------------------
// Icons: simple line drawings of our own (24x24, stroked with currentColor).
// ---------------------------------------------------------------------------
const ICONS = {
  overview:  ["M4 4h7v7H4z", "M13 4h7v4h-7z", "M13 10h7v10h-7z", "M4 13h7v7H4z"],
  sales:     ["M3 17l6-6 4 4 8-8", "M15 7h6v6"],
  products:  ["M3 7.5l9-4.5 9 4.5v9l-9 4.5-9-4.5z", "M3 7.5l9 4.5 9-4.5", "M12 12v9"],
  customers: ["M9 11.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z", "M2.5 20c0-3.6 2.9-6 6.5-6s6.5 2.4 6.5 6",
              "M16 5.2a3 3 0 0 1 0 5.6", "M17.5 14.3c2.3.6 4 2.6 4 5.7"],
  payments:  ["M3 6h18v12H3z", "M3 10h18", "M7 15h4"],
  money:     ["M12 3v18", "M16.5 7.5c0-1.9-2-3-4.5-3s-4.5 1.2-4.5 3.2 2 2.8 4.5 3.3 4.5 1.3 4.5 3.3-2 3.2-4.5 3.2-4.5-1.1-4.5-3"],
  bag:       ["M5 8h14l-1.2 12H6.2z", "M9 8V6.5a3 3 0 0 1 6 0V8"],
  receipt:   ["M6 3h12v18l-3-2-3 2-3-2-3 2z", "M9 8h6", "M9 12h6"],
  returns:   ["M9 14L4 9l5-5", "M4 9h11a5 5 0 0 1 0 10h-3"],
  check:     ["M5 12.5l4.5 4.5L19 7.5"],
  cross:     ["M6.5 6.5l11 11", "M17.5 6.5l-11 11"],
  sun:       ["M12 16.5a4.5 4.5 0 1 0 0-9 4.5 4.5 0 0 0 0 9z", "M12 2.5v2", "M12 19.5v2",
              "M4.6 4.6l1.4 1.4", "M18 18l1.4 1.4", "M2.5 12h2", "M19.5 12h2", "M4.6 19.4l1.4-1.4", "M18 6l1.4-1.4"],
  moon:      ["M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"],
  storage:   ["M3 9l9-5 9 5v11H3z", "M7 20v-7h10v7", "M7 16h10"],
  warehouse: ["M2.5 6.5h11v10h-11z", "M13.5 9.5h4l3.5 3.5v3.5h-7.5", "M6.5 19.5a2 2 0 1 0 0-4 2 2 0 0 0 0 4z",
              "M17 19.5a2 2 0 1 0 0-4 2 2 0 0 0 0 4z"],
  clock:     ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z", "M12 7v5l3.5 2"],
  box:       ["M3.5 8L12 4l8.5 4v8.5L12 20.5l-8.5-4z", "M3.5 8L12 12l8.5-4", "M12 12v8.5"],
  percent:   ["M18.5 5.5l-13 13", "M7 9.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z", "M17 19.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z"],
};

/** An inline SVG icon. Decorative: the words next to it carry the meaning. */
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  for (const d of ICONS[name] || []) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------
async function api(path, params = {}) {
  const res = await fetch(`/api/${path}?${new URLSearchParams(params)}`);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    // FastAPI puts the reason in `detail` (a string for 400, a list for 422).
    const d = body.detail;
    throw new Error(typeof d === "string" ? d : `HTTP ${res.status}`);
  }
  return body;
}

// ---------------------------------------------------------------------------
// The static parts: labels, period buttons, tabs, language button
// ---------------------------------------------------------------------------
function applyStatic() {
  document.documentElement.lang = state.lang;
  document.documentElement.dir = STRINGS[state.lang].dir;
  document.title = t("title");
  document.querySelectorAll("[data-t]").forEach((n) => { n.textContent = t(n.dataset.t); });

  // The theme button shows what you would switch TO: a moon in light mode.
  const dark = document.documentElement.dataset.theme === "dark";
  const themeBtn = document.getElementById("theme-toggle");
  themeBtn.replaceChildren(icon(dark ? "sun" : "moon"));
  themeBtn.setAttribute("aria-label", t(dark ? "themeLight" : "themeDark"));
  themeBtn.title = t(dark ? "themeLight" : "themeDark");

  const langBtn = document.getElementById("lang-toggle");
  langBtn.textContent = t("switchLang");
  langBtn.setAttribute("aria-label", t("switchLangLabel"));
  langBtn.lang = rtl() ? "en" : "ar";   // the button text is in the other language

  const periodBox = document.getElementById("period-buttons");
  periodBox.replaceChildren(...PERIODS.map((p) => el("button", {
    type: "button", "aria-pressed": String(state.period === p),
    onclick: () => setPeriod(p),
  }, t(p))));
  document.getElementById("period-group").setAttribute("aria-label", t("period"));

  const form = document.getElementById("custom-range");
  form.hidden = state.period !== "custom";
  const startIn = document.getElementById("custom-start");
  const endIn = document.getElementById("custom-end");
  // Sensible defaults: the current month so far.
  const today = todayInRiyadh();
  startIn.value = state.start || today.slice(0, 8) + "01";
  endIn.value = state.end || today;
  startIn.max = endIn.max = today;

  const nav = document.getElementById("nav");
  nav.setAttribute("aria-label", t("title"));
  nav.replaceChildren(...SECTIONS.map((s) => el("button", {
    type: "button", role: "tab", "aria-selected": String(state.section === s),
    onclick: () => setSection(s),
  }, icon(s), t(s))),
  // The storage app is its own page (/storage), shown as the last tab. It is
  // a link rather than a section: it has no period filter and its own screens.
  // The chosen language travels with it (both pages share localStorage "lang").
  el("a", { href: "/storage", role: "tab", "aria-selected": "false", class: "nav-link",
            onclick: () => { try { localStorage.setItem("lang", state.lang); } catch { /* blocked */ } } },
    icon("storage"), t("storage")));
  document.getElementById("page-title").textContent = t(state.section);
}

function setPeriod(p) {
  state.period = p;
  state.ordersPage = 1;          // a new period starts at its newest orders
  applyStatic();
  if (p === "custom" && !(state.start && state.end)) {
    // Wait for the user to pick dates and press Apply.
    writeState();
    return;
  }
  refresh({ userAction: true });
}

function setSection(s) {
  state.section = s;
  state.ordersPage = 1;
  applyStatic();
  refresh({ userAction: true });
}

// ---------------------------------------------------------------------------
// Fetch + draw
// ---------------------------------------------------------------------------
async function refresh({ userAction = false } = {}) {
  writeState();
  const seq = ++requestSeq;
  const main = document.getElementById("content");
  if (userAction) main.classList.add("loading");   // dim, don't blank

  const params = { period: state.period };
  if (state.period === "custom") Object.assign(params, { start: state.start, end: state.end });
  if (state.section === "products") params.limit = state.limit;
  if (state.section === "sales") params.limit = 10;
  // Only "Today" pages through its orders; the 30-second refresh keeps the
  // page the viewer is on.
  if (state.section === "overview" && state.period === "today" && state.ordersPage > 1) {
    params.orders_page = state.ordersPage;
  }

  try {
    const [data, health] = await Promise.all([api(state.section, params), api("health")]);
    if (seq !== requestSeq) return;        // a newer request has started; drop this one
    lastData = { section: state.section, data };
    lastHealth = health;
    // The server clamps a page past the end (the list can shrink); follow it.
    if (data.recent_orders_page) state.ordersPage = data.recent_orders_page.page;
    draw();
  } catch (err) {
    if (seq !== requestSeq) return;
    // Keep whatever is on screen and say what went wrong.
    showBanners([["error", `${t("loadError")}: ${err.message}. ${t("retrying")}`]]);
  } finally {
    if (seq === requestSeq) main.classList.remove("loading");
  }
}

/** "1 Sep – 23 Sep 2026 · Riyadh time": what the selected period covers.
 *  The API's end is exclusive, so a full day ending at midnight displays as
 *  the day before it. */
function periodText(period) {
  const start = new Date(period.current.start_utc);
  const end = new Date(new Date(period.current.end_utc).getTime() - 1);
  const f = new Intl.DateTimeFormat(locale(), { day: "numeric", month: "short", year: "numeric", timeZone: TZ });
  const sameDay = f.format(start) === f.format(end);
  return `${sameDay ? f.format(start) : f.formatRange(start, end)} · ${t("riyadhTime")}`;
}

function draw() {
  if (!lastData || lastData.section !== state.section) return;
  const d = lastData.data;
  drawFreshness(lastHealth);
  document.getElementById("page-title").textContent = t(state.section);
  document.getElementById("page-sub").textContent = periodText(d.period);

  // Notes about the period itself, above the numbers they qualify.
  const banners = [];
  // The Warehouse tab reads the storage app's own records, which have no
  // "previous period" and don't depend on the imported order history.
  const periodNotes = state.section !== "warehouse";
  if (periodNotes && !d.period.complete) {
    const when = new Intl.DateTimeFormat(locale(), {
      dateStyle: "long", timeStyle: "short", timeZone: TZ,
    }).format(new Date(lastHealth.data_start_utc));
    banners.push(["warn", t("incomplete", when)]);
  }
  if (periodNotes && !d.period.comparison_available) banners.push(["", t("noComparisonBanner")]);
  showBanners(banners);

  if (chart) { chart.destroy(); chart = null; }
  const views = { overview, sales, products, payments, warehouse };
  document.getElementById("content").replaceChildren(...views[state.section](d));
  // Chart.js sizes itself from its canvas, so create it only now that the
  // canvas is actually in the page.
  if (pendingChart) { pendingChart(); pendingChart = null; }
}

function showBanners(list) {
  document.getElementById("banners").replaceChildren(
    ...list.map(([kind, text]) => el("div", { class: `banner ${kind}`, role: kind === "error" ? "alert" : null }, text)));
}

function drawFreshness(health) {
  const node = document.getElementById("freshness");
  node.classList.remove("stale");
  node.removeAttribute("title");
  if (!health || !health.last_sync_utc) { node.textContent = t("neverSynced"); return; }
  node.textContent = t("synced", agoText(health.last_sync_utc));
  if (Date.now() - new Date(health.last_sync_utc).getTime() > STALE_MS) {
    node.classList.add("stale");
    node.title = t("staleSync");
  }
}

// ---------------------------------------------------------------------------
// Building blocks
// ---------------------------------------------------------------------------

/** A KPI tile. `metric` is {value, previous, change_pct} from the API, or
 *  {value} for a number with no comparison. goodWhenUp decides the colour of
 *  the change: more sales is good, more failed payments is bad.
 *
 *  points: for a metric that is itself a percentage (return rate), show the
 *  change in percentage POINTS (6.4% -> 6.5% is "+0.1 pts"). The relative
 *  change ("+1.6%") of a percentage is correct maths but misleads people. */
function deltaPill(metric, { goodWhenUp = true, points = false } = {}) {
  if (!("change_pct" in metric)) return null;
  const comparable = metric.change_pct != null || (points && metric.previous != null && metric.value != null);
  if (!comparable) return el("span", { class: "delta none" }, t("noComparison"));
  // Round BEFORE deciding the direction: +0.02 pts displays as "0", and
  // a "0" with a red up-arrow is a contradiction.
  const c = Math.round((points ? metric.value - metric.previous : metric.change_pct) * 10) / 10;
  const shown = points
    ? `${new Intl.NumberFormat(locale(), { maximumFractionDigits: 1, signDisplay: "exceptZero" }).format(c)} ${t("pts")}`
    : fmtPct(c, true);
  const dir = c > 0 ? "up" : c < 0 ? "down" : "flat";
  const good = dir === "flat" ? "flat" : (dir === "up") === goodWhenUp ? "good" : "bad";
  const arrow = dir === "up" ? "▲ " : dir === "down" ? "▼ " : "";
  return el("span", { class: `delta ${good}` }, arrow + shown);
}

/** Small bars showing the trend across the period (at most 20 bars; buckets
 *  are summed into bins). Decorative: the tile's number is the value, and
 *  the chart and its table have the detail. The newest bin is full accent. */
function sparkline(points, pick) {
  if (!points || points.length < 2) return null;
  const n = Math.min(20, points.length);
  const size = Math.ceil(points.length / n);
  const bins = [];
  for (let i = 0; i < points.length; i += size) bins.push(pick(points.slice(i, i + size)));
  const max = Math.max(...bins, 0) || 1;
  // Oldest first in the DOM; the flex row puts it on the left in English and
  // on the right in Arabic, matching the chart's time direction.
  return el("div", { class: "spark", "aria-hidden": "true" }, bins.map((v, i) =>
    el("i", { class: i === bins.length - 1 ? "now" : null,
              style: { height: `${Math.max(5, (v / max) * 100)}%` } })));
}

// What each tile's sparkline sums over a bin of chart buckets.
const SPARK = {
  sales: (b) => b.reduce((a, p) => a + p.sales, 0),
  orders: (b) => b.reduce((a, p) => a + p.orders, 0),
  aov: (b) => { const o = b.reduce((a, p) => a + p.orders, 0); return o ? b.reduce((a, p) => a + p.sales, 0) / o : 0; },
  customers: (b) => b.reduce((a, p) => a + p.customers, 0),   // shape only
};

function kpiTile(label, metric, format, { goodWhenUp = true, sub = null, points = false,
                                          iconName = "money", badge = 1, spark = null } = {}) {
  const pill = deltaPill(metric, { goodWhenUp, points });
  return el("div", { class: "card kpi" },
    el("div", { class: "kpi-head" },
      el("span", { class: `badge b${badge}` }, icon(iconName)),
      el("div", {},
        el("div", { class: "label" }, label),
        el("div", { class: "value", title: metric.value == null ? null : String(metric.value) },
          format(metric.value)))),
    pill && el("div", { class: "kpi-foot" }, pill,
      pill.classList.contains("none") ? null : el("span", { class: "ctx" }, t("vsPrevious"))),
    spark,
    sub && el("div", { class: "sub" }, sub));
}

function card(title, ...body) {
  return el("section", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, title)), ...body);
}

/** A ranked list drawn as bars: name ... value, bar underneath. Chart and table at once:
 *  every value is printed, so nothing depends on colour or hovering. */
function barTable(rows, { name, value, format, sub = null, detail = null }) {
  if (!rows.length) return el("p", { class: "empty" }, t("nothingHere"));
  const max = Math.max(...rows.map((r) => Number(value(r)) || 0), 0) || 1;
  return el("ul", { class: "bars" }, rows.map((r) => {
    const v = Number(value(r)) || 0;
    const nm = name(r);
    return el("li", {},
      el("div", { class: "row" },
        el("span", { class: "name", title: nm }, nm, detail && el("small", {}, detail(r))),
        el("span", { class: "val" }, format(v), sub && el("small", {}, sub(r)))),
      el("div", { class: "track" }, el("div", { class: "bar", style: { width: `${(v / max) * 100}%` } })));
  }));
}

/** A plain table. cols: [{label, get, num?}] */
function table(cols, rows) {
  if (!rows.length) return el("p", { class: "empty" }, t("nothingHere"));
  return el("table", {},
    el("thead", {}, el("tr", {}, cols.map((c) => el("th", { class: c.num ? "num" : null, scope: "col" }, c.label)))),
    el("tbody", {}, rows.map((r) => el("tr", {}, cols.map((c) =>
      el("td", { class: c.num ? "num" : null }, c.get(r)))))));
}

/** A "Show as table" panel that stays open across the 30-second refresh. */
function tableToggle(key, tableNode) {
  const details = el("details", { open: openTables.has(key) },
    el("summary", {}, t("showTable")), el("div", { class: "table-scroll" }, tableNode));
  details.addEventListener("toggle", () => {
    details.open ? openTables.add(key) : openTables.delete(key);
  });
  return details;
}

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/** Sales over time: the one real chart. Single series, so no legend — the
 *  title says what it is. Hover anywhere for the value; the table twin
 *  underneath has every number without hovering. */
function salesChart(sot, headline = null) {
  const hourly = sot.granularity === "hour";
  const labels = sot.points.map((p) => bucketLabel(p.bucket, hourly));
  const canvas = el("canvas", { role: "img", "aria-label": t(hourly ? "salesOverTimeHourly" : "salesOverTimeDaily") });

  const twin = table([
    { label: t(hourly ? "colTime" : "colDate"), get: (p) => bucketLabel(p.bucket, hourly) },
    { label: t("colSales"), get: (p) => fmtMoney(p.sales), num: true },
    { label: t("colOrders"), get: (p) => fmtNum(p.orders), num: true },
  ], sot.points);

  const node = card(t(hourly ? "salesOverTimeHourly" : "salesOverTimeDaily"),
    // The headline repeats Total sales so the chart reads on its own.
    headline && el("div", { class: "headline" },
      el("span", { class: "big" }, fmtMoney(headline.value)), deltaPill(headline)),
    el("div", { class: "chart-box" }, canvas),
    el("p", { class: "note" }, t("noteSales")),
    tableToggle("sales-over-time", twin));

  // Chart.js is loaded with `defer`; it is ready before this module runs.
  const Chart = window.Chart;
  const line = cssVar("--accent");
  // A thin vertical guide under the pointer (the crosshair).
  const crosshair = {
    id: "crosshair",
    afterDatasetsDraw(c) {
      const active = c.tooltip?.getActiveElements?.() || [];
      if (!active.length) return;
      const x = active[0].element.x, { top, bottom } = c.chartArea;
      c.ctx.save();
      c.ctx.strokeStyle = cssVar("--axis"); c.ctx.lineWidth = 1;
      c.ctx.beginPath(); c.ctx.moveTo(x, top); c.ctx.lineTo(x, bottom); c.ctx.stroke();
      c.ctx.restore();
    },
  };
  pendingChart = () => { chart = new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: [{
        data: sot.points.map((p) => p.sales),
        borderColor: line, backgroundColor: cssVar("--accent-wash"),
        fill: "origin", borderWidth: 2.5, borderJoinStyle: "round", borderCapStyle: "round",
        pointRadius: 0, pointHoverRadius: 6,
        pointHoverBackgroundColor: line, pointHoverBorderColor: cssVar("--surface"),
        pointHoverBorderWidth: 3,
        // Smooth, but "monotone" never overshoots: a curve that dips below a
        // real value (or below zero) between two days would be inventing data.
        cubicInterpolationMode: "monotone",
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      // Room at both ends so the first and last date labels are not clipped.
      layout: { padding: { left: 24, right: 24 } },
      animation: false,            // no re-animation on every 30 s refresh
      locale: locale(),
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          rtl: rtl(), textDirection: rtl() ? "rtl" : "ltr",
          displayColors: false,
          // A light card-style tooltip to match the page.
          backgroundColor: cssVar("--surface"), borderColor: cssVar("--border"), borderWidth: 1,
          titleColor: cssVar("--muted"), bodyColor: cssVar("--ink"),
          titleFont: { weight: "500" }, bodyFont: { weight: "600", size: 13 },
          padding: 12, cornerRadius: 12, caretSize: 0,
          callbacks: {
            label: (ctx) => `${fmtMoney(ctx.parsed.y)} · ${fmtNum(sot.points[ctx.dataIndex].orders)} ${t("ordersUnit")}`,
          },
        },
      },
      scales: {
        // Time runs right-to-left in Arabic, like the text around it.
        x: { reverse: rtl(), grid: { display: false },
             // With only a few points, leave half a step at each end so the
             // first and last date labels fit inside the chart.
             offset: sot.points.length <= 14,
             border: { color: cssVar("--axis") },
             // Fewer, well-spaced dates; the table twin has every one.
             ticks: { color: cssVar("--muted"), autoSkip: true, autoSkipPadding: 16,
                      maxTicksLimit: canvas.parentElement.clientWidth < 500 ? 4 : 8,
                      maxRotation: 0 } },
        y: { position: rtl() ? "right" : "left", beginAtZero: true,
             // A period with no sales would otherwise get a 0–1 axis with
             // fractional steps that all round to "⃁ 1".
             suggestedMax: sot.points.some((p) => p.sales > 0) ? undefined : 1000,
             grid: { color: cssVar("--grid") }, border: { display: false },
             ticks: { color: cssVar("--muted"), maxTicksLimit: 5,
                      callback: (v) => fmtMoney(v, true) } },
      },
    },
    plugins: [crosshair],
  }); };
  return node;
}

/** Payment success rate as a ring of 60 ticks: a meter (one ratio against
 *  100%), not a two-slice pie. The unfilled ticks are a lighter track, and
 *  the number sits in the middle, so nothing depends on reading the ring. */
function gaugeCard(summary) {
  const s = summary.successful.value, f = summary.failed.value;
  const rate = s + f ? (100 * s) / (s + f) : null;
  const N = 60, on = rate == null ? 0 : Math.round((rate / 100) * N);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 200 200");
  svg.setAttribute("aria-hidden", "true");
  for (let i = 0; i < N; i++) {
    // Clockwise from the top; mirrored in Arabic so it fills from the right.
    const a = ((rtl() ? -1 : 1) * (i / N) * 360 - 90) * (Math.PI / 180);
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    Object.entries({ x1: 100 + 74 * Math.cos(a), y1: 100 + 74 * Math.sin(a),
                     x2: 100 + 94 * Math.cos(a), y2: 100 + 94 * Math.sin(a) })
      .forEach(([k, v]) => line.setAttribute(k, v.toFixed(2)));
    line.setAttribute("class", i < on ? "tick on" : "tick");
    svg.append(line);
  }
  const mini = (label, metric, goodWhenUp) => el("div", { class: "mini" },
    el("span", { class: "label" }, label),
    el("span", { class: "value" }, fmtNum(metric.value), deltaPill(metric, { goodWhenUp })));
  return el("section", { class: "card gauge-card" },
    el("div", { class: "card-head" }, el("h2", {}, t("successRate"))),
    el("div", { class: "gauge", role: "img", "aria-label": `${t("successRate")}: ${fmtPct(rate)}` },
      svg, el("div", { class: "center" }, el("strong", {}, fmtPct(rate)), el("span", {}, t("ofAttempts")))),
    el("div", { class: "mini-stats" },
      mini(t("successfulPayments"), summary.successful, true),
      mini(t("failedPayments"), summary.failed, false)));
}

// ---------------------------------------------------------------------------
// Sections
// ---------------------------------------------------------------------------
function kpiRow(k, pts) {
  return el("div", { class: "kpis" },
    kpiTile(t("totalSales"), k.sales, (v) => fmtMoney(v, true),
      { iconName: "money", badge: 1, spark: sparkline(pts, SPARK.sales) }),
    kpiTile(t("orders"), k.orders, fmtNum,
      { iconName: "bag", badge: 2, spark: sparkline(pts, SPARK.orders),
        sub: t("ofAllOrders", fmtNum(k.orders_all)) }),
    kpiTile(t("aov"), k.aov, (v) => fmtMoney(v),
      { iconName: "receipt", badge: 3, spark: sparkline(pts, SPARK.aov) }),
    kpiTile(t("purchasingCustomers"), k.customers, fmtNum,
      { iconName: "customers", badge: 4, spark: sparkline(pts, SPARK.customers) }));
}

/** "Total sales" split by the sale statuses behind it. */
function salesByStatusCard(rows) {
  return card(t("salesByStatus"), barTable(rows, {
    name: (r) => statusName(r.status), value: (r) => r.sales, format: (v) => fmtMoney(v),
    sub: (r) => `${fmtNum(r.orders)} ${t("ordersUnit")}`,
  }), el("p", { class: "note" }, t("noteSalesByStatus")));
}

/** The 10 newest orders in the period: one row per product line, grouped per
 *  order (each order is its own <tbody>, so the divider falls between orders,
 *  not between an order's products). Date, order number, total and status
 *  span the order's rows. Long orders show 3 products and "+N more". */
const RECENT_ITEMS_SHOWN = 3;
function recentOrdersCard(orders, info) {
  // "Today" lists ALL of the day's orders, 10 per page (2026-09-23).
  // Every other period shows its 10 newest, without page buttons.
  const today = state.period === "today";
  const title = t(today ? "todaysOrders" : "recentOrders");
  if (!orders.length) return card(title, el("p", { class: "empty" }, t("nothingHere")));

  const head = el("thead", {}, el("tr", {},
    el("th", { scope: "col" }, t("colDate")),
    el("th", { scope: "col" }, t("colOrder")),
    el("th", { scope: "col" }, t("colProduct")),
    el("th", { scope: "col", class: "num" }, t("colQuantity")),
    el("th", { scope: "col", class: "num" }, t("colPrice")),
    el("th", { scope: "col", class: "num" }, t("colOrderTotal")),
    el("th", { scope: "col" }, t("colStatus"))));

  const bodies = orders.map((o) => {
    const shown = o.items.slice(0, RECENT_ITEMS_SHOWN);
    const hidden = o.items.length - shown.length;
    // Product cells for each row: the lines, then "+N more", or a
    // placeholder when the order has no product lines at all.
    const lines = shown.map((i) => [
      el("td", { class: "product", title: i.name || "" }, i.name || `#${i.product_id}`),
      el("td", { class: "num" }, fmtNum(i.quantity)),
      el("td", { class: "num" }, fmtMoney(i.value)),
    ]);
    if (hidden > 0) lines.push([el("td", { class: "more", colspan: 3 }, t("moreItems", hidden))]);
    if (!lines.length) lines.push([el("td", { class: "more", colspan: 3 }, t("noItems"))]);

    // The order-level cells go on the first row and span all of its rows.
    const span = lines.length;
    const rows = lines.map((cells, n) => el("tr", {}, n === 0 && [
      el("td", { rowspan: span, class: "when" }, orderTime(o.created_local)),
      el("td", { rowspan: span }, `#${o.number}`),
    ], cells, n === 0 && [
      el("td", { rowspan: span, class: "num total" }, fmtMoney(o.total)),
      // The status word is always shown; the tint (by outcome group) only
      // helps scanning, so nothing depends on colour alone.
      el("td", { rowspan: span }, el("span", { class: `status-tag g-${o.status_group}` }, statusName(o.status))),
    ]));
    return el("tbody", {}, rows);
  });

  return card(title,
    el("div", { class: "recent-scroll" }, el("table", { class: "recent" }, head, bodies)),
    today && info && info.pages > 1 && pager(info),
    el("p", { class: "note" }, t(today ? "noteTodaysOrders" : "noteRecentOrders")));
}

/** "Showing 11–20 of 57 orders"  [Previous] 1 … 4 5 6 … 12 [Next]
 *  Numbers shown: the first and last page, and the current one ±1, with
 *  "…" where pages are skipped, so the row stays short on a busy day. */
function pager({ page, pages, per_page, total }) {
  const go = (n) => { state.ordersPage = n; refresh({ userAction: true }); };
  const btn = (label, n, { current = false, disabled = false } = {}) =>
    el("button", { type: "button", class: current ? "current" : null, disabled,
                   "aria-current": current ? "page" : null,
                   onclick: disabled || current ? null : () => go(n) }, label);

  const nums = [...new Set([1, page - 1, page, page + 1, pages])]
    .filter((n) => n >= 1 && n <= pages).sort((a, b) => a - b);
  const buttons = [btn(t("prevPage"), page - 1, { disabled: page === 1 })];
  let last = 0;
  for (const n of nums) {
    if (n - last > 1) buttons.push(el("span", { class: "gap", "aria-hidden": "true" }, "…"));
    buttons.push(btn(fmtNum(n), n, { current: n === page }));
    last = n;
  }
  buttons.push(btn(t("nextPage"), page + 1, { disabled: page === pages }));

  const from = (page - 1) * per_page + 1, to = Math.min(page * per_page, total);
  return el("nav", { class: "pager", "aria-label": t("ordersPages") },
    el("span", { class: "muted" }, t("showingRange", fmtNum(from), fmtNum(to), fmtNum(total))),
    el("div", { class: "pages" }, buttons));
}

function overview(d) {
  const totalOrders = d.status_groups.reduce((a, g) => a + g.orders, 0);
  return [
    kpiRow(d.kpis, d.sales_over_time.points),
    el("div", { class: "grid-main" },
      salesChart(d.sales_over_time, d.kpis.sales),
      gaugeCard(d.payment_summary)),
    el("div", { class: "grid-3" },
      salesByStatusCard(d.sales_by_status),
      card(t("statusGroups"), barTable(d.status_groups, {
        name: (g) => groupName(g.status_group), value: (g) => g.orders, format: fmtNum,
        sub: (g) => fmtPct(totalOrders ? (100 * g.orders) / totalOrders : null),
      })),
      card(t("paymentMethods"), barTable(d.payment_methods, {
        name: gatewayName, value: (m) => m.sales, format: (v) => fmtMoney(v),
        detail: (m) => (m.enabled || m.pos ? null : t("notEnabled")),
        sub: (m) => `${fmtNum(m.successful)} ${t("ordersUnit")}`,
      }))),
    card(t("topProducts"), barTable(d.top_products, {
      name: (p) => p.name || `#${p.product_id}`, value: (p) => p.value, format: (v) => fmtMoney(v),
      sub: (p) => `${fmtNum(p.quantity)} ${t("units")}`,
    }), el("p", { class: "note" }, t("noteProductValue"))),
    recentOrdersCard(d.recent_orders, d.recent_orders_page),
  ];
}

function sales(d) {
  const r = d.return_rate;
  const pts = d.sales_over_time.points;
  return [
    el("div", { class: "kpis" },
      kpiTile(t("totalSales"), d.kpis.sales, (v) => fmtMoney(v, true),
        { iconName: "money", badge: 1, spark: sparkline(pts, SPARK.sales) }),
      kpiTile(t("orders"), d.kpis.orders, fmtNum,
        { iconName: "bag", badge: 2, spark: sparkline(pts, SPARK.orders),
          sub: t("ofAllOrders", fmtNum(d.kpis.orders_all)) }),
      kpiTile(t("returnRate"), r, (v) => fmtPct(v), {
        iconName: "returns", badge: 3, goodWhenUp: false, points: true,
        sub: t("returnedOf", fmtNum(r.returned_orders), fmtNum(r.sale_orders)) }),
      kpiTile(t("refunded"), { value: d.refunds.amount }, (v) => fmtMoney(v, true),
        { iconName: "receipt", badge: 4, sub: t("refundsCount", fmtNum(d.refunds.refunds)) })),
    el("div", { class: "grid-main" },
      salesChart(d.sales_over_time, d.kpis.sales),
      salesByStatusCard(d.sales_by_status)),
    el("div", { class: "grid-2" },
      card(t("ordersByStatus"), barTable(d.orders_by_status, {
        name: (s) => statusName(s.status), detail: (s) => groupName(s.status_group),
        value: (s) => s.orders, format: fmtNum, sub: (s) => fmtMoney(s.value),
      })),
      card(t("mostReturned"), barTable(d.most_returned, {
        name: (p) => p.name || `#${p.product_id}`, value: (p) => p.quantity, format: fmtNum,
        sub: (p) => fmtMoney(p.value),
      }), el("p", { class: "note" }, t("noteRefunds")))),
    heatmapCard(d.by_hour_weekday),
    el("p", { class: "note" }, t("noteReturnRate")),
  ];
}

function products(d) {
  const limitSelect = el("select", {
    "aria-label": t("rows"),
    onchange: (e) => { state.limit = Number(e.target.value); refresh({ userAction: true }); },
  }, LIMITS.map((n) => el("option", { value: n, selected: n === state.limit }, fmtNum(n))));
  const name = (p) => p.name || `#${p.product_id}`;
  return [
    el("div", { class: "toolbar" },
      el("label", { class: "muted" }, t("rows"), " ", limitSelect)),
    el("div", { class: "grid-2" },
      card(t("byValue"), barTable(d.by_value, {
        name, value: (p) => p.value, format: (v) => fmtMoney(v),
        sub: (p) => `${fmtNum(p.quantity)} ${t("units")}`,
      })),
      card(t("byQuantity"), barTable(d.by_quantity, {
        name, value: (p) => p.quantity, format: fmtNum, sub: (p) => fmtMoney(p.value),
      }))),
    card(t("byCategory"), barTable(d.by_category, {
      name: (c) => (c.category_id == null ? t("uncategorised") : c.category),
      value: (c) => c.value, format: (v) => fmtMoney(v),
      sub: (c) => `${fmtNum(c.orders)} ${t("ordersUnit")}`,
    }), el("p", { class: "note" }, t("noteCategories"))),
    el("p", { class: "note" }, t("noteProductValue")),
  ];
}

/** One row per gateway, then its variants (the options customers picked,
 *  e.g. Tamara "split in 4") as indented rows in the store's own wording. */
function paymentsTable(lines) {
  const num = (v) => el("td", { class: "num" }, v);
  const rate = (x) => (x.successful + x.failed ? fmtPct((100 * x.successful) / (x.successful + x.failed)) : "—");
  const rows = [];
  for (const m of lines) {
    rows.push(el("tr", { class: "gateway-row" },
      el("td", {}, gatewayName(m),
         !m.enabled && !m.pos && el("small", { class: "muted tag" }, t("notEnabled"))),
      num(fmtNum(m.successful)), num(fmtNum(m.failed)),
      num(fmtPct(m.success_rate_pct)), num(fmtMoney(m.sales))));
    // Variants only add information when a gateway has more than one.
    if (m.variants.length > 1) {
      for (const v of m.variants) {
        rows.push(el("tr", { class: "variant-row" },
          el("td", { title: v.method }, v.title || v.method),
          num(fmtNum(v.successful)), num(fmtNum(v.failed)), num(rate(v)), num(fmtMoney(v.sales))));
      }
    }
  }
  return el("table", {},
    el("thead", {}, el("tr", {},
      el("th", { scope: "col" }, t("colMethod")),
      ...["colSuccessful", "colFailed", "colSuccessRate", "colSales"].map((k) =>
        el("th", { scope: "col", class: "num" }, t(k))))),
    el("tbody", {}, rows));
}

function payments(d) {
  const s = d.successful.value, f = d.failed.value;
  const cx = d.cancellations;
  return [
    el("div", { class: "kpis" },
      kpiTile(t("successfulPayments"), d.successful, fmtNum, { iconName: "check", badge: 1 }),
      kpiTile(t("failedPayments"), d.failed, fmtNum, { iconName: "cross", badge: 2, goodWhenUp: false }),
      kpiTile(t("successRate"), { value: s + f ? (100 * s) / (s + f) : null }, (v) => fmtPct(v),
        { iconName: "percent", badge: 3 }),
      kpiTile(t("cancellationRate"), { value: cx.rate_pct }, (v) => fmtPct(v),
        { iconName: "returns", badge: 4, sub: t("cancelledOf", fmtNum(cx.cancelled), fmtNum(cx.orders)) })),
    card(t("paymentMethods"), paymentsTable(d.by_method),
      el("p", { class: "note" }, t("notePayments"))),
    el("div", { class: "grid-main" },
      cancelChart(cx.over_time),
      card(t("cancelByMethod"), barTable(cx.by_method, {
        name: gatewayName, value: (m) => m.rate_pct, format: (v) => fmtPct(v),
        detail: (m) => (m.enabled || m.pos ? null : t("notEnabled")),
        sub: (m) => t("cancelledOf", fmtNum(m.cancelled), fmtNum(m.orders)),
      }))),
    el("p", { class: "note" }, t("noteCancellations")),
  ];
}


// ---------------------------------------------------------------------------
// Charts other than "sales over time" (one Chart.js chart per page)
// ---------------------------------------------------------------------------

/** Chart.js options shared by the smaller charts: same look as the sales
 *  chart (tooltip card, axis colours, Arabic right-to-left). */
function chartOptions({ yFormat, suggestedMax, points, tooltipLabel, legend = false }) {
  return {
    responsive: true, maintainAspectRatio: false, animation: false, locale: locale(),
    layout: { padding: { left: 12, right: 12 } },
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: legend },
      tooltip: {
        rtl: rtl(), textDirection: rtl() ? "rtl" : "ltr",
        backgroundColor: cssVar("--surface"), borderColor: cssVar("--border"), borderWidth: 1,
        titleColor: cssVar("--muted"), bodyColor: cssVar("--ink"),
        titleFont: { weight: "500" }, bodyFont: { weight: "600", size: 13 },
        padding: 12, cornerRadius: 12, caretSize: 0, boxPadding: 4,
        callbacks: { label: tooltipLabel },
      },
    },
    scales: {
      x: { reverse: rtl(), grid: { display: false }, offset: true,
           border: { color: cssVar("--axis") },
           ticks: { color: cssVar("--muted"), autoSkip: true, autoSkipPadding: 16,
                    maxTicksLimit: 8, maxRotation: 0 } },
      y: { position: rtl() ? "right" : "left", beginAtZero: true, suggestedMax,
           grid: { color: cssVar("--grid") }, border: { display: false },
           ticks: { color: cssVar("--muted"), maxTicksLimit: 5, precision: 0, callback: yFormat } },
    },
  };
}

/** A small colour key: "■ Packed  ■ Shipped". */
function legendKey(items) {
  return el("div", { class: "legend-key" }, items.map(([cls, label]) =>
    el("span", {}, el("i", { class: cls, "aria-hidden": "true" }), label)));
}

/** Warehouse: orders packed and orders shipped per day, side by side. */
function perDayChart(points) {
  const labels = points.map((p) => bucketLabel(`${p.day}T00:00:00`, false));
  const canvas = el("canvas", { role: "img", "aria-label": t("packedShippedPerDay") });
  const twin = table([
    { label: t("colDate"), get: (p) => bucketLabel(`${p.day}T00:00:00`, false) },
    { label: t("colPacked"), get: (p) => fmtNum(p.packed), num: true },
    { label: t("colShipped"), get: (p) => fmtNum(p.shipped), num: true },
  ], points);
  const node = card(t("packedShippedPerDay"),
    legendKey([["key-packed", t("colPacked")], ["key-shipped", t("colShipped")]]),
    el("div", { class: "chart-box" }, canvas),
    el("p", { class: "note" }, t("notePerDay")),
    tableToggle("per-day", twin));
  const any = points.some((p) => p.packed || p.shipped);
  pendingChart = () => { chart = new window.Chart(canvas, {
    type: "bar",
    data: { labels, datasets: [
      { label: t("colPacked"), data: points.map((p) => p.packed), backgroundColor: cssVar("--accent-dim"),
        borderRadius: 4, maxBarThickness: 18 },
      { label: t("colShipped"), data: points.map((p) => p.shipped), backgroundColor: cssVar("--accent"),
        borderRadius: 4, maxBarThickness: 18 },
    ] },
    options: chartOptions({
      yFormat: (v) => fmtNum(v), suggestedMax: any ? undefined : 5,
      tooltipLabel: (ctx) => `${ctx.dataset.label}: ${fmtNum(ctx.parsed.y)}`,
    }),
  }); };
  return node;
}

/** Payments: cancellation rate per day (or per week for long periods). */
function cancelChart(ot) {
  const weekly = ot.granularity === "week";
  const title = t(weekly ? "cancelRateWeekly" : "cancelRateDaily");
  const pts = ot.points;
  const label = (p) => (weekly ? t("weekOf", bucketLabel(p.bucket, false)) : bucketLabel(p.bucket, false));
  const twin = table([
    { label: t(weekly ? "colWeek" : "colDate"), get: label },
    { label: t("colOrders"), get: (p) => fmtNum(p.orders), num: true },
    { label: t("colCancelled"), get: (p) => fmtNum(p.cancelled), num: true },
    { label: t("colCancelRate"), get: (p) => fmtPct(p.rate_pct), num: true },
  ], pts);
  if (pts.length < 2) {
    // One day: a single dot says nothing a number doesn't.
    return card(title, el("p", { class: "empty" }, t("chartNeedsDays")), tableToggle("cancel-rate", twin));
  }
  const canvas = el("canvas", { role: "img", "aria-label": title });
  const node = card(title, el("div", { class: "chart-box" }, canvas), tableToggle("cancel-rate", twin));
  const line = cssVar("--accent");
  pendingChart = () => { chart = new window.Chart(canvas, {
    type: "line",
    data: { labels: pts.map(label), datasets: [{
      data: pts.map((p) => p.rate_pct), spanGaps: true,
      borderColor: line, backgroundColor: cssVar("--accent-wash"), fill: "origin",
      borderWidth: 2.5, pointRadius: pts.length <= 31 ? 3 : 0, pointBackgroundColor: line,
      pointHoverRadius: 6, cubicInterpolationMode: "monotone",
    }] },
    options: chartOptions({
      yFormat: (v) => fmtPct(v), suggestedMax: pts.some((p) => p.rate_pct > 0) ? undefined : 10,
      tooltipLabel: (ctx) => {
        const p = pts[ctx.dataIndex];
        return `${fmtPct(p.rate_pct)} · ${t("cancelledOf", fmtNum(p.cancelled), fmtNum(p.orders))}`;
      },
    }),
  }); };
  return node;
}

// ---------------------------------------------------------------------------
// Sales by weekday and hour (heat map)
// ---------------------------------------------------------------------------

/** Weekday name; 0 = Sunday. 16 Aug 2026 is a Sunday. */
const weekdayName = (i, style = "short") =>
  new Intl.DateTimeFormat(locale(), { weekday: style, timeZone: "UTC" }).format(new Date(Date.UTC(2026, 7, 16 + i)));
const hh = (h) => `${String(h).padStart(2, "0")}:00`;

/** Sale orders by Riyadh weekday (rows, Sunday first like the Saudi week)
 *  and hour (columns). Darker = more orders. Every cell's number is in its
 *  tooltip and in the table underneath, so nothing depends on the colour. */
function heatmapCard(cells) {
  const grid = Array.from({ length: 7 }, () => Array(24).fill(null));
  for (const c of cells) grid[c.weekday][c.hour] = c;
  const max = Math.max(0, ...cells.map((c) => c.orders)) || 1;
  const byDay = grid.map((hours) => hours.reduce((a, c) => a + (c?.orders || 0), 0));
  const byHour = Array.from({ length: 24 }, (_, h) => grid.reduce((a, row) => a + (row[h]?.orders || 0), 0));
  const salesDay = grid.map((hours) => hours.reduce((a, c) => a + (c?.sales || 0), 0));
  const salesHour = Array.from({ length: 24 }, (_, h) => grid.reduce((a, row) => a + (row[h]?.sales || 0), 0));
  const title = t("whenCustomersBuy");
  if (!cells.length) return card(title, el("p", { class: "empty" }, t("nothingHere")));

  const shade = (n) => (n ? `color-mix(in srgb, var(--accent) ${Math.round(15 + 85 * (n / max))}%, var(--surface-2))` : null);
  const head = el("div", { class: "hm-row hm-head", "aria-hidden": "true" }, el("span"),
    Array.from({ length: 24 }, (_, h) => el("span", { class: "hm-hour" }, h % 3 === 0 ? String(h).padStart(2, "0") : "")));
  const rows = grid.map((hours, d) => el("div", { class: "hm-row" },
    el("span", { class: "hm-day" }, weekdayName(d)),
    hours.map((c, h) => {
      const n = c?.orders || 0;
      return el("span", {
        class: "hm-cell", style: shade(n) ? { background: shade(n) } : {},
        title: `${weekdayName(d, "long")} ${hh(h)} · ${fmtNum(n)} ${t("ordersUnit")}${c ? ` · ${fmtMoney(c.sales)}` : ""}`,
      });
    })));
  const topDay = byDay.indexOf(Math.max(...byDay));
  const topHour = byHour.indexOf(Math.max(...byHour));

  const twin = el("div", { class: "hm-tables" },
    table([
      { label: t("colWeekday"), get: (i) => weekdayName(i, "long") },
      { label: t("colOrders"), get: (i) => fmtNum(byDay[i]), num: true },
      { label: t("colSales"), get: (i) => fmtMoney(salesDay[i]), num: true },
    ], [0, 1, 2, 3, 4, 5, 6]),
    table([
      { label: t("colHour"), get: (h) => `${hh(h)}–${hh((h + 1) % 24)}` },
      { label: t("colOrders"), get: (h) => fmtNum(byHour[h]), num: true },
      { label: t("colSales"), get: (h) => fmtMoney(salesHour[h]), num: true },
    ], Array.from({ length: 24 }, (_, h) => h)));

  return card(title,
    el("p", { class: "hm-summary" }, t("busiest", weekdayName(topDay, "long"), `${hh(topHour)}–${hh((topHour + 1) % 24)}`)),
    el("div", { class: "hm-scroll" }, el("div", { class: "hm", role: "img", "aria-label": title }, head, rows)),
    el("div", { class: "hm-legend", "aria-hidden": "true" }, t("fewer"), el("i"), t("more")),
    el("p", { class: "note" }, t("noteHeatmap")),
    tableToggle("heatmap", twin));
}

// ---------------------------------------------------------------------------
// Warehouse (from the storage app's records; app/warehouse.py)
// ---------------------------------------------------------------------------

/** 0.3 h → "18 min", 11.5 h → "11.5 h", 106 h → "4.4 days". */
function fmtHours(h) {
  if (h == null) return "—";
  if (h < 1) return t("minutesShort", fmtNum(Math.round(h * 60)));
  if (h < 48) return t("hoursShort", fmtNum(h, 1));
  return t("daysShort", fmtNum(h / 24, 1));
}

/** A link into the storage app, keeping the chosen language. */
function storageLink(hash, label) {
  return el("a", { class: "go-link", href: `/storage${hash}`,
                   onclick: () => { try { localStorage.setItem("lang", state.lang); } catch { /* blocked */ } } },
    label, rtl() ? " ←" : " →");
}

/** Shipped today ÷ ready today (2026-09-30): "42 of 50 ready orders
 *  shipped today (84%)", then what is still waiting and since when. */
function readyCard(td) {
  const head = el("div", { class: "card-head" }, el("h2", {}, t("shippedToday")),
    el("span", { class: "now-chip" }, t("rightNow")));
  if (!td.ready) {
    return el("section", { class: "card ready-card" }, head,
      el("p", { class: "empty" }, t("nothingReady")), storageLink("#/website/loading", t("openTruck")));
  }
  const pct = td.rate_pct ?? 0;
  return el("section", { class: "card ready-card" }, head,
    el("div", { class: "ready-big" },
      el("strong", {}, fmtNum(td.shipped)),
      el("span", {}, t("ofReadyShipped", fmtNum(td.ready))),
      el("span", { class: "ready-pct" }, fmtPct(td.rate_pct))),
    el("div", { class: "ready-track", role: "img", "aria-label": fmtPct(td.rate_pct) },
      el("div", { class: "ready-bar", style: { width: `${pct}%` } })),
    td.waiting
      ? el("p", { class: "ready-left" },
          t("stillWaiting", fmtNum(td.waiting), fmtNum(td.waiting_today), fmtNum(td.waiting_older)),
          td.waiting_older ? t("oldestWaiting", t("daysCount", td.oldest_days)) : null)
      : el("p", { class: "ready-left" }, t("allShipped")),
    td.forgotten ? el("p", { class: "warn-line" }, t("forgottenLine", fmtNum(td.forgotten), td.forgotten_hours)) : null,
    td.partly_loaded ? el("p", { class: "muted small" }, t("partlyLoaded", fmtNum(td.partly_loaded))) : null,
    storageLink("#/website/loading", t("openTruck")));
}

/** Orders waiting to be PACKED, by how long ago they were placed. */
function backlogCard(b) {
  const rows = [
    { key: "backlogToday", n: b.today },
    { key: "backlogOneTwo", n: b.one_two },
    { key: "backlogThreePlus", n: b.three_plus, late: true },
  ];
  return el("section", { class: "card" },
    el("div", { class: "card-head" }, el("h2", {}, t("waitingToPack")),
      el("span", { class: "now-chip" }, t("rightNow"))),
    el("div", { class: "headline" }, el("span", { class: "big" }, fmtNum(b.total)),
      el("span", { class: "muted" }, t("ordersUnit"))),
    b.total ? barTable(rows, {
      name: (r) => t(r.key), value: (r) => r.n, format: fmtNum,
      detail: (r) => (r.late && r.n ? "⚠" : null),
    }) : el("p", { class: "empty" }, t("backlogEmpty")),
    b.oldest_days ? el("p", { class: "note" }, t("oldestPlaced", t("daysCount", b.oldest_days))) : null,
    storageLink("#/website", t("openToPack")));
}

function carriersCard(rows, otoSynced) {
  const name = (r) => r.carrier || t("carrierUnknown");
  const body = rows.length ? el("div", { class: "table-wide" }, table([
    { label: t("colCarrier"), get: name },
    { label: t("colOrders"), get: (r) => fmtNum(r.orders), num: true },
    { label: t("colDelivered"), get: (r) => fmtNum(r.delivered), num: true },
    { label: t("colDeliveryTime"), get: (r) => fmtHours(r.avg_delivery_hours), num: true },
  ], rows)) : el("p", { class: "empty" }, t("noOtoShipments"));
  return card(t("carriers"), body,
    el("p", { class: "note" }, t("noteCarriers"), " ",
      otoSynced ? t("otoRead", agoText(otoSynced)) : t("otoNever")));
}

function warehouse(d) {
  const tt = d.truck_time, sh = d.shipping;
  // Tile subtitles for the OTO figures: never read, nothing in the period,
  // or what the number is based on.
  const otoEmpty = d.oto_synced_utc ? t("noOtoData") : t("fromOto");
  return [
    el("div", { class: "grid-main" }, readyCard(d.today), backlogCard(d.backlog)),
    el("div", { class: "kpis" },
      kpiTile(t("ordersShipped"), { value: tt.orders }, fmtNum,
        { iconName: "warehouse", badge: 1, sub: t("loadedInPeriod") }),
      kpiTile(t("packedToTruck"), { value: tt.median_hours }, fmtHours,
        { iconName: "clock", badge: 2,
          sub: tt.orders ? t("truckSub", fmtHours(tt.avg_hours), fmtNum(tt.over_limit), tt.limit_hours) : null }),
      kpiTile(t("deliveryTime"), { value: sh.avg_delivery_hours }, fmtHours,
        { iconName: "box", badge: 3, sub: sh.timed ? t("deliveredOrders", fmtNum(sh.timed)) : otoEmpty })),
    perDayChart(d.per_day),
    carriersCard(d.carriers, d.oto_synced_utc),
    el("p", { class: "note" }, t("noteWarehouse")),
  ];
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
function init() {
  if (window.Chart) {
    window.Chart.defaults.font.family = getComputedStyle(document.documentElement).fontFamily;
    // Redraw once the bundled font has loaded, so the chart doesn't keep the
    // fallback font it measured with on first paint.
    document.fonts?.ready.then(() => chart && chart.update());
    // The chart is a canvas, and text drawn on a canvas never triggers a
    // font download by itself. Ask for the riyal font explicitly, so the
    // axis and tooltip show the sign rather than an empty box.
    Promise.all(["400", "700"].map((w) => document.fonts?.load(`${w} 16px "Saudi Riyal"`, RIYAL)))
      .then(() => chart && chart.update()).catch(() => { /* keep the fallback */ });
  }

  document.getElementById("lang-toggle").addEventListener("click", () => {
    state.lang = rtl() ? "en" : "ar";
    try { localStorage.setItem("lang", state.lang); } catch { /* storage blocked */ }
    applyStatic();
    writeState();
    draw();                       // same data, new language: no refetch needed
  });

  document.getElementById("custom-range").addEventListener("submit", (e) => {
    e.preventDefault();
    state.start = document.getElementById("custom-start").value;
    state.end = document.getElementById("custom-end").value;
    state.ordersPage = 1;
    refresh({ userAction: true });
  });

  // Live refresh: quietly, and only while the page is actually visible.
  setInterval(() => { if (!document.hidden) refresh(); }, REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

  // Light by default; dark only when the viewer asks for it. The choice is
  // remembered in this browser, and the chart re-reads the colours.
  document.getElementById("theme-toggle").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme !== "dark";
    if (dark) document.documentElement.dataset.theme = "dark";
    else delete document.documentElement.dataset.theme;
    try { localStorage.setItem("theme", dark ? "dark" : "light"); } catch { /* storage blocked */ }
    applyStatic();
    draw();
  });

  // The back/forward buttons and edited links change the hash.
  addEventListener("hashchange", () => {
    Object.assign(state, readState());
    applyStatic();
    refresh({ userAction: true });
  });

  applyStatic();
  if (state.period === "custom" && !(state.start && state.end)) return;
  refresh({ userAction: true });
}

init();
