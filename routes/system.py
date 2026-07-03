import os
import json
import time
import threading
from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash
from sqlalchemy.orm import joinedload
from models import db, Book, Series, Tag, Author, AuthorGender, AuthorInfoSuggestion
from scrapers import search_goodreads_for_book, scrape_goodreads, scrape_goodreads_series, scrape_amazon_series
from author_info import lookup_author_info
from notifications import send_pushover_notification

system_bp = Blueprint('system', __name__)

genre_scan = {
    'status': 'idle',       # idle, running, paused, complete, stopped
    'progress': 0,
    'total': 0,
    'current_book': '',
    'tags_added': 0,
    'results': [],
    'paused': False,
    'stop_requested': False,
}
genre_scan_lock = threading.Lock()

series_scan = {
    'status': 'idle',
    'progress': 0,
    'total': 0,
    'current_series': '',
    'updated': 0,
    'results': [],
    'paused': False,
    'stop_requested': False,
}
series_scan_lock = threading.Lock()

author_scan = {
    'status': 'idle',
    'progress': 0,
    'total': 0,
    'current_author': '',
    'suggestions_found': 0,
    'synced': 0,
    'results': [],
    'paused': False,
    'stop_requested': False,
}
author_scan_lock = threading.Lock()


def _snapshot(scan, lock):
    """Return a consistent copy of a scan dict under lock."""
    with lock:
        s = dict(scan)
        s['results'] = list(scan['results'])
    return s


@system_bp.route('/system', endpoint='system')
def system():
    changelog_path = os.path.join(current_app.root_path, 'changelog.json')
    try:
        with open(changelog_path) as f:
            changelog = json.load(f)
    except (OSError, ValueError):
        changelog = []
    pushover_configured = bool(os.environ.get('PUSHOVER_USER_KEY')) and bool(os.environ.get('PUSHOVER_APP_TOKEN'))
    suggestions = AuthorInfoSuggestion.query.options(
        joinedload(AuthorInfoSuggestion.author).joinedload(Author.gender),
        joinedload(AuthorInfoSuggestion.suggested_gender)
    ).join(Author).order_by(Author.name).all()
    return render_template('system.html',
                           scan=_snapshot(genre_scan, genre_scan_lock),
                           series_scan=_snapshot(series_scan, series_scan_lock),
                           author_scan=_snapshot(author_scan, author_scan_lock),
                           suggestions=suggestions,
                           version=current_app.config['APP_VERSION'],
                           changelog=changelog,
                           pushover_configured=pushover_configured)


@system_bp.route('/system/pushover-test', methods=['POST'], endpoint='system_pushover_test')
def system_pushover_test():
    if send_pushover_notification('Test notification', 'Pushover is set up correctly.'):
        flash('Test notification sent — check your phone', 'success')
    else:
        flash('Failed to send test notification — check the Pushover credentials and the server logs', 'error')
    return redirect(url_for('system.system'))


@system_bp.route('/system/scan-genres', methods=['POST'], endpoint='scan_genres_start')
def scan_genres_start():
    untagged_only = request.form.get('untagged_only') == 'on'

    with genre_scan_lock:
        # Refuse while a scan thread is alive ('paused' included — resetting its
        # state here would unpause it and leave two threads running).
        already_active = genre_scan['status'] in ('running', 'paused')
        if not already_active:
            genre_scan.update({
                'status': 'running',
                'progress': 0,
                'total': 0,
                'current_book': '',
                'tags_added': 0,
                'results': [],
                'paused': False,
                'stop_requested': False,
            })

    if not already_active:
        _app = current_app._get_current_object()
        thread = threading.Thread(target=run_genre_scan, args=(_app, untagged_only), daemon=True)
        thread.start()

    return render_template('system/_scan_progress.html', scan=_snapshot(genre_scan, genre_scan_lock))


@system_bp.route('/system/scan-genres/progress', endpoint='scan_genres_progress')
def scan_genres_progress():
    return render_template('system/_scan_progress.html', scan=_snapshot(genre_scan, genre_scan_lock))


@system_bp.route('/system/scan-genres/pause', methods=['POST'], endpoint='scan_genres_pause')
def scan_genres_pause():
    with genre_scan_lock:
        if genre_scan['status'] == 'running':
            genre_scan['paused'] = True
            genre_scan['status'] = 'paused'
        elif genre_scan['status'] == 'paused':
            genre_scan['paused'] = False
            genre_scan['status'] = 'running'
    return render_template('system/_scan_progress.html', scan=_snapshot(genre_scan, genre_scan_lock))


