-- ============================================================================
-- Storage app: which workers are assigned to what (2026-09-28).
--
--   * pick_workers  — who collects a pick list ("Picked by Sam, Ahmed"),
--                     one row per From-To range of dates.
--   * order_workers — who packs an order ("Order 123 packed by Sam,
--                     Ahmed"), one row per order.
--   * packed_orders.packed_by — the names on an order when it was approved,
--                     kept even if the assignment is changed afterwards.
--
-- Names are free text, as typed; the app trims them and drops duplicates.
-- There is no separate list of workers: the names already used are offered as
-- suggestions. Nothing here goes to WooCommerce.
--
-- On an EXISTING database this file has not run (initdb files only run when
-- the volume is first created). Run it once by hand — see README.md.
-- ============================================================================

-- An early version keyed this table by a single "batch day". The pick list
-- now covers a From-To range of dates, so that version is replaced (it only
-- ever held test names). Safe to run again: it only drops the OLD shape.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'pick_workers' AND column_name = 'batch_day') THEN
        DROP TABLE pick_workers;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS pick_workers (
    -- The dates the pick list covers (both included), as chosen on the page.
    date_from   DATE        NOT NULL,
    date_to     DATE        NOT NULL,
    workers     TEXT[]      NOT NULL,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (date_from, date_to)
);

CREATE TABLE IF NOT EXISTS order_workers (
    order_id    BIGINT PRIMARY KEY REFERENCES orders(id) ON DELETE CASCADE,
    workers     TEXT[]      NOT NULL,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE packed_orders ADD COLUMN IF NOT EXISTS packed_by TEXT[] NOT NULL DEFAULT '{}';
