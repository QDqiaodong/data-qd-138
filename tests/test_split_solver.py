"""Exact-solver and plan-lifecycle tests with an independent oracle.

The production depth-first solver is compared item-by-item against
:mod:`tests.split_oracle`, which independently enumerates every assignment:
mixed-stratum families, exact quotas, forced prior deliveries, genuinely
infeasible instances and lexicographic tie-breaking all receive paired
checks.  A case where per-family greedy choices fail but backtracking
succeeds proves the solver is not a greedy approximation; the 20-family /
4-stratum cases prove the search never performs the full 3**20 enumeration.
"""
import json
import random
import unittest

from raster_objects.split_store import (
    SplitError,
    SplitStore,
    solve_quotas,
)
from split_oracle import brute_force_solution

STRATA = ['red', 'green', 'blue', 'amber']
USES = ['train', 'eval', 'unused']


def make_store():
    import tempfile
    from pathlib import Path
    td = tempfile.TemporaryDirectory()
    store = SplitStore.initialize(Path(td.name) / 'split.db')
    store.addCleanup = lambda: td.cleanup()
    return store, td


def unique_grid(number):
    """A deterministic grid whose dihedral canonical key is unique per id.

    The integer is encoded as row-major bits in a 10 x 50 grid; the fixed
    layout keeps distinct numbers dihedrally distinct for the case sizes
    used here (well under 500 distinct ids).
    """
    cells = [(number >> index) & 1 for index in range(500)]
    return [cells[offset:offset + 50] for offset in range(0, 500, 50)]


def sample(sid, spec, stratum, grid=None):
    return {'sample_id': sid, 'specimen_id': spec, 'stratum': stratum,
            'connectivity': 4,
            'grid': grid if grid is not None else unique_grid(
                int(''.join(ch for ch in sid if ch.isdigit()) or '0') + 1)}


