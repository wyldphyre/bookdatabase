"""A long job over a list of items, run on its own thread and driven from the
System page: started, polled for progress, paused, resumed and stopped.

The series count scan and the author info scan are both one of these; each
supplies only the list of items and what to do with one.
"""
import logging
import threading
import time

from models import db
from scrapers import ScrapeBlockedError

logger = logging.getLogger(__name__)

# Between items, to be polite to the sites being asked
ITEM_DELAY_SECONDS = 2


class BackgroundScan:
    """One scan's state, shared between its thread and the requests polling it.

    The state is what the progress templates render: status (idle, running,
    paused, stopped, complete), progress and total, the item being worked on
    (current_<item_label>), results (one entry per item, keyed by item_label),
    stop_reason, and any counters the scan keeps.
    """

    def __init__(self, title, item_label, counters=()):
        self.title = title                        # for the log: 'Series scan'
        self.item_label = item_label              # an entry's key for its item: 'series'
        self.current_key = f'current_{item_label}'
        self.counters = tuple(counters)
        self._lock = threading.Lock()
        self._state = self._fresh_state('idle')

    def _fresh_state(self, status):
        state = {'status': status, 'progress': 0, 'total': 0, self.current_key: '', 'results': [],
                 'paused': False, 'stop_requested': False, 'stop_reason': ''}
        state.update({name: 0 for name in self.counters})
        return state

    def snapshot(self):
        """A consistent copy of the state, for rendering."""
        with self._lock:
            state = dict(self._state)
            state['results'] = list(self._state['results'])
        return state

    def start(self, app, list_items, process, item_name):
        """Start the scan on its own thread, unless one is already running or
        paused (starting afresh would unpause it and leave two threads going).

        list_items(scan) returns the items, process(scan, item) does one, and
        item_name(item) names it in the results; the first two run inside an
        app context."""
        with self._lock:
            if self._state['status'] in ('running', 'paused'):
                return
            self._state = self._fresh_state('running')
        thread_name = self.title.lower().replace(' ', '-')
        threading.Thread(target=self._run, args=(app, list_items, process, item_name),
                         daemon=True, name=thread_name).start()

    def toggle_pause(self):
        with self._lock:
            if self._state['status'] == 'running':
                self._state.update(paused=True, status='paused')
            elif self._state['status'] == 'paused':
                self._state.update(paused=False, status='running')

    def stop(self):
        with self._lock:
            self._state['stop_requested'] = True

    def add_result(self, name, status, counter=None, **details):
        """Record what happened to one item, adding one to counter if given."""
        with self._lock:
            if counter:
                self._state[counter] += 1
            self._state['results'].append({self.item_label: name, 'status': status, **details})

    def _carry_on(self):
        """Wait out a pause; False once a stop has been asked for."""
        while True:
            with self._lock:
                if self._state['stop_requested']:
                    return False
                if not self._state['paused']:
                    return True
            time.sleep(0.5)

    def _run(self, app, list_items, process, item_name):
        aborted = False
        try:
            with app.app_context():
                items = list_items(self)
                with self._lock:
                    self._state['total'] = len(items)
                logger.info('%s: %d to check', self.title, len(items))

                for i, item in enumerate(items):
                    if not self._carry_on():
                        break
                    name = item_name(item)
                    with self._lock:
                        self._state[self.current_key] = name
                        self._state['progress'] = i
                    try:
                        process(self, item)
                    except ScrapeBlockedError as e:
                        # The site is turning us away. The rest of the run
                        # would fail the same way, so stop rather than keep
                        # asking.
                        logger.warning('%s: stopping, %s', self.title, e)
                        self.add_result(name, 'blocked', message=str(e))
                        with self._lock:
                            self._state.update(stop_reason=str(e), stop_requested=True)
                        break
                    except Exception as e:
                        # Leave the session usable. A failed write (an
                        # IntegrityError, or SQLite refusing a locked database
                        # while the web side writes) leaves the transaction
                        # poisoned, and the next item would fail on it too.
                        logger.warning('%s: %r failed', self.title, name, exc_info=True)
                        db.session.rollback()
                        self.add_result(name, 'error', message=str(e))
                    time.sleep(ITEM_DELAY_SECONDS)
        except Exception as e:
            # Without this the status would stay 'running', the progress bar
            # frozen and polling forever, and no new scan could be started.
            logger.exception('%s aborted', self.title)
            aborted = True
            self.add_result('(scan aborted)', 'error', message=str(e))
        finally:
            with self._lock:
                # A scan that ended early (aborted, blocked, or stopped) hasn't
                # covered its total. Filling the bar and calling it complete
                # would claim the whole run happened and hide the stop_reason,
                # which only the 'stopped' state shows.
                stopped_early = aborted or self._state['stop_requested']
                if not stopped_early:
                    self._state['progress'] = self._state['total']
                self._state[self.current_key] = ''
                self._state['status'] = 'stopped' if stopped_early else 'complete'
                done, total = self._state['progress'], self._state['total']
            logger.info('%s: %s after %d of %d', self.title,
                        'stopped' if stopped_early else 'finished', done, total)
