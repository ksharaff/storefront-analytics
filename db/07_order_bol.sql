-- ============================================================================
-- Storage app: each order's BOL number (bill of lading), from OTO
-- (tryoto.com, the shipping platform)
-- (2026-09-29).
--
-- The "OTO sync" button on Orders to pack fetches the BOL number of every
-- order on screen and stores it here; the order tables show it in their
-- "BOL no." column, and the packed list shows it too.
--
-- Until the app is connected to OTO, the sync runs in TEST mode and invents
-- numbers (source = 'test', shaped "TEST-0000000000" so they can never be
-- mistaken for real ones). See fetch_bols_from_oto() in app/storage.py.
--
-- On an EXISTING database this file has not run (initdb files only run when
-- the volume is first created). Run it once by hand, see README.md. Safe to
-- run again.
-- ============================================================================

CREATE TABLE IF NOT EXISTS order_bol (
    -- The website order. ON DELETE CASCADE: goes with the order.
    order_id   BIGINT PRIMARY KEY REFERENCES orders(id) ON DELETE CASCADE,
    bol_no     TEXT NOT NULL,
    -- 'test' while simulated; 'oto' once read from OTO for real.
    source     TEXT NOT NULL DEFAULT 'test',
    synced_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Added 2026-09-30, from OTO's orderDetails (checked on the real account):
--   bol_no        OTO's shipmentId, the carrier's AWB number. Empty until OTO
--                 has created the shipment, so no longer NOT NULL.
--   boxes         OTO's packageCount; the scan screen checks the typed box
--                 count against it.
--   carrier       OTO's dcName (Aymakan, Aramex, ...).
--   tracking_url  OTO's trackingURL: the BOL no. links to it.
-- ADD COLUMN IF NOT EXISTS: re-running this file on a database that already
-- has the first version upgrades it in place, keeping its rows.
ALTER TABLE order_bol ALTER COLUMN bol_no DROP NOT NULL;
ALTER TABLE order_bol ADD COLUMN IF NOT EXISTS boxes        INTEGER;
ALTER TABLE order_bol ADD COLUMN IF NOT EXISTS carrier      TEXT;
ALTER TABLE order_bol ADD COLUMN IF NOT EXISTS tracking_url TEXT;
