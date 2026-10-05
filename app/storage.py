"""The storage app's API: pick & pack for the storage room workers.

    /storage                                   the app itself (web/storage.html)
    /api/storage/website/summary               the two tiles: pieces and orders waiting
    /api/storage/website/items                 the pick list (printed and walked)
    /api/storage/website/orders                waiting orders, split 1 / 2 / 3+ pieces
    /api/storage/website/orders/{id}           one order, for scanning
    /api/storage/website/orders/{id}/approve   POST: scanned + boxed -> packed
    /api/storage/website/orders/{id}/packing   DELETE: undo a mistaken approve
    /api/storage/website/packed                packed orders not yet loaded, newest first;
                                               ?from&to = packing days (default: this month so far)
    /api/storage/website/loading               every packed order not yet fully loaded (truck screen)
    /api/storage/website/load                  POST {code}: one box scanned onto the truck
    /api/storage/website/orders/{id}/loading   DELETE: undo loading
    /api/storage/website/shipping              orders being shipped; ?from&to = loading days
    /api/storage/website/workers               names used before (suggestions)
    /api/storage/website/pickers               PUT ?from&to: who collects that pick list
    /api/storage/website/orders/assign         PUT: who packs which orders
    /api/storage/website/labels/{code}         which item a unique piece label is
    /api/storage/website/oto/sync              POST ?from&to: fetch BOL numbers from OTO (TEST mode)
    /api/storage/website/orders/{id}/test-labels  POST: TEST labels, one per piece

"Website" is the demo WooCommerce store. The app's start page also offers
Trendyol, which has no data source yet; it lives under its own prefix later.

What "waiting" means (2026-09-28):
  * status `processing` (paid, not yet shipped) — WAITING_STATUSES below;
  * not already approved here (no row in packed_orders);
  * the E2E test orders are left out, like everywhere else.

DATES (2026-09-28): the tiles, pick list and order tables show the orders
PLACED between two dates, From and To, both included in full, with days
running from midnight to midnight Riyadh time: `?from=2026-09-27&to=2026-09-28`
is 27 Sep 00:00 up to (not including) 29 Sep 00:00. With no dates it is
YESTERDAY, a complete day whose list no longer grows. (This replaced an
earlier 06:30-to-06:30 "batch" day.)

Orders placed before From that are still waiting are not in the lists, but
they are counted (`older_waiting`) and can be listed with
`/orders?older=true`, so nothing is forgotten silently. Opening, scanning and
approving works for any waiting order, whatever its date.

Sizes count PIECES, not lines (2026-09-28): one line with quantity 2 is a
"two" order. Every sizing below is sum(quantity).

Nothing here writes to WooCommerce. Approving records the packing in our own
packed_orders table (db/03_storage.sql); the order stays `processing` in the
store until the store moves it on.
"""

import json
import secrets
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.db import connect
from app.metrics import NOT_EXCLUDED, cursor
from app.config import REPORT_TIMEZONE, OTO_REFRESH_TOKEN
from app import oto
from app.shelf_categories import shelf_category

router = APIRouter(prefix="/api/storage/website", tags=["storage"])

# Orders the storage room still has to pack. A tuple so a second status
# (for example a custom "ready to pack") is a one-word change.
WAITING_STATUSES = ("processing",)

# Waiting orders: the right status, not packed yet, not a test order.
# `o` is orders. NOT_EXCLUDED contains a doubled %% for psycopg, so every
# query using this must be run WITH a params dict (even an unused one),
# otherwise psycopg leaves the %% as it is.
WAITING = """
    FROM orders o
    WHERE o.status = ANY(%(waiting)s)
      AND NOT EXISTS (SELECT 1 FROM packed_orders p WHERE p.order_id = o.id)
""" + NOT_EXCLUDED

LOCAL_TZ = ZoneInfo(REPORT_TIMEZONE)

# The longest range the page may ask for, to keep a stray date from asking
# for years of orders.
MAX_RANGE_DAYS = 366

# Which orders fall in the chosen dates, and which are older and still
# waiting. `start` / `end` come from resolve_range(); end is exclusive.
IN_RANGE = """
      AND o.date_created_gmt >= %(start)s AND o.date_created_gmt < %(end)s
"""
BEFORE_RANGE = """
      AND o.date_created_gmt < %(start)s
"""

# The three order tables. The key is what the API and the frontend use.
SIZES = ("one", "two", "three_plus")


def _params(**extra) -> dict:
    return {"waiting": list(WAITING_STATUSES), "tz": REPORT_TIMEZONE, **extra}


def clock() -> datetime:
    """The current time. A function so the tests can freeze it."""
    return datetime.now(timezone.utc)


def today_local(now: datetime | None = None) -> date:
    """Today's date in Riyadh."""
    return (now or clock()).astimezone(LOCAL_TZ).date()


def local_midnight(day: date) -> datetime:
    """00:00 Riyadh time on that day, in UTC."""
    return datetime.combine(day, time(0, 0), tzinfo=LOCAL_TZ).astimezone(timezone.utc)


def month_bounds(today: date) -> tuple[date, date]:
    """(1st, last day) of today's month."""
    first = today.replace(day=1)
    next_first = (first + timedelta(days=32)).replace(day=1)
    return first, next_first - timedelta(days=1)


def resolve_range(date_from: date | None, date_to: date | None) -> dict:
    """The dates asked for, as a UTC [start, end) window: From 00:00 up to
    the day after To at 00:00 (To is included in full). No dates: the whole
    current month, 1st to last day (2026-10-01; it was yesterday before).
    One date alone: that single day. Refused (400): To before From, a date
    after the end of this month, or more than MAX_RANGE_DAYS days. Days
    still to come simply hold no orders yet."""
    today = today_local()
    month_first, month_last = month_bounds(today)
    if date_from is None and date_to is None:
        date_from, date_to = month_first, month_last
    date_from = date_from or date_to
    date_to = date_to or date_from
    if date_to < date_from:
        raise HTTPException(400, "The To date is before the From date.")
    if date_to > month_last:
        raise HTTPException(400, f"The To date is after the end of this month ({month_last}).")
    if (date_to - date_from).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(400, f"At most {MAX_RANGE_DAYS} days at a time.")
    return {"from": date_from, "to": date_to, "today": today,
            "month_first": month_first, "month_last": month_last,
            "start": local_midnight(date_from),
            "end": local_midnight(date_to + timedelta(days=1))}


