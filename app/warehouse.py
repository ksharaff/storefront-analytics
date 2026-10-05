"""Warehouse metrics for the dashboard's Warehouse tab (2026-09-30).

Packing and loading come from the storage app's own records: packed_orders
(packed_at, shipped_at, boxes) and box_loads, plus the orders waiting to be
packed. Those numbers start on the day the storage app came into use; there
is no history before it. Delivery companies and delivery time come from OTO
instead (see below).

Definitions agreed on 2026-09-30:
  * READY TODAY = orders loaded onto the truck today + packed orders still
    waiting to be loaded right now (packed today or earlier).
    The headline is SHIPPED TODAY ÷ READY TODAY: "42 of 50 ready orders
    shipped today (84%)". "Shipped" means the last box was scanned onto the
    truck in the storage app (packed_orders.shipped_at).
  * PACKED → TRUCK is shipped_at − packed_at of the orders shipped in the
    period. Over 24 hours means the order most likely sat forgotten.
  * BACKLOG is the orders waiting to be PACKED (the storage app's "waiting":
    status processing, not packed, not a test order), by how many Riyadh
    days ago they were placed: today, 1–2 days, 3 days or more.
  * PER DAY counts orders packed and orders shipped on each Riyadh day.
  * DELIVERY COMPANIES and DELIVERY TIME come from OTO (oto_orders, filled
    by scripts/sync_oto_tracking.py), for EVERY OTO shipment, not only the
    storage app's (2026-09-30). An order falls in the period in which OTO
    created its first shipment. Delivery time is OTO's "delivered" time minus
    its "picked up" time. Shipping cost was removed from the tab (2026-10-01).

The "right now" figures (today, backlog) ignore the period filter; the
others follow it. Test orders (E2E) are left out everywhere, as elsewhere.
"""

from datetime import datetime, timedelta

from app.config import REPORT_TIMEZONE
from app.metrics import NOT_EXCLUDED, num
from app.storage import LOCAL_TZ, WAITING, WAITING_STATUSES, local_midnight

# An order still on the shelf this long after packing is flagged.
FORGOTTEN_HOURS = 24


def _local(dt: datetime | None) -> str | None:
    """A UTC time as Riyadh wall-clock, without a zone (like the chart buckets)."""
    return dt.astimezone(LOCAL_TZ).replace(tzinfo=None).isoformat(timespec="seconds") if dt else None


def ready_today(cur, now: datetime) -> dict:
    """Shipped today ÷ ready today, and what is still waiting."""
    today = now.astimezone(LOCAL_TZ).date()
    start = local_midnight(today)
    row = cur.execute(f"""
        SELECT count(*) FILTER (WHERE p.shipped_at >= %(start)s AND p.shipped_at <= %(now)s) AS shipped,
               count(*) FILTER (WHERE p.shipped_at IS NULL AND p.packed_at >= %(start)s)    AS waiting_today,
               count(*) FILTER (WHERE p.shipped_at IS NULL AND p.packed_at <  %(start)s)    AS waiting_older,
               count(*) FILTER (WHERE p.shipped_at IS NULL
                                  AND p.packed_at < %(now)s - make_interval(hours => %(hours)s)) AS forgotten,
               count(*) FILTER (WHERE p.shipped_at IS NULL
                                  AND EXISTS (SELECT 1 FROM box_loads l WHERE l.order_id = p.order_id)) AS partly_loaded,
               min(p.packed_at) FILTER (WHERE p.shipped_at IS NULL)                         AS oldest
        FROM packed_orders p
        JOIN orders o ON o.id = p.order_id
        WHERE (p.shipped_at IS NULL OR p.shipped_at >= %(start)s)
        {NOT_EXCLUDED}
    """, {"start": start, "now": now, "hours": FORGOTTEN_HOURS}).fetchone()
    waiting = row["waiting_today"] + row["waiting_older"]
    ready = row["shipped"] + waiting
    oldest = row["oldest"]
    return {
        "date": today.isoformat(),
        "shipped": row["shipped"],
        "ready": ready,
        "rate_pct": round(100 * row["shipped"] / ready, 1) if ready else None,
        "waiting": waiting,
        "waiting_today": row["waiting_today"],
        "waiting_older": row["waiting_older"],
        "partly_loaded": row["partly_loaded"],
        "forgotten": row["forgotten"],
        "forgotten_hours": FORGOTTEN_HOURS,
        "oldest_packed_local": _local(oldest),
        # Whole days since the oldest waiting order was packed (0 = today).
        "oldest_days": (today - oldest.astimezone(LOCAL_TZ).date()).days if oldest else None,
    }


def truck_time(cur, rng) -> dict:
    """Packed → on the truck, for orders shipped in the period."""
    row = cur.execute(f"""
        WITH t AS (
            SELECT extract(epoch FROM p.shipped_at - p.packed_at) / 3600.0 AS hours
            FROM packed_orders p
            JOIN orders o ON o.id = p.order_id
            WHERE p.shipped_at >= %(start)s AND p.shipped_at < %(end)s
            {NOT_EXCLUDED}
        )
        SELECT count(*) AS orders,
               avg(hours) AS avg_hours,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY hours) AS median_hours,
               max(hours) AS max_hours,
               count(*) FILTER (WHERE hours > %(hours)s) AS over_limit
        FROM t
    """, {"start": rng.start, "end": rng.end, "hours": FORGOTTEN_HOURS}).fetchone()
    r1 = lambda v: None if v is None else round(float(v), 1)
    return {"orders": row["orders"], "avg_hours": r1(row["avg_hours"]),
            "median_hours": r1(row["median_hours"]), "max_hours": r1(row["max_hours"]),
            "over_limit": row["over_limit"], "limit_hours": FORGOTTEN_HOURS}


