"""WhatsApp, speech and PayPal requests are mocked; no live messages are sent."""
import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import ROOT, Settings
from app.main import create_app
from app.interpretation import Interpreter
from app.paypal import PayPal
from app.whatsapp import MAX_AUDIO, Speech, VoiceCommerce, WhatsApp
from test_application import SandboxMock, account, act


@pytest.fixture
def voice_env():
    settings = Settings(database=ROOT / 'artifacts' / 'tests' / (str(uuid.uuid4()) + '.db'),
        paypal_enabled=True, client_id='MOCK', client_secret='MOCK', ai_provider='deterministic',
        whatsapp_enabled=True, whatsapp_token='MOCK-TOKEN', whatsapp_secret='MOCK-SECRET',
        whatsapp_verify_token='MOCK-VERIFY', whatsapp_phone_id='1234', whatsapp_version='v24.0',
        speech_enabled=True, speech_endpoint='https://speech.example.test/transcriptions',
        speech_model='MOCK', speech_key='MOCK')
    state = {'requests': [], 'transcript': '5 cartons of milk at USD 25 each for Ada', 'send_fail': False,
             'media_url': 'https://lookaside.fbsbx.com/media', 'size': 5, 'mime': 'audio/ogg', 'speech_fail': False}

    def handler(request):
        state['requests'].append(request)
        if request.url.host == 'speech.example.test':
            if state['speech_fail']:
                return httpx.Response(503)
            assert b'voice.ogg' in request.content
            return httpx.Response(200, json={'text': state['transcript']})
        if request.url.host == 'lookaside.fbsbx.com':
            return httpx.Response(200, content=b'VOICE')
        assert request.url.host == 'graph.facebook.com'
        if request.url.path.endswith('/messages'):
            if state['send_fail']:
                raise httpx.ReadTimeout('MOCK ambiguous send')
            return httpx.Response(200, json={'messages': [{'id': 'wamid.OUT'}]})
        return httpx.Response(200, json={'url': state['media_url'], 'mime_type': state['mime'], 'file_size': state['size']})

    mock = SandboxMock()
    app = create_app(settings, PayPal(settings, httpx.MockTransport(mock)),
                     whatsapp=WhatsApp(settings, httpx.MockTransport(handler)),
                     speech=Speech(settings, httpx.MockTransport(handler)))
    with TestClient(app, base_url=settings.base_url, headers={'Origin': settings.base_url}) as client:
        account(client)
        settings.whatsapp_merchant_id = client.get('/api/me').json()['id']
        yield client, settings, state, mock, app


def event(mid='wamid.IN', timestamp=None, phone='1234'):
    return {'object': 'whatsapp_business_account', 'entry': [{'changes': [{'field': 'messages', 'value': {
        'metadata': {'phone_number_id': phone}, 'messages': [{'id': mid, 'from': '2348012345678',
        'timestamp': str(timestamp or int(time.time())), 'type': 'audio', 'audio': {'id': '999'}}]}}]}]}


