"""Load billing usage from the API into SQLite, picking up late and restated records.

The first run backfills from 2026-01-01. Later runs rescan from 30 days before the
latest usage date already stored, which covers restatements (up to 30 days) and
late arrivals. A run is one transaction holding the write lock, so a killed run
leaves the previous state intact and concurrent runs serialize.
"""

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import os
import random
import re
import sys
import time

import requests

import db

SOURCE_START = date(2026, 1, 1)
MAX_WINDOW_DAYS = 31
PAGE_SIZE = 200
LOOKBACK_DAYS = 30
MAX_ATTEMPTS = 6
RETRYABLE = {429, 500, 502, 503, 504}
COLUMNS = ('record_id', 'customer_id', 'usage_date', 'sku', 'quantity', 'unit',
           'amount_usd', 'status', 'updated_at')


class IngestionError(Exception):
    """A failure safe to print (never includes credentials or response bodies)."""


def decimal_text(value):
    """Exact decimal text for a JSON number; floats are parsed as Decimal upstream."""
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError('expected a number')
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError('nonfinite number')
    text = format(number, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def validate_record(record, start, end):
    """Return the record as a storage tuple, or raise IngestionError.

    updated_at must be exactly YYYY-MM-DDTHH:MM:SSZ so that comparing strings
    compares times.
    """
    try:
        for field in ('record_id', 'customer_id', 'sku', 'unit'):
            if not isinstance(record[field], str) or not record[field]:
                raise ValueError(field)
        if not start.isoformat() <= record['usage_date'] <= end.isoformat():
            raise ValueError('usage_date outside requested window')
        date.fromisoformat(record['usage_date'])
        if record['status'] not in ('final', 'void'):
            raise ValueError('status')
        if not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', record['updated_at']):
            raise ValueError('updated_at')
        row = dict(record, quantity=decimal_text(record['quantity']),
                   amount_usd=decimal_text(record['amount_usd']))
        return tuple(row[field] for field in COLUMNS)
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise IngestionError(f'invalid usage record ({exc}); run rolled back') from None


def windows(start, end):
    """Split an inclusive date range into windows of at most 31 days."""
    while start <= end:
        stop = min(start + timedelta(days=MAX_WINDOW_DAYS - 1), end)
        yield start, stop
        start = stop + timedelta(days=1)


class BillingClient:
    def __init__(self, url, client_id, secret, session=None, sleep=time.sleep):
        self.url = url.rstrip('/')
        self.client_id, self.secret = client_id, secret
        self.session = session or requests.Session()
        self.sleep = sleep
        self.token, self.expires = None, 0.0

    def close(self):
        self.session.close()

    def _request(self, method, path, **kwargs):
        """Retry connection errors, 429 and 5xx with backoff, honoring Retry-After."""
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = self.session.request(method, self.url + path, timeout=(5, 30), **kwargs)
            except (requests.ConnectionError, requests.Timeout):
                response = None
            if response is not None and response.status_code not in RETRYABLE:
                return response
            delay = random.uniform(0, min(30, 2 ** attempt))
            if response is not None and response.headers.get('Retry-After'):
                delay = max(delay, float(response.headers['Retry-After']))
            if attempt < MAX_ATTEMPTS - 1:
                self.sleep(delay)
        raise IngestionError(f'{method} {path} still failing after {MAX_ATTEMPTS} attempts')

    def _json(self, response):
        if response.status_code != 200:
            raise IngestionError(f'API returned HTTP {response.status_code}')
        try:
            return json.loads(response.text, parse_float=Decimal)
        except ValueError:
            raise IngestionError('malformed API response') from None

    def authenticate(self):
        started = time.monotonic()
        body = self._json(self._request('POST', '/oauth/token', json={
            'client_id': self.client_id, 'client_secret': self.secret}))
        # Refresh 5 seconds early so a token never expires mid-request.
        self.token, self.expires = body['access_token'], started + float(body['expires_in']) - 5

    def _get_usage(self, params):
        if self.token is None or time.monotonic() >= self.expires:
            self.authenticate()
        response = self._request('GET', '/v1/usage', params=params,
                                 headers={'Authorization': f'Bearer {self.token}'})
        if response.status_code == 401:
            self.authenticate()
            response = self._request('GET', '/v1/usage', params=params,
                                     headers={'Authorization': f'Bearer {self.token}'})
        return self._json(response)

    def pages(self, start, end):
        """Yield each page of records for one inclusive window of at most 31 days."""
        params = {'start_date': start.isoformat(), 'end_date': end.isoformat(),
                  'page_size': PAGE_SIZE}
        while True:
            body = self._get_usage(params)
            yield body['data']
            if not body.get('next_page_token'):
                return
            params['page_token'] = body['next_page_token']


def create_schema(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS usage (
        record_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, usage_date TEXT NOT NULL,
        sku TEXT NOT NULL, quantity TEXT NOT NULL, unit TEXT NOT NULL,
        amount_usd TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('final','void')),
        updated_at TEXT NOT NULL)''')
    connection.execute('CREATE INDEX IF NOT EXISTS usage_date_idx ON usage(usage_date)')
    connection.execute("CREATE VIEW IF NOT EXISTS non_void_usage AS SELECT * FROM usage WHERE status != 'void'")


def store_record(connection, row):
    """Insert or apply a newer version; return 'inserted', 'updated' or 'unchanged'."""
    stored = connection.execute('SELECT * FROM usage WHERE record_id=?', (row[0],)).fetchone()
    if stored == row or (stored and row[-1] < stored[-1]):
        return 'unchanged'  # a replay, or an older version than the one we have
    if stored and row[-1] == stored[-1]:
        raise IngestionError(f'record {row[0]} changed without a new updated_at')
    connection.execute('INSERT OR REPLACE INTO usage VALUES (?,?,?,?,?,?,?,?,?)', row)
    return 'updated' if stored else 'inserted'


def ingest(client, database, reconcile=False, today=None):
    """Fetch and store one scan; return a summary of what changed."""
    end = today or datetime.now(timezone.utc).date()
    connection = db.connect(database)
    counts = Counter(inserted=0, updated=0, unchanged=0)
    try:
        # Take the write lock before choosing the range, so concurrent runs can't interleave.
        connection.execute('BEGIN IMMEDIATE')
        create_schema(connection)
        latest = connection.execute('SELECT MAX(usage_date) FROM usage').fetchone()[0]
        full = reconcile or latest is None
        # Anchor the overlap to stored data, not the clock: the API's "today" can lag ours.
        start = SOURCE_START if full else max(
            SOURCE_START, date.fromisoformat(latest) - timedelta(days=LOOKBACK_DAYS))
        for first, last in windows(start, end):
            for page in client.pages(first, last):
                for record in page:
                    counts[store_record(connection, validate_record(record, first, last))] += 1
        connection.commit()
        return {'mode': 'full' if full else 'incremental', 'start_date': start.isoformat(),
                'end_date': end.isoformat(), **counts}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--api-url', default=os.getenv('BILLING_API_URL', 'http://127.0.0.1:8765'))
    parser.add_argument('--database', default=os.getenv('USAGE_DATABASE', 'storage/usage.sqlite3'))
    parser.add_argument('--reconcile', action='store_true', help='rescan all history')
    args = parser.parse_args()
    client_id, secret = os.getenv('BILLING_CLIENT_ID'), os.getenv('BILLING_CLIENT_SECRET')
    if not client_id or not secret:
        print('Set BILLING_CLIENT_ID and BILLING_CLIENT_SECRET', file=sys.stderr)
        return 1
    client = BillingClient(args.api_url, client_id, secret)
    try:
        print(json.dumps(ingest(client, args.database, args.reconcile), sort_keys=True))
        return 0
    except (IngestionError, requests.RequestException) as exc:
        print(f'Usage ingestion failed: {exc}', file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == '__main__':
    sys.exit(main())
