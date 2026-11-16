"""Persistent blind double-annotation collection pipeline.

The pipeline distributes initial binary rasters to independent re-annotators
who never see each other's answers, collects exactly two raw annotations per
normal item from two different qualified workers, and interleaves the work
with sentinel ("gold") quality probes.  Only the Python standard library and
:mod:`sqlite3` are used.

Design properties
-----------------

* Campaign inputs (every initial grid and every sentinel truth) are validated
  once with the repository's single-raster contract and stored immutably in
  the database; deleting or editing the source file afterwards cannot change
  a task.
* Workers, leases (quota occupancy) and submissions are first-class rows.
  The two-vote cap is evaluated from *valid* submissions plus *unexpired open
  leases*, so an expired slot is automatically reusable while a stale token
  can never pollute a re-dispatched slot.
* Validity is a column on the submission, not an export-time filter: when a
  worker fails a sentinel, the disqualification, the invalidation of their
  normal votes, lease state and the submit receipt all commit in one
  ``BEGIN IMMEDIATE`` transaction.  The dispatcher and the exporter therefore
  always agree on which samples are complete.
* Answer matrices are large opaque payloads.  No claim/submit/status query
  ever decodes another worker's ``grid_json``; disqualification is a scalar
  ``UPDATE ... WHERE worker_id = ?`` that never touches a payload column.
* A monotonic logical clock (``meta.clock``) records the largest ``T`` of any
  successful call, including ``no_work``.  Protocol, conflict, expiry and
  busy failures have no side effects and never advance the clock.  A
  successful submit is idempotent on its token: a normalized retry returns
  the first receipt forever, even after expiry, clock advance or
  disqualification, and can never resurrect an invalidated vote.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from pathlib import Path

from .common import ident
from .model import analyze

# --------------------------------------------------------------------- bounds
MAX_NORMAL_ITEMS = 2000
MAX_SENTINELS = 100
MIN_LEASE_SECONDS = 1
MAX_LEASE_SECONDS = 3600
MAX_TOTAL_INPUT_CELLS = 1_000_000
MIN_TIME = 0
MAX_TIME = 10**12

KIND_NORMAL = 'normal'
KIND_SENTINEL = 'sentinel'
LEASE_OPEN = 'open'
LEASE_SUBMITTED = 'submitted'
LEASE_EXPIRED = 'expired'

NO_WORK_GOLD_EXHAUSTED = 'gold_exhausted'
NO_WORK_NONE_AVAILABLE = 'none_available'

_SCHEMA = """
CREATE TABLE meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE items(
    item_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('normal', 'sentinel')),
    connectivity INTEGER NOT NULL,
    height INTEGER NOT NULL,
    width INTEGER NOT NULL,
    grid_json TEXT NOT NULL,
    truth_json TEXT
) WITHOUT ROWID;
CREATE TABLE workers(
    worker_id TEXT PRIMARY KEY,
    disqualified INTEGER NOT NULL DEFAULT 0,
    normal_count INTEGER NOT NULL DEFAULT 0,
    sentinel_due INTEGER NOT NULL DEFAULT 1
) WITHOUT ROWID;
CREATE TABLE leases(
    token TEXT PRIMARY KEY,
    worker_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('normal', 'sentinel')),
    issued_at INTEGER NOT NULL,
    deadline INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('open', 'submitted', 'expired'))
) WITHOUT ROWID;
CREATE INDEX idx_leases_worker ON leases(worker_id, state);
CREATE INDEX idx_leases_item ON leases(item_id, state, deadline);
-- Hard representation of "at most one unexpired lease per worker": expired
-- leases are transitioned out of 'open' before a new one is inserted.
CREATE UNIQUE INDEX idx_lease_one_open_per_worker
    ON leases(worker_id) WHERE state = 'open';
