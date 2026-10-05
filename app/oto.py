"""Minimal READ-ONLY client for OTO (tryoto.com), the shipping platform
(2026-09-30). Used by the storage app's "OTO sync" button.

What we read, per website order: OTO's orderDetails, which carries
  shipmentId     the carrier's AWB number: our "BOL no." (e.g. AY00000000001)
  packageCount   the number of boxes OTO has for the shipment
  dcName         the delivery company (Aymakan, Aramex, ...)
  trackingURL    the carrier's tracking page for the shipment
Confirmed against the real account with scripts/check_oto.py on 2026-09-30:
OTO's orderId IS the website order number (10038 = #10038).

Auth: the refresh token from the OTO dashboard (OTO_REFRESH_TOKEN in .env)
is exchanged for an access token that lasts one hour; it is cached here and
renewed a few minutes before it runs out. The same refresh token kept
working across calls, so nothing needs saving back.

Only read endpoints are called. Nothing is created, changed or cancelled in
OTO. Customer details in the reply (name, phone, address) are never kept.
"""
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import requests

from app.config import OTO_REFRESH_TOKEN

API = "https://api.tryoto.com/rest/v2"
TIMEOUT = 30          # seconds per request
WORKERS = 5           # orders asked at the same time: quick, but gentle on OTO


class OtoError(Exception):
    """OTO could not be used at all (bad token, OTO down, ...)."""


_token = {"value": None, "expires": 0.0}
_lock = threading.Lock()


def access_token() -> str:
    """A valid access token, from the cache or freshly from the refresh token."""
    with _lock:
        if _token["value"] and time.time() < _token["expires"]:
            return _token["value"]
        if not OTO_REFRESH_TOKEN:
            raise OtoError("OTO_REFRESH_TOKEN is not set in .env")
        try:
            r = requests.post(f"{API}/refreshToken", json={"refresh_token": OTO_REFRESH_TOKEN},
                              timeout=TIMEOUT)
        except requests.RequestException as e:
            raise OtoError(f"cannot reach OTO ({type(e).__name__})") from e
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or not data.get("access_token"):
            raise OtoError(f"OTO refused the refresh token (HTTP {r.status_code})")
        # Renew 5 minutes early, so a token never expires mid-sync.
        lifetime = int(data.get("expires_in") or 3600)
        _token.update(value=data["access_token"], expires=time.time() + max(60, lifetime - 300))
        return _token["value"]


def parse_order(data) -> dict | None:
    """The fields we keep from an orderDetails reply, or None when OTO has no
    such order. A shipment may not exist yet (bol_no None): the box count and
    carrier may still be known. Anything else in the reply is dropped."""
    if not isinstance(data, dict) or data.get("success") is False or not data.get("orderId"):
        return None
    bol = str(data.get("shipmentId") or "").strip() or None
    boxes = data.get("packageCount")
    boxes = boxes if isinstance(boxes, int) and 0 < boxes < 100 else None
    carrier = str(data.get("dcName") or "").strip() or None
    url = str(data.get("trackingURL") or "").strip()
    # Only a real web address may become a link on the page.
    url = url if url.startswith(("https://", "http://")) and len(url) < 500 else None
    if not (bol or boxes):
        return None
    return {"bol_no": bol, "boxes": boxes, "carrier": carrier, "tracking_url": url}


def order_details(number: str) -> dict | None:
    """One order's shipment facts (parse_order), or None if OTO has none.
    Raises requests errors / OtoError on failure, so the caller can count it."""
    r = requests.get(f"{API}/orderDetails", params={"orderId": number}, timeout=TIMEOUT,
                     headers={"Authorization": f"Bearer {access_token()}"})
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise OtoError(f"HTTP {r.status_code} for order {number}")
    return parse_order(r.json())


def fetch_orders(numbers: list[str]) -> tuple[dict[str, dict], int]:
    """({order number: facts}, how many could not be read). Orders OTO does
    not know, or has nothing for yet, are simply absent from the result."""
    access_token()                     # fail fast, once, if the token is wrong
    found, failed = {}, 0

    def one(n):
        try:
            return n, order_details(n), False
        except (requests.RequestException, OtoError, ValueError):
            return n, None, True

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for n, facts, bad in pool.map(one, numbers):
            failed += bad
            if facts:
                found[n] = facts
    return found, failed


