"""Fictional, isolated demo catalogs. Never imports operational merchant data."""
import getpass
import secrets

from app.config import Settings
from app.db import Database, now
from app.security import password_hash

SAMPLES = (
    ('sample-sunrise', 'Sunrise Pantry (fictional sample)', 'sunrise@example.test', (
        ('rice', 'Parboiled rice, 5 kg bag', 1250, 20),
        ('milk', 'Whole milk, 1 litre carton', 320, 30),
        ('coffee', 'Ground coffee, 250 g pack', 850, 12))),
    ('sample-river', 'River Market (fictional sample)', 'river@example.test', (
        ('rice', 'Jasmine rice, 5 kg bag', 1500, 15),
        ('milk', 'Low-fat milk, 1 litre carton', 290, 25),
        ('coffee', 'Coffee beans, 250 g pack', 1050, 10))),
    ('sample-copper', 'Copper Basket (fictional sample)', 'copper@example.test', (
        ('rice', 'Brown rice, 5 kg bag', 1400, 18),
        ('milk', 'Lactose-free milk, 1 litre carton', 410, 20),
        ('coffee', 'Decaf ground coffee, 250 g pack', 980, 8))),
)


def seed_catalogs(database):
    with database.connect(write=True) as db:
        for mid, name, email, products in SAMPLES:
            if not db.execute('SELECT 1 FROM merchants WHERE id=?', (mid,)).fetchone():
                db.execute('INSERT INTO merchants VALUES(?,?,?,?,?)',
                           (mid, email, name, password_hash(secrets.token_urlsafe(40)), now()))
            for category, product, price, stock in products:
                db.execute('INSERT OR IGNORE INTO catalog_products VALUES(?,?,?,?,?,?,1)',
                           (mid + '-' + category, mid, product, category, price, stock))


if __name__ == '__main__':
    settings = Settings()
    settings.validate()
    database = Database(settings.database)
    seed_catalogs(database)
    password = getpass.getpass('Set a local password for the three fictional sample accounts (12+ characters): ')
    if not 12 <= len(password) <= 128:
        raise SystemExit('Password must be 12-128 characters. Catalogs seeded; passwords unchanged.')
    with database.connect(write=True) as db:
        for mid, name, email, _ in SAMPLES:
            db.execute('UPDATE merchants SET password=? WHERE id=?', (password_hash(password), mid))
            print(f'{name}: {email} / merchant ID {mid}')
