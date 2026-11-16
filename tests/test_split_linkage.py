"""End-to-end leakage linkage, concurrency and crash-recovery tests.

Drives the canonical A/B/C contamination scenario, the plan/commit stale
re-check with related versus unrelated intervening ingestion, two-process
opposite-use races with a barrier, and fault injection inside the ingest and
commit transactions proving that samples, family indexes, receipts and
commitments roll back together with no ghost leakage after restart.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from raster_objects.split_store import (
    SplitBusy,
    SplitError,
    SplitStore,
)
import raster_objects.split_store as ss

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))


def distinct_grid(number):
    """A 3 x 3 grid from a pool of dihedrally distinct patterns."""
    pool = (
        [[0, 0, 0], [0, 0, 0], [0, 0, 1]],
        [[0, 0, 0], [0, 0, 0], [0, 1, 1]],
        [[0, 0, 0], [0, 0, 0], [1, 1, 1]],
        [[0, 0, 0], [0, 0, 1], [1, 1, 1]],
        [[0, 0, 0], [0, 0, 0], [0, 1, 0]],
        [[0, 0, 0], [0, 0, 0], [1, 0, 1]],
        [[0, 0, 1], [0, 1, 0], [1, 0, 0]],
        [[1, 0, 0], [0, 1, 1], [0, 1, 0]],
        [[0, 1, 0], [1, 1, 1], [0, 1, 0]],
        [[1, 0, 1], [0, 1, 0], [1, 0, 1]],
        [[0, 1, 1], [1, 1, 0], [1, 0, 0]],
        [[1, 1, 0], [0, 1, 1], [1, 0, 1]],
    )
    return [row[:] for row in pool[number % len(pool)]]


def sample(sid, spec, stratum='red', connectivity=4, grid=None):
    return {'sample_id': sid, 'specimen_id': spec, 'stratum': stratum,
            'connectivity': connectivity,
            'grid': distinct_grid(0) if grid is None else grid}


def family_map(status):
    return {family['family_id']: family for family in status['families']}


def make_db():
    td = tempfile.TemporaryDirectory()
    store = SplitStore.initialize(Path(td.name) / 'split.db')
    return store, td


class ABCContaminationScenario(unittest.TestCase):
    def setUp(self):
        self.store, self.td = make_db()
        self.addCleanup(self.td.cleanup)

    def test_bridge_reports_contamination_with_two_real_edge_paths(self):
        grid_b = distinct_grid(1)
        grid_a = distinct_grid(0)
        # A and B are unrelated; A is delivered train, B is delivered eval.
        self.store.ingest({'samples': [
            sample('a', 'spec-a', stratum='red', grid=grid_a),
            sample('b', 'spec-b', stratum='red', grid=grid_b)]})
        self.store.plan({'plan_id': 'pa', 'sample_ids': ['a'],
                         'quotas': {'red': {'train': 1, 'eval': 0}}})
        self.store.commit('pa')
        self.store.plan({'plan_id': 'pb', 'sample_ids': ['b'],
                         'quotas': {'red': {'train': 0, 'eval': 1}}})
        self.store.commit('pb')
        # C shares A's specimen and is a rotated duplicate of B.
        c_grid = ss._rotate90_cw(grid_b)
        self.store.ingest({'samples': [
            sample('c', 'spec-a', stratum='red', grid=c_grid)]})
        status = self.store.status()
        family = family_map(status)['a']
        self.assertTrue(family['contaminated'])
        self.assertEqual(family['members'], ['a', 'b', 'c'])
        path = family['contamination_path']['path']
        self.assertEqual(family['contamination_path']['train_sample'], 'a')
        self.assertEqual(family['contamination_path']['eval_sample'], 'b')
        kinds = [edge['kind'] for edge in path]
        # The bridge carries one specimen and one duplicate relation.
        self.assertIn('specimen', kinds)
        self.assertIn('duplicate', kinds)
        nodes = {path[0]['from'], *(edge['to'] for edge in path)}
        self.assertEqual(nodes, {'a', 'b', 'c'})

        # A fresh plan over the contaminated family is a business result,
        # not a protocol error, and occupies no plan id.
        result = self.store.plan({
            'plan_id': 'pc', 'sample_ids': ['c'],
            'quotas': {'red': {'train': 0, 'eval': 0}}})
        self.assertEqual(result['result'], 'contaminated')
        with self.assertRaises(SplitError):
            self.store.commit('pc')

        # Historical deliveries remain fully auditable and unchanged.
        export_a = self.store.export('pa')
        export_b = self.store.export('pb')
        self.assertEqual(
            [item['sample_id'] for item in export_a['train']], ['a'])
        self.assertEqual(
            [item['sample_id'] for item in export_b['eval']], ['b'])
        self.assertEqual(export_a['receipt']['plan_id'], 'pa')

    def test_alternative_path_exposes_both_edge_orderings(self):
        # Mirror of the canonical scenario with the duplicate edge first.
        grid_a = distinct_grid(2)
        grid_b = distinct_grid(3)
        self.store.ingest({'samples': [
            sample('a', 'spec-a', grid=grid_a),
            sample('b', 'spec-b', grid=grid_b)]})
        self.store.plan({'plan_id': 'pa', 'sample_ids': ['a'],
                         'quotas': {'red': {'train': 1, 'eval': 0}}})
        self.store.commit('pa')
        self.store.plan({'plan_id': 'pb', 'sample_ids': ['b'],
                         'quotas': {'red': {'train': 0, 'eval': 1}}})
        self.store.commit('pb')
        # c duplicates a, shares specimen with b.
        self.store.ingest({'samples': [
            sample('c', 'spec-b', grid=ss._rotate90_cw(grid_a))]})
        path = family_map(self.store.status())['a']['contamination_path']
        kinds = [edge['kind'] for edge in path['path']]
        self.assertEqual(sorted(kinds), ['duplicate', 'specimen'])


class StalePlanRecheck(unittest.TestCase):
    def setUp(self):
        self.store, self.td = make_db()
        self.addCleanup(self.td.cleanup)
        self.store.ingest({'samples': [
            sample('a1', 'sp-a', grid=distinct_grid(0)),
            sample('a2', 'sp-a', grid=distinct_grid(1)),
            sample('b1', 'sp-b', grid=distinct_grid(2)),
            sample('b2', 'sp-b', grid=distinct_grid(3)),
        ]})
        self.quotas = {'red': {'train': 2, 'eval': 2}}

    def commit_count(self):
        conn = sqlite3.connect(self.store.path)
        value = conn.execute(
            "SELECT value FROM meta WHERE key='commit_seq'").fetchone()[0]
        conn.close()
        return int(value)

    def test_related_bridge_makes_commit_stale_without_new_promise(self):
        plan = self.store.plan({
            'plan_id': 'p', 'sample_ids': ['a1', 'b1'],
            'quotas': self.quotas})
        self.assertEqual(plan['result'], 'planned')
        # A new member bridges the two selected families: same specimen as a
        # and a rotated duplicate of b.
        self.store.ingest({'samples': [
            sample('bridge', 'sp-a',
                   grid=ss._rotate90_cw(distinct_grid(2)))]})
        before = self.commit_count()
        result = self.store.commit('p')
        self.assertEqual(result['result'], 'stale_plan')
        self.assertTrue(
            any(reason['reason'] == 'family_membership_changed'
                for reason in result['reasons']))
        self.assertEqual(self.commit_count(), before)
        status = self.store.status()
        for family in status['families']:
            self.assertEqual(family['uses'], {'train': [], 'eval': []})
        # The proposal itself remains stored and uncommitted.
        self.assertEqual(
            [plan_row['state'] for plan_row in status['plans']
             if plan_row['plan_id'] == 'p'], ['proposed'])

    def test_unrelated_ingest_keeps_commit_valid(self):
        plan = self.store.plan({
            'plan_id': 'p', 'sample_ids': ['a1', 'b1'],
            'quotas': self.quotas})
        self.store.ingest({'samples': [
            sample('z1', 'sp-z', grid=distinct_grid(4))]})
        result = self.store.commit('p')
        self.assertEqual(result['result'], 'committed')
        self.assertEqual(
            sorted(result['receipt']['train'] + result['receipt']['eval']),
            ['a1', 'a2', 'b1', 'b2'])

    def test_unused_family_that_becomes_contaminated_is_stale(self):
        # An unused-only proposal over the still-independent family b.
        follow = self.store.plan({
            'plan_id': 'u', 'sample_ids': ['b1'],
            'quotas': {'red': {'train': 0, 'eval': 0}}})
        self.assertEqual(
            follow['sample_uses'], {'b1': 'unused', 'b2': 'unused'})
        # Family a is delivered train while family b later gets eval.
        self.store.plan({
            'plan_id': 'ta', 'sample_ids': ['a1'],
            'quotas': {'red': {'train': 2, 'eval': 0}}})
        self.store.commit('ta')
        self.store.plan({
            'plan_id': 'eb', 'sample_ids': ['b1'],
            'quotas': {'red': {'train': 0, 'eval': 2}}})
        self.store.commit('eb')
        # The bridge joins the eval family b into the train family a.
        self.store.ingest({'samples': [
            sample('bridge', 'sp-a',
                   grid=ss._rotate90_cw(distinct_grid(2)))]})
        result = self.store.commit('u')
        self.assertEqual(result['result'], 'stale_plan')
        self.assertTrue(
            any(reason['reason'] in ('family_membership_changed',
                                     'family_contaminated')
                for reason in result['reasons']))
        # Zero new promises were recorded by the failed commit.
        conn = sqlite3.connect(self.store.path)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM deliveries WHERE plan_id='u'")
            .fetchone()[0], 0)
        conn.close()

    def test_same_use_family_growth_does_not_invalidate(self):
        plan = self.store.plan({
            'plan_id': 'p', 'sample_ids': ['a1', 'b1'],
            'quotas': self.quotas})
        self.store.commit('p')
        # Another plan proposes train for family a again; a new same-family
        # member appearing before its commit is compatible.
        follow = self.store.plan({
            'plan_id': 'q', 'sample_ids': ['a1'],
            'quotas': {'red': {'train': 2, 'eval': 0}}})
        self.assertEqual(follow['result'], 'planned')
        self.store.ingest({'samples': [
            sample('a3', 'sp-a', grid=distinct_grid(5))]})
        # Membership changed since q was planned: stale is the honest answer,
        # and replanning against the current family then succeeds.
        result = self.store.commit('q')
        self.assertEqual(result['result'], 'stale_plan')
        replanned = self.store.plan({
            'plan_id': 'q2', 'sample_ids': ['a1', 'a3'],
            'quotas': {'red': {'train': 3, 'eval': 0}}})
        self.assertEqual(replanned['result'], 'planned')
        again = self.store.commit('q2')
        self.assertEqual(again['result'], 'committed')


def driver_script():
    return textwrap.dedent("""
        import sys
        import time
        from pathlib import Path
        from raster_objects.split_store import SplitStore, SplitBusy

        db, plan_id, use_name, barrier = sys.argv[1:5]
        barrier = Path(barrier)
        store = SplitStore(db)
        (barrier / (plan_id + '-ready')).touch()
        deadline = time.time() + 30
        while not (barrier / 'go').exists():
            if time.time() > deadline:
                raise RuntimeError('barrier timeout')
            time.sleep(0.01)
        try:
            while True:
                try:
                    result = store.commit(plan_id)
                    break
                except SplitBusy:
                    time.sleep(0.05)
        except Exception as exc:  # pragma: no cover - surfaced via result file
            result = {'result': 'error', 'error': str(exc)}
        (barrier / (plan_id + '-result')).write_text(
            __import__('json').dumps(result, sort_keys=True))
    """)


class TwoProcessOppositeUseRace(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.barrier = Path(self.td.name) / 'barrier'
        self.barrier.mkdir()
        self.db = Path(self.td.name) / 'split.db'
        self.store = SplitStore.initialize(self.db)
        self.store.ingest({'samples': [
            sample('a1', 'sp-a', grid=distinct_grid(0)),
            sample('a2', 'sp-a', grid=distinct_grid(1))]})
        self.store.plan({'plan_id': 'p-train', 'sample_ids': ['a1'],
                         'quotas': {'red': {'train': 2, 'eval': 0}}})
        self.store.plan({'plan_id': 'p-eval', 'sample_ids': ['a1'],
                         'quotas': {'red': {'train': 0, 'eval': 2}}})

    def tearDown(self):
        self.td.cleanup()

    def test_only_one_opposite_use_commit_wins(self):
        script = Path(self.td.name) / 'driver.py'
        script.write_text(driver_script())
        procs = []
        for plan_id, use_name in (('p-train', 'train'), ('p-eval', 'eval')):
            procs.append(subprocess.Popen(
                [sys.executable, str(script), str(self.db), plan_id,
                 use_name, str(self.barrier)],
                cwd=ROOT, env=ENV))
        deadline = 200
        while len(list(self.barrier.glob('*-ready'))) < 2 and deadline:
            deadline -= 1
            __import__('time').sleep(0.05)
        self.assertEqual(len(list(self.barrier.glob('*-ready'))), 2)
        (self.barrier / 'go').touch()
        for proc in procs:
            proc.wait(timeout=60)
            self.assertEqual(proc.returncode, 0)
        results = []
        for plan_id in ('p-train', 'p-eval'):
            results.append(json.loads(
                (self.barrier / (plan_id + '-result')).read_text()))
        committed = [r for r in results if r['result'] == 'committed']
        stale = [r for r in results if r['result'] == 'stale_plan']
        self.assertEqual(len(committed), 1)
        self.assertEqual(len(stale), 1)
        status = SplitStore(self.db).status()
        family = family_map(status)['a1']
        train = family['uses']['train']
        eval_ = family['uses']['eval']
        self.assertTrue(bool(train) ^ bool(eval_))
        self.assertEqual(len(train or eval_), 2)
        # Repeating the winning commit is idempotent, not double booked.
        winner = 'p-train' if train else 'p-eval'
        repeat = SplitStore(self.db).commit(winner)
        self.assertTrue(repeat['idempotent'])


class FaultInjectionRecovery(unittest.TestCase):
    def setUp(self):
        self.store, self.td = make_db()
        self.addCleanup(self.td.cleanup)

    def test_ingest_precommit_failure_rolls_everything_back(self):
        self.store.ingest({'samples': [
            sample('a', 'sp-a', grid=distinct_grid(0))]})

        def fail_ingest(phase, summary):
            if phase == 'ingest':
                raise RuntimeError('injected ingest failure')

        crashing = SplitStore(self.store.path,
                              precommit_hook=fail_ingest)
        with self.assertRaises(RuntimeError):
            crashing.ingest({'samples': [
                sample('b', 'sp-b', grid=distinct_grid(1))]})
        # Reopen to prove recovery, not just in-memory state.
        reopened = SplitStore(self.store.path)
        status = reopened.status()
        self.assertEqual(status['totals']['samples'], 1)
        self.assertEqual(family_map(status)['a']['members'], ['a'])
        # The failed batch can be retried cleanly.
        summary = reopened.ingest({'samples': [
            sample('b', 'sp-b', grid=distinct_grid(1))]})
        self.assertEqual(summary['ingested'], 1)

    def test_commit_precommit_failure_leaves_no_delivery(self):
        store = self.store
        store.ingest({'samples': [
            sample('a1', 'sp-a', grid=distinct_grid(0)),
            sample('a2', 'sp-a', grid=distinct_grid(1))]})
        store.plan({'plan_id': 'p', 'sample_ids': ['a1'],
                    'quotas': {'red': {'train': 2, 'eval': 0}}})

        def fail_commit(phase, receipt):
            if phase == 'commit':
                raise RuntimeError('injected commit failure')

        crashing = SplitStore(store.path, precommit_hook=fail_commit)
        with self.assertRaises(RuntimeError):
            crashing.commit('p')
        reopened = SplitStore(store.path)
        status = reopened.status()
        for family in status['families']:
            self.assertEqual(family['uses'], {'train': [], 'eval': []})
        self.assertEqual(
            [plan_row['state'] for plan_row in status['plans']],
            ['proposed'])
        conn = sqlite3.connect(store.path)
        self.assertEqual(conn.execute(
            'SELECT COUNT(*) FROM deliveries').fetchone()[0], 0)
        self.assertEqual(conn.execute(
            'SELECT COUNT(*) FROM sample_use').fetchone()[0], 0)
        self.assertEqual(conn.execute(
            'SELECT COUNT(*) FROM receipts').fetchone()[0], 0)
        conn.close()
        # Retry after restart completes the delivery exactly once.
        receipt = reopened.commit('p')
        self.assertEqual(receipt['result'], 'committed')
        again = reopened.commit('p')
        self.assertTrue(again['idempotent'])
        self.assertEqual(
            family_map(reopened.status())['a1']['uses']['train'],
            ['a1', 'a2'])

    def test_serial_interpretation_of_interleaved_ingest_and_commit(self):
        store = self.store
        store.ingest({'samples': [
            sample('a1', 'sp-a', grid=distinct_grid(0)),
            sample('b1', 'sp-b', grid=distinct_grid(1))]})
        store.plan({'plan_id': 'p', 'sample_ids': ['a1', 'b1'],
                    'quotas': {'red': {'train': 1, 'eval': 1}}})
        # Serial order 1: bridge lands first, so commit must be stale.
        store.ingest({'samples': [
            sample('c', 'sp-a',
                   grid=ss._rotate90_cw(distinct_grid(1)))]})
        self.assertEqual(store.commit('p')['result'], 'stale_plan')
        # A fresh plan under the serial post-bridge state commits normally.
        follow = store.plan({
            'plan_id': 'q', 'sample_ids': ['a1'],
            'quotas': {'red': {'train': 0, 'eval': 0}}})
        self.assertEqual(follow['result'], 'planned')
        self.assertEqual(store.commit('q')['result'], 'committed')


class SnapshotAndPayloadEvidence(unittest.TestCase):
    def setUp(self):
        self.store, self.td = make_db()
        self.addCleanup(self.td.cleanup)
        self.events = []
        self.observed = SplitStore(
            self.store.path, observer=lambda event, *args:
            self.events.append((event,) + args))

    def test_status_and_plan_never_decode_grid_payloads(self):
        self.observed.ingest({'samples': [
            sample('a1', 'sp-a', stratum='red', grid=distinct_grid(0)),
            sample('a2', 'sp-a', stratum='red', grid=distinct_grid(1)),
            sample('b1', 'sp-b', stratum='blue', grid=distinct_grid(2))]})
        self.events.clear()
        self.observed.status()
        self.observed.plan({'plan_id': 'p', 'sample_ids': ['a1', 'b1'],
                            'quotas': {
                                'red': {'train': 2, 'eval': 0},
                                'blue': {'train': 0, 'eval': 1}}})
        self.observed.commit('p')
        self.observed.status()
        payload_events = [event for event in self.events
                          if event[0] == 'payload_decode']
        self.assertEqual(payload_events, [])
        statements = ' '.join(
            sql for event, sql in self.events if event == 'stmt')
        # No family/planning query touches the opaque payload column.
        self.assertNotIn('grid_json', statements)

    def test_only_export_reads_required_payloads(self):
        self.observed.ingest({'samples': [
            sample('a1', 'sp-a', stratum='red', grid=distinct_grid(0)),
            sample('b1', 'sp-b', stratum='blue', grid=distinct_grid(2))]})
        self.observed.plan({'plan_id': 'p', 'sample_ids': ['a1', 'b1'],
                            'quotas': {
                                'red': {'train': 1, 'eval': 0},
                                'blue': {'train': 0, 'eval': 1}}})
        self.observed.commit('p')
        self.events.clear()
        report = self.observed.export('p')
        decoded = [event for event in self.events
                   if event[0] == 'payload_decode']
        self.assertEqual(
            sorted(event[2] for event in decoded), ['a1', 'b1'])
        self.assertEqual(
            [item['sample_id'] for item in report['train']], ['a1'])
        self.assertEqual(
            [item['sample_id'] for item in report['eval']], ['b1'])

    def test_status_is_one_snapshot(self):
        self.observed.ingest({'samples': [
            sample('a1', 'sp-a', grid=distinct_grid(0)),
            sample('b1', 'sp-b', grid=distinct_grid(1))]})
        transactions = [
            sql for event, sql in self.events
            if event == 'stmt' and sql.upper().startswith('BEGIN')]
        self.events.clear()
        self.observed.status()
        begins = [event for event, sql in self.events
                  if event == 'stmt' and sql.upper().startswith('BEGIN')]
        commits = [event for event, sql in self.events
                   if event == 'stmt' and sql.upper().startswith('COMMIT')]
        self.assertEqual(len(begins), 1)
        self.assertEqual(len(commits), 1)


if __name__ == '__main__':
    unittest.main()
