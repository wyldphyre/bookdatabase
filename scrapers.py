import re
import logging
import time
import threading
import requests as http_requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin


# Worth refreshing every so often: claiming a long-obsolete browser is one of
# the cheaper signals sites use to pick out automated traffic. This is the
# "reduced" form modern Chrome sends, where the minor version parts are frozen.
BROWSER_USER_AGENT = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36')

# Smallest gap allowed between two requests to the same host.
#
# The batch scanners already paced themselves, but one-off lookups didn't:
# importing a book and then fetching its tags fired several requests at
# goodreads.com within seconds, which is the shape rate-based bot rules watch
# for. Pacing every request through one place gives ad-hoc lookups the same
# courtesy, and costs nothing when the caller has already waited.
MIN_REQUEST_INTERVAL_SECONDS = 2.0

_last_request_at = {}          # host -> time.monotonic() of its last request
_host_locks = {}               # host -> Lock serialising that host's callers
_host_locks_guard = threading.Lock()


def _host_lock(host):
    with _host_locks_guard:
        lock = _host_locks.get(host)
        if lock is None:
            lock = _host_locks[host] = threading.Lock()
        return lock


def _throttle(host):
    """Block until this host may be contacted again.

    The wait happens while holding that host's lock, so simultaneous callers
    (a background scan and a click, say) queue up instead of all deciding at
    once that enough time has passed. Locking per host means a slow crawl of
    one site doesn't hold up another."""
    with _host_lock(host):
        last = _last_request_at.get(host)
        if last is not None:
            wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        _last_request_at[host] = time.monotonic()


class ScrapeBlockedError(Exception):
    """The site served an anti-bot challenge or rate-limit response instead of
    the page we asked for.

    Worth distinguishing from an ordinary parse failure: nothing is wrong with
    the URL or our selectors, and it is usually transient — the same request
    typically succeeds again a few minutes later."""


# Tokens unique to challenge/captcha interstitials, safe to match anywhere.
_BLOCK_TOKENS = (
    'awswafcookiedomainlist',       # AWS WAF (Goodreads and Amazon are both behind it)
    'aws-waf-token',
    'cf-browser-verification',      # Cloudflare
    'cf_chl_opt',
    '/errors/validatecaptcha',      # Amazon's captcha wall
)

# Weaker phrases that do appear in ordinary page text, so they only count when
# the body is far too small to be a real book/search page.
_BLOCK_PHRASES = ('just a moment', 'enable javascript and cookies', 'are you a robot',
                  'type the characters you see', 'unusual traffic')
_CHALLENGE_MAX_BYTES = 6000

# Statuses sites use to turn away automated traffic (as opposed to a genuine
# 404 for a book that doesn't exist).
_BLOCK_STATUSES = {403, 429, 503}


def _detect_block(response, host):
    """Raise ScrapeBlockedError if this response is a challenge/refusal rather
    than the page. Returns None otherwise."""
    if response.status_code in _BLOCK_STATUSES:
        raise ScrapeBlockedError(
            f'{host} refused the request (HTTP {response.status_code}) — it is rate-limiting '
            f'or blocking automated requests. This is usually temporary; try again later.')

    # A challenge page is served with a 2xx, so the status alone won't reveal it.
    body = response.text[:_CHALLENGE_MAX_BYTES].lower()
    hit = next((t for t in _BLOCK_TOKENS if t in body), None)
    if hit is None and len(response.text) < _CHALLENGE_MAX_BYTES:
        hit = next((p for p in _BLOCK_PHRASES if p in body), None)
    if hit:
        raise ScrapeBlockedError(
            f'{host} returned an anti-bot challenge instead of the page — it is temporarily '
            f'blocking automated requests. This is usually temporary; try again later.')


