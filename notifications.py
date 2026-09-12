import os
import logging
from urllib.parse import urlparse

import requests

from models import get_setting

PRIORITY_SETTING_KEY = 'pushover_priority'
DEFAULT_PRIORITY = 0

# Priority levels offered on the System page. Pushover also has Emergency (2),
# deliberately left out: it re-alerts until acknowledged on the device and is
# rejected by the API unless retry/expire are sent alongside it.
PUSHOVER_PRIORITIES = [
    (-2, 'Lowest — no alert'),
    (-1, 'Low — no sound or vibration'),
    (0, 'Normal'),
    (1, 'High — bypasses quiet hours'),
]
VALID_PRIORITIES = {value for value, _ in PUSHOVER_PRIORITIES}


def get_pushover_priority():
    """The configured notification priority, falling back to Normal if it is
    unset, unparseable, or out of range."""
    try:
        priority = int(get_setting(PRIORITY_SETTING_KEY))
    except (TypeError, ValueError):
        return DEFAULT_PRIORITY
    return priority if priority in VALID_PRIORITIES else DEFAULT_PRIORITY


def send_pushover_notification(title, message, url=None):
    """Send a push notification via Pushover. Returns True on success."""
    user_key = os.environ.get('PUSHOVER_USER_KEY')
    app_token = os.environ.get('PUSHOVER_APP_TOKEN')
    if not user_key or not app_token:
        logging.warning('Pushover not configured (PUSHOVER_USER_KEY/PUSHOVER_APP_TOKEN) — skipping notification')
        return False

    payload = {
        'token': app_token,
        'user': user_key,
        'title': title,
        'message': message,
        'priority': get_pushover_priority(),
    }
    if url:
        payload['url'] = url
        # The series monitor prefers a Goodreads link and falls back to Amazon,
        # so the label has to follow the link rather than assume the store.
        host = (urlparse(url).hostname or '').lower()
        if 'goodreads.' in host:
            payload['url_title'] = 'View on Goodreads'
        elif 'amazon.' in host:
            payload['url_title'] = 'View on Amazon'
        else:
            payload['url_title'] = 'View book'

    try:
        response = requests.post('https://api.pushover.net/1/messages.json', data=payload, timeout=10)
        result = response.json()
        if result.get('status') != 1:
            logging.warning('Pushover rejected the notification: %s', result.get('errors') or response.text)
            return False
        return True
    except Exception:
        logging.warning('Pushover notification failed', exc_info=True)
        return False
