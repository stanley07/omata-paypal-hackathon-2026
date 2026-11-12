"""Runner-only tests. Every provider response, including buyer approval, is MOCKED."""
import json
import sys

import httpx
import pytest
from fastapi import HTTPException

from app.config import ROOT, Settings
from app.demo import CUSTOMER, PHONE, Demo, initialize, main, run_path
from app.paypal import PayPal
from app.shopping import GroqIntent
from app.whatsapp import Speech, WhatsApp
from test_application import SandboxMock


@pytest.fixture
def runner(monkeypatch):
    # Mock evidence is confined to tests, never mixed with operator demonstration runs.
    monkeypatch.setattr('app.demo.RUNS', ROOT / 'artifacts' / 'tests' / 'shopping-demo')
    def forbidden(*args, **kwargs):
        pytest.fail('Real HTTP or WhatsApp transport must never be used in runner tests')

    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)
    monkeypatch.setattr(WhatsApp, 'send_text', forbidden)
    run_id = initialize()
    settings = Settings(paypal_enabled=True, client_id='MOCK-ID', client_secret='MOCK-SECRET',
                        speech_enabled=True, speech_endpoint='https://speech.example.test/transcriptions',
                        speech_key='MOCK-SPEECH', speech_model='MOCK',
                        groq_enabled=True, groq_key='MOCK-GROQ', groq_model='MOCK')
    state = {'review': False, 'speech_calls': 0}

    def provider(request):
        if request.url.host == 'speech.example.test':
            state['speech_calls'] += 1
            return httpx.Response(200, json={'text': 'Two cartons of milk; private input marker'})
        assert request.url.host == 'api.groq.com'
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
            'product_ids': ['sample-river-milk', 'sample-sunrise-milk', 'sample-copper-milk'],
            'quantity': 2, 'needs_review': state['review']})}}]})

    remote = SandboxMock()
    paypal = PayPal(settings, httpx.MockTransport(remote))
    intent = GroqIntent(settings, httpx.MockTransport(provider))
    speech = Speech(settings, httpx.MockTransport(provider))
    demo = Demo(run_id, settings, paypal, intent, speech)
    yield demo, remote, state, run_id
    demo.close()


def confirm_code(demo):
    with demo.database.connect() as db:
        return db.execute('SELECT confirmation FROM shopping_sessions').fetchone()[0]


def prepare(demo):
    demo.submit('Two cartons of milk; private input marker')
    demo.submit('option 2')
    return confirm_code(demo)


def test_local_voice_checkout_stops_then_requires_remote_buyer_approval(runner):
    demo, remote, state, _ = runner
    audio = demo.path / 'test.m4a'
    audio.write_bytes(b'MOCK-AUDIO')
    replies = demo.submit(str(audio), audio=True)
    assert state['speech_calls'] == 1 and not remote.requests
    assert len(demo.snapshot()['offer_product_ids']) == 3
    assert 'Sunrise' in replies[0] and 'River' in replies[0] and 'Copper' in replies[0]
    demo.submit('option 2')
    demo.submit('CONFIRM wrong')
    assert not remote.requests and demo.snapshot()['order'] is None
    replies = demo.submit('CONFIRM ' + confirm_code(demo))
    assert 'https://www.sandbox.paypal.com/' in replies[0]
    order = demo.snapshot()['order']
    assert order['status'] == 'CHECKOUT_CREATED' and order['total'] == '6.40'
    assert not any(r.url.path.endswith('/capture') for r in remote.requests)
    demo.submit('PAID')  # Operator's assertion cannot substitute for remote buyer approval.
    assert not any(r.url.path.endswith('/capture') for r in remote.requests)
    assert demo.snapshot()['order']['capture_id'] is None
    remote.orders[order['paypal_id']]['status'] = 'APPROVED'  # MOCK only, never runner code.
    demo.submit('PAID')
    final = demo.snapshot()['order']
    assert final['status'] == 'COMPLETED' and final['capture_id'] and final['verified_at']
    assert remote.requests[-1].method == 'GET'  # independent verification after capture
    demo.submit('PAID')
    assert sum(r.url.path.endswith('/capture') for r in remote.requests) == 1
    evidence = (demo.path / 'evidence.jsonl').read_text()
    assert 'private input marker' not in evidence and 'MOCK-SECRET' not in evidence
    assert 'MOCK-SPEECH' not in evidence and 'test.m4a' not in evidence
    assert CUSTOMER not in evidence and 'access_token' not in evidence
    records = [json.loads(line) for line in evidence.splitlines()]
    assert records[-1]['order']['verified_at'] and records[-1]['timestamp']
    assert records[-1]['selected_product_id'] == 'sample-sunrise-milk'
    with demo.database.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM whatsapp_messages').fetchone()[0] == 0
        assert db.execute('SELECT phone_id FROM shopping_sessions').fetchone()[0] == PHONE


