"""Exercise backfill_orders against a real Postgres, with the API stubbed.

    python -m tests.test_backfill

No network is used — app.woo.get is replaced with fixtures. But it DOES write
to a real database, so it refuses to run unless POSTGRES_DB ends in `_test`.
Create one first:

    docker compose exec db createdb -U storefront storefront_analytics_test
    docker compose exec -T db psql -U storefront -d storefront_analytics_test < db/01_schema.sql
    docker compose exec -T db psql -U storefront -d storefront_analytics_test < db/02_seed_status_groups.sql

then point POSTGRES_DB at it for the run:

    POSTGRES_DB=storefront_analytics_test python -m tests.test_backfill   # bash
    $env:POSTGRES_DB="storefront_analytics_test"; python -m tests.test_backfill   # PowerShell
"""
import json, sys
from datetime import datetime, timezone

from app import config

# Guard: never let a fixture run touch the real backfilled data.
if not config.POSTGRES_DB.endswith("_test"):
    sys.exit(
        f"Refusing to run against database {config.POSTGRES_DB!r}.\n"
        "These tests insert fixture orders. Point POSTGRES_DB at a database "
        "whose name ends in '_test' (see this file's docstring)."
    )

import app.woo as woo
from app.db import connect

FAILURES = []
def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond: FAILURES.append(name)


def reset_db():
    """Start each section from empty. These tests count rows absolutely, so a
    leftover fixture from a previous run (or a previous section) would fail
    them for the wrong reason."""
    with connect() as _conn:
        _conn.execute("TRUNCATE orders, refunds, refund_items, "
                      "product_categories, sync_state CASCADE")


reset_db()

def order(oid, status="delivered", modified="2026-02-01T10:00:00",
          total="430", items=None, refunds=None):
    return {
        "id": oid, "number": str(oid), "status": status, "currency": "SAR",
        "total": total, "shipping_total": "30", "total_tax": "52",
        "discount_total": "", "payment_method": "tabby",
        "payment_method_title": "تابي", "customer_id": 0,
        "billing": {"email": f"cust{oid}@example.com", "first_name": "Real",
                    "last_name": "Person", "phone": "+966500000000",
                    "address_1": "123 Somewhere St", "city": "Jeddah"},
        "created_via": "checkout",
        "date_created_gmt": "2026-02-01T09:00:00",
        "date_paid_gmt": "2026-02-01T09:05:00",
        "date_modified_gmt": modified,
        "line_items": items if items is not None else [
            {"id": 11, "product_id": 500, "variation_id": 0, "name": "Duvet",
             "sku": "DUV-1", "quantity": 2, "subtotal": "400", "total": "348",
             "total_tax": "52"}],
        "refunds": refunds or [],
    }

# --- stub the network ------------------------------------------------------
CALLS = []
PAGES = {}
def fake_get(path, params=None, retries=3):
    CALLS.append((path, dict(params or {})))
    key = (params or {}).get("page", 1)
    orders = PAGES.get(key, [])
    return orders, {"X-WP-TotalPages": str(max(PAGES.keys()) if PAGES else 1)}
woo.get = fake_get

import scripts.backfill_orders as bf
bf.get = fake_get

# --- 1. month_windows ------------------------------------------------------
w = list(bf.month_windows(datetime(2024, 1, 15), datetime(2024, 4, 1)))
check("month_windows starts at month boundary", w[0][0] == datetime(2024, 1, 1))
check("month_windows handles leap February",
      (datetime(2024, 2, 1), datetime(2024, 3, 1)) in w, f"{[x[0].strftime('%Y-%m') for x in w]}")
w2 = list(bf.month_windows(datetime(2021, 11, 1), datetime(2022, 2, 1)))
check("month_windows crosses the year boundary",
      [x[0].strftime("%Y-%m") for x in w2] == ["2021-11", "2021-12", "2022-01"])
check("month_windows is contiguous (no gaps)",
      all(w2[i][1] == w2[i+1][0] for i in range(len(w2)-1)))

# --- 2. request parameters -------------------------------------------------
PAGES = {1: []}
list(bf.fetch_window(datetime(2026, 2, 1), datetime(2026, 3, 1)))
p = CALLS[-1][1]
check("uses status=any", p.get("status") == "any", p.get("status"))
check("uses dates_are_gmt", p.get("dates_are_gmt") == "true")
check("after is exclusive-adjusted", p.get("after") == "2026-01-31T23:59:59", p.get("after"))
check("before is next window start", p.get("before") == "2026-03-01T00:00:00", p.get("before"))
check("stable ordering", (p.get("orderby"), p.get("order")) == ("id", "asc"))
check("meta_data excluded from _fields", "meta_data" not in p.get("_fields", ""))

