"""Configuration, loaded once from .env.

Everything secret lives in .env (gitignored). Nothing in this file, or any
other file in the repo, holds a real key.
"""

import os
from dotenv import load_dotenv

# Reads .env from the project root into the process environment.
# Real environment variables win over .env, which is what we want in
# production later on.
load_dotenv()


def _required(name: str) -> str:
    """Fail loudly at import time instead of mysteriously at request time."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name!r}. "
            "Copy .env.example to .env and fill it in."
        )
    return value


# --- WooCommerce ------------------------------------------------------------
# Trailing slash stripped so we can always build URLs as f"{WC_BASE_URL}/wp-json/...".
WC_BASE_URL = _required("WC_BASE_URL").rstrip("/")
WC_KEY      = _required("WC_KEY")
WC_SECRET   = _required("WC_SECRET")

# A slow store can take several seconds to answer a single-order request, so the
# default requests timeout (none at all) or a short one would both be wrong.
WC_TIMEOUT  = int(os.environ.get("WC_TIMEOUT", "60"))

# --- PostgreSQL -------------------------------------------------------------
POSTGRES_USER     = os.environ.get("POSTGRES_USER", "storefront")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")
POSTGRES_DB       = os.environ.get("POSTGRES_DB", "storefront_analytics")
POSTGRES_HOST     = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT     = int(os.environ.get("POSTGRES_PORT", "5434"))

# psycopg accepts a libpq connection string; this is the simplest form.
DATABASE_URL = (
    f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
    f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
)

# --- Reporting --------------------------------------------------------------
# All timestamps are stored in UTC. This is the zone we convert to when
# grouping by day, month, "today", "yesterday", etc.
REPORT_TIMEZONE = os.environ.get("REPORT_TIMEZONE", "Asia/Riyadh")

# --- Two different things. Do not conflate them. ---------------------------

# FACT about the store: its oldest order is 2020-12-31T21:04:38 GMT (order id
# 203887), verified by scripts/check_history_start.py. That GMT timestamp is
# 2021-01-01 00:04:38 in Asia/Riyadh, so an earlier note recording the store as
# starting "2021-01-01" was right in local time and wrong in UTC. Backfill
# windows compare against the *_gmt columns, which is why the UTC form matters.
STORE_OLDEST_ORDER = "2020-12-01"

# POLICY choice: how far back we actually import. Deliberately set to 2026 —
# the dashboard covers the current year only, not the store's full history.
# This is NOT a bug and should not be "corrected" back to STORE_OLDEST_ORDER.
#
# Consequence to be aware of: any previous-period comparison that reaches
# before this date has nothing to compare against. See docs/DESIGN.md.
#
# Widening it later costs nothing but time — every write is an upsert, so
# re-running with an earlier --start fills in the gap without touching what is
# already stored.
HISTORY_START = os.environ.get("HISTORY_START", "2026-01-01")

# OTO (tryoto.com), the shipping platform: the storage app's "OTO sync" reads
# each order's BOL (AWB) number and box count from it. Optional: without it
# the sync runs in TEST mode with made-up numbers. Secret; .env only.
OTO_REFRESH_TOKEN = os.environ.get("OTO_REFRESH_TOKEN", "").strip()
