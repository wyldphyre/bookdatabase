import os
import re
import socket
import ipaddress
from datetime import datetime
from urllib.parse import urlparse

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def parse_date(date_str):
    """Parse date string to datetime object."""
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        if dt.year < 1900 or dt.year > datetime.utcnow().year + 2:
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
