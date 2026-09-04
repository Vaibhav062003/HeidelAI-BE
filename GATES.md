# Gates: inbox outage patch

Scope: remove event-loop blocking on the incident paths and isolate new restock polling; preserve authorization and delivery safeguards. This is not a claim that the initiating database timeout has been reproduced.

- [x] G1: simulated database stalls do not freeze the event loop; restock failure does not prevent intent-worker startup or polling
  CHECK: python3 test_inbox_outage.py
  EXPECT: Inbox outage regression checks passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/private/tmp/heidel-sales-fixes.YhOQq3/backend; path=a7b9edecd96a/24 entries; output=Ran 10 tests in 0.155s | OK

- [x] G2: existing pricing, restock, WhatsApp binding and inbox contracts remain valid
  CHECK: python3 -m unittest test_stock_sheet_writeback test_product_sheet_pricing_foundation test_product_requests test_restock_dispatch -q && python3 test_whatsapp_number_binding.py && python3 test_inbox_live_delivery_contract.py && python3 test_runtime_safety_contract.py && git diff --check
  EXPECT: Runtime safety contracts passed.
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/private/tmp/heidel-sales-fixes.YhOQq3/backend; path=a7b9edecd96a/24 entries; output=Ran 50 tests in 0.375s | OK

- [ ] G3: affected deployed inbox loads historical messages and receives a new message after the patch
  EVIDENCE: pending; deployment and authenticated live acceptance have not been performed
