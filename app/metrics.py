"""The metrics layer: every dashboard number, as SQL over the local database.

Each function takes an open cursor and a periods.Range, and returns plain
dicts ready for JSON. No function knows about HTTP or about which period the
user picked; app/main.py composes them.

Definitions (docs/DESIGN.md, "Metric Definitions"; still pending company sign-off)
------------------------------------------------------------------------------
  * What counts as a sale, a refund, a failed payment, ... comes from the
    `order_status_groups` TABLE, never from a list of slugs in this file. It is
    always a LEFT JOIN, with a missing group treated as 'other', so a status
    the store invents later shows up as 'other' instead of silently vanishing.
  * Orders are placed in a period by `date_created_gmt`.
  * Revenue is GROSS `orders.total` (shipping and VAT included), matching the
    WooCommerce admin. Switching to merchandise-only is the one constant below.
  * Refunds are shown separately; they are never subtracted from sales.
"""

from datetime import timedelta
from decimal import Decimal

from psycopg.rows import dict_row

from app.config import REPORT_TIMEZONE

# Gross revenue, by decision of 2026-09-23 while follow-up question #5 is open.
# The alternative is "merchandise_total" (a generated column: total minus
# shipping and tax). Both columns exist, so switching is this line alone.
# Never built from user input — it is interpolated into SQL.
REVENUE_COLUMN = "total"

# Line-item value for product rankings: the line total after discounts, plus
# its VAT, so product figures are gross like the headline sales number.
# Shipping belongs to the order, not to any product, so it is not included.
ITEM_VALUE = "(i.total + i.total_tax)"

# The status-group expression used everywhere. `g` is order_status_groups.
GROUP = "COALESCE(g.status_group, 'other')"

# Orders that are not real business and never count in any metric.
# Decided on 2026-09-23: automated end-to-end tests create orders through
# the API with this payment title and no real payment. Matching the TITLE rather than the
# order ids means such test orders are also kept out if the tests ever run
# against the live store. The orders stay in the database, untouched; they are
# only left out of the numbers. Add a condition here to exclude more.
NOT_EXCLUDED = """
      AND COALESCE(o.payment_method_title, '') NOT LIKE 'E2E automated verification%%'
"""

# One customer = one billing email, compared without case or stray spaces
# (guest checkouts all have customer_id 0, so the id cannot be used).
# "A@x.com " and "a@x.com" are the same person; a blank email is nobody.
CUSTOMER = "NULLIF(lower(btrim(o.billing_email)), '')"

# The FROM clause every order query shares: orders in the range, with group,
# minus the excluded test orders.
ORDERS_IN_RANGE = """
    FROM orders o
    LEFT JOIN order_status_groups g ON g.status = o.status
    WHERE o.date_created_gmt >= %(start)s
      AND o.date_created_gmt <  %(end)s
""" + NOT_EXCLUDED


def _params(rng) -> dict:
    return {"start": rng.start, "end": rng.end, "tz": REPORT_TIMEZONE}


