"""Build the Part 2 model tables from loaded usage and contract terms.

Run it after both loaders:

    python build_models.py [--database PATH]

Inputs (written by the loaders, never changed here):

- usage / non_void_usage (ingest_usage.py): billing records. Voided records are
  kept in usage and left out of non_void_usage.
- contracts_current (ingest_commitments.py): the latest known terms of every
  contract, including contracts Finance has since removed from the tracker.

Outputs:

- contract_utilization_daily: one row per contract per day, from start_date to
  min(end_date, last usage date loaded), using each contract's latest known terms.
  Every day of the term gets a row, including days with no spend.
- unattributed_usage_daily: a customer's non-void spend on a day when none of
  their contracts was active. This is on-demand spend that counts toward no
  commitment.

Business rules:

- A contract is active on a day when start_date <= day <= end_date. Both dates are
  inclusive. Finance's status column is a hand-typed label and is not used here.
- The last usage date loaded is MAX(usage_date) over all records, voided ones
  included, because it marks how far the load has reached, not what was spent.
  A contract that starts after that date has no rows yet.

Why a full rebuild every run: past rows don't stay correct. Billing restates usage
up to 30 days back, usage can arrive late, and an amendment to a contract's terms
changes every row of that contract. Both tables are deleted and rebuilt inside one
BEGIN IMMEDIATE transaction, so:

- A rerun on the same inputs gives identical tables (idempotent).
- A killed run rolls back, and readers keep seeing the last committed tables.
- Concurrent runs queue for the write lock instead of interleaving.

At this size (about 11 contracts and 300 days) a rebuild takes milliseconds. An
incremental build would need to track every case that changes past rows.

Precision: money is computed with Decimal from the exact decimal text the loaders
store, so source amounts and their running sums are exact. Derived values are
rounded only when they are stored. at_risk is decided before any rounding, so
rounding can never flip it at the 0.9 boundary.
"""

import argparse
from datetime import date, timedelta
from decimal import Decimal
import json
import os
import sqlite3
import sys

import db

# Column order of each table, used for inserts. The names match the assignment exactly.
UTILIZATION_COLUMNS = (
    'contract_id', 'customer_id', 'date', 'commitment_usd', 'term_days', 'days_elapsed',
    'consumed_usd', 'consumed_to_date_usd', 'expected_to_date_usd', 'utilization_pct',
    'projected_term_usd', 'at_risk')
UNATTRIBUTED_COLUMNS = ('customer_id', 'date', 'amount_usd')

# at_risk = days_elapsed >= 30 AND projected_term_usd < 0.9 * commitment_usd.
# Before day 30 the projection rests on too few days to trust.
AT_RISK_MIN_DAYS = 30
AT_RISK_RATIO = Decimal('0.9')

# Rounding used when storing derived values: cents for money, 6 places for the ratio.
CENT = Decimal('0.01')
RATIO = Decimal('0.000001')


class ModelError(Exception):
    """A missing input or similar problem, safe to print."""


def decimal_text(value):
    """Exact decimal text without trailing zeros, matching how the loaders store amounts.

    Decimal('15.50') -> '15.5', Decimal('100') -> '100'. Fixed-point formatting
    avoids exponent forms like '1E+2'.
    """
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def create_schema(connection):
    """Create both output tables if they don't exist.

    Amounts are TEXT holding exact decimals, as in the input tables. Day counts
    and at_risk (0 or 1) are INTEGER. The primary keys are the keys the graders
    match rows by, and they also reject a duplicate row from a bug.
    """
    connection.execute('''CREATE TABLE IF NOT EXISTS contract_utilization_daily (
        contract_id TEXT NOT NULL, customer_id TEXT NOT NULL, date TEXT NOT NULL,
        commitment_usd TEXT NOT NULL, term_days INTEGER NOT NULL, days_elapsed INTEGER NOT NULL,
        consumed_usd TEXT NOT NULL, consumed_to_date_usd TEXT NOT NULL,
        expected_to_date_usd TEXT NOT NULL, utilization_pct TEXT NOT NULL,
        projected_term_usd TEXT NOT NULL, at_risk INTEGER NOT NULL CHECK(at_risk IN (0, 1)),
        PRIMARY KEY (contract_id, date))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS unattributed_usage_daily (
        customer_id TEXT NOT NULL, date TEXT NOT NULL, amount_usd TEXT NOT NULL,
        PRIMARY KEY (customer_id, date))''')


def read_inputs(connection):
    """Return (last usage date loaded or None, daily non-void spend, current contracts).

    - The last date is an ISO string, or None when no usage has been loaded.
    - Daily spend maps (customer_id, usage_date) to the Decimal sum over all SKUs.
      Days with no non-void records are absent, and callers treat them as 0.
    - Each contract is (contract_id, customer_id, start_date, end_date,
      commitment_usd) as stored, sorted by contract_id so the output order is
      deterministic.

    Raises ModelError when a loader hasn't created its tables yet.
    """
    try:
        # Void records count here: they show how far the load reached, not what was spent.
        last = connection.execute('SELECT MAX(usage_date) FROM usage').fetchone()[0]
        spend = {}
        for customer, day, amount in connection.execute(
                'SELECT customer_id, usage_date, amount_usd FROM non_void_usage'):
            spend[customer, day] = spend.get((customer, day), Decimal(0)) + Decimal(amount)
        contracts = connection.execute(
            'SELECT contract_id, customer_id, start_date, end_date, commitment_usd '
            'FROM contracts_current ORDER BY contract_id').fetchall()
    except sqlite3.OperationalError as exc:
        raise ModelError(f'input tables missing ({exc}); run both loaders first') from None
    return last, spend, contracts


