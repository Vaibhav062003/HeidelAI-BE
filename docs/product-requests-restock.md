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
variants. Specific named products outside the catalogue remain unmatched. Generic
categories, setup/build descriptions, complete sentences and low-confidence or
ambiguous references are not saved. Requested custom builds produce identifiable
component products, not a build description; unspecified RAM/storage specs are
omitted. A named product need not contain digits.

Each extraction carries a recent customer's exact evidence and a separately
grounded product mention/reference message. Public older context (within the
existing 30-message window) may resolve a clear reference; only the recent ten
messages can supply customer-request evidence. Seller suggestions alone do not
create demand. Private messages and dealer conversations remain excluded.
Follow-up questions reuse product identities. Stable request IDs are separate
from the sparse unique product_key, which reuses request_key without changing
restock dispatch identity. Existing completed/dismissed rows are not reopened.
The page leads with the reviewed catalogue name or extracted product name;
verbatim customer evidence is available under Customer message and in review.

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

`python3 -m unittest test_product_requests test_product_request_extraction test_restock_dispatch -v`

With installed backend dependencies and Moonshot credentials, evaluate six
synthetic screenshot/product cases against the actual extractor:
`python3 test_product_request_extraction.py --live`. This calls the model only;
it does not connect to MongoDB or schedule/send customer messages.

## Review and clean previously collected requests

Generate an organization-scoped report (model calls and database reads only):

```sh
python3 -m scripts.cleanup_product_requests --org-id ORG --report /tmp/product-requests.json
```

Review `changes`, `before`, `after`, and `review` in that file. The file contains
customer evidence and is created with owner-only permissions; an existing file
is not overwritten. Missing evidence, failed/ambiguous extraction, conflicting
interest statuses and corrections affecting reviewed products or outreach are
reported for review, not automatically discarded. Work is scoped to saved
requests and their source context; unrelated chat history is not backfilled.

Apply the exact reviewed report without another model call:

```sh
python3 -m scripts.cleanup_product_requests --org-id ORG --apply-report /tmp/product-requests.json
```

Each conversation applies in a MongoDB transaction (replica set/transaction
support required). Request, customer identity and evidence snapshots must still
match; pending outreach blocks application. On a stale report, regenerate and
review. A partially applied report can be rerun: completed conversations are
recognized and skipped. Concurrent writes abort the affected transaction.
Original data remains in cleanup_original and archived rows, with replaced_by
links. Splits receive no inherited catalogue match or notification permission;
terminal interest status is retained. Archives are excluded before list counts
and pagination and cannot be used for outreach. No report has been applied to
production as part of the code changes.

Service race/identity/retry regression tests are recorded in the top-level
sales-workflow checklist. Frontend: `node scripts/check-product-requests.mjs`.
