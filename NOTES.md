# Design decisions and tradeoffs

## Ingestion and completeness

The first load scans from `2026-01-01` through the current UTC date. Requests use
inclusive windows of at most 31 days and pages of 200 records. Subsequent runs
start 30 days before the maximum stored usage date. Anchoring the overlap to
received data, rather than the machine clock, handles the mock API's earlier
simulated date. Empty responses never advance the received-data watermark.

We assume first arrivals are no more than **30 days after the usage date**.
This is our implementation assumption, not a vendor guarantee: the assignment
bounds restatements to 30 days but gives no maximum first-arrival delay. To start
simple and avoid unnecessary API requests and compute, there is **no automatic
weekly reconciliation**. After backfill, normal runs use the 30-day overlap;
`--reconcile` remains available for a manual full-history refresh. The stored
`last_full_scan` marks a committed full scan; its age does not trigger work.

Black-box measurements at simulated API date **2026-10-26** covered all **5,616**
visible record IDs, with usage dates **2026-01-01 through 2026-09-27**:

| First-arrival delay | Records | Percentage |
| --- | ---: | ---: |
| 0–1 days | 5,613 | 99.95% |
| 2–7 days | 0 | 0% |
| 8–30 days | 3 | 0.05% |
| 31–60 days | 0 | 0% |
| More than 60 days | 0 | 0% |

We queried isolated, unchanged API instances at usage date +1, +7, +30, and +60
days (capped at the observation date), stopping once each ID appeared. This
measures visibility through the documented HTTP interface, not
`updated_at - usage_date`, which would mix first arrivals with restatements.
The three late records were C008's September 18 H100, H200, and SSD usage:
absent September 25, present September 26, **eight days late**, with current
amounts totaling **$10,775.44**. No source implementation or backing data was read.

The 30-day overlap covers every measured arrival, so this dataset provides no
evidence that weekly full scans are currently needed. It does not prove a future
lateness bound or include records not yet visible at the observation cutoff.
If the assumption is violated, records outside the overlap can remain missing
until a manual `--reconcile`; incremental and fresh loads may then differ.

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
single point-in-time snapshot; overlap revisits recent differences, and manual
reconciliation can repair older omissions.

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
reprocessing jobs. Measure first-arrival delays and revisit the overlap or add
scheduled reconciliation if evidence warrants it. Alert on failed or missing runs
and stale data; validate changes in CI before deployment. On spot nodes, durable
staging and atomic publication would avoid refetching entire interrupted runs. Finance workbook
layout detection remains part of the future contract-ingestion work, not this loader.

AI assistance was used to implement the loader, tests, and documentation. Verification
included deterministic tests and live requests to the unchanged mock API.
