# Senior Data Engineer: Home Assignment

We'll send you the submission deadline separately. There's no time limit within it, but we'd rather
see a smaller solution with clear trade-offs than a big one. If there's something you didn't get to,
write down what you would do next. That counts.

## Context

*Orbital Compute* is a (fictional) GPU cloud. Customers sign **commitment contracts**: they agree to
spend at least $X between a start date and an end date. Finance wants to know, every day, which
contracts are **on track** and which ones will likely **under-consume** by the end of their term.

You get two sources. Both are realistic, which means both are a bit messy:

| Source | What it is | Where |
|---|---|---|
| **Commitments tracker** | An Excel workbook that Finance maintains by hand. A copy is exported at every month end. | `data/commitments/commitments_<as_of>.xlsx` (3 snapshots) |
| **Billing usage API** | A third-party REST API with daily usage and spend per customer and SKU. | `mock_api/` (run locally or in Kubernetes) |

## Setup

You need:

- Python 3.10+
- Docker (Docker Desktop on macOS and Windows; on Windows, in Linux-containers mode, which is the default)
- [kind](https://kind.sigs.k8s.io/) and `kubectl`

Everything works on macOS, Linux and Windows (on Windows, use `py` wherever we write `python` if
`python` isn't on your PATH). **Your solution must too**, because we may run it on any of them.

For local development you can run the mock API directly (standard library only):

```bash
python mock_api/server.py --as-of 2026-09-20      # serves on http://127.0.0.1:8765
```

`--as-of` sets the API's "today". Treat the API as a black box and don't change `mock_api/`.

You can use any language, libraries and storage for your own solution (DuckDB, SQLite, Postgres,
Parquet files, …).

## Billing usage API

**Auth.** `POST /oauth/token` with JSON `{"client_id": "candidate", "client_secret": "orbital-s3cret"}`
returns `{"access_token": "...", "expires_in": 30}`. Send it as `Authorization: Bearer <token>`.
Tokens expire after **30 seconds**.

**Usage.** `GET /v1/usage?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD[&page_size=N][&page_token=T]`

- Both dates are inclusive. A request can cover **at most 31 days**. `page_size` max is 200 (default 100).
- Response: `{"data": [...], "next_page_token": "..." | null}`
- Each record looks like this:
  ```json
  {"record_id": "C002-20260903-gpu-h100", "customer_id": "C002", "usage_date": "2026-09-03",
   "sku": "gpu-h100", "quantity": 812.4, "unit": "gpu_hour", "amount_usd": 1706.1,
   "status": "final", "updated_at": "2026-09-04T03:00:00Z"}
  ```

**What the vendor documentation says (and it's accurate):**

- The API is rate limited (`429` with a `Retry-After` header in seconds) and sometimes returns `503`.
- Usage for a day is usually published the next morning, but it **can arrive late**.
- Billing may **restate** usage for up to **30 days** after the usage date. A restated record keeps
  its `record_id` and gets a new `updated_at`. `status` can become `"void"`, which means the
  record should no longer count.
- Usage data starts on **2026-01-01**.

## Commitments tracker

Each snapshot has one row per contract: contract id, customer id and name, start date, end date,
commitment amount (USD), status, and some other columns. Business rules from Finance:

- `End Date` is **inclusive**: it is the last day of the term.
- A contract's terms can be **amended** (for example, the end date or amount changes). The
  latest snapshot holds the current terms.
- Finance sometimes **removes expired contracts** from the tracker. They were still real contracts.
- The layout of the workbook is **not stable** between exports. Finance changes it and doesn't
  tell anyone.
- Usage is attributed to a contract by `customer_id` and date: a customer's spend on day D counts
  toward their contract that is active on D. Contracts of the same customer never overlap. Spend
  on a day with no active contract is on-demand. It doesn't count toward any commitment, but
  Finance wants to see it.

## What to build

### Part 1: Ingestion
1. Load usage from the API into your storage. The first run backfills everything. Later runs
   are incremental and must pick up late and restated data.
2. Load all three tracker snapshots and keep **full history** of contract terms (SCD Type 2 or
   equivalent), including contracts that disappear from the tracker.

Your pipeline must be **idempotent**: running it twice in a row against the same API state must not
change any result.

### Part 2: Model
Build these two tables.

**`contract_utilization_daily`**: one row per contract per day, from `start_date` to
`min(end_date, last usage date loaded)`, using each contract's **latest known terms**:

| column | definition |
|---|---|
| `contract_id`, `customer_id`, `date` | |
| `commitment_usd`, `term_days` | `term_days` includes both the start and end dates |
| `days_elapsed` | `date - start_date + 1` |
| `consumed_usd` | the customer's non-void spend on `date` |
| `consumed_to_date_usd` | cumulative `consumed_usd` from `start_date` through `date` |
| `expected_to_date_usd` | `commitment_usd * days_elapsed / term_days` (linear burn) |
| `utilization_pct` | `consumed_to_date_usd / expected_to_date_usd` |
| `projected_term_usd` | `consumed_to_date_usd / days_elapsed * term_days` |
| `at_risk` | `days_elapsed >= 30 AND projected_term_usd < 0.9 * commitment_usd` |

**`unattributed_usage_daily`**: `customer_id, date, amount_usd` for spend on days when the customer had
no active contract.

### Part 3: Run it on Kubernetes
Package the pipeline and run it as a **daily scheduled job** on a local [kind](https://kind.sigs.k8s.io/)
cluster, next to the billing API. Treat it like a small production deployment:

- Build your pipeline into a container image.
- Deploy the billing API with the manifest we provide, unchanged: `mock_api/Dockerfile` builds the image
  `orbital-billing-api:1.0`, and `mock_api/k8s/billing-api.yaml` creates the namespace `orbital`, the
  Deployment and the Service. In the cluster the API is at `http://billing-api.orbital.svc.cluster.local:8765`.
- Run the pipeline as a CronJob named **`orbital-pipeline`** in the namespace `orbital`.
- Your storage lives in the cluster too. How you run it is up to you.
- Don't bake configuration or credentials into the image.
- Assume the pipeline runs on **preemptible nodes**: a run can be killed at any moment and must
  recover on its own. Also, someone will eventually start a run by hand while another one is going.
  Neither may corrupt or double-count data.

Provide these three commands. They're how we run your solution, so please keep the names exactly:

| Command | What it does |
|---|---|
| `python run.py up` | Creates the kind cluster **`orbital`** if it doesn't exist, builds and loads the images, and deploys the billing API (at the manifest's default as-of), your storage and your CronJob. It doesn't run the pipeline. |
| `python run.py export OUT_DIR` | Writes your two Part 2 tables to `OUT_DIR/contract_utilization_daily.csv` and `OUT_DIR/unattributed_usage_daily.csv`, with a header row and the column names above. Formatting is up to you: we match rows by key and compare numbers with a small tolerance. |
| `python run.py down` | Deletes the cluster. |

Implement `run.py` however you like, as long as it works on macOS, Linux and Windows.

We trigger runs with plain `kubectl`, so you don't need a command for that:

```bash
kubectl -n orbital create job run-1 --from=cronjob/orbital-pipeline      # one run, now
kubectl -n orbital set env deployment/billing-api AS_OF=2026-09-28       # move the API's "today"
```

The API's "today" is simulated and your pipeline can't see it. Your machine's clock will be later than
it, and, as with any real API, usage after its "today" just isn't there yet.

### Part 4: Short write-up (`NOTES.md`, about 1 page)
- The main design decisions and trade-offs you made, and what you deliberately left out.
- How would you run this in production? Assume a real Kubernetes cluster on spot nodes and an
  orchestrator such as Airflow. Cover scheduling, backfills and reprocessing, alerting, CI/CD, and what
  you'd change in your Part 3 setup. You don't need to build it. A short description or a sketch is enough.
- What happens in your pipeline when Finance changes the workbook layout again next month?
  How would you find out?
- Which data-quality checks would you run, and which of them should block publishing?

## How we will run it

```bash
python run.py up                          # 1. fresh cluster, API at as-of 2026-09-20 (the manifest's default)
kubectl -n orbital create job ...         #    initial load. We delete the pipeline pod partway through.
kubectl -n orbital set env deployment/billing-api AS_OF=2026-09-28
kubectl -n orbital create job ...         # 2. incremental run, then python run.py export
kubectl -n orbital create job ...         # 3. same again: results must be identical
kubectl -n orbital create job ...  (x2)   # 4. two runs at the same time: results must still be identical
python run.py down && python run.py up    # 5. full reload: fresh cluster, set AS_OF=2026-09-28 again,
kubectl -n orbital create job ...         #    one run. Must match step 2.
```

## Submission

A git repository (or a zip with the `.git` folder) that contains:
- the source code, `run.py`, your Dockerfile(s) and Kubernetes manifests, and `mock_api/` and `data/`
  as provided, so `python run.py up` works from a fresh clone
- a `README` with anything we need beyond the three commands
- `NOTES.md`

Commit history is welcome. We like to see how you worked.

**AI tools:** you can use any tools you like, including AI assistants. In the follow-up interview we
will go through your solution with you, so make sure you understand and can defend every part of it.
Please mention briefly in `NOTES.md` how you used them.

In the follow-up call we will walk through your solution together and talk about
extending it. There is no single right answer. We are interested in how you reason about messy data.
