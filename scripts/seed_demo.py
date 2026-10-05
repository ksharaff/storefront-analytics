"""Fill the database with FAKE demo data, so the dashboard and the warehouse
app work out of the box, with no WooCommerce store and no API key.

    python -m scripts.seed_demo --reset          # orders from HISTORY_START (max 300 days)
    python -m scripts.seed_demo --reset --days 90 --per-day 40 --seed 7

Everything here is invented: products, customers (name@example.com), orders,
refunds, delivery companies, tracking numbers and packing records. Dates are
relative to "now", so there are always orders today and this month.

How it is built (and why it is a good demo of the real thing)
-------------------------------------------------------------
  * Orders and refunds are written through the SAME functions the real sync
    uses (`write_order`, `write_refunds`), fed with payloads shaped like the
    WooCommerce REST API. So the seed also exercises PII stripping, the
    upserts and the stored raw JSON exactly as a real sync would.
  * The warehouse tables (packed_orders, order_bol, box_loads, oto_orders)
    are filled directly, as if the storage app and the OTO sync had been
    running: some orders waiting to be packed, some packed and waiting for
    the truck, most already loaded and shipped.
  * One random generator with a fixed --seed, so a run is repeatable.

Safety: it only writes into an EMPTY database, or one you explicitly
--reset. --reset wipes the order, warehouse and sync tables, so never point
it at a database that holds real data.
"""

import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import HISTORY_START, POSTGRES_DB, REPORT_TIMEZONE
from app.db import connect
from scripts.backfill_orders import write_order
from scripts.backfill_refunds import write_refunds

TZ = ZoneInfo(REPORT_TIMEZONE)
VAT = 0.15                       # prices include 15% VAT, like the real store

# --- The fake catalogue ------------------------------------------------------
# (product id, name, shelf category, gross price in SAR, colours, sizes, extra categories)
COLOURS = ["Ivory", "Sage", "Navy", "Blush", "Charcoal"]
BED_SIZES = ["Single", "Queen", "King"]
CATALOG = [
    (1,  "Cotton Comforter Set",          "Comforter Sets",      389, COLOURS, BED_SIZES, ["New Arrivals"]),
    (2,  "Quilted Bed Cover Set",         "Bed Cover Sets",      329, COLOURS, ["Queen", "King"], []),
    (3,  "Hotel Duvet Insert",            "Duvet Inserts",       249, None,    ["Queen", "King"], ["Special Offers"]),
    (4,  "Compressed Duvet",              "Compressed Duvets",   179, None,    BED_SIZES, []),
    (5,  "Waterproof Mattress Pad",       "Mattress Pads",       159, None,    BED_SIZES, []),
    (6,  "Memory Foam Pillow",            "Pillows",              79, None,    None, ["Under SAR 100", "Special Offers"]),
    (7,  "Microfiber Pillow (2 pack)",    "Pillows",              59, None,    None, ["Under SAR 100"]),
    (8,  "Fleece Throw Blanket",          "Blankets & Throws",   119, COLOURS, ["Single", "Double"], ["New Arrivals"]),
    (9,  "Satin Bed Sheet Set",           "Bed Sheets",          199, COLOURS, BED_SIZES, []),
    (10, "Egyptian Cotton Bath Towel",    "Towels & Bathrobes",   69, COLOURS, ["50 x 90 cm", "70 x 140 cm"], ["Under SAR 100"]),
    (11, "Waffle Bathrobe",               "Towels & Bathrobes",  149, ["Ivory", "Navy", "Charcoal"], ["S/M", "L/XL"], []),
    (12, "Anti-slip Bath Mat",            "Bath Mats",            45, COLOURS, None, ["Under SAR 100"]),
    (13, "Ceramic Soap Dispenser",        "Bath Accessories",     39, ["Ivory", "Sage", "Charcoal"], None, ["Under SAR 100"]),
    (14, "Insulated Travel Mug",          "Insulated Mugs",       59, ["Navy", "Sage", "Blush", "Charcoal"], None, ["Under SAR 100"]),
    (15, "Porcelain Coffee Cup Set (6)",  "Coffee Cup Sets",     129, None,    None, ["Special Offers"]),
    (16, "Glass Lunch Box",               "Food Containers",      49, None,    None, ["Under SAR 100"]),
    (17, "Linen Spray - Lavender",        "Linen Sprays",         35, None,    None, ["Under SAR 100"]),
    (18, "Bathroom Diffuser - Sandalwood", "Bathroom Fragrances", 55, None,    None, ["Under SAR 100"]),
    (19, "Scented Candle - Amber",        "Home Fragrances",      65, None,    None, ["Under SAR 100", "New Arrivals"]),
    (20, "Cosy House Slippers",           "Slippers",             49, None,    ["36-38", "39-41", "42-44"], ["Under SAR 100"]),
    (21, "Cabin Travel Bag",              "Travel Bags",         229, ["Navy", "Charcoal", "Sage"], None, []),
    (22, "Plush Bear",                    "Kids Plush Toys",      55, ["Ivory", "Blush"], None, ["Pillows", "Under SAR 100"]),
    (23, "Kids Comforter Set",            "Comforter Sets",      249, ["Stars", "Clouds"], ["Single"], ["New Arrivals"]),
    (24, "Summer Cotton Blanket",         "Blankets & Throws",   139, None,    ["Single", "Double"], []),
]
CATEGORY_IDS = {}                # category name -> id, assigned as they appear

