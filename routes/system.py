import os
import json
import logging
import threading
from datetime import date, datetime
from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash, send_file, jsonify
from sqlalchemy.orm import joinedload
from models import db, Book, Series, Tag, Author, AuthorGender, AuthorInfoSuggestion, set_setting
from scrapers import scrape_goodreads_series, scrape_amazon_series
from author_info import lookup_author_info
from notifications import (send_pushover_notification, get_pushover_priority, pushover_configured,
                           PUSHOVER_PRIORITIES, VALID_PRIORITIES, PRIORITY_SETTING_KEY)
from utils import start_thumbnail_backfill, parse_tag_list, parse_id_list
from background_scan import BackgroundScan
from data_transfer import (build_export_zip, validate_import_zip, apply_import,
                           ImportValidationError, ImportCoverError, PRE_IMPORT_BACKUP_NAME)

system_bp = Blueprint('system', __name__)

logger = logging.getLogger(__name__)

# The System page's two scans; see background_scan.py
series_scan = BackgroundScan('Series scan', 'series', counters=('updated',))
author_scan = BackgroundScan('Author scan', 'author', counters=('suggestions_found', 'synced'))

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


@system_bp.route('/system', endpoint='system')
def system():
    changelog_path = os.path.join(current_app.root_path, 'changelog.json')
    try:
        with open(changelog_path) as f:
            changelog = json.load(f)
    except (OSError, ValueError):
        changelog = []
    return render_template('system.html',
                           series_scan=series_scan.snapshot(),
                           author_scan=author_scan.snapshot(),
                           suggestions=_load_suggestions(),
                           version=current_app.config['APP_VERSION'],
                           changelog=changelog,
                           pushover_configured=pushover_configured(),
                           pushover_priority=get_pushover_priority(),
                           pushover_priorities=PUSHOVER_PRIORITIES,
                           export=_export_snapshot(),
                           pending_import=_pending_import_manifest(),
                           current_book_count=Book.query.count(),
                           monitored_series=_monitored_series())


def _monitored_series():
    return Series.query.filter(Series.monitored.is_(True)).order_by(db.func.lower(Series.name)).all()


@system_bp.route('/system/monitored-series', methods=['POST'], endpoint='system_monitored_series')
def system_monitored_series():
    """Stop monitoring every series that was listed but left unticked.

    Only ids rendered in the form ("shown") are candidates, so a series that
    started being monitored after the page loaded isn't switched off by it."""
    shown = {int(i) for i in request.form.getlist('shown') if i.isdigit()}
    keep = {int(i) for i in request.form.getlist('keep') if i.isdigit()}
    to_stop = Series.query.filter(Series.id.in_(shown - keep), Series.monitored.is_(True)).all()
    for series in to_stop:
        series.monitored = False
        series.last_check_error = None
    db.session.commit()
    if to_stop:
        logger.info('Stopped monitoring %d series: %s', len(to_stop), ', '.join(repr(s.name) for s in to_stop))

    notice = f'Stopped monitoring {len(to_stop)} series.' if to_stop else None
    return render_template('system/_monitored_series.html',
                           monitored_series=_monitored_series(), notice=notice)


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
            thread = threading.Thread(target=run_export_build, args=(_app,), daemon=True, name='export')
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

    logger.info('Export: building')
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
        logger.info('Export: ready, %d books and %d covers in %.0f MB', manifest['counts']['book'],
                    manifest['cover_count'], size / (1024 * 1024))
    except Exception as e:
        logger.exception('Export failed')
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
        logger.info('Import: %s uploaded (%.0f MB), waiting for confirmation', file.filename,
                    os.path.getsize(path) / (1024 * 1024))
    except ImportValidationError as e:
        logger.warning('Import: %s refused: %s', file.filename, e)
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
        logger.info('Import: replacing the library from the uploaded export')
        result = apply_import(path, current_app.config['UPLOAD_FOLDER'])
        logger.info('Import: complete, %d books, %d authors, %d series, %d reads and %d covers; '
                    'the previous database was saved as %s', result['books'], result['authors'],
                    result['series'], result['reads'], result['covers'], PRE_IMPORT_BACKUP_NAME)
        start_thumbnail_backfill(current_app.config['UPLOAD_FOLDER'])
        flash(f"Import complete: {result['books']} books, {result['authors']} authors, "
              f"{result['series']} series, {result['reads']} reads and {result['covers']} covers restored. "
              f"The previous database was saved as {PRE_IMPORT_BACKUP_NAME}. "
              f"Cover thumbnails are being regenerated in the background.", 'success')
    except ImportValidationError as e:
        logger.warning('Import: refused: %s', e)
        flash(str(e), 'error')
    except ImportCoverError as e:
        # The rows are imported; only the cover files are suspect.
        logger.exception('Import: data imported, but replacing the covers failed')
        flash(str(e), 'error')
    except Exception as e:
        logger.exception('Import failed')
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
        logger.info('Import: cancelled')
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