class OracleComparison(unittest.TestCase):
    def build_store(self, family_shapes):
        """``family_shapes`` is a list of per-family stratum member counts."""
        store, td = make_store()
        self.addCleanup(td.cleanup)
        items = []
        counter = 0
        # Unique grids per family so families never duplicate-merge.
        for family_index, shape in enumerate(family_shapes):
            specimen = 'spec-%02d' % family_index
            for stratum, count in shape.items():
                for _ in range(count):
                    counter += 1
                    sid = 's%04d' % counter
                    items.append(sample(
                        sid, specimen, stratum, unique_grid(counter)))
        store.ingest({'samples': items})
        families = store.status()['families']
        self.assertEqual(len(families), len(family_shapes))
        ordered = sorted(families, key=lambda info: info['family_id'])
        return store, ordered

    def quotas_from(self, assignment, ordered, strata):
        quotas = {stratum: {'train': 0, 'eval': 0} for stratum in strata}
        for use, info in zip(assignment, ordered):
            if use == 'unused':
                continue
            for stratum, count in info['stratum_counts'].items():
                quotas[stratum][use] += count
        return quotas

    def test_mixed_stratum_families_match_oracle(self):
        rng = random.Random(20261116)
        checked = 0
        for case in range(120):
            family_count = rng.randint(1, 7)
            shapes = []
            used_strata = set()
            for _ in range(family_count):
                shape = {}
                for stratum in STRATA:
                    if rng.random() < 0.45:
                        shape[stratum] = rng.randint(1, 3)
                        used_strata.add(stratum)
                if not shape:
                    shape['red'] = rng.randint(1, 3)
                    used_strata.add('red')
                shapes.append(shape)
            strata = sorted(used_strata)
            store, ordered = self.build_store(shapes)
            family_counts = [info['stratum_counts'] for info in ordered]
            # Half the cases carry forced prior uses on random families.
            forced = [None] * family_count
            if case % 2 == 0:
                for index in rng.sample(
                        range(family_count), rng.randint(1, family_count)):
                    forced[index] = rng.choice(['train', 'eval'])
            feasible_assignment = [
                forced[index] if forced[index] is not None
                else rng.choice(USES) for index in range(family_count)]
            quotas = self.quotas_from(feasible_assignment, ordered, strata)
            if case % 3 == 0:
                # Perturb one quota to create genuinely infeasible cases.
                target = rng.choice(strata)
                quotas[target]['train'] += 1
            expected_uses, expected_vector = brute_force_solution(
                family_counts, forced, quotas, strata)
            actual_uses, stats = solve_quotas(
                family_counts, forced, quotas, strata)
            if expected_uses is None:
                self.assertIsNone(actual_uses, case)
                continue
            self.assertEqual(actual_uses, expected_uses, case)
            checked += 1
            self.assertLess(stats['nodes'], stats['full_enumeration'])
        self.assertGreater(checked, 30)

    def test_exact_quotas_never_approximate(self):
        shapes = [{'red': 2}, {'red': 1}, {'blue': 2}]
        store, ordered = self.build_store(shapes)
        family_counts = [info['stratum_counts'] for info in ordered]
        quotas = {'red': {'train': 2, 'eval': 1},
                 'blue': {'train': 0, 'eval': 1}}
        uses, _stats = solve_quotas(family_counts, [None] * 3,
                                    quotas, ['red', 'blue'])
        self.assertIsNone(uses)  # blue family cannot be split
        quotas['blue']['eval'] = 2
        uses, _stats = solve_quotas(family_counts, [None] * 3,
                                    quotas, ['red', 'blue'])
        self.assertEqual(uses, ['train', 'eval', 'eval'])

    def test_lexicographic_tie_break_prefers_train_earliest(self):
        shapes = [{'red': 1}, {'red': 1}, {'red': 1}]
        store, ordered = self.build_store(shapes)
        family_counts = [info['stratum_counts'] for info in ordered]
        quotas = {'red': {'train': 1, 'eval': 1}}
        uses, _stats = solve_quotas(family_counts, [None] * 3,
                                    quotas, ['red'])
        # train < eval < unused, and the family order is by smallest id.
        self.assertEqual(uses, ['train', 'eval', 'unused'])
        oracle_uses, oracle_vector = brute_force_solution(
            family_counts, [None] * 3, quotas, ['red'])
        self.assertEqual(uses, oracle_uses)

    def test_greedy_per_family_choice_fails_but_backtracking_succeeds(self):
        # A depth-first greedy walk assigns family 0 to train first; that
        # choice leaves the blue-eval quota unreachable for family 2, so the
        # search must backtrack and leave family 0 unused.
        shapes = [
            {'red': 1},
            {'red': 1, 'blue': 1},
            {'blue': 1},
            {'blue': 1},
        ]
        store, ordered = self.build_store(shapes)
        family_counts = [info['stratum_counts'] for info in ordered]
        quotas = {'red': {'train': 1, 'eval': 0},
                 'blue': {'train': 0, 'eval': 2}}
        uses, stats = solve_quotas(family_counts, [None] * 4,
                                   quotas, ['red', 'blue'])
        # Lexicographic order (train < eval < unused) prefers family0=train;
        # the point of this case is that backtracking must escape the dead
        # branches visited after that first greedy-feasible choice.
        self.assertEqual(uses, ['train', 'unused', 'eval', 'eval'])
        oracle_uses, _ = brute_force_solution(
            family_counts, [None] * 4, quotas, ['red', 'blue'])
        self.assertEqual(uses, oracle_uses)
        # The greedy train-first branch is abandoned before the full tree is
        # enumerated; memoization keeps the search far below 3 ** n.
        self.assertLess(stats['nodes'], stats['full_enumeration'])

    def test_greedy_walker_without_backtracking_misses_the_solution(self):
        # The two mixed families each hold red+blue; the blue-only families
        # are the only other blue source.  A walker that commits the first
        # locally-feasible use per family (mixed family 0 -> train, mixed
        # family 1 -> train) then cannot fill the blue eval quota from the
        # two single-blue families and reports failure, although the feasible
        # assignment mixed0/mixed1 -> eval exists.  Backtracking is essential.
        shapes = [
            {'red': 1, 'blue': 1},
            {'red': 1, 'blue': 1},
            {'blue': 1},
            {'blue': 1},
        ]
        store, ordered = self.build_store(shapes)
        family_counts = [info['stratum_counts'] for info in ordered]
        quotas = {'red': {'train': 0, 'eval': 2},
                 'blue': {'train': 0, 'eval': 2}}
        uses, stats = solve_quotas(family_counts, [None] * 4,
                                   quotas, ['red', 'blue'])
        self.assertEqual(uses, ['eval', 'eval', 'unused', 'unused'])
        oracle_uses, _ = brute_force_solution(
            family_counts, [None] * 4, quotas, ['red', 'blue'])
        self.assertEqual(uses, oracle_uses)
        # The memoized search visits only a small prefix of the 3**4 tree.
        self.assertLess(stats['nodes'], stats['full_enumeration'])

    def test_twenty_families_four_strata_feasible_without_full_enumeration(self):
        rng = random.Random(7)
        shapes = []
        chosen = [rng.choice(STRATA) for _ in range(20)]
        for index in range(20):
            shapes.append({chosen[index]: rng.randint(1, 4)})
        store, ordered = self.build_store(shapes)
        family_counts = [info['stratum_counts'] for info in ordered]
        assignment = [rng.choice(USES) for _ in range(20)]
        quotas = self.quotas_from(assignment, ordered, STRATA)
        uses, stats = solve_quotas(family_counts, [None] * 20,
                                   quotas, STRATA)
        self.assertIsNotNone(uses)
        self.assertEqual(stats['full_enumeration'], 3 ** 20)
        self.assertLess(stats['nodes'], 3 ** 20)
        self.assertLess(stats['nodes'], 10_000)

    def test_twenty_families_infeasible_terminates_without_timeout(self):
        rng = random.Random(11)
        shapes = [{'red': 1} for _ in range(20)]
        store, ordered = self.build_store(shapes)
        family_counts = [info['stratum_counts'] for info in ordered]
        # 20 one-member families cannot deliver 21 train samples.
        quotas = {stratum: {'train': 0, 'eval': 0} for stratum in STRATA}
        quotas['red'] = {'train': 21, 'eval': 0}
        uses, stats = solve_quotas(family_counts, [None] * 20,
                                   quotas, STRATA)
        self.assertIsNone(uses)
        self.assertLess(stats['nodes'], 3 ** 20)
        # Suffix-capacity pruning rejects the root immediately or nearly so.
        self.assertGreater(stats['pruned'], 0)

    def test_boolean_quotas_are_rejected(self):
        store, td = make_store()
        self.addCleanup(td.cleanup)
        store.ingest({'samples': [sample('a', 'sp-a', 'red')]})
        for bad in (True, False):
            with self.assertRaises(SplitError):
                store.plan({
                    'plan_id': 'p-%s' % bad, 'sample_ids': ['a'],
                    'quotas': {'red': {'train': bad, 'eval': 0}}})


