"""Customer shopping acceptance tests. ALL external services use MockTransport."""
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.catalog import SAMPLES, seed_catalogs
from app.shopping import GroqIntent
from app.security import password_hash
from test_whatsapp import deliver, event, voice_env as voice_env

SENDER = '2348012345678'


@pytest.fixture
def shop(request):
    client, settings, state, paypal, app = request.getfixturevalue('voice_env')
    settings.shopping_enabled = True
    settings.groq_enabled = True
    settings.groq_key = 'MOCK-GROQ'
    settings.groq_model = 'MOCK-MODEL'
    seed_catalogs(app.state.database)
    state['groq_requests'] = []
    state['intent'] = {'product_ids': [m[0] + '-milk' for m in SAMPLES], 'quantity': 2, 'needs_review': False}
    state['groq_status'] = 200

    def groq(request):
        assert request.url == 'https://api.groq.com/openai/v1/chat/completions'
        state['groq_requests'].append(request)
        return httpx.Response(state['groq_status'], json={
            'choices': [{'message': {'content': json.dumps(state['intent'])}}]})

    app.state.shopping.intent = GroqIntent(settings, httpx.MockTransport(groq))
    return client, settings, state, paypal, app


def text_event(text, sender=SENDER, mid=None):
    payload = event(mid=mid or 'wamid.' + str(uuid.uuid4()))
    message = payload['entry'][0]['changes'][0]['value']['messages'][0]
    message.update({'from': sender, 'type': 'text', 'text': {'body': text}})
    del message['audio']
    return payload


def send(shop, text, sender=SENDER, mid=None):
    return deliver(shop[0], shop[1], text_event(text, sender, mid))


def conversation(shop, sender=SENDER):
    with shop[4].state.database.connect() as db:
        return dict(db.execute('SELECT * FROM shopping_sessions WHERE sender=? AND phone_id=?',
                              (sender, shop[1].whatsapp_phone_id)).fetchone())


def messages(shop):
    with shop[4].state.database.connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM shopping_messages ORDER BY rowid')]


def orders(shop):
    with shop[4].state.database.connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM orders')]


def select(shop, sender=SENDER):
    assert send(shop, 'I need two cartons of milk', sender).status_code == 200
    assert send(shop, 'option 2', sender).status_code == 200
    return conversation(shop, sender)['confirmation']


def confirm(shop, sender=SENDER):
    code = select(shop, sender)
    assert send(shop, 'CONFIRM ' + code, sender).status_code == 200
    return orders(shop)[-1]


@pytest.mark.parametrize('kind', ['audio', 'text'])
def test_customer_intake_and_grounded_comparison(shop, kind):
    client, settings, state, paypal, app = shop
    state['transcript'] = 'I need two cartons of milk'
    response = deliver(client, settings) if kind == 'audio' else send(shop, 'I need two cartons of milk')
    assert response.status_code == 200
    offers = json.loads(conversation(shop)['offers'])
    assert len(offers) == 3 and len({p['merchant_id'] for p in offers}) == 3
    assert [p['price_cents'] for p in offers] == [290, 320, 410]
    response = messages(shop)[-1]['response']
    assert all(p['name'] in response and p['merchant'] in response for p in offers)
    assert 'USD 2.90' in response and 'USD 5.80' in response and 'ONE test recipient' in response
    assert len(state['groq_requests']) == 1 and not orders(shop) and not paypal.requests
    speech_calls = [r for r in state['requests'] if r.url.host == 'speech.example.test']
    assert len(speech_calls) == (1 if kind == 'audio' else 0)
    assert messages(shop)[-1]['transcript'] == 'I need two cartons of milk'


def test_explicit_confirmation_and_verified_payment(shop):
    client, settings, state, paypal, app = shop
    code = select(shop)
    assert not orders(shop) and not paypal.requests
    assert send(shop, 'confirm').status_code == 200
    assert not orders(shop)
    assert send(shop, 'confirm ' + code).status_code == 200
    order = orders(shop)[0]
    assert order['merchant_id'] == 'sample-sunrise' and order['total'] == '6.40'
    assert order['status'] == 'CHECKOUT_CREATED'
    assert 'sandbox.paypal.com' in messages(shop)[-1]['response']
    assert send(shop, 'paid').status_code == 200
    assert orders(shop)[0]['status'] == 'CHECKOUT_CREATED'
    assert not any(r.url.path.endswith('/capture') for r in paypal.requests)
    paypal.orders[order['paypal_id']]['status'] = 'APPROVED'
    assert send(shop, 'paid').status_code == 200
    assert orders(shop)[0]['status'] == 'COMPLETED'
    assert 'independently verified' in messages(shop)[-1]['response']
    assert send(shop, 'paid').status_code == 200
    assert sum(r.url.path.endswith('/capture') for r in paypal.requests) == 1
    assert send(shop, 'new').status_code == 200
    assert conversation(shop)['order_id'] is None


