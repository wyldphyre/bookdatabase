"""Year-scoped reading and spending figures for the dashboard's year tab.

Kept out of the route modules so the same numbers can be reused elsewhere (the
statistics page computes overlapping figures across all years).
"""
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import aliased

from models import db, Book, Author, AuthorGender, Read, book_authors


def year_window(year, days_elapsed=None):
    """Half-open [start, end) datetime bounds for a year.

    With days_elapsed given, the window stops that many days into the year
    instead of at its end — that's how the previous year is measured over the
    same slice of the calendar, so a part-finished year isn't compared against
    a complete one. Aligning on elapsed days rather than the same month/day
    also sidesteps Feb 29 having no counterpart in a non-leap year.
    """
    start = datetime(year, 1, 1)
    full_end = datetime(year + 1, 1, 1)
    if days_elapsed is None:
        return start, full_end
    return start, min(start + timedelta(days=days_elapsed), full_end)


def days_elapsed_in_year(today=None):
    """Days of the current year gone by, counting today as one."""
    today = today or datetime.now()
    return (today - datetime(today.year, 1, 1)).days + 1


def year_reading_stats(year, days_elapsed=None, include_books=False):
    """Every figure the year tab shows, for one year.

    Reads count when they're Completed with a finish_date inside the window;
    reads with no finish_date belong to no year and are excluded throughout.
    Purchases count on date_purchased.
    """
    start, end = year_window(year, days_elapsed)

    finished = db.and_(Read.status == 'Completed',
                       Read.finish_date >= start,
                       Read.finish_date < end)
    purchased = db.and_(Book.date_purchased >= start,
                        Book.date_purchased < end)

    # An alias and the author it points at are the same person, so fold each
    # credit onto the canonical author before counting. (The statistics page
    # instead drops aliases entirely, which would lose books credited to one.)
    canonical_id = func.coalesce(Author.alias_of_id, Author.id)

    reads_q = db.session.query(Read).join(Book, Book.id == Read.book_id).filter(finished)

    books_read = reads_q.with_entities(func.count(Read.id)).scalar() or 0
    series_read = reads_q.with_entities(
        func.count(func.distinct(Book.series_id))).scalar() or 0
    pages_read = reads_q.with_entities(func.sum(Book.page_count)).scalar() or 0

    authors_read = (reads_q
                    .join(book_authors, book_authors.c.book_id == Book.id)
                    .join(Author, Author.id == book_authors.c.author_id)
                    .with_entities(func.count(func.distinct(canonical_id)))
                    .scalar() or 0)

    # Gender comes from the canonical author, not the alias, so a pen name with
    # no gender set doesn't land in "Not Set" while its real author is filled in.
    canonical = aliased(Author)
    gender_rows = (reads_q
                   .join(book_authors, book_authors.c.book_id == Book.id)
                   .join(Author, Author.id == book_authors.c.author_id)
                   .join(canonical, canonical.id == canonical_id)
                   .outerjoin(AuthorGender, AuthorGender.id == canonical.gender_id)
                   .with_entities(AuthorGender.name, func.count(func.distinct(canonical.id)))
                   .group_by(AuthorGender.name)
                   .all())
    gender_breakdown = sorted(((name or 'Not Set', count) for name, count in gender_rows),
                              key=lambda row: row[1], reverse=True)

    spans = (reads_q
             .filter(Read.start_date.isnot(None))
             .with_entities(Read.start_date, Read.finish_date)
             .all())
    avg_days = (sum((f - s).days for s, f in spans) / len(spans)) if spans else None

    # Pace is measured over the part of the window that has actually happened —
    # dividing this year's reads by a full twelve months would understate it.
    measured_end = min(end, datetime.now() + timedelta(days=1))
    months = max((measured_end - start).days, 1) / 30.44
    per_month = books_read / months

    books_purchased = db.session.query(func.count(Book.id)).filter(purchased).scalar() or 0
    total_paid = db.session.query(func.sum(Book.paid)).filter(purchased).scalar() or 0
    total_saved = db.session.query(func.sum(Book.cost - Book.paid)).filter(
        purchased, Book.cost.isnot(None), Book.paid.isnot(None)).scalar() or 0

    # Free books would drag the typical price down without saying anything about
    # what a book costs, so they sit out of the average and median (as on the
    # statistics page).
    costs = sorted(row[0] for row in db.session.query(Book.cost).filter(
        purchased, Book.cost.isnot(None), Book.cost > 0).all())
    if costs:
        avg_cost = sum(costs) / len(costs)
        mid = len(costs) // 2
        median_cost = costs[mid] if len(costs) % 2 else (costs[mid - 1] + costs[mid]) / 2
    else:
        avg_cost = median_cost = None

    stats = {
        'year': year,
        'start': start,
        'end': end,
        'books_read': books_read,
        'series_read': series_read,
        'authors_read': authors_read,
        'pages_read': int(pages_read),
        'avg_days': avg_days,
        'per_month': per_month,
        'gender_breakdown': gender_breakdown,
        'books_purchased': books_purchased,
        'total_paid': float(total_paid),
        'total_saved': float(total_saved),
        'avg_cost': avg_cost,
        'median_cost': median_cost,
    }

    if include_books:
        stats['finished_books'] = (
            db.session.query(Book, Read.finish_date)
            .join(Read, Read.book_id == Book.id)
            .filter(finished)
            .order_by(Read.finish_date.desc())
            .all())

    return stats
