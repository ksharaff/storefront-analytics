"""Reconciliation: keep the local database current after the backfill.

    python -m scripts.reconcile                         # one run (for a scheduler)
    python -m scripts.reconcile --loop                  # every 3 minutes, for dev
    python -m scripts.reconcile --loop --every 120      # every 2 minutes
    python -m scripts.reconcile --since 2026-09-20T00:00:00   # one-off re-check

Each run asks WooCommerce for every order MODIFIED since the watermark and
writes it through exactly the same code as the backfill — `write_order` from
backfill_orders (PII stripping, upsert, date_modified ordering guard, line
items replaced) and `write_refunds` from backfill_refunds. Nothing about how
an order is stored is duplicated here.

Why this exists before webhooks (see docs/DESIGN.md, "Reconciliation"):
it needs only the existing read-only key, it meets "within a few minutes" on
its own, and it stays correct whether or not webhooks are ever added.

The watermark rule
------------------
The run's start time is recorded BEFORE fetching, and the watermark only
moves to it after the WHOLE run succeeded. Any failure — a page that would
not load, one order whose refunds could not be fetched, the result set
shifting mid-walk — leaves the watermark where it was, so the next run simply
covers the same range again. Re-covering is harmless because every write is an
upsert. This is the same principle as the backfill_refunds watermark fix.

Known limitation (acceptable for v1): orders that are trashed or permanently
deleted in WooCommerce stop appearing in `status=any` results, so they are
never reported as "modified" and would linger locally. The fix, when needed,
is the order.deleted webhook or a periodic id-set comparison.
"""

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone

from app.config import HISTORY_START
from app.db import connect, get_sync_state, set_sync_state
from app.woo import get

# Reused, not copied. If how an order or refund is written ever changes, it
# changes in one place and reconciliation follows automatically.
from scripts.backfill_orders import ORDER_FIELDS, PER_PAGE, iso, parse_dt, write_order
from scripts.backfill_refunds import write_refunds

# Its own key, separate from the backfill's. Holds a UTC timestamp in the same
# '2026-09-23T10:15:00' form WooCommerce accepts.
SYNC_KEY = "last_reconciled_at"

# Used only when no watermark exists yet. Safely before the orders backfill
# started (2026-09-23), so nothing written between "backfill fetched that
# month" and "reconciliation took over" can fall through the gap. The overlap
# costs a few extra requests on the first run, nothing more.
FIRST_WATERMARK = "2026-09-01T00:00:00"

# The new watermark is the run's start time MINUS this margin. Two reasons:
#   * Our clock and the store's clock are different machines. If ours runs
#     a little ahead, a watermark taken from it would skip orders the store
#     stamped a moment "earlier".
#   * On a slow store, an order can be stamped with date_modified at the
#     start of a save that only commits seconds later. A run landing in that
#     gap cannot see it yet; the next run, looking back 5 minutes, will.
# The cost is re-fetching a few recent orders each run — harmless upserts.
OVERLAP = timedelta(minutes=5)

DEFAULT_EVERY = 180  # seconds between runs in --loop mode

