"""Backfill historical orders and their line items from WooCommerce.

Run from the project root with the venv active:

    python -m scripts.backfill_orders                 # full history, resumable
    python -m scripts.backfill_orders --start 2026-01 # from a given month
    python -m scripts.backfill_orders --months 1      # one window, for testing
    python -m scripts.backfill_orders --restart       # ignore saved progress

Why it is built this way (see docs/DESIGN.md):

  * DATE WINDOWS, NOT page=1..1908. WooCommerce turns `page=N` into a SQL
    OFFSET, so deep pages make MySQL scan and throw away everything before
    them. On a store that needs ~4 seconds for a single order, that degrades
    to unusable. Walking month by month keeps every page shallow.

  * status=any, ALWAYS. This store has a status (`custom-refunded`) that
    WooCommerce's own registry does not recognise; querying it by name
    returns HTTP 400, and any per-status loop would silently drop its 2,645
    orders. `status=any` is the only filter proven to return every order.

  * RESUMABLE. The last completed window is written to `sync_state`. A crash
    or a dropped connection costs one month, not the whole run.

  * IDEMPOTENT. Every order is upserted by WooCommerce ID and only applied if
    it is not older than what is already stored, so re-running a window — or
    the whole backfill — is safe and changes nothing.

Expect hours, not minutes. Run it unattended.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone

from app.config import HISTORY_START
from app.db import connect, get_sync_state, set_sync_state
from app.woo import get, total_count

# --- What we ask WooCommerce for --------------------------------------------
# Trimming the payload matters a lot here: a bare order response on this store
# drags along a large `meta_data` array contributed by 28 active plugins, none
# of which the dashboard uses. Listing fields explicitly excludes it.
#
# `billing` is requested whole because WooCommerce's _fields does not reliably
# support nested paths, but ONLY the email survives into the database — see
# slim_order(). The rest (name, phone, street address) is real customer data
# and is dropped before anything is written.
ORDER_FIELDS = ",".join([
    "id", "number", "status", "currency",
    "total", "shipping_total", "total_tax", "discount_total",
    "payment_method", "payment_method_title",
    "customer_id", "billing", "created_via",
    "date_created_gmt", "date_paid_gmt", "date_modified_gmt",
    "line_items", "refunds",
])

PER_PAGE = 100          # WooCommerce's maximum
SYNC_KEY = "backfill_last_window"

# How long to keep trying a window after the network drops, before giving up.
# app.woo.get already retries an individual request three times over ~6
# seconds, which covers a hiccup but not a real outage — a dropped wifi
# connection fails instantly, so those three attempts are spent in under ten
# seconds. This ladder sits above that and retries the whole window, adding up
# to about 48 minutes of tolerance. A laptop that loses its connection for
# half an hour overnight resumes by itself instead of being found dead in the
# morning with one window of work lost.
WINDOW_RETRY_WAITS = [30, 60, 120, 300, 600, 600, 600]


# --- Small helpers -----------------------------------------------------------

def month_windows(start: datetime, end: datetime):
    """Yield (window_start, window_end) pairs, one calendar month each."""
    cur = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cur < end:
        # Jump to day 28 then add 4 days: lands in the next month for every
        # month length, including February in a leap year.
        nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        yield cur, nxt
        cur = nxt


def iso(dt: datetime) -> str:
    """WooCommerce wants ISO8601 without a timezone suffix."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def parse_dt(value):
    """WooCommerce returns '2021-01-01T10:00:00' with no zone. It is UTC."""
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def money(value) -> float:
    """Money arrives as a string, and sometimes as an empty one."""
    if value in (None, ""):
        return 0
    return float(value)


def slim_order(order: dict) -> dict:
    """Strip customer PII before the payload is stored as `raw`.

    A WooCommerce store holds real customer names, phone
    numbers and street addresses. The dashboard needs exactly one field from
    the billing block — the email, used to count unique customers, because
    guest checkouts all share customer_id = 0. Everything else is discarded
    here so it never reaches our database at all.
    """
    slim = dict(order)
    billing = slim.pop("billing", None) or {}
    slim["billing_email"] = billing.get("email") or None
    return slim


# --- Database writes ---------------------------------------------------------

