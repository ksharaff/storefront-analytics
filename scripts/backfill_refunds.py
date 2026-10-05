"""Backfill refunds and refund line items.

    python -m scripts.backfill_refunds              # resumable
    python -m scripts.backfill_refunds --limit 20   # small test run
    python -m scripts.backfill_refunds --restart    # ignore saved progress
    python -m scripts.backfill_refunds --scan-all   # check EVERY order (slow)

Run this AFTER scripts.backfill_orders. It depends on the orders table.

Why this is a separate pass
---------------------------
Refund line-item detail — which products came back and how many — only exists
at GET /wc/v3/orders/{id}/refunds. One call per order. On a store with ~190,000 orders and
~4 seconds per request that is about nine days of continuous requests, which is not
a real option.

The order payload already tells us which orders have refunds: it carries an
inline `refunds` array, which backfill_orders keeps in the stored `raw` JSON.
So this pass only calls the endpoint for orders where that array is non-empty
— typically a few percent of all orders. Hours instead of days, for exactly the same result.

`--scan-all` exists as an escape hatch if the inline array is ever wrong, but
it is the nine-day path. Do not reach for it casually.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone

from app.db import connect, get_sync_state, set_sync_state
from app.woo import get

SYNC_KEY = "refunds_last_order_id"

# Orders known to carry at least one refund, from the inline array that
# backfill_orders stored. Ascending id so progress is a single watermark.
CANDIDATES = """
SELECT id FROM orders
 WHERE jsonb_array_length(COALESCE(raw -> 'refunds', '[]'::jsonb)) > 0
   AND id > %s
 ORDER BY id
"""

ALL_ORDERS = "SELECT id FROM orders WHERE id > %s ORDER BY id"

UPSERT_REFUND = """
INSERT INTO refunds (id, order_id, amount, reason, date_created_gmt, raw, synced_at)
VALUES (%(id)s, %(order_id)s, %(amount)s, %(reason)s, %(date_created_gmt)s,
        %(raw)s, now())
ON CONFLICT (id) DO UPDATE SET
    amount           = EXCLUDED.amount,
    reason           = EXCLUDED.reason,
    date_created_gmt = EXCLUDED.date_created_gmt,
    raw              = EXCLUDED.raw,
    synced_at        = now()
"""

INSERT_REFUND_ITEM = """
INSERT INTO refund_items (refund_id, line_item_id, product_id, variation_id,
                          name, quantity, total)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (refund_id, line_item_id) DO NOTHING
"""


def parse_dt(value):
    """WooCommerce returns '2021-01-01T10:00:00' with no zone. It is UTC."""
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def positive_money(value) -> float:
    """WooCommerce reports refund money as negative. The schema stores it
    positive and enforces that with a CHECK, so flip the sign here."""
    if value in (None, ""):
        return 0.0
    return abs(float(value))


def positive_int(value) -> int:
    """Same convention for refunded quantities."""
    if value in (None, ""):
        return 0
    return abs(int(value))


def write_refunds(cur, order_id: int, refunds: list) -> int:
    """Store one order's refunds and their line items. Returns rows written."""
    written = 0
    for refund in refunds:
        date_created = parse_dt(refund.get("date_created_gmt"))
        if date_created is None:
            # The schema requires this column. A refund without a date is
            # malformed; skip it loudly rather than inventing a timestamp.
            print(f"    order {order_id}: refund {refund.get('id')} has no "
                  f"date_created_gmt — skipped")
            continue

        cur.execute(UPSERT_REFUND, {
            "id":               refund["id"],
            "order_id":         order_id,
            "amount":           positive_money(refund.get("amount")),
            "reason":           (refund.get("reason") or "").strip() or None,
            "date_created_gmt": date_created,
            "raw":              json.dumps(refund, ensure_ascii=False),
        })

        # Replace line items wholesale — same reasoning as order items.
        cur.execute("DELETE FROM refund_items WHERE refund_id = %s",
                    (refund["id"],))
        for item in refund.get("line_items") or []:
            cur.execute(INSERT_REFUND_ITEM, (
                refund["id"],
                item.get("id"),
                item.get("product_id"),
                item.get("variation_id") or 0,
                item.get("name"),
                positive_int(item.get("quantity")),
                positive_money(item.get("total")),
            ))
        written += 1
    return written


