"""Validation, bounds, immutability and lifecycle tests for the split store.

Covers the strict ingest contract (BATCH is exactly ``{samples}`` and each
sample exactly the five required fields), identifier and raster reuse, the
2000-sample / 4-stratum / 1,000,000-cell limits with whole-batch atomicity,
sample immutability with normalized-idempotent repeats, source-file
detachment, and the repository 8 MB / atomic-output / no-DB-overwrite
boundaries through the command line.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from raster_objects.split_store import (
    MAX_SAMPLES,
    SplitError,
    SplitStore,
    validate_batch,
)

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))


def write_json(path, payload):
    Path(path).write_text(json.dumps(payload))


def grid(h, w, fill=0):
    return [[fill for _ in range(w)] for _ in range(h)]


def sample(sid, spec=None, stratum='red', connectivity=4, raster=None):
    return {
        'sample_id': sid,
        'specimen_id': spec if spec is not None else 'spec-' + sid,
        'stratum': stratum,
        'connectivity': connectivity,
        'grid': raster if raster is not None else grid(2, 2),
    }


def batch(*items):
    return {'samples': list(items)}


def run_cli(*argv):
    proc = subprocess.run(
        [sys.executable, '-m', 'raster_objects.split_cli', *argv],
        cwd=ROOT, env=ENV, text=True, capture_output=True)
    return proc


class InitAndOpen(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db = Path(self.td.name) / 'split.db'

    def tearDown(self):
        self.td.cleanup()

    def test_init_creates_and_refuses_existing(self):
        SplitStore.initialize(self.db)
        self.assertTrue(self.db.exists())
        with self.assertRaises(SplitError):
            SplitStore.initialize(self.db)
        self.db.unlink()
        self.db.symlink_to(Path(self.td.name) / 'target')
        with self.assertRaises(SplitError):
            SplitStore.initialize(self.db)

    def test_open_rejects_foreign_or_missing_database(self):
        with self.assertRaises(SplitError):
            SplitStore(self.db)
        self.db.write_text('not a database')
        with self.assertRaises(SplitError):
            SplitStore(self.db)


class BatchValidation(unittest.TestCase):
    def bad(self, payload):
        with self.assertRaises(SplitError):
            validate_batch(payload)

    def test_strict_top_level_and_sample_fields(self):
        payload = batch(sample('a'))
        payload['extra'] = 1
        self.bad(payload)
        self.bad({'samples': []})
        self.bad({})
        good = sample('a')
        good['connectivity'] = 4
        for key in ('sample_id', 'specimen_id', 'stratum',
                    'connectivity', 'grid'):
            broken = sample('a')
            del broken[key]
            self.bad(batch(broken))
        extra = sample('a')
        extra['unexpected'] = 7
        self.bad(batch(extra))

    def test_identifiers_use_ident_rules(self):
        # Blank, whitespace-only, control-code-bearing and over-long ids are
        # the ident rejection cases; embedded spaces are ordinary characters.
        for bad_id in ('', ' ', 'x' * 129, 'tab\there'):
            for field in ('sample_id', 'specimen_id', 'stratum'):
                item = sample('a')
                item[field] = bad_id
                self.bad(batch(item))
        # ident allows up to 128 non-blank characters without control codes.
        validate_batch(batch(sample('a', stratum='z' * 128)))

    def test_duplicate_id_within_batch_rejected(self):
        self.bad(batch(sample('a'), sample('a', spec='other')))

    def test_raster_contract_is_reused(self):
        # booleans are not integers anywhere
        item = sample('a')
        item['grid'][0][0] = True
        self.bad(batch(item))
        item = sample('a', connectivity=True)
        self.bad(batch(item))
        self.bad(batch(sample('a', connectivity=6)))
        self.bad(batch(sample('a', raster=[[1], [0, 1]])))      # ragged
        self.bad(batch(sample('a', raster=grid(501, 1))))       # side limit
        self.bad(batch(sample('a', raster=grid(500, 500))))     # cell limit
        item = sample('a')
        item['grid'][0][0] = 2
        self.bad(batch(item))
        # null cells and empty-object rasters stay legal
        legal = sample('a', raster=[[None, 0], [1, None]])
        validate_batch(batch(legal))
        validate_batch(batch(sample('a', raster=[[0, 0], [0, 0]])))


class LimitsAndAtomicity(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = SplitStore.initialize(
            Path(self.td.name) / 'split.db')

    def tearDown(self):
        self.td.cleanup()

    def test_sample_limit_whole_batch_failure(self):
        first = [sample('s%04d' % i, stratum='red', raster=grid(1, 1))
                 for i in range(1999)]
        self.store.ingest(batch(*first))
        # Two more would cross 2000: nothing from the batch may survive.
        overflow = batch(
            sample('s1999', raster=grid(1, 1)),
            sample('s2000', raster=grid(1, 1)))
        with self.assertRaises(SplitError):
            self.store.ingest(overflow)
        self.assertEqual(self.store.status()['totals']['samples'], 1999)

    def test_stratum_limit(self):
        for name in ('red', 'green', 'blue', 'amber'):
            self.store.ingest(batch(sample('keep-' + name, stratum=name,
                                           raster=grid(1, 1))))
        with self.assertRaises(SplitError):
            self.store.ingest(batch(sample('fifth', stratum='violet',
                                           raster=grid(1, 1))))
        self.assertEqual(
            sorted(self.store.status()['totals']['strata']),
            ['amber', 'blue', 'green', 'red'])

    def test_cumulative_cell_limit(self):
        # 400 x 250 = 100,000 cells per raster, the single-raster maximum.
        big = grid(400, 250)
        for sid in ('q1', 'q2', 'q3'):
            self.store.ingest(batch(sample(sid, raster=[row[:] for row in big])))
        # 300,000 stored; eight more big rasters would cross 1,000,000.
        with self.assertRaises(SplitError):
            self.store.ingest(batch(*[
                sample('q%d' % n, raster=[row[:] for row in big])
                for n in range(4, 12)]))
        self.assertEqual(self.store.status()['totals']['cells'], 300_000)
        # Batches that land exactly on the cap still succeed.
        self.store.ingest(batch(
            sample('q4', raster=[row[:] for row in big]),
            sample('q5', raster=[row[:] for row in big])))
        self.assertEqual(self.store.status()['totals']['cells'], 500_000)
        self.store.ingest(batch(*[
            sample('r%d' % n, raster=[row[:] for row in big])
            for n in range(5)]))
        self.assertEqual(self.store.status()['totals']['cells'], 1_000_000)
        # Even one more cell crosses the cumulative cap and rolls back.
        with self.assertRaises(SplitError):
            self.store.ingest(batch(sample('overflow', raster=grid(1, 1))))
        self.assertEqual(self.store.status()['totals']['cells'], 1_000_000)

    def test_one_bad_item_rolls_back_the_rest(self):
        good = sample('ok', raster=grid(1, 1))
        bad = sample('broken', connectivity=6)
        with self.assertRaises(SplitError):
            self.store.ingest(batch(good, bad))
        self.assertEqual(self.store.status()['totals']['samples'], 0)

    def test_sample_limit_is_inclusive_and_restart_survives(self):
        items = [sample('m%04d' % i, raster=grid(1, 1))
                 for i in range(MAX_SAMPLES)]
        summary = self.store.ingest(batch(*items))
        self.assertEqual(summary['totals']['samples'], MAX_SAMPLES)
        with self.assertRaises(SplitError):
            self.store.ingest(batch(sample('one-too-many', raster=grid(1, 1))))
        # A brand-new handle sees the full, restarted state.
        reopened = SplitStore(self.store.path)
        self.assertEqual(
            reopened.status()['totals']['samples'], MAX_SAMPLES)


class ImmutabilityAndDetachment(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = SplitStore.initialize(
            Path(self.td.name) / 'split.db')
        self.source = Path(self.td.name) / 'batch.json'

    def tearDown(self):
        self.td.cleanup()

    def test_identical_repeat_is_side_effect_free(self):
        payload = batch(sample('a', raster=[[1, None], [0, 1]]))
        first = self.store.ingest(payload)
        second = self.store.ingest(json.loads(json.dumps(payload)))
        self.assertEqual(first['batch_seq'], second['batch_seq'])
        self.assertEqual(second['ingested'], 0)
        self.assertEqual(second['repeated'], 1)
        self.assertEqual(self.store.status()['totals']['samples'], 1)

    def test_any_field_change_is_rejected(self):
        original = sample('a', spec='sp1', stratum='red', connectivity=4,
                          raster=[[1, 0], [0, 1]])
        self.store.ingest(batch(original))
        mutations = [
            ('specimen_id', 'sp2'),
            ('stratum', 'blue'),
            ('connectivity', 8),
            ('grid', [[1, 0], [0, 0]]),
        ]
        for field, value in mutations:
            changed = json.loads(json.dumps(original))
            changed[field] = value
            with self.assertRaises(SplitError):
                self.store.ingest(batch(changed))
        self.assertEqual(self.store.status()['totals']['samples'], 1)

    def test_source_file_edits_do_not_reach_the_database(self):
        payload = batch(sample('a', raster=[[1, 0], [0, 1]]))
        write_json(self.source, payload)
        data, _ = __import__(
            'raster_objects.common', fromlist=['read']).read(self.source)
        self.store.ingest(data)
        self.source.write_text('{"tampered": true}')
        status = self.store.status()
        self.assertEqual(status['totals']['samples'], 1)
        self.source.unlink()
        plan = self.store.plan({
            'plan_id': 'p', 'sample_ids': ['a'],
            'quotas': {'red': {'train': 1, 'eval': 0}}})
        self.assertEqual(plan['result'], 'planned')


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db = Path(self.td.name) / 'split.db'
        self.batch_path = Path(self.td.name) / 'batch.json'
        self.request_path = Path(self.td.name) / 'request.json'
        self.output = Path(self.td.name) / 'export.json'
        write_json(self.batch_path, batch(sample(
            'a', stratum='red', raster=[[1, 0], [0, 1]])))

    def tearDown(self):
        self.td.cleanup()

    def test_full_cli_lifecycle(self):
        proc = run_cli('init', '--db', str(self.db))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = run_cli('ingest', '--db', str(self.db),
                       '--source', str(self.batch_path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report['ingested'], 1)
        write_json(self.request_path, {
            'plan_id': 'p1', 'sample_ids': ['a'],
            'quotas': {'red': {'train': 1, 'eval': 0}}})
        proc = run_cli('plan', '--db', str(self.db),
                       '--source', str(self.request_path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['result'], 'planned')
        proc = run_cli('commit', '--db', str(self.db), '--plan-id', 'p1')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['result'], 'committed')
        proc = run_cli('export', '--db', str(self.db),
                       '--plan-id', 'p1', '--output', str(self.output))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        exported = json.loads(self.output.read_text())
        self.assertEqual([item['sample_id'] for item in exported['train']],
                         ['a'])
        proc = run_cli('status', '--db', str(self.db))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['totals']['samples'], 1)

    def test_protocol_errors_exit_two(self):
        run_cli('init', '--db', str(self.db))
        bad = Path(self.td.name) / 'bad.json'
        write_json(bad, {'samples': []})
        proc = run_cli('ingest', '--db', str(self.db), '--source', str(bad))
        self.assertEqual(proc.returncode, 2)
        proc = run_cli('commit', '--db', str(self.db), '--plan-id', 'nope')
        self.assertEqual(proc.returncode, 2)
        proc = run_cli('export', '--db', str(self.db),
                       '--plan-id', 'nope', '--output', str(self.output))
        self.assertEqual(proc.returncode, 2)

    def test_export_cannot_overwrite_database_files(self):
        run_cli('init', '--db', str(self.db))
        run_cli('ingest', '--db', str(self.db),
                '--source', str(self.batch_path))
        write_json(self.request_path, {
            'plan_id': 'p1', 'sample_ids': ['a'],
            'quotas': {'red': {'train': 1, 'eval': 0}}})
        run_cli('plan', '--db', str(self.db),
                '--source', str(self.request_path))
        run_cli('commit', '--db', str(self.db), '--plan-id', 'p1')
        proc = run_cli('export', '--db', str(self.db),
                       '--plan-id', 'p1', '--output', str(self.db))
        self.assertEqual(proc.returncode, 2)
        for suffix in ('-wal', '-shm', '-journal'):
            helper = Path(str(self.db) + suffix)
            helper.write_text('x')
            proc = run_cli('export', '--db', str(self.db),
                           '--plan-id', 'p1', '--output', str(helper))
            self.assertEqual(proc.returncode, 2)

    def test_strict_json_boundaries(self):
        run_cli('init', '--db', str(self.db))
        dup_keys = Path(self.td.name) / 'dup.json'
        dup_keys.write_text('{"samples": [{"sample_id": "a",'
                            '"sample_id": "b"}]}')
        proc = run_cli('ingest', '--db', str(self.db),
                       '--source', str(dup_keys))
        self.assertEqual(proc.returncode, 2)
        huge = Path(self.td.name) / 'huge.json'
        huge.write_text('x' * (8_000_001))
        proc = run_cli('ingest', '--db', str(self.db),
                       '--source', str(huge))
        self.assertEqual(proc.returncode, 2)


if __name__ == '__main__':
    unittest.main()