def fetch_page(url):
    """Fetch a page with appropriate headers.

    Raises ScrapeBlockedError when the site serves a bot challenge or refuses
    the request, so callers can say so rather than reporting a parse failure."""
    parsed = urlparse(url)

    # These describe a plain top-level navigation — someone opening the URL
    # directly, with no page linking to it. Note there is deliberately no
    # Referer: 'Sec-Fetch-Site: none' means exactly "no initiator", so sending
    # one alongside it is a combination no real browser produces, and
    # self-contradictory headers are themselves a bot signal.
    headers = {
        'User-Agent': BROWSER_USER_AGENT,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Cache-Control': 'max-age=0',
    }
    _throttle(parsed.netloc)
    response = http_requests.get(url, headers=headers, timeout=15, allow_redirects=True)
    _detect_block(response, parsed.netloc)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def get_text_with_linebreaks(element):
    """Extract text from HTML element preserving paragraph breaks."""
    if element is None:
        return None

    # Replace block-level elements with newlines
    for br in element.find_all('br'):
        br.replace_with('\n')
    for p in element.find_all('p'):
        p.insert_before('\n\n')
        p.unwrap()

    # Get text and clean up
    text = element.get_text()
    # Normalize whitespace within lines but preserve line breaks
    lines = text.split('\n')
    lines = [' '.join(line.split()) for line in lines]
    text = '\n'.join(lines)
    # Remove excessive blank lines
    while '\n\n\n' in text:
        text = text.replace('\n\n\n', '\n\n')
    return text.strip()


def scrape_amazon(url):
    """Scrape book data from Amazon."""
    soup = fetch_page(url)

    data = {}

    # Title
    title_el = soup.select_one('#productTitle, #ebooksProductTitle')
    if title_el:
        data['title'] = title_el.get_text(strip=True)

    # Authors (get all, deduplicate while preserving order)
    author_els = soup.select('#bylineInfo .author a, .author a, .contributorNameID')
    if author_els:
        seen = set()
        authors = []
        for el in author_els:
            name = el.get_text(strip=True)
            if name and name not in seen:
                seen.add(name)
                authors.append(name)
        if authors:
            data['authors'] = authors

    # Description - try multiple selectors as Amazon's structure varies by book type
    desc_el = soup.select_one(
        '#bookDescription_feature_div .a-expander-content, '
        '#bookDescription_feature_div .a-expander-partial-collapse-content, '
        '#productDescription, '
        '#bookDescription_feature_div noscript, '
        '#bookDescription_feature_div'
    )
    if desc_el:
        # Remove "Read more" / "Read less" UI controls before extracting text
        to_remove = [el for el in desc_el.find_all(True)
                     if el.get_text(strip=True).lower() in ('read more', 'read less')]
        for el in to_remove:
            el.decompose()
        data['description'] = get_text_with_linebreaks(desc_el)

    # Cover image
    img_el = soup.select_one('#imgBlkFront, #ebooksImgBlkFront, #landingImage')
    if img_el:
        cover_url = img_el.get('src')
        if not cover_url:
            dynamic_image = img_el.get('data-a-dynamic-image', '')
            if '"' in dynamic_image:
                cover_url = dynamic_image.split('"')[1]
        data['cover_url'] = cover_url

    # Page count
    details = soup.select('#detailBullets_feature_div li, #productDetailsTable .content li')
    for detail in details:
        text = detail.get_text()
        if 'pages' in text.lower():
            match = re.search(r'(\d+)\s*pages', text, re.IGNORECASE)
            if match:
                data['page_count'] = int(match.group(1))
                break

    # Series info from title or breadcrumb
    series_el = soup.select_one('#seriesBulletWidget_feature_div a')
    if series_el:
        # The href is the series' own page — the cheapest way to learn a
        # series' Amazon URL when it hasn't got one recorded yet.
        series_href = series_el.get('href')
        if series_href:
            data['series_url'] = urljoin(url, series_href).split('?')[0]
        series_text = series_el.get_text(strip=True)
        # Handle "Book 1 of 16: The Good Guys" → series_name="The Good Guys", series_number=1
        m = re.match(r'^Book\s+(\d+(?:\.\d+)?)\s+of\s+\d+\s*:\s*(.+)$', series_text, re.IGNORECASE)
        if m:
            data['series_number'] = float(m.group(1))
            data['series_name'] = m.group(2).strip()
        else:
            data['series_name'] = series_text

    data['amazon_url'] = url

    # Detect Kindle format from the selected format swatch or ebook-specific page layout
    format_els = soup.select(
        '#variation_format_name .selection, '
        '#tmmSwatches .a-button-selected .slot-title, '
        '#tmmSwatches .a-button-selected'
    )
    if any('kindle' in el.get_text(strip=True).lower() for el in format_els):
        data['detected_format'] = 'Kindle'
    elif soup.select_one('#ebooksProductTitle'):
        data['detected_format'] = 'Kindle'

    # Price - read it from the Kindle format swatch button itself
    # ("Kindle AUD 0.00 or AUD 7.24 to buy"). This is the one element that's
    # unambiguous about which price belongs to the plain Kindle edition - the
    # page often also shows an unrelated bundle/promo price elsewhere (e.g. an
    # Audible add-on deal) that a generic price selector would pick up instead.
    # When there's a Kindle Unlimited "or X to buy" pairing, the buy price is
    # always the last price-like value in the swatch text.
    price_text = None
    swatch = soup.select_one('#tmmSwatches .a-button-selected, #tmm-grid-swatch-KINDLE')
    if swatch:
        matches = re.findall(r'([^\d\s]{0,4})\s*([\d,]+\.\d+)', swatch.get_text(' ', strip=True))
        if matches:
            currency, amount = matches[-1]
            price_text = currency + amount

    if not price_text:
        price_el = soup.select_one(
            '#corePriceDisplay_desktop_feature_div .a-price .a-offscreen, '
            '#kindle-price, '
            '.kindle-price, '
            '.a-price .a-offscreen'
        )
        if price_el:
            price_text = price_el.get_text(strip=True)

    if price_text:
        m = re.match(r'^([^\d]*)([\d,]+\.?\d*)', price_text)
        if m:
            try:
                data['price'] = float(m.group(2).replace(',', ''))
                data['currency'] = m.group(1).strip()
            except ValueError:
                pass

    return data if data.get('title') else None


