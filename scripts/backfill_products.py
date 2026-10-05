"""Backfill the product → category mapping.

    python -m scripts.backfill_products
    python -m scripts.backfill_products --pages 2   # small test run

Why this exists
---------------
"Sales by category" is a requirement, but WooCommerce line items do not carry
category data — only product_id and name. So the mapping has to come from the
products endpoint and be joined at query time.

This is a small pass: the product catalogue is in the thousands, not the
hundreds of thousands, so plain pagination is fine here. The deep-OFFSET
problem that forced date windows on the orders backfill does not bite at this
scale.

Re-running is safe: each product's categories are replaced, not appended, so
a product moved between categories ends up with the correct set rather than
the union of old and new.

Note on the join: a product in several categories will have its sales counted
under each one. That is the usual convention, but it means category totals sum
to more than overall sales. The metrics layer has to state which it does.
"""

import argparse
import sys

from app.db import connect
from app.woo import get, total_count

PER_PAGE = 100
PRODUCT_FIELDS = "id,categories"   # excludes descriptions, images, meta_data

INSERT_CATEGORY = """
INSERT INTO product_categories (product_id, category_id, category_name, synced_at)
VALUES (%s, %s, %s, now())
ON CONFLICT (product_id, category_id) DO UPDATE
    SET category_name = EXCLUDED.category_name,
        synced_at     = now()
"""


def write_product(cur, product: dict) -> int:
    """Replace one product's category rows. Returns how many were written."""
    product_id = product["id"]
    categories = product.get("categories") or []

    # Replace rather than merge, so a category the product was removed from
    # actually disappears instead of lingering forever.
    cur.execute("DELETE FROM product_categories WHERE product_id = %s",
                (product_id,))
    for category in categories:
        cur.execute(INSERT_CATEGORY,
                    (product_id, category["id"], category.get("name")))
    return len(categories)


def run(max_pages: int | None):
    total = total_count("wc/v3/products", {"status": "any"})
    print(f"{total:,} products in the catalogue.\n")

    page = 1
    products_seen = rows_written = uncategorised = 0

    while True:
        products, headers = get("wc/v3/products", {
            "status": "any",
            "orderby": "id",
            "order": "asc",      # stable ordering so pages cannot shuffle
            "per_page": PER_PAGE,
            "page": page,
            "_fields": PRODUCT_FIELDS,
        })
        if not products:
            break

        # Commit per page so a crash costs at most 100 products.
        with connect() as conn:
            cur = conn.cursor()
            for product in products:
                written = write_product(cur, product)
                rows_written += written
                products_seen += 1
                if written == 0:
                    uncategorised += 1

        print(f"  page {page}: {products_seen:,} products · "
              f"{rows_written:,} category rows", end="\r")

        total_pages = int(headers.get("X-WP-TotalPages", 1))
        if page >= total_pages or (max_pages and page >= max_pages):
            break
        page += 1

    print(f"\n\nDone. {products_seen:,} products, "
          f"{rows_written:,} product-category rows.")
    if uncategorised:
        # Worth surfacing: these products' sales will not appear under any
        # category, so category totals will not reconcile with overall sales.
        print(f"{uncategorised:,} product(s) have no category — their sales "
              f"will be missing from the category breakdown.")

    with connect() as conn:
        distinct = conn.execute(
            "SELECT count(DISTINCT category_id) FROM product_categories"
        ).fetchone()[0]
        orphans = conn.execute("""
            SELECT count(DISTINCT i.product_id)
              FROM order_items i
              LEFT JOIN product_categories pc ON pc.product_id = i.product_id
             WHERE pc.product_id IS NULL
               AND i.product_id IS NOT NULL
        """).fetchone()[0]

    print(f"{distinct:,} distinct categories.")
    if orphans:
        # Usually deleted products that still appear in historical orders.
        print(f"{orphans:,} product(s) appear in order line items but have no "
              f"category mapping (likely deleted from the catalogue since).")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int,
                        help="Stop after N pages. Use --pages 1 to test.")
    args = parser.parse_args()
    return run(args.pages)


if __name__ == "__main__":
    sys.exit(main())
