"""Telling you when a book that wasn't out yet is released.

A book with a release date is either a pre-order (owned: you've paid) or one
you're waiting on (not owned: you'll decide whether to buy it once it's out).
Either way you're notified once, on the day. The check itself is one small
query, so it runs hourly and the notification goes out soon after the day
starts, rather than waiting for a once-a-day run to come round.
"""
import time
import logging
import threading
from datetime import datetime, timedelta

from models import db, Book
from notifications import send_pushover_notification

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60 * 60
# Not before this hour (server time), so a release doesn't buzz you at midnight
NOTIFY_FROM_HOUR = 9
# A release missed by longer than this (the app was down, or notifications
# weren't set up) isn't announced late; the dashboard still lists it
NOTIFY_WITHIN_DAYS = 7


def due_release_books(today):
    """Released books that haven't been announced, within the window."""
    return (Book.query
            .filter(Book.release_date.isnot(None),
                    Book.release_date <= today,
                    Book.release_date > today - timedelta(days=NOTIFY_WITHIN_DAYS),
                    Book.release_notified_at.is_(None))
            .order_by(Book.release_date, Book.title)
            .all())


def release_notification(book, today):
    """(title, message, url) for a book that's come out."""
    when = 'today' if book.release_date == today else 'now'
    by = f' by {book.author_names}' if book.author_names else ''
    if book.owned:
        message = f'Your pre-order of {book.title}{by} is out {when}.'
    else:
        message = f'{book.title}{by} is out {when}, if you want to buy it.'
    # A store first, where it can be bought (or downloaded); Goodreads otherwise
    url = book.amazon_url or book.kobo_url or book.goodreads_url
    return f'Out {when}: {book.title}', message, url


def notify_releases(app, now=None):
    """Announce any books released today (or in the last few days and missed).
    Returns how many notifications were sent."""
    now = now or datetime.now()
    if now.hour < NOTIFY_FROM_HOUR:
        return 0
    with app.app_context():
        sent = 0
        for book in due_release_books(now.date()):
            title, message, url = release_notification(book, now.date())
            if send_pushover_notification(title=title, message=message, url=url):
                book.release_notified_at = now
                db.session.commit()
                sent += 1
                logger.info('Release notified: %r (%s)', book.title,
                            'pre-order' if book.owned else 'not owned')
            # Not sent (Pushover not set up, or it failed): left for the next check
        return sent


def start_release_reminder_scheduler(app):
    """Start the daemon thread that checks for released books every hour.

    As with the other schedulers, the caller only starts this in the process
    that serves requests."""
    def loop():
        while True:
            try:
                notify_releases(app)
            except Exception:
                logger.exception('Release check failed')
            time.sleep(CHECK_INTERVAL_SECONDS)

    threading.Thread(target=loop, daemon=True, name='release-reminders').start()
    logger.info('Release reminder scheduler started (checks hourly, notifies from %02d:00)',
                NOTIFY_FROM_HOUR)
