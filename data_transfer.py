"""Full-library export/import as a zip archive.

The zip contains:
  manifest.json  - format marker, schema/app versions, row counts
  data.json      - every table serialized as JSON, keyed by table name
  covers/        - the cover image files referenced by book.cover_image

Import is wipe-and-replace: all rows are deleted and re-inserted with their
original IDs, and the uploads folder contents are replaced by the archive's
covers. The database is backed up to books_pre_import.db first.
"""
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime

from sqlalchemy import DateTime

from models import (db, Tag, BookFormat, AuthorGender, Series, Author, Book, Read,
                    ReadingQueue, AuthorInfoSuggestion, PriceWatch,
                    book_authors, book_tags, author_tags, series_tags)
from database import CURRENT_SCHEMA_VERSION

EXPORT_FORMAT = 'bookdb-export'
PRE_IMPORT_BACKUP_NAME = 'books_pre_import.db'

# Every table that holds user data, in FK-safe insert order (referenced tables
# first). Deletes run in reverse. New tables must be added here or they will
# silently be left out of exports.
EXPORT_TABLES = [
    ('book_format', BookFormat.__table__),
    ('author_gender', AuthorGender.__table__),
    ('tag', Tag.__table__),
    ('series', Series.__table__),
    ('author', Author.__table__),
    ('book', Book.__table__),
    ('read', Read.__table__),
    ('reading_queue', ReadingQueue.__table__),
    ('author_info_suggestion', AuthorInfoSuggestion.__table__),
    ('price_watch', PriceWatch.__table__),
    ('book_authors', book_authors),
    ('book_tags', book_tags),
    ('author_tags', author_tags),
    ('series_tags', series_tags),
]


class ImportValidationError(Exception):
    """The uploaded file is not a usable export archive."""


def _serialize_tables():
    data = {}
    for name, table in EXPORT_TABLES:
        rows = db.session.execute(table.select()).mappings().all()
        data[name] = [
            {key: (value.isoformat() if isinstance(value, datetime) else value)
             for key, value in row.items()}
            for row in rows
        ]
    return data


def _referenced_covers(book_rows, upload_folder):
    """Cover filenames referenced by books that actually exist on disk."""
    covers = []
    seen = set()
    for row in book_rows:
        filename = row.get('cover_image')
        if filename and filename not in seen and os.path.isfile(os.path.join(upload_folder, filename)):
            seen.add(filename)
            covers.append(filename)
    return covers


def build_export_zip(upload_folder, app_version):
    """Write the export zip to a temp file; returns (path, manifest)."""
    data = _serialize_tables()
    covers = _referenced_covers(data['book'], upload_folder)
    manifest = {
        'format': EXPORT_FORMAT,
        'schema_version': CURRENT_SCHEMA_VERSION,
        'app_version': app_version,
        'exported_at': datetime.now().isoformat(timespec='seconds'),
        'counts': {name: len(rows) for name, rows in data.items()},
        'cover_count': len(covers),
    }
    tmp = tempfile.NamedTemporaryFile(suffix='.zip', delete=False)
    try:
        with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('manifest.json', json.dumps(manifest, indent=2, ensure_ascii=False))
            zf.writestr('data.json', json.dumps(data, indent=2, ensure_ascii=False))
            for filename in covers:
                # Covers are already-compressed images; deflating them wastes CPU
                zf.write(os.path.join(upload_folder, filename),
                         'covers/' + filename, compress_type=zipfile.ZIP_STORED)
        tmp.close()
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise
    return tmp.name, manifest


def _load_manifest(zf):
    try:
        manifest = json.loads(zf.read('manifest.json'))
    except KeyError:
        raise ImportValidationError('Not a Book Database export: no manifest.json in the zip.')
    except ValueError:
        raise ImportValidationError('The manifest.json in the zip is not valid JSON.')
    if manifest.get('format') != EXPORT_FORMAT:
        raise ImportValidationError('Not a Book Database export: unrecognized manifest format.')
    if manifest.get('schema_version', 0) > CURRENT_SCHEMA_VERSION:
        raise ImportValidationError(
            f"This export came from a newer version of the app "
            f"(schema v{manifest.get('schema_version')}, this instance is v{CURRENT_SCHEMA_VERSION}). "
            f"Update this instance first, then import.")
    return manifest


