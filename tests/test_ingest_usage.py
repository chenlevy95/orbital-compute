"""Deterministic tests for storage consistency and the billing HTTP contract.

Run with: python -m unittest discover -s tests -v
No running API or network access is needed. Storage tests use real temporary
SQLite files; HTTP tests script the transport and clock. Process tests use spawn
so they exercise the same process model on macOS, Linux, and Windows.
"""

import copy
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

import requests

from ingest_usage import BillingClient, IngestionError, ingest, validate_record, windows


NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)


def record(identifier='one', day='2026-09-19', amount=Decimal('12.345678901234567890123456789012345')):
    """Build a valid API record with exact decimal values and a configurable identity.

    The long default amount intentionally exceeds typical float precision so
    storage tests detect accidental rounding.
    """
    return dict(record_id=identifier, customer_id='C001', usage_date=day, sku='gpu',
                quantity=Decimal('1.00'), unit='gpu_hour', amount_usd=amount,
                status='final', updated_at=day + 'T23:00:00Z')


class Source:
    """In-memory replacement for BillingClient used to test storage without HTTP."""
    def __init__(self, records=(), crash=False):
        """Keep the visible records, requested ranges, and optional failure switch."""
        self.records = records
        self.crash = crash
        self.ranges = []
        self.counts = {}

    def pages(self, start, end):
        """Yield records inside the requested window, then optionally fail.

        Raising after yield lets tests interrupt a run after it has written rows
        but before it can commit them.
        """
        self.ranges.append((start, end))
        yield [r for r in self.records if start.isoformat() <= r['usage_date'] <= end.isoformat()]
        if self.crash:
            raise RuntimeError('interrupted')


class Response:
    """Minimal requests.Response substitute; raw text permits malformed JSON tests."""
    def __init__(self, status=200, payload=None, headers=None, text=None):
        """Create a response from a JSON payload or explicitly supplied response text."""
        self.status_code, self.headers = status, headers or {}
        self.text = text if text is not None else json.dumps(payload)

    def close(self):
        """Match the real response interface; the fixture owns no network resources."""
        pass


class Clock:
    """Controllable monotonic clock that records waits without sleeping."""
    def __init__(self):
        """Start at zero with an empty history of requested delays."""
        self.now = 0
        self.sleeps = []

    def __call__(self):
        """Return the simulated elapsed time, as time.monotonic would."""
        return self.now

    def sleep(self, delay):
        """Advance simulated time and retain the delay for backoff assertions."""
        self.sleeps.append(delay)
        self.now += delay


class Session:
    """Scripted HTTP transport returning responses or raising exceptions in order."""
    def __init__(self, responses):
        """Prepare the response sequence and an inspection log of outgoing calls."""
        self.responses = iter(responses)
        self.calls = []

    def request(self, *args, **kwargs):
        """Record a frozen copy of request parameters and consume the next outcome.

        Copying matters because pagination later mutates the params dictionary.
        Unexpected extra requests exhaust the iterator and fail the test.
        """
        self.calls.append((args, copy.deepcopy(kwargs)))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        """Match requests.Session cleanup; this fixture has nothing to release."""
        pass


def auth():
    """Return a successful authentication response with the documented token lifetime."""
    return Response(payload={'access_token': 'test-token', 'expires_in': 30})


def page(token=None):
    """Return an empty usage page, optionally with another page still to fetch."""
    return Response(payload={'data': [], 'next_page_token': token})


def paused_writer(path, ready):
    """Child-process entry point: write an uncommitted row and wait to be killed.

    The event is set only after the generator resumes following the yielded row,
    proving the caller has written that row inside its still-open transaction.
    Keeping process entry points at module scope supports Windows spawn.
    """
    class Paused(Source):
        """Source that holds a real SQLite write transaction open for the parent test."""
        def pages(self, start, end):
            """Yield one row, signal that it was processed, then hold the transaction open."""
            yield [record('uncommitted', '2026-01-01')]
            ready.set()
            time.sleep(120)
    ingest(Paused(), path, reconcile=True, now=NOW)


