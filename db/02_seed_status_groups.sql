-- ============================================================================
-- Seed: a proposed grouping of order statuses (sale / refund / failed /
-- cancelled / open / other). Business sign-off is the one thing it needs: when
-- the grouping changes, edit the rows here (or UPDATE the live table). No
-- application code changes.
--
-- The sync itself uses status=any and never a whitelist; this table only
-- classifies what the sync already brought in. Statuses missing from this
-- table show up as "other" (unclassified) instead of vanishing.
-- ============================================================================

INSERT INTO order_status_groups (status, status_group, note) VALUES
    -- ---- Sale: paid, fulfilling or fulfilled -------------------------------
    ('delivered',          'sale',      'Terminal success state of the fulfilment pipeline'),
    ('on-the-way',         'sale',      'In transit'),
    ('completed',          'sale',      'Standard WooCommerce status'),
    ('processing',         'sale',      'Standard WooCommerce status'),

    -- ---- Refund / return: money going back ---------------------------------
    ('refunded',           'refund',    'Standard WooCommerce refund'),
    ('returned',           'refund',    'Return completed'),
    ('rtnawb',             'refund',    'Return airway bill — return in progress'),
    ('recieved',           'refund',    'Return processing (slug is misspelled upstream)'),
    -- Kept OUT of the refund group by a decision (2026-09-23) until its
    -- origin is known: it shows as its own status under "other".
    ('custom-refunded',     'other',     'Custom status added by a plugin; kept separate from returns until it is classified'),

    -- ---- Failed payment -----------------------------------------------------
    ('failed',             'failed',    'Standard WooCommerce payment failure'),
    ('tamara-p-failed',    'failed',    'Tamara BNPL payment failed'),
    ('tamara-c-failed',    'failed',    'Tamara BNPL capture failed'),
    ('tamara-a-failed',    'failed',    'Tamara BNPL authorisation failed'),

    -- ---- Cancelled: never fulfilled -----------------------------------------
    ('cancelled',          'cancelled', 'Standard WooCommerce cancellation'),
    ('tamara-p-canceled',  'cancelled', 'Tamara BNPL payment abandoned'),
    ('tamara-o-canceled',  'cancelled', 'Tamara BNPL order cancelled'),
    ('cancelrequested',    'cancelled', 'Cancellation requested by customer'),

    -- ---- Open: not yet resolved, excluded from sales ------------------------
    ('pending',            'open',      'Awaiting payment; not a return'),
    ('on-hold',            'open',      'Standard WooCommerce hold'),
    ('checkout-draft',     'open',      'Abandoned checkout, never an order'),
    ('tamara-a-done',      'open',      'Tamara authorised, not yet captured'),
    ('tamara-p-capture',   'open',      'Tamara capture in progress'),
    ('printed',            'open',      'Label printed, pre-dispatch'),
    ('shipping',           'open',      'Dispatch in progress'),

    -- ---- Other: decide case by case -----------------------------------------
    ('refund-rejected',    'other',     'Refund denied — arguably still a sale'),

    -- ---- Store pickup -------------------------------------------------------
    -- Grouped as 'sale': a collected pickup order is fulfilled revenue.
    ('collected',          'sale',      'Pickup collected')
ON CONFLICT (status) DO NOTHING;   -- re-running this file is harmless