@system_bp.route('/system/scan-genres/stop', methods=['POST'], endpoint='scan_genres_stop')
def scan_genres_stop():
    with genre_scan_lock:
        genre_scan['stop_requested'] = True
    return render_template('system/_scan_progress.html', scan=_snapshot(genre_scan, genre_scan_lock))


def run_genre_scan(app, untagged_only):
    """Background thread that scans Goodreads for genres and imports as tags."""
    with app.app_context():
        query = Book.query.options(
            joinedload(Book.authors),
            joinedload(Book.tags)
        )

        if untagged_only:
            query = query.filter(~Book.tags.any())

        books = query.all()
        with genre_scan_lock:
            genre_scan['total'] = len(books)

        for i, book in enumerate(books):
            # Check for stop
            if genre_scan['stop_requested']:
                with genre_scan_lock:
                    genre_scan['status'] = 'stopped'
                    genre_scan['current_book'] = ''
                return

            # Handle pause
            while genre_scan['paused']:
                if genre_scan['stop_requested']:
                    with genre_scan_lock:
                        genre_scan['status'] = 'stopped'
                        genre_scan['current_book'] = ''
                    return
                time.sleep(0.5)

            with genre_scan_lock:
                genre_scan['current_book'] = book.title
                genre_scan['progress'] = i

            author_names = ', '.join(a.name for a in book.authors) if book.authors else ''

            try:
                # Search Goodreads for this book
                book_url = search_goodreads_for_book(book.title, author_names)
                if not book_url:
                    with genre_scan_lock:
                        genre_scan['results'].append({
                            'book': book.title,
                            'status': 'not_found',
                        })
                    time.sleep(1)
                    continue

                # Scrape the Goodreads page for genres
                book_data = scrape_goodreads(book_url)
                if not book_data or not book_data.get('genres'):
                    with genre_scan_lock:
                        genre_scan['results'].append({
                            'book': book.title,
                            'status': 'no_genres',
                        })
                    time.sleep(1)
                    continue

                # Find or create tags and add to book
                new_tags = []
                for genre_name in book_data['genres']:
                    tag = Tag.query.filter(db.func.lower(Tag.name) == genre_name.lower()).first()
                    if not tag:
                        tag = Tag(name=genre_name)
                        db.session.add(tag)
                        db.session.commit()

                    if tag not in book.tags:
                        book.tags.append(tag)
                        new_tags.append(tag.name)

                if new_tags:
                    db.session.commit()

                with genre_scan_lock:
                    if new_tags:
                        genre_scan['tags_added'] += len(new_tags)
                    genre_scan['results'].append({
                        'book': book.title,
                        'status': 'found',
                        'tags': new_tags if new_tags else book_data['genres'],
                    })

            except Exception as e:
                with genre_scan_lock:
                    genre_scan['results'].append({
                        'book': book.title,
                        'status': 'error',
                        'message': str(e),
                    })

            # Brief delay to avoid rate limiting
            time.sleep(2)

        with genre_scan_lock:
            genre_scan['progress'] = genre_scan['total']
            genre_scan['current_book'] = ''
            genre_scan['status'] = 'complete'


@system_bp.route('/system/scan-series', methods=['POST'], endpoint='scan_series_start')
def scan_series_start():
    with series_scan_lock:
        # Refuse while a scan thread is alive ('paused' included — resetting its
        # state here would unpause it and leave two threads running).
        already_active = series_scan['status'] in ('running', 'paused')
        if not already_active:
            series_scan.update({
                'status': 'running',
                'progress': 0,
                'total': 0,
                'current_series': '',
                'updated': 0,
                'results': [],
                'paused': False,
                'stop_requested': False,
            })

    if not already_active:
        _app = current_app._get_current_object()
        thread = threading.Thread(target=run_series_scan, args=(_app,), daemon=True)
        thread.start()

    return render_template('system/_series_scan_progress.html', scan=_snapshot(series_scan, series_scan_lock))


@system_bp.route('/system/scan-series/progress', endpoint='scan_series_progress')
def scan_series_progress():
    return render_template('system/_series_scan_progress.html', scan=_snapshot(series_scan, series_scan_lock))


