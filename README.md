# Orbital usage and commitment ingestion

Two loaders write to one SQLite database (`storage/usage.sqlite3` by default):

- `ingest_usage.py` loads billing usage from the API: a full backfill on the first
  run, then incremental runs with a 30-day overlap.
- `ingest_commitments.py` loads Finance's Excel snapshots into SCD Type 2 contract
  history.

`build_models.py` then builds the two Part 2 tables from what they loaded.

`run.py` (`up` / `export` / `down`) and the Kubernetes deployment are **not
implemented yet**. Design decisions are in [NOTES.md](NOTES.md).

## Setup

You need Python 3.10 or newer.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

On Windows use `py -3 -m venv .venv` and `.venv\Scripts\python.exe` instead.

## Usage loader

Start the mock API in a separate terminal and leave it running:

```sh
python3 mock_api/server.py --as-of 2026-09-20
```

Then run the loader:

```sh
export BILLING_CLIENT_ID=candidate BILLING_CLIENT_SECRET=orbital-s3cret
.venv/bin/python ingest_usage.py              # first run: full backfill
.venv/bin/python ingest_usage.py              # incremental run (30-day overlap)
.venv/bin/python ingest_usage.py --reconcile  # rescan all history
```

| Setting | Default | Override |
| --- | --- | --- |
| API URL | `http://127.0.0.1:8765` | `BILLING_API_URL` or `--api-url` |
| Database | `storage/usage.sqlite3` | `USAGE_DATABASE` or `--database` |
| Credentials | required | `BILLING_CLIENT_ID`, `BILLING_CLIENT_SECRET` |

Each run prints a JSON summary and exits 0, or exits 1 with an error message.
It creates these:

- `usage`: every record, including voided ones.
- `non_void_usage`: a view without the voided records.

Amounts are stored as exact decimal text.

## Commitments loader

```sh
.venv/bin/python ingest_commitments.py   # reads data/commitments/, or pass --input-dir
```

With the supplied files it stores 3 snapshots, 16 history versions and 11 contracts.
Running it again changes nothing. It creates these:

- `commitment_snapshots`: one row per loaded file (date, filename, checksum).
- `commitment_observations`: what each snapshot said about each contract.
- `contract_history`: SCD Type 2 versions, valid over `[observed_from, observed_to)`.
- `contracts_current`: the latest known version of every contract, including ones
  Finance has since removed from the tracker.

An invalid file is skipped, and the run reports which file and why, and exits 1. The
other files still load. Nothing from the skipped file is stored, so to backfill it,
fix it (or add a new header to `ALIASES` in `ingest_commitments.py`) and run the
loader again. History is rebuilt in date order, so the file lands in the right place.

The summary also lists `warnings`, which don't change the exit code. A warning
appears when a contract's status says it's over (Expired, Cancelled) but its end
date is still after the snapshot date, or when the status is a label the loader
doesn't know. Those contracts are still tracked by their dates. The warning repeats
on every run until Finance corrects the end date or the status. Once a new label's
meaning is confirmed, add it to `KNOWN_STATUSES`.

## Models

Run this after both loaders:

```sh
.venv/bin/python build_models.py   # rebuild both tables
```

It rebuilds these in one transaction, so running it again gives identical tables:

- `contract_utilization_daily`: one row per contract per day, from the start date
  to the earlier of the end date and the last usage date loaded, using the latest
  known terms.
- `unattributed_usage_daily`: non-void spend on days when the customer had no
  active contract.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tests/smoke_api.py   # needs the mock API and credentials
```