# Payment methods: (code, title shown at checkout, share of orders, failed-status slug)
PAYMENTS = [
    ("hyperpay_applepay",       "Apple Pay",         0.46, "failed"),
    ("hyperpay_mada",           "mada",              0.12, "failed"),
    ("tabby_installments",      "Tabby - Pay in 4",  0.17, "failed"),
    ("tamara-gateway-pay-in-4", "Tamara - Pay in 4", 0.13, "tamara-p-failed"),
    ("tamara-gateway-checkout", "Tamara",            0.12, "tamara-p-failed"),
]
CARRIERS = [("Aymakan", 0.40), ("SMSA", 0.25), ("Aramex", 0.20), ("Naqel", 0.15)]
PACKERS = ["Sam", "Alex", "Jordan", "Riley"]
REFUND_REASONS = ["Wrong size", "Changed my mind", "Damaged on arrival", "Not as described",
                  "Late delivery", None]

# Weekday weights (Monday=0). Evenings, Thursday to Saturday are the busy times.
WEEKDAY_W = [0.90, 0.90, 0.95, 1.15, 1.30, 1.10, 1.00]
HOUR_W = [3, 2, 1, 1, 1, 1, 2, 3, 4, 5, 6, 7, 8, 8, 7, 7, 8, 9, 10, 12, 14, 15, 12, 7]


def pick(rng, pairs):
    """Weighted choice from [(value, weight), ...]."""
    values, weights = zip(*pairs)
    return rng.choices(values, weights=weights)[0]


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def money(x):
    return f"{x:.2f}"


# --- Order status by age -----------------------------------------------------
def choose_status(rng, age_days, failed_slug, pay_code):
    """Statuses follow the order's age: fresh orders are mostly waiting to be
    packed, older ones are delivered. Weights are made up, but the mix
    (cancellations, failed payments, a few returns) exercises every metric."""
    if age_days < 1:
        w = {"processing": 58, "pending": 8, failed_slug: 4, "on-hold": 2, "cancelled": 4}
    elif age_days < 4:
        w = {"processing": 22, "on-the-way": 42, "delivered": 16, "cancelled": 8,
             "pending": 2, failed_slug: 3, "cancelrequested": 1}
    elif age_days < 14:
        w = {"on-the-way": 14, "delivered": 62, "cancelled": 9, "cancelrequested": 1,
             "refunded": 2, "returned": 2, failed_slug: 3, "pending": 1, "processing": 2}
    else:
        w = {"delivered": 72, "cancelled": 9, "returned": 2.5, "refunded": 1.5, "rtnawb": 0.3,
             "recieved": 0.3, "custom-refunded": 0.5, failed_slug: 3, "pending": 0.6,
             "on-hold": 0.3, "refund-rejected": 0.2}
    if pay_code.startswith("tamara"):
        w["tamara-p-canceled"] = 3      # abandoned Tamara payments, counted as cancelled
    return pick(rng, list(w.items()))


# --- Building one WooCommerce-shaped order -----------------------------------
def build_variation_lines(rng):
    """The variations of every product, flattened to one list of buyable items
    with a popularity weight (a few products sell much more than the rest)."""
    items = []
    for rank, (pid, name, cat, price, colours, sizes, extra) in enumerate(CATALOG):
        popularity = 1.0 / (rank + 2) ** 0.7
        combos = [(c, s) for c in (colours or [None]) for s in (sizes or [None])]
        for i, (colour, size) in enumerate(combos):
            label = name + (f" - {colour}" if colour else "")
            items.append({
                "product_id": pid, "variation_id": 0 if (colour is None and size is None) else pid * 100 + i + 1,
                "name": label, "sku": f"{pid:02d}{i + 1:03d}", "price": price,
                "colour": colour, "size": size, "weight": popularity / len(combos) ** 0.5,
            })
    return items


