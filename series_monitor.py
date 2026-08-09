"""Weekly checks of opted-in series for newly released books.

Deliberately unhurried: one series a minute, and a series is only revisited
once a week. The loop picks whichever monitored series has been waiting
longest rather than sweeping a list, so it paces itself, spreads load evenly,
and resumes where it left off after a restart without storing any progress.
"""
import re
import time
import logging
import threading
from datetime import datetime, timedelta

from models import db, Series, SeriesRelease
from scrapers import (scrape_goodreads_series_detail, scrape_goodreads,
                      search_goodreads_for_book, ScrapeBlockedError)
from notifications import send_pushover_notification

# How stale a series' last check must be before it's due again.
CHECK_INTERVAL_SECONDS = 7 * 24 * 60 * 60
# Gap between checking one series and the next.
SERIES_SPACING_SECONDS = 60
# After being turned away, wait this long before trying anyone again.
BLOCKED_BACKOFF_SECONDS = 30 * 60
# Books to try when working out a series' Goodreads URL from scratch.
URL_RESOLUTION_ATTEMPTS = 2


def match_key(title):
    """Normalised title, used to tell whether we've already seen an entry.

    Punctuation and case drift between listings, so comparing raw titles would
    re-announce the same book."""
    return re.sub(r'[^a-z0-9]+', ' ', (title or '').lower()).strip()


def resolve_series_url(series):
    """Find a series' Goodreads URL using a book we already own from it.

    Lets monitoring work on series with no external link recorded: search for
    one of its books, open that book's page, and take the series link from it.
    Costs two requests, once — the result is saved to the series."""
    for book in list(series.books)[:URL_RESOLUTION_ATTEMPTS]:
        if not book.title:
            continue
        try:
            book_url = search_goodreads_for_book(book.title, book.author_names)
            if not book_url:
                continue
            data = scrape_goodreads(book_url)
            if data and data.get('series_url'):
                return data['series_url']
        except ScrapeBlockedError:
            raise
        except Exception:
            logging.warning('Series URL lookup failed for %r', series.name, exc_info=True)
    return None


def check_series(series):
    """Check one series and record what's on its page.

    Returns the releases worth telling the user about — empty on the first
    check of a series, which only establishes what was already out."""
    if not series.goodreads_url:
        found = resolve_series_url(series)
        if not found:
            series.last_check_error = 'Could not work out a Goodreads URL for this series'
            series.last_checked_at = datetime.now()
            db.session.commit()
            return []
        series.goodreads_url = found

    detail = scrape_goodreads_series_detail(series.goodreads_url)

    # Keep the series' book count current, as part of the same visit. Only ever
    # revise upwards: a partial parse shouldn't quietly shrink a known count.
    count = detail.get('count')
    if count is not None and (series.number_in_series is None or count > series.number_in_series):
        series.number_in_series = count

    known = {r.match_key for r in series.releases}
    # No releases recorded yet means this is the first look at the series, so
    # everything on the page is backlist rather than news.
    is_baseline_run = not known

    owned_by_key = {match_key(b.title): b for b in series.books}
    now = datetime.now()
    newly_found = []

    for entry in detail.get('books') or []:
        key = match_key(entry['title'])
        if not key or key in known:
            continue
        known.add(key)

        owned = owned_by_key.get(key)
        release = SeriesRelease(
            series_id=series.id,
            title=entry['title'],
            match_key=key,
            series_number=entry.get('series_number'),
            external_url=entry.get('url'),
            discovered_at=now,
            is_baseline=is_baseline_run,
            book_id=owned.id if owned else None,
        )
        db.session.add(release)
        # Nothing to announce for the backlist, nor for a book already shelved.
        if not is_baseline_run and owned is None:
            newly_found.append(release)

    series.last_checked_at = now
    series.last_check_error = None
    db.session.commit()
    return newly_found


def notify_new_releases(series, releases):
    """One notification per series, listing everything new found this check."""
    if not releases:
        return
    lines = []
    for r in releases:
        number = f'#{r.series_number:g} ' if r.series_number is not None else ''
        lines.append(f'{number}{r.title}')
    plural = 'book' if len(releases) == 1 else 'books'
    sent = send_pushover_notification(
        title=f'New {plural} in {series.name}',
        message='\n'.join(lines),
        url=series.goodreads_url,
    )
    if sent:
        stamp = datetime.now()
        for r in releases:
            r.notified_at = stamp
        db.session.commit()


def _next_due_series():
    """The monitored series that has gone longest without a check, or None."""
    cutoff = datetime.now() - timedelta(seconds=CHECK_INTERVAL_SECONDS)
    return (Series.query
            .filter(Series.monitored.is_(True))
            .filter(db.or_(Series.last_checked_at.is_(None), Series.last_checked_at < cutoff))
            # Never-checked series first, then the least recently checked.
            .order_by(Series.last_checked_at.is_(None).desc(), Series.last_checked_at.asc())
            .first())


def run_due_series_check(app):
    """Check at most one series. Returns True if one was due."""
    with app.app_context():
        series = _next_due_series()
        if series is None:
            return False
        name = series.name
        try:
            releases = check_series(series)
        except ScrapeBlockedError as e:
            series.last_check_error = str(e)[:300]
            series.last_checked_at = datetime.now()
            db.session.commit()
            logging.warning('Series monitor blocked while checking %r: %s', name, e)
            raise
        except Exception as e:
            db.session.rollback()
            series.last_check_error = str(e)[:300]
            series.last_checked_at = datetime.now()
            db.session.commit()
            logging.warning('Series check failed for %r', name, exc_info=True)
            return True

        if releases:
            logging.info('Series monitor: %d new in %r', len(releases), name)
            notify_new_releases(series, releases)
        return True


def start_series_monitor_scheduler(app):
    """Start the daemon thread that checks one due series a minute.

    As with the price-watch scheduler, the caller is responsible for only
    starting this in the process that serves requests."""
    def loop():
        while True:
            try:
                run_due_series_check(app)
            except ScrapeBlockedError:
                # Being turned away means backing off entirely, not moving on
                # to the next series and asking again immediately.
                time.sleep(BLOCKED_BACKOFF_SECONDS)
                continue
            except Exception:
                logging.warning('Series monitor iteration failed', exc_info=True)
            time.sleep(SERIES_SPACING_SECONDS)

    threading.Thread(target=loop, daemon=True).start()
