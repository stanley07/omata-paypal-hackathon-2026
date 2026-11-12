"""Readiness must remain read-only and offline unless speech is explicitly requested."""
import httpx
import pytest
from fastapi import HTTPException

from app.readiness import inspect
from app.whatsapp import Speech
from test_shopping import shop as shop
from test_whatsapp import voice_env as voice_env


def test_readiness_checks_do_not_send_or_mutate(request, monkeypatch):
    shopping_env = request.getfixturevalue('shop')
    _, settings, _, _, app = shopping_env

    def forbidden(*args, **kwargs):
        pytest.fail('Readiness must not make network requests')

    monkeypatch.setattr(httpx.Client, 'send', forbidden)
    with app.state.database.connect() as db:
        before = list(db.iterdump())
    report = inspect(settings)
    assert not report['configuration_ready']  # local HTTP is not public ingress
    assert report['checks']['three_demo_catalogs_available']
    assert report['checks']['customer_shopping_enabled']
    assert len(report['catalogs']) == 3
    settings.base_url = 'https://demo.example.test'
    settings.cookie_secure = True
    assert inspect(settings)['configuration_ready']
    settings.whatsapp_version = ''
    report = inspect(settings)
    assert not report['configuration_ready'] and not report['checks']['whatsapp_configuration']
    with app.state.database.connect() as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize('status', [200, 403, 302])
def test_actual_speech_m4a_transport_contract(request, status):
    settings = request.getfixturevalue('voice_env')[1]
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers['user-agent'].startswith('python-httpx/')
        assert request.headers['authorization'] == 'Bearer MOCK'
        assert request.headers['content-type'].startswith('multipart/form-data; boundary=')
        assert b'filename="voice.m4a"' in request.content
        assert b'Content-Type: audio/mp4' in request.content
        assert b'name="model"' in request.content
        assert b'name="response_format"' in request.content
        return httpx.Response(status, json={'text': ' two cartons of milk '},
                              headers={'location': 'https://untrusted.example.test/'})

    speech = Speech(settings, httpx.MockTransport(handler))
    if status == 200:
        assert speech.transcribe(b'M4A-FIXTURE', 'audio/mp4') == 'two cartons of milk'
    else:
        with pytest.raises(HTTPException) as error:
            speech.transcribe(b'M4A-FIXTURE', 'audio/mp4')
        assert error.value.status_code == 502
        assert 'MOCK' not in error.value.detail
    assert len(calls) == 1  # no fallback or credential-bearing redirect
