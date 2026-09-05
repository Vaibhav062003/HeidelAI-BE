# Logged-in cart recovery installation and release checks

Implemented: authenticated pre-checkout cart capture and existing WhatsApp recovery
worker integration. **Not live until the merchant completes the installation below.**

## Install

1. Deploy this backend. Keep existing `SHOPIFY_CLIENT_SECRET` and public base URL.
2. In the existing Shopify app configuration set app proxy prefix `apps`, subpath
   `heidelai`, URL `https://YOUR_EXISTING_BACKEND/api/shopify/storefront`.
   Shopify forwards `/apps/heidelai/cart` to `/api/shopify/storefront/cart`.
   Use the actual production backend for this shop; do not point a production
   theme at a development database. Configure `write_app_proxy` if required by
   the app's configuration workflow.
3. Add `read_products` to the app's requested scopes and reconnect the store in
   HeidelAI. Keep its existing `read_orders`, `read_customers`, and
   `read_fulfillments` scopes. Customer phone access requires the app's protected
   customer data access to be approved. Verify checkout and order webhooks work.
4. Upload `heidel-cart-recovery.js` to the theme's Assets, add
   `heidel-cart-recovery.liquid` to Snippets, and render it once before `</body>`
   in `layout/theme.liquid` with `{% render 'heidel-cart-recovery' %}`.
   A theme app embed can package these same files later; this release uses the
   merchant-installed snippet. Position/style the consent text visibly.
5. In HeidelAI enable Shopify recovery and select its WhatsApp sender and an
   approved Marketing cart template. Existing template/footer and 24-hour
   per-recipient frequency cap apply. Pre-checkout delay is fixed at five minutes
   from the most recently captured cart change; checkout delay keeps its setting.
6. Sign in as a test customer with a valid phone in their Shopify account, select
   the explicit WhatsApp reminder checkbox, then add a normal product. Confirm
   the signed proxy POST returns `pending` and a `source=storefront_cart` record
   exists. No browser-supplied phone/customer identity is accepted.
7. Leave the cart without checking out. After five minutes plus worker scheduling
   latency, confirm exactly one WhatsApp template and its inbox reconciliation.

## Required acceptance cases before enabling for real shoppers

- Add/change/clear quantities; updates restart the five-minute timer, unchanged
  polling does not. Cart-empty and consent revocation cancel a pending send.
- Start checkout before due, complete purchase, retry/reorder webhooks, and race
  a cart update against a worker claim. No second cart message after checkout;
  existing checkout recovery may later send according to its own policy/cap.
- Anonymous customer, expired/forged proxy signature, body hash mismatch, missing
  phone, phone changed after consent, missing scopes, inactive/deleted variant,
  STOP opt-out, invalid template and disconnected sender must not send.
- Refresh/reopen page, two tabs, repeat identical capture, and two different
  customers must preserve recipient and cart isolation.
- Existing abandoned-checkout and Shopify order messaging still work.

## Limits that must remain visible

Login alone is not WhatsApp marketing consent. Missing signed customer ID is
rejected, including account/theme setups where Shopify does not expose a signed
storefront login. No SMS/email consent is treated as WhatsApp consent.

Only Shopify Online Store themes running this snippet are observed. Standard
fetch/XHR cart changes trigger capture immediately; visible-page polling every
15 seconds handles custom theme events and other tabs. Backend capture requests
are made only on content changes. Failed consent revocation stays queued in
session storage and retries even while the checkbox is unchecked; receipt cannot
be guaranteed after a shopper closes a page while their network is offline.
Headless
stores, disabled JavaScript, blocked requests, unsupported checkout buttons and
cart changes after the last successful observation cannot be inferred.
Late/missing Shopify checkout webhooks can race a send; verify merchant webhook
health. This is not a guarantee of observing every cart or exactly-once external
delivery after a network failure; existing worker uses `delivery_unknown` to
avoid blind retries after provider acceptance is uncertain.

Subscription/selling-plan and customised-line carts are cancelled by the snippet
because numeric variant cart permalinks cannot faithfully restore them. Recovery
links restore standard variants/quantities; do not promise captured prices,
discounts or inventory availability. Customer phone and active variants are
revalidated from Shopify before sending. Product titles come only from Shopify.

Official references:
- https://shopify.dev/docs/apps/build/online-store/app-proxies/authenticate-app-proxies
- https://shopify.dev/docs/api/ajax/reference/cart
- https://shopify.dev/docs/apps/build/checkout/create-cart-permalinks

Local checks (all mocked, no live messages):

```
python3 tests/shopify/test_storefront_cart.py
python3 tests/shopify/test_shopify_worker_runtime.py
node tests/shopify/test_storefront_capture.cjs
node --check storefront/shopify/heidel-cart-recovery.js
```
