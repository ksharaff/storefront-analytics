"""Exercise the period logic and the metrics API against a real Postgres.

    $env:POSTGRES_DB="storefront_analytics_test"; python -m tests.test_metrics   # PowerShell
    POSTGRES_DB=storefront_analytics_test python -m tests.test_metrics           # bash

Same rules as the other test files: a real database, a refusal to run unless
POSTGRES_DB ends in `_test`, and fixtures truncated and rebuilt on every run.

The fixtures deliberately sit on the Riyadh/UTC boundary. An order placed at
21:30 UTC on 14 March is 00:30 on 15 March in Riyadh, so it belongs to
"today" on the 15th even though its UTC date says the 14th. Getting that
wrong is the single most likely bug in a dashboard like this.
"""
import json, sys
from datetime import date, datetime, timedelta, timezone

from app import config

if not config.POSTGRES_DB.endswith("_test"):
    sys.exit(
        f"Refusing to run against database {config.POSTGRES_DB!r}.\n"
        "These tests insert fixture orders. Point POSTGRES_DB at a database "
        "whose name ends in '_test' (see tests/test_backfill.py's docstring)."
    )

from fastapi.testclient import TestClient

import app.main as main
from app import periods
from app.db import connect, set_sync_state

FAILURES = []
def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond: FAILURES.append(name)

UTC = timezone.utc
def utc(*a): return datetime(*a, tzinfo=UTC)


# ===========================================================================
# 1. Periods (pure functions, no database)
# ===========================================================================
NOW = utc(2026, 3, 15, 12, 0)          # 15:00 in Riyadh, 15 March

p = periods.resolve("today", now=NOW)
check("today starts at Riyadh midnight (21:00 UTC the day before)",
      p.current.start == utc(2026, 3, 14, 21), str(p.current.start))
check("today ends now", p.current.end == NOW)
check("today compares to yesterday's same 15 hours",
      (p.previous.start, p.previous.end) == (utc(2026, 3, 13, 21), utc(2026, 3, 14, 12)),
      f"{p.previous.start} → {p.previous.end}")

late = periods.resolve("today", now=utc(2026, 3, 15, 22, 0))   # 01:00 on the 16th locally
check("after 21:00 UTC it is already tomorrow in Riyadh",
      late.current.start == utc(2026, 3, 15, 21), str(late.current.start))

p = periods.resolve("yesterday", now=NOW)
check("yesterday = the whole previous Riyadh day",
      (p.current.start, p.current.end) == (utc(2026, 3, 13, 21), utc(2026, 3, 14, 21)))

p = periods.resolve("last_7_days", now=NOW)
check("last 7 days = 7 complete days, excluding today",
      (p.current.start, p.current.end) == (utc(2026, 3, 7, 21), utc(2026, 3, 14, 21)))
check("last 7 days compares to the 7 before",
      (p.previous.start, p.previous.end) == (utc(2026, 2, 28, 21), utc(2026, 3, 7, 21)))

p = periods.resolve("current_month", now=NOW)
check("current month compares to the same elapsed span of last month (Feb 1–15, 15:00)",
      (p.previous.start, p.previous.end) == (utc(2026, 1, 31, 21), utc(2026, 2, 15, 12)),
      f"{p.previous.start} → {p.previous.end}")
p = periods.resolve("current_month", now=utc(2026, 3, 31, 12))
check("…capped at the month boundary (March 31 vs all of February, not into March)",
      p.previous.end == utc(2026, 2, 28, 21), str(p.previous.end))

p = periods.resolve("previous_month", now=NOW)
check("previous month = February in Riyadh",
      (p.current.start, p.current.end) == (utc(2026, 1, 31, 21), utc(2026, 2, 28, 21)))

p = periods.resolve("previous_month", now=utc(2026, 1, 15))
check("December 2025: before our data → incomplete", p.complete is False)
check("December 2025: no comparison", p.comparison_available is False)

p = periods.resolve("this_year", now=NOW)
check("this year vs last year: no comparison (2025 not imported)",
      p.comparison_available is False)
check("this year flags the 3 Riyadh hours before our UTC data start",
      p.complete is False)
p = periods.resolve("current_month", now=utc(2026, 9, 23, 12))
check("September vs August: comparison available", p.comparison_available is True)
check("September: complete", p.complete is True)

