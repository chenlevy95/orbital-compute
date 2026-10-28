"""Load Finance's Excel snapshots without losing contracts or their history.

The layers deliberately answer different questions:
  files: what did Finance send, even if it failed validation (immutable evidence)?
  snapshots: which exports passed validation and were published?
  observations: what did each row mean after normalization?
  history: when did we first observe each set of core contract values?

Snapshot dates are observation dates, NOT amendment effective dates. Missing
rows never delete a contract. Replays do not duplicate evidence or history.
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
from xml.etree.ElementTree import ParseError

from openpyxl import load_workbook
from openpyxl.utils.datetime import from_excel


# Standardized fields and their storage/comparison order. CORE describes what we
# track, not what must arrive in every export. REQUIRED fields below determine
# whether a snapshot is safe to publish. Changes to known CORE values create a
# history version; extra fields such as notes are retained only in observations.
CORE = ('contract_id', 'customer_id', 'customer_name', 'start_date',
        'end_date', 'commitment_usd', 'status')

# Financial calculations need all five essential fields. Never fill missing
# essential values from an older export: that could conceal an amended term.
REQUIRED = ('contract_id', 'customer_id', 'start_date', 'end_date', 'commitment_usd')

# Missing descriptive columns are allowed. An absent column means "not supplied"
# (retain the last known value); a present empty cell means an explicit NULL.
DESCRIPTIVE = ('customer_name', 'status')

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

# Month abbreviations are parsed explicitly so text dates do not depend on the
# machine's language or locale.
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
    missing_fields: tuple = ()


def json_text(value):
    """Stable JSON makes equality independent of dictionary insertion order."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def header_name(value):
    """Normalize a header's whitespace and case before looking up its alias."""
    return ' '.join(str(value).split()).casefold() if value is not None else ''


