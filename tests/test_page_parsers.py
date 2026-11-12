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

    def test_different_books_differ(self):
        self.assertNotEqual(_url_key('https://www.goodreads.com/book/show/1'),
                            _url_key('https://www.goodreads.com/book/show/2'))


if __name__ == '__main__':
    unittest.main()
