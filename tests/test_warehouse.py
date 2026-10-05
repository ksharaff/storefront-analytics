"""The dashboard metrics added on 2026-09-30: the Warehouse tab (shipped
today ÷ ready today, packed → truck time, packing backlog, packed and
shipped per day, carriers with delivery time), sales by hour and weekday,
cancellations by payment method, and the OTO sync (with a fake OTO): every
OTO shipment, delivery time picked up → delivered. The Customers tab (new vs
returning) and the shipping cost were removed on 2026-10-01; the checks
below make sure they stay gone.

    POSTGRES_DB=storefront_analytics_test python -m tests.test_warehouse

Needs the storage tables (db/03, 05, 06, 07, 08) and db/09_oto_tracking.sql
(oto_orders) in the test database. Same rules as the other test files: refuses to run
unless POSTGRES_DB ends in `_test`, fixtures rebuilt on every run.

The clock is frozen at 30 Sep 2026 10:00 UTC = 13:00 in Riyadh. Riyadh's
30 September began at 29 Sep 21:00 UTC, so several fixtures sit on that
boundary on purpose.
"""
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app import config

if not config.POSTGRES_DB.endswith("_test"):
    sys.exit(f"Refusing to run against database {config.POSTGRES_DB!r}: "
             "POSTGRES_DB must end in '_test'.")

from fastapi.testclient import TestClient

import app.main as main
from app import oto
from app.db import connect
from scripts import sync_oto_tracking

FAILURES = []
def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond: FAILURES.append(name)

UTC = timezone.utc
def utc(*a): return datetime(*a, tzinfo=UTC)
NOW = utc(2026, 9, 30, 10, 0)

with connect() as conn:
    conn.execute("TRUNCATE orders, packed_orders, box_loads, order_bol, oto_orders CASCADE")

def add_order(oid, status, created, email=None, method="hyperpay_applepay", total=100,
              title=None):
    with connect() as conn:
        conn.execute("""INSERT INTO orders (id, number, status, total, payment_method,
                            payment_method_title, billing_email, date_created_gmt, created_via, raw)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'checkout', '{}')""",
                     (oid, str(oid), status, total, method, title or f"title of {method}",
                      email, created))

def pack(oid, packed, shipped=None, boxes=1, loads=0, carrier=None):
    with connect() as conn:
        conn.execute("""INSERT INTO packed_orders (order_id, boxes, items, packed_at, shipped_at)
                        VALUES (%s, %s, '[]', %s, %s)""", (oid, boxes, packed, shipped))
        for _ in range(loads):
            conn.execute("INSERT INTO box_loads (order_id, scanned_code, loaded_at) VALUES (%s, 'X', %s)",
                         (oid, packed))
        if carrier:
            conn.execute("""INSERT INTO order_bol (order_id, bol_no, carrier, boxes, source)
                            VALUES (%s, %s, %s, %s, 'oto')""", (oid, f"BOL{oid}", carrier, boxes))

