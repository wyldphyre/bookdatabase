from datetime import datetime, timedelta
from flask import Blueprint, render_template, request
from sqlalchemy import func
from sqlalchemy.orm import joinedload, subqueryload
from models import db, Book, Author, Series, Read, AuthorGender, BookFormat, Tag, RATING_LABELS, book_tags, author_tags, series_tags

search_bp = Blueprint('search', __name__)


@search_bp.route('/search', endpoint='search')
def search():
    query = request.args.get('q', '').strip()
    include_tags = request.args.get('tags') == '1'
    books = []
    authors = []
    series_results = []

    if query:
        # Search books
        book_filters = [
            Book.title.ilike(f'%{query}%'),
            Book.subtitle.ilike(f'%{query}%'),
            Book.description.ilike(f'%{query}%')
        ]
        if include_tags:
            book_filters.append(
                Book.tags.any(Tag.name.ilike(f'%{query}%'))
            )
        books = Book.query.filter(
            db.or_(*book_filters)
        ).order_by(Book.title).limit(20).all()

        # Search authors
        author_filters = [Author.name.ilike(f'%{query}%')]
        if include_tags:
            author_filters.append(
                Author.tags.any(Tag.name.ilike(f'%{query}%'))
            )
        authors = Author.query.filter(
            db.or_(*author_filters)
        ).order_by(Author.name).limit(20).all()

        # Search series
        series_filters = [Series.name.ilike(f'%{query}%')]
        if include_tags:
            series_filters.append(
                Series.tags.any(Tag.name.ilike(f'%{query}%'))
            )
        series_results = Series.query.filter(
            db.or_(*series_filters)
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
                         include_tags=include_tags,
                         books=books,
                         authors=authors,
                         series_results=series_results)


@search_bp.route('/recommendations', endpoint='recommendations')
def recommendations():
    twelve_months_ago = datetime.now() - timedelta(days=365)

    # --- Continue Reading: next unread book in recently-read series ---
    # Find series where user completed a book in the last 12 months
    recent_series_reads = db.session.query(
        Book.series_id,
        func.max(Book.series_number).label('max_read_num'),
        func.max(Read.finish_date).label('last_finished')
    ).join(Read, Read.book_id == Book.id)\
     .filter(
        Read.status == 'Completed',
        Read.finish_date >= twelve_months_ago,
        Book.series_id.isnot(None)
    ).group_by(Book.series_id).all()

    continue_reading = []
    for series_id, max_read_num, last_finished in recent_series_reads:
        # Find all completed/reading book numbers in this series
        read_numbers = db.session.query(Book.series_number).join(
            Read, Read.book_id == Book.id
        ).filter(
            Book.series_id == series_id,
            Read.status.in_(['Completed', 'Reading'])
        ).all()
        read_nums = {r[0] for r in read_numbers if r[0] is not None}

        # Find the next unread book in the series (lowest series_number not yet read)
        next_book = Book.query.options(
            subqueryload(Book.authors),
            joinedload(Book.series)
        ).filter(
            Book.series_id == series_id,
            Book.series_number.isnot(None),
            ~Book.series_number.in_(read_nums) if read_nums else True
        ).order_by(Book.series_number).first()

        if next_book:
            # Get the last book read in this series for context
            last_read_book = db.session.query(Book).join(
                Read, Read.book_id == Book.id
            ).filter(
                Book.series_id == series_id,
                Read.status == 'Completed'
            ).order_by(Read.finish_date.desc()).first()
            continue_reading.append((next_book, last_read_book, last_finished))

    # Sort by most recently read series first
    continue_reading.sort(key=lambda x: x[2] or datetime.min, reverse=True)

    # --- From the Pile: random unread books ---
    # Books with no Completed or Reading reads
    read_book_ids = db.session.query(Read.book_id).filter(
        Read.status.in_(['Completed', 'Reading'])
    ).distinct()

    from_the_pile = Book.query.options(
        subqueryload(Book.authors),
        joinedload(Book.series)
    ).filter(
        ~Book.id.in_(read_book_ids)
    ).order_by(func.random()).limit(5).all()

    # --- Recent Additions: newest unread books ---
    recent_additions = Book.query.options(
        subqueryload(Book.authors),
        joinedload(Book.series)
    ).filter(
        ~Book.id.in_(read_book_ids),
        Book.date_added.isnot(None)
    ).order_by(Book.date_added.desc()).limit(10).all()

    return render_template('recommendations.html',
                         continue_reading=continue_reading,
                         from_the_pile=from_the_pile,
                         recent_additions=recent_additions)