def _series_scan_progress():
    return render_template('system/_series_scan_progress.html', scan=series_scan.snapshot())


@system_bp.route('/system/scan-series', methods=['POST'], endpoint='scan_series_start')
def scan_series_start():
    series_scan.start(current_app._get_current_object(), _series_to_scan, _scan_one_series,
                      item_name=lambda series: series.name)
    return _series_scan_progress()


@system_bp.route('/system/scan-series/progress', endpoint='scan_series_progress')
def scan_series_progress():
    return _series_scan_progress()


@system_bp.route('/system/scan-series/pause', methods=['POST'], endpoint='scan_series_pause')
def scan_series_pause():
    series_scan.toggle_pause()
    return _series_scan_progress()


@system_bp.route('/system/scan-series/stop', methods=['POST'], endpoint='scan_series_stop')
def scan_series_stop():
    series_scan.stop()
    return _series_scan_progress()


def _series_to_scan(scan):
    """Every series with a Goodreads or Amazon page to read its count from."""
    return Series.query.filter(
        (Series.goodreads_url.isnot(None) & (Series.goodreads_url != '')) |
        (Series.amazon_url.isnot(None) & (Series.amazon_url != ''))
    ).order_by(Series.name).all()


def _scan_one_series(scan, series):
    """Read a series' book count, Goodreads first, and raise the recorded
    number if the page lists more."""
    count = source = None
    if series.goodreads_url:
        count = scrape_goodreads_series(series.goodreads_url)
        if count is not None:
            source = 'Goodreads'
    if count is None and series.amazon_url:
        count = scrape_amazon_series(series.amazon_url)
        if count is not None:
            source = 'Amazon'

    if count is None:
        scan.add_result(series.name, 'not_found')
        return
    old_count = series.number_in_series
    if old_count is None or count > old_count:
        series.number_in_series = count
        db.session.commit()
        scan.add_result(series.name, 'updated', counter='updated',
                        old_count=old_count, new_count=count, source=source)
    else:
        scan.add_result(series.name, 'unchanged', count=count, source=source)


def _author_scan_progress():
    return render_template('system/_author_scan_progress.html', scan=author_scan.snapshot())


@system_bp.route('/system/scan-authors', methods=['POST'], endpoint='scan_authors_start')
def scan_authors_start():
    author_scan.start(current_app._get_current_object(), _authors_to_scan, _scan_one_author,
                      item_name=lambda author: author.name)
    return _author_scan_progress()


@system_bp.route('/system/scan-authors/progress', endpoint='scan_authors_progress')
def scan_authors_progress():
    return _author_scan_progress()


@system_bp.route('/system/scan-authors/pause', methods=['POST'], endpoint='scan_authors_pause')
def scan_authors_pause():
    author_scan.toggle_pause()
    return _author_scan_progress()


@system_bp.route('/system/scan-authors/stop', methods=['POST'], endpoint='scan_authors_stop')
def scan_authors_stop():
    author_scan.stop()
    return _author_scan_progress()


def _unknown_gender_id():
    unknown = AuthorGender.query.filter(db.func.lower(AuthorGender.name) == 'unknown').first()
    return unknown.id if unknown else -1


def _author_needs_info(author, unknown_id):
    needs_pronouns = not author.pronouns
    needs_gender = author.gender_id is None or author.gender_id == unknown_id
    return needs_pronouns, needs_gender


def _sync_author_aliases():
    """Copy gender/pronouns between alias and primary author records when one
    side has the data and the other doesn't. Returns list of synced names."""
    unknown_id = _unknown_gender_id()
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


