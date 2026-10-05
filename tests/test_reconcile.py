"""Exercise scripts.reconcile against a real Postgres, with the API stubbed.

    $env:POSTGRES_DB="storefront_analytics_test"; python -m tests.test_reconcile   # PowerShell
    POSTGRES_DB=storefront_analytics_test python -m tests.test_reconcile           # bash

Same rules as the other test files: no network (app.woo.get is replaced with
fixtures), a real database, and a refusal to run unless POSTGRES_DB ends in
`_test`. Each section truncates its own data, so re-runs give identical output.
"""
import json, sys
from datetime import datetime, timezone

from app import config

# Guard: never let a fixture run touch the real backfilled data.
if not config.POSTGRES_DB.endswith("_test"):
    sys.exit(
        f"Refusing to run against database {config.POSTGRES_DB!r}.\n"
        "These tests insert fixture orders. Point POSTGRES_DB at a database "
        "whose name ends in '_test' (see tests/test_backfill.py's docstring)."
    )

from app.db import connect, get_sync_state, set_sync_state
import scripts.reconcile as rc

FAILURES = []
def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond: FAILURES.append(name)


def reset_db():
    with connect() as conn:
        conn.execute("TRUNCATE orders, refunds, refund_items, "
                     "product_categories, sync_state CASCADE")


def count(sql, params=()):
    with connect() as conn:
        return conn.execute(sql, params).fetchone()[0]


# Freeze the clock so the new watermark is predictable:
# 10:00:00 minus the 5-minute overlap = 09:55:00.
NOW = datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)
rc.utcnow = lambda: NOW
EXPECTED_WM = "2026-09-23T09:55:00"


def order(oid, status="delivered", created="2026-09-10T09:00:00",
          modified="2026-09-23T08:00:00", refunds=None, items=None):
    """An order as WooCommerce returns it, including PII we must drop."""
    return {
        "id": oid, "number": str(oid), "status": status, "currency": "SAR",
        "total": "430", "shipping_total": "30", "total_tax": "52",
        "discount_total": "0", "payment_method": "tabby",
        "payment_method_title": "تابي", "customer_id": 0,
        "billing": {"email": f"c{oid}@example.com", "first_name": "Real",
                    "phone": "+966500000000", "address_1": "123 Somewhere St"},
        "created_via": "checkout",
        "date_created_gmt": created, "date_paid_gmt": created,
        "date_modified_gmt": modified,
        "line_items": items if items is not None else [
            {"id": 11, "product_id": 500, "variation_id": 0, "name": "Duvet",
             "sku": "DUV-1", "quantity": 1, "subtotal": "348", "total": "348",
             "total_tax": "52"}],
        # The inline array only needs to be non-empty; detail comes from the
        # /refunds endpoint, exactly as on the real store.
        "refunds": refunds or [],
    }


def refund(rid, amount="-100.00", qty=-1, product=500):
    """A refund as the /orders/{id}/refunds endpoint returns it: negative."""
    return {"id": rid, "date_created_gmt": "2026-09-22T12:00:00",
            "amount": amount.lstrip("-"), "reason": "damaged",
            "line_items": [{"id": rid * 10, "product_id": product,
                            "variation_id": 0, "name": "Duvet",
                            "quantity": qty, "total": amount}]}


# --- stub the network ------------------------------------------------------
# PAGES: page number -> list of orders for GET /orders.
# TOTALS: optional page number -> X-WP-Total to report on that page, to
#         simulate the result set growing mid-walk.
# REFUND_API: order id -> refunds list for GET /orders/{id}/refunds.
# BROKEN: paths that raise, to simulate network failures.
PAGES, TOTALS, REFUND_API, BROKEN, CALLS = {}, {}, {}, set(), []

def fake_get(path, params=None, retries=3):
    CALLS.append((path, dict(params or {})))
    if path in BROKEN:
        raise RuntimeError("simulated network failure")
    if path == "wc/v3/orders":
        page = (params or {}).get("page", 1)
        n_pages = max(PAGES) if PAGES else 1
        all_orders = sum(PAGES.values(), [])
        total = TOTALS.get(page, len(all_orders))
        return PAGES.get(page, []), {"X-WP-TotalPages": str(n_pages),
                                     "X-WP-Total": str(total)}
    if path.endswith("/refunds"):
        return REFUND_API.get(int(path.split("/")[3]), []), {}
    raise AssertionError(f"unexpected path {path}")

rc.get = fake_get


