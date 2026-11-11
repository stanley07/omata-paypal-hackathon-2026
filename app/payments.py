import time
import uuid
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException

from app.db import audit, now


def owned(db, order_id, merchant):
    order = db.execute('SELECT * FROM orders WHERE id=? AND merchant_id=?', (order_id, merchant)).fetchone()
    if not order:
        raise HTTPException(404, 'Order not found.')
    return dict(order)


class Payments:
    def __init__(self, database, paypal):
        self.database = database
        self.paypal = paypal

    def claim(self, order_id, merchant, kind):
        with self.database.connect(write=True) as db:
            order = owned(db, order_id, merchant)
            if kind == 'create' and order['paypal_id']:
                return order, None
            if kind == 'capture' and order['status'] == 'COMPLETED':
                return order, None
            expected = 'APPROVED' if kind == 'create' else 'CHECKOUT_CREATED'
            if order['status'] != expected:
                raise HTTPException(409, f'Order must be {expected} first.')
            op = db.execute('SELECT * FROM operations WHERE order_id=? AND kind=?', (order_id, kind)).fetchone()
            current = time.time()
            if op and op['lease_until'] > current:
                raise HTTPException(409, 'Payment operation in progress. Wait before retrying.')
            # PayPal retention is finite. Never blindly repeat an uncertain create after expiry.
            if op and kind == 'create' and current - op['started'] > 5 * 3600:
                raise HTTPException(409, 'Uncertain create is older than 5 hours. Operator reconciliation required; automatic retry blocked.')
            request_id = op['request_id'] if op else str(uuid.uuid4())
            db.execute('INSERT INTO operations(order_id,kind,request_id,started,lease_until) VALUES(?,?,?,?,?) '
                       'ON CONFLICT(order_id,kind) DO UPDATE SET lease_until=excluded.lease_until',
                       (order_id, kind, request_id, current, current + 180))
            audit(db, merchant, f'{kind.upper()}_REQUESTED', order_id)
        return order, request_id

    def release(self, order_id, kind, success):
        with self.database.connect(write=True) as db:
            db.execute('UPDATE operations SET lease_until=0, done=? WHERE order_id=? AND kind=?',
                       (int(success), order_id, kind))

    def create(self, order_id, merchant):
        self.paypal.enabled()
        order, key = self.claim(order_id, merchant, 'create')
        if not key:
            return order
        success = False
        try:
            result = self.paypal.create(order, key)
            paypal_id = self.paypal.valid_id(result.get('id'))
            link = self.paypal.approval_link(result)
            with self.database.connect(write=True) as db:
                db.execute("UPDATE orders SET paypal_id=?,approval_url=?,status='CHECKOUT_CREATED' WHERE id=? AND status='APPROVED'",
                           (paypal_id, link, order_id))
                audit(db, merchant, 'SANDBOX_CHECKOUT_CREATED', order_id)
            success = True
        finally:
            self.release(order_id, 'create', success)
        with self.database.connect() as db:
            return owned(db, order_id, merchant)

    def apply_verified(self, db, order, remote):
        # Only a fresh server-to-server GET may reach this method.
        if remote.get('id') != order['paypal_id']:
            raise HTTPException(502, 'PayPal order identity mismatch.')
        units = remote.get('purchase_units', [])
        try:
            if len(units) != 1:
                raise ValueError()
            unit = units[0]
            if (unit.get('custom_id') != order['id'] or
                    unit['amount']['currency_code'] != order['currency'] or
                    Decimal(unit['amount']['value']) != Decimal(order['total'])):
                raise ValueError()
            captures = unit.get('payments', {}).get('captures', [])
            if remote.get('status') == 'COMPLETED':
                if len(captures) != 1:
                    raise ValueError()
                capture = captures[0]
                if (capture.get('status') != 'COMPLETED' or capture.get('final_capture') is not True or
                        capture['amount']['currency_code'] != order['currency'] or
                        Decimal(capture['amount']['value']) != Decimal(order['total'])):
                    raise ValueError()
                capture_id = self.paypal.valid_id(capture.get('id'))
                if order['status'] != 'COMPLETED':
                    if order['status'] != 'CHECKOUT_CREATED':
                        raise ValueError()
                    db.execute("UPDATE orders SET status='COMPLETED',capture_id=?,verified_at=? WHERE id=?",
                               (capture_id, now(), order['id']))
                    audit(db, order['merchant_id'], 'PAYMENT_COMPLETED_VERIFIED', order['id'])
            else:
                audit(db, order['merchant_id'], 'PAYMENT_CHECKED_NOT_COMPLETED', order['id'])
        except (KeyError, ValueError, InvalidOperation, TypeError, AttributeError):
            raise HTTPException(502, 'PayPal amount, currency, reference or capture verification failed. Payment not recorded.') from None

    def verify(self, order_id, merchant):
        with self.database.connect() as db:
            order = owned(db, order_id, merchant)
        if not order['paypal_id']:
            raise HTTPException(409, 'Create an approved sandbox checkout first.')
        remote = self.paypal.get(order['paypal_id'])
        with self.database.connect(write=True) as db:
            order = owned(db, order_id, merchant)
            self.apply_verified(db, order, remote)
            return owned(db, order_id, merchant)

    def capture(self, order_id, merchant):
        self.paypal.enabled()
        order, key = self.claim(order_id, merchant, 'capture')
        if not key:
            return order
        success = False
        try:
            remote = self.paypal.get(order['paypal_id'])
            with self.database.connect(write=True) as db:
                self.apply_verified(db, owned(db, order_id, merchant), remote)
            if remote.get('status') != 'COMPLETED':
                if remote.get('status') != 'APPROVED':
                    raise HTTPException(409, 'PayPal has not confirmed buyer approval. No capture attempted.')
                try:
                    self.paypal.capture(order['paypal_id'], key)
                except HTTPException:
                    # A timeout may have occurred after PayPal accepted capture.
                    verified = self.verify(order_id, merchant)
                    if verified['status'] != 'COMPLETED':
                        raise
                    success = True
                    return verified
            verified = self.verify(order_id, merchant)
            success = verified['status'] == 'COMPLETED'
            return verified
        finally:
            self.release(order_id, 'capture', success)
