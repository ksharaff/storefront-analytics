"""The metrics API: one endpoint per dashboard section.

    uvicorn app.main:app --reload
    http://127.0.0.1:8000/        the dashboard (web/)
    http://127.0.0.1:8000/docs    interactive API docs
    http://127.0.0.1:8000/storage the storage room's pick & pack app (app/storage.py)

Sections: overview, sales, products, payments, and warehouse
(app/warehouse.py: packing from the storage app, delivery and cost from OTO).

Every section endpoint takes the same period parameters:

    ?period=today | yesterday | last_7_days | current_month
           | previous_month | this_year | custom
    &start=YYYY-MM-DD&end=YYYY-MM-DD         # custom only; both inclusive, local dates

and returns a `period` block (what was queried, in UTC and Asia/Riyadh, plus
`complete` and `comparison_available`) next to the numbers. Metrics that are
compared come back as {value, previous, change_pct}; change_pct is null when
there is nothing honest to compare against, and the frontend then shows
"no comparison data".

Reads only the local database. Never calls WooCommerce.

Access control is NOT here yet: uvicorn binds to 127.0.0.1 by default, so
this is only reachable from this machine. Restricting it to authorised people
is part of the deployment step.
"""

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import metrics as m
from app import periods
from app import storage
from app import warehouse as wh
from app.db import connect
from psycopg import errors as pg_errors

app = FastAPI(
    title="Storefront Analytics — metrics API",
    description="Dashboard metrics for the Acme Home Store WooCommerce store, "
                "served from the local PostgreSQL copy.",
)

PeriodName = Literal["today", "yesterday", "last_7_days", "current_month",
                     "previous_month", "this_year", "custom"]


def clock() -> datetime:
    """The current time. A function so the tests can freeze it."""
    return datetime.now(timezone.utc)


