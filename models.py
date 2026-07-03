import re
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

RATING_LABELS = {
    1: '🚫 Abandoned',
    2: '☹️ Didn\'t like',
    3: '😑 Ok',
    4: '🙂 Liked',
    5: '🤩 Really liked',
}

# Association table for Book-Author many-to-many relationship
book_authors = db.Table('book_authors',
    db.Column('book_id', db.Integer, db.ForeignKey('book.id'), primary_key=True),
    db.Column('author_id', db.Integer, db.ForeignKey('author.id'), primary_key=True)
)

# Association tables for Tag many-to-many relationships
book_tags = db.Table('book_tags',
    db.Column('book_id', db.Integer, db.ForeignKey('book.id'), primary_key=True),
    db.Column('tag_id', db.Integer, db.ForeignKey('tag.id'), primary_key=True)
)

author_tags = db.Table('author_tags',
    db.Column('author_id', db.Integer, db.ForeignKey('author.id'), primary_key=True),
    db.Column('tag_id', db.Integer, db.ForeignKey('tag.id'), primary_key=True)
)

series_tags = db.Table('series_tags',
    db.Column('series_id', db.Integer, db.ForeignKey('series.id'), primary_key=True),
    db.Column('tag_id', db.Integer, db.ForeignKey('tag.id'), primary_key=True)
)


class Tag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)

    books = db.relationship('Book', secondary=book_tags, back_populates='tags')
    authors = db.relationship('Author', secondary=author_tags, back_populates='tags')
    series = db.relationship('Series', secondary=series_tags, back_populates='tags')


class BookFormat(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)

    books = db.relationship('Book', backref='format', lazy=True)


class AuthorGender(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)

    authors = db.relationship('Author', backref='gender', lazy=True)


class Series(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    number_in_series = db.Column(db.Integer)
    goodreads_url = db.Column(db.String(500))
    amazon_url = db.Column(db.String(500))
    storygraph_url = db.Column(db.String(500))

    books = db.relationship('Book', backref='series', lazy=True, order_by='Book.series_number')
    tags = db.relationship('Tag', secondary=series_tags, back_populates='series')


class Author(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, index=True)
    pronouns = db.Column(db.String(50))
    gender_id = db.Column(db.Integer, db.ForeignKey('author_gender.id'))
    goodreads_url = db.Column(db.String(500))
    amazon_url = db.Column(db.String(500))
    storygraph_url = db.Column(db.String(500))
    website = db.Column(db.String(500))
    alias_of_id = db.Column(db.Integer, db.ForeignKey('author.id'))

    # Self-referential relationship for aliases
    alias_of = db.relationship('Author', remote_side=[id], backref='aliases')

    books = db.relationship('Book', secondary=book_authors, back_populates='authors', order_by='Book.title')
    tags = db.relationship('Tag', secondary=author_tags, back_populates='authors')


class Book(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300), nullable=False)
    subtitle = db.Column(db.String(300))
    date_purchased = db.Column(db.DateTime)
    date_added = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)
    description = db.Column(db.Text)
    page_count = db.Column(db.Integer)
    series_id = db.Column(db.Integer, db.ForeignKey('series.id'), index=True)
    series_number = db.Column(db.Float)
    format_id = db.Column(db.Integer, db.ForeignKey('book_format.id'), nullable=False, index=True)
    cost = db.Column(db.Float)
    paid = db.Column(db.Float)
    discounts = db.Column(db.Float)
    is_book_bundle = db.Column(db.Boolean, default=False)
    bundled_books = db.Column(db.String(100))
    cover_image = db.Column(db.String(300))
    rating = db.Column(db.Float)
    comment = db.Column(db.Text)
    goodreads_url = db.Column(db.String(500))
    amazon_url = db.Column(db.String(500))
    parent_id = db.Column(db.Integer, db.ForeignKey('book.id'), index=True)

    authors = db.relationship('Author', secondary=book_authors, back_populates='books')
    tags = db.relationship('Tag', secondary=book_tags, back_populates='books')
    reads = db.relationship('Read', backref='book', lazy=True, order_by='Read.start_date.desc()')

    # Self-referential relationship for bundle parent/children
    parent = db.relationship('Book', remote_side='Book.id', foreign_keys=[parent_id], backref='bundle_children')

    @property
    def saved(self):
        cost = self.cost or 0
        paid = self.paid or 0
        return cost - paid

    @property
    def bundle_expected_count(self):
        """Parse bundled_books range string (e.g. '1-3' or '1-3, 5-7') into expected child count.
        Returns None if the field is absent or unparseable."""
        if not self.bundled_books:
            return None
        total = 0
        for part in self.bundled_books.split(','):
            part = part.strip()
            m = re.match(r'^(\d+)-(\d+)$', part)
            if m:
                lo, hi = int(m.group(1)), int(m.group(2))
                if hi >= lo:
                    total += hi - lo + 1
                else:
                    return None
            elif re.match(r'^\d+$', part):
                total += 1
            else:
                return None
        return total if total > 0 else None

    @property
    def is_completed(self):
        """True if this book (or all children of a bundle) has been completed."""
        if self.bundle_children:
            return len(self.bundle_children) > 0 and all(
                any(r.status == 'Completed' for r in child.reads)
                for child in self.bundle_children
            )
        return any(r.status == 'Completed' for r in self.reads)

    @property
    def display_rating(self):
        """Return the rating to display: own rating if set, otherwise average of rated child books."""
        if self.rating is not None:
            return self.rating
        if self.bundle_children:
            rated = [c.rating for c in self.bundle_children if c.rating is not None]
            if rated:
                avg = sum(rated) / len(rated)
                return float(round(avg))  # round to nearest integer
        return None

    @property
    def rating_label(self):
        """Return the emoji label for the display rating."""
        r = self.display_rating
        if r is None:
            return None
        return RATING_LABELS.get(int(round(r)))

    @property
    def active_read(self):
        # If reads are already loaded in memory (eager-loaded), filter in Python
        if 'reads' in self.__dict__:
            for read in self.reads:
                if read.status == 'Reading':
                    return read
            return None
        # Otherwise do a targeted query to avoid loading the full reads collection
        from sqlalchemy.orm import object_session
        session = object_session(self)
        if session is not None:
            return session.query(Read).filter_by(book_id=self.id, status='Reading').first()
        for read in self.reads:
            if read.status == 'Reading':
                return read
        return None

    @property
    def author_names(self):
        return ', '.join([a.name for a in self.authors])


