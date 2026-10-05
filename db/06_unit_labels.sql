-- ============================================================================
-- Storage app: one unique barcode label per PIECE (2026-09-29).
--
-- In the storage room every physical piece carries its own label: two
-- identical "Bedsheet X" have two different barcodes. So a scan cannot be
-- compared with the item number (SKU) directly; the app looks the label up
-- here to learn which item it is.
--
-- Where real labels come from is still open (an ERP, or the label
-- printer). Until then the app makes TEST labels: the "Test labels" button
-- on an order's scan screen creates one per piece (source = 'test').
--
-- Plain item-number scans still work, so orders without labels can still be
-- packed.
--
-- On an EXISTING database this file has not run (initdb files only run when
-- the volume is first created). Run it once by hand, see README.md. Safe to
-- run again.
-- ============================================================================

CREATE TABLE IF NOT EXISTS unit_labels (
    -- The code the scanner types, as printed on the label.
    barcode         TEXT PRIMARY KEY,

    -- Which item the piece is: the WooCommerce SKU (our "item number").
    sku             TEXT NOT NULL,

    -- 'test' for labels made by the Test labels button; later e.g. 'erp'.
    source          TEXT NOT NULL DEFAULT 'test',

    -- Test labels only: the order they were made for, so pressing the button
    -- again shows the same labels instead of making new ones. Any piece of an
    -- item can still go into any order, as in the real storage room.
    test_order_id   BIGINT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The order this piece was packed into, set by Approve. NULL = still on
    -- the shelf. A label that is packed cannot be packed into a second
    -- order. ON DELETE SET NULL: "Undo packing" deletes the packed_orders
    -- row, which frees its labels again.
    packed_order_id BIGINT REFERENCES packed_orders(order_id) ON DELETE SET NULL
);

-- Scanners may send lower case or a stray space; the app compares
-- lower(trim(code)), so that must be unique too, and fast to look up.
CREATE UNIQUE INDEX IF NOT EXISTS idx_unit_labels_code ON unit_labels (lower(barcode));
CREATE INDEX IF NOT EXISTS idx_unit_labels_packed ON unit_labels (packed_order_id);
CREATE INDEX IF NOT EXISTS idx_unit_labels_test_order ON unit_labels (test_order_id);
