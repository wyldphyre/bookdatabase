"""How soon bought books get read (stats.purchase_to_read_stats).

Run from the repository root:  python -m unittest discover tests
"""
import unittest
from datetime import datetime

from stats import first_read_after_purchase, purchase_to_read_stats


def d(text):
    return datetime.strptime(text, '%Y-%m-%d')


class FirstReadAfterPurchaseTest(unittest.TestCase):
    def test_earliest_read_of_this_copy(self):
        reads = [(d('2024-06-01'), d('2024-06-10')), (d('2024-02-01'), d('2024-02-05'))]
        self.assertEqual(first_read_after_purchase(d('2024-01-15'), reads),
                         (d('2024-02-01'), d('2024-02-05')))

    def test_read_of_an_earlier_copy_is_ignored(self):
        reads = [(d('2015-03-01'), d('2015-03-09')), (d('2024-05-01'), d('2024-05-04'))]
        self.assertEqual(first_read_after_purchase(d('2024-01-15'), reads),
                         (d('2024-05-01'), d('2024-05-04')))

    def test_dates_a_few_days_out_still_count(self):
        reads = [(d('2024-01-13'), d('2024-01-14'))]
        self.assertEqual(first_read_after_purchase(d('2024-01-15'), reads), reads[0])

    def test_never_read(self):
        self.assertIsNone(first_read_after_purchase(d('2024-01-15'), []))
        self.assertIsNone(first_read_after_purchase(d('2024-01-15'), [(None, None)]))


class PurchaseToReadStatsTest(unittest.TestCase):
    BOOKS = {
        1: (d('2023-03-01'), [(d('2023-03-02'), d('2023-03-10'))]),   # straight away
        2: (d('2023-11-20'), [(d('2023-12-28'), d('2024-01-03'))]),   # finished the next year
        3: (d('2023-05-01'), []),                                     # not read
        4: (d('2024-01-10'), [(None, d('2024-09-01'))]),              # no start date
        5: (d('2024-02-01'), [(d('2024-01-30'), d('2024-02-02'))]),   # started before the recorded purchase
        6: (d('2020-06-01'), []),                                     # before any read was recorded
        7: (d('2024-03-01'), [(d('2024-03-15'), d('2024-03-20'))]),   # a fortnight later
    }

    def setUp(self):
        self.stats = purchase_to_read_stats(self.BOOKS, today=d('2024-11-21'))

    def test_same_year(self):
        self.assertEqual(self.stats['same_year'], {
            2023: {'bought': 3, 'read': 1, 'percent': 33},
            2024: {'bought': 3, 'read': 3, 'percent': 100},
        })
        self.assertEqual(self.stats['current_year'], 2024)

    def test_gaps(self):
        buckets = self.stats['gap_buckets']
        self.assertEqual(buckets['Within a week'], 2)     # books 1 and 5
        self.assertEqual(buckets['1–4 weeks'], 1)         # book 7
        self.assertEqual(buckets['1–3 months'], 1)        # book 2, from its start date
        self.assertEqual(buckets['6–12 months'], 1)       # book 4, from its finish date
        self.assertEqual(sum(buckets.values()), 5)
        self.assertEqual((self.stats['read'], self.stats['unread']), (5, 2))

    def test_median(self):
        # Gaps 0, 1, 14, 38 and 235 days: the median is 14 days
        self.assertAlmostEqual(self.stats['median_months'] * 365.25 / 12, 14)

    def test_nothing_bought(self):
        stats = purchase_to_read_stats({})
        self.assertEqual(stats['same_year'], {})
        self.assertIsNone(stats['median_months'])
        self.assertEqual(stats['read'], 0)


if __name__ == '__main__':
    unittest.main()
