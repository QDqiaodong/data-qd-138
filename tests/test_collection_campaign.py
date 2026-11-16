"""Validation and lifecycle tests for the blind collection campaign.

Covers the strict campaign/answer contracts, the repository raster rules,
the 8 MB / duplicate-key / atomic-output security boundaries, init-only-once
behavior and source-file detachment after ingestion.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from raster_objects.collection import (
    Collection,
    CollectionError,
    validate_campaign,
)

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(__import__('os').environ, PYTHONPATH=str(ROOT / 'src'))


def write_json(path, payload):
    Path(path).write_text(json.dumps(payload))


def grid(h, w, fill=0):
    return [[fill for _ in range(w)] for _ in range(h)]


def campaign(items=None, sentinels=None, lease_seconds=100):
    return {
        'lease_seconds': lease_seconds,
        'items': items if items is not None else [
            {'item_id': 'n1', 'connectivity': 4, 'grid': grid(2, 2)}],
        'sentinels': sentinels if sentinels is not None else [
            {'item_id': 'g1', 'connectivity': 4, 'grid': grid(2, 2),
             'truth': grid(2, 2, 1)}],
    }


class CampaignValidation(unittest.TestCase):
    def bad(self, payload):
        with self.assertRaises(CollectionError):
            validate_campaign(payload)

    def test_strict_top_level_keys(self):
        payload = campaign()
        payload['extra'] = 1
        self.bad(payload)
        del payload['sentinels']
        self.bad(payload)
        self.bad({'lease_seconds': 100})

    def test_lease_seconds_bounds_and_types(self):
        for bad_lease in (0, 3601, -5, 1.5, True, '100', None):
            self.bad(campaign(lease_seconds=bad_lease))
        for ok_lease in (1, 3600):
            validate_campaign(campaign(lease_seconds=ok_lease))

    def test_item_and_sentinel_count_bounds(self):
        self.bad(campaign(items=[]))
        self.bad(campaign(sentinels=[]))
        self.bad(campaign(items=[
            {'item_id': 'n%d' % i, 'connectivity': 4, 'grid': [[0]]}
            for i in range(2001)]))
        self.bad(campaign(sentinels=[
            {'item_id': 'g%d' % i, 'connectivity': 4, 'grid': [[0]],
             'truth': [[1]]} for i in range(101)]))
        # At the inclusive upper limits validation succeeds (single-cell
        # rasters keep the total cell count below the cap).
        validate_campaign(campaign(items=[
            {'item_id': 'n%04d' % i, 'connectivity': 4, 'grid': [[0]]}
            for i in range(2000)], sentinels=[
            {'item_id': 'g%03d' % i, 'connectivity': 4, 'grid': [[0]],
             'truth': [[1]]} for i in range(100)]))

    def test_strict_item_keys_and_identifiers(self):
        good = campaign()
        good['items'][0]['truth'] = [[0]]          # normals cannot carry truth
        self.bad(good)
        bad = campaign()
        del bad['sentinels'][0]['truth']           # sentinels require truth
        self.bad(bad)
        for bad_id in ('', ' ', 'a\tb', 'x' * 129):
            payload = campaign()
            payload['items'][0]['item_id'] = bad_id
            self.bad(payload)

    def test_item_ids_unique_across_both_groups(self):
        payload = campaign()
        payload['sentinels'][0]['item_id'] = 'n1'
        self.bad(payload)

    def test_raster_contract_is_reused(self):
        # booleans are never integers
        payload = campaign()
        payload['items'][0]['grid'][0][0] = True
        self.bad(payload)
        # ragged, bad connectivity, oversized side, bad cell vocabulary
        self.bad(campaign(items=[
            {'item_id': 'n1', 'connectivity': 4, 'grid': [[1], [1, 0]]}]))
        self.bad(campaign(items=[
            {'item_id': 'n1', 'connectivity': 6, 'grid': [[1]]}]))
        self.bad(campaign(items=[
            {'item_id': 'n1', 'connectivity': 4, 'grid': grid(501, 1)}]))
        payload = campaign()
        payload['items'][0]['grid'][0][0] = 2
        self.bad(payload)

    def test_truth_shape_and_known_cell_requirement(self):
        payload = campaign()
        payload['sentinels'][0]['truth'] = [[None]]   # wrong shape
        self.bad(payload)
        payload = campaign(sentinels=[
            {'item_id': 'g1', 'connectivity': 4, 'grid': [[None, None]],
             'truth': [[None, None]]}])               # no known gold cell
        self.bad(payload)
        # null-truth sentinel is legal when at least one cell is known
        validated = validate_campaign(campaign(sentinels=[
            {'item_id': 'g1', 'connectivity': 4, 'grid': [[1, None]],
             'truth': [[1, None]]}]))
        self.assertEqual(validated['sentinels'][0]['height'], 1)

    def test_total_input_cell_cap(self):
        # 1001 items of 1000 cells each (grid+truth counted per sentinel).
        items = [{'item_id': 'n%04d' % i, 'connectivity': 4,
                  'grid': grid(25, 40)} for i in range(999)]
        sentinels = [{'item_id': 'g1', 'connectivity': 4,
                      'grid': grid(25, 40), 'truth': grid(25, 40)}]
        # 999*1000 + 1000 + 1000 = 1,001,000 > 1,000,000
        self.bad(campaign(items=items, sentinels=sentinels))
        # Just under the cap is accepted.
        items = [{'item_id': 'n%04d' % i, 'connectivity': 4,
                  'grid': grid(25, 40)} for i in range(998)]
        validate_campaign(campaign(items=items, sentinels=sentinels))


class LifecycleAndSecurity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_init_refuses_existing_database(self):
        db = self.td / 'c.db'
        Collection.initialize(db, campaign())
        with self.assertRaises(CollectionError):
            Collection.initialize(db, campaign())

    def test_opening_unknown_path_is_an_error(self):
        with self.assertRaises(CollectionError):
            Collection(self.td / 'missing.db')

    def test_ingested_source_is_detached(self):
        source = self.td / 'campaign.json'
        write_json(source, campaign())
        db = self.td / 'c.db'
        Collection.initialize(db, json.loads(source.read_text()))
        source.unlink()                                  # delete source file
        c = Collection(db)
        work = c.claim('A', 0)
        self.assertEqual(work['grid'], grid(2, 2))
        # Modifying a recreated file cannot change the stored task.
        write_json(source, {'tampered': True})
        self.assertEqual(Collection(db).claim('A', 1)['grid'], grid(2, 2))

    def test_duplicate_json_key_rejected(self):
        source = self.td / 'dup.json'
        source.write_text('{"lease_seconds": 10, "lease_seconds": 20,'
                          ' "items": [], "sentinels": []}')
        proc = subprocess.run(
            [sys.executable, '-m', 'raster_objects.collection_cli', 'init',
             '--db', str(self.td / 'c.db'), '--source', str(source)],
            capture_output=True, text=True, env=ENV, cwd=ROOT)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn('duplicate JSON key', proc.stderr)
        self.assertFalse((self.td / 'c.db').exists())

    def test_eight_megabyte_bound(self):
        big_grid = grid(2000, 2000)
        source = self.td / 'big.json'
        write_json(source, campaign(items=[
            {'item_id': 'big', 'connectivity': 4, 'grid': big_grid}]))
        self.assertGreater(source.stat().st_size, 8_000_000)
        proc = subprocess.run(
            [sys.executable, '-m', 'raster_objects.collection_cli', 'init',
             '--db', str(self.td / 'c.db'), '--source', str(source)],
            capture_output=True, text=True, env=ENV, cwd=ROOT)
        self.assertEqual(proc.returncode, 2)

    def test_export_refuses_database_and_aux_paths_and_preserves_report(self):
        db = self.td / 'c.db'
        Collection.initialize(db, campaign())
        old = self.td / 'report.json'
        old.write_text('{"prior": true}')
        for target in (db, Path(str(db) + '-wal'), Path(str(db) + '-shm')):
            proc = subprocess.run(
                [sys.executable, '-m', 'raster_objects.collection_cli',
                 'export', '--db', str(db), '--output', str(target)],
                capture_output=True, text=True, env=ENV, cwd=ROOT)
            self.assertEqual(proc.returncode, 2, proc.stderr)
        # A failed export leaves a previously published report untouched.
        proc = subprocess.run(
            [sys.executable, '-m', 'raster_objects.collection_cli',
             'export', '--db', str(db), '--output', str(db)],
            capture_output=True, text=True, env=ENV, cwd=ROOT)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(old.read_text(), '{"prior": true}')

    def test_bad_answer_file_exit_code_two_and_no_side_effects(self):
        db = self.td / 'c.db'
        c = Collection.initialize(db, campaign())
        work = c.claim('A', 0)
        answer = self.td / 'answer.json'
        answer.write_text('{"grid": [["not-an-int"]]}')
        proc = subprocess.run(
            [sys.executable, '-m', 'raster_objects.collection_cli', 'submit',
             '--db', str(db), '--worker', 'A', '--lease', work['token'],
             '--now', '1', '--source', str(answer)],
            capture_output=True, text=True, env=ENV, cwd=ROOT)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(c.status()['clock'], 0)


if __name__ == '__main__':
    unittest.main()
