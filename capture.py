"""Adding a book from a page captured in the browser.

The browser extension (and the iOS Shortcut) send the book page you're looking
at; it's read here with the same parsers the URL import uses, then held under a
short-lived token while the Add Book form opens pre-filled from it. Nothing is
written to the library until that form is saved.
"""
import re
import secrets
import threading
import time

from models import db, Book, BookFormat, Tag
from series_monitor import match_key

# Long enough to review the form and reload it; the token is only ever handed
# straight to the tab that opens the form.
CAPTURE_TTL_SECONDS = 30 * 60

# Production runs a single gunicorn worker, so process memory is shared by
# every request. Held captures are throwaway, so losing them on restart is fine.
_captures = {}                 # token -> (expires_at, prefill)
_captures_lock = threading.Lock()


def hold(prefill):
    """Keep a prefill for the form to pick up; returns its token."""
    token = secrets.token_urlsafe(16)
    now = time.monotonic()
    with _captures_lock:
        for key in [k for k, (expires, _) in _captures.items() if expires < now]:
            del _captures[key]
        _captures[token] = (now + CAPTURE_TTL_SECONDS, prefill)
    return token


def collect(token):
    """The prefill held under token, or None once it has expired. Not removed
    on use, so reloading the form within the window still finds it."""
    with _captures_lock:
        entry = _captures.get(token or '')
    if not entry or entry[0] < time.monotonic():
        return None
    return dict(entry[1])


def build_prefill(book_data, create_tags):
    """Turn parsed page data into the Add Book form's prefill.

    create_tags: the URL import creates a tag for every genre, as it always
    has. A capture only applies genres that are already tags and leaves the
    rest in new_genres for the form to offer, so capturing a page can't add
    tags to the library unless you choose to.
    """
    prefill = dict(book_data)

    genres = prefill.pop('genres', None) or []
    if create_tags:
        prefill['tag_ids'] = [Tag.find_or_create(name).id for name in genres]
        db.session.commit()
    elif genres:
        existing = {t.name.lower(): t for t in
                    Tag.query.filter(db.func.lower(Tag.name).in_([g.lower() for g in genres])).all()}
        prefill['tag_ids'] = [existing[g.lower()].id for g in genres if g.lower() in existing]
        prefill['new_genres'] = [g for g in genres if g.lower() not in existing]

    detected_format = prefill.pop('detected_format', None)
    if detected_format:
        format_match = BookFormat.query.filter(db.func.lower(BookFormat.name) == detected_format.lower()).first()
        if format_match:
            prefill['format_id'] = format_match.id

    # Cost is the book's full price — Saved is worked out as cost - paid — so
    # during a sale it's the list price; what you paid is yours to fill in.
    price = prefill.pop('list_price', None) or prefill.pop('price', None)
    prefill.pop('price', None)
    prefill.pop('currency', None)
    if price is not None:
        prefill['cost'] = price

    return prefill


def _url_key(url):
    """A site-specific identity for a book page link, so the same book matches
    however its URL is dressed (slug, region, query string)."""
    if not url:
        return None
    m = re.search(r'goodreads\.com/(?:[a-z]{2}/)?book/show/(\d+)', url)
    if m:
        return 'goodreads:' + m.group(1)
    m = re.search(r'amazon\.[a-z.]+/(?:.*/)?(?:dp|gp/product)/([A-Za-z0-9]{10})', url)
    if m:
        return 'amazon:' + m.group(1).upper()
    m = re.search(r'kobo\.com/(?:[a-z]{2}/[a-z]{2}/)?((?:ebook|audiobook)/[^/?#]+)', url)
    if m:
        return 'kobo:' + m.group(1).lower()
    return None


def find_existing(book_data, limit=5):
    """Library books that look like the captured one: the same page link
    first, then the same title (as series monitoring normalises it) with an
    author in common, or the same title alone when the page named no author."""
    page_key = _url_key(book_data.get('goodreads_url') or book_data.get('amazon_url')
                        or book_data.get('kobo_url'))
    title_key = match_key(book_data.get('title'))
    authors = {a.lower() for a in book_data.get('authors') or []}

    rows = db.session.query(Book.id, Book.title, Book.goodreads_url, Book.amazon_url, Book.kobo_url).all()
    by_link = [r.id for r in rows if page_key and page_key in
               {_url_key(r.goodreads_url), _url_key(r.amazon_url), _url_key(r.kobo_url)}]
    by_title = [r.id for r in rows if title_key and match_key(r.title) == title_key and r.id not in by_link]

    matches = Book.query.filter(Book.id.in_(by_link)).all() if by_link else []
    if by_title:
        for book in Book.query.filter(Book.id.in_(by_title)).all():
            if not authors or authors & {a.name.lower() for a in book.authors}:
                matches.append(book)
    return matches[:limit]
