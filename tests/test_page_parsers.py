"""Parsers against saved copies of real book pages.

Each fixture is a page saved from the browser (trimmed of scripts, styles and
SVG, which the parsers never read). When a site changes its layout and a
capture or import starts coming back wrong, save the new page, add it here and
fix the parser until this passes.

Run from the repository root:  python -m unittest discover tests
"""
import gzip
import os
import unittest

from bs4 import BeautifulSoup

from capture import _url_key
from scrapers import book_page_parser, parse_amazon, parse_goodreads, parse_kobo
from utils import amazon_asin

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures', 'pages')


def parse(parser, fixture, url):
    with gzip.open(os.path.join(FIXTURES, fixture), 'rt', encoding='utf-8') as f:
        return parser(BeautifulSoup(f.read(), 'html.parser'), url)


class GoodreadsPageTest(unittest.TestCase):
    URL = 'https://www.goodreads.com/book/show/240019758-moss-d-in-space'

    def setUp(self):
        self.data = parse(parse_goodreads, 'goodreads-mossd-in-space.html.gz', self.URL)

    def test_book_details(self):
        self.assertEqual(self.data['title'], "Moss'd in Space")
        self.assertEqual(self.data['authors'], ['Rebecca Thorne'])
        self.assertEqual(self.data['page_count'], 368)
        self.assertTrue(self.data['description'].startswith('USA Today Bestselling author Rebecca Thorne'))
        self.assertEqual(self.data['goodreads_url'], self.URL)
        # The book's own cover, not another book's card further down the page
        self.assertTrue(self.data['cover_url'].endswith('240019758.jpg'))

    def test_series(self):
        self.assertEqual(self.data['series_name'], "Moss'd in Space")
        self.assertEqual(self.data['series_number'], 1.0)
        self.assertEqual(self.data['series_url'], 'https://www.goodreads.com/series/425721-moss-d-in-space')

    def test_genres_are_the_books_own(self):
        # Not the site's Browse menu, which once added Comics, Graphic Novels…
        self.assertEqual(self.data['genres'],
                         ['Science Fiction', 'Cozy', 'Romance', 'Fantasy', 'LGBT', 'Queer', 'Fiction'])


class AmazonPageTest(unittest.TestCase):
    URL = 'https://www.amazon.com.au/dp/B0HFBPRB78'

    def setUp(self):
        self.data = parse(parse_amazon, 'amazon-suncatcher-hardcover.html.gz', self.URL)

    def test_book_details(self):
        self.assertEqual(self.data['authors'], ['Mariah Rae Birch'])
        self.assertEqual(self.data['page_count'], 308)
        self.assertTrue(self.data['description'].startswith('A sapphic star-crossed love story'))
        self.assertIn('715tswcJm8L', self.data['cover_url'])

    def test_instalment_moves_from_title_to_series_number(self):
        # Amazon titles this "Suncatcher (1)"
        self.assertEqual(self.data['title'], 'Suncatcher')
        self.assertEqual(self.data['series_number'], 1.0)

    def test_format_and_price_on_current_layout(self):
        self.assertEqual(self.data['detected_format'], 'Hardcover')
        self.assertEqual(self.data['price'], 59.30)


class AmazonPhonePageTest(unittest.TestCase):
    """Amazon's phone layout: #title, #main-image, a collapsed byline, the
    description in a tab, and print length only in the rich product info."""

    def test_kindle_book_in_a_series(self):
        url = 'https://www.amazon.com.au/Suncatcher-Sapphic-Western-Wanderers-Book-ebook/dp/B0HCM26DM8'
        data = parse(parse_amazon, 'amazon-suncatcher-kindle-phone.html.gz', url)
        self.assertEqual(data['title'], 'Suncatcher')
        self.assertEqual(data['authors'], ['Mariah Rae Birch'])
        self.assertEqual(data['series_name'], 'Sapphic Western Wanderers')
        self.assertEqual(data['series_number'], 1.0)
        self.assertEqual(data['page_count'], 361)
        self.assertTrue(data['description'].startswith('A sapphic star-crossed love story'))
        self.assertIn('81DorDmWE1L', data['cover_url'])
        self.assertEqual(data['detected_format'], 'Kindle')
        self.assertEqual(data['price'], 6.99)

    def test_several_authors_and_kindle_unlimited_price(self):
        url = 'https://www.amazon.com.au/Master-Swords-Omnibus-Books-1-3-ebook/dp/B0FJBNLCJD'
        data = parse(parse_amazon, 'amazon-master-of-swords-kindle-phone.html.gz', url)
        self.assertEqual(data['title'], 'Master of Swords Omnibus: Books 1-3')
        # The byline shows "Marcus Sloss and 1 more"; both are in its secondary view
        self.assertEqual(data['authors'], ['Marcus Sloss', 'Hunter Dane'])
        self.assertEqual(data['page_count'], 1089)
        self.assertTrue(data['description'].startswith('The complete series in a single offering!'))
        # "$0.00 or $3.99 to buy": the buy price, not the Kindle Unlimited one
        self.assertEqual(data['price'], 3.99)
        self.assertNotIn('series_name', data)


