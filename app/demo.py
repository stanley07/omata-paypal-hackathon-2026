"""Local test-input shopping runner. No Meta webhook or WhatsApp network transport."""
import argparse
import hashlib
import json
import logging
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

from app.catalog import seed_catalogs
from app.config import ROOT, Settings
from app.db import Database, now
from app.payments import Payments
from app.paypal import PayPal
from app.shopping import CustomerShopping, GroqIntent
from app.whatsapp import MAX_AUDIO, Speech

RUNS = ROOT / 'artifacts' / 'shopping-demo'
PHONE, CUSTOMER = 'LOCAL_TEST_INPUT', 'LOCAL_DEMO_CUSTOMER'


def run_path(run_id):
    if str(uuid.UUID(run_id)) != run_id:
        raise ValueError('Invalid run ID')
    path = RUNS / run_id
    # Never accept arbitrary DB paths or follow a redirected demo directory/file.
    if RUNS.resolve() != RUNS.absolute() or path.resolve() != path.absolute():
        raise ValueError('Redirected run directory')
    database = path / 'shopping.db'
    if database.exists() and (database.is_symlink() or database.stat().st_nlink != 1):
        raise ValueError('Redirected database')
    return path


def initialize():
    run_id = str(uuid.uuid4())
    path = run_path(run_id)
    path.mkdir(parents=True, exist_ok=False)
    database = Database(path / 'shopping.db')
    seed_catalogs(database)
    with database.connect(write=True) as db:
        db.executescript('''
            CREATE TABLE demo_identity (run_id TEXT PRIMARY KEY);
            CREATE TABLE demo_input (id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,
                                     received REAL NOT NULL, finished INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE demo_evidence (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
        ''')
        db.execute('INSERT INTO demo_identity VALUES(?)', (run_id,))
    return run_id


class LocalTestTransport:
    """Deliberately has no HTTP client. Sender labels are not phone numbers."""
    def __init__(self, settings):
        self.settings = settings
        self.replies = []

    def audio(self, content):
        path = Path(content)
        mime = {'.m4a': 'audio/mp4', '.ogg': 'audio/ogg', '.mp3': 'audio/mpeg'}.get(path.suffix.lower())
        if not mime or not 0 < path.stat().st_size <= MAX_AUDIO:
            raise ValueError('Unsupported local audio')
        audio = path.read_bytes()
        if not 0 < len(audio) <= MAX_AUDIO:
            raise ValueError('Unsupported local audio')
        return audio, mime

    def send_text(self, sender, body):
        if sender != CUSTOMER:
            raise ValueError('Only the local demo customer is allowed')
        self.replies.append(body)
        return 'local-output-' + str(uuid.uuid4())