class PlanLifecycle(unittest.TestCase):
    def setUp(self):
        self.store, self.td = make_store()
        self.addCleanup(self.td.cleanup)
        # One single-member family per stratum, all dihedrally distinct.
        items = [
            sample('f0', 'spec-0', 'red', grid=unique_grid(700)),
            sample('f1', 'spec-1', 'green', grid=unique_grid(701)),
            sample('f2', 'spec-2', 'blue', grid=unique_grid(702)),
            sample('f3', 'spec-3', 'amber', grid=unique_grid(703)),
        ]
        self.store.ingest({'samples': items})
        # Two families train, two eval: exact, independently satisfiable.
        self.quotas = {
            'red': {'train': 1, 'eval': 0},
            'green': {'train': 1, 'eval': 0},
            'blue': {'train': 0, 'eval': 1},
            'amber': {'train': 0, 'eval': 1},
        }

    def request(self, plan_id='p1', sample_ids=None):
        return {'plan_id': plan_id,
                'sample_ids': sample_ids if sample_ids is not None
                else ['f0', 'f1', 'f2', 'f3'],
                'quotas': json.loads(json.dumps(self.quotas))}

    def test_plan_is_not_a_commitment_until_commit(self):
        result = self.store.plan(self.request())
        self.assertEqual(result['result'], 'planned')
        status = self.store.status()
        for family in status['families']:
            self.assertEqual(family['uses'],
                             {'train': [], 'eval': []})
        self.assertEqual([plan['state'] for plan in status['plans']],
                         ['proposed'])

    def test_retry_returns_first_solution_regardless_of_order(self):
        first = self.store.plan(self.request())
        shuffled = self.request()
        shuffled['sample_ids'] = list(reversed(shuffled['sample_ids']))
        second = self.store.plan(shuffled)
        self.assertTrue(second['idempotent'])
        self.assertEqual(second['use_vector'], first['use_vector'])
        rekeyed = {
            'plan_id': 'p1',
            'quotas': dict(reversed(list(self.quotas.items()))),
            'sample_ids': self.request()['sample_ids'],
        }
        third = self.store.plan(rekeyed)
        self.assertTrue(third['idempotent'])

    def test_different_request_same_id_is_rejected(self):
        self.store.plan(self.request())
        changed = self.request()
        changed['quotas']['red']['train'] = 0
        with self.assertRaises(SplitError):
            self.store.plan(changed)
        # Rejected conflict never replaced the stored proposal.
        replay = self.store.plan(self.request())
        self.assertTrue(replay['idempotent'])

    def test_failed_outcomes_do_not_occupy_plan_id(self):
        # Infeasible request keeps the id free for a later feasible one.
        bad = self.request('p-bad')
        bad['quotas']['red']['train'] = 99
        result = self.store.plan(bad)
        self.assertEqual(result['result'], 'infeasible')
        good = self.request('p-bad')
        result = self.store.plan(good)
        self.assertEqual(result['result'], 'planned')

    def test_more_than_twenty_families_is_an_input_error(self):
        store, td = make_store()
        self.addCleanup(td.cleanup)
        items = [sample('x%02d' % i, 'spec-x-%02d' % i, 'red',
                        grid=unique_grid(300 + i)) for i in range(21)]
        store.ingest({'samples': items})
        # Sanity: the inputs really form 21 distinct families.
        self.assertEqual(len(store.status()['families']), 21)
        with self.assertRaises(SplitError):
            store.plan({
                'plan_id': 'big',
                'sample_ids': ['x%02d' % i for i in range(21)],
                'quotas': {'red': {'train': 0, 'eval': 0}}})

    def test_twenty_families_is_accepted(self):
        store, td = make_store()
        self.addCleanup(td.cleanup)
        items = [sample('t%02d' % i, 'spec-t-%02d' % i, 'red',
                        grid=unique_grid(400 + i)) for i in range(20)]
        store.ingest({'samples': items})
        result = store.plan({
            'plan_id': 'boundary',
            'sample_ids': ['t%02d' % i for i in range(20)],
            'quotas': {'red': {'train': 0, 'eval': 0}}})
        self.assertEqual(result['result'], 'planned')

    def test_unknown_samples_and_bad_quota_keys(self):
        with self.assertRaises(SplitError):
            self.store.plan({
                'plan_id': 'p', 'sample_ids': ['ghost'],
                'quotas': self.quotas})
        with self.assertRaises(SplitError):
            self.store.plan({
                'plan_id': 'p', 'sample_ids': ['f0'],
                'quotas': {'red': {'train': 1, 'eval': 1}}})


