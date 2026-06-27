import html
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from sqlalchemy import func
from sqlalchemy.orm import subqueryload
from models import db, Book, Series, Read, Tag
from scrapers import scrape_goodreads_series, scrape_amazon_series
from utils import clean_external_url

series_bp = Blueprint('series', __name__)


@series_bp.route('/series', endpoint='series_list')
def series_list():
    search = request.args.get('search', '').strip()
    filter_type = request.args.get('filter', 'all')
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)
    if per_page not in [10, 25, 50, 100]:
        per_page = 25
    query = Series.query.options(subqueryload(Series.books))
    if search:
        query = query.filter(Series.name.ilike(f'%{search}%'))
    if filter_type == 'no_links':
        query = query.filter(
            (Series.goodreads_url.is_(None) | (Series.goodreads_url == '')),
            (Series.amazon_url.is_(None) | (Series.amazon_url == '')),
            (Series.storygraph_url.is_(None) | (Series.storygraph_url == ''))
        )
    elif filter_type in ('incomplete', 'complete'):
        # Subquery: number of books owned per series
        owned_sq = (
            db.session.query(func.count(Book.id))
            .filter(Book.series_id == Series.id)
            .correlate(Series)
            .scalar_subquery()
        )
        # Subquery: number of distinct books with at least one Completed read
        read_sq = (
            db.session.query(func.count(func.distinct(Book.id)))
            .join(Read, (Read.book_id == Book.id) & (Read.status == 'Completed'))
            .filter(Book.series_id == Series.id)
            .correlate(Series)
            .scalar_subquery()
        )
        if filter_type == 'incomplete':
            query = query.filter(
                (read_sq < owned_sq) |
                (Series.number_in_series.isnot(None) & (owned_sq < Series.number_in_series))
            )
        else:  # complete
            query = query.filter(
                Series.number_in_series.isnot(None),
                owned_sq >= Series.number_in_series,
                read_sq >= owned_sq
            )

    all_series = query.order_by(Series.name).paginate(page=page, per_page=per_page, error_out=False)

    return render_template('series/list.html', series_list=all_series, search=search, per_page=per_page, filter_type=filter_type)


@series_bp.route('/series/<int:id>', endpoint='series_detail')
def series_detail(id):
    series = Series.query.options(
        subqueryload(Series.books).subqueryload(Book.reads),
        subqueryload(Series.books).subqueryload(Book.bundle_children)
    ).get_or_404(id)
    read_count = sum(1 for book in series.books if any(r.status == 'Completed' for r in book.reads))
    return render_template('series/detail.html', series=series, read_count=read_count)


@series_bp.route('/series/check-name', endpoint='series_check_name')
def series_check_name():
    name = request.args.get('name', '').strip()
    exclude_id = request.args.get('exclude_id', type=int)
    if not name:
        return ''
    q = Series.query.filter(Series.name.ilike(name))
    if exclude_id:
        q = q.filter(Series.id != exclude_id)
    existing = q.first()
    if not existing:
        return ''
    return (f'<small style="color: var(--pico-del-color);">'
            f'⚠ A series named <a href="{url_for("series_detail", id=existing.id)}" target="_blank">'
            f'{html.escape(existing.name)}</a> already exists.</small>')


@series_bp.route('/series/new', methods=['GET', 'POST'], endpoint='series_new')
def series_new():
    if request.method == 'POST':
        return save_series(None)
    return render_template('series/form.html', series=None)


@series_bp.route('/series/<int:id>/edit', methods=['GET', 'POST'], endpoint='series_edit')
def series_edit(id):
    series = db.get_or_404(Series, id)
    if request.method == 'POST':
        return save_series(series)
    return render_template('series/form.html', series=series)


def save_series(series):
    """Save a new or existing series."""
    is_new = series is None
    if is_new:
        series = Series()

    series.name = request.form.get('name', '').strip()
    if not series.name:
        flash('Name is required', 'error')
        return redirect(request.url)

    series.number_in_series = request.form.get('number_in_series', type=int) or None
    series.goodreads_url = clean_external_url(request.form.get('goodreads_url', '').strip()) or None
    series.amazon_url = clean_external_url(request.form.get('amazon_url', '').strip()) or None
    series.storygraph_url = clean_external_url(request.form.get('storygraph_url', '').strip()) or None

    # Handle tags
    tag_ids = request.form.getlist('tags')
    series.tags = Tag.query.filter(Tag.id.in_(tag_ids)).all() if tag_ids else []

    if is_new:
        db.session.add(series)
    db.session.commit()
    flash('Series saved successfully', 'success')
    return redirect(url_for('series_detail', id=series.id))


@series_bp.route('/series/<int:id>/delete', methods=['DELETE', 'POST'], endpoint='series_delete')
def series_delete(id):
    series = db.get_or_404(Series, id)
    # Remove series from books (but don't delete books)
    Book.query.filter_by(series_id=id).update({'series_id': None, 'series_number': None})
    db.session.delete(series)
    db.session.commit()
    flash('Series deleted successfully', 'success')

    if request.headers.get('HX-Request'):
        return '', 200, {'HX-Redirect': url_for('series_list')}
    return redirect(url_for('series_list'))


@series_bp.route('/series/<int:id>/update-count', methods=['POST'], endpoint='series_update_count')
def series_update_count(id):
    series = db.get_or_404(Series, id)
    counts = []
    if series.goodreads_url:
        gr = scrape_goodreads_series(series.goodreads_url)
        if gr is not None:
            counts.append(gr)
    if series.amazon_url:
        az = scrape_amazon_series(series.amazon_url)
        if az is not None:
            counts.append(az)

    if counts:
        count = max(counts)
        if series.number_in_series != count:
            series.number_in_series = count
            db.session.commit()
            flash(f'Series updated: {count} books in series', 'success')
        else:
            flash(f'Series count is already up to date ({count} books)', 'success')
    else:
        flash('Could not determine book count from the series page', 'error')

    return redirect(url_for('series_detail', id=id))


@series_bp.route('/series/quick-add', methods=['POST'], endpoint='series_quick_add')
def series_quick_add():
    """Quick add a series via htmx from the book form."""
    name = request.form.get('series_name', '').strip()
    if not name:
        return '<p class="error">Name is required</p>', 400

    series = Series(name=name)
    db.session.add(series)
    db.session.commit()

    return jsonify({'id': series.id, 'name': series.name})
