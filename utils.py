import os
import re
import socket
import ipaddress
import threading
from datetime import datetime
from urllib.parse import urlparse, urljoin

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

THUMB_SUBFOLDER = 'thumbs'
# 2x the ~200px-wide grid cards, so thumbs stay sharp on hidpi screens
THUMB_MAX_SIZE = (400, 640)


def thumb_path(upload_folder, filename):
    return os.path.join(upload_folder, THUMB_SUBFOLDER, filename)


def generate_thumbnail(upload_folder, filename):
    """Create a small copy of a cover for list/grid pages, keeping the same
    filename under uploads/thumbs/. Originals that already fit THUMB_MAX_SIZE
    get no thumb — cover_thumb_url falls back to the original. Returns True
    if a thumb was written. Thumbnailing is best-effort: any unreadable or
    hostile image (including Pillow's DecompressionBombError, which is not an
    OSError) is skipped rather than failing the book save or killing the
    backfill thread — the page falls back to the original."""
    from PIL import Image
    dst = thumb_path(upload_folder, filename)
    try:
        with Image.open(os.path.join(upload_folder, filename)) as img:
            if img.width <= THUMB_MAX_SIZE[0] and img.height <= THUMB_MAX_SIZE[1]:
                return False
            img.thumbnail(THUMB_MAX_SIZE)
            if dst.lower().endswith(('.jpg', '.jpeg')) and img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            img.save(dst, quality=80)
            return True
    except Exception:
        return False


def delete_thumbnail(upload_folder, filename):
    path = thumb_path(upload_folder, filename)
    if os.path.exists(path):
        os.remove(path)


_backfill_lock = threading.Lock()


def backfill_thumbnails(upload_folder):
    """Generate any missing thumbnails and prune thumbs whose original is
    gone. Cheap when everything is up to date (header reads only), so it's
    safe to run at every startup and after an import. Serialized under a
    lock so an import-triggered run can't race a still-running startup run,
    and the prune tolerates files vanishing between listdir and remove."""
    with _backfill_lock:
        thumbs_dir = os.path.join(upload_folder, THUMB_SUBFOLDER)
        try:
            os.makedirs(thumbs_dir, exist_ok=True)
            for entry in os.listdir(thumbs_dir):
                if not os.path.isfile(os.path.join(upload_folder, entry)):
                    try:
                        os.remove(os.path.join(thumbs_dir, entry))
                    except OSError:
                        pass
            for entry in os.listdir(upload_folder):
                if entry.startswith('.') or not allowed_file(entry):
                    continue
                if os.path.isfile(os.path.join(upload_folder, entry)) and \
                        not os.path.exists(thumb_path(upload_folder, entry)):
                    generate_thumbnail(upload_folder, entry)
        except FileNotFoundError:
            # An import wiped the folder out from under us mid-run; the
            # post-import backfill will regenerate everything.
            return


def start_thumbnail_backfill(upload_folder):
    threading.Thread(target=backfill_thumbnails, args=(upload_folder,), daemon=True).start()


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
