"""Opt-in WhatsApp Cloud API intake. Audio is held in memory, never on disk."""
import hashlib
import hmac
import json
import re
import time
import uuid
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from app.db import audit, now
from app.payments import owned

MAX_AUDIO = 16 * 1024 * 1024
MIME_EXT = {'audio/ogg': 'ogg', 'audio/mpeg': 'mp3', 'audio/mp4': 'm4a',
            'audio/aac': 'aac', 'audio/amr': 'amr'}


class WhatsApp:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.client = httpx.Client(timeout=30, follow_redirects=False, trust_env=False, transport=transport)

    def enabled(self):
        s = self.settings
        if not (s.whatsapp_enabled and s.whatsapp_token and s.whatsapp_secret and
                s.whatsapp_verify_token and s.whatsapp_merchant_id and
                re.fullmatch(r'\d+', s.whatsapp_phone_id) and re.fullmatch(r'v\d+\.\d+', s.whatsapp_version)):
            raise HTTPException(503, 'WhatsApp is not enabled/configured.')

    def verify(self, body, signature):
        self.enabled()
        expected = 'sha256=' + hmac.new(self.settings.whatsapp_secret.encode(), body, hashlib.sha256).hexdigest()
        if not re.fullmatch(r'sha256=[0-9a-f]{64}', signature) or not hmac.compare_digest(expected, signature):
            raise HTTPException(401, 'Invalid WhatsApp signature.')

    def graph(self, path):
        return f'https://graph.facebook.com/{self.settings.whatsapp_version}/{path}'

    @property
    def headers(self):
        return {'Authorization': f'Bearer {self.settings.whatsapp_token}'}

    def audio(self, media_id):
        self.enabled()
        try:
            response = self.client.get(self.graph(media_id), headers=self.headers,
                                       params={'phone_number_id': self.settings.whatsapp_phone_id})
            response.raise_for_status()
            metadata = response.json()
            url = urlparse(metadata['url'])
            # Never send the access token to arbitrary URLs supplied by a provider response.
            if (url.scheme != 'https' or url.hostname != 'lookaside.fbsbx.com' or
                    url.username or url.password or url.port not in (None, 443)):
                raise ValueError()
            mime = metadata['mime_type'].split(';')[0].strip().lower()
            if mime not in MIME_EXT or not 0 < int(metadata['file_size']) <= MAX_AUDIO:
                raise HTTPException(422, 'Voice note format or size is unsupported (maximum 16 MB).')
            audio = bytearray()
            with self.client.stream('GET', metadata['url'], headers=self.headers) as download:
                download.raise_for_status()
                for chunk in download.iter_bytes():
                    audio.extend(chunk)
                    if len(audio) > MAX_AUDIO:
                        raise HTTPException(422, 'Voice note exceeds 16 MB.')
            if not audio or len(audio) != int(metadata['file_size']):
                raise ValueError()
            return bytes(audio), mime
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(502, 'WhatsApp media retrieval failed.') from None

    def send_checkout(self, sender, order):
        return self.send_text(sender, f"PayPal SANDBOX checkout: {order['description']}\n"
                              f"{order['quantity']} x {order['unit_price']} {order['currency']} = "
                              f"{order['total']} {order['currency']}\n{order['approval_url']}\n"
                              'Test funds only. This link is not proof of payment.')

    def send_text(self, sender, body):
        self.enabled()
        try:
            response = self.client.post(self.graph(self.settings.whatsapp_phone_id + '/messages'),
                headers=self.headers, json={'messaging_product': 'whatsapp', 'to': sender, 'type': 'text',
                'text': {'preview_url': False, 'body': body}})
            response.raise_for_status()
            message_id = response.json()['messages'][0]['id']
            if not isinstance(message_id, str) or not message_id:
                raise ValueError()
            return message_id
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            raise HTTPException(502, 'WhatsApp send outcome is uncertain. Check Meta delivery records before retrying.') from None


