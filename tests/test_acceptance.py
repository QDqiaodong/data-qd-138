"""End-to-end acceptance tests for ledger linkage and transactional safety.

Covers the required cross-cutting scenarios:

* two non-overlapping corrections issued from the same base, where the first
  changes object merge relations and the second is still accepted but derives
  its inheritance from the first commit's head, with serial replay and
  persisted history matching item by item;
* concurrent writers, both in-process threads and separate processes, whose
  commits are explainable as one serial ordering with no lost versions;
* pre-commit exception injection proving failures consume neither versions
  nor identifiers and never expose a half-written snapshot;
* restart-safe idempotent retries returning the original receipt.
"""
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from raster_objects.ledger import (
    Ledger,
    LedgerBusy,
    LedgerError,
    RequestConflict,
)

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))


def write_json(path, payload):
    Path(path).write_text(json.dumps(payload))


def run_cli(*args):
    return subprocess.run(
        [sys.executable, '-m', 'raster_objects.ledger_cli', *map(str, args)],
        capture_output=True, text=True, env=ENV, cwd=ROOT,
    )


def request(request_id, base, changes):
    return {'request_id': request_id, 'base_version': base, 'changes': changes}


def chg(row, col, before, after):
    return {'row': row, 'col': col, 'before': before, 'after': after}


DUMBBELL = {
    'connectivity': 4,
    'grid': [
        [1, 1, 0, 1, 1],
        [1, 1, 1, 1, 1],
        [1, 1, 0, 1, 1],
    ],
}


class LinkageAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        self.source = self.td / 'source.json'
        write_json(self.source, DUMBBELL)

    def tearDown(self):
        self.temp.cleanup()

    def test_stale_sibling_correction_inherits_from_new_head(self):
        # Both corrections start from base 0 and touch disjoint cells.
        # Correction A removes the bridge (1,2): the single object splits.
        # Correction B fills (2,2), which is untouched by A; it must apply on
        # the post-A head and match against that head's two objects.
        payload_a = request('A', 0, [chg(1, 2, 1, 0)])
        payload_b = request('B', 0, [chg(2, 2, 0, 1)])

        db = self.td / 'link.db'
        ledger = Ledger.initialize(db, self.source)
        receipt_a = ledger.apply(payload_a)
        self.assertEqual(receipt_a['version'], 1)
        self.assertEqual(receipt_a['birth_ids'], [2])
        receipt_b = ledger.apply(payload_b)
        self.assertEqual(receipt_b['version'], 2)
        self.assertEqual(receipt_b['parent_version'], 1)

        # B's inheritance is computed against the post-A head (two objects),
        # never against base 0's single scan object.
        inherited = {(item['new_label'], item['object_id'], item['overlap_cells'])
                     for item in receipt_b['inheritance']}
        report_b = ledger.export(2)
        label_cells = self._cells_by_id(report_b['result']['labels'])
        for _new_label, object_id, overlap in inherited:
            self.assertIn(object_id, {1, 2})
            self.assertGreaterEqual(overlap, 1)
            self.assertIn(object_id, label_cells)

        # Serial replay in an independent ledger must match item by item.
        serial_db = self.td / 'serial.db'
        serial = Ledger.initialize(serial_db, self.source)
        serial_a = serial.apply(payload_a)
        serial_b = serial.apply(payload_b)
        self.assertEqual(serial_a, receipt_a)
        self.assertEqual(serial_b, receipt_b)
        for version in (0, 1, 2):
            self.assertEqual(
                Ledger(db).export(version),
                Ledger(serial_db).export(version),
            )

        # The persisted receipts remain available after reopening, and old
        # versions keep their own geometry without later ids leaking in.
        reopened = Ledger(db)
        self.assertEqual(reopened.export(2)['receipt'], receipt_b)
        v1_ids = [o['id'] for o in reopened.export(1)['result']['objects']]
        self.assertEqual(v1_ids, [1, 2])
        # Filling (2,2) 4-connects the two split objects back into one; id 2
        # retires through the overlap-maximizing matching against head v1.
        v2_ids = [o['id'] for o in reopened.export(2)['result']['objects']]
        self.assertEqual(v2_ids, [1])
        self.assertEqual(receipt_b['retired_ids'], [2])
        self.assertEqual(receipt_b['birth_ids'], [])

    def _cells_by_id(self, labels):
        cells = {}
        for r, row in enumerate(labels):
            for c, value in enumerate(row):
                if value:
                    cells.setdefault(value, []).append((r, c))
        return cells


class ConcurrencyAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        self.source = self.td / 'source.json'
        write_json(self.source, DUMBBELL)

    def tearDown(self):
        self.temp.cleanup()

    def test_threaded_writers_serialize_with_busy_retry(self):
        db = self.td / 'threads.db'
        Ledger.initialize(db, self.source)
        # All five cells are background in the base raster, so the same
        # before/after values stay valid for every stale retry and the cells
        # remain disjoint across requests.
        cells = [(0, 2), (2, 2)]
        outcomes = []
        lock = threading.Lock()

        def worker(index):
            row, col = cells[index]
            payload = request('w%d' % index, 0, [chg(row, col, 0, 1)])
            ledger = Ledger(db)
            rng = random.Random(index)
            for attempt in range(200):
                try:
                    receipt = ledger.apply(payload)
                    with lock:
                        outcomes.append(('ok', index, receipt['version']))
                    return
                except LedgerBusy:
                    time.sleep(0.005 + rng.random() * 0.02)
                    continue
            with lock:
                outcomes.append(('busy-exhausted', index, None))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(cells))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertTrue(all(kind == 'ok' for kind, _i, _v in outcomes), outcomes)
        versions = sorted(version for _kind, _index, version in outcomes)
        self.assertEqual(versions, [1, 2])
        # Every request id has exactly one receipt and one contiguous version.
        with sqlite3.connect(db) as conn:
            self.assertEqual(
                conn.execute('SELECT COUNT(*) FROM versions').fetchone()[0], 3)
            self.assertEqual(
                conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 2)
            rows = conn.execute(
                'SELECT version, parent_version FROM versions ORDER BY version').fetchall()
        for version, parent in rows[1:]:
            self.assertEqual(parent, version - 1)

    def test_two_process_race_is_serializable(self):
        db = self.td / 'proc.db'
        run_cli('init', '--db', db, '--source', self.source)
        req_a = self.td / 'a.json'
        req_b = self.td / 'b.json'
        write_json(req_a, request('PA', 0, [chg(0, 2, 0, 1)]))
        write_json(req_b, request('PB', 0, [chg(2, 2, 0, 1)]))

        proc_a = subprocess.Popen(
            [sys.executable, '-m', 'raster_objects.ledger_cli', 'apply',
             '--db', db, '--source', req_a],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV, cwd=ROOT)
        proc_b = subprocess.Popen(
            [sys.executable, '-m', 'raster_objects.ledger_cli', 'apply',
             '--db', db, '--source', req_b],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV, cwd=ROOT)
        out_a, err_a = proc_a.communicate()
        out_b, err_b = proc_b.communicate()
        results = [(proc_a.returncode, out_a, err_a), (proc_b.returncode, out_b, err_b)]
        codes = sorted(item[0] for item in results)
        # One writer wins immediately; the other either won the retry race in
        # a later run or reports the retryable busy code.
        self.assertIn(codes[0], (0, 4))
        self.assertEqual(codes[1], 0)
        busy = [item for item in results if item[0] == 4]
        if busy:
            # Retrying the loser now succeeds without changing its semantics.
            loser_source = req_b if results[1][0] == 4 else req_a
            retry = run_cli('apply', '--db', db, '--source', loser_source)
            self.assertEqual(retry.returncode, 0, retry.stderr)

        ledger = Ledger(db)
        self.assertEqual(ledger.head_version(), 2)
        receipts = [ledger.export(1)['receipt'], ledger.export(2)['receipt']]
        request_ids = {item['request_id'] for item in receipts}
        self.assertEqual(request_ids, {'PA', 'PB'})
        for receipt in receipts:
            self.assertEqual(receipt['parent_version'], receipt['version'] - 1)
        with sqlite3.connect(db) as conn:
            self.assertEqual(
                conn.execute('SELECT COUNT(*) FROM touches').fetchone()[0], 2)

    def test_precommit_failure_and_restart_retry(self):
        db = self.td / 'hook.db'
        Ledger.initialize(db, self.source)
        failures = []

        def hook(version, receipt):
            failures.append(version)
            raise RuntimeError('injected before commit')

        ledger = Ledger(db, precommit_hook=hook)
        payload = request('hooked', 0, [chg(1, 2, 1, 0)])
        with self.assertRaises(RuntimeError):
            ledger.apply(payload)
        self.assertEqual(failures, [1])

        # No snapshot may mix a new raster with old matching data: verify the
        # sole stored version is still the untouched version 0.
        with sqlite3.connect(db) as conn:
            self.assertEqual(
                conn.execute('SELECT MAX(version) FROM versions').fetchone()[0], 0)
            self.assertEqual(
                conn.execute('SELECT COUNT(*) FROM touches').fetchone()[0], 0)
            self.assertEqual(
                conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT value FROM meta WHERE key='next_id'").fetchone()[0],
                '1')

        # After a fresh process opens the database, the same request retries
        # cleanly and returns the canonical first-success receipt.
        reopened = Ledger(db)
        receipt = reopened.apply(payload)
        self.assertEqual(receipt['version'], 1)
        again = Ledger(db).apply(payload)
        self.assertEqual(again, receipt)
        self.assertEqual(reopened.head_version(), 1)

    def test_cli_export_uses_database_as_authority(self):
        db = self.td / 'exp.db'
        out = self.td / 'v1.json'
        run_cli('init', '--db', db, '--source', self.source)
        req = self.td / 'req.json'
        write_json(req, request('E', 0, [chg(1, 2, 1, 0)]))
        first = run_cli('apply', '--db', db, '--source', req)
        self.assertEqual(first.returncode, 0, first.stderr)
        result = run_cli('export', '--db', db, '--version', 1, '--output', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(out.read_text())
        self.assertEqual(report['version'], 1)
        self.assertEqual(report['parent_version'], 0)
        self.assertEqual(report['receipt'], json.loads(first.stdout))
        # Labels and object ids are the persistent ids; geometry fields and
        # exact fraction strings follow the original result contract.
        obj = report['result']['objects'][0]
        for field in ('bbox', 'cells', 'centroid_row', 'centroid_col',
                      'perimeter_edges', 'id'):
            self.assertIn(field, obj)
        # Fraction strings are preserved verbatim from the geometry contract
        # (integers render plainly, non-integers keep an exact "n/d" fraction).
        self.assertRegex(obj['centroid_row'], r'^\d+(/\d+)?$')
        self.assertRegex(obj['centroid_col'], r'^\d+(/\d+)?$')
        # Exporting an old version after further commits stays frozen.
        write_json(self.td / 'req2.json', request('E2', 1, [chg(2, 2, 0, 1)]))
        run_cli('apply', '--db', db, '--source', self.td / 'req2.json')
        frozen = json.loads(out.read_text())
        again = run_cli('export', '--db', db, '--version', 1,
                        '--output', self.td / 'v1-again.json')
        self.assertEqual(json.loads(again.stdout), frozen)
        self.assertEqual(Ledger(db).export(1), frozen)


if __name__ == '__main__':
    unittest.main()
