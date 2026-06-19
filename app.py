import os
import logging
from flask import Flask, request
from models import db
from database import init_db

APP_VERSION = '1.0.37'


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
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
    app.config['APP_VERSION'] = APP_VERSION
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 7 * 24 * 60 * 60  # 1 week, for static assets

    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    db.init_app(app)

    # Register blueprints
    from routes.books import books_bp
    from routes.authors import authors_bp
    from routes.series import series_bp
    from routes.queue import queue_bp
    from routes.search import search_bp
    from routes.system import system_bp
    app.register_blueprint(books_bp)
    app.register_blueprint(authors_bp)
    app.register_blueprint(series_bp)
    app.register_blueprint(queue_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(system_bp)

    # Add bare-name URL rule aliases for every blueprint endpoint so that
    # existing templates using url_for('book_detail', ...) continue to work
    # without modification.
    import werkzeug.routing as _wr
    for _rule in list(app.url_map.iter_rules()):
        _ep = _rule.endpoint
        if '.' not in _ep:
            continue  # already bare (e.g. 'static')
        _bare = _ep.split('.', 1)[1]
        if _bare in app.view_functions:
            continue  # already registered under bare name
        # Register same view function under bare endpoint name
        app.view_functions[_bare] = app.view_functions[_ep]
        # Add a URL rule so url_for can reverse the bare endpoint
        app.add_url_rule(
            _rule.rule,
            endpoint=_bare,
            view_func=app.view_functions[_bare],
            methods=list(_rule.methods),
        )

    # Context processor
    @app.context_processor
    def inject_version():
        return {'app_version': APP_VERSION}

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
    app.run(debug=True, port=5001)
