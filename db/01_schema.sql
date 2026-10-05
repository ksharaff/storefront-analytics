-- ============================================================================
-- Acme Home Store — analytics database schema
--
-- Design rules this file follows (from docs/DESIGN.md):
--   * Money is NUMERIC(12,2). Never float — floats lose cents.
--   * Timestamps are TIMESTAMPTZ holding UTC. WooCommerce gives us *_gmt
--     fields; we convert to Asia/Riyadh at query time, not at write time.
--   * orders.status stores the RAW WooCommerce slug (dozens of possible values
--     on a customised store). Never a boolean "is_sale" — which slugs count as a sale
--     is still awaiting company sign-off, and storing a boolean would bake
--     in an answer we do not have yet.
--   * Every table keeps the raw API JSON, so a changed metric definition can
--     be recomputed locally instead of re-pulling 190k orders from a store
--     that takes ~4 seconds per request.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- orders — one row per WooCommerce order.
-- Powers: total sales, order count, AOV, status breakdown, payment methods,
--         failed payments, unique customers.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    -- WooCommerce's own order ID. Used as the upsert key everywhere.
    id                  BIGINT PRIMARY KEY,

    -- Human-facing order number. Usually equals id, but not guaranteed.
    number              TEXT,

    -- Raw status slug: 'delivered', 'tamara-p-failed', 'custom-refunded', ...
    status              TEXT        NOT NULL,
    currency            TEXT        NOT NULL DEFAULT 'SAR',

    -- Gross order total. On this store prices_include_tax = true, so this
    -- INCLUDES shipping and VAT. The company has not yet said whether
    -- "total sales" means this or merchandise-only — hence we store the
    -- components separately and let the query decide (see merchandise_total).
    total               NUMERIC(12,2) NOT NULL DEFAULT 0,
    shipping_total      NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_tax           NUMERIC(12,2) NOT NULL DEFAULT 0,
    discount_total      NUMERIC(12,2) NOT NULL DEFAULT 0,

    -- Merchandise revenue, computed by Postgres on write. Having both means
    -- either answer to follow-up question #5 is a one-word change in the
    -- metrics query rather than a re-backfill.
    merchandise_total   NUMERIC(12,2)
                        GENERATED ALWAYS AS (total - shipping_total - total_tax) STORED,

    payment_method       TEXT,   -- machine slug, e.g. 'tabby', 'cod'
    payment_method_title TEXT,   -- what the customer saw, often Arabic

    -- 0 for guest checkouts, which is why unique customers are counted by
    -- billing_email instead (see the index below).
    customer_id         BIGINT      NOT NULL DEFAULT 0,
    billing_email       TEXT,

    -- 'checkout', 'admin', 'rest-api', 'pos', ... Answers follow-up #7:
    -- whether in-store Point of Sale orders should be in the dashboard.
    created_via         TEXT,

    date_created_gmt    TIMESTAMPTZ NOT NULL,
    date_paid_gmt       TIMESTAMPTZ,
    -- The ordering guard for idempotent upserts: only apply an incoming
    -- update if its date_modified is >= the one already stored. Protects
    -- against a late webhook overwriting a fresher reconciliation result.
    date_modified_gmt   TIMESTAMPTZ,

    -- Full API payload. Disk is cheap; re-pulling this store is not.
    raw                 JSONB       NOT NULL,

    -- When our sync last touched this row (our clock, not WooCommerce's).
    synced_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Nearly every dashboard query is "orders in a date range, grouped by X".
-- This composite index serves the range scan and the grouping together.
CREATE INDEX IF NOT EXISTS idx_orders_created_status
    ON orders (date_created_gmt, status);

-- Reconciliation fetches by modified date, so that column needs its own index.
CREATE INDEX IF NOT EXISTS idx_orders_modified
    ON orders (date_modified_gmt);

-- "Number of purchasing customers in the period" = COUNT(DISTINCT billing_email).
-- Partial index: skip the rows where the email is missing entirely.
CREATE INDEX IF NOT EXISTS idx_orders_billing_email
    ON orders (billing_email)
    WHERE billing_email IS NOT NULL;

-- Payment-method breakdown on the home page and the payments section.
CREATE INDEX IF NOT EXISTS idx_orders_payment_method
    ON orders (payment_method);


-- ---------------------------------------------------------------------------
-- order_items — line items within an order.
-- Powers: best sellers by value, best sellers by quantity, quantity per product.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_items (
    order_id        BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    -- WooCommerce's line-item ID; unique only within its order.
    line_item_id    BIGINT NOT NULL,

    product_id      BIGINT,
    variation_id    BIGINT NOT NULL DEFAULT 0,
    name            TEXT,
    sku             TEXT,
    quantity        INTEGER       NOT NULL DEFAULT 0,

    subtotal        NUMERIC(12,2) NOT NULL DEFAULT 0,  -- before line discounts
    total           NUMERIC(12,2) NOT NULL DEFAULT 0,  -- after line discounts
    total_tax       NUMERIC(12,2) NOT NULL DEFAULT 0,

    PRIMARY KEY (order_id, line_item_id)
);

-- Best-seller queries group by product_id across a date-filtered order set.
CREATE INDEX IF NOT EXISTS idx_order_items_product
    ON order_items (product_id);


-- ---------------------------------------------------------------------------
-- product_categories — product → category mapping.
-- Line items do NOT carry category data, so "sales by category" needs this
-- lookup table, filled from GET /wc/v3/products. A product can sit in several
-- categories, so a product's sales will appear under each of them; the
-- metrics layer has to say which convention it uses.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_categories (
    product_id      BIGINT NOT NULL,
    category_id     BIGINT NOT NULL,
    category_name   TEXT,
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (product_id, category_id)
);

CREATE INDEX IF NOT EXISTS idx_product_categories_category
    ON product_categories (category_id);


-- ---------------------------------------------------------------------------
-- refunds — one row per refund. An order can have several.
-- Powers: return rate, refund totals (shown separately — the company confirmed
--         total sales is NOT reduced by refunds).
-- Source: GET /wc/v3/orders/{id}/refunds
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS refunds (
    id                  BIGINT PRIMARY KEY,
    order_id            BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,

    -- WooCommerce reports refund amounts as negative. Store positive; make
    -- the sign convention explicit so no query has to guess.
    amount              NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (amount >= 0),
    reason              TEXT,
    date_created_gmt    TIMESTAMPTZ NOT NULL,
    raw                 JSONB NOT NULL,
    synced_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_refunds_order    ON refunds (order_id);
CREATE INDEX IF NOT EXISTS idx_refunds_created  ON refunds (date_created_gmt);


-- ---------------------------------------------------------------------------
-- refund_items — which products came back, and how many.
-- Powers: most returned products.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS refund_items (
    refund_id       BIGINT NOT NULL REFERENCES refunds(id) ON DELETE CASCADE,
    line_item_id    BIGINT NOT NULL,

    product_id      BIGINT,
    variation_id    BIGINT NOT NULL DEFAULT 0,
    name            TEXT,
    -- Stored positive, same convention as refunds.amount.
    quantity        INTEGER       NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    total           NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (total   >= 0),

    PRIMARY KEY (refund_id, line_item_id)
);

CREATE INDEX IF NOT EXISTS idx_refund_items_product
    ON refund_items (product_id);


-- ---------------------------------------------------------------------------
-- order_status_groups — which raw slugs count as a sale, a refund, and so on.
-- Kept as DATA rather than as code because the grouping is still awaiting
-- company sign-off (follow-up #6). When they answer, it is an UPDATE, not a
-- code change and redeploy. Seeded in 02_seed_status_groups.sql.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_status_groups (
    status      TEXT PRIMARY KEY,
    status_group TEXT NOT NULL
        CHECK (status_group IN ('sale','refund','failed','cancelled','open','other')),
    note        TEXT
);


-- ---------------------------------------------------------------------------
-- sync_state — small key/value scratchpad for the sync jobs.
-- Keys in use:
--   backfill_last_window   last completed month window, e.g. '2023-07'
--   last_reconciled_at     UTC timestamp of the last successful reconciliation
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_state (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
