"""Dispatch, lease and quota tests for the blind collection pipeline.

Verifies the work-response secrecy boundary, the at-most-one unexpired lease
per worker rule with no deadline extension, the two-vote cap from valid votes
plus unexpired leases, expiry-driven slot reuse, stale-token isolation,
token uniqueness, the monotonic logical clock and restart-safe quota state.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from raster_objects.collection import (
    Collection,
    CollectionBusy,
    CollectionError,
    LeaseConflict,
    LeaseExpired,
    StaleClock,
    UnknownLease,
    WorkerDisqualified,
)

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))

FORBIDDEN_IN_WORK = ('item_id', 'truth', 'kind', 'passed', 'complete',
                     'answers', 'valid')


def grid(h, w, fill=0):
    return [[fill for _ in range(w)] for _ in range(h)]


def base_campaign(lease=100):
    return {
        'lease_seconds': lease,
        'items': [
            {'item_id': 'x', 'connectivity': 4, 'grid': [[1, 0], [0, 1]]},
            {'item_id': 'y', 'connectivity': 4, 'grid': [[0, 0], [1, 1]]},
        ],
        'sentinels': [
            {'item_id': 'g1', 'connectivity': 4,
             'grid': [[1, 0]], 'truth': [[1, 0]]},
        ],
    }


class DispatchBehavior(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        self.db = self.td / 'c.db'
        self.c = Collection.initialize(self.db, base_campaign())

    def tearDown(self):
        self.temp.cleanup()

    def qualify(self, worker, t):
        work = self.c.claim(worker, t)
        receipt = self.c.submit(worker, work['token'], t + 1,
                                {'grid': work['grid']})
        self.assertTrue(receipt['passed'])
        return t + 2

    def test_new_worker_starts_on_smallest_sentinel_and_response_is_secret(self):
        work = self.c.claim('A', 0)
        self.assertEqual(set(work), {'token', 'deadline', 'connectivity',
                                     'grid'})
        for forbidden in FORBIDDEN_IN_WORK:
            self.assertNotIn(forbidden, work)
        self.assertEqual(work['grid'], [[1, 0]])          # g1, not a normal
        self.assertEqual(work['deadline'], 100)
        self.assertEqual(work['connectivity'], 4)

    def test_repeat_claim_returns_same_lease_without_extension(self):
        first = self.c.claim('A', 0)
        second = self.c.claim('A', 50)
        self.assertEqual(second['token'], first['token'])
        self.assertEqual(second['deadline'], first['deadline'])
        self.assertEqual(second['grid'], first['grid'])
        # Even a same-T repeat is side-effect free apart from the clock write.
        third = self.c.claim('A', 50)
        self.assertEqual(third['token'], first['token'])

    def test_submit_requires_strict_T_before_deadline(self):
        work = self.c.claim('A', 0)
        token = work['token']
        # T == deadline is already too late.
        with self.assertRaises(LeaseExpired):
            self.c.submit('A', token, 100, {'grid': work['grid']})
        # One second before the deadline is accepted.
        self.c = Collection(self.db)
        receipt = self.c.submit('A', token, 99, {'grid': work['grid']})
        self.assertTrue(receipt['passed'])

    def test_expired_slot_is_reusable_and_old_token_cannot_late_submit(self):
        work = self.c.claim('A', 0)
        with self.assertRaises(LeaseExpired):
            self.c.submit('A', work['token'], 100, {'grid': work['grid']})
        # After expiry the worker may claim again; the new token is fresh.
        again = self.c.claim('A', 100)
        self.assertNotEqual(again['token'], work['token'])
        # The old token can never be consumed, even with a valid answer.
        with self.assertRaises(LeaseConflict):
            self.c.submit('A', work['token'], 101, {'grid': work['grid']})
        self.c.submit('A', again['token'], 102, {'grid': again['grid']})

    def test_two_vote_cap_counts_valid_votes_plus_open_leases(self):
        self.qualify('A', 0)
        self.qualify('B', 2)
        a = self.c.claim('A', 4)                          # A leases x
        b = self.c.claim('B', 5)                          # B leases x (2nd slot)
        # No third worker can take x while both leases are open.
        t = self.qualify('C', 6)
        third = self.c.claim('C', t)
        self.assertEqual(third['grid'], [[0, 0], [1, 1]])  # dispatched y
        self.c.submit('A', a['token'], 11, {'grid': [[1, 1], [0, 1]]})
        self.c.submit('B', b['token'], 12, {'grid': [[1, 0], [0, 0]]})
        status = self.c.status()
        x = next(i for i in status['items'] if i['item_id'] == 'x')
        self.assertEqual(x['state'], 'complete')
        self.assertEqual(x['valid_workers'], ['A', 'B'])
        # x can no longer be claimed: C's debt is clear so the only remaining
        # work is y, after which no_work is returned.
        y_lease = self.c.claim('C', 13)
        self.assertEqual(y_lease['grid'], [[0, 0], [1, 1]])
        self.c.submit('C', y_lease['token'], 14,
                      {'grid': [[0, 0], [1, 1]]})
        self.assertEqual(self.c.claim('C', 15),
                         {'status': 'no_work', 'reason': 'none_available'})

    def test_same_worker_cannot_hold_two_opinions_of_one_item(self):
        self.qualify('A', 0)
        a = self.c.claim('A', 2)
        self.c.submit('A', a['token'], 3, {'grid': a['grid']})
        # A's next normal item must be y, never x again.
        nxt = self.c.claim('A', 4)
        self.assertEqual(nxt['grid'], [[0, 0], [1, 1]])

    def test_string_order_dispatch_within_a_kind(self):
        campaign = {
            'lease_seconds': 100,
            'items': [
                {'item_id': 'zeta', 'connectivity': 4, 'grid': [[1]]},
                {'item_id': 'alpha', 'connectivity': 4, 'grid': [[0]]},
                {'item_id': 'mid', 'connectivity': 4, 'grid': [[1, 0]]},
            ],
            'sentinels': [
                {'item_id': 'g1', 'connectivity': 4,
                 'grid': [[1]], 'truth': [[1]]}],
        }
        db = self.td / 'ordered.db'
        c = Collection.initialize(db, campaign)
        g = c.claim('A', 0)
        c.submit('A', g['token'], 1, {'grid': [[1]]})
        first = c.claim('A', 2)
        self.assertEqual(first['grid'], [[0]])            # alpha < mid < zeta

    def test_tokens_are_globally_unique_even_after_expiry(self):
        first = self.c.claim('A', 0)
        with self.assertRaises(LeaseExpired):
            self.c.submit('A', first['token'], 100,
                          {'grid': first['grid']})
        second = self.c.claim('A', 100)
        third = self.c.claim('B', 101)
        self.assertEqual(len({first['token'], second['token'],
                              third['token']}), 3)

    def test_monotonic_clock_advances_on_success_including_no_work(self):
        self.assertEqual(self.c.status()['clock'], -1)
        self.c.claim('A', 5)                              # jumps to 5
        self.assertEqual(self.c.status()['clock'], 5)
        with self.assertRaises(StaleClock):
            self.c.claim('A', 4)
        # Failures do not move the clock.
        with self.assertRaises(LeaseExpired):
            self.c.submit('A', self.c.claim('A', 5)['token'], 105,
                          {'grid': [[1, 0]]})
        self.assertEqual(self.c.status()['clock'], 5)

    def test_unknown_token_and_foreign_worker_are_protocol_errors(self):
        with self.assertRaises(UnknownLease):
            self.c.submit('A', 'never-issued', 1, {'grid': [[1, 0]]})
        work = self.c.claim('A', 0)
        with self.assertRaises(LeaseConflict):
            self.c.submit('B', work['token'], 1, {'grid': work['grid']})

    def test_disqualified_worker_is_blocked_from_claim_and_submit(self):
        campaign = {
            'lease_seconds': 100,
            'items': [{'item_id': 'x', 'connectivity': 4, 'grid': [[1]]}],
            'sentinels': [{'item_id': 'g1', 'connectivity': 4,
                           'grid': [[1]], 'truth': [[0]]},
                          {'item_id': 'g2', 'connectivity': 4,
                           'grid': [[0]], 'truth': [[0]]}],
        }
        db = self.td / 'dq.db'
        c = Collection.initialize(db, campaign)
        work = c.claim('A', 0)
        c.submit('A', work['token'], 1, {'grid': [[1]]})   # wrong -> DQ
        with self.assertRaises(WorkerDisqualified):
            c.claim('A', 2)
        # A lease granted to another worker can never be first-submitted by
        # the disqualified worker either.
        other = c.claim('B', 3)
        with self.assertRaises(LeaseConflict):
            c.submit('A', other['token'], 4, {'grid': [[0]]})
        # A's own consumed token still returns its first receipt (idempotency
        # outranks disqualification), but cannot create new work.
        retry = c.submit('A', work['token'], 5, {'grid': [[1]]})
        self.assertFalse(retry['passed'])

    def test_restart_restores_quota_and_leases(self):
        self.qualify('A', 0)
        a = self.c.claim('A', 2)                          # open lease on x
        del self.c
        reopened = Collection(self.db)
        # The lease survives restart: repeat claim returns the same token.
        again = reopened.claim('A', 10)
        self.assertEqual(again['token'], a['token'])
        self.assertEqual(again['deadline'], a['deadline'])
        # Another qualified worker sees one slot of x still occupied and
        # takes the remaining x slot (never a third lease on x).
        self.qualify_via(reopened, 'B', 20)
        b = reopened.claim('B', 22)
        self.assertEqual(b['grid'], [[1, 0], [0, 1]])    # x, second slot
        # The restarted open lease still counts and can be submitted after
        # the process restart, well within its original deadline.
        reopened.submit('A', a['token'], 23,
                        {'grid': [[1, 1], [0, 1]]})
        reopened.submit('B', b['token'], 24,
                        {'grid': [[1, 0], [0, 0]]})
        status = reopened.status()
        x = next(i for i in status['items'] if i['item_id'] == 'x')
        self.assertEqual(x['state'], 'complete')
        self.assertEqual(x['valid_workers'], ['A', 'B'])

    def qualify_via(self, handle, worker, t):
        work = handle.claim(worker, t)
        handle.submit(worker, work['token'], t + 1, {'grid': work['grid']})

    def test_work_response_never_mentions_sentinel_or_completion_fields(self):
        work = self.c.claim('A', 0)
        raw = json.dumps(work)
        for word in ('sentinel', 'gold', 'truth', 'item', 'worker',
                     'complete', 'progress'):
            self.assertNotIn(word, raw)

    def test_claim_precommit_failure_rolls_back_token_and_clock(self):
        def abort(phase, worker, token, info):
            if phase == 'claim' and not info.get('repeat'):
                raise RuntimeError('injected claim failure')

        failing = Collection(self.db, precommit_hook=abort)
        with self.assertRaises(RuntimeError):
            failing.claim('A', 0)
        import sqlite3
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(
                conn.execute('SELECT COUNT(*) FROM leases').fetchone()[0], 0)
            self.assertEqual(
                conn.execute('SELECT COUNT(*) FROM workers').fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT value FROM meta WHERE key='token_seq'"
                             ).fetchone()[0], '0')
            self.assertEqual(
                conn.execute("SELECT value FROM meta WHERE key='clock'"
                             ).fetchone()[0], '-1')
        # A fresh process/handle retries cleanly and gets the first token.
        ok = Collection(self.db).claim('A', 0)
        self.assertTrue(ok['token'].startswith('00000001-'))


class TwoProcessLastSlot(unittest.TestCase):
    """Two processes race for the same final slot; at most one new lease."""

    DRIVER = r'''
import sys, time
from pathlib import Path
from raster_objects.collection import Collection, CollectionBusy

db, worker, now, barrier = sys.argv[1:5]
now = int(now)
barrier = Path(barrier)

# Both processes become ready *before* touching the database, then are
# released together so their BEGIN IMMEDIATE transactions genuinely contend
# for the same final slot (a loser can fail with busy before any hook runs).
(barrier / (worker + "-ready")).touch()
deadline = time.time() + 30
while not (barrier / "go").exists():
    if time.time() > deadline:
        raise RuntimeError("barrier timeout")
    time.sleep(0.005)

for _ in range(50):
    try:
        report = Collection(db).claim(worker, now)
        if report.get("status") == "no_work":
            print("NO-WORK:" + report["reason"])
        else:
            print(report["token"])
        break
    except CollectionBusy:
        time.sleep(0.01)
else:
    print("BUSY-EXHAUSTED")
'''

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        self.barrier = self.td / 'barrier'
        self.barrier.mkdir()
        self.db = self.td / 'c.db'
        self.driver = self.td / 'driver.py'
        self.driver.write_text(self.DRIVER)
        campaign = {
            'lease_seconds': 1000,
            'items': [{'item_id': 'only', 'connectivity': 4,
                       'grid': [[1, 0]]}],
            'sentinels': [{'item_id': 'g1', 'connectivity': 4,
                           'grid': [[1]], 'truth': [[1]]}],
        }
        c = Collection.initialize(self.db, campaign)
        # All three racers must first clear their initial sentinel debt, so
        # every race claim is a normal-item dispatch (otherwise a brand-new
        # worker is correctly sent to the sentinel instead of the slot).
        for worker, t in (('A', 0), ('B', 2), ('C', 4)):
            g = c.claim(worker, t)
            c.submit(worker, g['token'], t + 1, {'grid': [[1]]})
        # A occupies one of the two slots; the race decides the final slot.
        a = c.claim('A', 10)
        self.assertEqual(a['grid'], [[1, 0]])

    def tearDown(self):
        self.temp.cleanup()

    def start(self, worker):
        return subprocess.Popen(
            [sys.executable, '-P', str(self.driver), str(self.db),
             worker, '20', str(self.barrier)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV, cwd=ROOT)

    def test_at_most_one_winner(self):
        p1 = self.start('B')
        p2 = self.start('C')
        deadline = time.time() + 30
        while len(list(self.barrier.glob('*-ready'))) < 2:
            if time.time() > deadline:
                self.fail('drivers never reached the barrier')
            time.sleep(0.01)
        (self.barrier / 'go').touch()
        out1, err1 = p1.communicate(timeout=30)
        out2, err2 = p2.communicate(timeout=30)
        self.assertEqual(p1.returncode, 0, err1.decode())
        self.assertEqual(p2.returncode, 0, err2.decode())
        r1, r2 = out1.decode().strip(), out2.decode().strip()
        # Exactly one process wins the final slot with a real token; the
        # loser is side-effect free (an explicit busy loss or a no_work
        # observation) and must never invent a third lease.
        results = sorted((r1, r2))
        winners = [r for r in results if not r.startswith(('BUSY', 'NO-WORK'))]
        losers = [r for r in results if r.startswith(('BUSY', 'NO-WORK'))]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(losers), 1, results)
        import sqlite3
        with sqlite3.connect(self.db) as conn:
            open_leases = conn.execute(
                "SELECT worker_id, token FROM leases WHERE state='open'"
            ).fetchall()
        self.assertEqual(len(open_leases), 2)
        workers = {w for w, _ in open_leases}
        self.assertEqual(workers, {'A', 'B'} if
                         not r1.startswith(('BUSY', 'NO-WORK'))
                         else {'A', 'C'})


class ThreadedRaces(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        self.db = self.td / 'c.db'
        campaign = {
            'lease_seconds': 100,
            'items': [{'item_id': 'n%d' % i, 'connectivity': 4,
                       'grid': [[0, 1]]} for i in range(50)],
            'sentinels': [{'item_id': 'g1', 'connectivity': 4,
                           'grid': [[1]], 'truth': [[1]]}],
        }
        Collection.initialize(self.db, campaign)

    def tearDown(self):
        self.temp.cleanup()

    def test_concurrent_claims_are_serializable_with_busy_errors(self):
        outcomes = []
        lock = threading.Lock()
        clock = {'t': 0}

        def next_time():
            # A shared monotonic counter keeps every retry at or above the
            # persisted logical clock, so races only ever surface as busy.
            with lock:
                clock['t'] += 1
                return clock['t']

        def worker(worker_id):
            handle = Collection(self.db)

            def claim():
                for _ in range(200):
                    try:
                        return handle.claim(worker_id, next_time())
                    except CollectionBusy:
                        time.sleep(0.005)
                self.fail('busy budget exhausted for ' + worker_id)

            def submit(token, grid):
                for _ in range(200):
                    try:
                        return handle.submit(worker_id, token,
                                             next_time(), {'grid': grid})
                    except CollectionBusy:
                        time.sleep(0.005)
                self.fail('busy budget exhausted submitting for ' + worker_id)

            g = claim()
            submit(g['token'], [[1]])
            for _ in range(200):
                try:
                    work = handle.claim(worker_id, next_time())
                    with lock:
                        outcomes.append(work['grid'])
                    return
                except CollectionBusy:
                    time.sleep(0.005)
            with lock:
                outcomes.append('BUSY-EXHAUSTED')

        threads = [threading.Thread(target=worker, args=('w%d' % i,))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertNotIn('BUSY-EXHAUSTED', outcomes)
        # Every observed dispatch is a real item; no normal item was leased
        # to more than two workers (sentinel leases are unlimited).
        import sqlite3
        with sqlite3.connect(self.db) as conn:
            counts = conn.execute(
                "SELECT item_id, COUNT(*) FROM leases WHERE kind='normal' "
                "GROUP BY item_id"
            ).fetchall()
        self.assertTrue(all(count <= 2 for _id, count in counts), counts)
        # One open lease per worker is enforced by the partial unique index.
        with sqlite3.connect(self.db) as conn:
            bad = conn.execute(
                "SELECT worker_id FROM leases WHERE state='open' "
                "GROUP BY worker_id HAVING COUNT(*) > 1").fetchall()
        self.assertEqual(bad, [])


if __name__ == '__main__':
    unittest.main()
