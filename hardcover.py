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
import logging

import requests

from scrapers import throttle
import genre_text

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

def _token():
    """Read the token per call so a test (or a restart-free .env edit) can set
    it without the import order mattering."""
    return (os.environ.get('HARDCOVER_TOKEN') or '').strip()


def is_configured():
    return bool(_token())


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
                  if genre_text.titles_agree(title, d.get('title'))
                  and genre_text.author_matches(author or '', d.get('author_names'))]
    if not candidates:
        return []

    return genre_text.clean_genres(_best_edition(candidates).get('genres'))
