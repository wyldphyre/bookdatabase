import time
import logging
import threading
from datetime import datetime

from models import db, PriceWatch
from scrapers import scrape_amazon, ScrapeBlockedError
from notifications import send_pushover_notification

CHECK_INTERVAL_SECONDS = 24 * 60 * 60

logger = logging.getLogger(__name__)


def run_price_checks(app):
    """Check every watched price, notify on drops, and record the latest price."""
    with app.app_context():
        watches = PriceWatch.query.all()
        logger.info('Price check: checking %d watched price(s)', len(watches))
        checked = drops = failed = 0
        for watch in watches:
            try:
                data = scrape_amazon(watch.amazon_url)
            except ScrapeBlockedError as e:
                # Amazon is turning us away; the remaining watches would all hit
                # the same wall, so record why and leave the rest for next run.
                watch.last_error = str(e)[:300]
                watch.last_checked_at = datetime.now()
                db.session.commit()
                logger.warning('Price check stopped after %d of %d: %s', checked, len(watches), e)
                return
            except Exception as e:
                logger.warning('Price check for %r (%s) failed', watch.title, watch.amazon_url, exc_info=True)
                failed += 1
                watch.last_error = f'Fetch failed: {e}'[:300]
                watch.last_checked_at = datetime.now()
                db.session.commit()
                time.sleep(2)
                continue
            if not data or data.get('price') is None:
                logger.warning('Price check for %r (%s): no price found on the page', watch.title, watch.amazon_url)
                failed += 1
                watch.last_error = 'Could not read price from page'
                watch.last_checked_at = datetime.now()
                db.session.commit()
                time.sleep(2)
                continue

            new_price = data['price']
            # Measure from the price you were last told about, not the last
            # price seen. Previously the alert's success was ignored and the
            # new price recorded regardless, so a drop that couldn't be sent
            # (Pushover misconfigured, or down) was absorbed for good — the
            # next check compared against the already-lower price and stayed
            # silent. Now an unsent drop stays pending and is retried.
            reference = watch.notified_price if watch.notified_price is not None else watch.current_price
            if reference is not None and new_price < reference:
                currency = watch.currency or ''
                sent = send_pushover_notification(
                    title='Price drop!',
                    message=f'{watch.title}: {currency}{reference:g} → {currency}{new_price:g}',
                    url=watch.amazon_url,
                )
                drops += 1
                logger.info('Price drop for %r: %s%g -> %s%g (%s)', watch.title, currency, reference,
                            currency, new_price, 'notified' if sent else 'notification not sent, will retry')
                if sent:
                    watch.notified_price = new_price
            else:
                # A rise or no change. Rises aren't announced, but they move
                # the baseline so a later fall is measured from the higher
                # price — the same behaviour as before. This also clears a
                # pending drop the price has since recovered from.
                watch.notified_price = new_price

            watch.current_price = new_price
            if data.get('currency'):
                watch.currency = data['currency']
            watch.last_checked_at = datetime.now()
            watch.last_error = None
            db.session.commit()
            checked += 1
            logger.debug('Price check for %r: %s%g', watch.title, watch.currency or '', new_price)

            time.sleep(2)
        logger.info('Price check finished: %d checked, %d drop(s), %d failed', checked, drops, failed)


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
                logger.exception('Price check failed')
            time.sleep(CHECK_INTERVAL_SECONDS)

    threading.Thread(target=loop, daemon=True, name='price-watch').start()
    logger.info('Price watch scheduler started (checks every %d hours)', CHECK_INTERVAL_SECONDS // 3600)
