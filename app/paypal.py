import logging
import re
import threading
import time
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

BASE = 'https://api-m.sandbox.paypal.com'
logger = logging.getLogger(__name__)


class PayPal:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.client = httpx.Client(base_url=BASE, timeout=20, follow_redirects=False,
                                   trust_env=False, transport=transport)
        self.token = None
        self.expires = 0
        self.lock = threading.Lock()

    def enabled(self):
        if not self.settings.paypal_enabled or not self.settings.client_id or not self.settings.client_secret:
            raise HTTPException(503, 'PayPal sandbox calls disabled or credentials unconfigured. Operator authorization required.')

    def access_token(self):
        self.enabled()
        with self.lock:
            if self.token and time.monotonic() < self.expires:
                return self.token
            try:
                r = self.client.post('/v1/oauth2/token', auth=(self.settings.client_id, self.settings.client_secret),
                                     data={'grant_type': 'client_credentials'})
                r.raise_for_status()
                data = r.json()
                self.token = data['access_token']
                self.expires = time.monotonic() + max(0, int(data['expires_in']) - 60)
                return self.token
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                logger.warning('PayPal sandbox OAuth failed; response withheld')
                raise HTTPException(502, 'PayPal sandbox authentication failed.') from None

    def request(self, method, path, payload=None, request_id=None):
        self.enabled()
        for attempt in range(2):
            headers = {'Authorization': f'Bearer {self.access_token()}', 'Prefer': 'return=representation'}
            if request_id:
                headers['PayPal-Request-Id'] = request_id
            try:
                r = self.client.request(method, path, headers=headers, json=payload)
                if r.status_code == 401 and attempt == 0:
                    self.token = None
                    continue
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, dict):
                    raise ValueError()
                return data
            except (httpx.HTTPError, ValueError):
                logger.warning('PayPal sandbox request failed; body, headers and secrets withheld')
                raise HTTPException(502, 'PayPal sandbox request failed or outcome is uncertain. Retry the same local order; do not create a replacement.') from None

    def create(self, order, request_id):
        return self.request('POST', '/v2/checkout/orders', {
            'intent': 'CAPTURE', 'purchase_units': [{'reference_id': order['id'], 'custom_id': order['id'],
                'description': order['description'], 'amount': {'currency_code': order['currency'], 'value': order['total']}}],
            'payment_source': {'paypal': {'experience_context': {
                'brand_name': 'Omata Pay Sandbox', 'shipping_preference': 'NO_SHIPPING', 'user_action': 'PAY_NOW',
                'return_url': self.settings.base_url + '/checkout/return',
                'cancel_url': self.settings.base_url + '/checkout/cancel'}}}}, request_id)

    @staticmethod
    def valid_id(value):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,80}', value):
            raise HTTPException(502, 'PayPal returned an invalid identifier.')
        return value

    def get(self, paypal_id):
        return self.request('GET', f'/v2/checkout/orders/{self.valid_id(paypal_id)}')

    def capture(self, paypal_id, request_id):
        return self.request('POST', f'/v2/checkout/orders/{self.valid_id(paypal_id)}/capture', {}, request_id)

    @staticmethod
    def approval_link(data):
        try:
            for link in data.get('links', []):
                url = urlparse(link.get('href', ''))
                if link.get('rel') in ('approve', 'payer-action') and url.scheme == 'https' and url.hostname == 'www.sandbox.paypal.com' and not url.username and url.port in (None, 443):
                    return link['href']
        except (ValueError, TypeError, AttributeError):
            pass
        raise HTTPException(502, 'No valid PayPal sandbox checkout link returned.')

    def verify_webhook(self, headers, event):
        if not self.settings.webhook_id:
            raise HTTPException(503, 'Sandbox webhook ID is not configured.')
        mapping = {'auth_algo': 'paypal-auth-algo', 'cert_url': 'paypal-cert-url',
                   'transmission_id': 'paypal-transmission-id', 'transmission_sig': 'paypal-transmission-sig',
                   'transmission_time': 'paypal-transmission-time'}
        if any(not headers.get(v) for v in mapping.values()):
            raise HTTPException(400, 'Missing webhook signature headers.')
        payload = {k: headers[v] for k, v in mapping.items()}
        payload.update(webhook_id=self.settings.webhook_id, webhook_event=event)
        result = self.request('POST', '/v1/notifications/verify-webhook-signature', payload)
        if result.get('verification_status') != 'SUCCESS':
            raise HTTPException(400, 'Webhook signature verification failed.')
