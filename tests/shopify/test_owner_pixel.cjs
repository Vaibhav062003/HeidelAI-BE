const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const file = 'storefront/shopify/extensions/heidel-owner-pixel/src/index.js';
const source = fs.readFileSync(file, 'utf8').replace(/^import[^\n]*\n/, '');
const subscriptions = {};
let consent;
let saved = null;
let revokeOnSave = false;
const sends = [];
vm.runInNewContext(source, { URL, Set, Boolean, JSON, fetch: async (url, options) => { sends.push(JSON.parse(options.body)); }, register: callback => callback({
  init: { customerPrivacy: { analyticsProcessingAllowed: false } },
  settings: { collectorUrl: 'https://api.test/pixel', installationToken: 'installation' },
  analytics: { subscribe: (name, handler) => { subscriptions[name] = handler; } },
  customerPrivacy: { subscribe: (name, handler) => { consent = handler; } },
  browser: { sessionStorage: { getItem: async () => saved, setItem: async (key, value) => { saved = value; if (revokeOnSave) consent({customerPrivacy: {analyticsProcessingAllowed: false}}); }, removeItem: async () => { saved = null; } } },
}) });
(async () => {
  const event = { name: 'page_viewed', id: 'id1', clientId: 'client1', context: { document: { location: { href: 'https://shop.test/?heidel_touch=abcdefghijklmnopqrstuvwx' } } }, data: {} };
  const observe = subscriptions.all_standard_events;
  await observe(event);
  assert.equal(sends.length, 0);
  consent({ customerPrivacy: { analyticsProcessingAllowed: true } });
  await observe(event);
  assert.equal(sends.length, 1);
  assert.equal(sends[0].touch_token, 'abcdefghijklmnopqrstuvwx');
  assert.equal(saved, sends[0].touch_token);
  await observe({ ...event, name: 'product_added_to_cart', data: { cartLine: { merchandise: { id: 'gid://shopify/ProductVariant/123' } } } });
  assert.equal(sends[1].variant_id, 'gid://shopify/ProductVariant/123');
  await observe({ ...event, name: 'unrelated_event' });
  assert.equal(sends.length, 2);
  // Consent can be revoked while sandbox storage is still awaiting a response.
  revokeOnSave = true;
  await observe(event);
  assert.equal(sends.length, 2);
  assert.equal(saved, null);
  consent({ customerPrivacy: { analyticsProcessingAllowed: false } });
  await observe(event);
  assert.equal(sends.length, 2);
  assert.equal(saved, null);
  assert.ok(!source.includes('customer_id'));
  console.log('SHOPIFY_OWNER_PIXEL_OK: consent, revocation, touch persistence, allowed events and variant validation');
})();
