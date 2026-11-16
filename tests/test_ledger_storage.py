"""Storage-layer tests for the SQLite correction ledger.

These tests independently verify persistence and protocol behavior:
restart-safe history queries, stale-but-untouched success, the 1->0->1
touch-history conflict, normalized idempotent retries, request-id release on
failure, and the atomicity guarantees around pre-commit failures.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from raster_objects.ledger import (
    Ledger,
    LedgerBusy,
    LedgerError,
    RequestConflict,
    RequestIdConflict,
    UnknownVersion,
    normalize_request,
)

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, payload):
    Path(path).write_text(json.dumps(payload))


class StorageBehavior(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        self.source = self.td / 'source.json'
        self.source.write_text((ROOT / 'fixtures/input.json').read_text())
        self.db = self.td / 'ledger.db'
        self.ledger = Ledger.initialize(self.db, self.source)

    def tearDown(self):
        self.temp.cleanup()

    def request(self, request_id, base, changes):
        return {'request_id': request_id, 'base_version': base, 'changes': changes}

    def chg(self, row, col, before, after):
        return {'row': row, 'col': col, 'before': before, 'after': after}

    def test_init_refuses_existing_database(self):
        with self.assertRaises(LedgerError):
            Ledger.initialize(self.db, self.source)
        self.assertEqual(self.ledger.head_version(), 0)

    def test_version_zero_contract(self):
        report = self.ledger.export(0)
        self.assertEqual(report['version'], 0)
        self.assertIsNone(report['parent_version'])
        self.assertIsNone(report['receipt'])
        result = report['result']
        self.assertEqual([obj['id'] for obj in result['objects']], [1, 2, 3])
        self.assertEqual(result['objects'][0]['centroid_row'], '1/3')
        self.assertEqual((result['foreground_cells'], result['background_cells'],
                          result['unknown_cells']), (6, 5, 1))
        self.assertEqual(result['labels'][1][2], None)

    def test_restart_preserves_full_history(self):
        receipt1 = self.ledger.apply(
            self.request('r1', 0, [self.chg(2, 0, 1, 0)]))
        receipt2 = self.ledger.apply(
            self.request('r2', 1, [self.chg(0, 2, 0, 1)]))
        del self.ledger
        reopened = Ledger(self.db)
        self.assertEqual(reopened.head_version(), 2)
        v0 = reopened.export(0)
        v1 = reopened.export(1)
        v2 = reopened.export(2)
        self.assertEqual([o['id'] for o in v0['result']['objects']], [1, 2, 3])
        self.assertEqual(v1['receipt'], receipt1)
        self.assertEqual(v2['receipt'], receipt2)
        self.assertEqual(v1['parent_version'], 0)
        self.assertEqual(v2['parent_version'], 1)
        # Old versions must not leak later geometry or identifiers.
        self.assertNotEqual(
            [o['id'] for o in v1['result']['objects']],
            [o['id'] for o in v2['result']['objects']],
        )

    def test_stale_baseline_untouched_cells_succeeds(self):
        self.ledger.apply(self.request('r1', 0, [self.chg(2, 0, 1, 0)]))
        # Base 0 is now stale, but (0,2) was never touched; before matches the
        # base raster, so the request commits against the current head.
        receipt = self.ledger.apply(
            self.request('r2', 0, [self.chg(0, 2, 0, 1)]))
        self.assertEqual(receipt['version'], 2)
        self.assertEqual(receipt['parent_version'], 1)
        report = self.ledger.export(2)
        self.assertEqual(report['receipt'], receipt)

    def test_1_0_1_then_stale_write_still_conflicts(self):
        self.ledger.apply(self.request('a', 0, [self.chg(2, 0, 1, 0)]))
        self.ledger.apply(self.request('b', 1, [self.chg(2, 0, 0, 1)]))
        # The cell returned to its original value, but the touch history makes
        # any base-0 write on it a conflict regardless of the current value.
        with self.assertRaises(RequestConflict) as ctx:
            self.ledger.apply(
                self.request('c', 0, [self.chg(2, 0, 1, 0)]))
        self.assertEqual(ctx.exception.conflicts, [[2, 0]])
        self.assertEqual(self.ledger.head_version(), 2)

    def test_conflict_returns_all_coordinates_sorted(self):
        self.ledger.apply(self.request('a', 0, [
            self.chg(2, 0, 1, 0), self.chg(0, 0, 1, 0)]))
        with self.assertRaises(RequestConflict) as ctx:
            self.ledger.apply(self.request('b', 0, [
                self.chg(0, 0, 1, None),
                self.chg(2, 0, 1, None),
                self.chg(1, 0, 0, 1),  # untouched, but the batch still fails
            ]))
        self.assertEqual(ctx.exception.conflicts, [[0, 0], [2, 0]])
        self.assertEqual(self.ledger.head_version(), 1)

    def test_normalized_retry_returns_first_receipt(self):
        payload = self.request('r1', 0, [self.chg(2, 0, 1, 0)])
        first = self.ledger.apply(payload)
        reordered = {
            'changes': [{'after': 0, 'col': 0, 'row': 2, 'before': 1}],
            'base_version': 0,
            'request_id': 'r1',
        }
        second = self.ledger.apply(reordered)
        self.assertEqual(second, first)
        # Even a stale baseline retry returns the stored receipt and creates
        # no new version or identifier allocations.
        self.ledger.apply(self.request('other', 1, [self.chg(0, 2, 0, 1)]))
        third = self.ledger.apply(reordered)
        self.assertEqual(third, first)
        self.assertEqual(self.ledger.head_version(), 2)

    def test_same_id_different_request_rejected(self):
        self.ledger.apply(self.request('r1', 0, [self.chg(2, 0, 1, 0)]))
        with self.assertRaises(RequestIdConflict):
            self.ledger.apply(self.request('r1', 0, [self.chg(0, 0, 1, 0)]))
        self.assertEqual(self.ledger.head_version(), 1)

    def test_failed_request_releases_its_id(self):
        with self.assertRaises(LedgerError):
            self.ledger.apply(self.request('fail', 0, [self.chg(2, 0, 0, 1)]))
        # The same id is free for a later, valid request.
        receipt = self.ledger.apply(
            self.request('fail', 0, [self.chg(2, 0, 1, 0)]))
        self.assertEqual(receipt['request_id'], 'fail')
        self.assertEqual(self.ledger.head_version(), 1)

    def test_input_error_matrix(self):
        def bad(payload):
            with self.assertRaises(LedgerError):
                self.ledger.apply(payload)

        bad(self.request('x', True, [self.chg(0, 0, 1, 0)]))
        bad(self.request('x', -1, [self.chg(0, 0, 1, 0)]))
        bad({'request_id': 'x', 'base_version': 0, 'changes': []})
        bad(self.request('x', 0, [self.chg(0, 0, True, 0)]))
        bad(self.request('x', 0, [self.chg(0, 0, 1, 1)]))
        bad(self.request('x', 0, [
            self.chg(0, 0, 1, 0), self.chg(0, 0, 1, None)]))
        bad(self.request('x', 0, [{'row': 9, 'col': 0, 'before': 0, 'after': 1}]))
        bad({'request_id': '  ', 'base_version': 0,
             'changes': [self.chg(0, 0, 1, 0)]})
        with self.assertRaises(UnknownVersion):
            self.ledger.apply(
                self.request('x', 99, [self.chg(0, 0, 1, 0)]))
        self.assertEqual(self.ledger.head_version(), 0)

    def test_versions_are_contiguous_and_identity_is_persistent(self):
        dumbbell = self.td / 'dumbbell.json'
        # An extra isolated background cell (4,0) is kept two rows away from
        # the foreground so flipping it later is a genuine new component.
        write_json(dumbbell, {'connectivity': 4, 'grid': [
            [1, 1, 0, 1, 1],
            [1, 1, 1, 1, 1],
            [1, 1, 0, 1, 1],
            [0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0],
        ]})
        db = self.td / 'd.db'
        ledger = Ledger.initialize(db, dumbbell)
        split = ledger.apply(
            self.request('split', 0, [self.chg(1, 2, 1, 0)]))
        self.assertEqual(split['version'], 1)
        self.assertEqual(split['birth_ids'], [2])
        merge = ledger.apply(
            self.request('merge', 1, [self.chg(1, 2, 0, 1)]))
        self.assertEqual(merge['version'], 2)
        self.assertEqual(merge['retired_ids'], [2])
        # Identifier 2 is never reused by a later birth: the isolated cell
        # creates a fresh component that receives id 3.
        born = ledger.apply(
            self.request('born', 2, [self.chg(4, 0, 0, 1)]))
        self.assertEqual(born['birth_ids'], [3])
        self.assertEqual([v for (v,) in ledger._connect_read().execute(
            'SELECT version FROM versions ORDER BY version')], [0, 1, 2, 3])

    def test_object_limit_rejects_whole_request(self):
        # A legal-size raster (259x259 <= 100000 cells) holding 129 isolated
        # foreground cells on every other row/column exceeds the object cap.
        size = 259
        grid = [[1 if r % 2 == 0 and c % 2 == 0 and (r // 2 * 130 + c // 2) < 129 else 0
                 for c in range(size)] for r in range(size)]
        source = self.td / 'many.json'
        write_json(source, {'connectivity': 4, 'grid': grid})
        db = self.td / 'many.db'
        with self.assertRaises(LedgerError):
            Ledger.initialize(db, source)
        self.assertFalse(db.exists())

    def test_object_limit_on_apply_rolls_back(self):
        # v0 has two isolated objects separated by gaps; flipping a cell can
        # only merge, so instead start from a background raster and create
        # 129 objects in one request to verify the apply-side cap.
        source = self.td / 'blank.json'
        write_json(source, {'connectivity': 4,
                            'grid': [[0 for _ in range(300)] for _ in range(300)]})
        db = self.td / 'blank.db'
        ledger = Ledger.initialize(db, source)
        changes = [
            self.chg(2 * i, 0, 0, 1) for i in range(129)
        ]
        with self.assertRaises(LedgerError):
            ledger.apply(self.request('too-many', 0, changes))
        self.assertEqual(ledger.head_version(), 0)
        self.assertEqual(ledger.export(0)['result']['objects'], [])

    def test_precommit_exception_leaves_no_partial_version(self):
        calls = []

        def hook(version, receipt):
            calls.append(version)
            raise RuntimeError('injected failure')

        ledger = Ledger(self.db, precommit_hook=hook)
        with self.assertRaises(RuntimeError):
            ledger.apply(self.request('boom', 0, [self.chg(2, 0, 1, 0)]))
        self.assertEqual(calls, [1])
        # Reopen and prove the database still only contains version 0 and the
        # id allocator was untouched.
        reopened = Ledger(self.db)
        self.assertEqual(reopened.head_version(), 0)
        self.assertEqual(reopened.export(0)['result']['objects'][-1]['id'], 3)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(
                conn.execute('SELECT COUNT(*) FROM touches').fetchone()[0], 0)
            self.assertEqual(
                conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT value FROM meta WHERE key='next_id'").fetchone()[0],
                '3')
        # The request id was not consumed and the retry succeeds.
        ok = reopened.apply(self.request('boom', 0, [self.chg(2, 0, 1, 0)]))
        self.assertEqual(ok['version'], 1)

    def test_fingerprint_normalization(self):
        _rid, base, changes, fp1 = normalize_request(
            self.request('r', 2, [self.chg(1, 0, 0, 1), self.chg(0, 0, 1, 0)]))
        _rid, base, changes, fp2 = normalize_request({
            'request_id': 'r',
            'base_version': 2,
            'changes': [
                {'after': 1, 'before': 0, 'col': 0, 'row': 1},
                {'after': 0, 'before': 1, 'col': 0, 'row': 0},
            ],
        })
        self.assertEqual(fp1, fp2)
        self.assertEqual(changes, [[0, 0, 1, 0], [1, 0, 0, 1]])

    def test_failed_init_leaves_no_database_and_can_retry(self):
        bad = self.td / 'bad.json'
        bad.write_text('{}')
        target = self.td / 'failed.db'
        with self.assertRaises(ValueError):
            Ledger.initialize(target, bad)
        self.assertFalse(target.exists())
        ledger = Ledger.initialize(target, self.source)
        self.assertEqual(ledger.head_version(), 0)

    def test_null_to_foreground_participates_in_matching(self):
        source = self.td / 'unknown.json'
        write_json(source, {'connectivity': 4, 'grid': [[1, None], [0, 1]]})
        ledger = Ledger.initialize(self.td / 'u.db', source)
        receipt = ledger.apply(
            self.request('fill', 0, [self.chg(0, 1, None, 1)]))
        report = ledger.export(1)
        self.assertEqual(report['result']['labels'], [[1, 1], [0, 1]])
        self.assertEqual(report['result']['unknown_cells'], 0)
        self.assertEqual(receipt['retired_ids'], [2])
        self.assertEqual(receipt['birth_ids'], [])

    def test_one_revision_splits_and_merges_simultaneously(self):
        # Base objects (4-connectivity):
        #   O1 = {(0,0),(0,1),(0,2),(1,2)}  (L shape)
        #   O2 = {(0,4),(1,4)}
        # Removing (0,1) splits O1 into a left remnant {(0,0)} and a right
        # remnant {(0,2),(1,2)}; filling (0,3) then joins that right remnant
        # with O2.  One request therefore splits O1 and merges its remnant
        # into O2 at the same time, and every object participates in matching.
        grid = [
            [1, 1, 1, 0, 1],
            [0, 0, 1, 0, 1],
            [0, 0, 0, 0, 0],
        ]
        source = self.td / 'sm.json'
        write_json(source, {'connectivity': 4, 'grid': grid})
        ledger = Ledger.initialize(self.td / 'sm.db', source)
        self.assertEqual(
            [o['id'] for o in ledger.export(0)['result']['objects']], [1, 2])
        receipt = ledger.apply(self.request('split-merge', 0, [
            self.chg(0, 1, 1, 0),
            self.chg(0, 3, 0, 1),
        ]))
        # Optimal matching keeps two pairs (total overlap 3 vs at most 2 with
        # one pair): the split remnant inherits id 1, the merged component
        # inherits id 2.  No object is born or retired in this revision.
        pairs = {item['object_id']: item['overlap_cells']
                 for item in receipt['inheritance']}
        self.assertEqual(pairs, {1: 1, 2: 2})
        self.assertEqual(receipt['birth_ids'], [])
        self.assertEqual(receipt['retired_ids'], [])
        report = ledger.export(1)
        labels = report['result']['labels']
        # The split remnant carries the original id 1 at (0,0) only.
        self.assertEqual(labels[0][0], 1)
        self.assertEqual(labels[0][1], 0)
        # The merged component carries id 2 across cells provenant from both
        # old objects plus the new bridge cell.
        merged_cells = {
            (r, c) for r, row in enumerate(labels) for c, value in enumerate(row)
            if value == 2
        }
        self.assertIn((0, 2), merged_cells)  # former O1 remnant
        self.assertIn((0, 4), merged_cells)  # former O2
        self.assertIn((0, 3), merged_cells)  # the filled bridge
        self.assertEqual(
            sorted(o['id'] for o in report['result']['objects']), [1, 2])

    def test_all_objects_disappear_then_birth_never_reuses_ids(self):
        grid = [[1, 0, 1]]
        source = self.td / 'vanish.json'
        write_json(source, {'connectivity': 4, 'grid': grid})
        ledger = Ledger.initialize(self.td / 'vanish.db', source)
        self.assertEqual(
            [o['id'] for o in ledger.export(0)['result']['objects']], [1, 2])
        gone = ledger.apply(self.request('wipe', 0, [
            self.chg(0, 0, 1, 0), self.chg(0, 2, 1, 0)]))
        self.assertEqual(gone['retired_ids'], [1, 2])
        self.assertEqual(gone['birth_ids'], [])
        self.assertEqual(gone['inheritance'], [])
        self.assertEqual(ledger.export(1)['result']['objects'], [])
        # A brand new component after total disappearance takes a fresh
        # allocator id and never recycles a retired one.
        reborn = ledger.apply(
            self.request('reborn', 1, [self.chg(0, 1, 0, 1)]))
        self.assertEqual(reborn['birth_ids'], [3])
        self.assertEqual(reborn['retired_ids'], [])
        self.assertEqual(
            [o['id'] for o in ledger.export(2)['result']['objects']], [3])

    def test_export_rejects_unknown_and_typed_versions(self):
        with self.assertRaises(UnknownVersion):
            self.ledger.export(7)
        for bad_version in (-1, True, '1'):
            with self.assertRaises(LedgerError):
                self.ledger.export(bad_version)


if __name__ == '__main__':
    unittest.main()
