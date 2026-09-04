"""Run: python3 tests/shopify/test_storefront_cart.py (no network/dependencies)."""
import asyncio
import copy
import hashlib
import hmac
import importlib.util
import json
import sys
import types
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

from test_shopify_worker_runtime import FakeCollection, FakeDB, HTTPException, NOW, load_service

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
s = load_service()
sys.modules['services.shopify_service'] = s
spec = importlib.util.spec_from_file_location('services.shopify_storefront_cart', ROOT / 'services/shopify_storefront_cart.py')
c = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = c
spec.loader.exec_module(c)


class Collection(FakeCollection):
    async def insert_one(self, document):
        document = copy.deepcopy(document)
        document.setdefault('_id', str(len(self.documents) + 1))
        if any(row.get('provider_id') == document.get('provider_id') for row in self.documents):
            raise s.DuplicateKeyError('duplicate')
        self.documents.append(document)
        return types.SimpleNamespace(inserted_id=document['_id'])


def signed(raw, *, customer='123', timestamp=None):
    values = {'shop': 'demo.myshopify.com', 'logged_in_customer_id': customer,
              'timestamp': str(int((timestamp or s.utcnow()).timestamp())),
              'path_prefix': '/apps/heidelai', 'body_sha256': hashlib.sha256(raw).hexdigest()}
    values['signature'] = hmac.new(b'secret', ''.join(sorted(f'{k}={v}' for k, v in values.items())).encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


async def post(*, event='updated', token='cart_token_123456', consent=True, items=None, customer='123'):
    raw = json.dumps({'token': token, 'event': event, 'consent': consent,
                      'items': [{'id': '10', 'quantity': 1}] if items is None else items}).encode()
    return await c.capture_cart(signed(raw, customer=customer), raw)


async def run():
    clock = [NOW]
    s.utcnow = lambda: clock[0]
    connection = {'org_id': 'org', 'shop': 'demo.myshopify.com', 'status': 'connected',
                  'automation_enabled': True, 'automation_enabled_at': NOW - timedelta(days=1),
                  'reminder_delay_minutes': 30}
    s.db = FakeDB(shopify_connections=Collection([connection]), shopify_checkouts=Collection())
    customer_phone = ['+14155550100']
    async def graphql(conn, query, variables, **kwargs):
        assert conn['org_id'] == 'org'
        return {'customer': {'id': variables['customer'], 'firstName': 'Sam',
                             'defaultPhoneNumber': {'phoneNumber': customer_phone[0]}},
                'nodes': [{'id': ident, 'title': 'Blue', 'product': {'title': 'Shirt', 'status': 'ACTIVE'}} for ident in variables['variants']]}
    async def false(*args, **kwargs): return False
    async def true(*args, **kwargs): return True
    async def noop(*args, **kwargs): pass
    s._graphql = graphql
    s._is_whatsapp_suppressed = false
    s._configuration_ready = true
    s._commerce_event = noop

    raw = b'{}'
    assert c.authenticated_customer(signed(raw), raw, 'secret', NOW.timestamp()) == ('demo.myshopify.com', 'gid://shopify/Customer/123')
    for query, body in [(signed(raw), b'{"x":1}'), (signed(raw, customer=''), raw),
                        (signed(raw, timestamp=NOW-timedelta(minutes=3)), raw), (signed(raw)+'&shop=other.myshopify.com', raw)]:
        try:
            c.authenticated_customer(query, body, 'secret', NOW.timestamp())
            raise AssertionError('forged/expired/anonymous request accepted')
        except HTTPException as exc: assert exc.status_code == 401
    for item in [{'id': '10', 'quantity': True}, {'id': 'evil/url', 'quantity': 1}, {'id': '10', 'quantity': 101}]:
        try:
            c.cart_payload(json.dumps({'token': 'cart_123456', 'items': [item]}).encode())
            raise AssertionError('invalid item accepted')
        except HTTPException as exc: assert exc.status_code == 422
    assert (await post())['status'] == 'consent_required'
    for event in [[], {}, 1, None]:
        try:
            c.cart_payload(json.dumps({'token': 'cart_123456', 'event': event}).encode())
            raise AssertionError('invalid event accepted')
        except HTTPException as exc: assert exc.status_code == 422
    assert (await post(event='consent_granted'))['status'] == 'pending'
    doc = s.db.shopify_checkouts.documents[0]
    assert doc['phone'] == customer_phone[0]
    assert doc['products'][0]['name'] == 'Shirt - Blue'
    assert doc['reminder_due_at'] == NOW + timedelta(minutes=5), 'pre-checkout must ignore 30m checkout delay'
    assert doc['recovery_url'] == 'https://demo.myshopify.com/cart/10:1?storefront=true'
    first_time = doc['provider_updated_at']
    assert (await post(event='consent_granted'))['status'] == 'pending'
    assert len(s.db.shopify_checkouts.documents) == 1 and doc['provider_updated_at'] == first_time
    clock[0] += timedelta(seconds=15)
    await post(items=[{'id': '10', 'quantity': 2}])
    assert doc['reminder_due_at'] == clock[0] + timedelta(minutes=5)
    changed = doc['provider_updated_at']
    clock[0] += timedelta(seconds=15)
    await post(items=[{'id': '10', 'quantity': 2}])
    assert doc['provider_updated_at'] == changed, 'polling reset timer'
    await post(event='consent_revoked', consent=False)
    assert doc['reminder_status'] == 'opted_out'
    assert (await post(token='other_cart_1234'))['status'] == 'consent_required', 'stale tab silently renewed consent'
    clock[0] += timedelta(seconds=15)
    await post(event='consent_granted')
    await post(event='checkout_started')
    assert doc['storefront_checkout_started'] and doc['reminder_status'] == 'cancelled'
    assert (await post())['status'] == 'already_processed', 'checkout cart was rearmed'

    clock[0] += timedelta(seconds=15)
    await post(token='second_cart_1234')
    second = s.db.shopify_checkouts.documents[-1]
    await post(token='second_cart_1234', items=[])
    assert second['record_status'] == 'deleted' and second['reminder_due_at'] is None
    clock[0] += timedelta(seconds=15)
    await post(token='second_cart_1234')
    clock[0] += timedelta(minutes=6)
    second.update({'reminder_status': 'sending', 'claim_token': 'lease'})
    current = await c.refresh_cart_before_send(copy.deepcopy(second), 'lease', connection)
    assert current['phone'] == '+14155550100'
    customer_phone[0] = '+14155550199'
    second['phone'] = customer_phone[0]  # Simulate customers/update webhook.
    assert await c.refresh_cart_before_send(copy.deepcopy(second), 'lease', connection) is None
    assert second['failure_category'] == 'customer_phone_changed'

    # Shopify checkout webhook retires a storefront cart even if browser checkout
    # navigation could not be captured; use delete event to avoid unrelated parser.
    second.update({'record_status': 'abandoned', 'reminder_status': 'pending'})
    await s._apply_webhook(connection, 'checkouts/delete', {'id': 55, 'cart_token': 'second_cart_1234'}, clock[0])
    assert second['storefront_checkout_started'] and second['reminder_status'] == 'cancelled'
    second.update({'record_status': 'abandoned', 'reminder_status': 'pending'})
    await s._mark_order_conversion('org', {'provider_id': 'order_1', 'cart_token': 'second_cart_1234?key=private'})
    assert second['record_status'] == 'converted', 'Key-suffixed order token failed to suppress cart'
    s.db.shopify_connections.documents[0]['automation_enabled'] = False
    assert (await post(event='consent_revoked', consent=False))['status'] == 'opted_out'
    preference = await s.db.shopify_storefront_consents.find_one({'_id': 'org:gid://shopify/Customer/123'})
    assert not preference['granted'], 'Revocation must work while automation is disabled'
    print('Storefront cart: signature, body binding, consent, five-minute clock, dedupe, empty-cart, checkout cancellation, phone revalidation passed')


if __name__ == '__main__':
    asyncio.run(run())
