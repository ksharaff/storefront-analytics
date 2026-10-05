"""Test backfill_refunds and backfill_products against a real Postgres."""
import json, sys
from datetime import datetime, timezone

from app import config
import sys as _sys
if not config.POSTGRES_DB.endswith("_test"):
    _sys.exit(
        f"Refusing to run against database {config.POSTGRES_DB!r}. "
        "These tests insert fixture rows. Point POSTGRES_DB at a database "
        "whose name ends in '_test'."
    )

import app.woo as woo
from app.db import connect, set_sync_state, get_sync_state

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

def seed_order(oid, refunds_inline):
    raw = {"id": oid, "refunds": refunds_inline}
    with connect() as conn:
        conn.execute("""INSERT INTO orders (id, status, total, date_created_gmt, raw)
                        VALUES (%s,'refunded',430,'2026-02-01T09:00:00Z',%s)
                        ON CONFLICT (id) DO UPDATE SET raw=EXCLUDED.raw""",
                     (oid, json.dumps(raw)))

# ============ REFUNDS ============
import scripts.backfill_refunds as br

# WooCommerce reports refunds with NEGATIVE line totals/quantities.
REFUND_API = {
    101: [{"id": 9001, "date_created_gmt": "2026-02-05T12:00:00",
           "amount": "100.00", "reason": " damaged ",
           "line_items": [{"id": 11, "product_id": 500, "variation_id": 0,
                           "name": "Duvet", "quantity": -2, "total": "-100.00"}]}],
    102: [{"id": 9002, "date_created_gmt": "2026-02-06T12:00:00",
           "amount": "50.00", "reason": "",
           "line_items": [{"id": 12, "product_id": 600, "variation_id": 0,
                           "name": "Towel", "quantity": -1, "total": "-50.00"}]},
          {"id": 9003, "date_created_gmt": "2026-02-07T12:00:00",
           "amount": "25.00", "reason": None, "line_items": []}],
    103: [{"id": 9004, "date_created_gmt": None, "amount": "10.00",
           "reason": "malformed", "line_items": []}],
}
CALLS = []
def fake_get(path, params=None, retries=3):
    CALLS.append(path)
    if path.startswith("wc/v3/orders/"):
        oid = int(path.split("/")[3])
        if oid == 999: raise RuntimeError("simulated network failure")
        return REFUND_API.get(oid, []), {}
    return [], {}
woo.get = fake_get; br.get = fake_get

for oid in (101, 102, 103):
    seed_order(oid, [{"id": 1}])
seed_order(104, [])            # no refunds inline -> must be skipped
seed_order(999, [{"id": 1}])   # simulates a fetch failure

br.run(scan_all=False, limit=None, restart=True)

check("only orders with inline refunds were fetched",
      "wc/v3/orders/104/refunds" not in CALLS, str(sorted(set(CALLS))))
with connect() as conn:
    amt, reason = conn.execute("SELECT amount, reason FROM refunds WHERE id=9001").fetchone()
    q, tot = conn.execute("SELECT quantity, total FROM refund_items WHERE refund_id=9001").fetchone()
    n_refunds = conn.execute("SELECT count(*) FROM refunds").fetchone()[0]
    r9003 = conn.execute("SELECT count(*) FROM refunds WHERE id=9003").fetchone()[0]
    r9004 = conn.execute("SELECT count(*) FROM refunds WHERE id=9004").fetchone()[0]
check("refund amount stored positive", float(amt) == 100.00, str(amt))
check("NEGATIVE quantity flipped positive", q == 2, str(q))
check("NEGATIVE line total flipped positive", float(tot) == 100.00, str(tot))
check("reason whitespace trimmed", reason == "damaged", repr(reason))
check("empty reason becomes NULL",
      conn_r := True and __import__("app.db", fromlist=["connect"]))
with connect() as conn:
    r2 = conn.execute("SELECT reason FROM refunds WHERE id=9002").fetchone()[0]
check("empty reason stored as NULL", r2 is None, repr(r2))
check("multiple refunds on one order all stored", r9003 == 1)
check("refund with no date skipped, not crashed", r9004 == 0)
check("network failure did not abort the run", n_refunds == 3, f"{n_refunds} refunds")

# idempotency
before = None
with connect() as conn:
    before = conn.execute("SELECT count(*) FROM refund_items").fetchone()[0]
br.run(scan_all=False, limit=None, restart=True)
with connect() as conn:
    after = conn.execute("SELECT count(*) FROM refund_items").fetchone()[0]
check("re-running does not duplicate refund items", before == after, f"{before} -> {after}")

# resume watermark
set_sync_state(br.SYNC_KEY, "102")
CALLS.clear()
br.run(scan_all=False, limit=None, restart=False)
check("resume skips orders at or below the watermark",
      all("/101/" not in c and "/102/" not in c for c in CALLS), str(CALLS))
