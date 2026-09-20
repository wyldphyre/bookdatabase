import os
import logging
from urllib.parse import urlparse
from flask import Flask, request, url_for
from models import db
from database import init_db
from price_watch import start_price_watch_scheduler
from series_monitor import start_series_monitor_scheduler
from utils import THUMB_SUBFOLDER, start_thumbnail_backfill

APP_VERSION = '1.4.2'


def create_app():
    app = Flask(__name__)

    _secret_key = os.environ.get('SECRET_KEY')
    if not _secret_key:
        logging.warning('SECRET_KEY env var not set — using insecure development default. Set SECRET_KEY for production.')
        _secret_key = 'dev-secret-key-change-in-production'
    app.config['SECRET_KEY'] = _secret_key
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///books.db'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['UPLOAD_FOLDER'] = os.path.join(app.static_folder, 'uploads')
    app.config['MAX_CONTENT_LENGTH'] = 512 * 1024 * 1024  # 512MB: import zips carry the full cover library
    app.config['APP_VERSION'] = APP_VERSION
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 7 * 24 * 60 * 60  # 1 week, for static assets

    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    db.init_app(app)

    # A restart orphans any parked export zip (its path lives only in memory)
    from data_transfer import cleanup_stale_exports
    cleanup_stale_exports()

    # Register blueprints
    from routes.books import books_bp
    from routes.authors import authors_bp
    from routes.series import series_bp
    from routes.queue import queue_bp
    from routes.search import search_bp
    from routes.system import system_bp
    from routes.price_watch import price_watch_bp
    app.register_blueprint(books_bp)
    app.register_blueprint(authors_bp)
    app.register_blueprint(series_bp)
    app.register_blueprint(queue_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(system_bp)
    app.register_blueprint(price_watch_bp)

    # Context processor
    @app.context_processor
    def inject_version():
        return {'app_version': APP_VERSION}

    @app.template_global('cover_thumb_url')
    def cover_thumb_url(filename):
        """URL of a cover's thumbnail for list/grid pages, falling back to the
        original when no thumb exists (small originals never get one)."""
        if filename and os.path.exists(os.path.join(app.config['UPLOAD_FOLDER'], THUMB_SUBFOLDER, filename)):
            return url_for('static', filename=f'uploads/{THUMB_SUBFOLDER}/{filename}')
        return url_for('static', filename=f'uploads/{filename}')

    # Anything that changes state must have been triggered from a page of this
    # app, not from some other site the browser happens to have open.
    #
    # The app has no login, so this isn't protecting one user's data from
    # another — anyone who can reach it on the network can use it directly.
    # What it stops is a drive-by: a malicious page can otherwise POST to this
    # app's address from your browser without the attacker having any access to
    # your network at all, and several endpoints here delete data or replace
    # the whole database. Checking the origin is enough for that, and avoids
    # putting a token in ~30 forms plus every htmx call.
    SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}

    @app.before_request
    def reject_cross_site_writes():
        if request.method in SAFE_METHODS:
            return None
        # Browsers send Origin on every POST, same-origin included, so a
        # mismatch is a genuine cross-site request. Referer is the fallback for
        # the DELETEs htmx issues.
        stated = request.headers.get('Origin') or request.headers.get('Referer')
        if not stated:
            # curl, scripts and the like send neither. A browser can't be made
            # to omit both on a cross-site write, so refusing here would only
            # break local tooling without closing anything.
            return None
        if urlparse(stated).netloc != request.host:
            logging.warning('Blocked cross-site %s to %s (origin %r)',
                            request.method, request.path, stated)
            return ('This request came from another site and was blocked.', 403)
        return None

    @app.errorhandler(OverflowError)
    def integer_out_of_range(error):
        """SQLite stores 64-bit integers; Python's have no such limit, so an
        oversized id or page number in a URL reaches the driver and raises.
        That's malformed input, not a server fault."""
        return ('A number in that request was out of range.', 400)

    # After-request hook
    @app.after_request
    def no_bfcache(response):
        # Don't disable caching for static assets (cover images, css, js) -
        # only dynamic pages need to be excluded from bfcache/disk cache.
        if not request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    # Template filters
    @app.template_filter('sort_by')
    def sort_by_filter(items, attribute, default=float('inf')):
        """Sort items by attribute, treating None as the default value."""
        return sorted(items, key=lambda x: getattr(x, attribute) if getattr(x, attribute) is not None else default)

    @app.template_filter('unique_series_count')
    def unique_series_count_filter(books):
        """Count unique series from a list of books."""
        series_ids = {book.series_id for book in books if book.series_id is not None}
        return len(series_ids)

    @app.template_filter('days_since')
    def days_since_filter(date):
        """Calculate days since a given date."""
        if not date:
            return None
        from datetime import date as date_type
        today = date_type.today()
        if hasattr(date, 'date'):
            date = date.date()
        return (today - date).days

    @app.template_filter('days_between')
    def days_between_filter(start_date, end_date):
        """Calculate days between two dates."""
        if not start_date or not end_date:
            return None
        if hasattr(start_date, 'date'):
            start_date = start_date.date()
        if hasattr(end_date, 'date'):
            end_date = end_date.date()
        return (end_date - start_date).days

    @app.template_filter('num')
    def num_filter(value):
        """Display a number without trailing .0"""
        if value is None:
            return ''
        if value == int(value):
            return str(int(value))
        return str(value)

    return app


app = create_app()
init_db(app)

if __name__ == '__main__':
    # Debug server: this module runs in both the reloader parent and the
    # serving child (WERKZEUG_RUN_MAIN=true); only the child gets a scheduler,
    # otherwise price checks run twice.
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        start_price_watch_scheduler(app)
        start_series_monitor_scheduler(app)
        start_thumbnail_backfill(app.config['UPLOAD_FOLDER'])
    app.run(debug=True, port=5001)
else:
    # Production (gunicorn, single worker): imported exactly once.
    start_price_watch_scheduler(app)
    start_series_monitor_scheduler(app)
    start_thumbnail_backfill(app.config['UPLOAD_FOLDER'])