class Speech:
    """Adapter for an operator-selected multipart transcription endpoint."""
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.transport = transport

    def transcribe(self, audio, mime):
        s = self.settings
        url = urlparse(s.speech_endpoint)
        if not (s.speech_enabled and s.speech_key and s.speech_model and url.scheme == 'https'
                and url.hostname and not url.username and not url.password and not url.fragment):
            raise HTTPException(503, 'Speech provider is not enabled/configured.')
        try:
            with httpx.Client(timeout=45, follow_redirects=False, trust_env=False, transport=self.transport) as client:
                response = client.post(s.speech_endpoint, headers={'Authorization': f'Bearer {s.speech_key}'},
                    data={'model': s.speech_model, 'response_format': 'json'},
                    files={'file': ('voice.' + MIME_EXT[mime], audio, mime)})
                response.raise_for_status()
                text = response.json()['text']
                if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
                    raise ValueError()
                return text.strip()
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise HTTPException(502, 'Speech provider failed or returned an invalid transcript. No fallback used.') from None


class VoiceCommerce:
    def __init__(self, database, whatsapp, speech, interpreter):
        self.database, self.whatsapp = database, whatsapp
        self.speech, self.interpreter = speech, interpreter

    def receive(self, raw):
        try:
            event = json.loads(raw)
            if event['object'] != 'whatsapp_business_account':
                raise ValueError()
            messages = []
            for entry in event['entry']:
                for change in entry['changes']:
                    if change['field'] != 'messages':
                        continue
                    value = change['value']
                    if value['metadata']['phone_number_id'] != self.whatsapp.settings.whatsapp_phone_id:
                        continue
                    for message in value.get('messages', []):
                        if message.get('type') != 'audio':
                            continue
                        mid, sender, media = message['id'], message['from'], message['audio']['id']
                        timestamp = float(message['timestamp'])
                        if (not isinstance(mid, str) or not 1 <= len(mid) <= 200 or
                                not re.fullmatch(r'\d{7,15}', sender) or not re.fullmatch(r'\d+', media) or
                                not 0 < timestamp <= time.time() + 300):
                            raise ValueError()
                        messages.append((mid, sender, media, timestamp))
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            raise HTTPException(400, 'Invalid WhatsApp webhook structure.') from None
        failure = None
        for message in messages:
            try:
                self.process(*message)
            except HTTPException as exc:
                # One failed provider request must not starve the rest of a batch.
                failure = exc
        if failure:
            raise failure
        return {'accepted': True}

    def process(self, mid, sender, media, timestamp):
        merchant = self.whatsapp.settings.whatsapp_merchant_id
        phone = self.whatsapp.settings.whatsapp_phone_id
        claim = str(uuid.uuid4())
        with self.database.connect(write=True) as db:
            if not db.execute('SELECT 1 FROM merchants WHERE id=?', (merchant,)).fetchone():
                raise HTTPException(503, 'Configured WhatsApp merchant does not exist.')
            row = db.execute('SELECT * FROM whatsapp_messages WHERE id=?', (mid,)).fetchone()
            if row and (row['merchant_id'] != merchant or row['sender'] != sender or row['media_id'] != media
                        or row['phone_id'] not in (None, phone)):
                raise HTTPException(409, 'WhatsApp message identity conflict.')
            if row and (row['order_id'] or row['failure'] == 'INVALID_ORDER'):
                return
            if row and row['lease_until'] > time.time():
                raise HTTPException(503, 'Voice message processing is in progress. Retry delivery.')
            db.execute('INSERT INTO whatsapp_messages(id,merchant_id,sender,received,media_id,lease_until,claim_token,phone_id) '
                       'VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET lease_until=excluded.lease_until,'
                       'claim_token=excluded.claim_token,phone_id=excluded.phone_id,failure=NULL',
                       (mid, merchant, sender, timestamp, media, time.time() + 300, claim, phone))
        try:
            transcript = row['transcript'] if row else None
            if not transcript:
                audio, mime = self.whatsapp.audio(media)
                transcript = self.speech.transcribe(audio, mime)
                with self.database.connect(write=True) as db:
                    updated = db.execute('UPDATE whatsapp_messages SET transcript=? WHERE id=? AND claim_token=?',
                                         (transcript, mid, claim))
                    if not updated.rowcount:
                        raise HTTPException(503, 'Voice processing lease changed. Retry delivery.')
            details, mode = self.interpreter.interpret(transcript)
            order_id = str(uuid.uuid4())
            with self.database.connect(write=True) as db:
                # Recheck under the write lock even if a slow worker outlived its lease.
                current = db.execute('SELECT order_id,claim_token FROM whatsapp_messages WHERE id=?', (mid,)).fetchone()
                if current['order_id']:
                    return
                if current['claim_token'] != claim:
                    raise HTTPException(503, 'Voice processing lease changed. Retry delivery.')
                db.execute('INSERT INTO orders(id,merchant_id,description,quantity,unit_price,currency,total,customer,interpretation,status,created_at) '
                           'VALUES(?,?,?,?,?,?,?,?,?,?,?)', (order_id, merchant, details['description'], details['quantity'],
                           details['unit_price'], details['currency'], details['total'], details['customer'], mode, 'DRAFT', now()))
                db.execute('UPDATE whatsapp_messages SET order_id=?,lease_until=0,claim_token=NULL WHERE id=?', (order_id, mid))
                audit(db, merchant, 'WHATSAPP_VOICE_DRAFT_CREATED', order_id)
        except HTTPException as exc:
            with self.database.connect(write=True) as db:
                db.execute('UPDATE whatsapp_messages SET lease_until=0,claim_token=NULL,failure=? WHERE id=? AND claim_token=?',
                           ('INVALID_ORDER' if exc.status_code == 422 else 'PROVIDER_FAILED', mid, claim))
            if exc.status_code != 422:
                raise

    def share(self, order_id, merchant):
        self.whatsapp.enabled()
        with self.database.connect(write=True) as db:
            order = owned(db, order_id, merchant)
            row = db.execute('SELECT * FROM whatsapp_messages WHERE order_id=? AND merchant_id=?', (order_id, merchant)).fetchone()
            if not row:
                raise HTTPException(409, 'This order has no WhatsApp voice conversation.')
            if merchant != self.whatsapp.settings.whatsapp_merchant_id:
                raise HTTPException(409, 'WhatsApp merchant configuration has changed.')
            if row['phone_id'] != self.whatsapp.settings.whatsapp_phone_id:
                raise HTTPException(409, 'WhatsApp business number binding requires operator reconciliation.')
            if row['share_state'] == 'SENT':
                return {'status': 'SENT'}
            if row['share_state'] != 'READY':
                raise HTTPException(409, 'Send outcome is uncertain or in progress. Operator reconciliation required.')
            if order['status'] != 'CHECKOUT_CREATED' or not order['approval_url']:
                raise HTTPException(409, 'Create an approved sandbox checkout first.')
            if time.time() - row['received'] >= 24 * 3600:
                raise HTTPException(409, 'WhatsApp reply window expired. Ask the customer for a new message.')
            # Commit before sending: ambiguous network outcomes must never auto-send a duplicate.
            db.execute("UPDATE whatsapp_messages SET share_state='UNCERTAIN' WHERE id=?", (row['id'],))
        outbound = self.whatsapp.send_checkout(row['sender'], order)
        with self.database.connect(write=True) as db:
            db.execute("UPDATE whatsapp_messages SET share_state='SENT',outbound_id=? WHERE id=?", (outbound, row['id']))
            audit(db, merchant, 'WHATSAPP_SANDBOX_LINK_SENT', order_id)
        return {'status': 'SENT'}