def range_info(cur, r: dict) -> dict:
    """What the page shows about the dates: From and To, the window in
    Riyadh time (ending 23:59, the way people say it), whether it is the
    default (this month), and how many OLDER orders are still waiting."""
    older = cur.execute(f"SELECT count(*) AS n {WAITING} {BEFORE_RANGE}",
                        _params(start=r["start"])).fetchone()["n"]
    local = lambda dt: dt.astimezone(LOCAL_TZ).replace(tzinfo=None).isoformat(timespec="minutes")
    return {
        "from": r["from"].isoformat(),
        "to": r["to"].isoformat(),
        "today": r["today"].isoformat(),
        "is_default": r["from"] == r["month_first"] and r["to"] == r["month_last"],
        "max": r["month_last"].isoformat(),     # the latest day the pickers offer
        "start_local": local(r["start"]),
        "last_local": local(r["end"] - timedelta(minutes=1)),
        "older_waiting": older,
    }


def size_of(pieces: int) -> str:
    """Which table an order goes in, by its number of pieces."""
    if pieces <= 1:
        return "one"
    return "two" if pieces == 2 else "three_plus"


def normalise(code: str | None) -> str:
    """How a scanned barcode and a stored SKU are compared: spaces trimmed
    and case ignored. Scanners sometimes add a trailing space or newline,
    and "ab12" vs "AB12" is not a real difference."""
    return (code or "").strip().casefold()


# --- Workers -------------------------------------------------------------------
# Who collects the pick list and who packs each order (2026-09-28):
# "Order 123 packed by Sam, Ahmed". Free-text names; db/05_workers.sql.

MAX_WORKERS = 10          # names on one assignment
MAX_NAME = 40             # characters in one name


def clean_workers(names: list[str]) -> list[str]:
    """Names as they will be stored: spaces trimmed and collapsed, empty ones
    dropped, duplicates dropped (case ignored, first spelling kept), in the
    order given. Too many or too long -> 422, rather than silently cutting."""
    out, seen = [], set()
    for raw in names:
        name = " ".join((raw or "").split())
        if not name:
            continue
        if len(name) > MAX_NAME:
            raise HTTPException(422, f"Name too long (max {MAX_NAME} characters): {name[:MAX_NAME]}…")
        if name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    if len(out) > MAX_WORKERS:
        raise HTTPException(422, f"At most {MAX_WORKERS} names.")
    return out


def order_workers(cur, order_ids: list[int]) -> dict[int, list[str]]:
    """Assigned packers per order (only orders that have some)."""
    rows = cur.execute("SELECT order_id, workers FROM order_workers WHERE order_id = ANY(%s)",
                       (order_ids,)).fetchall()
    return {r["order_id"]: list(r["workers"]) for r in rows}


def known_workers(cur) -> list[str]:
    """Every name used so far, for the name suggestions: assignments and
    packed orders, most recently used first, one spelling per name."""
    rows = cur.execute("""
        SELECT name, max(at) AS last_used FROM (
            SELECT unnest(workers) AS name, assigned_at AS at FROM pick_workers
            UNION ALL SELECT unnest(workers), assigned_at FROM order_workers
            UNION ALL SELECT unnest(packed_by), packed_at FROM packed_orders
        ) n
        GROUP BY name
        ORDER BY last_used DESC, name
    """).fetchall()
    out, seen = [], set()
    for r in rows:
        if r["name"].casefold() not in seen:
            seen.add(r["name"].casefold())
            out.append(r["name"])
    return out


# --- Queries ------------------------------------------------------------------

def summary(cur, b: dict) -> dict:
    """The two tiles for the chosen dates, plus how the orders split and
    what was packed today."""
    rows = cur.execute(f"""
        WITH waiting AS (SELECT o.id {WAITING} {IN_RANGE})
        SELECT w.id, COALESCE(sum(i.quantity), 0) AS pieces
        FROM waiting w
        LEFT JOIN order_items i ON i.order_id = w.id AND i.quantity > 0
        GROUP BY w.id
    """, _params(start=b["start"], end=b["end"])).fetchall()
    # Orders with no pieces at all (should not exist; the backfill checks for
    # it) are left out rather than shown as an order nobody can pack.
    rows = [r for r in rows if r["pieces"] > 0]
    by_size = Counter(size_of(r["pieces"]) for r in rows)
    # Today by the app's clock (the same clock Approve stamps packed_at with).
    # Only orders still on the Packed orders list: once an order's last box
    # is loaded onto the truck it moves to Orders being shipped and no longer
    # counts here (2026-10-01), so the button's badge matches its list.
    today = today_local()
    packed_today = cur.execute("""
        SELECT count(*) AS n FROM packed_orders
        WHERE packed_at >= %(start)s AND packed_at < %(end)s
          AND shipped_at IS NULL
    """, {"start": local_midnight(today),
          "end": local_midnight(today + timedelta(days=1))}).fetchone()["n"]
    return {
        "orders": len(rows),
        "pieces": sum(r["pieces"] for r in rows),
        "by_size": {s: by_size.get(s, 0) for s in SIZES},
        "packed_today": packed_today,
        "waiting_statuses": list(WAITING_STATUSES),
    }


