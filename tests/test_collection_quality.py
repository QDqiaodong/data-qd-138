"""Quality-debt and disqualification tests for the collection pipeline.

Covers null-aware sentinel scoring (a null answer on a known gold cell fails;
unknown gold cells are never scored), the new-worker and every-two-normals
debt cycle, gold exhaustion that cannot be bypassed, token idempotency and
the permanent effect of disqualification on vote validity and receipts.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from raster_objects.collection import (
    Collection,
    LeaseConflict,
)


def grid(h, w, fill=0):
    return [[fill for _ in range(w)] for _ in range(h)]


class SentinelScoring(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        # truth mixes known and unknown gold cells.
        campaign = {
            'lease_seconds': 100,
            'items': [{'item_id': 'n1', 'connectivity': 4,
                       'grid': [[0, 0], [0, 0]]}],
            'sentinels': [
                {'item_id': 'g1', 'connectivity': 4,
                 'grid': [[1, None], [0, None]],
                 'truth': [[1, None], [0, None]]}],
        }
        self.db = self.td / 'c.db'
        self.c = Collection.initialize(self.db, campaign)

    def tearDown(self):
        self.temp.cleanup()

    def test_unknown_gold_cells_are_not_scored(self):
        work = self.c.claim('A', 0)
        # Answer differs only on the null-truth cells; known cells match.
        receipt = self.c.submit(
            'A', work['token'], 1,
            {'grid': [[1, 1], [0, 0]]})
        self.assertTrue(receipt['passed'])
        self.assertFalse(receipt['disqualified'])

    def test_null_answer_on_known_gold_cell_fails(self):
        work = self.c.claim('A', 0)
        # Null where gold is known (bottom-left gold 0, top-left gold 1);
        # the unknown gold column stays null and is not scored.
        receipt = self.c.submit(
            'A', work['token'], 1,
            {'grid': [[1, None], [None, None]]})
        self.assertFalse(receipt['passed'])
        self.assertTrue(receipt['disqualified'])
        self.assertTrue(self.c.status()['workers'][0]['disqualified'])

    def test_known_value_mismatch_and_unknown_cell_combination(self):
        work = self.c.claim('A', 0)
        # (0,0) wrong value, unknown column untouched: still a failure.
        receipt = self.c.submit(
            'A', work['token'], 1,
            {'grid': [[0, None], [0, None]]})
        self.assertFalse(receipt['passed'])


class DebtCycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        campaign = {
            'lease_seconds': 100,
            'items': [
                {'item_id': 'n1', 'connectivity': 4, 'grid': [[0]]},
                {'item_id': 'n2', 'connectivity': 4, 'grid': [[1]]},
                {'item_id': 'n3', 'connectivity': 4, 'grid': [[1, 0]]},
                {'item_id': 'n4', 'connectivity': 4, 'grid': [[0, 1]]},
                {'item_id': 'n5', 'connectivity': 4, 'grid': [[1, 1]]},
            ],
            'sentinels': [
                {'item_id': 'g1', 'connectivity': 4,
                 'grid': [[1]], 'truth': [[1]]},
                {'item_id': 'g2', 'connectivity': 4,
                 'grid': [[0]], 'truth': [[0]]},
            ],
        }
        self.db = self.td / 'c.db'
        self.c = Collection.initialize(self.db, campaign)

    def tearDown(self):
        self.temp.cleanup()

    def worker(self, worker_id):
        return next(w for w in self.c.status()['workers']
                    if w['worker_id'] == worker_id)

    def test_new_worker_debt_and_two_normal_cycle(self):
        self.assertTrue(self.worker is not None)
        g = self.c.claim('A', 0)
        self.assertEqual(g['grid'], [[1]])                 # g1 first
        self.c.submit('A', g['token'], 1, {'grid': [[1]]})
        self.assertEqual(self.worker('A')['sentinel_due'], 0)
        # Two normal submissions, then the debt returns.
        n1 = self.c.claim('A', 2)
        self.assertEqual(n1['grid'], [[0]])
        self.c.submit('A', n1['token'], 3, {'grid': [[0]]})
        self.assertEqual(self.worker('A')['normal_count'], 1)
        n2 = self.c.claim('A', 4)
        self.c.submit('A', n2['token'], 5, {'grid': [[1]]})
        self.assertEqual(self.worker('A')['normal_count'], 2)
        self.assertEqual(self.worker('A')['sentinel_due'], 1)
        g2 = self.c.claim('A', 6)
        self.assertEqual(g2['grid'], [[0]])                # g2 (g1 already passed)
        self.c.submit('A', g2['token'], 7, {'grid': [[0]]})
        # Passing resets the normal counter and clears the debt.
        self.assertEqual(self.worker('A')['normal_count'], 0)
        self.assertEqual(self.worker('A')['sentinel_due'], 0)
        n3 = self.c.claim('A', 8)
        self.assertEqual(n3['grid'], [[1, 0]])             # normal work resumes

    def test_expired_sentinel_lease_does_not_count_as_answered(self):
        g = self.c.claim('A', 0)
        with self.assertRaises(Exception):
            self.c.submit('A', g['token'], 100, {'grid': [[0]]})
        # A still owes g1: the smallest unpassed sentinel has not changed.
        again = self.c.claim('A', 100)
        self.assertEqual(again['grid'], [[1]])
        self.assertNotEqual(again['token'], g['token'])

    def test_gold_exhausted_blocks_normal_dispatch(self):
        # A single qualified worker walks the full debt cycle with five
        # normal items and two sentinels; once every sentinel has been
        # passed, a renewed debt must return gold_exhausted rather than leak
        # a normal item.
        g1 = self.c.claim('A', 0)
        self.assertEqual(g1['grid'], [[1]])
        self.c.submit('A', g1['token'], 1, {'grid': [[1]]})
        # First two normals re-open the debt.
        n1 = self.c.claim('A', 2)
        self.assertEqual(n1['grid'], [[0]])
        self.c.submit('A', n1['token'], 3, {'grid': [[0]]})
        n2 = self.c.claim('A', 4)
        self.c.submit('A', n2['token'], 5, {'grid': [[1]]})
        g2 = self.c.claim('A', 6)                     # debt -> remaining gold
        self.assertEqual(g2['grid'], [[0]])
        self.c.submit('A', g2['token'], 7, {'grid': [[0]]})
        worker = next(w for w in self.c.status()['workers']
                      if w['worker_id'] == 'A')
        self.assertEqual(worker['normal_count'], 0)
        self.assertEqual(worker['sentinel_due'], 0)
        # Two more normals renew the debt again.
        n3 = self.c.claim('A', 8)
        self.c.submit('A', n3['token'], 9, {'grid': n3['grid']})
        n4 = self.c.claim('A', 10)
        self.c.submit('A', n4['token'], 11, {'grid': n4['grid']})
        worker = next(w for w in self.c.status()['workers']
                      if w['worker_id'] == 'A')
        self.assertEqual(worker['normal_count'], 2)
        self.assertEqual(worker['sentinel_due'], 1)
        # Both sentinels are already passed: no gold remains to re-probe.
        out = self.c.claim('A', 12)
        self.assertEqual(out, {'status': 'no_work',
                               'reason': 'gold_exhausted'})
        # The no_work call still succeeds and advances the clock.
        self.assertEqual(self.c.status()['clock'], 12)
        # A normal item remains unclaimed, proving debt -- not availability --
        # is the gate: the fifth normal item could otherwise be dispatched.
        pending = [i for i in self.c.status()['items']
                   if i['state'] == 'pending']
        self.assertTrue(pending)


class IdempotencyAndInvalidation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        campaign = {
            'lease_seconds': 100,
            'items': [
                {'item_id': 'x', 'connectivity': 4,
                 'grid': [[1, 0], [0, 1]]},
                {'item_id': 'y', 'connectivity': 4,
                 'grid': [[0, 0], [1, 1]]},
            ],
            'sentinels': [
                {'item_id': 'g1', 'connectivity': 4,
                 'grid': [[1]], 'truth': [[1]]},
                {'item_id': 'g2', 'connectivity': 4,
                 'grid': [[0]], 'truth': [[1]]}],
        }
        self.db = self.td / 'c.db'
        self.c = Collection.initialize(self.db, campaign)

    def tearDown(self):
        self.temp.cleanup()

    def qualify(self, worker, t):
        g = self.c.claim(worker, t)
        self.c.submit(worker, g['token'], t + 1, {'grid': [[1]]})

    def test_normalized_retry_returns_first_receipt_forever(self):
        self.qualify('A', 0)
        x = self.c.claim('A', 2)
        answer = {'grid': [[1, 1], [0, 1]]}
        first = self.c.submit('A', x['token'], 3, answer)
        # Same semantic answer with different JSON layout is the same answer.
        relayout = {'grid': [[1, 1], [0, 1]]}
        second = self.c.submit('A', x['token'], 99, relayout)
        self.assertEqual(second, first)
        # Even after the lease deadline and clock advancement, the first
        # receipt is returned and no new submission row appears.
        with sqlite3.connect(self.db) as conn:
            count = conn.execute(
                'SELECT COUNT(*) FROM submissions WHERE token=?',
                (x['token'],)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_same_token_different_answer_is_rejected(self):
        self.qualify('A', 0)
        x = self.c.claim('A', 2)
        self.c.submit('A', x['token'], 3, {'grid': [[1, 1], [0, 1]]})
        with self.assertRaises(LeaseConflict):
            self.c.submit('A', x['token'], 4, {'grid': [[0, 0], [0, 0]]})

    def test_disqualification_keeps_audit_rows_but_revives_never(self):
        self.qualify('A', 0)
        self.qualify('B', 2)
        ax = self.c.claim('A', 4)
        self.c.submit('A', ax['token'], 5, {'grid': [[1, 1], [0, 1]]})
        ay = self.c.claim('A', 6)
        self.c.submit('A', ay['token'], 7, {'grid': [[0, 0], [1, 1]]})
        bx = self.c.claim('B', 8)
        self.c.submit('B', bx['token'], 9, {'grid': [[1, 0], [0, 0]]})
        by = self.c.claim('B', 10)
        self.c.submit('B', by['token'], 11, {'grid': [[0, 1], [1, 1]]})
        self.assertEqual(len(self.c.export()['items']), 2)
        # A owes a sentinel after two normals and fails it.
        g2 = self.c.claim('A', 12)
        self.assertEqual(g2['grid'], [[0]])
        fail = self.c.submit('A', g2['token'], 13, {'grid': [[0]]})
        self.assertTrue(fail['disqualified'])
        # Both samples immediately fall back to B's single valid vote.
        for item in self.c.status()['items']:
            self.assertEqual(item['state'], 'pending')
            self.assertEqual(item['valid_workers'], ['B'])
        self.assertEqual(self.c.export()['items'], [])
        # Audit rows are retained, marked invalid.
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute(
                "SELECT item_id, valid FROM submissions "
                "WHERE worker_id='A' AND kind='normal' ORDER BY item_id"
            ).fetchall()
        self.assertEqual(rows, [('x', 0), ('y', 0)])
        # Retrying A's old successful submit returns the first receipt but
        # never resurrects the invalidated votes.
        retry = self.c.submit(
            'A', ax['token'], 500, {'grid': [[1, 1], [0, 1]]})
        self.assertTrue(retry['accepted'])
        for item in self.c.status()['items']:
            self.assertEqual(item['valid_workers'], ['B'])

    def test_different_worker_opinions_are_both_kept(self):
        self.qualify('A', 0)
        self.qualify('B', 2)
        ax = self.c.claim('A', 4)
        self.c.submit('A', ax['token'], 5, {'grid': [[1, 1], [0, 1]]})
        bx = self.c.claim('B', 6)
        self.c.submit('B', bx['token'], 7, {'grid': [[1, 0], [0, 0]]})
        report = self.c.export()
        x = next(i for i in report['items'] if i['item_id'] == 'x')
        answers = {a['worker_id']: a['grid'] for a in x['annotations']}
        self.assertEqual(answers['A'], [[1, 1], [0, 1]])
        self.assertEqual(answers['B'], [[1, 0], [0, 0]])
        # The two raw grids are delivered, never merged.
        self.assertEqual(len(x['annotations']), 2)
        self.assertNotIn('fused', x)
        for annotation in x['annotations']:
            self.assertIn('analysis', annotation)
            self.assertIn('labels', annotation['analysis'])


if __name__ == '__main__':
    unittest.main()