@system_bp.route('/system/scan-series/pause', methods=['POST'], endpoint='scan_series_pause')
def scan_series_pause():
    with series_scan_lock:
        if series_scan['status'] == 'running':
            series_scan['paused'] = True
            series_scan['status'] = 'paused'
        elif series_scan['status'] == 'paused':
            series_scan['paused'] = False
            series_scan['status'] = 'running'
    return render_template('system/_series_scan_progress.html', scan=_snapshot(series_scan, series_scan_lock))


@system_bp.route('/system/scan-series/stop', methods=['POST'], endpoint='scan_series_stop')
def scan_series_stop():
    with series_scan_lock:
        series_scan['stop_requested'] = True
    return render_template('system/_series_scan_progress.html', scan=_snapshot(series_scan, series_scan_lock))


def run_series_scan(app):
    """Background thread that scans Goodreads/Amazon for series book counts."""
    with app.app_context():
        all_series = Series.query.filter(
            (Series.goodreads_url.isnot(None) & (Series.goodreads_url != '')) |
            (Series.amazon_url.isnot(None) & (Series.amazon_url != ''))
        ).order_by(Series.name).all()

        with series_scan_lock:
            series_scan['total'] = len(all_series)

        for i, series in enumerate(all_series):
            if series_scan['stop_requested']:
                with series_scan_lock:
                    series_scan['status'] = 'stopped'
                    series_scan['current_series'] = ''
                return

            while series_scan['paused']:
                if series_scan['stop_requested']:
                    with series_scan_lock:
                        series_scan['status'] = 'stopped'
                        series_scan['current_series'] = ''
                    return
                time.sleep(0.5)

            with series_scan_lock:
                series_scan['current_series'] = series.name
                series_scan['progress'] = i

            try:
                count = None
                source = None
                if series.goodreads_url:
                    count = scrape_goodreads_series(series.goodreads_url)
                    if count is not None:
                        source = 'Goodreads'
                if count is None and series.amazon_url:
                    count = scrape_amazon_series(series.amazon_url)
                    if count is not None:
                        source = 'Amazon'

                if count is not None:
                    old_count = series.number_in_series
                    if old_count is None or count > old_count:
                        series.number_in_series = count
                        db.session.commit()
                        with series_scan_lock:
                            series_scan['updated'] += 1
                            series_scan['results'].append({
                                'series': series.name,
                                'status': 'updated',
                                'old_count': old_count,
                                'new_count': count,
                                'source': source,
                            })
                    else:
                        with series_scan_lock:
                            series_scan['results'].append({
                                'series': series.name,
                                'status': 'unchanged',
                                'count': count,
                                'source': source,
                            })
                else:
                    with series_scan_lock:
                        series_scan['results'].append({
                            'series': series.name,
                            'status': 'not_found',
                        })

            except Exception as e:
                with series_scan_lock:
                    series_scan['results'].append({
                        'series': series.name,
                        'status': 'error',
                        'message': str(e),
                    })

            time.sleep(2)

        with series_scan_lock:
            series_scan['progress'] = series_scan['total']
            series_scan['current_series'] = ''
            series_scan['status'] = 'complete'


@system_bp.route('/system/scan-authors', methods=['POST'], endpoint='scan_authors_start')
def scan_authors_start():
    with author_scan_lock:
        # Refuse while a scan thread is alive ('paused' included — resetting its
        # state here would unpause it and leave two threads running).
        already_active = author_scan['status'] in ('running', 'paused')
        if not already_active:
            author_scan.update({
                'status': 'running',
                'progress': 0,
                'total': 0,
                'current_author': '',
                'suggestions_found': 0,
                'synced': 0,
                'results': [],
                'paused': False,
                'stop_requested': False,
            })

    if not already_active:
        _app = current_app._get_current_object()
        thread = threading.Thread(target=run_author_scan, args=(_app,), daemon=True)
        thread.start()

    return render_template('system/_author_scan_progress.html', scan=_snapshot(author_scan, author_scan_lock))


@system_bp.route('/system/scan-authors/progress', endpoint='scan_authors_progress')
def scan_authors_progress():
    return render_template('system/_author_scan_progress.html', scan=_snapshot(author_scan, author_scan_lock))


@system_bp.route('/system/scan-authors/pause', methods=['POST'], endpoint='scan_authors_pause')
def scan_authors_pause():
    with author_scan_lock:
        if author_scan['status'] == 'running':
            author_scan['paused'] = True
            author_scan['status'] = 'paused'
        elif author_scan['status'] == 'paused':
            author_scan['paused'] = False
            author_scan['status'] = 'running'
    return render_template('system/_author_scan_progress.html', scan=_snapshot(author_scan, author_scan_lock))


