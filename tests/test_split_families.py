"""Family equivalence, explainable edges and restart verification tests.

Independently covers non-square rotations, mirror images, dimension
differences, null-vs-zero distinction, connectivity-independent duplicate
relations, specimen-only relations on different grids, and the transitive
family formed by one specimen edge plus one duplicate edge.  After a process
restart, members, stratum counts and every edge of the contamination path
must still be verifiable.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from raster_objects.split_store import SplitStore, canonical_form
import raster_objects.split_store as ss

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))


def sample(sid, spec, stratum='red', connectivity=4, grid=None):
    return {'sample_id': sid, 'specimen_id': spec, 'stratum': stratum,
            'connectivity': connectivity,
            'grid': [[1, 0], [0, 1]] if grid is None else grid}


def family_map(status):
    return {family['family_id']: family for family in status['families']}


class CanonicalForm(unittest.TestCase):
    def test_all_eight_dihedral_views_share_a_key(self):
        base = [[1, 0, 0], [0, 1, None]]
        expected = canonical_form(base)['key']
        for transform, _h, _w, view in ss._dihedral_views(base):
            self.assertEqual(canonical_form(view)['key'], expected, transform)

    def test_non_square_rotation_changes_dimensions_but_matches(self):
        tall = [[1, 0], [0, 1], [None, 0]]           # 3 x 2
        wide = ss._rotate90_cw(tall)                  # 2 x 3
        self.assertEqual((len(wide), len(wide[0])), (2, 3))
        self.assertEqual(canonical_form(tall)['key'],
                         canonical_form(wide)['key'])

    def test_dimension_difference_blocks_equality(self):
        two_by_two = [[1, 0], [0, 1]]
        one_by_four = [[1, 0, 0, 1]]
        self.assertNotEqual(canonical_form(two_by_two)['key'],
                            canonical_form(one_by_four)['key'])

    def test_null_is_not_zero(self):
        with_null = [[None, 1], [1, 0]]
        with_zero = [[0, 1], [1, 0]]
        self.assertNotEqual(canonical_form(with_null)['key'],
                            canonical_form(with_zero)['key'])

    def test_mirror_image_is_duplicate_but_different_grid_text(self):
        base = [[1, 0], [0, 0]]
        mirrored = [row[::-1] for row in base]
        self.assertNotEqual(
            json.dumps(base, separators=(',', ':')),
            json.dumps(mirrored, separators=(',', ':')))
        self.assertEqual(canonical_form(base)['key'],
                         canonical_form(mirrored)['key'])

    def test_canonical_key_is_deterministic_and_reviewable(self):
        base = [[1, None], [0, 1]]
        first = canonical_form(base)
        second = canonical_form([row[:] for row in base])
        self.assertEqual(first, second)
        self.assertIn(0, [t for t, _h, _w, _v in ss._dihedral_views(base)])
        # The key is strict JSON text, independently decodable.
        decoded = json.loads(first['key'])
        self.assertIsInstance(decoded, list)


class FamilyRelationTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = SplitStore.initialize(
            Path(self.td.name) / 'split.db')

    def tearDown(self):
        self.td.cleanup()

    def ingest(self, *items):
        self.store.ingest({'samples': list(items)})

    def test_non_square_rotation_and_mirror_join_one_family(self):
        tall = [[1, 0], [0, 1], [None, 0]]
        self.ingest(
            sample('a', 'sp-a', grid=tall),
            sample('b', 'sp-b', connectivity=8, grid=ss._rotate90_cw(tall)),
            sample('c', 'sp-c', grid=[row[::-1] for row in tall]))
        families = family_map(self.store.status())
        self.assertEqual(len(families), 1)
        self.assertEqual(families['a']['members'], ['a', 'b', 'c'])

    def test_connectivity_does_not_affect_duplicate_relation(self):
        grid = [[1, 0], [0, 1]]
        self.ingest(
            sample('a', 'sp-a', connectivity=4, grid=grid),
            sample('b', 'sp-b', connectivity=8, grid=grid))
        self.assertEqual(
            family_map(self.store.status())['a']['members'], ['a', 'b'])

    def test_null_vs_zero_stays_separate(self):
        self.ingest(
            sample('a', 'sp-a', grid=[[None, 1], [1, 0]]),
            sample('b', 'sp-b', grid=[[0, 1], [1, 0]]))
        self.assertEqual(len(family_map(self.store.status())), 2)

    def test_dimension_difference_stays_separate(self):
        self.ingest(
            sample('a', 'sp-a', grid=[[1, 0], [0, 1]]),
            sample('b', 'sp-b', grid=[[1, 0, 0, 1]]))
        self.assertEqual(len(family_map(self.store.status())), 2)

    def test_same_specimen_different_grids_join_via_specimen_edge(self):
        self.ingest(
            sample('a', 'sp-x', grid=[[1, 0], [0, 1]]),
            sample('b', 'sp-x', grid=[[0, 0], [0, 0]]))
        families = family_map(self.store.status())
        self.assertEqual(families['a']['members'], ['a', 'b'])

    def test_empty_and_all_null_grids_are_legal(self):
        self.ingest(
            sample('a', 'sp-a', grid=[[0, 0], [0, 0]]),
            sample('b', 'sp-b', grid=[[None, None], [None, None]]),
            sample('c', 'sp-c', grid=[[0, 0], [0, 0]]))  # duplicate of a
        families = family_map(self.store.status())
        self.assertEqual(families['a']['members'], ['a', 'c'])
        self.assertEqual(families['b']['members'], ['b'])

    def test_cross_stratum_duplicates_join_one_family(self):
        grid = [[1, 1], [0, 1]]
        self.ingest(
            sample('a', 'sp-a', stratum='red', grid=grid),
            sample('b', 'sp-b', stratum='blue', connectivity=8, grid=grid))
        family = family_map(self.store.status())['a']
        self.assertEqual(family['stratum_counts'], {'blue': 1, 'red': 1})

    def test_transitive_family_one_specimen_then_one_duplicate_edge(self):
        grid_a = [[1, 0], [0, 1]]
        grid_b = [[0, 1], [1, 0]]                    # different grid
        # a and b share a specimen; c is a rotated duplicate of b only.
        self.ingest(
            sample('a', 'sp-x', grid=grid_a),
            sample('b', 'sp-x', grid=grid_b),
            sample('c', 'sp-c', grid=ss._rotate90_cw(grid_b)))
        family = family_map(self.store.status())['a']
        self.assertEqual(family['members'], ['a', 'b', 'c'])

    def test_edges_are_persisted_and_labelled(self):
        import sqlite3
        grid_a = [[1, 0], [0, 1]]
        self.ingest(
            sample('a', 'sp-x', grid=grid_a),
            sample('b', 'sp-x', grid=[[0, 0], [0, 0]]),
            sample('c', 'sp-c', grid=ss._rotate90_cw(grid_a)))
        conn = sqlite3.connect(self.store.path)
        edges = conn.execute(
            'SELECT sample_a, sample_b, kind FROM edges ORDER BY sample_a,'
            ' sample_b').fetchall()
        conn.close()
        kinds = {(a, b): kind for a, b, kind in edges}
        self.assertEqual(kinds[('a', 'b')], 'specimen')
        self.assertEqual(kinds[('a', 'c')], 'duplicate')

    def test_pair_carrying_both_relations_keeps_both_edges(self):
        import sqlite3
        grid = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
        # Same specimen AND a rotated duplicate: both explainable edges must
        # survive instead of one silently overwriting the other.
        self.ingest(
            sample('a', 'sp-x', grid=grid),
            sample('b', 'sp-x', grid=ss._rotate90_cw(grid)))
        conn = sqlite3.connect(self.store.path)
        kinds = {kind for kind, in conn.execute(
            "SELECT kind FROM edges WHERE sample_a='a' AND sample_b='b'")}
        conn.close()
        self.assertEqual(kinds, {'specimen', 'duplicate'})
        self.assertEqual(family_map(self.store.status())['a']['members'],
                         ['a', 'b'])

    def test_bridging_multiple_old_families_merges_them(self):
        grid_a = [[1, 0], [0, 1]]
        self.ingest(sample('a', 'sp-1', grid=grid_a))
        self.ingest(sample('b', 'sp-2', grid=[[1, 1], [1, 1]]))
        self.assertEqual(len(family_map(self.store.status())), 2)
        # c shares specimen with a and is a duplicate of b: bridges both.
        self.ingest(sample('c', 'sp-1', grid=ss._rotate90_cw(
            [[1, 1], [1, 1]])))
        family = family_map(self.store.status())['a']
        self.assertEqual(family['members'], ['a', 'b', 'c'])

    def test_family_id_is_smallest_member_even_when_joined_later(self):
        grid = [[1, 0], [0, 1]]
        self.ingest(sample('z', 'sp-z', grid=grid))
        self.ingest(sample('m', 'sp-m', grid=ss._rotate90_cw(grid)))
        self.assertEqual(set(family_map(self.store.status())), {'m'})
        self.ingest(sample('a', 'sp-a', grid=grid))
        self.assertEqual(set(family_map(self.store.status())), {'a'})

    def test_members_counts_and_path_survive_restart(self):
        grid_a = [[1, 0, 0], [0, 1, 0], [0, 0, 0]]
        grid_b = [[0, 0, 0], [0, 0, 0], [0, 1, 1]]
        grid_c = [[0, 1, 0], [1, 1, 1], [0, 1, 0]]
        # Two independent families receive opposite deliveries first.
        self.ingest(
            sample('a', 'sp-x', stratum='red', grid=grid_a),
            sample('b', 'sp-y', stratum='blue', grid=grid_b))
        self.store.plan({
            'plan_id': 'p', 'sample_ids': ['a'],
            'quotas': {'red': {'train': 1, 'eval': 0},
                       'blue': {'train': 0, 'eval': 0}}})
        self.store.commit('p')
        self.store.plan({
            'plan_id': 'q', 'sample_ids': ['b'],
            'quotas': {'red': {'train': 0, 'eval': 0},
                       'blue': {'train': 0, 'eval': 1}}})
        self.store.commit('q')
        # c joins both: same specimen as a, duplicate grid of b.
        self.ingest(sample('c', 'sp-x', stratum='red',
                           grid=ss._rotate90_cw(grid_b)))
        # grid_c stays unrelated; it also exercises an untouched family.
        self.ingest(sample('d', 'sp-d', stratum='red', grid=grid_c))
        # Reopen the database through a brand-new handle (process restart in
        # the CLI tests below; here at minimum a fresh connection state).
        reopened = SplitStore(self.store.path)
        status = reopened.status()
        family = family_map(status)['a']
        self.assertEqual(family['members'], ['a', 'b', 'c'])
        self.assertEqual(family['stratum_counts'], {'blue': 1, 'red': 2})
        self.assertEqual(family['uses']['train'], ['a'])
        self.assertEqual(family['uses']['eval'], ['b'])
        path = family['contamination_path']['path']
        self.assertTrue(path)
        edge_kinds = {edge['kind'] for edge in path}
        self.assertTrue(edge_kinds <= {'specimen', 'duplicate'})
        # The transitive path must mention one edge of each kind.
        self.assertEqual(edge_kinds, {'specimen', 'duplicate'})
        # Walk the path and verify every edge against stored relations.
        import sqlite3
        conn = sqlite3.connect(self.store.path)
        stored = {
            (a, b): kind for a, b, kind in conn.execute(
                'SELECT sample_a, sample_b, kind FROM edges')}
        conn.close()
        for edge in path:
            self.assertNotEqual(edge['from'], edge['to'])
            key = tuple(sorted((edge['from'], edge['to'])))
            self.assertEqual(stored[key], edge['kind'])

    def test_restart_in_subprocess_reports_identical_status(self):
        grid_a = [[1, 0], [0, 1]]
        self.ingest(
            sample('a', 'sp-x', stratum='red', grid=grid_a),
            sample('b', 'sp-x', stratum='blue', grid=[[0, 0], [0, 0]]),
            sample('c', 'sp-c', stratum='red', grid=ss._rotate90_cw(grid_a)))
        before = self.store.status()
        proc = subprocess.run(
            [sys.executable, '-m', 'raster_objects.split_cli', 'status',
             '--db', self.store.path],
            cwd=ROOT, env=ENV, text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        after = json.loads(proc.stdout)
        self.assertEqual(after['families'], before['families'])


if __name__ == '__main__':
    unittest.main()