# --- 3. pagination ---------------------------------------------------------
PAGES = {1: [order(1)], 2: [order(2)], 3: [order(3)]}
pages = list(bf.fetch_window(datetime(2026, 2, 1), datetime(2026, 3, 1)))
check("follows X-WP-TotalPages", len(pages) == 3, f"{len(pages)} pages")

# --- 4. write + PII stripping ---------------------------------------------
with connect() as conn:
    cur = conn.cursor()
    bf.write_order(cur, order(100))
with connect() as conn:
    row = conn.execute("SELECT billing_email, raw, merchandise_total, created_via "
                       "FROM orders WHERE id=100").fetchone()
email, raw, merch, via = row
check("billing_email extracted", email == "cust100@example.com", email)
check("merchandise_total computed", float(merch) == 348.00, str(merch))
check("created_via stored", via == "checkout")
blob = json.dumps(raw)
check("raw has no billing block", "billing" not in raw, str(list(raw)[:20]))
for pii in ["Real", "Person", "+966500000000", "123 Somewhere St", "Jeddah"]:
    check(f"raw excludes PII: {pii!r}", pii not in blob)
check("raw keeps the email", "cust100@example.com" in blob)

with connect() as conn:
    n = conn.execute("SELECT count(*) FROM order_items WHERE order_id=100").fetchone()[0]
check("line items written", n == 1, f"{n} rows")

# --- 5. idempotency --------------------------------------------------------
with connect() as conn:
    cur = conn.cursor()
    bf.write_order(cur, order(100))
    bf.write_order(cur, order(100))
with connect() as conn:
    orders_n = conn.execute("SELECT count(*) FROM orders WHERE id=100").fetchone()[0]
    items_n = conn.execute("SELECT count(*) FROM order_items WHERE order_id=100").fetchone()[0]
check("re-running does not duplicate orders", orders_n == 1)
check("re-running does not duplicate line items", items_n == 1, f"{items_n} rows")

# --- 6. ordering guard -----------------------------------------------------
with connect() as conn:
    cur = conn.cursor()
    applied = bf.write_order(cur, order(100, status="cancelled",
                                        modified="2026-01-01T00:00:00"))
with connect() as conn:
    st = conn.execute("SELECT status FROM orders WHERE id=100").fetchone()[0]
check("stale update rejected", applied is False)
check("stale update left status alone", st == "delivered", st)

with connect() as conn:
    cur = conn.cursor()
    applied = bf.write_order(cur, order(100, status="returned",
                                        modified="2026-03-01T00:00:00",
                                        items=[{"id": 99, "product_id": 777,
                                                "variation_id": 0, "name": "Sheet",
                                                "sku": "SH-1", "quantity": 1,
                                                "subtotal": "100", "total": "100",
                                                "total_tax": "0"}]))
with connect() as conn:
    st = conn.execute("SELECT status FROM orders WHERE id=100").fetchone()[0]
    li = conn.execute("SELECT line_item_id, product_id FROM order_items "
                      "WHERE order_id=100").fetchall()
check("newer update applied", applied is True)
check("newer update changed status", st == "returned", st)
check("line items replaced, not appended", li == [(99, 777)], str(li))

# --- 7. empty / awkward values --------------------------------------------
odd = order(200, total="")
odd["discount_total"] = None
odd["payment_method"] = ""
odd["date_paid_gmt"] = None
odd["line_items"] = []
with connect() as conn:
    cur = conn.cursor()
    bf.write_order(cur, odd)
with connect() as conn:
    t, d, pm, dp = conn.execute("SELECT total, discount_total, payment_method, "
                                "date_paid_gmt FROM orders WHERE id=200").fetchone()
check("empty total becomes 0", float(t) == 0.0)
check("null discount becomes 0", float(d) == 0.0)
check("empty payment_method becomes NULL", pm is None)
check("null date_paid stays NULL", dp is None)

# --- 8. UTC handling -------------------------------------------------------
with connect() as conn:
    dc = conn.execute("SELECT date_created_gmt FROM orders WHERE id=200").fetchone()[0]
check("date parsed as UTC",
      dc == datetime(2026, 2, 1, 9, 0, tzinfo=timezone.utc), str(dc))

# --- 9. unclassified status surfaces --------------------------------------
with connect() as conn:
    cur = conn.cursor()
    bf.write_order(cur, order(300, status="brand-new-status-2026"))
with connect() as conn:
    unknown = conn.execute("""
        SELECT o.status FROM orders o
        LEFT JOIN order_status_groups g ON g.status = o.status
        WHERE g.status IS NULL""").fetchall()
check("unknown status detected by the reconcile query",
      unknown == [("brand-new-status-2026",)], str(unknown))
with connect() as conn:
    grp = conn.execute("""
        SELECT COALESCE(g.status_group,'other') FROM orders o
        LEFT JOIN order_status_groups g ON g.status=o.status
        WHERE o.id=300""").fetchone()[0]
check("unknown status falls back to 'other', not dropped", grp == "other")