def fresh(pages, refunds_api=None, totals=None):
    """Reset the stub for a new scenario."""
    global PAGES, TOTALS, REFUND_API
    PAGES, TOTALS, REFUND_API = pages, totals or {}, refunds_api or {}
    BROKEN.clear(); CALLS.clear()


def refund_calls():
    return [p for p, _ in CALLS if p.endswith("/refunds")]


# ===========================================================================
# 1. Request parameters and the first watermark
# ===========================================================================
reset_db()
fresh({1: []})
ok = rc.run_once()
path, p = CALLS[0]
check("first run uses FIRST_WATERMARK", p.get("modified_after") == rc.FIRST_WATERMARK,
      p.get("modified_after"))
check("filters by modified_after, not by created date",
      "after" not in p and "before" not in p, str(sorted(p)))
check("uses status=any", p.get("status") == "any")
check("uses dates_are_gmt", p.get("dates_are_gmt") == "true")
check("stable ordering", (p.get("orderby"), p.get("order")) == ("id", "asc"))
check("same trimmed _fields as the backfill", p.get("_fields") == rc.ORDER_FIELDS)
check("an empty run is still a clean run", ok is True)
check("watermark = run start minus overlap", get_sync_state(rc.SYNC_KEY) == EXPECTED_WM,
      get_sync_state(rc.SYNC_KEY))

# The second run must start from the stored watermark.
fresh({1: []})
rc.run_once()
check("next run starts from the stored watermark",
      CALLS[0][1].get("modified_after") == EXPECTED_WM, CALLS[0][1].get("modified_after"))


# ===========================================================================
# 2. Applying changes: reuse of write_order, pagination, scope, PII
# ===========================================================================
reset_db()
fresh({1: [order(1), order(2)], 2: [order(3)]})
ok = rc.run_once()
check("follows X-WP-TotalPages", count("SELECT count(*) FROM orders") == 3)
check("clean run returns True", ok is True)
raw = json.dumps(count("SELECT raw FROM orders WHERE id=1"))
check("PII stripped (write_order reused)", "Real" not in raw and "+966" not in raw
      and "Somewhere" not in raw)
check("billing email kept", count("SELECT billing_email FROM orders WHERE id=1")
      == "c1@example.com")
check("line items written", count("SELECT count(*) FROM order_items") == 3)

# An order CREATED before HISTORY_START but MODIFIED recently.
reset_db()
fresh({1: [order(10, created="2025-12-31T20:59:59"),     # 23:59:59 Riyadh, still 2025 UTC
           order(11, created="2026-01-01T00:00:00")]})   # first second of scope
rc.run_once()
check("pre-2026 order skipped even though recently modified",
      count("SELECT count(*) FROM orders WHERE id=10") == 0)
check("order at exactly HISTORY_START is kept",
      count("SELECT count(*) FROM orders WHERE id=11") == 1)

# Idempotency: the same changes twice.
fresh({1: [order(11)]})
rc.run_once(); rc.run_once()
check("re-running does not duplicate orders", count("SELECT count(*) FROM orders") == 1)
check("re-running does not duplicate items",
      count("SELECT count(*) FROM order_items WHERE order_id=11") == 1)

# Status change arrives: e.g. on-the-way -> delivered.
reset_db()
fresh({1: [order(20, status="on-the-way", modified="2026-09-23T07:00:00")]})
rc.run_once()
fresh({1: [order(20, status="delivered", modified="2026-09-23T09:00:00")]})
rc.run_once()
check("status change applied", count("SELECT status FROM orders WHERE id=20") == "delivered")

# The ordering guard: the store hands back an OLDER copy than we hold.
fresh({1: [order(20, status="on-the-way", modified="2026-09-23T07:00:00",
                 refunds=[{"id": 1}])]},
      refunds_api={20: [refund(900)]})
rc.run_once()
check("older copy rejected by the ordering guard",
      count("SELECT status FROM orders WHERE id=20") == "delivered")
check("rejected order does not trigger a refund fetch", refund_calls() == [], str(refund_calls()))


# ===========================================================================
# 3. Refunds
# ===========================================================================
reset_db()
fresh({1: [order(30, status="refunded", refunds=[{"id": 1}]),
           order(31)]},                                   # no refunds inline
      refunds_api={30: [refund(900), refund(901, amount="-50.00", product=600)]})
rc.run_once()
check("refund endpoint called only for orders with inline refunds",
      refund_calls() == ["wc/v3/orders/30/refunds"], str(refund_calls()))
check("refunds written (write_refunds reused)",
      count("SELECT count(*) FROM refunds WHERE order_id=30") == 2)
check("refund amount stored positive",
      float(count("SELECT amount FROM refunds WHERE id=901")) == 50.0)
