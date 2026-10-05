"""Read delivery times and shipping costs from OTO (tryoto.com) for the
dashboard's Warehouse tab (2026-09-30).

    python -m scripts.sync_oto_tracking              # the last 90 days (OTO's limit)
    python -m scripts.sync_oto_tracking --days 7     # a quicker look at the last week

Every OTO shipment counts, not only the orders scanned onto the truck in the
storage app. Two steps:

  1. OTO's shipment list for the last N days (shipmentTransactions): per
     order, the carrier, when the shipment was created and what the delivery
     company charged. Returns and cancelled shipments are left out.
  2. For each of those orders not delivered yet in our table, OTO's status
     history (orderDetails): when the carrier picked it up and when it was
     delivered. Delivered orders are never asked again, and neither are
     cancelled or returned ones, so after the first run this step is small.

The results are kept in the oto_orders table (db/09_oto_tracking.sql). OTO only
shows about 90 days, so run this at least once a month; daily is best. What
was read stays in our table after OTO stops showing it.

The FIRST run asks OTO about every order of the last 90 days, one by one
(5 at a time). On production that can take a while; it prints its progress.

Needs OTO_REFRESH_TOKEN in .env, like the storage app's OTO sync.
READ-ONLY towards OTO: it creates, changes and cancels nothing there.
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone

from app import oto
from app.db import connect
from app.metrics import cursor

MAX_DAYS = 90          # OTO does not show shipments older than this
BATCH = 200            # status histories asked per progress line


def window(now: datetime, days: int):
    """(first day, last day) in Riyadh dates, both included: `days` days
    ending today, never more than OTO's 90."""
    days = max(1, min(days, MAX_DAYS))
    today = now.astimezone(oto.OTO_TZ).date()
    return today - timedelta(days=days - 1), today


def store_shipments(cur, orders: dict[str, dict], now: datetime) -> int:
    """Upsert one row per order. A known value is never replaced by
    "unknown": OTO may answer without the cost one day and with it the next."""
    for o in orders.values():
        cur.execute("""
            INSERT INTO oto_orders (order_number, carrier, shipment_no, created_at, status, charge, synced_at)
            VALUES (%(order_number)s, %(carrier)s, %(shipment_no)s, %(created_at)s, %(status)s,
                    %(charge)s, %(now)s)
            ON CONFLICT (order_number) DO UPDATE SET
                carrier     = COALESCE(EXCLUDED.carrier, oto_orders.carrier),
                shipment_no = COALESCE(EXCLUDED.shipment_no, oto_orders.shipment_no),
                -- the FIRST shipment decides the period, even if a later run
                -- only still sees a re-shipment
                created_at  = LEAST(EXCLUDED.created_at, oto_orders.created_at),
                -- once delivered, the status history's word is final
                status      = CASE WHEN oto_orders.delivered_at IS NOT NULL THEN oto_orders.status
                                   ELSE COALESCE(EXCLUDED.status, oto_orders.status) END,
                charge      = COALESCE(EXCLUDED.charge, oto_orders.charge),
                synced_at   = EXCLUDED.synced_at
        """, {**o, "now": now})
    return len(orders)


def orders_to_ask(cur, since: datetime) -> list[str]:
    """Orders whose shipment was created since `since` (so OTO still shows
    them) and that are not delivered yet in our table. Cancelled and returned
    ones are dropped by the caller (oto.is_finished_undelivered)."""
    rows = cur.execute("""
        SELECT order_number, status FROM oto_orders
        WHERE created_at >= %(since)s AND delivered_at IS NULL
        ORDER BY created_at
    """, {"since": since}).fetchall()
    return [r["order_number"] for r in rows if not oto.is_finished_undelivered(r["status"])]


def store_history(cur, found: dict[str, dict], now: datetime):
    for number, h in found.items():
        cur.execute("""
            UPDATE oto_orders SET
                status          = COALESCE(%(status)s, status),
                picked_up_at    = COALESCE(picked_up_at, %(picked_up_at)s),
                delivered_at    = COALESCE(%(delivered_at)s, delivered_at),
                history_read_at = %(now)s
            WHERE order_number = %(number)s
        """, {**h, "number": number, "now": now})


def run(days: int = MAX_DAYS, list_shipments=None, fetch_history=None,
        now: datetime | None = None, progress=None) -> dict:
    """Read OTO and store what it says. The tests pass fake list_shipments
    and fetch_history. Returns {orders, asked, delivered, failed, first, last}."""
    list_shipments = list_shipments or oto.list_shipments
    fetch_history = fetch_history or oto.fetch_history
    now = now or datetime.now(timezone.utc)
    first, last = window(now, days)
    since = datetime.combine(first, datetime.min.time(), tzinfo=oto.OTO_TZ)

    # Step 1: the shipment list. If it fails, nothing is written.
    orders = oto.per_order(list_shipments(first.isoformat(), last.isoformat()))
    with connect() as conn:
        cur = cursor(conn)
        n_orders = store_shipments(cur, orders, now)
        to_ask = orders_to_ask(cur, since)

    # Step 2: status histories, committed batch by batch, so a stopped run
    # keeps what it already read.
    delivered = failed = 0
    for i in range(0, len(to_ask), BATCH):
        batch = to_ask[i:i + BATCH]
        found, bad = fetch_history(batch)
        failed += bad
        delivered += sum(1 for h in found.values() if h.get("delivered_at"))
        with connect() as conn:
            store_history(cursor(conn), found, now)
        if progress:
            progress(min(i + BATCH, len(to_ask)), len(to_ask))
    return {"orders": n_orders, "asked": len(to_ask), "delivered": delivered,
            "failed": failed, "first": first.isoformat(), "last": last.isoformat()}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=int, default=MAX_DAYS,
                    help=f"shipments created in the last N days (default and maximum {MAX_DAYS})")
    args = ap.parse_args()
    show = lambda done, total: print(f"  status histories read: {done} of {total}", flush=True)
    try:
        r = run(args.days, progress=show)
    except oto.OtoError as e:
        sys.exit(f"OTO: {e}")
    print(f"OTO shipments {r['first']} to {r['last']}: {r['orders']} order(s). "
          f"Asked for {r['asked']} status histories: {r['delivered']} delivered, "
          f"{r['failed']} could not be read. Nothing was changed in OTO.")


if __name__ == "__main__":
    main()