def num(value) -> float | int | None:
    """Decimal -> float rounded to cents, for JSON. Storage stays NUMERIC;
    this is only the last step before the numbers reach a chart."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return round(float(value), 2)
    return value


def change(current, previous, available: bool) -> dict:
    """A metric with its comparison. `change_pct` is null — never +inf% or a
    fake 0% — when there is nothing honest to compare against:
      * the previous period lies (partly) before our imported data, or
      * the previous value is zero or missing.
    The frontend shows "no comparison data" whenever it is null."""
    if not available:
        return {"value": num(current), "previous": None, "change_pct": None}
    pct = None
    if previous not in (None, 0) and current is not None:
        pct = round((float(current) - float(previous)) / float(previous) * 100, 1)
    return {"value": num(current), "previous": num(previous), "change_pct": pct}


# --- Headline numbers --------------------------------------------------------

def kpis(cur, rng) -> dict:
    """Total sales, sale orders, all orders, purchasing customers, AOV."""
    row = cur.execute(f"""
        SELECT COALESCE(sum(o.{REVENUE_COLUMN}) FILTER (WHERE {GROUP} = 'sale'), 0) AS sales,
               count(*) FILTER (WHERE {GROUP} = 'sale')                          AS orders,
               count(*)                                                           AS orders_all,
               -- Guest checkouts all have customer_id = 0, so customers are
               -- counted by email. count(DISTINCT) ignores NULL emails.
               count(DISTINCT {CUSTOMER}) FILTER (WHERE {GROUP} = 'sale')        AS customers
        {ORDERS_IN_RANGE}
    """, _params(rng)).fetchone()
    row = dict(row)
    # AOV is undefined, not zero, when nothing sold.
    row["aov"] = (row["sales"] / row["orders"]) if row["orders"] else None
    return row


# --- Charts and breakdowns ---------------------------------------------------

def sales_over_time(cur, rng) -> dict:
    """Sales and sale orders per LOCAL hour (for ranges up to 2 days) or per
    LOCAL day. Buckets with no sales are returned as zeros, so a line chart
    dips to zero instead of drawing straight across a quiet day."""
    unit = "hour" if (rng.end - rng.start).total_seconds() <= 2 * 86400 else "day"
    step = timedelta(hours=1) if unit == "hour" else timedelta(days=1)
    rows = cur.execute(f"""
        WITH buckets AS (
            -- Every bucket in the range, in local time. `end` is exclusive,
            -- hence the microsecond: a range ending at midnight has no bucket
            -- for the day that starts at that midnight.
            SELECT generate_series(
                       date_trunc(%(unit)s, %(start)s::timestamptz AT TIME ZONE %(tz)s),
                       (%(end)s::timestamptz - interval '1 microsecond') AT TIME ZONE %(tz)s,
                       %(step)s) AS bucket
        ),
        sales AS (
            SELECT date_trunc(%(unit)s, o.date_created_gmt AT TIME ZONE %(tz)s) AS bucket,
                   sum(o.{REVENUE_COLUMN}) AS sales,
                   count(*)                AS orders,
                   count(DISTINCT {CUSTOMER}) AS customers
            {ORDERS_IN_RANGE}
              AND {GROUP} = 'sale'
            GROUP BY 1
        )
        SELECT b.bucket, COALESCE(s.sales, 0) AS sales, COALESCE(s.orders, 0) AS orders,
               COALESCE(s.customers, 0) AS customers
        FROM buckets b LEFT JOIN sales s USING (bucket)
        ORDER BY b.bucket
    """, {**_params(rng), "unit": unit, "step": step}).fetchall()
    return {
        "granularity": unit,
        "timezone": REPORT_TIMEZONE,
        # `customers` is distinct buyers per bucket; they do NOT add up to the
        # period's customer count (one person can buy on several days).
        "points": [{"bucket": r["bucket"].isoformat(), "sales": num(r["sales"]),
                    "orders": r["orders"], "customers": r["customers"]} for r in rows],
    }


def orders_by_status(cur, rng) -> list[dict]:
    """Every raw status in the period, with its group. All orders, not just
    sales — this is the 'what happened to orders' view."""
    rows = cur.execute(f"""
        SELECT o.status, {GROUP} AS status_group,
               count(*) AS orders, sum(o.{REVENUE_COLUMN}) AS value
        {ORDERS_IN_RANGE}
        GROUP BY 1, 2
        ORDER BY 3 DESC, 1
    """, _params(rng)).fetchall()
    return [{**r, "value": num(r["value"])} for r in rows]


def sales_by_status(cur, rng) -> list[dict]:
    """Total sales split by the raw statuses that make up the sale group
    (Delivered, On the way, Completed, Processing on this store). The parts
    add up exactly to the "Total sales" KPI — decided 2026-09-23: one total,
    broken down, rather than separate totals."""
    rows = cur.execute(f"""
        SELECT o.status, count(*) AS orders, sum(o.{REVENUE_COLUMN}) AS sales
        {ORDERS_IN_RANGE}
          AND {GROUP} = 'sale'
        GROUP BY 1
        ORDER BY 3 DESC, 1
    """, _params(rng)).fetchall()
    return [{**r, "sales": num(r["sales"])} for r in rows]


def orders_by_group(cur, rng) -> list[dict]:
    """The same, rolled up to sale / refund / failed / cancelled / open / other.
    The compact version for the home page summary."""
    rows = cur.execute(f"""
        SELECT {GROUP} AS status_group, count(*) AS orders
        {ORDERS_IN_RANGE}
        GROUP BY 1
        ORDER BY 2 DESC, 1
    """, _params(rng)).fetchall()
    return [dict(r) for r in rows]


# --- Recent orders -------------------------------------------------------------

def recent_orders(cur, rng, limit: int = 10, offset: int = 0) -> list[dict]:
    """The newest orders placed in the period, EVERY status (decided
    2026-09-23: show what is really coming in, cancelled and failed included,
    each labelled with its status). Each order carries its product lines.

    No customer details are returned: the page shows products, not people.
    The E2E test orders stay out, like everywhere else (ORDERS_IN_RANGE).

    `offset` pages through the list (the dashboard pages through "Today").
    The id tie-break keeps pages stable when several orders share a second.

    Two small queries instead of one join: LIMIT has to count orders, not
    line items, and the item query then reads only those few orders."""
    orders = cur.execute(f"""
        SELECT o.id,
               COALESCE(o.number, o.id::text)        AS number,
               o.status,
               {GROUP}                               AS status_group,
               -- Riyadh wall-clock time, sent without a zone like the chart
               -- buckets, so the viewer's own timezone can't shift it.
               o.date_created_gmt AT TIME ZONE %(tz)s AS created_local,
               o.{REVENUE_COLUMN}                    AS total
        {ORDERS_IN_RANGE}
        ORDER BY o.date_created_gmt DESC, o.id DESC
        LIMIT %(limit)s OFFSET %(offset)s
    """, {**_params(rng), "limit": limit, "offset": offset}).fetchall()
    if not orders:
        return []

    # Product lines for just those orders, in the order the customer added
    # them. `value` is the line total after discounts plus its VAT: the same
    # gross figure the product rankings use (ITEM_VALUE).
    items = cur.execute(f"""
        SELECT i.order_id, i.product_id, i.name, i.quantity,
               {ITEM_VALUE} AS value
        FROM order_items i
        WHERE i.order_id = ANY(%(ids)s)
        ORDER BY i.order_id, i.line_item_id
    """, {"ids": [o["id"] for o in orders]}).fetchall()
    by_order: dict[int, list] = {}
    for i in items:
        by_order.setdefault(i["order_id"], []).append({
            "product_id": i["product_id"], "name": i["name"],
            "quantity": i["quantity"], "value": num(i["value"]),
        })

    return [{
        **o,
        "created_local": o["created_local"].isoformat(timespec="seconds"),
        "total": num(o["total"]),
        "items": by_order.get(o["id"], []),
    } for o in orders]


def order_count(cur, rng) -> int:
    """How many orders recent_orders() can page through: every status, test
    orders excluded. Same filter, so page counts always match the list."""
    return cur.execute(f"SELECT count(*) AS n {ORDERS_IN_RANGE}", _params(rng)).fetchone()["n"]


# --- Payment methods -----------------------------------------------------------
#
# Orders store a payment METHOD code; the store's settings page shows GATEWAYS.
# They are not the same list: Tamara alone writes five codes (pay-in-2/3/4,
# checkout, and the plain gateway), one per option the customer picked. The
# dashboard reports per gateway, with the codes kept underneath as variants.

# Payment method code -> gateway key. The frontend turns keys into labels in
# both languages. A code missing here is reported under its own name, so a new
# gateway shows up instead of disappearing.
GATEWAY_OF = {
    "hyperpay_applepay": "applepay",
    "hyperpay_mada": "mada",
    "hyperpay": "card",                      # HyperPay credit card, since removed
    "tabby_installments": "tabby",
    "tabby_pay_later": "tabby",
    "tabby_credit_card_installments": "tabby",
    "tamara-gateway": "tamara",
    "tamara-gateway-checkout": "tamara",
    "tamara-gateway-pay-in-2": "tamara",
    "tamara-gateway-pay-in-3": "tamara",
    "tamara-gateway-pay-in-4": "tamara",
    "cod": "cod",
    "bacs": "bank_transfer",
    "cheque": "cheque",
}

# The 4 gateways ENABLED in WooCommerce -> Settings -> Payments, read from
# GET /wc/v3/payment_gateways on 2026-09-23. These are always listed, even at
# zero; any other gateway appears only when orders in the period used it.
# If the store enables or removes a gateway, update this line.
ENABLED_GATEWAYS = ("applepay", "mada", "tamara", "tabby")

# Point of Sale orders are identified by how they were created, not by their
# payment code. WooCommerce's own POS records created_via = "pos-rest-api".
# They get their own line, labelled PoS, never merged into online payments.
# (None exist in 2026 so far: every order is "checkout" or "rest-api".)
POS_CREATED_VIA = ("pos-rest-api", "pos")


def payments(cur, rng) -> list[dict]:
    """Per gateway (and per channel: online or PoS): successful sales, failed
    payment attempts, sales value, success rate, and the method codes behind
    it as `variants`. Failed = the 'failed' status GROUP (failed,
    tamara-p-failed, tamara-c-failed, ...), not the 'failed' slug alone."""
    rows = cur.execute(f"""
        SELECT COALESCE(NULLIF(o.payment_method, ''), '(none)')        AS method,
               -- what the customer saw; the store's wording, any language
               max(o.payment_method_title)                             AS title,
               COALESCE(o.created_via = ANY(%(pos)s), false)           AS pos,
               count(*) FILTER (WHERE {GROUP} = 'sale')                AS successful,
               count(*) FILTER (WHERE {GROUP} = 'failed')              AS failed,
               COALESCE(sum(o.{REVENUE_COLUMN}) FILTER (WHERE {GROUP} = 'sale'), 0) AS sales
        {ORDERS_IN_RANGE}
        GROUP BY 1, 3
    """, {**_params(rng), "pos": list(POS_CREATED_VIA)}).fetchall()

    # The 4 enabled gateways first, so they are present even with no orders.
    lines = {(g, False): {"gateway": g, "pos": False, "enabled": True,
                          "successful": 0, "failed": 0, "sales": Decimal(0),
                          "variants": []} for g in ENABLED_GATEWAYS}
    for r in rows:
        if r["successful"] + r["failed"] == 0:
            continue                      # e.g. only cancelled orders: not a payment attempt
        g = GATEWAY_OF.get(r["method"], r["method"])
        line = lines.setdefault((g, r["pos"]), {
            "gateway": g, "pos": r["pos"], "enabled": g in ENABLED_GATEWAYS and not r["pos"],
            "successful": 0, "failed": 0, "sales": Decimal(0), "variants": []})
        line["successful"] += r["successful"]
        line["failed"] += r["failed"]
        line["sales"] += r["sales"]
        line["variants"].append({"method": r["method"], "title": r["title"],
                                 "successful": r["successful"], "failed": r["failed"],
                                 "sales": num(r["sales"])})

    out = []
    for line in lines.values():
        attempts = line["successful"] + line["failed"]
        line["variants"].sort(key=lambda v: (-v["sales"], v["method"]))
        out.append({**line, "sales": num(line["sales"]),
                    "success_rate_pct": round(100 * line["successful"] / attempts, 1)
                                        if attempts else None})
    out.sort(key=lambda x: (-x["sales"], x["gateway"]))
    return out


def payment_totals(cur, rng) -> dict:
    """Successful vs failed payment transactions across all methods."""
    row = cur.execute(f"""
        SELECT count(*) FILTER (WHERE {GROUP} = 'sale')   AS successful,
               count(*) FILTER (WHERE {GROUP} = 'failed') AS failed
        {ORDERS_IN_RANGE}
    """, _params(rng)).fetchone()
    return dict(row)


# --- Products ----------------------------------------------------------------

def top_products(cur, rng, by: str = "value", limit: int = 10) -> list[dict]:
    """Products from SALE orders in the period, ranked by gross value or by
    quantity. Variations roll up to their parent: WooCommerce puts the parent
    in product_id and the variation in variation_id."""
    order_by = {"value": "value DESC", "quantity": "quantity DESC"}[by]
    rows = cur.execute(f"""
        SELECT i.product_id,
               max(i.name)          AS name,   -- names can drift; any is fine
               sum(i.quantity)      AS quantity,
               sum({ITEM_VALUE})    AS value,
               count(DISTINCT o.id) AS orders
        FROM order_items i
        JOIN orders o ON o.id = i.order_id
        LEFT JOIN order_status_groups g ON g.status = o.status
        WHERE o.date_created_gmt >= %(start)s
          AND o.date_created_gmt <  %(end)s
          AND {GROUP} = 'sale'
          {NOT_EXCLUDED}
        GROUP BY i.product_id
        ORDER BY {order_by}, i.product_id
        LIMIT %(limit)s
    """, {**_params(rng), "limit": limit}).fetchall()
    return [{**r, "value": num(r["value"])} for r in rows]


def sales_by_category(cur, rng) -> list[dict]:
    """Gross product value per category, from sale orders.

    A product in several categories counts in EACH of them, so the category
    values add up to more than total sales. That is the usual convention and
    is stated in the API response. Products with no category mapping are
    grouped as 'Uncategorised' rather than dropped."""
    rows = cur.execute(f"""
        SELECT c.category_id,
               COALESCE(c.category_name, 'Uncategorised') AS category,
               sum(i.quantity)      AS quantity,
               sum({ITEM_VALUE})    AS value,
               count(DISTINCT o.id) AS orders
        FROM order_items i
        JOIN orders o ON o.id = i.order_id
        LEFT JOIN order_status_groups g ON g.status = o.status
        LEFT JOIN product_categories c ON c.product_id = i.product_id
        WHERE o.date_created_gmt >= %(start)s
          AND o.date_created_gmt <  %(end)s
          AND {GROUP} = 'sale'
          {NOT_EXCLUDED}
        GROUP BY 1, 2
        ORDER BY value DESC, 2
    """, _params(rng)).fetchall()
    return [{**r, "value": num(r["value"])} for r in rows]


# --- Returns -----------------------------------------------------------------

def return_rate(cur, rng) -> dict:
    """Orders PLACED in the period that have at least one refund, divided by
    sale orders placed in the period. Cohort-based: it answers "of what we
    sold in March, how much came back", even if the refund happened in April."""
    row = cur.execute(f"""
        SELECT count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM refunds r WHERE r.order_id = o.id)) AS returned_orders,
               count(*) FILTER (WHERE {GROUP} = 'sale')             AS sale_orders
        {ORDERS_IN_RANGE}
    """, _params(rng)).fetchone()
    row = dict(row)
    row["rate_pct"] = (round(100 * row["returned_orders"] / row["sale_orders"], 2)
                       if row["sale_orders"] else None)
    return row