def test_uncertain_create_survives_restart_and_requires_explicit_retry(runner):
    demo, remote, _, run_id = runner
    code = prepare(demo)
    remote.fail_create_once = True
    with pytest.raises(HTTPException):
        demo.submit('CONFIRM ' + code)
    requests = len(remote.requests)
    resumed = Demo(run_id, demo.settings, demo.paypal, demo.engine.intent, demo.engine.speech)
    with pytest.raises(ValueError):
        resumed.submit('CONFIRM ' + code)
    assert len(remote.requests) == requests
    assert resumed.snapshot()['pending_input']
    resumed.submit(retry=True)
    assert len(remote.create_keys) == 1 and not resumed.snapshot()['pending_input']
    creates = [r for r in remote.requests if r.url.path == '/v2/checkout/orders']
    assert len(creates) == 2
    assert creates[0].headers['paypal-request-id'] == creates[1].headers['paypal-request-id']
    with demo.database.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 1
        assert db.execute("SELECT stock FROM catalog_products WHERE id='sample-sunrise-milk'").fetchone()[0] == 28


def test_custom_request_uses_existing_human_review(runner):
    demo, remote, state, _ = runner
    state['review'] = True
    reply = demo.submit('custom private input marker')
    assert 'merchant review' in reply[0]
    assert demo.snapshot()['state'] == 'REVIEW' and demo.snapshot()['human_review_count'] == 1
    assert not remote.requests and demo.snapshot()['order'] is None
    assert 'private input marker' not in (demo.path / 'evidence.jsonl').read_text()


def test_isolated_initialization_and_path_rejection(runner):
    demo, remote, _, run_id = runner
    assert demo.database.path == run_path(run_id) / 'shopping.db'
    assert demo.database.path.parts[-3] == 'shopping-demo'
    with demo.database.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM merchants').fetchone()[0] == 3
        assert db.execute('SELECT COUNT(*) FROM catalog_products').fetchone()[0] == 9
        assert db.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 0
    for invalid in ('../../data/omata.db', str(demo.database.path), 'omata.db'):
        with pytest.raises(ValueError):
            Demo(invalid)
    demo.export()
    assert not remote.requests


def test_cli_requires_opt_in_before_loading_credentials_or_network(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['demo', 'input', '--run', 'unused', '--text', 'CONFIRM anything'])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_inventory_recheck_and_no_fabricated_verified_evidence(runner):
    demo, remote, _, _ = runner
    code = prepare(demo)
    with demo.database.connect(write=True) as db:
        db.execute("UPDATE catalog_products SET stock=0 WHERE id='sample-sunrise-milk'")
    demo.submit('CONFIRM ' + code)
    assert demo.snapshot()['order'] is None and not remote.requests
    assert all(json.loads(line)['order'] is None for line in (demo.path / 'evidence.jsonl').read_text().splitlines())


def test_verification_mismatch_never_becomes_completed_evidence(runner):
    demo, remote, _, _ = runner
    demo.submit('CONFIRM ' + prepare(demo))
    order = demo.snapshot()['order']
    remote.orders[order['paypal_id']]['status'] = 'APPROVED'
    remote.orders[order['paypal_id']]['purchase_units'][0]['amount']['value'] = '999.00'
    with pytest.raises(HTTPException):
        demo.submit('PAID')
    assert not any(r.url.path.endswith('/capture') for r in remote.requests)
    evidence = json.loads((demo.path / 'evidence.jsonl').read_text().splitlines()[-1])
    assert evidence['outcome'] == 'FAILED_OR_UNCERTAIN'
    assert evidence['order']['status'] == 'CHECKOUT_CREATED'
    assert evidence['order']['capture_id'] is None and evidence['order']['verified_at'] is None
