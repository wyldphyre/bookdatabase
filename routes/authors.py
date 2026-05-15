from flask import Blueprint, render_template, request, redirect, url_for, flash
from sqlalchemy.orm import subqueryload
from models import db, Book, Author, AuthorGender, Tag

authors_bp = Blueprint('authors', __name__)


@authors_bp.route('/authors', endpoint='author_list')
def author_list():
    search = request.args.get('search', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)
    if per_page not in [10, 25, 50, 100]:
        per_page = 25
    query = Author.query.options(subqueryload(Author.books)).filter_by(alias_of_id=None)
    if search:
        query = query.filter(Author.name.ilike(f'%{search}%'))
    authors = query.order_by(Author.name).paginate(page=page, per_page=per_page, error_out=False)
    return render_template('authors/list.html', authors=authors, search=search, per_page=per_page)


@authors_bp.route('/authors/<int:id>', endpoint='author_detail')
def author_detail(id):
    author = Author.query.options(
        subqueryload(Author.books).subqueryload(Book.reads)
    ).get_or_404(id)
    sorted_books = sorted(author.books, key=lambda b: (
        b.series.name.lower() if b.series else '\xff',
        b.series_number or 0,
        b.title.lower()
    ))
    return render_template('authors/detail.html', author=author, sorted_books=sorted_books)


@authors_bp.route('/authors/new', methods=['GET', 'POST'], endpoint='author_new')
def author_new():
    if request.method == 'POST':
        return save_author(None)

    genders = AuthorGender.query.all()
    authors = Author.query.filter_by(alias_of_id=None).order_by(Author.name).all()
    return render_template('authors/form.html', author=None, genders=genders, authors=authors)


@authors_bp.route('/authors/<int:id>/edit', methods=['GET', 'POST'], endpoint='author_edit')
def author_edit(id):
    author = db.get_or_404(Author, id)
    if request.method == 'POST':
        return save_author(author)

    genders = AuthorGender.query.all()
    # Exclude self from alias options
    authors = Author.query.filter(Author.id != id, Author.alias_of_id.is_(None)).order_by(Author.name).all()
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

    # Handle tags
    tag_ids = request.form.getlist('tags')
    author.tags = Tag.query.filter(Tag.id.in_(tag_ids)).all() if tag_ids else []

    if is_new:
        db.session.add(author)
    db.session.commit()
    flash('Author saved successfully', 'success')
    return redirect(url_for('author_detail', id=author.id))


@authors_bp.route('/authors/quick-add', methods=['POST'], endpoint='author_quick_add')
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


@authors_bp.route('/authors/search', endpoint='author_search')
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


@authors_bp.route('/authors/<int:id>/delete', methods=['DELETE', 'POST'], endpoint='author_delete')
def author_delete(id):
    author = db.get_or_404(Author, id)
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