def refund_totals(cur, rng) -> dict:
    """Refunds ISSUED in the period (by refund date): money that went back
    in this period, whenever the order was placed."""
    row = cur.execute("""
        SELECT count(*) AS refunds, COALESCE(sum(amount), 0) AS amount,
               count(DISTINCT order_id) AS orders
        FROM refunds
        WHERE date_created_gmt >= %(start)s AND date_created_gmt < %(end)s
    """, _params(rng)).fetchone()
    return {**row, "amount": num(row["amount"])}


def most_returned(cur, rng, limit: int = 10) -> list[dict]:
    """Products by quantity refunded in the period (by refund date).
    Amount-only refunds (no line items) cannot name a product, so they count
    in refund_totals but not here."""
    rows = cur.execute("""
        SELECT ri.product_id,
               max(ri.name)              AS name,
               sum(ri.quantity)          AS quantity,
               sum(ri.total)             AS value,
               count(DISTINCT r.order_id) AS orders
        FROM refund_items ri
        JOIN refunds r ON r.id = ri.refund_id
        WHERE r.date_created_gmt >= %(start)s AND r.date_created_gmt < %(end)s
        GROUP BY ri.product_id
        ORDER BY quantity DESC, value DESC, ri.product_id
        LIMIT %(limit)s
    """, {**_params(rng), "limit": limit}).fetchall()
    return [{**r, "value": num(r["value"])} for r in rows]


