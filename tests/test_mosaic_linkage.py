"""Linkage and concurrency acceptance tests for the mosaic warehouse.

Scenario from the specification: a low-priority 0, a high-priority 1 and
another low-priority 1 cover the same cell.  Withdrawing the high-priority
source must turn the resolved cell back into a low-priority conflict with
updated witnesses and a null result; withdrawing one conflict party removes
the conflict and restores correct geometry.

Concurrency is exercised with two OS processes and file-based barriers so a
reader holds an open snapshot while a writer commits a withdrawal: the
returned report must describe exactly one complete catalog state, and a
fresh query after restart must match the post-withdrawal state.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from raster_objects.mosaic import Mosaic, MosaicBusy

from mosaic_oracle import OracleMosaic

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))

HOLD_DRIVER = r'''
import json, sys, time
from pathlib import Path
from raster_objects.mosaic import Mosaic

db, row, col, h, w, k, barrier = sys.argv[1:8]
row, col, h, w, k = map(int, (row, col, h, w, k))
barrier = Path(barrier)

def observer(event, *args):
    if event == "commit":
        (barrier / "started").touch()
        deadline = time.time() + 30
        while not (barrier / "release").exists():
            if time.time() > deadline:
                raise RuntimeError("barrier timeout")
            time.sleep(0.005)

report = Mosaic(db).window(row, col, h, w, k, observer=observer)
(barrier / "report.json").write_text(json.dumps(report, sort_keys=True))
'''

WITHDRAW_DRIVER = r'''
import sys, time, json
from pathlib import Path
from raster_objects.mosaic import Mosaic

db, tile_id, barrier = sys.argv[1:4]
barrier = Path(barrier)
deadline = time.time() + 30
while not (barrier / "started").exists():
    if time.time() > deadline:
        raise RuntimeError("barrier timeout")
    time.sleep(0.005)
receipt = Mosaic(db).withdraw(tile_id)
(barrier / "withdraw.json").write_text(json.dumps(receipt, sort_keys=True))
(barrier / "done").touch()
'''


def tile(tile_id, row, col, priority, grid):
    return {'tile_id': tile_id, 'row': row, 'col': col,
            'priority': priority, 'grid': grid}


class WithdrawReexposure(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        self.db = self.td / 'm.db'
        self.mosaic = Mosaic.initialize(self.db)
        self.oracle = OracleMosaic()
        # Same cell: low 0 (lo0), high 1 (hi1), another low 1 (lo1).
        self.payloads = [
            tile('lo0', 0, 0, 1, [[0]]),
            tile('hi1', 0, 0, 5, [[1]]),
            tile('lo1', 0, 0, 1, [[1]]),
        ]
        for payload in self.payloads:
            self.mosaic.put(payload)
            self.oracle.put(payload)

    def tearDown(self):
        self.temp.cleanup()

    def _expected(self, *withdrawals):
        oracle = OracleMosaic()
        for payload in self.payloads:
            oracle.put(payload)
        for tid in withdrawals:
            oracle.withdraw(tid)
        return oracle.window(0, 0, 1, 1, 4)

    def test_resolved_then_low_priority_conflict_then_geometry_restored(self):
        # Initially the high-priority 1 wins; low contributions are retained.
        before = self.mosaic.window(0, 0, 1, 1, 4)
        self.assertEqual(before['states'], [['resolved']])
        self.assertEqual(before['values'], [[1]])
        self.assertEqual(before['provenance'], [[{
            'priority': 5, 'top_count': 1,
            'zero_witness': None, 'one_witness': 'hi1'}]])
        self.assertEqual(before['result']['foreground_cells'], 1)
        self.assertEqual(before['catalog_seq'], 3)

        # Withdraw the winner: the two low-priority known values re-emerge as
        # a conflict; witnesses change and result becomes null.
        receipt = self.mosaic.withdraw('hi1')
        self.assertEqual(receipt['catalog_seq'], 4)
        self.oracle.withdraw('hi1')
        middle = self.mosaic.window(0, 0, 1, 1, 4)
        self.assertEqual(middle, self._expected('hi1'))
        self.assertEqual(middle['states'], [['conflict']])
        self.assertEqual(middle['values'], [[None]])
        self.assertEqual(middle['provenance'], [[{
            'priority': 1, 'top_count': 2,
            'zero_witness': 'lo0', 'one_witness': 'lo1'}]])
        self.assertIsNone(middle['result'])
        # Re-putting the withdrawn high tile must not resurrect it.
        self.assertFalse(self.mosaic.put(tile('hi1', 0, 0, 5, [[1]]))['active'])
        still_conflict = self.mosaic.window(0, 0, 1, 1, 4)
        self.assertEqual(still_conflict['states'], [['conflict']])

        # Withdraw the low-priority 1 party: the conflict disappears and the
        # surviving 0 is the resolved geometry again.
        self.mosaic.withdraw('lo1')
        self.oracle.withdraw('lo1')
        after = self.mosaic.window(0, 0, 1, 1, 4)
        self.assertEqual(after, self._expected('hi1', 'lo1'))
        self.assertEqual(after['states'], [['resolved']])
        self.assertEqual(after['values'], [[0]])
        self.assertEqual(after['provenance'], [[{
            'priority': 1, 'top_count': 1,
            'zero_witness': 'lo0', 'one_witness': None}]])
        self.assertEqual(after['result']['foreground_cells'], 0)
        self.assertEqual(after['result']['background_cells'], 1)
        # The high tile's cells remain stored even though it is inactive.
        import sqlite3
        with sqlite3.connect(self.db) as conn:
            stored = conn.execute(
                'SELECT value FROM cells WHERE tile_id=?', ('hi1',)).fetchall()
            self.assertEqual(stored, [(1,)])
            self.assertEqual(
                conn.execute('SELECT active FROM sources WHERE tile_id=?',
                             ('hi1',)).fetchone()[0], 0)

    def test_full_window_geometry_after_partial_withdrawal(self):
        # Extend the cell into a small window so object geometry is exercised.
        extra = tile('shape', 0, 1, 1, [[1, 1], [1, 0]])
        self.mosaic.put(extra)
        self.oracle.put(extra)
        self.mosaic.withdraw('hi1')
        self.oracle.withdraw('hi1')
        actual = self.mosaic.window(0, 0, 2, 3, 4)
        expected = self.oracle.window(0, 0, 2, 3, 4)
        self.assertIsNone(actual['result'])       # conflict at (0,0)
        self.assertEqual(actual, expected)
        self.mosaic.withdraw('lo1')
        self.oracle.withdraw('lo1')
        actual = self.mosaic.window(0, 0, 2, 3, 4)
        expected = self.oracle.window(0, 0, 2, 3, 4)
        self.assertIsNotNone(actual['result'])
        self.assertEqual(actual, expected)


class SnapshotIsolation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        self.db = self.td / 'm.db'
        self.mosaic = Mosaic.initialize(self.db)
        self.oracle = OracleMosaic()
        self.payloads = [
            tile('lo0', 0, 0, 1, [[0, 1], [1, 0]]),
            tile('hi1', 0, 0, 5, [[1, None], [None, 1]]),
            tile('lo1', 0, 0, 1, [[1, 0], [0, 1]]),
        ]
        for payload in self.payloads:
            self.mosaic.put(payload)
            self.oracle.put(payload)

    def tearDown(self):
        self.temp.cleanup()

    def _oracle_state(self, withdrawn):
        oracle = OracleMosaic()
        for payload in self.payloads:
            oracle.put(payload)
        for tid in withdrawn:
            oracle.withdraw(tid)
        return oracle.window(0, 0, 2, 2, 4)

    def _start(self, script, *args):
        path = self.td / (script.__name__ if hasattr(script, '__name__') else 'driver.py')
        path.write_text(script)
        return subprocess.Popen(
            [sys.executable, '-P', str(path), *map(str, args)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV, cwd=ROOT)

    def test_reader_holding_snapshot_is_not_mixed_with_withdrawal(self):
        pre = self._oracle_state(())
        post = self._oracle_state(('hi1',))
        barrier = self.td / 'barrier'
        barrier.mkdir()

        reader = self._start(
            HOLD_DRIVER, self.db, 0, 0, 2, 2, 4, barrier)
        # Wait until the reader has gathered every contribution and is parked
        # inside its open read transaction, just before COMMIT.
        deadline = time.time() + 30
        while not (barrier / 'started').exists():
            self.assertIsNone(reader.poll())
            if time.time() > deadline:
                self.fail('reader never reached barrier')
            time.sleep(0.01)

        writer = self._start(WITHDRAW_DRIVER, self.db, 'hi1', barrier)
        w_out, w_err = writer.communicate(timeout=30)
        self.assertEqual(writer.returncode, 0, w_err.decode())
        self.assertTrue((barrier / 'done').exists())
        # The withdrawal is now committed while the reader still holds its
        # pre-withdrawal WAL snapshot.
        (barrier / 'release').touch()
        r_out, r_err = reader.communicate(timeout=30)
        self.assertEqual(reader.returncode, 0, r_err.decode())

        held = json.loads((barrier / 'report.json').read_text())
        self.assertEqual(held, pre)
        self.assertEqual(held['catalog_seq'], pre['catalog_seq'])
        self.assertNotEqual(pre['catalog_seq'], post['catalog_seq'])

        # A fresh process after the barrier observes exactly the post state.
        fresh = subprocess.run(
            [sys.executable, '-P', '-c',
             'import json,sys; from raster_objects.mosaic import Mosaic;'
             'print(json.dumps(Mosaic(sys.argv[1]).window(0,0,2,2,4), sort_keys=True))',
             str(self.db)],
            capture_output=True, text=True, env=ENV, cwd=ROOT)
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        self.assertEqual(json.loads(fresh.stdout), post)

        # Restarting (new handle) keeps returning the same consistent state.
        self.assertEqual(Mosaic(self.db).window(0, 0, 2, 2, 4), post)

    def test_concurrent_readers_each_match_one_complete_state(self):
        pre = self._oracle_state(())
        post = self._oracle_state(('hi1',))
        reports = []
        lock = threading.Lock()

        def reader():
            for _ in range(50):
                report = Mosaic(self.db).window(0, 0, 2, 2, 4)
                with lock:
                    reports.append(report)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for thread in threads:
            thread.start()
        time.sleep(0.05)
        self.mosaic.withdraw('hi1')
        for thread in threads:
            thread.join()
        self.assertTrue(reports)
        for report in reports:
            # Every report is wholly one state or the other: no mixed
            # post-state geometry with a pre-state sequence, etc.
            self.assertIn(report, (pre, post))
        self.assertIn(pre, reports)
        self.assertIn(post, reports)

    def test_concurrent_writers_serialize_with_side_effect_free_busy(self):
        # Two distinct withdrawals race; exactly one consumes a sequence and
        # the loser either retries cleanly or reports a busy error.
        outcomes = []

        def withdraw(tid):
            for _ in range(100):
                try:
                    outcomes.append(('ok', tid, Mosaic(self.db).withdraw(tid)))
                    return
                except MosaicBusy:
                    time.sleep(0.005)
            outcomes.append(('busy-exhausted', tid, None))

        t1 = threading.Thread(target=withdraw, args=('hi1',))
        t2 = threading.Thread(target=withdraw, args=('lo1',))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertTrue(all(kind == 'ok' for kind, _t, _r in outcomes))
        seqs = sorted(receipt['catalog_seq'] for _k, _t, receipt in outcomes)
        self.assertEqual(seqs, [4, 5])
        final = self.mosaic.window(0, 0, 2, 2, 4)
        self.oracle.withdraw('hi1')
        self.oracle.withdraw('lo1')
        self.assertEqual(final, self.oracle.window(0, 0, 2, 2, 4))


if __name__ == '__main__':
    unittest.main()
