"""Genre lookup through the Hardcover API.

Goodreads turns the scraper away after a couple of requests, which is not
enough to tag even one newly added book (finding it costs one request, reading
its page another). Hardcover is a Goodreads-style community catalogue with an
actual GraphQL API, and its search index carries each book's genres inline —
so a lookup here is a single authenticated call rather than two scrapes of a
site actively trying to stop us.

Measured against a 50-book sample of this library, Hardcover returned usable
genres for 60% of it, against Open Library's 28%; the gap is almost entirely
indie and self-published titles, which is most of what this library holds.
The remaining 40% is genuinely not in Hardcover — obscure self-published work
and individual manga volumes — so Goodreads stays as the fallback rather than
being replaced.

Disabled when HARDCOVER_TOKEN is unset: lookup_genres returns an empty list
and callers carry on to Goodreads exactly as before.
"""

import os
import re
import logging

import requests

from scrapers import throttle

API_URL = 'https://api.hardcover.app/v1/graphql'
API_HOST = 'api.hardcover.app'

# Hardcover is an API rather than a site we're scraping, so it doesn't need the
# 2s the HTML scrapers use. A second between calls kept ~75 test requests well
# clear of any throttling.
MIN_REQUEST_INTERVAL_SECONDS = 1.0

REQUEST_TIMEOUT_SECONDS = 20


class HardcoverAuthError(Exception):
    """The token was missing, rejected or expired.

    Separate from every other failure because it is a configuration problem,
    not a fact about the book: without it a stale token looks exactly like
    'Hardcover has never heard of this book', which sends you looking in the
    wrong place. The chain still falls through to Goodreads when it sees one;
    it is asking Hardcover *directly* that reports it."""

# The search index is Typesense; `genres` comes back on the document itself,
# which is what makes this one request instead of two.
_SEARCH_QUERY = '''
query BookSearch($q: String!, $n: Int!) {
  search(query: $q, query_type: "Book", per_page: $n) { results }
}
'''

_CANDIDATES_PER_SEARCH = 8

# Hardcover's genre list is partly ingested from library records, and some of
# those arrive as one BISAC heading that has been split on its commas —
# "Fantasy comic books, strips, etc" becomes three entries, two of which are
# meaningless on their own. Others arrive semicolon-joined in a single string.
_FRAGMENT_SEPARATORS = re.compile(r'\s*;\s*')

# Some entries carry their own gloss — "LitRPG (Literary Role-Playing Game)" —
# which would otherwise become a second tag alongside the plain "LitRPG" the
# same book also carries.
_PARENTHETICAL = re.compile(r'\s*\([^)]*\)')
_JUNK_FRAGMENTS = {'etc', 'strips', 'general', 'other', 'misc'}
_MIN_GENRE_LENGTH = 3

# Where Hardcover's wording differs from the vocabulary this library already
# uses. Only worth an entry when the existing tag is well established; anything
# not listed here is passed through and may create a new tag, which is what the
# Goodreads path has always done.
#
# LGBTQ is deliberately absent: it used to fold onto the older LGBT tag, but
# the longer form is the more standard one, so Hardcover's wording is now kept
# and LGBT is the form being moved away from. Don't re-add it.
_GENRE_ALIASES = {
    'young adult fiction': 'Young Adult',
    'juvenile fiction': 'Middle Grade',
    "children's fiction": 'Middle Grade',
    'comics & graphic novels': 'Graphic Novels',
    'comic books': 'Comics',
    'dystopian': 'Dystopia',
    'action & adventure': 'Adventure',
}


def _token():
    """Read the token per call so a test (or a restart-free .env edit) can set
    it without the import order mattering."""
    return (os.environ.get('HARDCOVER_TOKEN') or '').strip()


def is_configured():
    return bool(_token())


def _normalise(text):
    """Lowercased, punctuation-free form used for comparing titles and names.

    'volume' and 'part' are folded to the abbreviations Hardcover tends to use
    so that "Paper Girls volume 1" can meet "Paper Girls, Vol. 1".
    """
    text = (text or '').lower()
    text = re.sub(r'\bvolume\b', 'vol', text)
    text = re.sub(r'\bpart\b', 'pt', text)
    return re.sub(r'[^a-z0-9]+', ' ', text).strip()


