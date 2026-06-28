import os
import logging
import requests


def send_pushover_notification(title, message, url=None):
    """Send a push notification via Pushover. Returns True on success."""
    user_key = os.environ.get('PUSHOVER_USER_KEY')
    app_token = os.environ.get('PUSHOVER_APP_TOKEN')
    if not user_key or not app_token:
        logging.warning('Pushover not configured (PUSHOVER_USER_KEY/PUSHOVER_APP_TOKEN) — skipping notification')
        return False

    payload = {'token': app_token, 'user': user_key, 'title': title, 'message': message}
    if url:
        payload['url'] = url
        payload['url_title'] = 'View on Amazon'

    try:
        requests.post('https://api.pushover.net/1/messages.json', data=payload, timeout=10)
        return True
    except Exception:
        logging.warning('Pushover notification failed', exc_info=True)
        return False