def text_value(value):
    """Require nonblank text and trim whitespace without coercing other types."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError('expected nonempty text')
    return value.strip()


def date_value(value, epoch):
    """Accept Excel dates, ISO dates and '1 Jun 2026', independent of OS locale."""
    # bool is an int subclass in Python, but True/False are never Excel dates.
    if isinstance(value, bool):
        raise ValueError('boolean is not a date')
    # Excel stores dates as serial numbers; conversion needs this workbook's
    # epoch because older Excel workbooks use a different starting date.
    if isinstance(value, (int, float)):
        value = from_excel(value, epoch)
    if isinstance(value, datetime):
        # Contract terms are dates, not timestamps: silently dropping a time
        # would make different source values appear identical.
        if value.time() != datetime.min.time():
            raise ValueError('date must not contain a time')
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        value = value.strip()
        # Keep accepted text formats explicit rather than interpreting dates
        # according to the host machine's locale.
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
        # Validate grouping before removing separators; blindly deleting commas
        # could turn malformed values such as "1,00" into a plausible amount.
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


def snapshot_date(filename):
    """Extract a real observation date from the source name, never from its notes."""
    # Require the entire filename to look like commitments_2026-07-31.xlsx.
    # \d{4} means four digits; \d{2} means two; \. matches a literal dot.
    # Parentheses capture YYYY-MM-DD as match[1] for extraction below.
    # This checks the filename's shape; date.fromisoformat checks calendar validity.
    match = re.fullmatch(r'commitments_(\d{4}-\d{2}-\d{2})\.xlsx', filename)
    if not match:
        raise TrackerError(f'{filename}: expected commitments_YYYY-MM-DD.xlsx')
    try:
        return date.fromisoformat(match[1]).isoformat()
    except ValueError as exc:
        raise TrackerError(f'{filename}: invalid snapshot date') from exc


def _find_snapshot_table(workbook, filename):
    """Find the only complete header row and map canonical field names to columns.

    Every required field must occur in the same row. ``most_complete_headers``
    exists only to make the no-match error useful; it does not relax validation.
    """
    candidates = []
    most_complete_headers = set()
    required = set(REQUIRED)

    for sheet in workbook:
        for row in sheet.iter_rows():
            names = [ALIASES.get(header_name(cell.value), header_name(cell.value))
                     for cell in row]
            recognized = required.intersection(names)
            if len(recognized) > len(most_complete_headers):
                most_complete_headers = recognized
            # Do not combine headers spread across separate rows or sheets:
            # that could silently map data under the wrong columns.
            if required.issubset(names):
                candidates.append((sheet, row[0].row, names))

    # Zero candidates means no usable table; multiple candidates mean the
    # intended table is ambiguous. In either case, guessing could misread data.
    if len(candidates) != 1:
        missing = ', '.join(field for field in REQUIRED
                            if field not in most_complete_headers)
        detail = (f' Missing essential headers in best recognized row: {missing}.'
                  if not candidates else '')
        raise TrackerError(f'{filename}: expected one complete essential header row; '
                           f'found {len(candidates)}.{detail} '
                           'Review sheet layout/header aliases.')

    sheet, header_row, names = candidates[0]
    nonempty = [name for name in names if name]
    # Two source columns resolving to the same canonical field are ambiguous,
    # even if one of them happens to be empty in the current export.
    if len(set(nonempty)) != len(nonempty):
        raise TrackerError(f'{filename}:{sheet.title}:{header_row}: '
                           'duplicate/ambiguous headers')
    columns = {name: index for index, name in enumerate(names) if name}
    return sheet, header_row, names, columns


def _is_ignored_snapshot_row(values, columns):
    """Skip blank lines and the known TOTAL/footnote rows in Finance exports."""
    # Empty rows are layout, not contracts.
    if all(value is None or (isinstance(value, str) and not value.strip())
           for value in values):
        return True

    identifier = values[columns['contract_id']]
    customer = values[columns['customer_id']]
    # A row named TOTAL is ignored only when its customer and term cells are
    # empty; this avoids discarding a real contract with that identifier.
    if isinstance(identifier, str) and identifier.strip().upper() == 'TOTAL' and not customer:
        footer_fields = ('customer_name', 'start_date', 'end_date', 'status')
        if all(values[columns[field]] is None for field in footer_fields if field in columns):
            return True

    # Some exports include a one-cell footnote beginning with "*". A star in a
    # contract ID alone is not enough to ignore a populated row.
    return (isinstance(identifier, str) and identifier.strip().startswith('*')
            and all(value is None for index, value in enumerate(values)
                    if index != columns['contract_id']))


def _parse_core_values(row, columns, missing_fields, workbook, location, warnings):
    """Normalize fields in CORE order and report invalid values at their source.

    Missing descriptive columns become None here, while ``missing_fields``
    preserves the distinction from a present column whose cell is blank.
    """
    core = []
    for field in CORE:
        if field in missing_fields:
            core.append(None)
            continue

        cell = row[columns[field]]
        try:
            # Formulas are not calculated when the workbook is read, and Excel
            # errors are not valid contract values; neither is safe to publish.
            if cell.data_type in ('f', 'e'):
                raise ValueError('formula/error cells are not allowed in core fields')
            if field in ('start_date', 'end_date'):
                value = date_value(cell.value, workbook.epoch)
            elif field == 'commitment_usd':
                value = money_value(cell.value)
            elif field in DESCRIPTIVE and (
                    cell.value is None or
                    isinstance(cell.value, str) and not cell.value.strip()):
                value = None
                warnings.append(f'{location}: {field}: explicit empty value recorded')
            else:
                value = text_value(cell.value)
            core.append(value)
        except (ValueError, TypeError, InvalidOperation, OverflowError) as exc:
            # Preserve the specific validation message but add enough source
            # location to find the bad cell without stepping through the parser.
            raise TrackerError(f'{location}: {field}: {exc}') from exc

    # The normalized dates are ISO strings, so lexical ordering is chronological.
    if core[3] > core[4]:
        raise TrackerError(f'{location}: end_date precedes start_date')
    return tuple(core)


def _add_snapshot_record(records, core, extra, row_number, location, warnings):
    """Deduplicate identical contract rows, retaining every source row number.

    Rows with the same contract ID but different core or extra data are rejected:
    choosing one would hide a conflict in the source workbook.
    """
    identifier = core[0]
    if identifier in records:
        previous, previous_extra, source_rows = records[identifier]
        if (previous, previous_extra) != (core, extra):
            raise TrackerError(f'{location}: conflicting duplicate contract {identifier}')
        source_rows.append(row_number)
        warnings.append(f'{location}: collapsed duplicate {identifier}')
        return
    records[identifier] = (core, extra, [row_number])


def parse_snapshot(path, *, content=None):
    """Read, validate, and normalize one dated Excel snapshot.

    The workbook must contain exactly one table with every essential header.
    Descriptive headers may be absent; extra columns are retained as JSON.
    Supply archived ``content`` to reprocess a snapshot without its source file.
    """
    path = Path(path)
    as_of = snapshot_date(path.name)
    if content is None:
        content = path.read_bytes()
    try:
        workbook = load_workbook(BytesIO(content), data_only=False)
    except (BadZipFile, ValueError, KeyError, IndexError, TypeError, OverflowError,
            OSError, ParseError) as exc:
        # A ZIP can be readable while its internal cell/style references are
        # corrupt. Treat decoder errors as source failures, not endless retries.
        raise TrackerError(f'{path.name}: cannot read Excel workbook: {exc}') from exc

    try:
        sheet, header_row, names, columns = _find_snapshot_table(workbook, path.name)
        # Absence means "not supplied" and can inherit history later; an empty
        # cell in a present column is an explicit NULL and must not inherit.
        missing_fields = tuple(field for field in DESCRIPTIVE if field not in columns)
        extras = sorted(set(columns) - set(CORE))
        warnings = []
        if missing_fields:
            warnings.append(f'{path.name}: descriptive columns not supplied: '
                            f'{", ".join(missing_fields)}; '
                            'previous known values retained in history')
        if extras:
            warnings.append(f'{path.name}: extra columns retained: {", ".join(extras)}')

        records = {}
        # Begin immediately below the discovered header; rows above it may
        # contain titles or notes and are not part of the table.
        for row in sheet.iter_rows(min_row=header_row + 1):
            values = [cell.value for cell in row]
            if _is_ignored_snapshot_row(values, columns):
                continue

            location = f'{path.name}:{sheet.title}:{row[0].row}'
            core = _parse_core_values(
                row, columns, missing_fields, workbook, location, warnings)
            # A populated unnamed column cannot be safely interpreted or kept
            # as a named extra field, so reject rather than silently drop it.
            if any(values[index] is not None for index, name in enumerate(names) if not name):
                raise TrackerError(f'{location}: populated column has no header')

            # Preserve non-core fields as typed JSON, separate from the values
            # used to compare contract terms and build history.
            extra = json_text({
                name: extra_value(row[columns[name]])
                for name in extras
            })
            _add_snapshot_record(
                records, core, extra, row[0].row, location, warnings)

        # An empty export must not be interpreted as evidence that all contracts
        # disappeared; absence from a snapshot never deletes a contract.
        if not records:
            raise TrackerError(f'{path.name}:{sheet.title}: no contract rows; '
                               'refusing empty export')

        # Store the source layout alongside parsed values for later audit/debug.
        layout = json_text({'sheet': sheet.title, 'header_row': header_row, 'columns': names})
        return Snapshot(as_of, path.name, sha256(content).hexdigest(), content, layout,
                        tuple(records[key] for key in sorted(records)),
                        tuple(warnings), missing_fields)
    finally:
        workbook.close()


def prepare_database(connection, lock_timeout):
    """Acquire the writer slot and migrate tracker tables atomically, if needed.

    The usage loader's tables are never altered. A failed migration rolls back
    with its enclosing transaction, leaving the previous schema usable.
    """
    deadline = time.monotonic() + lock_timeout
    while True:
        try:
            # WAL mode supports concurrent readers while a writer is active.
            connection.execute('PRAGMA journal_mode=WAL')
            break
        except sqlite3.OperationalError as exc:
            # journal_mode can be temporarily locked by another process. Retry
            # only that condition, and stop at the caller's timeout deadline.
            if 'locked' not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    # FULL synchronous mode favors durability; foreign keys enforce the archive
    # -> snapshot -> observation relationships declared in the schema.
    connection.execute('PRAGMA synchronous=FULL')
    connection.execute('PRAGMA foreign_keys=ON')
    # Reserve the writer slot before migrations/schema changes to keep them atomic.
    connection.execute('BEGIN IMMEDIATE')
    # Read the columns defined by SQLite for this table (schema)
    legacy_columns = {row[1] for row in connection.execute('PRAGMA table_info(commitment_snapshots)')}
    # The original commitments database schema (schema version 1, not a
    # workbook/scan version) stored workbook bytes on commitment_snapshots.
    # That old-only column identifies databases that need the migration below.
    if 'workbook' in legacy_columns:
        migrate_legacy_database(connection)
    else:
        create_schema(connection)


def create_schema(connection):
    """Create archive, validated observations, and derived contract-history tables.

    Files may be quarantined even when invalid; only validated files get a
    snapshot row. Observations preserve what each export said, while history
    records changes over time and the view exposes its current rows.
    """
    # Avoid executescript: it implicitly commits an existing transaction.
    connection.execute('''CREATE TABLE IF NOT EXISTS commitment_files (
        filename TEXT NOT NULL, checksum TEXT NOT NULL, as_of TEXT,
        workbook BLOB NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','quarantined','published','superseded')),
        errors_json TEXT NOT NULL, warnings_json TEXT NOT NULL, layout_json TEXT NOT NULL,
        PRIMARY KEY (filename, checksum))''')
    # A snapshot date may be published only once; the composite foreign key
    # points to the exact archived file bytes that were validated.
    connection.execute('''CREATE TABLE IF NOT EXISTS commitment_snapshots (
        as_of TEXT PRIMARY KEY, filename TEXT NOT NULL, checksum TEXT NOT NULL,
        layout_json TEXT NOT NULL,
        FOREIGN KEY (filename, checksum) REFERENCES commitment_files(filename, checksum))''')
    fields = ', '.join(f'{field} TEXT' + ('' if field in DESCRIPTIVE else ' NOT NULL')
                       for field in CORE)
    # Observations are immutable per snapshot/contract. Descriptive fields
    # permit NULL because some exports omit those columns or leave cells blank.
    # extras_json is always JSON text: '{}' means there were no extra columns.
    connection.execute(f'''CREATE TABLE IF NOT EXISTS commitment_observations (
        as_of TEXT NOT NULL REFERENCES commitment_snapshots(as_of), {fields},
        extras_json TEXT NOT NULL, source_rows_json TEXT NOT NULL,
        missing_fields_json TEXT NOT NULL,
        PRIMARY KEY (as_of, contract_id))''')
    # History ranges use a NULL end date for the current version. The CHECK
    # prevents zero-length or backwards ranges.
    connection.execute(f'''CREATE TABLE IF NOT EXISTS contract_history (
        {fields}, observed_from TEXT NOT NULL, observed_to TEXT,
        PRIMARY KEY (contract_id, observed_from),
        CHECK (observed_to IS NULL OR observed_to > observed_from))''')
    # Enforce no more than one open/current version for each contract.
    connection.execute('''CREATE UNIQUE INDEX IF NOT EXISTS contract_current_idx
        ON contract_history(contract_id) WHERE observed_to IS NULL''')
    connection.execute('''CREATE VIEW IF NOT EXISTS contracts_current AS
        SELECT * FROM contract_history WHERE observed_to IS NULL''')


def migrate_legacy_database(connection):
    """Upgrade the original database schema without reparsing published files.

    SQLite cannot remove NOT NULL in place. Rebuild only our small tracker tables
    in the same transaction, move each workbook from commitment_snapshots into
    commitment_files, and mark all old descriptive fields as supplied. No source
    bytes or business rows are dropped.
    """
    snapshots = connection.execute('SELECT * FROM commitment_snapshots').fetchall()
    observations = connection.execute('SELECT * FROM commitment_observations').fetchall()
    history = connection.execute('SELECT * FROM contract_history').fetchall()
    # Cache the old rows before dropping tables; all following DDL and inserts
    # remain inside prepare_database's transaction and roll back together.
    connection.execute('DROP VIEW contracts_current')
    connection.execute('DROP TABLE commitment_observations')
    connection.execute('DROP TABLE contract_history')
    connection.execute('DROP TABLE commitment_snapshots')
    create_schema(connection)
    for as_of, filename, checksum, workbook, layout in snapshots:
        connection.execute('INSERT INTO commitment_files VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           (filename, checksum, as_of, workbook, 'published', '[]', '[]', layout))
        connection.execute('INSERT INTO commitment_snapshots VALUES (?, ?, ?, ?)',
                           (as_of, filename, checksum, layout))
    # The original schema required descriptive columns, so each old observation
    # supplied those fields. Record an empty missing-fields list and preserve
    # existing business values as-is.
    connection.executemany('INSERT INTO commitment_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                           [(*row, '[]') for row in observations])
    connection.executemany('INSERT INTO contract_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', history)


def validate_nonoverlap(current, as_of):
    """Inclusive ends mean next.start must be strictly AFTER previous.end.

    Check latest-known terms for ALL contracts, including ones missing from this
    snapshot. Status is descriptive: 'Expired' does not erase a historical term.
    """
    # Sort by customer, then start date, so only adjacent terms need checking.
    # CORE order places customer_id at 1, start_date at 3, contract_id at 0.
    ordered = sorted(current.values(), key=lambda row: (row[1], row[3], row[0]))
    for previous, row in zip(ordered, ordered[1:]):
        # End dates are inclusive: a following contract cannot start on the
        # previous contract's end date.
        if previous[1] == row[1] and row[3] <= previous[4]:
            raise TrackerError(f'as of {as_of}: overlapping inclusive terms for customer '
                               f'{row[1]}: {previous[0]} and {row[0]}')


def rebuild_history(connection):
    """Reconstruct deterministically, so late older snapshots need no special case.

    This is intentionally a full rebuild for small Finance trackers. Natural
    keys and source dates keep results identical regardless of arrival order.
    Only run when a candidate is published, inside its savepoint. Missing optional
    columns inherit known descriptions; explicit blank cells do not. Observation
    rows always retain the source's NULL and its missing-column flag separately.
    """
    connection.execute('DELETE FROM contract_history')
    current = {}
    # Replaying observations in date order makes the result independent of the
    # order in which the source files originally arrived.
    for (as_of,) in connection.execute('SELECT as_of FROM commitment_snapshots ORDER BY as_of'):
        rows = connection.execute(
            f'SELECT {", ".join(CORE)}, missing_fields_json FROM commitment_observations '
            'WHERE as_of=? ORDER BY contract_id', (as_of,)).fetchall()
        for observation in rows:
            row = list(observation[:-1])
            identifier = row[0]
            missing_fields = json.loads(observation[-1])
            previous = current.get(identifier)
            for field in missing_fields:
                index = CORE.index(field)
                # Only absent descriptive columns inherit a prior known value.
                # For a new contract, there is nothing to inherit.
                row[index] = previous[index] if previous else None
            row = tuple(row)
            # Extra-only changes and repeated observations do not create a new
            # history version; history tracks changes to CORE values only.
            if current.get(identifier) == row:
                continue
            # Close the previous open interval before inserting the new version.
            connection.execute('UPDATE contract_history SET observed_to=? '
                               'WHERE contract_id=? AND observed_to IS NULL', (as_of, identifier))
            connection.execute('INSERT INTO contract_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)',
                               (*row, as_of))
            current[identifier] = row
        validate_nonoverlap(current, as_of)


def archive_inputs(connection, paths):
    """Save readable files before parsing. Rejected variants keep their own bytes.

    A file's identity is its filename + checksum, not just its claimed date.
    Only published snapshots have a unique date. Invalid dates and corrupt Excel
    files can therefore still be archived. Unreadable filesystem inputs cannot.
    """
    archived = 0
    input_errors = []
    received = []
    for path in paths:
        try:
            content = path.read_bytes()
        except OSError as exc:
            # An unreadable file cannot be archived, but report it without
            # preventing other readable files from being considered.
            input_errors.append(f'{path.name}: could not read file: {exc}')
            continue
        checksum = sha256(content).hexdigest()
        received.append((path.name, checksum))
        # Filename + checksum is the identity: an unchanged replay is already
        # preserved, while corrected bytes under the same name are new evidence.
        if connection.execute('SELECT 1 FROM commitment_files WHERE filename=? AND checksum=?',
                              (path.name, checksum)).fetchone():
            continue
        try:
            as_of = snapshot_date(path.name)
        except TrackerError:
            # Keep even badly named files; parse_snapshot will store the detailed
            # validation failure in the quarantine record.
            as_of = None  # Parsing records the specific validation error later.
        connection.execute('INSERT INTO commitment_files VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           (path.name, checksum, as_of, content, 'pending', '[]', '[]', '{}'))
        archived += 1
    return archived, received, input_errors


def update_file(connection, filename, checksum, state, errors, warnings, layout):
    """Persist a validation outcome only if changed, keeping exact replays stable."""
    values = (state, json_text(errors), json_text(warnings), layout)
    old = connection.execute('SELECT state, errors_json, warnings_json, layout_json '
                             'FROM commitment_files WHERE filename=? AND checksum=?',
                             (filename, checksum)).fetchone()
    # Avoid rewriting identical diagnostics on every replay of a quarantine.
    if old != values:
        connection.execute('UPDATE commitment_files SET state=?, errors_json=?, warnings_json=?, '
                           'layout_json=? WHERE filename=? AND checksum=?',
                           (*values, filename, checksum))


def publish_snapshot(connection, snapshot):
    """Publish one validated snapshot; caller's savepoint isolates its failure."""
    existing = connection.execute('SELECT checksum FROM commitment_snapshots WHERE as_of=?',
                                  (snapshot.as_of,)).fetchone()
    # A date is immutable after publication, even if a later file claims that
    # date with different bytes. Corrections require review, not silent replace.
    if existing:
        raise TrackerError(f'{snapshot.filename}: different contents for stored date '
                           f'{snapshot.as_of}; published snapshot is immutable')
    connection.execute('INSERT INTO commitment_snapshots VALUES (?, ?, ?, ?)',
                       (snapshot.as_of, snapshot.filename, snapshot.checksum, snapshot.layout))
    connection.executemany(
        'INSERT INTO commitment_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [(snapshot.as_of, *core, extra, json_text(source_rows), json_text(snapshot.missing_fields))
         for core, extra, source_rows in snapshot.rows])
    # Rebuild and validate all dates: an older arrival can change the terms in
    # force at a later date, so validating only the newly added date is unsafe.
    rebuild_history(connection)