def pick_list(cur, b: dict) -> list[dict]:
    """Every piece the waiting orders of the chosen dates need, per item.

    One row per VARIATION (size/colour), not per parent product: a worker
    fetching "Duvet" needs to know which duvet. The item number is the SKU.
    Categories belong to the parent product (that is how WooCommerce stores
    them). The store files each product under many categories, mostly
    marketing ones, so `category` is the ONE product-type category picked by
    app/shelf_categories.py (2026-09-28); `all_categories` keeps the full
    list (the page shows it on hover).

    Sorted by category, then name, so the printed list follows the shelves
    roughly instead of jumping around."""
    rows = cur.execute(f"""
        WITH waiting AS (SELECT o.id {WAITING} {IN_RANGE})
        SELECT i.product_id, i.variation_id,
               -- The name and SKU of one line stand for the whole group
               -- (they are the same item). max() just picks one.
               max(i.sku)  AS sku,
               max(i.name) AS name,
               sum(i.quantity)            AS quantity,
               count(DISTINCT i.order_id) AS orders,
               -- One line of the group, to read its colour/size from (every
               -- line of a variation carries the same ones).
               (array_agg(i.order_id     ORDER BY i.order_id, i.line_item_id))[1] AS any_order,
               (array_agg(i.line_item_id ORDER BY i.order_id, i.line_item_id))[1] AS any_line,
               -- Every category of the product; Python picks the one to show.
               (SELECT array_agg(DISTINCT c.category_name ORDER BY c.category_name)
                  FROM product_categories c
                 WHERE c.product_id = i.product_id) AS all_categories
        FROM order_items i
        JOIN waiting w ON w.id = i.order_id
        WHERE i.quantity > 0
        GROUP BY i.product_id, i.variation_id
    """, _params(start=b["start"], end=b["end"])).fetchall()
    options = line_options(cur, list({r["any_order"] for r in rows}))
    out = []
    for r in rows:
        r = dict(r)
        r["options"] = options.get((r.pop("any_order"), r.pop("any_line")), [])
        r["all_categories"] = list(r["all_categories"] or [])
        r["category"] = shelf_category(r["all_categories"])
        out.append(r)
    # Sorted here, not in SQL, because the shown category is picked above:
    # by category (uncategorised last), then name, then variation.
    out.sort(key=lambda x: (x["category"] is None, x["category"] or "",
                            x["name"] or "", x["variation_id"]))
    return out


# --- Colour, size and other options of an order line ---------------------------
# A product that comes in variations (a robe in several colours and sizes)
# stores the customer's choice on each order line, in the line's meta_data,
# which our import keeps inside orders.raw. On this store (a check,
# 2026-09-28) they look like:
#     key "pa_color-ar"         display_key "اللون"        display_value "ازرق"
#     key "pa_size-ar"          display_key "المقاس"       display_value "50 × 90 سم"
#     key "pa_choose-the-size"  display_key "اختر المقاس"  display_value "10 - 12"
# Plugins add their own entries too (discount details, "_reduced_stock");
# those keys start with "_", and are skipped. Reading raw means no new import
# and no call to the store.

MAX_OPTION_LEN = 60       # longer "values" are plugin data, not a colour


def option_kind(key: str, label: str) -> str:
    """'color', 'size' or 'other', so the page can use its own words and
    columns for the two that matter in the storage room."""
    k, lab = (key or "").lower(), label or ""
    if "color" in k or "colour" in k or "لون" in lab:
        return "color"
    if "size" in k or "مقاس" in lab:
        return "size"
    return "other"


def line_options(cur, order_ids: list[int]) -> dict[tuple[int, int], list[dict]]:
    """{(order_id, line_item_id): [{kind, label, value}, ...]} for the
    lines of these orders that carry options, in the store's order."""
    rows = cur.execute("""
        SELECT o.id AS order_id, (li->>'id')::bigint AS line_item_id, li->'meta_data' AS meta
        FROM orders o,
             jsonb_array_elements(COALESCE(o.raw->'line_items', '[]'::jsonb)) li
        WHERE o.id = ANY(%(ids)s)
    """, {"ids": order_ids}).fetchall()
    out = {}
    for r in rows:
        opts = []
        for m in r["meta"] or []:
            key = str(m.get("key") or "")
            value = m.get("display_value", m.get("value"))
            if key.startswith("_") or not isinstance(value, str):
                continue
            value = " ".join(value.split())
            if not value or len(value) > MAX_OPTION_LEN:
                continue
            label = str(m.get("display_key") or key)
            opts.append({"kind": option_kind(key, label), "label": label, "value": value})
        if opts:
            out[(r["order_id"], r["line_item_id"])] = opts
    return out


def _lines(cur, order_ids: list[int]) -> dict[int, list]:
    """Product lines for these orders, in the order the customer added them."""
    rows = cur.execute("""
        SELECT i.order_id, i.line_item_id, i.product_id, i.variation_id,
               i.name, i.sku, i.quantity
        FROM order_items i
        WHERE i.order_id = ANY(%(ids)s) AND i.quantity > 0
        ORDER BY i.order_id, i.line_item_id
    """, {"ids": order_ids}).fetchall()
    options = line_options(cur, order_ids)
    out: dict[int, list] = {}
    for r in rows:
        out.setdefault(r["order_id"], []).append({
            "line_item_id": r["line_item_id"], "product_id": r["product_id"],
            "variation_id": r["variation_id"], "name": r["name"],
            # An empty SKU is sent as null: the frontend then offers
            # "confirm by hand" for that line, since there is nothing to scan.
            "sku": (r["sku"] or "").strip() or None,
            "quantity": r["quantity"],
            # Colour, size, ... chosen by the customer (empty for plain products).
            "options": options.get((r["order_id"], r["line_item_id"]), []),
        })
    return out


def order_bols(cur, order_ids: list[int]) -> dict[int, dict]:
    """{order_id: {bol_no, boxes, carrier, tracking_url}} from the OTO sync
    (db/07_order_bol.sql), for the orders that have a row."""
    if not order_ids:
        return {}
    rows = cur.execute("""SELECT order_id, bol_no, boxes, carrier, tracking_url
                          FROM order_bol WHERE order_id = ANY(%s)""", (order_ids,)).fetchall()
    return {r["order_id"]: {k: r[k] for k in ("bol_no", "boxes", "carrier", "tracking_url")}
            for r in rows}


def waiting_orders(cur, b: dict, older: bool = False) -> dict:
    """The waiting orders of the chosen dates in their three tables, oldest
    first in each. With older=True: instead the orders placed BEFORE From
    that are still waiting (the ones the warning banner counts)."""
    scope = BEFORE_RANGE if older else IN_RANGE
    orders = cur.execute(f"""
        SELECT o.id, COALESCE(o.number, o.id::text) AS number,
               o.date_created_gmt AT TIME ZONE %(tz)s AS created_local
        {WAITING} {scope}
        ORDER BY o.date_created_gmt, o.id
    """, _params(start=b["start"], end=b["end"])).fetchall()
    lines = _lines(cur, [o["id"] for o in orders])
    workers = order_workers(cur, [o["id"] for o in orders])
    bols = order_bols(cur, [o["id"] for o in orders])
    tables = {s: [] for s in SIZES}
    for o in orders:
        items = lines.get(o["id"], [])
        pieces = sum(x["quantity"] for x in items)
        if pieces == 0:
            continue                      # nothing to pack; see summary()
        tables[size_of(pieces)].append({
            "id": o["id"], "number": o["number"],
            "created_local": o["created_local"].isoformat(timespec="seconds"),
            "pieces": pieces, "items": items,
            # From the OTO sync; None = not synced yet, or no shipment yet.
            "bol_no": bols.get(o["id"], {}).get("bol_no"),
            "carrier": bols.get(o["id"], {}).get("carrier"),
            "tracking_url": bols.get(o["id"], {}).get("tracking_url"),
            "workers": workers.get(o["id"], []),
        })
    return tables