CREATE TABLE submissions(
    token TEXT PRIMARY KEY,
    worker_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('normal', 'sentinel')),
    passed INTEGER,
    valid INTEGER NOT NULL DEFAULT 1,
    submitted_at INTEGER NOT NULL,
    grid_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    receipt_json TEXT NOT NULL
) WITHOUT ROWID;
CREATE UNIQUE INDEX idx_sub_normal_once
    ON submissions(worker_id, item_id) WHERE kind = 'normal';
CREATE INDEX idx_sub_item_valid ON submissions(item_id, kind, valid);
CREATE INDEX idx_sub_worker_kind ON submissions(worker_id, kind);
"""


class CollectionError(ValueError):
    """Base class for collection input and protocol errors (exit code 2)."""


class CollectionBusy(CollectionError):
    """A write lost a concurrency race; the call had no side effects."""


class UnknownLease(CollectionError):
    """The token has never been issued by this database."""


class LeaseConflict(CollectionError):
    """The token does not belong to the worker, or was already consumed."""


class LeaseExpired(CollectionError):
    """The lease deadline passed (T < deadline is required)."""


class WorkerDisqualified(CollectionError):
    """The worker permanently failed a sentinel and may no longer participate."""


class StaleClock(CollectionError):
    """A successful call may not move the persistent logical clock backwards."""


# ---------------------------------------------------------------- validation
def _strict_int(value, name):
    if type(value) is not int:
        raise CollectionError('%s must be an integer (booleans are rejected)' % name)
    return value


def _check_time(value):
    value = _strict_int(value, 'T')
    if not MIN_TIME <= value <= MAX_TIME:
        raise CollectionError('T must lie within [%d, %d]' % (MIN_TIME, MAX_TIME))
    return value


def _check_raster(grid, connectivity):
    """Validate a raster with the repository's single-raster contract."""
    if type(connectivity) is not int or connectivity not in (4, 8):
        raise CollectionError('connectivity must be 4 or 8 (booleans rejected)')
    if not isinstance(grid, list) or not grid:
        raise CollectionError('grid must be a nonempty array')
    if len(grid) > 500:
        raise CollectionError('grid height must not exceed 500 rows')
    first = grid[0]
    if not isinstance(first, list) or not first or len(first) > 500:
        raise CollectionError('grid rows must be nonempty arrays of at most 500 cells')
    width = len(first)
    height = len(grid)
    if height * width > 100_000:
        raise CollectionError('raster too large')
    for row in grid:
        if not isinstance(row, list) or len(row) != width:
            raise CollectionError('grid must be rectangular')
        for value in row:
            if value is not None and (type(value) is not int or value not in (0, 1)):
                raise CollectionError(
                    'grid cells must be 0, 1 or null (booleans are rejected)')
    # Reuse the exact production analyzer so every single-raster rule
    # (connectivity, bounds, cell vocabulary, empty-object legality) matches.
    analyze({'connectivity': connectivity, 'grid': grid})
    return height, width


def _canonical(grid):
    return json.dumps(grid, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False)


