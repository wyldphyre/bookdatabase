import os
import json
import time
import threading
from datetime import date, datetime
from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash, send_file
from sqlalchemy.orm import joinedload
from models import db, Book, Series, Tag, Author, AuthorGender, AuthorInfoSuggestion, set_setting
from scrapers import scrape_goodreads_series, scrape_amazon_series, ScrapeBlockedError
from author_info import lookup_author_info
import hardcover
import genre_sources
from notifications import (send_pushover_notification, get_pushover_priority,
                           PUSHOVER_PRIORITIES, VALID_PRIORITIES, PRIORITY_SETTING_KEY)
from utils import start_thumbnail_backfill
from data_transfer import (build_export_zip, validate_import_zip, apply_import,
                           ImportValidationError, ImportCoverError, PRE_IMPORT_BACKUP_NAME)

system_bp = Blueprint('system', __name__)

# Which books a genre scan walks. Three, not two, because "has some tags" and
# "has had Goodreads asked about it" stopped being the same question once a
# fallback could tag a book on its own.
SCAN_UNTAGGED = 'untagged'          # no tags at all
SCAN_NOT_GOODREADS = 'not_goodreads'  # untagged, or tagged by a fallback source
SCAN_ALL = 'all'
SCAN_SCOPES = (SCAN_UNTAGGED, SCAN_NOT_GOODREADS, SCAN_ALL)

genre_scan = {
    'status': 'idle',       # idle, running, paused, complete, stopped
    'progress': 0,
    'total': 0,
    'current_book': '',
    'tags_added': 0,
    'results': [],
    'paused': False,
    'stop_requested': False,
    'stop_reason': '',
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
    'stop_reason': '',
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
    'stop_reason': '',
}
author_scan_lock = threading.Lock()

export_state = {
    'status': 'idle',       # idle, building, ready, error
    'progress': 0,          # covers added so far
    'total': 0,             # total covers
    'path': None,           # temp file holding the finished zip
    'size_label': '',
    'built_at': '',
    'book_count': 0,
    'cover_count': 0,
    'error': '',
}
export_lock = threading.Lock()


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
    return render_template('system.html',
                           tag_sources=genre_sources.source_status(),
                           scan=_snapshot(genre_scan, genre_scan_lock),
                           series_scan=_snapshot(series_scan, series_scan_lock),
                           author_scan=_snapshot(author_scan, author_scan_lock),
                           suggestions=_load_suggestions(),
                           version=current_app.config['APP_VERSION'],
                           changelog=changelog,
                           pushover_configured=pushover_configured,
                           pushover_priority=get_pushover_priority(),
                           pushover_priorities=PUSHOVER_PRIORITIES,
                           export=_export_snapshot(),
                           pending_import=_pending_import_manifest(),
                           current_book_count=Book.query.count())


def _pending_import_path():
    return os.path.join(current_app.instance_path, 'import_pending.zip')


def _pending_import_manifest():
    """Manifest of the uploaded-but-unconfirmed import zip, or None. A file
    that no longer validates (e.g. truncated upload) is discarded."""
    path = _pending_import_path()
    if not os.path.exists(path):
        return None
    try:
        return validate_import_zip(path)
    except ImportValidationError:
        os.unlink(path)
        return None


def _export_snapshot():
    with export_lock:
        return dict(export_state)


@system_bp.route('/system/export/start', methods=['POST'], endpoint='system_export_start')
def system_export_start():
    with export_lock:
        if export_state['status'] != 'building':
            # Drop the previous build before starting a new one
            if export_state['path'] and os.path.exists(export_state['path']):
                os.unlink(export_state['path'])
            export_state.update(status='building', progress=0, total=0, path=None,
                                size_label='', built_at='', book_count=0,
                                cover_count=0, error='')
            _app = current_app._get_current_object()
            thread = threading.Thread(target=run_export_build, args=(_app,), daemon=True)
            thread.start()
    return render_template('system/_export_progress.html', export=_export_snapshot())


