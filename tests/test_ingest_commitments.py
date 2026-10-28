"""Tracker acceptance tests using real Excel files and real SQLite transactions.

Run: python -m unittest discover -s tests -v
Generated workbooks and databases live only in temporary directories. Process
tests use spawn, so they work on Windows as well as macOS/Linux.
"""

from contextlib import closing
from datetime import date
from hashlib import sha256
import io
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
# Excel can leave ~$ lock workbooks beside exports while a user has them open.
FILES = sorted(SOURCE.glob('commitments_*.xlsx'))
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


def database_state(path, include_archive=True):
    """Compare source bytes and every persisted value, not just row counts."""
    with closing(sqlite3.connect(path)) as connection:
        tables = ('commitment_snapshots', 'commitment_observations',
                  'contract_history', 'contracts_current')
        if include_archive:
            tables += ('commitment_files',)
        return {table: connection.execute(f'SELECT * FROM {table} ORDER BY 1, 2').fetchall()
                for table in tables}


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
    connection.execute("UPDATE commitment_files SET errors_json='[\"uncommitted\"]'")
    ready.set()
    # Do not create child-owned semaphores: termination cannot clean them up.
    time.sleep(30)


def stop_during_publication(folder, database, ready):
    """The archive has committed when reconstruction begins; die before publish."""
    def wait_for_termination(connection):
        ready.set()
        time.sleep(30)
    with patch.object(tracker, 'rebuild_history', wait_for_termination):
        tracker.ingest(folder, database)


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / 'input'
        self.input.mkdir()
        self.database = self.root / 'tracker.sqlite3'

    def load(self, **kwargs):
        return tracker.ingest(self.input, self.database, **kwargs)

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
        self.assertEqual(self.query("SELECT workbook FROM commitment_files "
                                    "WHERE filename='commitments_2026-07-31.xlsx'")[0][0],
                         FILES[0].read_bytes())

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

    def test_same_date_conflict_is_archived_without_blocking_other_snapshots(self):
        path = write_book(self.input)
        self.load()
        original = self.query('SELECT checksum FROM commitment_snapshots')[0][0]
        # Earlier new file is inserted before the conflicting July file is checked.
        write_book(self.input, as_of='2026-06-30')
        workbook = load_workbook(path)
        workbook.active['F3'] = 200
        workbook.save(path)
        workbook.close()
        result = self.load()
        self.assertEqual(result['snapshots_added'], 1)
        self.assertEqual(result['quarantined_files'], 1)
        self.assertEqual(self.query("SELECT checksum FROM commitment_snapshots WHERE as_of='2026-07-31'"),
                         [(original,)])
        self.assertEqual(self.query('SELECT commitment_usd FROM contracts_current'), [('100',)])
        self.assertEqual(self.query('SELECT COUNT(*) FROM commitment_files'), [(3,)])

    def test_layout_reordering_and_normalization(self):
        row = [' K-1 ', ' C1 ', ' Customer ', '1 Jan 2026', '2026-06-30', '$100.00', 'Active', None]
        path = write_book(self.input, [list(reversed(row))], headers=list(reversed(HEADERS)))
        snapshot = tracker.parse_snapshot(path)
        self.assertEqual(snapshot.rows[0][0], ('K-1', 'C1', 'Customer', '2026-01-01',
                                             '2026-06-30', '100', 'Active'))

    def test_invalid_rows_fail_with_location(self):
        cases = [(0, None), (3, '01/02/2026'), (4, '2025-01-01'),
                 (5, '$1,00'), (5, 0), (5, -1), (5, '=SUM(1,2)')]
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

    def test_missing_essential_header_archives_and_exact_replay_changes_nothing(self):
        write_book(self.input)
        self.load()
        published = database_state(self.database, include_archive=False)
        missing_index = HEADERS.index('Commit (USD)')
        headers = [value for i, value in enumerate(HEADERS) if i != missing_index]
        row = [value for i, value in enumerate(ROW) if i != missing_index]
        rejected = write_book(self.input, [row], headers=headers, as_of='2026-08-31')
        expected_bytes = rejected.read_bytes()
        result = self.load()
        self.assertEqual(result['files_archived'], 1)
        self.assertEqual(result['snapshots_added'], 0)
        self.assertEqual(result['quarantined_files'], 1)
        self.assertIn('commitment_usd', str(result['quarantines']))
        self.assertEqual(published, database_state(self.database, include_archive=False))
        self.assertEqual(self.query("SELECT workbook, state FROM commitment_files "
                                    "WHERE filename='commitments_2026-08-31.xlsx'"),
                         [(expected_bytes, 'quarantined')])
        before = database_state(self.database)
        replay = self.load()
        self.assertEqual(replay['files_archived'], 0)
        self.assertEqual(before, database_state(self.database))
        # Failed reprocessing should not append timestamps or duplicate findings.
        self.load(reprocess=True)
        self.assertEqual(before, database_state(self.database))

    def test_missing_descriptive_headers_retain_known_values_without_new_version(self):
        write_book(self.input)
        self.load()
        included = [i for i in range(len(HEADERS)) if i not in (2, 6)]
        headers = [HEADERS[i] for i in included]
        row = [ROW[i] for i in included]
        write_book(self.input, [row], headers=headers, as_of='2026-08-31')
        result = self.load()
        self.assertEqual(result['quarantined_files'], 0)
        self.assertEqual(result['snapshots_added'], 1)
        self.assertEqual(result['history_versions'], 1)
        values = self.query("SELECT customer_name, status, missing_fields_json "
                            "FROM commitment_observations WHERE as_of='2026-08-31'")[0]
        self.assertEqual(values[:2], (None, None))
        self.assertEqual(set(json.loads(values[2])), {'customer_name', 'status'})
        self.assertEqual(self.query('SELECT customer_name, status FROM contracts_current'),
                         [('Customer', 'Active')])
        self.assertTrue(result['warnings'])

    def test_new_contract_without_descriptive_headers_publishes_nulls(self):
        included = [i for i in range(len(HEADERS)) if i not in (2, 6)]
        write_book(self.input, [[ROW[i] for i in included]], headers=[HEADERS[i] for i in included])
        result = self.load()
        self.assertEqual(result['quarantined_files'], 0)
        self.assertEqual(self.query('SELECT customer_name, status FROM contracts_current'),
                         [(None, None)])

    def test_explicit_blank_descriptive_values_create_version_distinct_from_absence(self):
        write_book(self.input)
        self.load()
        row = list(ROW)
        row[2], row[6] = None, '   '
        write_book(self.input, [row], as_of='2026-08-31')
        result = self.load()
        self.assertEqual(result['history_versions'], 2)
        self.assertEqual(self.query('SELECT customer_name, status FROM contracts_current'), [(None, None)])
        absent = self.query("SELECT missing_fields_json FROM commitment_observations "
                            "WHERE as_of='2026-08-31'")[0][0]
        self.assertEqual(json.loads(absent), [])

    def test_mixed_batch_and_corrected_quarantined_file(self):
        invalid_row = list(ROW)
        invalid_row[5] = 0
        rejected = write_book(self.input, [invalid_row])
        original = rejected.read_bytes()
        write_book(self.input, as_of='2026-08-31')
        result = self.load()
        self.assertEqual((result['files_archived'], result['snapshots_added'],
                          result['quarantined_files']), (2, 1, 1))
        self.assertEqual(self.query('SELECT as_of FROM commitment_snapshots'), [('2026-08-31',)])
        write_book(self.input)
        result = self.load()
        self.assertEqual((result['snapshots_added'], result['quarantined_files']), (1, 0))
        self.assertEqual(self.query("SELECT workbook, state FROM commitment_files "
                                    f"WHERE checksum='{sha256(original).hexdigest()}'"),
                         [(original, 'superseded')])
        self.assertEqual(self.query('SELECT COUNT(*) FROM commitment_files'), [(3,)])

    def test_reprocess_after_reviewed_alias_change_uses_archived_bytes(self):
        headers = list(HEADERS)
        headers[5] = 'New commitment heading'
        source = write_book(self.input, headers=headers)
        self.assertEqual(self.load()['quarantined_files'], 1)
        source.unlink()
        with patch.dict(tracker.ALIASES, {'new commitment heading': 'commitment_usd'}):
            result = self.load(reprocess=True)
        self.assertEqual((result['files_archived'], result['snapshots_added'],
                          result['quarantined_files']), (0, 1, 0))
        self.assertEqual(self.query('SELECT commitment_usd FROM contracts_current'), [('100',)])

    def test_corrupt_workbook_is_archived_and_quarantined(self):
        source = self.input / 'commitments_2026-07-31.xlsx'
        original = b'This is not a ZIP workbook.'
        source.write_bytes(original)
        result = self.load()
        self.assertEqual((result['files_archived'], result['snapshots_added'],
                          result['quarantined_files']), (1, 0, 1))
        self.assertEqual(self.query('SELECT workbook FROM commitment_files'), [(original,)])
        self.assertEqual(self.query('SELECT COUNT(*) FROM contracts_current'), [(0,)])

    def test_invalid_filename_date_is_archived_for_review(self):
        source = write_book(self.input)
        source.rename(self.input / 'commitments_2026-02-30.xlsx')
        result = self.load()
        self.assertEqual(result['quarantined_files'], 1)
        self.assertEqual(self.query('SELECT as_of, state FROM commitment_files'), [(None, 'quarantined')])

    def test_late_snapshot_cannot_invalidate_previously_published_later_terms(self):
        later = ['K-2', 'C1', 'Customer', date(2026, 7, 1), date(2026, 12, 31), 200, 'Active', None]
        write_book(self.input, [later], as_of='2026-08-31')
        self.load()
        before = database_state(self.database, include_archive=False)
        earlier = list(ROW)
        earlier[4] = date(2026, 7, 31)
        write_book(self.input, [earlier])
        result = self.load()
        self.assertEqual(result['quarantined_files'], 1)
        self.assertIn('overlapping', str(result['quarantines']))
        self.assertEqual(before, database_state(self.database, include_archive=False))

    def test_cli_returns_nonzero_for_visible_quarantine(self):
        source = self.input / 'commitments_2026-07-31.xlsx'
        source.write_bytes(b'corrupt')
        output = io.StringIO()
        with patch('sys.argv', ['ingest_commitments.py', '--input-dir', str(self.input),
                                '--database', str(self.database)]), patch('sys.stdout', output):
            code = tracker.main()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())['quarantined_files'], 1)

    def test_inclusive_term_overlap_rolls_back_reconstruction(self):
        write_book(self.input)
        self.load()
        before = database_state(self.database, include_archive=False)
        renewal = ['K-2', 'C1', 'Customer', date(2026, 6, 30), date(2026, 12, 31), 200, 'Signed', None]
        write_book(self.input, [renewal], as_of='2026-08-31')
        result = self.load()
        self.assertEqual(result['quarantined_files'], 1)
        self.assertIn('overlapping', str(result['quarantines']))
        self.assertEqual(before, database_state(self.database, include_archive=False))
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
        before = database_state(self.database, include_archive=False)
        write_book(self.input, as_of='2026-08-31')
        original = tracker.rebuild_history

        def fail(connection):
            original(connection)
            raise RuntimeError('simulated failure after writes')

        with patch.object(tracker, 'rebuild_history', fail):
            with self.assertRaises(RuntimeError):
                self.load()
        self.assertEqual(before, database_state(self.database, include_archive=False))
        self.assertEqual(self.query("SELECT COUNT(*) FROM commitment_files WHERE state='pending'"), [(1,)])

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

    def test_killed_publication_resumes_pending_archive_without_input_file(self):
        source = write_book(self.input)
        original = source.read_bytes()
        context = multiprocessing.get_context('spawn')
        ready = context.Event()
        process = context.Process(target=stop_during_publication,
                                  args=(self.input, self.database, ready))
        process.start()
        try:
            self.assertTrue(ready.wait(15), 'writer did not reach publication')
            self.assertEqual(self.query('SELECT workbook, state FROM commitment_files'),
                             [(original, 'pending')])
            self.assertEqual(self.query('SELECT COUNT(*) FROM contract_history'), [(0,)])
        finally:
            process.terminate()
            process.join(10)
        self.assertFalse(process.is_alive())
        source.unlink()
        result = self.load()
        self.assertEqual((result['files_archived'], result['snapshots_added'],
                          result['quarantined_files']), (0, 1, 0))
        self.assertEqual(self.query('SELECT state FROM commitment_files'), [('published',)])

    def test_populated_v1_database_migrates_without_business_changes(self):
        """Build the old schema explicitly: using today's schema would not test migration."""
        source = write_book(self.input)
        parsed = tracker.parse_snapshot(source)
        core, extra, references = parsed.rows[0]
        fields = ', '.join(f'{name} TEXT NOT NULL' for name in tracker.CORE)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute('''CREATE TABLE commitment_snapshots (
                as_of TEXT PRIMARY KEY, filename TEXT NOT NULL, checksum TEXT NOT NULL,
                workbook BLOB NOT NULL, layout_json TEXT NOT NULL)''')
            connection.execute(f'''CREATE TABLE commitment_observations (
                as_of TEXT NOT NULL REFERENCES commitment_snapshots(as_of), {fields},
                extras_json TEXT NOT NULL, source_rows_json TEXT NOT NULL,
                PRIMARY KEY(as_of, contract_id))''')
            connection.execute(f'''CREATE TABLE contract_history (
                {fields}, observed_from TEXT NOT NULL, observed_to TEXT,
                PRIMARY KEY(contract_id, observed_from),
                CHECK(observed_to IS NULL OR observed_to > observed_from))''')
            connection.execute('''CREATE UNIQUE INDEX contract_current_idx ON
                contract_history(contract_id) WHERE observed_to IS NULL''')
            connection.execute('''CREATE VIEW contracts_current AS
                SELECT * FROM contract_history WHERE observed_to IS NULL''')
            connection.execute('INSERT INTO commitment_snapshots VALUES (?, ?, ?, ?, ?)',
                               (parsed.as_of, parsed.filename, parsed.checksum, parsed.content, parsed.layout))
            connection.execute('INSERT INTO commitment_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                               (parsed.as_of, *core, extra, json.dumps(references)))
            connection.execute('INSERT INTO contract_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)',
                               (*core, parsed.as_of))
            connection.execute('CREATE TABLE usage (record_id TEXT PRIMARY KEY, amount_usd TEXT)')
            connection.execute("INSERT INTO usage VALUES ('untouched', '12.34')")
            connection.commit()
        old_history = self.query('SELECT * FROM contract_history')
        result = self.load()
        self.assertEqual(result['snapshots_added'], 0)
        self.assertEqual(old_history, self.query('SELECT * FROM contract_history'))
        self.assertEqual(self.query('SELECT * FROM usage'), [('untouched', '12.34')])
        self.assertEqual(self.query('SELECT workbook, state FROM commitment_files'),
                         [(source.read_bytes(), 'published')])
        self.assertEqual(self.query('SELECT missing_fields_json FROM commitment_observations'), [('[]',)])
        self.assertEqual(self.query('PRAGMA foreign_key_check'), [])
        for table in ('commitment_observations', 'contract_history'):
            info = {row[1]: row for row in self.query(f'PRAGMA table_info({table})')}
            self.assertEqual(info['customer_name'][3], 0)
            self.assertEqual(info['status'][3], 0)
        before = database_state(self.database)
        self.load()
        self.assertEqual(before, database_state(self.database))
        # The migrated schema must also accept the newly optional values.
        included = [i for i in range(len(HEADERS)) if i not in (2, 6)]
        new_row = list(ROW)
        new_row[0], new_row[1] = 'K-NEW', 'C-NEW'
        write_book(self.input, [[new_row[i] for i in included]],
                   headers=[HEADERS[i] for i in included], as_of='2026-08-31')
        self.assertEqual(self.load()['snapshots_added'], 1)

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