UPSERT_ORDER = """
INSERT INTO orders (
    id, number, status, currency,
    total, shipping_total, total_tax, discount_total,
    payment_method, payment_method_title,
    customer_id, billing_email, created_via,
    date_created_gmt, date_paid_gmt, date_modified_gmt,
    raw, synced_at
) VALUES (
    %(id)s, %(number)s, %(status)s, %(currency)s,
    %(total)s, %(shipping_total)s, %(total_tax)s, %(discount_total)s,
    %(payment_method)s, %(payment_method_title)s,
    %(customer_id)s, %(billing_email)s, %(created_via)s,
    %(date_created_gmt)s, %(date_paid_gmt)s, %(date_modified_gmt)s,
    %(raw)s, now()
)
ON CONFLICT (id) DO UPDATE SET
    number               = EXCLUDED.number,
    status               = EXCLUDED.status,
    currency             = EXCLUDED.currency,
    total                = EXCLUDED.total,
    shipping_total       = EXCLUDED.shipping_total,
    total_tax            = EXCLUDED.total_tax,
    discount_total       = EXCLUDED.discount_total,
    payment_method       = EXCLUDED.payment_method,
    payment_method_title = EXCLUDED.payment_method_title,
    customer_id          = EXCLUDED.customer_id,
    billing_email        = EXCLUDED.billing_email,
    created_via          = EXCLUDED.created_via,
    date_created_gmt     = EXCLUDED.date_created_gmt,
    date_paid_gmt        = EXCLUDED.date_paid_gmt,
    date_modified_gmt    = EXCLUDED.date_modified_gmt,
    raw                  = EXCLUDED.raw,
    synced_at            = now()
-- The ordering guard. A webhook that arrives late must not overwrite a
-- fresher row already written by reconciliation. No row is returned when
-- this WHERE rejects the update, which is how we know to leave the
-- existing line items alone too.
WHERE orders.date_modified_gmt IS NULL
   OR EXCLUDED.date_modified_gmt IS NULL
   OR EXCLUDED.date_modified_gmt >= orders.date_modified_gmt
RETURNING id
"""

INSERT_ITEM = """
INSERT INTO order_items (
    order_id, line_item_id, product_id, variation_id,
    name, sku, quantity, subtotal, total, total_tax
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (order_id, line_item_id) DO NOTHING
"""


def write_order(cur, order: dict) -> bool:
    """Upsert one order and its line items. Returns True if it was applied."""
    slim = slim_order(order)

    params = {
        "id":                   slim["id"],
        "number":               slim.get("number"),
        "status":               slim.get("status"),
        "currency":             slim.get("currency") or "SAR",
        "total":                money(slim.get("total")),
        "shipping_total":       money(slim.get("shipping_total")),
        "total_tax":            money(slim.get("total_tax")),
        "discount_total":       money(slim.get("discount_total")),
        "payment_method":       slim.get("payment_method") or None,
        "payment_method_title": slim.get("payment_method_title") or None,
        "customer_id":          slim.get("customer_id") or 0,
        "billing_email":        slim.get("billing_email"),
        "created_via":          slim.get("created_via"),
        "date_created_gmt":     parse_dt(slim.get("date_created_gmt")),
        "date_paid_gmt":        parse_dt(slim.get("date_paid_gmt")),
        "date_modified_gmt":    parse_dt(slim.get("date_modified_gmt")),
        # Stored as jsonb. Passing JSON text lets Postgres parse it directly.
        "raw":                  json.dumps(slim, ensure_ascii=False),
    }

    row = cur.execute(UPSERT_ORDER, params).fetchone()
    if row is None:
        # Stored copy is newer. Leave it, and its line items, untouched.
        return False

    # Replace line items wholesale rather than diffing them: an order that
    # changed may have had items added, removed or re-quantified, and the set
    # is small (a handful of rows per order).
    cur.execute("DELETE FROM order_items WHERE order_id = %s", (slim["id"],))
    for item in slim.get("line_items") or []:
        cur.execute(INSERT_ITEM, (
            slim["id"],
            item.get("id"),
            item.get("product_id"),
            item.get("variation_id") or 0,
            item.get("name"),
            item.get("sku"),
            item.get("quantity") or 0,
            money(item.get("subtotal")),
            money(item.get("total")),
            money(item.get("total_tax")),
        ))
    return True


# --- The window loop ---------------------------------------------------------

def fetch_window(win_start: datetime, win_end: datetime):
    """Yield every order created in [win_start, win_end), page by page."""
    page = 1
    while True:
        orders, headers = get("wc/v3/orders", {
            "status": "any",          # never a whitelist — see the docstring
            "after": iso(win_start - timedelta(seconds=1)),  # `after` is exclusive
            "before": iso(win_end),                          # `before` is exclusive
            "dates_are_gmt": "true",  # compare against *_gmt, not site-local time
            "orderby": "id",
            "order": "asc",           # stable ordering so pages cannot shuffle
            "per_page": PER_PAGE,
            "page": page,
            "_fields": ORDER_FIELDS,
        })
        if not orders:
            return

        yield orders

        total_pages = int(headers.get("X-WP-TotalPages", 1))
        if page >= total_pages:
            return
        page += 1


