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
CREATE INDEX IF NOT EXISTS orders_merchant ON orders(merchant_id,created_at);
CREATE INDEX IF NOT EXISTS audit_merchant ON audit(merchant_id,id);
'''


class Database:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

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
