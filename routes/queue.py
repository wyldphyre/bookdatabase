from flask import Blueprint, render_template, request, redirect, url_for, jsonify
from sqlalchemy.orm import joinedload, subqueryload
from models import db, Book, ReadingQueue, PriceWatch

queue_bp = Blueprint('queue', __name__)


WANT_SORTS = {
    'newest': Book.date_added.desc(),
    'title': db.func.lower(Book.title),
    'shuffle': db.func.random(),
}


# /queue was this page's address before it became To Be Read; kept so old
# links and bookmarks still land here. url_for builds the first rule
# registered, which is the lower decorator.
@queue_bp.route('/queue', endpoint='queue_list')
@queue_bp.route('/to-be-read', endpoint='queue_list')
def queue_list():
    """To Be Read: the ordered queue of what's next, and the Want to Read
    list laid out for browsing — big covers and the blurb beside them."""
    items = ReadingQueue.query.options(
        joinedload(ReadingQueue.book).joinedload(Book.authors),
        joinedload(ReadingQueue.book).joinedload(Book.series),
        joinedload(ReadingQueue.book).joinedload(Book.format),
    ).order_by(ReadingQueue.position).all()

    sort = request.args.get('sort', 'newest')
    if sort not in WANT_SORTS:
        sort = 'newest'
    want_books = Book.query.options(
        subqueryload(Book.authors),
        subqueryload(Book.reads),
        subqueryload(Book.bundle_children),
        subqueryload(Book.queue_items),
        joinedload(Book.series),
        joinedload(Book.format),
    ).filter(Book.want_to_read.is_(True)).order_by(WANT_SORTS[sort], Book.id).all()

    # Current price for any Want to Read book being watched (matched by ASIN)
    watches_by_asin = PriceWatch.by_asin() if any(b.amazon_url for b in want_books) else {}

    tab = 'want' if request.args.get('tab') == 'want' else 'queue'
    return render_template('queue.html', items=items, want_books=want_books, tab=tab, sort=sort,
                           watches_by_asin=watches_by_asin)


@queue_bp.route('/queue/add', methods=['POST'], endpoint='queue_add')
def queue_add():
    book_id = request.form.get('book_id', type=int)
    if not book_id:
        return 'Missing book_id', 400
    book = db.get_or_404(Book, book_id)

    # Don't add duplicates
    existing = ReadingQueue.query.filter_by(book_id=book_id).first()
    if not existing:
        add_to_top = request.form.get('add_to_top') == '1'
        if add_to_top:
            min_pos = db.session.query(db.func.min(ReadingQueue.position)).scalar()
            position = 1 if min_pos is None else min_pos - 1
        else:
            max_pos = db.session.query(db.func.max(ReadingQueue.position)).scalar()
            position = 1 if max_pos is None else max_pos + 1
        item = ReadingQueue(book_id=book_id, position=position)
        db.session.add(item)
        db.session.commit()

    if request.headers.get('HX-Request'):
        in_queue = ReadingQueue.query.filter_by(book_id=book_id).first() is not None
        return render_template('queue/_button.html', book=book, in_queue=in_queue)
    return redirect(request.referrer or url_for('queue.queue_list'))


@queue_bp.route('/queue/<int:item_id>/remove', methods=['POST', 'DELETE'], endpoint='queue_remove')
def queue_remove(item_id):
    item = db.get_or_404(ReadingQueue, item_id)
    book = item.book
    book_id = item.book_id
    db.session.delete(item)
    db.session.commit()

    if request.headers.get('HX-Request'):
        # If removing from the queue page itself, return empty to delete the row
        if request.headers.get('HX-Target', '').startswith('queue-item-'):
            return ''
        # If removing via button on another page, return the updated button
        if book:
            return render_template('queue/_button.html', book=book, in_queue=False)
        return ''
    return redirect(request.referrer or url_for('queue.queue_list'))


@queue_bp.route('/queue/reorder', methods=['POST'], endpoint='queue_reorder')
def queue_reorder():
    data = request.get_json(silent=True)
    if not isinstance(data, list):
        return jsonify({'error': 'expected a list of {id, position} entries'}), 400
    positions = {}
    for entry in data:
        if not isinstance(entry, dict) or not isinstance(entry.get('id'), int) or not isinstance(entry.get('position'), int):
            return jsonify({'error': 'each entry needs integer id and position'}), 400
        positions[entry['id']] = entry['position']
    items = ReadingQueue.query.filter(ReadingQueue.id.in_(positions.keys())).all()
    for item in items:
        item.position = positions[item.id]
    db.session.commit()
    return jsonify({'status': 'ok'})
