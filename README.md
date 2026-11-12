# Book Database

A personal book database web application for tracking books, authors, series, and reading progress.

## Features

- **Book Management**: Add, edit, and delete books with cover images, ratings, and detailed metadata
- **Author Management**: Track authors with links to their books and external profiles
- **Series Management**: Organize books into series with proper ordering
- **Bundle/Omnibus Support**: Link child books to a parent bundle, with combined reading history and visual indicators on book covers
- **Reading Tracking**: Track your reading progress with start/finish dates and status
- **To Be Read**: Plan your next reads with a drag-to-reorder queue, and browse your Want to Read list by cover and blurb
- **Owned & Want to Read**: Keep books you don't own yet alongside your library, and flag any book as one you want to read
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

Needs **Python 3.10 or newer** - Pillow 12, which the cover thumbnails use, dropped 3.9. The Docker image is built on 3.12.

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
├── scrapers.py         # Amazon/Goodreads/Kobo page reading (book data, prices, series counts)
├── capture.py          # Adding books from pages captured in the browser
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
│   ├── price_watch.py
│   └── api.py          # JSON endpoints for the browser extension and Shortcut
├── browser-extension/  # Chromium extension for adding books from the browser
├── tests/              # Page parser tests, with saved pages in tests/fixtures/pages
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

Only JPEG, PNG, GIF and WebP are decoded when building thumbnails. Pillow identifies a file by its magic bytes rather than its name and will open 43 formats by default, so the upload extension check constrains what gets *stored*, not what gets *decoded* - a cover fetched from a URL is saved under a guessed extension, and unrecognised types default to `.jpg`. Naming the permitted formats (`COVER_IMAGE_FORMATS` in `utils.py`) keeps everything else away from the decoders, which is also the documented mitigation for CVE-2026-25990, an out-of-bounds write reached by opening a crafted PSD.

## Price Watch

