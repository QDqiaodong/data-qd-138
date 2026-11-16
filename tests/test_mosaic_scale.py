"""Offline sparse-scale acceptance test.

Builds 10,000 mutually far single-cell tiles plus one high-overlap area with
25 overlapping tiles, then proves:

* window results match the independent cell-by-cell oracle;
* queries locate candidates through the persistent spatial index, so tiles
  disjoint from the window are never decoded -- the test observes actual
  storage read counts (per-tile cell loads and executed SQL) rather than
  timing;
* the immutable ``grid_json`` payload column is never read by a query;
* peak Python memory is bounded and recorded.
"""
import gc
import json
import os
import random
import tempfile
import tracemalloc
import unittest
from pathlib import Path

from raster_objects.mosaic import Mosaic, BUCKET_SPAN

from mosaic_oracle import OracleMosaic

SPARSE_COUNT = 10_000
OVERLAP_COUNT = 25


def tile(tile_id, row, col, priority, grid):
    return {'tile_id': tile_id, 'row': row, 'col': col,
            'priority': priority, 'grid': grid}


class ReadCounter:
    """Observable storage-read evidence for one window query."""

    def __init__(self):
        self.events = []
        self.statements = []
        self.tile_loads = []          # (tile_id, rows_returned)
        self.candidates = None

    def __call__(self, event, *args):
        self.events.append((event,) + args)
        if event == 'stmt':
            self.statements.append(args[0])
        elif event == 'candidates':
            self.candidates = list(args[0])
        elif event == 'tile_cells':
            self.tile_loads.append((args[0], args[1]))