# --- Delivery time and shipping cost (dashboard Warehouse tab) --------------
# 2026-09-30: every OTO shipment counts, not only the orders scanned onto
# the truck in the storage app. scripts/sync_oto_tracking.py reads
#   shipmentTransactions  OTO's list of shipments for a date range (OTO keeps
#                         about 90 days): order number, carrier, created,
#                         dcCharge (what the delivery company charged)
#   orderDetails          per order, its status history: when the carrier
#                         picked it up and when it was delivered
# Delivery time = delivered − picked up. Both replies are read defensively:
# the field names of a history entry are looked up by pattern (a status
# field and a date/time field), because a missing or renamed field must mean
# "not known yet", never a crash or a wrong date. No customer details are kept.

# OTO is a Saudi platform: a time written without a zone is Riyadh time.
OTO_TZ = ZoneInfo("Asia/Riyadh")
_NOT_DELIVERED = re.compile(r"(un|not|fail|return|attempt|out ?for|pending)", re.I)
_PICKED_UP = re.compile(r"picked[ _-]?up", re.I)
_IN_TRANSIT = re.compile(r"(in[ _-]?transit|shipped)", re.I)
_NOT_PICKED = re.compile(r"(not|fail|cancel|ready|request|schedul|pending)", re.I)
# A shipment that is cancelled, or a return coming back to us.
_CANCELLED = re.compile(r"cancel", re.I)
_RETURN = re.compile(r"(return|reverse)", re.I)
PER_PAGE = 100        # OTO's maximum for shipmentTransactions
MAX_PAGES = 1000      # a safety stop: 100,000 shipments


def _parse_time(v) -> datetime | None:
    """A time in any of the usual shapes -> aware datetime, or None."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        secs = v / 1000 if v > 1e11 else v          # epoch milliseconds or seconds
        try:
            return datetime.fromtimestamp(secs, tz=timezone.utc) if secs > 1e9 else None
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(v, str) or not v.strip():
        return None
    s = v.strip().replace("Z", "+00:00")
    dt = None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
                    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                pass
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=OTO_TZ)


def _entry(item: dict) -> tuple[str | None, datetime | None]:
    """(status text, time) of one status-history entry. OTO's own status
    ("status", "otoStatus") is preferred over the carrier's ("dcStatus")."""
    statuses, when = [], None
    for k, v in item.items():
        kl = k.lower()
        if "status" in kl and isinstance(v, str) and v.strip():
            rank = 0 if kl in ("status", "otostatus", "orderstatus") else 1
            statuses.append((rank, v.strip()))
        if when is None and ("date" in kl or "time" in kl or kl.endswith("at")):
            when = _parse_time(v)
    return (min(statuses)[1] if statuses else None), when


def is_delivered(status: str | None) -> bool:
    """"Delivered", "DELIVERED", "delivered_to_customer": yes.
    "Undelivered", "Failed delivery", "Out for delivery", "Return delivered": no."""
    if not status:
        return False
    s = status.lower()
    return "deliver" in s and not _NOT_DELIVERED.search(s)


def is_picked_up(status: str | None) -> bool:
    """"pickedUp", "Picked up": yes. "readyForPickup", "pickupRequested",
    "Not picked up", "pickup failed": no."""
    return bool(status) and bool(_PICKED_UP.search(status)) and not _NOT_PICKED.search(status)


def is_finished_undelivered(status: str | None) -> bool:
    """Cancelled or returned: it will never be delivered, stop asking."""
    return bool(status) and bool(_CANCELLED.search(status) or _RETURN.search(status))


def _find_lists(data, key_pred):
    """Every list anywhere in a JSON reply whose key matches."""
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list) and key_pred(k):
                yield v
            yield from _find_lists(v, key_pred)
    elif isinstance(data, list):
        for v in data:
            yield from _find_lists(v, key_pred)


def _text(v, limit=100) -> str | None:
    s = str(v).strip() if v is not None else ""
    return s[:limit] or None


# ---- The shipment list -------------------------------------------------------

def parse_shipment(item) -> dict | None:
    """One entry of shipmentTransactions -> the fields we keep, or None if it
    has no order number or no creation date (it could not be put in a period)."""
    if not isinstance(item, dict):
        return None
    number = _text(item.get("orderId"))
    created = _parse_time(item.get("shipmentCreationDate") or item.get("createdDate")
                          or item.get("creationDate"))
    if not number or not created:
        return None
    charge = None
    try:
        c = Decimal(str(item.get("dcCharge")))
        charge = c if c.is_finite() and c >= 0 else None
    except (InvalidOperation, ValueError):
        pass
    kind = _text(item.get("shipmentType")) or ""
    status = _text(item.get("status"))
    return {"order_number": number,
            "shipment_no": _text(item.get("shipmentNumber")),
            "created_at": created,
            "carrier": _text(item.get("deliveryCompanyName") or item.get("dcName")),
            "charge": charge,
            "status": status,
            "is_return": bool(_RETURN.search(kind)),
            "is_cancelled": bool(status and _CANCELLED.search(status))}


