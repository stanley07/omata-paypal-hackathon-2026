import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    database: Path = field(default_factory=lambda: ROOT / 'data' / 'omata.db')
    base_url: str = field(default_factory=lambda: os.getenv('APP_BASE_URL', 'http://127.0.0.1:8000').rstrip('/'))
    paypal_enabled: bool = field(default_factory=lambda: os.getenv('PAYPAL_ENABLED') == 'true')
    client_id: str = field(default_factory=lambda: os.getenv('PAYPAL_CLIENT_ID', ''), repr=False)
    client_secret: str = field(default_factory=lambda: os.getenv('PAYPAL_CLIENT_SECRET', ''), repr=False)
    webhook_id: str = field(default_factory=lambda: os.getenv('PAYPAL_WEBHOOK_ID', ''), repr=False)
    cookie_secure: bool = field(default_factory=lambda: os.getenv('COOKIE_SECURE') == 'true')
    ai_provider: str = field(default_factory=lambda: os.getenv('AI_PROVIDER', 'deterministic'))
    ai_enabled: bool = field(default_factory=lambda: os.getenv('AI_ENABLED') == 'true')
    ai_endpoint: str = field(default_factory=lambda: os.getenv('AI_ENDPOINT', ''))
    ai_model: str = field(default_factory=lambda: os.getenv('AI_MODEL', ''))
    ai_key: str = field(default_factory=lambda: os.getenv('AI_API_KEY', ''), repr=False)

    def validate(self):
        url = urlparse(self.base_url)
        if url.scheme not in ('http', 'https') or not url.hostname or url.query or url.fragment or url.username or url.path:
            raise ValueError('APP_BASE_URL must be an origin without a path')
        if url.scheme != 'https' and url.hostname not in ('localhost', '127.0.0.1'):
            raise ValueError('HTTP is restricted to loopback')
        if url.scheme == 'https' and not self.cookie_secure:
            raise ValueError('HTTPS requires COOKIE_SECURE=true')
        if not self.database.resolve().is_relative_to(ROOT):
            raise ValueError('Database must remain inside this project')