def _significant_words(text):
    return {w for w in _normalise(text).split() if len(w) > 2}


def _author_matches(known, candidate_names):
    """True when the book's author shares a significant word with any of the
    candidate's contributors. Mirrors the rule the Goodreads search already
    uses, and tolerates 'E. J. Stevens' against 'E.J. Stevens'."""
    wanted = _significant_words(known)
    if not wanted:
        return True                      # nothing to check against; title alone decides
    return any(wanted & _significant_words(name) for name in candidate_names or [])


def _titles_agree(wanted, candidate):
    """Deliberately strict: one title has to contain the other.

    A looser word-overlap rule was tried and recovered two of ten missed
    comics, but matched "Moonstruck Volume 3" to "Moonstruck, Vol. 2" in the
    process. A wrong volume's genres are not worth two extra hits."""
    a, b = _normalise(wanted), _normalise(candidate)
    return bool(a and b and (a in b or b in a))


def _clean_genres(raw):
    """Split compound entries, drop ingestion fragments, de-duplicate."""
    cleaned = []
    seen = set()
    for entry in raw or []:
        for piece in _FRAGMENT_SEPARATORS.split(str(entry)):
            piece = _PARENTHETICAL.sub('', piece).strip().strip('.,;')
            if len(piece) < _MIN_GENRE_LENGTH or piece.lower() in _JUNK_FRAGMENTS:
                continue
            name = _GENRE_ALIASES.get(piece.lower(), piece)
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            cleaned.append(name)
    return cleaned


def _best_edition(documents):
    """Hardcover carries duplicate editions, and the duplicates are usually the
    untagged ones — Dungeon Crawler Carl appears with 10,399 readers and a full
    tag set, and again with one reader and none. Take the edition people
    actually use."""
    return max(documents, key=lambda d: ((d.get('users_count') or 0),
                                         (d.get('ratings_count') or 0)))


def lookup_genres(title, author=''):
    """Genres for a book, or [] when Hardcover can't confidently supply any.

    Returns [] for every ordinary failure — network trouble, a GraphQL error,
    no match — so the caller falls back to Goodreads; those are logged rather
    than surfaced, because this sits in front of a fallback that reports its
    own problems. The one exception is HardcoverAuthError, which is a broken
    token rather than a missing book and would otherwise be indistinguishable
    from one.

    The query is the title alone. Including the author wrecks the ranking:
    searching "Shadow Sight E. J. Stevens" returns 1980s fantasy anthologies
    whose contributor lists happen to contain those words. The author is used
    to verify the result instead.
    """
    token = _token()
    if not token or not (title or '').strip():
        return []

    throttle(API_HOST, MIN_REQUEST_INTERVAL_SECONDS)
    try:
        response = requests.post(
            API_URL,
            headers={'Authorization': f'Bearer {token}',
                     'Content-Type': 'application/json'},
            json={'query': _SEARCH_QUERY,
                  'variables': {'q': title, 'n': _CANDIDATES_PER_SEARCH}},
            timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        logging.warning('Hardcover lookup for %r failed: %s', title, e)
        return []

    if response.status_code in (401, 403):
        raise HardcoverAuthError(
            'Hardcover rejected the API token — check HARDCOVER_TOKEN is set to a '
            'current token from your Hardcover account settings.')

    if response.status_code != 200:
        logging.warning('Hardcover returned HTTP %s for %r', response.status_code, title)
        return []

    try:
        payload = response.json()
    except ValueError:
        logging.warning('Hardcover returned a non-JSON body for %r', title)
        return []

    if payload.get('errors'):
        logging.warning('Hardcover rejected the query for %r: %s', title, payload['errors'])
        return []

    results = ((payload.get('data') or {}).get('search') or {}).get('results') or {}
    documents = [hit.get('document') or {} for hit in results.get('hits') or []]

    candidates = [d for d in documents
                  if _titles_agree(title, d.get('title'))
                  and _author_matches(author or '', d.get('author_names'))]
    if not candidates:
        return []

    return _clean_genres(_best_edition(candidates).get('genres'))