def order_detail(cur, order_id: int) -> dict:
    """One order for the scanning screen, with whether it can still be packed."""
    o = cur.execute("""
        SELECT o.id, COALESCE(o.number, o.id::text) AS number, o.status,
               o.date_created_gmt AT TIME ZONE %(tz)s AS created_local,
               p.boxes, p.packed_at, COALESCE(p.bol_no, b.bol_no) AS bol_no, p.packed_by,
               b.boxes AS oto_boxes, b.carrier, b.tracking_url, p.shipped_at,
               (SELECT count(*) FROM box_loads l WHERE l.order_id = o.id) AS loaded,
               w.workers
        FROM orders o
        LEFT JOIN packed_orders p ON p.order_id = o.id
        LEFT JOIN order_workers w ON w.order_id = o.id
        LEFT JOIN order_bol b ON b.order_id = o.id
        WHERE o.id = %(id)s
    """, _params(id=order_id)).fetchone()
    if o is None:
        raise HTTPException(404, f"Order {order_id} is not in the database.")
    items = _lines(cur, [order_id]).get(order_id, [])
    # The unique piece labels packed into it (db/06_unit_labels.sql).
    labels = [] if o["packed_at"] is None else [dict(r) for r in cur.execute(
        "SELECT barcode, sku FROM unit_labels WHERE packed_order_id = %s ORDER BY sku, barcode",
        (order_id,)).fetchall()]
    if o["packed_at"] is not None:
        state = "packed"
    elif o["status"] in WAITING_STATUSES:
        state = "waiting"
    else:
        state = "not_waiting"             # shipped, cancelled, ... meanwhile
    return {
        "id": o["id"], "number": o["number"], "status": o["status"], "state": state,
        "created_local": o["created_local"].isoformat(timespec="seconds"),
        "pieces": sum(x["quantity"] for x in items),
        "items": items,
        # Who is assigned to pack it (changeable until it is packed).
        "workers": list(o["workers"] or []),
        "bol_no": o["bol_no"],                  # from the OTO sync; None = not yet
        # What OTO says (OTO sync): the box count the scan screen checks against.
        "oto": {"boxes": o["oto_boxes"], "carrier": o["carrier"], "tracking_url": o["tracking_url"]},
        "packing": None if state != "packed" else {
            "boxes": o["boxes"], "bol_no": o["bol_no"],
            "packed_by": list(o["packed_by"] or []),
            "packed_at_utc": o["packed_at"].isoformat(timespec="seconds"),
            "labels": labels,
            # Loading onto the truck (db/08_shipping.sql): boxes scanned so
            # far, and when the last one was (None = not all loaded yet).
            "loaded": o["loaded"],
            "shipped_at_utc": o["shipped_at"].isoformat(timespec="seconds") if o["shipped_at"] else None,
        },
    }


def find_labels(cur, codes: list[str]) -> dict[str, dict]:
    """The unique piece labels among these scanned codes, keyed by the
    normalised code: {code: {barcode, sku, packed_order_id, packed_number}}.
    Codes that are not labels (e.g. plain item numbers) are simply absent."""
    wanted = sorted({normalise(c) for c in codes if normalise(c)})
    if not wanted:
        return {}
    rows = cur.execute("""
        SELECT l.barcode, l.sku, l.packed_order_id,
               COALESCE(o.number, o.id::text) AS packed_number
        FROM unit_labels l
        LEFT JOIN orders o ON o.id = l.packed_order_id
        WHERE lower(l.barcode) = ANY(%(codes)s)
    """, {"codes": wanted}).fetchall()
    return {normalise(r["barcode"]): dict(r) for r in rows}


def check_scans(items: list[dict], scans: list[str], manual: list[int],
                labels: dict[str, dict] | None = None) -> list[str]:
    """Do the scanned barcodes cover the order exactly? Returns the problems,
    empty when the order may be approved. This is the SAME rule the scanning
    screen applies; the server checks it again so a stale page, a double
    click or a hand-made request cannot approve an order that was not
    fully scanned.

      * Every piece of a line with a SKU is scanned once (quantity 2 = two
        scans). Lines sharing a SKU are pooled, so it does not matter which
        of them a scan is credited to.
      * A line without a SKU cannot be scanned; it must be confirmed by hand
        (its id in `manual`). A line WITH a SKU cannot be confirmed by hand.
      * No barcode that is not in the order, and no extra scans.
      * A scan may also be a unique PIECE label (2026-09-29; `labels` from
        find_labels): it counts for its label's item. Each label counts
        once, and a label already packed into another order is refused."""
    problems = []
    labels = labels or {}
    needed = Counter()
    for x in items:
        if x["sku"]:
            needed[normalise(x["sku"])] += x["quantity"]
    got = Counter()
    label_scans = Counter()
    for raw in scans:
        code = normalise(raw)
        if not code:
            continue
        label = labels.get(code)
        if label is None:
            got[code] += 1                    # an item-number scan, as before
            continue
        label_scans[code] += 1
        if label_scans[code] == 2:            # reported once, however often
            problems.append(f"label {label['barcode']!r} was scanned more than once")
        if label["packed_order_id"] is not None:
            problems.append(f"label {label['barcode']!r} is already packed "
                            f"in order #{label['packed_number']}")
        got[normalise(label["sku"])] += 1     # counts for its item

    for code in sorted(set(needed) | set(got)):
        if code not in needed:
            problems.append(f"barcode {code!r} is not in this order ({got[code]} scan(s))")
        elif got[code] < needed[code]:
            problems.append(f"barcode {code!r}: {got[code]} of {needed[code]} scanned")
        elif got[code] > needed[code]:
            problems.append(f"barcode {code!r}: scanned {got[code]} times, needs {needed[code]}")

    no_sku = {x["line_item_id"] for x in items if not x["sku"]}
    for line in sorted(no_sku - set(manual)):
        problems.append(f"line {line} has no barcode and was not confirmed by hand")
    for line in sorted(set(manual) - no_sku):
        problems.append(f"line {line} cannot be confirmed by hand "
                        "(it has a barcode, or is not in this order)")
    return problems