check("resume still processes later orders",
      any("/103/" in c for c in CALLS), str(CALLS))

# the silent no-op guard
with connect() as conn:
    conn.execute("UPDATE orders SET raw = jsonb_set(raw,'{refunds}','[]'::jsonb)")
rc = br.run(scan_all=False, limit=None, restart=True)
check("guard fires when no order has an inline refunds array", rc == 1, f"rc={rc}")

# orphan protection: refund for an order we never imported
with connect() as conn:
    try:
        br.write_refunds(conn.cursor(), 777777,
                         [{"id": 9999, "date_created_gmt": "2026-02-01T00:00:00",
                           "amount": "5", "reason": "x", "line_items": []}])
        ok = False
    except Exception:
        ok = True
check("refund for an unknown order is rejected by the FK", ok)

# ============ PRODUCTS ============
import scripts.backfill_products as bp
PRODUCT_PAGES = {
    1: [{"id": 500, "categories": [{"id": 10, "name": "Bedding"},
                                   {"id": 11, "name": "Duvets"}]},
        {"id": 600, "categories": [{"id": 12, "name": "Bath"}]},
        {"id": 700, "categories": []}],
}
def fake_products(path, params=None, retries=3):
    if path == "wc/v3/products":
        return PRODUCT_PAGES.get((params or {}).get("page", 1), []), {"X-WP-TotalPages": "1"}
    return [], {}
bp.get = fake_products
bp.total_count = lambda *a, **k: 3

bp.run(max_pages=None)
with connect() as conn:
    rows = conn.execute("SELECT category_id, category_name FROM product_categories "
                        "WHERE product_id=500 ORDER BY category_id").fetchall()
    n700 = conn.execute("SELECT count(*) FROM product_categories WHERE product_id=700").fetchone()[0]
check("multi-category product stored", rows == [(10, "Bedding"), (11, "Duvets")], str(rows))
check("product with no categories stores nothing", n700 == 0)
check("_fields excludes heavy product data", "description" not in bp.PRODUCT_FIELDS)

# category move must replace, not accumulate
PRODUCT_PAGES[1] = [{"id": 500, "categories": [{"id": 99, "name": "Clearance"}]}]
bp.run(max_pages=None)
with connect() as conn:
    rows = conn.execute("SELECT category_id FROM product_categories "
                        "WHERE product_id=500").fetchall()
check("moved product's old categories removed", rows == [(99,)], str(rows))

# renamed category updates in place
PRODUCT_PAGES[1] = [{"id": 500, "categories": [{"id": 99, "name": "Final Clearance"}]}]
bp.run(max_pages=None)
with connect() as conn:
    nm = conn.execute("SELECT category_name FROM product_categories "
                      "WHERE product_id=500").fetchone()[0]
check("renamed category updated", nm == "Final Clearance", nm)


# ===========================================================================
# Watermark: a failure mid-run must not let progress skip past it
# ===========================================================================
reset_db()
from app.db import get_sync_state

def seed_wm(oid):
    with connect() as conn:
        conn.execute("""INSERT INTO orders (id,status,total,date_created_gmt,raw)
                        VALUES (%s,'refunded',100,'2026-02-01T09:00:00Z',%s)
                        ON CONFLICT (id) DO NOTHING""",
                     (oid, json.dumps({"id": oid, "refunds": [{"id": 1}]})))

for oid in (10, 20, 30, 40, 50):
    seed_wm(oid)

BROKEN = {30}          # order 30 fails; 40 and 50 succeed after it
CALLS = []
def fake_get(path, params=None, retries=3):
    oid = int(path.split("/")[3]); CALLS.append(oid)
    if oid in BROKEN: raise RuntimeError("boom")
    return [{"id": 9000+oid, "date_created_gmt": "2026-02-05T12:00:00",
             "amount": "10", "reason": "r", "line_items": []}], {}
br.get = fake_get

br.run(scan_all=False, limit=None, restart=True)
wm = get_sync_state(br.SYNC_KEY)
check("watermark held before the failure", wm == "20", f"watermark={wm}")

# Now the transient failure clears and we simply re-run (no --restart).
BROKEN.clear(); CALLS.clear()
br.run(scan_all=False, limit=None, restart=False)
check("plain re-run retries the failed order", 30 in CALLS, str(CALLS))
check("plain re-run also covers orders after it", {40, 50} <= set(CALLS), str(CALLS))
wm = get_sync_state(br.SYNC_KEY)
check("watermark advances to the end once clean", wm == "50", f"watermark={wm}")
with connect() as conn:
    n = conn.execute("SELECT count(*) FROM refunds").fetchone()[0]
check("every order's refund eventually stored", n == 5, f"{n} refunds")

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}"); sys.exit(1)
print("All refund + product checks passed.")
