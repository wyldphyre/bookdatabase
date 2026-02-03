import os
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash
from werkzeug.utils import secure_filename
from models import db, Book, Author, Series, Read, BookFormat, AuthorGender
from database import init_db

app = Flask(__name__)
app.config['SECRET_KEY'] = 'dev-secret-key-change-in-production'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///books.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(app.static_folder, 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

db.init_app(app)


# Custom Jinja filter for sorting with None values
@app.template_filter('sort_by')
def sort_by_filter(items, attribute, default=float('inf')):
    """Sort items by attribute, treating None as the default value."""
    return sorted(items, key=lambda x: getattr(x, attribute) if getattr(x, attribute) is not None else default)


# Custom Jinja filter to count unique series from books
@app.template_filter('unique_series_count')
def unique_series_count_filter(books):
    """Count unique series from a list of books."""
    series_ids = {book.series_id for book in books if book.series_id is not None}
    return len(series_ids)


# Ensure upload folder exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def parse_date(date_str):
    """Parse date string to datetime object."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        return None


def parse_float(value):
    """Parse string to float, return None if invalid."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def validate_rating(rating):
    """Validate rating is between 0-5 in 0.25 increments."""
    if rating is None:
        return None
    if rating < 0 or rating > 5:
        return None
    # Round to nearest 0.25
    return round(rating * 4) / 4


# Dashboard
@app.route('/')
def dashboard():
    active_reads = Read.query.filter_by(status='Reading').order_by(Read.start_date.desc()).all()
    total_books = Book.query.count()
    total_reads = Read.query.filter_by(status='Completed').count()
    return render_template('dashboard.html',
                         active_reads=active_reads,
                         total_books=total_books,
                         total_reads=total_reads)


