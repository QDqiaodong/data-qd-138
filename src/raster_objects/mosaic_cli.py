"""Command line for the activity-aware multi-source mosaic warehouse.

Subcommands::

    python -m raster_objects.mosaic_cli init     --db DB
    python -m raster_objects.mosaic_cli put      --db DB --source TILE
    python -m raster_objects.mosaic_cli withdraw --db DB --tile-id ID
    python -m raster_objects.mosaic_cli window   --db DB --row R --col C
                                                 --height H --width W
                                                 --connectivity K --output JSON

Exit codes: 0 success, 2 input/protocol error, 4 retryable busy error from a
concurrent writer (the failed call had no side effects).
"""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

from .common import read, save
from .mosaic import Mosaic, MosaicBusy, MosaicError

_BUSY_ATTEMPTS = 20
_BUSY_DELAY = 0.05


def _retry(action):
    delay = _BUSY_DELAY
    for attempt in range(_BUSY_ATTEMPTS):
        try:
            return action()
        except MosaicBusy:
            if attempt == _BUSY_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 1.5, 0.5)


def _emit(report):
    print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))


def _init(args):
    Mosaic.initialize(args.db)
    _emit({'created': str(args.db), 'catalog_seq': 0})
    return 0


def _put(args):
    payload, _digest = read(args.source)
    receipt = _retry(lambda: Mosaic(args.db).put(payload))
    _emit(receipt)
    return 0


def _withdraw(args):
    receipt = _retry(lambda: Mosaic(args.db).withdraw(args.tile_id))
    _emit(receipt)
    return 0


def _window(args):
    report = _retry(lambda: Mosaic(args.db).window(
        args.row, args.col, args.height, args.width, args.connectivity))
    # The report must never be published over the database or its SQLite
    # auxiliary files (-wal/-shm/-journal).
    output = Path(args.output)
    db = Path(args.db)
    forbidden = {db} | {Path(str(db) + suffix)
                        for suffix in ('-wal', '-shm', '-journal')}
    if output.resolve() in {candidate.resolve() for candidate in forbidden}:
        raise MosaicError('window output must not overwrite the database'
                          ' or its SQLite auxiliary files')
    save(output, report)
    _emit(report)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='raster_objects.mosaic_cli',
        description='Activity-aware multi-source raster mosaic warehouse',
    )
    sub = parser.add_subparsers(dest='command', required=True)

    p_init = sub.add_parser('init', help='create a new mosaic warehouse')
    p_init.add_argument('--db', required=True)
    p_init.set_defaults(handler=_init)

    p_put = sub.add_parser('put', help='register one source tile')
    p_put.add_argument('--db', required=True)
    p_put.add_argument('--source', required=True)
    p_put.set_defaults(handler=_put)

    p_withdraw = sub.add_parser('withdraw', help='deactivate one source tile')
    p_withdraw.add_argument('--db', required=True)
    p_withdraw.add_argument('--tile-id', required=True)
    p_withdraw.set_defaults(handler=_withdraw)

    p_window = sub.add_parser('window', help='fused snapshot of a spatial window')
    p_window.add_argument('--db', required=True)
    p_window.add_argument('--row', required=True, type=int)
    p_window.add_argument('--col', required=True, type=int)
    p_window.add_argument('--height', required=True, type=int)
    p_window.add_argument('--width', required=True, type=int)
    p_window.add_argument('--connectivity', required=True, type=int)
    p_window.add_argument('--output', required=True)
    p_window.set_defaults(handler=_window)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except MosaicBusy as error:
        print(json.dumps({'busy': str(error), 'retryable': True},
                         sort_keys=True), file=sys.stderr)
        return 4
    except (MosaicError, sqlite3.Error, OSError, ValueError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