check("refunded quantity stored positive",
      count("SELECT quantity FROM refund_items WHERE refund_id=900") == 1)

# One of the two refunds is deleted in the store.
fresh({1: [order(30, status="refunded", modified="2026-09-23T09:00:00",
                 refunds=[{"id": 1}])]},
      refunds_api={30: [refund(900)]})
rc.run_once()
check("refund deleted in the store is deleted here",
      count("SELECT count(*) FROM refunds WHERE id=901") == 0)
check("its refund items go with it (cascade)",
      count("SELECT count(*) FROM refund_items WHERE refund_id=901") == 0)
check("the surviving refund is kept",
      count("SELECT count(*) FROM refunds WHERE id=900") == 1)

# All refunds removed: the inline array is now empty. No API call needed.
fresh({1: [order(30, status="delivered", modified="2026-09-23T09:30:00")]})
rc.run_once()
check("empty inline array clears local refunds",
      count("SELECT count(*) FROM refunds WHERE order_id=30") == 0)
check("...without calling the refund endpoint", refund_calls() == [], str(refund_calls()))


# ===========================================================================
# 4. Failures hold the watermark
# ===========================================================================
START_WM = "2026-09-20T00:00:00"

# (a) The order list itself cannot be fetched.
reset_db(); set_sync_state(rc.SYNC_KEY, START_WM)
fresh({1: [order(40)]}); BROKEN.add("wc/v3/orders")
ok = rc.run_once()
check("fetch failure returns False, does not raise", ok is False)
check("fetch failure leaves the watermark", get_sync_state(rc.SYNC_KEY) == START_WM,
      get_sync_state(rc.SYNC_KEY))
check("fetch failure writes nothing", count("SELECT count(*) FROM orders") == 0)

# (b) One order's refunds fail; another order's succeed.
reset_db(); set_sync_state(rc.SYNC_KEY, START_WM)
fresh({1: [order(50, refunds=[{"id": 1}]), order(51, refunds=[{"id": 1}])]},
      refunds_api={50: [refund(950)], 51: [refund(951)]})
BROKEN.add("wc/v3/orders/50/refunds")
ok = rc.run_once()
check("refund failure makes the run unclean", ok is False)
check("refund failure holds the watermark", get_sync_state(rc.SYNC_KEY) == START_WM,
      get_sync_state(rc.SYNC_KEY))
check("the order itself is still written", count("SELECT count(*) FROM orders WHERE id=50") == 1)
check("other orders' refunds still written",
      count("SELECT count(*) FROM refunds WHERE order_id=51") == 1)

# The network recovers; a plain re-run fixes it. Same data, same modified
# timestamps — the ordering guard's >= lets the equal copy through, which is
# what makes the retry reach the refunds.
BROKEN.clear(); CALLS.clear()
ok = rc.run_once()
check("plain re-run starts from the held watermark",
      CALLS[0][1].get("modified_after") == START_WM)
check("plain re-run fetches the missed refunds",
      count("SELECT count(*) FROM refunds WHERE order_id=50") == 1)
check("watermark advances once the run is clean",
      ok is True and get_sync_state(rc.SYNC_KEY) == EXPECTED_WM, get_sync_state(rc.SYNC_KEY))

# (c) The result set grows while paging (an order modified mid-run).
reset_db(); set_sync_state(rc.SYNC_KEY, START_WM)
fresh({1: [order(60)], 2: [order(61)]}, totals={1: 2, 2: 3})
ok = rc.run_once()
check("shifting result set detected", ok is False)
check("shifting result set holds the watermark", get_sync_state(rc.SYNC_KEY) == START_WM)
check("...but what was fetched is still written", count("SELECT count(*) FROM orders") == 2)

# (d) A duplicate across pages (the shift made one order appear twice).
reset_db()
fresh({1: [order(70), order(71)], 2: [order(71), order(72)]})
rc.run_once()
check("an order seen on two pages is written once", count("SELECT count(*) FROM orders") == 3)


# ===========================================================================
# 5. --since is a one-off
# ===========================================================================
reset_db(); set_sync_state(rc.SYNC_KEY, START_WM)
fresh({1: [order(80)]})
ok = rc.run_once(since_override="2026-09-22T00:00:00")
check("--since is used for the fetch",
      CALLS[0][1].get("modified_after") == "2026-09-22T00:00:00")
check("--since does not move the watermark", get_sync_state(rc.SYNC_KEY) == START_WM,
      get_sync_state(rc.SYNC_KEY))
check("--since still writes what it finds", count("SELECT count(*) FROM orders") == 1)


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("All reconciliation checks passed.")