@system_bp.route('/system/export/progress', endpoint='system_export_progress')
def system_export_progress():
    return render_template('system/_export_progress.html', export=_export_snapshot())


@system_bp.route('/system/export/download', endpoint='system_export_download')
def system_export_download():
    with export_lock:
        path = export_state['path'] if export_state['status'] == 'ready' else None
    if not path or not os.path.exists(path):
        flash('No export is ready — build one first', 'error')
        return redirect(url_for('system.system') + '#import-export')
    # The file is kept so the download can be retried; it's replaced on the next build.
    return send_file(path, as_attachment=True, mimetype='application/zip',
                     download_name=f'bookdb-export-{date.today().isoformat()}.zip')


def run_export_build(app):
    """Background thread that builds the export zip and parks it for download."""
    def on_progress(done, total):
        with export_lock:
            export_state['progress'] = done
            export_state['total'] = total

    try:
        with app.app_context():
            zip_path, manifest = build_export_zip(app.config['UPLOAD_FOLDER'],
                                                  app.config['APP_VERSION'],
                                                  progress=on_progress)
        size = os.path.getsize(zip_path)
        with export_lock:
            export_state.update(status='ready', path=zip_path,
                                size_label=f'{size / (1024 * 1024):.0f} MB',
                                built_at=datetime.now().strftime('%H:%M'),
                                book_count=manifest['counts']['book'],
                                cover_count=manifest['cover_count'])
    except Exception as e:
        with export_lock:
            export_state.update(status='error', error=str(e))


@system_bp.route('/system/import', methods=['POST'], endpoint='system_import_upload')
def system_import_upload():
    file = request.files.get('file')
    if not file or not file.filename:
        flash('Choose an export zip file to upload', 'error')
        return redirect(url_for('system.system') + '#import-export')
    path = _pending_import_path()
    file.save(path)
    try:
        validate_import_zip(path)
    except ImportValidationError as e:
        os.unlink(path)
        flash(str(e), 'error')
    return redirect(url_for('system.system') + '#import-export')


@system_bp.route('/system/import/confirm', methods=['POST'], endpoint='system_import_confirm')
def system_import_confirm():
    path = _pending_import_path()
    if not os.path.exists(path):
        flash('No uploaded import file found — upload the export zip again', 'error')
        return redirect(url_for('system.system') + '#import-export')
    with export_lock:
        export_building = export_state['status'] == 'building'
    if export_building:
        # The export thread is reading the same uploads folder the import
        # would wipe — let it finish first.
        flash('An export is currently building — wait for it to finish before importing', 'error')
        return redirect(url_for('system.system') + '#import-export')
    try:
        result = apply_import(path, current_app.config['UPLOAD_FOLDER'])
        start_thumbnail_backfill(current_app.config['UPLOAD_FOLDER'])
        flash(f"Import complete: {result['books']} books, {result['authors']} authors, "
              f"{result['series']} series, {result['reads']} reads and {result['covers']} covers restored. "
              f"The previous database was saved as {PRE_IMPORT_BACKUP_NAME}. "
              f"Cover thumbnails are being regenerated in the background.", 'success')
    except ImportValidationError as e:
        flash(str(e), 'error')
    except ImportCoverError as e:
        # The rows are imported; only the cover files are suspect.
        flash(str(e), 'error')
    except Exception as e:
        current_app.logger.exception('Import failed')
        db.session.rollback()
        flash(f'Import failed and the database was left unchanged: {e}', 'error')
    finally:
        if os.path.exists(path):
            os.unlink(path)
    return redirect(url_for('system.system'))


@system_bp.route('/system/import/cancel', methods=['POST'], endpoint='system_import_cancel')
def system_import_cancel():
    path = _pending_import_path()
    if os.path.exists(path):
        os.unlink(path)
    return redirect(url_for('system.system') + '#import-export')


def _load_suggestions():
    return AuthorInfoSuggestion.query.options(
        joinedload(AuthorInfoSuggestion.author).joinedload(Author.gender),
        joinedload(AuthorInfoSuggestion.suggested_gender)
    ).join(Author).order_by(Author.name).all()


