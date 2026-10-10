# Customer product requests and restock outreach

## Existing mechanisms reused

- `purchase_intent_queue` and its existing structured extractor capture product
  observations separately from time-bound purchase intent. One additional independent
  verification call is used only for uncertain batches; durable event results are reused.
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

Capture validates an exact product/category phrase and quote, then evaluates the
**whole source message**, including negation, completion, references and notification
revocation. Legitimate titles such as Hello Kitty backpack and Nothing Phone (2) are
not rejected by word blacklists. Unique exact matches in the tenant-selected catalogue
can establish identity but do not establish customer interest. Compatible variants are
required; fuzzy search remains a staff matching aid. Unknown products and categories
remain supported through independent verification.

Structured observations include entity type, interest action, notification action and
quoted requirements. Explicit build components share their original source message;
models are never invented from a budget/specification. Missing verification, malformed
results, disagreement and incomplete context produce `needs_review`. A batch receives
at most one verification call with a 45-second timeout and a 60,000-character context
limit. Each item is validated independently; product capture failure does not prevent
timed-purchase follow-ups being handled before the job is retried.

`product_request_events` stores source-grounded observations and cached decisions.
Identity uses exact catalogue IDs or normalized text plus quoted variant requirements;
quantity and budget do not change the product identity. Existing request/outbox IDs are
preserved. Source times guard updates, replay cannot freshen interest, and conflicting
same-time observations need review. Closed/rejected interest is not automatically reopened;
staff confirmation is required and old notification consent/deduplication is not reset.

Pending uncertain changes are stored separately from accepted evidence. Review holds
exclude requests from outreach. Uncertain withdrawal/revocation holds applicable existing
interest and cancels pending outreach. Staff can confirm/correct/reject without a catalogue
match. Rejecting an uncertain update to an accepted request preserves the accepted facts.
Unresolved positive requests display “Product needs identification.”

The queue retains triggering message IDs and the original WhatsApp line when coalescing
pending work. Trigger messages and reply anchors are fetched independently of the latest
30-message context. Public intervening context is bounded at 100 messages; a larger gap
forces review. Customer replies can select an agent offer only when the exact reply link
and structured product ID are verified against the selected catalogue.

The default list is confirmed active interest. `verification=needs_review` includes
pending holds, with independent interest/match/line filters. Legacy rows without verification
are interpreted as confirmed only when `reviewed_by` exists; otherwise they enter review.
No automatic backfill, rewrite, deletion or alias merge is performed.

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
Capture and staff review use the same revision-guarded request-row update to cancel.
The provider-send boundary atomically claims that same request row using the queued
revision, verification, interest, consent and identity snapshot. A withdrawal/review hold
that wins first prevents sending; a send claim that wins first is reported as already sending. Once sending begins, cancellation cannot
recall a message. Definitely unsent blocked/cancelled jobs may be revalidated and
retried; accepted/unknown jobs are never automatically retried. A provider timeout
or interrupted send is UNKNOWN, not failed-and-safe-to-resend. Meta acceptance is
shown separately from subsequent message delivery status.

## Explicit limits / release gates

- Text-only extraction uses source IDs and bounded conversation context. It does not
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

## API release contract and checks

Release the dashboard and API together. PATCH review requires `expected_revision` and
`verification_status`, with optional corrected `product_name`, catalogue match, interest
status and separate consent evidence. Preview includes each request's `revision`; send
requires a `revisions` map keyed by request ID. A changed row returns 409. Old clients
without these fields receive 422 rather than silently approving stale data.

Offline checks:

```sh
python3 -m unittest test_product_requests test_product_request_v2 test_product_request_schema test_restock_dispatch test_inbox_outage test_release_catalogue -q
python3 scripts/evaluate_product_requests.py
```

Pydantic schema tests require the existing runtime dependency and otherwise skip.
Frontend: `node scripts/check-product-requests.mjs` and `tsc --noEmit --incremental false`.
The regression suite covers provenance, semantic disagreement/failure, legacy eligibility,
source retention, state/revision conflicts, consent reopening, durable replay and send races.

Bounded real-model evaluation (24 labelled English/Hindi/Hinglish fixtures; at most one
extra verification call per fixture batch):

```sh
python3 scripts/evaluate_product_requests.py --live --limit 24
```

It reports accepted-entity precision, misses, review rate, primary/extra calls and errors.
This environment has no configured model credentials; **no real-model quality measurement
or production WhatsApp send has been completed**. Fixture/unit results are not a model
precision or recall claim. Review unknown/reference observations conservatively until the
labelled evaluation and a consenting end-to-end outreach check have passed.

## Legacy historical cleanup compatibility

Archived requests remain hidden from capture, review and outreach. Existing public IDs
and sparse unique `product_key` values are retained. The old historical-cleanup policy
is isolated from v2 extraction: generation using v2 observations returns review entries
instead of rewriting or archiving records. Previously reviewed version-2 reports retain
their source, snapshot, transaction and pending-outreach safeguards; stale reports fail.
Use dashboard review for v2 product observations. No historical cleanup was executed.
