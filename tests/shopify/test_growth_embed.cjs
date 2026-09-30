// node tests/shopify/test_growth_embed.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

async function main() {
  const root = path.resolve(__dirname, '../../storefront/shopify');
  const asset = fs.readFileSync(path.join(root, 'extensions/heidel-recovery/assets/heidel-shopper-recovery.js'), 'utf8');
  assert.ok(asset.startsWith(fs.readFileSync(path.join(root, 'heidel-cart-recovery.js'), 'utf8')), 'The extension must reuse the tested cart client');
  const liquid = fs.readFileSync(path.join(root, 'extensions/heidel-recovery/blocks/recovery.liquid'), 'utf8');
  const schema = JSON.parse(liquid.split('{% schema %}')[1].split('{% endschema %}')[0]);
  assert.equal(schema.target, 'body');
  assert.equal(schema.javascript, 'heidel-shopper-recovery.js');
  const events = {};
  const status = { textContent: '' };
  const button = { disabled: false };
  const controls = ['whatsapp', 'email'].map(name => ({ name, checked: false, disabled: false,
    addEventListener(event, callback) { events[`${name}:${event}`] = callback; } }));
  const form = { dataset: { proxy: '/apps/heidelai/consent' },
    querySelector: selector => selector === 'button' ? button : status,
    querySelectorAll: () => controls,
    addEventListener: (event, callback) => { events[event] = callback; } };
  const requests = [];
  const context = { document: { getElementById: id => id === 'heidel-marketing-consent' ? form : null },
    crypto: webcrypto, TextEncoder, Uint8Array, AbortController, setTimeout, clearTimeout,
    fetch: async (url, options) => {
      requests.push({ url, ...options });
      return { ok: true, json: async () => options.method === 'GET' ? {whatsapp:false,email:true} : {status:'subscribed'} };
    } };
  vm.runInNewContext(asset, context);
  for (let count = 0; count < 10 && button.disabled; count++) await new Promise(resolve => setTimeout(resolve, 1));
  assert.equal(controls[1].checked, true, 'Existing email preference was not loaded');
  controls[0].checked = true;
  events['whatsapp:change']();
  await events.submit({ preventDefault() {} });
  assert.equal(requests.length, 2, 'An untouched channel must not be changed');
  assert.deepEqual(JSON.parse(requests[1].body), {channel:'whatsapp',granted:true});
  const digest = Buffer.from(await webcrypto.subtle.digest('SHA-256', new TextEncoder().encode(requests[1].body))).toString('hex');
  assert.equal(requests[1].url, `/apps/heidelai/consent?body_sha256=${digest}`);
  assert.equal(requests[1].credentials, 'same-origin');
  assert.equal(controls[0].disabled, false);
  assert.match(status.textContent, /Preferences saved/);
  controls[0].checked = false;
  events['whatsapp:change']();
  await events.submit({ preventDefault() {} });
  assert.deepEqual(JSON.parse(requests[2].body), {channel:'whatsapp',granted:false});
  console.log('GROWTH_EMBED_OK: native app embed, existing preferences, separate explicit consent, signed bodies and revocation');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