def packed(cur, limit: int, start: datetime, end: datetime) -> list[dict]:
    """Orders PACKED in [start, end), newest first: the order / BOL / boxes
    table. (Filtered by when they were packed, not when they were placed.)"""
    rows = cur.execute("""
        SELECT p.order_id AS id, COALESCE(o.number, o.id::text) AS number,
               -- Typed in at packing (none yet) or else synced from OTO.
               p.boxes, COALESCE(p.bol_no, b.bol_no) AS bol_no, p.packed_at, o.status, p.packed_by,
               p.packed_at AT TIME ZONE %(tz)s AS packed_local,
               (SELECT COALESCE(sum((x->>'quantity')::int), 0)
                  FROM jsonb_array_elements(p.items) x) AS pieces,
               (SELECT count(*) FROM box_loads l WHERE l.order_id = p.order_id) AS loaded
        FROM packed_orders p
        JOIN orders o ON o.id = p.order_id
        LEFT JOIN order_bol b ON b.order_id = p.order_id
        WHERE p.packed_at >= %(start)s AND p.packed_at < %(end)s
          AND p.shipped_at IS NULL           -- fully loaded ones are "being shipped"
        ORDER BY p.packed_at DESC, p.order_id DESC
        LIMIT %(limit)s
    """, _params(limit=limit, start=start, end=end)).fetchall()
    return [{
        "id": r["id"], "number": r["number"], "boxes": r["boxes"],
        "bol_no": r["bol_no"], "pieces": r["pieces"], "status": r["status"],
        "loaded": r["loaded"],                 # boxes already scanned onto the truck
        "packed_by": list(r["packed_by"] or []),
        "packed_local": r["packed_local"].isoformat(timespec="seconds"),
    } for r in rows]


# --- Endpoints ----------------------------------------------------------------

FROM_Q = Query(None, alias="from", description="First day (YYYY-MM-DD), from 00:00 Riyadh time. "
                                                 "Default: the 1st of this month.")
TO_Q = Query(None, alias="to", description="Last day (YYYY-MM-DD), included in full. "
                                           "Default: the same as From; with no dates at "
                                           "all, the last day of this month.")


@router.get("/summary")
def get_summary(date_from: date | None = FROM_Q, date_to: date | None = TO_Q):
    """The start of the Website flow: pieces and orders waiting."""
    r = resolve_range(date_from, date_to)
    with connect() as conn:
        cur = cursor(conn)
        return {**summary(cur, r), "range": range_info(cur, r)}


@router.get("/items")
def get_items(date_from: date | None = FROM_Q, date_to: date | None = TO_Q):
    """The pick list: every piece the waiting orders need, per item."""
    r = resolve_range(date_from, date_to)
    with connect() as conn:
        cur = cursor(conn)
        items = pick_list(cur, r)
        pickers = cur.execute("SELECT workers FROM pick_workers WHERE date_from = %s AND date_to = %s",
                              (r["from"], r["to"])).fetchone()
        return {"items": items, "pieces": sum(x["quantity"] for x in items),
                "summary": summary(cur, r), "range": range_info(cur, r),
                # Who collects this pick list; printed on the sheet.
                "pickers": list(pickers["workers"]) if pickers else []}


@router.get("/orders")
def get_orders(date_from: date | None = FROM_Q, date_to: date | None = TO_Q,
               older: bool = Query(False, description="List the orders placed before "
                                   "From that are still waiting, instead.")):
    """Waiting orders in three tables: 1 piece, exactly 2, 3 or more."""
    r = resolve_range(date_from, date_to)
    with connect() as conn:
        cur = cursor(conn)
        return {"tables": waiting_orders(cur, r, older=older), "older": older,
                "range": range_info(cur, r)}


# --- OTO (tryoto.com): BOL numbers and box counts (2026-09-29/30) -------
# The "OTO sync" button asks OTO, the shipping platform, for every order on
# the screen: its BOL (the carrier's AWB number), box count, carrier and
# tracking link. The client
# is app/oto.py. With no OTO_REFRESH_TOKEN in .env it runs in TEST mode.

BOL_MODE = "oto" if OTO_REFRESH_TOKEN else "test"


def fetch_bols_from_oto(orders: list[dict]) -> tuple[dict[int, dict], int]:
    """({order_id: {bol_no, boxes, carrier, tracking_url}}, failures) for
    these orders ({id, number} each). Orders OTO has nothing for are left out.

    TEST MODE (no token): invents a BOL per order, "TEST-" + 10 digits so it
    can never pass for a real one, and 1 box."""
    if BOL_MODE == "test":
        return {o["id"]: {"bol_no": "TEST-" + "".join(secrets.choice("0123456789") for _ in range(10)),
                          "boxes": 1, "carrier": None, "tracking_url": None}
                for o in orders}, 0
    by_number, failed = oto.fetch_orders([str(o["number"]) for o in orders])
    return {o["id"]: by_number[str(o["number"])] for o in orders
            if str(o["number"]) in by_number}, failed


@router.post("/oto/sync")
def oto_sync(date_from: date | None = FROM_Q, date_to: date | None = TO_Q,
             older: bool = Query(False, description="The older waiting orders instead.")):
    """Read the BOL number, box count, carrier and tracking link of the
    waiting orders on screen (the same From/To, or the older ones) from OTO
    and store them in order_bol.

    With real OTO every order is asked again (a shipment can be created or
    replaced after a failed delivery). In test mode only orders without a
    number get one, so the test numbers stay put. 502 when OTO cannot be
    used at all (e.g. a wrong token); single orders that fail are counted."""
    r = resolve_range(date_from, date_to)
    with connect() as conn:
        cur = cursor(conn)
        tables = waiting_orders(cur, r, older=older)
        orders = [o for size in SIZES for o in tables[size]]
        ask = [o for o in orders if BOL_MODE != "test" or not o["bol_no"]]
        try:
            found, failed = fetch_bols_from_oto(ask)
        except oto.OtoError as e:
            raise HTTPException(502, f"OTO: {e}") from e
        changed = 0
        for order_id, f in found.items():
            changed += cur.execute("""
                INSERT INTO order_bol (order_id, bol_no, boxes, carrier, tracking_url, source)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (order_id) DO UPDATE
                    SET bol_no = EXCLUDED.bol_no, boxes = EXCLUDED.boxes,
                        carrier = EXCLUDED.carrier, tracking_url = EXCLUDED.tracking_url,
                        source = EXCLUDED.source, synced_at = now()
                    WHERE (order_bol.bol_no, order_bol.boxes, order_bol.carrier, order_bol.tracking_url)
                          IS DISTINCT FROM
                          (EXCLUDED.bol_no, EXCLUDED.boxes, EXCLUDED.carrier, EXCLUDED.tracking_url)
            """, (order_id, f["bol_no"], f["boxes"], f["carrier"], f["tracking_url"], BOL_MODE)).rowcount
        with_bol = sum(1 for o in orders
                       if (found.get(o["id"]) or {}).get("bol_no") or (o["id"] not in found and o["bol_no"]))
        return {"mode": BOL_MODE, "orders": len(orders), "updated": changed,
                "with_bol": with_bol, "failed": failed}