def resolve_period(period: str, start: date | None, end: date | None) -> periods.Period:
    """Turn the query parameters into a Period, or a clean HTTP 400."""
    try:
        return periods.resolve(period, now=clock(), start=start, end=end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def compared_kpis(cur, p: periods.Period) -> dict:
    """The four headline numbers, each with its previous-period comparison.
    The previous period is only queried when it can actually be compared."""
    now = m.kpis(cur, p.current)
    ok = p.comparison_available
    prev = m.kpis(cur, p.previous) if ok else {}
    return {
        "sales":     m.change(now["sales"],     prev.get("sales"),     ok),
        "orders":    m.change(now["orders"],    prev.get("orders"),    ok),
        "aov":       m.change(now["aov"],       prev.get("aov"),       ok),
        "customers": m.change(now["customers"], prev.get("customers"), ok),
        # Every order placed, whatever happened to it. Context, not a KPI.
        "orders_all": now["orders_all"],
    }


def compared_payments(cur, p: periods.Period) -> dict:
    """Successful and failed payment attempts, each with its comparison."""
    ok = p.comparison_available
    now = m.payment_totals(cur, p.current)
    prev = m.payment_totals(cur, p.previous) if ok else {}
    return {"successful": m.change(now["successful"], prev.get("successful"), ok),
            "failed": m.change(now["failed"], prev.get("failed"), ok)}


# Recent orders are listed 10 at a time. The dashboard shows page buttons
# only for "Today" (2026-09-23); other periods show the first page.
RECENT_PER_PAGE = 10


def recent_orders_page(cur, p: periods.Period, page: int) -> dict:
    """One page of the newest orders, plus what the page buttons need. A page
    past the end (the list shrank, or a stale link) shows the last page
    instead of an empty card."""
    total = m.order_count(cur, p.current)
    pages = max(1, -(-total // RECENT_PER_PAGE))        # ceiling division
    page = min(page, pages)
    return {
        "recent_orders": m.recent_orders(cur, p.current, limit=RECENT_PER_PAGE,
                                         offset=(page - 1) * RECENT_PER_PAGE),
        "recent_orders_page": {"page": page, "pages": pages,
                               "per_page": RECENT_PER_PAGE, "total": total},
    }


# Shared query parameters, declared once. FastAPI validates `period` against
# the Literal above and returns 422 for anything else.
PERIOD_Q = Query("current_month", description="Which period to report on.")
START_Q = Query(None, description="Custom range start (local date, inclusive).")
END_Q = Query(None, description="Custom range end (local date, inclusive).")


# --- Endpoints -----------------------------------------------------------------

# The dashboard is plain HTML/CSS/JS in web/ — no build step. Serving it
# from this same app means one server, one address, and no CORS setup.
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.middleware("http")
async def revalidate_page_files(request: Request, call_next):
    """Make browsers re-check the page and its scripts on every load, so an
    edited app.js is picked up on refresh instead of a stale cached copy.
    Cheap: an unchanged file comes back as a tiny 304."""
    response = await call_next(request)
    if request.url.path in ("/", "/storage") or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Browsers request /favicon.ico by themselves; serve the logo."""
    return FileResponse(WEB_DIR / "img" / "favicon-32.png", media_type="image/png")


@app.get("/", include_in_schema=False)
def dashboard():
    """The dashboard page. Everything else it needs comes from /static/ and /api/."""
    return FileResponse(WEB_DIR / "index.html")


# The storage room's pick & pack app: a separate page for the storage workers,
# served by the same app and database (one server, one login). Its API lives
# in app/storage.py.
app.include_router(storage.router)


@app.get("/storage", include_in_schema=False)
def storage_app():
    """The storage app's page. Its script and styles come from /static/."""
    return FileResponse(WEB_DIR / "storage.html")


@app.get("/api/health")
def health():
    """Is the database reachable, and how fresh is the data?"""
    with connect() as conn:
        return {"status": "ok", "data_start_utc": periods.DATA_START.isoformat(),
                **m.freshness(m.cursor(conn))}


@app.get("/api/overview")
def overview(period: PeriodName = PERIOD_Q, start: date | None = START_Q,
             end: date | None = END_Q,
             orders_page: int = Query(1, ge=1, le=100_000,
                                      description="Page of the recent-orders list (10 per page).")):
    """Home page: headline KPIs with % change, the sales chart, short
    summaries of statuses, payment methods and top products, and the 10
    newest orders in the period."""
    p = resolve_period(period, start, end)
    with connect() as conn:
        cur = m.cursor(conn)
        return {
            "period": p.as_dict(),
            "kpis": compared_kpis(cur, p),
            "sales_by_status": m.sales_by_status(cur, p.current),
            "sales_over_time": m.sales_over_time(cur, p.current),
            "status_groups": m.orders_by_group(cur, p.current),
            "payment_methods": m.payments(cur, p.current),
            "payment_summary": compared_payments(cur, p),
            "top_products": m.top_products(cur, p.current, by="value", limit=5),
            **recent_orders_page(cur, p, orders_page),
            "freshness": m.freshness(cur),
        }


@app.get("/api/sales")
def sales(period: PeriodName = PERIOD_Q, start: date | None = START_Q,
          end: date | None = END_Q,
          limit: int = Query(10, ge=1, le=100, description="Most-returned list size.")):
    """Sales, orders and returns section."""
    p = resolve_period(period, start, end)
    with connect() as conn:
        cur = m.cursor(conn)
        ok = p.comparison_available
        rate_now = m.return_rate(cur, p.current)
        rate_prev = m.return_rate(cur, p.previous) if ok else {}
        return {
            "period": p.as_dict(),
            "kpis": compared_kpis(cur, p),
            "sales_by_status": m.sales_by_status(cur, p.current),
            "sales_over_time": m.sales_over_time(cur, p.current),
            "orders_by_status": m.orders_by_status(cur, p.current),
            "return_rate": {
                **m.change(rate_now["rate_pct"], rate_prev.get("rate_pct"), ok),
                "returned_orders": rate_now["returned_orders"],
                "sale_orders": rate_now["sale_orders"],
                "basis": "orders placed in the period that have at least one refund, "
                         "divided by sale orders placed in the period",
            },
            "refunds": {**m.refund_totals(cur, p.current),
                        "basis": "refunds issued in the period, by refund date"},
            "most_returned": m.most_returned(cur, p.current, limit),
            "by_hour_weekday": m.sales_by_hour_weekday(cur, p.current),
        }


@app.get("/api/products")
def products(period: PeriodName = PERIOD_Q, start: date | None = START_Q,
             end: date | None = END_Q,
             limit: int = Query(10, ge=1, le=500,
                                description="Rows per ranking. Raise it for the full "
                                            "quantity-per-product list.")):
    """Products section: best sellers by value and by quantity, and sales by
    category. All from sale orders only."""
    p = resolve_period(period, start, end)
    with connect() as conn:
        cur = m.cursor(conn)
        return {
            "period": p.as_dict(),
            "by_value": m.top_products(cur, p.current, by="value", limit=limit),
            "by_quantity": m.top_products(cur, p.current, by="quantity", limit=limit),
            "by_category": m.sales_by_category(cur, p.current),
            "notes": {
                "value": "gross line value: line total after discounts plus VAT; "
                         "shipping is not attributed to products",
                "categories": "a product in several categories counts in each, so "
                              "category values add up to more than total sales",
            },
        }


@app.get("/api/payments")
def payment_methods(period: PeriodName = PERIOD_Q, start: date | None = START_Q,
                    end: date | None = END_Q):
    """Payment methods section: orders and sales per method, and successful
    vs failed payment transactions."""
    p = resolve_period(period, start, end)
    with connect() as conn:
        cur = m.cursor(conn)
        summary = compared_payments(cur, p)
        return {
            "period": p.as_dict(),
            "by_method": m.payments(cur, p.current),
            "successful": summary["successful"],
            "failed": summary["failed"],
            "cancellations": m.cancellations(cur, p.current),
            "basis": "successful = sale status group; failed = failed-payment "
                     "status group (failed, tamara-p-failed, tamara-c-failed, ...)",
            "cancellation_basis": "cancelled status group ÷ orders placed, not counting "
                                  "failed payments and open (unpaid, on hold) orders",
        }


@app.get("/api/warehouse")
def warehouse(period: PeriodName = PERIOD_Q, start: date | None = START_Q,
              end: date | None = END_Q):
    """Warehouse section (app/warehouse.py). From the storage app's records:
    shipped today ÷ ready today and the packing backlog (right now, whatever
    the period), and for the period packed → truck time and packed and
    shipped per day. From OTO (every OTO shipment created in the period):
    orders and delivery time per delivery company."""
    p = resolve_period(period, start, end)
    now = clock()
    try:
        with connect() as conn:
            cur = m.cursor(conn)
            carriers = wh.carriers(cur, p.current)
            return {
                "period": p.as_dict(),
                "now_utc": now.isoformat(),
                "today": wh.ready_today(cur, now),
                "backlog": wh.backlog(cur, now),
                "truck_time": wh.truck_time(cur, p.current),
                "per_day": wh.per_day(cur, p.current),
                "carriers": carriers,
                "shipping": wh.shipping_totals(carriers),
                "oto_synced_utc": wh.oto_freshness(cur),
            }
    except pg_errors.UndefinedTable as e:
        # A db/ setup file has not been run on this database yet.
        raise HTTPException(503, f"A storage table is missing ({e.diag.message_primary}). "
                                 "Run the db/ setup files 03 to 09, see README.md.") from e
