"""JSON endpoints for the browser extension and the iOS Shortcut.

Neither client is a web page, and neither endpoint should be callable by one:
the app has no login, so any site you visit could otherwise post to it from
your browser. The app-wide cross-site check (app.py) already refuses writes
from other origins, making an exception only for the extension's origin here.
On top of that, every request must carry the X-Book-Database-Client header —
which a web page can only send after a CORS preflight this app never answers —
and a request that declares a website as its Origin is refused outright. The
extension's origin is chrome-extension://…, and Shortcuts sends none.
"""
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from flask import Blueprint, current_app, jsonify, request, url_for

import capture
from scrapers import book_page_parser
from utils import clean_external_url

api_bp = Blueprint('api', __name__, url_prefix='/api')

CLIENT_HEADER = 'X-Book-Database-Client'

# A rendered Amazon page runs to about 5MB of HTML; leave headroom.
MAX_CAPTURE_BYTES = 15 * 1024 * 1024


@api_bp.before_request
def only_our_clients():
    if not request.headers.get(CLIENT_HEADER):
        return jsonify({'error': f'Missing {CLIENT_HEADER} header'}), 403
    origin = request.headers.get('Origin', '')
    if urlparse(origin).scheme in ('http', 'https'):
        return jsonify({'error': 'Not available to web pages'}), 403


@api_bp.route('/status', endpoint='status')
def status():
    """Lets a client confirm the address it was given is this app."""
    return jsonify({'app': 'Book Database', 'version': current_app.config['APP_VERSION'], 'api': 1})


@api_bp.route('/capture', methods=['POST'], endpoint='capture')
def capture_page():
    """Read a book page captured in the browser and hold it for the Add Book
    form. Takes JSON {"url": the page's address, "html": its rendered HTML}.
    Returns a preview, any library books it may duplicate, and the form's
    address for adding it as owned or not owned."""
    if (request.content_length or 0) > MAX_CAPTURE_BYTES:
        return jsonify({'error': 'That page is too large to capture'}), 413
    payload = request.get_json(silent=True) or {}
    url = payload.get('url')
    html = payload.get('html')
    if not isinstance(url, str) or not isinstance(html, str) or not html.strip():
        return jsonify({'error': 'Expected JSON with "url" and "html"'}), 400

    url = clean_external_url(url.strip())
    source, parser = book_page_parser(url)
    if not parser:
        return jsonify({'error': 'Not a Goodreads, Amazon or Kobo book page'}), 422

    try:
        book_data = parser(BeautifulSoup(html, 'html.parser'), url)
    except Exception:
        current_app.logger.exception('Capture: parsing a %s page failed', source)
        book_data = None
    if not book_data:
        return jsonify({'error': f"Couldn't find the book's details on that {source.title()} page"}), 422

    prefill = capture.build_prefill(book_data, create_tags=False)
    token = capture.hold(prefill)
    existing = capture.find_existing(book_data)

    return jsonify({
        'source': source,
        'title': prefill.get('title'),
        'authors': prefill.get('authors') or [],
        'series_name': prefill.get('series_name'),
        'series_number': prefill.get('series_number'),
        'cover_url': prefill.get('cover_url'),
        'existing': [{'id': b.id, 'title': b.title, 'owned': b.owned,
                      'url': url_for('books.book_detail', id=b.id, _external=True)} for b in existing],
        'add_owned_url': url_for('books.book_new', capture=token, _external=True),
        'add_not_owned_url': url_for('books.book_new', capture=token, owned=0, _external=True),
    })
