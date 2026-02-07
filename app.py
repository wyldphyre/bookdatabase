import os
import re
import requests as http_requests
from datetime import datetime
from urllib.parse import urlparse
from flask import Flask, render_template, request, redirect, url_for, flash, session
from werkzeug.utils import secure_filename
from bs4 import BeautifulSoup
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


# Custom Jinja filter to calculate days between dates
@app.template_filter('days_since')
def days_since_filter(date):
    """Calculate days since a given date."""
    if not date:
        return None
    from datetime import date as date_type
    today = date_type.today()
    if hasattr(date, 'date'):
        date = date.date()
    return (today - date).days


@app.template_filter('days_between')
def days_between_filter(start_date, end_date):
    """Calculate days between two dates."""
    if not start_date or not end_date:
        return None
    if hasattr(start_date, 'date'):
        start_date = start_date.date()
    if hasattr(end_date, 'date'):
        end_date = end_date.date()
    return (end_date - start_date).days


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
    per_page = request.args.get('per_page', 10, type=int)
    # Constrain to valid options
    if per_page not in [10, 25, 50, 100]:
        per_page = 10
    books = Book.query.order_by(Book.date_added.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    return render_template('books/list.html', books=books, per_page=per_page)


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

    # Check for pre-filled data from import
    prefill = session.pop('book_prefill', None)

    return render_template('books/form.html',
                         book=None,
                         formats=formats,
                         authors=authors,
                         series_list=series_list,
                         prefill=prefill)


@app.route('/books/import')
def book_import():
    """Import book data from external URLs."""
    source = request.args.get('source', '')
    url = request.args.get('url', '')

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
            session['book_prefill'] = book_data
            flash('Book data imported. Please review and save.', 'success')
        else:
            flash('Could not extract book data from URL', 'warning')
    except Exception as e:
        flash(f'Error importing book: {str(e)}', 'error')

    return redirect(url_for('book_new'))


def fetch_page(url):
    """Fetch a page with appropriate headers."""
    # Parse the URL to get the host for Referer header
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Cache-Control': 'max-age=0',
        'Referer': base_url,
    }
    response = http_requests.get(url, headers=headers, timeout=15, allow_redirects=True)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def scrape_amazon(url):
    """Scrape book data from Amazon."""
    soup = fetch_page(url)

    data = {}

    # Title
    title_el = soup.select_one('#productTitle, #ebooksProductTitle')
    if title_el:
        data['title'] = title_el.get_text(strip=True)

    # Authors (get all, deduplicate while preserving order)
    author_els = soup.select('#bylineInfo .author a, .author a, .contributorNameID')
    if author_els:
        seen = set()
        authors = []
        for el in author_els:
            name = el.get_text(strip=True)
            if name and name not in seen:
                seen.add(name)
                authors.append(name)
        if authors:
            data['authors'] = authors

    # Description
    desc_el = soup.select_one('#bookDescription_feature_div .a-expander-content, #productDescription')
    if desc_el:
        data['description'] = desc_el.get_text(strip=True)

    # Cover image
    img_el = soup.select_one('#imgBlkFront, #ebooksImgBlkFront, #landingImage')
    if img_el:
        data['cover_url'] = img_el.get('src') or img_el.get('data-a-dynamic-image', '').split('"')[1] if '"' in img_el.get('data-a-dynamic-image', '') else None

    # Page count
    details = soup.select('#detailBullets_feature_div li, #productDetailsTable .content li')
    for detail in details:
        text = detail.get_text()
        if 'pages' in text.lower():
            match = re.search(r'(\d+)\s*pages', text, re.IGNORECASE)
            if match:
                data['page_count'] = int(match.group(1))
                break

    # Series info from title or breadcrumb
    series_el = soup.select_one('#seriesBulletWidget_feature_div a')
    if series_el:
        series_text = series_el.get_text(strip=True)
        data['series_name'] = series_text

    return data if data.get('title') else None


def scrape_goodreads(url):
    """Scrape book data from Goodreads."""
    soup = fetch_page(url)

    data = {}

    # Title
    title_el = soup.select_one('h1[data-testid="bookTitle"], h1.Text__title1')
    if title_el:
        data['title'] = title_el.get_text(strip=True)

    # Authors (get all, deduplicate while preserving order)
    author_els = soup.select('span[data-testid="name"], a.ContributorLink')
    if author_els:
        seen = set()
        authors = []
        for el in author_els:
            name = el.get_text(strip=True)
            if name and name not in seen:
                seen.add(name)
                authors.append(name)
        if authors:
            data['authors'] = authors

    # Description
    desc_el = soup.select_one('div[data-testid="description"] .Formatted, span.Formatted')
    if desc_el:
        data['description'] = desc_el.get_text(strip=True)

    # Cover image
    img_el = soup.select_one('img.ResponsiveImage, div.BookCover img')
    if img_el:
        data['cover_url'] = img_el.get('src')

    # Page count
    pages_el = soup.select_one('p[data-testid="pagesFormat"]')
    if pages_el:
        text = pages_el.get_text()
        match = re.search(r'(\d+)\s*pages', text, re.IGNORECASE)
        if match:
            data['page_count'] = int(match.group(1))

    # Series
    series_el = soup.select_one('h3.Text__italic a, div[data-testid="bookSeries"] a')
    if series_el:
        series_text = series_el.get_text(strip=True)
        # Parse "Series Name #1" format
        match = re.match(r'(.+?)\s*#(\d+(?:\.\d+)?)', series_text)
        if match:
            data['series_name'] = match.group(1).strip()
            data['series_number'] = float(match.group(2))
        else:
            data['series_name'] = series_text

    # Goodreads URL for author
    data['goodreads_url'] = url

    return data if data.get('title') else None


