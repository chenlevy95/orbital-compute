"""Run: python -m unittest discover -s tests -v"""

from contextlib import closing
import csv
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

import build_models as models
import ingest_commitments as tracker
import ingest_usage as usage


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.db = self.tmp / 'db.sqlite3'
        with closing(sqlite3.connect(self.db)) as connection:
            usage.create_schema(connection)
            tracker.create_schema(connection)
            connection.commit()

    def contract(self, contract_id, customer, start, end, commitment,
                 observed_from='2026-07-31', observed_to=None):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute('INSERT INTO contract_history VALUES (?,?,?,?,?,?,?,?,?)',
                               (contract_id, customer, 'Name', start, end, commitment, 'Active',
                                observed_from, observed_to))
            connection.commit()

    def spend(self, customer, day, amount, status='final'):
        with closing(sqlite3.connect(self.db)) as connection:
            count = connection.execute('SELECT COUNT(*) FROM usage').fetchone()[0]
            connection.execute('INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?)',
                               (f'r{count}', customer, day, 'gpu', '1', 'gpu_hour', amount,
                                status, '2026-09-20T03:00:00Z'))
            connection.commit()

    def table(self, name):
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute(f'SELECT * FROM {name} ORDER BY 1, date').fetchall()

    def test_hand_computed_rows(self):
        self.contract('K-1', 'C1', '2026-01-01', '2026-01-10', '100')
        self.spend('C1', '2026-01-01', '10')
        self.spend('C1', '2026-01-02', '99', status='void')
        self.spend('C1', '2026-01-03', '3.25')
        self.spend('C1', '2026-01-03', '2.25')  # two SKUs on one day
        self.spend('C1', '2026-01-04', '7', status='void')  # void, but still sets the last date
        summary = models.build(self.db)
        self.assertEqual(summary, {'last_usage_date': '2026-01-04', 'contract_days': 4,
                                   'at_risk_contracts': 0, 'unattributed_days': 0})
        self.assertEqual(self.table('contract_utilization_daily'), [
            ('K-1', 'C1', '2026-01-01', '100', 10, 1, '10', '10', '10.00', '1.000000', '100.00', 0),
            ('K-1', 'C1', '2026-01-02', '100', 10, 2, '0', '10', '20.00', '0.500000', '50.00', 0),
            ('K-1', 'C1', '2026-01-03', '100', 10, 3, '5.5', '15.5', '30.00', '0.516667', '51.67', 0),
            ('K-1', 'C1', '2026-01-04', '100', 10, 4, '0', '15.5', '40.00', '0.387500', '38.75', 0),
        ])

    def test_rows_stop_at_end_date_or_last_loaded(self):
        self.contract('K-1', 'C1', '2026-01-01', '2026-01-03', '100')
        self.contract('K-2', 'C2', '2026-01-02', '2026-12-31', '100')
        self.contract('K-3', 'C3', '2026-02-01', '2026-12-31', '100')  # starts after last loaded
        self.spend('C2', '2026-01-05', '1')
        models.build(self.db)
        days = [(row[0], row[2]) for row in self.table('contract_utilization_daily')]
        self.assertEqual(days, [('K-1', '2026-01-01'), ('K-1', '2026-01-02'), ('K-1', '2026-01-03'),
                                ('K-2', '2026-01-02'), ('K-2', '2026-01-03'), ('K-2', '2026-01-04'),
                                ('K-2', '2026-01-05')])

    def test_at_risk_needs_30_days_and_strictly_below_90_percent(self):
        # 100-day terms: projected = daily spend * 100.
        self.contract('K-1', 'C1', '2026-01-01', '2026-04-10', '100')
        self.contract('K-2', 'C2', '2026-01-01', '2026-04-10', '100')
        for offset in range(31):
            day = f'2026-01-{offset + 1:02d}'
            self.spend('C1', day, '0.89')  # projected 89 < 90
            self.spend('C2', day, '0.9')   # projected exactly 90
        self.assertEqual(models.build(self.db)['at_risk_contracts'], 1)
        flags = {(row[0], row[5]): row[-1] for row in self.table('contract_utilization_daily')}
        self.assertEqual(flags['K-1', 29], 0)
        self.assertEqual(flags['K-1', 30], 1)
        self.assertEqual(flags['K-2', 30], 0)
        self.assertEqual(flags['K-2', 31], 0)

    def test_unattributed_outside_every_contract(self):
        self.contract('K-1', 'C1', '2026-01-02', '2026-01-05', '100')
        self.contract('K-2', 'C1', '2026-01-10', '2026-01-20', '100')
        for day in ('2026-01-01', '2026-01-02', '2026-01-05', '2026-01-06', '2026-01-10'):
            self.spend('C1', day, '1')
        self.spend('C9', '2026-01-03', '2')  # no contract at all
        self.spend('C9', '2026-01-04', '5', status='void')
        self.assertEqual(models.build(self.db)['unattributed_days'], 3)
        self.assertEqual(self.table('unattributed_usage_daily'), [
            ('C1', '2026-01-01', '1'), ('C1', '2026-01-06', '1'), ('C9', '2026-01-03', '2')])

    def test_uses_latest_terms(self):
        self.contract('K-1', 'C1', '2026-01-01', '2026-01-02', '50',
                      observed_from='2026-07-31', observed_to='2026-08-31')
        self.contract('K-1', 'C1', '2026-01-01', '2026-01-04', '80', observed_from='2026-08-31')
        self.spend('C1', '2026-01-04', '1')
        models.build(self.db)
        rows = self.table('contract_utilization_daily')
        self.assertEqual(len(rows), 4)
        self.assertEqual({(row[3], row[4]) for row in rows}, {('80', 4)})
        self.assertEqual(self.table('unattributed_usage_daily'), [])

    def test_rebuild_is_idempotent_and_exports_csv(self):
        self.contract('K-1', 'C1', '2026-01-01', '2026-01-10', '100')
        self.spend('C1', '2026-01-02', '4')
        self.spend('C2', '2026-01-02', '3')
        models.build(self.db)
        first = self.table('contract_utilization_daily'), self.table('unattributed_usage_daily')
        models.build(self.db)
        self.assertEqual((self.table('contract_utilization_daily'),
                          self.table('unattributed_usage_daily')), first)

        models.export(self.db, self.tmp / 'out')
        with open(self.tmp / 'out' / 'contract_utilization_daily.csv', newline='') as file:
            rows = list(csv.reader(file))
        self.assertEqual(tuple(rows[0]), models.UTILIZATION_COLUMNS)
        self.assertEqual(len(rows), 3)
        with open(self.tmp / 'out' / 'unattributed_usage_daily.csv', newline='') as file:
            self.assertEqual(list(csv.reader(file)),
                             [list(models.UNATTRIBUTED_COLUMNS), ['C2', '2026-01-02', '3']])

    def test_no_usage_builds_empty_tables(self):
        self.contract('K-1', 'C1', '2026-01-01', '2026-01-10', '100')
        self.assertEqual(models.build(self.db)['contract_days'], 0)
        self.assertEqual(self.table('contract_utilization_daily'), [])

    def test_missing_inputs_fail(self):
        empty = self.tmp / 'empty.sqlite3'
        with self.assertRaises(models.ModelError):
            models.build(empty)


if __name__ == '__main__':
    unittest.main()
