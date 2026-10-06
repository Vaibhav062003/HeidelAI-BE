import {register} from '@shopify/web-pixels-extension';

register(({analytics, browser, settings, customerPrivacy, init}) => {
  const names = new Set(['page_viewed', 'product_viewed', 'product_added_to_cart', 'cart_viewed', 'checkout_started', 'checkout_completed']);
  let touch = null;
  let analyticsAllowed = Boolean(init.customerPrivacy.analyticsProcessingAllowed);
  const loadTouch = browser.sessionStorage.getItem('heidel_touch').then(value => { touch = value; });
  customerPrivacy.subscribe('visitorConsentCollected', event => {
    analyticsAllowed = Boolean(event.customerPrivacy.analyticsProcessingAllowed);
    if (!analyticsAllowed) {
      touch = null;
      void browser.sessionStorage.removeItem('heidel_touch');
    }
  });
  analytics.subscribe('all_standard_events', async event => {
    if (!names.has(event.name) || !analyticsAllowed) return;
    await loadTouch;
    if (!analyticsAllowed) return;
    const url = new URL(event.context.document.location.href);
    const selected = url.searchParams.get('heidel_touch');
    if (selected && /^[A-Za-z0-9_-]{20,100}$/.test(selected)) {
      touch = selected;
      await browser.sessionStorage.setItem('heidel_touch', selected);
    }
    if (!analyticsAllowed) {
      touch = null;
      await browser.sessionStorage.removeItem('heidel_touch');
      return;
    }
    const variant = event.data?.productVariant?.id || event.data?.cartLine?.merchandise?.id;
    await fetch(settings.collectorUrl, {
      method: 'POST', headers: {'Content-Type': 'text/plain'}, keepalive: true,
      body: JSON.stringify({installation_token: settings.installationToken, event_id: event.id,
        client_id: event.clientId, event_name: event.name, touch_token: touch,
        variant_id: /^gid:\/\/shopify\/ProductVariant\/\d+$/.test(variant || '') ? variant : null}),
    }).catch(() => {});
  });
});
