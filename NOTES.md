# Design decisions and tradeoffs

## Ingestion and completeness

The first load scans from `2026-01-01` through the current UTC date. Requests use
inclusive windows of at most 31 days and pages of 200 records. Subsequent runs
start 30 days before the maximum stored usage date. Anchoring the overlap to
received data, rather than the machine clock, handles the mock API's earlier
simulated date. Empty responses never advance the received-data watermark.

The first run at least seven days after the last successful full scan reconciles
all history; `--reconcile` forces this immediately. The vendor bounds restatements
but not first-arrival lateness, so a bounded overlap alone cannot guarantee
completeness. Weekly reconciliation reduces routine requests at the cost of
waiting until reconciliation for old late arrivals. Full scans every run discover
these sooner but become increasingly expensive. Failed or missed runs extend the
reconciliation delay. A lookback-only solution would require a vendor lateness guarantee.

## Storage, consistency, and reliability

SQLite provides portable storage without a database service. Each run acquires a
write lock before fetching and commits usage and reconciliation state in one
transaction. A crash rolls back the run; overlapping writers serialize or fail
after a bounded lock wait. WAL allows readers to see the previous committed data.
This is simple and atomic, but holds a writer lock during HTTP waits and repeats
all fetching after interruption. Durable staging and checkpoints would improve
recovery for larger loads. PostgreSQL would support workers across multiple hosts.

Records are keyed by `record_id`; only a newer UTC `updated_at` replaces
a stored version. Identical duplicates and older versions do nothing. Equal
timestamps with different values fail the run. Void records remain stored and are
excluded by `non_void_usage`; absence from a response never implies deletion.
Amounts and quantities use canonical decimal text to preserve precision. This
requires explicit decimal arithmetic downstream. Idempotency applies to usage
and derived results; operational logs and full-scan timestamps can change.

The raw `updated_at` values in all 5,373 records returned at API as-of
`2026-09-20` already used `YYYY-MM-DDTHH:MM:SSZ`. We therefore validate that exact
format and calendar validity, then store the original string without timezone
conversion. Fixed-width UTC strings support chronological comparison directly.
This is an observed source-format assumption, not a documented guarantee for all
future responses: offsets, fractional seconds, or other format changes fail the
run and require review. Existing timestamps written by the earlier loader as
`.000000+00:00` are converted back to `Z` inside the next successful ingestion
transaction, avoiding mixed-format comparisons when reusing an existing database.

A reused HTTP session, proactive token refresh, one refresh after `401`, timeouts,
and bounded retries handle transient failures. Backoff includes jitter and honors
`Retry-After`; an excessive delay fails the run instead of retrying prematurely.
Requests are sequential to limit rate pressure. The API offers no documented
snapshot-consistent pagination, so a changing source cannot be read as a guaranteed
single point-in-time snapshot; overlap and reconciliation repair eventual differences.

## Validation and deliberate omissions

Validation uses explicit Python checks rather than Pydantic because there is one
small record schema and keeping dependencies minimal makes the validation rules
easy to inspect. Pydantic would offer declarative models and structured field
errors, especially useful if more schemas are added. It would still require
explicit rules for the exact timestamp spelling, decimal precision, and whether
a usage date belongs to the requested window. The tradeoff is more handwritten
validation and less detailed errors in the current implementation.

Malformed pages, pagination cycles, invalid required fields, out-of-window dates,
nonfinite numbers, unknown statuses, and conflicting versions block publication.
Negative amounts are valid. Extra fields are ignored. Tests cover HTTP failures,
late arrivals, corrections, voids, exact decimals, reruns, reconciliation, killed
writers, and concurrent processes. A black-box smoke test compares incremental,
reconciled, and fresh loads without inspecting or modifying the API implementation.

The agreed scope is usage ingestion. Contract snapshot history, financial models,
CSV model exports, `run.py`, pipeline containers, and Kubernetes deployment are
not implemented. The provided `mock_api/` and `data/` remain unchanged. Historical
usage versions, durable checkpoints, central monitoring, and distributed execution
are also deliberately omitted to keep this stage small and understandable.

For production, use persistent managed storage, a secret store, and Airflow or an
equivalent scheduler with daily runs, delayed retries, and explicit backfill and
reconciliation jobs. Alert on failed or missing runs, stale data, and reconciliation
age; validate changes in CI before deployment. On spot nodes, durable staging and
atomic publication would avoid refetching entire interrupted runs. Finance workbook
layout detection remains part of the future contract-ingestion work, not this loader.

AI assistance was used to implement the loader, tests, and documentation. Verification
included deterministic tests and live requests to the unchanged mock API.
