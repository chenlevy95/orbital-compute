"""Load Finance's Excel snapshots without losing contracts or their history.

The three layers deliberately answer different questions:
  snapshots: what file did Finance send (immutable evidence)?
  observations: what did each row mean after normalization?
  history: when did we first observe each set of core contract values?

Snapshot dates are observation dates, NOT amendment effective dates. Missing
rows never delete a contract. A replay of identical files writes no new data.
Only openpyxl is needed beyond Python's standard library; no API is contacted.
"""

import argparse
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.datetime import from_excel


# Required, standardized fields that define a contract version after alias mapping.
# Used to identify the header row, validate each contract, and keep values in a
# consistent order when storing/comparing database rows. For the same contract_id,
# a change to another CORE field creates a new history version. Extra fields such
# as notes or account owner are preserved separately without creating a version.
CORE = ('contract_id', 'customer_id', 'customer_name', 'start_date',
        'end_date', 'commitment_usd', 'status')

# Map Finance's Excel headers (left) to our stable code/database fields (right).
# header_name() normalizes capitalization and whitespace before this lookup.
# Different export layouts can name the same field differently; keep those
# aliases together below. Add a new alias only after confirming its meaning.
ALIASES = {
    # Contract ID headers
    'contract id': 'contract_id',

    # Customer ID headers
    'customer id': 'customer_id',

    # Customer name headers
    'customer': 'customer_name',

    # Start date headers
    'start date': 'start_date',

    # End date headers
    'end date': 'end_date',

    # Commitment amount headers — both names represent the same USD amount
    'total commitment (usd)': 'commitment_usd',
    'commit (usd)': 'commitment_usd',

    # Status headers
    'status': 'status',
}

MONTHS = {name: index for index, name in enumerate(
    ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep',
     'oct', 'nov', 'dec'), 1)}


class TrackerError(Exception):
    """An actionable source or business-rule error, safe to print to stderr."""


@dataclass(frozen=True)
class Snapshot:
    """Validated input held outside the write transaction; rows are deduplicated."""

    as_of: str
    filename: str
    checksum: str
    content: bytes
    layout: str
    # Each row is (seven core values, canonical extra-column JSON, row numbers).
    rows: tuple
    warnings: tuple


def json_text(value):
    """Stable JSON makes equality independent of dictionary insertion order."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def header_name(value):
    return ' '.join(str(value).split()).casefold() if value is not None else ''


def text_value(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('expected nonempty text')
    return value.strip()


def date_value(value, epoch):
    """Accept Excel dates, ISO dates and '1 Jun 2026', independent of OS locale."""
    if isinstance(value, bool):
        raise ValueError('boolean is not a date')
    if isinstance(value, (int, float)):
        value = from_excel(value, epoch)
    if isinstance(value, datetime):
        if value.time() != datetime.min.time():
            raise ValueError('date must not contain a time')
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            return date.fromisoformat(value).isoformat()
        match = re.fullmatch(r'(\d{1,2}) ([A-Za-z]{3}) (\d{4})', value)
        if match and match[2].lower() in MONTHS:
            return date(int(match[3]), MONTHS[match[2].lower()], int(match[1])).isoformat()
    raise ValueError('expected Excel date, YYYY-MM-DD, or D Mon YYYY')


def money_value(value):
    """Store positive USD as decimal TEXT; never sum financial values as floats.

    Excel numeric cells already arrive as Python numbers. Decimal(str(value))
    avoids adding further binary rounding; it cannot recover precision Excel lost.
    Text amounts accept US grouping explicitly, not arbitrary comma removal.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('expected a positive USD amount')
    if isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r'\$?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?', value):
            raise ValueError('expected USD number, optionally $ and US thousands separators')
        value = value.replace('$', '').replace(',', '')
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError('commitment must be finite and greater than zero')
    result = format(number, 'f')
    return result.rstrip('0').rstrip('.') if '.' in result else result


def extra_value(cell):
    """Typed JSON retains extra values; workbook bytes remain the exact source."""
    value = cell.value
    if isinstance(value, (date, datetime)):
        value = value.isoformat()
    return {'type': cell.data_type, 'value': value}