# Book routes
@app.route('/books')
def book_list():
    page = request.args.get('page', 1, type=int)
    per_page = 25
    books = Book.query.order_by(Book.date_added.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    return render_template('books/list.html', books=books)


@app.route('/books/<int:id>')
def book_detail(id):
    book = Book.query.get_or_404(id)
    return render_template('books/detail.html', book=book)


@app.route('/books/new', methods=['GET', 'POST'])
def book_new():
    if request.method == 'POST':
        return save_book(None)

    formats = BookFormat.query.all()
    authors = Author.query.filter_by(alias_of_id=None).order_by(Author.name).all()
    series_list = Series.query.order_by(Series.name).all()
    return render_template('books/form.html',
                         book=None,
                         formats=formats,
                         authors=authors,
                         series_list=series_list)


@app.route('/books/<int:id>/edit', methods=['GET', 'POST'])
def book_edit(id):
    book = Book.query.get_or_404(id)
    if request.method == 'POST':
        return save_book(book)

    formats = BookFormat.query.all()
    authors = Author.query.filter_by(alias_of_id=None).order_by(Author.name).all()
    series_list = Series.query.order_by(Series.name).all()
    return render_template('books/form.html',
                         book=book,
                         formats=formats,
                         authors=authors,
                         series_list=series_list)


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
    book.date_purchased = parse_date(request.form.get('date_purchased'))

    # Handle authors
    author_ids = request.form.getlist('authors')
    book.authors = Author.query.filter(Author.id.in_(author_ids)).all() if author_ids else []

    # Handle cover image upload
    if 'cover_image' in request.files:
        file = request.files['cover_image']
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            # Add timestamp to avoid conflicts
            base, ext = os.path.splitext(filename)
            filename = f"{base}_{int(datetime.now().timestamp())}{ext}"
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            book.cover_image = filename

    if is_new:
        db.session.add(book)
    db.session.commit()
    flash('Book saved successfully', 'success')
    return redirect(url_for('book_detail', id=book.id))


@app.route('/books/<int:id>/delete', methods=['DELETE', 'POST'])
def book_delete(id):
    book = Book.query.get_or_404(id)
    # Delete associated reads
    Read.query.filter_by(book_id=id).delete()
    db.session.delete(book)
    db.session.commit()
    flash('Book deleted successfully', 'success')

    if request.headers.get('HX-Request'):
        return '', 200, {'HX-Redirect': url_for('book_list')}
    return redirect(url_for('book_list'))


# Read routes
@app.route('/books/<int:book_id>/reads', methods=['POST'])
def read_add(book_id):
    book = Book.query.get_or_404(book_id)

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
    db.session.commit()
    flash('Read added successfully', 'success')

    if request.headers.get('HX-Request'):
        return redirect(url_for('book_detail', id=book_id))
    return redirect(url_for('book_detail', id=book_id))


@app.route('/reads/<int:id>', methods=['POST'])
def read_update(id):
    read = Read.query.get_or_404(id)

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


@app.route('/reads/<int:id>/delete', methods=['DELETE', 'POST'])
def read_delete(id):
    read = Read.query.get_or_404(id)
    book_id = read.book_id
    db.session.delete(read)
    db.session.commit()
    flash('Read deleted successfully', 'success')

    if request.headers.get('HX-Request'):
        return '', 200, {'HX-Redirect': url_for('book_detail', id=book_id)}
    return redirect(url_for('book_detail', id=book_id))


# Author routes
@app.route('/authors')
def author_list():
    search = request.args.get('search', '').strip()
    query = Author.query.filter_by(alias_of_id=None)
    if search:
        query = query.filter(Author.name.ilike(f'%{search}%'))
    authors = query.order_by(Author.name).all()
    return render_template('authors/list.html', authors=authors, search=search)


@app.route('/authors/<int:id>')
def author_detail(id):
    author = Author.query.get_or_404(id)
    return render_template('authors/detail.html', author=author)


@app.route('/authors/new', methods=['GET', 'POST'])
def author_new():
    if request.method == 'POST':
        return save_author(None)

    genders = AuthorGender.query.all()
    authors = Author.query.filter_by(alias_of_id=None).order_by(Author.name).all()
    return render_template('authors/form.html', author=None, genders=genders, authors=authors)


@app.route('/authors/<int:id>/edit', methods=['GET', 'POST'])
def author_edit(id):
    author = Author.query.get_or_404(id)
    if request.method == 'POST':
        return save_author(author)

    genders = AuthorGender.query.all()
    # Exclude self from alias options
    authors = Author.query.filter(Author.id != id, Author.alias_of_id == None).order_by(Author.name).all()
    return render_template('authors/form.html', author=author, genders=genders, authors=authors)


def save_author(author):
    """Save a new or existing author."""
    is_new = author is None
    if is_new:
        author = Author()

    author.name = request.form.get('name', '').strip()
    if not author.name:
        flash('Name is required', 'error')
        return redirect(request.url)

    author.pronouns = request.form.get('pronouns', '').strip() or None
    author.gender_id = request.form.get('gender_id', type=int) or None
    author.goodreads_url = request.form.get('goodreads_url', '').strip() or None
    author.amazon_url = request.form.get('amazon_url', '').strip() or None
    author.storygraph_url = request.form.get('storygraph_url', '').strip() or None
    author.website = request.form.get('website', '').strip() or None
    author.alias_of_id = request.form.get('alias_of_id', type=int) or None

    if is_new:
        db.session.add(author)
    db.session.commit()
    flash('Author saved successfully', 'success')
    return redirect(url_for('author_detail', id=author.id))


@app.route('/authors/quick-add', methods=['POST'])
def author_quick_add():
    """Quick add an author via htmx from the book form."""
    name = request.form.get('name', '').strip()
    if not name:
        return '<p class="error">Name is required</p>', 400

    author = Author(name=name)
    db.session.add(author)
    db.session.commit()

    # Return the new author as a selected chip
    return render_template('books/_author_chip.html', author=author)


@app.route('/authors/search')
def author_search():
    """Search authors for the author picker."""
    query = request.args.get('q', '').strip()
    exclude_str = request.args.get('exclude', '')

    # Parse comma-separated exclude IDs
    exclude_ids = []
    if exclude_str:
        exclude_ids = [int(x) for x in exclude_str.split(',') if x.strip().isdigit()]

    if len(query) < 1:
        return ''

    authors = Author.query.filter(
        Author.alias_of_id.is_(None),
        Author.name.ilike(f'%{query}%')
    )

    if exclude_ids:
        authors = authors.filter(~Author.id.in_(exclude_ids))

    authors = authors.order_by(Author.name).limit(10).all()
    return render_template('books/_author_search_results.html', authors=authors, query=query)


@app.route('/authors/<int:id>/delete', methods=['DELETE', 'POST'])
def author_delete(id):
    author = Author.query.get_or_404(id)
    # Remove author from books (but don't delete books)
    author.books = []
    # Update any aliases pointing to this author
    Author.query.filter_by(alias_of_id=id).update({'alias_of_id': None})
    db.session.delete(author)
    db.session.commit()
    flash('Author deleted successfully', 'success')

    if request.headers.get('HX-Request'):
        return '', 200, {'HX-Redirect': url_for('author_list')}
    return redirect(url_for('author_list'))


# Series routes
@app.route('/series')
def series_list():
    search = request.args.get('search', '').strip()
    query = Series.query
    if search:
        query = query.filter(Series.name.ilike(f'%{search}%'))
    all_series = query.order_by(Series.name).all()
    return render_template('series/list.html', series_list=all_series, search=search)


@app.route('/series/<int:id>')
def series_detail(id):
    series = Series.query.get_or_404(id)
    return render_template('series/detail.html', series=series)


@app.route('/series/new', methods=['GET', 'POST'])
def series_new():
    if request.method == 'POST':
        return save_series(None)
    return render_template('series/form.html', series=None)


@app.route('/series/<int:id>/edit', methods=['GET', 'POST'])
def series_edit(id):
    series = Series.query.get_or_404(id)
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
    series.goodreads_url = request.form.get('goodreads_url', '').strip() or None
    series.amazon_url = request.form.get('amazon_url', '').strip() or None
    series.storygraph_url = request.form.get('storygraph_url', '').strip() or None

    if is_new:
        db.session.add(series)
    db.session.commit()
    flash('Series saved successfully', 'success')
    return redirect(url_for('series_detail', id=series.id))


@app.route('/series/<int:id>/delete', methods=['DELETE', 'POST'])
def series_delete(id):
    series = Series.query.get_or_404(id)
    # Remove series from books (but don't delete books)
    Book.query.filter_by(series_id=id).update({'series_id': None, 'series_number': None})
    db.session.delete(series)
    db.session.commit()
    flash('Series deleted successfully', 'success')

    if request.headers.get('HX-Request'):
        return '', 200, {'HX-Redirect': url_for('series_list')}
    return redirect(url_for('series_list'))


# Search routes
@app.route('/search')
def search():
    query = request.args.get('q', '').strip()
    books = []
    authors = []
    series_results = []

    if query:
        # Search books
        books = Book.query.filter(
            db.or_(
                Book.title.ilike(f'%{query}%'),
                Book.subtitle.ilike(f'%{query}%'),
                Book.description.ilike(f'%{query}%')
            )
        ).order_by(Book.title).limit(20).all()

        # Search authors
        authors = Author.query.filter(
            Author.name.ilike(f'%{query}%')
        ).order_by(Author.name).limit(20).all()

        # Search series
        series_results = Series.query.filter(
            Series.name.ilike(f'%{query}%')
        ).order_by(Series.name).limit(20).all()

    # For htmx requests, return just the results
    if request.headers.get('HX-Request'):
        return render_template('search_results.html',
                             query=query,
                             books=books,
                             authors=authors,
                             series_results=series_results)

    return render_template('search.html',
                         query=query,
                         books=books,
                         authors=authors,
                         series_results=series_results)


if __name__ == '__main__':
    init_db(app)
    app.run(debug=True)
