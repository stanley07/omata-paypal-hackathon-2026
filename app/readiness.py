"""Read-only local readiness checks; speech inference requires --transcribe."""
import argparse
import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from fastapi import HTTPException

from app.config import Settings
from app.whatsapp import MAX_AUDIO, Speech, WhatsApp


def inspect(settings):
    """No migrations, seeding, authentication requests or outbound messages."""
    checks = {}
    try:
        settings.validate()
        checks['settings_valid'] = True
    except ValueError:
        checks['settings_valid'] = False
    checks['customer_shopping_enabled'] = settings.shopping_enabled
    checks['https_origin_and_secure_cookie'] = settings.base_url.startswith('https://') and settings.cookie_secure
    whatsapp = WhatsApp(settings)
    try:
        whatsapp.enabled()
        checks['whatsapp_configuration'] = True
    except HTTPException:
        checks['whatsapp_configuration'] = False
    finally:
        whatsapp.client.close()
    checks['graph_version_present'] = bool(settings.whatsapp_version)
    checks['speech_configuration'] = bool(settings.speech_enabled and settings.speech_key and
                                          settings.speech_model and settings.speech_endpoint.startswith('https://'))
    checks['groq_configuration'] = bool(settings.groq_enabled and settings.groq_key and settings.groq_model)
    checks['paypal_configuration'] = bool(settings.paypal_enabled and settings.client_id and settings.client_secret)
    catalogs = []
    try:
        with closing(sqlite3.connect(settings.database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            checks['coordinator_exists'] = bool(db.execute('SELECT 1 FROM merchants WHERE id=?',
                                                          (settings.whatsapp_merchant_id,)).fetchone())
            for merchant in ('sample-sunrise', 'sample-river', 'sample-copper'):
                active, milk = db.execute('SELECT COUNT(*), COALESCE(SUM(category=\'milk\' AND stock>=2),0) '
                                         'FROM catalog_products WHERE merchant_id=? AND active=1', (merchant,)).fetchone()
                catalogs.append({'merchant': merchant, 'active_products': active, 'milk_for_two_available': bool(milk)})
        checks['three_demo_catalogs_available'] = all(c['active_products'] >= 3 and c['milk_for_two_available'] for c in catalogs)
    except sqlite3.Error:
        checks['catalog_database_readable'] = False
        checks['three_demo_catalogs_available'] = False
    return {'checks': checks, 'catalogs': catalogs, 'configuration_ready': all(checks.values()),
            'live_whatsapp_and_paypal': 'not tested', 'paypal_webhook_id_present': bool(settings.webhook_id)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, help='Explicit private env file; existing process variables take precedence')
    parser.add_argument('--transcribe', type=Path, help='Explicitly authorize one live speech call using an M4A recording')
    args = parser.parse_args()
    # Provider errors and HTTP logging must not expose URLs, credentials or audio text.
    logging.disable(logging.CRITICAL)
    presence = {}
    if args.env_file:
        if not args.env_file.is_file():
            parser.exit(2, 'Environment file is missing.\n')
        presence = {key: 'present' if value else 'empty' for key, value in dotenv_values(args.env_file).items()}
        load_dotenv(args.env_file, override=False)
    settings = Settings()
    report = inspect(settings)
    report['env_file_presence'] = presence
    if args.transcribe:
        try:
            if args.transcribe.suffix.lower() != '.m4a' or not 0 < args.transcribe.stat().st_size <= MAX_AUDIO:
                raise ValueError()
            Speech(settings).transcribe(args.transcribe.read_bytes(), 'audio/mp4')
            report['actual_speech_adapter'] = 'PASS; nonempty transcript; content withheld'
        except (HTTPException, OSError, ValueError):
            report['actual_speech_adapter'] = 'FAIL; details withheld'
    print(json.dumps(report, indent=2))
    return 0 if report['configuration_ready'] and not report.get('actual_speech_adapter', '').startswith('FAIL') else 1


if __name__ == '__main__':
    raise SystemExit(main())
