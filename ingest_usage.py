"""Load the latest billing usage into SQLite without double-counting reruns.

Reading order:
    * Normalization helpers turn API values into stable, comparable values.
    * BillingClient handles authentication, HTTP retries, and pagination.
    * Storage helpers select the scan range and apply record versions.
    * ingest() coordinates one atomic run; main() supplies the CLI.

The HTTP client never commits data. The ingestion function owns the transaction,
so a failure anywhere in a scan leaves the previous committed result intact.
Only the documented API is used; the mock's internal dataset is never read.
"""

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import json
import math
import os
from pathlib import Path
import random
import re
import sqlite3
import sys
import time
from urllib.parse import urlsplit
from typing import NamedTuple

import requests

SOURCE_START = date(2026, 1, 1)
MAX_WINDOW_DAYS = 31
PAGE_SIZE = 200
LOOKBACK_DAYS = 30
RECONCILE_INTERVAL = timedelta(days=7)
MAX_HTTP_ATTEMPTS = 6
HTTP_TIMEOUT = (5, 30)  # Connect timeout, then read timeout, in seconds.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
# This order is shared by validation, SQLite inserts, and stored-row comparisons.
COLUMNS = ('record_id', 'customer_id', 'usage_date', 'sku', 'quantity', 'unit',
           'amount_usd', 'status', 'updated_at')


class UsageRecord(NamedTuple):
    """Validated storage row with named fields and SQLite-compatible tuple values.

    Dates and UTC timestamps have canonical string formats. Decimal fields are
    text to avoid SQLite's floating-point conversion of financial values.
    """

    record_id: str
    customer_id: str
    usage_date: str
    sku: str
    quantity: str
    unit: str
    amount_usd: str
    status: str
    updated_at: str


class IngestionError(Exception):
    """A safe-to-display ingestion failure (never contains HTTP bodies)."""


def timestamp(value: str) -> str:
    """Validate the observed API timestamp format and return the original string.

    The API returns UTC timestamps as YYYY-MM-DDTHH:MM:SSZ. Requiring this fixed
    format makes string ordering chronological without conversion. Check both
    spelling and calendar validity; reject offsets, fractions, and naive times
    rather than silently accepting a change in the observed source format.
    """
    if not isinstance(value, str) or not re.fullmatch(
        r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', value
    ):
        raise ValueError('timestamp must be YYYY-MM-DDTHH:MM:SSZ')
    datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
    return value


def strict_date(value: str) -> date:
    """Parse exactly YYYY-MM-DD, rejecting alternate ISO date spellings."""
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError('date must be YYYY-MM-DD')
    return parsed


def decimal_text(value: int | Decimal) -> str:
    """Return exact, canonical decimal text for a finite JSON numeric value.

    JSON fractions are decoded as Decimal before reaching this function. Floats,
    strings, and booleans are rejected so precision loss or coercion cannot pass
    unnoticed. Equivalent values such as 1, 1.00, and 1E+0 store as the same text.
    """
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError('expected JSON number')
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError('nonfinite number')
    # Avoid normalize(), which can round using the active Decimal context.
    result = format(number, 'f')
    if '.' in result:
        result = result.rstrip('0').rstrip('.')
    return '0' if number == 0 else result


def validate_record(record, start: date, end: date) -> UsageRecord:
    """Validate one API object and return its canonical storage representation.

    The usage date must lie within the inclusive window that was requested.
    Negative amounts are allowed; unknown statuses and missing required fields
    are not. Extra API fields are ignored. The input dictionary is not modified.
    Invalid records raise a sanitized IngestionError, causing ingest() to roll back
    the whole run rather than silently skip potentially important billing data.
    """
    try:
        if not isinstance(record, dict):
            raise ValueError('expected object')
        for field in ('record_id', 'customer_id', 'sku', 'unit'):
            if not isinstance(record[field], str) or not record[field].strip():
                raise ValueError('empty identifier')
        usage_date = strict_date(record['usage_date'])
        if not start <= usage_date <= end or record['status'] not in ('final', 'void'):
            raise ValueError('invalid date or status')
        result = dict(record)
        result['updated_at'] = timestamp(record['updated_at'])
        for field in ('quantity', 'amount_usd'):
            result[field] = decimal_text(record[field])
        return UsageRecord(*(result[field] for field in COLUMNS))
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        raise IngestionError('Invalid usage record; transaction rolled back') from None


def windows(start, end):
    """Yield contiguous inclusive date windows, each at most 31 dates long.

    For example, January 1 through February 2 becomes January 1–31 and February
    1–2. An empty range yields nothing. Adding one day after each window prevents
    overlap or gaps at the boundary.
    """
    while start <= end:
        stop = min(start + timedelta(days=MAX_WINDOW_DAYS - 1), end)
        yield start, stop
        start = stop + timedelta(days=1)


