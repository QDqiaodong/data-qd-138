"""Command line for the persistent raster correction ledger.

Subcommands::

    python -m raster_objects.ledger_cli init   --db DB --source JSON
    python -m raster_objects.ledger_cli apply  --db DB --source JSON
    python -m raster_objects.ledger_cli export --db DB --version N --output JSON

Exit codes: 0 success, 2 input/protocol error, 3 stale-baseline conflict,
4 retryable busy error from a concurrent writer.
"""
import argparse
import json
import sys
import time

from .common import read, save
from .ledger import (
    Ledger,
    LedgerBusy,
    LedgerError,
    RequestConflict,
)

# Bounded retry budget for short write races; callers that need fail-fast
# behavior use the Python API directly with timeout-zero semantics.
_BUSY_ATTEMPTS = 20
_BUSY_DELAY = 0.05


def _load_payload(source):
    payload, _digest = read(source)
    return payload


def _init(args):
    Ledger.initialize(args.db, args.source)
    print(json.dumps({'version': 0, 'parent_version': None}, sort_keys=True))
    return 0


def _apply(args):
    payload = _load_payload(args.source)
    ledger = Ledger(args.db)
    delay = _BUSY_DELAY
    for attempt in range(_BUSY_ATTEMPTS):
        try:
            receipt = ledger.apply(payload)
            print(json.dumps(receipt, sort_keys=True, ensure_ascii=False, allow_nan=False))
            return 0
        except LedgerBusy:
            if attempt == _BUSY_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 1.5, 0.5)


def _export(args):
    report = Ledger(args.db).export(args.version)
    save(args.output, report)
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='raster_objects.ledger_cli',
        description='Persistent raster correction and object-identity ledger',
    )
    sub = parser.add_subparsers(dest='command', required=True)

    p_init = sub.add_parser('init', help='create a new ledger at version 0')
    p_init.add_argument('--db', required=True)
    p_init.add_argument('--source', required=True)
    p_init.set_defaults(handler=_init)

    p_apply = sub.add_parser('apply', help='commit one correction request')
    p_apply.add_argument('--db', required=True)
    p_apply.add_argument('--source', required=True)
    p_apply.set_defaults(handler=_apply)

    p_export = sub.add_parser('export', help='publish one committed version')
    p_export.add_argument('--db', required=True)
    p_export.add_argument('--version', required=True, type=int)
    p_export.add_argument('--output', required=True)
    p_export.set_defaults(handler=_export)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except RequestConflict as error:
        print(json.dumps({'conflicts': error.conflicts}, sort_keys=True), file=sys.stderr)
        return 3
    except LedgerBusy as error:
        print(json.dumps({'busy': str(error), 'retryable': True}, sort_keys=True), file=sys.stderr)
        return 4
    except (LedgerError, OSError, ValueError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
