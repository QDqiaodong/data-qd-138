"""Identity matching tests with an independent exhaustive oracle.

The oracle enumerates every bipartite matching on small instances and
applies the specification's lexicographic objective directly, so the
scalar-encoding Hungarian implementation in :mod:`raster_objects.matching`
is verified against an independent algorithm rather than shared code.
"""
import itertools
import random
import unittest

from raster_objects.matching import match_objects


def oracle_key(old, new):
    """Brute-force optimum as a comparable key.

    Key shape: ``(total_overlap, pair_count, negated_vector_digits)``.
    An unmatched new object contributes a digit larger than every old id;
    the negation turns "lexicographically smallest vector" into a maximum.
    """
    old_ids = [oid for oid, _ in old]
    infinity = max(old_ids, default=0) + 1
    weights = {}
    positive = []
    for i, (_nkey, ncells) in enumerate(new):
        for j, (oid, ocells) in enumerate(old):
            w = len(ncells & ocells)
            weights[(i, j)] = w
            if w > 0:
                positive.append((i, j))

    best = None
    for size in range(len(positive) + 1):
        for edges in itertools.combinations(positive, size):
            rows = {i for i, _ in edges}
            cols = {j for _, j in edges}
            if len(rows) != size or len(cols) != size:
                continue
            total = sum(weights[e] for e in edges)
            edge_by_new = {i: old_ids[j] for i, j in edges}
            vector = tuple(edge_by_new.get(i, infinity) for i in range(len(new)))
            candidate = (total, size, tuple(-d for d in vector))
            if best is None or candidate > best:
                best = candidate
    return best


def matcher_key(result, old):
    infinity = max((oid for oid, _ in old), default=0) + 1
    digits = tuple(infinity if d is None else d for d in result['vector'])
    return result['total_overlap'], len(result['pairs']), tuple(-d for d in digits)


def build_instance(rng, n_old, n_new, shared):
    """Random bipartite instance with disjoint new-object cell sets."""
    old = []
    for oid in range(1, n_old + 1):
        pool = list(shared)
        rng.shuffle(pool)
        take = rng.randint(1, max(1, len(pool)))
        old.append((oid, frozenset(pool[:take])))
    assigned = {}
    new = []
    for key in range(1, n_new + 1):
        pool = [cell for cell in shared if cell not in assigned]
        if not pool:
            pool = [(900 + key, 900 + key)]
        rng.shuffle(pool)
        take = rng.randint(1, max(1, len(pool)))
        kept = frozenset(pool[:take])
        for cell in kept:
            assigned[cell] = key
        new.append((key, kept))
    new.sort(key=lambda item: min(item[1]))
    return old, new