# --- 10. resume arithmetic -------------------------------------------------
from datetime import timedelta
for last, expected in [("2021-01", "2021-02"), ("2021-12", "2022-01"),
                       ("2024-02", "2024-03")]:
    done = datetime.strptime(last, "%Y-%m")
    nxt = (done.replace(day=28) + timedelta(days=4)).replace(day=1)
    check(f"resume after {last} → {expected}", nxt.strftime("%Y-%m") == expected,
          nxt.strftime("%Y-%m"))


# ===========================================================================
# Outage and resume behaviour
# ===========================================================================
reset_db()
from app.db import get_sync_state

bf.WINDOW_RETRY_WAITS = [0, 0, 0]          # no real sleeping in tests

def order(oid, created):
    return {"id": oid, "number": str(oid), "status": "delivered", "currency": "SAR",
            "total": "100", "shipping_total": "0", "total_tax": "0",
            "discount_total": "0", "payment_method": "cod",
            "payment_method_title": "COD", "customer_id": 0,
            "billing": {"email": f"c{oid}@e.com"}, "created_via": "checkout",
            "date_created_gmt": created, "date_paid_gmt": created,
            "date_modified_gmt": created, "line_items": [], "refunds": []}

# Three months of data. The outage happens partway through the second.
DATA = {"2020-12": [order(1, "2020-12-31T21:04:38")],
        "2021-01": [order(2, "2021-01-10T10:00:00")],
        "2021-02": [order(3, "2021-02-10T10:00:00")]}
outage_budget = [0]
def make_get(fail_on_month, fail_times):
    def fake(path, params=None, retries=3):
        after = params["after"]
        month = (datetime.strptime(after[:10], "%Y-%m-%d")).strftime("%Y-%m")
        # `after` is one second before the window start, so Dec 31 -> Jan.
        if after.endswith("23:59:59"):
            d = datetime.strptime(after[:10], "%Y-%m-%d")
            month = (d.replace(day=28).replace(day=28)).strftime("%Y-%m") if False else None
            import calendar
            nxt = d.replace(day=1)
            month = (datetime(d.year + (d.month == 12), (d.month % 12) + 1, 1)).strftime("%Y-%m")
        if month == fail_on_month and outage_budget[0] < fail_times:
            outage_budget[0] += 1
            raise RuntimeError("simulated connection reset")
        if params.get("page", 1) > 1:
            return [], {"X-WP-TotalPages": "1"}
        return DATA.get(month, []), {"X-WP-TotalPages": "1"}
    return fake

print("--- outage that recovers within the retry ladder ---")
bf.get = make_get("2021-01", 2)        # fails twice, then succeeds
outage_budget[0] = 0
bf.run(datetime(2020, 12, 1), datetime(2021, 3, 1), None)
with connect() as c:
    n = c.execute("SELECT count(*) FROM orders").fetchone()[0]
check("run survived a recoverable outage", n == 3, f"{n} orders")
check("watermark reached the last window", get_sync_state(bf.SYNC_KEY) == "2021-02",
      get_sync_state(bf.SYNC_KEY))

print("\n--- outage that outlasts the retry ladder ---")
with connect() as c:
    c.execute("TRUNCATE orders CASCADE"); c.execute("DELETE FROM sync_state")
bf.get = make_get("2021-01", 99)       # never recovers
outage_budget[0] = 0
try:
    bf.run(datetime(2020, 12, 1), datetime(2021, 3, 1), None)
    raised = None
except SystemExit as e:
    raised = e.code
check("exits cleanly (SystemExit, not a traceback)", raised == 1, f"code={raised}")
wm = get_sync_state(bf.SYNC_KEY)
check("watermark held at the last GOOD window", wm == "2020-12", f"watermark={wm}")
with connect() as c:
    n = c.execute("SELECT count(*) FROM orders").fetchone()[0]
check("completed window's data kept", n == 1, f"{n} orders")

print("\n--- reconnect and re-run with NO --start ---")
bf.get = make_get("nope", 0)
outage_budget[0] = 0
last = get_sync_state(bf.SYNC_KEY)
from datetime import timedelta
done = datetime.strptime(last, "%Y-%m")
resume = (done.replace(day=28) + timedelta(days=4)).replace(day=1)
check("resume point is the month after the last good one",
      resume.strftime("%Y-%m") == "2021-01", resume.strftime("%Y-%m"))
bf.run(resume, datetime(2021, 3, 1), None)
with connect() as c:
    n = c.execute("SELECT count(*) FROM orders").fetchone()[0]
    ids = [r[0] for r in c.execute("SELECT id FROM orders ORDER BY id").fetchall()]
check("resume completed the remaining months", n == 3, f"{n} orders, ids={ids}")
check("no duplicates from the interrupted window", ids == [1, 2, 3], str(ids))

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("All backfill checks passed.")
