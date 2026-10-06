"""HTTP → Shopify workers → real Mongo → receipts → reports, with simulated providers.
Run with the backend environment: python -B tests/shopify/test_owner_e2e.py
Only a randomly named shopify_e2e_* database is created and removed. No production config is loaded.
"""
import ast
import asyncio
import hashlib
import hmac
import importlib.util
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace, ModuleType
from typing import Annotated

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(Path(__file__).parent)]
import httpx
import fastapi
import pymongo
from fastapi import Depends, Request, Header, BackgroundTasks, HTTPException
from pymongo.errors import ConnectionFailure
from test_shopify_worker_runtime import load_service, checkout, _configured_send_db, FakeCollection
from services import shopify_growth as growth, shopify_retargeting as retarget, shopify_reporting as reporting


async def noop(*args, **kwargs): pass


def function_from_file(path, name, namespace):
    node = next(node for node in ast.parse(path.read_text()).body if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace[name]


async def setup():
    saved = dict(sys.modules)
    s = load_service()
    for name in list(sys.modules):
        if name in saved: sys.modules[name] = saved[name]
        elif name.startswith(('config', 'database', 'schemas', 'cryptography')): del sys.modules[name]
    sys.modules['services.shopify_service'] = s
    s.DuplicateKeyError = pymongo.errors.DuplicateKeyError
    s.PyMongoError = pymongo.errors.PyMongoError
    s.HTTPException = HTTPException
    s.ReturnDocument = pymongo.ReturnDocument
    name = 'shopify_e2e_' + uuid.uuid4().hex
    uri = os.environ.get('SHOPIFY_TEST_MONGO_URI', 'mongodb://%2Fprivate%2Ftmp%2Fmongodb-27017.sock')
    mongo = pymongo.AsyncMongoClient(uri, serverSelectionTimeoutMS=3000, tz_aware=True)
    sync_mongo = pymongo.MongoClient(uri, serverSelectionTimeoutMS=3000, tz_aware=True)
    s.db, s.sync_db = mongo[name], sync_mongo[name]
    await mongo.admin.command('ping')
    await s.ensure_shopify_indexes()
    growth.service = lambda: s
    clock = [datetime.now(timezone.utc).replace(microsecond=0)]
    s.utcnow = lambda: clock[0]
    posts = []
    nodes = {}
    class Provider:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            posts.append(kwargs['json'])
            # Meta can report delivery before its message ID is saved locally.
            await s.update_shopify_reminder_delivery('org', 'wamid.'+str(len(posts)), 'delivered', provider_timestamp=str(int(clock[0].timestamp())))
            return SimpleNamespace(status_code=200, headers={'content-type': 'application/json'}, json=lambda: {'messages': [{'id': 'wamid.' + str(len(posts))}]})
    provider = SimpleNamespace(AsyncClient=Provider, RequestError=httpx.RequestError)
    s.httpx = provider
    retarget.httpx = provider
    s._commerce_event = noop
    async def graphql(connection, query, variables, **kwargs):
        if 'GrowthCustomer' in query:
            phone = next(node['customer']['defaultPhoneNumber']['phoneNumber'] for node in nodes.values() if node['customer']['id'] == variables['id'])
            return {'customer': {'id': variables['id'], 'numberOfOrders': 1, 'defaultPhoneNumber': {'phoneNumber': phone}, 'lastOrder': {'createdAt': (clock[0]-timedelta(days=30)).isoformat()}}}
        if 'ReportShop' in query: return {'shop': {'ianaTimezone': 'Asia/Kolkata'}}
        return {'node': nodes[variables['id']]}
    s._graphql = graphql
    async def templates(*args, **kwargs): return [{'name': 'cart', 'language': 'en', 'status': 'APPROVED', 'category': 'MARKETING'}]
    s.list_compatible_templates = templates
    async def conversation(**kwargs): return 'whatsapp_' + kwargs['customer_phone_no']
    async def store(**kwargs):
        await s.db['messages_' + kwargs['org_id']].update_one({'message_id': kwargs['message_id']}, {'$setOnInsert': {'platform': 'whatsapp', 'content': kwargs['content'], 'conversation_id': kwargs['conversation_id'], 'sender_id': kwargs['customer_phone_no'], 'business_phone_id': kwargs['business_phone_id'], 'status': kwargs['status'], 'role': 'agent', 'timestamp': clock[0]}}, upsert=True)
    s.create_or_update_whatsapp_conversation, s.store_whatsapp_message = conversation, store
    fixture = _configured_send_db(FakeCollection())
    for collection in ('shopify_connections', 'whatsapp_connections'):
        await s.db[collection].insert_many(fixture.collections[collection].documents)
    await growth.save_settings('org', {'attribution_days': 7, 'message_rate': {'amount': '0.10', 'currency': 'INR'}})
    # Real message status handler: replay exercises its production rank filter.
    wa = ModuleType('services.wa_service')
    wa.update_whatsapp_message_status = function_from_file(ROOT/'services/wa_service.py', 'update_whatsapp_message_status', {'asyncio': asyncio, 'db': s.sync_db, 'ReturnDocument': pymongo.ReturnDocument, 'Optional': __import__('typing').Optional, 'ConnectionFailure': ConnectionFailure, 'logger': s.logger, 'whatsapp_free_entry_expiry': lambda _: None, 'broadcast_main_ws': noop, 'serialize_mongo': lambda x: x})
    sys.modules['services.wa_service'] = wa
    # Real route validation and role checks. The test identity replaces only Clerk verification.
    from auth.models import CurrentUserModel
    dependencies = ModuleType('auth.dependencies')
    async def identity(request: Request):
        return CurrentUserModel(user_id='test', org_id=request.headers.get('x-test-org', 'org'), role=request.headers.get('x-test-role', 'admin'))
    dependencies.CurrentUser = Annotated[CurrentUserModel, Depends(identity)]
    sys.modules['auth.dependencies'] = dependencies
    limiter = ModuleType('core.limiter')
    limiter.limiter = SimpleNamespace(limit=lambda *_: lambda function: function)
    sys.modules['core.limiter'] = limiter
    spec = importlib.util.spec_from_file_location('owner_e2e_routes', ROOT/'api/commerce_growth_routes.py')
    routes = importlib.util.module_from_spec(spec); spec.loader.exec_module(routes)
    app = fastapi.FastAPI(); app.include_router(routes.router)
    # Exercise the complete receipt path from the actual Meta handler, without importing unrelated bots.
    secret = b'local-e2e-secret'
    async def signature(request, value):
        expected = 'sha256=' + hmac.new(secret, await request.body(), hashlib.sha256).hexdigest()
        if not value or not hmac.compare_digest(expected, value): raise HTTPException(403, 'Invalid signature')
    async def propagation(org, mid, state, payload, error, include_shopify):
        if include_shopify: await s.update_shopify_reminder_delivery(org, mid, state, error, payload.get('timestamp'))
    webhook = function_from_file(ROOT/'api/webhook_routes.py', 'handle_webhook', {'Request': Request, 'Header': Header, 'BackgroundTasks': BackgroundTasks, 'signature_required': signature, 'logger': s.logger, 'ENVIRONMENT': 'test', 'json': json, 'remove_expired_message_ids': lambda: None, 'asyncio': asyncio, 'resolve_active_whatsapp_connection': lambda *_: {'org_id': 'org', 'phone_id': 'phone', 'wa_id': 'waba'}, 'db': s.sync_db, 'HTTPException': HTTPException, 'ai_reply_jobs': SimpleNamespace(enabled=lambda _: False), 'update_whatsapp_message_status': wa.update_whatsapp_message_status, 'update_shopify_reminder_delivery': s.update_shopify_reminder_delivery, 'normalize_whatsapp_delivery_error': s.normalize_whatsapp_delivery_error, '_propagate_whatsapp_delivery_status': propagation, 'broadcast_main_ws': noop})
    app.post('/webhook')(webhook)
    async def add_cart(identifier, phone, customer):
        node = {'id': identifier, 'createdAt': (clock[0]-timedelta(minutes=20)).isoformat(), 'updatedAt': (clock[0]-timedelta(minutes=20)).isoformat(), 'abandonedCheckoutUrl': 'https://shop.test/checkouts/' + identifier + '/recover', 'customer': {'id': customer, 'firstName': 'Buyer', 'defaultPhoneNumber': {'phoneNumber': phone}}, 'lineItems': {'nodes': [{'id': 'line:'+identifier, 'title': 'Board', 'quantity': 1, 'variant': {'id': 'gid://shopify/ProductVariant/1'}, 'originalTotalPriceSet': {'shopMoney': {'amount': '100.10', 'currencyCode': 'INR'}}}]}}
        nodes[identifier] = node
        cart = s._checkout_from_graphql('org', 'shop.test', node, False, 10)
        cart.update(_id=identifier, whatsapp_consent='SUBSCRIBED', reminder_status='sending', claim_token=identifier, claim_expires_at=clock[0]+timedelta(minutes=5))
        await s.db.shopify_checkouts.insert_one(cart)
        await s.db.shopify_storefront_consents.insert_one({'_id': 'org:'+customer, 'org_id': 'org', 'customer_id': customer, 'granted': True, 'phone_key': phone.lstrip('+')})
        await s._send_claimed_checkout(cart, identifier)
        cart = await s.db.shopify_checkouts.find_one({'_id': identifier})
        assert cart.get('provider_message_id'), cart
        await s._reconcile_accepted(cart)
        return cart
    async def receipt(client, message, status, when, expected=200):
        payload = json.dumps({'object': 'whatsapp_business_account', 'entry': [{'id': 'waba', 'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': 'phone'}, 'statuses': [{'id': message, 'status': status, 'timestamp': str(int(when.timestamp()))}]}}]}]}).encode()
        result = await client.post('/webhook', content=payload, headers={'x-hub-signature-256': 'sha256='+hmac.new(secret, payload, hashlib.sha256).hexdigest()})
        assert result.status_code == expected, result.text
    async def close():
        assert name.startswith('shopify_e2e_')
        await mongo.drop_database(name)
        await mongo.close(); sync_mongo.close()
    return SimpleNamespace(s=s, app=app, clock=clock, posts=posts, add_cart=add_cart, receipt=receipt, close=close, routes=routes)


