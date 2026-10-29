"""Run: python -m unittest discover -s tests -v"""

from contextlib import closing
from datetime import date
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest

from openpyxl import Workbook

import ingest_commitments as tracker

SOURCE = Path(__file__).resolve().parents[1] / 'data' / 'commitments'
HEADERS = ['Contract ID', 'Customer ID', 'Customer', 'Start Date', 'End Date',
           'Commit (USD)', 'Status']
ROW = ['K-1', 'C1', 'Customer', date(2026, 1, 1), date(2026, 6, 30), 100, 'Active']


def write_book(folder, rows, as_of='2026-07-31', headers=HEADERS):
    workbook = Workbook()
    workbook.active.append(['Title row'])
    workbook.active.append(headers)
    for row in rows:
        workbook.active.append(row)
    path = Path(folder) / f'commitments_{as_of}.xlsx'
    workbook.save(path)
    return path


def state(database):
    with closing(sqlite3.connect(database)) as connection:
        return {table: connection.execute(f'SELECT * FROM {table} ORDER BY 1, 2').fetchall()
                for table in ('commitment_snapshots', 'commitment_observations', 'contract_history')}


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.db = self.tmp / 'db.sqlite3'
        self.input = self.tmp / 'input'
        self.input.mkdir()

    def copy(self, *names):
        for name in names:
            shutil.copy(SOURCE / f'commitments_{name}.xlsx', self.input)

    def test_supplied_snapshots_and_replay(self):
        self.copy('2026-07-31', '2026-08-31', '2026-09-28')
        first = tracker.ingest(self.input, self.db)
        self.assertEqual(first, {'snapshots_added': 3, 'snapshots': 3,
                                 'history_versions': 16, 'contracts': 11, 'errors': [],
                                 'warnings': []})
        before = state(self.db)
        self.assertEqual(tracker.ingest(self.input, self.db)['snapshots_added'], 0)
        self.assertEqual(state(self.db), before)

        with closing(sqlite3.connect(self.db)) as connection:
            current = {row[0]: row for row in connection.execute('SELECT * FROM contracts_current')}
            amendments = connection.execute(
                "SELECT commitment_usd, end_date, observed_from, observed_to FROM contract_history "
                "WHERE contract_id='K-1009' ORDER BY observed_from").fetchall()
        # Removed from the September tracker, but still a real contract.
        self.assertEqual(current['K-1004'][4], '2026-06-30')
        # Normalized: padded ID, '$900,000.00' and '1 Jun 2026'.
        self.assertIn('K-1002', current)
        self.assertEqual(current['K-1005'][5], '900000.00')
        self.assertEqual(current['K-1007'][3], '2026-06-01')
        self.assertEqual(amendments, [('500000.00', '2026-11-30', '2026-07-31', '2026-08-31'),
                                      ('650000.00', '2027-01-31', '2026-08-31', None)])

    def test_arrival_order_does_not_matter(self):
        self.copy('2026-09-28')
        tracker.ingest(self.input, self.db)
        self.copy('2026-07-31', '2026-08-31')
        tracker.ingest(self.input, self.db)

        fresh = self.tmp / 'fresh.sqlite3'
        tracker.ingest(self.input, fresh)
        self.assertEqual(state(self.db), state(fresh))

    def test_concurrent_runs_store_once(self):
        self.copy('2026-07-31', '2026-08-31', '2026-09-28')
        threads = [threading.Thread(target=tracker.ingest, args=(self.input, self.db))
                   for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        fresh = self.tmp / 'fresh.sqlite3'
        tracker.ingest(self.input, fresh)
        self.assertEqual(state(self.db), state(fresh))

    def test_invalid_file_is_skipped_and_loads_once_fixed(self):
        self.copy('2026-07-31', '2026-09-28')
        broken = write_book(self.input, [ROW], as_of='2026-08-31', headers=HEADERS[:-1])
        result = tracker.ingest(self.input, self.db)
        self.assertEqual((result['snapshots_added'], len(result['errors'])), (2, 1))
        self.assertIn('commitments_2026-08-31.xlsx', result['errors'][0])

        # Finance sends a corrected file under the same date: the next run backfills it.
        broken.unlink()
        self.copy('2026-08-31')
        result = tracker.ingest(self.input, self.db)
        self.assertEqual((result['snapshots_added'], result['errors']), (1, []))
        fresh = self.tmp / 'fresh.sqlite3'
        tracker.ingest(self.input, fresh)
        self.assertEqual(state(self.db), state(fresh))

    def test_invalid_file_changes_nothing(self):
        write_book(self.input, [ROW])
        tracker.ingest(self.input, self.db)
        before = state(self.db)
        cases = {
            'missing header': dict(headers=HEADERS[:-1]),
            'bad amount': dict(rows=[ROW[:5] + ['lots', 'Active']]),
            'conflicting duplicate': dict(rows=[ROW, ROW[:5] + [200, 'Active']]),
            'overlapping contracts': dict(rows=[ROW, ['K-2', 'C1', 'x', date(2026, 6, 30),
                                                      date(2026, 12, 31), 5, 'Active']]),
            'reversed term': dict(rows=[ROW[:3] + [date(2026, 7, 1), date(2026, 6, 30), 1, 'x']]),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                bad = write_book(self.input, **{'rows': [ROW], **kwargs}, as_of='2026-08-31')
                result = tracker.ingest(self.input, self.db)
                self.assertEqual((result['snapshots_added'], len(result['errors'])), (0, 1))
                self.assertEqual(state(self.db), before)
                bad.unlink()

    def test_published_snapshot_cannot_change(self):
        write_book(self.input, [ROW])
        tracker.ingest(self.input, self.db)
        write_book(self.input, [ROW[:5] + [999, 'Active']])
        before = state(self.db)
        self.assertIn('different contents', tracker.ingest(self.input, self.db)['errors'][0])
        self.assertEqual(state(self.db), before)

    def test_skips_blank_total_and_footnote_rows_and_exact_duplicates(self):
        write_book(self.input, [ROW, ROW, [None] * 7, ['TOTAL', None, None, None, None, '=SUM(F3:F4)'],
                                ['* unaudited']])
        self.assertEqual(tracker.ingest(self.input, self.db)['contracts'], 1)

    def test_warns_on_status_that_contradicts_dates_without_blocking(self):
        ended_early = ['K-2', 'C2', 'Customer', date(2026, 1, 1), date(2026, 12, 31), 100, 'Cancelled']
        rows = [ROW,  # Active, already past its end date: a stale label, not flagged
                ['K-3', 'C3', 'Customer', date(2026, 1, 1), date(2026, 6, 30), 100, ' EXPIRED '],
                ['K-4', 'C4', 'Customer', date(2026, 1, 1), date(2026, 12, 31), 100, 'On Hold'],
                ended_early]
        write_book(self.input, rows)
        result = tracker.ingest(self.input, self.db)
        self.assertEqual((result['snapshots_added'], result['contracts'], result['errors']), (1, 4, []))
        self.assertEqual(len(result['warnings']), 2)
        self.assertIn("K-2: status 'Cancelled' but end date 2026-12-31 is after snapshot 2026-07-31",
                      result['warnings'][0])
        self.assertIn("K-4: unknown status 'On Hold'", result['warnings'][1])
        # Reported on every run until the tracker is fixed.
        self.assertEqual(tracker.ingest(self.input, self.db)['warnings'], result['warnings'])

        # Finance corrects K-2's end date and K-4's label in the next export.
        rows[2][-1], rows[3][4] = 'Active', date(2026, 7, 15)
        write_book(self.input, rows, as_of='2026-08-31')
        self.assertEqual(tracker.ingest(self.input, self.db)['warnings'], [])


if __name__ == '__main__':
    unittest.main()
