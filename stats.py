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


DAYS_PER_MONTH = 365.25 / 12

# How long after buying a book you started it, grouped. Each bucket is
# (upper bound in months, label); the last has no upper bound. Most books are
# started soon after buying, so the first month is split in two.
PURCHASE_GAP_BUCKETS = [
    (7 / DAYS_PER_MONTH, 'Within a week'),
    (1, '1–4 weeks'),
    (3, '1–3 months'),
    (6, '3–6 months'),
    (12, '6–12 months'),
    (24, '1–2 years'),
    (36, '2–3 years'),
    (60, '3–5 years'),
    (None, '5+ years'),
]

# A read finishing up to this long before the recorded purchase date still
# counts as reading that copy: dates entered a day or so out, or a sample read
# just before buying. A read finished earlier than that was of another copy
# (bought again later, in another format) and says nothing about this one.
PURCHASE_DATE_SLACK_DAYS = 7


def _day(value):
    return value.date() if hasattr(value, 'date') else value


def first_read_after_purchase(purchased, reads):
    """The (start, finish) of the first completed read of this copy, or None.

    reads holds (start_date, finish_date) pairs; either may be missing."""
    earliest = _day(purchased) - timedelta(days=PURCHASE_DATE_SLACK_DAYS)
    candidates = []
    for start, finish in reads:
        when = _day(finish or start)
        if when is not None and when >= earliest:
            candidates.append((when, start, finish))
    if not candidates:
        return None
    _, start, finish = min(candidates, key=lambda c: c[0])
    return start, finish


def purchase_to_read_stats(books, today=None):
    """Figures for how soon the books you buy get read.

    books maps a book's id to (date_purchased, [(start_date, finish_date)
    of each completed read]). Returns:
      same_year: {year: {'bought', 'read', 'percent'}} — of the books bought
          that year, how many were finished the same calendar year; from the
          year of the first recorded read on
      gap_buckets: {label: count} — months from purchase to starting the
          first read (its finish date when there's no start date)
      median_months: median of those gaps, or None
      read, unread: books read since purchase / not yet
    """
    today = _day(today or datetime.now())
    same_year = {}
    gaps = []
    unread = 0
    for purchased, reads in books.values():
        purchased = _day(purchased)
        year = same_year.setdefault(purchased.year, {'bought': 0, 'read': 0})
        year['bought'] += 1
        first = first_read_after_purchase(purchased, reads)
        if first is None:
            unread += 1
            continue
        start, finish = first
        if finish is not None and _day(finish).year == purchased.year:
            year['read'] += 1
        # Reads begun a day or two before the recorded purchase are "straight away"
        gaps.append(max(0, (_day(start or finish) - purchased).days) / DAYS_PER_MONTH)

    # Before the first recorded read, reading wasn't being tracked yet, so
    # those years would show 0% for want of records rather than of reading.
    finishes = [_day(finish) for _, reads in books.values() for _, finish in reads if finish]
    if finishes:
        first_year = min(finishes).year
        same_year = {y: v for y, v in same_year.items() if y >= first_year}
    for year in same_year.values():
        year['percent'] = round(100 * year['read'] / year['bought'])

    gap_buckets = {label: 0 for _, label in PURCHASE_GAP_BUCKETS}
    for months in gaps:
        label = next(label for limit, label in PURCHASE_GAP_BUCKETS
                     if limit is None or months < limit)
        gap_buckets[label] += 1

    median_months = None
    if gaps:
        ordered = sorted(gaps)
        mid = len(ordered) // 2
        median_months = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2

    return {
        'same_year': dict(sorted(same_year.items())),
        'current_year': today.year,
        'gap_buckets': gap_buckets,
        'median_months': median_months,
        'read': len(gaps),
        'unread': unread,
    }


def owned_purchases_with_reads():
    """Input for purchase_to_read_stats: every owned book with a purchase date.

    A book inside a bundle takes the bundle's purchase date (they're rarely
    given their own), and the bundle itself is left out in favour of the books
    in it, as those are what get read."""
    parent = aliased(Book)
    purchased = func.coalesce(Book.date_purchased, parent.date_purchased)
    rows = (db.session.query(Book.id, purchased, Read.start_date, Read.finish_date)
            .outerjoin(parent, parent.id == Book.parent_id)
            .outerjoin(Read, db.and_(Read.book_id == Book.id, Read.status == 'Completed'))
            .filter(Book.owned.is_(True), purchased.isnot(None), ~Book.bundle_children.any())
            .all())
    books = {}
    for book_id, bought, start, finish in rows:
        reads = books.setdefault(book_id, (bought, []))[1]
        if start is not None or finish is not None:
            reads.append((start, finish))
    return books
