from datetime import datetime, timedelta
from flask import Blueprint, render_template, request
from sqlalchemy import func
from sqlalchemy.orm import joinedload, subqueryload
from models import db, Book, Author, Series, Read, BookFormat, Tag
from stats import DAYS_PER_MONTH, STATISTICS_TABS, STATISTICS_TAB_DATA

search_bp = Blueprint('search', __name__)


@search_bp.route('/search', endpoint='search')
def search():
    query = request.args.get('q', '').strip()
    include_tags = request.args.get('tags') == '1'
    books = []
    authors = []
    series_results = []

    if query:
        # Search books — also match books whose author name matches the query
        book_filters = [
            Book.title.ilike(f'%{query}%'),
            Book.subtitle.ilike(f'%{query}%'),
            Book.description.ilike(f'%{query}%'),
            Book.authors.any(Author.name.ilike(f'%{query}%')),
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
    if recent_series_reads:
        series_ids = [r[0] for r in recent_series_reads]

        # Batch: all read/reading series numbers across every relevant series (1 query)
        read_nums_by_series = {}
        for sid, snum in db.session.query(Book.series_id, Book.series_number)\
                .join(Read, Read.book_id == Book.id)\
                .filter(
                    Book.series_id.in_(series_ids),
                    Read.status.in_(['Completed', 'Reading']),
                    Book.series_number.isnot(None)
                ).all():
            read_nums_by_series.setdefault(sid, set()).add(snum)

        # Batch: last book read per series (1 query via max finish_date subquery)
        last_finish_sq = db.session.query(
            Book.series_id,
            func.max(Read.finish_date).label('max_finish')
        ).join(Read, Read.book_id == Book.id)\
         .filter(Book.series_id.in_(series_ids), Read.status == 'Completed')\
         .group_by(Book.series_id).subquery()

        last_book_by_series = {}
        for b in db.session.query(Book)\
                .join(last_finish_sq, Book.series_id == last_finish_sq.c.series_id)\
                .join(Read, (Read.book_id == Book.id)
                      & (Read.finish_date == last_finish_sq.c.max_finish)
                      & (Read.status == 'Completed'))\
                .all():
            last_book_by_series[b.series_id] = b

        for series_id, _max_read_num, last_finished in recent_series_reads:
            read_nums = read_nums_by_series.get(series_id, set())

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
                continue_reading.append((next_book, last_book_by_series.get(series_id), last_finished))

    # Sort by most recently read series first
    continue_reading.sort(key=lambda x: x[2] or datetime.min, reverse=True)

    # --- From the Pile: random unread books you own ---
    # Materialise read/active book IDs once and reuse for both unread queries
    read_book_ids = {r[0] for r in db.session.query(Read.book_id).filter(
        Read.status.in_(['Completed', 'Reading'])
    ).distinct().all()}

    from_the_pile = Book.query.options(
        subqueryload(Book.authors),
        joinedload(Book.series)
    ).filter(
        ~Book.id.in_(read_book_ids),
        Book.owned.is_(True)
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
    tab = request.args.get('tab', 'overview')
    if tab not in STATISTICS_TAB_DATA:
        tab = 'overview'
    return render_template('statistics.html',
                           tab=tab,
                           tabs=STATISTICS_TABS,
                           # Each format keeps one colour on every chart
                           format_names=[name for (name,) in db.session.query(BookFormat.name).order_by(BookFormat.id)],
                           days_per_month=DAYS_PER_MONTH,
                           **STATISTICS_TAB_DATA[tab]())