@router.get("/orders/{order_id}")
def get_order(order_id: int):
    """One order, for the scanning screen."""
    with connect() as conn:
        return order_detail(cursor(conn), order_id)


class Approval(BaseModel):
    """What the scanning screen sends when the worker presses Approve."""
    # Number of boxes, typed in by the worker. (Checking it against the shipping platform
    # is done by the OTO sync.)
    boxes: int = Field(ge=1, le=99)
    # Every barcode scanned for this order, one entry per scan, as scanned.
    scans: list[str] = Field(default_factory=list, max_length=2000)
    # Line ids confirmed by hand because they have no barcode.
    manual: list[int] = Field(default_factory=list, max_length=500)
    # Who packed it. Left out: the order's assigned packers are used.
    workers: list[str] | None = Field(default=None, max_length=50)


@router.post("/orders/{order_id}/approve")
def approve(order_id: int, body: Approval):
    """Record an order as packed. Refused unless every piece was scanned.

    409 = the order can no longer be packed (already packed, or no longer
          waiting in the store); 422 = the scans do not match the order."""
    with connect() as conn:
        cur = cursor(conn)
        # Lock the order row for the rest of this transaction, so two workers
        # approving the same order at the same moment are handled one after
        # the other: the second one then finds the packing row and gets 409.
        locked = cur.execute("SELECT id FROM orders WHERE id = %s FOR UPDATE",
                             (order_id,)).fetchone()
        if locked is None:
            raise HTTPException(404, f"Order {order_id} is not in the database.")
        order = order_detail(cur, order_id)
        if order["state"] == "packed":
            raise HTTPException(409, "This order has already been packed.")
        if order["state"] != "waiting":
            raise HTTPException(409, f"This order is no longer waiting to be packed "
                                     f"(its status is now {order['status']!r}).")
        labels = find_labels(cur, body.scans)
        problems = check_scans(order["items"], body.scans, body.manual, labels)
        if problems:
            raise HTTPException(422, {"message": "The scans do not match the order.",
                                      "problems": problems})
        # Which labels went with which line, in scan order (lines sharing an
        # item number take them in turn), for the packing record.
        by_sku: dict[str, list[str]] = {}
        for raw in body.scans:
            label = labels.get(normalise(raw))
            if label:
                by_sku.setdefault(normalise(label["sku"]), []).append(label["barcode"])
        snapshot = []
        for x in order["items"]:
            key = normalise(x["sku"]) if x["sku"] else None
            pool = by_sku.get(key, []) if key else []
            taken = pool[:x["quantity"]]          # this line's labels...
            if key:
                by_sku[key] = pool[x["quantity"]:]  # ...the rest for the next line
            snapshot.append({"line_item_id": x["line_item_id"], "name": x["name"],
                             "sku": x["sku"], "quantity": x["quantity"], "options": x["options"],
                             "manual": x["line_item_id"] in body.manual,
                             "labels": taken})
        packed_by = (clean_workers(body.workers) if body.workers is not None
                     else order["workers"])
        cur.execute("""
            INSERT INTO packed_orders (order_id, boxes, items, packed_by, packed_at)
            VALUES (%s, %s, %s::jsonb, %s, %s)
        """, (order_id, body.boxes, json.dumps(snapshot, ensure_ascii=False), packed_by, clock()))
        # Mark the labels as packed into this order. Only labels still on the
        # shelf: if another worker packed one of them a moment ago, the counts
        # differ, and the whole approval is undone (409; scan again).
        used = sorted(x["barcode"] for x in labels.values())
        if used:
            marked = cur.execute("""
                UPDATE unit_labels SET packed_order_id = %s
                WHERE barcode = ANY(%s) AND packed_order_id IS NULL
            """, (order_id, used)).rowcount
            if marked != len(used):
                raise HTTPException(409, "A label was just packed into another order. "
                                         "Start over and scan again.")
        return order_detail(cur, order_id)


@router.get("/labels/{code}")
def get_label(code: str):
    """Which item a unique piece label is, and whether it is already packed.
    404 = not a known label (it may still be a plain item number)."""
    with connect() as conn:
        label = find_labels(cursor(conn), [code]).get(normalise(code))
    if label is None:
        raise HTTPException(404, f"{code.strip()!r} is not a known label.")
    return label


# Test label codes: "U" + 7 characters, without the look-alikes I/O/0/1.
LABEL_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_label_code() -> str:
    return "U" + "".join(secrets.choice(LABEL_ALPHABET) for _ in range(7))


