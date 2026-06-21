import os
import re
import html
import requests as http_requests
from datetime import datetime
from urllib.parse import urlparse
from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash, session, jsonify
from werkzeug.utils import secure_filename
from sqlalchemy.orm import joinedload, subqueryload
from models import db, Book, Author, Series, Read, ReadingQueue, BookFormat, Tag, RATING_LABELS
from utils import allowed_file, parse_date, parse_float, validate_rating, _is_safe_cover_url, clean_external_url
from scrapers import scrape_amazon, scrape_goodreads, search_amazon_for_book, search_goodreads_for_book

books_bp = Blueprint('books', __name__)


@books_bp.route('/', endpoint='dashboard')
def dashboard():
    active_reads = Read.query.options(
        joinedload(Read.book).subqueryload(Book.authors),
        joinedload(Read.book).joinedload(Book.series)
    ).filter_by(status='Reading').order_by(Read.start_date.desc()).all()
    total_books = Book.query.count()
    total_reads = Read.query.filter_by(status='Completed').count()
    recently_added = Book.query.options(
        subqueryload(Book.authors),
        subqueryload(Book.reads)
    ).order_by(Book.date_added.desc()).limit(10).all()
    return render_template('dashboard.html',
                         active_reads=active_reads,
                         total_books=total_books,
                         total_reads=total_reads,
                         recently_added=recently_added)


