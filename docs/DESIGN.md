# Design notes

Why the project is built the way it is.

## Architecture

```
WooCommerce ──(1) backfill: historical orders via REST ──────┐
            ──(2) reconcile: every few minutes, orders        ├─> PostgreSQL ─> metrics API ─> dashboard
                  modified since the last clean run ─────────┘                              └> storage app
OTO (shipping platform) ──(3) read-only sync: BOL numbers, carriers, delivery times ─┘
```

- **Local database, never live queries.** A busy WooCommerce store can take
  seconds per request. Syncing once and querying locally gives millisecond
  dashboard queries (measured on 190k synthetic orders: a month's headline
  tiles in ~4 ms).
- **Reconciliation before webhooks.** Polling for `modified_after` needs only a
  read key and meets a "within a few minutes" goal. Webhooks would add speed but
  also need a public endpoint and write access; they are an optional upgrade.
- **Idempotent, resumable jobs.** Every write is an upsert keyed on the
  WooCommerce id, guarded by `date_modified` so a late update never overwrites a
  fresher row. Progress is a watermark that only advances after a fully clean
  run, so a failure is retried, never skipped.
- **Date windows, not deep pagination.** WooCommerce turns `page=N` into an SQL
  OFFSET; month windows keep every page shallow.

## Data and metrics

- **Raw status slug is stored**, and a table maps slug → sale / refund / failed /
  cancelled / open / other. Metrics `LEFT JOIN` it and treat a missing row as
  `other`, so a new custom status shows up as unclassified instead of vanishing.
- **Gross revenue** (`total`, including shipping and VAT) matches the WooCommerce
  admin; one constant switches to merchandise-only.
- **Honest comparisons.** `change_pct` is `null` when the previous period starts
  before the imported data or its value is zero. The UI shows "no comparison
  data", never 0% or ∞. Periods are calendar periods in the reporting timezone,
  converted to half-open UTC ranges.
- **Return rate is cohort-based** (orders placed in the period with a refund ÷
  sale orders placed in the period); refund totals use the refund date.
- **Privacy by construction.** Only the billing email is kept (to count unique
  customers, since guest orders share `customer_id = 0`); everything else is
  stripped before write, including from the stored raw JSON.

## Front end

- No framework and no build step: vanilla ES modules, Chart.js bundled so it
  works offline. Ranked lists are HTML "bar tables" (a chart and a table at once).
- All user-visible text lives in `web/i18n.js`, English and Arabic side by side;
  the layout flips with logical CSS properties. Store data (product names) is
  never translated, and everything from the store is inserted with `textContent`.
- The view (section, period, language) lives in the URL hash.

## Storage app

- It only writes its own tables (`packed_orders`, `box_loads`, ...) and never
  writes to WooCommerce. Approve is re-validated on the server, under a row lock.
- Every piece can have its own barcode (`unit_labels`); a scan is a label or an
  item number. Boxes are loaded onto the carrier's truck by scanning each BOL.

## Deployment

One VPS running Docker Compose, with no public ports: a Cloudflare Tunnel
provides HTTPS and Cloudflare Access an emailed login. See `deploy/RUNBOOK.md`.

## Known limitations

Deleted or trashed orders are not detected by `modified_after`; there is no
in-app authentication (access control happens at the edge); the history window
is a policy (`HISTORY_START`), not a technical limit.
