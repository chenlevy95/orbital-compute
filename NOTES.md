# Design decisions and trade-offs

## Usage ingestion

The first run scans from 2026-01-01 through today (UTC), in windows of at most
31 days and pages of 200 records. Later runs start 30 days before the latest usage
date already stored. The overlap is anchored to stored data rather than the clock
because the API's simulated "today" lags the real one. The overlap covers
restatements (documented as up to 30 days) and late first arrivals.
`--reconcile` rescans all history.

**Assumption:** a record first arrives no more than 30 days after its usage date.
The vendor doesn't guarantee this, so I measured it through the API at a simulated
date of 2026-10-26. That covered all 5,616 record IDs for usage dates 2026-01-01
to 2026-09-27:

- 99.95% of records arrived within one day.
- Three records arrived 8 days late: C008's usage on 2026-09-18.
- No record arrived more than 30 days late.

A record that arrives later than the overlap stays missing until someone runs
`--reconcile`.

Records are keyed by `record_id`. A version replaces the stored one only if its
`updated_at` is newer, so replays and older versions change nothing. If the same
`updated_at` comes with different contents, the run fails. Voided records are
kept, and the `non_void_usage` view leaves them out. `updated_at` must match the
observed `YYYY-MM-DDTHH:MM:SSZ` format exactly, so that comparing the strings
compares the times. Amounts are stored as exact decimal text.

For HTTP, the client retries connection errors, 429 and 5xx up to 6 times with
jittered backoff, and honors `Retry-After`. It refreshes the token before it
expires, and once more after a 401.

## Commitments ingestion

Every workbook is parsed and validated before the database is touched. The header
row is found by looking in every sheet for a single row that names all seven
fields. Each field name goes through an explicit alias table, so the parser copes
with renamed sheets, title rows above the table, reordered columns and the two
commitment header names. The parser also handles these:

- IDs with stray spaces are trimmed.
- Dates can be Excel dates, `YYYY-MM-DD` or `1 Jun 2026`.
- Amounts like `$900,000.00` are parsed.
- Blank rows, the TOTAL row and `*` footnotes are skipped.
- Exact duplicate rows collapse into one.

Anything else makes the loader skip that file. That includes a missing or ambiguous
header, a bad value, conflicting duplicates, an end date before the start date,
overlapping contracts for one customer, and changed bytes for a date that was
already loaded. I chose to reject the file instead of guessing: a misread amount
column would silently corrupt every utilization number. Each file loads inside its
own savepoint, so a file that fails a check against stored data is undone on its
own. The other files still load, and the run exits 1 with the errors, so the job
keeps failing until someone fixes the file.

Trade-off: the checks that need stored data run by writing first. The changed-bytes
check and the overlap check both work this way, and a rejected file is undone with
`ROLLBACK TO` the savepoint. That rollback only happens on rejection, and it is cheap
because nothing has been committed, so readers never see the rejected rows. The
bigger cost is on the happy path. History is deleted and replayed after every new
file, so a first run with three files rebuilds it three times. At a few dozen rows
this takes milliseconds, so I kept the simpler code.

A skipped file is never stored. Once Finance sends a fixed file under the same date,
or a new alias is added, the next normal run loads it. History is rebuilt in date
order, so no separate backfill step is needed. The side effect is that a late middle
snapshot can move later versions' `observed_from` dates. For example, if August were
skipped, K-1009's amendment would first show as observed on Sep 28, then move to
Aug 31 once August loads.

New snapshots are stored as observations. History is then rebuilt from all
observations in date order, so files can arrive in any order. A contract that
Finance removes from a later snapshot keeps its last version open in
`contracts_current`. `observed_from` is the date of the snapshot where a version
first appears, not the date the amendment took effect.

## Models

`build_models.py` rebuilds both tables in full on every run, from
`contracts_current` (the latest known terms) and `non_void_usage`. A full rebuild
is cheap at this size, and it means a rerun can't drift from its inputs.

- Whether a contract is active on a day depends only on its dates. Finance's
  `status` column is a label and isn't used.
- Every day of the term gets a row, including days with no spend
  (`consumed_usd = 0`).
- "Last usage date loaded" is the latest `usage_date` in `usage`, counting voided
  records, because it marks how far the load has reached.
- Spend is summed with `Decimal` from the stored decimal text. Source amounts and
  their sums are exact. Expected and projected spend are rounded to cents.
- `utilization_pct` is the ratio from the spec (1.0 means on track), rounded to
  6 decimal places. It is not multiplied by 100. `at_risk` is computed from the
  unrounded values.

## Concurrency and recovery

The loaders and the model build use SQLite in WAL mode. Each run is a single `BEGIN IMMEDIATE`
transaction:

- A killed run rolls back.
- Concurrent runs queue for the write lock instead of interleaving.
- Readers keep seeing the last committed state.

The cost is that the write lock is held while waiting on HTTP, and an interrupted
run starts its scan from the beginning. At this data size that's fine.

## Left out on purpose

- I didn't build `run.py` or the Kubernetes deployment.
- Workbooks and rejection reasons are not stored in the database. Errors go to the
  run's output, and the source files remain the evidence.
- Replacing a snapshot that was already loaded isn't supported. A changed file for
  a stored date is rejected.
- Extra workbook columns (owner, region, notes) aren't stored.
- No PostgreSQL, durable staging or checkpoints. SQLite is enough for one writer.

## Next steps

- Build the Part 2 models, `run.py` and the Kubernetes CronJob.
- Validate commitment snapshots in memory, then write once. Read the stored
  observations, add each new file in memory, and rebuild history with a pure function
  that returns rows instead of writing them. Drop any file that fails a check, then
  write the accepted snapshots and replace `contract_history` once per run. That
  removes the savepoints, the write-then-undo, and the repeated rebuilds. It also
  makes the history logic testable without a database. At much larger sizes, update
  history incrementally in SQL instead of keeping observations in memory.

## Production

- **Scheduling:** run the jobs from Airflow (or a similar orchestrator) every day,
  with retries after a delay. Make backfills and full reconciliations explicit,
  parameterized jobs.
- **Storage and secrets:** use a managed database and a secret store. On spot
  nodes, stage data durably and publish it atomically, so an interrupted run
  doesn't have to fetch everything again.
- **Alerting:** alert on failed or missing runs, stale data, and a Finance export
  that didn't arrive.
- **CI:** run the tests and the smoke check before every deploy.
- **Layout changes:** when Finance changes the workbook layout, the run fails and
  names the file and the missing header. Someone confirms the meaning with Finance,
  adds an alias and a test, and the next run loads the file.

## AI usage

I used AI assistants to write and later simplify the loaders, the tests and this
documentation. I verified the results with the unit tests and with live runs
against the unchanged mock API.
