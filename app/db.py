import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


SCHEMA = '''
CREATE TABLE IF NOT EXISTS merchants (
 id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
 password TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
 token TEXT PRIMARY KEY, merchant_id TEXT NOT NULL REFERENCES merchants(id),
 csrf TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts (key TEXT PRIMARY KEY, failures INTEGER NOT NULL, until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL REFERENCES merchants(id),
 description TEXT NOT NULL, quantity TEXT NOT NULL, unit_price TEXT NOT NULL,
 currency TEXT NOT NULL, total TEXT NOT NULL, customer TEXT NOT NULL,
 interpretation TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('DRAFT','REJECTED','APPROVED','CHECKOUT_CREATED','COMPLETED')),
 created_at TEXT NOT NULL, approved_at TEXT, paypal_id TEXT UNIQUE,
 approval_url TEXT, capture_id TEXT UNIQUE, verified_at TEXT);
CREATE TABLE IF NOT EXISTS operations (
 order_id TEXT NOT NULL REFERENCES orders(id), kind TEXT NOT NULL,
 request_id TEXT UNIQUE NOT NULL, started REAL NOT NULL,
 lease_until REAL NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(order_id,kind));
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id TEXT NOT NULL,
 order_id TEXT, event TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS webhook_events (id TEXT PRIMARY KEY, event_type TEXT NOT NULL, processed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS whatsapp_messages (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL REFERENCES merchants(id),
 sender TEXT NOT NULL, received REAL NOT NULL, media_id TEXT NOT NULL,
 transcript TEXT, order_id TEXT UNIQUE REFERENCES orders(id),
 lease_until REAL NOT NULL DEFAULT 0, failure TEXT,
 claim_token TEXT, phone_id TEXT,
 share_state TEXT NOT NULL DEFAULT 'READY', outbound_id TEXT);
CREATE INDEX IF NOT EXISTS orders_merchant ON orders(merchant_id,created_at);
CREATE INDEX IF NOT EXISTS audit_merchant ON audit(merchant_id,id);
CREATE TABLE IF NOT EXISTS catalog_products (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL REFERENCES merchants(id),
 name TEXT NOT NULL, category TEXT NOT NULL, price_cents INTEGER NOT NULL CHECK(price_cents>0),
 stock INTEGER NOT NULL CHECK(stock>=0), active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS shopping_sessions (
 phone_id TEXT NOT NULL, sender TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'NEW',
 offers TEXT NOT NULL DEFAULT '[]', quantity INTEGER NOT NULL DEFAULT 1,
 selected TEXT, confirmation TEXT, expires REAL NOT NULL DEFAULT 0,
 order_id TEXT REFERENCES orders(id), lease_until REAL NOT NULL DEFAULT 0, claim_token TEXT,
 PRIMARY KEY(phone_id,sender));
CREATE TABLE IF NOT EXISTS shopping_messages (
 id TEXT PRIMARY KEY, phone_id TEXT NOT NULL, sender TEXT NOT NULL, payload_hash TEXT NOT NULL,
 received REAL NOT NULL, transcript TEXT, response TEXT, send_state TEXT NOT NULL DEFAULT 'NEW',
 outbound_id TEXT, work_order_id TEXT REFERENCES orders(id), work_action TEXT);
CREATE TABLE IF NOT EXISTS shopping_reviews (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL REFERENCES merchants(id),
 message_id TEXT UNIQUE NOT NULL REFERENCES shopping_messages(id), sender TEXT NOT NULL,
 request TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'NEEDS_REVIEW', created_at TEXT NOT NULL);
'''


class Database:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            # Upgrade installations created before voice worker fencing was added.
            columns = {row['name'] for row in db.execute('PRAGMA table_info(whatsapp_messages)')}
            for column in ('claim_token', 'phone_id'):
                if column not in columns:
                    db.execute(f'ALTER TABLE whatsapp_messages ADD COLUMN {column} TEXT')

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


def audit(db, merchant, event, order=None):
    db.execute('INSERT INTO audit(merchant_id,order_id,event,created_at) VALUES(?,?,?,?)',
               (merchant, order, event, now()))