class Demo:
    def __init__(self, run_id, settings=None, paypal=None, intent=None, speech=None):
        self.path = run_path(run_id)
        if not (self.path / 'shopping.db').is_file():
            raise ValueError('Initialize a fresh demo first')
        self.database = Database(self.path / 'shopping.db')
        with self.database.connect() as db:
            if db.execute('SELECT run_id FROM demo_identity').fetchone()['run_id'] != run_id:
                raise ValueError('Run identity mismatch')
        self.settings = settings or Settings()
        self.settings.database = self.path / 'shopping.db'
        self.settings.base_url = 'http://127.0.0.1:8000'
        self.settings.cookie_secure = False
        self.settings.whatsapp_enabled = False
        self.settings.whatsapp_token = self.settings.whatsapp_secret = self.settings.whatsapp_verify_token = ''
        self.settings.whatsapp_phone_id = PHONE
        self.settings.whatsapp_merchant_id = 'sample-sunrise'
        self.settings.validate()
        self.paypal = paypal or PayPal(self.settings)
        self.local = LocalTestTransport(self.settings)
        self.engine = CustomerShopping(self.database, self.local, speech or Speech(self.settings),
                                       intent or GroqIntent(self.settings), Payments(self.database, self.paypal))

    def snapshot(self):
        with self.database.connect() as db:
            session = db.execute('SELECT * FROM shopping_sessions WHERE phone_id=? AND sender=?', (PHONE, CUSTOMER)).fetchone()
            order = db.execute('SELECT * FROM orders WHERE id=?', (session['order_id'],)).fetchone() if session else None
            selected = json.loads(session['selected']) if session and session['selected'] else None
            offers = json.loads(session['offers']) if session else []
            return {'transport': 'LOCAL_TEST_INPUT; no WhatsApp delivery', 'timestamp': now(),
                    'payment_environment': 'PayPal Sandbox',
                    'state': session['state'] if session else 'NEW',
                    'quantity': session['quantity'] if session else None,
                    'offer_product_ids': [p['id'] for p in offers],
                    'selected_product_id': selected['id'] if selected else None,
                    'selected_option': next((i for i, p in enumerate(offers, 1) if selected and p['id'] == selected['id']), None),
                    'order': {k: order[k] for k in ('id', 'merchant_id', 'quantity', 'total', 'currency',
                             'paypal_id', 'capture_id', 'verified_at', 'status')} if order else None,
                    'pending_input': [r['id'] for r in db.execute('SELECT id FROM demo_input WHERE finished=0')],
                    'human_review_count': db.execute('SELECT COUNT(*) FROM shopping_reviews').fetchone()[0]}

    def export(self):
        with self.database.connect() as db:
            lines = [r['payload'] for r in db.execute('SELECT payload FROM demo_evidence ORDER BY id')]
        target = self.path / 'evidence.jsonl'
        if target.is_symlink() or (target.exists() and target.stat().st_nlink != 1):
            raise ValueError('Redirected evidence file')
        target.write_text('\n'.join(lines) + ('\n' if lines else ''), encoding='utf-8')

    def submit(self, content=None, audio=False, retry=False):
        self.local.replies.clear()
        with self.database.connect(write=True) as db:
            pending = db.execute('SELECT * FROM demo_input WHERE finished=0').fetchone()
            if retry:
                if not pending:
                    raise ValueError('No pending input to retry')
                row = dict(pending)
            else:
                if pending:
                    raise ValueError('Pending outcome: inspect and reconcile before explicit retry')
                if not isinstance(content, str) or not content.strip() or (not audio and len(content) > 2000):
                    raise ValueError('Input required')
                row = {'id': 'local-input-' + str(uuid.uuid4()), 'kind': 'audio' if audio else 'text',
                       'content': str(Path(content).resolve()) if audio else content, 'received': time.time()}
                db.execute('INSERT INTO demo_input(id,kind,content,received) VALUES(:id,:kind,:content,:received)', row)
        outcome = 'FAILED_OR_UNCERTAIN'
        try:
            digest = hashlib.sha256((row['kind'] + '\0' + row['content']).encode()).hexdigest()
            # Call the existing state machine directly; never fabricate/sign a Meta webhook.
            self.engine.process(row['id'], PHONE, CUSTOMER, row['received'], row['kind'], row['content'], digest)
            outcome = 'PROCESSED'
        finally:
            evidence = self.snapshot()
            evidence.update(input_id=row['id'], input_kind=row['kind'], outcome=outcome, explicit_retry=retry)
            with self.database.connect(write=True) as db:
                if outcome == 'PROCESSED':
                    db.execute('UPDATE demo_input SET finished=1 WHERE id=?', (row['id'],))
                    evidence['pending_input'] = []
                db.execute('INSERT INTO demo_evidence(payload) VALUES(?)', (json.dumps(evidence),))
            self.export()
        return self.local.replies

    def close(self):
        self.paypal.client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['init', 'input', 'inspect', 'retry'])
    parser.add_argument('--run', help='UUID printed by init; arbitrary paths are not accepted')
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--allow-sandbox', action='store_true', help='Explicitly enable provider calls for this invocation')
    parser.add_argument('--reconciled', action='store_true', help='Operator has reviewed the pending outcome before retry')
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument('--text')
    inputs.add_argument('--audio', type=Path)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    demo = None
    try:
        if args.action == 'init':
            print(initialize())
            return 0
        if not args.run:
            parser.error('--run is required')
        if args.action in ('input', 'retry'):
            if not args.allow_sandbox or (args.action == 'retry' and not args.reconciled):
                parser.error('Provider calls require --allow-sandbox; retry also requires --reconciled')
            if not args.env_file.is_file():
                raise ValueError('Private environment file missing')
            load_dotenv(args.env_file, override=False)
        demo = Demo(args.run)
        if args.action == 'inspect':
            demo.export()
            print(json.dumps(demo.snapshot(), indent=2))
        else:
            content = str(args.audio) if args.audio else args.text
            for reply in demo.submit(content, audio=bool(args.audio), retry=args.action == 'retry'):
                print('LOCAL TEST OUTPUT (not WhatsApp):\n' + reply)
            print('Stopped. No buyer approval is assumed. Next input requires a separate operator command.')
        return 0
    except (Exception, KeyboardInterrupt):
        # Never print provider exceptions, configuration values or customer input.
        print('STOPPED: input/configuration failure or uncertain provider outcome. Inspect this run; do not start a replacement order.')
        return 1
    finally:
        if demo:
            demo.close()


if __name__ == '__main__':
    raise SystemExit(main())