def _authors_to_scan(scan):
    """Primary authors missing pronouns or a gender, after a free pre-pass that
    copies either between aliases and their primary record."""
    for name in _sync_author_aliases():
        scan.add_result(name, 'synced', counter='synced')
    unknown_id = _unknown_gender_id()
    return Author.query.filter(
        Author.alias_of_id.is_(None),
        db.or_(
            Author.pronouns.is_(None), Author.pronouns == '',
            Author.gender_id.is_(None), Author.gender_id == unknown_id,
        )
    ).order_by(Author.name).all()


def _scan_one_author(scan, author):
    """Look up an author's gender and pronouns and record a suggestion for
    review. Never writes to the author itself."""
    needs_pronouns, needs_gender = _author_needs_info(author, _unknown_gender_id())
    info = lookup_author_info(author.name, author.goodreads_url, author.website)

    suggested_pronouns = info['pronouns'] if info and needs_pronouns else None
    suggested_gender = None
    if info and needs_gender and info['gender']:
        suggested_gender = AuthorGender.query.filter(
            db.func.lower(AuthorGender.name) == info['gender'].lower()).first()

    if not (suggested_pronouns or suggested_gender):
        scan.add_result(author.name, 'not_found')
        return
    AuthorInfoSuggestion.query.filter_by(author_id=author.id).delete()
    db.session.add(AuthorInfoSuggestion(
        author_id=author.id,
        suggested_gender_id=suggested_gender.id if suggested_gender else None,
        suggested_pronouns=suggested_pronouns,
        evidence=info['evidence'],
        source_url=info['source_url'],
    ))
    db.session.commit()
    scan.add_result(author.name, 'suggested', counter='suggestions_found',
                    pronouns=suggested_pronouns,
                    gender=suggested_gender.name if suggested_gender else None)


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
    logger.info('Applied all %d author info suggestion(s)', len(suggestions))
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
    old_name = tag.name
    tag.name = new_name
    db.session.commit()
    logger.info('Renamed tag %r to %r', old_name, new_name)
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
    logger.info('Replaced tag %r with %r on %d book(s), %d author(s) and %d series', old_name,
                target.name, counts['book'], counts['author'], counts['series'])

    return results(notice=f'Replaced "{old_name}" with "{target.name}" on '
                          f'{counts["book"]} book(s), {counts["author"]} author(s) '
                          f'and {counts["series"]} series.')


@system_bp.route('/system/tags/<int:id>/delete', methods=['DELETE', 'POST'], endpoint='system_tag_delete')
def system_tag_delete(id):
    tag = db.get_or_404(Tag, id)
    name = tag.name
    tag.books = []
    tag.authors = []
    tag.series = []
    db.session.delete(tag)
    db.session.commit()
    logger.info('Deleted tag %r', name)
    return ''


@system_bp.route('/tags/search', endpoint='tag_search')
def tag_search():
    """Search tags for the tag picker."""
    query = request.args.get('q', '').strip()
    exclude_ids = parse_id_list(request.args.get('exclude'))

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

    tag = Tag.find_or_create(name)
    db.session.commit()
    return render_template('books/_tag_chip.html', tag=tag)


@system_bp.route('/tags/bulk-add', methods=['POST'], endpoint='tag_bulk_add')
def tag_bulk_add():
    """Resolve a pasted list of tag names for the tag picker, creating any
    that don't exist yet. Returns JSON so the picker can add the chips and say
    which tags were new — a paste creates them without asking, so a typo'd
    name should at least be visible."""
    max_len = Tag.name.type.length
    names = parse_tag_list(request.form.get('tag_text', ''))
    skipped = sum(1 for n in names if len(n) > max_len)
    names = [n for n in names if len(n) <= max_len]

    existing = {t.name.lower(): t for t in
                Tag.query.filter(db.func.lower(Tag.name).in_([n.lower() for n in names]))} if names else {}
    tags, created = [], []
    for name in names:
        tag = existing.get(name.lower())
        if not tag:
            tag = Tag.find_or_create(name)
            created.append(tag.name)
        tags.append({'id': tag.id, 'name': tag.name})
    db.session.commit()
    return jsonify(tags=tags, created=created, skipped=skipped)