def scrape_goodreads(url):
    """Scrape book data from Goodreads."""
    soup = fetch_page(url)

    data = {}

    # Title
    title_el = soup.select_one('h1[data-testid="bookTitle"], h1.Text__title1')
    if title_el:
        data['title'] = title_el.get_text(strip=True)

    # Authors (get all, deduplicate while preserving order)
    author_els = soup.select('span[data-testid="name"], a.ContributorLink')
    if author_els:
        seen = set()
        authors = []
        for el in author_els:
            name = el.get_text(strip=True)
            if name and name not in seen:
                seen.add(name)
                authors.append(name)
        if authors:
            data['authors'] = authors

    # Description
    desc_el = soup.select_one(
        'div[data-testid="description"] .Formatted, '
        'div[data-testid="description"], '
        '.BookPageMetadataSection__description .Formatted, '
        '.BookPageMetadataSection__description, '
        'span.Formatted'
    )
    if desc_el:
        data['description'] = get_text_with_linebreaks(desc_el)

    # Cover image
    img_el = soup.select_one('img.ResponsiveImage, div.BookCover img')
    if img_el:
        data['cover_url'] = img_el.get('src')

    # Page count
    pages_el = soup.select_one('p[data-testid="pagesFormat"]')
    if pages_el:
        text = pages_el.get_text()
        match = re.search(r'(\d+)\s*pages', text, re.IGNORECASE)
        if match:
            data['page_count'] = int(match.group(1))

    # Series
    series_el = soup.select_one('h3.Text__italic a, div[data-testid="bookSeries"] a')
    if series_el:
        # The href is the series' own page — the cheapest way to learn a series'
        # Goodreads URL when it hasn't got one recorded yet.
        series_href = series_el.get('href')
        if series_href:
            if series_href.startswith('/'):
                series_href = f'https://www.goodreads.com{series_href}'
            data['series_url'] = series_href
        series_text = series_el.get_text(strip=True)
        # Parse "Series Name #1" format
        match = re.match(r'(.+?)\s*#(\d+(?:\.\d+)?)', series_text)
        if match:
            data['series_name'] = match.group(1).strip()
            data['series_number'] = float(match.group(2))
        else:
            data['series_name'] = series_text

    # Genres/tags
    genre_els = soup.select('span.BookPageMetadataSection__genreButton a, a[href*="/genres/"]')
    if genre_els:
        seen = set()
        genres = []
        for el in genre_els:
            name = el.get_text(strip=True)
            if name and name.lower() not in seen:
                seen.add(name.lower())
                genres.append(name)
        if genres:
            data['genres'] = genres

    # Goodreads URL for author
    data['goodreads_url'] = url

    return data if data.get('title') else None