def _cover_members(zf):
    """Validated covers/* members; rejects anything that could escape the uploads folder."""
    members = []
    for info in zf.infolist():
        name = info.filename
        if not name.startswith('covers/') or name == 'covers/':
            continue
        basename = name[len('covers/'):]
        if info.is_dir():
            continue
        if '/' in basename or '\\' in basename or basename in ('.', '..') or os.path.isabs(basename):
            raise ImportValidationError(f'Unsafe cover path in the zip: {name}')
        members.append((name, basename))
    return members


def validate_import_zip(zip_path):
    """Check the archive is a usable export without touching any data. Returns its manifest."""
    if not zipfile.is_zipfile(zip_path):
        raise ImportValidationError('The uploaded file is not a zip archive.')
    with zipfile.ZipFile(zip_path) as zf:
        manifest = _load_manifest(zf)
        try:
            json.loads(zf.read('data.json'))
        except KeyError:
            raise ImportValidationError('Not a Book Database export: no data.json in the zip.')
        except ValueError:
            raise ImportValidationError('The data.json in the zip is not valid JSON.')
        _cover_members(zf)
    return manifest


def _deserialize_rows(rows, table):
    """Convert exported JSON rows back to insertable dicts.

    Only known columns are kept, so an export carrying columns this instance
    doesn't have imports cleanly. Columns missing from the export (older
    exports) are left out entirely so column defaults apply.
    """
    columns = {col.name: col for col in table.columns}
    out = []
    for row in rows:
        clean = {}
        for key, value in row.items():
            col = columns.get(key)
            if col is None:
                continue
            if value is not None and isinstance(col.type, DateTime):
                value = datetime.fromisoformat(value)
            clean[key] = value
        out.append(clean)
    return out


def _backup_database():
    """Snapshot the live SQLite file via the sqlite backup API (safe while open)."""
    db_path = db.engine.url.database
    backup_path = os.path.join(os.path.dirname(db_path), PRE_IMPORT_BACKUP_NAME)
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(backup_path)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    return backup_path


def apply_import(zip_path, upload_folder):
    """Replace all data and covers with the archive's contents.

    Covers are fully extracted and validated before the database is touched,
    and the row replacement is a single transaction, so a bad archive or a
    failure mid-import leaves the database unchanged.
    """
    with zipfile.ZipFile(zip_path) as zf:
        _load_manifest(zf)
        data = json.loads(zf.read('data.json'))
        covers = _cover_members(zf)

        # Stage covers next to the uploads folder so the final moves are cheap renames
        staging = tempfile.mkdtemp(prefix='import-covers-', dir=os.path.dirname(upload_folder))
        try:
            for arcname, basename in covers:
                with zf.open(arcname) as src, open(os.path.join(staging, basename), 'wb') as dst:
                    shutil.copyfileobj(src, dst)

            _backup_database()

            for name, table in reversed(EXPORT_TABLES):
                db.session.execute(table.delete())
            for name, table in EXPORT_TABLES:
                rows = _deserialize_rows(data.get(name, []), table)
                if rows:
                    db.session.execute(table.insert(), rows)
            db.session.commit()

            for entry in os.listdir(upload_folder):
                if entry.startswith('.'):
                    continue
                path = os.path.join(upload_folder, entry)
                if os.path.isfile(path):
                    os.unlink(path)
            for entry in os.listdir(staging):
                shutil.move(os.path.join(staging, entry), os.path.join(upload_folder, entry))
        except Exception:
            db.session.rollback()
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    return {
        'books': len(data.get('book', [])),
        'authors': len(data.get('author', [])),
        'series': len(data.get('series', [])),
        'reads': len(data.get('read', [])),
        'covers': len(covers),
    }