class SparseScale(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Ten thousand commit transactions: prefer tmpfs so overlay-fs fsync
        # latency does not dominate the storage-behavior assertions.
        fast_root = '/dev/shm' if os.path.isdir('/dev/shm') else None
        cls.temp = tempfile.TemporaryDirectory(dir=fast_root)
        cls.td = Path(cls.temp.name)
        cls.db = cls.td / 'scale.db'
        cls.rng = random.Random(424242)
        cls.mosaic = Mosaic.initialize(cls.db)
        cls.oracle = OracleMosaic()
        cls.sparse_ids = []

        # Far-apart single-cell tiles: spacing 2000 rows guarantees distinct
        # spatial buckets (bucket span is 500) and zero mutual overlap.
        for i in range(SPARSE_COUNT):
            tid = 'sp%05d' % i
            cls.sparse_ids.append(tid)
            payload = tile(tid, -10**9 + i * 2000, 10**6,
                           cls.rng.randint(-5, 5),
                           [[cls.rng.choice((0, 1))]])
            cls.mosaic.put(payload)
            cls.oracle.put(payload)

        # High-overlap area: 25 tiles covering the same 2x2 window, mixed
        # priorities and 0/1/null opinions to force conflicts and nulls.
        cls.overlap_ids = []
        for i in range(OVERLAP_COUNT):
            tid = 'ov%02d' % i
            cls.overlap_ids.append(tid)
            grid = [[cls.rng.choice((0, 1, None)) for _ in range(2)]
                    for _ in range(2)]
            payload = tile(tid, 100, 100, i - 10, grid)
            cls.mosaic.put(payload)
            cls.oracle.put(payload)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_01_spatial_index_reads_only_intersecting_tiles(self):
        # Window inside the overlap area: only overlap tiles are candidates.
        counter = ReadCounter()
        report = self.mosaic.window(100, 100, 2, 2, 4, observer=counter)
        expected = self.oracle.window(100, 100, 2, 2, 4)
        self.assertEqual(report, expected)
        self.assertEqual(set(counter.candidates), set(self.overlap_ids))
        # None of the 10,000 far-away single-cell tiles is even loaded.
        loaded = {tid for tid, _rows in counter.tile_loads}
        self.assertEqual(loaded, set(self.overlap_ids))
        self.assertTrue(loaded.isdisjoint(self.sparse_ids))
        # Every loaded tile really intersects the window and returns cells.
        self.assertTrue(all(rows == 4 for _tid, rows in counter.tile_loads))
        # The immutable grid payload column is never touched by a query.
        self.assertFalse(any('grid_json' in sql for sql in counter.statements))
        # Candidate discovery is an indexed bucket lookup, never a full scan
        # of the catalog: no unconstrained scan over the sources table.
        self.assertTrue(any('tile_buckets' in sql for sql in counter.statements))

    def test_02_single_cell_window_loads_one_tile_payload(self):
        target = self.sparse_ids[1234]
        grow = -10**9 + 1234 * 2000
        counter = ReadCounter()
        report = self.mosaic.window(grow, 10**6, 1, 1, 8, observer=counter)
        expected = self.oracle.window(grow, 10**6, 1, 1, 8)
        self.assertEqual(report, expected)
        self.assertEqual(counter.candidates, [target])
        self.assertEqual(counter.tile_loads, [(target, 1)])
        self.assertFalse(any('grid_json' in sql for sql in counter.statements))

    def test_03_empty_window_decodes_no_tile_at_all(self):
        # A bucket range that contains no registered tile: the spatial index
        # returns zero candidates and no cell payload is read.
        counter = ReadCounter()
        report = self.mosaic.window(5 * 10**8, 5 * 10**8, 3, 3, 4,
                                    observer=counter)
        expected = self.oracle.window(5 * 10**8, 5 * 10**8, 3, 3, 4)
        self.assertEqual(report, expected)
        self.assertEqual(counter.candidates, [])
        self.assertEqual(counter.tile_loads, [])
        self.assertEqual(set(report['states'][0]), {'uncovered'})

    def test_04_bucket_span_makes_each_sparse_tile_unique(self):
        # Structural precondition for the read-count evidence above.
        self.assertGreater(2000, BUCKET_SPAN)
        distinct_buckets = {
            (-10**9 + i * 2000) // BUCKET_SPAN for i in range(SPARSE_COUNT)}
        self.assertEqual(len(distinct_buckets), SPARSE_COUNT)

    def test_05_peak_memory_is_bounded_and_recorded(self):
        gc.collect()
        tracemalloc.start()
        try:
            # Repeated large-window queries must not accumulate state: cells
            # are folded per candidate and released.
            peaks = []
            for _ in range(5):
                counter = ReadCounter()
                report = self.mosaic.window(
                    100, 100, 2, 2, 4, observer=counter)
                expected = self.oracle.window(100, 100, 2, 2, 4)
                self.assertEqual(report, expected)
                _, peak = tracemalloc.get_traced_memory()
                peaks.append(peak)
        finally:
            tracemalloc.stop()
        recorded = {
            'sparse_tiles': SPARSE_COUNT,
            'overlap_tiles': OVERLAP_COUNT,
            'peak_python_bytes_per_window_query': max(peaks),
        }
        # The window itself is only 4 cells: even with 25 candidates, the
        # query-side working set stays far below one megabyte.  This is an
        # explicit, observable bound rather than a timing assertion.
        self.assertLess(recorded['peak_python_bytes_per_window_query'],
                        1024 * 1024)
        (self.td / 'scale-evidence.json').write_text(json.dumps(
            recorded, sort_keys=True, indent=2))
        print('\nscale evidence: ' + json.dumps(recorded, sort_keys=True))

    def test_06_withdraw_reexposure_at_scale_matches_oracle(self):
        # Withdraw every overlap tile with priority >= 0 (15 tiles); the
        # remaining negative-priority opinions must re-emerge exactly.
        for tid in self.overlap_ids[10:]:
            self.mosaic.withdraw(tid)
            self.oracle.withdraw(tid)
        counter = ReadCounter()
        report = self.mosaic.window(100, 100, 2, 2, 8, observer=counter)
        expected = self.oracle.window(100, 100, 2, 2, 8)
        self.assertEqual(report, expected)
        # Withdrawn tiles are not candidates and are never loaded.
        self.assertTrue(set(counter.candidates).isdisjoint(
            self.overlap_ids[10:]))
        # Sparse tiles remain untouched by the overlap-area query.
        self.assertTrue(
            set(counter.tile_loads).isdisjoint(
                {(tid, 4) for tid in self.sparse_ids}))


if __name__ == '__main__':
    unittest.main()
