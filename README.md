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

## Docker Deployment

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
