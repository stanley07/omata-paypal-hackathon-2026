"""All PayPal responses below are MOCKS. No external network calls are made."""
import copy
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import ROOT, Settings
from app.interpretation import Interpreter, deterministic, validate_details
from app.main import create_app
from app.paypal import PayPal


class SandboxMock:
    def __init__(self):
        self.requests = []
        self.orders = {}
        self.create_keys = {}
        self.fail_create_once = False
        self.fail_capture_once = False
        self.signature = 'SUCCESS'
        self.oauth_fail = False
        self.unauthorized_once = False

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        assert request.url.host == 'api-m.sandbox.paypal.com'
        if path.endswith('/token'):
            return httpx.Response(401 if self.oauth_fail else 200, json={'access_token':'MOCK-TOKEN','expires_in':3600})
        if self.unauthorized_once:
            self.unauthorized_once = False
            return httpx.Response(401, json={})
        if path.endswith('verify-webhook-signature'):
            return httpx.Response(200, json={'verification_status':self.signature})
        if path == '/v2/checkout/orders':
            key = request.headers['paypal-request-id']
            if key not in self.create_keys:
                remote_id = 'MOCK-' + str(len(self.orders) + 1)
                body = json.loads(request.content)
                self.orders[remote_id] = {'id':remote_id,'status':'CREATED','purchase_units':body['purchase_units'],
                    'links':[{'rel':'payer-action','href':'https://www.sandbox.paypal.com/checkoutnow?token='+remote_id}]}
                self.create_keys[key] = remote_id
            result = self.orders[self.create_keys[key]]
            if self.fail_create_once:
                self.fail_create_once = False
                raise httpx.ReadTimeout('MOCK timeout after creation')
            return httpx.Response(201, json=result)
        remote_id = path.split('/')[4]
        remote = self.orders[remote_id]
        if path.endswith('/capture'):
            remote['status'] = 'COMPLETED'
            remote['purchase_units'][0]['payments'] = {'captures':[{'id':'CAP-'+remote_id,'status':'COMPLETED',
                'final_capture':True,'amount':remote['purchase_units'][0]['amount']}]}
            if self.fail_capture_once:
                self.fail_capture_once = False
                raise httpx.ReadTimeout('MOCK timeout after capture')
        return httpx.Response(200, json=remote)


@pytest.fixture
def env():
    path = ROOT / 'artifacts' / 'tests' / (str(uuid.uuid4()) + '.db')
    settings = Settings(database=path, paypal_enabled=True, client_id='MOCK-ID', client_secret='MOCK-SECRET', webhook_id='MOCK-WEBHOOK', ai_provider='deterministic')
    mock = SandboxMock()
    paypal = PayPal(settings, httpx.MockTransport(mock))
    app = create_app(settings, paypal)
    with TestClient(app, base_url=settings.base_url, headers={'Origin':settings.base_url}) as client:
        yield client, app, mock, paypal


def account(client, email='ada@example.test'):
    response = client.post('/api/auth/register', json={'email':email,'password':'long-test-password!','name':'Ada Provisions'})
    assert response.status_code == 200
    client.headers['X-CSRF-Token'] = response.json()['csrf']


def draft(client):
    response = client.post('/api/orders', json={'text':'5 cartons of milk at USD 25 each for Ada'})
    assert response.status_code == 200
    return response.json()


def act(client, order, action):
    return client.post(f"/api/orders/{order['id']}/{action}")


def checkout(client):
    order = draft(client)
    assert act(client, order, 'approve').status_code == 200
    response = act(client, order, 'checkout')
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('text,total', [('Prepare a PayPal payment for 5 cartons of milk at $25 each.', '125.00'),
    ('3 bags at EUR 0.10 each for Ada','0.30'),('1 bag at GBP 20.05 each','20.05')])
def test_valid_parser(text, total):
    assert deterministic(text)['total'] == total


@pytest.mark.parametrize('text', ['milk', '5 milk at 25 each','0 milk at $5 each','2 milk at $0 each',
    '2 milk at $1.001 each','2 milk at $-2 each','2.5 milk at $4 each','10001 milk at $2 each',
    '2 milk at NGN 5 each','2 milk at $5 each and 3 bread at $2 each','1 milk at $NaN each'])
def test_invalid_parser(text):
    with pytest.raises(HTTPException):
        deterministic(text)


