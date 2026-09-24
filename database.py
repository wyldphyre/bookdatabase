from sqlalchemy import event
from sqlalchemy.engine import Engine

from models import db, BookFormat, AuthorGender

CURRENT_SCHEMA_VERSION = 13


@event.listens_for(Engine, 'connect')
def _use_unicode_lower(dbapi_connection, connection_record):
    """Make SQL lower() fold non-ASCII letters, as Python's str.lower() does.

    SQLite's built-in lower() only touches A-Z, so a case-insensitive lookup
    written as `db.func.lower(Tag.name) == name.lower()` silently fails to
    match anything with an uppercase accented letter: the column side leaves
    'Éducation' alone while the Python side produces 'éducation'. The row is
    then judged absent and re-inserted, which the UNIQUE constraint on the
    name rejects — a 500 from the book page's Fetch tags, or a failed book in
    a scan.

    That comparison appears at fifteen call sites across tags, series, authors
    and formats, so this replaces the function they all rely on rather than
    correcting each one and waiting for the sixteenth to be written. An
    application-defined function takes precedence over SQLite's built-in.
    Declared deterministic so it stays usable wherever SQLite requires that.
    """
    if not hasattr(dbapi_connection, 'create_function'):
        return                       # not SQLite; nothing to correct
    try:
        dbapi_connection.create_function(
            'lower', 1, lambda value: value.lower() if value is not None else None,
            deterministic=True)
    except TypeError:                # deterministic= needs Python 3.8+/SQLite 3.8.3+
        dbapi_connection.create_function(
            'lower', 1, lambda value: value.lower() if value is not None else None)


def _get_schema_version(cursor):
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
    )
    cursor.execute("SELECT version FROM schema_version")
    row = cursor.fetchone()
    return row[0] if row else 0


def _set_schema_version(cursor, conn, version):
    cursor.execute("DELETE FROM schema_version")
    cursor.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    conn.commit()


def _migrate_data_baseline_done(cursor):
    """v11 data step: any series that already has releases has been baselined."""
    cursor.execute("UPDATE series SET baseline_done = 1 WHERE id IN "
                   "(SELECT DISTINCT series_id FROM series_release)")


def _migrate_data_match_keys(cursor):
    """v12 data step: match_key now normalises "Title: Book 2" and "Title #2"
    to the same key, so keys recorded by the old rule no longer match what a
    check computes. Left alone, every affected book would read as newly
    released and be announced a second time."""
    from series_monitor import match_key
    cursor.execute("SELECT id, title FROM series_release")
    for release_id, title in cursor.fetchall():
        cursor.execute("UPDATE series_release SET match_key = ? WHERE id = ?",
                       (match_key(title), release_id))


def _migrate_data_genre_source(cursor):
    """v13 data step: credit existing tags to Goodreads.

    Goodreads was the only genre source until v1.5.0, so anything already
    tagged got those tags from it. Left null they would look never-scanned,
    and the first "not yet tagged from Goodreads" run would sweep the entire
    library instead of the handful a fallback tagged."""
    cursor.execute("UPDATE book SET genre_source = 'goodreads' WHERE genre_source IS NULL "
                   "AND id IN (SELECT DISTINCT book_id FROM book_tags)")


# Every entry here rewrites *data* rather than schema, so it has to be re-run
# against rows arriving from an older export as well as against this
# instance's own rows. Keyed by the version that introduced the step.
# Schema-only migrations don't belong here: an import lands in tables this
# instance already created at the current version.
DATA_MIGRATIONS = {
    11: _migrate_data_baseline_done,
    12: _migrate_data_match_keys,
    13: _migrate_data_genre_source,
}


def migrate_imported_data(from_version):
    """Bring rows from an older export up to what the current code expects.

    run_migrations() is keyed on this instance's own schema_version, which is
    already current by the time an import runs, so none of its steps fire for
    the rows the import just inserted. The tables are this instance's and need
    no schema work; what needs redoing is every step that rewrote data.

    Runs on the caller's open transaction, so it commits and rolls back with
    the import itself. Returns the versions applied.
    """
    if from_version >= CURRENT_SCHEMA_VERSION:
        return []
    cursor = db.session.connection().connection.cursor()
    applied = []
    for version in sorted(DATA_MIGRATIONS):
        if from_version < version:
            DATA_MIGRATIONS[version](cursor)
            applied.append(version)
    return applied