def deliver(client, settings, payload=None):
    raw = json.dumps(payload or event()).encode()
    signature = 'sha256=' + hmac.new(settings.whatsapp_secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post('/api/webhooks/whatsapp', content=raw,
                       headers={'x-hub-signature-256': signature, 'Origin': 'https://external.example'})


def test_voice_to_verified_paypal(voice_env):
    client, settings, state, mock, app = voice_env
    assert deliver(client, settings).status_code == 200
    assert deliver(client, settings).status_code == 200
    orders = client.get('/api/orders').json()
    assert len(orders) == 1
    order = orders[0]
    assert order['status'] == 'DRAFT' and order['total'] == '125.00'
    assert order['transcript'] == state['transcript'] and not mock.requests
    assert act(client, order, 'share-whatsapp').status_code == 409
    assert act(client, order, 'approve').status_code == 200
    assert not mock.requests
    order = act(client, order, 'checkout').json()
    assert act(client, order, 'share-whatsapp').json() == {'status': 'SENT'}
    assert act(client, order, 'share-whatsapp').status_code == 200
    sent = [r for r in state['requests'] if r.url.path.endswith('/messages')]
    assert len(sent) == 1
    body = json.loads(sent[0].content)
    assert body['to'] == '2348012345678' and order['approval_url'] in body['text']['body']
    assert client.get('/api/orders').json()[0]['status'] == 'CHECKOUT_CREATED'
    mock.orders[order['paypal_id']]['status'] = 'APPROVED'
    assert act(client, order, 'capture').json()['status'] == 'COMPLETED'


def test_signature_challenge_and_disabled(voice_env):
    client, settings, state, *_ = voice_env
    params = {'hub.mode': 'subscribe', 'hub.verify_token': 'MOCK-VERIFY', 'hub.challenge': '123'}
    assert client.get('/api/webhooks/whatsapp', params=params).text == '123'
    params['hub.verify_token'] = 'wrong'
    assert client.get('/api/webhooks/whatsapp', params=params).status_code == 403
    params['hub.verify_token'] = '\u2603'
    assert client.get('/api/webhooks/whatsapp', params=params).status_code == 403
    assert client.post('/api/webhooks/whatsapp', json=event()).status_code == 401
    assert client.post('/api/webhooks/whatsapp', json=event(),
                       headers={'x-hub-signature-256': 'sha256=invalid'}).status_code == 401
    settings.whatsapp_enabled = False
    assert deliver(client, settings).status_code == 503
    assert not state['requests']


def test_routing_and_isolation(voice_env):
    client, settings, state, *_ = voice_env
    assert deliver(client, settings, event(phone='5678')).status_code == 200
    assert not state['requests']
    assert deliver(client, settings).status_code == 200
    order = client.get('/api/orders').json()[0]
    client.post('/api/auth/logout')
    account(client, 'other@example.test')
    assert client.get('/api/whatsapp/messages').json() == []
    assert client.get('/api/orders').json() == []
    assert act(client, order, 'share-whatsapp').status_code == 404


def test_provider_retry_and_invalid_order(voice_env):
    client, settings, state, *_ = voice_env
    state['speech_fail'] = True
    assert deliver(client, settings).status_code == 502
    assert client.get('/api/orders').json() == []
    state['speech_fail'] = False
    state['transcript'] = 'some milk please'
    assert deliver(client, settings).status_code == 200
    assert client.get('/api/whatsapp/messages').json()[0]['failure'] == 'INVALID_ORDER'
    before = len(state['requests'])
    assert deliver(client, settings).status_code == 200
    assert len(state['requests']) == before
    assert client.get('/api/orders').json() == []


@pytest.mark.parametrize('key,value,status', [('media_url', 'https://evil.example/steal', 502),
    ('media_url', 'https://lookaside.fbsbx.com.evil.example/media', 502), ('size', MAX_AUDIO + 1, 200),
    ('mime', 'application/pdf', 200), ('size', 4, 502)])
def test_media_validation(voice_env, key, value, status):
    client, settings, state, *_ = voice_env
    state[key] = value
    assert deliver(client, settings).status_code == status
    assert client.get('/api/orders').json() == []
    assert not any(r.url.host == 'speech.example.test' for r in state['requests'])


def test_uncertain_send_blocks_duplicate(voice_env):
    client, settings, state, *_ = voice_env
    deliver(client, settings)
    order = client.get('/api/orders').json()[0]
    act(client, order, 'approve')
    act(client, order, 'checkout')
    state['send_fail'] = True
    assert act(client, order, 'share-whatsapp').status_code == 502
    assert act(client, order, 'share-whatsapp').status_code == 409
    assert sum(r.url.path.endswith('/messages') for r in state['requests']) == 1


def test_expired_reply_window(voice_env):
    client, settings, state, *_ = voice_env
    deliver(client, settings, event(timestamp=int(time.time()) - 25 * 3600))
    order = client.get('/api/orders').json()[0]
    act(client, order, 'approve')
    act(client, order, 'checkout')
    assert act(client, order, 'share-whatsapp').status_code == 409
    assert not any(r.url.path.endswith('/messages') for r in state['requests'])


def test_concurrent_delivery(voice_env):
    client, settings, state, *_ = voice_env
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(lambda _: deliver(client, settings).status_code, range(2)))
    assert 200 in codes and all(code in (200, 503) for code in codes)
    assert len(client.get('/api/orders').json()) == 1


@pytest.mark.parametrize('payload', [[], {'object': 'whatsapp_business_account', 'entry': None},
                                   {'object': 'other'}, {'object': 'whatsapp_business_account', 'entry': [None]}])
def test_malformed_payload(voice_env, payload):
    client, settings, *_ = voice_env
    raw = json.dumps(payload).encode()
    signature = 'sha256=' + hmac.new(settings.whatsapp_secret.encode(), raw, hashlib.sha256).hexdigest()
    assert client.post('/api/webhooks/whatsapp', content=raw, headers={'x-hub-signature-256': signature}).status_code == 400


def test_speech_disabled():
    with pytest.raises(HTTPException) as exc:
        Speech(Settings(speech_enabled=False)).transcribe(b'VOICE', 'audio/ogg')
    assert exc.value.status_code == 503


@pytest.mark.parametrize('old_worker_fails', [False, True])
def test_expired_worker_cannot_overwrite_new_result(voice_env, old_worker_fails):
    client, settings, state, mock, app = voice_env
    adapter = WhatsApp(settings, httpx.MockTransport(lambda _: httpx.Response(500)))
    adapter.audio = lambda _: (b'VOICE', 'audio/ogg')

    class SlowSpeech:
        calls = 0

        def transcribe(self, audio, mime):
            self.calls += 1
            if self.calls == 1:
                # Simulate a slow worker losing its lease and another delivery finishing.
                with app.state.database.connect(write=True) as db:
                    db.execute('UPDATE whatsapp_messages SET lease_until=0')
                commerce.process('wamid.STALE', '2348012345678', '999', time.time())
                if old_worker_fails:
                    raise HTTPException(422, 'Old worker failure')
                return '1 old product at USD 99 each'
            return '2 new products at USD 10 each'

    commerce = VoiceCommerce(app.state.database, adapter, SlowSpeech(), Interpreter(settings))
    try:
        if old_worker_fails:
            commerce.process('wamid.STALE', '2348012345678', '999', time.time())
        else:
            with pytest.raises(HTTPException) as exc:
                commerce.process('wamid.STALE', '2348012345678', '999', time.time())
            assert exc.value.status_code == 503
        orders = client.get('/api/orders').json()
        assert len(orders) == 1 and orders[0]['total'] == '20.00'
        assert orders[0]['transcript'] == '2 new products at USD 10 each'
        message = client.get('/api/whatsapp/messages').json()[0]
        assert message['failure'] is None
    finally:
        adapter.client.close()


