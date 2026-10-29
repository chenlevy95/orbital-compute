"""Run: python -m unittest discover -s tests -v

End to end on the real inputs: the mock API's usage file at as-of 2026-09-20 and the
Finance workbooks in data/commitments, compared with the CSVs in tests/golden.
The records come from mock_api/server.py's own load_visible() and are served in
memory, so no server is needed. After an intended change to the results, review
the diff and regenerate:
UPDATE_GOLDEN=1 python -m unittest discover -s tests -p test_regression.py
"""

from datetime import date
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import unittest

import build_models as models
import ingest_commitments as tracker
import ingest_usage as usage
from test_ingest_usage import FakeAPI

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).with_name('golden')
AS_OF = date(2026, 9, 20)


def mock_records(as_of):
    spec = importlib.util.spec_from_file_location('mock_server', ROOT / 'mock_api' / 'server.py')
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    return server.load_visible(as_of)


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.db = self.tmp / 'db.sqlite3'

    def test_outputs_match_golden(self):
        api = FakeAPI(mock_records(AS_OF), page_size=200)
        client = usage.BillingClient('http://api', 'id', 'secret', api, lambda _: None)
        usage.ingest(client, self.db, today=AS_OF)
        loaded = tracker.ingest(ROOT / 'data' / 'commitments', self.db)
        self.assertEqual((loaded['snapshots'], loaded['errors']), (3, []))
        summary = models.build(self.db)
        models.export(self.db, self.tmp / 'out')

        if os.getenv('UPDATE_GOLDEN'):
            shutil.copytree(self.tmp / 'out', GOLDEN, dirs_exist_ok=True)
        self.assertEqual(summary, {'last_usage_date': '2026-09-19', 'contract_days': 1619,
                                   'at_risk_contracts': 4, 'unattributed_days': 173})
        for table in models.TABLES:
            with self.subTest(table=table):
                expected = (GOLDEN / f'{table}.csv').read_text(encoding='utf-8').splitlines()
                actual = (self.tmp / 'out' / f'{table}.csv').read_text(encoding='utf-8').splitlines()
                self.assertEqual(len(actual), len(expected), 'row count changed')
                for number, (got, want) in enumerate(zip(actual, expected), 1):
                    self.assertEqual(got, want, f'{table}.csv line {number}')


if __name__ == '__main__':
    unittest.main()