def _fingerprint(grid):
    return json.dumps(grid, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def validate_campaign(campaign):
    """Validate a campaign payload and return its normalized description.

    The result carries every raster as parsed Python lists plus canonical
    JSON text, ready for immutable insertion.  Any violation raises
    :class:`CollectionError` before a database file is touched.
    """
    if not isinstance(campaign, dict) or set(campaign) != {
        'lease_seconds', 'items', 'sentinels'
    }:
        raise CollectionError(
            'campaign must contain exactly lease_seconds, items and sentinels')
    lease_seconds = _strict_int(campaign['lease_seconds'], 'lease_seconds')
    if not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS:
        raise CollectionError(
            'lease_seconds must be within [%d, %d]'
            % (MIN_LEASE_SECONDS, MAX_LEASE_SECONDS))
    raw_items = campaign['items']
    raw_sentinels = campaign['sentinels']
    if not isinstance(raw_items, list) or not (1 <= len(raw_items) <= MAX_NORMAL_ITEMS):
        raise CollectionError('items must contain 1..%d entries' % MAX_NORMAL_ITEMS)
    if not isinstance(raw_sentinels, list) or not (1 <= len(raw_sentinels) <= MAX_SENTINELS):
        raise CollectionError('sentinels must contain 1..%d entries' % MAX_SENTINELS)

    seen_ids = set()
    normals = []
    sentinels = []
    total_cells = 0

    def take(entry, is_sentinel):
        required = {'item_id', 'connectivity', 'grid', 'truth'} if is_sentinel \
            else {'item_id', 'connectivity', 'grid'}
        if not isinstance(entry, dict) or set(entry) != required:
            raise CollectionError(
                'each %s must contain exactly %s'
                % ('sentinel' if is_sentinel else 'item',
                   ', '.join(sorted(required))))
        try:
            item_id = ident(entry['item_id'])
        except ValueError as exc:
            raise CollectionError(str(exc)) from exc
        if item_id in seen_ids:
            raise CollectionError('duplicate item_id across campaign: ' + item_id)
        seen_ids.add(item_id)
        connectivity = _strict_int(entry['connectivity'], 'connectivity')
        grid = entry['grid']
        height, width = _check_raster(grid, connectivity)
        record = {
            'item_id': item_id,
            'connectivity': connectivity,
            'height': height,
            'width': width,
            'grid': grid,
            'grid_json': _canonical(grid),
        }
        nonlocal total_cells
        total_cells += height * width
        if is_sentinel:
            truth = entry['truth']
            if not isinstance(truth, list) or len(truth) != height or any(
                    not isinstance(row, list) or len(row) != width for row in truth):
                raise CollectionError(
                    'sentinel truth must have the same shape as its grid: ' + item_id)
            # Truth obeys the same 0/1/null raster contract, and must score at
            # least one known cell.
            _check_raster(truth, connectivity)
            if all(value is None for row in truth for value in row):
                raise CollectionError(
                    'sentinel truth must contain at least one non-null cell: ' + item_id)
            record['truth'] = truth
            record['truth_json'] = _canonical(truth)
            total_cells += height * width
            sentinels.append(record)
        else:
            normals.append(record)

    for entry in raw_items:
        take(entry, False)
    for entry in raw_sentinels:
        take(entry, True)
    if total_cells > MAX_TOTAL_INPUT_CELLS:
        raise CollectionError(
            'total input matrices exceed %d cells (got %d)'
            % (MAX_TOTAL_INPUT_CELLS, total_cells))
    return {
        'lease_seconds': lease_seconds,
        'items': normals,
        'sentinels': sentinels,
        'total_cells': total_cells,
    }


def validate_answer(answer, height, width, connectivity):
    """Validate an ``{grid}`` answer against the claimed item's shape."""
    if not isinstance(answer, dict) or set(answer) != {'grid'}:
        raise CollectionError('answer must contain exactly grid')
    grid = answer['grid']
    if not isinstance(grid, list) or len(grid) != height or any(
            not isinstance(row, list) or len(row) != width for row in grid):
        raise CollectionError(
            'answer grid must match the claimed raster shape %d x %d'
            % (height, width))
    _check_raster(grid, connectivity)
    return grid


# ------------------------------------------------------------------- warehouse
class Collection:
    """Handle to an on-disk blind double-annotation database."""

    def __init__(self, path, *, precommit_hook=None, observer=None):
        self.path = str(path)
        self.precommit_hook = precommit_hook
        self.observer = observer
        target = Path(self.path)
        if not target.is_file():
            raise CollectionError('not a collection database: ' + self.path)
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            tables = {
                name for (name,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")
            }
            required = {'meta', 'items', 'workers', 'leases', 'submissions'}
            if not required <= tables:
                raise CollectionError('malformed collection database: ' + self.path)
            if conn.execute("SELECT 1 FROM meta WHERE key='clock'").fetchone() is None:
                raise CollectionError('malformed collection database: ' + self.path)
        finally:
            conn.close()

    def _emit(self, event, *args):
        if self.observer is not None:
            self.observer(event, *args)

    def _trace(self, conn):
        if self.observer is not None:
            conn.set_trace_callback(lambda sql: self.observer('stmt', sql))

    # -------------------------------------------------------------- lifecycle
    @staticmethod
    def initialize(path, campaign):
        """Create a brand new collection database; existing paths are refused."""
        target = Path(path)
        if target.exists() or target.is_symlink():
            raise CollectionError('database already exists: ' + str(target))
        normalized = validate_campaign(campaign)
        conn = None
        try:
            conn = sqlite3.connect(target, timeout=0, isolation_level=None)
            conn.executescript(_SCHEMA)
            try:
                mode = conn.execute('PRAGMA journal_mode=WAL').fetchone()[0]
            except sqlite3.Error as exc:
                raise CollectionError(
                    'could not enable WAL journal mode: ' + str(exc)) from exc
            if mode not in ('wal', 'memory'):
                raise CollectionError(
                    'could not enable WAL journal mode: %s' % mode)
            conn.execute('PRAGMA synchronous=NORMAL')
            conn.execute('BEGIN IMMEDIATE')
            conn.executemany(
                'INSERT INTO meta(key, value) VALUES(?, ?)',
                [
                    ('lease_seconds', str(normalized['lease_seconds'])),
                    ('clock', str(MIN_TIME - 1)),
                    ('token_seq', '0'),
                ],
            )
            conn.executemany(
                'INSERT INTO items(item_id, kind, connectivity, height, width,'
                ' grid_json, truth_json) VALUES(?, ?, ?, ?, ?, ?, ?)',
                [(item['item_id'], KIND_NORMAL, item['connectivity'],
                  item['height'], item['width'], item['grid_json'], None)
                 for item in normalized['items']]
                + [(item['item_id'], KIND_SENTINEL, item['connectivity'],
                    item['height'], item['width'], item['grid_json'],
                    item['truth_json'])
                   for item in normalized['sentinels']],
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
        return Collection(target)

    def _connect_write(self):
        return sqlite3.connect(self.path, timeout=0, isolation_level=None)

    @staticmethod
    def _is_busy(exc):
        message = str(exc).lower()
        return 'locked' in message or 'busy' in message

    def _clock(self, conn):
        return int(conn.execute(
            "SELECT value FROM meta WHERE key='clock'").fetchone()[0])

    def _lease_seconds(self, conn):
        return int(conn.execute(
            "SELECT value FROM meta WHERE key='lease_seconds'").fetchone()[0])

    def _next_token(self, conn):
        seq = int(conn.execute(
            "SELECT value FROM meta WHERE key='token_seq'").fetchone()[0]) + 1
        conn.execute("UPDATE meta SET value=? WHERE key='token_seq'", (str(seq),))
        # The monotonic sequence makes reuse impossible; the random suffix
        # keeps tokens unguessable across campaigns.
        return '%08d-%s' % (seq, secrets.token_hex(12))

    # ------------------------------------------------------------------- claim
    def claim(self, worker, now):
        """Claim one task for ``worker`` at logical time ``now``.

        Returns either a work dict ``{token, deadline, connectivity, grid}``
        or ``{'status': 'no_work', 'reason': ...}``.  A repeated claim while
        the worker still holds an unexpired lease returns that lease with its
        original deadline.
        """
        try:
            worker = ident(worker)
        except ValueError as exc:
            raise CollectionError(str(exc)) from exc
        now = _check_time(now)
        conn = None
        try:
            conn = self._connect_write()
            self._trace(conn)
            conn.execute('BEGIN IMMEDIATE')
            return self._claim_locked(conn, worker, now)
        except sqlite3.OperationalError as exc:
            if self._is_busy(exc):
                raise CollectionBusy(str(exc)) from exc
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

    def _claim_locked(self, conn, worker, now):
        clock = self._clock(conn)
        if now < clock:
            raise StaleClock(
                'T=%d is below the persisted clock %d' % (now, clock))

        row = conn.execute(
            "SELECT worker_id, disqualified FROM workers WHERE worker_id=?",
            (worker,)).fetchone()
        if row is None:
            # First contact: auto-register with an initial sentinel debt.
            conn.execute(
                'INSERT INTO workers(worker_id, disqualified, normal_count,'
                ' sentinel_due) VALUES(?, 0, 0, 1)', (worker,))
        elif row[1] == 1:
            raise WorkerDisqualified(
                'worker %r is permanently disqualified' % worker)

        # Repeated claim: hand back the still-unexpired lease without
        # touching its deadline; the global clock still advances.
        active = conn.execute(
            'SELECT token, deadline FROM leases WHERE worker_id=? AND state=?',
            (worker, LEASE_OPEN)).fetchone()
        if active is not None:
            token, deadline = active
            if deadline > now:
                conn.execute("UPDATE meta SET value=? WHERE key='clock'", (str(now),))
                if self.precommit_hook is not None:
                    self.precommit_hook('claim', worker, token, {'repeat': True})
                response = self._work_from_conn(conn, token)
                conn.execute('COMMIT')
                return response

        # Any previously held open lease has expired by now; move it out of
        # the partial-unique 'open' state before dispatching again.
        conn.execute(
            'UPDATE leases SET state=? WHERE worker_id=? AND state=? AND deadline <= ?',
            (LEASE_EXPIRED, worker, LEASE_OPEN, now))

        due = conn.execute(
            'SELECT sentinel_due FROM workers WHERE worker_id=?',
            (worker,)).fetchone()[0]
        if due == 1:
            choice = self._pick_sentinel(conn, worker)
            if choice is None:
                self._advance_clock_and_commit(conn, now, worker, None,
                                               NO_WORK_GOLD_EXHAUSTED)
                return {'status': 'no_work',
                        'reason': NO_WORK_GOLD_EXHAUSTED}
        else:
            choice = self._pick_normal(conn, worker, now)
            if choice is None:
                self._advance_clock_and_commit(conn, now, worker, None,
                                               NO_WORK_NONE_AVAILABLE)
                return {'status': 'no_work',
                        'reason': NO_WORK_NONE_AVAILABLE}

        item_id, kind, connectivity, _unused = choice
        token = self._next_token(conn)
        deadline = now + self._lease_seconds(conn)
        conn.execute(
            'INSERT INTO leases(token, worker_id, item_id, kind, issued_at,'
            ' deadline, state) VALUES(?, ?, ?, ?, ?, ?, ?)',
            (token, worker, item_id, kind, now, deadline, LEASE_OPEN))
        conn.execute("UPDATE meta SET value=? WHERE key='clock'", (str(now),))
        if self.precommit_hook is not None:
            self.precommit_hook('claim', worker, token,
                                {'repeat': False, 'item_id': item_id})
        response = self._work_from_conn(conn, token)
        conn.execute('COMMIT')
        return response

    def _pick_sentinel(self, conn, worker):
        """Smallest-ID sentinel this worker has never passed."""
        row = conn.execute(
            "SELECT i.item_id, i.kind, i.connectivity FROM items i "
            "WHERE i.kind = ? AND NOT EXISTS ("
            "  SELECT 1 FROM submissions s WHERE s.item_id = i.item_id "
            "  AND s.worker_id = ? AND s.kind = ? AND s.passed = 1) "
            "ORDER BY i.item_id LIMIT 1",
            (KIND_SENTINEL, worker, KIND_SENTINEL)).fetchone()
        return None if row is None else (row[0], row[1], row[2], None)

    def _pick_normal(self, conn, worker, now):
        """First string-ordered normal item with a free slot for the worker."""
        row = conn.execute(
            "SELECT i.item_id, i.kind, i.connectivity FROM items i "
            "WHERE i.kind = ? "
            # The worker can only ever give one opinion per item.
            "AND NOT EXISTS ("
            "  SELECT 1 FROM submissions s WHERE s.item_id = i.item_id "
            "  AND s.worker_id = ? AND s.kind = ?) "
            "AND ("
            "  (SELECT COUNT(*) FROM submissions s WHERE s.item_id = i.item_id "
            "   AND s.kind = ? AND s.valid = 1) + "
            "  (SELECT COUNT(*) FROM leases l WHERE l.item_id = i.item_id "
            "   AND l.kind = ? AND l.state = ? AND l.deadline > ?)"
            ") < 2 "
            "ORDER BY i.item_id LIMIT 1",
            (KIND_NORMAL, worker, KIND_NORMAL,
             KIND_NORMAL, KIND_NORMAL, LEASE_OPEN, now)).fetchone()
        return None if row is None else (row[0], row[1], row[2], None)

    def _advance_clock_and_commit(self, conn, now, worker, token, reason):
        conn.execute("UPDATE meta SET value=? WHERE key='clock'", (str(now),))
        if self.precommit_hook is not None:
            self.precommit_hook('claim', worker, token,
                                {'repeat': False, 'no_work': reason})
        conn.execute('COMMIT')

    @staticmethod
    def _work_from_conn(conn, token):
        """Load the single leased item's raster inside an open transaction.

        Only the one leased item's ``grid_json`` is decoded; the query never
        touches the submissions payload column or any sentinel truth.
        """
        row = conn.execute(
            'SELECT l.deadline, i.connectivity, i.grid_json '
            'FROM leases l JOIN items i ON i.item_id = l.item_id '
            'WHERE l.token = ?', (token,)).fetchone()
        deadline, connectivity, grid_json = row
        return {
            'token': token,
            'deadline': deadline,
            'connectivity': connectivity,
            'grid': json.loads(grid_json),
        }

    # ------------------------------------------------------------------ submit
    def submit(self, worker, token, now, answer):
        """Submit the answer for a leased task; return the success receipt."""
        try:
            worker = ident(worker)
        except ValueError as exc:
            raise CollectionError(str(exc)) from exc
        now = _check_time(now)
        if not isinstance(token, str) or not token:
            raise UnknownLease('unknown lease token')
        conn = None
        try:
            conn = self._connect_write()
            self._trace(conn)
            conn.execute('BEGIN IMMEDIATE')
            return self._submit_locked(conn, worker, token, now, answer)
        except sqlite3.OperationalError as exc:
            if self._is_busy(exc):
                raise CollectionBusy(str(exc)) from exc
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

    def _submit_locked(self, conn, worker, token, now, answer):
        lease = conn.execute(
            'SELECT worker_id, item_id, kind, deadline, state '
            'FROM leases WHERE token = ?', (token,)).fetchone()
        if lease is None:
            raise UnknownLease('unknown lease token: ' + token)
        lease_worker, item_id, kind, deadline, state = lease
        if lease_worker != worker:
            raise LeaseConflict('token %r belongs to another worker' % token)

        consumed = conn.execute(
            'SELECT fingerprint, receipt_json FROM submissions WHERE token = ?',
            (token,)).fetchone()
        if consumed is not None:
            # Idempotency wins over every later rule including expiry, clock
            # advance and disqualification, but only for the identical
            # normalized answer; a different answer is a permanent conflict.
            if answer is not None and isinstance(answer, dict) and \
                    set(answer) == {'grid'}:
                try:
                    fingerprint = _fingerprint(answer['grid'])
                except (TypeError, ValueError):
                    raise LeaseConflict(
                        'token already consumed with a different answer')
                if fingerprint != consumed[0]:
                    raise LeaseConflict(
                        'token already consumed with a different answer')
                return json.loads(consumed[1])
            raise LeaseConflict('token already consumed with a different answer')
        if state != LEASE_OPEN:
            raise LeaseConflict('lease %r is no longer open' % token)

        clock = self._clock(conn)
        if now < clock:
            raise StaleClock(
                'T=%d is below the persisted clock %d' % (now, clock))
        if not now < deadline:
            # No side effect: the token is not consumed and the clock does
            # not move; the expired slot becomes reusable by dispatch.
            raise LeaseExpired(
                'lease deadline %d reached at T=%d (T < deadline required)'
                % (deadline, now))
        worker_row = conn.execute(
            'SELECT disqualified, normal_count, sentinel_due '
            'FROM workers WHERE worker_id=?', (worker,)).fetchone()
        if worker_row is None or worker_row[0] == 1:
            raise WorkerDisqualified(
                'worker %r may not submit while disqualified' % worker)

        item = conn.execute(
            'SELECT connectivity, height, width, truth_json FROM items '
            'WHERE item_id = ?', (item_id,)).fetchone()
        connectivity, height, width, truth_json = item
        grid = validate_answer(answer, height, width, connectivity)
        grid_json = _canonical(grid)
        fingerprint = _fingerprint(grid)

        receipt = {
            'token': token,
            'worker': worker,
            'submitted_at': now,
            'kind': kind,
        }
        if kind == KIND_SENTINEL:
            truth = json.loads(truth_json)
            passed = True
            for r, truth_row in enumerate(truth):
                for c, truth_value in enumerate(truth_row):
                    if truth_value is None:
                        continue  # unknown gold cells are never scored
                    if grid[r][c] != truth_value:
                        # An answer null at a known gold cell is unequal too.
                        passed = False
            receipt['passed'] = passed
            receipt['disqualified'] = not passed
            conn.execute(
                'INSERT INTO submissions(token, worker_id, item_id, kind,'
                ' passed, valid, submitted_at, grid_json, fingerprint,'
                ' receipt_json) VALUES(?, ?, ?, ?, ?, 1, ?, ?, ?, ?)',
                (token, worker, item_id, kind, 1 if passed else 0, now,
                 grid_json, fingerprint,
                 json.dumps(receipt, sort_keys=True, ensure_ascii=False,
                            allow_nan=False)))
            if passed:
                # Debt cleared; the normal-submission counter restarts.
                conn.execute(
                    'UPDATE workers SET sentinel_due = 0, normal_count = 0 '
                    'WHERE worker_id = ?', (worker,))
            else:
                conn.execute(
                    'UPDATE workers SET disqualified = 1 WHERE worker_id = ?',
                    (worker,))
                # All of this worker's prior normal opinions leave the valid
                # vote set immediately.  This is a scalar update: no other
                # worker's payload is ever decoded.
                conn.execute(
                    "UPDATE submissions SET valid = 0 WHERE worker_id = ? "
                    "AND kind = 'normal'", (worker,))
        else:
            receipt['accepted'] = True
            conn.execute(
                'INSERT INTO submissions(token, worker_id, item_id, kind,'
                ' passed, valid, submitted_at, grid_json, fingerprint,'
                ' receipt_json) VALUES(?, ?, ?, ?, NULL, 1, ?, ?, ?, ?)',
                (token, worker, item_id, kind, now, grid_json, fingerprint,
                 json.dumps(receipt, sort_keys=True, ensure_ascii=False,
                            allow_nan=False)))
            # Every two accepted normal opinions re-open a sentinel debt.
            conn.execute(
                'UPDATE workers SET normal_count = normal_count + 1 '
                'WHERE worker_id = ?', (worker,))
            count_row = conn.execute(
                'SELECT normal_count FROM workers WHERE worker_id = ?',
                (worker,)).fetchone()
            if count_row[0] >= 2:
                conn.execute(
                    'UPDATE workers SET sentinel_due = 1 WHERE worker_id = ?',
                    (worker,))
        conn.execute(
            'UPDATE leases SET state = ? WHERE token = ?',
            (LEASE_SUBMITTED, token))
        conn.execute("UPDATE meta SET value=? WHERE key='clock'", (str(now),))
        if self.precommit_hook is not None:
            self.precommit_hook('submit', worker, token, receipt)
        conn.execute('COMMIT')
        return receipt

    # ------------------------------------------------------------------ status
    def status(self):
        """Snapshot-consistent status of workers and every normal item."""
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            self._trace(conn)
            conn.execute('BEGIN')
            clock = self._clock(conn)
            workers = []
            for worker_id, disqualified, normal_count, sentinel_due in conn.execute(
                    'SELECT worker_id, disqualified, normal_count, sentinel_due '
                    'FROM workers ORDER BY worker_id'):
                workers.append({
                    'worker_id': worker_id,
                    'qualified': disqualified == 0,
                    'disqualified': disqualified == 1,
                    'normal_count': normal_count,
                    'sentinel_due': sentinel_due,
                })
            items = []
            item_rows = conn.execute(
                "SELECT item_id FROM items WHERE kind = ? ORDER BY item_id",
                (KIND_NORMAL,)).fetchall()
            for (item_id,) in item_rows:
                valid_workers = [w for (w,) in conn.execute(
                    'SELECT worker_id FROM submissions WHERE item_id = ? '
                    "AND kind = 'normal' AND valid = 1 ORDER BY worker_id",
                    (item_id,))]
                open_leases = [
                    {'worker_id': w, 'deadline': d}
                    for w, d in conn.execute(
                        'SELECT worker_id, deadline FROM leases '
                        "WHERE item_id = ? AND kind = 'normal' AND state = 'open' "
                        'AND deadline > ? ORDER BY worker_id',
                        (item_id, clock))]
                items.append({
                    'item_id': item_id,
                    'state': 'complete' if len(valid_workers) == 2 else 'pending',
                    'valid_workers': valid_workers,
                    'valid_votes': len(valid_workers),
                    'open_leases': open_leases,
                })
            conn.execute('COMMIT')
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        return {
            'schema_version': 1,
            'clock': clock,
            'workers': workers,
            'items': items,
        }

    # ------------------------------------------------------------------ export
    def export(self):
        """Build the delivered dataset from one read snapshot.

        Only normal items currently holding exactly two valid annotations are
        included, sorted by item_id.  The two raw answers are kept separate
        (sorted by worker) and each carries its full original ``analyze``
        result; the answers are never merged into a new grid.  Sentinels and
        their truths are never read.
        """
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            self._trace(conn)
            conn.execute('BEGIN')
            delivered = []
            item_rows = conn.execute(
                "SELECT i.item_id, i.connectivity, i.grid_json FROM items i "
                "WHERE i.kind = ? AND ("
                "  SELECT COUNT(*) FROM submissions s WHERE s.item_id = i.item_id "
                "  AND s.kind = 'normal' AND s.valid = 1) = 2 "
                'ORDER BY i.item_id', (KIND_NORMAL,)).fetchall()
            for item_id, connectivity, grid_json in item_rows:
                answers = conn.execute(
                    'SELECT worker_id, grid_json FROM submissions '
                    "WHERE item_id = ? AND kind = 'normal' AND valid = 1 "
                    'ORDER BY worker_id', (item_id,)).fetchall()
                if len(answers) != 2:  # snapshot invariant guard
                    continue
                annotations = []
                for worker_id, answer_json in answers:
                    answer_grid = json.loads(answer_json)
                    annotations.append({
                        'worker_id': worker_id,
                        'grid': answer_grid,
                        'analysis': analyze(
                            {'connectivity': connectivity, 'grid': answer_grid}),
                    })
                delivered.append({
                    'item_id': item_id,
                    'connectivity': connectivity,
                    'grid': json.loads(grid_json),
                    'annotations': annotations,
                })
            conn.execute('COMMIT')
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        return {'schema_version': 1, 'items': delivered}


# ------------------------------------------------------------ module-level API
def init_collection(path, campaign):
    """Create a new collection database from a validated campaign payload."""
    return Collection.initialize(path, campaign)


def claim_work(path, worker, now, **kwargs):
    """Open an existing database and claim one task."""
    return Collection(path, **kwargs).claim(worker, now)


def submit_answer(path, worker, token, now, answer, **kwargs):
    """Open an existing database and submit one leased answer."""
    return Collection(path, **kwargs).submit(worker, token, now, answer)


def collection_status(path, **kwargs):
    """Open an existing database and return one snapshot-consistent status."""
    return Collection(path, **kwargs).status()


def export_dataset(path, **kwargs):
    """Open an existing database and export the delivered dataset."""
    return Collection(path, **kwargs).export()