def run(start: datetime, end: datetime, max_windows: int | None):
    windows = list(month_windows(start, end))
    if max_windows:
        windows = windows[:max_windows]

    print(f"Backfilling {len(windows)} window(s): "
          f"{windows[0][0]:%Y-%m} → {windows[-1][0]:%Y-%m}\n")

    grand_total = 0
    for win_start, win_end in windows:
        label = f"{win_start:%Y-%m}"
        attempt = 0

        while True:
            # Counters reset per attempt: a retry re-walks the window from
            # page 1, which is safe because every write is an upsert.
            applied = skipped = 0
            try:
                for page_of_orders in fetch_window(win_start, win_end):
                    # Commit per page, not per window. A window can hold
                    # thousands of orders; committing as we go means a crash
                    # loses at most 100, and re-running is harmless.
                    with connect() as conn:
                        cur = conn.cursor()
                        for order in page_of_orders:
                            if write_order(cur, order):
                                applied += 1
                            else:
                                skipped += 1
                    print(f"  {label}: {applied + skipped} orders processed",
                          end="\r")
                break

            except KeyboardInterrupt:
                # Deliberate stop. Everything up to the previous window is
                # already recorded, so say how to pick it back up.
                print(f"\n\nStopped during {label}. "
                      f"Re-run without --start to resume from here.")
                raise SystemExit(130)

            except Exception as exc:
                if attempt >= len(WINDOW_RETRY_WAITS):
                    # Out of patience. Exit cleanly rather than with a
                    # traceback: the run is resumable and the user needs to
                    # know that, not a stack trace.
                    print(f"\n\n{label} failed after "
                          f"{len(WINDOW_RETRY_WAITS)} retries: {exc}")
                    print(f"Progress is saved through the previous window. "
                          f"Once the connection is back, re-run:")
                    print(f"    python -m scripts.backfill_orders")
                    print(f"(no --start — it resumes from sync_state)")
                    raise SystemExit(1)

                wait = WINDOW_RETRY_WAITS[attempt]
                attempt += 1
                print(f"\n  {label}: {exc}")
                print(f"  retry {attempt}/{len(WINDOW_RETRY_WAITS)} "
                      f"in {wait}s...")
                time.sleep(wait)

        # Only now is the window complete and safe to record as done.
        set_sync_state(SYNC_KEY, label)
        grand_total += applied + skipped
        note = f" ({skipped} already newer)" if skipped else ""
        retried = f" (after {attempt} retr{'y' if attempt == 1 else 'ies'})" if attempt else ""
        print(f"  {label}: {applied + skipped} orders{note}{retried}" + " " * 20)

    print(f"\nDone. {grand_total:,} orders processed in this run.")
    return grand_total


def reconcile():
    """Compare what we stored against what the store reports."""
    print("\nReconciling...")
    store_total = total_count("wc/v3/orders", {"status": "any"})

    with connect() as conn:
        db_total = conn.execute("SELECT count(*) FROM orders").fetchone()[0]
        unknown = conn.execute("""
            SELECT o.status, count(*)
              FROM orders o
              LEFT JOIN order_status_groups g ON g.status = o.status
             WHERE g.status IS NULL
             GROUP BY o.status
             ORDER BY 2 DESC
        """).fetchall()

    print(f"  store: {store_total:,} orders")
    print(f"  local: {db_total:,} orders")

    if unknown:
        # Exactly the failure mode that hid custom-refunded for so long: a
        # status nobody classified, quietly absent from every metric.
        print("\n  UNCLASSIFIED STATUSES — add these to order_status_groups:")
        for status, count in unknown:
            print(f"    {status}: {count:,}")

    if db_total == store_total:
        print("\n  Counts match.")
        return True
    print(f"\n  Difference: {store_total - db_total:,}. "
          "Expected if this was a partial run; investigate if it was a full one.")
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", help="First month, YYYY-MM. Default: resume, "
                                        "or the start of store history.")
    parser.add_argument("--months", type=int, help="Stop after N windows. "
                                                   "Use --months 1 to test.")
    parser.add_argument("--restart", action="store_true",
                        help="Ignore saved progress and start from the beginning.")
    parser.add_argument("--no-reconcile", action="store_true",
                        help="Skip the count check at the end.")
    args = parser.parse_args()

    if args.start:
        start = datetime.strptime(args.start, "%Y-%m")
    elif args.restart:
        start = datetime.strptime(HISTORY_START, "%Y-%m-%d")
    else:
        last = get_sync_state(SYNC_KEY)
        if last:
            # Resume at the month after the last completed one.
            done = datetime.strptime(last, "%Y-%m")
            start = (done.replace(day=28) + timedelta(days=4)).replace(day=1)
            print(f"Resuming after {last}.")
        else:
            start = datetime.strptime(HISTORY_START, "%Y-%m-%d")

    end = datetime.utcnow()
    if start >= end:
        print("Nothing to do — already caught up.")
        return 0

    run(start, end, args.months)

    if not args.no_reconcile and not args.months:
        reconcile()
    return 0


if __name__ == "__main__":
    sys.exit(main())
