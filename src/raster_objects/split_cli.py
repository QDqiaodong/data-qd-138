"""Command line for the persistent leakage-safe train/eval split warehouse.

Subcommands::

    python -m raster_objects.split_cli init   --db DB
    python -m raster_objects.split_cli ingest --db DB --source BATCH
    python -m raster_objects.split_cli plan   --db DB --source REQUEST
    python -m raster_objects.split_cli commit --db DB --plan-id ID
    python -m raster_objects.split_cli export --db DB --plan-id ID --output JSON
    python -m raster_objects.split_cli status --db DB

Exit codes: 0 success (including the explicit business results
``contaminated`` / ``infeasible`` / ``stale_plan``), 2 input or protocol
error, 4 retryable busy error from a concurrent writer.  Every error leaves
the database and any previously published report untouched.
"""
import argparse
import json
import sys
import time
from pathlib import Path

from .common import read, save
from .split_store import SplitBusy, SplitError, SplitStore

_BUSY_ATTEMPTS = 20
_BUSY_DELAY = 0.05


def _retry(action):
    delay = _BUSY_DELAY
    for attempt in range(_BUSY_ATTEMPTS):
        try:
            return action()
        except SplitBusy:
            if attempt == _BUSY_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 1.5, 0.5)


def _emit(report):
    print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))


def _init(args):
    SplitStore.initialize(args.db)
    _emit({'created': str(args.db)})
    return 0


def _ingest(args):
    batch, _digest = read(args.source)
    report = _retry(lambda: SplitStore(args.db).ingest(batch))
    _emit(report)
    return 0


def _plan(args):
    request, _digest = read(args.source)
    report = _retry(lambda: SplitStore(args.db).plan(request))
    _emit(report)
    return 0


def _commit(args):
    report = _retry(lambda: SplitStore(args.db).commit(args.plan_id))
    _emit(report)
    return 0


def _export(args):
    output = Path(args.output)
    db = Path(args.db)
    forbidden = {db} | {Path(str(db) + suffix)
                        for suffix in ('-wal', '-shm', '-journal')}
    if output.resolve() in {candidate.resolve() for candidate in forbidden}:
        raise SplitError(
            'export output must not overwrite the database'
            ' or its SQLite auxiliary files')
    report = SplitStore(args.db).export(args.plan_id)
    save(output, report)
    _emit(report)
    return 0


def _status(args):
    _emit(SplitStore(args.db).status())
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='raster_objects.split_cli',
        description='Persistent leakage-safe train/eval split warehouse',
    )
    sub = parser.add_subparsers(dest='command', required=True)

    p_init = sub.add_parser('init', help='create a new split warehouse')
    p_init.add_argument('--db', required=True)
    p_init.set_defaults(handler=_init)

    p_ingest = sub.add_parser('ingest', help='ingest one sample batch')
    p_ingest.add_argument('--db', required=True)
    p_ingest.add_argument('--source', required=True)
    p_ingest.set_defaults(handler=_ingest)

    p_plan = sub.add_parser('plan', help='propose a quota-satisfying split')
    p_plan.add_argument('--db', required=True)
    p_plan.add_argument('--source', required=True)
    p_plan.set_defaults(handler=_plan)

    p_commit = sub.add_parser('commit', help='commit a proposed plan')
    p_commit.add_argument('--db', required=True)
    p_commit.add_argument('--plan-id', required=True)
    p_commit.set_defaults(handler=_commit)

    p_export = sub.add_parser('export', help='export a committed plan')
    p_export.add_argument('--db', required=True)
    p_export.add_argument('--plan-id', required=True)
    p_export.add_argument('--output', required=True)
    p_export.set_defaults(handler=_export)

    p_status = sub.add_parser('status', help='snapshot warehouse status')
    p_status.add_argument('--db', required=True)
    p_status.set_defaults(handler=_status)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except SplitBusy as error:
        print(json.dumps({'busy': str(error), 'retryable': True},
                         sort_keys=True), file=sys.stderr)
        return 4
    except (SplitError, OSError, ValueError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