# --- Sales by hour and weekday (2026-09-30) ----------------------------------

def sales_by_hour_weekday(cur, rng) -> list[dict]:
    """Sale orders and sales per (Riyadh weekday, Riyadh hour), for the
    heat map. weekday: 0 = Sunday ... 6 = Saturday. Empty cells are left
    out; the frontend fills the 7 x 24 grid."""
    rows = cur.execute(f"""
        SELECT extract(dow  FROM o.date_created_gmt AT TIME ZONE %(tz)s)::int AS weekday,
               extract(hour FROM o.date_created_gmt AT TIME ZONE %(tz)s)::int AS hour,
               count(*) AS orders, sum(o.{REVENUE_COLUMN}) AS sales
        {ORDERS_IN_RANGE}
          AND {GROUP} = 'sale'
        GROUP BY 1, 2
        ORDER BY 1, 2
    """, _params(rng)).fetchall()
    return [{**r, "sales": num(r["sales"])} for r in rows]


# --- Cancellations (2026-09-30) -----------------------------------------------
#
# Cancellation rate = orders in the 'cancelled' status GROUP ÷ orders placed,
# where "placed" leaves out failed payments and orders still open (pending
# payment, on hold): those never became real orders, and counting them would
# make the rate depend on how many people abandon checkout.

