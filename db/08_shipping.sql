-- ============================================================================
-- Storage app: loading packed orders onto the courier's truck (2026-09-30).
--
-- Packed is not shipped: a ready box can be forgotten when the courier
-- comes. So at handover every BOX is scanned (its BOL label) in the app's
-- "Load onto truck" screen. When all of an order's boxes are scanned, the
-- order leaves "Packed orders" and appears in "Orders being shipped".
--
-- On an EXISTING database this file has not run (initdb files only run when
-- the volume is first created). Run it once by hand, see README.md. Safe to
-- run again.
-- ============================================================================

-- One row per box scan at the truck. An order packed in 2 boxes needs 2.
-- (Both boxes carry the same BOL label, so the app cannot tell box 1 from
-- box 2: it counts scans.) ON DELETE CASCADE: undoing the packing, or the
-- order leaving our copy, removes its loads too.
CREATE TABLE IF NOT EXISTS box_loads (
    id           BIGSERIAL PRIMARY KEY,
    order_id     BIGINT NOT NULL REFERENCES packed_orders(order_id) ON DELETE CASCADE,
    scanned_code TEXT NOT NULL,                    -- as scanned, for the record
    loaded_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_box_loads_order ON box_loads (order_id);

-- When the LAST box was loaded: the order is then "being shipped".
-- NULL = still in Packed orders (none or only some boxes loaded).
ALTER TABLE packed_orders ADD COLUMN IF NOT EXISTS shipped_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_packed_orders_shipped_at ON packed_orders (shipped_at);
