# Book Database

A personal book database web application for tracking books, authors, series, and reading progress.

## Features

- **Book Management**: Add, edit, and delete books with cover images, ratings, and detailed metadata
- **Author Management**: Track authors with links to their books and external profiles
- **Series Management**: Organize books into series with proper ordering
- **Bundle/Omnibus Support**: Link child books to a parent bundle, with combined reading history and visual indicators on book covers
- **Reading Tracking**: Track your reading progress with start/finish dates and status
- **Reading Queue**: Plan your next reads with a drag-to-reorder queue; supports external (non-library) entries
- **Dashboard**: View currently reading books at a glance
- **Recommendations**: Get suggestions for series continuations, recent additions, and random picks from your library
- **Statistics**: Charts and summaries of your reading history
- **Search**: Find books, authors, and series quickly
- **Price Watch**: Track Amazon Kindle book prices and get a [Pushover](https://pushover.net/) notification when the price drops

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
├── app.py              # Flask app factory; registers blueprints, starts the price watch scheduler
├── models.py           # SQLAlchemy ORM models
├── database.py         # Database initialization, migrations, and seed data
├── scrapers.py         # Amazon/Goodreads page scraping (book data, prices, series counts)
├── notifications.py    # Pushover notification helper
├── price_watch.py      # Daily background price check + manual "Check Now" logic
├── utils.py            # Shared helpers (URL cleaning, validation, parsing)
├── requirements.txt    # Python dependencies
├── README.md
├── changelog.json      # Version history
├── routes/             # Flask blueprints, one per feature area
│   ├── books.py
│   ├── authors.py
│   ├── series.py
│   ├── queue.py
│   ├── search.py
│   ├── system.py
│   └── price_watch.py
├── static/
│   ├── css/style.css   # Custom CSS overrides
│   └── uploads/        # Book cover images
└── templates/
    ├── base.html       # Base template with sidebar navigation
    ├── dashboard.html
    ├── recommendations.html
    ├── statistics.html
    ├── search.html
    ├── search_results.html
    ├── queue.html
    ├── queue/
    │   ├── _button.html  # Add/remove queue button partial
    │   └── _item.html    # Queue row partial
    ├── price_watch/
    │   ├── list.html      # Add form + watch list
    │   └── _item.html     # Watch row partial
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

## Price Watch

Paste an Amazon Kindle URL on the Price Watch page to start tracking it - no need for the book to already be in your library. A background check runs once a day (there's also a "Check Now" button for an on-demand check), comparing the latest price against the last known price. If it's dropped, you get a Pushover notification; either way the watch's current price is updated. Only the initial and current price are kept, not a full history.

Requires `PUSHOVER_USER_KEY`/`PUSHOVER_APP_TOKEN` to be set (see [Environment Variables](#environment-variables)) for notifications to actually be sent - without them, price drops are still detected and shown on the page, just not pushed to your phone.

## Docker Deployment

### Environment Variables

Create a `.env` file in the project root (it's gitignored, so it never gets committed) with any of the following:

```
SECRET_KEY=some-random-string
PUSHOVER_USER_KEY=your-pushover-user-key
PUSHOVER_APP_TOKEN=your-pushover-application-token
```

- `SECRET_KEY` - used to sign Flask session cookies. Falls back to an insecure development default (with a startup warning) if unset.
- `PUSHOVER_USER_KEY` / `PUSHOVER_APP_TOKEN` - optional. Required only for [Price Watch](#price-watch) notifications. Get both from [pushover.net](https://pushover.net/) (the user key from your dashboard, the app token by creating an application). When unset, price drops are detected but no notification is sent, and the "Send Test Notification" button on the System page is hidden.

`docker-compose.yml`/`docker-compose.prod.yml` read these via `${VARNAME}` substitution, which Docker Compose resolves automatically from a `.env` file in the same directory - the compose files themselves never contain real secrets.

### Build and export

```bash
./build-docker.sh
```

This builds the Docker image and exports it to `bookdatabase.tar`.

### Deploy locally

```bash
./deploy-local.sh
```

### Deploy to production (Windows)

Copy `bookdatabase.tar` and `docker-compose.prod.yml` to the host, then:

```powershell
.\deploy-prod.ps1
```

Data is stored in `./data/instance/` (database) and `./data/uploads/` (cover images).

## Backups

A PowerShell backup script provides rolling daily/weekly/monthly backups of the database and cover images.

### Basic usage

```powershell
# Backup to a local directory
.\backup.ps1

# Backup to a NAS or network share
.\backup.ps1 -BackupDir "Z:\Backups\BookDatabase"
.\backup.ps1 -BackupDir "\\nas\backups\bookdatabase"
```

### How it works

1. **Database**: Uses `sqlite3 .backup` via the Docker container for a safe, consistent copy (falls back to file copy if the container isn't running)
2. **Images**: Copies the `data/uploads/` directory
3. **Compression**: Bundles everything into a timestamped `.zip` file
4. **Rotation**: Manages three tiers of backups:
   - **Daily**: kept for 7 days (default)
   - **Weekly**: created on Sundays, kept for 4 weeks (default)
   - **Monthly**: created on the 1st, kept for 12 months (default)

### Retention settings

```powershell
.\backup.ps1 -BackupDir "Z:\Backups\BookDatabase" -DailyKeep 14 -WeeklyKeep 8 -MonthlyKeep 24
```

### Scheduling with Task Scheduler

To run backups automatically at 2 AM daily:

1. Open **Task Scheduler** (`taskschd.msc`)
2. Click **Create Basic Task**
3. Set the trigger to **Daily** at **2:00 AM**
4. Action: **Start a program**
   - Program: `powershell.exe`
   - Arguments: `-ExecutionPolicy Bypass -File "C:\path\to\bookdatabase\backup.ps1" -BackupDir "Z:\Backups\BookDatabase"`
   - Start in: `C:\path\to\bookdatabase`
5. Check "Run whether user is logged on or not"

### Restoring from backup

1. Stop the container: `docker compose -f docker-compose.prod.yml down`
2. Extract the backup zip
3. Copy `books.db` to `./data/instance/`
4. Copy the `uploads/` folder to `./data/uploads/`
5. Start the container: `docker compose -f docker-compose.prod.yml up -d`

## Usage

1. **Add Authors**: Start by adding authors you want to track
2. **Add Series**: Create series for book collections
3. **Add Books**: Add books with author and series associations
4. **Track Reading**: Start a read from the book detail page
5. **Dashboard**: View your currently reading books
6. **Reading Queue**: Add books to your queue from any book card or detail page; drag to reorder
7. **Recommendations**: Get reading suggestions based on your library and reading history
8. **Price Watch**: Paste an Amazon Kindle URL to get notified via Pushover when its price drops
