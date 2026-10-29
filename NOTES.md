# Notes

**Guiding principle:** keep it small and explainable, but never trade away financial
correctness, history, or safe reruns.

## 1. Design decisions and trade-offs

### Usage ingestion

- **Scan window: a 30-day overlap**
  - *Choice:* each run rescans from 30 days before the latest stored `usage_date`
    through today (UTC). `--reconcile` rescans from 2026-01-01.
  - *Why:* the API restates records for up to 30 days. 
    The window counts back from the newest stored usage date rather than the system clock, 
    because the API’s simulated date can be weeks behind the real one.
  - *Trade-off:* this assumes a record first arrives within 30 days. I measured it:
    of 5,616 records, 99.95% arrived within a day, 3 arrived 8 days late, and none
    arrived later. Anything older stays missing until someone runs `--reconcile`.
- **Record identity: latest version per `record_id`**
  - *Choice:* a newer `updated_at` replaces the stored version. 
    An identical or older version of a stored record is ignored. The same `updated_at` with different contents fails the run.
  - *Why:* reruns are idempotent and corrections never double-count.
  - *Trade-off:* we keep only the latest state, not every billing revision.
- **Voids are kept, not deleted**
  - *Choice:* void rows stay in `usage` and are excluded by the `non_void_usage` view.
  - *Why:* Keeping the void row means an older copy of the record, if it arrives again, is recognized as outdated and ignored.
- **Strict validation (fail closed)**
  - *Choice:* the run fails on an unknown `status` (anything but `final`/`void`) or on a
    timestamp not in the exact `YYYY-MM-DDTHH:MM:SSZ` format. In a fixed format like
    that, string order is time order.
  - *Trade-off:* one bad record rolls back the whole usage step. The data stays stale
    until the record is fixed, but a skipped charge or void would silently corrupt totals.
- **Money:** Amounts are stored as text and added up in Python with Decimal, because SQLite’s SUM() 
    converts them to floating-point numbers and introduces small rounding errors.
- **HTTP:** up to 6 attempts with jittered backoff, honoring `Retry-After`. These are
    starting defaults, not yet tuned against request metrics.

### Commitments workbooks

- **Layout drift: discover the header row, then explicit aliases**
  - *Choice:* find the one row that names all five mandatory fields, normalize it, and
    map it through `ALIASES`.
  - *Why:* this handles renamed sheets, title rows, reordered columns and both
    commitment header names.
  - *Trade-off:* each new header name needs a one-line alias. I rejected fuzzy
    matching: "Monthly" and "Total Commitment" look alike but are different money.
- **Mandatory vs descriptive fields**
  - *Mandatory* (contract ID, customer ID, start date, end date, commitment): the
    calculations need them, so a missing column or empty cell rejects the file.
  - *Descriptive* (customer name, status): nice to have, so they may be null. The file
    still loads and the run summary warns Finance once, when that snapshot loads.
  - *History rule:* a missing descriptive value doesn't count as an amendment, so a
    dropped column doesn't create fake versions. Any real change stores the row as
    received, nulls included. I never copy values from older snapshots.
  - *Trade-off:* a contract's name or status can be null in the output. The models
    don't use either, so the numbers are unaffected.
- **One bad file doesn't block the rest**
  - *Choice:* each file loads in its own savepoint. A bad file is rolled back, the other
    files load, and the run exits 1.
  - *Trade-off:* history is rebuilt after every new file. That takes milliseconds here,
    but it is wasted work at scale.
- **Snapshot identity: date + checksum**
  - *Choice:* a replay is a no-op, changed bytes for a loaded date are rejected, and an
    older unseen date is accepted.
  - *Trade-off:* there's no workflow yet to correct an already loaded snapshot.
- **History: SCD Type 2, rebuilt from observations**
  - *Choice:* replay every snapshot in date order, and expose `contracts_current`.
  - *Why:* late or out-of-order files just work, and the result is deterministic.
  - *Caveats:* `observed_from` is when we saw a change, not when it took effect. A
    contract missing from a later snapshot keeps its last known version.

### Models

- **Full rebuild of both tables in one transaction:** cheap at this size, and the
  tables can't drift from their inputs.
- **Latest known terms** (`contracts_current`). This isn't an "as known at the time" report.
- **Dates decide activity, not the status label**
  - *Why:* an early end needs a real end date, and guessing one would misattribute spend.
  - *Trade-off:* a contract marked Expired with a future end date is still tracked.
    The loader warns Finance (exit 0) instead of failing.
- **Zero-spend days are included.** Spend with no active contract goes to
  `unattributed_usage_daily`.
- **`utilization_pct` is a ratio** (1.0 = on track). `at_risk` is computed before
  rounding, so rounding can't flip a decision at the 0.9 boundary.

### Runs, transactions and concurrency

- **Choice:** each step is one `BEGIN IMMEDIATE` transaction in SQLite (WAL,
  `synchronous=FULL`). `pipeline.py` runs all three steps even if one fails, then exits
  non-zero.