Paste an Amazon Kindle URL on the Price Watch page to start tracking it - no need for the book to already be in your library. A background check runs once a day (there's also a "Check Now" button for an on-demand check), comparing the latest price against the last known price. If it's dropped, you get a Pushover notification; either way the watch's current price is updated. Only the initial and current price are kept, not a full history.

Requires `PUSHOVER_USER_KEY`/`PUSHOVER_APP_TOKEN` to be set (see [Environment Variables](#environment-variables)) for notifications to actually be sent. Without them the Price Watch page says so in a banner, and drops are still detected but kept as **pending** rather than discarded: each watch remembers the last price you were notified about, and a drop is only marked as notified once Pushover accepts it. A pending drop is flagged on its row and retried on every check, so it arrives as soon as notifications work again - the same applies if Pushover itself is down when a drop is found.

The notification priority is set on the System page under Notifications (Lowest, Low, Normal, or High - High bypasses your device's quiet hours). It defaults to Normal and applies to both price-drop alerts and the test notification. Pushover's Emergency priority is not offered: it re-alerts until acknowledged on the device and needs retry/expire values, which is more than a price drop warrants.

## Series Monitoring

Series you opt into are checked once a week for newly released books. Turn it on with the **Monitor for New Books** button on a series page; the Series list has a **Monitored** filter and marks monitored series with a badge. It's opt-in on purpose - nothing is checked unless you ask for it.

The first check of a series records what's already published without notifying you, so switching monitoring on for a long-running series doesn't announce its entire backlist. Only books that appear *after* that produce a [Pushover](#price-watch) notification, at whatever priority you've set on the System page. Novellas and in-between instalments (#2.5) count as releases too.

Books found this way are **not** added to your library - the library stays a record of books you actually have. They're listed under **Newly Released** on the dashboard, separate from Recently Added, each with a Dismiss button for the box sets, omnibuses and foreign editions that series pages tend to mix in. If you later add the book yourself, the entry links itself to it and drops off the list.

Checks read whichever series page you've recorded - Goodreads first, then Amazon - and try the other if one can't be read, so a site changing its markup or blocking the app doesn't stop a series listed on both. A series doesn't need either URL configured: if neither is set, the app works one out once from a book you already own in that series - reading the series straight off that book's Goodreads or Amazon page when the book has a link, and searching by title otherwise - then reuses it. The weekly check also refreshes the series' book count while it's there.

Checking is deliberately slow - one series a minute, and each series only revisited weekly - to stay well clear of the rate limits described under [Price Watch](#price-watch). If a site blocks the app mid-run, checking backs off rather than continuing.

## Adding Books From the Browser

You can add the Goodreads, Amazon or Kobo book page you're looking at straight to the app: from a browser extension in Chrome, Vivaldi, Edge, Brave or any other Chromium browser, or from a Shortcut in Safari's Share menu on iPhone, iPad or Mac. Either way you pick **Owned** or **Not Owned**, and the Add Book form opens filled in from the page for you to review and save. Nothing is saved until you press Save.

The app reads your browser's copy of the page rather than downloading its own, so sites that block automated requests don't get in the way, and Kobo works even though the app can't fetch from it. Genres on the page are only applied if they're already tags; any others are listed under the tag picker with a button to add them. The page's price fills in Cost (the list price, if the page shows a sale).

Both need the device to reach the app: on your home network that's automatic, and away from home it needs a VPN back to it (Tailscale, or your router's VPN).

### Browser extension (Chromium browsers)

The extension lives in `browser-extension/` and is installed straight from that folder; it isn't published to any extension store.

1. Open the browser's extensions page: `vivaldi://extensions` in Vivaldi, `chrome://extensions` in Chrome, `edge://extensions` in Edge, `brave://extensions` in Brave.
2. Turn on **Developer mode** (a toggle at the top right of that page).
3. Click **Load unpacked** and choose the `browser-extension` folder from this repository.
4. Pin the **Book Database** button to the toolbar: click the extensions (puzzle piece) icon and pin it. In Vivaldi, right-click the toolbar and use **Edit** → **Customize Toolbar** if it doesn't appear.
5. Open the extension's settings (on its card on the extensions page, **Details** → **Extension options**, or **Settings** in its popup). Enter the address you open the app at, including the port - for example `http://192.168.1.20:5001` - and click **Save and Test Connection**. The browser asks for permission to contact that address; allow it. You should see *Connected to Book Database* with the app's version.

To use it, open a book's page on Goodreads, Amazon or Kobo and click the toolbar button. The popup shows the book it found (and warns if it looks like something already in your library); click **Add as Owned** or **Add as Not Owned**.

To update after pulling new code, click the reload icon on the extension's card on the extensions page. If you move the repository folder, remove the extension and load it again from the new location.

What it can access: only the tab you click its button on, and only at that moment - it doesn't run on pages in the background or see other tabs. It can only contact the one address you set (changing the address drops access to the old one), and all its code is in that folder; it loads nothing from the internet.

### Shortcut (Safari on iPhone, iPad and Mac)

Build this once in the **Shortcuts** app; it syncs to your other Apple devices through iCloud. Where a step says to pick a variable (shown in blue, like *Shortcut Input*), tap the field and choose it from the bar of variables above the keyboard.

1. In Shortcuts, tap **+** to make a new shortcut and name it **Add to Book Database**.
2. Tap the **ⓘ** (details) button, turn on **Show in Share Sheet**, then tap **Done**. At the top of the shortcut, tap the input types and leave only **Safari web pages** selected.
3. Add **Run JavaScript on Web Page**. Set its input to *Shortcut Input* and replace its script with:
   ```javascript
   completion(JSON.stringify({ url: location.href, html: document.documentElement.outerHTML }));
   ```
4. Add **Text** and enter the app's address followed by `/api/capture`, for example `http://192.168.1.20:5001/api/capture`.
5. Add **Get Contents of URL** and set it up:
   - URL: the *Text* from step 4.
   - Tap **Show More**. Method: **POST**.
   - Headers: add one with key `X-Book-Database-Client` and value `shortcut`.
   - Request Body: **File**, and set the file to *JavaScript Result*.
6. Add **Get Dictionary Value**: get *Value* for key `error` in *Contents of URL*.
7. Add **If**: *Dictionary Value* **has any value**. Inside it, add **Show Alert** with *Dictionary Value* as its message, then **Stop This Shortcut**. Leave the **Otherwise** part empty.
8. After **End If**, add **Get Dictionary Value**: get *Value* for key `summary` in *Contents of URL*.
9. Add **Choose from Menu**. Set its prompt to that *Dictionary Value* (the book's title and author, plus a warning if it may already be in your library), and name its two options `Owned` and `Not Owned`.
10. Under **Owned**, add **Get Dictionary Value** for key `add_owned_url` in *Contents of URL*, then **Open URLs** with that *Dictionary Value*.
11. Under **Not Owned**, add **Get Dictionary Value** for key `add_not_owned_url` in *Contents of URL*, then **Open URLs** with that *Dictionary Value*.

To use it, open a book's page in Safari, tap **Share** (on a Mac, the Share button in Safari's toolbar), then **Add to Book Database**. The first time it runs on each site it asks permission to run JavaScript on that site; choose **Always Allow**. It then shows the book and asks Owned or Not Owned, and opens the filled-in Add Book form in Safari.

If the app can't be reached, Shortcuts shows its own connection error at step 5. On a Mac, if Shortcuts says it can't run JavaScript in Safari, turn on Safari ▸ Settings ▸ Advanced ▸ **Show features for web developers**, then Safari's **Develop** menu ▸ **Allow JavaScript from Apple Events**.

Phone layouts can differ from the desktop pages the app's readers are built from - Amazon's mobile pages in particular. If a page can't be read the Shortcut says so, and the app keeps a copy of the page (the newest five, in `instance/captures/`) so it can be added as a test fixture and the reader fixed; in the meantime, Safari's **Request Desktop Website** (in the **ᴀA** menu) usually works around it.

### How it works

Both clients use two endpoints:

- `GET /api/status` - returns the app's name and version, so a client can confirm its configured address really is this app.
- `POST /api/capture` - takes JSON `{"url": ..., "html": ...}` for a book page and reads it with the same parsers the URL import uses. It returns a preview (`title`, `authors`, `series_name`, `series_number`, `cover_url`), a one-line `summary`, any library books it may duplicate in `existing` (same page link, or same title and author), and two form addresses, `add_owned_url` and `add_not_owned_url`. The parsed details are held for 30 minutes, then forgotten.

Both endpoints require an `X-Book-Database-Client` header and refuse any request whose `Origin` is a website, so a web page can't call them (see [A Note on Access Control](#a-note-on-access-control)).

## A Note on Access Control

The app has no login: anyone who can reach it on your network can use it. It's built for a single user on a private network, and adding accounts would be the change to make if that ever stops being true.

State-changing requests (anything that isn't a GET) are checked to make sure they came from a page of this app rather than another site. Without that, a malicious page open in your browser could quietly POST to the app's address - deleting books, or triggering the import that replaces the whole database - without the attacker needing any access to your network. Requests that send no `Origin` or `Referer` at all, such as `curl` or a script, are allowed through: a browser can't be made to omit both on a cross-site write, so refusing them would only break local tooling. The one exception is the browser extension's own origin (`chrome-extension://…`), which is accepted on `/api/` only.

## Docker Deployment

### Environment Variables

Create a `.env` file in the project root (it's gitignored, so it never gets committed) with any of the following:

```
SECRET_KEY=some-random-string
PUSHOVER_USER_KEY=your-pushover-user-key
PUSHOVER_APP_TOKEN=your-pushover-application-token
```

- `SECRET_KEY` - used to sign Flask session cookies. Falls back to an insecure development default (with a startup warning) if unset. **Set this on the production host**: generate one with `python -c "import secrets; print(secrets.token_hex(32))"` and put it in `.env`. Until v1.1.1 the compose files hardcoded a placeholder, which meant the `.env` value was ignored and the startup warning never fired.
- `PUSHOVER_USER_KEY` / `PUSHOVER_APP_TOKEN` - optional. Required only for [Price Watch](#price-watch) notifications. Get both from [pushover.net](https://pushover.net/) (the user key from your dashboard, the app token by creating an application). When unset, price drops are kept as pending until they can be sent, the Price Watch page shows a warning, and the "Send Test Notification" button on the System page is hidden.

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

## Tests

The page parsers are tested against saved copies of real Goodreads, Amazon and Kobo pages in `tests/fixtures/pages/`. Run them from the repository root:

```bash
python -m unittest discover tests
```

When a site changes its layout and imports or captures start coming back wrong, save the new page from the browser, add it as a fixture (gzipped; scripts, styles and SVG can be stripped since the parsers never read them), and fix the parser until the tests pass.

## Usage

1. **Add Authors**: Start by adding authors you want to track
2. **Add Series**: Create series for book collections
3. **Add Books**: Add books with author and series associations
4. **Track Reading**: Start a read from the book detail page
5. **Dashboard**: View your currently reading books
6. **To Be Read**: Add books to your queue from any book card or detail page and drag to reorder; the Want to Read tab lists the books you've flagged, with their covers and blurbs
7. **Recommendations**: Get reading suggestions based on your library and reading history
8. **Price Watch**: Paste an Amazon Kindle URL to get notified via Pushover when its price drops