class ExhaustiveOracle(unittest.TestCase):
    def test_exhaustive_small_instances(self):
        rng = random.Random(20261116)
        shared = [(r, c) for r in range(4) for c in range(4)]
        checked = 0
        for _trial in range(500):
            n_old = rng.randint(0, 3)
            n_new = rng.randint(1, 3)
            old, new = build_instance(rng, n_old, n_new, shared)
            result = match_objects(old, new)
            self.assertEqual(matcher_key(result, old), oracle_key(old, new),
                             msg=(old, new, result))
            checked += 1
        self.assertGreater(checked, 400)

    def test_greedy_old_id_loses_optimal_overlap(self):
        # Greedy per-old-id assignment (give id1 its best edge X=4) leaves Y
        # unmatched, total 4; the global optimum pairs X->2 (5) and Y->1 (4),
        # total 9.
        old = [
            (1, frozenset({('a', 1), ('a', 2), ('a', 3), ('a', 4),
                           ('y', 1), ('y', 2), ('y', 3), ('y', 4)})),
            (2, frozenset({('a', 1), ('a', 2), ('a', 3), ('a', 4), ('a', 5)})),
        ]
        new = [
            ('X', frozenset({('a', 1), ('a', 2), ('a', 3), ('a', 4), ('a', 5)})),
            ('Y', frozenset({('y', 1), ('y', 2), ('y', 3), ('y', 4)})),
        ]
        result = match_objects(old, new)
        pairs = {key: oid for key, oid, _ in result['pairs']}
        self.assertEqual(pairs, {'X': 2, 'Y': 1})
        self.assertEqual(result['total_overlap'], 9)
        self.assertEqual(result['retired_ids'], [])
        self.assertEqual(matcher_key(result, old), oracle_key(old, new))

    def test_pair_count_is_secondary_objective(self):
        # Overlap dominates pair count: one pair of weight 3 beats two pairs
        # of combined weight 2.
        old = [
            (1, frozenset({('s', 1), ('s', 2), ('s', 3), ('y', 1)})),
            (2, frozenset({('t', 1)})),
        ]
        new = [
            ('X', frozenset({('s', 1), ('s', 2), ('s', 3), ('t', 1)})),
            ('Y', frozenset({('y', 1)})),
        ]
        result = match_objects(old, new)
        self.assertEqual(result['total_overlap'], 3)
        self.assertEqual(len(result['pairs']), 1)
        self.assertEqual(matcher_key(result, old), oracle_key(old, new))

        # Equal totals let the pair-count objective pick two pairs.
        old2 = [
            (1, frozenset({('s', 1)})),
            (2, frozenset({('t', 1)})),
        ]
        new2 = [
            ('X', frozenset({('s', 1), ('t', 1)})),
            ('Y', frozenset({('s', 1), ('y', 0)})),
        ]
        result2 = match_objects(old2, new2)
        self.assertEqual(result2['total_overlap'], 2)
        self.assertEqual(len(result2['pairs']), 2)
        self.assertEqual(
            {key: oid for key, oid, _ in result2['pairs']},
            {'X': 2, 'Y': 1},
        )
        self.assertEqual(matcher_key(result2, old2), oracle_key(old2, new2))

    def test_vector_tie_break_and_infinity(self):
        # Every positive edge has weight 1: the lexicographically smallest
        # id vector [1, 2] wins among the tied two-pair matchings.
        old = [
            (1, frozenset({('a', 0), ('b', 0)})),
            (2, frozenset({('a', 0), ('b', 0)})),
        ]
        new = [
            ('X', frozenset({('a', 0), ('x', 0)})),
            ('Y', frozenset({('b', 0), ('y', 0)})),
        ]
        result = match_objects(old, new)
        self.assertEqual(result['vector'], [1, 2])
        self.assertEqual(matcher_key(result, old), oracle_key(old, new))

        # An unmatched position is the infinity digit, beyond every old id.
        old3 = [(7, frozenset({('a', 0)}))]
        new3 = [
            ('X', frozenset({('a', 0), ('x', 0)})),
            ('Y', frozenset({('y', 0)})),
        ]
        result3 = match_objects(old3, new3)
        self.assertEqual(result3['vector'], [7, None])
        self.assertEqual(result3['unmatched_new'], ['Y'])
        self.assertEqual(result3['retired_ids'], [])
        self.assertEqual(matcher_key(result3, old3), oracle_key(old3, new3))

    def test_disappearance_and_birth(self):
        old = [(1, frozenset({('a', 0)})), (5, frozenset({('b', 0)}))]
        result = match_objects(old, [])
        self.assertEqual(result['retired_ids'], [1, 5])
        self.assertEqual(result['pairs'], [])
        self.assertEqual(result['vector'], [])

        new = [('X', frozenset({('z', 0)}))]
        result2 = match_objects([], new)
        self.assertEqual(result2['unmatched_new'], ['X'])
        self.assertEqual(result2['retired_ids'], [])
        self.assertEqual(result2['vector'], [None])
        self.assertEqual(matcher_key(result2, []), oracle_key([], new))

    def test_only_positive_edges_match(self):
        old = [(1, frozenset({('a', 0)}))]
        new = [('X', frozenset({('x', 0)}))]
        result = match_objects(old, new)
        self.assertEqual(result['pairs'], [])
        self.assertEqual(result['retired_ids'], [1])
        self.assertEqual(result['unmatched_new'], ['X'])
        self.assertEqual(result['total_overlap'], 0)


if __name__ == '__main__':
    unittest.main()
