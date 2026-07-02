import time
import logging
import threading
from datetime import datetime

from models import db, PriceWatch
from scrapers import scrape_amazon
from notifications import send_pushover_notification

CHECK_INTERVAL_SECONDS = 24 * 60 * 60


def run_price_checks(app):
    """Check every watched price, notify on drops, and record the latest price."""
    with app.app_context():
        for watch in PriceWatch.query.all():
            try:
                data = scrape_amazon(watch.amazon_url)
            except Exception as e:
                watch.last_error = f'Fetch failed: {e}'[:300]
                watch.last_checked_at = datetime.now()
                db.session.commit()
                time.sleep(2)
                continue
            if not data or data.get('price') is None:
                watch.last_error = 'Could not read price from page'
                watch.last_checked_at = datetime.now()
                db.session.commit()
                time.sleep(2)
                continue

            new_price = data['price']
            if watch.current_price is not None and new_price < watch.current_price:
                currency = watch.currency or ''
                send_pushover_notification(
                    title='Price drop!',
                    message=f'{watch.title}: {currency}{watch.current_price:g} → {currency}{new_price:g}',
                    url=watch.amazon_url,
                )

            watch.current_price = new_price
            if data.get('currency'):
                watch.currency = data['currency']
            watch.last_checked_at = datetime.now()
            watch.last_error = None
            db.session.commit()

            time.sleep(2)


def start_price_watch_scheduler(app):
    """Start a daemon thread that checks prices once a day.

    The caller is responsible for only invoking this in the process that
    serves requests — see app.py, which skips the debug reloader's parent
    process (it would otherwise run a second, duplicate scheduler)."""
    def loop():
        while True:
            try:
                run_price_checks(app)
            except Exception:
                logging.warning('Price watch check failed', exc_info=True)
            time.sleep(CHECK_INTERVAL_SECONDS)

    threading.Thread(target=loop, daemon=True).start()
