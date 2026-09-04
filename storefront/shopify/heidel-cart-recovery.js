/* Installed once per Shopify theme. No phone, customer ID, price or URL is posted. */
(() => {
  const root = document.getElementById('heidel-cart-recovery');
  if (!root || root.dataset.started) return;
  root.dataset.started = 'true';
  const consent = document.getElementById('heidel-cart-consent');
  const status = document.getElementById('heidel-cart-status');
  const consentKey = `heidel-whatsapp-cart-consent:${root.dataset.customer}`;
  const pendingKey = `${consentKey}:pending`;
  let pendingConsent = '';
  let lastFingerprint = '';
  let chain = Promise.resolve();
  let leaving = false;
  try {
    consent.checked = sessionStorage.getItem(consentKey) === 'yes';
    pendingConsent = sessionStorage.getItem(pendingKey) || '';
  } catch (_) {}

  async function capture(event = 'updated') {
    if (pendingConsent && (pendingConsent === 'consent_revoked' || event !== 'checkout_started')) event = pendingConsent;
    if (!consent.checked && event !== 'consent_revoked') return;
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 5000);
    try {
      const cartResponse = await fetch(`${window.Shopify?.routes?.root || '/'}cart.js`, {
        credentials: 'same-origin', cache: 'no-store', signal: abort.signal,
      });
      if (!cartResponse.ok) throw new Error('cart_unavailable');
      const cart = await cartResponse.json();
      // Selling plans and line properties cannot be restored by this permalink.
      // Fail closed rather than send a link which changes a customised purchase.
      const unsupported = (cart.items || []).some(item => item.selling_plan_allocation ||
        Object.keys(item.properties || {}).length > 0);
      const body = JSON.stringify({
        token: cart.token,
        items: unsupported ? [] : (cart.items || []).map(item => ({ id: String(item.variant_id), quantity: item.quantity })),
        event,
        consent: consent.checked,
      });
      if (body === lastFingerprint) return;
      const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(body))))
        .map(byte => byte.toString(16).padStart(2, '0')).join('');
      const response = await fetch(`${root.dataset.proxy}?body_sha256=${digest}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Heidel-Cart': '1' }, body,
        credentials: 'same-origin', signal: abort.signal, keepalive: true,
      });
      if (!response.ok) throw new Error(`capture_${response.status}`);
      const result = await response.json();
      if (result.status !== 'disabled') lastFingerprint = body;
      if (pendingConsent === event && result.status !== 'disabled') {
        pendingConsent = '';
        try { sessionStorage.removeItem(pendingKey); } catch (_) {}
      }
      const messages = {
        pending: 'Cart reminder enabled.', phone_missing: 'Add a phone number to your store account to receive reminders.',
        disabled: 'Cart reminders are not enabled for this store.', opted_out: 'Cart reminders disabled.',
        consent_required: 'Please select the checkbox to enable WhatsApp reminders.',
        already_processed: 'This cart already has a reminder or has entered checkout.',
      };
      status.textContent = unsupported ? 'Reminders are not available for customised or subscription carts.' : (messages[result.status] || '');
    } catch (_) {
      status.textContent = 'Cart reminder could not be updated. We will try again while this page is open.';
    } finally { clearTimeout(timeout); }
  }
  function sync(event) {
    chain = chain.catch(() => {}).then(() => capture(event));
    return chain;
  }
  consent.addEventListener('change', () => {
    pendingConsent = consent.checked ? 'consent_granted' : 'consent_revoked';
    try {
      sessionStorage.setItem(consentKey, consent.checked ? 'yes' : 'no');
      sessionStorage.setItem(pendingKey, pendingConsent);
    } catch (_) {}
    void sync(pendingConsent);
  });
  const timer = setInterval(() => {
    if (!document.hidden && !leaving && (consent.checked || pendingConsent)) void sync('updated');
  }, 15000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !leaving && (consent.checked || pendingConsent)) void sync('updated');
  });
  // Capture common theme cart mutations immediately; polling is a fallback for
  // custom theme events and other tabs. Preserve the original fetch/XHR result.
  const isMutation = input => {
    try {
      const url = new URL(input instanceof Request ? input.url : String(input), location.origin);
      return url.origin === location.origin && /\/cart\/(add|change|update|clear)(\.js)?$/.test(url.pathname);
    } catch (_) { return false; }
  };
  let debounce;
  const cartChanged = () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => { if (!leaving && consent.checked) void sync('updated'); }, 100);
  };
  const originalFetch = window.fetch;
  window.fetch = function (...args) {
    const result = originalFetch.apply(this, args);
    if (isMutation(args[0])) result.then(response => { if (response.ok) cartChanged(); }, () => {});
    return result;
  };
  const originalOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...args) {
    if (isMutation(url)) this.addEventListener('load', () => {
      if (this.status >= 200 && this.status < 300) cartChanged();
    }, { once: true });
    return originalOpen.call(this, method, url, ...args);
  };
  document.addEventListener('cart:updated', cartChanged);
  // Tell the server before a standard checkout navigation. Shopify checkout
  // webhooks are the independent fallback for checkout/order cancellation.
  document.addEventListener('submit', event => {
    if (!consent.checked || leaving || event.submitter?.name !== 'checkout') return;
    const form = event.target;
    event.preventDefault();
    leaving = true;
    Promise.race([sync('checkout_started'), new Promise(resolve => setTimeout(resolve, 2500))]).finally(() => {
      const field = document.createElement('input');
      field.type = 'hidden'; field.name = 'checkout'; field.value = 'Checkout'; form.appendChild(field);
      HTMLFormElement.prototype.submit.call(form);
    });
  }, true);
  document.addEventListener('click', event => {
    const link = event.target.closest?.('a[href]');
    if (!link || !consent.checked || leaving || event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
    const destination = new URL(link.href, location.origin);
    if (destination.origin !== location.origin || !/\/checkout\/?$/.test(destination.pathname)) return;
    event.preventDefault();
    leaving = true;
    Promise.race([sync('checkout_started'), new Promise(resolve => setTimeout(resolve, 2500))])
      .finally(() => location.assign(destination.href));
  }, true);
  window.addEventListener('pagehide', () => clearInterval(timer), { once: true });
  void sync('updated');
})();
