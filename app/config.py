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
    whatsapp_enabled: bool = field(default_factory=lambda: os.getenv('WHATSAPP_ENABLED') == 'true')
    whatsapp_token: str = field(default_factory=lambda: os.getenv('WHATSAPP_ACCESS_TOKEN', ''), repr=False)
    whatsapp_secret: str = field(default_factory=lambda: os.getenv('WHATSAPP_APP_SECRET', ''), repr=False)
    whatsapp_verify_token: str = field(default_factory=lambda: os.getenv('WHATSAPP_VERIFY_TOKEN', ''), repr=False)
    whatsapp_phone_id: str = field(default_factory=lambda: os.getenv('WHATSAPP_PHONE_NUMBER_ID', ''))
    whatsapp_merchant_id: str = field(default_factory=lambda: os.getenv('WHATSAPP_MERCHANT_ID', ''))
    whatsapp_version: str = field(default_factory=lambda: os.getenv('WHATSAPP_GRAPH_VERSION', ''))
    speech_enabled: bool = field(default_factory=lambda: os.getenv('SPEECH_ENABLED') == 'true')
    speech_endpoint: str = field(default_factory=lambda: os.getenv('SPEECH_ENDPOINT', ''))
    speech_model: str = field(default_factory=lambda: os.getenv('SPEECH_MODEL', ''))
    speech_key: str = field(default_factory=lambda: os.getenv('SPEECH_API_KEY', ''), repr=False)
    shopping_enabled: bool = field(default_factory=lambda: os.getenv('CUSTOMER_SHOPPING_ENABLED') == 'true')
    groq_enabled: bool = field(default_factory=lambda: os.getenv('GROQ_ENABLED') == 'true')
    groq_key: str = field(default_factory=lambda: os.getenv('GROQ_API_KEY', ''), repr=False)
    groq_model: str = field(default_factory=lambda: os.getenv('GROQ_MODEL', ''))

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