@system_bp.route('/system/scan-authors/stop', methods=['POST'], endpoint='scan_authors_stop')
def scan_authors_stop():
    with author_scan_lock:
        author_scan['stop_requested'] = True
    return render_template('system/_author_scan_progress.html', scan=_snapshot(author_scan, author_scan_lock))


def _author_needs_info(author, unknown_id):
    needs_pronouns = not author.pronouns
    needs_gender = author.gender_id is None or author.gender_id == unknown_id
    return needs_pronouns, needs_gender


def _sync_author_aliases():
    """Copy gender/pronouns between alias and primary author records when one
    side has the data and the other doesn't. Returns list of synced names."""
    unknown = AuthorGender.query.filter(db.func.lower(AuthorGender.name) == 'unknown').first()
    unknown_id = unknown.id if unknown else -1
    synced = []
    for alias in Author.query.filter(Author.alias_of_id.isnot(None)).all():
        primary = alias.alias_of
        if not primary:
            continue
        for src, dst in ((primary, alias), (alias, primary)):
            changed = False
            if src.pronouns and not dst.pronouns:
                dst.pronouns = src.pronouns
                changed = True
            if src.gender_id and src.gender_id != unknown_id and \
                    (dst.gender_id is None or dst.gender_id == unknown_id):
                dst.gender_id = src.gender_id
                changed = True
            if changed:
                synced.append(dst.name)
    if synced:
        db.session.commit()
    return synced


def run_author_scan(app):
    """Background thread that looks up gender/pronouns for authors missing
    them and records suggestions for review. Never writes to the author
    directly, except for the alias<->primary sync pre-pass."""
    with app.app_context():
        # Free pre-pass: sync data between aliases and their primary record
        synced = _sync_author_aliases()
        with author_scan_lock:
            author_scan['synced'] = len(synced)
            for name in synced:
                author_scan['results'].append({'author': name, 'status': 'synced'})

        unknown = AuthorGender.query.filter(db.func.lower(AuthorGender.name) == 'unknown').first()
        unknown_id = unknown.id if unknown else -1

        authors = Author.query.filter(
            Author.alias_of_id.is_(None),
            db.or_(
                Author.pronouns.is_(None), Author.pronouns == '',
                Author.gender_id.is_(None), Author.gender_id == unknown_id,
            )
        ).order_by(Author.name).all()

        with author_scan_lock:
            author_scan['total'] = len(authors)

        for i, author in enumerate(authors):
            if author_scan['stop_requested']:
                with author_scan_lock:
                    author_scan['status'] = 'stopped'
                    author_scan['current_author'] = ''
                return

            while author_scan['paused']:
                if author_scan['stop_requested']:
                    with author_scan_lock:
                        author_scan['status'] = 'stopped'
                        author_scan['current_author'] = ''
                    return
                time.sleep(0.5)

            with author_scan_lock:
                author_scan['current_author'] = author.name
                author_scan['progress'] = i

            needs_pronouns, needs_gender = _author_needs_info(author, unknown_id)

            try:
                info = lookup_author_info(author.name, author.goodreads_url, author.website)

                suggested_pronouns = info['pronouns'] if info and needs_pronouns else None
                suggested_gender = None
                if info and needs_gender and info['gender']:
                    suggested_gender = AuthorGender.query.filter(
                        db.func.lower(AuthorGender.name) == info['gender'].lower()).first()

                if suggested_pronouns or suggested_gender:
                    AuthorInfoSuggestion.query.filter_by(author_id=author.id).delete()
                    db.session.add(AuthorInfoSuggestion(
                        author_id=author.id,
                        suggested_gender_id=suggested_gender.id if suggested_gender else None,
                        suggested_pronouns=suggested_pronouns,
                        evidence=info['evidence'],
                        source_url=info['source_url'],
                    ))
                    db.session.commit()
                    with author_scan_lock:
                        author_scan['suggestions_found'] += 1
                        author_scan['results'].append({
                            'author': author.name,
                            'status': 'suggested',
                            'pronouns': suggested_pronouns,
                            'gender': suggested_gender.name if suggested_gender else None,
                        })
                else:
                    with author_scan_lock:
                        author_scan['results'].append({
                            'author': author.name,
                            'status': 'not_found',
                        })

            except Exception as e:
                with author_scan_lock:
                    author_scan['results'].append({
                        'author': author.name,
                        'status': 'error',
                        'message': str(e),
                    })

            # Brief delay to be polite to the sources
            time.sleep(2)

        with author_scan_lock:
            author_scan['progress'] = author_scan['total']
            author_scan['current_author'] = ''
            author_scan['status'] = 'complete'


