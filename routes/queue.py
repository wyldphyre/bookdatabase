from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from sqlalchemy.orm import joinedload
from models import db, Book, ReadingQueue

queue_bp = Blueprint('queue', __name__)


@queue_bp.route('/queue', endpoint='queue_list')
def queue_list():
    items = ReadingQueue.query.options(
        joinedload(ReadingQueue.book).joinedload(Book.authors),
        joinedload(ReadingQueue.book).joinedload(Book.series),
        joinedload(ReadingQueue.book).joinedload(Book.format),
    ).order_by(ReadingQueue.position).all()
    return render_template('queue.html', items=items)


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


@queue_bp.route('/queue/add-external', methods=['POST'], endpoint='queue_add_external')
def queue_add_external():
    title = request.form.get('title', '').strip()
    if not title:
        return '<p class="error">Title is required</p>', 400

    max_pos = db.session.query(db.func.max(ReadingQueue.position)).scalar()
    item = ReadingQueue(
        position=1 if max_pos is None else max_pos + 1,
        external_title=title,
        external_author=request.form.get('author', '').strip() or None,
        external_url=request.form.get('url', '').strip() or None,
    )
    db.session.add(item)
    db.session.commit()

    if request.headers.get('HX-Request'):
        return render_template('queue/_item.html', item=item)
    return redirect(url_for('queue.queue_list'))


@queue_bp.route('/queue/<int:item_id>/link', methods=['POST'], endpoint='queue_link')
def queue_link(item_id):
    item = db.get_or_404(ReadingQueue, item_id)
    book_id = request.form.get('book_id', type=int) or request.args.get('book_id', type=int)
    book = db.get_or_404(Book, book_id)
    item.book_id = book.id
    item.external_title = None
    item.external_author = None
    item.external_url = None
    db.session.commit()
    flash(f'Queue entry linked to "{book.title}"', 'success')
    return redirect(url_for('books.book_detail', id=book_id))


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