def scrape_amazon_series(url):
    """Scrape series page from Amazon to get book count."""
    try:
        soup = fetch_page(url)

        # Look for book count in series page
        # Amazon shows "X books" or "X titles" in series
        count_el = soup.select_one('.series-childAsin-count, .seriesHeader span')
        if count_el:
            text = count_el.get_text()
            match = re.search(r'(\d+)\s*(?:book|title|item)', text, re.IGNORECASE)
            if match:
                return int(match.group(1))

        # Alternative: count items in series list
        items = soup.select('.series-childAsin-item, .seriesItem')
        if items:
            return len(items)

        return None
    except ScrapeBlockedError:
        raise
    except Exception:
        return None


def _parse_amazon_series_books(soup, base_url):
    """Every entry listed on an Amazon Kindle series page, in page order.

    Same shape as _parse_series_books so the monitor doesn't care which site a
    series came from."""
    books = []
    for item in soup.select('.series-childAsin-item'):
        link = item.select_one('a.itemBookTitle')
        heading = item.select_one('a.itemBookTitle h3')
        source = heading or link
        title = source.get_text(strip=True) if source else ''
        if not title:
            continue

        # The position sits in its own label, e.g. aria-label="Book 1".
        series_number = None
        position = item.select_one('.itemPositionLabel')
        if position:
            text = position.get('aria-label') or position.get_text(' ', strip=True)
            match = re.search(r'(\d+(?:\.\d+)?)', text)
            if match:
                series_number = float(match.group(1))

        href = link.get('href') if link else None
        if href:
            # Marketplace hosts differ (.com, .com.au), so resolve against the
            # page rather than assuming one. The ref_ tracking query is noise.
            href = urljoin(base_url, href).split('?')[0]

        books.append({'title': title, 'series_number': series_number, 'url': href})
    return books


