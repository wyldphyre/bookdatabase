"""The logging setup's pieces (logging_config.py), without starting the app.

Run from the repository root:  python -m unittest discover tests
"""
import logging
import unittest

from flask import Flask

from logging_config import _ContextFilter, _NoDevServerAccessLines, level_from_env


def record(name='app', msg='message', args=()):
    return logging.LogRecord(name, logging.INFO, __file__, 1, msg, args, None)


class LevelFromEnvTest(unittest.TestCase):
    def test_names_any_case(self):
        self.assertEqual(level_from_env('debug'), logging.DEBUG)
        self.assertEqual(level_from_env(' WARNING '), logging.WARNING)

    def test_unknown_or_empty_is_none(self):
        for value in ('LOUD', '', None, 'NOTSET'):
            self.assertIsNone(level_from_env(value), value)


class ContextFilterTest(unittest.TestCase):
    def test_request(self):
        app = Flask(__name__)
        rec = record()
        with app.test_request_context('/books/12/edit', method='POST'):
            _ContextFilter().filter(rec)
        self.assertEqual(rec.context, 'POST /books/12/edit')

    def test_background_thread(self):
        rec = record()
        rec.threadName = 'price-watch'
        _ContextFilter().filter(rec)
        self.assertEqual(rec.context, 'price-watch')


class DevServerAccessLinesTest(unittest.TestCase):
    def test_access_lines_dropped_banner_kept(self):
        keep = _NoDevServerAccessLines().filter
        self.assertFalse(keep(record('werkzeug', '127.0.0.1 - - [15/Nov/2026 09:00:00] "%s" %s %s\n')))
        self.assertTrue(keep(record('werkzeug', ' * Running on http://127.0.0.1:5001')))
        self.assertTrue(keep(record('app', 'something - - [with brackets]')))


if __name__ == '__main__':
    unittest.main()