def ingest(input_dir, database, lock_timeout=60, reprocess=False):
    """Archive evidence, then publish valid snapshots and quarantine invalid ones.

    Archiving commits first, so even a killed parser cannot lose received bytes.
    Pending files resume automatically. --reprocess additionally retries archived
    quarantines using the current parser. Each candidate has a savepoint: one bad
    snapshot does not prevent other valid snapshots from publishing. No individual
    rows from a rejected snapshot are published. Unexpected operational errors
    roll back publication but never the already-committed archive.
    """
    paths = sorted(Path(input_dir).glob('commitments_*.xlsx'))
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=lock_timeout, isolation_level=None)
    added = observations = 0
    try:
        prepare_database(connection, lock_timeout)
        archived, received, input_errors = archive_inputs(connection, paths)
        # Count only files already published before this run as unchanged.
        skipped = sum(connection.execute('SELECT state FROM commitment_files '
                                         'WHERE filename=? AND checksum=?', key).fetchone()[0]
                      == 'published' for key in received)
        # Commit archived bytes before parsing so a crash or bad workbook cannot
        # erase evidence that was successfully received.
        connection.commit()

        # Parsing outside the writer lock lets readers and other loaders proceed.
        # Include pending files from previous runs even if their source was removed.
        states = ('pending', 'quarantined') if reprocess else ('pending',)
        # Use bound placeholders for the selected states rather than embedding
        # state values into the SQL text.
        placeholders = ','.join('?' for _ in states)
        candidates = connection.execute(
            'SELECT filename, checksum, workbook FROM commitment_files '
            f'WHERE state IN ({placeholders}) ORDER BY as_of, filename, checksum', states).fetchall()
        # An empty routine run is likely a path/configuration mistake. Explicit
        # --reprocess is allowed to inspect quarantines without source files.
        if not paths and not candidates and not reprocess:
            raise TrackerError(f'{input_dir}: no commitments_YYYY-MM-DD.xlsx files or pending archives found; '
                               'use --reprocess to review archived quarantines')
        parsed = []
        for filename, checksum, content in candidates:
            try:
                parsed.append((filename, checksum, parse_snapshot(filename, content=content), None))
            except TrackerError as exc:
                # Source validation errors become quarantines; unexpected
                # operational/programming errors still abort the run.
                parsed.append((filename, checksum, None, str(exc)))

        # Parsing can take time, so acquire the writer lock only when ready to
        # publish. The archive transaction above has already committed.
        connection.execute('BEGIN IMMEDIATE')
        for filename, checksum, snapshot, error in parsed:
            # Another process may have published this same file during parsing.
            state = connection.execute('SELECT state FROM commitment_files WHERE filename=? AND checksum=?',
                                       (filename, checksum)).fetchone()[0]
            # Do not publish work another process has already completed (or
            # otherwise made ineligible for this run).
            if state not in states:
                continue
            warnings = snapshot.warnings if snapshot else ()
            layout = snapshot.layout if snapshot else '{}'
            if snapshot:
                # A conflict in this candidate should undo only this snapshot,
                # not valid snapshots already handled in the same batch.
                connection.execute('SAVEPOINT candidate')
                try:
                    publish_snapshot(connection, snapshot)
                except TrackerError as exc:
                    connection.execute('ROLLBACK TO candidate')
                    error = str(exc)
                finally:
                    connection.execute('RELEASE candidate')
            if error:
                update_file(connection, filename, checksum, 'quarantined', [error], warnings, layout)
                continue
            update_file(connection, filename, checksum, 'published', [], warnings, layout)
            # An accepted replacement resolves older rejected versions of this
            # unpublished date. Keep their evidence and diagnostic details forever.
            connection.execute("UPDATE commitment_files SET state='superseded' "
                               "WHERE as_of=? AND state='quarantined'", (snapshot.as_of,))
            added += 1
            observations += len(snapshot.rows)
        history_count = connection.execute('SELECT COUNT(*) FROM contract_history').fetchone()[0]
        current_count = connection.execute('SELECT COUNT(*) FROM contracts_current').fetchone()[0]
        quarantines = [dict(filename=row[0], checksum=row[1], as_of=row[2], errors=json.loads(row[3]))
                       for row in connection.execute('SELECT filename, checksum, as_of, errors_json '
                                                     "FROM commitment_files WHERE state='quarantined' "
                                                     'ORDER BY as_of, filename, checksum')]
        warnings = [warning for (value,) in connection.execute(
            "SELECT warnings_json FROM commitment_files WHERE state IN ('published','quarantined') "
            'ORDER BY as_of, filename, checksum') for warning in json.loads(value)]
        connection.commit()
        return {'files_archived': archived, 'snapshots_added': added, 'snapshots_unchanged': skipped,
                'observations_added': observations, 'history_versions': history_count,
                'contracts_current': current_count, 'quarantined_files': len(quarantines),
                'quarantines': quarantines, 'input_errors': input_errors, 'warnings': warnings}
    except BaseException:
        # Roll back schema/publication work on any interruption, but the earlier
        # archive commit remains durable because this is a separate transaction.
        connection.rollback()
        raise
    finally:
        connection.close()


def main():
    """Command-line entry point; return a process status suitable for automation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir', default='data/commitments', help='directory of dated Excel exports')
    parser.add_argument('--database', default=os.getenv('USAGE_DATABASE', 'storage/usage.sqlite3'))
    parser.add_argument('--reprocess', action='store_true', help='retry quarantined bytes from the archive')
    args = parser.parse_args()
    try:
        result = ingest(args.input_dir, args.database, reprocess=args.reprocess)
        print(json.dumps(result, sort_keys=True))
        # Quarantines and unreadable inputs need review even if other files worked.
        return 1 if result['quarantined_files'] or result['input_errors'] else 0
    except KeyboardInterrupt:
        # ingest has already rolled back uncommitted publication; archived files
        # remain available for the next run.
        print('Tracker ingestion interrupted; committed archives retained, uncommitted publication rolled back',
              file=sys.stderr)
        return 130
    except (TrackerError, OSError, sqlite3.Error) as exc:
        print(f'Tracker ingestion failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
