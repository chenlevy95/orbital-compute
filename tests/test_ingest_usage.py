"""Run: python -m unittest discover -s tests -v

FakeAPI stands in for the HTTP session, so these tests need no running server.
"""

from contextlib import closing
from datetime import date
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest

import ingest_usage as usage

TODAY = date(2026, 9, 20)


def record(record_id='r1', day='2026-09-19', amount='10.5', status='final',
           updated='2026-09-20T03:00:00Z'):
    return {'record_id': record_id, 'customer_id': 'C1', 'usage_date': day, 'sku': 'gpu',
            'quantity': 1.0, 'unit': 'gpu_hour', 'amount_usd': float(amount),
            'status': status, 'updated_at': updated}


class Response:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code, self.headers = status_code, headers or {}
        self.text = json.dumps(body)


class FakeAPI:
    """Serves self.records page by page; queue Responses in self.failures to inject errors."""

    def __init__(self, records=(), page_size=2):
        self.records, self.page_size = list(records), page_size
        self.failures, self.requests, self.tokens = [], [], 0
        self.lock = threading.Lock()

    def request(self, method, url, params=None, headers=None, **_):
        with self.lock:
            self.requests.append((method, url, dict(params or {})))
            if self.failures:
                return self.failures.pop(0)
            if url.endswith('/oauth/token'):
                self.tokens += 1
                return Response(200, {'access_token': f't{self.tokens}', 'expires_in': 30})
            if headers['Authorization'] != f'Bearer t{self.tokens}':
                return Response(401)
            rows = [r for r in self.records
                    if params['start_date'] <= r['usage_date'] <= params['end_date']]
            offset = int(params.get('page_token', 0))
            more = offset + self.page_size < len(rows)
            return Response(200, {'data': rows[offset:offset + self.page_size],
                                  'next_page_token': str(offset + self.page_size) if more else None})

    def close(self):
        pass


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.db = self.tmp / 'db.sqlite3'
        self.api = FakeAPI()
        self.sleeps = []
        self.client = usage.BillingClient('http://api', 'id', 'secret', self.api, self.sleeps.append)

    def run_ingest(self, **kwargs):
        return usage.ingest(self.client, self.db, today=TODAY, **kwargs)

    def rows(self, view='usage'):
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute(f'SELECT * FROM {view} ORDER BY record_id').fetchall()

    def test_backfill_then_idempotent_incremental(self):
        self.api.records = [record('r1', '2026-01-01'), record('r2', '2026-05-01'),
                            record('r3', '2026-09-19', amount='0.10000')]
        first = self.run_ingest()
        self.assertEqual((first['mode'], first['inserted']), ('full', 3))
        self.assertEqual(self.rows()[2][6], '0.1')
        before = self.rows()

        second = self.run_ingest()
        self.assertEqual((second['mode'], second['start_date']), ('incremental', '2026-08-20'))
        self.assertEqual((second['inserted'], second['updated']), (0, 0))
        self.assertEqual(self.rows(), before)

    def test_windows_are_at_most_31_days(self):
        self.run_ingest()
        for _, url, params in self.api.requests:
            if url.endswith('/v1/usage'):
                span = date.fromisoformat(params['end_date']) - date.fromisoformat(params['start_date'])
                self.assertLess(span.days, 31)

    def test_restatements_late_arrivals_and_voids(self):
        self.api.records = [record('r1'), record('r2')]
        self.run_ingest()
        self.api.records = [record('r1', amount='99', updated='2026-09-21T00:00:00Z'),
                            record('r2', status='void', updated='2026-09-21T00:00:00Z'),
                            record('late', '2026-08-25')]
        result = self.run_ingest()
        self.assertEqual((result['inserted'], result['updated']), (1, 2))
        self.assertEqual([row[0] for row in self.rows('non_void_usage')], ['late', 'r1'])
        self.assertEqual(self.rows()[1][6], '99')

        # An older version never overwrites a newer one.
        self.api.records = [record('r1', amount='1', updated='2026-09-19T00:00:00Z')]
        self.assertEqual(self.run_ingest()['unchanged'], 1)
        self.assertEqual(self.rows()[1][6], '99')

    def test_conflicting_version_rolls_back_whole_run(self):
        self.api.records = [record('r1')]
        self.run_ingest()
        self.api.records = [record('new'), record('r1', amount='5')]
        with self.assertRaises(usage.IngestionError):
            self.run_ingest()
        self.assertEqual([row[0] for row in self.rows()], ['r1'])

    def test_invalid_record_rolls_back(self):
        self.api.records = [record('r1')]
        self.run_ingest()
        self.api.records = [record('r2'), {**record('r3'), 'status': 'pending'}]
        with self.assertRaises(usage.IngestionError):
            self.run_ingest()
        self.assertEqual([row[0] for row in self.rows()], ['r1'])

    def test_retries_honor_retry_after(self):
        self.api.records = [record()]
        self.api.failures = [Response(429, headers={'Retry-After': '7'}), Response(503)]
        self.assertEqual(self.run_ingest()['inserted'], 1)
        self.assertEqual(len(self.sleeps), 2)
        self.assertGreaterEqual(self.sleeps[0], 7)

    def test_gives_up_after_max_attempts(self):
        self.api.failures = [Response(503)] * usage.MAX_ATTEMPTS
        with self.assertRaisesRegex(usage.IngestionError, 'attempts'):
            self.run_ingest()

    def test_reauthenticates_after_401(self):
        self.api.records = [record()]
        self.client.authenticate()
        self.api.tokens += 1  # the server revokes the client's token
        self.assertEqual(self.run_ingest()['inserted'], 1)

    def test_concurrent_runs_store_once(self):
        self.api.records = [record(f'r{i}', f'2026-09-{i:02}') for i in range(1, 20)]
        clients = [usage.BillingClient('http://api', 'id', 'secret', FakeAPI(self.api.records), print)
                   for _ in range(2)]
        threads = [threading.Thread(target=usage.ingest, args=(client, self.db),
                                    kwargs={'today': TODAY}) for client in clients]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.rows()), 19)


if __name__ == '__main__':
    unittest.main()
