"""Books not out yet: saving a release date, the release notifications, and
how pre-orders count in the reading statistics.

Runs against a throwaway in-memory database, with only the books blueprint,
so it never touches instance/books.db or starts the background schedulers.

Run from the repository root:  python -m unittest discover tests
"""
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from flask import Flask

from models import db, Book, BookFormat
import release_reminders
from stats import owned_purchases_with_reads

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODAY = date.today()


def make_app():
    app = Flask('app', root_path=REPO)
    app.config.update(SQLALCHEMY_DATABASE_URI='sqlite://', SECRET_KEY='test', TESTING=True,
                      UPLOAD_FOLDER=tempfile.mkdtemp())
    db.init_app(app)
    from routes.books import books_bp
    app.register_blueprint(books_bp)
    return app


class ReleaseDateTestCase(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.kindle = BookFormat(name='Kindle')
        db.session.add(self.kindle)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        self.ctx.pop()

    def add_book(self, title, release_date=None, owned=True, **fields):
        book = Book(title=title, format_id=self.kindle.id, owned=owned, release_date=release_date, **fields)
        db.session.add(book)
        db.session.commit()
        return book


class SavingReleaseDateTest(ReleaseDateTestCase):
    def post_book(self, url, release_date, **fields):
        form = {'title': 'Tales from the Territory', 'format_id': str(self.kindle.id),
                'release_date': release_date, **fields}
        return self.app.test_client().post(url, data=form)

    def test_new_book_with_a_date_to_come(self):
        due = TODAY + timedelta(days=10)
        self.post_book('/books/new', due.isoformat(), owned='on')
        book = Book.query.one()
        self.assertEqual(book.release_date, due)
        self.assertTrue(book.is_preorder)
        self.assertEqual(book.days_until_release, 10)

    def test_a_new_date_must_be_after_today(self):
        for day in (TODAY, TODAY - timedelta(days=3)):
            self.post_book('/books/new', day.isoformat())
            self.assertEqual(Book.query.count(), 0, day)

    def test_nonsense_date_is_refused(self):
        self.post_book('/books/new', '2026-02-31')
        self.assertEqual(Book.query.count(), 0)

    def test_released_book_keeps_its_past_date_on_edit(self):
        released = TODAY - timedelta(days=20)
        book = self.add_book('Out already', released, release_notified_at=datetime.now())
        self.post_book(f'/books/{book.id}/edit', released.isoformat(), owned='on')
        db.session.refresh(book)
        self.assertEqual(book.title, 'Tales from the Territory')   # the edit was saved
        self.assertEqual(book.release_date, released)
        self.assertIsNotNone(book.release_notified_at)

    def test_changing_the_date_announces_it_again(self):
        book = self.add_book('Delayed', TODAY + timedelta(days=2), release_notified_at=datetime.now())
        later = TODAY + timedelta(days=30)
        self.post_book(f'/books/{book.id}/edit', later.isoformat(), owned='on')
        db.session.refresh(book)
        self.assertEqual(book.release_date, later)
        self.assertIsNone(book.release_notified_at)

    def test_clearing_the_date(self):
        book = self.add_book('No date now', TODAY + timedelta(days=2))
        self.post_book(f'/books/{book.id}/edit', '', owned='on')
        db.session.refresh(book)
        self.assertIsNone(book.release_date)


class ReleaseNotificationTest(ReleaseDateTestCase):
    MORNING = datetime.combine(TODAY, datetime.min.time()).replace(hour=10)

    def setUp(self):
        super().setUp()
        self.waiting = self.add_book('Waiting On', TODAY, owned=False,
                                     kobo_url='https://www.kobo.com/au/en/ebook/waiting-on')
        self.preorder = self.add_book('Pre-ordered', TODAY - timedelta(days=1),
                                      amazon_url='https://www.amazon.com.au/dp/B0GQZ7Y8DM')
        self.add_book('Long ago', TODAY - timedelta(days=10), owned=False)        # missed: not announced late
        self.add_book('Not yet', TODAY + timedelta(days=1))
        self.add_book('Told already', TODAY, release_notified_at=datetime.now())
        self.add_book('No date')

    def notify(self, now=None, sent=True):
        with mock.patch.object(release_reminders, 'send_pushover_notification', return_value=sent) as send:
            count = release_reminders.notify_releases(self.app, now=now or self.MORNING)
        return count, send

    def test_announces_each_release_once(self):
        count, send = self.notify()
        self.assertEqual(count, 2)
        calls = {c.kwargs['title']: c.kwargs for c in send.call_args_list}
        self.assertEqual(calls['Out today: Waiting On']['message'], 'Waiting On is out today, if you want to buy it.')
        self.assertEqual(calls['Out today: Waiting On']['url'], 'https://www.kobo.com/au/en/ebook/waiting-on')
        self.assertEqual(calls['Out now: Pre-ordered']['message'], 'Your pre-order of Pre-ordered is out now.')
        self.assertEqual(calls['Out now: Pre-ordered']['url'], 'https://www.amazon.com.au/dp/B0GQZ7Y8DM')

        count, send = self.notify()
        self.assertEqual(count, 0)
        send.assert_not_called()

    def test_not_before_the_morning(self):
        count, send = self.notify(now=self.MORNING.replace(hour=release_reminders.NOTIFY_FROM_HOUR - 1))
        self.assertEqual(count, 0)
        send.assert_not_called()

    def test_unsent_notification_is_retried(self):
        count, _ = self.notify(sent=False)
        self.assertEqual(count, 0)
        self.assertIsNone(db.session.get(Book, self.waiting.id).release_notified_at)
        count, _ = self.notify()
        self.assertEqual(count, 2)


class PreorderStatisticsTest(ReleaseDateTestCase):
    def test_preorders_count_from_their_release(self):
        bought = datetime.combine(TODAY - timedelta(days=60), datetime.min.time())
        out = TODAY - timedelta(days=5)
        released = self.add_book('Came out', out, date_purchased=bought)
        coming = self.add_book('Still to come', TODAY + timedelta(days=5), date_purchased=bought)
        ordinary = self.add_book('Ordinary', date_purchased=bought)

        books = owned_purchases_with_reads()
        self.assertNotIn(coming.id, books)                 # can't be read yet
        self.assertEqual(books[released.id][0], out)       # available from release day
        self.assertEqual(books[ordinary.id][0], bought)


if __name__ == '__main__':
    unittest.main()
