# Orbital usage and commitment ingestion

Two loaders write to one SQLite database (`storage/usage.sqlite3` by default):

- `ingest_usage.py` loads billing usage from the API: a full backfill on the first
  run, then incremental runs with a 30-day overlap.
- `ingest_commitments.py` loads Finance's Excel snapshots into SCD Type 2 contract
  history.

`build_models.py` then builds the two Part 2 tables from what they loaded.

`pipeline.py` runs all three in order. It's what the Kubernetes CronJob runs (see
[Kubernetes](#kubernetes) below). Design decisions are in [NOTES.md](NOTES.md). A visual
walkthrough of the loaders and models, with the real numbers, is in
[docs/ingestion-flow.html](docs/ingestion-flow.html). Open it in a browser.

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
.venv/bin/python build_models.py                  # rebuild both tables
.venv/bin/python build_models.py --export out/    # also write them as CSV
```

It rebuilds these in one transaction, so running it again gives identical tables:

- `contract_utilization_daily`: one row per contract per day, from the start date
  to the earlier of the end date and the last usage date loaded, using the latest
  known terms.
- `unattributed_usage_daily`: non-void spend on days when the customer had no
  active contract.

## Kubernetes

You need Docker (running), [kind](https://kind.sigs.k8s.io/) and `kubectl` on PATH.
`run.py` uses only the Python standard library, so it needs no virtualenv.

```sh
python run.py up              # cluster "orbital", images, billing API, storage, CronJob
python run.py export OUT_DIR  # the two Part 2 tables as CSV
python run.py down            # delete the cluster and everything in it
```

`up` can be run again at any time. It rebuilds and reloads the images and updates
every object. It also re-applies the provided API manifest, which resets the API's
`AS_OF` to its default (2026-09-20). It doesn't start a run. To start one:

```sh
kubectl -n orbital create job run-1 --from=cronjob/orbital-pipeline
kubectl -n orbital wait --for=condition=complete job/run-1 --timeout=15m
kubectl -n orbital logs job/run-1          # one JSON summary per step
kubectl -n orbital get jobs,pods           # retries show up as extra pods
```

What runs where:

| Object | Source | Purpose |
| --- | --- | --- |
| Deployment and Service `billing-api` | `mock_api/k8s/billing-api.yaml`, unchanged | The mock API |
| CronJob `orbital-pipeline` | `k8s/pipeline.yaml` | `python pipeline.py`, daily at 03:00 UTC |
| PVC `orbital-storage` | `k8s/pipeline.yaml` | The SQLite file, at `/var/lib/orbital/usage.sqlite3` |
| ConfigMap `orbital-pipeline-config` | `k8s/pipeline.yaml` | API URL, database path, lock timeout |
| Secret `billing-api-credentials` | created by `run.py up` | `BILLING_CLIENT_ID`, `BILLING_CLIENT_SECRET` |
| ConfigMap `orbital-commitments` | created by `run.py up` from `data/commitments/` | The Finance workbooks, mounted at `/data/commitments` |
| Pod `orbital-export` | `k8s/export-pod.yaml` | Started and deleted by `run.py export` |

The credentials come from `BILLING_CLIENT_ID` and `BILLING_CLIENT_SECRET` in your
environment when you run `up`, and fall back to the assignment's demo values. To add a
new Finance snapshot, put it in `data/commitments/` and run `python run.py up`. No
image rebuild is needed.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tests/smoke_api.py   # needs the mock API and credentials
```