def parse_snapshot(path):
    """Locate exactly one table, normalize rows, and fail rather than guess.

    A complete header match prevents the September 'Read me' sheet's lone
    'Contract ID' cell from being mistaken for the actual table. All columns are
    addressed by header, never by a fixed position or hardcoded sheet name.
    """
    path = Path(path)
    # Require the entire filename to look like commitments_2026-07-31.xlsx.
    # \d{4} means four digits; \d{2} means two; \. matches a literal dot.
    # Parentheses capture YYYY-MM-DD as match[1] for extraction below.
    # This checks the filename's shape; date.fromisoformat checks calendar validity.
    match = re.fullmatch(r'commitments_(\d{4}-\d{2}-\d{2})\.xlsx', path.name)
    if not match:
        raise TrackerError(f'{path.name}: expected commitments_YYYY-MM-DD.xlsx')
    try:
        as_of = date.fromisoformat(match[1]).isoformat()
    except ValueError as exc:
        raise TrackerError(f'{path.name}: invalid snapshot date') from exc
    content = path.read_bytes()
    try:
        workbook = load_workbook(BytesIO(content), data_only=False)
    except (BadZipFile, ValueError, KeyError, OSError) as exc:
        raise TrackerError(f'{path.name}: cannot read Excel workbook: {exc}') from exc
    try:
        candidates = []
        for sheet in workbook:
            for row in sheet.iter_rows():
                names = [ALIASES.get(header_name(c.value), header_name(c.value)) for c in row]
                if set(CORE).issubset(names):
                    candidates.append((sheet, row[0].row, names))
        if len(candidates) != 1:
            raise TrackerError(f'{path.name}: expected one complete required header row; '
                               f'found {len(candidates)}. Review sheet layout/header aliases.')
        sheet, header_row, names = candidates[0]
        nonempty = [name for name in names if name]
        if len(set(nonempty)) != len(nonempty):
            raise TrackerError(f'{path.name}:{sheet.title}:{header_row}: duplicate/ambiguous headers')
        columns = {name: i for i, name in enumerate(names) if name}
        records = {}
        warnings = []
        extras = sorted(set(columns) - set(CORE))
        if extras:
            warnings.append(f'{path.name}: extra columns retained: {", ".join(extras)}')
        for row in sheet.iter_rows(min_row=header_row + 1):
            values = [cell.value for cell in row]
            if all(v is None or (isinstance(v, str) and not v.strip()) for v in values):
                continue
            identifier = values[columns['contract_id']]
            customer = values[columns['customer_id']]
            # Recognize the supplied TOTAL and single-cell footnote, not any row
            # whose contract ID happens to be missing or malformed.
            if isinstance(identifier, str) and identifier.strip().upper() == 'TOTAL' and not customer:
                if all(values[columns[k]] is None for k in
                       ('customer_name', 'start_date', 'end_date', 'status')):
                    continue
            if (isinstance(identifier, str) and identifier.strip().startswith('*')
                    and all(v is None for i, v in enumerate(values) if i != columns['contract_id'])):
                continue
            location = f'{path.name}:{sheet.title}:{row[0].row}'
            core = []
            for field in CORE:
                cell = row[columns[field]]
                try:
                    if cell.data_type in ('f', 'e'):
                        raise ValueError('formula/error cells are not allowed in core fields')
                    if field in ('start_date', 'end_date'):
                        value = date_value(cell.value, workbook.epoch)
                    elif field == 'commitment_usd':
                        value = money_value(cell.value)
                    else:
                        value = text_value(cell.value)
                    core.append(value)
                except (ValueError, TypeError, InvalidOperation, OverflowError) as exc:
                    raise TrackerError(f'{location}: {field}: {exc}') from exc
            if core[3] > core[4]:
                raise TrackerError(f'{location}: end_date precedes start_date')
            if any(values[i] is not None for i, name in enumerate(names) if not name):
                raise TrackerError(f'{location}: populated column has no header')
            extra = json_text({name: extra_value(row[columns[name]]) for name in extras})
            identifier = core[0]
            core = tuple(core)
            if identifier in records:
                previous, previous_extra, source_rows = records[identifier]
                if (previous, previous_extra) != (core, extra):
                    raise TrackerError(f'{location}: conflicting duplicate contract {identifier}')
                source_rows.append(row[0].row)
                warnings.append(f'{location}: collapsed duplicate {identifier}')
            else:
                records[identifier] = (core, extra, [row[0].row])
        if not records:
            raise TrackerError(f'{path.name}:{sheet.title}: no contract rows; refusing empty export')
        layout = json_text({'sheet': sheet.title, 'header_row': header_row, 'columns': names})
        return Snapshot(as_of, path.name, sha256(content).hexdigest(), content, layout,
                        tuple(records[key] for key in sorted(records)), tuple(warnings))
    finally:
        workbook.close()


def prepare_database(connection, lock_timeout):
    """Acquire the only writer slot. Do not touch the usage loader's tables."""
    deadline = time.monotonic() + lock_timeout
    while True:
        try:
            connection.execute('PRAGMA journal_mode=WAL')
            break
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    connection.execute('PRAGMA synchronous=FULL')
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('BEGIN IMMEDIATE')
    # Avoid executescript: it implicitly commits an existing transaction.
    connection.execute('''CREATE TABLE IF NOT EXISTS commitment_snapshots (
        as_of TEXT PRIMARY KEY, filename TEXT NOT NULL, checksum TEXT NOT NULL,
        workbook BLOB NOT NULL, layout_json TEXT NOT NULL)''')
    fields = ', '.join(f'{field} TEXT NOT NULL' for field in CORE)
    connection.execute(f'''CREATE TABLE IF NOT EXISTS commitment_observations (
        as_of TEXT NOT NULL REFERENCES commitment_snapshots(as_of), {fields},
        extras_json TEXT NOT NULL, source_rows_json TEXT NOT NULL,
        PRIMARY KEY (as_of, contract_id))''')
    connection.execute(f'''CREATE TABLE IF NOT EXISTS contract_history (
        {fields}, observed_from TEXT NOT NULL, observed_to TEXT,
        PRIMARY KEY (contract_id, observed_from),
        CHECK (observed_to IS NULL OR observed_to > observed_from))''')
    connection.execute('''CREATE UNIQUE INDEX IF NOT EXISTS contract_current_idx
        ON contract_history(contract_id) WHERE observed_to IS NULL''')
    connection.execute('''CREATE VIEW IF NOT EXISTS contracts_current AS
        SELECT * FROM contract_history WHERE observed_to IS NULL''')