def scrape_amazon_series(url):
    """Scrape series page from Amazon to get book count."""
    try:
        soup = fetch_page(url)

        # Look for book count in series page
        # Amazon shows "X books" or "X titles" in series
        count_el = soup.select_one('.series-childAsin-count, .seriesHeader span')
        if count_el:
            text = count_el.get_text()
            match = re.search(r'(\d+)\s*(?:book|title|item)', text, re.IGNORECASE)
            if match:
                return int(match.group(1))

        # Alternative: count items in series list
        items = soup.select('.series-childAsin-item, .seriesItem')
        if items:
            return len(items)

        return None
    except Exception:
        return None


def scrape_goodreads_series(url):
    """Scrape series page from Goodreads to get book count."""
    try:
        soup = fetch_page(url)

        # Goodreads shows "X primary works, Y total" or just lists books
        # Look for the count text
        count_el = soup.select_one('.responsiveSeriesHeader__subtitle, .seriesDesc')
        if count_el:
            text = count_el.get_text()
            # Match "X primary works" or "X works"
            match = re.search(r'(\d+)\s*(?:primary\s+)?works?', text, re.IGNORECASE)
            if match:
                return int(match.group(1))

        # Alternative: count book entries
        items = soup.select('.listWithDividers__item, .bookTitle')
        if items:
            # Filter to only numbered entries (main series books)
            numbered_count = 0
            for item in items:
                num_el = item.select_one('.responsiveBook__seriesNum, .bookMeta')
                if num_el:
                    text = num_el.get_text()
                    if re.search(r'^#?\d+(\.\d+)?$', text.strip()):
                        numbered_count += 1
            if numbered_count > 0:
                return numbered_count
            return len(items)

        return None
    except Exception:
        return None


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

    # Handle cover image upload (file takes priority over URL)
    if 'cover_image' in request.files:
        file = request.files['cover_image']
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            # Add timestamp to avoid conflicts
            base, ext = os.path.splitext(filename)
            filename = f"{base}_{int(datetime.now().timestamp())}{ext}"
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            book.cover_image = filename

    # Handle cover image URL (only if no file uploaded)
    cover_url = request.form.get('cover_image_url', '').strip()
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
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            with open(filepath, 'wb') as f:
                f.write(response.content)
            book.cover_image = filename
        except Exception as e:
            flash(f'Could not download cover image: {str(e)}', 'warning')

    if is_new:
        db.session.add(book)
    db.session.commit()
    flash('Book saved successfully', 'success')
    return redirect(url_for('book_detail', id=book.id))


@app.route('/books/<int:id>/delete', methods=['DELETE', 'POST'])
def book_delete(id):
    book = Book.query.get_or_404(id)

    # Delete cover image file if it exists
    if book.cover_image:
        cover_path = os.path.join(app.config['UPLOAD_FOLDER'], book.cover_image)
        if os.path.exists(cover_path):
            os.remove(cover_path)

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


@app.route('/reads/<int:id>/complete', methods=['POST'])
def read_complete(id):
    read = Read.query.get_or_404(id)
    read.status = 'Completed'
    read.finish_date = datetime.now()
    db.session.commit()
    flash('Read marked as completed!', 'success')
    return redirect(url_for('book_detail', id=read.book_id))


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


@app.route('/series/search')
def series_search():
    """Search series for the series picker."""
    query = request.args.get('q', '').strip()
    current_id = request.args.get('current', '')

    if len(query) < 1:
        return ''

    series_query = Series.query.filter(Series.name.ilike(f'%{query}%'))

    # Exclude current selection if provided
    if current_id and current_id.isdigit():
        series_query = series_query.filter(Series.id != int(current_id))

    series_list = series_query.order_by(Series.name).limit(10).all()
    return render_template('books/_series_search_results.html', series_list=series_list, query=query)


@app.route('/series/quick-add', methods=['POST'])
def series_quick_add():
    """Quick add a series via htmx from the book form."""
    name = request.form.get('series_name', '').strip()
    if not name:
        return '<p class="error">Name is required</p>', 400

    series = Series(name=name)
    db.session.add(series)
    db.session.commit()

    # Return the new series as a selected chip
    return render_template('books/_series_chip.html', series=series)


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


