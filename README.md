# Orbital billing usage ingestion

This repository currently implements usage ingestion only. The assignment's
`python run.py up`, `python run.py export OUT_DIR`, and `python run.py down`
commands are **not implemented**; use the local instructions below for this stage.
Design decisions, tradeoffs, and deliberate omissions are in [NOTES.md](NOTES.md).

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
and retry nonzero exits after a delay. Weekly reconciliation is automatic; use
`--reconcile` to refresh all history immediately.

- Connection failures: check that the mock is running and the API URL is correct.
- `401`/`403`: check credentials in the environment of the running job.
- Retry exhaustion or database lock: wait for the API or active run, then rerun.
- Invalid records or conflicting versions: investigate the source data before retrying.
- Database errors: check directory permissions and disk space. Do not delete SQLite
  `-wal` or `-shm` files while the database is in use; use SQLite's backup API for backups.

To exercise new usage, stop the mock and restart it with `--as-of 2026-09-28`, then
rerun the loader against the same database.
