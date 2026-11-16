"""Fusion acceptance tests against an independent cell-by-cell oracle.

The warehouse is fed tiles in varying arrival orders and queried over windows
with negative coordinates; every report is compared field by field with
:class:`tests.mosaic_oracle.OracleMosaic`, whose labeling is a separate BFS
implementation.  Covered semantics:

* same-priority conflicting values (conflict, witness tile ids, result=null);
* same-priority agreeing sources (top_count, minimum-id witnesses);
* high-priority nulls never suppressing lower-priority known values;
* uncovered vs only-null-covered (unknown);
* global-coordinate bbox and exact fraction-string centroids;
* window-clipped perimeters;
* more than 128 disconnected objects (the ledger's 128 cap does not apply).
"""
import random
import tempfile
import unittest
from itertools import permutations
from pathlib import Path

from raster_objects.mosaic import Mosaic

from mosaic_oracle import OracleMosaic


def tile(tile_id, row, col, priority, grid):
    return {'tile_id': tile_id, 'row': row, 'col': col,
            'priority': priority, 'grid': grid}


def assert_report_equal(testcase, actual, expected):
    for field in ('row', 'col', 'height', 'width', 'connectivity',
                  'catalog_seq', 'states', 'values', 'provenance',
                  'state_counts', 'result'):
        testcase.assertEqual(actual[field], expected[field],
                             'field %s differs\nactual=%s\nexpected=%s'
                             % (field, actual[field], expected[field]))