def validate_nonoverlap(current, as_of):
    """Inclusive ends mean next.start must be strictly AFTER previous.end.

    Check latest-known terms for ALL contracts, including ones missing from this
    snapshot. Status is descriptive: 'Expired' does not erase a historical term.
    """
    ordered = sorted(current.values(), key=lambda row: (row[1], row[3], row[0]))
    for previous, row in zip(ordered, ordered[1:]):
        if previous[1] == row[1] and row[3] <= previous[4]:
            raise TrackerError(f'as of {as_of}: overlapping inclusive terms for customer '
                               f'{row[1]}: {previous[0]} and {row[0]}')


def rebuild_history(connection):
    """Reconstruct deterministically, so late older snapshots need no special case.

    This is intentionally a full rebuild for small Finance trackers. Natural
    keys and source dates keep results identical regardless of arrival order.
    Only run when a NEW snapshot was added, and inside the publishing transaction.
    """
    connection.execute('DELETE FROM contract_history')
    current = {}
    for (as_of,) in connection.execute('SELECT as_of FROM commitment_snapshots ORDER BY as_of'):
        rows = connection.execute(
            f'SELECT {", ".join(CORE)} FROM commitment_observations '
            'WHERE as_of=? ORDER BY contract_id', (as_of,)).fetchall()
        for row in rows:
            identifier = row[0]
            if current.get(identifier) == row:
                continue
            connection.execute('UPDATE contract_history SET observed_to=? '
                               'WHERE contract_id=? AND observed_to IS NULL', (as_of, identifier))
            connection.execute('INSERT INTO contract_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)',
                               (*row, as_of))
            current[identifier] = row
        validate_nonoverlap(current, as_of)


def ingest(input_dir, database, lock_timeout=60):
    """Parse first, then atomically publish all new snapshots. Return JSON counters.

    Matching bytes are skipped under the lock. A different file claiming an
    existing date fails instead of rewriting evidence. Original files need not
    remain in input_dir after loading; the database holds the complete archive.
    """
    paths = sorted(Path(input_dir).glob('commitments_*.xlsx'))
    if not paths:
        raise TrackerError(f'{input_dir}: no commitments_YYYY-MM-DD.xlsx files found')
    snapshots = [parse_snapshot(path) for path in paths]
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=lock_timeout, isolation_level=None)
    added = skipped = observations = 0
    try:
        prepare_database(connection, lock_timeout)
        for snapshot in snapshots:
            existing = connection.execute('SELECT checksum FROM commitment_snapshots WHERE as_of=?',
                                          (snapshot.as_of,)).fetchone()
            if existing:
                if existing[0] != snapshot.checksum:
                    raise TrackerError(f'{snapshot.filename}: different contents for stored date '
                                       f'{snapshot.as_of}; original snapshot is immutable')
                skipped += 1
                continue
            connection.execute('INSERT INTO commitment_snapshots VALUES (?, ?, ?, ?, ?)',
                               (snapshot.as_of, snapshot.filename, snapshot.checksum,
                                snapshot.content, snapshot.layout))
            connection.executemany(
                'INSERT INTO commitment_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                [(snapshot.as_of, *core, extra, json_text(source_rows))
                 for core, extra, source_rows in snapshot.rows])
            added += 1
            observations += len(snapshot.rows)
        if added:
            rebuild_history(connection)
        history_count = connection.execute('SELECT COUNT(*) FROM contract_history').fetchone()[0]
        current_count = connection.execute('SELECT COUNT(*) FROM contracts_current').fetchone()[0]
        connection.commit()
        return {'snapshots_added': added, 'snapshots_unchanged': skipped,
                'observations_added': observations, 'history_versions': history_count,
                'contracts_current': current_count,
                'warnings': [warning for snapshot in snapshots for warning in snapshot.warnings]}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir', default='data/commitments', help='directory of dated Excel exports')
    parser.add_argument('--database', default=os.getenv('USAGE_DATABASE', 'storage/usage.sqlite3'))
    args = parser.parse_args()
    try:
        print(json.dumps(ingest(args.input_dir, args.database), sort_keys=True))
        return 0
    except KeyboardInterrupt:
        print('Tracker ingestion interrupted; uncommitted changes rolled back', file=sys.stderr)
        return 130
    except (TrackerError, OSError, sqlite3.Error) as exc:
        print(f'Tracker ingestion failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