@router.post("/orders/{order_id}/test-labels")
def make_test_labels(order_id: int):
    """TEST ONLY (2026-09-29): one unique label per piece of this order,
    until real labels come from an ERP / the label printer.

    Pressing it again returns the same labels (topped up if the order grew,
    or if some of them were packed into ANOTHER order meanwhile, which is
    allowed: any piece of an item can go into any order)."""
    with connect() as conn:
        cur = cursor(conn)
        order = order_detail(cur, order_id)          # 404 if unknown
        need = Counter()
        for x in order["items"]:
            if x["sku"]:
                need[x["sku"]] += x["quantity"]
        mine = """SELECT barcode, sku, packed_order_id FROM unit_labels
                  WHERE test_order_id = %(id)s
                    AND (packed_order_id IS NULL OR packed_order_id = %(id)s)"""
        have = Counter(r["sku"] for r in cur.execute(mine, {"id": order_id}).fetchall())
        for sku, n in need.items():
            for _ in range(n - have[sku]):
                # A clash with an existing code is practically impossible
                # (32^7 codes); on one, simply draw again.
                while not cur.execute("""
                    INSERT INTO unit_labels (barcode, sku, source, test_order_id)
                    VALUES (%s, %s, 'test', %s) ON CONFLICT DO NOTHING RETURNING barcode
                """, (new_label_code(), sku, order_id)).fetchone():
                    pass
        items = {x["sku"]: x for x in reversed(order["items"]) if x["sku"]}
        rows = cur.execute(mine + " ORDER BY sku, created_at, barcode", {"id": order_id}).fetchall()
        return {"labels": [{
            "barcode": r["barcode"], "sku": r["sku"],
            "name": items.get(r["sku"], {}).get("name"),
            "options": items.get(r["sku"], {}).get("options", []),
            "packed": r["packed_order_id"] is not None,
        } for r in rows if r["sku"] in need]}


