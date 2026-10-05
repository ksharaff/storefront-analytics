-- ============================================================================
-- Storage app (pick & pack for the storage room) — its own tables.
--
-- Everything else in this database is a COPY of WooCommerce. This file is the
-- first thing the storage workers write themselves: which orders they have
-- scanned, boxed and approved. It is kept apart from the synced tables on
-- purpose — the sync never touches it, and it never changes a synced row.
--
-- Nothing here is sent back to WooCommerce (yet). An approved order stays
-- `processing` in the store; the app simply stops listing it as waiting.
-- Whether Approve should also move the order on in WooCommerce is an open
-- question (it would need a WRITE API key; ours is read-only).
--
-- On an EXISTING database this file has not run (initdb files only run when
-- the volume is first created). Run it once by hand — see README.md.
-- ============================================================================

CREATE TABLE IF NOT EXISTS packed_orders (
    -- One row per packed order. The primary key is also the guard against two
    -- workers approving the same order: the second INSERT fails.
    -- ON DELETE CASCADE: if the order itself is ever removed from our copy,
    -- its packing record goes with it.
    order_id    BIGINT PRIMARY KEY REFERENCES orders(id) ON DELETE CASCADE,

    -- Typed in by the worker. Later to be checked against the shipping platform's number.
    boxes       INTEGER     NOT NULL CHECK (boxes BETWEEN 1 AND 99),

    -- What the order held when it was approved, line by line:
    --   [{"line_item_id", "name", "sku", "quantity", "manual"}]
    -- `manual` = true when a line had no barcode (SKU) and the worker
    -- confirmed it by hand instead of scanning. Kept so a later dispute
    -- ("was this checked?") can be answered, and so an order edited in the
    -- store after packing can be spotted.
    items       JSONB       NOT NULL,

    packed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Bill of lading number. Empty for now: where it comes from (the shipping
    -- company, an ERP, or typed in) is still an open question.
    bol_no      TEXT
);

-- The "packed" list is shown newest first.
CREATE INDEX IF NOT EXISTS idx_packed_orders_packed_at
    ON packed_orders (packed_at);
