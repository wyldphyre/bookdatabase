#!/usr/bin/env python3
"""
Import data from notion_data.json into the BookDatabase SQLite database.

Usage:
    python notion_import.py [--clear]

Options:
    --clear     Clear existing data before importing (WARNING: destructive!)

Reads: notion_data.json
Writes to: instance/books.db
"""

import json
import os
import sys
import requests
from datetime import datetime
from urllib.parse import urlparse

# Add the app directory to the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, db
from models import Book, Author, Series, Read, BookFormat, AuthorGender, book_authors
from database import init_db


def parse_date(date_str):
    """Parse ISO date string to datetime."""
    if not date_str:
        return None
    try:
        # Handle both date and datetime formats
        if 'T' in date_str:
            return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        return datetime.strptime(date_str, '%Y-%m-%d')
    except (ValueError, TypeError):
        return None


def clear_uploads_folder():
    """Clear all files from the uploads folder except .gitkeep."""
    upload_folder = app.config['UPLOAD_FOLDER']
    if os.path.exists(upload_folder):
        for filename in os.listdir(upload_folder):
            if filename != '.gitkeep':
                file_path = os.path.join(upload_folder, filename)
                try:
                    os.remove(file_path)
                except Exception as e:
                    print(f"      Warning: Could not delete {filename}: {e}")


def download_cover_image(url, book_title):
    """Download cover image and save to uploads folder."""
    if not url:
        return None

    try:
        # Create a safe filename from book title
        safe_title = "".join(c for c in book_title if c.isalnum() or c in (' ', '-', '_')).strip()
        safe_title = safe_title[:50]  # Limit length

        # Determine extension from URL or default to jpg
        parsed = urlparse(url)
        path = parsed.path.lower()
        if '.png' in path:
            ext = '.png'
        elif '.webp' in path:
            ext = '.webp'
        elif '.gif' in path:
            ext = '.gif'
        else:
            ext = '.jpg'

        filename = f"{safe_title}_{int(datetime.now().timestamp())}{ext}"

        # Download the image
        response = requests.get(url, timeout=30)
        response.raise_for_status()

        # Save to uploads folder
        upload_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        with open(upload_path, 'wb') as f:
            f.write(response.content)

        print(f"      Downloaded cover: {filename}")
        return filename

    except Exception as e:
        print(f"      Warning: Could not download cover for '{book_title}': {e}")
        return None


