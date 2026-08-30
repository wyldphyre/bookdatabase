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
from scrapers import (scrape_goodreads_series_detail, scrape_amazon_series_detail,
                      scrape_goodreads, scrape_amazon, search_goodreads_for_book,
                      ScrapeBlockedError)
from notifications import send_pushover_notification

# How stale a series' last check must be before it's due again.
CHECK_INTERVAL_SECONDS = 7 * 24 * 60 * 60
# Gap between checking one series and the next.
SERIES_SPACING_SECONDS = 60
# After being turned away, wait this long before trying anyone again.
BLOCKED_BACKOFF_SECONDS = 30 * 60
# Books to try when working out a series' Goodreads URL from scratch.
URL_RESOLUTION_ATTEMPTS = 2


# "Book 2" (Amazon), "#2" (Goodreads) and a bare "2" all name the same
# instalment. Applied before punctuation is flattened, so that a novella's
# ".5" survives; only matched at the end, where the numbering lives, and only
# with a real title in front of it, so a book actually called "Book 1" keeps
# its name.
_INSTALMENT_NOISE = re.compile(r'(?<=\w)\W+(?:book|bk|volume|vol|part)\s*(\d+(?:\.\d+)?)\s*$',
                               re.IGNORECASE)


def match_key(title):
    """Normalised title, used to tell whether we've already seen an entry.

    Punctuation and case drift between listings, so comparing raw titles would
    re-announce the same book. Sites also disagree about how to write the
    instalment number: Amazon ships "Title: Book 2" where Goodreads has
    "Title #2". The noise word is dropped but the number is kept — collapsing
    it away entirely would give every instalment the same key as book 1, and
    new releases would then be silently taken for books already seen."""
    raw = _INSTALMENT_NOISE.sub(r' \1', (title or '').strip())
    return re.sub(r'[^a-z0-9]+', ' ', raw.lower()).strip()


def _series_url_from_book_page(book):
    """(field, series_url) read straight off a book's own page, or (None, None).

    Both sites name the series on a book's page, so a book we already hold a
    link for answers the question in one request — no search, and nothing to
    mis-identify."""
    if book.goodreads_url:
        data = scrape_goodreads(book.goodreads_url)
        if data and data.get('series_url'):
            return 'goodreads_url', data['series_url']
    if book.amazon_url:
        data = scrape_amazon(book.amazon_url)
        if data and data.get('series_url'):
            return 'amazon_url', data['series_url']
    return None, None


def resolve_series_url(series):
    """Work out a series URL from the books we already own, as (field, url).

    Lets monitoring work on series with no external link recorded. Books whose
    own page we already have a link to are tried first: that path is one
    request and can't pick the wrong book, whereas searching by title depends
    on a search result being right, and quietly finds nothing for anything
    obscure. Searching stays as the fallback for books with no link at all.
    Both passes are capped, so this stays a couple of requests, once — the
    result is saved to the series."""
    linked = [b for b in series.books if b.goodreads_url or b.amazon_url]
    for book in linked[:URL_RESOLUTION_ATTEMPTS]:
        try:
            field, url = _series_url_from_book_page(book)
            if url:
                return field, url
        except ScrapeBlockedError:
            raise
        except Exception:
            logging.warning('Series URL lookup failed for %r via %r', series.name, book.title,
                            exc_info=True)

    for book in [b for b in series.books if b.title][:URL_RESOLUTION_ATTEMPTS]:
        try:
            book_url = search_goodreads_for_book(book.title, book.author_names)
            if not book_url:
                continue
            data = scrape_goodreads(book_url)
            if data and data.get('series_url'):
                return 'goodreads_url', data['series_url']
        except ScrapeBlockedError:
            raise
        except Exception:
            logging.warning('Series URL lookup failed for %r', series.name, exc_info=True)
    return None, None


def series_sources(series):
    """The pages that can be read for this series, richest first.

    Goodreads leads because its listing carries the whole series including
    novellas; Amazon covers series that were never on Goodreads, or that no
    URL could be worked out for."""
    sources = []
    if series.goodreads_url:
        sources.append(('Goodreads', series.goodreads_url, scrape_goodreads_series_detail))
    if series.amazon_url:
        sources.append(('Amazon', series.amazon_url, scrape_amazon_series_detail))
    return sources


def read_series_page(series):
    """First source that yields a readable listing, as (detail, failures).

    Every recorded URL is tried before giving up, so one site's markup drifting
    doesn't stop a series that's also listed elsewhere. A block is remembered
    rather than raised on the spot: the other host may well answer, and only if
    nothing does is it re-raised, so the scheduler still backs off."""
    blocked = None
    failures = []
    for name, url, scrape in series_sources(series):
        try:
            detail = scrape(url)
        except ScrapeBlockedError as e:
            blocked = e
            failures.append(f'{name} is blocking automated requests')
            continue
        except Exception:
            logging.warning('Series page fetch failed for %r at %s', series.name, url, exc_info=True)
            failures.append(f'{name} page could not be fetched')
            continue
        if detail.get('books'):
            return detail, failures
        failures.append(f'no books could be read from the {name} page')
    if blocked is not None:
        raise blocked
    return None, failures


def check_series(series):
    """Check one series and record what's on its page.

    Returns the releases worth telling the user about — empty on the first
    check of a series, which only establishes what was already out."""
    if not series_sources(series):
        field, found = resolve_series_url(series)
        if not found:
            series.last_check_error = ('No series page to check: no Goodreads or Amazon URL is '
                                       'recorded for this series, and one could not be worked out '
                                       'from its books.')
            series.last_checked_at = datetime.now()
            db.session.commit()
            return []
        setattr(series, field, found)

    detail, failures = read_series_page(series)

    # A page we can't read any books from is a failure, not an empty series.
    # Treating it as success would be doubly bad: the breakage would be silent,
    # and the series would never get baselined, so the next book to appear
    # would be filed as backlist and never announced. Goodreads' markup does
    # drift — the selectors this replaced had already gone stale.
    if detail is None:
        reason = '; '.join(failures) or 'no usable series URL'
        series.last_check_error = (f'Could not read the series: {reason}. The page layout may have '
                                   f'changed, or a URL may not point at a series.')[:300]
        series.last_checked_at = datetime.now()
        db.session.commit()
        return []

    entries = detail['books']

    # Keep the series' book count current, as part of the same visit. Only ever
    # revise upwards: a partial parse shouldn't quietly shrink a known count.
    count = detail.get('count')
    if count is not None and (series.number_in_series is None or count > series.number_in_series):
        series.number_in_series = count

    known = {r.match_key for r in series.releases}
    is_baseline_run = not series.baseline_done

    owned_by_key = {match_key(b.title): b for b in series.books}
    now = datetime.now()
    newly_found = []

    for entry in entries:
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
    series.baseline_done = True
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
    # Link to whichever site this series is actually listed on — an
    # Amazon-only series would otherwise get a notification with no link.
    sent = send_pushover_notification(
        title=f'New {plural} in {series.name}',
        message='\n'.join(lines),
        url=series.goodreads_url or series.amazon_url,
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
