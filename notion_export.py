#!/usr/bin/env python3
"""
Export data from Notion databases to a local JSON file.

Usage:
    python notion_export.py

Requires NOTION_TOKEN in .env file or as environment variable.
Outputs: notion_data.json
"""

import json
import os
import re
import sys
import requests
from datetime import datetime


def load_dotenv():
    """Load environment variables from .env file."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ.setdefault(key.strip(), value.strip())


load_dotenv()

# Notion API configuration
NOTION_TOKEN = os.environ.get('NOTION_TOKEN')
if not NOTION_TOKEN:
    print("Error: NOTION_TOKEN not found. Set it in .env file or as environment variable.")
    sys.exit(1)
NOTION_VERSION = '2022-06-28'
BASE_URL = 'https://api.notion.com/v1'

# Database IDs from your Notion workspace
DATABASES = {
    'books': 'a39c13a6-5c1e-4ff9-b947-48be240302e5',
    'authors': '404de99f-ba67-457e-a63d-3643e5af9c15',
    'series': 'adba9204-285d-4737-802f-ed6727e5433a',
    'genders': '6fbf0509-301d-408b-9855-a023da3f64dd',
    'formats': 'd76fae7e-7263-4117-9be0-c60229950b17',
}

HEADERS = {
    'Authorization': f'Bearer {NOTION_TOKEN}',
    'Notion-Version': NOTION_VERSION,
    'Content-Type': 'application/json',
}


def get_all_pages(database_id):
    """Fetch all pages from a Notion database, handling pagination."""
    pages = []
    url = f'{BASE_URL}/databases/{database_id}/query'
    has_more = True
    start_cursor = None

    while has_more:
        payload = {}
        if start_cursor:
            payload['start_cursor'] = start_cursor

        response = requests.post(url, headers=HEADERS, json=payload)
        response.raise_for_status()
        data = response.json()

        pages.extend(data['results'])
        has_more = data['has_more']
        start_cursor = data.get('next_cursor')

    return pages


def extract_title(prop):
    """Extract plain text from a title property."""
    if not prop or not prop.get('title'):
        return ''
    return ''.join([t.get('plain_text', '') for t in prop['title']])


def extract_rich_text(prop):
    """Extract plain text from a rich_text property."""
    if not prop or not prop.get('rich_text'):
        return ''
    return ''.join([t.get('plain_text', '') for t in prop['rich_text']])


def extract_number(prop):
    """Extract number from a number property."""
    if not prop:
        return None
    return prop.get('number')


def extract_select(prop):
    """Extract select value."""
    if not prop or not prop.get('select'):
        return None
    return prop['select'].get('name')


def extract_date(prop):
    """Extract date string from a date property."""
    if not prop or not prop.get('date'):
        return None
    date_obj = prop['date']
    return date_obj.get('start')


def extract_url(prop):
    """Extract URL from a url property."""
    if not prop:
        return None
    return prop.get('url')


def extract_relation_ids(prop):
    """Extract list of related page IDs from a relation property."""
    if not prop or not prop.get('relation'):
        return []
    return [r['id'] for r in prop['relation']]


def extract_files(prop):
    """Extract file URLs from a files property."""
    if not prop or not prop.get('files'):
        return []
    files = []
    for f in prop['files']:
        if f['type'] == 'file':
            files.append(f['file']['url'])
        elif f['type'] == 'external':
            files.append(f['external']['url'])
    return files


def extract_created_time(prop):
    """Extract created_time."""
    if not prop:
        return None
    return prop.get('created_time')


def parse_rating(rating_str):
    """Convert Notion rating (emoji stars or numbers) to numeric value."""
    if not rating_str:
        return None

    # Count star emojis
    star_count = rating_str.count('⭐')
    if star_count > 0:
        # Check for half ratings like "⭐ ⭐ ⭐.5"
        if '.5' in rating_str:
            return star_count + 0.5
        elif '.75' in rating_str:
            return star_count + 0.75
        elif '.25' in rating_str:
            return star_count + 0.25
        return float(star_count)

    # Try to parse as number
    try:
        # Extract number from string like "3.5"
        match = re.search(r'(\d+\.?\d*)', rating_str)
        if match:
            return float(match.group(1))
    except (ValueError, AttributeError):
        pass

    return None


def export_genders(pages):
    """Export gender data."""
    genders = []
    for page in pages:
        props = page['properties']
        genders.append({
            'notion_id': page['id'],
            'name': extract_title(props.get('Name')),
        })
    return genders


def export_formats(pages):
    """Export format data."""
    formats = []
    for page in pages:
        props = page['properties']
        formats.append({
            'notion_id': page['id'],
            'name': extract_title(props.get('Name')),
        })
    return formats


def export_authors(pages):
    """Export author data."""
    authors = []
    for page in pages:
        props = page['properties']
        gender_ids = extract_relation_ids(props.get('Gender'))
        alias_ids = extract_relation_ids(props.get('Alias For'))

        authors.append({
            'notion_id': page['id'],
            'name': extract_title(props.get('Name')),
            'pronouns': extract_rich_text(props.get('Pronouns')) or None,
            'gender_notion_id': gender_ids[0] if gender_ids else None,
            'goodreads_url': extract_url(props.get('GoodReads Page')),
            'amazon_url': extract_url(props.get('Amazon Page')),
            'website': extract_url(props.get('Website')),
            'alias_of_notion_id': alias_ids[0] if alias_ids else None,
        })
    return authors


def export_series(pages):
    """Export series data."""
    series_list = []
    for page in pages:
        props = page['properties']
        series_list.append({
            'notion_id': page['id'],
            'name': extract_title(props.get('Name')),
            'number_in_series': extract_number(props.get('Books/Volumes')),
            'goodreads_url': extract_url(props.get('GoodReads Page')),
        })
    return series_list


def export_books(pages):
    """Export book data."""
    books = []
    for page in pages:
        props = page['properties']

        # Get relation IDs
        author_ids = extract_relation_ids(props.get('Author'))
        series_ids = extract_relation_ids(props.get('Series'))
        format_ids = extract_relation_ids(props.get('Format'))

        # Map Notion status to our status
        notion_status = extract_select(props.get('Status'))
        status_map = {
            'Read': 'Completed',
            'Unread': None,  # No read entry
            'In Progress': 'Reading',
            'Did not finish': 'Abandoned',
        }
        read_status = status_map.get(notion_status)

        # Get cover image
        cover_urls = extract_files(props.get('Cover'))

        # Parse bundle
        bundle_select = extract_select(props.get('Bundle'))
        is_bundle = bundle_select == 'Yes' if bundle_select else False

        # Get read count for multiple re-reads
        read_count = extract_number(props.get('Read #'))
        read_count = int(read_count) if read_count else None

        books.append({
            'notion_id': page['id'],
            'title': extract_title(props.get('Title')),
            'subtitle': extract_rich_text(props.get('Subtitle')) or None,
            'author_notion_ids': author_ids,
            'series_notion_id': series_ids[0] if series_ids else None,
            'series_number': extract_number(props.get('Series Number')),
            'format_notion_id': format_ids[0] if format_ids else None,
            'page_count': extract_number(props.get('Pages')),
            'cost': extract_number(props.get('Cost')),
            'paid': extract_number(props.get('Paid')),
            'discounts': extract_number(props.get('VIP Savings')),
            'date_purchased': extract_date(props.get('Purchase Date')),
            'date_added': extract_created_time(props.get('Date Added')),
            'rating': parse_rating(extract_select(props.get('Rating'))),
            'comment': extract_rich_text(props.get('Comment')) or None,
            'is_book_bundle': is_bundle,
            'bundled_books': extract_rich_text(props.get('Bundled Books')) or None,
            'cover_url': cover_urls[0] if cover_urls else None,
            # Reading data (will be used to create Read entries)
            'read_status': read_status,
            'start_date': extract_date(props.get('Start Date')),
            'finish_date': extract_date(props.get('Finished Date')),
            'read_count': read_count,  # Number of times read (for re-reads)
        })
    return books


def main():
    print('Exporting data from Notion...')

    # Fetch all data
    print('  Fetching genders...')
    gender_pages = get_all_pages(DATABASES['genders'])
    genders = export_genders(gender_pages)
    print(f'    Found {len(genders)} genders')

    print('  Fetching formats...')
    format_pages = get_all_pages(DATABASES['formats'])
    formats = export_formats(format_pages)
    print(f'    Found {len(formats)} formats')

    print('  Fetching authors...')
    author_pages = get_all_pages(DATABASES['authors'])
    authors = export_authors(author_pages)
    print(f'    Found {len(authors)} authors')

    print('  Fetching series...')
    series_pages = get_all_pages(DATABASES['series'])
    series = export_series(series_pages)
    print(f'    Found {len(series)} series')

    print('  Fetching books...')
    book_pages = get_all_pages(DATABASES['books'])
    books = export_books(book_pages)
    print(f'    Found {len(books)} books')

    # Compile all data
    data = {
        'exported_at': datetime.now().isoformat(),
        'genders': genders,
        'formats': formats,
        'authors': authors,
        'series': series,
        'books': books,
    }

    # Write to file
    output_file = 'notion_data.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f'\nExported to {output_file}')
    print(f'  {len(genders)} genders')
    print(f'  {len(formats)} formats')
    print(f'  {len(authors)} authors')
    print(f'  {len(series)} series')
    print(f'  {len(books)} books')


if __name__ == '__main__':
    main()