def meta_for(item, line_id):
    """Line meta_data the way WooCommerce sends it: attributes plus a plugin
    entry (leading underscore) that the storage app must ignore."""
    meta = []
    if item["colour"]:
        meta.append({"id": line_id * 10 + 1, "key": "pa_color", "display_key": "Colour",
                     "value": item["colour"].lower(), "display_value": item["colour"]})
    if item["size"]:
        meta.append({"id": line_id * 10 + 2, "key": "pa_size", "display_key": "Size",
                     "value": item["size"].lower(), "display_value": item["size"]})
    meta.append({"id": line_id * 10 + 3, "key": "_reduced_stock", "display_key": "_reduced_stock",
                 "value": "1", "display_value": "1"})
    return meta


def build_order(rng, order_id, created, now, items, weights, customer):
    """A WooCommerce REST-shaped order (what `write_order` expects)."""
    age_days = (now - created).total_seconds() / 86400
    code, title, _, failed_slug = pick(rng, [(p, p[2]) for p in PAYMENTS])
    status = choose_status(rng, age_days, failed_slug, code)

    n_lines = pick(rng, [(1, 55), (2, 28), (3, 12), (4, 5)])
    chosen, lines = set(), []
    gross_items = 0.0
    discounted = rng.random() < 0.12
    while len(lines) < n_lines:
        item = rng.choices(items, weights=weights)[0]
        if item["sku"] in chosen:
            continue
        chosen.add(item["sku"])
        qty = pick(rng, [(1, 70), (2, 22), (3, 8)])
        gross = item["price"] * qty
        gross_net = gross * (0.9 if discounted else 1.0)       # 10% off when discounted
        line_id = order_id * 10 + len(lines) + 1
        lines.append({
            "id": line_id, "product_id": item["product_id"], "variation_id": item["variation_id"],
            "name": item["name"], "sku": item["sku"], "quantity": qty,
            "subtotal": money(gross / (1 + VAT)), "total": money(gross_net / (1 + VAT)),
            "total_tax": money(gross_net - gross_net / (1 + VAT)),
            "meta_data": meta_for(item, line_id),
        })
        gross_items += gross_net
    shipping = 0.0 if gross_items >= 200 else 25.0
    total = round(gross_items + shipping, 2)
    tax = round(total * VAT / (1 + VAT), 2)

    paid = status not in ("pending", "failed", "tamara-p-failed", "tamara-p-canceled", "on-hold")
    via = pick(rng, [("checkout", 97), ("rest-api", 2), ("pos-rest-api", 1)])
    if via == "pos-rest-api":
        code, title = "hyperpay_mada", "mada"
    # Last change: soon after creation for orders that stall, later once moved on.
    hours = {"processing": 0.1, "pending": 0.1}.get(status, rng.uniform(4, 60))
    modified = min(now, created + timedelta(hours=hours))
    return {
        "id": order_id, "number": str(order_id), "status": status, "currency": "SAR",
        "total": money(total), "shipping_total": money(shipping), "total_tax": money(tax),
        "discount_total": money(sum(float(l["subtotal"]) - float(l["total"]) for l in lines)),
        "payment_method": code, "payment_method_title": title,
        "customer_id": customer["id"], "billing": {"email": customer["email"], "first_name": "Demo",
                                                    "last_name": "Customer"},   # stripped on write
        "created_via": via,
        "date_created_gmt": iso(created),
        "date_paid_gmt": iso(created + timedelta(minutes=rng.randint(1, 4))) if paid else None,
        "date_modified_gmt": iso(modified),
        "line_items": lines, "refunds": [],
    }