PLACED = f"{GROUP} IN ('sale', 'refund', 'cancelled', 'other')"


def cancellations(cur, rng) -> dict:
    """Per gateway (like payments()), in total, and over time (per Riyadh
    day, or per week for periods longer than two months)."""
    rows = cur.execute(f"""
        SELECT COALESCE(NULLIF(o.payment_method, ''), '(none)')  AS method,
               COALESCE(o.created_via = ANY(%(pos)s), false)     AS pos,
               count(*) FILTER (WHERE {PLACED})                  AS orders,
               count(*) FILTER (WHERE {GROUP} = 'cancelled')     AS cancelled
        {ORDERS_IN_RANGE}
        GROUP BY 1, 2
    """, {**_params(rng), "pos": list(POS_CREATED_VIA)}).fetchall()
    lines: dict = {}
    for r in rows:
        if not r["orders"]:
            continue
        g = GATEWAY_OF.get(r["method"], r["method"])
        line = lines.setdefault((g, r["pos"]), {"gateway": g, "pos": r["pos"],
                                                "enabled": g in ENABLED_GATEWAYS and not r["pos"],
                                                "orders": 0, "cancelled": 0})
        line["orders"] += r["orders"]
        line["cancelled"] += r["cancelled"]
    rate = lambda c, n: round(100 * c / n, 1) if n else None
    by_method = sorted(({**x, "rate_pct": rate(x["cancelled"], x["orders"])} for x in lines.values()),
                       key=lambda x: (-x["orders"], x["gateway"]))
    orders = sum(x["orders"] for x in by_method)
    cancelled = sum(x["cancelled"] for x in by_method)

    unit = "week" if (rng.end - rng.start).days > 62 else "day"
    points = cur.execute(f"""
        WITH buckets AS (
            SELECT generate_series(
                       date_trunc(%(unit)s, %(start)s::timestamptz AT TIME ZONE %(tz)s),
                       (%(end)s::timestamptz - interval '1 microsecond') AT TIME ZONE %(tz)s,
                       %(step)s) AS bucket
        ),
        c AS (
            SELECT date_trunc(%(unit)s, o.date_created_gmt AT TIME ZONE %(tz)s) AS bucket,
                   count(*) FILTER (WHERE {PLACED})              AS orders,
                   count(*) FILTER (WHERE {GROUP} = 'cancelled') AS cancelled
            {ORDERS_IN_RANGE}
            GROUP BY 1
        )
        SELECT b.bucket, COALESCE(c.orders, 0) AS orders, COALESCE(c.cancelled, 0) AS cancelled
        FROM buckets b LEFT JOIN c USING (bucket)
        ORDER BY b.bucket
    """, {**_params(rng), "unit": unit,
          "step": timedelta(weeks=1) if unit == "week" else timedelta(days=1)}).fetchall()
    return {
        "orders": orders, "cancelled": cancelled, "rate_pct": rate(cancelled, orders),
        "by_method": by_method,
        "over_time": {"granularity": unit, "points": [
            {"bucket": p["bucket"].isoformat(), "orders": p["orders"], "cancelled": p["cancelled"],
             "rate_pct": rate(p["cancelled"], p["orders"])} for p in points]},
    }