- **Gains:**
  - A killed pod loses only its uncommitted work.
  - Concurrent runs queue instead of interleaving.
  - Readers see the last commit.
  - A bad workbook doesn't block fresh usage.
- **Costs:**
  - The pipeline isn't atomic across sources. The three steps commit separately,
    so the models may combine new usage with last month’s contract terms when a workbook is rejected.
  - The write lock is held during HTTP calls.
  - An interrupted scan starts over.


### Left out on purpose

- **PostgreSQL server instead of a SQLite file:** SQLite needs no deployment and its
  transactions already cover crashes and concurrent runs. But the file lives on one
  node's disk, so on a multi-node spot cluster I'd move to Postgres and use an advisory
  lock in place of `BEGIN IMMEDIATE`.
- **Durable staging and checkpoints:** would let an interrupted run resume instead of
  fetching everything again.
- Scheduled reconciliation, and `--start-date/--end-date` for targeted rescans
- A snapshot correction workflow
- Storing extra workbook columns (owner, region, notes)
- Alerting
- Checking the workbook TOTAL row (it is skipped today)
- **Archive and quarantine:** keep every received workbook, including rejected ones,
  with its rejection reason, so a held file can be reviewed and reprocessed. Today a
  rejected file is only reported, and the source file is the evidence.

## 2. Production (real cluster, spot nodes, Airflow)

- **Changes to Part 3**
  - SQLite on a `ReadWriteOnce` PVC → Postgres, either managed or on on-demand nodes.
    A spot node takes its local disk with it.
  - `BEGIN IMMEDIATE` → a Postgres advisory lock.
  - Workbooks from a ConfigMap → object storage, with every received file archived.
  - Secrets from a secret manager. Images tagged by git SHA.
  - Keep `podFailurePolicy` for preemptions, the restricted security context and the
    resource limits.
- **Scheduling**
  - An Airflow DAG: `ingest_usage` ∥ `ingest_commitments` → quality checks →
    `build_models` → publish.
  - Each task runs as a `KubernetesPodOperator` on spot nodes, with retries, a
    timeout and `max_active_runs=1`.
  - Fetched windows are committed to a staging table, so a preempted run resumes
    instead of refetching. Publishing is an atomic swap.
- **Backfills and reprocessing**
  - A manual DAG takes a date range. It is tracked separately from the full-scan marker.
  - A monthly full reconcile as cheap insurance against the 30-day assumption.
  - The models are full rebuilds, so re-running them is the reprocess.
- **Alerting**
  - Page on-call for failed or missing runs and stale data (latest `usage_date` or
    snapshot older than expected).
  - Send Finance's channel the rejected files, status contradictions and unknown labels.
  - Track HTTP retries and latency.
- **CI/CD**
  - Lint and unit tests on every PR.
  - An integration run on kind against the mock API: the grader's sequence, then a
    diff of the exports.
  - Build once, then promote the same SHA-tagged image through staging.

## 3. When Finance changes the layout again

- **Already handled:** renamed sheets, moved or extra columns, title rows, known header
  variants.
- **Loads with a warning:** a dropped or renamed descriptive column (customer name,
  status). Those values are stored as null.
- **Fails safely:** an unknown mandatory header, a field matched twice, or a new date
  format rejects *that file only*, with an error that names the file and the header.
  - Usage and the other files still publish.
  - The last good contract terms stay in use.
- **How we find out:** the run exits 1, so the Job or Airflow task fails and alerts.
- **Fix:** confirm the meaning with Finance, add an alias and a test, and rerun. History
  rebuilds in order, so no separate backfill is needed.
- **New status label:** a warning, not a failure.
- **Nice to have:** suggest the closest alias in the error, but never apply it
  automatically.

## 4. Data-quality checks

**Rule:** block when publishing would show wrong money. Warn when the data is
plausible but needs a human, or when blocking would hide good data from everyone.

| Check | Built | Blocks publishing? |
|---|---|---|
| Usage schema, types, `status` in `final`/`void`, exact timestamp format | Yes | Yes |
| Same `record_id` + `updated_at` with different contents | Yes | Yes |
| Workbook: mandatory headers and values, parseable dates and amounts, end ≥ start | Yes | Yes, that file |
| Workbook: missing or empty customer name or status | Yes | No, warn Finance |
| Conflicting duplicate rows, overlapping contracts for one customer | Yes | Yes, that file |
| Changed bytes for an already loaded snapshot date | Yes | Yes, that file |
| Status label contradicts dates, unknown label | Yes | No, warn Finance |
| One row per contract-day; attributed + unattributed = non-void usage total | No | Yes |
| Workbook TOTAL row ≠ sum of commitments | No | No, warn Finance |
| Freshness: usage or snapshot older than expected | No | No, alert |
| Day-over-day spend or row-count anomaly, rising unattributed share | No | No, alert |

## AI usage

I used AI assistants to lay out alternatives, to write and then simplify the loaders,
tests and docs, and to run the measurements above. I checked the results with the unit
tests and with live runs against the unchanged mock API.