def import_data(clear_existing=False):
    """Import data from notion_data.json into the database."""

    # Load JSON data
    input_file = 'notion_data.json'
    if not os.path.exists(input_file):
        print(f"Error: {input_file} not found. Run notion_export.py first.")
        sys.exit(1)

    with open(input_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    print(f"Importing data from {input_file}")
    print(f"  Exported at: {data['exported_at']}")

    # Initialize database (creates tables if they don't exist)
    init_db(app)

    with app.app_context():
        if clear_existing:
            print("\n  Clearing existing data...")
            Read.query.delete()
            # Clear the book_authors association table
            db.session.execute(book_authors.delete())
            Book.query.delete()
            Author.query.delete()
            Series.query.delete()
            # Don't delete formats and genders, just update them
            db.session.commit()
            # Clear uploaded cover images
            print("  Clearing uploaded covers...")
            clear_uploads_folder()

        # Build mapping dictionaries (Notion ID -> Local ID)
        gender_map = {}  # notion_id -> local AuthorGender
        format_map = {}  # notion_id -> local BookFormat
        author_map = {}  # notion_id -> local Author
        series_map = {}  # notion_id -> local Series

        # Import Genders
        print(f"\n  Importing {len(data['genders'])} genders...")
        for g in data['genders']:
            existing = AuthorGender.query.filter_by(name=g['name']).first()
            if existing:
                gender_map[g['notion_id']] = existing
            else:
                gender = AuthorGender(name=g['name'])
                db.session.add(gender)
                db.session.flush()
                gender_map[g['notion_id']] = gender
        db.session.commit()
        print(f"    Done ({len(gender_map)} mapped)")

        # Import Formats
        print(f"\n  Importing {len(data['formats'])} formats...")
        for f in data['formats']:
            existing = BookFormat.query.filter_by(name=f['name']).first()
            if existing:
                format_map[f['notion_id']] = existing
            else:
                fmt = BookFormat(name=f['name'])
                db.session.add(fmt)
                db.session.flush()
                format_map[f['notion_id']] = fmt
        db.session.commit()
        print(f"    Done ({len(format_map)} mapped)")

        # Import Authors (first pass - without alias relationships)
        print(f"\n  Importing {len(data['authors'])} authors...")
        for a in data['authors']:
            # Check if author already exists by name
            existing = Author.query.filter_by(name=a['name']).first()
            if existing and not clear_existing:
                author_map[a['notion_id']] = existing
                continue

            gender = gender_map.get(a['gender_notion_id']) if a['gender_notion_id'] else None

            author = Author(
                name=a['name'],
                pronouns=a['pronouns'],
                gender_id=gender.id if gender else None,
                goodreads_url=a['goodreads_url'],
                amazon_url=a['amazon_url'],
                website=a['website'],
                # alias_of_id will be set in second pass
            )
            db.session.add(author)
            db.session.flush()
            author_map[a['notion_id']] = author
        db.session.commit()

        # Import Authors (second pass - set alias relationships)
        print("    Setting author aliases...")
        for a in data['authors']:
            if a['alias_of_notion_id']:
                author = author_map.get(a['notion_id'])
                alias_of = author_map.get(a['alias_of_notion_id'])
                if author and alias_of:
                    author.alias_of_id = alias_of.id
        db.session.commit()
        print(f"    Done ({len(author_map)} mapped)")

        # Import Series
        print(f"\n  Importing {len(data['series'])} series...")
        for s in data['series']:
            # Check if series already exists by name
            existing = Series.query.filter_by(name=s['name']).first()
            if existing and not clear_existing:
                series_map[s['notion_id']] = existing
                continue

            series = Series(
                name=s['name'],
                number_in_series=s['number_in_series'],
                goodreads_url=s['goodreads_url'],
            )
            db.session.add(series)
            db.session.flush()
            series_map[s['notion_id']] = series
        db.session.commit()
        print(f"    Done ({len(series_map)} mapped)")

        # Import Books
        print(f"\n  Importing {len(data['books'])} books...")
        books_imported = 0
        reads_created = 0

        for b in data['books']:
            # Check if book already exists by title
            existing = Book.query.filter_by(title=b['title']).first()
            if existing and not clear_existing:
                print(f"    Skipping existing: {b['title']}")
                continue

            # Get related entities
            series = series_map.get(b['series_notion_id']) if b['series_notion_id'] else None
            fmt = format_map.get(b['format_notion_id']) if b['format_notion_id'] else None

            # If no format found, use a default
            if not fmt:
                fmt = BookFormat.query.first()

            # Download cover image if available
            cover_filename = None
            if b['cover_url']:
                cover_filename = download_cover_image(b['cover_url'], b['title'])

            book = Book(
                title=b['title'],
                subtitle=b['subtitle'],
                date_purchased=parse_date(b['date_purchased']),
                date_added=parse_date(b['date_added']) or datetime.now(),
                page_count=b['page_count'],
                series_id=series.id if series else None,
                series_number=b['series_number'],
                format_id=fmt.id,
                cost=b['cost'],
                paid=b['paid'],
                discounts=b['discounts'],
                is_book_bundle=b['is_book_bundle'],
                bundled_books=b['bundled_books'],
                cover_image=cover_filename,
                rating=b['rating'],
                comment=b['comment'],
            )

            # Add authors
            for author_notion_id in b['author_notion_ids']:
                author = author_map.get(author_notion_id)
                if author:
                    book.authors.append(author)

            db.session.add(book)
            db.session.flush()
            books_imported += 1

            # Create Read entries
            read_count = b.get('read_count') or 0

            # If read_count > 1, create additional completed reads without dates for previous reads
            if read_count > 1:
                for i in range(read_count - 1):
                    prior_read = Read(
                        book_id=book.id,
                        start_date=None,
                        finish_date=None,
                        status='Completed',
                    )
                    db.session.add(prior_read)
                    reads_created += 1

            # Create the current/most recent Read entry if there's reading data
            if b['read_status']:
                read = Read(
                    book_id=book.id,
                    start_date=parse_date(b['start_date']),
                    finish_date=parse_date(b['finish_date']),
                    status=b['read_status'],
                )
                db.session.add(read)
                reads_created += 1

            if books_imported % 50 == 0:
                print(f"    Imported {books_imported} books...")
                db.session.commit()

        db.session.commit()
        print(f"    Done ({books_imported} books, {reads_created} reads)")

        # Summary
        print("\n" + "="*50)
        print("Import Summary:")
        print(f"  Genders:  {AuthorGender.query.count()}")
        print(f"  Formats:  {BookFormat.query.count()}")
        print(f"  Authors:  {Author.query.count()}")
        print(f"  Series:   {Series.query.count()}")
        print(f"  Books:    {Book.query.count()}")
        print(f"  Reads:    {Read.query.count()}")
        print("="*50)


def main():
    clear = '--clear' in sys.argv

    if clear:
        response = input("WARNING: This will delete all existing books, authors, series, and reads. Continue? [y/N] ")
        if response.lower() != 'y':
            print("Aborted.")
            sys.exit(0)

    import_data(clear_existing=clear)


if __name__ == '__main__':
    main()
