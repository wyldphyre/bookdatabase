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

# A plain-text search ranks looser than a field-scoped one would, so look a
# little further down the results for the verified match.
_CANDIDATES_PER_SEARCH = 10


class GoogleBooksAuthError(Exception):
    """The key was rejected, or its Cloud project has no Books API enabled.

    Separate from every other failure for the same reason Hardcover's is: a
    configuration problem that otherwise reads as 'no such book', which sends
    you looking in the wrong place. The chain falls through past it; asking
    for Google Books directly reports it.
    """


# Google's error bodies say *why* a request failed; the status code alone
# doesn't. A bad key is HTTP 400 (API_KEY_INVALID), but so is a malformed
# query, and 403 covers both a disabled Books API and an exhausted daily quota
# — so only the reasons below mean the key or its project needs fixing.
_KEY_REASONS = {'API_KEY_INVALID', 'keyInvalid', 'SERVICE_DISABLED', 'accessNotConfigured',
                'API_KEY_SERVICE_BLOCKED', 'API_KEY_IP_ADDRESS_BLOCKED',
                'API_KEY_HTTP_REFERRER_BLOCKED'}
_QUOTA_REASONS = {'RATE_LIMIT_EXCEEDED', 'RESOURCE_EXHAUSTED', 'dailyLimitExceeded',
                  'rateLimitExceeded', 'userRateLimitExceeded', 'quotaExceeded'}


def _error_reasons(response):
    """Every reason code in a Google API error body, from both the legacy
    error.errors[].reason and the newer error.details[].reason, plus the
    top-level status."""
    try:
        error = (response.json() or {}).get('error') or {}
    except ValueError:
        return set()
    reasons = {e.get('reason') for e in error.get('errors') or []}
    reasons |= {d.get('reason') for d in error.get('details') or []}
    reasons.add(error.get('status'))
    return {r for r in reasons if r}


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

    Searches plain text — title and first author — and relies on the title
    and author checks afterwards to reject anything that isn't the book.
    Google's field operators (`intitle:`, `inauthor:`, `isbn:`) would be the
    natural way to ask, but since late September 2026 any query using them
    returns zero results with HTTP 200 and no error, while plain-text
    queries still work. Asking with operators made this source silently find
    nothing for every book.

    Returns [] for every ordinary failure so the caller moves on to the next
    source. GoogleBooksAuthError is the exception, being a broken key rather
    than a missing book.
    """
    key = _token()
    if not key or not (title or '').strip():
        return []

    # One author is enough to disambiguate, and the full list of a comic's
    # contributors would over-constrain the query.
    query = title.strip()
    if author:
        query += ' ' + author.split(',')[0].strip()

    throttle(API_HOST, MIN_REQUEST_INTERVAL_SECONDS)
    try:
        # The key goes in a header, not the query string: requests puts the
        # full URL into its exception messages, so a key in the URL ends up in
        # the log on any connection error.
        response = requests.get(
            API_URL,
            params={'q': query, 'maxResults': _CANDIDATES_PER_SEARCH},
            headers={'X-goog-api-key': key},
            timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        logging.warning('Google Books lookup for %r failed: %s', title, e)
        return []

    if response.status_code != 200:
        reasons = _error_reasons(response)
        if reasons & _KEY_REASONS:
            raise GoogleBooksAuthError(
                'Google Books rejected the API key — check GOOGLEBOOKS_TOKEN, and that the '
                'Books API is enabled for its project in the Google Cloud console.')
        if response.status_code == 429 or reasons & _QUOTA_REASONS:
            logging.warning('Google Books quota exhausted while looking up %r', title)
        else:
            logging.warning('Google Books returned HTTP %s (%s) for %r', response.status_code,
                            ', '.join(sorted(reasons)) or 'no reason given', title)
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
