"""Exercise the storage app's API (app/storage.py) against a real Postgres.

    $env:POSTGRES_DB="storefront_analytics_test"; python -m tests.test_storage   # PowerShell
    POSTGRES_DB=storefront_analytics_test python -m tests.test_storage           # bash

Same rules as the other test files: a real database, a refusal to run unless
POSTGRES_DB ends in `_test`, and fixtures truncated and rebuilt on every run.
Needs db/03_storage.sql, 05_workers.sql, 06_unit_labels.sql, 07_order_bol.sql
and 08_shipping.sql applied to the test database (see README.md).

What it pins down:
  * "waiting" = processing, not packed, not an E2E test order;
  * orders are sized by PIECES (sum of quantities), not by lines;
  * the pick list adds up per variation and carries SKU and category;
  * Approve is refused unless every piece was scanned exactly once, lines
    without a barcode were confirmed by hand, and the order is still waiting;
  * an approved order leaves the waiting lists and appears in the packed list;
  * undo puts it back;
  * unique piece labels: one label counts once, for its item; a label packed
    into one order cannot go into another; undo frees it again.
"""
import json, sys
from datetime import date, datetime, timezone

from app import config

if not config.POSTGRES_DB.endswith("_test"):
    sys.exit(
        f"Refusing to run against database {config.POSTGRES_DB!r}.\n"
        "These tests insert fixture orders. Point POSTGRES_DB at a database "
        "whose name ends in '_test' (see tests/test_backfill.py's docstring)."
    )

from fastapi.testclient import TestClient

import app.main as main
from app import storage
from app.db import connect

FAILURES = []
def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond: FAILURES.append(name)

UTC = timezone.utc
def utc(*a): return datetime(*a, tzinfo=UTC)


# ===========================================================================
# 1. Pure rules (no database)
# ===========================================================================
check("1 piece -> one", storage.size_of(1) == "one")
check("2 pieces -> two", storage.size_of(2) == "two")
check("3 and 7 pieces -> three_plus",
      storage.size_of(3) == storage.size_of(7) == "three_plus")
check("barcodes compare without spaces or case", storage.normalise(" Ab12\n") == "ab12")

LINES = [{"line_item_id": 1, "sku": "5025", "quantity": 2},
         {"line_item_id": 2, "sku": "12025", "quantity": 1},
         {"line_item_id": 3, "sku": None, "quantity": 1}]
check("complete scans + hand-confirmed line -> no problems",
      storage.check_scans(LINES, ["5025", "12025", " 5025 "], [3]) == [])
check("quantity 2 needs two scans",
      any("1 of 2" in p for p in storage.check_scans(LINES, ["5025", "12025"], [3])))
check("a third scan of a quantity-2 item is refused",
      any("3 times" in p for p in storage.check_scans(LINES, ["5025"] * 3 + ["12025"], [3])))
check("a barcode that is not in the order is refused",
      any("not in this order" in p
          for p in storage.check_scans(LINES, ["5025", "5025", "12025", "999"], [3])))
check("a line without a barcode must be confirmed by hand",
      any("line 3" in p for p in storage.check_scans(LINES, ["5025", "5025", "12025"], [])))
check("a line WITH a barcode cannot be confirmed by hand instead",
      any("line 2" in p for p in storage.check_scans(LINES, ["5025", "5025"], [2, 3])))
check("two lines with the same SKU are pooled",
      storage.check_scans([{"line_item_id": 1, "sku": "A", "quantity": 1},
                           {"line_item_id": 2, "sku": "a", "quantity": 1}],
                          ["A", "A"], []) == [])

# Unique piece labels (2026-09-29): as find_labels returns them.
LABELS = {"u1": {"barcode": "U1", "sku": "5025", "packed_order_id": None, "packed_number": None},
          "u2": {"barcode": "U2", "sku": "5025", "packed_order_id": None, "packed_number": None},
          "u9": {"barcode": "U9", "sku": "5025", "packed_order_id": 77, "packed_number": "77"}}
check("labels: two different labels cover a quantity-2 item",
      storage.check_scans(LINES, ["U1", " u2 ", "12025"], [3], LABELS) == [])
check("labels: a label and an item-number scan can be mixed",
      storage.check_scans(LINES, ["U1", "5025", "12025"], [3], LABELS) == [])
check("labels: the same label twice is refused",
      any("more than once" in p for p in storage.check_scans(LINES, ["U1", "U1", "12025"], [3], LABELS)))
check("labels: a label packed into another order is refused",
      any("already packed in order #77" in p
          for p in storage.check_scans(LINES, ["U1", "U9", "12025"], [3], LABELS)))


# ===========================================================================
# 2. Fixtures
# ===========================================================================
with connect() as conn:
    conn.execute("TRUNCATE orders, refunds, refund_items, product_categories, "
                 "sync_state, packed_orders, pick_workers, order_workers, unit_labels, order_bol, "
                 "box_loads CASCADE")