def _parse_amazon_series_count(soup):
    """The headline "(N book series)" count, if the page states one."""
    for el in soup.select('.a-size-extra-large, #collection-masthead, .series-childAsin-count'):
        match = re.search(r'(\d+)\s*book series', el.get_text(' ', strip=True), re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def scrape_amazon_series_detail(url):
    """An Amazon series page in the same shape as its Goodreads counterpart, so
    either can drive series monitoring.

    Note this reads only what the page ships in its HTML. If Amazon ever holds
    part of a long series back for lazy loading, the tail — where new releases
    live — would be missing, so a stated count higher than the number of
    entries parsed is logged rather than passed over."""
    soup = fetch_page(url)
    books = _parse_amazon_series_books(soup, url)
    count = _parse_amazon_series_count(soup)
    if count is not None and books and count > len(books):
        logging.warning('Amazon series page %s says %d books but only %d are listed in the page',
                        url, count, len(books))
    return {'count': count, 'books': books}


def _parse_series_books(soup):
    """Every entry listed on a Goodreads series page, in page order.

    Returns [{'title', 'series_number', 'url'}]. series_number is None for
    entries Goodreads doesn't number; those are kept, since novellas and
    in-between instalments (#2.5) are still releases worth hearing about."""
    books = []
    for item in soup.select('.listWithDividers__item'):
        link = item.select_one('a[itemprop="url"]')
        name = item.select_one('[itemprop="name"]')
        title = (name or link).get_text(strip=True) if (name or link) else ''
        if not title:
            continue

        # The entry's position sits in its own heading, e.g. "Book 1", "Book 2.5".
        # Anything unparseable (omnibus ranges, "Books 1-3") leaves it unset.
        series_number = None
        heading = item.find('h3')
        if heading:
            match = re.search(r'Book\s+(\d+(?:\.\d+)?)\s*$', heading.get_text(' ', strip=True), re.IGNORECASE)
            if match:
                series_number = float(match.group(1))

        href = link.get('href') if link else None
        if href and href.startswith('/'):
            href = f'https://www.goodreads.com{href}'

        books.append({'title': title, 'series_number': series_number, 'url': href})
    return books


def scrape_goodreads_series_detail(url):
    """Both halves of a series page in one request: the headline work count and
    the list of entries. Used by the series monitor, which needs the titles to
    spot new releases and the count to keep the series record up to date."""
    soup = fetch_page(url)
    return {'count': _parse_series_count(soup), 'books': _parse_series_books(soup)}


def _parse_series_count(soup):
    count_el = soup.select_one('.responsiveSeriesHeader__subtitle, .seriesDesc')
    if count_el:
        match = re.search(r'(\d+)\s*(?:primary\s+)?works?', count_el.get_text(), re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def scrape_goodreads_series(url):
    """Scrape series page from Goodreads to get book count."""
    try:
        soup = fetch_page(url)

        # Goodreads shows "X primary works, Y total" or just lists books
        # Look for the count text
        count_el = soup.select_one('.responsiveSeriesHeader__subtitle, .seriesDesc')
        if count_el:
            text = count_el.get_text()
            # Match "X primary works" or "X works"
            match = re.search(r'(\d+)\s*(?:primary\s+)?works?', text, re.IGNORECASE)
            if match:
                return int(match.group(1))

        # Alternative: count book entries
        items = soup.select('.listWithDividers__item, .bookTitle')
        if items:
            # Filter to only numbered entries (main series books)
            numbered_count = 0
            for item in items:
                num_el = item.select_one('.responsiveBook__seriesNum, .bookMeta')
                if num_el:
                    text = num_el.get_text()
                    if re.search(r'^#?\d+(\.\d+)?$', text.strip()):
                        numbered_count += 1
            if numbered_count > 0:
                return numbered_count
            return len(items)

        return None
    except ScrapeBlockedError:
        raise
    except Exception:
        return None


def search_amazon_for_book(title, author):
    """Search Amazon for a book by title and author, return the first result URL."""
    from urllib.parse import quote_plus

    # Try Amazon AU first, then fall back to Amazon US
    search_query = f"{title} {author}".strip()
    domains = [
        ('amazon.com.au', 'https://www.amazon.com.au/s?k={}&i=digital-text'),
        ('amazon.com', 'https://www.amazon.com/s?k={}&i=digital-text'),
    ]

    for domain, url_template in domains:
        try:
            search_url = url_template.format(quote_plus(search_query))
            soup = fetch_page(search_url)

            # Find the first book result link
            result_link = soup.select_one('div[data-component-type="s-search-result"] h2 a')
            if result_link:
                href = result_link.get('href', '')
                if href:
                    if href.startswith('/'):
                        return f"https://www.{domain}{href}"
                    return href
        except ScrapeBlockedError:
            raise
        except Exception:
            continue

    return None


def search_goodreads_for_book(title, author):
    """Search Goodreads for a book by title and author, return the first result URL."""
    from urllib.parse import quote_plus

    # Patterns that indicate junk listings rather than the actual book
    skip_patterns = ['book only', 'study guide', 'summary', 'workbook', 'analysis', 'notebook']

    def author_matches(known, result):
        """True if known and result authors share at least one significant word."""
        normalize = lambda s: re.sub(r'[^a-z0-9 ]', '', s.lower())
        known_tokens = {t for t in normalize(known).split() if len(t) > 2}
        result_tokens = {t for t in normalize(result).split() if len(t) > 2}
        return bool(known_tokens & result_tokens)

    # Use title only — Goodreads returns 0 results when author is included in the query
    search_url = f"https://www.goodreads.com/search?q={quote_plus(title)}"

    try:
        soup = fetch_page(search_url)

        # Check all result rows, skip junk listings
        rows = soup.select('table.tableList tr')
        for row in rows:
            title_el = row.select_one('a.bookTitle')
            if not title_el:
                continue

            result_title = title_el.get_text(strip=True).lower()

            # Skip junk listings by title
            if any(pattern in result_title for pattern in skip_patterns):
                continue

            # Skip results with 0 ratings (usually spam/junk entries)
            rating_el = row.select_one('span.minirating')
            if rating_el:
                if '0 ratings' in rating_el.get_text(strip=True):
                    continue

            href = title_el.get('href', '')
            if not href:
                continue

            # If we know the author, verify it matches before accepting
            if author:
                author_el = row.select_one('a.authorName')
                result_author = author_el.get_text(strip=True) if author_el else ''
                if not author_matches(author, result_author):
                    continue

            if href.startswith('/'):
                return f"https://www.goodreads.com{href}"
            return href
    except ScrapeBlockedError:
        raise
    except Exception:
        pass

    return None