@system_bp.route('/system/author-suggestions', endpoint='author_suggestions_partial')
def author_suggestions_partial():
    """The suggestions review section, fetched via htmx so the 'Review
    suggestions' button works without a page reload (the section may not have
    existed when the page was first rendered)."""
    return render_template('system/_author_suggestions.html', suggestions=_load_suggestions())


@system_bp.route('/system/pushover-priority', methods=['POST'], endpoint='system_pushover_priority')
def system_pushover_priority():
    try:
        priority = int(request.form.get('priority', ''))
    except ValueError:
        priority = None
    if priority not in VALID_PRIORITIES:
        flash('Invalid notification priority', 'error')
        return redirect(url_for('system.system'))

    set_setting(PRIORITY_SETTING_KEY, priority)
    label = dict(PUSHOVER_PRIORITIES)[priority]
    flash(f'Notification priority set to {label}', 'success')
    return redirect(url_for('system.system'))


@system_bp.route('/system/pushover-test', methods=['POST'], endpoint='system_pushover_test')
def system_pushover_test():
    if send_pushover_notification('Test notification', 'Pushover is set up correctly.'):
        flash('Test notification sent — check your phone', 'success')
    else:
        flash('Failed to send test notification — check the Pushover credentials and the server logs', 'error')
    return redirect(url_for('system.system'))


@system_bp.route('/system/scan-genres', methods=['POST'], endpoint='scan_genres_start')
def scan_genres_start():
    scope = request.form.get('scope', SCAN_UNTAGGED)
    if scope not in SCAN_SCOPES:
        scope = SCAN_UNTAGGED

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
                'stop_reason': '',
            })

    if not already_active:
        _app = current_app._get_current_object()
        thread = threading.Thread(target=run_genre_scan, args=(_app, scope), daemon=True)
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


def run_genre_scan(app, scope):
    """Background thread that scans Goodreads for genres and imports as tags."""
    try:
        _run_genre_scan(app, scope)
    except Exception as e:
        # Without this, an uncaught error would leave the status stuck at
        # 'running' with the progress bar frozen and polling forever — and
        # scan_genres_start refuses to start another while it is.
        with genre_scan_lock:
            genre_scan['results'].append({'book': '(scan aborted)', 'status': 'error', 'message': str(e)})
            genre_scan['current_book'] = ''
            genre_scan['status'] = 'stopped'


