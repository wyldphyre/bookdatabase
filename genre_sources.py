"""Where a book's genres come from, and in what order.

Two callers need the same answer: the System page's genre scan, walking the
whole library, and the Fetch tags menu on a book's own page. Keeping the chain
here means the button really does do what the scan does, rather than being a
second implementation that drifts from it.

The order is Hardcover, then Goodreads. Hardcover is one authenticated API
call to a site that isn't trying to block us and covers most of this library;
Goodreads is two scrapes of a site that stops answering after a handful of
requests, so it is the fallback for what Hardcover doesn't hold.
"""

import logging

from models import db, Tag
import hardcover
from scrapers import search_goodreads_for_book, scrape_goodreads

AUTO = 'auto'
HARDCOVER = 'hardcover'
GOODREADS = 'goodreads'

#: Sources a caller may ask for by name. AUTO is the chain; the other two force
#: one source, which is how you find out whether a disappointing result was
#: Hardcover not knowing the book or Goodreads refusing to talk.
SOURCES = (AUTO, HARDCOVER, GOODREADS)


def _author_names(book):
    return ', '.join(a.name for a in book.authors) if book.authors else ''


def hardcover_genres(book):
    """Genres from Hardcover, or [] if it can't confidently supply any.

    Hardcover can't tell "no such book" from "book with no genres" — both are
    an empty list — so unlike the Goodreads side this never returns None.
    """
    return hardcover.lookup_genres(book.title, _author_names(book))


def goodreads_genres(book):
    """Genres from Goodreads, or None when it can't identify the book.

    None rather than [] for "no such book", so a caller can tell that apart
    from a book that was found with nothing listed on it.

    Prefers a URL already on the book: finding one costs a request to /search,
    the most aggressively protected endpoint on the site and the one that trips
    the block, while a known URL needs only the page itself. A URL the search
    turns up is saved, so nothing pays for that search twice.

    Raises ScrapeBlockedError when Goodreads serves a challenge, which callers
    are expected to handle — the scan retires Goodreads for the rest of its
    run, and the book page reports it.
    """
    book_url = book.goodreads_url
    if not book_url:
        book_url = search_goodreads_for_book(book.title, _author_names(book))
        if not book_url:
            return None
        book.goodreads_url = book_url
        db.session.commit()

    book_data = scrape_goodreads(book_url)
    if not book_data:
        return None
    return book_data.get('genres') or []


def fetch_genres(book, source=AUTO, allow_goodreads=True):
    """Genres for one book, and which source supplied them.

    Returns (genres, source_used) where genres is a list, or None when the
    source that ran could not identify the book at all. source_used names the
    source whose answer is being returned, so a caller can report it.

    `allow_goodreads=False` keeps the chain from falling back — the scan sets
    it once Goodreads has blocked us, so the rest of the run leans on
    Hardcover instead of hammering a site that has already said no.
    """
    if source not in SOURCES:
        raise ValueError(f'unknown genre source: {source!r}')

    if source == HARDCOVER:
        return hardcover_genres(book), HARDCOVER
    if source == GOODREADS:
        return goodreads_genres(book), GOODREADS

    try:
        genres = hardcover_genres(book)
    except hardcover.HardcoverAuthError as e:
        # A broken token shouldn't stop the chain — Goodreads can still answer.
        # Asking for Hardcover by name reports it instead; see fetch_genres's
        # HARDCOVER branch above, which lets it through.
        logging.warning('Skipping Hardcover: %s', e)
        genres = []

    if genres or not allow_goodreads:
        return genres, HARDCOVER
    return goodreads_genres(book), GOODREADS


def apply_genres(book, genres):
    """Attach genres to a book as tags, creating any that don't exist yet.

    Returns the names actually added, so callers can report "3 new tags" apart
    from "found the book, knew all of it already". Tag names are matched
    case-insensitively, which is what keeps Hardcover's lowercase 'litrpg' from
    becoming a second tag beside an existing 'LitRPG'.
    """
    existing = {t.name.lower() for t in book.tags}
    added = []
    for name in genres or []:
        if name.lower() in existing:
            continue
        tag = Tag.query.filter(db.func.lower(Tag.name) == name.lower()).first()
        if not tag:
            tag = Tag(name=name)
            db.session.add(tag)
            db.session.flush()
        book.tags.append(tag)
        existing.add(name.lower())
        added.append(tag.name)

    if added:
        db.session.commit()
    return added