@books_bp.route('/books', endpoint='book_list')
def book_list():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    filter_status = request.args.get('filter', 'all')
    pages_filter = request.args.get('pages', '')
    # Constrain to valid options
    if per_page not in [10, 25, 50, 100]:
        per_page = 10
    if filter_status not in ['all', 'unread', 'read', 'bundle']:
        filter_status = 'all'
    if pages_filter not in ['lt300', '300to499', '500plus', '']:
        pages_filter = ''

    # Build query based on filter
    base = Book.query.options(subqueryload(Book.authors), subqueryload(Book.reads))
    if filter_status == 'read':
        # Books with at least one completed read
        query = base.filter(
            Book.id.in_(
                db.session.query(Read.book_id).filter(Read.status == 'Completed')
            )
        )
    elif filter_status == 'unread':
        # Books with no completed reads
        query = base.filter(
            ~Book.id.in_(
                db.session.query(Read.book_id).filter(Read.status == 'Completed')
            )
        )
    elif filter_status == 'bundle':
        query = base.filter(Book.is_book_bundle == True)
    else:
        query = base

    if pages_filter == 'lt300':
        query = query.filter(Book.page_count.isnot(None), Book.page_count < 300)
    elif pages_filter == '300to499':
        query = query.filter(Book.page_count.isnot(None), Book.page_count >= 300, Book.page_count < 500)
    elif pages_filter == '500plus':
        query = query.filter(Book.page_count.isnot(None), Book.page_count >= 500)

    books = query.order_by(Book.date_added.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    return render_template('books/list.html', books=books, per_page=per_page, filter_status=filter_status, pages_filter=pages_filter)


@books_bp.route('/books/<int:id>', endpoint='book_detail')
def book_detail(id):
    from datetime import date
    book = db.get_or_404(Book, id)
    suggest_queue_id = request.args.get('suggest_queue', type=int)
    return render_template('books/detail.html', book=book, today=date.today().isoformat(), suggest_queue_id=suggest_queue_id)


@books_bp.route('/books/new', methods=['GET', 'POST'], endpoint='book_new')
def book_new():
    if request.method == 'POST':
        return save_book(None)

    formats = BookFormat.query.all()
    authors = Author.query.order_by(Author.name).all()
    series_list = Series.query.order_by(Series.name).all()

    # Check for pre-filled data from import, or parent_id query param
    prefill = session.pop('book_prefill', None)
    if not prefill and request.args.get('parent_id'):
        prefill = {'parent_id': request.args.get('parent_id', type=int)}

    # Resolve prefill tag IDs to Tag objects
    prefill_tags = []
    if prefill and prefill.get('tag_ids'):
        prefill_tags = Tag.query.filter(Tag.id.in_(prefill['tag_ids'])).all()

    # Resolve parent book for display if importing as bundle child
    prefill_parent = None
    if prefill and prefill.get('parent_id'):
        prefill_parent = db.session.get(Book, prefill['parent_id'])

    return render_template('books/form.html',
                         book=None,
                         formats=formats,
                         authors=authors,
                         series_list=series_list,
                         prefill=prefill,
                         prefill_tags=prefill_tags,
                         prefill_parent=prefill_parent)


@books_bp.route('/books/bundle-child-quick-add', methods=['POST'], endpoint='book_bundle_child_quick_add')
def book_bundle_child_quick_add():
    title = request.form.get('title', '').strip()
    format_id = request.form.get('format_id', type=int)
    parent_id = request.form.get('parent_id', type=int)
    if not title or not format_id or not parent_id:
        return '', 400
    child = Book(title=title, format_id=format_id, parent_id=parent_id)
    db.session.add(child)
    db.session.commit()
    return (
        f'<span class="tag-chip" data-book-id="{child.id}">'
        f'{html.escape(child.title)}'
        f'<input type="hidden" name="bundle_children" value="{child.id}">'
        f'<button type="button" class="chip-remove" onclick="this.parentElement.remove()" aria-label="Remove">&times;</button>'
        f'</span>'
    )


@books_bp.route('/books/bundle-child-search', endpoint='book_bundle_child_search')
def book_bundle_child_search():
    q = request.args.get('q', '').strip()
    exclude_str = request.args.get('exclude', '')
    exclude_ids = set(int(x) for x in exclude_str.split(',') if x.strip().isdigit())
    bundle_id = request.args.get('bundle_id', type=int)
    if bundle_id:
        exclude_ids.add(bundle_id)

    if not q:
        return ''

    books = Book.query.filter(Book.title.ilike(f'%{q}%')).order_by(Book.title).limit(20).all()
    results = [b for b in books if b.id not in exclude_ids]

    rows = []
    for b in results:
        author_str = f' — {html.escape(b.authors[0].name)}' if b.authors else ''
        rows.append(
            f'<div class="tag-search-item" data-book-id="{b.id}" data-book-title="{html.escape(b.title, quote=True)}">'
            f'{html.escape(b.title)}{author_str}</div>'
        )
    return '\n'.join(rows)


@books_bp.route('/books/import', endpoint='book_import')
def book_import():
    """Import book data from external URLs."""
    source = request.args.get('source', '')
    url = clean_external_url(request.args.get('url', '').strip())
    parent_id = request.args.get('parent_id', type=int)

    if not source or not url:
        flash('Missing source or URL', 'error')
        return redirect(url_for('book_list'))

    scrapers = {
        'amazon': scrape_amazon,
        'goodreads': scrape_goodreads,
    }

    scraper = scrapers.get(source)
    if not scraper:
        flash('Unknown import source', 'error')
        return redirect(url_for('book_list'))

    try:
        book_data = scraper(url)
        if book_data:
            # Auto-create tags from genres
            if book_data.get('genres'):
                tag_ids = []
                for genre_name in book_data['genres']:
                    tag = Tag.query.filter(Tag.name.ilike(genre_name)).first()
                    if not tag:
                        tag = Tag(name=genre_name)
                        db.session.add(tag)
                        db.session.commit()
                    tag_ids.append(tag.id)
                book_data['tag_ids'] = tag_ids

            # Map detected format (e.g. Kindle) to a format_id
            detected_format = book_data.pop('detected_format', None)
            if detected_format:
                format_match = BookFormat.query.filter(BookFormat.name.ilike(detected_format)).first()
                if format_match:
                    book_data['format_id'] = format_match.id

            if parent_id:
                book_data['parent_id'] = parent_id
            session['book_prefill'] = book_data
            flash('Book data imported. Please review and save.', 'success')
        else:
            flash('Could not extract book data from URL', 'warning')
    except Exception as e:
        flash(f'Error importing book: {str(e)}', 'error')

    return redirect(url_for('book_new'))


@books_bp.route('/books/scrape-description', endpoint='scrape_description')
def scrape_description():
    """Scrape book description from Amazon or Goodreads URL."""
    url = request.args.get('url', '').strip()

    if not url:
        return jsonify({'error': 'URL is required'}), 400

    try:
        # Determine source from URL
        if 'amazon' in url:
            data = scrape_amazon(url)
        elif 'goodreads' in url:
            data = scrape_goodreads(url)
        else:
            return jsonify({'error': 'URL must be from Amazon or Goodreads'}), 400

        if data and data.get('description'):
            return jsonify({'description': data['description']})
        else:
            return jsonify({'error': 'Could not find description on page'}), 404

    except Exception as e:
        return jsonify({'error': f'Failed to fetch page: {str(e)}'}), 500


@books_bp.route('/books/search-description', endpoint='search_description')
def search_description():
    """Search for a book by title/author and scrape its description."""
    title = request.args.get('title', '').strip()
    author = request.args.get('author', '').strip()
    if not title:
        return jsonify({'error': 'Book title is required'}), 400

    try:
        data = None
        book_url = None

        # Try Goodreads first — Amazon is frequently bot-blocked
        book_url = search_goodreads_for_book(title, author)
        if book_url:
            data = scrape_goodreads(book_url)

        # Fall back to Amazon if Goodreads didn't work
        if not (data and data.get('description')):
            amazon_url = search_amazon_for_book(title, author)
            if amazon_url:
                amazon_data = scrape_amazon(amazon_url)
                if amazon_data and amazon_data.get('description'):
                    return jsonify({'description': amazon_data['description'], 'source_url': amazon_url})

        if not book_url:
            return jsonify({'error': 'Could not find book on Goodreads or Amazon'}), 404

        if data and data.get('description'):
            return jsonify({'description': data['description'], 'source_url': book_url})
        else:
            return jsonify({'error': 'Found book but could not extract description', 'source_url': book_url}), 404

    except Exception as e:
        return jsonify({'error': f'Failed to search: {str(e)}'}), 500


@books_bp.route('/books/<int:id>/edit', methods=['GET', 'POST'], endpoint='book_edit')
def book_edit(id):
    book = db.get_or_404(Book, id)
    if request.method == 'POST':
        return save_book(book)

    formats = BookFormat.query.all()
    authors = Author.query.order_by(Author.name).all()
    series_list = Series.query.order_by(Series.name).all()
    return render_template('books/form.html',
                         book=book,
                         formats=formats,
                         authors=authors,
                         series_list=series_list,
                         bundle_id=book.id if book.is_book_bundle else None)


def save_book(book):
    """Save a new or existing book."""
    is_new = book is None
    if is_new:
        book = Book()

    book.title = request.form.get('title', '').strip()
    if not book.title:
        flash('Title is required', 'error')
        return redirect(request.url)

    book.subtitle = request.form.get('subtitle', '').strip() or None
    book.description = request.form.get('description', '').strip() or None
    book.page_count = request.form.get('page_count', type=int) or None
    book.format_id = request.form.get('format_id', type=int)
    book.series_id = request.form.get('series_id', type=int) or None
    book.series_number = parse_float(request.form.get('series_number'))
    book.cost = parse_float(request.form.get('cost'))
    book.paid = parse_float(request.form.get('paid'))
    book.discounts = parse_float(request.form.get('discounts'))
    book.is_book_bundle = request.form.get('is_book_bundle') == 'on'
    book.bundled_books = request.form.get('bundled_books', '').strip() or None
    book.rating = validate_rating(parse_float(request.form.get('rating')))
    book.comment = request.form.get('comment', '').strip() or None
    book.goodreads_url = clean_external_url(request.form.get('goodreads_url', '').strip()) or None
    book.amazon_url = clean_external_url(request.form.get('amazon_url', '').strip()) or None
    book.date_purchased = parse_date(request.form.get('date_purchased'))

    # Handle authors
    author_ids = request.form.getlist('authors')
    book.authors = Author.query.filter(Author.id.in_(author_ids)).all() if author_ids else []

    # Handle tags
    tag_ids = request.form.getlist('tags')
    book.tags = Tag.query.filter(Tag.id.in_(tag_ids)).all() if tag_ids else []

    # Set parent_id if provided in form (e.g. from import flow)
    form_parent_id = request.form.get('parent_id', type=int)
    if form_parent_id is not None:
        book.parent_id = form_parent_id

    # Handle cover image upload (file takes priority over URL)
    if 'cover_image' in request.files:
        file = request.files['cover_image']
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            # Add timestamp to avoid conflicts
            base, ext = os.path.splitext(filename)
            filename = f"{base}_{int(datetime.now().timestamp())}{ext}"
            file.save(os.path.join(current_app.config['UPLOAD_FOLDER'], filename))
            book.cover_image = filename

    # Handle cover image URL (only if no file uploaded)
    cover_url = request.form.get('cover_image_url', '').strip()
    if cover_url and not (request.files.get('cover_image') and request.files['cover_image'].filename):
        if not _is_safe_cover_url(cover_url):
            flash('Cover image URL must be a public http/https address.', 'warning')
            cover_url = ''
    if cover_url and not (request.files.get('cover_image') and request.files['cover_image'].filename):
        try:
            response = http_requests.get(cover_url, timeout=10)
            response.raise_for_status()

            # Determine file extension from URL or content type
            parsed_url = urlparse(cover_url)
            url_path = parsed_url.path.lower()

            content_type = response.headers.get('content-type', '')
            ext = None
            if 'jpeg' in content_type or 'jpg' in content_type or url_path.endswith('.jpg') or url_path.endswith('.jpeg'):
                ext = '.jpg'
            elif 'png' in content_type or url_path.endswith('.png'):
                ext = '.png'
            elif 'gif' in content_type or url_path.endswith('.gif'):
                ext = '.gif'
            elif 'webp' in content_type or url_path.endswith('.webp'):
                ext = '.webp'
            else:
                ext = '.jpg'  # Default to jpg

            # Generate filename from book title
            safe_title = secure_filename(book.title[:50])
            filename = f"{safe_title}_{int(datetime.now().timestamp())}{ext}"

            # Save the image
            filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
            with open(filepath, 'wb') as f:
                f.write(response.content)
            book.cover_image = filename
        except Exception as e:
            flash(f'Could not download cover image: {str(e)}', 'warning')

    if is_new:
        db.session.add(book)
    db.session.flush()  # ensure book.id is available for new books

    # Sync bundle children
    if book.is_book_bundle:
        submitted_ids = set(int(x) for x in request.form.getlist('bundle_children') if x)
        for child in list(book.bundle_children):
            if child.id not in submitted_ids:
                child.parent_id = None
        existing_ids = {c.id for c in book.bundle_children}
        for child_id in submitted_ids - existing_ids:
            child = db.session.get(Book, child_id)
            if child:
                child.parent_id = book.id
    else:
        # If no longer a bundle, detach all children
        for child in list(book.bundle_children):
            child.parent_id = None

    db.session.commit()
    flash('Book saved successfully', 'success')

    # For new books, check if any external queue entry matches this title
    if is_new:
        match = ReadingQueue.query.filter(
            ReadingQueue.book_id.is_(None),
            db.func.lower(ReadingQueue.external_title) == book.title.lower()
        ).first()
        if match:
            return redirect(url_for('book_detail', id=book.id, suggest_queue=match.id))

    return redirect(url_for('book_detail', id=book.id))


@books_bp.route('/books/<int:id>/rate', methods=['POST'], endpoint='book_rate')
def book_rate(id):
    book = db.get_or_404(Book, id)
    rating = validate_rating(parse_float(request.form.get('rating')))
    book.rating = rating
    db.session.commit()
    return redirect(url_for('book_detail', id=id))


@books_bp.route('/books/<int:id>/delete', methods=['DELETE', 'POST'], endpoint='book_delete')
def book_delete(id):
    book = db.get_or_404(Book, id)

    # Delete cover image file if it exists
    if book.cover_image:
        cover_path = os.path.join(current_app.config['UPLOAD_FOLDER'], book.cover_image)
        if os.path.exists(cover_path):
            os.remove(cover_path)

    # Detach bundle children before deletion
    for child in list(book.bundle_children):
        child.parent_id = None

    # Delete associated reads
    Read.query.filter_by(book_id=id).delete()
    db.session.delete(book)
    db.session.commit()
    flash('Book deleted successfully', 'success')

    if request.headers.get('HX-Request'):
        return '', 200, {'HX-Redirect': url_for('book_list')}
    return redirect(url_for('book_list'))


@books_bp.route('/books/<int:id>/update-tags', methods=['POST'], endpoint='book_update_tags')
def book_update_tags(id):
    book = db.get_or_404(Book, id)

    if not book.goodreads_url:
        flash('This book has no Goodreads URL', 'error')
        return redirect(url_for('book_detail', id=id))

    data = scrape_goodreads(book.goodreads_url)
    if not data or not data.get('genres'):
        flash('Could not fetch tags from Goodreads', 'error')
        return redirect(url_for('book_detail', id=id))

    existing_tag_names = {t.name.lower() for t in book.tags}
    added = []
    for genre_name in data['genres']:
        if genre_name.lower() in existing_tag_names:
            continue
        tag = Tag.query.filter(Tag.name.ilike(genre_name)).first()
        if not tag:
            tag = Tag(name=genre_name)
            db.session.add(tag)
            db.session.flush()
        book.tags.append(tag)
        existing_tag_names.add(genre_name.lower())
        added.append(genre_name)

    db.session.commit()

    if added:
        flash(f'Added {len(added)} tag(s): {", ".join(added)}', 'success')
    else:
        flash('No new tags found', 'success')

    return redirect(url_for('book_detail', id=id))


@books_bp.route('/books/<int:book_id>/reads', methods=['POST'], endpoint='read_add')
def read_add(book_id):
    book = db.get_or_404(Book, book_id)

    # Check for active read
    status = request.form.get('status', 'Reading')
    if status == 'Reading' and book.active_read:
        flash('This book already has an active read', 'error')
        return redirect(url_for('book_detail', id=book_id))

    read = Read(
        book_id=book_id,
        start_date=parse_date(request.form.get('start_date')),
        finish_date=parse_date(request.form.get('finish_date')),
        status=status
    )
    db.session.add(read)

    # Remove from reading queue when a read is started
    if status == 'Reading':
        ReadingQueue.query.filter_by(book_id=book_id).delete()

    db.session.commit()
    flash('Read added successfully', 'success')

    if request.headers.get('HX-Request'):
        return redirect(url_for('book_detail', id=book_id))
    return redirect(url_for('book_detail', id=book_id))


@books_bp.route('/reads/<int:id>', methods=['POST'], endpoint='read_update')
def read_update(id):
    read = db.get_or_404(Read, id)

    new_status = request.form.get('status', read.status)
    # Check for active read if changing to Reading
    if new_status == 'Reading' and read.status != 'Reading':
        if read.book.active_read:
            flash('This book already has an active read', 'error')
            return redirect(url_for('book_detail', id=read.book_id))

    read.start_date = parse_date(request.form.get('start_date'))
    read.finish_date = parse_date(request.form.get('finish_date'))
    read.status = new_status
    db.session.commit()
    flash('Read updated successfully', 'success')
    return redirect(url_for('book_detail', id=read.book_id))


@books_bp.route('/reads/<int:id>/delete', methods=['DELETE', 'POST'], endpoint='read_delete')
def read_delete(id):
    read = db.get_or_404(Read, id)
    book_id = read.book_id
    db.session.delete(read)
    db.session.commit()
    flash('Read deleted successfully', 'success')

    if request.headers.get('HX-Request'):
        return '', 200, {'HX-Redirect': url_for('book_detail', id=book_id)}
    return redirect(url_for('book_detail', id=book_id))


@books_bp.route('/reads/<int:id>/complete', methods=['POST'], endpoint='read_complete')
def read_complete(id):
    read = db.get_or_404(Read, id)
    read.status = 'Completed'
    read.finish_date = datetime.now()
    db.session.commit()
    flash('Read marked as completed!', 'success')
    return redirect(url_for('book_detail', id=read.book_id))


@books_bp.route('/reads/<int:id>/abandon', methods=['POST'], endpoint='read_abandon')
def read_abandon(id):
    read = db.get_or_404(Read, id)
    read.status = 'Abandoned'
    read.finish_date = datetime.now()
    db.session.commit()
    flash('Read marked as abandoned', 'success')
    return redirect(url_for('book_detail', id=read.book_id))
