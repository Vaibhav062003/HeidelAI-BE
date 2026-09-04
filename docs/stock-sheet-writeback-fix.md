# Stock pricing → Google Sheet writeback

Status: implemented locally; not deployed or tested against a live merchant Sheet.

## Confirmed faults

- `primary_product_sheet()` used an undefined `db`, making Sheet edits fail.
- Stock edit/retry UI announced success for HTTP 200 responses carrying `failed`, `needs_review`, or `queued_offline` status.
- Writeback ignored saved column mappings and non-A column offsets, trusted cached row numbers, and did not refresh a rejected write token.
- Editing the legacy `price` column could be overwritten by the canonical target selling price during the next sync.
- Local updates bypassed the Sheet normalizer, leaving price provenance and canonical selling price stale.

## Implemented

- Use the database factory; require the designated primary Sheet, including when another Sheet is active while the primary is syncing.
- Resolve the current SKU row before every edit, reject duplicates/deleted source products, and preserve formula cells and conflicting external edits.
- Preserve internal blank headers and column offsets; honor persisted mappings and quoted tab names.
- Update target selling price when present. If only an approved floor exists, require a mapped target selling price rather than silently changing the floor.
- Refresh expired tokens on write/readback; verify the returned row and changed cells before publishing the canonical product and source snapshot.
- Reuse the Sheet sync lease across writeback and catalogue publication, with durable reconciliation scheduled after failures or ambiguous delivery.
- Display truthful completed, pending, failed, and review statuses for both edit and retry/approval actions.
- Keep existing Tally-first verification: an offline Tally connector leaves the change pending; the UI no longer says it is complete.

## Verification

`python3 -m unittest test_stock_sheet_writeback test_product_sheet_pricing_foundation -q` — 29 tests pass, mocked network/database only.

Frontend: `node scripts/check-stock-writeback-status.mjs`, `node scripts/check-stock-workspace.mjs`, and `node scripts/check-stock-gst-ui.mjs bulk` pass.

The existing `check-stock-gst-ui.mjs product` fails an unrelated source-string assertion in `SalesInboxDemo.tsx` for `gstInclusive: product.gst_inclusive === true`; it is not a Stock writeback failure.

## Remaining deployment checks and boundaries

- Run a real edit on a test Sheet with custom headers, a moved SKU row, and an expired/reconnected Google token; verify both Sheet and catalogue after resync.
- No live Sheet, database, or customer message was modified by these tests.
- Google Sheets does not provide conditional cell updates. A human sorting/editing the Sheet between preflight and write cannot be made atomic by the application lease; readback detects a changed identity/value and reports failure. Avoid simultaneous manual structural edits during stock writes.
- The sync lease prevents competing application writes/reconciliation, not human Google Sheets edits. Existing bulk GST operations have mapping/offset and stale-row corrections but do not yet share the single-edit write lease.
