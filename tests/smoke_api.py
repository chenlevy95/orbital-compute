"""Check the usage loader against a running mock API: python tests/smoke_api.py

Needs BILLING_CLIENT_ID and BILLING_CLIENT_SECRET (and optionally BILLING_API_URL).
Keep the mock's as-of date fixed while it runs. Uses temporary databases only.
"""

from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ingest_usage import BillingClient, ingest


def rows(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute('SELECT * FROM usage ORDER BY record_id').fetchall()


def main():
    today = datetime.now(timezone.utc).date()
    client = BillingClient(os.getenv('BILLING_API_URL', 'http://127.0.0.1:8765'),
                           os.environ['BILLING_CLIENT_ID'], os.environ['BILLING_CLIENT_SECRET'])
    with tempfile.TemporaryDirectory() as directory, closing(client):
        first, fresh = Path(directory) / 'first.db', Path(directory) / 'fresh.db'
        print('Initial:', ingest(client, first, today=today), flush=True)
        expected = rows(first)
        assert expected, 'the mock API returned no usage'
        print('Repeat:', ingest(client, first, today=today), flush=True)
        assert rows(first) == expected, 'incremental rerun changed results'
        print('Reconcile:', ingest(client, first, reconcile=True, today=today), flush=True)
        assert rows(first) == expected, 'reconciliation changed results'
        print('Fresh:', ingest(client, fresh, today=today), flush=True)
        assert rows(fresh) == expected, 'fresh load disagrees'
        print(f'PASS: {len(expected)} records')


if __name__ == '__main__':
    main()