@search_bp.route('/statistics', endpoint='statistics')
def statistics():
    from collections import defaultdict
    from models import book_authors

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
    rating_data = {RATING_LABELS.get(int(rating), str(rating)): count for rating, count in rating_stats}

    # Books read per month (last 12 months)
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
    most_common_rating = None
    if rating_stats:
        most_common_entry = max(rating_stats, key=lambda x: x[1])
        most_common_rating = RATING_LABELS.get(int(most_common_entry[0]))

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
    total_saved = db.session.query(
        func.sum(Book.cost - Book.paid)
    ).filter(Book.cost.isnot(None), Book.paid.isnot(None)).scalar() or 0

    # Books read per year (completed reads with a finish date)
    read_by_year_rows = db.session.query(
        func.strftime('%Y', Read.finish_date),
        func.count(Read.id)
    ).filter(Read.status == 'Completed', Read.finish_date.isnot(None))\
     .group_by(func.strftime('%Y', Read.finish_date))\
     .order_by(func.strftime('%Y', Read.finish_date)).all()
    read_by_year = {year: count for year, count in read_by_year_rows if year}

    # Books purchased per year
    added_by_year_rows = db.session.query(
        func.strftime('%Y', Book.date_purchased),
        func.count(Book.id)
    ).filter(Book.date_purchased.isnot(None))\
     .group_by(func.strftime('%Y', Book.date_purchased))\
     .order_by(func.strftime('%Y', Book.date_purchased)).all()
    added_by_year = {year: count for year, count in added_by_year_rows if year}

    # Spending and savings per year
    spend_save_rows = db.session.query(
        func.strftime('%Y', Book.date_purchased),
        func.sum(Book.paid),
        func.sum(Book.cost)
    ).filter(
        Book.date_purchased.isnot(None)
    ).group_by(func.strftime('%Y', Book.date_purchased))\
     .order_by(func.strftime('%Y', Book.date_purchased)).all()
    spent_by_year = {year: round(float(paid), 2) for year, paid, cost in spend_save_rows if year and paid is not None}
    saved_by_year = {year: round(float((cost or 0) - (paid or 0)), 2) for year, paid, cost in spend_save_rows if year and cost is not None}

    # Tag statistics
    total_tags = Tag.query.count()

    # Top tags by total usage (across books, authors, series)
    tag_book_counts = db.session.query(
        Tag.id, Tag.name, func.count(book_tags.c.book_id).label('count')
    ).outerjoin(book_tags, Tag.id == book_tags.c.tag_id)\
     .group_by(Tag.id, Tag.name).all()

    tag_author_counts = db.session.query(
        Tag.id, func.count(author_tags.c.author_id).label('count')
    ).outerjoin(author_tags, Tag.id == author_tags.c.tag_id)\
     .group_by(Tag.id).all()

    tag_series_counts = db.session.query(
        Tag.id, func.count(series_tags.c.series_id).label('count')
    ).outerjoin(series_tags, Tag.id == series_tags.c.tag_id)\
     .group_by(Tag.id).all()

    # Merge counts
    tag_totals = {}
    tag_names = {}
    tag_by_type = {}
    for tag_id, tag_name, count in tag_book_counts:
        tag_totals[tag_id] = count
        tag_names[tag_id] = tag_name
        tag_by_type[tag_name] = {'books': count, 'authors': 0, 'series': 0}
    for tag_id, count in tag_author_counts:
        tag_totals[tag_id] = tag_totals.get(tag_id, 0) + count
        if tag_names.get(tag_id) in tag_by_type:
            tag_by_type[tag_names[tag_id]]['authors'] = count
    for tag_id, count in tag_series_counts:
        tag_totals[tag_id] = tag_totals.get(tag_id, 0) + count
        if tag_names.get(tag_id) in tag_by_type:
            tag_by_type[tag_names[tag_id]]['series'] = count

    # Sort by total usage, take top 15
    top_tags = sorted(tag_totals.items(), key=lambda x: x[1], reverse=True)[:15]
    top_tag_data = {tag_names[tid]: count for tid, count in top_tags if count > 0}

    # Breakdown for top tags (books/authors/series stacked)
    top_tag_breakdown = {name: tag_by_type[name] for name in top_tag_data}

    # Page count distribution
    page_count_data = {
        '<300': Book.query.filter(Book.page_count.isnot(None), Book.page_count < 300).count(),
        '300-499': Book.query.filter(Book.page_count.isnot(None), Book.page_count >= 300, Book.page_count < 500).count(),
        '500+': Book.query.filter(Book.page_count.isnot(None), Book.page_count >= 500).count(),
    }
    page_count_data = {k: v for k, v in page_count_data.items() if v > 0}

    # Most read books (by number of completed reads)
    most_read_books = db.session.query(
        Book, func.count(Read.id).label('read_count')
    ).join(Read, Read.book_id == Book.id)\
     .filter(Read.status == 'Completed')\
     .group_by(Book.id)\
     .order_by(func.count(Read.id).desc())\
     .limit(10).all()

    # Most read authors (by number of completed reads across their books)
    most_read_authors = db.session.query(
        Author, func.count(Read.id).label('read_count')
    ).join(book_authors, Author.id == book_authors.c.author_id)\
     .join(Book, Book.id == book_authors.c.book_id)\
     .join(Read, Read.book_id == Book.id)\
     .filter(Read.status == 'Completed', Author.alias_of_id.is_(None))\
     .group_by(Author.id)\
     .order_by(func.count(Read.id).desc())\
     .limit(10).all()

    # Most read authors (by distinct books read — multiple reads of same book count once)
    most_read_authors_distinct = db.session.query(
        Author, func.count(func.distinct(Book.id)).label('book_count')
    ).join(book_authors, Author.id == book_authors.c.author_id)\
     .join(Book, Book.id == book_authors.c.book_id)\
     .join(Read, Read.book_id == Book.id)\
     .filter(Read.status == 'Completed', Author.alias_of_id.is_(None))\
     .group_by(Author.id)\
     .order_by(func.count(func.distinct(Book.id)).desc())\
     .limit(10).all()

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
                         total_tags=total_tags,
                         books_with_rating=books_with_rating,
                         most_common_rating=most_common_rating,
                         pages_read=pages_read,
                         avg_days=round(avg_days, 1),
                         total_spent=total_spent,
                         total_saved=total_saved,
                         spent_by_year=spent_by_year,
                         saved_by_year=saved_by_year,
                         top_tag_data=top_tag_data,
                         top_tag_breakdown=top_tag_breakdown,
                         added_by_year=added_by_year,
                         read_by_year=read_by_year,
                         most_read_books=most_read_books,
                         most_read_authors=most_read_authors,
                         most_read_authors_distinct=most_read_authors_distinct,
                         page_count_data=page_count_data)
