// node tests/shopify/test_storefront_capture.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const path = require('node:path');

async function main() {
  const events = {};
  const changes = {};
  const storage = new Map();
  const posted = [];
  let poll;
  let failRevocation = true;
  let quantity = 1;
  const root = { dataset: { customer: '123', proxy: '/apps/heidelai/cart' } };
  const consent = { checked: false, addEventListener: (event, fn) => { changes[event] = fn; } };
  const status = { textContent: '' };
  const context = {
    document: { hidden: false,
      getElementById: id => ({ 'heidel-cart-recovery': root, 'heidel-cart-consent': consent, 'heidel-cart-status': status }[id]),
      addEventListener: (event, fn) => { events[event] = fn; },
    },
    location: { origin: 'https://demo.myshopify.com' }, Shopify: { routes: { root: '/' } },
    sessionStorage: { getItem: key => storage.get(key), setItem: (key, val) => storage.set(key, val), removeItem: key => storage.delete(key) },
    crypto: webcrypto, TextEncoder, URL, Request, Uint8Array, Promise, AbortController,
    setInterval: fn => { poll = fn; return 1; }, clearInterval: () => {},
    setTimeout: (fn, delay) => delay <= 100 ? setTimeout(fn, 0) : undefined, clearTimeout,
    XMLHttpRequest: function () {}, HTMLFormElement: function () {},
    addEventListener: () => {},
    fetch: async (url, options) => {
      if (url === '/cart.js') return { ok: true, json: async () => ({ token: 'cart_token_123456', items: [{ variant_id: 10, quantity }] }) };
      if (String(url).includes('/cart/add.js')) return { ok: true };
      assert.equal(options.headers['X-Heidel-Cart'], '1');
      const body = JSON.parse(options.body);
      posted.push(body);
      assert.equal(new URL(url, 'https://demo.myshopify.com').searchParams.get('body_sha256').length, 64);
      if (body.event === 'consent_revoked' && failRevocation) throw new Error('network unavailable');
      return { ok: true, json: async () => ({ status: body.event === 'consent_revoked' ? 'opted_out' : 'pending' }) };
    },
  };
  context.XMLHttpRequest.prototype.open = () => {};
  context.window = context;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../storefront/shopify/heidel-cart-recovery.js'), 'utf8'), context);
  const flush = async () => { for (let i = 0; i < 30; i++) await new Promise(resolve => setImmediate(resolve)); };
  await flush();
  assert.equal(posted.length, 0, 'No automatic opt-in');
  consent.checked = true;
  changes.change();
  await flush();
  assert.equal(posted[0].event, 'consent_granted');
  quantity = 2;
  await context.fetch('/cart/add.js');
  await new Promise(resolve => setTimeout(resolve, 10));
  await flush();
  assert.equal(posted.at(-1).items[0].quantity, 2, 'Cart mutation captured before polling');
  consent.checked = false;
  changes.change();
  await flush();
  assert.equal(posted.at(-1).event, 'consent_revoked');
  assert.equal(storage.get('heidel-whatsapp-cart-consent:123:pending'), 'consent_revoked');
  failRevocation = false;
  poll();
  await flush();
  assert.equal(posted.filter(item => item.event === 'consent_revoked').length, 2, 'Unchecked consent still retries failed revocation');
  assert.equal(storage.has('heidel-whatsapp-cart-consent:123:pending'), false, 'Only clear after acknowledgement');
  console.log('Storefront JS: explicit consent, immediate mutation capture, signed body hash, durable revocation retry passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