@system_bp.route('/system/author-suggestions/<int:id>/accept', methods=['POST'], endpoint='author_suggestion_accept')
def author_suggestion_accept(id):
    suggestion = db.get_or_404(AuthorInfoSuggestion, id)
    if suggestion.suggested_gender_id:
        suggestion.author.gender_id = suggestion.suggested_gender_id
    if suggestion.suggested_pronouns:
        suggestion.author.pronouns = suggestion.suggested_pronouns
    db.session.delete(suggestion)
    db.session.commit()
    return ''


@system_bp.route('/system/author-suggestions/<int:id>/reject', methods=['POST', 'DELETE'], endpoint='author_suggestion_reject')
def author_suggestion_reject(id):
    suggestion = db.get_or_404(AuthorInfoSuggestion, id)
    db.session.delete(suggestion)
    db.session.commit()
    return ''


@system_bp.route('/system/author-suggestions/accept-all', methods=['POST'], endpoint='author_suggestion_accept_all')
def author_suggestion_accept_all():
    suggestions = AuthorInfoSuggestion.query.all()
    for suggestion in suggestions:
        if suggestion.suggested_gender_id:
            suggestion.author.gender_id = suggestion.suggested_gender_id
        if suggestion.suggested_pronouns:
            suggestion.author.pronouns = suggestion.suggested_pronouns
        db.session.delete(suggestion)
    db.session.commit()
    flash(f'Applied {len(suggestions)} author info suggestion(s)', 'success')
    return redirect(url_for('system.system'))


@system_bp.route('/system/tags/search', endpoint='system_tag_search')
def system_tag_search():
    query = request.args.get('q', '').strip()
    if len(query) < 1:
        return ''
    tags = Tag.query.filter(Tag.name.ilike(f'%{query}%')).order_by(Tag.name).limit(50).all()
    return render_template('system/_tag_results.html', tags=tags, query=query)


@system_bp.route('/system/tags/<int:id>/rename', methods=['POST'], endpoint='system_tag_rename')
def system_tag_rename(id):
    tag = db.get_or_404(Tag, id)
    new_name = request.form.get('name', '').strip()
    if not new_name:
        return render_template('system/_tag_row.html', tag=tag, error='Name is required')
    existing = Tag.query.filter(db.func.lower(Tag.name) == new_name.lower(), Tag.id != id).first()
    if existing:
        return render_template('system/_tag_row.html', tag=tag, error=f'A tag named "{existing.name}" already exists')
    tag.name = new_name
    db.session.commit()
    return render_template('system/_tag_row.html', tag=tag)


@system_bp.route('/system/tags/<int:id>/delete', methods=['DELETE', 'POST'], endpoint='system_tag_delete')
def system_tag_delete(id):
    tag = db.get_or_404(Tag, id)
    tag.books = []
    tag.authors = []
    tag.series = []
    db.session.delete(tag)
    db.session.commit()
    return ''


@system_bp.route('/tags/search', endpoint='tag_search')
def tag_search():
    """Search tags for the tag picker."""
    query = request.args.get('q', '').strip()
    exclude_str = request.args.get('exclude', '')

    exclude_ids = []
    if exclude_str:
        exclude_ids = [int(x) for x in exclude_str.split(',') if x.strip().isdigit()]

    if len(query) < 1:
        return ''

    tags = Tag.query.filter(Tag.name.ilike(f'%{query}%'))

    if exclude_ids:
        tags = tags.filter(~Tag.id.in_(exclude_ids))

    tags = tags.order_by(Tag.name).limit(10).all()
    return render_template('books/_tag_search_results.html', tags=tags, query=query)


@system_bp.route('/tags/quick-add', methods=['POST'], endpoint='tag_quick_add')
def tag_quick_add():
    """Quick add a tag via htmx from a form."""
    name = request.form.get('tag_name', '').strip()
    if not name:
        return '<p class="error">Name is required</p>', 400

    # Check if tag already exists (case-insensitive)
    existing = Tag.query.filter(db.func.lower(Tag.name) == name.lower()).first()
    if existing:
        return render_template('books/_tag_chip.html', tag=existing)

    tag = Tag(name=name)
    db.session.add(tag)
    db.session.commit()

    return render_template('books/_tag_chip.html', tag=tag)