class AmazonTitleAndFormatTest(unittest.TestCase):
    """Small hand-made pages for title clean-up and format precedence."""

    def amazon(self, body):
        return parse_amazon(BeautifulSoup(f'<html><body>{body}</body></html>', 'html.parser'),
                            'https://www.amazon.com/dp/B000000000')

    def title(self, title):
        return self.amazon(f'<span id="productTitle">{title}</span>')

    def test_instalment_in_title(self):
        for title, expected in [
            ('Suncatcher (1)', ('Suncatcher', None, 1.0)),
            ('Title (Some Series Book 2)', ('Title', 'Some Series', 2.0)),
            ('Spellmonger (Spellmonger Series: Book 1)', ('Spellmonger', 'Spellmonger Series', 1.0)),
            ('Title (Some Series, #3)', ('Title', 'Some Series', 3.0)),
            ('Station Eleven (Book 1)', ('Station Eleven', None, 1.0)),
        ]:
            data = self.title(title)
            self.assertEqual((data['title'], data.get('series_name'), data.get('series_number')), expected, title)

    def test_year_or_edition_stays_in_title(self):
        data = self.title('Fahrenheit 451 (2003)')
        self.assertEqual(data['title'], 'Fahrenheit 451 (2003)')
        self.assertNotIn('series_number', data)

    def test_selected_format_beats_byline(self):
        data = self.amazon('<span id="productTitle">T</span>'
                           '<span id="inline-twister-expanded-dimension-text-media_format">Paperback</span>'
                           '<div id="bylineInfo">Author (Author) See all formats: Kindle Edition</div>')
        self.assertEqual(data['detected_format'], 'Paperback')

    def test_price_to_pay_beats_an_earlier_promo_price(self):
        data = self.amazon('<span id="productTitle">T</span>'
                           '<div class="promo"><span class="a-price"><span class="a-offscreen">$0.99</span></span></div>'
                           '<span id="apex-pricetopay-accessibility-label">$12.50</span>')
        self.assertEqual(data['price'], 12.50)

    def test_byline_format_when_nothing_selected(self):
        data = self.amazon('<span id="productTitle">T</span><div id="bylineInfo">Format: Hardcover</div>')
        self.assertEqual(data['detected_format'], 'Hardcover')


