import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool

from app.config import ROOT, Settings
from app.db import Database, audit, now
from app.interpretation import Interpreter
from app.payments import Payments, owned
from app.paypal import PayPal
from app.security import password_hash, password_matches, token_hash
from app.whatsapp import Speech, VoiceCommerce, WhatsApp
from app.catalog import seed_catalogs
from app.shopping import CustomerShopping, GroqIntent


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=200, pattern=r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
    password: str = Field(min_length=12, max_length=128)
    name: str = Field(default='Merchant', min_length=1, max_length=80)


class Prompt(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def create_app(settings=None, paypal=None, interpreter=None, whatsapp=None, speech=None, shopping_intent=None):
    settings = settings or Settings()
    settings.validate()
    database = Database(settings.database)
    paypal = paypal or PayPal(settings)
    interpreter = interpreter or Interpreter(settings)
    payments = Payments(database, paypal)
    whatsapp = whatsapp or WhatsApp(settings)
    voice = VoiceCommerce(database, whatsapp, speech or Speech(settings), interpreter)
    shopping = CustomerShopping(database, whatsapp, speech or Speech(settings), shopping_intent or GroqIntent(settings), payments)
    if settings.shopping_enabled:
        seed_catalogs(database)

    @asynccontextmanager
    async def lifespan(app):
        yield
        paypal.client.close()
        whatsapp.client.close()

    app = FastAPI(title='Omata Pay · Sandbox', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.database = database
    app.state.payments = payments
    app.state.shopping = shopping
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlparse(settings.base_url).hostname])

    @app.middleware('http')
    async def boundaries(request, call_next):
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and request.url.path not in ('/api/webhooks/paypal', '/api/webhooks/whatsapp'):
            if request.headers.get('origin') != settings.base_url:
                return JSONResponse({'detail': 'Same-origin request required.'}, status_code=403)
        # Limit streamed payloads too; do not rely only on Content-Length.
        if request.method == 'POST':
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 65536:
                    return JSONResponse({'detail': 'Request too large.'}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"})
        return response

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(request, exc):
        return JSONResponse({'detail': 'Conflicting or duplicate record. Refresh and verify the existing order.'}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Pydantic's default response includes input values, potentially passwords.
        return JSONResponse({'detail': 'Invalid input. Check required fields, email format and password length (12–128 characters).'}, status_code=422)

    def session(request: Request):
        token = request.cookies.get('omata_session', '')
        with database.connect() as db:
            row = db.execute('SELECT s.*,m.name,m.email FROM sessions s JOIN merchants m ON m.id=s.merchant_id WHERE token=? AND expires>?',
                             (token_hash(token), time.time())).fetchone()
        if not row:
            raise HTTPException(401, 'Please sign in.')
        if request.method == 'POST' and not secrets.compare_digest(request.headers.get('x-csrf-token', ''), row['csrf']):
            raise HTTPException(403, 'Invalid CSRF token. Refresh and try again.')
        return dict(row)

    def start_session(response, merchant):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with database.connect(write=True) as db:
            db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (token_hash(token), merchant, csrf, time.time() + 8 * 3600))
            audit(db, merchant, 'SIGNED_IN')
        response.set_cookie('omata_session', token, httponly=True, secure=settings.cookie_secure,
                            samesite='lax', max_age=8 * 3600, path='/')
        return {'csrf': csrf}

    def auth_limit(request, email):
        key = token_hash((request.client.host if request.client else 'local') + ':' + email.lower())
        with database.connect(write=True) as db:
            row = db.execute('SELECT * FROM login_attempts WHERE key=?', (key,)).fetchone()
            if row and row['until'] > time.time() and row['failures'] >= 10:
                raise HTTPException(429, 'Too many authentication attempts. Try again in 15 minutes.')
            count = row['failures'] + 1 if row and row['until'] > time.time() else 1
            db.execute('INSERT INTO login_attempts VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET failures=?,until=?',
                       (key, count, time.time() + 900, count, time.time() + 900))
        return key

    @app.post('/api/auth/register')
    def register(body: Credentials, request: Request, response: Response):
        if not body.name.strip():
            raise HTTPException(422, 'Business name cannot be blank.')
        auth_limit(request, body.email)
        merchant = str(uuid.uuid4())
        with database.connect(write=True) as db:
            db.execute('INSERT INTO merchants VALUES(?,?,?,?,?)',
                       (merchant, body.email.lower(), body.name.strip(), password_hash(body.password), now()))
            audit(db, merchant, 'MERCHANT_REGISTERED')
        return start_session(response, merchant)

    @app.post('/api/auth/login')
    def login(body: Credentials, request: Request, response: Response):
        key = auth_limit(request, body.email)
        with database.connect() as db:
            merchant = db.execute('SELECT * FROM merchants WHERE email=?', (body.email.lower(),)).fetchone()
        dummy = '00' * 16 + ':' + '00' * 64
        valid = password_matches(body.password, merchant['password'] if merchant else dummy)
        if not merchant or not valid:
            raise HTTPException(401, 'Invalid email or password.')
        with database.connect(write=True) as db:
            db.execute('DELETE FROM login_attempts WHERE key=?', (key,))
        return start_session(response, merchant['id'])

    @app.post('/api/auth/logout')
    def logout(response: Response, user=Depends(session)):
        with database.connect(write=True) as db:
            db.execute('DELETE FROM sessions WHERE token=?', (user['token'],))
        response.delete_cookie('omata_session')
        return {'ok': True}

    @app.get('/api/me')
    def me(user=Depends(session)):
        return {'id': user['merchant_id'], 'name': user['name'], 'email': user['email'], 'csrf': user['csrf'],
                'sandbox': True, 'shopping_enabled': settings.shopping_enabled,
                'paypal_enabled': bool(settings.paypal_enabled and settings.client_id and settings.client_secret),
                'interpretation': 'Deterministic development parser — not live AI' if settings.ai_provider == 'deterministic' else 'Configured AI provider — live inference only on successful requests'}

    @app.get('/api/orders')
    def orders(user=Depends(session)):
        with database.connect() as db:
            return [dict(r) for r in db.execute('SELECT o.*,w.transcript,w.sender AS whatsapp_sender,w.share_state AS whatsapp_share_state '
                'FROM orders o LEFT JOIN whatsapp_messages w ON w.order_id=o.id AND w.merchant_id=o.merchant_id '
                'WHERE o.merchant_id=? ORDER BY o.created_at DESC', (user['merchant_id'],))]

    @app.get('/api/whatsapp/messages')
    def voice_messages(user=Depends(session)):
        with database.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,sender,received,transcript,order_id,failure,share_state '
                'FROM whatsapp_messages WHERE merchant_id=? ORDER BY received DESC LIMIT 100', (user['merchant_id'],))]

    @app.get('/api/webhooks/whatsapp')
    def whatsapp_challenge(request: Request):
        whatsapp.enabled()
        query = request.query_params
        if (query.get('hub.mode') != 'subscribe' or not query.get('hub.challenge') or
                not secrets.compare_digest(query.get('hub.verify_token', '').encode(), settings.whatsapp_verify_token.encode())):
            raise HTTPException(403, 'Invalid WhatsApp verification challenge.')
        return PlainTextResponse(query['hub.challenge'])

    @app.post('/api/webhooks/whatsapp')
    async def whatsapp_webhook(request: Request):
        raw = await request.body()
        whatsapp.verify(raw, request.headers.get('x-hub-signature-256', ''))
        return await run_in_threadpool(shopping.receive if settings.shopping_enabled else voice.receive, raw)

    @app.get('/api/catalog')
    def merchant_catalog(user=Depends(session)):
        with database.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM catalog_products WHERE merchant_id=? ORDER BY name',
                                                   (user['merchant_id'],))]

    @app.get('/api/shopping/reviews')
    def shopping_reviews(user=Depends(session)):
        with database.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM shopping_reviews WHERE merchant_id=? ORDER BY created_at DESC LIMIT 100',
                                                   (user['merchant_id'],))]

    @app.post('/api/orders')
    def draft(body: Prompt, user=Depends(session)):
        details, mode = interpreter.interpret(body.text)
        order_id = str(uuid.uuid4())
        with database.connect(write=True) as db:
            db.execute('INSERT INTO orders(id,merchant_id,description,quantity,unit_price,currency,total,customer,interpretation,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (order_id, user['merchant_id'], details['description'], details['quantity'], details['unit_price'], details['currency'], details['total'], details['customer'], mode, 'DRAFT', now()))
            audit(db, user['merchant_id'], 'DRAFT_CREATED', order_id)
            return owned(db, order_id, user['merchant_id'])

    @app.post('/api/orders/{order_id}/{action}')
    def action(order_id: str, action: str, user=Depends(session)):
        merchant = user['merchant_id']
        # Ownership is checked even while integrations are disabled.
        with database.connect() as db:
            owned(db, order_id, merchant)
        if action == 'share-whatsapp':
            return voice.share(order_id, merchant)
        if action in ('approve', 'reject'):
            with database.connect(write=True) as db:
                order = owned(db, order_id, merchant)
                target = 'APPROVED' if action == 'approve' else 'REJECTED'
                if order['status'] == target:
                    return order
                if order['status'] != 'DRAFT':
                    raise HTTPException(409, 'Only a draft may be approved or rejected.')
                db.execute('UPDATE orders SET status=?,approved_at=? WHERE id=?', (target, now() if action == 'approve' else None, order_id))
                audit(db, merchant, 'MERCHANT_' + target, order_id)
                return owned(db, order_id, merchant)
        if action == 'checkout':
            return payments.create(order_id, merchant)
        if action == 'capture':
            return payments.capture(order_id, merchant)
        if action == 'verify':
            return payments.verify(order_id, merchant)
        raise HTTPException(404, 'Unknown action.')

    @app.get('/api/audit')
    def events(user=Depends(session)):
        with database.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM audit WHERE merchant_id=? ORDER BY id DESC LIMIT 100', (user['merchant_id'],))]

    @app.post('/api/webhooks/paypal')
    def webhook(request: Request, event: dict):
        if not isinstance(event.get('id'), str) or not 1 <= len(event['id']) <= 200:
            raise HTTPException(400, 'Invalid webhook event ID.')
        paypal.verify_webhook(request.headers, event)
        if not isinstance(event.get('event_type'), str) or not isinstance(event.get('resource'), dict):
            raise HTTPException(400, 'Invalid webhook structure.')
        with database.connect() as db:
            if db.execute('SELECT 1 FROM webhook_events WHERE id=?', (event['id'],)).fetchone():
                return {'duplicate': True}
            resource = event.get('resource', {})
            supplemental = resource.get('supplementary_data') or {}
            related = supplemental.get('related_ids', {}) if isinstance(supplemental, dict) else {}
            if not isinstance(related, dict):
                raise HTTPException(400, 'Invalid webhook related identifiers.')
            remote_id = related.get('order_id') or (resource.get('id') if event.get('event_type', '').startswith('CHECKOUT.ORDER.') else None)
            if remote_id is not None:
                paypal.valid_id(remote_id)
            row = db.execute('SELECT * FROM orders WHERE paypal_id=?', (remote_id,)).fetchone()
        remote = paypal.get(remote_id) if row else None
        with database.connect(write=True) as db:
            if db.execute('SELECT 1 FROM webhook_events WHERE id=?', (event['id'],)).fetchone():
                return {'duplicate': True}
            if row:
                order = owned(db, row['id'], row['merchant_id'])
                payments.apply_verified(db, order, remote)
                audit(db, row['merchant_id'], 'VERIFIED_WEBHOOK_PROCESSED', row['id'])
            db.execute('INSERT INTO webhook_events VALUES(?,?,?)', (event['id'], str(event.get('event_type', ''))[:120], now()))
        return {'accepted': True, 'matched': bool(row)}

    @app.get('/health')
    def health():
        return {'status': 'ok', 'mode': 'sandbox', 'integration_verification': 'pending operator validation'}

    @app.get('/')
    @app.get('/checkout/return')
    @app.get('/checkout/cancel')
    def index(request: Request):
        # Redirects do not mutate financial state. Merchant explicitly initiates server capture.
        if settings.shopping_enabled and request.url.path.startswith('/checkout/'):
            return FileResponse(ROOT / 'app' / 'static' / 'shopping-return.html')
        return FileResponse(ROOT / 'app' / 'static' / 'index.html')

    app.mount('/static', StaticFiles(directory=ROOT / 'app' / 'static'), name='static')
    return app