def maybe_refund(rng, order, now, refund_id):
    """A refund for orders that came back (always) or, rarely, for a delivered
    order. Amounts are NEGATIVE, like the WooCommerce API sends them."""
    status = order["status"]
    came_back = status in ("refunded", "returned", "rtnawb", "recieved", "custom-refunded")
    if not (came_back and rng.random() < 0.85) and not (status == "delivered" and rng.random() < 0.02):
        return None
    created = datetime.fromisoformat(order["date_created_gmt"]).replace(tzinfo=timezone.utc)
    when = created + timedelta(days=rng.uniform(3, 20))
    if when > now - timedelta(hours=1):
        return None
    lines = order["line_items"]
    if rng.random() < 0.15:           # amount-only refund (goodwill): no line items
        picked = []
        amount = round(rng.choice([10, 15, 25, 30]), 2)
    else:
        picked = [rng.choice(lines)] if (status == "delivered" or rng.random() < 0.5) else lines
        amount = round(sum((float(l["total"]) + float(l["total_tax"])) for l in picked), 2)
    refund = {
        "id": refund_id, "reason": rng.choice(REFUND_REASONS) or "", "amount": f"-{money(amount)}",
        "date_created_gmt": iso(when),
        "line_items": [{"id": l["id"], "product_id": l["product_id"], "variation_id": l["variation_id"],
                        "name": l["name"], "quantity": -l["quantity"],
                        "total": f"-{money(float(l['total']) + float(l['total_tax']))}"} for l in picked],
    }
    order["refunds"] = [{"id": refund_id, "reason": refund["reason"], "total": refund["amount"]}]
    return refund


# --- Warehouse records -------------------------------------------------------
def line_options(line):
    out = []
    for m in line["meta_data"]:
        if m["key"].startswith("_"):
            continue
        kind = "color" if "color" in m["key"] else "size"
        out.append({"kind": kind, "label": m["display_key"], "value": m["display_value"]})
    return out


def warehouse_rows(rng, order, now):
    """packed_orders / order_bol / box_loads / oto_orders for one order, or None
    when the order was never packed (still waiting, cancelled, failed, ...)."""
    status = order["status"]
    created = datetime.fromisoformat(order["date_created_gmt"]).replace(tzinfo=timezone.utc)
    age_days = (now - created).total_seconds() / 86400
    if status in ("delivered", "on-the-way") and age_days <= 60:
        packed_at = created + timedelta(hours=rng.uniform(1, 20))
        shipped_at = packed_at + timedelta(hours=rng.uniform(1, 20))
        if shipped_at > now:
            return None
    elif status == "processing" and 0.3 < age_days <= 3 and rng.random() < 0.40:
        packed_at, shipped_at = created + timedelta(hours=rng.uniform(1, 6)), None   # waits for the truck
        if packed_at > now:
            return None
    else:
        return None

    boxes = pick(rng, [(1, 85), (2, 13), (3, 2)])
    bol = "DM" + "".join(str(rng.randint(0, 9)) for _ in range(11))
    carrier = pick(rng, CARRIERS)
    items = [{"line_item_id": l["id"], "name": l["name"], "sku": l["sku"], "quantity": l["quantity"],
              "options": line_options(l), "manual": False, "labels": []} for l in order["line_items"]]
    packed_by = rng.sample(PACKERS, k=pick(rng, [(1, 80), (2, 20)]))
    rows = {
        "packed": (order["id"], boxes, json.dumps(items), packed_at, shipped_at, bol, packed_by),
        "bol": (order["id"], bol, "demo", boxes, carrier, f"https://example.com/track/{bol}"),
        "loads": [(order["id"], bol, shipped_at) for _ in range(boxes)] if shipped_at else [],
        "oto": None,
    }
    if shipped_at and age_days <= 90:
        picked_up = shipped_at + timedelta(hours=rng.uniform(2, 8))
        delivered = picked_up + timedelta(hours=rng.uniform(18, 96)) if status == "delivered" else None
        if delivered and delivered > now:
            delivered = None
        state = "Delivered" if delivered else ("In transit" if picked_up <= now else "Created")
        rows["oto"] = (order["number"], carrier, bol, packed_at, state,
                       round(rng.uniform(14, 28), 2), picked_up if picked_up <= now else None,
                       delivered, now, now)
    return rows


