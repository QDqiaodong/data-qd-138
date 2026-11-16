"""Observable read-bounds evidence for the collection pipeline.

The representation keeps answer matrices as opaque JSON payload columns.
These tests trace the exact SQL statements executed by claim, submit,
status, export and disqualification and assert that:

* a claim never decodes another worker's answer payload, never reads a
  sentinel truth and only loads the single leased item's initial grid;
* status runs entirely on scalar columns (no ``grid_json``/``truth_json``);
* disqualification is a scalar update -- it never decodes any answer,
  including the disqualified worker's own historical normal answers;
* export reads payloads only for items that currently hold two valid
  votes, one point query per delivered item, and never reads a truth.
"""
import tempfile
import unittest
from pathlib import Path

from raster_objects.collection import Collection


class Tracer:
    def __init__(self):
        self.statements = []

    def __call__(self, event, *args):
        if event == 'stmt':
            self.statements.append(args[0])


def campaign():
    return {
        'lease_seconds': 100,
        'items': [
            {'item_id': 'n%03d' % i, 'connectivity': 4,
             'grid': [[(i + r + c) % 2 for c in range(4)] for r in range(4)]}
            for i in range(30)],
        'sentinels': [
            {'item_id': 'g1', 'connectivity': 4,
             'grid': [[1]], 'truth': [[1]]},
            {'item_id': 'g2', 'connectivity': 4,
             'grid': [[0]], 'truth': [[1]]}],
    }


class ReadBounds(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        self.db = self.td / 'c.db'
        Collection.initialize(self.db, campaign())

    def tearDown(self):
        self.temp.cleanup()

    def qualify(self, handle, worker, t):
        work = handle.claim(worker, t)
        handle.submit(worker, work['token'], t + 1, {'grid': work['grid']})
        return t + 2

    def test_claim_reads_only_the_leased_item_payload(self):
        tracer = Tracer()
        handle = Collection(self.db, observer=tracer)
        work = handle.claim('A', 0)
        # A new worker is sent to the smallest sentinel g1, not a normal.
        self.assertEqual(work['grid'], [[1]])
        # Exactly one grid payload point read (the leased item's initial
        # grid); zero truth reads and zero reads of any answer column.
        grid_reads = [s for s in tracer.statements
                      if s.startswith('SELECT l.deadline')]
        self.assertEqual(len(grid_reads), 1)
        self.assertFalse(any('truth_json' in s for s in tracer.statements))
        self.assertFalse(
            any(s.upper().startswith('SELECT') and 'grid_json' in s
                and 'submissions' in s for s in tracer.statements))
        # Dispatch uses indexed existence/count subqueries, never SELECT *.
        self.assertFalse(any('SELECT *' in s for s in tracer.statements))

    def test_repeat_claim_does_not_reload_another_payload(self):
        handle = Collection(self.db)
        first = handle.claim('A', 0)
        tracer = Tracer()
        second = Collection(self.db, observer=tracer).claim('A', 1)
        self.assertEqual(second['token'], first['token'])
        # The repeat path still resolves the leased item grid once, but
        # never touches the submissions table payload.
        self.assertFalse(
            any('submissions' in s and 'grid_json' in s
                for s in tracer.statements))

    def test_status_touches_no_payload_or_truth_column(self):
        handle = Collection(self.db)
        self.qualify(handle, 'A', 0)
        work = handle.claim('A', 2)
        handle.submit('A', work['token'], 3, {'grid': work['grid']})
        tracer = Tracer()
        report = Collection(self.db, observer=tracer).status()
        self.assertFalse(
            any('grid_json' in s or 'truth_json' in s
                for s in tracer.statements))
        # The snapshot read is a single read transaction.
        self.assertEqual(
            sum(1 for s in tracer.statements if s == 'BEGIN'), 1)
        self.assertEqual(
            sum(1 for s in tracer.statements if s == 'COMMIT'), 1)
        self.assertEqual(len(report['items']), 30)

    def test_disqualification_decodes_no_answer_matrix(self):
        handle = Collection(self.db)
        t = self.qualify(handle, 'A', 0)
        # Two normals so a renewed debt forces the g2 sentinel.
        for _ in range(2):
            work = handle.claim('A', t)
            handle.submit('A', work['token'], t + 1,
                          {'grid': work['grid']})
            t += 2
        g2 = handle.claim('A', t)
        self.assertEqual(g2['grid'], [[0]])
        tracer = Tracer()
        failing = Collection(self.db, observer=tracer)
        receipt = failing.submit('A', g2['token'], t + 1, {'grid': [[0]]})
        self.assertTrue(receipt['disqualified'])
        selects = [s for s in tracer.statements if s.upper().startswith('SELECT')]
        # No SELECT pulls an answer payload: historical answers of any worker
        # (including A's own) are never decoded for disqualification.
        self.assertFalse(
            any('grid_json' in s for s in selects), selects)
        # The only truth read is the leased sentinel's own gold row, needed
        # to score this answer; no other sentinel truth is touched.
        truth_reads = [s for s in selects if 'truth_json' in s]
        self.assertEqual(len(truth_reads), 1)
        self.assertIn("item_id = 'g2'", truth_reads[0])
        invalidation = [s for s in tracer.statements
                        if 'UPDATE submissions SET valid' in s]
        self.assertEqual(len(invalidation), 1)
        self.assertNotIn('grid_json', invalidation[0])

    def test_export_reads_payloads_only_for_two_vote_items(self):
        handle = Collection(self.db)
        t = self.qualify(handle, 'A', 0)
        t = self.qualify(handle, 'B', t)
        # Only the first normal item gets two votes; everything else stays
        # pending.
        for worker in ('A', 'B'):
            work = handle.claim(worker, t)
            handle.submit(worker, work['token'], t + 1,
                          {'grid': work['grid']})
            t += 2
        tracer = Tracer()
        report = Collection(self.db, observer=tracer).export()
        self.assertEqual([i['item_id'] for i in report['items']], ['n000'])
        # Answer payloads are fetched by exactly one point query on the
        # single delivered item; the item-level scan's count subquery never
        # selects a grid column.
        answer_queries = [
            s for s in tracer.statements
            if s.upper().startswith('SELECT WORKER_ID, GRID_JSON')]
        self.assertEqual(len(answer_queries), 1)
        self.assertIn("item_id = 'n000'", answer_queries[0])
        self.assertFalse(
            any('truth_json' in s for s in tracer.statements))
        # Each delivered annotation keeps its raw grid and a full analyze
        # result; grids are never fused.
        item = report['items'][0]
        self.assertEqual(len(item['annotations']), 2)
        self.assertEqual(
            [a['worker_id'] for a in item['annotations']], ['A', 'B'])
        for annotation in item['annotations']:
            self.assertIn('labels', annotation['analysis'])
            self.assertIn('objects', annotation['analysis'])

    def test_export_empty_campaign_runs_scalar_queries_only(self):
        tracer = Tracer()
        report = Collection(self.db, observer=tracer).export()
        self.assertEqual(report['items'], [])
        self.assertFalse(
            any(s.upper().startswith('SELECT WORKER_ID, GRID_JSON')
                for s in tracer.statements))
        self.assertFalse(
            any('truth_json' in s for s in tracer.statements))


if __name__ == '__main__':
    unittest.main()
