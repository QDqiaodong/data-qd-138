"""Persistent raster correction ledger and object-identity bookkeeping.

A ledger is a single SQLite database recording an append-only chain of
raster revisions.  Version 0 is the initial source raster; every successful
``apply`` appends one contiguous version.  Each version stores the raster,
its component geometry with persistent object identifiers, the full receipt
of the change request, and a per-cell touch history that lets stale-baseline
corrections be accepted or rejected without coordinating clients.

Only the Python standard library and SQLite are used.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .common import ident, read
from .matching import match_objects
from .model import analyze

MAX_OBJECTS = 128
_SCHEMA = """
CREATE TABLE meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE versions(
    version INTEGER PRIMARY KEY,
    parent_version INTEGER,
    request_id TEXT,
    created_at TEXT NOT NULL,
    connectivity INTEGER NOT NULL,
    height INTEGER NOT NULL,
    width INTEGER NOT NULL,
    grid_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    receipt_json TEXT
);
CREATE TABLE touches(
    version INTEGER NOT NULL,
    row_index INTEGER NOT NULL,
    col_index INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    PRIMARY KEY(version, row_index, col_index)
) WITHOUT ROWID;
CREATE TABLE requests(
    request_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    receipt_json TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX idx_touch_cell ON touches(row_index, col_index, version);
"""


class LedgerError(ValueError):
    """Base class for ledger-side input and protocol errors."""


class UnknownVersion(LedgerError):
    """The referenced base version has never been committed."""


class RequestConflict(LedgerError):
    """A stale-baseline correction touches cells modified since its base."""

    def __init__(self, conflicts):
        super().__init__('correction conflicts with later commits')
        self.conflicts = conflicts


class RequestIdConflict(LedgerError):
    """A request id was reused with a different payload."""


class LedgerBusy(LedgerError):
    """A write lost a concurrency race; the call had no side effects."""


def _strict_int(value):
    if type(value) is not int:
        raise LedgerError('expected an integer (booleans are not accepted)')
    return value


def _cell_value(value):
    if value is None:
        return None
    if type(value) is not int or value not in (0, 1):
        raise LedgerError('cell values must be 0, 1 or null (booleans rejected)')
    return value


def normalize_request(payload):
    """Validate an apply payload and return its canonical fingerprint.

    The fingerprint is independent of key order and the order of the
    ``changes`` array; changes are normalized into row/column order.
    """
    if not isinstance(payload, dict) or set(payload) != {'request_id', 'base_version', 'changes'}:
        raise LedgerError("expected exactly the fields request_id, base_version and changes")
    try:
        request_id = ident(payload['request_id'])
    except ValueError as exc:
        raise LedgerError(str(exc)) from exc
    base_version = _strict_int(payload['base_version'])
    if base_version < 0:
        raise LedgerError('base_version must be a nonnegative committed integer')
    changes_raw = payload['changes']
    if not isinstance(changes_raw, list) or not changes_raw:
        raise LedgerError('changes must be a nonempty array')
    seen = set()
    normalized = []
    for item in changes_raw:
        if not isinstance(item, dict) or set(item) != {'row', 'col', 'before', 'after'}:
            raise LedgerError('each change must contain exactly row, col, before, after')
        row = _strict_int(item['row'])
        col = _strict_int(item['col'])
        before = _cell_value(item['before'])
        after = _cell_value(item['after'])
        if before == after:
            raise LedgerError('before and after must differ')
        if (row, col) in seen:
            raise LedgerError('duplicate cell coordinate in changes')
        seen.add((row, col))
        normalized.append([row, col, before, after])
    normalized.sort(key=lambda change: (change[0], change[1]))
    fingerprint = json.dumps(
        {'base_version': base_version, 'changes': normalized},
        sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False,
    )
    return request_id, base_version, normalized, fingerprint


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    """Handle to an on-disk correction ledger."""

    def __init__(self, path, *, precommit_hook=None):
        self.path = str(path)
        self.precommit_hook = precommit_hook
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            tables = {name for name, in rows}
            if not tables:
                raise LedgerError('not a ledger database: ' + self.path)
            required = {'meta', 'versions', 'touches', 'requests'}
            if not required <= tables:
                raise LedgerError('malformed ledger database: ' + self.path)
        finally:
            conn.close()

    # ------------------------------------------------------------------ init
    @staticmethod
    def initialize(path, source_path):
        """Create a new ledger at ``path`` from a source JSON and return it."""
        target = Path(path)
        if target.exists() or target.is_symlink():
            raise LedgerError('database already exists: ' + str(target))
        data, digest = read(source_path)
        result = analyze(data)
        if len(result['objects']) > MAX_OBJECTS:
            raise LedgerError('object count %d exceeds limit %d' % (len(result['objects']), MAX_OBJECTS))
        connectivity = data['connectivity']
        grid = data['grid']
        height = len(grid)
        width = len(grid[0])
        conn = None
        try:
            conn = sqlite3.connect(target, timeout=0, isolation_level=None)
            conn.executescript(_SCHEMA)
            conn.execute('BEGIN IMMEDIATE')
            conn.executemany(
                'INSERT INTO meta(key, value) VALUES(?, ?)',
                [
                    ('connectivity', str(connectivity)),
                    ('height', str(height)),
                    ('width', str(width)),
                    ('next_id', str(len(result['objects']))),
                    ('source_sha256', digest),
                ],
            )
            conn.execute(
                'INSERT INTO versions(version, parent_version, request_id, created_at,'
                ' connectivity, height, width, grid_json, result_json, receipt_json)'
                ' VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (0, None, None, _now_iso(), connectivity, height, width,
                 json.dumps(grid, separators=(',', ':'), allow_nan=False),
                 json.dumps(result, sort_keys=True, allow_nan=False), None),
            )
            conn.execute('COMMIT')
        except Exception:
            if conn is not None:
                conn.rollback()
                conn.close()
            if target.exists():
                target.unlink()
            raise
        else:
            conn.close()
        return Ledger(target)

    # ------------------------------------------------------------- internals
    def _connect_write(self):
        conn = sqlite3.connect(self.path, timeout=0, isolation_level=None)
        return conn

    @staticmethod
    def _meta(conn, key):
        return conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0]

    def _load_head(self, conn):
        row = conn.execute(
            'SELECT version, grid_json, result_json FROM versions ORDER BY version DESC LIMIT 1'
        ).fetchone()
        return row[0], json.loads(row[1]), json.loads(row[2])

    def _object_cells(self, labels, objects):
        cells_by_id = {obj['id']: [] for obj in objects}
        for r, row in enumerate(labels):
            for c, label in enumerate(row):
                if label is not None and label != 0:
                    cells_by_id[label].append((r, c))
        return cells_by_id

    # ------------------------------------------------------------------ apply
    def apply(self, payload):
        """Apply a validated change request; return the success receipt."""
        request_id, base_version, changes, fingerprint = normalize_request(payload)
        conn = None
        try:
            conn = self._connect_write()
            conn.execute('BEGIN IMMEDIATE')
            return self._apply_locked(conn, request_id, base_version, changes, fingerprint)
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if 'locked' in message or 'busy' in message:
                raise LedgerBusy(str(exc)) from exc
            raise
        except BaseException:
            # Never leave a half-open transaction behind: the next close must
            # observe a clean rollback even if the interpreter is unwinding.
            if conn is not None:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            raise
        finally:
            if conn is not None:
                conn.close()

    def _apply_locked(self, conn, request_id, base_version, changes, fingerprint):
        # Idempotency: a previously accepted request id short-circuits every
        # other rule and never allocates ids or writes a new version.
        cached = conn.execute(
            'SELECT fingerprint, receipt_json FROM requests WHERE request_id=?', (request_id,)
        ).fetchone()
        if cached is not None:
            if cached[0] != fingerprint:
                raise RequestIdConflict('request_id already used with a different request')
            return json.loads(cached[1])

        head_version, head_grid, head_result = self._load_head(conn)
        connectivity = int(self._meta(conn, 'connectivity'))
        height = int(self._meta(conn, 'height'))
        width = int(self._meta(conn, 'width'))
        next_id = int(self._meta(conn, 'next_id'))

        base_row = conn.execute(
            'SELECT grid_json FROM versions WHERE version=?', (base_version,)
        ).fetchone()
        if base_row is None:
            raise UnknownVersion('unknown base_version %r' % (base_version,))
        base_grid = json.loads(base_row[0])

        # Input errors are reported before any touch-history conflict.  The
        # "before" value is verified against the request's own baseline, not
        # against the current head.
        for row, col, before, _after in changes:
            if not (0 <= row < height and 0 <= col < width):
                raise LedgerError('change coordinate (%d,%d) is outside the raster' % (row, col))
            if base_grid[row][col] != before:
                raise LedgerError(
                    'before value at (%d,%d) does not match the base-version raster' % (row, col)
                )

        conflict_rows = conn.execute(
            'SELECT DISTINCT row_index, col_index FROM touches '
            'WHERE version > ? AND (row_index, col_index) IN (%s) '
            'ORDER BY row_index, col_index'
            % ','.join('(?, ?)' for _ in changes),
            [base_version] + [value for change in changes for value in change[:2]],
        ).fetchall()
        if conflict_rows:
            raise RequestConflict([[row, col] for row, col in conflict_rows])

        new_grid = [row[:] for row in head_grid]
        for row, col, _before, after in changes:
            new_grid[row][col] = after
        new_result = analyze({'connectivity': connectivity, 'grid': new_grid})
        if len(new_result['objects']) > MAX_OBJECTS:
            raise LedgerError(
                'object count %d exceeds limit %d; the whole request is rejected'
                % (len(new_result['objects']), MAX_OBJECTS)
            )

        # Identity tracking always runs between the committed head and the
        # candidate raster, never against the request's baseline.
        old_cells_map = self._object_cells(head_result['labels'], head_result['objects'])
        old_objects = [(obj['id'], old_cells_map[obj['id']]) for obj in head_result['objects']]
        new_cells_map = self._object_cells(new_result['labels'], new_result['objects'])
        ordered_new = sorted(
            ((obj['id'], new_cells_map[obj['id']]) for obj in new_result['objects']),
            key=lambda item: min(item[1]),
        )
        matching = match_objects(old_objects, ordered_new)

        label_map = {}
        inheritance = []
        for new_key, old_id, overlap in matching['pairs']:
            label_map[new_key] = old_id
            inheritance.append({'new_label': new_key, 'object_id': old_id, 'overlap_cells': overlap})
        birth_ids = []
        for new_key in matching['unmatched_new']:
            next_id += 1
            label_map[new_key] = next_id
            birth_ids.append(next_id)

        persistent_labels = [
            [None if value is None else (0 if value == 0 else label_map[value]) for value in row]
            for row in new_result['labels']
        ]
        persistent_objects = []
        for obj in sorted(new_result['objects'], key=lambda item: label_map[item['id']]):
            published = dict(obj)
            published['id'] = label_map[obj['id']]
            persistent_objects.append(published)
        published_result = {
            'labels': persistent_labels,
            'objects': persistent_objects,
            'foreground_cells': new_result['foreground_cells'],
            'background_cells': new_result['background_cells'],
            'unknown_cells': new_result['unknown_cells'],
        }
        new_version = head_version + 1
        receipt = {
            'version': new_version,
            'parent_version': head_version,
            'request_id': request_id,
            'inheritance': inheritance,
            'retired_ids': matching['retired_ids'],
            'birth_ids': birth_ids,
        }

        conn.execute(
            'INSERT INTO versions(version, parent_version, request_id, created_at,'
            ' connectivity, height, width, grid_json, result_json, receipt_json)'
            ' VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (new_version, head_version, request_id, _now_iso(), connectivity, height, width,
             json.dumps(new_grid, separators=(',', ':'), allow_nan=False),
             json.dumps(published_result, sort_keys=True, allow_nan=False),
             json.dumps(receipt, sort_keys=True, allow_nan=False)),
        )
        conn.executemany(
            'INSERT INTO touches(version, row_index, col_index, request_id) VALUES(?, ?, ?, ?)',
            [(new_version, row, col, request_id) for row, col, _b, _a in changes],
        )
        conn.execute("UPDATE meta SET value=? WHERE key='next_id'", (str(next_id),))
        conn.execute(
            'INSERT INTO requests(request_id, fingerprint, receipt_json) VALUES(?, ?, ?)',
            (request_id, fingerprint, json.dumps(receipt, sort_keys=True, allow_nan=False)),
        )
        if self.precommit_hook is not None:
            self.precommit_hook(new_version, receipt)
        conn.execute('COMMIT')
        return receipt

    # ---------------------------------------------------------------- export
    def head_version(self):
        with self._connect_read() as conn:
            return conn.execute('SELECT MAX(version) FROM versions').fetchone()[0]

    def _connect_read(self):
        return sqlite3.connect(self.path, timeout=30)

    def export(self, version):
        """Return the authoritative report for one committed version."""
        delay = 0.02
        for attempt in range(6):
            try:
                return self._export_once(version)
            except sqlite3.OperationalError as exc:
                if attempt == 5:
                    raise LedgerBusy(str(exc)) from exc
                time.sleep(delay)
                delay *= 2

    def _export_once(self, version):
        if _strict_int(version) < 0:
            raise LedgerError('version must be a nonnegative integer')
        with self._connect_read() as conn:
            row = conn.execute(
                'SELECT parent_version, result_json, receipt_json FROM versions WHERE version=?',
                (version,),
            ).fetchone()
            if row is None:
                raise UnknownVersion('unknown version %r' % (version,))
            source_sha = self._meta(conn, 'source_sha256')
            connectivity = int(self._meta(conn, 'connectivity'))
        parent_version, result_json, receipt_json = row
        report = {
            'schema_version': 1,
            'version': version,
            'parent_version': parent_version,
            'connectivity': connectivity,
            'source_sha256': source_sha,
            'receipt': None if receipt_json is None else json.loads(receipt_json),
            'result': json.loads(result_json),
        }
        return report


def init_ledger(path, source_path):
    """Module-level convenience wrapper around :meth:`Ledger.initialize`."""
    return Ledger.initialize(path, source_path)


def apply_request(path, payload):
    """Open an existing ledger and apply one request."""
    return Ledger(path).apply(payload)


def export_version(path, version):
    """Open an existing ledger and export one version."""
    return Ledger(path).export(version)