@app.route('/series/<int:id>/update-count', methods=['POST'])
def series_update_count(id):
    series = Series.query.get_or_404(id)
    count = None

    # Try Goodreads first, then Amazon
    if series.goodreads_url:
        count = scrape_goodreads_series(series.goodreads_url)
    if count is None and series.amazon_url:
        count = scrape_amazon_series(series.amazon_url)

    if count is not None:
        if series.number_in_series != count:
            series.number_in_series = count
            db.session.commit()
            flash(f'Series updated: {count} books in series', 'success')
        else:
            flash(f'Series count is already up to date ({count} books)', 'success')
    else:
        flash('Could not determine book count from the series page', 'error')

    return redirect(url_for('series_detail', id=id))


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


@app.route('/statistics')
def statistics():
    from sqlalchemy import func
    from collections import defaultdict

    # Author gender breakdown
    gender_stats = db.session.query(
        AuthorGender.name,
        func.count(Author.id)
    ).outerjoin(Author, Author.gender_id == AuthorGender.id)\
     .group_by(AuthorGender.id, AuthorGender.name).all()

    # Count authors with no gender set
    no_gender_count = Author.query.filter_by(gender_id=None).count()
    gender_data = {name: count for name, count in gender_stats if count > 0}
    if no_gender_count > 0:
        gender_data['Not Set'] = no_gender_count

    # Book format breakdown
    format_stats = db.session.query(
        BookFormat.name,
        func.count(Book.id)
    ).outerjoin(Book, Book.format_id == BookFormat.id)\
     .group_by(BookFormat.id, BookFormat.name).all()
    format_data = {name: count for name, count in format_stats if count > 0}

    # Rating distribution
    rating_stats = db.session.query(
        Book.rating,
        func.count(Book.id)
    ).filter(Book.rating.isnot(None))\
     .group_by(Book.rating)\
     .order_by(Book.rating).all()
    rating_data = {str(rating): count for rating, count in rating_stats}

    # Books read per month (last 12 months)
    from datetime import datetime, timedelta
    twelve_months_ago = datetime.now() - timedelta(days=365)
    monthly_reads = db.session.query(
        func.strftime('%Y-%m', Read.finish_date),
        func.count(Read.id)
    ).filter(
        Read.status == 'Completed',
        Read.finish_date >= twelve_months_ago
    ).group_by(func.strftime('%Y-%m', Read.finish_date))\
     .order_by(func.strftime('%Y-%m', Read.finish_date)).all()

    # Fill in missing months
    monthly_data = {}
    current = twelve_months_ago.replace(day=1)
    while current <= datetime.now():
        key = current.strftime('%Y-%m')
        monthly_data[key] = 0
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    for month, count in monthly_reads:
        if month in monthly_data:
            monthly_data[month] = count

    # Reading completion rate
    completion_stats = db.session.query(
        Read.status,
        func.count(Read.id)
    ).group_by(Read.status).all()
    completion_data = {status: count for status, count in completion_stats}

    # Summary statistics
    total_books = Book.query.count()
    total_authors = Author.query.filter_by(alias_of_id=None).count()
    total_series = Series.query.count()
    total_reads = Read.query.filter_by(status='Completed').count()
    books_with_rating = Book.query.filter(Book.rating.isnot(None)).count()
    avg_rating = db.session.query(func.avg(Book.rating)).filter(Book.rating.isnot(None)).scalar() or 0

    # Pages read
    pages_read = db.session.query(func.sum(Book.page_count)).join(Read).filter(
        Read.status == 'Completed'
    ).scalar() or 0

    # Average days to finish
    completed_with_dates = Read.query.filter(
        Read.status == 'Completed',
        Read.start_date.isnot(None),
        Read.finish_date.isnot(None)
    ).all()
    if completed_with_dates:
        total_days = sum((r.finish_date - r.start_date).days for r in completed_with_dates)
        avg_days = total_days / len(completed_with_dates)
    else:
        avg_days = 0

    # Financial stats
    total_spent = db.session.query(func.sum(Book.paid)).scalar() or 0
    total_saved = db.session.query(func.sum(Book.discounts)).scalar() or 0

    return render_template('statistics.html',
                         gender_data=gender_data,
                         format_data=format_data,
                         rating_data=rating_data,
                         monthly_data=monthly_data,
                         completion_data=completion_data,
                         total_books=total_books,
                         total_authors=total_authors,
                         total_series=total_series,
                         total_reads=total_reads,
                         books_with_rating=books_with_rating,
                         avg_rating=round(avg_rating, 2),
                         pages_read=pages_read,
                         avg_days=round(avg_days, 1),
                         total_spent=total_spent,
                         total_saved=total_saved)


if __name__ == '__main__':
    init_db(app)
    app.run(debug=True)