@router.delete("/orders/{order_id}/packing")
def undo_packing(order_id: int):
    """Undo an approval made by mistake: the order is waiting again."""
    with connect() as conn:
        if conn.execute("SELECT 1 FROM box_loads WHERE order_id = %s LIMIT 1", (order_id,)).fetchone():
            raise HTTPException(409, "Boxes of this order are already loaded onto the truck. "
                                     "Undo the loading first.")
        row = conn.execute("DELETE FROM packed_orders WHERE order_id = %s RETURNING order_id",
                           (order_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"Order {order_id} is not packed.")
        return order_detail(cursor(conn), order_id)


@router.get("/packed")
def get_packed(date_from: date | None = Query(None, alias="from", description="First PACKING day "
                                             "(YYYY-MM-DD). Default: the 1st of this month."),
               date_to: date | None = Query(None, alias="to", description="Last packing day, "
                                            "included in full. Default: today."),
               limit: int = Query(5000, ge=1, le=20000)):
    """Orders packed between two days (Riyadh days, both included), newest
    first. No dates (2026-09-30): the whole current month so far. The
    same rules as the other dates otherwise (400 for To before From, a day
    after today, or more than a year)."""
    r, info = month_range(date_from, date_to)
    with connect() as conn:
        rows = packed(cursor(conn), limit + 1, r["start"], r["end"])
    return {
        "packed": rows[:limit],
        "truncated": len(rows) > limit,           # more than `limit` in the range
        "range": info,
    }


def month_range(date_from: date | None, date_to: date | None) -> tuple[dict, dict]:
    """Packing / loading days asked for; no dates = this month so far. The
    same checks as resolve_range. Returns (window, what the page shows)."""
    today = today_local()
    if date_from is None and date_to is None:
        date_from, date_to = today.replace(day=1), today
    r = resolve_range(date_from, date_to)
    local = lambda dt: dt.astimezone(LOCAL_TZ).replace(tzinfo=None).isoformat(timespec="minutes")
    return r, {"from": r["from"].isoformat(), "to": r["to"].isoformat(),
               "today": today.isoformat(),
               "is_default": r["from"] == today.replace(day=1) and r["to"] == today,
               "max": r["month_last"].isoformat(),
               "start_local": local(r["start"]),
               "last_local": local(r["end"] - timedelta(minutes=1))}


# --- Loading onto the truck (2026-09-30) ----------------------------------
# At handover every BOX is scanned (its BOL label). An order packed in N
# boxes needs N scans; after the last one it leaves "Packed orders" and is
# "being shipped". Scans are counted: the boxes of one order carry the same
# BOL, so box 1 and box 2 cannot be told apart. db/08_shipping.sql.

_BOL = "lower(btrim(COALESCE(p.bol_no, b.bol_no)))"


def loading_list(cur) -> list[dict]:
    """Every packed order not yet fully loaded, whenever it was packed,
    OLDEST first: the ones waiting longest (forgotten?) come first."""
    rows = cur.execute("""
        SELECT p.order_id AS id, COALESCE(o.number, o.id::text) AS number, p.boxes,
               COALESCE(p.bol_no, b.bol_no) AS bol_no, b.carrier, b.tracking_url,
               p.packed_at AT TIME ZONE %(tz)s AS packed_local, p.packed_by,
               (SELECT count(*) FROM box_loads l WHERE l.order_id = p.order_id) AS loaded
        FROM packed_orders p
        JOIN orders o ON o.id = p.order_id
        LEFT JOIN order_bol b ON b.order_id = p.order_id
        WHERE p.shipped_at IS NULL
        ORDER BY p.packed_at, p.order_id
    """, _params()).fetchall()
    return [{**{k: r[k] for k in ("id", "number", "boxes", "bol_no", "carrier", "tracking_url", "loaded")},
             "packed_by": list(r["packed_by"] or []),
             "packed_local": r["packed_local"].isoformat(timespec="seconds")} for r in rows]


@router.get("/loading")
def get_loading():
    """The truck screen's list: packed orders still (partly) to be loaded."""
    with connect() as conn:
        return {"orders": loading_list(cursor(conn))}


class BoxScan(BaseModel):
    """One box's BOL label, as scanned at the truck."""
    code: str = Field(min_length=1, max_length=200)


@router.post("/load")
def load_box(body: BoxScan):
    """One box scanned onto the truck. Finds the packed order with that BOL
    and counts the box; the last box moves the order to "being shipped".

    404 = no packed order has this BOL; 409 = the order is not packed yet,
    or all its boxes are already loaded (a box scanned twice?)."""
    code = normalise(body.code)
    if not code:
        raise HTTPException(422, "Empty scan.")
    with connect() as conn:
        cur = cursor(conn)
        # Lock the order's packing row, so two scans of the same order at
        # the same moment are counted one after the other.
        rows = cur.execute(f"""
            SELECT p.order_id AS id, COALESCE(o.number, o.id::text) AS number, p.boxes,
                   p.shipped_at, COALESCE(p.bol_no, b.bol_no) AS bol_no
            FROM packed_orders p
            JOIN orders o ON o.id = p.order_id
            LEFT JOIN order_bol b ON b.order_id = p.order_id
            WHERE {_BOL} = %(code)s
            ORDER BY p.shipped_at NULLS FIRST, p.packed_at
            FOR UPDATE OF p
        """, {"code": code}).fetchall()
        if not rows:
            waiting = cur.execute("""
                SELECT COALESCE(o.number, o.id::text) AS number
                FROM order_bol b JOIN orders o ON o.id = b.order_id
                WHERE lower(btrim(b.bol_no)) = %(code)s
                  AND NOT EXISTS (SELECT 1 FROM packed_orders p WHERE p.order_id = o.id)
                LIMIT 1
            """, {"code": code}).fetchone()
            if waiting:
                raise HTTPException(409, {"reason": "not_packed", "number": waiting["number"]})
            raise HTTPException(404, {"reason": "unknown", "code": body.code.strip()})
        o = next((r for r in rows if r["shipped_at"] is None), None)
        if o is None:
            raise HTTPException(409, {"reason": "all_loaded", "number": rows[0]["number"],
                                      "boxes": rows[0]["boxes"]})
        cur.execute("INSERT INTO box_loads (order_id, scanned_code, loaded_at) VALUES (%s, %s, %s)",
                    (o["id"], body.code.strip(), clock()))
        loaded = cur.execute("SELECT count(*) AS n FROM box_loads WHERE order_id = %s",
                             (o["id"],)).fetchone()["n"]
        shipped = loaded >= o["boxes"]
        if shipped:
            cur.execute("UPDATE packed_orders SET shipped_at = %s WHERE order_id = %s",
                        (clock(), o["id"]))
        return {"id": o["id"], "number": o["number"], "bol_no": o["bol_no"],
                "boxes": o["boxes"], "loaded": loaded, "shipped": shipped}


@router.delete("/orders/{order_id}/loading")
def undo_loading(order_id: int):
    """Undo a loading scanned by mistake: the order's box scans are removed
    and it is back in "Packed orders"."""
    with connect() as conn:
        n = conn.execute("DELETE FROM box_loads WHERE order_id = %s", (order_id,)).rowcount
        if not n:
            raise HTTPException(404, f"No boxes of order {order_id} are loaded.")
        conn.execute("UPDATE packed_orders SET shipped_at = NULL WHERE order_id = %s", (order_id,))
        return order_detail(cursor(conn), order_id)


@router.get("/shipping")
def get_shipping(date_from: date | None = Query(None, alias="from", description="First LOADING day "
                                               "(YYYY-MM-DD). Default: the 1st of this month."),
                 date_to: date | None = Query(None, alias="to", description="Last loading day. "
                                              "Default: today."),
                 limit: int = Query(5000, ge=1, le=20000)):
    """Orders being shipped: all boxes loaded onto the truck, by the day the
    last box was loaded (default: this month so far), newest first."""
    r, info = month_range(date_from, date_to)
    with connect() as conn:
        rows = cursor(conn).execute("""
            SELECT p.order_id AS id, COALESCE(o.number, o.id::text) AS number, p.boxes,
                   COALESCE(p.bol_no, b.bol_no) AS bol_no, b.carrier, b.tracking_url,
                   p.packed_by, p.packed_at AT TIME ZONE %(tz)s AS packed_local,
                   p.shipped_at AT TIME ZONE %(tz)s AS shipped_local,
                   (SELECT COALESCE(sum((x->>'quantity')::int), 0)
                      FROM jsonb_array_elements(p.items) x) AS pieces
            FROM packed_orders p
            JOIN orders o ON o.id = p.order_id
            LEFT JOIN order_bol b ON b.order_id = p.order_id
            WHERE p.shipped_at >= %(start)s AND p.shipped_at < %(end)s
            ORDER BY p.shipped_at DESC, p.order_id DESC
            LIMIT %(limit)s
        """, _params(start=r["start"], end=r["end"], limit=limit + 1)).fetchall()
    out = [{**{k: x[k] for k in ("id", "number", "boxes", "bol_no", "carrier", "tracking_url", "pieces")},
            "packed_by": list(x["packed_by"] or []),
            "packed_local": x["packed_local"].isoformat(timespec="seconds"),
            "shipped_local": x["shipped_local"].isoformat(timespec="seconds")} for x in rows]
    return {"shipped": out[:limit], "truncated": len(out) > limit, "range": info}


class Workers(BaseModel):
    """Names to assign. An empty list removes the assignment."""
    workers: list[str] = Field(default_factory=list, max_length=50)


class OrderAssignment(Workers):
    """The same names for one or several orders at once."""
    order_ids: list[int] = Field(min_length=1, max_length=1000)


@router.get("/workers")
def get_workers():
    """Names used before, most recent first: suggestions for the name boxes."""
    with connect() as conn:
        return {"workers": known_workers(cursor(conn))}


@router.put("/pickers")
def set_pickers(body: Workers, date_from: date | None = FROM_Q, date_to: date | None = TO_Q):
    """Who collects the pick list of these dates ("Picked by …")."""
    r = resolve_range(date_from, date_to)
    names = clean_workers(body.workers)
    with connect() as conn:
        if names:
            conn.execute("""
                INSERT INTO pick_workers (date_from, date_to, workers, assigned_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (date_from, date_to) DO UPDATE
                    SET workers = EXCLUDED.workers, assigned_at = now()
            """, (r["from"], r["to"], names))
        else:
            conn.execute("DELETE FROM pick_workers WHERE date_from = %s AND date_to = %s",
                         (r["from"], r["to"]))
    return {"from": r["from"].isoformat(), "to": r["to"].isoformat(), "workers": names}


@router.put("/orders/assign")
def assign_orders(body: OrderAssignment):
    """Who packs these orders ("Order 123 packed by Sam, Ahmed"). The same
    names go on every order listed; an empty list clears them. Orders already
    packed keep the names they were packed with (packed_by) either way."""
    names = clean_workers(body.workers)
    ids = sorted(set(body.order_ids))
    with connect() as conn:
        found = {r[0] for r in conn.execute("SELECT id FROM orders WHERE id = ANY(%s)",
                                             (ids,)).fetchall()}
        missing = [i for i in ids if i not in found]
        if missing:
            raise HTTPException(404, f"Not in the database: {missing[:10]}")
        if names:
            for i in ids:
                conn.execute("""
                    INSERT INTO order_workers (order_id, workers, assigned_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (order_id) DO UPDATE
                        SET workers = EXCLUDED.workers, assigned_at = now()
                """, (i, names))
        else:
            conn.execute("DELETE FROM order_workers WHERE order_id = ANY(%s)", (ids,))
    return {"order_ids": ids, "workers": names}