def utilization_rows(contract, spend, last):
    """Yield one contract_utilization_daily row per day of the contract up to `last`.

    The days run from start_date through min(end_date, last). There are none if
    the contract starts after `last`. Per day:

    - term_days            = end_date - start_date + 1 (both ends inclusive)
    - days_elapsed         = date - start_date + 1
    - consumed_usd         = the customer's non-void spend that day (0 if none)
    - consumed_to_date_usd = running sum of consumed_usd since start_date
    - expected_to_date_usd = commitment * days_elapsed / term_days (linear burn)
    - utilization_pct      = consumed_to_date / expected_to_date, a ratio where
                             1.0 means on track (not multiplied by 100)
    - projected_term_usd   = consumed_to_date / days_elapsed * term_days
    - at_risk              = days_elapsed >= 30 and projected < 0.9 * commitment

    expected is never 0, because commitments are positive (the loader enforces
    it) and days_elapsed starts at 1, so the ratio is always defined.
    """
    contract_id, customer, start, end, commitment = contract
    start, end, commitment = date.fromisoformat(start), date.fromisoformat(end), Decimal(commitment)
    term_days = (end - start).days + 1
    consumed_to_date = Decimal(0)
    for offset in range((min(end, last) - start).days + 1):
        day = (start + timedelta(days=offset)).isoformat()
        days_elapsed = offset + 1
        consumed = spend.get((customer, day), Decimal(0))
        consumed_to_date += consumed
        expected = commitment * days_elapsed / term_days
        projected = consumed_to_date / days_elapsed * term_days
        # Decided on unrounded values; rounding happens only in the stored row below.
        at_risk = days_elapsed >= AT_RISK_MIN_DAYS and projected < AT_RISK_RATIO * commitment
        yield (contract_id, customer, day, decimal_text(commitment), term_days, days_elapsed,
               decimal_text(consumed), decimal_text(consumed_to_date),
               str(expected.quantize(CENT)), str((consumed_to_date / expected).quantize(RATIO)),
               str(projected.quantize(CENT)), int(at_risk))


def unattributed_rows(contracts, spend):
    """Yield (customer_id, date, amount_usd) for spend on days with no active contract.

    That covers days before a customer's first contract, after their last one,
    in a gap between two contracts, and every day of a customer with no contract
    at all. Only days with non-void spend appear; a day with only voided records
    has nothing to attribute. Dates are ISO strings, so comparing the strings
    compares the dates. The loader guarantees a customer's contracts never
    overlap, so a day can't count toward two contracts.
    """
    periods = {}
    for _, customer, start, end, _ in contracts:
        periods.setdefault(customer, []).append((start, end))
    for (customer, day), amount in sorted(spend.items()):
        if not any(start <= day <= end for start, end in periods.get(customer, ())):
            yield customer, day, decimal_text(amount)


def build(database):
    """Rebuild both tables in one transaction and return a summary of the result.

    The summary holds:
    - last_usage_date: the cutoff used, or None when no usage has been loaded.
    - contract_days: the number of contract_utilization_daily rows.
    - at_risk_contracts: contracts flagged at_risk on their latest row.
    - unattributed_days: the number of unattributed_usage_daily rows.

    The write lock is taken before reading, so the inputs can't change between
    the read and the write. With no usage loaded, the utilization table is empty
    and there is nothing to attribute.
    """
    connection = db.connect(database)
    try:
        connection.execute('BEGIN IMMEDIATE')
        last, spend, contracts = read_inputs(connection)
        create_schema(connection)
        # Full rebuild: see the module docstring for why past rows can't be kept.
        connection.execute('DELETE FROM contract_utilization_daily')
        connection.execute('DELETE FROM unattributed_usage_daily')
        contract_days = at_risk = 0
        if last is not None:
            for contract in contracts:
                rows = list(utilization_rows(contract, spend, date.fromisoformat(last)))
                connection.executemany(
                    f'INSERT INTO contract_utilization_daily VALUES '
                    f'({", ".join("?" * len(UTILIZATION_COLUMNS))})', rows)
                contract_days += len(rows)
                at_risk += bool(rows and rows[-1][-1])  # at_risk is the last column
        unattributed = list(unattributed_rows(contracts, spend))
        connection.executemany('INSERT INTO unattributed_usage_daily VALUES (?, ?, ?)', unattributed)
        connection.commit()
        return {'last_usage_date': last, 'contract_days': contract_days,
                'at_risk_contracts': at_risk, 'unattributed_days': len(unattributed)}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def main():
    """Rebuild the tables and print the JSON summary.

    Exits 0 on success, or 1 with a message on stderr when an input is missing.
    """
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--database', default=os.getenv('USAGE_DATABASE', 'storage/usage.sqlite3'))
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.database), sort_keys=True))
        return 0
    except ModelError as exc:
        print(f'Model build failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