# Orders created before this are outside the import scope (see config.py).
# `modified_after` selects by MODIFICATION date, so an old 2023 order edited
# today would otherwise come back and be written into a 2026-only database.
SCOPE_START = datetime.strptime(HISTORY_START, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    """The current UTC time. A function so the tests can freeze the clock."""
    return datetime.now(timezone.utc)


# --- Fetch --------------------------------------------------------------------

def fetch_changed(since: str):
    """Every order modified after `since`. Returns (orders, stable).

    `stable` is False if the result set changed size while we were paging
    through it. Pages are cut from an id-ordered list; if an order joins or
    leaves that list mid-walk, everything after it shifts by one and an order
    can slip between two pages unseen. We cannot prevent that on a live
    store, but we can notice it (X-WP-Total moves) and refuse to advance the
    watermark, so the next run re-covers the range.
    """
    orders_by_id = {}
    first_total = last_total = None
    page = 1
    while True:
        orders, headers = get("wc/v3/orders", {
            "status": "any",            # never a whitelist: custom-refunded is
                                        # unregistered and returns HTTP 400
            "modified_after": since,
            "dates_are_gmt": "true",    # compare against date_modified_gmt
            "orderby": "id",
            "order": "asc",             # stable ordering across pages
            "per_page": PER_PAGE,
            "page": page,
            "_fields": ORDER_FIELDS,    # same trimmed payload as the backfill
        })

        total = headers.get("X-WP-Total")
        if total is not None:
            if first_total is None:
                first_total = int(total)
            last_total = int(total)

        # Keyed by id: if the list did shift, an order may appear on two
        # pages. Keeping one copy avoids writing it twice.
        for order in orders or []:
            orders_by_id[order["id"]] = order

        total_pages = int(headers.get("X-WP-TotalPages", 1) or 1)
        if not orders or page >= total_pages:
            break
        page += 1

    stable = first_total == last_total
    return list(orders_by_id.values()), stable


# --- Refunds for one order ---------------------------------------------------

def sync_refunds(order_id: int, has_inline_refunds: bool) -> tuple[int, int]:
    """Make this order's local refunds match the store. Returns (written, removed).

    Two cases:
      * The order's inline `refunds` array is non-empty: fetch the full refund
        detail (the only place refunded line items exist) and replace ours.
      * It is empty: the store says this order has no refunds. If we hold any,
        they were deleted in WooCommerce, so delete them here too. No API
        call needed for that.
    """
    refunds = []
    if has_inline_refunds:
        # Raises on failure; the caller records it and holds the watermark.
        refunds, _ = get(f"wc/v3/orders/{order_id}/refunds", {"per_page": 100})
        refunds = refunds or []

    keep_ids = [r["id"] for r in refunds]
    with connect() as conn:
        cur = conn.cursor()
        # Refunds we hold that the store no longer has. When keep_ids is
        # empty, `id = ANY('{}')` is false for every row, so this removes all
        # of the order's refunds. refund_items go with them (ON DELETE CASCADE).
        cur.execute(
            "DELETE FROM refunds WHERE order_id = %s AND NOT (id = ANY(%s))",
            (order_id, keep_ids),
        )
        removed = cur.rowcount
        # Same function the backfill uses: sign flip, reason trim, items
        # replaced wholesale, undated refunds skipped.
        written = write_refunds(cur, order_id, refunds) if refunds else 0
    return written, removed


# --- One run --------------------------------------------------------------------

def run_once(since_override: str | None = None) -> bool:
    """Reconcile once. Returns True if the run was clean and the watermark moved.

    With `since_override` the run is a one-off check: it fetches from that
    point but never touches the stored watermark. Otherwise a --since later
    than the real watermark could silently skip the gap between them.
    """
    # Taken BEFORE fetching. Anything modified after this moment is left for
    # the next run, which will start from (this moment - OVERLAP).
    run_start = utcnow()
    stored = get_sync_state(SYNC_KEY)
    since = since_override or stored or FIRST_WATERMARK
    stamp = f"{run_start:%Y-%m-%d %H:%M:%S}Z"

    try:
        orders, stable = fetch_changed(since)
    except Exception as exc:
        # The store is unreachable or answered with an error. Nothing was
        # written; the watermark stays, so the next run tries the same range.
        print(f"[{stamp}] fetch failed, watermark unchanged ({since}): {exc}")
        return False

    applied = older = out_of_scope = malformed = 0
    to_refresh = []   # (order_id, has_inline_refunds) for orders we applied

    # All order writes in one transaction. The set is small (minutes of
    # changes), and it commits before any slow refund requests start.
    with connect() as conn:
        cur = conn.cursor()
        for order in orders:
            created = parse_dt(order.get("date_created_gmt"))
            if created is None:
                # date_created_gmt is NOT NULL in the schema. Skip rather than
                # fail the whole run on every future attempt.
                print(f"  order {order.get('id')}: no date_created_gmt — skipped")
                malformed += 1
                continue
            if created < SCOPE_START:
                out_of_scope += 1
                continue

            if write_order(cur, order):
                applied += 1
                to_refresh.append((order["id"], bool(order.get("refunds"))))
            else:
                # The ordering guard rejected it: we already hold a copy at
                # least as new. Its refunds were handled when that copy landed.
                older += 1

    # Refunds, one order at a time. The endpoint is ~4 seconds per call, so
    # only orders that actually carry refunds make a request.
    refund_written = refund_removed = 0
    refund_failed = []
    for order_id, has_inline in to_refresh:
        try:
            w, r = sync_refunds(order_id, has_inline)
            refund_written += w
            refund_removed += r
        except Exception as exc:
            # Do not stop: the other orders are still worth syncing. But the
            # run is no longer clean, so the watermark must not move.
            refund_failed.append(order_id)
            print(f"  order {order_id}: refunds not fetched: {exc}")

    clean = stable and not refund_failed

    # --- Summary line ---------------------------------------------------------
    parts = [f"{len(orders)} changed", f"{applied} applied"]
    if older:
        parts.append(f"{older} already newer")
    if out_of_scope:
        parts.append(f"{out_of_scope} pre-{HISTORY_START[:4]} skipped")
    if malformed:
        parts.append(f"{malformed} malformed")
    if refund_written or refund_removed:
        parts.append(f"refunds +{refund_written}/-{refund_removed}")
    print(f"[{stamp}] since {since}: " + ", ".join(parts))

    if not stable:
        print("  result set changed while paging — watermark held; "
              "the next run re-covers this range")
    if refund_failed:
        print(f"  {len(refund_failed)} order(s) missing refunds "
              f"{refund_failed} — watermark held so the next run retries them")

    if since_override:
        print("  (--since run: stored watermark left as it was)")
        return clean

    if clean:
        new_watermark = iso(run_start - OVERLAP)
        set_sync_state(SYNC_KEY, new_watermark)
    return clean


def loop(every: int):
    """Run forever, every `every` seconds. This is the production scheduler
    too: the "sync" service in deploy/docker-compose.yml runs --loop, and
    Docker restarts it if the process ever dies."""
    print(f"Reconciling every {every}s. Ctrl+C to stop.\n")
    try:
        while True:
            try:
                run_once()
            except Exception as exc:
                # Most likely the database is down. Say so and keep going;
                # the watermark has not moved, so nothing is lost.
                print(f"  run failed: {exc}")
            time.sleep(every)
    except KeyboardInterrupt:
        print("\nStopped. The watermark is saved; the next run picks up from it.")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--loop", action="store_true",
                        help="Keep running, every --every seconds.")
    parser.add_argument("--every", type=int, default=DEFAULT_EVERY,
                        help=f"Seconds between runs with --loop (default {DEFAULT_EVERY}).")
    parser.add_argument("--since",
                        help="One-off: fetch orders modified after this UTC time "
                             "(YYYY-MM-DDTHH:MM:SS). Does not move the watermark.")
    args = parser.parse_args()

    if args.since:
        if args.loop:
            parser.error("--since is a one-off check; it cannot be combined with --loop")
        # Validate early; WooCommerce would reject a malformed date with a 400.
        datetime.strptime(args.since, "%Y-%m-%dT%H:%M:%S")

    if args.loop:
        loop(args.every)
        return 0

    try:
        return 0 if run_once(args.since) else 1
    except Exception as exc:
        # Almost always the database (Docker not running). One line, not a
        # traceback: the watermark has not moved, so a re-run is all it takes.
        print(f"Reconciliation failed: {exc}\nThe watermark is unchanged; re-run once fixed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
