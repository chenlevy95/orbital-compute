"""Tracker acceptance tests using real Excel files and real SQLite transactions.

Run: python -m unittest discover -s tests -v
Generated workbooks and databases live only in temporary directories. Process
tests use spawn, so they work on Windows as well as macOS/Linux.
"""

from contextlib import closing
from datetime import date
import json
import multiprocessing
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

import ingest_commitments as tracker


SOURCE = Path(__file__).resolve().parents[1] / 'data' / 'commitments'
FILES = sorted(SOURCE.glob('*.xlsx'))
HEADERS = ['Contract ID', 'Customer ID', 'Customer', 'Start Date', 'End Date',
           'Commit (USD)', 'Status', 'Notes']
ROW = ['K-1', 'C1', 'Customer', date(2026, 1, 1), date(2026, 6, 30), 100, 'Active', None]


def write_book(folder, rows=None, as_of='2026-07-31', headers=None):
    """Small source fixture; caller can mutate individual cells after saving."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Unexpected name'
    sheet.append(['Preamble'])
    sheet.append(headers or HEADERS)
    for row in rows if rows is not None else [ROW]:
        sheet.append(row)
    path = Path(folder) / f'commitments_{as_of}.xlsx'
    workbook.save(path)
    workbook.close()
    return path


def database_state(path):
    """Compare source bytes and every persisted value, not just row counts."""
    with closing(sqlite3.connect(path)) as connection:
        return {table: connection.execute(f'SELECT * FROM {table} ORDER BY 1, 2').fetchall()
                for table in ('commitment_snapshots', 'commitment_observations',
                              'contract_history', 'contracts_current')}


def worker(folder, database, start, results):
    start.wait(15)
    try:
        results.put(tracker.ingest(folder, database))
    except Exception as exc:
        results.put(str(exc))


def uncommitted_writer(database, ready):
    """Hold real unpublished mutations until the parent forcibly kills us."""
    connection = sqlite3.connect(database, isolation_level=None)
    tracker.prepare_database(connection, 5)
    connection.execute('DELETE FROM contract_history')
    connection.execute("UPDATE commitment_snapshots SET filename='uncommitted'")
    ready.set()
    # Do not create child-owned semaphores: termination cannot clean them up.
    time.sleep(30)


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / 'input'
        self.input.mkdir()
        self.database = self.root / 'tracker.sqlite3'

    def load(self):
        return tracker.ingest(self.input, self.database)

    def all_sources(self):
        for path in FILES:
            shutil.copyfile(path, self.input / path.name)

    def query(self, sql):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(sql).fetchall()

    def test_supplied_exports_and_exact_replay(self):
        self.all_sources()
        result = self.load()
        self.assertEqual((result['snapshots_added'], result['observations_added'],
                          result['history_versions'], result['contracts_current']), (3, 29, 16, 11))
        before = database_state(self.database)
        result = self.load()
        self.assertEqual(result['snapshots_added'], 0)
        self.assertEqual(result['observations_added'], 0)
        self.assertEqual(before, database_state(self.database))
        self.assertEqual(self.query("SELECT commitment_usd, end_date, observed_from, observed_to "
                                    "FROM contract_history WHERE contract_id='K-1009' "
                                    "ORDER BY observed_from"),
                         [('500000', '2026-11-30', '2026-07-31', '2026-08-31'),
                          ('650000', '2027-01-31', '2026-08-31', None)])
        self.assertEqual(self.query("SELECT status FROM contracts_current WHERE contract_id='K-1004'"),
                         [('Expired',)])
        self.assertEqual(self.query("SELECT source_rows_json FROM commitment_observations "
                                    "WHERE as_of='2026-09-28' AND contract_id='K-1008'"), [('[12,13]',)])
        self.assertTrue(any('duplicate K-1008' in w for w in result['warnings']))
        self.assertEqual(before['commitment_snapshots'][0][3], FILES[0].read_bytes())

    def test_incremental_and_late_older_inputs_equal_fresh(self):
        tracker.ingest(SOURCE, self.database)
        expected = database_state(self.database)
        for ordering in (FILES, list(reversed(FILES))):
            with tempfile.TemporaryDirectory() as directory:
                folder = Path(directory) / 'input'
                folder.mkdir()
                database = Path(directory) / 'other.sqlite3'
                for path in ordering:
                    # Only the incoming file is present: prior archive must survive.
                    for old in folder.glob('*.xlsx'):
                        old.unlink()
                    shutil.copyfile(path, folder / path.name)
                    tracker.ingest(folder, database)
                self.assertEqual(expected, database_state(database))

    def test_new_observation_same_terms_and_extra_only_change(self):
        write_book(self.input)
        self.load()
        row = list(ROW)
        row[-1] = 'A new note, not new core terms'
        write_book(self.input, [row], as_of='2026-08-31')
        result = self.load()
        self.assertEqual(result['observations_added'], 1)
        self.assertEqual(result['history_versions'], 1)
        extra = self.query("SELECT extras_json FROM commitment_observations WHERE as_of='2026-08-31'")[0][0]
        self.assertEqual(json.loads(extra)['notes']['value'], row[-1])

    def test_disappearance_and_reappearance_do_not_invent_versions(self):
        second = ['K-2', 'C2', 'Other', *ROW[3:]]
        write_book(self.input, [ROW, second])
        write_book(self.input, [second], as_of='2026-08-31')
        write_book(self.input, [ROW, second], as_of='2026-09-28')
        self.assertEqual(self.load()['history_versions'], 2)
        self.assertEqual(len(self.query('SELECT * FROM contracts_current')), 2)

    def test_same_date_conflict_rolls_back_whole_batch(self):
        path = write_book(self.input)
        self.load()
        before = database_state(self.database)
        # Earlier new file is inserted before the conflicting July file is checked.
        write_book(self.input, as_of='2026-06-30')
        workbook = load_workbook(path)
        workbook.active['F3'] = 200
        workbook.save(path)
        workbook.close()
        with self.assertRaisesRegex(tracker.TrackerError, 'immutable'):
            self.load()
        self.assertEqual(before, database_state(self.database))

    def test_layout_reordering_and_normalization(self):
        row = [' K-1 ', ' C1 ', ' Customer ', '1 Jan 2026', '2026-06-30', '$100.00', 'Active', None]
        path = write_book(self.input, [list(reversed(row))], headers=list(reversed(HEADERS)))
        snapshot = tracker.parse_snapshot(path)
        self.assertEqual(snapshot.rows[0][0], ('K-1', 'C1', 'Customer', '2026-01-01',
                                             '2026-06-30', '100', 'Active'))

    def test_invalid_rows_fail_with_location(self):
        cases = [(0, None), (3, '01/02/2026'), (4, '2025-01-01'),
                 (5, '$1,00'), (5, 0), (5, -1), (5, '=SUM(1,2)'), (6, None)]
        for column, value in cases:
            with self.subTest(column=column, value=value):
                row = list(ROW)
                row[column] = value
                path = write_book(self.input, [row])
                with self.assertRaisesRegex(tracker.TrackerError, 'Unexpected name:3'):
                    tracker.parse_snapshot(path)

    def test_conflicting_duplicate_and_ambiguous_header(self):
        other = list(ROW)
        other[5] = 200
        path = write_book(self.input, [ROW, other])
        with self.assertRaisesRegex(tracker.TrackerError, 'conflicting duplicate'):
            tracker.parse_snapshot(path)
        path = write_book(self.input)
        workbook = load_workbook(path)
        workbook.create_sheet('Second table').append(HEADERS)
        workbook.save(path)
        workbook.close()
        with self.assertRaisesRegex(tracker.TrackerError, 'found 2'):
            tracker.parse_snapshot(path)

    def test_unknown_header_and_empty_export_block(self):
        headers = list(HEADERS)
        headers[5] = 'Unreviewed revenue column'
        with self.assertRaisesRegex(tracker.TrackerError, 'header'):
            tracker.parse_snapshot(write_book(self.input, headers=headers))
        with self.assertRaisesRegex(tracker.TrackerError, 'no contract rows'):
            tracker.parse_snapshot(write_book(self.input, rows=[]))

    def test_inclusive_term_overlap_rolls_back_reconstruction(self):
        write_book(self.input)
        self.load()
        before = database_state(self.database)
        renewal = ['K-2', 'C1', 'Customer', date(2026, 6, 30), date(2026, 12, 31), 200, 'Signed', None]
        write_book(self.input, [renewal], as_of='2026-08-31')
        with self.assertRaisesRegex(tracker.TrackerError, 'overlapping'):
            self.load()
        self.assertEqual(before, database_state(self.database))
        renewal[3] = date(2026, 7, 1)
        write_book(self.input, [renewal], as_of='2026-08-31')
        self.assertEqual(self.load()['contracts_current'], 2)

    def test_usage_tables_are_not_changed(self):
        from ingest_usage import prepare_database
        with closing(sqlite3.connect(self.database, isolation_level=None)) as connection:
            prepare_database(connection, 5)
            connection.execute("INSERT INTO usage VALUES ('r','C1','2026-01-01','gpu',"
                               "'1','hour','2','final','2026-01-02T03:00:00Z')")
            connection.commit()
        before = self.query('SELECT * FROM usage')
        self.all_sources()
        self.load()
        self.load()
        self.assertEqual(before, self.query('SELECT * FROM usage'))

    def test_exception_after_history_writes_rolls_back(self):
        write_book(self.input)
        self.load()
        before = database_state(self.database)
        write_book(self.input, as_of='2026-08-31')
        original = tracker.rebuild_history

        def fail(connection):
            original(connection)
            raise RuntimeError('simulated failure after writes')

        with patch.object(tracker, 'rebuild_history', fail):
            with self.assertRaises(RuntimeError):
                self.load()
        self.assertEqual(before, database_state(self.database))

    def test_killed_writer_recovers(self):
        self.all_sources()
        self.load()
        before = database_state(self.database)
        context = multiprocessing.get_context('spawn')
        ready = context.Event()
        process = context.Process(target=uncommitted_writer, args=(self.database, ready))
        process.start()
        try:
            self.assertTrue(ready.wait(15), 'writer did not reach transaction')
            # WAL reader still sees the committed state while writer holds lock.
            self.assertEqual(before, database_state(self.database))
        finally:
            process.terminate()
            process.join(10)
        self.assertFalse(process.is_alive())
        self.load()
        self.assertEqual(before, database_state(self.database))

    def test_two_fresh_processes_publish_only_once(self):
        self.all_sources()
        context = multiprocessing.get_context('spawn')
        start, results = context.Event(), context.Queue()
        processes = [context.Process(target=worker, args=(self.input, self.database, start, results))
                     for _ in range(2)]
        for process in processes:
            process.start()
        start.set()
        try:
            output = [results.get(timeout=30) for _ in processes]
            self.assertTrue(all(isinstance(r, dict) for r in output), output)
            self.assertEqual(sorted(r['snapshots_added'] for r in output), [0, 3])
            before = database_state(self.database)
            self.load()
            self.assertEqual(before, database_state(self.database))
        finally:
            for process in processes:
                process.join(5)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            results.close()


if __name__ == '__main__':
    unittest.main()
