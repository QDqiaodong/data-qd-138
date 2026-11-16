"""Persistent leakage-safe train/eval split warehouse.

The warehouse stores independent raster samples, groups them into *families*
of images that must never be separated across the train/eval boundary, and
records irreversible per-sample usage commitments.  Two equivalence relations
define a family, both kept as explainable persistent edges whose transitive
closure is materialized as a relational family index:

* ``specimen`` edges connect samples carrying the same ``specimen_id``;
* ``duplicate`` edges connect grids that become identical after any of the
  eight dihedral transformations (0/90/180/270 degree rotations, with or
  without a mirror reflection).  Transformed dimensions participate in the
  comparison and ``null`` never equals ``0``; connectivity and stratum do not
  influence the duplicate relation.

Only the Python standard library and :mod:`sqlite3` are used.  Grid payloads
are decoded exactly once, at ingestion, to derive a reviewable canonical
dihedral key; every later family query, plan, commitment and status report is
answered from scalar columns and relational indexes.  Only ``export`` reads
stored grid payloads back.

Design properties
-----------------

* Every write (ingest / plan / commit) is one ``BEGIN IMMEDIATE`` transaction
  so concurrent processes have a serial interpretation; a lost lock raises
  :class:`SplitBusy` with zero side effects.
* A ``precommit_hook`` fires immediately before each commit; raising from it
  rolls the whole transaction back, including family indexes and commitments.
* Plans are proposals only.  A ``commit`` re-validates the *current* family
  membership and every prior delivery: later bridging, contamination or an
  opposite-use commitment by another plan yields ``stale_plan`` with no new
  promise.  Unrelated samples never invalidate a plan.
* Contamination is never hidden by rejecting input: bridging samples ingest
  successfully and status/plans report the contaminated family together with
  a real edge path whose edges are labelled ``specimen`` or ``duplicate``.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .common import ident
from .model import analyze

# --------------------------------------------------------------------- bounds
MAX_SAMPLES = 2000
MAX_STRATA = 4
MAX_TOTAL_CELLS = 1_000_000
MAX_PLAN_FAMILIES = 20
USE_TRAIN = 'train'
USE_EVAL = 'eval'
USE_UNUSED = 'unused'
# Numeric order encodes the tie-break rule: train < eval < unused.
USE_ORDER = {USE_TRAIN: 0, USE_EVAL: 1, USE_UNUSED: 2}
USE_BY_INDEX = {0: USE_TRAIN, 1: USE_EVAL, 2: USE_UNUSED}
EDGE_SPECIMEN = 'specimen'
EDGE_DUPLICATE = 'duplicate'

_SCHEMA = """
CREATE TABLE meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE samples(
    sample_id TEXT PRIMARY KEY,
    specimen_id TEXT NOT NULL,
    stratum TEXT NOT NULL,
    connectivity INTEGER NOT NULL,
    height INTEGER NOT NULL,
    width INTEGER NOT NULL,
    cell_count INTEGER NOT NULL,
    canon_key TEXT NOT NULL,
    canon_transform INTEGER NOT NULL,
    content_fp TEXT NOT NULL,
    grid_json TEXT NOT NULL,
    batch_seq INTEGER NOT NULL
) WITHOUT ROWID;
CREATE INDEX idx_samples_specimen ON samples(specimen_id);
CREATE INDEX idx_samples_canon ON samples(canon_key);
CREATE INDEX idx_samples_stratum ON samples(stratum);
CREATE TABLE edges(
    sample_a TEXT NOT NULL,
    sample_b TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('specimen', 'duplicate')),
    batch_seq INTEGER NOT NULL,
    PRIMARY KEY(sample_a, sample_b, kind)
) WITHOUT ROWID;
CREATE INDEX idx_edges_b ON edges(sample_b);
CREATE INDEX idx_edges_kind ON edges(kind);
CREATE TABLE family_member(
    sample_id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX idx_family_member_family ON family_member(family_id);
CREATE TABLE plans(
    plan_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('proposed', 'committed')),
    request_json TEXT NOT NULL,
    solution_json TEXT NOT NULL,
    batch_seq INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE deliveries(
    plan_id TEXT NOT NULL,
    sample_id TEXT NOT NULL,
    use_name TEXT NOT NULL CHECK(use_name IN ('train', 'eval')),
    PRIMARY KEY(plan_id, sample_id)
) WITHOUT ROWID;
CREATE INDEX idx_deliveries_sample ON deliveries(sample_id);
CREATE TABLE sample_use(
    sample_id TEXT PRIMARY KEY,
    use_name TEXT NOT NULL CHECK(use_name IN ('train', 'eval'))
) WITHOUT ROWID;
CREATE TABLE receipts(
    plan_id TEXT PRIMARY KEY,
    receipt_json TEXT NOT NULL
) WITHOUT ROWID;
"""


class SplitError(ValueError):
    """Base class for split-warehouse input and protocol errors (exit 2)."""


class SplitBusy(SplitError):
    """A write lost a concurrency race; the call had no side effects."""


class UnknownPlan(SplitError):
    """The referenced plan id has never been accepted by this database."""


# ------------------------------------------------------------ pure grid tools
def _rotate90_cw(grid):
    height = len(grid)
    width = len(grid[0])
    return [[grid[height - 1 - c][r] for c in range(height)]
            for r in range(width)]


def _dihedral_views(grid):
    """Yield the eight dihedral views as ``(transform, height, width, view)``.

    Transform numbers are ``4 * reflect + rotations`` where ``reflect`` is 0
    or 1 and ``rotations`` is the number of 90-degree clockwise turns.
    """
    views = []
    for reflect in (0, 1):
        current = [row[::-1] for row in grid] if reflect else [row[:] for row in grid]
        for rotations in range(4):
            if rotations:
                current = _rotate90_cw(current)
            views.append((4 * reflect + rotations,
                          len(current), len(current[0]), current))
    return views


def _canonical_text(grid):
    return json.dumps(grid, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False)


def canonical_form(grid):
    """Return the reviewable dihedral canonical representation of a grid.

    The canonical ``key`` is the lexicographically smallest strict JSON
    serialization among the eight rotated/mirrored views.  The serialized
    shape makes transformed dimensions part of the comparison, while ``null``
    and ``0`` remain distinct tokens.  Two grids share a key exactly when they
    are duplicate-related.
    """
    best = None
    for transform, height, width, view in _dihedral_views(grid):
        key = _canonical_text(view)
        if best is None or key < best['key']:
            best = {'key': key, 'transform': transform,
                    'height': height, 'width': width}
    return best


def _strict_int(value, name):
    if type(value) is not int:
        raise SplitError('%s must be an integer (booleans are rejected)' % name)
    return value


def validate_batch(batch):
    """Validate an ingest payload and return normalized sample records.

    Validation is entirely side-effect free: no database file is touched and
    a rejected batch leaves the caller's database unchanged by construction.
    """
    if not isinstance(batch, dict) or set(batch) != {'samples'}:
        raise SplitError('batch must contain exactly samples')
    raw = batch['samples']
    if not isinstance(raw, list) or not raw:
        raise SplitError('samples must be a nonempty array')
    seen = set()
    normalized = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {
                'sample_id', 'specimen_id', 'stratum',
                'connectivity', 'grid'}:
            raise SplitError(
                'each sample must contain exactly sample_id, specimen_id,'
                ' stratum, connectivity, grid')
        try:
            sample_id = ident(item['sample_id'])
            specimen_id = ident(item['specimen_id'])
            stratum = ident(item['stratum'])
        except ValueError as exc:
            raise SplitError(str(exc)) from exc
        connectivity = _strict_int(item['connectivity'], 'connectivity')
        if connectivity not in (4, 8):
            raise SplitError('connectivity must be 4 or 8')
        grid = item['grid']
        # Reuse the repository's full single-raster contract: bounds,
        # rectangularity, 0/1/null vocabulary and boolean rejection.
        try:
            analyze({'connectivity': connectivity, 'grid': grid})
        except (ValueError, TypeError, RecursionError) as exc:
            raise SplitError('grid failed raster validation: ' + str(exc)) from exc
        if sample_id in seen:
            raise SplitError('duplicate sample_id in batch: ' + sample_id)
        seen.add(sample_id)
        height = len(grid)
        width = len(grid[0])
        canon = canonical_form(grid)
        record = {
            'sample_id': sample_id,
            'specimen_id': specimen_id,
            'stratum': stratum,
            'connectivity': connectivity,
            'height': height,
            'width': width,
            'cell_count': height * width,
            'grid': grid,
            'grid_json': _canonical_text(grid),
            'canon_key': canon['key'],
            'canon_transform': canon['transform'],
            'content_fp': json.dumps(
                {'specimen_id': specimen_id, 'stratum': stratum,
                 'connectivity': connectivity, 'grid': grid},
                sort_keys=True, separators=(',', ':'),
                ensure_ascii=False, allow_nan=False),
        }
        normalized.append(record)
    return normalized


def request_fingerprint(request):
    """Canonical identity of a plan request (order-independent)."""
    sample_ids = sorted(request['sample_ids'])
    quotas = {
        stratum: {'eval': int(pair['eval']), 'train': int(pair['train'])}
        for stratum, pair in request['quotas'].items()
    }
    return json.dumps(
        {'quotas': quotas, 'sample_ids': sample_ids},
        sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False)


# ------------------------------------------------------------------- DSU core
class _DSU:
    def __init__(self):
        self.parent = {}

    def add(self, node):
        if node not in self.parent:
            self.parent[node] = node

    def find(self, node):
        root = node
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[node] != node:
            nxt = self.parent[node]
            self.parent[node] = root
            node = nxt
        return root

    def union(self, a, b):
        ra = self.find(a)
        rb = self.find(b)
        if ra == rb:
            return ra
        # Family id is the smallest member id; keep it as the DSU root.
        if rb < ra:
            ra, rb = rb, ra
        self.parent[rb] = ra
        return ra


def solve_quotas(family_counts, forced_uses, quotas, strata):
    """Exact lexicographically-smallest quota solver.

    ``family_counts[i][s]`` is the number of members of family ``i`` in
    stratum ``s``; ``forced_uses[i]`` is ``None`` or a forced
    ``train``/``eval`` use.  Families must be ordered by their smallest
    member id, which makes the first depth-first solution identical to the
    lexicographically smallest per-sample use vector under
    ``train < eval < unused``.

    The search is memoized and pruned against suffix capacity.  It never
    enumerates all ``3 ** n`` assignments and returns search evidence via the
    ``stats`` block of the result.
    """
    families = len(family_counts)
    strata_count = len(strata)
    counts = [[row.get(strata[s], 0) for s in range(strata_count)]
              for row in family_counts]
    forced = [None if use is None else USE_ORDER[use] for use in forced_uses]
    need_train = [quotas[strata[s]]['train'] for s in range(strata_count)]
    need_eval = [quotas[strata[s]]['eval'] for s in range(strata_count)]
    # suffix[s][i] = total members in stratum s across families i..n-1.
    suffix = [[0] * strata_count for _ in range(families + 1)]
    for i in range(families - 1, -1, -1):
        for s in range(strata_count):
            suffix[i][s] = suffix[i + 1][s] + counts[i][s]

    memo = set()
    stats = {'nodes': 0, 'memo_hits': 0, 'pruned': 0,
             'full_enumeration': 3 ** families}

    def dfs(index, rem_train, rem_eval):
        stats['nodes'] += 1
        if index == families:
            return [] if not any(rem_train) and not any(rem_eval) else None
        key = (index, tuple(rem_train), tuple(rem_eval))
        if key in memo:
            stats['memo_hits'] += 1
            return None
        for s in range(strata_count):
            available = suffix[index][s]
            if rem_train[s] > available or rem_eval[s] > available \
                    or rem_train[s] + rem_eval[s] > available:
                memo.add(key)
                stats['pruned'] += 1
                return None
        for use in (0, 1, 2):
            if forced[index] is not None and use != forced[index]:
                continue
            next_train = list(rem_train)
            next_eval = list(rem_eval)
            if use == 0:
                for s in range(strata_count):
                    next_train[s] -= counts[index][s]
                if any(value < 0 for value in next_train):
                    continue
            elif use == 1:
                for s in range(strata_count):
                    next_eval[s] -= counts[index][s]
                if any(value < 0 for value in next_eval):
                    continue
            tail = dfs(index + 1, next_train, next_eval)
            if tail is not None:
                return [use] + tail
        memo.add(key)
        return None

    vector = dfs(0, need_train, need_eval)
    if vector is None:
        return None, stats
    return [USE_BY_INDEX[value] for value in vector], stats


# ------------------------------------------------------------------- warehouse
class SplitStore:
    """Handle to an on-disk leakage-safe split warehouse."""

    def __init__(self, path, *, precommit_hook=None, observer=None):
        self.path = str(path)
        self.precommit_hook = precommit_hook
        self.observer = observer
        target = Path(self.path)
        if not target.is_file():
            raise SplitError('not a split warehouse database: ' + self.path)
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            try:
                table_rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            except sqlite3.DatabaseError as exc:
                raise SplitError(
                    'malformed split warehouse database: ' + self.path) from exc
            tables = {name for (name,) in table_rows}
            required = {'meta', 'samples', 'edges', 'family_member', 'plans',
                        'deliveries', 'sample_use', 'receipts'}
            if not required <= tables:
                raise SplitError(
                    'malformed split warehouse database: ' + self.path)
            if conn.execute(
                    "SELECT 1 FROM meta WHERE key='schema_version'").fetchone() is None:
                raise SplitError(
                    'malformed split warehouse database: ' + self.path)
        finally:
            conn.close()

    def _emit(self, event, *args):
        if self.observer is not None:
            self.observer(event, *args)

    def _trace(self, conn):
        if self.observer is not None:
            conn.set_trace_callback(lambda sql: self.observer('stmt', sql))

    @staticmethod
    def _is_busy(exc):
        message = str(exc).lower()
        return 'locked' in message or 'busy' in message

    # -------------------------------------------------------------- lifecycle
    @staticmethod
    def initialize(path):
        """Create a brand new warehouse; an existing path is refused."""
        target = Path(path)
        if target.exists() or target.is_symlink():
            raise SplitError('database already exists: ' + str(target))
        conn = None
        try:
            conn = sqlite3.connect(target, timeout=0, isolation_level=None)
            conn.executescript(_SCHEMA)
            try:
                mode = conn.execute('PRAGMA journal_mode=WAL').fetchone()[0]
            except sqlite3.Error as exc:
                raise SplitError(
                    'could not enable WAL journal mode: ' + str(exc)) from exc
            if mode not in ('wal', 'memory'):
                raise SplitError(
                    'could not enable WAL journal mode: %s' % mode)
            conn.execute('PRAGMA synchronous=NORMAL')
            conn.execute('BEGIN IMMEDIATE')
            conn.executemany(
                'INSERT INTO meta(key, value) VALUES(?, ?)',
                [('schema_version', '1'), ('batch_seq', '0'),
                 ('commit_seq', '0')],
            )
            conn.execute('COMMIT')
        except Exception:
            if conn is not None:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                conn.close()
            for suffix in ('', '-wal', '-shm'):
                stale = Path(str(target) + suffix)
                if stale.exists():
                    stale.unlink()
            raise
        else:
            conn.close()
        return SplitStore(target)

    def _connect_write(self):
        return sqlite3.connect(self.path, timeout=0, isolation_level=None)

    def _connect_read(self):
        return sqlite3.connect(self.path, timeout=30, isolation_level=None)

    @staticmethod
    def _meta(conn, key):
        return conn.execute(
            'SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0]

    # ------------------------------------------------------------------ ingest
    def ingest(self, batch):
        """Validate and atomically ingest one batch; return the summary."""
        normalized = validate_batch(batch)
        conn = None
        try:
            conn = self._connect_write()
            self._trace(conn)
            conn.execute('BEGIN IMMEDIATE')
            return self._ingest_locked(conn, normalized)
        except sqlite3.OperationalError as exc:
            if self._is_busy(exc):
                raise SplitBusy(str(exc)) from exc
            raise
        except BaseException:
            if conn is not None:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            raise
        finally:
            if conn is not None:
                conn.close()

    def _ingest_locked(self, conn, normalized):
        existing = {
            row[0]: row for row in conn.execute(
                'SELECT sample_id, content_fp, specimen_id, canon_key,'
                ' cell_count FROM samples')
        }
        total_samples = len(existing)
        total_cells = sum(row[4] for row in existing.values())
        strata = {
            value for (value,) in conn.execute('SELECT DISTINCT stratum FROM samples')
        }

        fresh = []
        repeated = 0
        for record in normalized:
            row = existing.get(record['sample_id'])
            if row is not None:
                if row[1] != record['content_fp']:
                    raise SplitError(
                        'sample_id already stored with different content: '
                        + record['sample_id'])
                repeated += 1
                continue
            fresh.append(record)

        if fresh:
            if total_samples + len(fresh) > MAX_SAMPLES:
                raise SplitError(
                    'sample limit %d exceeded; the whole batch is rejected'
                    % MAX_SAMPLES)
            new_cells = sum(record['cell_count'] for record in fresh)
            if total_cells + new_cells > MAX_TOTAL_CELLS:
                raise SplitError(
                    'cumulative cell limit %d exceeded; the whole batch is'
                    ' rejected' % MAX_TOTAL_CELLS)
            new_strata = {record['stratum'] for record in fresh}
            if len(strata | new_strata) > MAX_STRATA:
                raise SplitError(
                    'stratum limit %d exceeded; the whole batch is rejected'
                    % MAX_STRATA)

        batch_seq = int(self._meta(conn, 'batch_seq'))
        if fresh:
            batch_seq += 1
        # A spanning forest of explainable edges: each new sample gets at most
        # one specimen edge and one duplicate edge, linking it to the earliest
        # known member of each equivalence class.
        first_specimen = {
            specimen: sample_id for sample_id, _fp, specimen, _canon, _cells
            in conn.execute(
                'SELECT sample_id, content_fp, specimen_id, canon_key,'
                ' cell_count FROM samples ORDER BY sample_id')
        }
        first_canon = {
            canon: sample_id for sample_id, canon in conn.execute(
                'SELECT sample_id, canon_key FROM samples ORDER BY sample_id')
        }
        dsu = _DSU()
        for sample_id, family_id in conn.execute(
                'SELECT sample_id, family_id FROM family_member'):
            dsu.add(sample_id)
            dsu.union(sample_id, family_id)

        new_edges = []
        for record in sorted(fresh, key=lambda item: item['sample_id']):
            sample_id = record['sample_id']
            dsu.add(sample_id)
            peer = first_specimen.get(record['specimen_id'])
            if peer is not None:
                a, b = sorted((peer, sample_id))
                new_edges.append((a, b, EDGE_SPECIMEN, batch_seq))
                dsu.union(peer, sample_id)
            else:
                first_specimen[record['specimen_id']] = sample_id
            peer = first_canon.get(record['canon_key'])
            if peer is not None:
                a, b = sorted((peer, sample_id))
                new_edges.append((a, b, EDGE_DUPLICATE, batch_seq))
                dsu.union(peer, sample_id)
            else:
                first_canon[record['canon_key']] = sample_id

        for record in fresh:
            conn.execute(
                'INSERT INTO samples(sample_id, specimen_id, stratum,'
                ' connectivity, height, width, cell_count, canon_key,'
                ' canon_transform, content_fp, grid_json, batch_seq)'
                ' VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (record['sample_id'], record['specimen_id'], record['stratum'],
                 record['connectivity'], record['height'], record['width'],
                 record['cell_count'], record['canon_key'],
                 record['canon_transform'], record['content_fp'],
                 record['grid_json'], batch_seq))
        conn.executemany(
            'INSERT OR IGNORE INTO edges(sample_a, sample_b, kind, batch_seq)'
            ' VALUES(?, ?, ?, ?)', new_edges)

        # Re-materialize the family index for every component touched by the
        # batch; family ids are the smallest member sample id.
        touched_roots = {dsu.find(record['sample_id']) for record in fresh}
        for root in touched_roots:
            family_id = root
            members = [node for node in dsu.parent if dsu.find(node) == root]
            for member in members:
                conn.execute(
                    'INSERT INTO family_member(sample_id, family_id) VALUES(?, ?)'
                    ' ON CONFLICT(sample_id) DO UPDATE SET family_id=excluded.family_id',
                    (member, family_id))

        conn.execute("UPDATE meta SET value=? WHERE key='batch_seq'",
                     (str(batch_seq),))
        summary = {
            'result': 'ingested',
            'batch_seq': batch_seq,
            'ingested': len(fresh),
            'repeated': repeated,
            'totals': self._totals_locked(conn),
        }
        if self.precommit_hook is not None:
            self.precommit_hook('ingest', summary)
        conn.execute('COMMIT')
        return summary

    @staticmethod
    def _totals_locked(conn):
        sample_count = conn.execute(
            'SELECT COUNT(*) FROM samples').fetchone()[0]
        total_cells = conn.execute(
            'SELECT COALESCE(SUM(cell_count), 0) FROM samples').fetchone()[0]
        strata = [value for (value,) in conn.execute(
            'SELECT DISTINCT stratum FROM samples ORDER BY stratum')]
        return {'samples': sample_count, 'cells': total_cells,
                'strata': strata,
                'limits': {'max_samples': MAX_SAMPLES,
                           'max_strata': MAX_STRATA,
                           'max_cells': MAX_TOTAL_CELLS}}

    # ------------------------------------------------------------ family reads
    def _load_families(self, conn, sample_ids=None):
        """Load current family state from scalar/relational indexes only."""
        rows = conn.execute(
            'SELECT fm.sample_id, fm.family_id, s.specimen_id, s.stratum '
            'FROM family_member fm JOIN samples s ON s.sample_id=fm.sample_id'
        ).fetchall()
        families = {}
        for sample_id, family_id, specimen_id, stratum in rows:
            info = families.setdefault(family_id, {
                'family_id': family_id, 'members': [],
                'stratum_counts': {}, 'specimens': set()})
            info['members'].append(sample_id)
            info['specimens'].add(specimen_id)
            info['stratum_counts'][stratum] = \
                info['stratum_counts'].get(stratum, 0) + 1
        uses = {
            sample_id: use for sample_id, use in conn.execute(
                'SELECT sample_id, use_name FROM sample_use')
        }
        for info in families.values():
            info['members'].sort()
            train = sorted(sid for sid in info['members']
                           if uses.get(sid) == USE_TRAIN)
            eval_ = sorted(sid for sid in info['members']
                           if uses.get(sid) == USE_EVAL)
            info['uses'] = {USE_TRAIN: train, USE_EVAL: eval_}
            info['contaminated'] = bool(train and eval_)
        if sample_ids is not None:
            wanted = set(sample_ids)
            families = {
                family_id: info for family_id, info in families.items()
                if wanted.intersection(info['members'])
            }
        return dict(sorted(families.items()))

    def _contamination_path(self, conn, family_id, members, uses=None):
        if uses is None:
            uses = {
                sample_id: use for sample_id, use in conn.execute(
                    'SELECT sample_id, use_name FROM sample_use')}
        train = next(sid for sid in members if uses.get(sid) == USE_TRAIN)
        eval_ = next(sid for sid in members if uses.get(sid) == USE_EVAL)
        adjacency = {sid: [] for sid in members}
        member_set = set(members)
        placeholders = ','.join('?' for _ in members)
        for a, b, kind in conn.execute(
                'SELECT sample_a, sample_b, kind FROM edges '
                'WHERE sample_a IN (%s) OR sample_b IN (%s)'
                % (placeholders, placeholders), members + members):
            if a in member_set and b in member_set:
                adjacency[a].append((b, kind))
                adjacency[b].append((a, kind))
        queue = [train]
        parent = {train: None}
        while queue:
            current = queue.pop(0)
            if current == eval_:
                break
            for neighbor, kind in adjacency[current]:
                if neighbor not in parent:
                    parent[neighbor] = (current, kind)
                    queue.append(neighbor)
        path = []
        cursor = eval_
        while parent[cursor] is not None:
            previous, kind = parent[cursor]
            path.append({'from': previous, 'to': cursor, 'kind': kind})
            cursor = previous
        path.reverse()
        return {'family_id': family_id,
                'train_sample': train, 'eval_sample': eval_, 'path': path}

    # -------------------------------------------------------------------- plan
    def plan(self, request):
        """Validate, solve and persist a proposal; never commits a usage."""
        self._validate_request_shape(request)
        fingerprint = request_fingerprint(request)
        conn = None
        try:
            conn = self._connect_write()
            self._trace(conn)
            conn.execute('BEGIN IMMEDIATE')
            return self._plan_locked(conn, request, fingerprint)
        except sqlite3.OperationalError as exc:
            if self._is_busy(exc):
                raise SplitBusy(str(exc)) from exc
            raise
        except BaseException:
            if conn is not None:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            raise
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _validate_request_shape(request):
        if not isinstance(request, dict) or set(request) != {
                'plan_id', 'sample_ids', 'quotas'}:
            raise SplitError(
                'request must contain exactly plan_id, sample_ids, quotas')
        try:
            ident(request['plan_id'])
        except ValueError as exc:
            raise SplitError(str(exc)) from exc
        sample_ids = request['sample_ids']
        if not isinstance(sample_ids, list) or not sample_ids:
            raise SplitError('sample_ids must be a nonempty array')
        if len(sample_ids) != len(set(sample_ids)):
            raise SplitError('sample_ids must not contain duplicates')
        quotas = request['quotas']
        if not isinstance(quotas, dict) or not quotas:
            raise SplitError('quotas must be a nonempty object')
        for stratum, pair in quotas.items():
            try:
                ident(stratum)
            except ValueError as exc:
                raise SplitError(str(exc)) from exc
            if not isinstance(pair, dict) or set(pair) != {'train', 'eval'}:
                raise SplitError(
                    'each quota must contain exactly train and eval')
            _strict_int(pair['train'], 'train quota')
            _strict_int(pair['eval'], 'eval quota')
            if pair['train'] < 0 or pair['eval'] < 0:
                raise SplitError('quotas must be nonnegative integers')

    def _plan_locked(self, conn, request, fingerprint):
        plan_id = request['plan_id']
        cached = conn.execute(
            'SELECT solution_json, state FROM plans WHERE plan_id=?',
            (plan_id,)).fetchone()
        if cached is not None:
            stored_solution, state = cached
            stored_fp = conn.execute(
                'SELECT fingerprint FROM plans WHERE plan_id=?',
                (plan_id,)).fetchone()[0]
            if stored_fp != fingerprint:
                raise SplitError(
                    'plan_id already used with a different request: ' + plan_id)
            return {'result': 'planned', 'idempotent': True,
                    'state': state, **json.loads(stored_solution)}

        known = {sid for (sid,) in conn.execute('SELECT sample_id FROM samples')}
        unknown = [sid for sid in request['sample_ids'] if sid not in known]
        if unknown:
            raise SplitError(
                'unknown sample_id: ' + ', '.join(sorted(unknown)))
        strata = [value for (value,) in conn.execute(
            'SELECT DISTINCT stratum FROM samples ORDER BY stratum')]
        if set(request['quotas']) != set(strata):
            raise SplitError(
                'quotas keys must be exactly the current strata: '
                + ', '.join(strata))

        families = self._load_families(conn, request['sample_ids'])
        if len(families) > MAX_PLAN_FAMILIES:
            raise SplitError(
                'request expands to %d families; limit is %d'
                % (len(families), MAX_PLAN_FAMILIES))
        ordered = [families[fid] for fid in sorted(families)]

        contaminated = [
            self._contamination_path(conn, info['family_id'], info['members'])
            for info in ordered if info['contaminated']
        ]
        if contaminated:
            # Business outcome, not a protocol error: no plan row is written,
            # so the plan_id stays free and the bridging facts remain stored.
            return {'result': 'contaminated',
                    'families': [self._public_family(info) for info in ordered],
                    'paths': contaminated}

        forced_uses = []
        for info in ordered:
            if info['uses'][USE_TRAIN]:
                forced_uses.append(USE_TRAIN)
            elif info['uses'][USE_EVAL]:
                forced_uses.append(USE_EVAL)
            else:
                forced_uses.append(None)
        family_counts = [info['stratum_counts'] for info in ordered]
        uses, stats = solve_quotas(
            family_counts, forced_uses, request['quotas'], strata)
        if uses is None:
            return {'result': 'infeasible',
                    'families': [self._public_family(info) for info in ordered],
                    'quotas': request['quotas'],
                    'search': stats}

        family_solution = []
        sample_uses = {}
        for info, use in zip(ordered, uses):
            entry = {
                'family_id': info['family_id'],
                'members': list(info['members']),
                'stratum_counts': dict(info['stratum_counts']),
                'use': use,
                'forced': use in (USE_TRAIN, USE_EVAL)
                and forced_uses[len(family_solution)] == use,
                'delivered': {
                    USE_TRAIN: list(info['uses'][USE_TRAIN]),
                    USE_EVAL: list(info['uses'][USE_EVAL])},
            }
            family_solution.append(entry)
            for member in info['members']:
                sample_uses[member] = use
        expanded_samples = sorted(sample_uses)
        use_vector = [sample_uses[sid] for sid in expanded_samples]
        # Exact quota verification before the proposal is persisted.
        observed = {stratum: {USE_TRAIN: 0, USE_EVAL: 0} for stratum in strata}
        for entry in family_solution:
            if entry['use'] == USE_UNUSED:
                continue
            for stratum, count in entry['stratum_counts'].items():
                observed[stratum][entry['use']] += count
        for stratum in strata:
            if observed[stratum][USE_TRAIN] != request['quotas'][stratum]['train'] \
                    or observed[stratum][USE_EVAL] != request['quotas'][stratum]['eval']:
                raise SplitError('internal quota verification failure')

        solution = {
            'plan_id': plan_id,
            'families': family_solution,
            'sample_uses': {sid: sample_uses[sid] for sid in expanded_samples},
            'use_vector': use_vector,
            'expanded_samples': expanded_samples,
            'quotas': request['quotas'],
            'strata': strata,
            'basis': [
                {'family_id': entry['family_id'],
                 'forced': entry['forced'],
                 'delivered': entry['delivered']}
                for entry in family_solution],
            'search': stats,
        }
        batch_seq = int(self._meta(conn, 'batch_seq'))
        conn.execute(
            'INSERT INTO plans(plan_id, fingerprint, state, request_json,'
            ' solution_json, batch_seq) VALUES(?, ?, ?, ?, ?, ?)',
            (plan_id, fingerprint, 'proposed',
             json.dumps(request, sort_keys=True, ensure_ascii=False,
                        allow_nan=False),
             json.dumps(solution, sort_keys=True, ensure_ascii=False,
                        allow_nan=False), batch_seq))
        if self.precommit_hook is not None:
            self.precommit_hook('plan', solution)
        conn.execute('COMMIT')
        return {'result': 'planned', 'idempotent': False, **solution}

    @staticmethod
    def _public_family(info):
        return {'family_id': info['family_id'],
                'members': list(info['members']),
                'stratum_counts': dict(info['stratum_counts']),
                'uses': {USE_TRAIN: list(info['uses'][USE_TRAIN]),
                         USE_EVAL: list(info['uses'][USE_EVAL])},
                'contaminated': info['contaminated']}

    # ------------------------------------------------------------------ commit
    def commit(self, plan_id):
        """Commit a proposal after re-checking current family state."""
        try:
            ident(plan_id)
        except ValueError as exc:
            raise SplitError(str(exc)) from exc
        conn = None
        try:
            conn = self._connect_write()
            self._trace(conn)
            conn.execute('BEGIN IMMEDIATE')
            return self._commit_locked(conn, plan_id)
        except sqlite3.OperationalError as exc:
            if self._is_busy(exc):
                raise SplitBusy(str(exc)) from exc
            raise
        except BaseException:
            if conn is not None:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            raise
        finally:
            if conn is not None:
                conn.close()

    def _commit_locked(self, conn, plan_id):
        row = conn.execute(
            'SELECT state, solution_json FROM plans WHERE plan_id=?',
            (plan_id,)).fetchone()
        if row is None:
            raise UnknownPlan('unknown plan_id: ' + plan_id)
        state, solution_json = row
        solution = json.loads(solution_json)
        if state == 'committed':
            receipt = json.loads(conn.execute(
                'SELECT receipt_json FROM receipts WHERE plan_id=?',
                (plan_id,)).fetchone()[0])
            return {'result': 'committed', 'idempotent': True,
                    'plan_id': plan_id, 'receipt': receipt}

        current = self._load_families(
            conn, [sid for entry in solution['families']
                   for sid in entry['members']])
        reasons = []
        for entry in solution['families']:
            planned_family = entry['family_id']
            planned_members = set(entry['members'])
            current_info = current.get(planned_family)
            if current_info is None or \
                    set(current_info['members']) != planned_members:
                reasons.append({
                    'family_id': planned_family,
                    'reason': 'family_membership_changed',
                    'planned_members': sorted(planned_members),
                    'current_members': None if current_info is None
                    else list(current_info['members'])})
                continue
            if entry['use'] == USE_UNUSED:
                # Even an unused family must be rejected once later bridging
                # has contaminated it: committing a stale proposal must not
                # silently ratify a leakage state the plan never observed.
                if current_info['contaminated']:
                    reasons.append({
                        'family_id': planned_family,
                        'reason': 'family_contaminated',
                        'path': self._contamination_path(
                            conn, planned_family, current_info['members'])})
                continue
            opposite = USE_EVAL if entry['use'] == USE_TRAIN else USE_TRAIN
            conflicts = current_info['uses'][opposite]
            if conflicts:
                reasons.append({
                    'family_id': planned_family,
                    'reason': 'opposite_use_committed',
                    'opposite': opposite,
                    'samples': conflicts})
            if current_info['contaminated']:
                reasons.append({
                    'family_id': planned_family,
                    'reason': 'family_contaminated',
                    'path': self._contamination_path(
                        conn, planned_family, current_info['members'])})
        if reasons:
            # Explicit business result: rollback every tentative check and
            # record nothing.
            conn.rollback()
            return {'result': 'stale_plan', 'plan_id': plan_id,
                    'reasons': reasons}

        commit_seq = int(self._meta(conn, 'commit_seq')) + 1
        deliveries = {USE_TRAIN: [], USE_EVAL: []}
        for entry in solution['families']:
            if entry['use'] not in (USE_TRAIN, USE_EVAL):
                continue
            for sample_id in entry['members']:
                conn.execute(
                    'INSERT OR IGNORE INTO deliveries(plan_id, sample_id,'
                    ' use_name) VALUES(?, ?, ?)',
                    (plan_id, sample_id, entry['use']))
                # Same-use promises from other plans are compatible; an
                # opposite use would have produced stale_plan above.
                conn.execute(
                    'INSERT OR IGNORE INTO sample_use(sample_id, use_name)'
                    ' VALUES(?, ?)', (sample_id, entry['use']))
                deliveries[entry['use']].append(sample_id)
        for use in deliveries:
            deliveries[use].sort()
        receipt = {
            'plan_id': plan_id,
            'commit_seq': commit_seq,
            'committed_at': datetime.now(timezone.utc).isoformat(),
            'train': deliveries[USE_TRAIN],
            'eval': deliveries[USE_EVAL],
            'quotas': solution['quotas'],
        }
        conn.execute(
            "UPDATE plans SET state='committed' WHERE plan_id=?", (plan_id,))
        conn.execute(
            'INSERT INTO receipts(plan_id, receipt_json) VALUES(?, ?)',
            (plan_id, json.dumps(receipt, sort_keys=True, ensure_ascii=False,
                                 allow_nan=False)))
        conn.execute("UPDATE meta SET value=? WHERE key='commit_seq'",
                     (str(commit_seq),))
        if self.precommit_hook is not None:
            self.precommit_hook('commit', receipt)
        conn.execute('COMMIT')
        return {'result': 'committed', 'idempotent': False,
                'plan_id': plan_id, 'receipt': receipt}

    # ------------------------------------------------------------------ export
    def export(self, plan_id):
        """Deliver original samples of a committed plan; reads payloads here."""
        try:
            ident(plan_id)
        except ValueError as exc:
            raise SplitError(str(exc)) from exc
        conn = None
        try:
            conn = self._connect_read()
            self._trace(conn)
            conn.execute('BEGIN')
            row = conn.execute(
                'SELECT state, solution_json FROM plans WHERE plan_id=?',
                (plan_id,)).fetchone()
            if row is None:
                raise UnknownPlan('unknown plan_id: ' + plan_id)
            state, solution_json = row
            if state != 'committed':
                raise SplitError(
                    'plan has not been committed; export refused: ' + plan_id)
            solution = json.loads(solution_json)
            receipt = json.loads(conn.execute(
                'SELECT receipt_json FROM receipts WHERE plan_id=?',
                (plan_id,)).fetchone()[0])
            groups = {USE_TRAIN: [], USE_EVAL: []}
            wanted = [sid for sid in solution['expanded_samples']
                      if solution['sample_uses'][sid] in (USE_TRAIN, USE_EVAL)]
            for sample_id in wanted:
                record = conn.execute(
                    'SELECT specimen_id, stratum, connectivity, grid_json'
                    ' FROM samples WHERE sample_id=?', (sample_id,)).fetchone()
                # The only post-ingest path allowed to decode a grid payload.
                self._emit('payload_decode', 'export', sample_id)
                groups[solution['sample_uses'][sample_id]].append({
                    'sample_id': sample_id,
                    'specimen_id': record[0],
                    'stratum': record[1],
                    'connectivity': record[2],
                    'grid': json.loads(record[3]),
                })
            for use in groups:
                groups[use].sort(key=lambda item: item['sample_id'])
            conn.execute('COMMIT')
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            if conn is not None:
                conn.close()
        return {'schema_version': 1, 'plan_id': plan_id,
                'quotas': solution['quotas'], 'receipt': receipt,
                'train': groups[USE_TRAIN], 'eval': groups[USE_EVAL]}

    # ------------------------------------------------------------------ status
    def status(self):
        """Snapshot-consistent families, counts, commitments and plans."""
        conn = None
        try:
            conn = self._connect_read()
            self._trace(conn)
            conn.execute('BEGIN')
            families_map = self._load_families(conn)
            result_families = []
            for family_id in sorted(families_map):
                info = families_map[family_id]
                public = self._public_family(info)
                public['contamination_path'] = (
                    self._contamination_path(
                        conn, family_id, info['members'])
                    if info['contaminated'] else None)
                result_families.append(public)
            plans = [
                {'plan_id': plan_id, 'state': state}
                for plan_id, state in conn.execute(
                    'SELECT plan_id, state FROM plans ORDER BY plan_id')]
            totals = self._totals_locked(conn)
            conn.execute('COMMIT')
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            if conn is not None:
                conn.close()
        return {'schema_version': 1, 'totals': totals,
                'families': result_families, 'plans': plans}


# ------------------------------------------------------------ module-level API
def init_store(path):
    """Create a new split warehouse database."""
    return SplitStore.initialize(path)


def ingest_batch(path, batch, **kwargs):
    """Open an existing warehouse and ingest one batch."""
    return SplitStore(path, **kwargs).ingest(batch)


def plan_request(path, request, **kwargs):
    """Open an existing warehouse and record or replay a plan proposal."""
    return SplitStore(path, **kwargs).plan(request)


def commit_plan(path, plan_id, **kwargs):
    """Open an existing warehouse and commit (or re-check) one plan."""
    return SplitStore(path, **kwargs).commit(plan_id)


def export_plan(path, plan_id, **kwargs):
    """Open an existing warehouse and export one committed plan."""
    return SplitStore(path, **kwargs).export(plan_id)


def store_status(path, **kwargs):
    """Open an existing warehouse and return one snapshot status report."""
    return SplitStore(path, **kwargs).status()