def shipments_page(data) -> tuple[list[dict], int]:
    """(parsed shipments, number of raw entries) of one reply page. The raw
    count decides whether another page follows."""
    if not isinstance(data, dict) or data.get("success") is False:
        raise OtoError("OTO did not return a shipment list")
    raw = []
    for lst in _find_lists(data, lambda k: k.lower() in ("shipments", "data", "items", "transactions")):
        raw += [i for i in lst if isinstance(i, dict)]
    return [s for s in map(parse_shipment, raw) if s], len(raw)


def list_shipments(min_date, max_date) -> list[dict]:
    """Every shipment OTO created between two dates (both included, as
    yyyy-mm-dd), page by page. Raises OtoError / requests errors on failure:
    a half-read list must not look like a quiet week."""
    auth = {"Authorization": f"Bearer {access_token()}"}
    out = []
    for page in range(1, MAX_PAGES + 1):
        r = requests.get(f"{API}/shipmentTransactions", timeout=TIMEOUT, headers=auth,
                         params={"minDate": str(min_date), "maxDate": str(max_date),
                                 "perPage": PER_PAGE, "page": page})
        if r.status_code != 200:
            raise OtoError(f"HTTP {r.status_code} reading shipments, page {page}")
        found, n_raw = shipments_page(r.json())
        out += found
        if n_raw < PER_PAGE:
            return out
    raise OtoError(f"more than {MAX_PAGES} pages of shipments; stopped")


def per_order(shipments: list[dict]) -> dict[str, dict]:
    """Shipments -> one entry per order number. Cancelled shipments and
    returns are left out. Charges of an order's shipments are added up; the
    carrier, number and status are the latest shipment's; the period is
    decided by the first shipment's creation date."""
    orders = {}
    for s in sorted(shipments, key=lambda s: s["created_at"]):
        if s["is_return"] or s["is_cancelled"]:
            continue
        o = orders.setdefault(s["order_number"], {"order_number": s["order_number"],
                                                  "created_at": s["created_at"], "charge": None})
        for k in ("carrier", "shipment_no", "status"):
            o[k] = s[k] or o.get(k)
        if s["charge"] is not None:
            o["charge"] = (o["charge"] or Decimal(0)) + s["charge"]
    return orders


# ---- One order's status history ----------------------------------------------

def parse_history(details) -> dict | None:
    """{status, picked_up_at, delivered_at} from an orderDetails reply, or
    None when OTO has no such order. Picked up = the first "picked up" entry
    (or, when there is none, the first "in transit"); delivered = the first
    "delivered" entry."""
    if not isinstance(details, dict) or details.get("success") is False:
        return None
    entries = []
    for lst in _find_lists(details, lambda k: "history" in k.lower()):
        entries += [_entry(i) for i in lst if isinstance(i, dict)]
    first = lambda test: min((w for s, w in entries if w and test(s)), default=None)
    delivered = first(is_delivered)
    picked = first(is_picked_up) or first(lambda s: bool(s and _IN_TRANSIT.search(s)))
    latest = max((e for e in entries if e[1]), key=lambda e: e[1], default=(None, None))[0]
    top = details.get("status") if isinstance(details.get("status"), str) else None
    status = _text(top or latest)
    if not (status or picked or delivered):
        return None
    return {"status": status, "picked_up_at": picked, "delivered_at": delivered}


def history(number: str) -> dict | None:
    """One order's pickup and delivery times (parse_history). Raises on failure."""
    r = requests.get(f"{API}/orderDetails", params={"orderId": number}, timeout=TIMEOUT,
                     headers={"Authorization": f"Bearer {access_token()}"})
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise OtoError(f"HTTP {r.status_code} for order {number}")
    return parse_history(r.json())


def fetch_history(numbers: list[str]) -> tuple[dict[str, dict], int]:
    """({order number: history facts}, failures), like fetch_orders."""
    access_token()
    found, failed = {}, 0

    def one(n):
        try:
            return n, history(n), False
        except (requests.RequestException, OtoError, ValueError):
            return n, None, True

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for n, facts, bad in pool.map(one, numbers):
            failed += bad
            if facts:
                found[n] = facts
    return found, failed