def run(scan_all: bool, limit: int | None, restart: bool):
    watermark = 0 if restart else int(get_sync_state(SYNC_KEY, 0) or 0)
    if watermark:
        print(f"Resuming after order {watermark}.")

    sql = ALL_ORDERS if scan_all else CANDIDATES
    with connect() as conn:
        order_ids = [r[0] for r in conn.execute(sql, (watermark,)).fetchall()]
        total_orders = conn.execute("SELECT count(*) FROM orders").fetchone()[0]

    if not order_ids and not scan_all and total_orders and not watermark:
        # The guard against a silent no-op: if backfill_orders ever stopped
        # requesting the `refunds` field, every candidate query would come
        # back empty and this pass would "succeed" having done nothing.
        print("No orders carry an inline `refunds` array, yet the orders table "
              "is populated.\nThat is suspicious — check that ORDER_FIELDS in "
              "backfill_orders.py still includes 'refunds',\nor re-run with "
              "--scan-all (very slow).")
        return 1

    if limit:
        order_ids = order_ids[:limit]

    if not order_ids:
        print("Nothing to do — all candidate orders already processed.")
        return 0

    # The store needs roughly 4 seconds per request, so say so up front.
    estimate = len(order_ids) * 4 / 3600
    print(f"{len(order_ids):,} orders to check. "
          f"At ~4s per request that is roughly {estimate:.1f} hours.\n")

    started = time.time()
    refunds_written = failures = 0

    # The watermark may only advance across an unbroken run of successes.
    # Orders are processed in ascending id, so once one fails we stop moving
    # it: a plain re-run then comes back to that order and everything after
    # it. Advancing past a failure would silently skip it forever.
    safe_watermark = watermark
    hit_failure = False

    for n, order_id in enumerate(order_ids, 1):
        try:
            refunds, _ = get(f"wc/v3/orders/{order_id}/refunds",
                             {"per_page": 100})
        except Exception as exc:
            # One unreachable order must not end a multi-hour run.
            failures += 1
            hit_failure = True
            print(f"    order {order_id}: {exc}" + " " * 20)
            continue

        if refunds:
            with connect() as conn:
                refunds_written += write_refunds(conn.cursor(), order_id, refunds)

        if not hit_failure:
            safe_watermark = order_id
            set_sync_state(SYNC_KEY, str(order_id))

        if n % 25 == 0 or n == len(order_ids):
            elapsed = time.time() - started
            rate = n / elapsed if elapsed else 0
            remaining = (len(order_ids) - n) / rate / 60 if rate else 0
            print(f"  {n:,}/{len(order_ids):,} orders · "
                  f"{refunds_written:,} refunds · "
                  f"{remaining:.0f} min left", end="\r")

    print(f"\n\nDone. {refunds_written:,} refunds written from "
          f"{len(order_ids):,} orders.")
    if failures:
        print(f"{failures} order(s) could not be fetched. Progress was held at "
              f"order {safe_watermark}, so simply re-running this script "
              f"retries them along with everything after.")

    with connect() as conn:
        items = conn.execute("SELECT count(*) FROM refund_items").fetchone()[0]
        orders_with = conn.execute(
            "SELECT count(DISTINCT order_id) FROM refunds").fetchone()[0]
    print(f"Totals in the database: {orders_with:,} orders with refunds, "
          f"{items:,} refunded line items.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="Process at most N orders.")
    parser.add_argument("--restart", action="store_true",
                        help="Ignore saved progress and start from the beginning.")
    parser.add_argument("--scan-all", action="store_true",
                        help="Check every order, not just those with an inline "
                             "refunds array. Takes days — see the docstring.")
    args = parser.parse_args()
    return run(args.scan_all, args.limit, args.restart)


if __name__ == "__main__":
    sys.exit(main())
