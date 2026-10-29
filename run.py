"""Run the pipeline on a local kind cluster. Standard library only; macOS, Linux, Windows.

    python run.py up              # create the cluster, build and load images, deploy
    python run.py export OUT_DIR  # write the two Part 2 tables as CSV
    python run.py down            # delete the cluster

Needs docker, kind and kubectl on PATH. Every kubectl call names the kind-orbital
context, so this never touches another cluster you're signed in to.

The billing API credentials go into a Kubernetes Secret. They come from
BILLING_CLIENT_ID and BILLING_CLIENT_SECRET when set, otherwise from the demo
values the assignment publishes. They are never written into an image or a file.
"""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
CLUSTER = 'orbital'
CONTEXT = f'kind-{CLUSTER}'
NAMESPACE = 'orbital'
API_IMAGE = 'orbital-billing-api:1.0'
PIPELINE_IMAGE = 'orbital-pipeline:1.0'
EXPORT_POD = 'orbital-export'
TABLES = ('contract_utilization_daily', 'unattributed_usage_daily')
DEMO_CREDENTIALS = ('candidate', 'orbital-s3cret')


class RunError(Exception):
    """A step failed; the message says which."""


def run(*command, stdin=None, capture=False):
    """Run a command from the repository root; return its stdout bytes when capture is set."""
    shown = ['--from-literal=BILLING_CLIENT_SECRET=***'
             if arg.startswith('--from-literal=BILLING_CLIENT_SECRET=') else arg
             for arg in command]
    print('+', ' '.join(shown), flush=True)
    try:
        result = subprocess.run(command, cwd=ROOT, input=stdin,
                                stdout=subprocess.PIPE if capture else None)
    except FileNotFoundError:
        raise RunError(f'{command[0]} not found on PATH') from None
    if result.returncode != 0:
        raise RunError(f'{command[0]} exited {result.returncode}')
    return result.stdout


def kubectl(*args, **kwargs):
    return run('kubectl', '--context', CONTEXT, '--namespace', NAMESPACE, *args, **kwargs)


def apply_generated(*create_args):
    """Create or update an object that kubectl generates (a Secret, a ConfigMap from files).

    `kubectl create` fails if the object exists, so render it with --dry-run and
    pipe it to `kubectl apply`, which creates or updates. That keeps `up` rerunnable.
    """
    manifest = kubectl('create', *create_args, '--dry-run=client', '--output=yaml', capture=True)
    kubectl('apply', '--filename=-', stdin=manifest)


def up():
    for tool in ('docker', 'kind', 'kubectl'):
        if shutil.which(tool) is None:
            raise RunError(f'{tool} not found on PATH')

    clusters = run('kind', 'get', 'clusters', capture=True).decode().split()
    if CLUSTER not in clusters:
        run('kind', 'create', 'cluster', '--name', CLUSTER, '--wait', '180s')

    # The cluster can't download these images, so copy them into the kind node.
    run('docker', 'build', '--tag', API_IMAGE, 'mock_api')
    run('docker', 'build', '--tag', PIPELINE_IMAGE, '.')
    run('kind', 'load', 'docker-image', '--name', CLUSTER, API_IMAGE, PIPELINE_IMAGE)

    # The provided manifest, unchanged. It creates the namespace and resets AS_OF
    # to its default.
    kubectl('apply', '--filename', str(Path('mock_api', 'k8s', 'billing-api.yaml')))

    client_id = os.getenv('BILLING_CLIENT_ID') or DEMO_CREDENTIALS[0]
    secret = os.getenv('BILLING_CLIENT_SECRET') or DEMO_CREDENTIALS[1]
    apply_generated('secret', 'generic', 'billing-api-credentials',
                    f'--from-literal=BILLING_CLIENT_ID={client_id}',
                    f'--from-literal=BILLING_CLIENT_SECRET={secret}')
    # Same pattern as the loader, so Excel's ~$ lock files are left out.
    workbooks = sorted(Path('data', 'commitments').glob('commitments_*.xlsx'))
    apply_generated('configmap', 'orbital-commitments',
                    *(f'--from-file={path}' for path in workbooks))

    kubectl('apply', '--filename', str(Path('k8s', 'pipeline.yaml')))
    kubectl('rollout', 'status', 'deployment/billing-api', '--timeout=180s')
    print('Ready. Start a run with:\n'
          f'  kubectl -n {NAMESPACE} create job run-1 --from=cronjob/orbital-pipeline')


def export(out_dir):
    """Copy both tables out of the cluster's database through a short-lived helper pod.

    The tables are printed to stdout with kubectl exec and written here byte for
    byte, which works the same on every OS (kubectl cp needs tar). It only reads
    the last committed tables, so it's safe while a run is in progress.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    kubectl('delete', 'pod', EXPORT_POD, '--ignore-not-found', '--wait=true')
    kubectl('apply', '--filename', str(Path('k8s', 'export-pod.yaml')))
    try:
        kubectl('wait', '--for=condition=Ready', f'pod/{EXPORT_POD}', '--timeout=180s')
        for table in TABLES:
            content = kubectl('exec', EXPORT_POD, '--', 'python', 'build_models.py',
                              '--print', table, capture=True)
            path = out_dir / f'{table}.csv'
            partial = path.with_suffix('.csv.partial')
            partial.write_bytes(content)
            partial.replace(path)
            print(f'Wrote {path}')
    finally:
        kubectl('delete', 'pod', EXPORT_POD, '--ignore-not-found', '--wait=false')


def down():
    run('kind', 'delete', 'cluster', '--name', CLUSTER)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('up', help='create the cluster and deploy everything')
    export_parser = commands.add_parser('export', help='write the Part 2 tables as CSV')
    export_parser.add_argument('out_dir', metavar='OUT_DIR')
    commands.add_parser('down', help='delete the cluster')
    args = parser.parse_args()
    try:
        if args.command == 'up':
            up()
        elif args.command == 'export':
            export(args.out_dir)
        else:
            down()
        return 0
    except RunError as exc:
        print(f'run.py {args.command} failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