@pytest.mark.parametrize('price', ['NaN','Infinity','-1','0','0.001','1000001'])
def test_invalid_decimal(price):
    with pytest.raises(HTTPException):
        validate_details({'description':'Milk','quantity':'1','unit_price':price,'currency':'USD'})


def test_auth_approval_rejection(env):
    client, app, mock, paypal = env
    assert client.get('/api/orders').status_code == 401
    account(client)
    order = draft(client)
    assert act(client, order, 'checkout').status_code == 409
    assert not mock.requests
    rejected = act(client, order, 'reject').json()
    assert rejected['status'] == 'REJECTED' and rejected['approved_at'] is None
    assert act(client, order, 'approve').status_code == 409
    order = draft(client)
    result = act(client, order, 'approve').json()
    assert result['approved_at'] and result['status'] == 'APPROVED'
    assert not mock.requests
    assert act(client, order, 'approve').json()['approved_at'] == result['approved_at']
    assert any(a['event']=='MERCHANT_APPROVED' for a in client.get('/api/audit').json())


def test_tenant_isolation(env):
    client, app, mock, paypal = env
    account(client)
    order = checkout(client)
    client.post('/api/auth/logout')
    account(client, 'other@example.test')
    assert client.get('/api/orders').json() == []
    assert all(a['order_id'] is None for a in client.get('/api/audit').json())
    for action in ['approve','reject','checkout','capture','verify']:
        assert act(client, order, action).status_code == 404


def test_csrf_origin_and_logout(env):
    client, *_ = env
    account(client)
    assert client.post('/api/orders', json={'text':'x'}, headers={'Origin':'https://evil.example'}).status_code == 403
    assert client.post('/api/orders', json={'text':'x'}, headers={'X-CSRF-Token':'wrong'}).status_code == 403
    assert client.post('/api/auth/logout').status_code == 200
    assert client.get('/api/me').status_code == 401


def test_login_and_rate_limit(env):
    client, *_ = env
    account(client)
    client.post('/api/auth/logout')
    body = {'email':'ada@example.test','password':'long-test-password!'}
    assert client.post('/api/auth/login', json=body).status_code == 200
    body['password'] = 'incorrect-password'
    for _ in range(10):
        assert client.post('/api/auth/login', json=body).status_code == 401
    assert client.post('/api/auth/login', json=body).status_code == 429


def test_idempotent_create_and_capture(env):
    client, app, mock, paypal = env
    account(client)
    order = checkout(client)
    assert act(client, order, 'checkout').json()['paypal_id'] == order['paypal_id']
    assert len(mock.create_keys) == 1
    assert act(client, order, 'capture').status_code == 409
    assert client.get('/checkout/return?token='+order['paypal_id']).status_code == 200
    assert client.get('/api/orders').json()[0]['status'] == 'CHECKOUT_CREATED'
    mock.orders[order['paypal_id']]['status'] = 'APPROVED'
    paid = act(client, order, 'capture').json()
    assert paid['status'] == 'COMPLETED' and paid['verified_at'] and paid['capture_id']
    assert act(client, order, 'capture').json()['capture_id'] == paid['capture_id']
    assert sum(r.url.path.endswith('/capture') for r in mock.requests) == 1
    assert sum(r.url.path.endswith('/token') for r in mock.requests) == 1


def test_create_timeout_recovery(env):
    client, app, mock, paypal = env
    account(client)
    order = draft(client)
    act(client, order, 'approve')
    mock.fail_create_once = True
    assert act(client, order, 'checkout').status_code == 502
    assert client.get('/api/orders').json()[0]['status'] == 'APPROVED'
    assert act(client, order, 'checkout').status_code == 200
    requests = [r for r in mock.requests if r.url.path == '/v2/checkout/orders']
    assert len(requests) == 2 and requests[0].headers['paypal-request-id'] == requests[1].headers['paypal-request-id']
    assert len(mock.orders) == 1


def test_capture_timeout_recovery(env):
    client, app, mock, paypal = env
    account(client)
    order = checkout(client)
    mock.orders[order['paypal_id']]['status'] = 'APPROVED'
    mock.fail_capture_once = True
    assert act(client, order, 'capture').json()['status'] == 'COMPLETED'
    assert act(client, order, 'capture').json()['status'] == 'COMPLETED'
    assert sum(r.url.path.endswith('/capture') for r in mock.requests) == 1