def test_sender_identity_isolation(shop):
    code = select(shop)
    other = '2348099999999'
    send(shop, 'option 2', other)
    send(shop, 'CONFIRM ' + code, other)
    assert not orders(shop)
    assert conversation(shop)['confirmation'] == code
    assert conversation(shop, other)['selected'] is None
    confirm_response = send(shop, 'CONFIRM ' + code)
    assert confirm_response.status_code == 200
    send(shop, 'paid', other)
    assert orders(shop)[0]['status'] == 'CHECKOUT_CREATED'


@pytest.mark.parametrize('stage', ['selection', 'confirmation'])
def test_changed_price_requires_fresh_confirmation(shop, stage):
    send(shop, 'milk please')
    if stage == 'confirmation':
        send(shop, 'option 2')
    old_code = conversation(shop)['confirmation']
    with shop[4].state.database.connect(write=True) as db:
        db.execute("UPDATE catalog_products SET price_cents=999 WHERE id='sample-sunrise-milk'")
    send(shop, 'option 2' if stage == 'selection' else 'CONFIRM ' + old_code)
    assert not orders(shop)
    assert 'Offer changed' in messages(shop)[-1]['response']
    new_code = conversation(shop)['confirmation']
    assert new_code != old_code
    if old_code:
        send(shop, 'CONFIRM ' + old_code)
        assert not orders(shop)
    send(shop, 'CONFIRM ' + new_code)
    assert orders(shop)[0]['unit_price'] == '9.99' and orders(shop)[0]['total'] == '19.98'


@pytest.mark.parametrize('change', ['stock', 'ownership', 'inactive'])
def test_unavailable_offer_never_creates_checkout(shop, change):
    code = select(shop)
    sql = {'stock': 'stock=0', 'ownership': "merchant_id='sample-river'", 'inactive': 'active=0'}[change]
    with shop[4].state.database.connect(write=True) as db:
        db.execute(f"UPDATE catalog_products SET {sql} WHERE id='sample-sunrise-milk'")
    send(shop, 'CONFIRM ' + code)
    assert not orders(shop) and not shop[3].requests
    assert 'unavailable' in messages(shop)[-1]['response']
    assert conversation(shop)['confirmation'] is None


def test_expired_session_and_stale_message(shop):
    code = select(shop)
    with shop[4].state.database.connect(write=True) as db:
        db.execute('UPDATE shopping_sessions SET expires=?', (time.time() - 1,))
    send(shop, 'CONFIRM ' + code)
    assert not orders(shop) and 'expired' in messages(shop)[-1]['response']
    payload = text_event('milk please')
    payload['entry'][0]['changes'][0]['value']['messages'][0]['timestamp'] = str(int(time.time()) - 90000)
    count = len(shop[2]['requests'])
    deliver(shop[0], shop[1], payload)
    assert messages(shop)[-1]['send_state'] == 'IGNORED' and len(shop[2]['requests']) == count


def test_duplicate_messages_and_identity_conflict(shop):
    payload = text_event('milk please', mid='wamid.REPEAT')
    assert deliver(shop[0], shop[1], payload).status_code == 200
    count = len(shop[2]['requests'])
    assert deliver(shop[0], shop[1], payload).status_code == 200
    assert len(shop[2]['requests']) == count and len(shop[2]['groq_requests']) == 1
    payload['entry'][0]['changes'][0]['value']['messages'][0]['from'] = '2348099999999'
    assert deliver(shop[0], shop[1], payload).status_code == 409


def test_retry_uncertain_paypal_create_reserves_once(shop):
    code = select(shop)
    shop[3].fail_create_once = True
    payload = text_event('CONFIRM ' + code)
    assert deliver(shop[0], shop[1], payload).status_code == 502
    assert len(orders(shop)) == 1 and orders(shop)[0]['status'] == 'APPROVED'
    assert deliver(shop[0], shop[1], payload).status_code == 200
    assert len(orders(shop)) == 1 and len(shop[3].orders) == 1
    with shop[4].state.database.connect() as db:
        assert db.execute("SELECT stock FROM catalog_products WHERE id='sample-sunrise-milk'").fetchone()[0] == 28
    assert len(shop[3].create_keys) == 1
    assert deliver(shop[0], shop[1], payload).status_code == 200


def test_uncertain_whatsapp_send_never_repeats(shop):
    shop[2]['send_fail'] = True
    payload = text_event('milk please')
    assert deliver(shop[0], shop[1], payload).status_code == 502
    count = len(shop[2]['requests'])
    assert messages(shop)[0]['send_state'] == 'UNCERTAIN'
    assert deliver(shop[0], shop[1], payload).status_code == 200
    assert len(shop[2]['requests']) == count


