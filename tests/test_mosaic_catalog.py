"""Catalog acceptance tests for the mosaic warehouse.

Covers restart persistence, normalized idempotent duplicates, withdrawal
semantics (no resurrection, repeated no-op, unknown id error), different
content under a known id, bounds validation, the source/cell caps, source
file independence, pre-commit rollback and the init-only-if-absent rule.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from raster_objects.common import read
from raster_objects.mosaic import (
    Mosaic,
    MosaicError,
    TileConflict,
    UnknownTile,
    MAX_SOURCES,
    MAX_TOTAL_CELLS,
)

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))


def tile(tile_id, row, col, priority, grid):
    return {'tile_id': tile_id, 'row': row, 'col': col,
            'priority': priority, 'grid': grid}


def write_json(path, payload):
    Path(path).write_text(json.dumps(payload))


def run_cli(*args):
    return subprocess.run(
        [sys.executable, '-m', 'raster_objects.mosaic_cli', *map(str, args)],
        capture_output=True, text=True, env=ENV, cwd=ROOT,
    )


class CatalogBehavior(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        self.db = self.td / 'm.db'
        self.mosaic = Mosaic.initialize(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_init_only_creates_missing_database(self):
        with self.assertRaises(MosaicError):
            Mosaic.initialize(self.db)
        # A symlink target is refused as well.
        link = self.td / 'link.db'
        link.symlink_to(self.db)
        with self.assertRaises(MosaicError):
            Mosaic.initialize(link)
        # Opening an unrelated/empty file as a mosaic fails.
        other = self.td / 'other.db'
        other.write_text('not a database')
        with self.assertRaises((MosaicError, sqlite3.Error)):
            Mosaic(other)
        # A missing path must not be created as a stray empty database.
        missing = self.td / 'missing.db'
        with self.assertRaises(MosaicError):
            Mosaic(missing)
        self.assertFalse(missing.exists())

    def test_first_put_is_active_and_seq_starts_at_zero(self):
        receipt = self.mosaic.put(tile('a', 0, 0, 5, [[1]]))
        self.assertEqual(receipt, {
            'tile_id': 'a', 'registered': True, 'active': True,
            'catalog_seq': 1})
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(
                conn.execute("SELECT value FROM meta WHERE key='catalog_seq'").fetchone()[0],
                '1')
            self.assertEqual(
                conn.execute('SELECT active FROM sources WHERE tile_id=?', ('a',)).fetchone()[0],
                1)

    def test_restart_preserves_registration_and_state(self):
        self.mosaic.put(tile('a', -3, -7, -9, [[1, None], [0, 1]]))
        self.mosaic.withdraw('a')
        reopened = Mosaic(self.db)
        self.assertEqual(reopened.withdraw('a')['catalog_seq'], 2)
        # The withdrawn source contributes nothing anymore: the window is
        # uncovered, not unknown (unknown requires an active null opinion).
        report = reopened.window(-3, -7, 2, 2, 4)
        self.assertEqual(
            report['states'], [['uncovered'] * 2, ['uncovered'] * 2])
        self.assertEqual(report['catalog_seq'], 2)

    def test_same_content_reput_is_side_effect_free_even_when_withdrawn(self):
        payload = tile('a', 0, 0, 3, [[1, 0], [None, 1]])
        self.mosaic.put(payload)
        self.mosaic.withdraw('a')
        # Re-put identical content after withdrawal must not resurrect.
        duplicate = {k: payload[k] for k in ('priority', 'tile_id', 'grid', 'row', 'col')}
        receipt = self.mosaic.put(duplicate)
        self.assertEqual(receipt, {
            'tile_id': 'a', 'registered': False, 'active': False,
            'catalog_seq': 2})
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(
                conn.execute('SELECT active FROM sources WHERE tile_id=?', ('a',)).fetchone()[0],
                0)
            self.assertEqual(
                conn.execute("SELECT value FROM meta WHERE key='catalog_seq'").fetchone()[0],
                '2')
        # A still-active duplicate is also a no-op.
        self.mosaic.put(tile('b', 5, 5, 1, [[0]]))
        again = self.mosaic.put(tile('b', 5, 5, 1, [[0]]))
        self.assertFalse(again['registered'])
        self.assertTrue(again['active'])
        self.assertEqual(again['catalog_seq'], 3)

    def test_different_content_same_id_is_rejected_without_side_effects(self):
        self.mosaic.put(tile('a', 0, 0, 1, [[1]]))
        variants = [
            tile('a', 0, 0, 1, [[0]]),            # different grid value
            tile('a', 0, 0, 2, [[1]]),            # different priority
            tile('a', 1, 0, 1, [[1]]),            # different origin
            tile('a', 0, 0, 1, [[1, 1]]),         # different shape
        ]
        for bad in variants:
            with self.assertRaises(TileConflict):
                self.mosaic.put(bad)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(
                conn.execute("SELECT value FROM meta WHERE key='catalog_seq'").fetchone()[0],
                '1')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sources').fetchone()[0], 1)
        # The original registration is unchanged and still active.
        receipt = self.mosaic.put(tile('a', 0, 0, 1, [[1]]))
        self.assertTrue(receipt['active'])
        self.assertFalse(receipt['registered'])

    def test_withdraw_lifecycle(self):
        self.mosaic.put(tile('a', 0, 0, 1, [[1]]))
        self.assertEqual(self.mosaic.withdraw('a')['catalog_seq'], 2)
        # Repeated withdrawal is a no-op and does not consume a sequence.
        again = self.mosaic.withdraw('a')
        self.assertFalse(again['withdrawn'])
        self.assertEqual(again['catalog_seq'], 2)
        with self.assertRaises(UnknownTile):
            self.mosaic.withdraw('ghost')
        for bad in ('', '  ', 'x' * 129):
            with self.assertRaises(MosaicError):
                self.mosaic.withdraw(bad)

    def test_original_file_changes_do_not_affect_stored_tile(self):
        source = self.td / 't.json'
        write_json(source, tile('a', 0, 0, 1, [[1, 0]]))
        payload, _digest = read(source)
        self.mosaic.put(payload)
        source.unlink()
        report = self.mosaic.window(0, 0, 1, 2, 4)
        self.assertEqual(report['values'], [[1, 0]])
        write_json(source, tile('a', 0, 0, 1, [[0, 0]]))
        report = self.mosaic.window(0, 0, 1, 2, 4)
        self.assertEqual(report['values'], [[1, 0]])

    def test_input_validation_matrix(self):
        def bad(payload):
            with self.assertRaises(MosaicError):
                self.mosaic.put(payload)

        bad({'tile_id': 'a', 'row': 0, 'col': 0, 'priority': 1})  # missing grid
        bad(dict(tile('a', 0, 0, 1, [[1]]), extra=1))             # extra field
        bad(tile('', 0, 0, 1, [[1]]))
        bad(tile(' a', 0, 0, 1, [[1]]))
        for field in ('row', 'col', 'priority'):
            base = tile('z', 0, 0, 1, [[1]])
            base[field] = True
            bad(base)
            base[field] = 10**9 + 1
            bad(base)
            base[field] = -10**9 - 1
            bad(base)
        bad(tile('z', '0', 0, 1, [[1]]))
        # Grid rules reused from the raster contract.
        bad(tile('z', 0, 0, 1, []))
        bad(tile('z', 0, 0, 1, [[]]))
        bad(tile('z', 0, 0, 1, [[True]]))
        bad(tile('z', 0, 0, 1, [[2]]))
        bad(tile('z', 0, 0, 1, [[1], [0, 1]]))                    # ragged
        bad(tile('z', 0, 0, 1, [[1] * 501]))                      # side too long
        big = [[1] * 317 for _ in range(316)]                     # > 100000 cells
        self.assertEqual(len(big) * len(big[0]), 316 * 317)
        bad(tile('z', 0, 0, 1, big))
        # Global coordinates must stay in range at every corner.
        bad(tile('z', 10**9, 0, 1, [[1], [1]]))          # max row 1e9+1
        bad(tile('z', 0, 10**9, 1, [[1, 1]]))             # max col 1e9+1
        # Rejected puts consume neither a source slot nor a sequence.
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sources').fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT value FROM meta WHERE key='catalog_seq'").fetchone()[0],
                '0')

    def test_boundary_coordinates_are_accepted(self):
        edge = 10**9 - 1
        self.mosaic.put(tile('lo', -10**9, -10**9, 0, [[1, 1], [1, 1]]))
        self.mosaic.put(tile('hi', edge, edge, 0, [[1, 1], [1, 1]]))
        report = self.mosaic.window(edge, edge, 2, 2, 4)
        self.assertEqual(report['state_counts']['resolved'], 4)

    def test_source_and_cell_caps_fail_the_whole_put(self):
        # Twenty thousand single-cell transactions exercise the real commit
        # path; on overlay filesystems each WAL fsync dominates, so use a
        # tmpfs-backed temp directory when the environment provides one.
        import tempfile as _tempfile
        fast_root = '/dev/shm' if os.path.isdir('/dev/shm') else None
        with _tempfile.TemporaryDirectory(dir=fast_root) as fast_td:
            fast_db = Path(fast_td) / 'caps.db'
            mosaic = Mosaic.initialize(fast_db)
            for i in range(MAX_SOURCES):
                mosaic.put(tile('s%05d' % i, i * 10, 0, 0, [[1]]))
            with self.assertRaises(MosaicError):
                mosaic.put(tile('overflow', 0, 0, 0, [[1]]))
            with sqlite3.connect(fast_db) as conn:
                self.assertEqual(
                    conn.execute('SELECT COUNT(*) FROM sources').fetchone()[0], MAX_SOURCES)
                self.assertEqual(
                    conn.execute('SELECT COUNT(*) FROM cells').fetchone()[0], MAX_SOURCES)
                self.assertEqual(
                    conn.execute("SELECT value FROM meta WHERE key='catalog_seq'").fetchone()[0],
                    str(MAX_SOURCES))

        # Cell cap in a fresh warehouse: cumulative cells include withdrawn
        # sources, and an over-limit put rolls back entirely.  Large tiles
        # (each 100k cells, the per-tile maximum) keep the number of
        # transactions small while driving the cumulative counter directly.
        db2 = self.td / 'cap.db'
        mosaic = Mosaic.initialize(db2)
        slab = [[1] * 500 for _ in range(200)]             # 100000 cells
        for i in range(49):
            mosaic.put(tile('slab%02d' % i, 5000 + i * 300, 0, 0, slab))
        mosaic.withdraw('slab00')                           # cells still count
        with sqlite3.connect(db2) as conn:
            used = conn.execute(
                'SELECT COALESCE(SUM(height*width),0) FROM sources').fetchone()[0]
        self.assertEqual(used, 4_900_000)
        # Two rectangular tiles (all sides <= 500) reach exactly 4,999,999.
        mosaic.put(tile('tail-big', 0, 0, 0, [[1] * 333 for _ in range(300)]))
        mosaic.put(tile('tail-small', 100, 100, 0, [[1] * 11 for _ in range(9)]))
        with sqlite3.connect(db2) as conn:
            used = conn.execute(
                'SELECT COALESCE(SUM(height*width),0) FROM sources').fetchone()[0]
        self.assertEqual(used, MAX_TOTAL_CELLS - 1)
        with self.assertRaises(MosaicError):
            mosaic.put(tile('toomuch', 100, 100, 0, [[1, 1]]))
        with sqlite3.connect(db2) as conn:
            self.assertIsNone(
                conn.execute('SELECT 1 FROM sources WHERE tile_id=?', ('toomuch',)).fetchone())
            self.assertEqual(
                conn.execute('SELECT COALESCE(SUM(height*width),0) FROM sources').fetchone()[0],
                MAX_TOTAL_CELLS - 1)

    def test_precommit_exception_rolls_back_catalog_index_and_sequence(self):
        calls = []

        def hook(kind, tile_id, seq):
            calls.append((kind, tile_id, seq))
            raise RuntimeError('injected before commit')

        mosaic = Mosaic(self.db, precommit_hook=hook)
        with self.assertRaises(RuntimeError):
            mosaic.put(tile('boom', 0, 0, 1, [[1, None], [0, 1]]))
        self.assertEqual(calls, [('put', 'boom', 1)])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sources').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tile_buckets').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM cells').fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT value FROM meta WHERE key='catalog_seq'").fetchone()[0],
                '0')

        # Same guarantee for withdrawal after a successful put.
        plain = Mosaic(self.db)
        plain.put(tile('boom', 0, 0, 1, [[1]]))
        hooked = Mosaic(self.db, precommit_hook=hook)
        with self.assertRaises(RuntimeError):
            hooked.withdraw('boom')
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(
                conn.execute('SELECT active FROM sources WHERE tile_id=?', ('boom',)).fetchone()[0],
                1)
            self.assertEqual(
                conn.execute("SELECT value FROM meta WHERE key='catalog_seq'").fetchone()[0],
                '1')
        # Retry after rollback works and consumes the next sequence.
        self.assertEqual(plain.withdraw('boom')['catalog_seq'], 2)

    def test_cli_end_to_end_and_safety_boundaries(self):
        tile_path = self.td / 't.json'
        write_json(tile_path, tile('a', 0, 0, 1, [[1, 0], [None, 1]]))
        self.assertEqual(run_cli('init', '--db', self.td / 'fresh.db').returncode, 0)
        # Re-init is an error and leaves the existing database intact.
        again = run_cli('init', '--db', self.td / 'fresh.db')
        self.assertEqual(again.returncode, 2)
        ok = run_cli('put', '--db', self.td / 'fresh.db', '--source', tile_path)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)['catalog_seq'], 1)
        # Duplicate keys and oversized inputs follow the shared JSON contract.
        dup = self.td / 'dup.json'
        dup.write_text('{"tile_id":"a","tile_id":"b","row":0,"col":0,'
                       '"priority":1,"grid":[[1]]}')
        bad = run_cli('put', '--db', self.td / 'fresh.db', '--source', dup)
        self.assertEqual(bad.returncode, 2)
        huge = self.td / 'huge.json'
        huge.write_bytes(b'{"tile_id":"x","row":0,"col":0,"priority":1,'
                         b'"grid":[[1]],' + b'"pad":"' + b'x' * (8 * 1024 * 1024) + b'"}')
        self.assertEqual(
            run_cli('put', '--db', self.td / 'fresh.db', '--source', huge).returncode, 2)
        # Window output may not overwrite the database or its -wal/-shm files.
        for suffix in ('', '-wal', '-shm'):
            target = Path(str(self.td / 'fresh.db') + suffix)
            blocked = run_cli('window', '--db', self.td / 'fresh.db',
                              '--row', 0, '--col', 0, '--height', 2, '--width', 2,
                              '--connectivity', 4, '--output', target)
            self.assertEqual(blocked.returncode, 2, blocked.stderr)
        out = self.td / 'report.json'
        win = run_cli('window', '--db', self.td / 'fresh.db',
                      '--row', 0, '--col', 0, '--height', 2, '--width', 2,
                      '--connectivity', 4, '--output', out)
        self.assertEqual(win.returncode, 0, win.stderr)
        saved = json.loads(out.read_text())
        self.assertEqual(json.loads(win.stdout), saved)
        # A failed window preserves the previous report.
        before = out.read_text()
        fail = run_cli('window', '--db', self.td / 'fresh.db',
                       '--row', 0, '--col', 0, '--height', 0, '--width', 2,
                       '--connectivity', 4, '--output', out)
        self.assertEqual(fail.returncode, 2)
        self.assertEqual(out.read_text(), before)
        # Unknown withdrawal exits 2 and leaves the catalog untouched.
        self.assertEqual(run_cli('withdraw', '--db', self.td / 'fresh.db',
                                 '--tile-id', 'ghost').returncode, 2)


if __name__ == '__main__':
    unittest.main()