def run_migrations():
    """Apply schema migrations that db.create_all() won't handle on existing tables."""
    conn = db.engine.raw_connection()
    try:
        cursor = conn.cursor()
        version = _get_schema_version(cursor)

        if version < 1:
            # Add parent_id to book table
            cursor.execute("PRAGMA table_info(book)")
            columns = [row[1] for row in cursor.fetchall()]
            if 'parent_id' not in columns:
                cursor.execute("ALTER TABLE book ADD COLUMN parent_id INTEGER REFERENCES book(id)")
            conn.commit()

        if version < 2:
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

        if version < 3:
            # Rename "Apple eBook" → "Apple"
            cursor.execute("UPDATE book_format SET name = 'Apple' WHERE name = 'Apple eBook'")
            conn.commit()

        if version < 4:
            # Migrate fractional ratings to integer 1-5 scale
            cursor.execute("SELECT id, rating FROM book WHERE rating IS NOT NULL")
            rows = cursor.fetchall()
            for row_id, rating in rows:
                new_rating = max(1, min(5, round(float(rating))))
                if float(new_rating) != float(rating):
                    cursor.execute("UPDATE book SET rating = ? WHERE id = ?", (float(new_rating), row_id))
            conn.commit()

        if version < 5:
            cursor.execute("PRAGMA table_info(book)")
            columns = [row[1] for row in cursor.fetchall()]
            if 'goodreads_url' not in columns:
                cursor.execute("ALTER TABLE book ADD COLUMN goodreads_url VARCHAR(500)")
            conn.commit()

        if version < 6:
            cursor.execute("PRAGMA table_info(book)")
            columns = [row[1] for row in cursor.fetchall()]
            if 'amazon_url' not in columns:
                cursor.execute("ALTER TABLE book ADD COLUMN amazon_url VARCHAR(500)")
            conn.commit()

        if version < 7:
            # Add indexes on frequently filtered/joined foreign keys and columns
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_book_date_added ON book(date_added)")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_book_series_id ON book(series_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_book_format_id ON book(format_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_book_parent_id ON book(parent_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_author_alias_of_id ON author(alias_of_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_read_book_id ON read(book_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_read_status ON read(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_reading_queue_book_id ON reading_queue(book_id)")
            conn.commit()

        if version < 8:
            # ix_author_alias_of_id had terrible selectivity (~97% of authors have
            # alias_of_id IS NULL, which is the common query direction), causing SQLite
            # to pick a worse plan than a plain scan. Drop it and index name instead,
            # which is what author listing/search actually orders by.
            cursor.execute("DROP INDEX IF EXISTS ix_author_alias_of_id")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_author_name ON author(name)")
            conn.commit()

        if version < 9:
            cursor.execute("PRAGMA table_info(author)")
            columns = [row[1] for row in cursor.fetchall()]
            if 'notes' not in columns:
                cursor.execute("ALTER TABLE author ADD COLUMN notes TEXT")
            conn.commit()

        if version < 10:
            # Series monitoring. The series_release table itself is created by
            # db.create_all(); only added columns need handling here.
            cursor.execute("PRAGMA table_info(series)")
            columns = [row[1] for row in cursor.fetchall()]
            if 'monitored' not in columns:
                cursor.execute("ALTER TABLE series ADD COLUMN monitored BOOLEAN NOT NULL DEFAULT 0")
            if 'last_checked_at' not in columns:
                cursor.execute("ALTER TABLE series ADD COLUMN last_checked_at DATETIME")
            if 'last_check_error' not in columns:
                cursor.execute("ALTER TABLE series ADD COLUMN last_check_error VARCHAR(300)")
            conn.commit()

        if version < 11:
            cursor.execute("PRAGMA table_info(series)")
            columns = [row[1] for row in cursor.fetchall()]
            if 'baseline_done' not in columns:
                cursor.execute("ALTER TABLE series ADD COLUMN baseline_done BOOLEAN NOT NULL DEFAULT 0")
                _migrate_data_baseline_done(cursor)
            conn.commit()

        if version < 12:
            _migrate_data_match_keys(cursor)
            conn.commit()

        if version < 13:
            cursor.execute("PRAGMA table_info(book)")
            columns = [row[1] for row in cursor.fetchall()]
            if 'genre_source' not in columns:
                cursor.execute("ALTER TABLE book ADD COLUMN genre_source VARCHAR(20)")
            _migrate_data_genre_source(cursor)
            conn.commit()

        if version < CURRENT_SCHEMA_VERSION:
            _set_schema_version(cursor, conn, CURRENT_SCHEMA_VERSION)
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

    # Seed author genders ('Male and Female' is for two people writing under one name)
    genders = ['Female', 'Male', 'Male and Female', 'Nonbinary', 'Unknown']
    for gender_name in genders:
        if not AuthorGender.query.filter_by(name=gender_name).first():
            db.session.add(AuthorGender(name=gender_name))

    db.session.commit()