class KoboPageTest(unittest.TestCase):
    def test_ebook(self):
        url = 'https://www.kobo.com/au/en/ebook/moss-d-in-space-6'
        data = parse(parse_kobo, 'kobo-mossd-in-space.html.gz', url)
        self.assertEqual(data['title'], "Moss'd In Space")
        self.assertEqual(data['authors'], ['Rebecca Thorne'])
        self.assertEqual(data['page_count'], 368)
        self.assertEqual(data['price'], 12.99)
        self.assertNotIn('list_price', data)
        self.assertEqual(data['detected_format'], 'Kobo')
        self.assertEqual(data['kobo_url'], url)
        self.assertTrue(data['cover_url'].startswith('https://cdn.kobo.com/book-images/'))
        self.assertNotIn('series_name', data)

    def test_series_and_sale_price(self):
        data = parse(parse_kobo, 'kobo-suncatcher.html.gz', 'https://www.kobo.com/au/en/ebook/suncatcher-9')
        self.assertEqual(data['series_name'], 'Sapphic Western Wanderers')
        self.assertEqual(data['series_number'], 1.0)
        self.assertEqual(data['series_url'], 'https://www.kobo.com/au/en/series/sapphic-western-wanderers')
        self.assertEqual(data['price'], 7.02)
        self.assertEqual(data['list_price'], 7.80)
        self.assertTrue(data['description'].startswith('A sapphic star-crossed love story'))

    def test_comic(self):
        data = parse(parse_kobo, 'kobo-lilith.html.gz', 'https://www.kobo.com/au/en/ebook/lilith-vol-1-1')
        self.assertEqual(data['title'], 'Lilith Vol. 1')
        self.assertEqual(data['authors'], ['Corin Howell'])
        self.assertEqual(data['page_count'], 128)

    def test_no_details_block_means_no_authors_from_elsewhere(self):
        html = ('<h1 data-testid="book-title">T</h1>'
                '<section class="recommendations"><a href="/au/en/author/someone-else">Someone Else</a>'
                '<a href="/au/en/series/other-series">Other Series</a></section>')
        data = parse_kobo(BeautifulSoup(html, 'html.parser'), 'https://www.kobo.com/au/en/ebook/t')
        self.assertNotIn('authors', data)
        self.assertNotIn('series_name', data)

    def test_audiobook_format(self):
        data = parse(parse_kobo, 'kobo-mossd-in-space.html.gz', 'https://www.kobo.com/au/en/audiobook/moss-d-in-space-5')
        self.assertEqual(data['detected_format'], 'Audiobook')


class BookPageDetectionTest(unittest.TestCase):
    def test_supported_pages(self):
        for url, source in [
            ('https://www.goodreads.com/book/show/123-title', 'goodreads'),
            ('https://www.goodreads.com/en/book/show/123', 'goodreads'),
            ('https://www.amazon.com.au/Suncatcher-Mariah/dp/B0HFBPRB78/ref=sr_1_1', 'amazon'),
            ('https://www.amazon.com/gp/product/B0HFBPRB78', 'amazon'),
            ('https://www.kobo.com/au/en/ebook/moss-d-in-space-6', 'kobo'),
            ('https://www.kobo.com/au/en/audiobook/moss-d-in-space-5', 'kobo'),
        ]:
            self.assertEqual(book_page_parser(url)[0], source, url)

    def test_everything_else_is_refused(self):
        for url in [
            'https://www.kobo.com/au/en/series/sapphic-western-wanderers',
            'https://www.goodreads.com/author/show/123',
            'https://evil.example/www.goodreads.com/book/show/1',
            'https://www.goodreads.com.evil.example/book/show/1',
            'javascript:alert(1)',
            'file:///etc/passwd',
        ]:
            self.assertEqual(book_page_parser(url), (None, None), url)


class UrlKeyTest(unittest.TestCase):
    def test_same_book_however_linked(self):
        self.assertEqual(_url_key('https://www.goodreads.com/book/show/240019758-moss-d-in-space'),
                         _url_key('https://www.goodreads.com/en/book/show/240019758'))
        self.assertEqual(_url_key('https://www.amazon.com.au/Suncatcher/dp/b0hfbprb78/ref=x'),
                         _url_key('https://www.amazon.com.au/dp/B0HFBPRB78'))
        self.assertEqual(_url_key('https://www.kobo.com/au/en/ebook/suncatcher-9'),
                         _url_key('https://www.kobo.com/us/en/ebook/suncatcher-9?sId=1'))

    def test_asin_matches_across_amazon_sites(self):
        self.assertEqual(amazon_asin('https://www.amazon.com/dp/b0hfbprb78'),
                         amazon_asin('https://www.amazon.com.au/Suncatcher/dp/B0HFBPRB78/ref=x'))
        self.assertEqual(amazon_asin('https://www.amazon.com/gp/product/B0HFBPRB78?th=1'), 'B0HFBPRB78')
        self.assertIsNone(amazon_asin('https://www.amazon.com/s?k=suncatcher'))
        self.assertIsNone(amazon_asin(None))

    def test_different_books_differ(self):
        self.assertNotEqual(_url_key('https://www.goodreads.com/book/show/1'),
                            _url_key('https://www.goodreads.com/book/show/2'))


if __name__ == '__main__':
    unittest.main()
