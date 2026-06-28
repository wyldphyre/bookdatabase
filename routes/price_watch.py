from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash
from sqlalchemy.exc import IntegrityError
from models import db, PriceWatch
from utils import clean_external_url
from scrapers import scrape_amazon
from price_watch import run_price_checks

price_watch_bp = Blueprint('price_watch', __name__)


@price_watch_bp.route('/price-watch', endpoint='price_watch_list')
def price_watch_list():
    watches = PriceWatch.query.order_by(PriceWatch.created_at.desc()).all()
    return render_template('price_watch/list.html', watches=watches)


@price_watch_bp.route('/price-watch/add', methods=['POST'], endpoint='price_watch_add')
def price_watch_add():
    url = clean_external_url(request.form.get('amazon_url', '').strip())
    if not url:
        flash('An Amazon URL is required', 'error')
        return redirect(url_for('price_watch_list'))

    data = scrape_amazon(url)
    if not data or data.get('price') is None:
        flash('Could not read a price from that page', 'error')
        return redirect(url_for('price_watch_list'))

    watch = PriceWatch(
        amazon_url=url,
        title=data.get('title'),
        cover_url=data.get('cover_url'),
        initial_price=data['price'],
        current_price=data['price'],
        currency=data.get('currency'),
    )
    db.session.add(watch)
    try:
        db.session.commit()
        flash(f'Now watching "{watch.title}"', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('You\'re already watching that book', 'error')

    return redirect(url_for('price_watch_list'))


@price_watch_bp.route('/price-watch/<int:id>/delete', methods=['DELETE', 'POST'], endpoint='price_watch_delete')
def price_watch_delete(id):
    watch = db.get_or_404(PriceWatch, id)
    db.session.delete(watch)
    db.session.commit()

    if request.headers.get('HX-Request'):
        return '', 200
    flash('Removed from price watch', 'success')
    return redirect(url_for('price_watch_list'))


@price_watch_bp.route('/price-watch/check-now', methods=['POST'], endpoint='price_watch_check_now')
def price_watch_check_now():
    app = current_app._get_current_object()
    run_price_checks(app)
    flash('Price check complete', 'success')
    return redirect(url_for('price_watch_list'))
