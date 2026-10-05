"""Step 1 of connecting OTO (tryoto.com): check that we can READ the BOL
numbers, and see what OTO's replies look like (2026-09-30).

    python -m scripts.check_oto                   # shipments of the last 7 days
    python -m scripts.check_oto --days 30
    python -m scripts.check_oto --order 10037    # one order: its details + shipments

Needs OTO_REFRESH_TOKEN in .env (OTO dashboard → Settings → Developers →
API Integrations → Connect; see README "Connecting OTO").

READ-ONLY. It only calls OTO endpoints that read: refreshToken (turns the
refresh token into a 1-hour access token), shipmentTransactions and
orderDetails. It creates, changes and cancels nothing.

PRIVACY. OTO's replies contain customer names, phones and addresses. This
script prints VALUES only for fields that identify an order or a shipment
(order id, shipment / tracking / AWB number, carrier, status, dates). For
every other field it prints just the field's NAME, so the structure can be
seen without printing anyone's details. Safe to paste the output back.
"""
import argparse
import os
import re
import sys
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

API = "https://api.tryoto.com/rest/v2"     # OTO API v2 (apis.tryoto.com)
TIMEOUT = 60

# Field names whose VALUES are safe and useful to print: order and shipment
# identifiers, carrier, status, dates, counts. Matched case-insensitively
# against the field name, anywhere in the reply.
SAFE = re.compile(r"(orderid|ordernumber|reference|shipment|tracking|awb|waybill|bol|"
                  r"deliverycompany|dcname|carrier|status|date|type|box|package|count|"
                  r"success|message|error|page|total)", re.I)
# Never print these values even if a name above matches (e.g. "addressType").
NEVER = re.compile(r"(name$|phone|mobile|email|address|city|district|street|"
                   r"customer|receiver|sender|lat|lon|token|secret|key)", re.I)
# ...but these carrier-name fields are fine.
CARRIER_OK = re.compile(r"^(deliveryCompanyName|dcConnectionName|carrierName)$", re.I)


def show(value, path="", depth=0, max_items=3):
    """Print a JSON reply: values for safe identifier fields, names only for
    everything else. Lists show their first few entries."""
    pad = "  " * depth
    if isinstance(value, dict):
        for k, v in value.items():
            p = f"{path}.{k}" if path else k
            if isinstance(v, (dict, list)):
                print(f"{pad}{k}:")
                # A status history is shown in full: the "delivered" entry
                # (needed by sync_oto_tracking) is usually the last one.
                show(v, p, depth + 1, 100 if "history" in k.lower() else max_items)
            elif CARRIER_OK.match(k) or (SAFE.search(k) and not NEVER.search(k)):
                print(f"{pad}{k} = {v!r}")
            else:
                print(f"{pad}{k}  (value hidden)")
    elif isinstance(value, list):
        print(f"{pad}[{len(value)} item(s)]")
        for i, item in enumerate(value[:max_items]):
            print(f"{pad}- #{i + 1}")
            show(item, path, depth + 1, max_items)
    else:
        print(f"{pad}(value hidden)")


def access_token(refresh: str) -> str:
    """POST /refreshToken {"refresh_token": ...} → a 1-hour access token."""
    r = requests.post(f"{API}/refreshToken", json={"refresh_token": refresh}, timeout=TIMEOUT)
    if r.status_code != 200:
        sys.exit(f"refreshToken failed: HTTP {r.status_code}. Is OTO_REFRESH_TOKEN right "
                 f"(copied whole, no quotes or spaces)?")
    data = r.json()
    # The docs don't name the field; accept the usual spellings.
    token = data.get("access_token") or data.get("accessToken") or data.get("token")
    if not token:
        sys.exit(f"No access token in OTO's reply. Its field names: {sorted(data)}")
    print(f"✓ access token received (reply fields: {sorted(data)})")
    return token


def get(token: str, path: str, **params):
    r = requests.get(f"{API}/{path}", params=params, timeout=TIMEOUT,
                     headers={"Authorization": f"Bearer {token}"})
    print(f"\nGET /{path} {params} → HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        print("  (not JSON)")
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=int, default=7, help="shipments of the last N days (default 7)")
    ap.add_argument("--order", help="one website order number, e.g. 10037")
    args = ap.parse_args()

    load_dotenv()
    refresh = os.environ.get("OTO_REFRESH_TOKEN", "").strip()
    if not refresh:
        sys.exit("Add OTO_REFRESH_TOKEN=... to .env first (see README → Connecting OTO).")
    token = access_token(refresh)

    if args.order:
        # One order: what OTO holds for it, and its shipment(s).
        show(get(token, "orderDetails", orderId=args.order) or {})
        show(get(token, "shipmentTransactions", orderId=args.order, perPage=10, page=1) or {})
    else:
        # Recent shipments: do their orderId values look like our order
        # numbers (e.g. 10037), and which field holds the BOL?
        today = date.today()
        show(get(token, "shipmentTransactions", perPage=5, page=1,
                 minDate=(today - timedelta(days=args.days)).isoformat(),
                 maxDate=today.isoformat()) or {})

    print("\nDone. Nothing was changed in OTO. Paste this output to Claude (no customer details are in it).")


if __name__ == "__main__":
    main()