def backlog(cur, now: datetime) -> dict:
    """Orders waiting to be packed, by age in Riyadh days since placed."""
    today = now.astimezone(LOCAL_TZ).date()
    row = cur.execute(f"""
        WITH w AS (
            SELECT %(today)s::date - (o.date_created_gmt AT TIME ZONE %(tz)s)::date AS days,
                   o.date_created_gmt
            {WAITING}
        )
        SELECT count(*) FILTER (WHERE days <= 0)          AS today,
               count(*) FILTER (WHERE days BETWEEN 1 AND 2) AS one_two,
               count(*) FILTER (WHERE days >= 3)          AS three_plus,
               count(*)                                   AS total,
               min(date_created_gmt)                      AS oldest
        FROM w
    """, {"waiting": list(WAITING_STATUSES), "tz": REPORT_TIMEZONE, "today": today}).fetchone()
    oldest = row["oldest"]
    return {"today": row["today"], "one_two": row["one_two"], "three_plus": row["three_plus"],
            "total": row["total"], "oldest_placed_local": _local(oldest),
            "oldest_days": (today - oldest.astimezone(LOCAL_TZ).date()).days if oldest else None}


def per_day(cur, rng) -> list[dict]:
    """Orders packed and orders shipped on each Riyadh day of the period,
    zeros included, so the chart shows quiet days."""
    rows = cur.execute(f"""
        WITH days AS (
            SELECT generate_series((%(start)s::timestamptz AT TIME ZONE %(tz)s)::date,
                                   ((%(end)s::timestamptz - interval '1 microsecond') AT TIME ZONE %(tz)s)::date,
                                   interval '1 day')::date AS day
        ),
        p AS (
            SELECT p.* FROM packed_orders p JOIN orders o ON o.id = p.order_id
            WHERE true {NOT_EXCLUDED}
        ),
        packed AS (
            SELECT (packed_at AT TIME ZONE %(tz)s)::date AS day, count(*) AS n FROM p
            WHERE packed_at >= %(start)s AND packed_at < %(end)s GROUP BY 1
        ),
        shipped AS (
            SELECT (shipped_at AT TIME ZONE %(tz)s)::date AS day, count(*) AS n FROM p
            WHERE shipped_at >= %(start)s AND shipped_at < %(end)s GROUP BY 1
        )
        SELECT d.day, COALESCE(pk.n, 0) AS packed, COALESCE(s.n, 0) AS shipped
        FROM days d
        LEFT JOIN packed pk USING (day)
        LEFT JOIN shipped s USING (day)
        ORDER BY d.day
    """, {"start": rng.start, "end": rng.end, "tz": REPORT_TIMEZONE}).fetchall()
    return [{"day": r["day"].isoformat(), "packed": r["packed"], "shipped": r["shipped"]} for r in rows]


def carriers(cur, rng) -> list[dict]:
    """OTO orders whose first shipment was created in the period, per
    delivery company, with delivery time (picked up → delivered).
    Shipping cost is not shown (2026-10-01), though the sync still keeps
    OTO's charge in oto_orders.charge."""
    rows = cur.execute(f"""
        WITH t AS (
            SELECT t.*,
                   CASE WHEN t.delivered_at > t.picked_up_at
                        THEN extract(epoch FROM t.delivered_at - t.picked_up_at) / 3600.0 END AS hours
            FROM oto_orders t
            -- only to leave out the E2E test orders, when we have the order
            LEFT JOIN orders o ON o.number = t.order_number
            WHERE t.created_at >= %(start)s AND t.created_at < %(end)s
            {NOT_EXCLUDED}
        )
        SELECT NULLIF(btrim(carrier), '')                    AS carrier,
               count(*)                                      AS orders,
               count(delivered_at)                           AS delivered,
               count(hours)                                  AS timed,
               avg(hours)                                    AS avg_delivery_hours,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY hours) AS median_delivery_hours
        FROM t
        GROUP BY 1
        ORDER BY 2 DESC, 1 NULLS LAST
    """, {"start": rng.start, "end": rng.end}).fetchall()
    r1 = lambda v: None if v is None else round(float(v), 1)
    return [{"carrier": r["carrier"], "orders": r["orders"],
             "delivered": r["delivered"], "timed": r["timed"],
             "avg_delivery_hours": r1(r["avg_delivery_hours"]),
             "median_delivery_hours": r1(r["median_delivery_hours"])} for r in rows]


def shipping_totals(rows: list[dict]) -> dict:
    """All carriers together, from carriers() rows (for the tile)."""
    timed = [r for r in rows if r["timed"]]
    n_timed = sum(r["timed"] for r in timed)
    return {
        "orders": sum(r["orders"] for r in rows),
        "delivered": sum(r["delivered"] for r in rows),
        "timed": n_timed,
        # Weighted by timed orders, so a carrier with 2 orders counts less.
        "avg_delivery_hours": round(sum(r["avg_delivery_hours"] * r["timed"] for r in timed) / n_timed, 1)
                              if n_timed else None,
    }


def oto_freshness(cur) -> str | None:
    """When OTO was last read (UTC ISO), or None if never."""
    row = cur.execute("SELECT max(synced_at) AS t FROM oto_orders").fetchone()
    return row["t"].isoformat() if row and row["t"] else None
