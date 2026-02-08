# Book Database

A personal book database web application for tracking books, authors, series, and reading progress.

## Features

- **Book Management**: Add, edit, and delete books with cover images, ratings, and detailed metadata
- **Author Management**: Track authors with links to their books and external profiles
- **Series Management**: Organize books into series with proper ordering
- **Reading Tracking**: Track your reading progress with start/finish dates and status
- **Dashboard**: View currently reading books at a glance
- **Search**: Find books, authors, and series quickly

## Tech Stack

- **Backend**: Flask (Python)
- **Database**: SQLite
- **Frontend**: HTML templates (Jinja2) with htmx for interactivity
- **CSS**: Pico CSS (classless framework)

## Setup

1. Create a virtual environment (recommended):
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Run the application:
   ```bash
   python app.py
   ```

4. Open http://localhost:5001 in your browser

## File Structure

```
book-database/
├── app.py              # Main Flask application with routes
├── models.py           # SQLAlchemy ORM models
├── database.py         # Database initialization and seed data
├── requirements.txt    # Python dependencies
├── README.md
├── static/
│   ├── css/style.css   # Custom CSS overrides
│   └── uploads/        # Book cover images
└── templates/
    ├── base.html       # Base template with navigation
    ├── dashboard.html
    ├── search.html
    ├── search_results.html
    ├── books/
    │   ├── list.html
    │   ├── detail.html
    │   └── form.html
    ├── authors/
    │   ├── list.html
    │   ├── detail.html
    │   └── form.html
    └── series/
        ├── list.html
        ├── detail.html
        └── form.html
```

## Database

The SQLite database (`books.db`) is created automatically on first run with seed data for:
- Book formats: Kindle, Kobo, ePub, Hardcover, Paperback, Comic Archive, Audiobook, PDF
- Author genders: Female, Male, Nonbinary, Unknown

### Cover Images

Book cover images are stored on the filesystem in `static/uploads/` rather than as BLOBs in the database. This keeps the database small, allows Flask to serve images directly as static files with browser caching, and avoids the overhead of streaming binary data through a database query. The tradeoff is that `static/uploads/` must be backed up separately from `books.db`.

## Usage

1. **Add Authors**: Start by adding authors you want to track
2. **Add Series**: Create series for book collections
3. **Add Books**: Add books with author and series associations
4. **Track Reading**: Start a read from the book detail page
5. **Dashboard**: View your currently reading books
