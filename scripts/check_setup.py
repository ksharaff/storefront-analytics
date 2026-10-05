"""Smoke test for the local environment.

Run from the project root, with the venv active:

    python -m scripts.check_setup

Checks, in order:
  1. .env loads and every required variable is present
  2. PostgreSQL is reachable and the schema is loaded
  3. The WooCommerce API key still works

Each check prints PASS or FAIL and the script exits non-zero if anything
failed, so it is also usable as a pre-flight step before the backfill.
"""

import sys

EXPECTED_TABLES = {
    "orders",
    "order_items",
    "product_categories",
    "refunds",
    "refund_items",
    "order_status_groups",
    "sync_state",
}

results = []


def report(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))


# --- 1. Configuration -------------------------------------------------------
try:
    from app import config

    report(
        "config loaded",
        True,
        f"store={config.WC_BASE_URL}  db={config.POSTGRES_HOST}:{config.POSTGRES_PORT}",
    )
except Exception as exc:
    report("config loaded", False, str(exc))
    # Nothing else can run without config.
    sys.exit(1)


# --- 2. Database ------------------------------------------------------------
try:
    from app.db import connect

    with connect() as conn:
        version = conn.execute("SELECT version()").fetchone()[0]
        found = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
        }
        statuses = conn.execute(
            "SELECT count(*) FROM order_status_groups"
        ).fetchone()[0] if "order_status_groups" in found else 0

    report("postgres reachable", True, version.split(",")[0])

    missing = EXPECTED_TABLES - found
    report(
        "schema loaded",
        not missing,
        "all tables present" if not missing else f"missing: {sorted(missing)}",
    )
    report(
        "status groups seeded",
        statuses == 37,
        f"{statuses} rows (expected 37)",
    )
except Exception as exc:
    report("postgres reachable", False, str(exc))


# --- 3. WooCommerce API -----------------------------------------------------
try:
    from app.woo import total_count

    # status=any is the only filter proven to return every order on this
    # store — an unregistered status like custom-refunded returns HTTP 400.
    count = total_count("wc/v3/orders", {"status": "any"})
    report(
        "woocommerce api",
        count > 0,
        f"{count:,} orders visible",
    )
except Exception as exc:
    report("woocommerce api", False, str(exc))


print()
if all(results):
    print("All checks passed — ready for the backfill.")
    sys.exit(0)

print("Some checks failed. Fix those before moving on.")
sys.exit(1)
