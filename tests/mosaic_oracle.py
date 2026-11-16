"""Cell-by-cell fusion oracle, independent of the warehouse implementation.

The oracle keeps tiles in plain Python dictionaries and recomputes every
cell from scratch.  Its component labeling is an independent BFS (it does
not import :mod:`raster_objects.components` or any mosaic storage code), so
warehouse reports can be compared against semantics expressed directly from
the specification.
"""


class OracleMosaic:
    """Reference fusion engine over an in-memory set of active tiles."""

    def __init__(self):
        # tile_id -> dict with row, col, priority, grid, active
        self.tiles = {}
        self.seq = 0

    def put(self, tile):
        tid = tile['tile_id']
        if tid in self.tiles:
            stored = self.tiles[tid]
            if (stored['row'], stored['col'], stored['priority'], stored['grid']) != (
                tile['row'], tile['col'], tile['priority'], tile['grid']
            ):
                raise ValueError('conflicting content for ' + tid)
            return self.seq
        self.tiles[tid] = {
            'row': tile['row'], 'col': tile['col'],
            'priority': tile['priority'], 'grid': tile['grid'],
            'active': True,
        }
        self.seq += 1
        return self.seq

    def withdraw(self, tid):
        if tid not in self.tiles:
            raise KeyError(tid)
        if self.tiles[tid]['active']:
            self.tiles[tid]['active'] = False
            self.seq += 1
        return self.seq

    # ----------------------------------------------------------- fusion view
    def cell_view(self, lr, lc, grow, gc):
        """Fuse one global cell from every active contribution."""
        covered = False
        top = None
        witnesses = {0: [], 1: []}
        top_count = 0
        for tid, tile in self.tiles.items():
            if not tile['active']:
                continue
            dr = grow - tile['row']
            dc = gc - tile['col']
            if not (0 <= dr < len(tile['grid']) and 0 <= dc < len(tile['grid'][0])):
                continue
            covered = True
            value = tile['grid'][dr][dc]
            if value is None:
                continue
            if top is None or tile['priority'] > top:
                top = tile['priority']
                top_count = 1
                witnesses = {0: [], 1: []}
                witnesses[value].append(tid)
            elif tile['priority'] == top:
                top_count += 1
                witnesses[value].append(tid)
        if not covered:
            return {'state': 'uncovered', 'value': None, 'provenance': None}
        if top is None:
            return {'state': 'unknown', 'value': None, 'provenance': None}
        prov = {
            'priority': top,
            'top_count': top_count,
            'zero_witness': min(witnesses[0]) if witnesses[0] else None,
            'one_witness': min(witnesses[1]) if witnesses[1] else None,
        }
        if witnesses[0] and witnesses[1]:
            return {'state': 'conflict', 'value': None, 'provenance': prov}
        value = 1 if witnesses[1] else 0
        return {'state': 'resolved', 'value': value, 'provenance': prov}

    # -------------------------------------------------- independent geometry
    @staticmethod
    def _label(grid, connectivity):
        """Independent BFS labeling with window-clipped edge perimeters.

        Background cells are labeled 0 and unknown cells stay null, matching
        the shared raster contract used by the warehouse's geometry pass.
        """
        h = len(grid)
        w = len(grid[0])
        labels = [[None if value is None else 0 for value in line]
                  for line in grid]
        objects = []
        orth = [(-1, 0), (1, 0), (0, -1), (0, 1)]
        steps = orth + ([(-1, -1), (-1, 1), (1, -1), (1, 1)]
                        if connectivity == 8 else [])
        for r in range(h):
            for c in range(w):
                if grid[r][c] != 1 or labels[r][c]:
                    continue
                oid = len(objects) + 1
                labels[r][c] = oid
                pending = [(r, c)]
                cells = []
                perimeter = 0
                while pending:
                    ar, ac = pending.pop()
                    cells.append((ar, ac))
                    for dr, dc in orth:
                        nr, nc = ar + dr, ac + dc
                        if not (0 <= nr < h and 0 <= nc < w) or grid[nr][nc] != 1:
                            perimeter += 1
                    for dr, dc in steps:
                        nr, nc = ar + dr, ac + dc
                        if (0 <= nr < h and 0 <= nc < w
                                and grid[nr][nc] == 1 and not labels[nr][nc]):
                            labels[nr][nc] = oid
                            pending.append((nr, nc))
                objects.append((oid, cells, perimeter))
        return labels, objects

    def window(self, row, col, height, width, connectivity):
        states = []
        values = []
        provenance = []
        counts = {'uncovered': 0, 'unknown': 0, 'resolved': 0, 'conflict': 0}
        has_conflict = False
        for lr in range(height):
            s_row = []
            v_row = []
            p_row = []
            for lc in range(width):
                view = self.cell_view(lr, lc, row + lr, col + lc)
                s_row.append(view['state'])
                v_row.append(view['value'])
                p_row.append(view['provenance'])
                counts[view['state']] += 1
                if view['state'] == 'conflict':
                    has_conflict = True
            states.append(s_row)
            values.append(v_row)
            provenance.append(p_row)

        result = None
        if not has_conflict:
            labels, raw_objects = self._label(values, connectivity)
            objects = []
            foreground = sum(value == 1 for line in values for value in line)
            background = sum(value == 0 for line in values for value in line)
            unknown = sum(value is None for line in values for value in line)
            for oid, cells, perimeter in raw_objects:
                row_totals = [cr for cr, _cc in cells]
                col_totals = [cc for _cr, cc in cells]

                def frac(total, count, offset):
                    # Exact reduced fraction string with the global integer
                    # offset applied (matches Fraction's reduced rendering).
                    numerator = total + offset * count
                    denominator = count
                    gx, gy = abs(numerator), denominator
                    while gy:
                        gx, gy = gy, gx % gy
                    numerator //= gx
                    denominator //= gx
                    return (str(numerator) if denominator == 1
                            else '%d/%d' % (numerator, denominator))

                objects.append({
                    'id': oid,
                    'cells': len(cells),
                    'perimeter_edges': perimeter,
                    'bbox': [row + min(row_totals), col + min(col_totals),
                             row + max(row_totals), col + max(col_totals)],
                    'centroid_row': frac(sum(row_totals), len(cells), row),
                    'centroid_col': frac(sum(col_totals), len(cells), col),
                })
            result = {
                'labels': labels,
                'objects': objects,
                'foreground_cells': foreground,
                'background_cells': background,
                'unknown_cells': unknown,
            }
        return {
            'schema_version': 1,
            'row': row, 'col': col, 'height': height, 'width': width,
            'connectivity': connectivity,
            'catalog_seq': self.seq,
            'states': states,
            'values': values,
            'provenance': provenance,
            'state_counts': counts,
            'result': result,
        }