def test_disabled_and_oauth_errors(env):
    client, app, mock, paypal = env
    account(client)
    order = draft(client)
    act(client, order, 'approve')
    paypal.settings.paypal_enabled = False
    assert act(client, order, 'checkout').status_code == 503 and not mock.requests
    paypal.settings.paypal_enabled = True
    mock.oauth_fail = True
    response = act(client, order, 'checkout')
    assert response.status_code == 502 and 'MOCK-SECRET' not in response.text
    mock.oauth_fail = False
    assert act(client, order, 'checkout').status_code == 200


def test_token_refresh(env):
    client, app, mock, paypal = env
    account(client)
    order = checkout(client)
    paypal.expires = 0
    act(client, order, 'verify')
    assert sum(r.url.path.endswith('/token') for r in mock.requests) == 2
    mock.unauthorized_once = True
    assert act(client, order, 'verify').status_code == 200
    assert sum(r.url.path.endswith('/token') for r in mock.requests) == 3


@pytest.mark.parametrize('field', ['amount','currency','reference','capture_status','identity','final'])
def test_payment_mismatch_never_completes(env, field):
    client, app, mock, paypal = env
    account(client)
    order = checkout(client)
    remote = mock.orders[order['paypal_id']]
    remote['status'] = 'APPROVED'
    paypal.capture(order['paypal_id'], 'MOCK-DIRECT-TEST-CAPTURE')
    unit = remote['purchase_units'][0]
    if field == 'amount':
        unit['payments']['captures'][0]['amount'] = {'value':'1.00','currency_code':'USD'}
    elif field == 'currency':
        unit['amount']['currency_code'] = 'EUR'
    elif field == 'reference':
        unit['custom_id'] = 'another-order'
    elif field == 'capture_status':
        unit['payments']['captures'][0]['status'] = 'PENDING'
    elif field == 'identity':
        remote['id'] = 'WRONG'
    else:
        unit['payments']['captures'][0]['final_capture'] = False
    assert act(client, order, 'verify').status_code == 502
    assert client.get('/api/orders').json()[0]['status'] == 'CHECKOUT_CREATED'


def test_duplicate_webhooks_and_failure_retry(env):
    client, app, mock, paypal = env
    account(client)
    order = checkout(client)
    remote = mock.orders[order['paypal_id']]
    remote['status'] = 'APPROVED'
    paypal.capture(order['paypal_id'], 'MOCK-DIRECT-TEST-CAPTURE')
    event = {'id':'MOCK-EVENT','event_type':'PAYMENT.CAPTURE.COMPLETED','resource':{'supplementary_data':{'related_ids':{'order_id':order['paypal_id']}}}}
    headers = {key:'MOCK' for key in ['paypal-auth-algo','paypal-cert-url','paypal-transmission-id','paypal-transmission-sig','paypal-transmission-time']}
    assert client.post('/api/webhooks/paypal', json=event).status_code == 400
    mock.signature = 'FAILURE'
    assert client.post('/api/webhooks/paypal', json=event, headers=headers).status_code == 400
    mock.signature = 'SUCCESS'
    original = copy.deepcopy(remote['purchase_units'])
    remote['purchase_units'][0]['amount']['value'] = '3.00'
    assert client.post('/api/webhooks/paypal', json=event, headers=headers).status_code == 502
    remote['purchase_units'] = original
    assert client.post('/api/webhooks/paypal', json=event, headers=headers).json()['accepted']
    assert client.post('/api/webhooks/paypal', json=event, headers=headers).json()['duplicate']
    assert sum(a['event']=='PAYMENT_COMPLETED_VERIFIED' for a in client.get('/api/audit').json()) == 1


def test_operation_lease_and_expired_create(env):
    client, app, mock, paypal = env
    account(client)
    order = draft(client)
    act(client, order, 'approve')
    app.state.payments.claim(order['id'], order['merchant_id'], 'create')
    assert act(client, order, 'checkout').status_code == 409
    with app.state.database.connect(write=True) as db:
        db.execute('UPDATE operations SET lease_until=0,started=?', (time.time()-6*3600,))
    assert act(client, order, 'checkout').status_code == 409
    assert not mock.requests


def test_ai_explicit_configuration_gate():
    s = Settings(ai_provider='chat-completions', ai_enabled=False)
    with pytest.raises(HTTPException) as exc:
        Interpreter(s).interpret('5 milk at USD 25 each')
    assert exc.value.status_code == 503


