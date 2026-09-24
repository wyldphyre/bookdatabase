"""Genre lookup through the Google Books API.

The third and last link in the chain, and there for one reason: it reaches
books the others cannot. Measured over this library's 97 untagged books —
the obscure indie titles, box sets and manga volumes that are untagged
precisely because nothing has data on them — Google Books answered for 15 of
them and Hardcover for none, because Google ingests publisher metadata
straight from KDP.

Set against that, its categories are extremely thin. Over 250 tagged books it
returned a mean of 1.0 categories each, against Hardcover's 5.1 and the 6.2
this library carries by hand, and 110 of about 160 values returned were the
single word "Fiction". It agreed with only 9% of the tags already on those
books. So it goes last: it is worth asking when the better sources have
nothing, and not worth asking before them.

Needs a free key in GOOGLEBOOKS_TOKEN. Keyless requests are refused outright
with HTTP 429 rather than throttled, so without one this source is simply
disabled and the chain carries on without it.
"""

import os
import logging
import urllib.parse

import requests

from scrapers import throttle
import genre_text

API_URL = 'https://www.googleapis.com/books/v1/volumes'
API_HOST = 'www.googleapis.com'

# The free quota is 1,000 requests a day, so a full-library scan of ~1,000
# books would consume all of it in one run. A second between calls also keeps
# clear of the per-user rate limit, which is much tighter than the daily one.
MIN_REQUEST_INTERVAL_SECONDS = 1.0

REQUEST_TIMEOUT_SECONDS = 20

_CANDIDATES_PER_SEARCH = 5


class GoogleBooksAuthError(Exception):
    """The key was rejected, or its Cloud project has no Books API enabled.

    Separate from every other failure for the same reason Hardcover's is: a
    configuration problem that otherwise reads as 'no such book', which sends
    you looking in the wrong place. The chain falls through past it; asking
    for Google Books directly reports it.
    """


def _token():
    return (os.environ.get('GOOGLEBOOKS_TOKEN') or '').strip()


def is_configured():
    return bool(_token())


def _best_volume(volumes):
    """Google returns one row per edition and only some carry categories at
    all, so prefer whichever says the most rather than whichever ranks first."""
    return max(volumes, key=lambda v: len(v.get('categories') or []))


def lookup_genres(title, author=''):
    """Categories for a book, or [] when Google Books can't confidently supply any.

    Unlike Hardcover this queries title *and* author together: Google's
    `intitle:`/`inauthor:` operators are field-scoped, so naming the author
    narrows rather than pollutes. The result is still verified afterwards,
    because the operators match loosely.

    Returns [] for every ordinary failure so the caller moves on to the next
    source. GoogleBooksAuthError is the exception, being a broken key rather
    than a missing book.
    """
    key = _token()
    if not key or not (title or '').strip():
        return []

    query = f'intitle:"{title}"'
    if author:
        # One author is enough to disambiguate, and the full list of a comic's
        # contributors would over-constrain the query.
        query += f' inauthor:"{author.split(",")[0].strip()}"'

    throttle(API_HOST, MIN_REQUEST_INTERVAL_SECONDS)
    try:
        response = requests.get(
            API_URL,
            params={'q': query, 'maxResults': _CANDIDATES_PER_SEARCH, 'key': key},
            timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        logging.warning('Google Books lookup for %r failed: %s', title, e)
        return []

    if response.status_code in (400, 401, 403):
        raise GoogleBooksAuthError(
            'Google Books rejected the API key — check GOOGLEBOOKS_TOKEN, and that the '
            'Books API is enabled for its project in the Google Cloud console.')

    if response.status_code == 429:
        logging.warning('Google Books quota exhausted while looking up %r', title)
        return []

    if response.status_code != 200:
        logging.warning('Google Books returned HTTP %s for %r', response.status_code, title)
        return []

    try:
        payload = response.json()
    except ValueError:
        logging.warning('Google Books returned a non-JSON body for %r', title)
        return []

    volumes = [item.get('volumeInfo') or {} for item in payload.get('items') or []]
    candidates = [v for v in volumes
                  if genre_text.titles_agree(title, v.get('title'))
                  and genre_text.author_matches(author or '', v.get('authors'))]
    if not candidates:
        return []

    return genre_text.clean_genres(_best_volume(candidates).get('categories'))
