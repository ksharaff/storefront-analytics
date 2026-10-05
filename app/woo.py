"""Minimal WooCommerce REST client.

Only what the sync actually needs: authenticated GETs, a retry for the
timeouts this slow store produces, and the total-count header WooCommerce
returns on list endpoints.
"""

import time

import requests

from app.config import WC_BASE_URL, WC_KEY, WC_SECRET, WC_TIMEOUT

# HTTP Basic over HTTPS is WooCommerce's documented auth for TLS sites.
AUTH = (WC_KEY, WC_SECRET)

# Requests that failed for a reason worth trying again (as opposed to a 400,
# which means we asked the wrong question and retrying will not help).
_RETRY_STATUS = {429, 500, 502, 503, 504}


def get(path: str, params: dict | None = None, retries: int = 3):
    """GET a WooCommerce endpoint and return (json_body, response_headers).

    `path` is relative to /wp-json, e.g. "wc/v3/orders".

    Headers are returned alongside the body because WooCommerce puts the
    total result count in X-WP-Total, which is how we know how many orders
    exist without downloading them all.
    """
    url = f"{WC_BASE_URL}/wp-json/{path.lstrip('/')}"

    last_error = None
    for attempt in range(retries):
        try:
            response = requests.get(
                url, auth=AUTH, params=params or {}, timeout=WC_TIMEOUT
            )
            if response.status_code in _RETRY_STATUS:
                last_error = f"HTTP {response.status_code}"
            else:
                # Any other non-2xx (notably 400) is a real error — raise now.
                response.raise_for_status()
                return response.json(), response.headers
        except requests.RequestException as exc:
            # Timeouts and connection resets are common on this store.
            last_error = str(exc)

        # Back off: 2s, 4s, 8s. Gives a struggling store room to recover.
        if attempt < retries - 1:
            time.sleep(2 ** (attempt + 1))

    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last_error}")


def total_count(path: str, params: dict | None = None) -> int:
    """How many records match, without downloading them.

    Asks for a single record and reads WooCommerce's X-WP-Total header.
    """
    params = dict(params or {})
    params["per_page"] = 1
    _, headers = get(path, params)
    return int(headers.get("X-WP-Total", 0))
