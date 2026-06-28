import re
import requests as http_requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse


def fetch_page(url):
    """Fetch a page with appropriate headers."""
    # Parse the URL to get the host for Referer header
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Cache-Control': 'max-age=0',
        'Referer': base_url,
    }
    response = http_requests.get(url, headers=headers, timeout=15, allow_redirects=True)
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
        data['cover_url'] = img_el.get('src') or img_el.get('data-a-dynamic-image', '').split('"')[1] if '"' in img_el.get('data-a-dynamic-image', '') else None

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
    except Exception:
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
    except Exception:
        pass

    return None