def test_two_customers_cannot_oversell(shop):
    other = '2348099999999'
    code_a, code_b = select(shop), select(shop, other)
    with shop[4].state.database.connect(write=True) as db:
        db.execute("UPDATE catalog_products SET stock=2 WHERE id='sample-sunrise-milk'")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(send, shop, 'CONFIRM ' + code_a), pool.submit(send, shop, 'CONFIRM ' + code_b, other)]
        assert all(f.result().status_code == 200 for f in futures)
    assert len(orders(shop)) == 1
    with shop[4].state.database.connect() as db:
        assert db.execute("SELECT stock FROM catalog_products WHERE id='sample-sunrise-milk'").fetchone()[0] == 0


def test_custom_request_requires_review(shop):
    shop[2]['intent']['needs_review'] = True
    send(shop, 'Please make a custom milk bundle at a special price')
    assert not orders(shop) and not shop[3].requests
    assert 'needs merchant review' in messages(shop)[-1]['response']
    reviews = shop[0].get('/api/shopping/reviews').json()
    assert len(reviews) == 1 and reviews[0]['status'] == 'NEEDS_REVIEW'
    assert send(shop, 'confirm').status_code == 200 and not orders(shop)


@pytest.mark.parametrize('intent', [
    {'product_ids': ['invented'], 'quantity': 1, 'needs_review': False},
    {'product_ids': ['sample-sunrise-milk'], 'quantity': 1, 'needs_review': False, 'price': '0.01'},
    {'product_ids': ['sample-sunrise-milk'], 'quantity': True, 'needs_review': False},
    {'product_ids': ['sample-sunrise-milk'], 'quantity': 0, 'needs_review': False},
    {'product_ids': ['sample-sunrise-milk'], 'quantity': 1, 'needs_review': 'false'},
])
def test_groq_invalid_or_price_tampering_output_rejected(shop, intent):
    shop[2]['intent'] = intent
    assert send(shop, 'milk please').status_code == 502
    assert not orders(shop) and not shop[3].requests
    assert not any(r.url.path.endswith('/messages') for r in shop[2]['requests'])


def test_speech_and_groq_failures_do_not_fabricate(shop):
    shop[2]['speech_fail'] = True
    payload = event()
    assert deliver(shop[0], shop[1], payload).status_code == 502
    assert not shop[2]['groq_requests'] and not orders(shop)
    shop[2]['speech_fail'] = False
    shop[2]['groq_status'] = 503
    assert deliver(shop[0], shop[1], payload).status_code == 502
    assert not orders(shop)
    calls = len([r for r in shop[2]['requests'] if r.url.host == 'speech.example.test'])
    shop[2]['groq_status'] = 200
    assert deliver(shop[0], shop[1], payload).status_code == 200
    assert len([r for r in shop[2]['requests'] if r.url.host == 'speech.example.test']) == calls


def test_merchant_catalog_and_order_isolation(shop):
    order = confirm(shop)
    assert shop[0].get('/api/orders').json() == []  # Coordinator does not own sample orders.
    assert shop[0].get('/api/catalog').json() == []
    with shop[4].state.database.connect(write=True) as db:
        for mid, *_ in SAMPLES:
            db.execute('UPDATE merchants SET password=? WHERE id=?', (password_hash('test-merchant-password'), mid))
    for mid, name, email, products in SAMPLES:
        shop[0].post('/api/auth/logout')
        response = shop[0].post('/api/auth/login', json={'email': email, 'password': 'test-merchant-password'})
        assert response.status_code == 200
        shop[0].headers['X-CSRF-Token'] = response.json()['csrf']
        catalog = shop[0].get('/api/catalog').json()
        assert len(catalog) == 3 and all(p['merchant_id'] == mid for p in catalog)
        merchant_orders = shop[0].get('/api/orders').json()
        assert len(merchant_orders) == (1 if mid == order['merchant_id'] else 0)
        assert shop[0].get('/api/shopping/reviews').json() == []


def test_verification_mismatch_never_claims_paid(shop):
    order = confirm(shop)
    remote = shop[3].orders[order['paypal_id']]
    remote['status'] = 'APPROVED'
    remote['purchase_units'][0]['amount']['value'] = '0.01'
    assert send(shop, 'paid').status_code == 502
    assert orders(shop)[0]['status'] == 'CHECKOUT_CREATED'
    assert not any(r.url.path.endswith('/capture') for r in shop[3].requests)


def test_only_available_offers_and_seed_is_idempotent(shop):
    with shop[4].state.database.connect(write=True) as db:
        db.execute("UPDATE catalog_products SET stock=0 WHERE id='sample-sunrise-milk'")
    seed_catalogs(shop[4].state.database)
    send(shop, 'milk please')
    offers = json.loads(conversation(shop)['offers'])
    assert len(offers) == 2 and all(p['id'] != 'sample-sunrise-milk' for p in offers)