def _run_genre_scan(app, scope):
    with app.app_context():
        query = Book.query.options(
            joinedload(Book.authors),
            joinedload(Book.tags)
        )

        if scope == SCAN_UNTAGGED:
            query = query.filter(~Book.tags.any())
        elif scope == SCAN_NOT_GOODREADS:
            # Books a fallback tagged while Goodreads was unavailable, plus any
            # still untagged. Both are books Goodreads has not answered for, and
            # both are worth another pass once it is talking again.
            query = query.filter((Book.genre_source.is_(None))
                                 | (Book.genre_source != genre_sources.GOODREADS))

        books = query.all()
        with genre_scan_lock:
            genre_scan['total'] = len(books)

        # Set once Goodreads has refused us, so the rest of the run stops
        # asking it and leans on Hardcover alone.
        goodreads_blocked = False

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

            try:
                # A scan is judged on finishing, not on any single answer, so
                # it takes the sources that can't block first and leaves
                # Goodreads — the best of them, and the one that stops
                # answering — for whatever the others could not place.
                genres, source = genre_sources.fetch_genres(
                    book, chain=genre_sources.SCAN_CHAIN,
                    unavailable=(genre_sources.GOODREADS,) if goodreads_blocked else ())
                if genres is None:
                    # Goodreads couldn't identify the book at all.
                    with genre_scan_lock:
                        genre_scan['results'].append({
                            'book': book.title,
                            'status': 'not_found',
                        })
                    time.sleep(1)
                    continue
                if not genres:
                    with genre_scan_lock:
                        # Once Goodreads has blocked us, an empty result means
                        # only that the other sources don't hold the book —
                        # the best one was never asked. Reporting that as "no
                        # genres listed" would claim all of them were checked.
                        genre_scan['results'].append({
                            'book': book.title,
                            'status': 'source_unavailable' if goodreads_blocked else 'no_genres',
                        })
                    time.sleep(1)
                    continue

                new_tags = genre_sources.apply_genres(book, genres, source)

                with genre_scan_lock:
                    if new_tags:
                        genre_scan['tags_added'] += len(new_tags)
                    genre_scan['results'].append({
                        'book': book.title,
                        'status': 'found',
                        'source': source,
                        'tags': new_tags if new_tags else genres,
                    })

            except ScrapeBlockedError as e:
                # Goodreads is turning us away, so every later book would fail
                # there too — stop asking it. That used to end the scan, which
                # is right when Goodreads is the only source but wasteful now:
                # Hardcover answers for most of this library and isn't blocking
                # us, so the run carries on with whatever it can still tag.
                with genre_scan_lock:
                    genre_scan['results'].append({
                        'book': book.title,
                        'status': 'blocked',
                        'message': str(e),
                    })
                    genre_scan['stop_reason'] = str(e)
                    if not hardcover.is_configured():
                        genre_scan['stop_requested'] = True
                if not hardcover.is_configured():
                    break
                goodreads_blocked = True
            except Exception as e:
                # Leave the session usable. Without this a failed write — an
                # IntegrityError, or SQLite refusing a locked database while the
                # web side writes — leaves the transaction poisoned, and the next
                # book's attribute access outside this try raises
                # PendingRollbackError, which escapes to the thread guard and ends
                # the whole run with nothing tagged.
                db.session.rollback()
                with genre_scan_lock:
                    genre_scan['results'].append({
                        'book': book.title,
                        'status': 'error',
                        'message': str(e),
                    })

            # Brief delay to avoid rate limiting
            time.sleep(2)

        with genre_scan_lock:
            # A scan that broke out early (the site blocked us, or the user
            # pressed Stop) hasn't covered its total. Filling the bar and
            # calling it complete would claim the whole run happened and
            # hide the stop_reason, which only the 'stopped' branch shows.
            stopped_early = genre_scan['stop_requested']
            if not stopped_early:
                genre_scan['progress'] = genre_scan['total']
            genre_scan['current_book'] = ''
            genre_scan['status'] = 'stopped' if stopped_early else 'complete'


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
                'stop_reason': '',
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
    try:
        _run_series_scan(app)
    except Exception as e:
        # See run_genre_scan: a wedged 'running' status blocks all later scans.
        with series_scan_lock:
            series_scan['results'].append({'series': '(scan aborted)', 'status': 'error', 'message': str(e)})
            series_scan['current_series'] = ''
            series_scan['status'] = 'stopped'


def _run_series_scan(app):
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

            except ScrapeBlockedError as e:
                # The site is turning us away — the rest of the run would fail
                # identically, so stop rather than hammering it.
                with series_scan_lock:
                    series_scan['results'].append({
                        'series': series.name,
                        'status': 'blocked',
                        'message': str(e),
                    })
                    series_scan['stop_reason'] = str(e)
                    series_scan['stop_requested'] = True
                break
            except Exception as e:
                # See the genre scan: without this the session stays poisoned
                # and every later item fails too.
                db.session.rollback()
                with series_scan_lock:
                    series_scan['results'].append({
                        'series': series.name,
                        'status': 'error',
                        'message': str(e),
                    })

            time.sleep(2)

        with series_scan_lock:
            # A scan that broke out early (the site blocked us, or the user
            # pressed Stop) hasn't covered its total. Filling the bar and
            # calling it complete would claim the whole run happened and
            # hide the stop_reason, which only the 'stopped' branch shows.
            stopped_early = series_scan['stop_requested']
            if not stopped_early:
                series_scan['progress'] = series_scan['total']
            series_scan['current_series'] = ''
            series_scan['status'] = 'stopped' if stopped_early else 'complete'


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
                'stop_reason': '',
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
    try:
        _run_author_scan(app)
    except Exception as e:
        # Without this, an uncaught error would leave the status stuck at
        # 'running' with the progress bar frozen and polling forever.
        with author_scan_lock:
            author_scan['results'].append({'author': '(scan aborted)', 'status': 'error', 'message': str(e)})
            author_scan['current_author'] = ''
            author_scan['status'] = 'stopped'


