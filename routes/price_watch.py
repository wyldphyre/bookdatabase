from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash
from sqlalchemy.exc import IntegrityError
from models import db, PriceWatch, Book
from sqlalchemy import or_
from utils import amazon_asin, clean_external_url
from scrapers import scrape_amazon, ScrapeBlockedError
from price_watch import run_price_checks
from notifications import pushover_configured

price_watch_bp = Blueprint('price_watch', __name__)


@price_watch_bp.route('/price-watch', endpoint='price_watch_list')
def price_watch_list():
    watches = PriceWatch.query.order_by(PriceWatch.created_at.desc()).all()
    # Watches aren't linked to books; a library book whose Amazon link has
    # the same ASIN (on any Amazon site) is the one being watched.
    asins = {w.asin for w in watches if w.asin}
    books_by_asin = {}
    if asins:
        candidates = Book.query.filter(or_(*[Book.amazon_url.like(f'%{a}%') for a in asins])).all()
        books_by_asin = {b.amazon_asin: b for b in candidates if b.amazon_asin in asins}
    return render_template('price_watch/list.html', watches=watches, books_by_asin=books_by_asin,
                           pushover_configured=pushover_configured())


@price_watch_bp.route('/price-watch/add', methods=['POST'], endpoint='price_watch_add')
def price_watch_add():
    """Start watching an Amazon URL. Posted from the Price Watch page, or from
    a book's page (with book_id), in which case it returns to that book."""
    book_id = request.form.get('book_id', type=int)
    back = url_for('books.book_detail', id=book_id) if book_id else url_for('price_watch.price_watch_list')

    url = clean_external_url(request.form.get('amazon_url', '').strip())
    if not url:
        flash('An Amazon URL is required', 'error')
        return redirect(back)
    # The same book on another Amazon site is the same watch
    asin = amazon_asin(url)
    if asin and asin in PriceWatch.by_asin():
        flash('You\'re already watching that book', 'error')
        return redirect(back)

    try:
        data = scrape_amazon(url)
    except ScrapeBlockedError as e:
        flash(str(e), 'error')
        return redirect(back)

    if not data or data.get('price') is None:
        flash('Could not read a price from that page', 'error')
        return redirect(back)

    watch = PriceWatch(
        amazon_url=url,
        title=data.get('title'),
        cover_url=data.get('cover_url'),
        initial_price=data['price'],
        current_price=data['price'],
        notified_price=data['price'],   # you saw this price when adding it
        currency=data.get('currency'),
    )
    db.session.add(watch)
    try:
        db.session.commit()
        flash(f'Now watching "{watch.title}" at {watch.currency or ""}{watch.current_price:.2f}', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('You\'re already watching that book', 'error')

    return redirect(back)


@price_watch_bp.route('/price-watch/<int:id>/delete', methods=['DELETE', 'POST'], endpoint='price_watch_delete')
def price_watch_delete(id):
    watch = db.get_or_404(PriceWatch, id)
    db.session.delete(watch)
    db.session.commit()

    if request.headers.get('HX-Request'):
        return '', 200
    flash('Removed from price watch', 'success')
    return redirect(url_for('price_watch.price_watch_list'))


@price_watch_bp.route('/price-watch/check-now', methods=['POST'], endpoint='price_watch_check_now')
def price_watch_check_now():
    app = current_app._get_current_object()
    run_price_checks(app)
    flash('Price check complete', 'success')
    return redirect(url_for('price_watch.price_watch_list'))
