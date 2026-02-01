from models import db, BookFormat, AuthorGender


def init_db(app):
    """Initialize the database and create tables."""
    with app.app_context():
        db.create_all()
        seed_data()


def seed_data():
    """Add initial seed data for formats and genders."""
    # Seed book formats
    formats = ['Kindle', 'Kobo', 'ePub', 'Hardcover', 'Paperback', 'Comic Archive', 'Audiobook', 'PDF']
    for format_name in formats:
        if not BookFormat.query.filter_by(name=format_name).first():
            db.session.add(BookFormat(name=format_name))

    # Seed author genders
    genders = ['Female', 'Male', 'Nonbinary', 'Unknown']
    for gender_name in genders:
        if not AuthorGender.query.filter_by(name=gender_name).first():
            db.session.add(AuthorGender(name=gender_name))

    db.session.commit()