# --- Freshness ----------------------------------------------------------------

def freshness(cur) -> dict:
    """How current the data is: the reconciliation watermark (UTC) and the
    newest order we hold. Lets the dashboard say "updated 3 minutes ago"."""
    row = cur.execute("""
        SELECT s.value      AS last_reconciled_at,
               s.updated_at AS last_sync,
               (SELECT max(date_created_gmt) FROM orders) AS newest_order,
               (SELECT count(*) FROM orders)              AS orders
        FROM (SELECT 1) AS one
        LEFT JOIN sync_state s ON s.key = 'last_reconciled_at'
    """).fetchone()
    iso = lambda v: v.isoformat() if v else None
    return {
        # The watermark: where the NEXT run starts. Deliberately 5 minutes
        # before the last run began (the overlap), so not a "last updated" time.
        "last_reconciled_at_utc": row["last_reconciled_at"],
        # When the last clean reconciliation run wrote that watermark. This is
        # the honest "data last synced" time the dashboard shows.
        "last_sync_utc": iso(row["last_sync"]),
        "newest_order_utc": iso(row["newest_order"]),
        "orders": row["orders"],
    }


def cursor(conn):
    """A cursor that returns rows as dicts, which every function above expects."""
    return conn.cursor(row_factory=dict_row)