def test_security_headers_and_body_limit(env):
    client, *_ = env
    response = client.get('/')
    assert response.status_code == 200 and 'frame-ancestors' in response.headers['content-security-policy']
    assert client.post('/api/auth/login', content='x'*65537).status_code == 413
    assert client.get('/', headers={'Host':'evil.example'}).status_code == 400


def test_checkout_url_rejects_live_and_attacker():
    for url in ['https://www.paypal.com/checkoutnow', 'https://www.sandbox.paypal.com.evil.example/', 'javascript:alert(1)']:
        with pytest.raises(HTTPException):
            PayPal.approval_link({'links':[{'rel':'approve','href':url}]})


def test_capture_checks_amount_before_mutation(env):
    client, app, mock, paypal = env
    account(client)
    order = checkout(client)
    remote = mock.orders[order['paypal_id']]
    remote['status'] = 'APPROVED'
    remote['purchase_units'][0]['amount']['value'] = '999.00'
    assert act(client, order, 'capture').status_code == 502
    assert not any(r.url.path.endswith('/capture') for r in mock.requests)


def test_password_not_echoed_in_validation(env):
    client, *_ = env
    response = client.post('/api/auth/login', json={'email':'bad','password':'do-not-echo'})
    assert response.status_code == 422 and 'do-not-echo' not in response.text


def test_concurrent_operation_claim(env):
    client, app, mock, paypal = env
    account(client)
    order = draft(client)
    act(client, order, 'approve')

    def claim():
        try:
            app.state.payments.claim(order['id'], order['merchant_id'], 'create')
            return 200
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: claim(), range(2))) == [200, 409]


def test_restart_preserves_create_request_id(env):
    client, app, mock, paypal = env
    account(client)
    order = draft(client)
    act(client, order, 'approve')
    mock.fail_create_once = True
    assert act(client, order, 'checkout').status_code == 502
    from app.db import Database
    from app.payments import Payments
    restarted = Payments(Database(app.state.database.path), paypal)
    result = restarted.create(order['id'], order['merchant_id'])
    assert result['status'] == 'CHECKOUT_CREATED' and len(mock.orders) == 1
    assert len(mock.create_keys) == 1


@pytest.mark.parametrize('payload', [None, [], [None], [{'rel':'approve','href':'https://www.sandbox.paypal.com:bad/'}]])
def test_malformed_checkout_links(payload):
    with pytest.raises(HTTPException) as exc:
        PayPal.approval_link({'links':payload})
    assert exc.value.status_code == 502


@pytest.mark.parametrize('result,expected', [
    ({'description':'milk','quantity':'3','unit_price':'0.10','currency':'USD','customer':None}, 200),
    ({'description':'milk','quantity':None,'unit_price':'0.10','currency':'USD'}, 422),
    ({'description':'milk','quantity':'3','unit_price':'NaN','currency':'USD'}, 422),
    ('not JSON', 502),
])
def test_ai_adapter_mocked(monkeypatch, result, expected):
    original_client = httpx.Client
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == 'ai.example.test'
        content = result if isinstance(result, str) else json.dumps(result)
        return httpx.Response(200, json={'choices':[{'message':{'content':content}}]})

    monkeypatch.setattr(httpx, 'Client', lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw))
    settings = Settings(ai_provider='chat-completions', ai_enabled=True, ai_endpoint='https://ai.example.test/chat/completions',
                        ai_model='MOCK-MODEL', ai_key='MOCK-KEY')
    adapter = Interpreter(settings)
    if expected == 200:
        details, provenance = adapter.interpret('3 milk at USD 0.10 each')
        assert details['total'] == '0.30' and details['customer'] == '' and provenance.startswith('Live AI proposal')
    else:
        with pytest.raises(HTTPException) as exc:
            adapter.interpret('input')
        assert exc.value.status_code == expected
    assert len(requests) == 1


def test_ai_network_failure_never_falls_back(monkeypatch):
    original_client = httpx.Client

    def handler(request):
        raise httpx.ReadTimeout('MOCK provider failure')

    monkeypatch.setattr(httpx, 'Client', lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw))
    settings = Settings(ai_provider='chat-completions', ai_enabled=True, ai_endpoint='https://ai.example.test/chat/completions',
                        ai_model='MOCK-MODEL', ai_key='MOCK-KEY')
    with pytest.raises(HTTPException) as exc:
        Interpreter(settings).interpret('3 milk at USD 0.10 each')
    assert exc.value.status_code == 502
