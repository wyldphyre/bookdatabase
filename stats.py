"""Reading, collection and spending figures: the dashboard's year tab, and
each tab of the statistics page.

Kept out of the route modules so the same numbers can be reused elsewhere.
"""
from datetime import date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import aliased

from models import (db, Book, Author, AuthorGender, BookFormat, Read, Series, Tag,
                    RATING_LABELS, author_tags, book_authors, book_tags, series_tags)


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
    in it, as those are what get read.

    A pre-order couldn't be read until it came out, so its clock starts on its
    release date rather than the day it was paid for; one that isn't out yet
    is left out, rather than counted as bought and not read."""
    today = date.today()
    parent = aliased(Book)
    purchased = func.coalesce(Book.date_purchased, parent.date_purchased)
    rows = (db.session.query(Book.id, purchased, Book.release_date, Read.start_date, Read.finish_date)
            .outerjoin(parent, parent.id == Book.parent_id)
            .outerjoin(Read, db.and_(Read.book_id == Book.id, Read.status == 'Completed'))
            .filter(Book.owned.is_(True), purchased.isnot(None), ~Book.bundle_children.any())
            .all())
    books = {}
    for book_id, bought, released, start, finish in rows:
        if released is not None:
            if released > today:
                continue
            bought = max(_day(bought), released)
        reads = books.setdefault(book_id, (bought, []))[1]
        if start is not None or finish is not None:
            reads.append((start, finish))
    return books


# ---------------------------------------------------------------------------
# Statistics page, all time. One function per tab, so a tab only runs the
# queries it shows; the overview borrows a figure or chart from each of the
# others.
# ---------------------------------------------------------------------------

STATISTICS_TABS = [
    ('overview', 'Overview'),
    ('reading', 'Reading'),
    ('library', 'Library'),
    ('authors', 'Authors'),
    ('spending', 'Spending'),
]

# Book length bands: (label, the book list's ?pages= filter, low, high)
BOOK_LENGTH_BANDS = [
    ('Under 300 pages', 'lt300', None, 300),
    ('300–499 pages', '300to499', 300, 500),
    ('500+ pages', '500plus', 500, None),
]

# An alias and the author it points at are the same person
_person_id = func.coalesce(Author.alias_of_id, Author.id)


def _authors_by_id(rows):
    """Swap the person id leading each row for its Author, keeping the order."""
    ids = [row[0] for row in rows]
    authors = {a.id: a for a in Author.query.filter(Author.id.in_(ids))} if ids else {}
    return [(authors[row[0]], *row[1:]) for row in rows if row[0] in authors]


def _completed_reads():
    return Read.query.filter_by(status='Completed').count()


def _pages_read():
    """Pages of every completed read, re-reads included."""
    return db.session.query(func.sum(Book.page_count)).join(Read).filter(
        Read.status == 'Completed').scalar() or 0


def _total_spent():
    return db.session.query(func.sum(Book.paid)).scalar() or 0


def _read_by_year():
    year = func.strftime('%Y', Read.finish_date)
    rows = (db.session.query(year, func.count(Read.id))
            .filter(Read.status == 'Completed', Read.finish_date.isnot(None))
            .group_by(year).order_by(year).all())
    return {y: count for y, count in rows if y}


def _format_data():
    """Books you own in each format."""
    rows = (db.session.query(BookFormat.name, func.count(Book.id))
            .join(Book, Book.format_id == BookFormat.id)
            .filter(Book.owned.is_(True))
            .group_by(BookFormat.id, BookFormat.name)
            .order_by(func.count(Book.id).desc()).all())
    return {name: count for name, count in rows if count}


def _gender_data():
    """Authors by gender. Primary authors only: a pen name is the same person."""
    rows = (db.session.query(AuthorGender.name, func.count(Author.id))
            .join(Author, Author.gender_id == AuthorGender.id)
            .filter(Author.alias_of_id.is_(None))
            .group_by(AuthorGender.id, AuthorGender.name).all())
    data = {name: count for name, count in rows if count}
    not_set = Author.query.filter_by(gender_id=None, alias_of_id=None).count()
    if not_set:
        data['Not Set'] = not_set
    return data


def _spending_by_year():
    """Spent and saved per purchase year, as lists lined up with 'years'.
    Saved only counts books with both a cost and a price paid."""
    year = func.strftime('%Y', Book.date_purchased)
    rows = (db.session.query(year, func.sum(Book.paid), func.sum(Book.cost - Book.paid))
            .filter(Book.date_purchased.isnot(None))
            .group_by(year).order_by(year).all())
    rows = [(y, paid, saved) for y, paid, saved in rows if y and (paid is not None or saved is not None)]
    return {
        'years': [y for y, _, _ in rows],
        'spent': [round(float(paid or 0), 2) for _, paid, _ in rows],
        'saved': [round(float(saved or 0), 2) for _, _, saved in rows],
    }


def overview_stats():
    return {
        'total_books': Book.query.filter(Book.owned.is_(True)).count(),
        'total_authors': Author.query.filter_by(alias_of_id=None).count(),
        'total_series': Series.query.count(),
        'total_reads': _completed_reads(),
        'pages_read': _pages_read(),
        'total_spent': _total_spent(),
        'read_by_year': _read_by_year(),
        'format_data': _format_data(),
        'gender_data': _gender_data(),
        'spending': _spending_by_year(),
    }


def reading_stats():
    status_counts = dict(db.session.query(Read.status, func.count(Read.id)).group_by(Read.status).all())
    completed = status_counts.get('Completed', 0)
    abandoned = status_counts.get('Abandoned', 0)

    rating_rows = (db.session.query(Book.rating, func.count(Book.id))
                   .filter(Book.rating.isnot(None))
                   .group_by(Book.rating).order_by(Book.rating).all())
    most_common_rating = None
    if rating_rows:
        most_common_rating = RATING_LABELS.get(int(max(rating_rows, key=lambda r: r[1])[0]))

    with_dates = (db.session.query(Read.start_date, Read.finish_date)
                  .filter(Read.status == 'Completed',
                          Read.start_date.isnot(None), Read.finish_date.isnot(None)).all())
    avg_days = (sum((finish - start).days for start, finish in with_dates) / len(with_dates)
                if with_dates else 0)

    # Books read per month over the last twelve, empty months included
    now = datetime.now()
    twelve_months_ago = now - timedelta(days=365)
    month = func.strftime('%Y-%m', Read.finish_date)
    monthly_rows = dict(db.session.query(month, func.count(Read.id))
                        .filter(Read.status == 'Completed', Read.finish_date >= twelve_months_ago)
                        .group_by(month).all())
    monthly_data = {}
    current = twelve_months_ago.replace(day=1)
    while current <= now:
        key = current.strftime('%Y-%m')
        monthly_data[key] = monthly_rows.get(key, 0)
        current = (current.replace(year=current.year + 1, month=1) if current.month == 12
                   else current.replace(month=current.month + 1))

    most_read_books = (db.session.query(Book, func.count(Read.id))
                       .join(Read, Read.book_id == Book.id)
                       .filter(Read.status == 'Completed')
                       .group_by(Book.id)
                       .order_by(func.count(Read.id).desc())
                       .limit(10).all())

    return {
        'total_reads': completed,
        'pages_read': _pages_read(),
        'avg_days': round(avg_days, 1),
        'most_common_rating': most_common_rating,
        # Of the reads that have ended; ones still in progress count for neither
        'finished_percent': round(100 * completed / (completed + abandoned)) if completed + abandoned else None,
        'abandoned': abandoned,
        'monthly_data': monthly_data,
        'read_by_year': _read_by_year(),
        'rating_data': {RATING_LABELS.get(int(r), str(r)): count for r, count in rating_rows},
        'most_read_books': most_read_books,
        'purchase_reads': purchase_to_read_stats(owned_purchases_with_reads()),
    }


def library_stats():
    owned = Book.owned.is_(True)

    purchased = func.strftime('%Y', Book.date_purchased)
    added_rows = (db.session.query(purchased, func.count(Book.id))
                  .filter(owned, Book.date_purchased.isnot(None))
                  .group_by(purchased).order_by(purchased).all())

    book_lengths = []
    for label, filter_value, low, high in BOOK_LENGTH_BANDS:
        query = Book.query.filter(owned, Book.page_count.isnot(None))
        if low is not None:
            query = query.filter(Book.page_count >= low)
        if high is not None:
            query = query.filter(Book.page_count < high)
        count = query.count()
        if count:
            book_lengths.append({'label': label, 'filter': filter_value, 'count': count})

    # Tags by how often they're used, across books, authors and series
    usage = {}
    for table, column in ((book_tags, book_tags.c.book_id),
                          (author_tags, author_tags.c.author_id),
                          (series_tags, series_tags.c.series_id)):
        for tag_id, count in (db.session.query(table.c.tag_id, func.count(column))
                              .group_by(table.c.tag_id).all()):
            usage[tag_id] = usage.get(tag_id, 0) + count
    top_ids = sorted(usage, key=usage.get, reverse=True)[:15]
    names = dict(db.session.query(Tag.id, Tag.name).filter(Tag.id.in_(top_ids)).all()) if top_ids else {}

    return {
        'total_books': Book.query.filter(owned).count(),
        'total_series': Series.query.count(),
        'total_tags': Tag.query.count(),
        'format_data': _format_data(),
        'added_by_year': {y: count for y, count in added_rows if y},
        'book_lengths': book_lengths,
        'top_tag_data': {names[t]: usage[t] for t in top_ids if t in names},
    }


def author_stats():
    """Pen names count towards the author behind them throughout."""
    # Completed reads and the different books they cover. Reads are counted
    # distinct so a book with two of the same person's names on it counts once.
    read_rows = (db.session.query(_person_id,
                                  func.count(func.distinct(Read.id)),
                                  func.count(func.distinct(Book.id)))
                 .join(book_authors, Author.id == book_authors.c.author_id)
                 .join(Book, Book.id == book_authors.c.book_id)
                 .join(Read, Read.book_id == Book.id)
                 .filter(Read.status == 'Completed')
                 .group_by(_person_id)
                 .order_by(func.count(func.distinct(Read.id)).desc(),
                           func.count(func.distinct(Book.id)).desc())
                 .limit(10).all())

    # Pages of the books you own. A book inside a bundle is skipped: the
    # bundle's page count already covers it.
    owned_by_person = (db.session.query(_person_id.label('person_id'),
                                        Book.id.label('book_id'), Book.page_count)
                       .join(book_authors, Author.id == book_authors.c.author_id)
                       .join(Book, Book.id == book_authors.c.book_id)
                       .filter(Book.owned.is_(True), Book.parent_id.is_(None), Book.page_count > 0)
                       .distinct().subquery())
    page_rows = (db.session.query(owned_by_person.c.person_id,
                                  func.sum(owned_by_person.c.page_count),
                                  func.count(owned_by_person.c.book_id))
                 .group_by(owned_by_person.c.person_id)
                 .order_by(func.sum(owned_by_person.c.page_count).desc())
                 .limit(10).all())

    return {
        'total_authors': Author.query.filter_by(alias_of_id=None).count(),
        'gender_data': _gender_data(),
        'most_read_authors': _authors_by_id(read_rows),
        'top_authors_by_pages': _authors_by_id(page_rows),
    }


def _per_format_per_year(amount, *conditions):
    """{format: {year: total}} of an amount per purchase year, and the years."""
    year = func.strftime('%Y', Book.date_purchased)
    rows = (db.session.query(BookFormat.name, year, func.sum(amount))
            .join(Book, Book.format_id == BookFormat.id)
            .filter(Book.date_purchased.isnot(None), amount > 0, *conditions)
            .group_by(BookFormat.name, year).all())
    data, years = {}, set()
    for fmt, y, total in rows:
        if y and total:
            data.setdefault(fmt, {})[y] = round(float(total), 2)
            years.add(y)
    return dict(sorted(data.items())), sorted(years)


def spending_stats():
    total_saved = (db.session.query(func.sum(Book.cost - Book.paid))
                   .filter(Book.cost.isnot(None), Book.paid.isnot(None)).scalar() or 0)

    costs = sorted(c for (c,) in db.session.query(Book.cost).filter(Book.cost > 0))
    if costs:
        mid = len(costs) // 2
        avg_book_cost = sum(costs) / len(costs)
        median_book_cost = costs[mid] if len(costs) % 2 else (costs[mid - 1] + costs[mid]) / 2
    else:
        avg_book_cost = median_book_cost = None

    format_spend, format_years = _per_format_per_year(Book.paid)
    discount_by_format, discount_years = _per_format_per_year(Book.discounts)

    return {
        'total_spent': _total_spent(),
        'total_saved': total_saved,
        'avg_book_cost': avg_book_cost,
        'median_book_cost': median_book_cost,
        'spending': _spending_by_year(),
        'format_spend': format_spend,
        'format_years': format_years,
        'format_totals': {fmt: round(sum(by_year.values()), 2) for fmt, by_year in format_spend.items()},
        'discount_by_format': discount_by_format,
        'discount_years': discount_years,
    }


STATISTICS_TAB_DATA = {
    'overview': overview_stats,
    'reading': reading_stats,
    'library': library_stats,
    'authors': author_stats,
    'spending': spending_stats,
}
