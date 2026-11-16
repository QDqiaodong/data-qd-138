"""Persistent object identity matching between two labeled rasters.

The matcher works on bipartite graphs of *old* objects (already holding
persistent identifiers) and *new* objects (fresh scan labels of a corrected
raster).  An edge exists only when two objects share foreground cells; the
edge weight is that shared-cell count.

A one-to-one matching is chosen by lexicographic optimization on

1. total overlap cell count (maximize),
2. number of matched pairs (maximize),
3. the vector of old identifiers in the new-object order defined by each
   object's minimum cell (row first, then column); an unmatched new object
   carries a digit larger than every existing old identifier.

The three objectives are encoded into one arbitrary-precision integer so a
single rectangular assignment (Hungarian algorithm) yields the optimum.
"""


def _hungarian_min(cost):
    """Minimum-cost square assignment.

    ``cost`` is a (k+1)x(k+1) matrix indexed from 1 (row/column 0 are
    unused dummies).  Returns ``p`` where column ``j`` is assigned to row
    ``p[j]``.
    """
    k = len(cost) - 1
    u = [0] * (k + 1)
    v = [0] * (k + 1)
    p = [0] * (k + 1)
    way = [0] * (k + 1)
    infinity = float('inf')
    for i in range(1, k + 1):
        p[0] = i
        j0 = 0
        minv = [infinity] * (k + 1)
        used = [False] * (k + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = infinity
            j1 = 0
            for j in range(1, k + 1):
                if not used[j]:
                    cur = cost[i0][j] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(k + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    return p


def match_objects(old, new):
    """Match old objects onto new objects.

    ``old`` is a sequence of ``(old_id, cells)`` tuples with positive
    integer identifiers; ``new`` is a sequence of ``(new_key, cells)``
    tuples already ordered by minimum cell (row first, then column).
    ``cells`` are iterables of ``(row, col)`` foreground coordinates.

    Returns a dict with:

    * ``pairs``: list of ``(new_key, old_id, overlap_cells)`` in new order,
    * ``unmatched_new``: ``new_key`` values receiving fresh identifiers,
    * ``retired_ids``: old identifiers absent from the new raster, sorted,
    * ``vector``: old-id digits in new order, with ``None`` marking an
      unmatched position (the conceptual infinity digit),
    * ``total_overlap``: summed overlap of the chosen pairs.
    """
    old = [(oid, frozenset(cells)) for oid, cells in old]
    new = [(key, frozenset(cells)) for key, cells in new]
    n = len(old)
    m = len(new)
    old_ids = [oid for oid, _ in old]
    new_keys = [key for key, _ in new]
    if len(set(old_ids)) != n:
        raise ValueError('old object identifiers must be unique')
    if len(set(new_keys)) != m:
        raise ValueError('new object keys must be unique')
    if m == 0:
        return {
            'pairs': [],
            'unmatched_new': [],
            'retired_ids': sorted(old_ids),
            'vector': [],
            'total_overlap': 0,
        }
    max_old = max(old_ids, default=0)
    infinity_digit = max_old + 1
    base = infinity_digit + 1
    # Position weight B**(m-1-i): fixed-length base-B digits compare
    # lexicographically exactly like the identifier vector.
    position_weights = [base ** (m - 1 - i) for i in range(m)]
    vector_upper_bound = m * infinity_digit * base ** m
    pair_bonus = vector_upper_bound + 1
    weight_bonus = pair_bonus * (m + 1) + 1
    overlap = [[len(ncells & ocells) for _, ocells in old] for _, ncells in new]
    max_cells = max((len(ncells) for _, ncells in new), default=0)
    # Safe upper bound on the cost of any assignment that only uses allowed
    # cells: at most m real rows take dummy columns (infinity digits), plus
    # matched edges never cost more than zero.  Forbidden real/real zero
    # weight cells must be strictly worse than every legal assignment.
    max_legal_cost = m * infinity_digit * base ** (m - 1) + 1
    forbidden = weight_bonus * (max_cells + 1) + max_legal_cost

    # The (m+n)x(m+n) square reserves n dummy columns for unmatched new
    # objects and m dummy rows for unmatched old objects.
    k = m + n
    cost = [[0] * (k + 1) for _ in range(k + 1)]
    for i in range(1, k + 1):
        for j in range(1, k + 1):
            if i <= m and j <= n:
                w = overlap[i - 1][j - 1]
                if w > 0:
                    score = weight_bonus * w + pair_bonus - old_ids[j - 1] * position_weights[i - 1]
                    cost[i][j] = -score
                else:
                    cost[i][j] = forbidden
            elif i <= m:
                # Real new object left unmatched: infinity digit at its slot.
                cost[i][j] = infinity_digit * position_weights[i - 1]
            else:
                # Dummy row: unmatched old object or dummy-dummy, zero cost.
                cost[i][j] = 0
    assignment = _hungarian_min(cost)
    # The standard implementation returns ``p[j] = i``: row i (new object or
    # dummy row) is assigned to column j (old object or dummy column).
    used_old = set()
    pair_at_new = [None] * m
    total = 0
    for col in range(1, k + 1):
        row = assignment[col]
        if row <= m and col <= n:
            w = overlap[row - 1][col - 1]
            if w <= 0:  # pragma: no cover - forbidden cells must be avoided
                raise RuntimeError('matching used a zero-overlap edge')
            used_old.add(old_ids[col - 1])
            pair_at_new[row - 1] = (old_ids[col - 1], w)
            total += w
    pairs = []
    unmatched_new = []
    vector = []
    for i, (new_key, _cells) in enumerate(new):
        entry = pair_at_new[i]
        if entry is None:
            unmatched_new.append(new_key)
            vector.append(None)
        else:
            old_id, w = entry
            pairs.append((new_key, old_id, w))
            vector.append(old_id)
    retired = sorted(oid for oid in old_ids if oid not in used_old)
    return {
        'pairs': pairs,
        'unmatched_new': unmatched_new,
        'retired_ids': retired,
        'vector': vector,
        'total_overlap': total,
    }
