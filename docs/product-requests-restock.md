# Customer product requests and restock outreach

## Existing mechanisms reused

- `purchase_intent_queue` and its existing structured extractor capture product
  enquiries separately from time-bound purchase intent. No second model call.
- Conversation team access, configured `organizations.sales_catalogue_org_id`,
  existing template sender, WhatsApp suppression and broadcast quota checks.
- The existing intent-worker loop dispatches the durable restock outbox. No new
  environment variables, scheduler or external queue.

## Behaviour

Dashboard → Product requests lists new WhatsApp product enquiries, the customer,
original line, timestamp and exact supporting customer message. Filters cover
text/customer, interest status, match status, exact product and WhatsApp line.
Pagination is 50 rows. The register does not silently scan historical chats.

Every catalogue match requires human review, including capacity/colour/model
variants. Unknown products remain recorded and unmatched; low-confidence entries
are visibly marked. Agent messages, private notes, missing evidence, and dealer
conversations cannot create customer demand. Explicit withdrawal suppresses
interest. Existing completed/dismissed rows are not silently reopened by AI.

To send, an administrator selects requests for ONE exact catalogue product and
ONE original WhatsApp line, creates/checks the dedicated Marketing template,
reviews the recipient preview, then explicitly queues it. Each recipient needs
recorded restock permission; a generic product enquiry is not treated as consent.
The exact footer is `Powered by Heidelai.com - AI Sales Agent`.

Eligibility is checked at preview and dispatch: current conversation identity,
current permissions, active connection, unchanged catalogue/permission, customer
opt-outs, active interest no older than 90 days, positive stock updated within
24 hours, approved template and available broadcasting quota. The original
customer/line is never replaced with an organisation primary number.

Queued/checking jobs can be cancelled by dismissing interest or clearing consent.
The provider-send boundary is explicit. Once sending begins, cancellation cannot
recall a message. Definitely unsent blocked/cancelled jobs may be revalidated and
retried; accepted/unknown jobs are never automatically retried. A provider timeout
or interrupted send is UNKNOWN, not failed-and-safe-to-resend. Meta acceptance is
shown separately from subsequent message delivery status.

## Explicit limits / release gates

- Text-only extraction uses the existing recent-chat context window. It does not
  read image/PDF/audio requirements unless their text is already in chat. Model
  extraction is not guaranteed recall; staff must review evidence and variants.
- One accepted notification per organisation/customer/product/line. Repeated
  stock cycles do not silently reset this guard. An explicit, audited new-interest
  cycle would be needed for future repeat campaigns.
- At most 100 selected requests per API request (UI shows 50 per page). Dispatch
  is deliberately rate-limited; five minutes is not promised for this manual flow.
- A synced positive stock value is not a physical reservation. Concurrent sales
  may exhaust stock; message invites a reply for current pricing/availability.
- Live end-to-end verification with consenting test customers, a real approved
  template, Clerk membership changes and webhook delivery is still required.
- No database backfill, production message, merge or deployment was performed.

Official API reference used for exact-name template lookup:
https://www.postman.com/meta/whatsapp-business-platform/documentation/wlk6lh4/whatsapp-cloud-api

## Checks

`python3 -m unittest test_product_requests -v`

Service race/identity/retry regression tests are recorded in the top-level
sales-workflow checklist. Frontend: `node scripts/check-product-requests.mjs`.
