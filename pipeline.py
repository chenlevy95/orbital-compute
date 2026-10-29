"""Run the whole pipeline once: usage, then commitments, then the models.

    python pipeline.py

This is the container's entrypoint. Every setting comes from the environment
(BILLING_API_URL, BILLING_CLIENT_ID, BILLING_CLIENT_SECRET, USAGE_DATABASE,
COMMITMENTS_DIR, DB_LOCK_TIMEOUT_SECONDS), so the image holds no configuration.

Each step commits on its own. A run killed between steps leaves consistent tables
that are only as stale as the previous run, and the next run catches up. Every
step runs even if an earlier one failed, so a rejected Excel file doesn't stop
fresh usage from reaching the models. The exit code is the worst of the steps,
so a failure still shows on the Job and Kubernetes retries it.
"""

import sys
import traceback

import build_models
import ingest_commitments
import ingest_usage

STEPS = (('usage', ingest_usage.main),
         ('commitments', ingest_commitments.main),
         ('models', build_models.main))


def run(steps=STEPS):
    worst = 0
    for name, step in steps:
        print(f'== {name}', flush=True)
        sys.argv = [f'pipeline.py {name}']  # each main() parses its own (empty) arguments
        try:
            code = step()
        except Exception:
            traceback.print_exc()
            code = 1
        sys.stdout.flush()
        sys.stderr.flush()
        worst = max(worst, code or 0)
    print(f'== done, exit {worst}', flush=True)
    return worst


if __name__ == '__main__':
    sys.exit(run())
