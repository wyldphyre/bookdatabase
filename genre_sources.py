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
import googlebooks
from scrapers import search_goodreads_for_book, scrape_goodreads

AUTO = 'auto'
HARDCOVER = 'hardcover'
GOODREADS = 'goodreads'
GOOGLEBOOKS = 'googlebooks'

#: Sources a caller may ask for by name. AUTO walks a chain; the rest force one
#: source, which is how you find out whether a disappointing result was a
#: source not knowing the book or a source refusing to talk.
SOURCES = (AUTO, GOODREADS, HARDCOVER, GOOGLEBOOKS)

#: Human-readable, for messages and the System page.
LABELS = {GOODREADS: 'Goodreads', HARDCOVER: 'Hardcover', GOOGLEBOOKS: 'Google Books'}

# The order differs by caller, because the two jobs want opposite things.
#
# Measured over this library: Goodreads gives by far the richest tags but stops
# answering after a couple of requests; Hardcover gives about five genres a
# book, free and unlimited, and agrees with 43% of the tags already here;
# Google Books gives about one category a book — usually the word "Fiction" —
# but is the only one that reaches the obscure indie titles at all.
#
#: One book at a time, where a couple of requests is affordable and quality is
#: the whole point: ask the best source first.
BOOK_CHAIN = (GOODREADS, HARDCOVER, GOOGLEBOOKS)

#: Hundreds of books in a row, where being able to finish matters more than any
#: single answer: Goodreads would block on book three and retire, so it goes
#: last and only sees what the other two could not place.
SCAN_CHAIN = (HARDCOVER, GOOGLEBOOKS, GOODREADS)


def source_status():
    """Each source in the order the scan tries them, whether it can be used,
    and why not.

    The System page shows this because an unconfigured source is otherwise
    invisible until someone notices a greyed-out entry on a book page. The
    usual cause is an environment variable that never reached the process —
    under Docker that needs the name listed in the compose file's environment
    *and* a value in the .env beside it, and missing either looks identical
    from in here.
    """
    docker_note = ('Under Docker it must be both listed in the compose file\'s environment '
                   'section and given a value in the .env beside it — either one alone '
                   'leaves it unset in here.')
    hardcover_ready = hardcover.is_configured()
    google_ready = googlebooks.is_configured()
    return [
        {
            'name': LABELS[HARDCOVER],
            'ready': hardcover_ready,
            'detail': ('Tried first in a scan. One API call per book, no scraping, and '
                       'around five genres when it has the book at all.'
                       if hardcover_ready else
                       f'Set HARDCOVER_TOKEN to enable. {docker_note}'),
        },
        {
            'name': LABELS[GOOGLEBOOKS],
            'ready': google_ready,
            'detail': ('Reaches obscure titles the others miss, but usually returns a '
                       'single broad category. Free quota is 1,000 lookups a day.'
                       if google_ready else
                       f'Set GOOGLEBOOKS_TOKEN to enable. {docker_note}'),
        },
        {
            'name': LABELS[GOODREADS],
            'ready': True,
            'detail': 'The richest tags by far, and the reason most of this library is '
                      'already tagged — but it is scraped rather than an API, and blocks '
                      'after a handful of requests. Tried first for a single book, last '
                      'in a scan, and dropped for the rest of a run once it refuses.',
        },
    ]


def _author_names(book):
    return ', '.join(a.name for a in book.authors) if book.authors else ''


def hardcover_genres(book):
    """Genres from Hardcover, or [] if it can't confidently supply any.

    Hardcover can't tell "no such book" from "book with no genres" — both are
    an empty list — so unlike the Goodreads side this never returns None.
    """
    return hardcover.lookup_genres(book.title, _author_names(book))


def googlebooks_genres(book):
    """Categories from Google Books, or [] if it can't confidently supply any.

    Same empty-list-for-everything contract as Hardcover's.
    """
    return googlebooks.lookup_genres(book.title, _author_names(book))


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


def _provider(name):
    """The function backing a source name.

    Resolved per call rather than captured in a module-level table, so the
    three functions above stay the real seam — a table built at import time
    would keep pointing at the original functions after anything rebound them.
    """
    return {GOODREADS: goodreads_genres,
            HARDCOVER: hardcover_genres,
            GOOGLEBOOKS: googlebooks_genres}[name]

#: Raised past the chain rather than swallowed: a rejected key or token is a
#: configuration problem, not a fact about the book.
_AUTH_ERRORS = (hardcover.HardcoverAuthError, googlebooks.GoogleBooksAuthError)


def fetch_genres(book, source=AUTO, chain=BOOK_CHAIN, unavailable=()):
    """Genres for one book, and which source supplied them.

    Returns (genres, source_used). genres is a list, or None when the last
    source tried could not identify the book at all — only Goodreads can say
    that, the others return an empty list either way.

    `source` other than AUTO forces one source and skips the chain, which is
    how the book page's explicit menu entries work and how you tell a source
    having nothing apart from a source being unreachable.

    `unavailable` names sources to skip for this run. The scan puts Goodreads
    in there once it has been blocked, so the rest of the run stops asking a
    site that has already refused rather than collecting hundreds of identical
    failures.

    A broken token stops that one source rather than the chain: it is logged
    and the next source is tried, because a misconfigured key is no reason to
    leave a book untagged. Asking for that source by name still raises.
    """
    if source not in SOURCES:
        raise ValueError(f'unknown genre source: {source!r}')
    if source != AUTO:
        return _provider(source)(book), source

    result, last = [], chain[-1]
    for name in chain:
        if name in unavailable:
            continue
        try:
            genres = _provider(name)(book)
        except _AUTH_ERRORS as e:
            logging.warning('Skipping %s: %s', LABELS[name], e)
            continue
        last = name
        if genres:
            return genres, name
        result = genres          # keep the last answer, None included

    return result, last


def apply_genres(book, genres, source=None):
    """Attach genres to a book as tags, creating any that don't exist yet.

    Returns the names actually added, so callers can report "3 new tags" apart
    from "found the book, knew all of it already". Tag names are matched
    case-insensitively, which is what keeps a lowercase 'litrpg' from becoming
    a second tag beside an existing 'LitRPG'.

    `source` records which source supplied them, so a later scan can find the
    books a fallback tagged while Goodreads was unavailable and go back for
    better ones. Recorded whenever a source answered, even if it said nothing
    this book didn't already have — it was still asked, and that is what the
    filter is asking about.
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

    changed_source = bool(source) and book.genre_source != source
    if changed_source:
        book.genre_source = source
    if added or changed_source:
        db.session.commit()
    return added
