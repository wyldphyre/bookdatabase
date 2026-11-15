"""Amazon links: finding the ASIN, cleaning the link, and storing the ASIN in
its own column (database.fill_amazon_asins).

Run from the repository root:  python -m unittest discover tests
"""
import sqlite3
import unittest

from database import fill_amazon_asins
from scrapers import book_page_parser
from utils import amazon_asin, clean_external_url


class AmazonLinkTest(unittest.TestCase):
    LINKS = [
        'https://www.amazon.com.au/dp/B0HFBPRB78',
        'https://www.amazon.com.au/Suncatcher-Mariah-Birch/dp/b0hfbprb78/ref=sr_1_1?k=x',
        'https://www.amazon.com.au/dp/product/B0HFBPRB78',
        'https://www.amazon.com/gp/product/B0HFBPRB78?th=1',
        'https://www.amazon.com/gp/aw/d/B0HFBPRB78',
    ]

    def test_asin_in_every_form(self):
        for url in self.LINKS:
            self.assertEqual(amazon_asin(url), 'B0HFBPRB78', url)

    def test_not_a_product_link(self):
        for url in ('https://www.amazon.com/s?k=suncatcher', 'https://www.amazon.com/dp/B0HFBPRB78X',
                    'https://www.amazon.com/dp/product/', '', None):
            self.assertIsNone(amazon_asin(url), url)

    def test_links_cleaned_to_the_standard_form(self):
        self.assertEqual(clean_external_url(self.LINKS[2]), 'https://www.amazon.com.au/dp/B0HFBPRB78')
        self.assertEqual(clean_external_url(self.LINKS[4]), 'https://www.amazon.com/dp/B0HFBPRB78')

    def test_every_form_is_a_capturable_book_page(self):
        for url in self.LINKS:
            self.assertEqual(book_page_parser(url)[0], 'amazon', url)


class FillAmazonAsinsTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE book (id INTEGER PRIMARY KEY, amazon_url TEXT, amazon_asin TEXT);
            CREATE TABLE price_watch (id INTEGER PRIMARY KEY, amazon_url TEXT UNIQUE, asin TEXT);
        """)
        self.cursor = self.db.cursor()

    def rows(self, table):
        return self.cursor.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()

    def test_books(self):
        self.cursor.executemany('INSERT INTO book VALUES (?, ?, ?)', [
            (1, 'https://www.amazon.com.au/dp/product/B0FLWLTHH5', None),
            (2, 'https://www.amazon.com/dp/B0HFBPRB78', None),
            (3, None, 'B0STALE000'),           # link removed, ASIN left behind
            (4, 'https://www.amazon.com/s?k=x', None),
        ])
        fill_amazon_asins(self.cursor)
        self.assertEqual(self.rows('book'), [
            (1, 'https://www.amazon.com.au/dp/B0FLWLTHH5', 'B0FLWLTHH5'),
            (2, 'https://www.amazon.com/dp/B0HFBPRB78', 'B0HFBPRB78'),
            (3, None, None),
            (4, 'https://www.amazon.com/s', None),
        ])
        fill_amazon_asins(self.cursor)           # repeatable
        self.assertEqual(self.rows('book')[0][2], 'B0FLWLTHH5')

    def test_watch_links_stay_unique(self):
        self.cursor.executemany('INSERT INTO price_watch VALUES (?, ?, ?)', [
            (1, 'https://www.amazon.com/dp/B0HFBPRB78', None),
            (2, 'https://www.amazon.com/dp/product/B0HFBPRB78', None),   # cleans to watch 1's link
            (3, 'https://www.amazon.com/gp/product/B0FB1ZDTVK', None),
        ])
        fill_amazon_asins(self.cursor)
        self.assertEqual(self.rows('price_watch'), [
            (1, 'https://www.amazon.com/dp/B0HFBPRB78', 'B0HFBPRB78'),
            (2, 'https://www.amazon.com/dp/product/B0HFBPRB78', 'B0HFBPRB78'),
            (3, 'https://www.amazon.com/dp/B0FB1ZDTVK', 'B0FB1ZDTVK'),
        ])


if __name__ == '__main__':
    unittest.main()