def test_failed_batch_item_does_not_starve_later_message(voice_env, monkeypatch):
    client, settings, state, *_ = voice_env
    original = Speech.transcribe
    calls = []

    def fail_once(self, audio, mime):
        calls.append(True)
        if len(calls) == 1:
            raise HTTPException(502, 'MOCK transient failure')
        return original(self, audio, mime)

    monkeypatch.setattr(Speech, 'transcribe', fail_once)
    payload = event()
    payload['entry'][0]['changes'][0]['value']['messages'].extend(
        event(mid='wamid.SECOND')['entry'][0]['changes'][0]['value']['messages'])
    assert deliver(client, settings, payload).status_code == 502
    assert len(client.get('/api/orders').json()) == 1
    assert deliver(client, settings, payload).status_code == 200
    assert len(client.get('/api/orders').json()) == 2
    assert len(calls) == 3


def test_business_number_change_blocks_sharing(voice_env):
    client, settings, state, *_ = voice_env
    deliver(client, settings)
    order = client.get('/api/orders').json()[0]
    act(client, order, 'approve')
    act(client, order, 'checkout')
    settings.whatsapp_phone_id = '5678'
    assert act(client, order, 'share-whatsapp').status_code == 409
    assert not any(r.url.path.endswith('/messages') for r in state['requests'])


def test_share_requires_csrf_and_origin(voice_env):
    client, settings, state, *_ = voice_env
    deliver(client, settings)
    order = client.get('/api/orders').json()[0]
    act(client, order, 'approve')
    act(client, order, 'checkout')
    url = f"/api/orders/{order['id']}/share-whatsapp"
    assert client.post(url, headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    assert client.post(url, headers={'Origin': 'https://attacker.example'}).status_code == 403
    assert not any(r.url.path.endswith('/messages') for r in state['requests'])


def test_concurrent_sharing_sends_once(voice_env):
    client, settings, state, *_ = voice_env
    deliver(client, settings)
    order = client.get('/api/orders').json()[0]
    act(client, order, 'approve')
    act(client, order, 'checkout')
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(lambda _: act(client, order, 'share-whatsapp').status_code, range(2)))
    assert 200 in codes and all(code in (200, 409) for code in codes)
    assert sum(r.url.path.endswith('/messages') for r in state['requests']) == 1


def test_restart_preserves_duplicate_protection(voice_env):
    client, settings, state, mock, app = voice_env
    deliver(client, settings)
    before = len(state['requests'])
    adapter = WhatsApp(settings, httpx.MockTransport(lambda _: httpx.Response(500)))
    from app.db import Database
    restarted = VoiceCommerce(Database(app.state.database.path), adapter, Speech(settings), Interpreter(settings))
    try:
        restarted.receive(json.dumps(event()).encode())
        assert len(client.get('/api/orders').json()) == 1
        assert len(state['requests']) == before
    finally:
        adapter.client.close()


def test_existing_voice_database_migration():
    from app.db import Database
    path = ROOT / 'artifacts' / 'tests' / (str(uuid.uuid4()) + '.db')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE whatsapp_messages (id TEXT PRIMARY KEY, transcript TEXT)')
        db.execute("INSERT INTO whatsapp_messages VALUES ('existing', 'retained transcript')")
    database = Database(path)
    with database.connect() as db:
        row = dict(db.execute('SELECT * FROM whatsapp_messages').fetchone())
        assert row == {'id': 'existing', 'transcript': 'retained transcript', 'claim_token': None, 'phone_id': None}
    Database(path)  # Migration is idempotent.


def test_streamed_audio_limit_ignores_false_metadata():
    settings = Settings(whatsapp_enabled=True, whatsapp_token='MOCK', whatsapp_secret='MOCK',
        whatsapp_verify_token='MOCK', whatsapp_merchant_id='MOCK', whatsapp_phone_id='123', whatsapp_version='v24.0')

    class OversizedStream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(17):
                yield b'x' * (1024 * 1024)

    def handler(request):
        if request.url.host == 'graph.facebook.com':
            return httpx.Response(200, json={'url': 'https://lookaside.fbsbx.com/media',
                                            'file_size': 5, 'mime_type': 'audio/ogg'})
        return httpx.Response(200, stream=OversizedStream())

    adapter = WhatsApp(settings, httpx.MockTransport(handler))
    try:
        with pytest.raises(HTTPException) as exc:
            adapter.audio('999')
        assert exc.value.status_code == 422
    finally:
        adapter.client.close()
