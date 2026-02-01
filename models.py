from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# Association table for Book-Author many-to-many relationship
book_authors = db.Table('book_authors',
    db.Column('book_id', db.Integer, db.ForeignKey('book.id'), primary_key=True),
    db.Column('author_id', db.Integer, db.ForeignKey('author.id'), primary_key=True)
)


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


class Author(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    pronouns = db.Column(db.String(50))
    gender_id = db.Column(db.Integer, db.ForeignKey('author_gender.id'))
    goodreads_url = db.Column(db.String(500))
    amazon_url = db.Column(db.String(500))
    storygraph_url = db.Column(db.String(500))
    website = db.Column(db.String(500))
    alias_of_id = db.Column(db.Integer, db.ForeignKey('author.id'))

    # Self-referential relationship for aliases
    alias_of = db.relationship('Author', remote_side=[id], backref='aliases')

    books = db.relationship('Book', secondary=book_authors, back_populates='authors')


class Book(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300), nullable=False)
    subtitle = db.Column(db.String(300))
    date_purchased = db.Column(db.DateTime)
    date_added = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    description = db.Column(db.Text)
    page_count = db.Column(db.Integer)
    series_id = db.Column(db.Integer, db.ForeignKey('series.id'))
    series_number = db.Column(db.Float)
    format_id = db.Column(db.Integer, db.ForeignKey('book_format.id'), nullable=False)
    cost = db.Column(db.Float)
    paid = db.Column(db.Float)
    discounts = db.Column(db.Float)
    is_book_bundle = db.Column(db.Boolean, default=False)
    bundled_books = db.Column(db.String(100))
    cover_image = db.Column(db.String(300))
    rating = db.Column(db.Float)
    comment = db.Column(db.Text)

    authors = db.relationship('Author', secondary=book_authors, back_populates='books')
    reads = db.relationship('Read', backref='book', lazy=True, order_by='Read.start_date.desc()')

    @property
    def saved(self):
        cost = self.cost or 0
        discounts = self.discounts or 0
        paid = self.paid or 0
        return cost - discounts - paid

    @property
    def active_read(self):
        for read in self.reads:
            if read.status == 'Reading':
                return read
        return None

    @property
    def author_names(self):
        return ', '.join([a.name for a in self.authors])


class Read(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    book_id = db.Column(db.Integer, db.ForeignKey('book.id'), nullable=False)
    start_date = db.Column(db.DateTime)
    finish_date = db.Column(db.DateTime)
    status = db.Column(db.String(20), nullable=False, default='Reading')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
