-- ============================================================================
-- Dashboard, Warehouse tab: delivery time and shipping cost, from OTO
-- (tryoto.com). Filled by scripts/sync_oto_tracking.py, which only READS OTO.
--
-- 2026-09-30: EVERY OTO shipment counts, not only the orders scanned onto
-- the truck in the storage app. OTO lists about the last 90 days; this table
-- keeps what was read, so the history here grows past those 90 days.
--
-- One row per ORDER (OTO's orderId = the website order number). There is no
-- link to the orders table on purpose: OTO may know orders our database does
-- not (yet). The dashboard joins on the number only for "cost ÷ order value".
--
--   created_at    when OTO created the order's first shipment. This date
--                 decides which period the order falls in.
--   charge        what the delivery company charged (OTO's dcCharge), all of
--                 the order's shipments added up; returns and cancelled
--                 shipments are not counted.
--   picked_up_at  first "picked up" in OTO's status history
--   delivered_at  first "delivered" in OTO's status history
--                 delivery time = delivered_at - picked_up_at
--
-- Only these facts are kept: no customer name, phone or address.
--
-- On an EXISTING database this file has not run (initdb files only run when
-- the volume is first created). Run it by hand, see README.md. Safe to run
-- again.
-- ============================================================================

-- The first version (2026-09-30, commit 139ff8a) only covered orders loaded
-- onto the truck in the storage app and was never filled. Replaced below.
DROP TABLE IF EXISTS oto_tracking;

CREATE TABLE IF NOT EXISTS oto_orders (
    order_number     TEXT PRIMARY KEY,           -- OTO orderId = website order number
    carrier          TEXT,                       -- delivery company, as OTO names it
    shipment_no      TEXT,                       -- latest shipment's AWB / BOL number
    created_at       TIMESTAMPTZ NOT NULL,       -- first shipment created in OTO
    status           TEXT,                       -- OTO's latest status, as OTO writes it
    charge           NUMERIC(12,2) CHECK (charge >= 0),   -- NULL = OTO has no charge yet
    picked_up_at     TIMESTAMPTZ,                -- NULL = not picked up (yet) / not known
    delivered_at     TIMESTAMPTZ,                -- NULL = not delivered (yet)
    history_read_at  TIMESTAMPTZ,                -- when the status history was last read
    synced_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS oto_orders_created ON oto_orders (created_at);

-- "Cost ÷ order value" joins OTO's order number to ours.
CREATE INDEX IF NOT EXISTS idx_orders_number ON orders (number);
