"""Load operator-owned local settings; never used on Azure or shipped with secrets."""
import json
import logging
import os
from pathlib import Path

ALLOWED = {
    'NOTIFICATION_SECRET_KEY', 'NOTIFICATION_PASSWORD_HASH',
    'NOTIFICATION_RECIPIENTS_JSON', 'NOTIFICATION_COOKIE_SECURE', 'SMS_ENABLED', 'PURESMS_API_KEY', 'PURESMS_SENDER',
}


def load_local_settings(env=None, path=None, dotenv_path=None):
    env = os.environ if env is None else env
    if env.get('WEBSITE_HOSTNAME'):
        return
    # Local .env only: Azure keeps using its managed application settings.
    from dotenv import dotenv_values
    source = Path(dotenv_path) if dotenv_path else (Path(path).parent if path else Path(__file__).parent) / '.env'
    if source.exists():
        for key, value in dotenv_values(source, interpolate=False).items():
            if value is not None:
                env.setdefault(key, value)
    # Legacy JSON remains a fallback for existing installations.
    path = Path(path) if path else Path(__file__).parent / '.local-notifications.json'
    if not path.exists():
        return
    try:
        settings = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(settings, dict) or any(k not in ALLOWED or not isinstance(v, str) for k, v in settings.items()):
            raise ValueError('invalid local settings')
        # Explicit shell configuration takes precedence over local settings.
        for key, value in settings.items():
            env.setdefault(key, value)
    except (OSError, ValueError):
        logging.getLogger(__name__).warning('Local notification settings could not be loaded; check .local-notifications.json.')
