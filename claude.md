# Book Database Web Application

## Project Overview

Build a personal book database web application for tracking books, authors, series, and reading progress. The application should be simple, lightweight, and easy to deploy locally or on basic infrastructure.

## Technology Stack

- **Backend**: Flask (Python)
- **Database**: SQLite
- **Frontend**: HTML templates (Jinja2) with htmx for interactivity
- **CSS**: Minimal custom CSS or a lightweight framework (your choice)
- **Dependencies**: Minimal - Flask, SQLite (built-in), htmx (CDN)

## Core Features

### 1. Book Management
- Add new books with all fields from schema
- Edit existing books
- View book details including:
  - Cover image
  - All metadata (title, subtitle, description, etc.)
  - Associated author(s)
  - Series information
  - All reads for this book
- Delete books (with confirmation)
- Upload/manage cover images

### 2. Author Management
- Add new authors
- Edit author information
- View author details including:
  - All books by this author
  - Aliases (other author entities linked as aliases)
- Link authors to books (many-to-many relationship)
- Delete authors (with appropriate handling of book relationships)

### 3. Series Management
- Add new series
- Edit series information
- View series details including:
  - All books in the series (ordered by seriesNumber)
  - External links (Goodreads, Amazon, Storygraph)
- Delete series (with appropriate handling of book relationships)

### 4. Reading Tracking
- Add new read entries for books
- Edit read information (start date, finish date, status)
- Business rule: Only one active read per book at a time
  - Active = status is "Reading" (not "Completed" or "Abandoned")
- View reading history per book

### 5. Dashboard
- Display all books with active reads (status = "Reading")
- Show key information for each active read:
  - Book title and cover
  - Start date
  - Author(s)
  - Page count
- Sort/filter options for active reads

### 6. Search Functionality
- Search for books by title, subtitle, or description
- Search for authors by name
- Search for series by name
- Display search results with relevant information
- Click through to detailed views

## Database Schema

### Book Table
```sql
- id (primary key)
- title (required)
- subtitle (optional)
- date_purchased (datetime, optional)
- date_added (datetime, required, default: current timestamp)
- description (text, optional)
- page_count (integer, optional)
- series_id (foreign key to Series, optional)
- series_number (float, optional)
- format_id (foreign key to BookFormat, required)
- cost (decimal, optional)
- paid (decimal, optional)
- discounts (decimal, optional)
- saved (computed: cost - paid)
- is_book_bundle (boolean, default: false)
- bundled_books (string, optional, e.g., "1-3, 1-10")
- cover_image (string/path, optional)
- rating (float, 0-5 in 0.25 increments, optional)
- comment (text, optional)
```

### Author Table
```sql
- id (primary key)
- name (required)
- pronouns (optional)
- gender_id (foreign key to AuthorGender, optional)
- goodreads_url (optional)
- amazon_url (optional)
- storygraph_url (optional)
- website (optional)
- alias_of_id (foreign key to Author, optional - for tracking aliases)
```

### BookAuthor Table (Many-to-Many)
```sql
- book_id (foreign key to Book)
- author_id (foreign key to Author)
- primary key (book_id, author_id)
```

### Series Table
```sql
- id (primary key)
- name (required)
- number_in_series (integer, optional)
- goodreads_url (optional)
- amazon_url (optional)
- storygraph_url (optional)
```

### Read Table
```sql
- id (primary key)
- book_id (foreign key to Book, required)
- start_date (datetime, optional)
- finish_date (datetime, optional)
- status (enum: 'Reading', 'Completed', 'Abandoned', required)
- created_at (datetime, default: current timestamp)
```

### BookFormat Table
```sql
- id (primary key)
- name (required, e.g., "Kindle", "Kobo", "ePub", "Hardcover", "Paperback", "Comic Archive")
```

### AuthorGender Table
```sql
- id (primary key)
- name (required, e.g., "Female", "Male", "Unknown", "Nonbinary")
```

## User Interface Requirements

### Layout
- Simple, clean navigation bar with links to:
  - Dashboard
  - Books
  - Authors
  - Series
  - Search
- Responsive design (mobile-friendly)

### Pages Needed

1. **Dashboard** (`/`)
   - List of books with active reads
   - Quick stats (optional: total books, total reads, etc.)

2. **Books List** (`/books`)
   - Paginated list of all books
   - Search/filter options
   - "Add New Book" button
   - Each book shows: cover thumbnail, title, author(s), rating

3. **Book Detail** (`/books/<id>`)
   - Full book information
   - Edit button
   - Delete button
   - List of all reads for this book
   - "Add Read" button

4. **Add/Edit Book** (`/books/new`, `/books/<id>/edit`)
   - Form with all book fields
   - Author selection (multi-select or autocomplete)
   - Series selection
   - Format selection
   - Cover image upload

5. **Authors List** (`/authors`)
   - List of all authors
   - "Add New Author" button
   - Search functionality

6. **Author Detail** (`/authors/<id>`)
   - Author information
   - List of books by this author
   - Edit/delete buttons

7. **Add/Edit Author** (`/authors/new`, `/authors/<id>/edit`)
   - Form with all author fields
   - Gender selection
   - Alias selection

8. **Series List** (`/series`)
   - List of all series
   - "Add New Series" button

9. **Series Detail** (`/series/<id>`)
   - Series information
   - Books in series (ordered by series number)
   - Edit/delete buttons

10. **Add/Edit Series** (`/series/new`, `/series/<id>/edit`)
    - Form with all series fields

11. **Search** (`/search`)
    - Search form
    - Tabbed or sectioned results (Books, Authors, Series)

### htmx Interactivity
- Search results update as you type (debounced)
- Add/edit forms submit without full page reload
- Delete actions with inline confirmation
- Dynamic form fields (e.g., adding multiple authors)
- Inline editing where appropriate

## Technical Requirements

### File Structure
```
book-database/
├── app.py                 # Main Flask application
├── database.py            # Database initialization and models
├── models.py              # SQLAlchemy models (or raw SQL)
├── static/
│   ├── css/
│   │   └── style.css
│   └── uploads/           # For book cover images
├── templates/
│   ├── base.html          # Base template with nav
│   ├── dashboard.html
│   ├── books/
│   │   ├── list.html
│   │   ├── detail.html
│   │   └── form.html
│   ├── authors/
│   │   ├── list.html
│   │   ├── detail.html
│   │   └── form.html
│   ├── series/
│   │   ├── list.html
│   │   ├── detail.html
│   │   └── form.html
│   └── search.html
├── requirements.txt       # Python dependencies
├── README.md             # Setup and usage instructions
└── books.db              # SQLite database (created on first run)
```

### Setup and Deployment
- Include instructions for:
  - Installing dependencies (`pip install -r requirements.txt`)
  - Running locally (`python app.py`)
  - Database initialization
- Make it easy to run with: `python app.py`

### Data Validation
- Ensure only one active read per book
- Validate rating is between 0-5 in 0.25 increments
- Validate URLs are properly formatted (optional)
- Validate cost/paid/discounts are valid currency values

### Image Handling
- Allow uploading book cover images
- Store images in `static/uploads/`
- Display placeholder if no cover exists
- Support common image formats (jpg, png, webp)

## Optional Enhancements (Future)
- Export data to CSV/JSON
- Import books from Goodreads/other services
- Reading statistics and charts
- Tags for books
- Wishlists or "to-read" tracking
- Book recommendations based on reading history

## Notes
- Prioritize simplicity and functionality over complexity
- Keep dependencies minimal
- Ensure the app can run entirely offline
- Use sensible defaults and helpful error messages
- Include seed data or easy way to add initial formats/genders
