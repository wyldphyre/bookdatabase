"""Logging for the whole app, set up once at startup.

Every module logs through its own logger, `logging.getLogger(__name__)`, and
this sends them all to two places:

- stderr, which `docker logs` keeps and the dev server shows in the terminal;
- instance/logs/app.log, which outlives the container (instance/ is a mounted
  volume) and rotates at 5MB, keeping the five previous files, so it never
  grows without limit.

LOG_LEVEL sets how much is written: DEBUG, INFO (the default), WARNING or
ERROR. At INFO the log records changes to the library, background jobs and
anything that went wrong; DEBUG adds every page view and every page fetched
from another site.

Each line says what was running when it was written: the request
("POST /books/12/edit") or, outside a request, the thread ("price-watch").
"""
import logging
import logging.handlers
import os
import sys
import threading

from flask import has_request_context, request

LOG_FORMAT = '%(asctime)s %(levelname)-7s %(name)s [%(context)s] %(message)s'
LOG_FILE_NAME = 'app.log'
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUPS = 5

logger = logging.getLogger(__name__)

_configured = False


class _ContextFilter(logging.Filter):
    """Adds %(context)s: the request being served, or the thread's name."""

    def filter(self, record):
        if has_request_context():
            record.context = f'{request.method} {request.path}'
        else:
            record.context = record.threadName
        return True


class _NoDevServerAccessLines(logging.Filter):
    """The dev server logs a line for every request, which would repeat the
    app's own request lines (app.py). Its startup banner still comes through."""

    def filter(self, record):
        return not (record.name == 'werkzeug' and ' - - [' in str(record.msg))


def level_from_env(value):
    """The logging level a LOG_LEVEL value names, or None if it names none."""
    level = logging.getLevelNamesMapping().get((value or '').strip().upper())
    return level if isinstance(level, int) and level > logging.NOTSET else None


def _log_uncaught_thread_exception(args):
    if args.exc_type is SystemExit:
        return
    logger.critical('Uncaught exception in thread %s',
                    args.thread.name if args.thread else '?',
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def configure_logging(log_dir):
    """Set up the handlers. Safe to call more than once; only the first counts."""
    global _configured
    if _configured:
        return
    _configured = True

    level_setting = os.environ.get('LOG_LEVEL', 'INFO')
    level = level_from_env(level_setting)

    handlers = [logging.StreamHandler(sys.stderr)]
    file_error = None
    try:
        os.makedirs(log_dir, exist_ok=True)
        # delay: the file is only opened once something is logged, so the dev
        # server's reloader parent, which logs next to nothing, leaves it be.
        handlers.append(logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, LOG_FILE_NAME), maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUPS, encoding='utf-8', delay=True))
    except OSError as e:
        file_error = e

    formatter = logging.Formatter(LOG_FORMAT)
    root = logging.getLogger()
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(_ContextFilter())
        handler.addFilter(_NoDevServerAccessLines())
        root.addHandler(handler)
    root.setLevel(level or logging.INFO)

    # Libraries' own chatter: connection pooling, image decoding
    for name in ('urllib3', 'PIL'):
        logging.getLogger(name).setLevel(logging.WARNING)

    # Background threads catch their own errors, but anything that slips past
    # would otherwise go to bare stderr and never reach the log file.
    threading.excepthook = _log_uncaught_thread_exception

    if level is None:
        logger.warning('LOG_LEVEL %r is not a logging level; using INFO', level_setting)
    if file_error:
        logger.warning('Could not open a log file in %s (%s); logging to stderr only', log_dir, file_error)
