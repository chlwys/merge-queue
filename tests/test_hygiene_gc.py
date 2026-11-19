"""State hygiene: orphan pruning in gc, the shared receipt lookup, bounded tail reads, lock-aware stale sweeps and
submission that never holds the submit lock across a slow planner. All processes and repositories are fixture-owned."""
import copy
import fcntl
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixture import Fixture, MQ_CMD, UNIT_POLICY, load_module

DAY = 86400


def age(path, days=3):
    old = time.time() - days * DAY
    os.utime(path, (old, old))


class GcOrphans(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.mq = load_module()
        self.ctx = self.mq.load_ctx(str(self.fx.primary))
        self.state = self.fx.state

    def make(self, relative, text="x\n", old=True):
        path = self.state / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if old:
            age(path)
        return path

    def orphan_files(self, rid):
        return [self.make(f"locks/{rid}.rw"), self.make(f"locks/{rid}.lock"), self.make(f"locks/mcp-{rid}.lock"),
                self.make(f"mcp-receipts/{rid}.json", "{}\n")]

    def test_removes_stale_orphans_and_keeps_live_recent_and_fixed(self):
        gone = self.orphan_files("orphan-old")
        recent = self.orphan_files("orphan-new")
        for path in recent:
            os.utime(path, None)
        live = self.orphan_files("has-request")
        self.make("requests/has-request.json", "{}\n", old=False)
        tombed = self.orphan_files("has-tombstone")
        self.make("tombstones/has-tombstone.json", "{}\n", old=False)
        fixed = [self.make(f"locks/{name}.lock") for name in ("integration", "submit", "runner")]
        unrelated = self.make("locks/notes.txt")
        self.mq.gc(self.ctx)
        self.assertFalse(any(p.exists() for p in gone))
        for path in (*recent, *live, *tombed, *fixed, unrelated):
            self.assertTrue(path.exists(), path)

    def test_busy_lock_is_kept_and_released_one_is_removed(self):
        busy_rw, busy_lock, *_ = self.orphan_files("busy")
        held = [self.mq.FileLock(p, blocking=False) for p in (busy_rw, busy_lock)]
        for lock in held:
            self.assertTrue(lock.acquire())
        try:
            self.mq.gc(self.ctx)
            self.assertTrue(busy_rw.exists() and busy_lock.exists())
        finally:
            for lock in held:
                lock.release()
        self.mq.gc(self.ctx)
        self.assertFalse(busy_rw.exists() or busy_lock.exists())

    def test_mcp_named_lock_of_an_existing_request_is_kept(self):
        # `mcp-<id>.lock` is also the landing lock of a request that happens to be named `mcp-<id>`.
        lock = self.make("locks/mcp-job.lock")
        self.make("requests/mcp-job.json", "{}\n", old=False)
        self.mq.gc(self.ctx)
        self.assertTrue(lock.exists())

    def test_mcp_intent_is_kept_while_its_bound_receipt_is_known(self):
        # An intent under requested id R may be bound (`queue_request_id`) to another canonical receipt C by exact-SHA
        # dedup; R itself has no request or tombstone, but `wait_for_result(R)` must keep resolving to C.
        bound = self.make("mcp-receipts/alias.json", '{"queue_request_id": "canon"}\n')
        self.make("requests/canon.json", "{}\n", old=False)
        tombed = self.make("mcp-receipts/alias-t.json", '{"queue_request_id": "canon-t"}\n')
        self.make("tombstones/canon-t.json", "{}\n", old=False)
        unknown = self.make("mcp-receipts/alias-gone.json", '{"queue_request_id": "nowhere"}\n')
        unbound = self.make("mcp-receipts/unbound.json", '{"owner_id": "o"}\n')
        unreadable = self.make("mcp-receipts/garbled.json", "{not json")
        recent = self.make("mcp-receipts/alias-recent.json", '{"queue_request_id": "nowhere"}\n', old=False)
        self.mq.gc(self.ctx)
        self.assertTrue(bound.exists() and tombed.exists() and recent.exists())
        self.assertFalse(unknown.exists() or unbound.exists() or unreadable.exists())

    def test_empty_inbox_owner_directories(self):
        inbox = self.state / "inbox"
        empty_old, empty_new, full = inbox / "empty-old", inbox / "empty-new", inbox / "full"
        for directory in (empty_old, empty_new, full):
            directory.mkdir(parents=True)
        (full / "req.json").write_text("{}\n")
        age(empty_old)
        age(full)
        self.mq.gc(self.ctx)
        self.assertFalse(empty_old.exists())
        self.assertTrue(empty_new.exists())  # emptied recently: a writer may be about to use it
        self.assertTrue((full / "req.json").exists())


class SharedHelpers(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.mq = load_module()
        self.ctx = self.mq.load_ctx(str(self.fx.primary))

    def test_receipt_finds_request_tombstone_and_unknown(self):
        wt, _ = self.fx.agent("r", {"extra.py": "X = 1\n"})
        self.fx.submit(wt, "rcpt-live")
        live = self.mq.receipt(self.ctx, "rcpt-live")
        self.assertEqual(live["request_id"], "rcpt-live")
        self.mq.write_json(self.ctx.p("tombstones", "rcpt-old.json"), {"request_id": "rcpt-old", "state": "landed"})
        self.assertEqual(self.mq.receipt(self.ctx, "rcpt-old")["state"], "landed")
        self.assertIsNone(self.mq.receipt(self.ctx, "rcpt-none"))
        with self.assertRaises(self.mq.MqError):
            self.mq.receipt(self.ctx, "../escape")

    def test_tail_reads_only_the_end_of_a_large_log(self):
        path = Path(self.fx.root) / "big.log"
        body = "".join(f"line {i:06d} " + "x" * 40 + "\n" for i in range(5000))
        path.write_text(body)
        self.assertGreater(path.stat().st_size, 64 * 1024)
        expected = "\n".join(body.splitlines()[-20:])[-2000:]
        self.assertEqual(self.mq.tail(path), expected)
        self.assertEqual(self.mq.tail(path, lines=3, limit=50), "\n".join(body.splitlines()[-3:])[-50:])
        small = Path(self.fx.root) / "small.log"
        small.write_text("a\nb\nc\n")
        self.assertEqual(self.mq.tail(small), "a\nb\nc")
        self.assertEqual(self.mq.tail(Path(self.fx.root) / "missing.log"), "")


class StaleSweepLiveness(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.mq = load_module()
        self.ctx = self.mq.load_ctx(str(self.fx.primary))

    def stale_attempt(self, rid):
        attempt = self.fx.state / "attempts" / f"{rid}.json"
        self.mq.write_json(attempt, {"identity": None, "attempt": "dead0001", "group": None})
        work = self.fx.state / "work" / f"{rid}-dead0001"
        work.mkdir(parents=True)
        return attempt, work

    def test_held_request_lock_marks_the_attempt_live(self):
        attempt, work = self.stale_attempt("sweep-busy")
        lock = self.mq.FileLock(self.fx.state / "locks" / "sweep-busy.lock", blocking=False)
        self.assertTrue(lock.acquire())
        try:
            self.mq.sweep_stale(self.ctx)
            self.assertTrue(attempt.exists() and work.exists())
        finally:
            lock.release()
        self.mq.sweep_stale(self.ctx)  # the lock is free again: the attempt is dead and is cleaned as before
        self.assertFalse(attempt.exists() or work.exists())

    def test_own_request_lock_does_not_protect_its_own_stale_attempt(self):
        attempt, work = self.stale_attempt("sweep-own")
        lock = self.mq.FileLock(self.fx.state / "locks" / "sweep-own.lock", blocking=False)
        self.assertTrue(lock.acquire())
        try:
            self.mq.sweep_stale(self.ctx, own="sweep-own")
            self.assertFalse(attempt.exists() or work.exists())
        finally:
            lock.release()


class SlowPlannerDoesNotHoldSubmitLock(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        policy = copy.deepcopy(UNIT_POLICY)
        policy["checks"]["presentation"] = {"argv": [sys.executable, "-c", "pass"], "timeout": 10}
        policy["rules"].append({"match": ["docs/*.css"], "checks": ["presentation"]})
        self.marker = self.fx.root / "planner-runs"
        policy["fast_path"] = {"plan": [sys.executable, "{checkout}/slow-plan.py", str(self.marker), "{target}",
                                        "{candidate}"], "checks": ["presentation"], "timeout": 20}
        self.fx.write(self.fx.primary, "slow-plan.py",
                      "import json, sys, time\n"
                      "with open(sys.argv[1], 'a') as f:\n    f.write('run\\n')\n"
                      "time.sleep(float(__import__('os').environ.get('PLANNER_SLEEP', '0')) or 0)\n"
                      "print(json.dumps(dict(eligible=False, reason='slow planner says normal')))\n")
        self.fx.git("add", "slow-plan.py")
        self.fx.git("commit", "-q", "-m", "trusted planner")
        self.fx.set_policy(policy)

    def runs(self):
        return self.marker.read_text().count("run") if self.marker.exists() else 0

    def test_submit_lock_is_free_while_the_planner_runs(self):
        fx = self.fx
        wt, _ = fx.agent("slow", {"docs/slow.css": "body { color: red; }\n"})
        proc = subprocess.Popen([*MQ_CMD, "submit", "--request-id", "slow-1", "--json"], cwd=wt,
                                env=dict(fx.env, PLANNER_SLEEP="3"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        try:
            fx.wait_for(lambda: self.runs() >= 1, 30, "the planner to start")
            lock_path = fx.state / "locks" / "submit.lock"
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises while the submit holds it
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            self.assertIsNone(proc.poll(), "planner finished before the lock was probed")
        finally:
            out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, out + err)
        req = fx.req("slow-1")
        self.assertEqual((req["lane"], req["fast_path"]["reason"]), ("normal", "slow planner says normal"))

    def test_duplicates_do_not_run_the_planner(self):
        fx = self.fx
        wt, _ = fx.agent("dup", {"docs/dup.css": "body { color: red; }\n"})
        fx.submit(wt, "dup-1")
        self.assertEqual(self.runs(), 1)
        _, again = fx.submit(wt, "dup-1")  # same id and commit
        _, other = fx.submit(wt, "dup-2")  # other id, same live commit: the live request is returned
        self.assertEqual(self.runs(), 1)
        self.assertEqual(other["request_id"], "dup-1")
        self.assertIsNone(fx.req("dup-2"))
        self.assertFalse((fx.state / "evidence" / "dup-2").exists())
        self.assertEqual(list((fx.state / "routing").glob("dup-2*")) if (fx.state / "routing").exists() else [], [])


if __name__ == "__main__":
    unittest.main()
