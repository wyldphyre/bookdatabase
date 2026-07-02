import os
import re
import socket
import ipaddress
from datetime import datetime
from urllib.parse import urlparse, urljoin

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def parse_date(date_str):
    """Parse date string to datetime object."""
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        if dt.year < 1900 or dt.year > datetime.now().year + 2:
            return None
        return dt
    except ValueError:
        return None


def parse_float(value):
    """Parse string to float, return None if invalid."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def validate_rating(rating):
    """Validate rating is an integer between 1-5."""
    if rating is None:
        return None
    rating = round(rating)
    if rating < 1 or rating > 5:
        return None
    return float(rating)


def clean_external_url(url):
    """Strip tracking/search cruft from Goodreads, Amazon, and StoryGraph URLs.

    Amazon links are canonicalized to a bare https://domain/dp/ASIN form.
    Goodreads/StoryGraph links keep their path (it's a meaningful id, not
    tracking) but lose the query string and fragment.
    Unrecognized domains are returned unchanged.
    """
    if not url:
        return url
    url = url.strip()
    if not url:
        return url
    try:
        parsed = urlparse(url)
    except ValueError:
        return url

    host = (parsed.hostname or '').lower()

    if 'amazon.' in host:
        match = re.search(r'/(?:dp|gp/product)/([A-Za-z0-9]{10})', parsed.path)
        if match:
            return f'{parsed.scheme}://{parsed.netloc}/dp/{match.group(1).upper()}'
        return parsed._replace(query='', fragment='').geturl()

    if 'goodreads.com' in host or 'thestorygraph.com' in host:
        return parsed._replace(query='', fragment='').geturl()

    return url


def _is_safe_cover_url(url):
    """Return True only for public http/https URLs — blocks private/loopback targets."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        addr = ipaddress.ip_address(socket.gethostbyname(hostname))
        return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved)
    except Exception:
        return False


MAX_COVER_DOWNLOAD_BYTES = 10 * 1024 * 1024  # 10MB


def fetch_cover_image(url, max_redirects=5):
    """Download a cover image, re-validating every redirect hop against
    _is_safe_cover_url and capping the download size.

    Returns (content_bytes, content_type). Raises ValueError on an unsafe
    URL / too many redirects / oversized file, and requests exceptions on
    network errors."""
    import requests

    for _ in range(max_redirects + 1):
        if not _is_safe_cover_url(url):
            raise ValueError('URL must be a public http/https address')
        response = requests.get(url, timeout=10, stream=True, allow_redirects=False)
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get('location')
            if not location:
                raise ValueError('Redirect without a location header')
            url = urljoin(url, location)
            continue
        response.raise_for_status()

        chunks = []
        size = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            size += len(chunk)
            if size > MAX_COVER_DOWNLOAD_BYTES:
                raise ValueError('Image is too large (over 10MB)')
            chunks.append(chunk)
        return b''.join(chunks), response.headers.get('content-type', '')

    raise ValueError('Too many redirects')