async def main():
    run = await setup()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=run.app), base_url='http://test') as client:
            assert (await client.put('/api/commerce/shopify/retargeting', json=retarget.DEFAULTS, headers={'x-test-role': 'member'})).status_code == 403
            assert (await client.get('/api/commerce/shopify/reports', headers={'x-heidel-organization': 'another'})).status_code == 409
            config = {**retarget.DEFAULTS, 'enabled': True, 'read_template_name': 'cart', 'read_template_language': 'en', 'unconfirmed_template_name': 'cart', 'unconfirmed_template_language': 'en'}
            assert (await client.put('/api/commerce/shopify/retargeting', json=config)).status_code == 200
            assert (await client.put('/api/commerce/shopify/retargeting', json={**config, 'read_delay_minutes': 0})).status_code == 422
            cart = await run.add_cart('read-cart', '+14155550100', 'customer-read')
            original = cart['provider_message_id']
            assert (await client.post('/webhook', json={})).status_code == 403
            persist = retarget.record_receipt
            async def storage_unavailable(*args, **kwargs): raise ConnectionError('Receipt storage unavailable')
            retarget.record_receipt = storage_unavailable
            try: await run.receipt(client, original, 'read', run.clock[0], expected=503)
            finally: retarget.record_receipt = persist
            await run.receipt(client, original, 'delivered', run.clock[0])
            await run.receipt(client, original, 'read', run.clock[0])
            await run.receipt(client, original, 'read', run.clock[0])
            assert await run.s.db.commerce_delivery_receipts.count_documents({'message_id': original}) == 2
            run.clock[0] += timedelta(minutes=4)
            await retarget.process_retargeting(); assert len(run.posts) == 1
            run.clock[0] += timedelta(minutes=1)
            await asyncio.gather(*(retarget.process_retargeting() for _ in range(4)))
            assert len(run.posts) == 2
            follow = await run.s.db.commerce_retargeting_jobs.find_one({'checkout_id': 'read-cart'})
            assert follow['branch'] == 'read' and follow['status'] == 'accepted' and follow['inbox_sync_status'] == 'complete'
            assert (await run.s.db.shopify_checkouts.find_one({'_id': 'read-cart'}))['provider_message_id'] == original
            await run.receipt(client, follow['provider_message_id'], 'read', run.clock[0])
            await retarget.process_retargeting()
            assert (await run.s.db.messages_org.find_one({'message_id': follow['provider_message_id']}))['status'] == 'read'
            run.clock[0] += timedelta(minutes=1)
            order = {'org_id': 'org', '_id': 'order', 'provider_id': 'order', 'customer_id': 'customer-read', 'checkout_token': 'read-cart', 'provider_created_at': run.clock[0], 'provider_updated_at': run.clock[0], 'record_status': 'active', 'payment_status': 'PARTIALLY_REFUNDED', 'net_payment': {'amount': '80.10', 'currency': 'INR'}, 'total': {'amount': '100.10', 'currency': 'INR'}, 'order_number': '#1001', 'products': []}
            await run.s.db.shopify_orders.insert_one(order)
            await run.s._mark_order_conversion('org', order)
            data = (await client.get('/api/commerce/shopify/reports')).json()
            assert data['summary']['net_payments'] == [{'currency': 'INR', 'amount': '80.10'}], data
            assert data['summary']['matched_orders'] == 1 and data['orders'][0]['kind'] == 'retargeting'
            assert (await client.get('/api/commerce/shopify/reports/export')).status_code == 200
            assert (await client.get('/api/commerce/shopify/reports/export', headers={'x-test-role': 'member'})).status_code == 403
            assert (await client.get('/api/commerce/shopify/weekly/missing')).status_code == 404
            # Delivery without read: no submission before the full thirty minutes.
            cart2 = await run.add_cart('delivered-cart', '+14155550101', 'customer-delivered')
            await run.receipt(client, cart2['provider_message_id'], 'delivered', run.clock[0])
            run.clock[0] += timedelta(minutes=29); await retarget.process_retargeting(); assert len(run.posts) == 3
            run.clock[0] += timedelta(minutes=1); await retarget.process_retargeting(); assert len(run.posts) == 4
            assert (await run.s.db.commerce_retargeting_jobs.find_one({'checkout_id': 'delivered-cart'}))['branch'] == 'unconfirmed'
            # Replies, STOP, purchases and explicit merchant cancellation each stop a new journey.
            for index, reason in enumerate(('reply', 'stop', 'purchase', 'cancel', 'pause')):
                cart = await run.add_cart(reason, '+141555501'+str(index+2).zfill(2), 'customer-'+reason)
                await run.receipt(client, cart['provider_message_id'], 'delivered', run.clock[0])
                count = len(run.posts)
                job = await run.s.db.commerce_retargeting_jobs.find_one({'checkout_id': reason})
                if reason == 'reply': await run.s.db.messages_org.insert_one({'sender_id': cart['phone'].lstrip('+'), 'role': 'customer', 'business_phone_id': 'phone', 'timestamp': run.clock[0], 'content': 'Help me'})
                elif reason == 'stop': await run.s.db.whatsapp_suppressions.insert_one({'org_id': 'org', 'phone': cart['phone'].lstrip('+'), 'reason': 'customer_opt_out'})
                elif reason == 'purchase':
                    bought = {**order, '_id': 'bought', 'provider_id': 'bought', 'checkout_token': reason, 'customer_id': cart['customer_id']}
                    await run.s.db.shopify_orders.insert_one(bought); await run.s._mark_order_conversion('org', bought)
                elif reason == 'cancel': assert (await client.delete('/api/commerce/shopify/retargeting/'+job['_id'])).status_code == 200
                else: assert (await client.put('/api/commerce/shopify/retargeting', json={**config, 'enabled': False})).status_code == 200
                run.clock[0] += timedelta(minutes=31); await retarget.process_retargeting()
                assert len(run.posts) == count, reason
                assert (await run.s.db.commerce_retargeting_jobs.find_one({'_id': job['_id']}))['status'] == 'cancelled', reason
            # Real partial unique index and concurrent idempotency: one active experiment.
            body = {'name': 'Recovery holdout', 'holdout_percent': 10, 'request_id': 'same-request-123456789'}
            outcomes = await asyncio.gather(*(client.post('/api/commerce/shopify/experiments', json=body) for _ in range(3)))
            assert all(item.status_code == 201 for item in outcomes), [item.text for item in outcomes]
            assert len({item.json()['experiment_id'] for item in outcomes}) == 1
            assert (await client.post('/api/commerce/shopify/experiments', json={**body, 'request_id': 'another-request-123456'})).status_code == 409
            await retarget.process_retargeting(); assert len(run.posts) == 9
            print('SHOPIFY_OWNER_E2E_OK: real Mongo indexes, real HTTP routes and receipt handler, original send, both follow-ups, duplicate workers, Inbox, order/refund attribution, exports, replies/STOP/purchase/cancel/pause, experiment retries and tenant/admin fences; providers and identity simulated')
    finally:
        await run.close()


if __name__ == '__main__': asyncio.run(main())