class Read(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    book_id = db.Column(db.Integer, db.ForeignKey('book.id'), nullable=False, index=True)
    start_date = db.Column(db.DateTime)
    finish_date = db.Column(db.DateTime)
    status = db.Column(db.String(20), nullable=False, default='Reading', index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)


class ReadingQueue(db.Model):
    __tablename__ = 'reading_queue'
    id = db.Column(db.Integer, primary_key=True)
    position = db.Column(db.Integer, nullable=False)
    book_id = db.Column(db.Integer, db.ForeignKey('book.id'), nullable=True, index=True)
    external_title = db.Column(db.String(300), nullable=True)
    external_author = db.Column(db.String(300), nullable=True)
    external_url = db.Column(db.String(500), nullable=True)
    added_at = db.Column(db.DateTime, nullable=False, default=datetime.now)

    book = db.relationship('Book', backref='queue_items')

    @property
    def display_title(self):
        return self.book.title if self.book else self.external_title

    @property
    def display_author(self):
        return self.book.author_names if self.book else self.external_author


class AuthorInfoSuggestion(db.Model):
    """A gender/pronoun suggestion found by the author info scan, awaiting
    review on the System page."""
    __tablename__ = 'author_info_suggestion'
    id = db.Column(db.Integer, primary_key=True)
    author_id = db.Column(db.Integer, db.ForeignKey('author.id'), nullable=False, unique=True)
    suggested_gender_id = db.Column(db.Integer, db.ForeignKey('author_gender.id'))
    suggested_pronouns = db.Column(db.String(50))
    evidence = db.Column(db.Text)
    source_url = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)

    author = db.relationship('Author', backref=db.backref('info_suggestion', uselist=False))
    suggested_gender = db.relationship('AuthorGender')


class PriceWatch(db.Model):
    __tablename__ = 'price_watch'
    id = db.Column(db.Integer, primary_key=True)
    amazon_url = db.Column(db.String(500), nullable=False, unique=True)
    title = db.Column(db.String(300))
    cover_url = db.Column(db.String(500))
    initial_price = db.Column(db.Float)
    current_price = db.Column(db.Float)
    currency = db.Column(db.String(10))
    last_checked_at = db.Column(db.DateTime)
    last_error = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)

    @property
    def dropped(self):
        return self.current_price is not None and self.initial_price is not None and self.current_price < self.initial_price