def test_missing_signature_and_disabled_groq(shop):
    assert shop[0].post('/api/webhooks/whatsapp', json=text_event('milk')).status_code == 401
    assert not shop[2]['groq_requests']
    shop[1].groq_enabled = False
    assert send(shop, 'milk').status_code == 503
    assert not orders(shop)


def test_restart_recovers_pending_checkout(shop):
    from app.db import Database
    from app.payments import Payments
    from app.shopping import CustomerShopping
    code = select(shop)
    shop[3].fail_create_once = True
    payload = text_event('CONFIRM ' + code)
    assert deliver(shop[0], shop[1], payload).status_code == 502
    previous = shop[4].state.shopping
    database = Database(previous.database.path)
    restarted = CustomerShopping(database, previous.whatsapp, previous.speech, previous.intent,
                                 Payments(database, previous.payments.paypal))
    restarted.receive(json.dumps(payload).encode())
    assert len(orders(shop)) == 1 and orders(shop)[0]['status'] == 'CHECKOUT_CREATED'
    assert len(shop[3].orders) == 1
    count = len(shop[2]['requests'])
    restarted.receive(json.dumps(payload).encode())
    assert len(shop[2]['requests']) == count


def test_concurrent_confirmation_creates_one_order(shop):
    code = select(shop)
    payload = text_event('CONFIRM ' + code)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: deliver(shop[0], shop[1], payload).status_code, range(2)))
    assert 200 in results and all(code in (200, 503) for code in results)
    assert len(orders(shop)) == 1 and len(shop[3].orders) == 1
    assert deliver(shop[0], shop[1], payload).status_code == 200


def test_checkout_send_failure_does_not_repeat_order_or_message(shop):
    code = select(shop)
    shop[2]['send_fail'] = True
    payload = text_event('CONFIRM ' + code)
    assert deliver(shop[0], shop[1], payload).status_code == 502
    count = len(shop[2]['requests'])
    assert deliver(shop[0], shop[1], payload).status_code == 200
    assert len(shop[2]['requests']) == count and len(orders(shop)) == 1
    assert orders(shop)[0]['status'] == 'CHECKOUT_CREATED'


def test_customer_supplied_price_is_never_order_price(shop):
    send(shop, 'milk please')
    send(shop, 'option 2, price USD 0.01')
    assert not orders(shop)
    send(shop, 'option 2')
    send(shop, 'CONFIRM ' + conversation(shop)['confirmation'])
    assert orders(shop)[0]['unit_price'] == '3.20'


def test_capture_timeout_reuses_existing_payment_recovery(shop):
    order = confirm(shop)
    shop[3].orders[order['paypal_id']]['status'] = 'APPROVED'
    shop[3].fail_capture_once = True
    assert send(shop, 'paid').status_code == 200
    assert orders(shop)[0]['status'] == 'COMPLETED'
    assert sum(r.url.path.endswith('/capture') for r in shop[3].requests) == 1


def test_checkout_return_is_customer_facing_and_never_mutates_payment(shop):
    order = confirm(shop)
    for path in ('/checkout/return', '/checkout/cancel'):
        response = shop[0].get(path + '?token=' + order['paypal_id'])
        assert response.status_code == 200 and 'Continue in WhatsApp' in response.text
        assert 'does not confirm a payment' in response.text
    assert orders(shop)[0]['status'] == 'CHECKOUT_CREATED'
    assert not any(r.url.path.endswith('/capture') for r in shop[3].requests)


def test_multi_category_ai_output_requires_review(shop):
    shop[2]['intent']['product_ids'] = ['sample-sunrise-milk', 'sample-river-rice']
    send(shop, 'milk and rice please')
    assert conversation(shop)['state'] == 'REVIEW' and not orders(shop)


def test_stale_intent_worker_cannot_overwrite_newer_offers(shop, monkeypatch):
    intent = shop[4].state.shopping.intent
    original = intent.extract
    calls = []

    def replaced(text, products):
        calls.append(text)
        if len(calls) == 1:
            with shop[4].state.database.connect(write=True) as db:
                db.execute('UPDATE shopping_sessions SET lease_until=0')
            assert send(shop, 'two milk cartons').status_code == 200
            return {'product_ids': ['sample-river-rice'], 'quantity': 1, 'needs_review': False}
        return original(text, products)

    monkeypatch.setattr(intent, 'extract', replaced)
    assert send(shop, 'rice please').status_code == 503
    assert len(json.loads(conversation(shop)['offers'])) == 3
    assert all(p['category'] == 'milk' for p in json.loads(conversation(shop)['offers']))