# --- Main --------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Seed the database with fake demo data.")
    ap.add_argument("--days", type=int, default=None,
                    help="days of history (default: from HISTORY_START, or 300 if that is further back)")
    ap.add_argument("--per-day", type=float, default=28, help="average orders per day (default 28)")
    ap.add_argument("--seed", type=int, default=42, help="random seed (default 42)")
    ap.add_argument("--reset", action="store_true",
                    help="wipe the order, warehouse and sync tables first (DEMO databases only)")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    now = datetime.now(timezone.utc).replace(microsecond=0)

    with connect() as conn:
        existing = conn.execute("SELECT count(*) FROM orders").fetchone()[0]
        if existing and not args.reset:
            sys.exit(f"{POSTGRES_DB!r} already holds {existing} orders. Run with --reset to wipe "
                     f"and re-seed (demo databases only), or point POSTGRES_DB at an empty one.")
        if args.reset:
            conn.execute("TRUNCATE orders, product_categories, sync_state, pick_workers, oto_orders, "
                         "unit_labels CASCADE")      # CASCADE also clears items, refunds, packing

    # Never start before HISTORY_START: periods that begin earlier are flagged
    # "incomplete" by the API. (Set HISTORY_START in .env to widen it.)
    history_start = datetime.fromisoformat(HISTORY_START).replace(tzinfo=timezone.utc)
    days = args.days or min((now - history_start).days, 300)
    start = max(now - timedelta(days=days), history_start)
    items = build_variation_lines(rng)
    weights = [i["weight"] for i in items]

    # ---- products -> categories (the real sync reads these from /products) ----
    cat_rows = []
    for pid, _name, cat, _price, _c, _s, extra in CATALOG:
        for name in [cat] + extra:
            cid = CATEGORY_IDS.setdefault(name, 100 + len(CATEGORY_IDS))
            cat_rows.append((pid, cid, name))

    # ---- customers: a growing pool, so some buy again (returning customers) ----
    customers, next_customer = [], 1
    def customer():
        nonlocal next_customer
        if customers and rng.random() < 0.30:
            return rng.choice(customers)
        c = {"id": 5000 + next_customer if rng.random() < 0.35 else 0,
             "email": f"customer{next_customer:05d}@example.com"}
        next_customer += 1
        customers.append(c)
        return c

    # ---- one pass over the days -------------------------------------------------
    order_id, refund_id, n_orders = 100000, 900000, 0
    with connect() as conn:
        cur = conn.cursor()
        day = start.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        n_days = (now.astimezone(TZ) - day).days + 1
        for d in range(n_days):
            local_day = day + timedelta(days=d)
            trend = 0.75 + 0.5 * d / max(1, n_days)               # volume grows over the period
            promo = 1.8 if d % 47 == 20 else 1.0                  # an occasional sale day
            mean = args.per_day * trend * promo * WEEKDAY_W[local_day.weekday()]
            count = max(0, round(rng.gauss(mean, mean ** 0.5)))
            for _ in range(count):
                hour = rng.choices(range(24), weights=HOUR_W)[0]
                created = (local_day + timedelta(hours=hour, minutes=rng.randint(0, 59),
                                                 seconds=rng.randint(0, 59))).astimezone(timezone.utc)
                if created > now or created < start:
                    continue
                order_id += 1
                order = build_order(rng, order_id, created, now, items, weights, customer())
                refund = maybe_refund(rng, order, now, refund_id + 1)
                write_order(cur, order)
                if refund:
                    refund_id += 1
                    write_refunds(cur, order_id, [refund])
                n_orders += 1
                w = warehouse_rows(rng, order, now)
                if w:
                    cur.execute("""INSERT INTO packed_orders (order_id, boxes, items, packed_at, shipped_at,
                                       bol_no, packed_by) VALUES (%s,%s,%s::jsonb,%s,%s,%s,%s)""",
                                (w["packed"][0], w["packed"][1], w["packed"][2], w["packed"][3],
                                 w["packed"][4], w["packed"][5], w["packed"][6]))
                    cur.execute("""INSERT INTO order_bol (order_id, bol_no, source, boxes, carrier, tracking_url)
                                   VALUES (%s,%s,%s,%s,%s,%s)""", w["bol"])
                    for row in w["loads"]:
                        cur.execute("INSERT INTO box_loads (order_id, scanned_code, loaded_at) VALUES (%s,%s,%s)", row)
                    if w["oto"]:
                        cur.execute("""INSERT INTO oto_orders (order_number, carrier, shipment_no, created_at,
                                           status, charge, picked_up_at, delivered_at, history_read_at, synced_at)
                                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", w["oto"])
        cur.executemany("""INSERT INTO product_categories (product_id, category_id, category_name)
                           VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""", cat_rows)
        # The same bookkeeping the real jobs leave behind: the dashboard's "synced N min ago".
        for key, value in (("last_reconciled_at", iso(now - timedelta(minutes=5))),
                           ("backfill_last_window", now.strftime("%Y-%m")),
                           ("refunds_last_order_id", str(order_id))):
            cur.execute("""INSERT INTO sync_state (key, value) VALUES (%s, %s)
                           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                        (key, value))
        stats = {t: cur.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                 for t in ("orders", "order_items", "refunds", "packed_orders", "oto_orders")}

    print(f"Seeded {POSTGRES_DB}: " + ", ".join(f"{n} {t}" for t, n in stats.items()))
    print(f"Orders from {start.date()} to {now.date()} (seed {args.seed}). "
          f"Start the dashboard:  uvicorn app.main:app --reload")


if __name__ == "__main__":
    main()