class FusionSemantics(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'm.db'

    def tearDown(self):
        self.temp.cleanup()

    def _build(self, tiles, order=None):
        mosaic = Mosaic.initialize(self.db)
        oracle = OracleMosaic()
        order = list(tiles) if order is None else order
        for payload in order:
            self.assertEqual(mosaic.put(payload)['catalog_seq'], oracle.put(payload))
        return mosaic, oracle

    def _check(self, mosaic, oracle, window):
        actual = mosaic.window(*window)
        expected = oracle.window(*window)
        assert_report_equal(self, actual, expected)
        return actual

    def test_arrival_order_independence(self):
        tiles = [
            tile('a', -2, -2, 1, [[1, 0], [None, 1]]),
            tile('b', -1, -2, 1, [[0, 1], [1, None]]),
            tile('c', -2, -1, 2, [[None, None], [None, 0]]),
            tile('d', -3, -3, 0, [[1, 1], [1, 1]]),
        ]
        window = (-3, -3, 4, 4, 4)
        first = None
        for order in permutations(tiles):
            db = Path(self.temp.name) / ('order-%s.db' % '-'.join(t['tile_id'] for t in order))
            mosaic = Mosaic.initialize(db)
            oracle = OracleMosaic()
            for payload in order:
                mosaic.put(payload)
                oracle.put(payload)
            report = mosaic.window(*window)
            if first is None:
                first = report
            assert_report_equal(self, report, first)
            assert_report_equal(self, report, oracle.window(*window))

    def test_same_priority_conflict_witnesses_and_null_result(self):
        tiles = [
            tile('zeta', 0, 0, 5, [[1, 1], [0, None]]),
            tile('alpha', 0, 0, 5, [[0, 1], [0, 1]]),
        ]
        mosaic, oracle = self._build(tiles)
        report = self._check(mosaic, oracle, (0, 0, 2, 2, 4))
        # (0,0): top-level 0 and 1 -> conflict, witnesses are string-ordered.
        cell = report['provenance'][0][0]
        self.assertEqual(report['states'][0][0], 'conflict')
        self.assertIsNone(report['values'][0][0])
        self.assertEqual(cell, {
            'priority': 5, 'top_count': 2,
            'zero_witness': 'alpha', 'one_witness': 'zeta'})
        self.assertIsNone(report['result'])
        self.assertEqual(report['state_counts']['conflict'], 1)
        # (0,1): both say 1 -> top_count 2, single witness is the min id.
        agree = report['provenance'][0][1]
        self.assertEqual(report['states'][0][1], 'resolved')
        self.assertEqual(agree, {
            'priority': 5, 'top_count': 2,
            'zero_witness': None, 'one_witness': 'alpha'})

    def test_same_priority_same_value_counts_and_string_order(self):
        tiles = [
            tile('m10', 0, 0, 3, [[1]]),
            tile('m2', 0, 0, 3, [[1]]),   # 'm10' < 'm2' in Python string order
            tile('m1', 0, 0, 3, [[1]]),
        ]
        mosaic, oracle = self._build(tiles)
        report = self._check(mosaic, oracle, (0, 0, 1, 1, 4))
        self.assertEqual(report['provenance'][0][0], {
            'priority': 3, 'top_count': 3,
            'zero_witness': None, 'one_witness': 'm1'})

    def test_high_priority_null_does_not_suppress_known_value(self):
        tiles = [
            tile('low', 0, 0, 1, [[1, 0], [0, 1]]),
            tile('high-null', 0, 0, 9, [[None, None], [None, None]]),
        ]
        mosaic, oracle = self._build(tiles)
        report = self._check(mosaic, oracle, (0, 0, 2, 2, 4))
        self.assertEqual(report['states'], [['resolved'] * 2, ['resolved'] * 2])
        self.assertEqual(report['values'], [[1, 0], [0, 1]])
        self.assertEqual(
            report['provenance'][0][0]['priority'], 1)

    def test_uncovered_vs_unknown_distinction(self):
        tiles = [
            # Covers only column 1, all nulls.
            tile('opinionless', 0, 1, 4, [[None], [None], [None]]),
            # Covers (2,0) with a known value.
            tile('solo', 2, 0, 2, [[1]]),
        ]
        mosaic, oracle = self._build(tiles)
        report = self._check(mosaic, oracle, (0, 0, 3, 2, 8))
        self.assertEqual(report['states'], [
            ['uncovered', 'unknown'],
            ['uncovered', 'unknown'],
            ['resolved', 'unknown'],
        ])
        self.assertEqual(report['state_counts'], {
            'uncovered': 2, 'unknown': 3, 'resolved': 1, 'conflict': 0})
        # No conflict, so result exists; unknown/uncovered both enter as null.
        self.assertEqual(report['result']['unknown_cells'], 5)
        self.assertEqual(report['result']['foreground_cells'], 1)
        self.assertEqual(report['result']['labels'], [
            [None, None], [None, None], [1, None]])

    def test_negative_coordinates_global_geometry_and_centroid(self):
        tiles = [
            tile('neg', -4, -4, 1, [
                [1, 1, 0],
                [1, 0, 0],
                [0, 0, 1],
            ]),
        ]
        mosaic, oracle = self._build(tiles)
        report = self._check(mosaic, oracle, (-4, -4, 3, 3, 4))
        objects = report['result']['objects']
        self.assertEqual(len(objects), 2)
        self.assertEqual(objects[0]['bbox'], [-4, -4, -3, -3])
        # Cells at global (-4,-4),(-4,-3),(-3,-4): centroid (-11/3, -11/3).
        self.assertEqual(objects[0]['centroid_row'], '-11/3')
        self.assertEqual(objects[0]['centroid_col'], '-11/3')
        self.assertEqual(objects[1]['bbox'], [-2, -2, -2, -2])
        self.assertEqual(objects[1]['centroid_row'], '-2')
        self.assertEqual(objects[1]['centroid_col'], '-2')

    def test_window_clipped_perimeter_matches_oracle(self):
        # A solid foreground block larger than the query window: edges on the
        # window boundary are exposed because neighbors outside are unknown.
        tiles = [tile('block', 0, 0, 1, [[1] * 10 for _ in range(10)])]
        mosaic, oracle = self._build(tiles)
        for connectivity in (4, 8):
            report = self._check(mosaic, oracle, (2, 3, 4, 5, connectivity))
            self.assertEqual(len(report['result']['objects']), 1)
            # 4x5 rectangle clipped on every side -> perimeter 2*(4+5)=18.
            self.assertEqual(report['result']['objects'][0]['perimeter_edges'], 18)
            self.assertEqual(report['result']['objects'][0]['bbox'], [2, 3, 5, 7])
            self.assertEqual(report['result']['objects'][0]['centroid_row'], '7/2')

    def test_more_than_128_disconnected_objects(self):
        # 150 isolated foreground cells, 4-connectivity, within a 500-side
        # 22500-cell window (the ledger's 128-object cap does not apply here).
        positions = [(3 * r, 3 * c) for r in range(10) for c in range(15)]
        self.assertEqual(len(positions), 150)
        grid = [[0] * 47 for _ in range(30)]
        for r, c in positions:
            grid[r][c] = 1
        mosaic, oracle = self._build([tile('many', 0, 0, 0, grid)])
        report = self._check(mosaic, oracle, (0, 0, 30, 47, 4))
        self.assertEqual(len(report['result']['objects']), 150)
        # Window-local row-major label numbering, not any cross-query identity.
        self.assertEqual([obj['id'] for obj in report['result']['objects']],
                         list(range(1, 151)))
        self.assertEqual(report['result']['foreground_cells'], 150)

    def test_randomized_oracle_comparison(self):
        rng = random.Random(20261116)
        mosaic = Mosaic.initialize(Path(self.temp.name) / 'rand.db')
        oracle = OracleMosaic()
        ids = []
        for i in range(24):
            tid = 't%02d' % i
            ids.append(tid)
            h = rng.randint(1, 6)
            w = rng.randint(1, 6)
            row = rng.randint(-8, 6)
            col = rng.randint(-8, 6)
            if row + h - 1 > 10**9 or col + w - 1 > 10**9:
                continue
            grid = [[rng.choice([0, 1, None, 1]) for _ in range(w)]
                    for _ in range(h)]
            payload = tile(tid, row, col, rng.randint(-3, 3), grid)
            mosaic.put(payload)
            oracle.put(payload)
        # Withdraw roughly a third of the sources at several checkpoints.
        for i, tid in enumerate(ids):
            if i % 3 == 0:
                mosaic.withdraw(tid)
                oracle.withdraw(tid)
            if i % 4 == 0:
                wr = rng.randint(-6, 2)
                wc = rng.randint(-6, 2)
                wh = rng.randint(1, 8)
                ww = rng.randint(1, 8)
                self._check(mosaic, oracle, (wr, wc, wh, ww,
                                             rng.choice((4, 8))))
        for _ in range(12):
            wr = rng.randint(-10, 4)
            wc = rng.randint(-10, 4)
            wh = rng.randint(1, 10)
            ww = rng.randint(1, 10)
            self._check(mosaic, oracle, (wr, wc, wh, ww, rng.choice((4, 8))))


if __name__ == '__main__':
    unittest.main()
