"""Compare what we stored for a month against what the store reports.

    python -m scripts.verify_window 2026-01

Independent check on the backfill: asks WooCommerce how many orders exist in
the window, counts what actually landed in the database, and breaks the result
down by status so a discrepancy points somewhere useful rather than just
being a number that does not match.

Uses the same window arithmetic as backfill_orders, so this is a genuine
comparison and not a differently-phrased question.
"""

import sys
from datetime import datetime, timedelta

from app.db import connect
from app.woo import total_count


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    month = sys.argv[1]
    start = datetime.strptime(month, "%Y-%m")
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)

    # Identical to backfill_orders.fetch_window: `after` is exclusive, so it
    # is nudged back one second; `before` is the next window's start.
    after = (start - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S")
    before = end.strftime("%Y-%m-%dT%H:%M:%S")

    print(f"Window {month}: after={after}  before={before}  (GMT)\n")

    store = total_count("wc/v3/orders", {
        "status": "any", "after": after, "before": before,
        "dates_are_gmt": "true",
    })

    with connect() as conn:
        local = conn.execute(
            "SELECT count(*) FROM orders "
            " WHERE date_created_gmt >= %s AND date_created_gmt < %s",
            (start, end),
        ).fetchone()[0]

        by_status = conn.execute("""
            SELECT o.status, COALESCE(g.status_group, 'UNCLASSIFIED') AS grp,
                   count(*)
              FROM orders o
              LEFT JOIN order_status_groups g ON g.status = o.status
             WHERE o.date_created_gmt >= %s AND o.date_created_gmt < %s
             GROUP BY 1, 2
             ORDER BY 3 DESC
        """, (start, end)).fetchall()

        # Sanity checks that a wrong count alone would not reveal.
        no_email = conn.execute(
            "SELECT count(*) FROM orders"
            " WHERE date_created_gmt >= %s AND date_created_gmt < %s"
            "   AND billing_email IS NULL", (start, end)).fetchone()[0]
        no_items = conn.execute("""
            SELECT count(*) FROM orders o
             WHERE o.date_created_gmt >= %s AND o.date_created_gmt < %s
               AND NOT EXISTS (SELECT 1 FROM order_items i
                                WHERE i.order_id = o.id)
        """, (start, end)).fetchone()[0]
        with_refunds = conn.execute("""
            SELECT count(*) FROM orders
             WHERE date_created_gmt >= %s AND date_created_gmt < %s
               AND jsonb_array_length(COALESCE(raw->'refunds','[]'::jsonb)) > 0
        """, (start, end)).fetchone()[0]

    print(f"  store: {store:,}")
    print(f"  local: {local:,}")
    print(f"  {'MATCH' if store == local else f'MISMATCH ({store - local:+,})'}\n")

    print("  by status:")
    for status, grp, count in by_status:
        flag = "  <-- not in order_status_groups" if grp == "UNCLASSIFIED" else ""
        print(f"    {status:<22} {grp:<13} {count:>7,}{flag}")

    print(f"\n  orders with no billing email: {no_email:,}")
    print(f"  orders with no line items:    {no_items:,}"
          + ("   (worth a look)" if no_items else ""))
    print(f"  orders carrying refunds:      {with_refunds:,}"
          f"   <- what backfill_refunds will fetch for this month")

    return 0 if store == local else 1


if __name__ == "__main__":
    sys.exit(main())
