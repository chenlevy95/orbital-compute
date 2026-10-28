# Orbital usage and commitment ingestion

This repository implements usage ingestion and auditable contract snapshot history.
The assignment's
`python run.py up`, `python run.py export OUT_DIR`, and `python run.py down`
commands are **not implemented**; use the local instructions below for this stage.
Design decisions, tradeoffs, and deliberate omissions are in [NOTES.md](NOTES.md).

For contract ingestion, follow [Commitments tracker](#commitments-tracker) below.
It needs neither the billing API nor credentials. The offline
[contract-history guide](docs/commitments-flow.html) explains the flow interactively.

## Setup

Copy or clone this repository, including `mock_api/`. Install Python 3.10 or newer
with pip, and open a terminal in the repository directory. No database server,
Docker, or Kubernetes is required. Installation needs internet access.

macOS / Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export BILLING_CLIENT_ID=candidate
export BILLING_CLIENT_SECRET=orbital-s3cret
```

Windows PowerShell:

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:BILLING_CLIENT_ID = 'candidate'
$env:BILLING_CLIENT_SECRET = 'orbital-s3cret'
```

These credentials belong to the supplied mock. Set real credentials through your
environment or a scheduler's secret store. The loader does not read `.env` files,
accept credentials on the command line, or log credentials or response bodies.
Use HTTPS for a remote API; plain HTTP is appropriate for the local mock.

## Run

Start the mock in a separate terminal and leave it running:

```sh
python3 mock_api/server.py --as-of 2026-09-20
```

On Windows use `py -3` instead of `python3`. In the terminal where you set credentials:

```sh
.venv/bin/python ingest_usage.py
.venv/bin/python ingest_usage.py
.venv/bin/python ingest_usage.py --reconcile
```

On Windows replace `.venv/bin/python` with `.venv\Scripts\python.exe` in all
commands below and above. The first command backfills; the second is incremental;
the third forces historical reconciliation. You may interrupt a run and rerun
the same command. Uncommitted changes are rolled back automatically.

| Setting | Default | Override |
| --- | --- | --- |
| API URL | `http://127.0.0.1:8765` | `BILLING_API_URL` or `--api-url` |
| SQLite file | `storage/usage.sqlite3` | `USAGE_DATABASE` or `--database` |
| Client ID | required | `BILLING_CLIENT_ID` |
| Client secret | required | `BILLING_CLIENT_SECRET` |

CLI options take precedence over environment settings. Relative database paths
are relative to the process working directory. Use an absolute path in scheduled
jobs. Parent directories are created automatically. Keep the database on persistent
local storage, not a network filesystem or an ephemeral container filesystem.

A successful run exits `0` and prints one JSON summary. Counters report inserted,
updated, unchanged/older records, encountered void records (`void_seen`), HTTP
requests (including authentication), and retries. Void counts are observations,
not the total stored void count. Failures exit `1`; keyboard interruption exits
`130`. Failed runs do not publish a success summary.

## Verification and operation

Run the tests:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

With the mock running and credentials configured:

```sh
.venv/bin/python tests/smoke_api.py
```

The smoke check uses temporary databases; keep the API snapshot unchanged during
it. It may take a few minutes. Rerunning the loader against the same API state
should report zero inserts and updates.

The output database contains `usage` (including void records) and `non_void_usage`
(excluding void records). Amounts and quantities are decimal text: use Python
`Decimal` for exact arithmetic rather than SQLite's floating-point `SUM`.

For daily runs, configure cron or Windows Task Scheduler with an absolute Python
and database path and the credential environment variables. Capture stdout/stderr
and retry nonzero exits after a delay. There is no automatic periodic full scan;
normal runs use the 30-day overlap, assuming first arrivals are no more than
30 days late (see NOTES.md for measured evidence and limits). Use `--reconcile`
to refresh all history manually when needed.

- Connection failures: check that the mock is running and the API URL is correct.
- `401`/`403`: check credentials in the environment of the running job.
- Retry exhaustion or database lock: wait for the API or active run, then rerun.
- Invalid records or conflicting versions: investigate the source data before retrying.
- Database errors: check directory permissions and disk space. Do not delete SQLite
  `-wal` or `-shm` files while the database is in use; use SQLite's backup API for backups.

To exercise new usage, stop the mock and restart it with `--as-of 2026-09-28`, then
rerun the loader against the same database.

## Commitments tracker

The tracker loader reads all `commitments_YYYY-MM-DD.xlsx` files in a directory.
It archives each original workbook, keeps normalized observations, and derives
SCD Type 2 history: a new version when core fields change, without losing the old
values. Contracts removed from later exports remain available.

### Install and run on another computer

Copy/clone the repository **including `data/commitments/`**. Python 3.10+ with pip
is sufficient; no Excel installation, API server, credentials, Docker, or database
server is needed. `openpyxl` reads Excel; SQLite is included in Python.
Run from the repository directory (or pass absolute input and database paths).

macOS / Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python ingest_commitments.py
.venv/bin/python ingest_commitments.py
```

Windows PowerShell:

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe ingest_commitments.py
.venv\Scripts\python.exe ingest_commitments.py
```

If a project virtual environment already exists, reuse it and reinstall the
requirements. Installation requires internet access; tracker runs work offline.
Paths containing spaces must be quoted. A virtual environment is not portable:
recreate it on the destination computer rather than copying `.venv`.

Defaults: input `data/commitments`, database `storage/usage.sqlite3`. The database
directory is created automatically. `--database` overrides `USAGE_DATABASE`, which
overrides the default. Input defaults do not depend on environment variables.

```sh
.venv/bin/python ingest_commitments.py --input-dir data/commitments --database storage/demo.sqlite3
```

The supplied exports produce **3 snapshots, 29 observations, 16 core versions,
and 11 latest-known contracts**. The second identical run reports
`snapshots_added: 0`, `observations_added: 0`, and `snapshots_unchanged: 3`.
All persisted tracker rows, source bytes, and history boundaries stay identical.
Layout and duplicate warnings may repeat in stdout; they are not stored changes.
Exit codes: `0` success, `1` failure, `130` keyboard interruption.

### What is stored, and how to read it

| Table/view | Grain and purpose |
| --- | --- |
| `commitment_snapshots` | One immutable export per `as_of` date: filename, SHA-256 checksum, original workbook BLOB, detected sheet/header metadata |
| `commitment_observations` | One row per `(as_of, contract_id)`: normalized core values, extra-column JSON, original row-number references |
| `contract_history` | One row per `(contract_id, observed_from)`: core values with an exclusive `observed_to`; `NULL` means latest known |
| `contracts_current` | View of the latest known version of every contract, including disappeared and expired contracts |

Core columns are `contract_id`, `customer_id`, `customer_name`, `start_date`,
`end_date`, `commitment_usd`, and `status`. Names and statuses are versioned too.
Owner, region, notes, and future extra columns are retained as typed JSON in each
observation; changes to these alone do not create core versions. Original workbook
bytes preserve everything, including duplicates and formatting. Snapshot dates
come from filenames, not free-text notes or a worksheet's title.

`observed_from` is when the export first shows the version, **not** when an amendment
legally took effect. Intervals are `[observed_from, observed_to)`. Contract start
and end dates are separate, inclusive business dates. For example, K-1009 changes
from $500,000 through November 30 to $650,000 through January 31 in the August 31
snapshot. Its July history ends at August 31; the August version remains open.
The August 20 amendment note is preserved but never interpreted as an effective date.

K-1004 disappears in September but remains in `contracts_current`. Here “current”
means latest **known**, not currently active. Status does not filter out contracts;
future usage attribution should use inclusive business dates. Snapshots cannot
recover amendments made and reversed between exports.

Inspect without installing the SQLite CLI (on Windows substitute the virtual-env
Python path shown above):

```sh
.venv/bin/python -c "import sqlite3; c=sqlite3.connect('storage/usage.sqlite3'); print(c.execute('SELECT contract_id, commitment_usd, observed_from, observed_to FROM contract_history ORDER BY contract_id, observed_from').fetchall()); c.close()"
.venv/bin/python -c "import sqlite3; c=sqlite3.connect('storage/usage.sqlite3'); print(c.execute('SELECT contract_id, start_date, end_date, status FROM contracts_current ORDER BY contract_id').fetchall()); c.close()"
```

Amounts are canonical decimal **TEXT**. Use Python `Decimal`, not SQLite `SUM`,
for exact arithmetic. Text amounts retain decimal precision; numeric Excel cells
have Excel's existing numeric precision, which the loader cannot recover beyond.

### New exports, validation, and recovery

Put the next dated file in the input directory and run the same command. Files
need not be month-end exports. Previously unseen older dates are accepted; history
is reconstructed chronologically from all stored observations, so arrival order
does not affect the result. Removing an input file does not remove its archive.

The reader searches all sheets for one complete required header row. It handles
sheet renaming, shifted headers, reordered columns, and the two supplied amount
header aliases. It trims identifiers, accepts Excel dates, `YYYY-MM-DD`, and
English `D Mon YYYY`, and parses USD strings such as `$900,000.00`. These date
rules do not depend on the operating system's locale. Identical duplicates are
collapsed with source row references; conflicting duplicates fail.

Recognized blank/total/footnote rows are skipped. New extra headers are retained
and listed in warnings. Missing/ambiguous core headers, empty exports, malformed
rows, formula/error cells in core fields, nonpositive amounts, reversed terms,
and overlapping customer contracts block the entire batch. Non-overlap is checked
at every stored snapshot state, including carried-forward disappeared contracts.
Unknown nonempty status text is retained as descriptive data.

For layout errors, review the reported workbook/sheet/row/field, add a reviewed
alias or parser rule in `ingest_commitments.py`, add a fixture/test, then retry.
Never silently guess a replacement amount column. Formula totals are allowed
because they are skipped; contract-field formulas are not evaluated.

The same stored date with different workbook bytes is an error, even if only
formatting changed. Keep originals immutable. Restore the original input file to
resume ingestion; publish a later-dated export for genuinely later observations.
Historical corrections require a separate reviewed correction workflow, which is
not implemented. Do not relabel a historical correction as a later observation.

Each run parses first, obtains a SQLite write lock, rechecks snapshot identities,
and commits the whole batch once. A crash before commit rolls back; a crash after
commit is safe to replay. Concurrent writers serialize; the lock timeout is 60
seconds per locking operation, after which the contender fails and should retry.
WAL readers can see the old complete state while the new one is being written.
Keep the database on persistent local disk, not a network filesystem. Usage and
tracker ingestion share the database but commit independently; this is not yet an
atomic, combined pipeline for downstream utilization tables.

Back up the whole database using SQLite's backup API, which safely includes both
loaders' committed tables. Do not copy only the database file during active WAL
writes or delete its `-wal`/`-shm` files:

```sh
.venv/bin/python -c "import sqlite3; s=sqlite3.connect('storage/usage.sqlite3'); d=sqlite3.connect('storage/backup.sqlite3'); s.backup(d); d.close(); s.close()"
```

For scheduled runs, use absolute paths for Python, input directory, and database;
capture stdout/stderr, alert on failures or missing runs, and retry after a delay.
No credentials are required for this loader. Missing files do not delete records,
so monitor expected export delivery separately from successful ingestion.

### Verify and understand the tradeoffs

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests use temporary files and need no running API. They compare complete stored
state on replay, incremental versus fresh loads, out-of-order arrivals, vanished
contracts, normalization, drift failures, amendments, and inclusive renewal
boundaries. Real process tests cover a killed writer and overlapping fresh loads;
failure tests verify batch rollback and preservation of existing usage records.

| Choice | Benefit | Cost / alternative |
| --- | --- | --- |
| SQLite | Portable, durable, no service to operate | One local writer; PostgreSQL for distributed workers |
| Archive + observations + SCD | Source evidence and convenient history/current queries | More storage; observations alone defer version derivation to readers |
| Explicit header aliases | Predictable, reviewable financial interpretation | Unknown required names block; fuzzy matching risks silent errors |
| Rebuild history only for new snapshots | Simple, deterministic handling of older arrivals | Work grows with retained observations; incremental maintenance is more complex |
| Whole-batch transaction | Simple crash recovery, no partially published history | An invalid file blocks all new files; interruption repeats work |

For these small workbooks, parsing in memory and rebuilding history is sufficient.
Unchanged replays still read/validate inputs, but skip observation and history writes.
At larger scale, stream parsing, durable staging, incremental history maintenance,
and a server database are possible next steps.

Open the visual guide directly in a browser. On macOS:

```sh
open docs/commitments-flow.html
```

On Windows, double-click the file or use `Start-Process docs/commitments-flow.html`.
On Linux, open the file in your browser or use `xdg-open docs/commitments-flow.html`.
