import hashlib
import hmac
import secrets


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ':' + digest


def password_matches(password, stored):
    return hmac.compare_digest(password_hash(password, stored.split(':')[0]), stored)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()
