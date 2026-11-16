"""End-to-end linkage acceptance for the blind collection pipeline.

Drives the canonical A/B/C scenario: A and B qualify on sentinels and each
give independent (different) answers to x and y; A then fails a sentinel,
which must atomically demote both samples to one valid vote and open the
gaps to C.  Failure injection inside the disqualifying transaction proves
that qualification, vote validity, completion state and receipt roll back
together, and that a restarted retry completes the whole linkage.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from raster_objects.collection import Collection

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))


def campaign():
    return {
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
             'grid': [[0]], 'truth': [[1]]},
        ],
    }


ANSWERS = {
    'A': {'x': [[1, 1], [0, 1]], 'y': [[0, 0], [1, 1]]},
    'B': {'x': [[1, 0], [0, 0]], 'y': [[0, 1], [1, 1]]},
    'C': {'x': [[1, 1], [0, 1]], 'y': [[0, 0], [1, 0]]},
}


class LinkageAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        self.db = self.td / 'c.db'
        self.c = Collection.initialize(self.db, campaign())

    def tearDown(self):
        self.temp.cleanup()

    def qualify(self, worker, t):
        gold = self.c.claim(worker, t)
        self.c.submit(worker, gold['token'], t + 1, {'grid': [[1]]})
        return t + 2

    def annotate(self, worker, t, item):
        work = self.c.claim(worker, t)
        self.assertEqual(work['grid'], campaign()['items'][
            0 if item == 'x' else 1]['grid'])
        self.c.submit(worker, work['token'], t + 1,
                      {'grid': ANSWERS[worker][item]})
        return t + 2

    def test_full_linkage_with_interleaved_failure_and_restart(self):
        t = 0
        t = self.qualify('A', t)
        t = self.qualify('B', t)
        t = self.annotate('A', t, 'x')
        t = self.annotate('B', t, 'x')
        t = self.annotate('A', t, 'y')
        t = self.annotate('B', t, 'y')

        # Both samples are complete with two distinct, independently kept
        # opinions.
        exported = self.c.export()
        self.assertEqual([i['item_id'] for i in exported['items']], ['x', 'y'])
        for item in exported['items']:
            self.assertEqual(
                [a['worker_id'] for a in item['annotations']], ['A', 'B'])
            grids = [a['grid'] for a in item['annotations']]
            self.assertEqual(grids[0], ANSWERS['A'][item['item_id']])
            self.assertEqual(grids[1], ANSWERS['B'][item['item_id']])

        # A now owes a sentinel.  Interleave the failing submit with a hook
        # that aborts the transaction: nothing may become visible.
        g2 = self.c.claim('A', t)
        self.assertEqual(g2['grid'], [[0]])
        t += 1

        def abort_hook(phase, worker, token, receipt):
            if phase == 'submit' and receipt.get('disqualified'):
                raise RuntimeError('injected mid-disqualification')

        failing = Collection(self.db, precommit_hook=abort_hook)
        with self.assertRaises(RuntimeError):
            failing.submit('A', g2['token'], t, {'grid': [[0]]})

        # Whole-transaction rollback: A still qualified, votes still valid,
        # samples still complete, lease still open, clock unmoved.
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute(
                "SELECT disqualified FROM workers WHERE worker_id='A'"
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM submissions WHERE valid=0").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT state FROM leases WHERE token=?",
                (g2['token'],)).fetchone()[0], 'open')
            self.assertEqual(conn.execute(
                "SELECT value FROM meta WHERE key='clock'").fetchone()[0],
                str(t - 1))
        self.assertEqual(len(self.c.export()['items']), 2)

        # Restart into a fresh handle and retry: disqualification and all
        # downstream effects now commit atomically.
        reopened = Collection(self.db)
        receipt = reopened.submit('A', g2['token'], t + 1, {'grid': [[0]]})
        self.assertTrue(receipt['disqualified'])
        status = reopened.status()
        a = next(w for w in status['workers'] if w['worker_id'] == 'A')
        self.assertTrue(a['disqualified'])
        for item in status['items']:
            self.assertEqual(item['state'], 'pending')
            self.assertEqual(item['valid_workers'], ['B'])
        self.assertEqual(reopened.export()['items'], [])

        # C qualifies and fills both gaps; B's votes and leases are intact.
        t = t + 2
        gold = reopened.claim('C', t)
        reopened.submit('C', gold['token'], t + 1, {'grid': [[1]]})
        t += 2
        for item in ('x', 'y'):
            work = reopened.claim('C', t)
            reopened.submit('C', work['token'], t + 1,
                            {'grid': ANSWERS['C'][item]})
            t += 2
        final = reopened.export()
        self.assertEqual([i['item_id'] for i in final['items']], ['x', 'y'])
        for item in final['items']:
            self.assertEqual(
                [a['worker_id'] for a in item['annotations']], ['B', 'C'])
            self.assertEqual(len(item['annotations']), 2)
        # A never reappears, even via a stale receipt retry.
        retry = reopened.submit(
            'A', g2['token'], t, {'grid': [[0]]})
        self.assertTrue(retry['disqualified'])
        for item in reopened.export()['items']:
            self.assertEqual(
                [a['worker_id'] for a in item['annotations']], ['B', 'C'])

    def test_every_status_report_matches_one_serial_state(self):
        # While C fills the gaps, every status snapshot must be internally
        # consistent: complete <=> exactly two valid workers, and A never
        # appears in any valid worker list after disqualification.
        t = 0
        t = self.qualify('A', t)
        t = self.qualify('B', t)
        for item in ('x', 'y'):
            t = self.annotate('A', t, item)
            t = self.annotate('B', t, item)
        g2 = self.c.claim('A', t)
        self.c.submit('A', g2['token'], t + 1, {'grid': [[0]]})
        t += 2
        gold = self.c.claim('C', t)
        self.c.submit('C', gold['token'], t + 1, {'grid': [[1]]})
        t += 2
        observed = []
        for item in ('x', 'y'):
            work = self.c.claim('C', t)
            self.c.submit('C', work['token'], t + 1,
                          {'grid': ANSWERS['C'][item]})
            t += 2
            snapshot = self.c.status()
            observed.append(snapshot)
            for sample in snapshot['items']:
                self.assertEqual(
                    sample['state'] == 'complete',
                    len(sample['valid_workers']) == 2)
                self.assertNotIn('A', sample['valid_workers'])
                self.assertLessEqual(len(sample['valid_workers']), 2)
        self.assertEqual(observed[-1]['items'][0]['valid_workers'], ['B', 'C'])


class CliLinkage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        self.db = self.td / 'c.db'
        self.source = self.td / 'campaign.json'
        self.source.write_text(json.dumps(campaign()))

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, '-m', 'raster_objects.collection_cli',
             *map(str, args)],
            capture_output=True, text=True, env=ENV, cwd=ROOT)

    def test_cli_end_to_end_and_exit_codes(self):
        init = self.cli('init', '--db', self.db, '--source', self.source)
        self.assertEqual(init.returncode, 0, init.stderr)
        # Re-init is a protocol error.
        self.assertEqual(self.cli(
            'init', '--db', self.db, '--source', self.source).returncode, 2)

        def claim(worker, now):
            proc = self.cli('claim', '--db', self.db,
                            '--worker', worker, '--now', now)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)

        def submit(worker, token, now, grid):
            answer = self.td / ('a-%s-%s.json' % (worker, now))
            answer.write_text(json.dumps({'grid': grid}))
            proc = self.cli('submit', '--db', self.db, '--worker', worker,
                            '--lease', token, '--now', now,
                            '--source', answer)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)

        t = 0
        for worker in ('A', 'B'):
            w = claim(worker, t)
            self.assertEqual(set(w), {'token', 'deadline',
                                      'connectivity', 'grid'})
            submit(worker, w['token'], t + 1, [[1]])
            t += 2
        for item, grid_a, grid_b in (
                ('x', ANSWERS['A']['x'], ANSWERS['B']['x']),
                ('y', ANSWERS['A']['y'], ANSWERS['B']['y'])):
            w = claim('A', t)
            submit('A', w['token'], t + 1, grid_a)
            t += 2
            w = claim('B', t)
            submit('B', w['token'], t + 1, grid_b)
            t += 2

        status = self.cli('status', '--db', self.db)
        self.assertEqual(status.returncode, 0)
        report = json.loads(status.stdout)
        self.assertEqual(len(report['workers']), 2)
        self.assertTrue(all(i['state'] == 'complete'
                            for i in report['items']))

        out = self.td / 'dataset.json'
        exported = self.cli('export', '--db', self.db, '--output', out)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        dataset = json.loads(out.read_text())
        self.assertEqual([i['item_id'] for i in dataset['items']], ['x', 'y'])

        # A fails the debt sentinel through the CLI; export becomes empty.
        g2 = claim('A', t)
        fail = submit('A', g2['token'], t + 1, [[0]])
        self.assertTrue(fail['disqualified'])
        out2 = self.td / 'dataset2.json'
        self.cli('export', '--db', self.db, '--output', out2)
        self.assertEqual(json.loads(out2.read_text())['items'], [])
        # Old report is preserved because export writes a new path.
        self.assertTrue(out.exists())


if __name__ == '__main__':
    unittest.main()