class BillingClient:
    """Stateful API client sharing a connection pool and short-lived bearer token.

    Call pages() for a single date window and close() when finished. Request and
    retry counters accumulate over this client's lifetime; ingest() reports only
    the difference attributable to its own run. This client is used sequentially.
    """

    def __init__(self, url, client_id, secret, session=None, sleep=time.sleep,
                 monotonic=time.monotonic, jitter=random.uniform, retry_budget=120):
        """Configure transport and timing without making a network request.

        session, sleep, monotonic, and jitter can be replaced by deterministic test
        doubles. retry_budget is the retry deadline in seconds per HTTP operation,
        not a time limit for the complete ingestion run. close() also closes an
        injected session, so the caller should not share it with unrelated work.
        """
        parts = urlsplit(url)
        if (parts.scheme not in ('http', 'https') or not parts.netloc or
                parts.username or parts.password or parts.query or parts.fragment):
            raise IngestionError('API URL must be HTTP(S), without credentials, query, or fragment')
        self.url = url.rstrip('/')
        self.client_id, self.secret = client_id, secret
        self.session = session or requests.Session()
        self.sleep, self.clock, self.jitter = sleep, monotonic, jitter
        self.retry_budget = retry_budget
        self.token, self.expires = None, 0
        self.counts = Counter(requests=0, retries=0)

    def close(self):
        """Release the HTTP session and its pooled connections."""
        self.session.close()

    def _request(self, method, path, **kwargs):
        """Send an HTTP operation with bounded retries for transient failures.

        Return the first non-retryable response, including permanent HTTP errors;
        the caller interprets it and must close it. Authentication uses this same
        retry path. A 401 is handled by pages(), because it needs token refresh.
        No response body or credential is included in a failure message.
        """
        deadline = self.clock() + self.retry_budget
        for attempt in range(MAX_HTTP_ATTEMPTS):
            self.counts['requests'] += 1
            response = None
            try:
                response = self.session.request(method, self.url + path,
                                                timeout=HTTP_TIMEOUT, allow_redirects=False, **kwargs)
            except (requests.ConnectionError, requests.Timeout):
                pass
            # requests.Response is false for HTTP errors; test against None so a
            # real 429/503 response still contributes its Retry-After header.
            if response is not None and response.status_code not in RETRYABLE_STATUS_CODES:
                return response
            delay = self.jitter(0, min(30, 2 ** attempt))
            if response is not None:
                retry_after = response.headers.get('Retry-After')
                response.close()
                if retry_after is not None:
                    delay = max(delay, self._retry_after_seconds(retry_after))
            if attempt == MAX_HTTP_ATTEMPTS - 1 or self.clock() + delay >= deadline:
                raise IngestionError('HTTP retry budget exhausted; retry the run later')
            self.counts['retries'] += 1
            self.sleep(delay)
        raise AssertionError('unreachable')

    @staticmethod
    def _retry_after_seconds(value):
        """Interpret Retry-After as seconds or an HTTP date, without sleeping.

        The documented vendor format is seconds; HTTP-date support also accepts
        standards-compliant responses from intermediaries. Past HTTP dates imply
        no extra wait. Invalid headers fail the request instead of ignoring the
        vendor's rate-limit instruction.
        """
        try:
            seconds = float(value)
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError()
            return seconds
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                return max(0, (when - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                raise IngestionError('Invalid Retry-After header') from None

    @staticmethod
    def _json(response):
        """Require a successful JSON object and always release the response.

        Decode fractional numbers directly to Decimal, preserving their original
        precision. Nonstandard NaN/Infinity values become None and are rejected
        when a required numeric field is validated.
        """
        try:
            if response.status_code != 200:
                raise IngestionError(f'API returned HTTP {response.status_code}')
            result = json.loads(response.text, parse_float=Decimal,
                                parse_constant=lambda _: None)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, TypeError):
            raise IngestionError('Malformed API JSON response') from None
        finally:
            response.close()

    def authenticate(self):
        """Fetch a token and store a conservative monotonic refresh deadline.

        Start timing before the request and subtract a small safety margin so
        network latency does not overstate the remaining token lifetime.
        """
        started = self.clock()
        result = self._json(self._request('POST', '/oauth/token', json={
            'client_id': self.client_id, 'client_secret': self.secret}))
        try:
            token, lifetime = result['access_token'], result['expires_in']
            if not isinstance(token, str) or not token or isinstance(lifetime, bool):
                raise ValueError()
            lifetime = float(lifetime)
            if not math.isfinite(lifetime) or lifetime <= 0:
                raise ValueError()
        except (KeyError, TypeError, ValueError, OverflowError):
            raise IngestionError('Invalid authentication response') from None
        self.token, self.expires = token, started + lifetime - min(5, lifetime / 10)

    def pages(self, start, end):
        """Yield raw record lists for one inclusive API-sized date window.

        Refresh authentication proactively, or once after a rejected token. Keep
        the same date parameters across pages; reject token cycles to avoid an
        endless scan. An empty page can still have a continuation token. Record
        validation belongs to ingest(); this layer validates the page envelope.
        """
        params = {'start_date': start.isoformat(), 'end_date': end.isoformat(), 'page_size': PAGE_SIZE}
        seen = set()
        while True:
            if self.token is None or self.clock() >= self.expires:
                self.authenticate()
            response = self._request('GET', '/v1/usage', params=params,
                                     headers={'Authorization': 'Bearer ' + self.token})
            if response.status_code == 401:
                response.close()
                self.authenticate()
                response = self._request('GET', '/v1/usage', params=params,
                                         headers={'Authorization': 'Bearer ' + self.token})
            result = self._json(response)
            if not isinstance(result.get('data'), list) or 'next_page_token' not in result:
                raise IngestionError('Malformed usage page')
            token = result['next_page_token']
            if token is not None and (not isinstance(token, str) or not token or token in seen):
                raise IngestionError('Invalid or repeated pagination token')
            yield result['data']
            if token is None:
                break
            seen.add(token)
            params['page_token'] = token


def prepare_database(connection, lock_timeout):
    """Enable durable storage, acquire the writer lock, and create the schema.

    BEGIN IMMEDIATE obtains the lock before any API request or scan-state read.
    Schema creation is part of the same transaction as the data load, so a failed
    first run can be retried cleanly. This function deliberately does not commit.
    lock_timeout bounds the extra retry loop for concurrent WAL initialization;
    SQLite's connection timeout handles ordinary writer-lock contention.
    """
    # WAL initialization can return SQLITE_BUSY immediately when two fresh
    # processes open the same database, even with SQLite's busy timeout.
    lock_deadline = time.monotonic() + lock_timeout
    while True:
        try:
            connection.execute('PRAGMA journal_mode=WAL')
            break
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc).lower() or time.monotonic() >= lock_deadline:
                raise
            time.sleep(min(0.05, max(0, lock_deadline - time.monotonic())))
    connection.execute('PRAGMA synchronous=FULL')
    connection.execute('BEGIN IMMEDIATE')
    connection.execute('''CREATE TABLE IF NOT EXISTS usage (
        record_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, usage_date TEXT NOT NULL,
        sku TEXT NOT NULL, quantity TEXT NOT NULL, unit TEXT NOT NULL,
        amount_usd TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('final','void')),
        updated_at TEXT NOT NULL)''')
    connection.execute('CREATE INDEX IF NOT EXISTS usage_date_idx ON usage(usage_date)')
    connection.execute("CREATE VIEW IF NOT EXISTS non_void_usage AS SELECT * FROM usage WHERE status != 'void'")
    connection.execute('CREATE TABLE IF NOT EXISTS ingestion_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    # Earlier loader versions expanded the observed second-precision UTC values
    # to .000000+00:00. Convert those stored values once so version comparisons
    # remain correct when an existing database is opened by this version.
    # This migration is in the run's transaction and is a no-op on later runs.
    connection.execute("""UPDATE usage SET updated_at = substr(updated_at, 1, 19) || 'Z'
        WHERE length(updated_at) = 32 AND substr(updated_at, 20) = '.000000+00:00'""")


def choose_scan(connection, now, reconcile):
    """Return (inclusive_start_date, is_full_scan) from committed source state.

    Full scans are required on first use, on explicit request, or after seven
    days. Incremental scans overlap the latest *received* usage date by 30 days;
    empty future API responses must never advance that date. This matters when
    the API's simulated present is earlier than the computer's current date.
    Call this only after taking the writer lock so another run cannot change the
    state between this decision and publication.
    """
    last_full_row = connection.execute(
        "SELECT value FROM ingestion_state WHERE key='last_full_scan'"
    ).fetchone()
    latest_usage_date = connection.execute('SELECT MAX(usage_date) FROM usage').fetchone()[0]

    if reconcile or latest_usage_date is None or last_full_row is None:
        return SOURCE_START, True

    last_full_scan = datetime.fromisoformat(last_full_row[0])
    if now - last_full_scan >= RECONCILE_INTERVAL:
        return SOURCE_START, True

    overlap_start = date.fromisoformat(latest_usage_date) - timedelta(days=LOOKBACK_DAYS)
    return max(SOURCE_START, overlap_start), False


def store_record(connection, row: UsageRecord) -> str:
    """Apply one validated version and return inserted, updated, or unchanged.

    The caller owns the transaction. A newer version replaces the entire stored
    row, including its void status. Older versions cannot undo a correction.
    Equal timestamps with unequal contents are ambiguous and abort the run.
    Identical or older rows cause no UPDATE, preserving data-level idempotency.
    """
    stored = connection.execute(
        'SELECT * FROM usage WHERE record_id=?', (row.record_id,)
    ).fetchone()
    if stored is None:
        connection.execute('INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?)', row)
        return 'inserted'

    previous = UsageRecord(*stored)
    if row.updated_at > previous.updated_at:
        connection.execute(
            '''UPDATE usage SET
                customer_id=:customer_id, usage_date=:usage_date, sku=:sku,
                quantity=:quantity, unit=:unit, amount_usd=:amount_usd,
                status=:status, updated_at=:updated_at
            WHERE record_id=:record_id''',
            row._asdict(),
        )
        return 'updated'

    if row.updated_at == previous.updated_at and row != previous:
        raise IngestionError('Conflicting record versions with identical updated_at')
    return 'unchanged'


def ingest(client, database, reconcile=False, now=None, lock_timeout=60):
    """Fetch and atomically publish one scan, returning a per-run summary.

    Args:
        client: BillingClient, or a test double with pages() and a counts mapping.
        database: SQLite file path on persistent local storage.
        reconcile: Force all history to be scanned regardless of the schedule.
        now: A timezone-aware datetime for deterministic tests; defaults to UTC now.
        lock_timeout: Seconds to wait per database locking operation.

    Returns:
        A JSON-compatible dictionary with scan mode/range, record outcomes, and
        request/retry counts. void_seen counts incoming voids, not stored totals.

    Raises:
        IngestionError for invalid source data or exhausted HTTP retries. Storage
        and interruption exceptions propagate after rollback. No partial scan is
        committed, and the connection always closes. The client stays open so its
        owner can reuse it or close it. A killed process is recovered by SQLite.
    """
    now = now or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    end = now.date()
    if end < SOURCE_START:
        raise IngestionError('Current UTC date precedes source start')
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=lock_timeout, isolation_level=None)
    counts = Counter(inserted=0, updated=0, unchanged=0, void_seen=0)
    http_before = dict(client.counts)
    try:
        prepare_database(connection, lock_timeout)
        start, full = choose_scan(connection, now, reconcile)
        for first, last in windows(start, end):
            for page in client.pages(first, last):
                for record in page:
                    row = validate_record(record, first, last)
                    outcome = store_record(connection, row)
                    counts[outcome] += 1
                    if row.status == 'void':
                        counts['void_seen'] += 1
        # Publish reconciliation state only with the successfully loaded data.
        if full:
            connection.execute("INSERT INTO ingestion_state VALUES ('last_full_scan', ?) "
                               'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (now.isoformat(),))
        connection.commit()

        # A caller may reuse the client across runs; report this run's HTTP work.
        http_counts = {
            key: value - http_before.get(key, 0)
            for key, value in client.counts.items()
        }
        return {
            'mode': 'full' if full else 'incremental',
            'start_date': start.isoformat(),
            'end_date': end.isoformat(),
            **counts,
            **http_counts,
        }
    except BaseException:
        # KeyboardInterrupt must roll back just like an ordinary fetch failure.
        connection.rollback()
        raise
    finally:
        connection.close()


def main():
    """Resolve CLI/environment settings, run ingestion, and return an exit code.

    Credentials come only from the environment. CLI URL/database options override
    environment defaults. Print a JSON summary on success and sanitized errors on
    stderr on failure; never print raw transport exceptions or response bodies.
    Exit codes are 0 (success), 1 (failure), and 130 (keyboard interruption).
    """
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--api-url', default=os.getenv('BILLING_API_URL', 'http://127.0.0.1:8765'))
    parser.add_argument('--database', default=os.getenv('USAGE_DATABASE', 'storage/usage.sqlite3'))
    parser.add_argument('--reconcile', action='store_true',
                        help='Scan all history instead of using the normal refresh schedule')
    args = parser.parse_args()
    client = None
    try:
        client_id, secret = os.getenv('BILLING_CLIENT_ID'), os.getenv('BILLING_CLIENT_SECRET')
        if not client_id or not secret:
            raise IngestionError('Set BILLING_CLIENT_ID and BILLING_CLIENT_SECRET')
        client = BillingClient(args.api_url, client_id, secret)
        print(json.dumps(ingest(client, args.database, args.reconcile), sort_keys=True))
        return 0
    except IngestionError as exc:
        print(f'Ingestion failed: {exc}', file=sys.stderr)
        return 1
    except sqlite3.OperationalError:
        print('Ingestion failed: database unavailable, locked, or not writable; retry after checking storage', file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError, requests.RequestException):
        print('Ingestion failed: storage or transport error; check configuration and retry', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Ingestion interrupted; transaction rolled back', file=sys.stderr)
        return 130
    finally:
        if client is not None:
            client.close()


if __name__ == '__main__':
    sys.exit(main())