def track(number, created, carrier=None, charge=None, picked=None, delivered=None, status=None):
    """One row of OTO data (oto_orders), as the sync would store it."""
    with connect() as conn:
        conn.execute("""INSERT INTO oto_orders (order_number, carrier, created_at, status, charge,
                                                picked_up_at, delivered_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                     (str(number), carrier, created, status, charge, picked, delivered))


# ===========================================================================
# 1. Fixtures: packed and shipped orders
# ===========================================================================
for oid, total in [(101, 400), (102, 300), (103, 100), (104, 100), (105, 100),
                   (106, 500), (107, 100)]:
    add_order(oid, "processing", utc(2026, 9, 20, 9), total=total)
# 101: packed yesterday 22:00 Riyadh, loaded today → shipped today, 11 h on the shelf
pack(101, utc(2026, 9, 29, 19), utc(2026, 9, 30, 6), carrier="Aymakan")
# 102: packed and loaded today, 2 h
pack(102, utc(2026, 9, 30, 5), utc(2026, 9, 30, 7), carrier="Aymakan")
# 103: packed today, 1 of its 2 boxes loaded → waiting (packed today), partly loaded
pack(103, utc(2026, 9, 30, 6), boxes=2, loads=1)
# 104: packed on the 27th, never loaded → waiting (older), forgotten, 3 days old
pack(104, utc(2026, 9, 27, 8))
# 105: 29 Sep 22:00 UTC is 30 Sep 01:00 Riyadh → packed TODAY
pack(105, utc(2026, 9, 29, 22))
# 106: loaded 29 Sep 23:00 Riyadh → shipped YESTERDAY, not part of today's number
pack(106, utc(2026, 9, 25, 10), utc(2026, 9, 29, 20), carrier="Aramex")
# 107: packed and loaded just after Riyadh midnight → shipped today, no carrier known
pack(107, utc(2026, 9, 29, 21, 30), utc(2026, 9, 29, 21, 45))
# An automated-test order, packed and shipped today: never counted.
add_order(199, "processing", utc(2026, 9, 30, 1), title="E2E automated verification (no real payment)")
pack(199, utc(2026, 9, 30, 2), utc(2026, 9, 30, 3), carrier="Aymakan")

# OTO data, independent of the storage app. The period is decided by when
# OTO created the shipment (Riyadh days).
# 106: Aramex, picked up → delivered in 12 h, cost 25 on 500 (5%)
track(106, utc(2026, 9, 25, 10), "Aramex", Decimal("25.00"),
      utc(2026, 9, 29, 20), utc(2026, 9, 30, 8), "delivered")
# 903: Aramex, delivered but OTO never said "picked up": delivered, not timed
track(903, utc(2026, 9, 10, 9), "Aramex", None, None, utc(2026, 9, 12, 9), "delivered")
# 101: no company given, picked up, not delivered yet, cost 20 on 400 (5%)
track(101, utc(2026, 9, 29, 19), None, Decimal("20.00"), utc(2026, 9, 30, 6), None, "pickedUp")
# 900: Aymakan, 24 h, cost 30; NOT in our orders table, so it counts for the
# average cost and time but not for cost ÷ order value
track(900, utc(2026, 9, 28, 10), "Aymakan", Decimal("30.00"),
      utc(2026, 9, 28, 12), utc(2026, 9, 29, 12), "delivered")
# 901: 31 Aug 23:00 Riyadh → August, outside September
track(901, utc(2026, 8, 31, 20), "Aymakan", Decimal("40.00"))
# 902: 1 Sep 00:30 Riyadh → September; no cost yet
track(902, utc(2026, 8, 31, 21, 30), "Aymakan")
# 199 is the E2E test order: never counted
track(199, utc(2026, 9, 30, 1), "Aymakan", Decimal("99.00"))

# Waiting to be PACKED (backlog), by Riyadh days since placed.
add_order(201, "processing", utc(2026, 9, 30, 8))       # today
add_order(205, "processing", utc(2026, 9, 29, 21, 10))  # 00:10 Riyadh on the 30th → today
add_order(202, "processing", utc(2026, 9, 29, 20, 30))  # 23:30 Riyadh on the 29th → 1 day
add_order(203, "processing", utc(2026, 9, 28, 10))      # 2 days
add_order(204, "processing", utc(2026, 9, 26, 10))      # 4 days
add_order(206, "delivered", utc(2026, 9, 26, 10))       # not waiting: already delivered
add_order(207, "processing", utc(2026, 9, 26, 10),
          title="E2E automated verification (no real payment)")   # test order


main.clock = lambda: NOW
client = TestClient(main.app)
def get(path, **params):
    r = client.get(path, params=params)
    return r.status_code, r.json()


# ===========================================================================
# 2. Warehouse tab
# ===========================================================================
code, w = get("/api/warehouse", period="current_month")
check("warehouse returns 200", code == 200, str(code) if code == 200 else str(w))
t = w["today"]
check("shipped today: 101, 102 and 107 (107 just after Riyadh midnight; 106 was yesterday)",
      t["shipped"] == 3, str(t))
check("still waiting: 2 packed today (103, and 105 at 01:00 Riyadh) + 1 older (104)",
      (t["waiting_today"], t["waiting_older"], t["waiting"]) == (2, 1, 3), str(t))
check("ready today = shipped today + waiting = 6, rate 50%",
      t["ready"] == 6 and t["rate_pct"] == 50.0, str(t))
check("the E2E test order is not counted", t["shipped"] == 3 and t["ready"] == 6)
check("oldest waiting: packed 3 days ago (27 Sep)", t["oldest_days"] == 3
      and t["oldest_packed_local"] == "2026-09-27T11:00:00", str(t))
check("over 24 h on the shelf: 104 only", t["forgotten"] == 1 and t["forgotten_hours"] == 24, str(t))
check("partly loaded: 103 (1 of 2 boxes)", t["partly_loaded"] == 1, str(t))

tt = w["truck_time"]
check("packed → truck this month: 4 orders (101 11h, 102 2h, 107 0.25h, 106 106h)",
      tt["orders"] == 4 and tt["median_hours"] == 6.5 and tt["max_hours"] == 106.0, str(tt))
check("…one of them over 24 h (106)", tt["over_limit"] == 1, str(tt))
_, wt = get("/api/warehouse", period="today")
check("packed → truck today: 3 orders, median 2 h, average 4.4 h",
      wt["truck_time"]["orders"] == 3 and wt["truck_time"]["median_hours"] == 2.0
      and wt["truck_time"]["avg_hours"] == 4.4, str(wt["truck_time"]))
check("the 'right now' numbers ignore the period",
      wt["today"] == w["today"] and wt["backlog"] == w["backlog"])

b = w["backlog"]
check("backlog: 2 placed today (incl. 00:10 Riyadh), 2 one–two days, 1 three+ days",
      (b["today"], b["one_two"], b["three_plus"], b["total"]) == (2, 2, 1, 5), str(b))
check("backlog leaves out packed, delivered and test orders; oldest 4 days",
      b["oldest_days"] == 4, str(b))

pd = {d["day"]: d for d in w["per_day"]}
check("per day: every day of September so far, zeros included", len(w["per_day"]) == 30
      and pd["2026-09-10"] == {"day": "2026-09-10", "packed": 0, "shipped": 0}, str(len(w["per_day"])))
check("per day: 30 Sep packed 4 (102, 103, 105, 107), shipped 3",
      (pd["2026-09-30"]["packed"], pd["2026-09-30"]["shipped"]) == (4, 3), str(pd["2026-09-30"]))
check("per day: 29 Sep packed 1 (101), shipped 1 (106, 23:00 Riyadh)",
      (pd["2026-09-29"]["packed"], pd["2026-09-29"]["shipped"]) == (1, 1), str(pd["2026-09-29"]))
check("per day totals match", sum(d["packed"] for d in w["per_day"]) == 7
      and sum(d["shipped"] for d in w["per_day"]) == 4)

cr = w["carriers"]
check("carriers from OTO: Aramex 2, Aymakan 2, unknown 1 (not 901 of August, not the E2E 199)",
      [(c["carrier"], c["orders"]) for c in cr] == [("Aramex", 2), ("Aymakan", 2), (None, 1)], str(cr))
ar, ay, unk = cr
check("Aramex: 2 delivered, 1 timed (12 h)",
      (ar["delivered"], ar["timed"], ar["avg_delivery_hours"]) == (2, 1, 12.0), str(ar))
check("Aymakan: 900 is not in our orders table but still counts (24 h)",
      (ay["delivered"], ay["timed"], ay["avg_delivery_hours"]) == (1, 1, 24.0), str(ay))
check("unknown company: 101, not delivered", unk["delivered"] == 0
      and unk["avg_delivery_hours"] is None, str(unk))
check("no shipping cost in the API any more (removed 2026-10-01)",
      not any(k in c for c in cr for k in ("cost", "avg_cost", "cost_pct", "with_cost")), str(cr[0]))
sh = w["shipping"]
check("totals: 5 orders, 3 delivered, 2 timed averaging 18 h, no cost",
      sh == {"orders": 5, "delivered": 3, "timed": 2, "avg_delivery_hours": 18.0}, str(sh))
_, aug = get("/api/warehouse", period="custom", start="2026-08-31", end="2026-08-31")
check("31 Aug (Riyadh) holds 901 only", [(c["carrier"], c["orders"]) for c in aug["carriers"]]
      == [("Aymakan", 1)], str(aug["carriers"]))
check("oto_synced_utc is reported", w["oto_synced_utc"] is not None)

_, empty = get("/api/warehouse", period="custom", start="2026-08-01", end="2026-08-02")
check("a period with no packing or OTO shipments: empty carriers, null times, zero days",
      empty["carriers"] == [] and empty["truck_time"]["median_hours"] is None
      and all(d["packed"] == 0 for d in empty["per_day"]) and len(empty["per_day"]) == 2)


# ===========================================================================
# 3. OTO: parsing and the sync script (fake OTO)
# ===========================================================================
# The status history, in the shape trackShipment documents (otoStatus,
# dcStatus, dcUpdateDate without a zone = Riyadh time).
details = {"orderId": "5", "status": "delivered", "statusHistory": [
    {"updateStatusDate": True, "dcStatus": "DATA RECEIVED", "otoStatus": "shipmentCreated",
     "dcUpdateDate": "2026-09-27T09:00:00"},
    {"dcStatus": "PICKED UP", "otoStatus": "pickedUp", "dcUpdateDate": "2026-09-28T10:00:00"},
    {"otoStatus": "outForDelivery", "dcUpdateDate": "2026-09-29T09:00:00"},
    {"dcStatus": "DELIVERED", "otoStatus": "delivered", "dcUpdateDate": "2026-09-29T15:30:00"}]}
h = oto.parse_history(details)
check("OTO history: picked up and delivered times, Riyadh time when no zone is given",
      h == {"status": "delivered", "picked_up_at": utc(2026, 9, 28, 7),
            "delivered_at": utc(2026, 9, 29, 12, 30)}, str(h))
h2 = oto.parse_history({"orderId": "6", "history": [
    {"status": "inTransit", "date": "2026-09-28 08:00:00"},
    {"status": "Delivered", "updatedAt": 1790000000000}]})
check("OTO history: no 'picked up' → first 'in transit'; epoch-ms times read",
      h2["picked_up_at"] == utc(2026, 9, 28, 5)
      and h2["delivered_at"] == datetime.fromtimestamp(1790000000, tz=UTC), str(h2))
check("OTO history: nothing known → None", oto.parse_history({"success": False}) is None
      and oto.parse_history({"orderId": "7"}) is None)
check("'out for delivery', 'undelivered', 'failed delivery' are not delivered",
      not oto.is_delivered("outForDelivery") and not oto.is_delivered("Undelivered")
      and not oto.is_delivered("Failed delivery") and oto.is_delivered("DELIVERED"))
check("'picked up' yes; 'ready for pickup', 'pickup requested', 'not picked up' no",
      oto.is_picked_up("pickedUp") and oto.is_picked_up("Picked Up")
      and not oto.is_picked_up("readyForPickup") and not oto.is_picked_up("pickupRequested")
      and not oto.is_picked_up("notPickedUp"))

page = {"success": True, "shipments": [
    {"orderId": "8", "shipmentNumber": "AY1", "shipmentCreationDate": "2026-09-28T10:00:00",
     "deliveryCompanyName": "Aymakan", "dcCharge": 22.5, "shipmentType": "forward", "status": "pickedUp"},
    {"orderId": "8", "shipmentNumber": "AY2", "shipmentCreationDate": "2026-09-29T10:00:00",
     "deliveryCompanyName": "Aymakan", "dcCharge": "7.25", "shipmentType": "forward", "status": "delivered"},
    {"orderId": "8", "shipmentNumber": "AYR", "shipmentCreationDate": "2026-09-30T10:00:00",
     "deliveryCompanyName": "Aymakan", "dcCharge": 15, "shipmentType": "return"},
    {"orderId": "9", "shipmentCreationDate": "2026-09-28T10:00:00", "dcCharge": "n/a",
     "status": "Cancelled"},
    {"orderId": "10", "deliveryCompanyName": "SMSA"}]}
ships, n_raw = oto.shipments_page(page)
check("shipment list: 5 raw entries, 4 usable (no date → dropped)", (len(ships), n_raw) == (4, 5))
per = oto.per_order(ships)
check("per order: forward charges added up (29.75), return and cancelled left out, "
      "first shipment's date, latest status",
      list(per) == ["8"] and per["8"]["charge"] == Decimal("29.75")
      and per["8"]["created_at"] == utc(2026, 9, 28, 7) and per["8"]["status"] == "delivered"
      and per["8"]["shipment_no"] == "AY2", str(per))
try:
    oto.shipments_page({"success": False, "message": "x"})
    check("a refused shipment list raises", False)
except oto.OtoError:
    check("a refused shipment list raises", True)

RAW = [
    {"orderId": "102", "shipmentNumber": "AY102", "shipmentCreationDate": "2026-09-30T08:00:00",
     "deliveryCompanyName": "Aymakan", "dcCharge": 18.5, "status": "pickedUp", "shipmentType": "forward"},
    {"orderId": "102", "shipmentNumber": "AY102R", "shipmentCreationDate": "2026-09-30T09:00:00",
     "deliveryCompanyName": "Aymakan", "dcCharge": 15, "shipmentType": "return"},
    {"orderId": "101", "shipmentNumber": "X101", "shipmentCreationDate": "2026-09-29T22:00:00",
     "dcCharge": None, "status": "inTransit"},
    {"orderId": "555", "shipmentCreationDate": "2026-09-20T10:00:00", "deliveryCompanyName": "SMSA",
     "dcCharge": "12.00", "status": "cancelled"},
    {"orderId": "556", "shipmentCreationDate": "2026-09-21T10:00:00", "deliveryCompanyName": "SMSA",
     "dcCharge": "11.00", "status": "returned"},
]
listed, asked, seen = [], [], []
def fake_list(first, last):
    listed.append((first, last))
    return [s for s in map(oto.parse_shipment, RAW) if s]
def fake_history(numbers):
    asked.extend(numbers)
    return {"102": {"status": "delivered", "picked_up_at": utc(2026, 9, 30, 8, 30),
                    "delivered_at": utc(2026, 9, 30, 9, 30)},
            "101": {"status": "outForDelivery", "picked_up_at": utc(2026, 9, 30, 7),
                    "delivered_at": None}}, 1
r = sync_oto_tracking.run(days=500, list_shipments=fake_list, fetch_history=fake_history,
                          now=NOW, progress=lambda d, n: seen.append((d, n)))
check("sync reads at most OTO's 90 days: 3 Jul to 30 Sep (Riyadh dates)",
      listed == [("2026-07-03", "2026-09-30")] and (r["first"], r["last"]) == ("2026-07-03", "2026-09-30"),
      str(listed))
check("sync asks for the history of undelivered orders only, not cancelled or returned ones "
      "(101, 102, 199, 901, 902; not 106/900/903 delivered, not 555/556)",
      sorted(asked) == ["101", "102", "199", "901", "902"], str(sorted(asked)))
check("sync result counts, progress reported",
      (r["orders"], r["asked"], r["delivered"], r["failed"]) == (3, 5, 1, 1) and seen == [(5, 5)], str(r))
with connect() as conn:
    rows = {x[0]: x[1:] for x in conn.execute(
        """SELECT order_number, carrier, charge, status, picked_up_at, delivered_at
           FROM oto_orders""").fetchall()}
check("sync stores 102: carrier, forward charge only (18.50), picked up and delivered",
      rows["102"] == ("Aymakan", Decimal("18.50"), "delivered", utc(2026, 9, 30, 8, 30),
                      utc(2026, 9, 30, 9, 30)), str(rows.get("102")))
check("101 keeps its known cost (20.00) and first pickup time; status updated",
      rows["101"] == (None, Decimal("20.00"), "outForDelivery", utc(2026, 9, 30, 6), None),
      str(rows.get("101")))
check("a cancelled shipment is not stored; a returned order is (not asked)",
      "555" not in rows and rows["556"][2] == "returned")
asked.clear()
sync_oto_tracking.run(days=90, list_shipments=fake_list, fetch_history=fake_history, now=NOW)
check("delivered orders are not asked again (102)", "102" not in asked and "101" in asked, str(asked))
before = rows
def broken_list(first, last):
    raise oto.OtoError("HTTP 500 reading shipments, page 3")
try:
    sync_oto_tracking.run(list_shipments=broken_list, fetch_history=fake_history, now=NOW)
    check("a failed shipment list stops the sync", False)
except oto.OtoError:
    with connect() as conn:
        n = conn.execute("SELECT count(*) FROM oto_orders").fetchone()[0]
    check("a failed shipment list stops the sync and writes nothing", n == len(before), str(n))


# ===========================================================================
# 4. August fixtures for the heat map and cancellations (clear of the above)
# ===========================================================================
add_order(301, "delivered", utc(2026, 7, 5, 9), "Old@X.com ", total=80)
add_order(302, "delivered", utc(2026, 8, 12, 9), "old@x.com", total=100)
add_order(303, "delivered", utc(2026, 8, 12, 10), "new@x.com", "tamara-gateway-pay-in-3", total=200)
add_order(304, "on-the-way", utc(2026, 8, 15, 21, 30), "new@x.com", "tamara-gateway-checkout", total=50)
add_order(305, "cancelled", utc(2026, 7, 1, 9), "c@x.com", total=60)
add_order(306, "delivered", utc(2026, 8, 14, 9), "c@x.com", "hyperpay_mada", total=70)
add_order(307, "delivered", utc(2026, 8, 14, 9), "", total=30)
add_order(308, "delivered", utc(2026, 8, 16, 9), "old2@x.com", total=40)
add_order(300, "delivered", utc(2026, 7, 9, 9), "old2@x.com", total=1,
          title="E2E automated verification (no real payment)")
# cancellations and non-orders in the same window
add_order(309, "tamara-p-canceled", utc(2026, 8, 13, 9), "x@x.com", "tamara-gateway-checkout")
add_order(310, "cancelled", utc(2026, 8, 13, 9), "y@x.com", "hyperpay_applepay")
add_order(311, "failed", utc(2026, 8, 13, 10), "z@x.com", "hyperpay_applepay")
add_order(312, "pending", utc(2026, 8, 13, 10), "z@x.com", "hyperpay_mada")

AUG = {"period": "custom", "start": "2026-08-10", "end": "2026-08-20"}
code, _ = get("/api/customers", **AUG)
check("the Customers tab's endpoint is gone (removed 2026-10-01)", code == 404, str(code))


# ===========================================================================
# 5. Sales by hour and weekday
# ===========================================================================
code, s = get("/api/sales", **AUG)
hw = {(x["weekday"], x["hour"]): x for x in s["by_hour_weekday"]}
check("heat map: 304 at 21:30 UTC Sat 15 Aug is Sunday 00:30 in Riyadh → (0, 0)",
      hw.get((0, 0), {}).get("orders") == 1 and hw[(0, 0)]["sales"] == 50.0, str(hw.get((0, 0))))
check("heat map: 302 and 303, Wed 12 Aug 12:00 and 13:00 Riyadh",
      hw.get((3, 12), {}).get("orders") == 1 and hw.get((3, 13), {}).get("orders") == 1, str(sorted(hw)))
check("heat map: sale orders only (6: 302, 303, 304, 306, 307, 308)",
      sum(x["orders"] for x in s["by_hour_weekday"]) == 6)
check("heat map total equals total sales",
      round(sum(x["sales"] for x in s["by_hour_weekday"]), 2) == s["kpis"]["sales"]["value"])


# ===========================================================================
# 6. Cancellations by payment method
# ===========================================================================
code, p = get("/api/payments", **AUG)
cx = p["cancellations"]
bm = {(x["gateway"], x["pos"]): x for x in cx["by_method"]}
check("cancellations: 8 orders placed (failed payment and pending left out), 2 cancelled, 25%",
      (cx["orders"], cx["cancelled"], cx["rate_pct"]) == (8, 2, 25.0), str(cx))
check("Tamara: 3 orders, 1 cancelled (33.3%), codes grouped",
      (bm[("tamara", False)]["orders"], bm[("tamara", False)]["cancelled"],
       bm[("tamara", False)]["rate_pct"]) == (3, 1, 33.3), str(bm.get(("tamara", False))))
check("Apple Pay: 302, 307, 308, 310 → 4 orders, 1 cancelled (the failed 311 is not an order)",
      (bm[("applepay", False)]["orders"], bm[("applepay", False)]["cancelled"]) == (4, 1),
      str(bm.get(("applepay", False))))
check("mada: 1 order, 0 cancelled, 0%", bm[("mada", False)]["rate_pct"] == 0.0)
ot = cx["over_time"]
pts = {x["bucket"][:10]: x for x in ot["points"]}
check("over time: daily, 11 days, 13 Aug = 2 of 2 cancelled",
      ot["granularity"] == "day" and len(ot["points"]) == 11
      and (pts["2026-08-13"]["orders"], pts["2026-08-13"]["cancelled"], pts["2026-08-13"]["rate_pct"])
      == (2, 2, 100.0), str(pts.get("2026-08-13")))
check("over time: a day with no orders has rate null, not 0", pts["2026-08-11"]["rate_pct"] is None)
_, py = get("/api/payments", period="this_year")
check("long periods are weekly", py["cancellations"]["over_time"]["granularity"] == "week")


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("All warehouse and new-metrics checks passed.")