def add_order(oid, status, created, items, title="Apple Pay", meta=None):
    """items: (line_id, product_id, variation_id, name, sku, qty).
    meta: {line_id: [meta_data entries]}, stored in raw like the API sends it."""
    raw = {"line_items": [{"id": line, "meta_data": (meta or {}).get(line, [])}
                          for line, *_ in items]}
    with connect() as conn:
        conn.execute("""INSERT INTO orders (id, number, status, total, payment_method,
                            payment_method_title, date_created_gmt, raw)
                        VALUES (%s, %s, %s, 100, 'hyperpay_applepay', %s, %s, %s)""",
                     (oid, str(oid), status, title, created, json.dumps(raw)))
        for line, pid, var, name, sku, qty in items:
            conn.execute("""INSERT INTO order_items (order_id, line_item_id, product_id,
                                variation_id, name, sku, quantity)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                         (oid, line, pid, var, name, sku, qty))

# The clock: 3 September, 13:00 Riyadh time. With no dates the page shows
# YESTERDAY, 2 September: 1 Sep 21:00 to 2 Sep 21:00 UTC (00:00 to 00:00 in
# Riyadh). All the "waiting" fixtures below sit inside it.
storage.clock = lambda: utc(2026, 9, 3, 10)

# Waiting (processing), placed on 2 September (Riyadh):
add_order(5001, "processing", utc(2026, 9, 2, 5), [(1, 10, 0, "Doll 1", "1001", 1)])
add_order(5002, "processing", utc(2026, 9, 2, 4), [(1, 11, 0, "Doll 2", "1002", 1)])  # older
add_order(5004, "processing", utc(2026, 9, 2, 10), [(1, 10, 0, "Doll 1", "1001", 2)])  # 1 line, 2 pieces
add_order(5005, "processing", utc(2026, 9, 2, 11), [(1, 10, 0, "Doll 1", "1001", 1),
                                                    (2, 11, 0, "Doll 2", "1002", 1)])
# Line meta_data as this store sends it (a check, 2026-09-28): colour and
# size attributes, plus plugin entries that must NOT show up.
SCARF_RED = [
    {"key": "pa_color-ar", "display_key": "اللون", "value": "red", "display_value": "احمر"},
    {"key": "pa_choose-the-size", "display_key": "اختر المقاس", "value": "l", "display_value": "L"},
    {"key": "_reduced_stock", "display_key": "_reduced_stock", "value": "1", "display_value": "1"},
    {"key": "_wdr_discounts", "display_key": "_wdr_discounts", "value": {"saved_amount": 6},
     "display_value": {"saved_amount": 6}},
    {"key": "gift-note", "display_key": "Gift note", "value": "x" * 200, "display_value": "x" * 200},
    {"key": "engraving", "display_key": "Engraving", "value": "K", "display_value": " K "},
]
add_order(5007, "processing", utc(2026, 9, 2, 20), [(1, 20, 21, "Scarf 1 - Red", "5025", 2),
                                                    (2, 10, 0, "Doll 1", "1001", 1),
                                                    (3, 30, 0, "Blanket 1", None, 1)],
          meta={1: SCARF_RED})
add_order(5008, "processing", utc(2026, 9, 2, 20, 30), [(1, 20, 22, "Scarf 1 - Blue", "5026", 3)],
          meta={1: [{"key": "pa_color-ar", "display_key": "اللون", "value": "blue",
                     "display_value": "ازرق"},
                    {"key": "pa_size-ar", "display_key": "المقاس", "value": "m",
                     "display_value": "50 × 90 سم"}]})
# Waiting, but OUTSIDE 2 September:
add_order(5200, "processing", utc(2026, 9, 1, 12), [(1, 11, 0, "Doll 2", "1002", 1)])  # 1 Sep: older
add_order(5201, "processing", utc(2026, 9, 2, 21), [(1, 11, 0, "Doll 2", "1002", 1)])  # 3 Sep 00:00 sharp: today
# Not waiting:
add_order(5100, "delivered",  utc(2026, 9, 1, 8), [(1, 10, 0, "Doll 1", "1001", 5)])
add_order(5101, "pending",    utc(2026, 9, 1, 8), [(1, 10, 0, "Doll 1", "1001", 5)])
add_order(5102, "processing", utc(2026, 9, 1, 8), [(1, 10, 0, "Doll 1", "1001", 5)],
          title="E2E automated verification (no real payment)")
with connect() as conn:
    conn.execute("""INSERT INTO product_categories (product_id, category_id, category_name)
                    VALUES (10, 1, 'Plushies'), (11, 1, 'Plushies'),
                           (20, 2, 'Scarves'), (20, 3, 'Winter'), (30, 4, 'Blankets')""")

client = TestClient(main.app)
# Since 2026-10-01 the order screens default to the whole current month. Most
# checks below were written for ONE day, 2 Sep (yesterday by the test clock),
# so the list endpoints get those dates unless a test passes its own; the
# month default itself is checked with raw_get.
DAY = {"from": "2026-09-02", "to": "2026-09-02"}
DAY_PATHS = ("/summary", "/orders", "/items", "/oto/sync")
def with_day(path, params):
    if path.endswith(DAY_PATHS) and "from" not in params and "to" not in params:
        return {**DAY, **params}
    return params
def raw_get(path, **params):
    r = client.get(path, params=params)
    return r.status_code, r.json()
def get(path, **params):
    return raw_get(path, **with_day(path, params))
def post(path, body):
    r = client.post(path, json=body, params=with_day(path, {}))
    return r.status_code, r.json()
def put(path, body):
    r = client.put(path, json=body)
    return r.status_code, r.json()
BASE = "/api/storage/website"


# ===========================================================================
# 3. Summary tiles
# ===========================================================================
code, s = get(f"{BASE}/summary")
check("summary: 200", code == 200, str(code))
check("summary: 6 waiting orders (delivered, pending and E2E left out)",
      s["orders"] == 6, str(s))
# 1 + 1 + 2 + 2 + 4 + 3 = 13 pieces
check("summary: 13 pieces (quantities, not lines)", s["pieces"] == 13, str(s))
check("summary: split by pieces 2 / 2 / 2",
      s["by_size"] == {"one": 2, "two": 2, "three_plus": 2}, str(s["by_size"]))
check("summary: nothing packed today yet", s["packed_today"] == 0)
check("summary: 2 Sep is 00:00 to 23:59, Riyadh time, and not the default",
      (s["range"]["from"], s["range"]["to"], s["range"]["start_local"], s["range"]["last_local"],
       s["range"]["is_default"])
      == ("2026-09-02", "2026-09-02", "2026-09-02T00:00", "2026-09-02T23:59", False), str(s["range"]))
# No dates (2026-10-01): the whole current month, 1st to last day.
code, sm = raw_get(f"{BASE}/summary")
check("by default the whole month: 1 to 30 Sep, the pickers go up to 30 Sep",
      code == 200 and (sm["range"]["from"], sm["range"]["to"], sm["range"]["max"], sm["range"]["is_default"],
                       sm["range"]["last_local"])
      == ("2026-09-01", "2026-09-30", "2026-09-30", True, "2026-09-30T23:59"), str(sm.get("range")))
check("…holding every waiting order of September (1, 2 and 3 Sep: 8), none older",
      sm["orders"] == 8 and sm["range"]["older_waiting"] == 0, str(sm))
code, om = raw_get(f"{BASE}/orders")
check("orders to pack by default: the month too",
      code == 200 and om["range"]["is_default"] and sum(len(v) for v in om["tables"].values()) == 8,
      str(om.get("range")))
check("summary: one older order is still waiting (placed 1 Sep)",
      s["range"]["older_waiting"] == 1, str(s["range"]))

# The date rules themselves.
check("a day starts at 00:00 Riyadh = 21:00 UTC the day before",
      storage.local_midnight(date(2026, 9, 28)) == utc(2026, 9, 27, 21))
code, s2 = get(f"{BASE}/summary", **{"from": "2026-09-01", "to": "2026-09-01"})
check("one other day: 1 Sep holds the 1 Sep order, nothing older",
      code == 200 and s2["orders"] == 1 and s2["range"]["is_default"] is False
      and s2["range"]["older_waiting"] == 0, str(s2))
code, s2 = get(f"{BASE}/summary", **{"from": "2026-09-02", "to": "2026-09-03"})
check("From and To both included: 2-3 Sep holds 2 Sep and today's midnight order (7)",
      code == 200 and s2["orders"] == 7 and s2["range"]["last_local"] == "2026-09-03T23:59", str(s2))
code, s2 = get(f"{BASE}/summary", **{"from": "2026-09-03"})
check("From alone = that one day (the 00:00 order is in today, not yesterday)",
      code == 200 and s2["orders"] == 1 and s2["range"]["to"] == "2026-09-03", str(s2))
code, _ = get(f"{BASE}/summary", **{"from": "2026-09-03", "to": "2026-09-02"})
check("To before From -> 400", code == 400, str(code))
code, s2 = get(f"{BASE}/summary", **{"from": "2026-09-02", "to": "2026-09-30"})
check("a later day of this month is allowed (nothing placed yet)", code == 200 and s2["orders"] == 7, str(code))
code, _ = get(f"{BASE}/summary", **{"from": "2026-09-02", "to": "2026-10-01"})
check("a To date after the end of this month -> 400", code == 400, str(code))
code, _ = get(f"{BASE}/summary", **{"from": "2024-01-01", "to": "2026-09-02"})
check("more than a year at once -> 400", code == 400, str(code))
code, old = get(f"{BASE}/orders", older="true")
check("older=true lists the older waiting orders only",
      [x["id"] for t in old["tables"].values() for x in t] == [5200], str(old["tables"]))


# ===========================================================================
# 4. Pick list
# ===========================================================================
code, it = get(f"{BASE}/items")
rows = {(x["product_id"], x["variation_id"]): x for x in it["items"]}
check("items: one row per product variation", len(rows) == 5, str(sorted(rows)))
check("items: Doll 1 adds up over 4 orders (1 + 2 + 1 + 1 = 5)",
      rows[(10, 0)]["quantity"] == 5 and rows[(10, 0)]["orders"] == 4, str(rows[(10, 0)]))
check("items: the two scarf colours stay separate",
      rows[(20, 21)]["quantity"] == 2 and rows[(20, 22)]["quantity"] == 3)
check("items: item number is the SKU", rows[(20, 21)]["sku"] == "5025")
RED = [{"kind": "color", "label": "اللون", "value": "احمر"},
       {"kind": "size", "label": "اختر المقاس", "value": "L"},
       {"kind": "other", "label": "Engraving", "value": "K"}]
check("items: colour and size of a variation come from the order line; plugin data is skipped",
      rows[(20, 21)]["options"] == RED, str(rows[(20, 21)]["options"]))
check("items: the other colour has its own row and its own options",
      [(o["kind"], o["value"]) for o in rows[(20, 22)]["options"]]
      == [("color", "ازرق"), ("size", "50 × 90 سم")], str(rows[(20, 22)]["options"]))
check("items: a plain product has no options", rows[(10, 0)]["options"] == [])
check("option kinds: by key, or by the Arabic label",
      storage.option_kind("pa_colour", "") == "color" and storage.option_kind("x", "اللون") == "color"
      and storage.option_kind("pa_choose-the-size", "") == "size"
      and storage.option_kind("x", "المقاس") == "size" and storage.option_kind("pa_material", "") == "other")
check("items: a product in two categories lists both",
      rows[(20, 21)]["category"] == "Scarves, Winter", rows[(20, 21)]["category"])
check("items: all_categories keeps the full list",
      rows[(20, 21)]["all_categories"] == ["Scarves", "Winter"], str(rows[(20, 21)]))

# The one category shown (app/shelf_categories.py, 2026-09-28).
from app.shelf_categories import shelf_category
check("shelf category: the product type wins over marketing categories",
      shelf_category(["أقل من 100 ريال", "عروض خاصة", "غرفة النوم", "لباد سرير"]) == "لباد سرير")
check("shelf category: kids' comforter sets show the general name",
      shelf_category(["أطقم لحافات", "اطقم لحافات أطفال", "الاطفال", "مفارش سرير"]) == "أطقم لحافات")
check("shelf category: bed covers win over comforter/quilt categories",
      shelf_category(["أطقم غطاء سرير", "اطقم لحافات صيفية", "لحاف مضغوط"]) == "أطقم غطاء سرير")
check("shelf category: soft toys win over pillows",
      shelf_category(["دمى اطفال", "مخدات", "الاطفال"]) == "دمى اطفال")
check("shelf category: the kids' robe goes with towels and robes",
      shelf_category(["ارواب حمام اطفال", "حمام", "مناشف و ارواب حمام"]) == "مناشف و ارواب حمام")
check("shelf category: extra spaces in the store's name still match",
      shelf_category(["لباد  سرير "]) == "لباد  سرير ")
check("shelf category: no known type -> the full list, unchanged",
      shelf_category(["Winter", "Scarves"]) == "Scarves, Winter")
check("shelf category: none at all -> None",
      shelf_category([]) is None and shelf_category(None) is None)

check("items: a line without SKU still appears (sku null)",
      rows[(30, 0)]["sku"] is None and rows[(30, 0)]["quantity"] == 1)
check("items: total pieces equals the summary tile",
      it["pieces"] == it["summary"]["pieces"] == 13)
check("items: sorted by category (Blankets first)",
      [x["category"] for x in it["items"]][0] == "Blankets")


# ===========================================================================
# 5. Order tables
# ===========================================================================
code, o = get(f"{BASE}/orders")
t = {k: [x["id"] for x in v] for k, v in o["tables"].items()}
check("orders: one-piece table, oldest first", t["one"] == [5002, 5001], str(t))
check("orders: one line of quantity 2 is a TWO order", 5004 in t["two"], str(t))
check("orders: two lines of 1 each is a TWO order", 5005 in t["two"])
check("orders: 3+ table", t["three_plus"] == [5007, 5008], str(t))
o5007 = o["tables"]["three_plus"][0]
check("orders: each order carries its lines with SKUs",
      [x["sku"] for x in o5007["items"]] == ["5025", "1001", None], str(o5007["items"]))
check("orders: no customer details are sent",
      not any(k in o5007 for k in ("billing_email", "email", "customer_id")))


# ===========================================================================
# 6. One order, scanning and approval
# ===========================================================================
code, d = get(f"{BASE}/orders/5007")
check("order page: each line carries its colour and size",
      d["items"][0]["options"] == RED and d["items"][1]["options"] == [], str(d["items"][0]))
check("order: waiting, 4 pieces", d["state"] == "waiting" and d["pieces"] == 4, str(d))
code, _ = get(f"{BASE}/orders/424242")
check("order: unknown id -> 404", code == 404, str(code))
code, d = get(f"{BASE}/orders/5100")
check("order: a delivered order is 'not_waiting'", d["state"] == "not_waiting")

GOOD = {"boxes": 2, "scans": ["5025", "5025", "1001"], "manual": [3]}
code, r = post(f"{BASE}/orders/5007/approve", {**GOOD, "scans": ["5025", "1001"]})
check("approve: one scan missing -> 422 with the reason",
      code == 422 and any("1 of 2" in p for p in r["detail"]["problems"]), str(r))
code, r = post(f"{BASE}/orders/5007/approve", {**GOOD, "manual": []})
check("approve: unconfirmed no-barcode line -> 422", code == 422, str(r))
code, r = post(f"{BASE}/orders/5007/approve", {**GOOD, "boxes": 0})
check("approve: 0 boxes -> 422", code == 422, str(code))
code, r = post(f"{BASE}/orders/5007/approve", {**GOOD, "boxes": 100})
check("approve: 100 boxes -> 422", code == 422, str(code))
code, r = post(f"{BASE}/orders/5100/approve", {"boxes": 1, "scans": ["1001"] * 5})
check("approve: an order that is no longer waiting -> 409", code == 409, str(r))

code, r = post(f"{BASE}/orders/5007/approve", GOOD)
check("approve: complete scans -> 200, packed", code == 200 and r["state"] == "packed"
      and r["packing"]["boxes"] == 2, str(r))
code, r = post(f"{BASE}/orders/5007/approve", GOOD)
check("approve: the same order twice -> 409", code == 409, str(r))

with connect() as conn:
    snap = conn.execute("SELECT items FROM packed_orders WHERE order_id = 5007").fetchone()[0]
check("approve: stores what was packed, marking the hand-confirmed line",
      [(x["sku"], x["quantity"], x["manual"]) for x in snap]
      == [("5025", 2, False), ("1001", 1, False), (None, 1, True)], str(snap))

code, s = get(f"{BASE}/summary")
check("after approve: 5 orders and 9 pieces waiting",
      s["orders"] == 5 and s["pieces"] == 9 and s["packed_today"] == 1, str(s))
code, o = get(f"{BASE}/orders")
check("after approve: gone from the 3+ table",
      [x["id"] for x in o["tables"]["three_plus"]] == [5008])
code, it = get(f"{BASE}/items")
check("after approve: its pieces leave the pick list",
      (20, 21) not in {(x["product_id"], x["variation_id"]) for x in it["items"]})

code, p = get(f"{BASE}/packed")
check("packed list: order, boxes, pieces, BOL still empty",
      p["packed"] == [{**p["packed"][0], "id": 5007, "boxes": 2, "pieces": 4, "bol_no": None}],
      str(p))

# Packed list dates (2026-09-30): the days orders were PACKED; default is
# this month so far. The clock says 3 Sept, and Approve stamps that time.
check("packed list: by default the current month so far",
      p["range"]["from"] == "2026-09-01" and p["range"]["to"] == "2026-09-03"
      and p["range"]["is_default"] and not p["truncated"], str(p["range"]))
check("packed_at is the app's clock (3 Sept, 13:00 Riyadh)",
      p["packed"][0]["packed_local"] == "2026-09-03T13:00:00", p["packed"][0]["packed_local"])
code, pa = get(f"{BASE}/packed", **{"from": "2026-08-01", "to": "2026-08-31"})
check("packed list: August -> nothing packed then", code == 200 and pa["packed"] == []
      and not pa["range"]["is_default"], str(pa))
code, pd = get(f"{BASE}/packed", **{"from": "2026-09-03", "to": "2026-09-03"})
check("packed list: one day -> the order packed that day",
      [x["id"] for x in pd["packed"]] == [5007], str(pd))
code, _ = get(f"{BASE}/packed", **{"from": "2026-09-01", "to": "2026-10-01"})
check("packed list: To after the end of this month -> 400", code == 400, str(code))
code, pl = get(f"{BASE}/packed", limit=1, **{"from": "2026-09-01", "to": "2026-09-03"})
check("packed list: more than the limit -> flagged, not silently cut", len(pl["packed"]) == 1, str(pl))

r = client.delete(f"{BASE}/orders/5007/packing")
check("undo: 200 and the order waits again", r.status_code == 200
      and r.json()["state"] == "waiting", r.text)
r = client.delete(f"{BASE}/orders/5007/packing")
check("undo twice -> 404", r.status_code == 404)
code, s = get(f"{BASE}/summary")
check("after undo: back to 6 orders", s["orders"] == 6, str(s))


# ===========================================================================
# 6b. Workers: who picks, who packs
# ===========================================================================
check("names are cleaned: trimmed, spaces collapsed, duplicates (any case) dropped",
      storage.clean_workers(["  Sam ", "ahmed  al  x", "SAM", "", "Ahmed al x"])
      == ["Sam", "ahmed al x"])
code, r = put(f"{BASE}/pickers?from=2026-09-02&to=2026-09-02", {"workers": ["Sam", " Ahmed "]})
check("pickers: saved for the dates", code == 200 and r["workers"] == ["Sam", "Ahmed"], str(r))
code, it = get(f"{BASE}/items")
check("pickers: returned with the pick list of those dates", it["pickers"] == ["Sam", "Ahmed"], str(it.get("pickers")))
code, it2 = get(f"{BASE}/items", **{"from": "2026-09-01", "to": "2026-09-02"})
check("pickers: other dates have their own (none)", it2["pickers"] == [])
code, r = put(f"{BASE}/pickers?from=2026-09-02&to=2026-09-02", {"workers": ["Jordan"]})
code, it = get(f"{BASE}/items")
check("pickers: replaced, not added to", it["pickers"] == ["Jordan"], str(it["pickers"]))
code, r = put(f"{BASE}/pickers?from=2026-09-02&to=2026-09-02", {"workers": []})
code, it = get(f"{BASE}/items")
check("pickers: an empty list clears them", it["pickers"] == [])
code, r = put(f"{BASE}/pickers?from=2026-09-02&to=2026-09-02", {"workers": [f"W{i}" for i in range(11)]})
check("pickers: more than 10 names -> 422", code == 422, str(code))
code, r = put(f"{BASE}/pickers?from=2026-09-02&to=2026-09-02", {"workers": ["x" * 41]})
check("pickers: a name over 40 characters -> 422", code == 422, str(code))

code, r = put(f"{BASE}/orders/assign", {"order_ids": [5001, 5004], "workers": ["Sam", "Ahmed"]})
check("assign: several orders at once", code == 200 and r["order_ids"] == [5001, 5004], str(r))
code, r = put(f"{BASE}/orders/assign", {"order_ids": [5005], "workers": ["Jordan"]})
code, o = get(f"{BASE}/orders")
w = {x["id"]: x["workers"] for t in o["tables"].values() for x in t}
check("assign: each order carries its packers in the order tables",
      w[5001] == ["Sam", "Ahmed"] and w[5004] == ["Sam", "Ahmed"] and w[5005] == ["Jordan"]
      and w[5002] == [], str(w))
code, d = get(f"{BASE}/orders/5005")
check("assign: and on the order's own page", d["workers"] == ["Jordan"], str(d.get("workers")))
code, r = put(f"{BASE}/orders/assign", {"order_ids": [424242], "workers": ["X"]})
check("assign: an unknown order -> 404", code == 404, str(code))
code, r = put(f"{BASE}/orders/assign", {"order_ids": [5004], "workers": []})
code, d = get(f"{BASE}/orders/5004")
check("assign: an empty list clears them", d["workers"] == [], str(d["workers"]))

code, r = post(f"{BASE}/orders/5005/approve", {"boxes": 1, "scans": ["1001", "1002"]})
check("approve: without names, the assigned packers are recorded",
      code == 200 and r["packing"]["packed_by"] == ["Jordan"], str(r.get("packing")))
code, r = post(f"{BASE}/orders/5001/approve", {"boxes": 1, "scans": ["1001"],
                                               "workers": ["Ahmed", " ahmed "]})
check("approve: names sent with Approve win, cleaned",
      code == 200 and r["packing"]["packed_by"] == ["Ahmed"], str(r.get("packing")))
code, p = get(f"{BASE}/packed")
pb = {x["id"]: x["packed_by"] for x in p["packed"]}
check("packed list shows who packed each order", pb == {5005: ["Jordan"], 5001: ["Ahmed"]}, str(pb))
code, r = put(f"{BASE}/orders/assign", {"order_ids": [5005], "workers": ["Someone else"]})
code, d = get(f"{BASE}/orders/5005")
check("changing the assignment later does not rewrite who packed it",
      d["packing"]["packed_by"] == ["Jordan"] and d["workers"] == ["Someone else"], str(d))
code, kw = get(f"{BASE}/workers")
check("workers: every name used, once each (suggestions)",
      sorted(kw["workers"]) == ["Ahmed", "Jordan", "Sam", "Someone else"], str(kw))
for oid in (5005, 5001):
    client.delete(f"{BASE}/orders/{oid}/packing")


# ===========================================================================
# 6c. Unique piece labels (2026-09-29)
# ===========================================================================
# Waiting now: 5004 (Doll 1 x2, line 1), 5001 (Doll 1 x1), 5002 (Doll 2 x1).
code, r = post(f"{BASE}/orders/5004/test-labels", {})
labels = [x["barcode"] for x in r["labels"]]
check("test labels: one per piece, unique, for the right item",
      code == 200 and len(set(labels)) == 2
      and all(x["sku"] == "1001" and x["name"] == "Doll 1" and not x["packed"] for x in r["labels"]),
      str(r))
code, r = post(f"{BASE}/orders/5004/test-labels", {})
check("test labels: pressing again gives the same labels",
      sorted(x["barcode"] for x in r["labels"]) == sorted(labels), str(r))
L1, L2 = labels
code, r = get(f"{BASE}/labels/ {L1.lower()} ")
check("label lookup: case and spaces ignored", code == 200 and r["sku"] == "1001"
      and r["packed_order_id"] is None, str(r))
code, _ = get(f"{BASE}/labels/NOPE123")
check("label lookup: unknown -> 404", code == 404, str(code))

code, r = post(f"{BASE}/orders/5004/approve", {"boxes": 1, "scans": [L1, L1]})
check("approve: the same label twice -> 422", code == 422
      and any("more than once" in p for p in r["detail"]["problems"]), str(r))
code, r = post(f"{BASE}/orders/5004/approve", {"boxes": 1, "scans": [L1, "1001"]})
check("approve: a label + an item-number scan -> packed, the label recorded",
      code == 200 and r["packing"]["labels"] == [{"barcode": L1, "sku": "1001"}], str(r))
with connect() as conn:
    items = conn.execute("SELECT items FROM packed_orders WHERE order_id = 5004").fetchone()[0]
check("approve: the packing record lists the label on its line",
      items[0]["labels"] == [L1], str(items))

code, r = post(f"{BASE}/orders/5001/approve", {"boxes": 1, "scans": [L1]})
check("approve: a label packed in another order -> 422, naming that order", code == 422
      and any("already packed in order #5004" in p for p in r["detail"]["problems"]), str(r))
code, r = post(f"{BASE}/orders/5002/approve", {"boxes": 1, "scans": [L2]})
check("approve: a label of another item -> 422", code == 422, str(r))
code, r = post(f"{BASE}/orders/5001/approve", {"boxes": 1, "scans": [L2]})
check("approve: any free piece of the item can go into any order", code == 200, str(r))

code, r = post(f"{BASE}/orders/5004/test-labels", {})
now = [x["barcode"] for x in r["labels"]]
check("test labels: a label packed elsewhere is replaced by a new one",
      len(now) == 2 and L1 in now and L2 not in now, str(r))

client.delete(f"{BASE}/orders/5004/packing")
code, r = get(f"{BASE}/labels/{L1}")
check("undo packing frees its labels", r["packed_order_id"] is None, str(r))
client.delete(f"{BASE}/orders/5001/packing")


# ===========================================================================
# 6d. OTO sync of BOL numbers, TEST mode (2026-09-29)
# ===========================================================================
code, o = get(f"{BASE}/orders")
listed = [x for size in storage.SIZES for x in o["tables"][size]]
check("orders: every order has a bol_no field, empty before any sync",
      listed and all(x["bol_no"] is None for x in listed), str(listed[:1]))
code, r = post(f"{BASE}/oto/sync", {})
check("oto sync: test mode, one number per order on screen",
      code == 200 and r["mode"] == "test" and r["updated"] == r["orders"] == r["with_bol"] == len(listed), str(r))
code, o = get(f"{BASE}/orders")
bols = {x["id"]: x["bol_no"] for size in storage.SIZES for x in o["tables"][size]}
check("orders: the BOL numbers show, shaped TEST-##########",
      all(b and b.startswith("TEST-") and len(b) == 15 for b in bols.values()), str(bols))
code, r = post(f"{BASE}/oto/sync", {})
code, o = get(f"{BASE}/orders")
again = {x["id"]: x["bol_no"] for size in storage.SIZES for x in o["tables"][size]}
check("oto sync again: test numbers stay as they were", r["updated"] == 0 and again == bols, str(r))
code, r = client.post(f"{BASE}/oto/sync", params={**DAY, "older": "true"}).status_code, None
code2, older = get(f"{BASE}/orders", older="true")
older_bols = [x["bol_no"] for size in storage.SIZES for x in older["tables"][size]]
check("oto sync (older): the older waiting orders get numbers too",
      code == 200 and older_bols and all(older_bols), str(older_bols))
some = next(iter(bols))
code, d = get(f"{BASE}/orders/{some}")
check("order page carries the BOL number", d["bol_no"] == bols[some], str(d.get("bol_no")))
code, r = post(f"{BASE}/orders/{some}/approve", {"boxes": 1, "manual": [x["line_item_id"] for x in d["items"] if not x["sku"]],
                                               "scans": [x["sku"] for x in d["items"] if x["sku"] for _ in range(x["quantity"])]})
code, p = get(f"{BASE}/packed")
check("packed list shows the synced BOL number",
      any(x["id"] == some and x["bol_no"] == bols[some] for x in p["packed"]), str(p["packed"][:2]))
client.delete(f"{BASE}/orders/{some}/packing")


# ===========================================================================
# 6e. OTO for real (2026-09-30): reading orderDetails, with OTO faked
# ===========================================================================
from app import oto
# The shape of a real orderDetails reply (scripts/check_oto.py, order 10038),
# with the customer part as it comes, to prove it is not kept.
REAL = {"success": True, "orderId": "10038", "shipmentId": "AY00000000001", "packageCount": 1,
        "dcName": "Aymakan", "trackingURL": "https://aymakan.com/en/tracking/AY00000000001",
        "status": "delivered", "customer": {"name": "x", "mobile": "0500000000"}}
check("oto parse: BOL, boxes, carrier, tracking link; nothing else kept",
      oto.parse_order(REAL) == {"bol_no": "AY00000000001", "boxes": 1, "carrier": "Aymakan",
                                "tracking_url": "https://aymakan.com/en/tracking/AY00000000001"},
      str(oto.parse_order(REAL)))
check("oto parse: no shipment yet, but a box count -> kept without a BOL",
      oto.parse_order({"success": True, "orderId": "1", "packageCount": 2}) ==
      {"bol_no": None, "boxes": 2, "carrier": None, "tracking_url": None})
check("oto parse: unknown order / failure -> None",
      oto.parse_order({"success": False}) is None and oto.parse_order(None) is None
      and oto.parse_order({"success": True, "orderId": "1"}) is None)
check("oto parse: a tracking link that is not a web address is dropped",
      oto.parse_order({**REAL, "trackingURL": "javascript:alert(1)"})["tracking_url"] is None)

# Pretend OTO answers for two of the waiting orders, one of them without a shipment.
code, o = get(f"{BASE}/orders")
nums = {x["id"]: x["number"] for size in storage.SIZES for x in o["tables"][size]}
a, b = sorted(nums)[:2]
fake_reply = {nums[a]: {"bol_no": "AY1", "boxes": 2, "carrier": "Aymakan",
                        "tracking_url": "https://aymakan.com/en/tracking/AY1"},
              nums[b]: {"bol_no": None, "boxes": 1, "carrier": None, "tracking_url": None}}
real_fetch, real_mode = oto.fetch_orders, storage.BOL_MODE
oto.fetch_orders = lambda numbers: ({n: fake_reply[n] for n in numbers if n in fake_reply}, 1)
storage.BOL_MODE = "oto"
try:
    code, r = post(f"{BASE}/oto/sync", {})
    check("oto sync (real mode): stores what OTO has, reports failures",
          code == 200 and r["mode"] == "oto" and r["updated"] == 2 and r["failed"] == 1, str(r))
    code, o = get(f"{BASE}/orders")
    rows = {x["id"]: x for size in storage.SIZES for x in o["tables"][size]}
    check("orders: BOL with carrier and tracking link from OTO",
          rows[a]["bol_no"] == "AY1" and rows[a]["carrier"] == "Aymakan"
          and rows[a]["tracking_url"].startswith("https://"), str(rows[a]))
    check("orders: no shipment in OTO yet -> no BOL", rows[b]["bol_no"] is None, str(rows[b]))
    code, d = get(f"{BASE}/orders/{a}")
    check("order page: OTO's box count, for the scan screen check",
          d["oto"] == {"boxes": 2, "carrier": "Aymakan",
                       "tracking_url": "https://aymakan.com/en/tracking/AY1"}, str(d["oto"]))
    code, r = post(f"{BASE}/oto/sync", {})
    check("oto sync again: nothing changed in OTO -> nothing updated", r["updated"] == 0, str(r))
    def broken(numbers): raise oto.OtoError("OTO refused the refresh token (HTTP 401)")
    oto.fetch_orders = broken
    code, r = post(f"{BASE}/oto/sync", {})
    check("oto sync: OTO unusable -> 502 with the reason", code == 502 and "401" in r["detail"], str(r))
finally:
    oto.fetch_orders, storage.BOL_MODE = real_fetch, real_mode


# ===========================================================================
# 6f. Loading onto the truck -> Orders being shipped (2026-09-30)
# ===========================================================================
code, d = get(f"{BASE}/orders/5008")
bol = d["bol_no"]
check("truck: the order to load has a BOL", d["state"] == "waiting" and bool(bol), str(d.get("bol_no")))
code, _ = post(f"{BASE}/orders/5008/approve", {"boxes": 2, "scans": ["5026"] * 3})
code, lo = get(f"{BASE}/loading")
check("truck list: the packed order, 0 of 2 boxes loaded",
      any(o["id"] == 5008 and o["loaded"] == 0 and o["boxes"] == 2 for o in lo["orders"]), str(lo))
# "N packed today" on the Packed orders button counts only orders still on
# that list: a fully loaded order leaves it (2026-10-01).
packed_today = lambda: get(f"{BASE}/summary")[1]["packed_today"]
n_packed = packed_today()
check("packed today: includes 5008, packed just now", n_packed >= 1, str(n_packed))

code, r = post(f"{BASE}/load", {"code": f"  {bol.lower()} "})
check("truck: first box scanned (case/spaces ignored) -> 1 of 2, not shipped yet",
      code == 200 and r["id"] == 5008 and r["loaded"] == 1 and not r["shipped"], str(r))
code, p = get(f"{BASE}/packed")
check("packed list: still there, showing 1 box loaded",
      any(x["id"] == 5008 and x["loaded"] == 1 for x in p["packed"]), str(p["packed"]))
r = client.delete(f"{BASE}/orders/5008/packing")
check("undo packing refused once a box is on the truck", r.status_code == 409, r.text)
check("packed today: a partly loaded order still counts", packed_today() == n_packed)

code, r = post(f"{BASE}/load", {"code": bol})
check("truck: second box -> all loaded, being shipped", code == 200 and r["loaded"] == 2 and r["shipped"], str(r))
code, p = get(f"{BASE}/packed")
check("packed list: the order has left it", all(x["id"] != 5008 for x in p["packed"]), str(p["packed"]))
code, lo = get(f"{BASE}/loading")
check("truck list: gone from it too", all(o["id"] != 5008 for o in lo["orders"]), str(lo))
check("packed today: one fewer once all its boxes are on the truck", packed_today() == n_packed - 1,
      f"{packed_today()} vs {n_packed}")
code, sh = get(f"{BASE}/shipping")
check("orders being shipped: there, loaded on 3 Sept (this month by default)",
      [x["id"] for x in sh["shipped"]] == [5008] and sh["shipped"][0]["boxes"] == 2
      and sh["shipped"][0]["shipped_local"].startswith("2026-09-03") and sh["range"]["is_default"], str(sh))
code, d = get(f"{BASE}/orders/5008")
check("order page: loaded 2, on the truck since…",
      d["packing"]["loaded"] == 2 and d["packing"]["shipped_at_utc"], str(d["packing"]))

code, r = post(f"{BASE}/load", {"code": bol})
check("truck: a third scan -> 409 all loaded (same box twice?)",
      code == 409 and r["detail"]["reason"] == "all_loaded" and r["detail"]["boxes"] == 2, str(r))
code, r = post(f"{BASE}/load", {"code": "NO-SUCH-BOL"})
check("truck: unknown BOL -> 404", code == 404 and r["detail"]["reason"] == "unknown", str(r))
code, o = get(f"{BASE}/orders")
waiting_bol = next(x["bol_no"] for size in storage.SIZES for x in o["tables"][size] if x["bol_no"])
code, r = post(f"{BASE}/load", {"code": waiting_bol})
check("truck: BOL of an order not packed yet -> 409 not packed",
      code == 409 and r["detail"]["reason"] == "not_packed", str(r))
code, sa = get(f"{BASE}/shipping", **{"from": "2026-08-01", "to": "2026-08-31"})
check("orders being shipped: August -> none", sa["shipped"] == [], str(sa))

r = client.delete(f"{BASE}/orders/5008/loading")
check("undo loading: back in packed orders, 0 loaded",
      r.status_code == 200 and r.json()["packing"]["loaded"] == 0
      and r.json()["packing"]["shipped_at_utc"] is None, r.text)
code, p = get(f"{BASE}/packed")
check("packed list: the order is back", any(x["id"] == 5008 and x["loaded"] == 0 for x in p["packed"]))
check("packed today: counts it again after undo loading", packed_today() == n_packed)
r = client.delete(f"{BASE}/orders/5008/loading")
check("undo loading twice -> 404", r.status_code == 404)
client.delete(f"{BASE}/orders/5008/packing")


# ===========================================================================
# 7. The page is served
# ===========================================================================
r = client.get("/storage")
check("/storage serves the app page", r.status_code == 200
      and "/static/storage.js" in r.text, str(r.status_code))
check("/storage is revalidated on every load", r.headers.get("cache-control") == "no-cache")
for asset in ("storage.js", "storage.css", "i18n.js", "barcode.js"):
    r = client.get(f"/static/{asset}")
    check(f"/static/{asset} is served", r.status_code == 200, str(r.status_code))


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("All storage checks passed.")
