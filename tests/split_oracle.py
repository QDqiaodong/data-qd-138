"""Independent brute-force oracle for the train/eval split solver.

The oracle shares no code with the production :func:`solve_quotas`: it
enumerates every family-use assignment (``3 ** n`` vectors, only viable at
the small random case sizes used by the tests) and independently verifies
the exact quotas, forced uses and family atomicity rules.  Among feasible
vectors it returns the lexicographically smallest per-sample use vector
under the documented order ``train < eval < unused``.
"""
import itertools

TRAIN = 'train'
EVAL = 'eval'
UNUSED = 'unused'
_RANK = {TRAIN: 0, EVAL: 1, UNUSED: 2}


def brute_force_solution(family_counts, forced_uses, quotas, strata):
    """Return ``(uses, vector)`` for the optimum, or ``(None, None)``.

    ``family_counts[i]`` maps stratum name to member count for family ``i``.
    Families are assumed to be ordered by their smallest member id, matching
    the production contract.
    """
    families = range(len(family_counts))
    best = None
    for assignment in itertools.product((TRAIN, EVAL, UNUSED),
                                        repeat=len(family_counts)):
        for index, forced in enumerate(forced_uses):
            if forced is not None and assignment[index] != forced:
                break
        else:
            totals = {stratum: {TRAIN: 0, EVAL: 0} for stratum in strata}
            for index, use in enumerate(assignment):
                if use == UNUSED:
                    continue
                for stratum, count in family_counts[index].items():
                    totals[stratum][use] += count
            if all(totals[stratum][TRAIN] == quotas[stratum]['train']
                   and totals[stratum][EVAL] == quotas[stratum]['eval']
                   for stratum in strata):
                vector = []
                for index, use in enumerate(assignment):
                    vector.extend([use] * sum(family_counts[index].values()))
                if best is None or [_RANK[v] for v in vector] < \
                        [_RANK[v] for v in best[1]]:
                    best = (list(assignment), vector)
    if best is None:
        return None, None
    return best