p = periods.resolve("custom", start=date(2026, 3, 10), end=date(2026, 3, 10), now=NOW)
check("custom single day is the whole Riyadh day (end inclusive)",
      (p.current.start, p.current.end) == (utc(2026, 3, 9, 21), utc(2026, 3, 10, 21)))
check("custom compares to the equal-length period just before",
      (p.previous.start, p.previous.end) == (utc(2026, 3, 8, 21), utc(2026, 3, 9, 21)))
for bad, why in [(("custom", None, None), "custom without dates"),
                 (("custom", date(2026, 3, 10), date(2026, 3, 9)), "end before start"),
                 (("next_week", None, None), "unknown period")]:
    try:
        periods.resolve(bad[0], start=bad[1], end=bad[2], now=NOW); ok = False
    except ValueError:
        ok = True
    check(f"rejects {why}", ok)


# ===========================================================================
# 2. Fixtures
# ===========================================================================
with connect() as conn:
    conn.execute("TRUNCATE orders, refunds, refund_items, "
                 "product_categories, sync_state CASCADE")

def add_order(oid, status, total, created, email, method, items=(), via="checkout"):
    """Insert an order directly. items: (line_id, product_id, name, qty, total, tax).
    `method` is a real payment code from the store; `via` is created_via."""
    with connect() as conn:
        conn.execute("""INSERT INTO orders (id, status, total, payment_method,
                            payment_method_title, billing_email, date_created_gmt,
                            created_via, raw)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'{}')""",
                     (oid, status, total, method, f"title of {method}" if method else None,
                      email, created, via))
        for line, pid, name, qty, tot, tax in items:
            conn.execute("""INSERT INTO order_items (order_id, line_item_id, product_id,
                                name, quantity, total, total_tax)
                            VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                         (oid, line, pid, name, qty, tot, tax))

def add_refund(rid, oid, amount, created, items=()):
    """items: (line_id, product_id, name, qty, total) — stored positive."""
    with connect() as conn:
        conn.execute("""INSERT INTO refunds (id, order_id, amount, date_created_gmt, raw)
                        VALUES (%s,%s,%s,%s,'{}')""", (rid, oid, amount, created))
        for line, pid, name, qty, tot in items:
            conn.execute("""INSERT INTO refund_items (refund_id, line_item_id, product_id,
                                name, quantity, total) VALUES (%s,%s,%s,%s,%s,%s)""",
                         (rid, line, pid, name, qty, tot))

# --- "today" (15 March, Riyadh) ---
# 1: 00:30 Riyadh on the 15th, although its UTC date is the 14th.
add_order(1, "delivered", 430, utc(2026, 3, 14, 21, 30), "a@x.com", "hyperpay_applepay",
          [(11, 500, "Duvet", 2, 348, 52)])
# 2: 11:00 Riyadh. Two products; 700 has no category mapping.
add_order(2, "on-the-way", 200, utc(2026, 3, 15, 8), "b@x.com", "tamara-gateway-pay-in-4",
          [(21, 600, "Towel", 1, 100, 15), (22, 700, "Soap", 3, 74, 11)])
add_order(3, "cancelled", 999, utc(2026, 3, 15, 9), "c@x.com", "hyperpay_applepay",
          [(31, 500, "Duvet", 5, 999, 0)])                   # not a sale
add_order(4, "tamara-p-failed", 300, utc(2026, 3, 15, 9), "d@x.com", "tamara-gateway-checkout")
add_order(5, "brand-new-status", 50, utc(2026, 3, 15, 9), "e@x.com", "tabby_installments")  # unclassified
add_order(8, "refunded", 150, utc(2026, 3, 15, 10), "f@x.com", "hyperpay_mada",
          [(81, 500, "Duvet", 1, 150, 0)])

# An automated-test order ("E2E automated verification"), today, as a sale
# with a product line. It must not count anywhere.
with connect() as conn:
    conn.execute("""INSERT INTO orders (id, status, total, payment_method, payment_method_title,
                        billing_email, date_created_gmt, created_via, raw)
                    VALUES (9, 'processing', 1.00, 'bacs',
                            'E2E automated verification (no real payment)',
                            'e2e@x.com', %s, 'rest-api', '{}')""", (utc(2026, 3, 15, 9, 30),))
    conn.execute("""INSERT INTO order_items (order_id, line_item_id, product_id, name, quantity, total, total_tax)
                    VALUES (9, 91, 999, 'Test product', 50, 1, 0)""")

# --- "yesterday" (14 March, Riyadh) ---
# 6: 23:30 Riyadh on the 14th: yesterday, but AFTER the same-time cutoff for
#    today's comparison (today has only run until 15:00).
add_order(6, "delivered", 100, utc(2026, 3, 14, 20, 30), "a@x.com", "hyperpay_applepay")
# 7: 09:00 Riyadh on the 14th: inside the same-span comparison window.
add_order(7, "delivered", 315, utc(2026, 3, 14, 6), "g@x.com", "hyperpay")

# --- 20 February: payment-method grouping cases, clear of every other window ---
add_order(20, "delivered", 80, utc(2026, 2, 20, 9), "p@x.com", "pos_cash", via="pos-rest-api")   # PoS
add_order(21, "delivered", 90, utc(2026, 2, 20, 9), "q@x.com", "hyperpay_applepay", via="pos-rest-api")  # PoS, card
add_order(22, "delivered", 50, utc(2026, 2, 20, 9), "r@x.com", "hyperpay_applepay", via="rest-api")  # API: still online
add_order(23, "cancelled", 40, utc(2026, 2, 20, 9), "s@x.com", "cod")    # disabled + no attempt: hidden

# Categories: product 500 sits in two categories on purpose.
with connect() as conn:
    conn.execute("""INSERT INTO product_categories (product_id, category_id, category_name)
                    VALUES (500,10,'Bedding'), (500,11,'Duvets'), (600,12,'Bath')""")

# Refunds issued today: order 8 fully, and a partial one on sale order 1.
add_refund(9001, 8, 150, utc(2026, 3, 15, 10, 30), [(1, 500, "Duvet", 1, 150)])
add_refund(9002, 1, 50,  utc(2026, 3, 15, 11), [(2, 500, "Duvet", 1, 50)])
add_refund(9003, 7, 20,  utc(2026, 3, 15, 11))   # amount-only: no line items

set_sync_state("last_reconciled_at", "2026-03-15T11:55:00")

main.clock = lambda: NOW
client = TestClient(main.app)
def get(path, **params):
    r = client.get(path, params=params)
    return r.status_code, r.json()


# ===========================================================================
# 3. Overview (home page)
# ===========================================================================
code, o = get("/api/overview", period="today")
k = o["kpis"]
check("overview returns 200", code == 200, str(code))
check("sales = sale-group orders only, Riyadh 'today' (430 + 200)",
      k["sales"]["value"] == 630.0, str(k["sales"]))
check("orders = sale orders", k["orders"]["value"] == 2, str(k["orders"]))
check("orders_all counts every order placed", k["orders_all"] == 6, str(k["orders_all"]))
check("AOV = 630 / 2", k["aov"]["value"] == 315.0, str(k["aov"]))
check("customers = distinct emails of sale orders", k["customers"]["value"] == 2)
check("previous = yesterday until 15:00 only (order 7, not order 6)",
      k["sales"]["previous"] == 315.0, str(k["sales"]))
check("change = +100%", k["sales"]["change_pct"] == 100.0, str(k["sales"]))

sbs = {x["status"]: (x["orders"], x["sales"]) for x in o["sales_by_status"]}
check("total sales is broken down by sale status",
      sbs == {"delivered": (1, 430.0), "on-the-way": (1, 200.0)}, str(sbs))
check("…and the parts add up to the Total sales KPI",
      sum(v[1] for v in sbs.values()) == k["sales"]["value"])
code, s_ = get("/api/sales", period="today")
check("the sales section carries the same breakdown", s_["sales_by_status"] == o["sales_by_status"])

pts = o["sales_over_time"]["points"]
check("today's chart is hourly", o["sales_over_time"]["granularity"] == "hour")
check("empty hours are filled with zeros (00:00 to 14:00 = 15 buckets)",
      len(pts) == 15, f"{len(pts)} buckets")
check("first bucket is local midnight and holds order 1",
      pts[0]["bucket"] == "2026-03-15T00:00:00" and pts[0]["sales"] == 430.0, str(pts[0]))
check("order 2 lands in the 11:00 local bucket",
      [x for x in pts if x["bucket"] == "2026-03-15T11:00:00"][0]["sales"] == 200.0)
check("chart total equals the sales KPI", sum(x["sales"] for x in pts) == 630.0)
check("chart buckets carry distinct customers (for the sparkline)",
      sum(x["customers"] for x in pts) == 2 and pts[0]["customers"] == 1)
ps = o["payment_summary"]
check("overview carries the payment summary for the success gauge",
      ps["successful"]["value"] == 2 and ps["failed"]["value"] == 1, str(ps))

groups = {g["status_group"]: g["orders"] for g in o["status_groups"]}
check("the E2E test order is not counted: sales, orders and customers unchanged",
      k["sales"]["value"] == 630.0 and k["orders"]["value"] == 2 and k["orders_all"] == 6
      and k["customers"]["value"] == 2)
check("…nor in the products lists", all(x["product_id"] != 999 for x in o["top_products"]))
check("an unknown status appears as 'other', not dropped", groups.get("other") == 1,
      str(groups))
check("status groups cover every order", sum(groups.values()) == 6, str(groups))
check("freshness reports the reconciliation watermark",
      o["freshness"]["last_reconciled_at_utc"] == "2026-03-15T11:55:00")

# Yesterday, whole day: orders 6 and 7.
code, y = get("/api/overview", period="yesterday")
check("yesterday includes the 23:30 Riyadh order (UTC date irrelevant)",
      y["kpis"]["sales"]["value"] == 415.0, str(y["kpis"]["sales"]))
check("a customer who bought on two days counts once per period",
      y["kpis"]["customers"]["value"] == 2)
check("yesterday vs the day before: previous 0 → change is null, not +inf",
      y["kpis"]["sales"]["previous"] == 0.0 and y["kpis"]["sales"]["change_pct"] is None,
      str(y["kpis"]["sales"]))

# Multi-day range → daily buckets.
code, w = get("/api/overview", period="last_7_days")
check("7-day chart is daily with 7 buckets",
      w["sales_over_time"]["granularity"] == "day" and len(w["sales_over_time"]["points"]) == 7,
      str(len(w["sales_over_time"]["points"])))
check("last 7 days excludes today", w["kpis"]["sales"]["value"] == 415.0,
      str(w["kpis"]["sales"]))

# No comparison data.
code, t = get("/api/overview", period="this_year")
check("this year: comparison unavailable is explicit",
      t["period"]["comparison_available"] is False)
check("…every KPI has previous=null and change_pct=null",
      all(t["kpis"][x]["previous"] is None and t["kpis"][x]["change_pct"] is None
          for x in ("sales", "orders", "aov", "customers")))
check("…and the period is flagged incomplete", t["period"]["complete"] is False)

# Empty period: AOV undefined, not zero.
code, e = get("/api/overview", period="custom", start="2026-03-01", end="2026-03-01")
check("a day with no sales has AOV null, not 0", e["kpis"]["aov"]["value"] is None,
      str(e["kpis"]["aov"]))


# --- Recent orders (overview card) ---
rec = o["recent_orders"]
check("recent orders: every status, newest first, E2E test order left out",
      [r["id"] for r in rec] == [8, 5, 4, 3, 2, 1], str([r["id"] for r in rec]))
check("recent orders: equal times fall back to the higher id first",
      [r["id"] for r in rec][1:4] == [5, 4, 3])
check("recent orders: status and group are carried (cancelled, failed, unclassified)",
      {r["id"]: r["status_group"] for r in rec if r["id"] in (3, 4, 5)}
      == {3: "cancelled", 4: "failed", 5: "other"})
r1 = next(r for r in rec if r["id"] == 1)
check("recent orders: time is Riyadh wall-clock (00:30 on the 15th, not 21:30 UTC)",
      r1["created_local"] == "2026-03-15T00:30:00", r1["created_local"])
r2 = next(r for r in rec if r["id"] == 2)
check("recent orders: product lines with quantity and gross value, in line order",
      [(i["name"], i["quantity"], i["value"]) for i in r2["items"]]
      == [("Towel", 1, 115), ("Soap", 3, 85)], str(r2["items"]))
check("recent orders: order total is the gross order total", r2["total"] == 200)
check("recent orders: an order without product lines has an empty list",
      next(r for r in rec if r["id"] == 4)["items"] == [])
check("recent orders: order number falls back to the id", r1["number"] == "1", r1["number"])
check("recent orders: no customer details in the response",
      not any("email" in k for r in rec for k in r), str(sorted(rec[0])))
check("recent orders follow the period (yesterday: orders 6 and 7)",
      [r["id"] for r in y["recent_orders"]] == [6, 7], str([r["id"] for r in y["recent_orders"]]))
check("recent orders are capped at 10",
      len(get("/api/overview", period="this_year")[1]["recent_orders"]) <= 10)

# --- Paging through the orders (the dashboard does this for "Today") ---
check("recent orders: page info for today (6 orders → 1 page of 10)",
      o["recent_orders_page"] == {"page": 1, "pages": 1, "per_page": 10, "total": 6},
      str(o["recent_orders_page"]))
_, ty1 = get("/api/overview", period="this_year")
_, ty2 = get("/api/overview", period="this_year", orders_page=2)
check("this year holds 12 orders (E2E excluded) → 2 pages",
      ty1["recent_orders_page"]["total"] == 12 and ty1["recent_orders_page"]["pages"] == 2,
      str(ty1["recent_orders_page"]))
check("page 2 continues where page 1 stopped: the two oldest, no overlap",
      [r["id"] for r in ty2["recent_orders"]] == [21, 20]
      and not {r["id"] for r in ty1["recent_orders"]} & {r["id"] for r in ty2["recent_orders"]},
      str([r["id"] for r in ty2["recent_orders"]]))
_, far = get("/api/overview", period="today", orders_page=99)
check("a page past the end shows the last page instead of an empty list",
      far["recent_orders_page"]["page"] == 1 and len(far["recent_orders"]) == 6)
check("orders_page=0 is rejected", get("/api/overview", period="today", orders_page=0)[0] == 422)


# ===========================================================================
# 4. Sales, orders and returns
# ===========================================================================
code, s = get("/api/sales", period="today")
statuses = {x["status"]: x for x in s["orders_by_status"]}
check("orders by status lists every raw slug",
      set(statuses) == {"delivered", "on-the-way", "cancelled", "tamara-p-failed",
                        "brand-new-status", "refunded"}, str(sorted(statuses)))
check("each status carries its group",
      statuses["tamara-p-failed"]["status_group"] == "failed"
      and statuses["brand-new-status"]["status_group"] == "other")
rr = s["return_rate"]
check("return rate = orders with a refund / sale orders (2 / 2)",
      rr["value"] == 100.0 and rr["returned_orders"] == 2 and rr["sale_orders"] == 2, str(rr))
check("refunds issued today: 3 refunds, 220 SAR",
      s["refunds"]["refunds"] == 3 and s["refunds"]["amount"] == 220.0, str(s["refunds"]))
mr = s["most_returned"]
check("most returned: product 500, quantity 2 across both refunds",
      mr and mr[0]["product_id"] == 500 and mr[0]["quantity"] == 2
      and mr[0]["value"] == 200.0, str(mr))
check("an amount-only refund names no product", len(mr) == 1, str(mr))


# ===========================================================================
# 5. Products
# ===========================================================================
code, pr = get("/api/products", period="today")
bv = [(x["product_id"], x["value"]) for x in pr["by_value"]]
bq = [(x["product_id"], x["quantity"]) for x in pr["by_quantity"]]
check("by value: gross line value, sale orders only (cancelled 999 excluded)",
      bv == [(500, 400.0), (600, 115.0), (700, 85.0)], str(bv))
check("by quantity ranks differently", bq == [(700, 3), (500, 2), (600, 1)], str(bq))
cats = {x["category"]: x["value"] for x in pr["by_category"]}
check("a two-category product counts in both",
      cats.get("Bedding") == 400.0 and cats.get("Duvets") == 400.0, str(cats))
check("unmapped products are 'Uncategorised', not dropped",
      cats.get("Uncategorised") == 85.0, str(cats))
code, one = get("/api/products", period="today", limit=1)
check("limit applies", len(one["by_value"]) == 1)


# ===========================================================================
# 6. Payments
# ===========================================================================
# The Customers tab was removed (2026-10-01); purchasing customers is
# still an Overview KPI, checked above.
code, _ = get("/api/customers", period="today")
check("no customers endpoint any more", code == 404, str(code))

code, pay = get("/api/payments", period="today")
methods = {(x["gateway"], x["pos"]): x for x in pay["by_method"]}
ap, tm = methods[("applepay", False)], methods[("tamara", False)]
check("payments: Apple Pay 1 sale worth 430 (cancelled order is not an attempt)",
      ap["successful"] == 1 and ap["failed"] == 0 and ap["sales"] == 430.0, str(ap))
check("payments: two Tamara codes grouped under one Tamara line",
      tm["successful"] == 1 and tm["failed"] == 1 and tm["success_rate_pct"] == 50.0
      and sorted(v["method"] for v in tm["variants"])
          == ["tamara-gateway-checkout", "tamara-gateway-pay-in-4"], str(tm))
check("…failed counted via the status group (tamara-p-failed)", tm["failed"] == 1)
check("the 4 enabled gateways are always listed, even at zero",
      {g for g, pos in methods} == {"applepay", "mada", "tamara", "tabby"}
      and methods[("mada", False)]["successful"] == 0
      and methods[("mada", False)]["success_rate_pct"] is None, str(sorted(methods)))

code, pay10 = get("/api/payments", period="custom", start="2026-02-20", end="2026-02-20")
m10 = {(x["gateway"], x["pos"]): x for x in pay10["by_method"]}
check("PoS orders get their own lines, labelled pos=true",
      m10.get(("pos_cash", True), {}).get("sales") == 80.0
      and m10.get(("applepay", True), {}).get("sales") == 90.0, str(sorted(m10)))
check("PoS is never merged into the online line of the same gateway",
      m10[("applepay", False)]["sales"] == 50.0, str(m10[("applepay", False)]))
check("orders created via the API count as online, not PoS",
      m10[("applepay", False)]["successful"] == 1)
check("a disabled gateway with no attempts is not listed",
      ("cod", False) not in m10, str(sorted(m10)))
code, pay_y = get("/api/payments", period="yesterday")
my = {(x["gateway"], x["pos"]): x for x in pay_y["by_method"]}
check("a retired gateway with orders is listed and flagged not enabled",
      my[("card", False)]["successful"] == 1 and my[("card", False)]["enabled"] is False,
      str(my.get(("card", False))))
check("successful vs failed totals", pay["successful"]["value"] == 2
      and pay["failed"]["value"] == 1, f"{pay['successful']} / {pay['failed']}")


# ===========================================================================
# 7. Validation and health
# ===========================================================================
code, _ = get("/api/overview", period="custom")
check("custom without dates → 400", code == 400, str(code))
code, _ = get("/api/overview", period="custom", start="2026-03-10", end="2026-03-01")
check("custom with end before start → 400", code == 400, str(code))
code, _ = get("/api/overview", period="fortnight")
check("unknown period → 422", code == 422, str(code))
code, _ = get("/api/overview", period="custom", start="10-03-2026", end="2026-03-10")
check("malformed date → 422", code == 422, str(code))
code, h = get("/api/health")
check("health: ok with order count (all stored rows, test order included)",
      code == 200 and h["status"] == "ok" and h["orders"] == 13, str(h))
check("health: reports when the last sync ran (not just the watermark)",
      h.get("last_sync_utc") is not None, str(h.get("last_sync_utc")))


# ===========================================================================
# 8. The dashboard page is served by the same app
# ===========================================================================
r = client.get("/")
check("/ serves the dashboard page", r.status_code == 200
      and "text/html" in r.headers["content-type"] and "/static/app.js" in r.text,
      f"{r.status_code} {r.headers.get('content-type')}")
for asset in ("app.js", "i18n.js", "styles.css", "vendor/chart.umd.min.js",
              "vendor/fonts/saudi-riyal-400.woff2", "vendor/fonts/saudi-riyal-700.woff2"):
    r = client.get(f"/static/{asset}")
    check(f"/static/{asset} is served", r.status_code == 200, str(r.status_code))
check("page files are revalidated on every load (no stale app.js)",
      client.get("/static/app.js").headers.get("cache-control") == "no-cache")
check("API responses are not affected by that header",
      client.get("/api/health").headers.get("cache-control") != "no-cache")


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("All metrics checks passed.")