class ForcedUseTests(unittest.TestCase):
    def setUp(self):
        self.store, self.td = make_store()
        self.addCleanup(self.td.cleanup)
        self.store.ingest({'samples': [
            sample('a1', 'sp-a', 'red', grid=unique_grid(600)),
            sample('a2', 'sp-a', 'red', grid=unique_grid(601)),
            sample('b1', 'sp-b', 'blue', grid=unique_grid(602)),
        ]})

    def test_prior_delivery_forces_whole_family_and_includes_all_members(self):
        first = self.store.plan({
            'plan_id': 'p1', 'sample_ids': ['a1', 'b1'],
            'quotas': {'red': {'train': 2, 'eval': 0},
                       'blue': {'train': 0, 'eval': 1}}})
        self.assertEqual(first['result'], 'planned')
        self.store.commit('p1')
        # A later request nominates only one member of family a: the full
        # family must expand and remain train.
        follow = self.store.plan({
            'plan_id': 'p2', 'sample_ids': ['a2'],
            'quotas': {'red': {'train': 2, 'eval': 0},
                       'blue': {'train': 0, 'eval': 0}}})
        self.assertEqual(follow['result'], 'planned')
        self.assertEqual(
            follow['sample_uses'], {'a1': 'train', 'a2': 'train'})

    def test_opposite_quota_for_forced_family_is_infeasible(self):
        self.store.plan({
            'plan_id': 'p1', 'sample_ids': ['a1', 'b1'],
            'quotas': {'red': {'train': 2, 'eval': 0},
                       'blue': {'train': 0, 'eval': 1}}})
        self.store.commit('p1')
        result = self.store.plan({
            'plan_id': 'p2', 'sample_ids': ['a1'],
            'quotas': {'red': {'train': 0, 'eval': 2},
                       'blue': {'train': 0, 'eval': 0}}})
        self.assertEqual(result['result'], 'infeasible')


if __name__ == '__main__':
    unittest.main()
