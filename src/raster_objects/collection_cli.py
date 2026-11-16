"""Command line for the persistent blind double-annotation collection.

Subcommands::

    python -m raster_objects.collection_cli init   --db DB --source CAMPAIGN
    python -m raster_objects.collection_cli claim  --db DB --worker W --now T
    python -m raster_objects.collection_cli submit --db DB --worker W \
        --lease TOKEN --now T --source ANSWER
    python -m raster_objects.collection_cli status --db DB
    python -m raster_objects.collection_cli export --db DB --output JSON

Exit codes: 0 success, 2 input/protocol error (including unknown, conflicting,
expired or disqualified calls), 4 retryable busy error from a concurrent
writer.  Every error leaves the database and any previously published report
untouched.
"""
import argparse
import json
import sys
import time
from pathlib import Path

from .collection import (
    Collection,
    CollectionBusy,
    CollectionError,
)
from .common import read, save

_BUSY_ATTEMPTS = 20
_BUSY_DELAY = 0.05


def _retry(action):
    delay = _BUSY_DELAY
    for attempt in range(_BUSY_ATTEMPTS):
        try:
            return action()
        except CollectionBusy:
            if attempt == _BUSY_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 1.5, 0.5)


def _emit(report):
    print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))


def _init(args):
    campaign, _digest = read(args.source)
    Collection.initialize(args.db, campaign)
    _emit({'created': str(args.db)})
    return 0


def _claim(args):
    report = _retry(lambda: Collection(args.db).claim(args.worker, args.now))
    _emit(report)
    return 0


def _submit(args):
    answer, _digest = read(args.source)
    report = _retry(lambda: Collection(args.db).submit(
        args.worker, args.lease, args.now, answer))
    _emit(report)
    return 0


def _status(args):
    report = Collection(args.db).status()
    _emit(report)
    return 0


def _export(args):
    report = Collection(args.db).export()
    # Reports may never overwrite the database or its SQLite auxiliary files.
    output = Path(args.output)
    db = Path(args.db)
    forbidden = {db} | {Path(str(db) + suffix)
                        for suffix in ('-wal', '-shm', '-journal')}
    if output.resolve() in {candidate.resolve() for candidate in forbidden}:
        raise CollectionError(
            'export output must not overwrite the database'
            ' or its SQLite auxiliary files')
    save(output, report)
    _emit(report)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='raster_objects.collection_cli',
        description='Persistent blind double-annotation collection pipeline',
    )
    sub = parser.add_subparsers(dest='command', required=True)

    p_init = sub.add_parser('init', help='create a new collection database')
    p_init.add_argument('--db', required=True)
    p_init.add_argument('--source', required=True)
    p_init.set_defaults(handler=_init)

    p_claim = sub.add_parser('claim', help='claim one task for a worker')
    p_claim.add_argument('--db', required=True)
    p_claim.add_argument('--worker', required=True)
    p_claim.add_argument('--now', required=True, type=int)
    p_claim.set_defaults(handler=_claim)

    p_submit = sub.add_parser('submit', help='submit one leased answer')
    p_submit.add_argument('--db', required=True)
    p_submit.add_argument('--worker', required=True)
    p_submit.add_argument('--lease', required=True)
    p_submit.add_argument('--now', required=True, type=int)
    p_submit.add_argument('--source', required=True)
    p_submit.set_defaults(handler=_submit)

    p_status = sub.add_parser('status', help='snapshot status of the campaign')
    p_status.add_argument('--db', required=True)
    p_status.set_defaults(handler=_status)

    p_export = sub.add_parser('export', help='publish the delivered dataset')
    p_export.add_argument('--db', required=True)
    p_export.add_argument('--output', required=True)
    p_export.set_defaults(handler=_export)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except CollectionBusy as error:
        print(json.dumps({'busy': str(error), 'retryable': True},
                         sort_keys=True), file=sys.stderr)
        return 4
    except (CollectionError, OSError, ValueError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