def _run_author_scan(app):
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

            except ScrapeBlockedError as e:
                # The site is turning us away — the rest of the run would fail
                # identically, so stop rather than hammering it.
                with author_scan_lock:
                    author_scan['results'].append({
                        'author': author.name,
                        'status': 'blocked',
                        'message': str(e),
                    })
                    author_scan['stop_reason'] = str(e)
                    author_scan['stop_requested'] = True
                break
            except Exception as e:
                # See the genre scan: without this the session stays poisoned
                # and every later item fails too.
                db.session.rollback()
                with author_scan_lock:
                    author_scan['results'].append({
                        'author': author.name,
                        'status': 'error',
                        'message': str(e),
                    })

            # Brief delay to be polite to the sources
            time.sleep(2)

        with author_scan_lock:
            # A scan that broke out early (the site blocked us, or the user
            # pressed Stop) hasn't covered its total. Filling the bar and
            # calling it complete would claim the whole run happened and
            # hide the stop_reason, which only the 'stopped' branch shows.
            stopped_early = author_scan['stop_requested']
            if not stopped_early:
                author_scan['progress'] = author_scan['total']
            author_scan['current_author'] = ''
            author_scan['status'] = 'stopped' if stopped_early else 'complete'


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


@system_bp.route('/system/tags/<int:id>/replace', methods=['POST'], endpoint='system_tag_replace')
def system_tag_replace(id):
    """Move everything carrying one tag onto another, then delete the first.

    Rename can't do this: it refuses a name another tag already holds, which
    is exactly the case when you want two tags to become one (LGBT and LGBTQ,
    say). Tags hang off authors and series as well as books, so all three have
    to move — dropping the source tag with only its books moved would discard
    the rest silently.

    Re-renders the whole result list rather than the one row, because the
    target tag's counts change too and it is usually sitting in the same list.
    """
    tag = db.get_or_404(Tag, id)
    query = request.args.get('q', '') or request.form.get('q', '')

    def results(**extra):
        tags = (Tag.query.filter(Tag.name.ilike(f'%{query}%')).order_by(Tag.name).limit(50).all()
                if query else [])
        return render_template('system/_tag_results.html', tags=tags, query=query, **extra)

    def failed(message):
        # Hand back what was typed so a typo can be corrected rather than retyped.
        return results(replace_error=message, replace_error_tag_id=tag.id,
                       replace_target=request.form.get('target', '').strip())

    target_name = request.form.get('target', '').strip()
    if not target_name:
        return failed('Enter the tag to replace it with')

    target = Tag.query.filter(db.func.lower(Tag.name) == target_name.lower()).first()
    if not target:
        return failed(f'No tag named "{target_name}". Use Rename to change this tag\'s own name.')
    if target.id == tag.id:
        return failed('That is the same tag')

    counts = {'book': len(tag.books), 'author': len(tag.authors), 'series': len(tag.series)}
    for holder in (*tag.books, *tag.authors, *tag.series):
        if target not in holder.tags:
            holder.tags.append(target)

    # Clear first: deleting a tag that still has rows would leave the
    # association rows behind, pointing at a tag that no longer exists.
    old_name = tag.name
    tag.books = []
    tag.authors = []
    tag.series = []
    db.session.delete(tag)
    db.session.commit()

    return results(notice=f'Replaced "{old_name}" with "{target.name}" on '
                          f'{counts["book"]} book(s), {counts["author"]} author(s) '
                          f'and {counts["series"]} series.')


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
