"""Check where the store's history actually begins, and what we import of it.

    python -m scripts.check_history_start

Two separate things, deliberately reported separately:

  * STORE_OLDEST_ORDER is a FACT — where the store's data really starts.
    If anything turns up before it, that fact is wrong and needs correcting.
  * HISTORY_START is a POLICY — how far back we choose to import. Orders
    before it are excluded on purpose, not by mistake.

Questions asked, so one odd answer cannot pass unnoticed:

  1. Does anything exist before STORE_OLDEST_ORDER? Should be 0.
  2. What is the oldest order, sorted ascending by date?
  3. What is the oldest order by id, which for WooCommerce is creation order?
  4. How many orders does the current HISTORY_START policy exclude? (Not an
     error — reported so the size of the choice is visible.)

(2) and (3) should agree. If they do not, something is unusual — an imported
order carrying a backdated creation date, for instance.

Costs five API calls.
"""

import sys
from datetime import datetime, timedelta

from app.config import HISTORY_START, STORE_OLDEST_ORDER
from app.woo import get, total_count


def show(label, order):
    if not order:
        print(f"  {label}: none returned")
        return None
    created = order.get("date_created_gmt")
    print(f"  {label}: order {order.get('id')} created {created} (GMT)")
    return created


def main():
    print(f"STORE_OLDEST_ORDER (fact)   = {STORE_OLDEST_ORDER}")
    print(f"HISTORY_START      (policy) = {HISTORY_START}\n")

    # 1. Anything before where we believe the store's data begins?
    before_count = total_count("wc/v3/orders", {
        "status": "any",
        "before": f"{STORE_OLDEST_ORDER}T00:00:00",
        "dates_are_gmt": "true",
    })
    print(f"Orders before STORE_OLDEST_ORDER ({STORE_OLDEST_ORDER}): "
          f"{before_count:,}")
    if before_count:
        print("  ^ That fact is WRONG — the store has older data than recorded.")
        print("    Correct STORE_OLDEST_ORDER in app/config.py.\n")
    else:
        print("  Nothing earlier — the recorded store start is correct.\n")

    # 2. Oldest by date.
    print("Oldest order, sorted by date ascending:")
    rows, _ = get("wc/v3/orders", {
        "status": "any", "orderby": "date", "order": "asc",
        "per_page": 1, "_fields": "id,date_created_gmt",
    })
    by_date = show("by date", rows[0] if rows else None)

    # 3. Oldest by id — a second opinion that does not rely on the date index.
    print("\nOldest order, sorted by id ascending:")
    rows, _ = get("wc/v3/orders", {
        "status": "any", "orderby": "id", "order": "asc",
        "per_page": 1, "_fields": "id,date_created_gmt",
    })
    by_id = show("by id", rows[0] if rows else None)

    if by_date and by_id and by_date[:7] != by_id[:7]:
        print("\n  These disagree. Start the backfill from the EARLIER month.")

    # 4. Whole-store total, for later reconciliation.
    total = total_count("wc/v3/orders", {"status": "any"})
    print(f"\nTotal orders in the store: {total:,}")

    # 5. What the current policy leaves out. Informational, not an error.
    excluded = total_count("wc/v3/orders", {
        "status": "any",
        "before": f"{HISTORY_START}T00:00:00",
        "dates_are_gmt": "true",
    })
    pct = excluded / total * 100 if total else 0
    print(f"\nHISTORY_START = {HISTORY_START} excludes {excluded:,} orders "
          f"({pct:.1f}% of the store).")
    if excluded:
        print("  This is the deliberate scope choice, not a fault. Any")
        print("  previous-period comparison reaching before this date will")
        print("  have nothing to compare against.")
        print(f"  To widen it later: python -m scripts.backfill_orders "
              f"--start <YYYY-MM>")
        print("  Existing rows are untouched — every write is an upsert.")

    print(f"\nTo import the configured range: "
          f"python -m scripts.backfill_orders --start {HISTORY_START[:7]}")
    return 0 if not before_count else 1


if __name__ == "__main__":
    sys.exit(main())
