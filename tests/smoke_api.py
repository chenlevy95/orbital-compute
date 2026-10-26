"""Verify ingestion against an already running, stable mock API over real HTTP.

Run with: python tests/smoke_api.py
Configure BILLING_CLIENT_ID, BILLING_CLIENT_SECRET, and optionally BILLING_API_URL
as for the loader. Unlike the unit suite, this check makes real requests and may
wait for rate limits. It creates temporary databases and removes them on exit;
the regular storage/usage.sqlite3 database is never touched.

Keep the mock's as-of date unchanged during the check. All four scans must see
the same source state for equality comparisons to be meaningful.
"""

from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

# Direct script execution puts tests/, not the repository root, on sys.path.
# Add the root so this works without installing the loader as a Python package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ingest_usage import BillingClient, ingest


def rows(path):
    """Return every stored column in key order for exact, repeatable comparisons.

    Include void records as well as active usage: idempotency applies to stored
    source data, not just the spend visible through the non-void view.
    """
    with closing(sqlite3.connect(path)) as db:
        return db.execute('SELECT * FROM usage ORDER BY record_id').fetchall()


def main():
    """Compare initial, incremental, reconciled, and independent fresh results.

    A fixed UTC instant keeps scan boundaries consistent even across midnight.
    Assertions fail loudly on a mismatch. TemporaryDirectory and finally ensure
    database files and HTTP connections are cleaned up after success or failure.
    """
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as directory:
        first = Path(directory) / 'first.db'
        fresh = Path(directory) / 'fresh.db'
        client = BillingClient(os.getenv('BILLING_API_URL', 'http://127.0.0.1:8765'),
                               os.environ['BILLING_CLIENT_ID'], os.environ['BILLING_CLIENT_SECRET'])
        try:
            # Establish a nonempty baseline; an empty source would make the
            # equality checks pass without exercising real record ingestion.
            print('Initial:', ingest(client, first, now=now), flush=True)
            expected = rows(first)
            assert expected, 'Smoke test requires a nonempty mock API snapshot'

            # Reusing the database exercises the overlap and version comparison.
            print('Repeat:', ingest(client, first, now=now), flush=True)
            assert rows(first) == expected, 'Incremental rerun changed results'

            # Force all historical windows, independently of the weekly timer.
            print('Reconcile:', ingest(client, first, reconcile=True, now=now), flush=True)
            assert rows(first) == expected, 'Reconciliation changed results'

            # A different database must converge to exactly the same records.
            print('Fresh:', ingest(client, fresh, now=now), flush=True)
            assert rows(fresh) == expected, 'Fresh load disagrees'
            print(f'PASS: {len(expected)} records; incremental, reconciliation, and fresh load agree')
        finally:
            client.close()


if __name__ == '__main__':
    main()
