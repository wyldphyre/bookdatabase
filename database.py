from models import db, BookFormat, AuthorGender


def run_migrations():
    """Apply schema migrations that db.create_all() won't handle on existing tables."""
    conn = db.engine.raw_connection()
    try:
        cursor = conn.cursor()
        # Add parent_id to book table if it doesn't exist
        cursor.execute("PRAGMA table_info(book)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'parent_id' not in columns:
            cursor.execute("ALTER TABLE book ADD COLUMN parent_id INTEGER REFERENCES book(id)")
            conn.commit()

        # Consolidate verbose format names: "Kindle eBook" → "Kindle", "Kobo eBook" → "Kobo"
        for old_name, new_name in [('Kindle eBook', 'Kindle'), ('Kobo eBook', 'Kobo')]:
            cursor.execute("SELECT id FROM book_format WHERE name = ?", (old_name,))
            old_row = cursor.fetchone()
            if old_row:
                old_id = old_row[0]
                cursor.execute("SELECT id FROM book_format WHERE name = ?", (new_name,))
                new_row = cursor.fetchone()
                if new_row:
                    new_id = new_row[0]
                    cursor.execute("UPDATE book SET format_id = ? WHERE format_id = ?", (new_id, old_id))
                    cursor.execute("DELETE FROM book_format WHERE id = ?", (old_id,))
                else:
                    cursor.execute("UPDATE book_format SET name = ? WHERE id = ?", (new_name, old_id))
                conn.commit()

        # Rename "Apple eBook" → "Apple"
        cursor.execute("UPDATE book_format SET name = 'Apple' WHERE name = 'Apple eBook'")
        conn.commit()

        # Migrate fractional ratings to integer 1-5 scale
        cursor.execute("SELECT id, rating FROM book WHERE rating IS NOT NULL")
        rows = cursor.fetchall()
        for row_id, rating in rows:
            new_rating = max(1, min(5, round(float(rating))))
            if float(new_rating) != float(rating):
                cursor.execute("UPDATE book SET rating = ? WHERE id = ?", (float(new_rating), row_id))
        conn.commit()
    finally:
        conn.close()


def init_db(app):
    """Initialize the database and create tables."""
    with app.app_context():
        db.create_all()
        run_migrations()
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
