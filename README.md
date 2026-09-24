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
- **Series Monitoring**: Opt a series in to weekly checks and get notified when a new book is released

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
├── app.py              # Flask app factory; registers blueprints, starts background schedulers
├── models.py           # SQLAlchemy ORM models
├── database.py         # Database initialization, migrations, and seed data
├── scrapers.py         # Amazon/Goodreads page scraping (book data, prices, series counts)
├── hardcover.py        # Hardcover API genre lookup
├── googlebooks.py      # Google Books API genre lookup
├── genre_text.py       # Shared title/author matching and genre tidying
├── genre_sources.py    # Which sources a book's genres come from, and in what order
├── notifications.py    # Pushover notification helper
├── price_watch.py      # Daily background price check + manual "Check Now" logic
├── series_monitor.py   # Weekly background check of monitored series for new releases
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
│   ├── img/            # App icons (favicon, home-screen) + UI overlays
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

The notification priority is set on the System page under Notifications (Lowest, Low, Normal, or High - High bypasses your device's quiet hours). It defaults to Normal and applies to both price-drop alerts and the test notification. Pushover's Emergency priority is not offered: it re-alerts until acknowledged on the device and needs retry/expire values, which is more than a price drop warrants.

## Series Monitoring

Series you opt into are checked once a week for newly released books. Turn it on with the **Monitor for New Books** button on a series page; the Series list has a **Monitored** filter and marks monitored series with a badge. It's opt-in on purpose - nothing is checked unless you ask for it.

The first check of a series records what's already published without notifying you, so switching monitoring on for a long-running series doesn't announce its entire backlist. Only books that appear *after* that produce a [Pushover](#price-watch) notification, at whatever priority you've set on the System page. Novellas and in-between instalments (#2.5) count as releases too.

Books found this way are **not** added to your library - the library stays a record of books you actually have. They're listed under **Newly Released** on the dashboard, separate from Recently Added, each with a Dismiss button for the box sets, omnibuses and foreign editions that series pages tend to mix in. If you later add the book yourself, the entry links itself to it and drops off the list.

Checks read whichever series page you've recorded - Goodreads first, then Amazon - and try the other if one can't be read, so a site changing its markup or blocking the app doesn't stop a series listed on both. A series doesn't need either URL configured: if neither is set, the app works one out once from a book you already own in that series - reading the series straight off that book's Goodreads or Amazon page when the book has a link, and searching by title otherwise - then reuses it. The weekly check also refreshes the series' book count while it's there.

Checking is deliberately slow - one series a minute, and each series only revisited weekly - to stay well clear of the rate limits described under [Price Watch](#price-watch). If a site blocks the app mid-run, checking backs off rather than continuing.

## A Note on Access Control

The app has no login: anyone who can reach it on your network can use it. It's built for a single user on a private network, and adding accounts would be the change to make if that ever stops being true.

State-changing requests (anything that isn't a GET) are checked to make sure they came from a page of this app rather than another site. Without that, a malicious page open in your browser could quietly POST to the app's address - deleting books, or triggering the import that replaces the whole database - without the attacker needing any access to your network. Requests that send no `Origin` or `Referer` at all, such as `curl` or a script, are allowed through: a browser can't be made to omit both on a cross-site write, so refusing them would only break local tooling.

## Docker Deployment

### Environment Variables

Create a `.env` file in the project root (it's gitignored, so it never gets committed) with any of the following:

```
SECRET_KEY=some-random-string
PUSHOVER_USER_KEY=your-pushover-user-key
PUSHOVER_APP_TOKEN=your-pushover-application-token
HARDCOVER_TOKEN=your-hardcover-api-token
GOOGLEBOOKS_TOKEN=your-google-books-api-key
```

- `SECRET_KEY` - used to sign Flask session cookies. Falls back to an insecure development default (with a startup warning) if unset. **Set this on the production host**: generate one with `python -c "import secrets; print(secrets.token_hex(32))"` and put it in `.env`. Until v1.1.1 the compose files hardcoded a placeholder, which meant the `.env` value was ignored and the startup warning never fired.
- `PUSHOVER_USER_KEY` / `PUSHOVER_APP_TOKEN` - optional. Required only for [Price Watch](#price-watch) notifications. Get both from [pushover.net](https://pushover.net/) (the user key from your dashboard, the app token by creating an application). When unset, price drops are detected but no notification is sent, and the "Send Test Notification" button on the System page is hidden.
- `HARDCOVER_TOKEN` - optional, recommended. A free account at [Hardcover](https://hardcover.app/) (token in your account settings) adds it to the chain below. Measured over this library it supplies genres for about two thirds of it, roughly five per book, agreeing with 43% of the tags already present - but it is blind to obscure self-published and manga titles.
- `GOOGLEBOOKS_TOKEN` - optional. A free [Google Cloud](https://console.cloud.google.com/) API key with the **Books API** enabled on its project (enable it first, or it won't appear in the key's API restriction list). It reaches indie titles the other sources miss - it was the only source to answer for any of this library's untagged books - but returns about one broad category per book, most often just "Fiction". Free quota is 1,000 lookups a day. Keyless requests are refused outright, so without a key this source is simply skipped.

Genres are looked up through a chain of sources, and the order differs by context. Fetching tags for **one book** tries Goodreads first, then Hardcover, then Google Books: a request or two is affordable and Goodreads gives by far the best tags. A **library-wide scan** reverses that - Hardcover, then Google Books, then Goodreads - because Goodreads blocks automated requests after a handful and a scan is judged on finishing. Once it blocks during a scan it is dropped for the rest of that run. Each source can also be aimed at directly from the book page's tag menu, and the System page lists which are configured.

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
