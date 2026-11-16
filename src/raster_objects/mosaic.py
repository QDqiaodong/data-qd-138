"""Activity-aware multi-source raster mosaic warehouse.

Multiple annotation sources deliver overlapping raster tiles.  This module
keeps *every* contributed cell (including the ones hidden behind a
higher-priority source) in an SQLite database so that withdrawing a source
re-exposes the lower-priority observations instead of leaving holes.

Design properties
-----------------

* Only the Python standard library and :mod:`sqlite3` are used.
* Tiles are validated once into an immutable normalized representation.  The
  working data model is per-cell contribution rows plus an explicit spatial
  bucket index -- never a single whole-raster winner version.
* Catalog state, spatial index rows, per-cell contributions and the
  ``catalog_seq`` counter are committed together in one ``BEGIN IMMEDIATE``
  transaction, so a failure can never leave a half-registered source or
  consume a sequence number.
* Window queries run inside one read transaction, which in WAL mode is one
  immutable snapshot: catalog status, contributions, sequence number and the
  produced report always describe the same committed state.
* Window queries locate candidate tiles through the spatial index and stream
  each candidate's cells separately; ``grid_json`` payloads are never read by
  a query, and tiles disjoint from the window are never touched.

The warehouse deliberately does not store correction revisions, does not
assign cross-query object identities and never reads a correction ledger as
a data source.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from fractions import Fraction
from pathlib import Path

from .common import ident
from .components import label

# --------------------------------------------------------------------- bounds
COORD_MIN = -10**9
COORD_MAX = 10**9
MAX_SIDE = 500
MAX_TILE_CELLS = 100_000
MAX_SOURCES = 20_000
MAX_TOTAL_CELLS = 5_000_000
# A spatial bucket spans BUCKET_SPAN global rows/columns.  Tiles are at most
# 500x500, so a tile intersects at most four buckets in each dimension band
# (in practice 1..4 bucket rows total per axis pair).
BUCKET_SPAN = 500

_SCHEMA = """
CREATE TABLE meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE sources(
    tile_id TEXT PRIMARY KEY,
    tile_row INTEGER NOT NULL,
    tile_col INTEGER NOT NULL,
    height INTEGER NOT NULL,
    width INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    grid_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    active INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE tile_buckets(
    tile_id TEXT NOT NULL,
    bucket_row INTEGER NOT NULL,
    bucket_col INTEGER NOT NULL,
    PRIMARY KEY(tile_id, bucket_row, bucket_col)
) WITHOUT ROWID;
CREATE INDEX idx_bucket_space
    ON tile_buckets(bucket_row, bucket_col, tile_id);
CREATE TABLE cells(
    tile_id TEXT NOT NULL,
    grow INTEGER NOT NULL,
    gc INTEGER NOT NULL,
    value INTEGER,
    PRIMARY KEY(tile_id, grow, gc)
) WITHOUT ROWID;
"""

_STATE_KEYS = ('uncovered', 'unknown', 'resolved', 'conflict')


class MosaicError(ValueError):
    """Base class for mosaic input and protocol errors."""


class MosaicBusy(MosaicError):
    """A write lost a concurrency race; the call had no side effects."""


class TileConflict(MosaicError):
    """A registered tile_id was re-submitted with different content."""


class UnknownTile(MosaicError):
    """A withdrawal referenced a tile_id that was never registered."""


# ---------------------------------------------------------------- validation
def _bounded_int(value, name):
    if type(value) is not int:
        raise MosaicError('%s must be an integer (booleans are not accepted)' % name)
    if not COORD_MIN <= value <= COORD_MAX:
        raise MosaicError('%s must lie within [%d, %d]' % (name, COORD_MIN, COORD_MAX))
    return value


def _cell_value(value):
    if value is not None and (type(value) is not int or value not in (0, 1)):
        raise MosaicError('grid cells must be 0, 1 or null (booleans are rejected)')
    return value


def normalize_tile(payload):
    """Validate a tile payload and return its normalized description.

    The input must be exactly ``{tile_id, row, col, priority, grid}`` with no
    extra keys.  Returns a dict carrying the validated scalar fields, the
    checked grid, its canonical JSON text and a content fingerprint over the
    full normalized tile.  Key ordering of the incoming JSON never affects
    the fingerprint.
    """
    if not isinstance(payload, dict) or set(payload) != {
        'tile_id', 'row', 'col', 'priority', 'grid'
    }:
        raise MosaicError(
            'tile must contain exactly tile_id, row, col, priority and grid')
    try:
        tile_id = ident(payload['tile_id'])
    except ValueError as exc:
        raise MosaicError(str(exc)) from exc
    row = _bounded_int(payload['row'], 'row')
    col = _bounded_int(payload['col'], 'col')
    priority = _bounded_int(payload['priority'], 'priority')
    grid = payload['grid']
    if not isinstance(grid, list) or not grid:
        raise MosaicError('grid must be a nonempty array')
    if len(grid) > MAX_SIDE:
        raise MosaicError('grid height must not exceed %d rows' % MAX_SIDE)
    first = grid[0]
    if not isinstance(first, list) or not first or len(first) > MAX_SIDE:
        raise MosaicError('grid rows must be nonempty arrays of at most %d cells' % MAX_SIDE)
    width = len(first)
    height = len(grid)
    if height * width > MAX_TILE_CELLS:
        raise MosaicError('grid has more than %d cells' % MAX_TILE_CELLS)
    for grow in grid:
        if not isinstance(grow, list) or len(grow) != width:
            raise MosaicError('grid must be rectangular')
        for value in grow:
            _cell_value(value)
    max_row = row + height - 1
    max_col = col + width - 1
    if max_row > COORD_MAX or max_col > COORD_MAX:
        raise MosaicError(
            'tile global cell coordinates must lie within [%d, %d]'
            % (COORD_MIN, COORD_MAX))
    grid_json = json.dumps(
        grid, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    fingerprint = hashlib.sha256(json.dumps(
        {'row': row, 'col': col, 'priority': priority, 'grid': grid},
        sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        allow_nan=False,
    ).encode('utf-8')).hexdigest()
    return {
        'tile_id': tile_id,
        'row': row,
        'col': col,
        'height': height,
        'width': width,
        'priority': priority,
        'grid': grid,
        'grid_json': grid_json,
        'fingerprint': fingerprint,
    }


def _validate_window(row, col, height, width, connectivity):
    row = _bounded_int(row, 'row')
    col = _bounded_int(col, 'col')
    if type(height) is not int or type(width) is not int:
        raise MosaicError('height and width must be integers (booleans rejected)')
    if height < 1 or width < 1:
        raise MosaicError('window height and width must be positive')
    if height > MAX_SIDE or width > MAX_SIDE:
        raise MosaicError('window sides must not exceed %d' % MAX_SIDE)
    if height * width > MAX_TILE_CELLS:
        raise MosaicError('window has more than %d cells' % MAX_TILE_CELLS)
    if row + height - 1 > COORD_MAX or col + width - 1 > COORD_MAX:
        raise MosaicError(
            'window global coordinates must lie within [%d, %d]'
            % (COORD_MIN, COORD_MAX))
    if type(connectivity) is not int or connectivity not in (4, 8):
        raise MosaicError('connectivity must be 4 or 8 (booleans rejected)')
    return row, col, height, width, connectivity


def _bucket_bounds(lo, hi):
    """Inclusive bucket indices covering global coordinates lo..hi."""
    return lo // BUCKET_SPAN, hi // BUCKET_SPAN


def _bucket_rows(row, col, height, width):
    br0, br1 = _bucket_bounds(row, row + height - 1)
    bc0, bc1 = _bucket_bounds(col, col + width - 1)
    return [(br, bc) for br in range(br0, br1 + 1)
            for bc in range(bc0, bc1 + 1)]


# ------------------------------------------------------------------- warehouse
class Mosaic:
    """Handle to an on-disk activity-aware mosaic database."""

    def __init__(self, path, *, precommit_hook=None, observer=None):
        self.path = str(path)
        self.precommit_hook = precommit_hook
        self.observer = observer
        target = Path(self.path)
        if not target.is_file():
            # Never let sqlite3.connect silently create an empty database
            # when asked to open an existing warehouse.
            raise MosaicError('not a mosaic database: ' + self.path)
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            tables = {
                name for (name,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")
            }
            required = {'meta', 'sources', 'tile_buckets', 'cells'}
            if not required <= tables:
                raise MosaicError('not a mosaic database: ' + self.path)
            if conn.execute("SELECT 1 FROM meta WHERE key='catalog_seq'").fetchone() is None:
                raise MosaicError('malformed mosaic database: ' + self.path)
        finally:
            conn.close()

    def _emit(self, event, *args):
        if self.observer is not None:
            self.observer(event, *args)

    # -------------------------------------------------------------- lifecycle
    @staticmethod
    def initialize(path):
        """Create a brand new mosaic database; existing paths are refused."""
        target = Path(path)
        if target.exists() or target.is_symlink():
            raise MosaicError('database already exists: ' + str(target))
        conn = None
        try:
            conn = sqlite3.connect(target, timeout=0, isolation_level=None)
            conn.executescript(_SCHEMA)
            # WAL lets a window reader hold one stable snapshot while another
            # process commits puts/withdrawals.
            try:
                mode = conn.execute('PRAGMA journal_mode=WAL').fetchone()[0]
            except sqlite3.Error as exc:
                raise MosaicError('could not enable WAL journal mode: ' + str(exc)) from exc
            if mode not in ('wal', 'memory'):
                raise MosaicError('could not enable WAL journal mode: %s' % mode)
            conn.execute('PRAGMA synchronous=NORMAL')
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(
                'INSERT INTO meta(key, value) VALUES(?, ?)',
                ('catalog_seq', '0'))
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
        return Mosaic(target)

    def _connect_write(self):
        return sqlite3.connect(self.path, timeout=0, isolation_level=None)

    @staticmethod
    def _is_busy(exc):
        message = str(exc).lower()
        return 'locked' in message or 'busy' in message

    def _seq(self, conn):
        return int(conn.execute(
            "SELECT value FROM meta WHERE key='catalog_seq'").fetchone()[0])

    # -------------------------------------------------------------------- put
    def put(self, payload):
        """Register one tile, or accept a same-content retry as a no-op.

        Returns a receipt ``{tile_id, registered, active, catalog_seq}``.
        A re-put of identical content never changes the database, not even
        when the source has since been withdrawn; different content under a
        known id is rejected with :class:`TileConflict`.
        """
        tile = normalize_tile(payload)
        conn = None
        try:
            conn = self._connect_write()
            conn.execute('BEGIN IMMEDIATE')
            known = conn.execute(
                'SELECT fingerprint, active FROM sources WHERE tile_id=?',
                (tile['tile_id'],),
            ).fetchone()
            if known is not None:
                # Idempotent duplicate: no sequence bump, no resurrection,
                # regardless of the current active flag.
                if known[0] != tile['fingerprint']:
                    raise TileConflict(
                        'tile_id %r is already registered with different content'
                        % tile['tile_id'])
                seq = self._seq(conn)
                conn.execute('ROLLBACK')
                return {
                    'tile_id': tile['tile_id'],
                    'registered': False,
                    'active': bool(known[1]),
                    'catalog_seq': seq,
                }

            source_count, used_cells = conn.execute(
                'SELECT COUNT(*), COALESCE(SUM(height * width), 0) FROM sources'
            ).fetchone()
            if source_count >= MAX_SOURCES:
                raise MosaicError(
                    'source limit %d reached; the whole put is rejected'
                    % MAX_SOURCES)
            tile_cells = tile['height'] * tile['width']
            if used_cells + tile_cells > MAX_TOTAL_CELLS:
                raise MosaicError(
                    'cumulative input-cell limit %d reached; the whole put is rejected'
                    % MAX_TOTAL_CELLS)

            conn.execute(
                'INSERT INTO sources(tile_id, tile_row, tile_col, height, width,'
                ' priority, grid_json, fingerprint, active)'
                ' VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1)',
                (tile['tile_id'], tile['row'], tile['col'], tile['height'],
                 tile['width'], tile['priority'], tile['grid_json'],
                 tile['fingerprint']),
            )
            conn.executemany(
                'INSERT INTO tile_buckets(tile_id, bucket_row, bucket_col)'
                ' VALUES(?, ?, ?)',
                [(tile['tile_id'], br, bc)
                 for br, bc in _bucket_rows(
                     tile['row'], tile['col'], tile['height'], tile['width'])],
            )
            cell_rows = []
            for dr, grow in enumerate(tile['grid']):
                gr = tile['row'] + dr
                for dc, value in enumerate(grow):
                    cell_rows.append(
                        (tile['tile_id'], gr, tile['col'] + dc, value))
            conn.executemany(
                'INSERT INTO cells(tile_id, grow, gc, value) VALUES(?, ?, ?, ?)',
                cell_rows,
            )
            seq = self._seq(conn) + 1
            conn.execute(
                "UPDATE meta SET value=? WHERE key='catalog_seq'", (str(seq),))
            if self.precommit_hook is not None:
                self.precommit_hook('put', tile['tile_id'], seq)
            conn.execute('COMMIT')
            return {
                'tile_id': tile['tile_id'],
                'registered': True,
                'active': True,
                'catalog_seq': seq,
            }
        except sqlite3.OperationalError as exc:
            if self._is_busy(exc):
                raise MosaicBusy(str(exc)) from exc
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

    # ---------------------------------------------------------------- withdraw
    def withdraw(self, tile_id):
        """Permanently deactivate a known active source.

        Repeated withdrawal of an inactive id is a no-op; an unknown id
        raises :class:`UnknownTile`.  Contribution rows are retained so that
        lower-priority observations remain reconstructable.
        """
        try:
            tile_id = ident(tile_id)
        except ValueError as exc:
            raise MosaicError(str(exc)) from exc
        conn = None
        try:
            conn = self._connect_write()
            conn.execute('BEGIN IMMEDIATE')
            known = conn.execute(
                'SELECT active FROM sources WHERE tile_id=?', (tile_id,)
            ).fetchone()
            if known is None:
                raise UnknownTile('unknown tile_id %r' % tile_id)
            if known[0] == 0:
                seq = self._seq(conn)
                conn.execute('ROLLBACK')
                return {
                    'tile_id': tile_id,
                    'withdrawn': False,
                    'active': False,
                    'catalog_seq': seq,
                }
            conn.execute(
                'UPDATE sources SET active=0 WHERE tile_id=?', (tile_id,))
            seq = self._seq(conn) + 1
            conn.execute(
                "UPDATE meta SET value=? WHERE key='catalog_seq'", (str(seq),))
            if self.precommit_hook is not None:
                self.precommit_hook('withdraw', tile_id, seq)
            conn.execute('COMMIT')
            return {
                'tile_id': tile_id,
                'withdrawn': True,
                'active': False,
                'catalog_seq': seq,
            }
        except sqlite3.OperationalError as exc:
            if self._is_busy(exc):
                raise MosaicBusy(str(exc)) from exc
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

    # ------------------------------------------------------------------ window
    def window(self, row, col, height, width, connectivity, *, observer=None):
        """Build the fused report for one spatial window from one snapshot."""
        emit = observer or self.observer
        row, col, height, width, connectivity = _validate_window(
            row, col, height, width, connectivity)
        r_hi = row + height - 1
        c_hi = col + width - 1
        br0, br1 = _bucket_bounds(row, r_hi)
        bc0, bc1 = _bucket_bounds(col, c_hi)

        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            if emit is not None:
                conn.set_trace_callback(lambda sql: emit('stmt', sql))
            # One explicit read transaction = one immutable WAL snapshot for
            # every statement below, including catalog_seq.
            conn.execute('BEGIN')
            emit and emit('begin')
            seq = self._seq(conn)

            # Spatial candidate lookup: only the bucket B-tree and the
            # sources catalog are read.  grid_json payloads never appear.
            candidates = conn.execute(
                'SELECT DISTINCT b.tile_id, s.priority '
                'FROM tile_buckets b '
                'JOIN sources s ON s.tile_id = b.tile_id '
                'WHERE s.active = 1 '
                'AND b.bucket_row BETWEEN ? AND ? '
                'AND b.bucket_col BETWEEN ? AND ?',
                (br0, br1, bc0, bc1),
            ).fetchall()
            emit and emit('candidates', [tile_id for tile_id, _p in candidates])

            accum = {}
            cell_sql = (
                'SELECT grow, gc, value FROM cells '
                'WHERE tile_id = ? AND grow BETWEEN ? AND ? '
                'AND gc BETWEEN ? AND ?')
            # Each candidate is streamed and folded separately so cell data
            # for all candidates is never held at once.
            for tile_id, priority in candidates:
                read_rows = 0
                for grow, gc, value in conn.execute(
                        cell_sql, (tile_id, row, r_hi, col, c_hi)):
                    read_rows += 1
                    self._fold(accum, grow - row, gc - col,
                               priority, value, tile_id)
                emit and emit('tile_cells', tile_id, read_rows)

            report = self._build_report(
                accum, row, col, height, width, connectivity, seq)
            conn.execute('COMMIT')
            emit and emit('commit')
            return report
        except sqlite3.OperationalError as exc:
            if self._is_busy(exc):
                raise MosaicBusy(str(exc)) from exc
            raise
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    @staticmethod
    def _fold(accum, lr, lc, priority, value, tile_id):
        key = (lr, lc)
        entry = accum.get(key)
        if entry is None:
            entry = {'covered': False, 'top': None, 'top_count': 0,
                     'zero': None, 'one': None}
            accum[key] = entry
        entry['covered'] = True
        if value is None:
            # Null means "no opinion": it records coverage but can never
            # suppress a known value at any priority.
            return
        if entry['top'] is None or priority > entry['top']:
            entry['top'] = priority
            entry['top_count'] = 1
            entry['zero'] = tile_id if value == 0 else None
            entry['one'] = tile_id if value == 1 else None
        elif priority == entry['top']:
            entry['top_count'] += 1
            witness_key = 'zero' if value == 0 else 'one'
            current = entry[witness_key]
            if current is None or tile_id < current:
                entry[witness_key] = tile_id

    def _build_report(self, accum, row, col, height, width, connectivity, seq):
        states = []
        values = []
        provenance = []
        counts = {key: 0 for key in _STATE_KEYS}
        has_conflict = False
        for lr in range(height):
            state_row = []
            value_row = []
            provenance_row = []
            for lc in range(width):
                entry = accum.get((lr, lc))
                if entry is None or not entry['covered']:
                    state = 'uncovered'
                    value = None
                    prov = None
                elif entry['top'] is None:
                    state = 'unknown'
                    value = None
                    prov = None
                else:
                    prov = {
                        'priority': entry['top'],
                        'top_count': entry['top_count'],
                        'zero_witness': entry['zero'],
                        'one_witness': entry['one'],
                    }
                    if entry['zero'] is not None and entry['one'] is not None:
                        state = 'conflict'
                        value = None
                        has_conflict = True
                    else:
                        state = 'resolved'
                        value = 1 if entry['one'] is not None else 0
                counts[state] += 1
                state_row.append(state)
                value_row.append(value)
                provenance_row.append(prov)
            states.append(state_row)
            values.append(value_row)
            provenance.append(provenance_row)

        result = None
        if not has_conflict:
            # uncovered and unknown both enter the reused analyzer as null.
            local_grid = [
                [values[lr][lc] for lc in range(width)]
                for lr in range(height)
            ]
            analyzed = label(local_grid, connectivity)
            objects = []
            for obj in analyzed['objects']:
                r0, c0, r1, c1 = obj['bbox']
                objects.append({
                    'id': obj['id'],
                    'cells': obj['cells'],
                    'perimeter_edges': obj['perimeter_edges'],
                    'bbox': [row + r0, col + c0, row + r1, col + c1],
                    # Exact fraction strings translated into global coords.
                    'centroid_row': str(
                        Fraction(obj['centroid_row']) + row),
                    'centroid_col': str(
                        Fraction(obj['centroid_col']) + col),
                })
            result = {
                'labels': analyzed['labels'],
                'objects': objects,
                'foreground_cells': analyzed['foreground_cells'],
                'background_cells': analyzed['background_cells'],
                'unknown_cells': analyzed['unknown_cells'],
            }

        return {
            'schema_version': 1,
            'row': row,
            'col': col,
            'height': height,
            'width': width,
            'connectivity': connectivity,
            'catalog_seq': seq,
            'states': states,
            'values': values,
            'provenance': provenance,
            'state_counts': counts,
            'result': result,
        }


# ------------------------------------------------------------ module-level API
def init_mosaic(path):
    """Create a new mosaic warehouse database."""
    return Mosaic.initialize(path)


def put_tile(path, payload):
    """Open an existing warehouse and register (or idempotently re-put) a tile."""
    return Mosaic(path).put(payload)


def withdraw_tile(path, tile_id):
    """Open an existing warehouse and withdraw one source."""
    return Mosaic(path).withdraw(tile_id)


def window_query(path, row, col, height, width, connectivity, *, observer=None):
    """Open an existing warehouse and run one snapshot-consistent window query."""
    return Mosaic(path, observer=observer).window(
        row, col, height, width, connectivity, observer=observer)