def ordinary_writer(path, ready, results):
    """Wait for a shared start signal, ingest in a child process, and report outcome.

    Only exception class names cross the result queue; the parent makes assertions
    and cleans up the processes even when a writer fails.
    """
    ready.wait(10)
    try:
        ingest(Source([record()]), path, now=NOW, lock_timeout=10)
        results.put('ok')
    except Exception as exc:
        results.put(type(exc).__name__)


class StorageTests(unittest.TestCase):
    """Exercise real SQLite transactions against a deterministic in-memory source."""
    def setUp(self):
        """Give each test an isolated database directory and automatic cleanup."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / 'usage.db')

    def rows(self, table='usage'):
        """Read committed rows in stable order for comparisons across runs.

        table is a test-owned identifier, never external input. closing() is used
        because a SQLite connection context manager alone does not close it.
        """
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()

    def run_source(self, records, **kwargs):
        """Run a fixture at a controlled date and return its requests and summary."""
        source = Source(records)
        summary = ingest(source, self.path, now=kwargs.pop('now', NOW), **kwargs)
        return source, summary

    def test_window_boundaries(self):
        """Prove inclusive 31-day windows neither overlap nor skip dates."""
        self.assertEqual(list(windows(date(2026, 1, 1), date(2026, 2, 2))), [
            (date(2026, 1, 1), date(2026, 1, 31)),
            (date(2026, 2, 1), date(2026, 2, 2))])

    def test_idempotency_precision_and_simulated_clock(self):
        """Preserve exact amounts and overlap received usage despite a later host clock."""
        self.run_source([record()])
        before = self.rows()
        source, summary = self.run_source([record()])
        self.assertEqual(self.rows(), before)
        self.assertEqual(before[0][6], '12.345678901234567890123456789012345')
        self.assertEqual(source.ranges[0][0], date(2026, 8, 20))
        self.assertEqual(source.ranges[-1][1], NOW.date())
        self.assertEqual(summary['inserted'], 0)
        self.assertEqual(summary['updated'], 0)
        self.assertEqual(summary['unchanged'], 1)

    def test_empty_runs_do_not_advance_watermark(self):
        """Keep scanning from source start when no usage has ever been received."""
        self.run_source([])
        source, _ = self.run_source([])
        self.assertEqual(source.ranges[0][0], date(2026, 1, 1))

    def test_late_restatement_void_and_older_version(self):
        """Accept late rows and corrections, reject regression, and exclude void spend."""
        original = record()
        self.run_source([original])
        changed = dict(original, amount_usd=Decimal('-7.25'), updated_at='2026-09-20T00:00:00Z')
        self.run_source([changed, record('late', '2026-09-01')])
        self.assertEqual(self.rows()[1][6], '-7.25')
        self.run_source([original])
        self.assertEqual(self.rows()[1][6], '-7.25')
        void = dict(changed, status='void', updated_at='2026-09-21T00:00:00Z')
        self.run_source([void])
        self.assertEqual(len(self.rows('non_void_usage')), 1)
        self.assertEqual(len(self.rows()), 2)

    def test_equal_timestamp_conflict_rolls_back(self):
        """Roll back both new usage and reconciliation state when versions conflict."""
        self.run_source([record()])
        before, state = self.rows(), self.rows('ingestion_state')
        with self.assertRaises(IngestionError):
            self.run_source([record('new'), dict(record(), amount_usd=Decimal('99'))], reconcile=True)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.rows('ingestion_state'), state)

    def test_reconciliation_and_fresh_load_agree(self):
        """Elapsed time never forces a full scan; manual reconciliation repairs history."""
        self.run_source([record()])
        records = [record(), record('old-late', '2026-02-01')]
        before, state = self.rows(), self.rows('ingestion_state')
        for days in (6, 7, 90):
            source, summary = self.run_source(records, now=NOW + timedelta(days=days))
            self.assertEqual(summary['mode'], 'incremental')
            self.assertEqual(source.ranges[0][0], date(2026, 8, 20))
            self.assertEqual(self.rows(), before)
            self.assertEqual(self.rows('ingestion_state'), state)
        _, summary = self.run_source(records, reconcile=True, now=NOW + timedelta(days=90))
        self.assertEqual(summary['mode'], 'full')
        self.assertEqual(len(self.rows()), 2)
        expected = self.rows()
        fresh = str(Path(self.directory.name) / 'fresh.db')
        ingest(Source(records), fresh, now=NOW + timedelta(days=90))
        with closing(sqlite3.connect(fresh)) as db:
            self.assertEqual(db.execute('SELECT * FROM usage ORDER BY 1').fetchall(), expected)
        records.append(record('forced', '2026-03-01'))
        self.run_source(records, reconcile=True)
        self.assertEqual(len(self.rows()), 3)

    def test_exception_rolls_back(self):
        """Keep previously committed rows intact after a mid-scan source exception."""
        self.run_source([record()])
        before = self.rows()
        with self.assertRaises(RuntimeError):
            ingest(Source([record('new', '2026-01-01')], crash=True), self.path, reconcile=True, now=NOW)
        self.assertEqual(self.rows(), before)

    def test_failed_first_load_can_restart(self):
        """Recover a failed initial schema/data transaction with another full scan."""
        with self.assertRaises(RuntimeError):
            ingest(Source([record('new', '2026-01-01')], crash=True), self.path, now=NOW)
        _, summary = self.run_source([record()])
        self.assertEqual(summary['mode'], 'full')
        self.assertEqual(len(self.rows()), 1)

    def test_timestamp_preserved_and_numeric_formats_are_identical(self):
        """Store the API timestamp unchanged while canonicalizing decimal spellings."""
        self.run_source([record(amount=Decimal('12.00'))])
        self.assertEqual(self.rows()[0][-1], record()['updated_at'])
        _, summary = self.run_source([record(amount=12)])
        self.assertEqual(summary['unchanged'], 1)
        self.assertEqual(summary['updated'], 0)

    def test_timestamp_format_changes_and_invalid_dates_roll_back(self):
        """Reject source format drift and impossible calendar dates without publishing."""
        self.run_source([record()])
        before = self.rows()
        for value in ('2026-09-20T01:00:00+02:00', '2026-09-19T23:00:00+00:00',
                      '2026-09-19T23:00:00.000000Z', '2026-09-19T23:00:00',
                      '2026-9-19T23:00:00Z', '2026-02-30T23:00:00Z',
                      '2026-09-19T24:00:00Z', None):
            with self.subTest(value=value), self.assertRaises(IngestionError):
                self.run_source([dict(record(), updated_at=value)])
            self.assertEqual(self.rows(), before)

    def test_previous_storage_format_migrates_without_false_updates(self):
        """An old database must compare equal after switching to unchanged API timestamps."""
        self.run_source([record()])
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE usage SET updated_at='2026-09-19T23:00:00.000000+00:00'")
            db.commit()
        _, summary = self.run_source([record()])
        self.assertEqual(self.rows()[0][-1], record()['updated_at'])
        self.assertEqual(summary['updated'], 0)
        self.assertEqual(summary['unchanged'], 1)
        _, repeated = self.run_source([record()])
        self.assertEqual(repeated['updated'], 0)

    def test_killed_writer_lock_and_recovery(self):
        """Verify reader visibility, writer exclusion, and rollback after process death."""
        self.run_source([record()])
        context = multiprocessing.get_context('spawn')
        ready = context.Event()
        process = context.Process(target=paused_writer, args=(self.path, ready))
        process.start()
        try:
            self.assertTrue(ready.wait(10))
            self.assertEqual(len(self.rows()), 1)  # WAL reader sees committed data.
            with self.assertRaises(sqlite3.OperationalError):
                ingest(Source(), self.path, now=NOW, lock_timeout=0.05)
        finally:
            process.terminate()
            process.join(10)
        self.run_source([record()])
        self.assertEqual(len(self.rows()), 1)

    def test_two_concurrent_processes(self):
        """Start two writers together and require one consistent, unduplicated result."""
        context = multiprocessing.get_context('spawn')
        ready, results = context.Event(), context.Queue()
        processes = [context.Process(target=ordinary_writer, args=(self.path, ready, results)) for _ in range(2)]
        for process in processes:
            process.start()
        ready.set()
        try:
            self.assertEqual([results.get(timeout=15) for _ in processes], ['ok', 'ok'])
            self.assertEqual(len(self.rows()), 1)
        finally:
            for process in processes:
                process.join(5)
                if process.is_alive():
                    process.terminate()
                    process.join()
            results.close()

    def test_invalid_records(self):
        """Reject invalid statuses, dates, timestamps, numbers, and identifiers."""
        for changes in ({'status': 'mystery'}, {'usage_date': '2026-09-30'},
                        {'updated_at': '2026-09-19T00:00:00'}, {'amount_usd': Decimal('NaN')},
                        {'quantity': True}, {'customer_id': ''}):
            with self.subTest(changes=changes), self.assertRaises(IngestionError):
                validate_record(dict(record(), **changes), date(2026, 9, 1), date(2026, 9, 20))


class HTTPTests(unittest.TestCase):
    """Test API behavior using scripted responses and a clock that never sleeps."""
    def client(self, responses, budget=120):
        """Build a client with controlled outcomes, timing, and maximum jitter.

        Choosing the upper jitter bound makes expected backoff delays exact.
        """
        self.clock, self.session = Clock(), Session(responses)
        return BillingClient('http://example.test', 'id', 'secret', self.session,
                             self.clock.sleep, self.clock, lambda a, b: b, budget)

    def fetch(self, client):
        """Exhaust the page generator so every scripted request and validation runs."""
        return list(client.pages(date(2026, 9, 1), date(2026, 9, 20)))

    def test_pagination_and_proactive_refresh(self):
        """Refresh near expiry while preserving the continuation token and page size."""
        client = self.client([auth(), page('next'), auth(), page()])
        iterator = client.pages(date(2026, 9, 1), date(2026, 9, 20))
        self.assertEqual(next(iterator), [])
        self.clock.now = 28
        self.assertEqual(list(iterator), [[]])
        self.assertEqual(self.session.calls[-1][1]['params']['page_token'], 'next')
        self.assertEqual(self.session.calls[-1][1]['params']['page_size'], 200)

    def test_401_refresh_once(self):
        """Recover from one rejected token but stop if its replacement is rejected too."""
        client = self.client([auth(), Response(401), auth(), page()])
        self.assertEqual(self.fetch(client), [[]])
        client = self.client([auth(), Response(401), auth(), Response(401)])
        with self.assertRaisesRegex(IngestionError, '401'):
            self.fetch(client)

    def test_retry_auth_and_usage(self):
        """Apply backoff to both endpoints and respect the vendor Retry-After delay."""
        client = self.client([Response(503), auth(), requests.Timeout(),
                              Response(429, headers={'Retry-After': '7'}), page()])
        self.assertEqual(self.fetch(client), [[]])
        self.assertEqual(client.counts['retries'], 3)
        self.assertEqual(self.clock.sleeps, [1, 1, 7])

    def test_retry_budget_and_attempt_limit(self):
        """Refuse an excessive vendor wait and bound repeated transient failures."""
        client = self.client([Response(429, headers={'Retry-After': '121'})])
        with self.assertRaisesRegex(IngestionError, 'budget'):
            self.fetch(client)
        self.assertEqual(self.clock.sleeps, [])
        client = self.client([Response(503) for _ in range(6)])
        with self.assertRaisesRegex(IngestionError, 'budget'):
            self.fetch(client)
        self.assertEqual(client.counts['requests'], 6)

    def test_permanent_auth_failure(self):
        """Fail immediately on forbidden credentials without exposing response bodies."""
        client = self.client([Response(403, text='private response must not be logged')])
        with self.assertRaisesRegex(IngestionError, '^API returned HTTP 403$'):
            self.fetch(client)
        self.assertEqual(len(self.session.calls), 1)

    def test_malformed_pages_and_cycle(self):
        """Reject invalid JSON, missing pagination metadata, and repeated tokens."""
        for responses in ([auth(), Response(text='broken')], [auth(), Response(payload={'data': []})],
                          [auth(), page('a'), page('b'), page('a')]):
            client = self.client(responses)
            with self.assertRaises(IngestionError):
                self.fetch(client)

    def test_json_decimals_preserved(self):
        """Decode JSON numeric fractions without passing through a binary float."""
        response = Response(text='{"amount": 123456789.123456789123456789}')
        self.assertEqual(BillingClient._json(response)['amount'], Decimal('123456789.123456789123456789'))


if __name__ == '__main__':
    unittest.main()
