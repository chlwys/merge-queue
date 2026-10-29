"""Integration tests for the merge-queue core (`python -m merge_queue.core`).

Every scenario runs against temporary fixture Git repositories with the real Block
`tq` (pinned 0.4.1) scheduling the landings. Nothing here touches a real repository.
The suite is safe inside the queue's own mandatory validation: every fixture has its own repository,
state directory, tq data directory and validation policy (tiny tests/ directory only, never this suite),
and the fixture environment drops GIT_*, MERGE_QUEUE_*, TASK_QUEUE_*, XDG_* and session variables.
"""
import json
import os
import plistlib
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixture import CORE_FILE, MQ_CMD, ROOT, SRC, TQ, UNIT_POLICY, Fixture, load_module  # noqa: E402


class QueueCase(unittest.TestCase):
    fixture_args: dict = {}

    @classmethod
    def setUpClass(cls):
        if not os.access(TQ, os.X_OK):
            raise AssertionError(f"Block tq not found ({TQ}); install with: uv tool install agent-task-queue==0.4.1")

    def setUp(self):
        self.fx = Fixture(**self.fixture_args)
        self.addCleanup(self.fx.close)


class FifoAndOutcomes(QueueCase):
    def test_a_two_clean_merges_land_in_fifo_order(self):
        fx = self.fx
        a_wt, a_sha = fx.agent("a", {"a.py": "A = 1\n"})
        b_wt, b_sha = fx.agent("b", {"b.py": "B = 1\n"})
        fx.write(b_wt, "scratch.txt", "uncommitted work\n")
        fx.start_runner()
        base = fx.main()
        self.assertEqual(fx.submit(a_wt, "req-a")[0].returncode, 0)
        self.assertEqual(fx.submit(b_wt, "req-b")[0].returncode, 0)
        states = fx.wait_states(["req-a", "req-b"])
        self.assertEqual({k: v["state"] for k, v in states.items()}, {"req-a": "landed", "req-b": "landed"})
        self.assertEqual(fx.subjects(), ["b", "a", "base"])
        self.assertEqual(states["req-a"]["result"]["landed_sha"], a_sha)  # already contained the target
        self.assertNotEqual(states["req-b"]["result"]["landed_sha"], b_sha)  # rebased onto a
        self.assertEqual(fx.main(), states["req-b"]["result"]["landed_sha"])
        self.assertEqual(fx.git("rev-parse", "agent/b").stdout.strip(), b_sha)  # worker branch untouched
        self.assertEqual((b_wt / "scratch.txt").read_text(), "uncommitted work\n")
        self.assertEqual(fx.git("status", "--porcelain", cwd=b_wt).stdout.strip(), "?? scratch.txt")
        self.assertTrue((fx.primary / "a.py").exists() and (fx.primary / "b.py").exists())
        self.assertEqual(fx.git("status", "--porcelain").stdout, "")
        self.assertEqual(states["req-a"]["target_sha"], base)
        for req in states.values():
            for key in ("queue_wait_seconds", "prepare_seconds", "validation_seconds", "lock_acquired_at"):
                self.assertIn(key, req["timings"])
        fx.wait_for(lambda: len([e for e in fx.tq_events() if e["event"] == "task_completed"]) == 2, 20, "tq metrics")
        self.assertEqual([e["exit_code"] for e in fx.tq_events() if e["event"] == "task_completed"], [0, 0])
        self.assertEqual(fx.inbox("wt-a") + fx.inbox("wt-b"), [])
        self.assertEqual(len(fx.worktrees()), 3)  # no leftover candidate workspace
        self.assertEqual(fx.refs(), [])
        self.assertEqual(fx.mq("inbox", "--quiet", cwd=b_wt).returncode, 0)
        # Receipts must not claim logs that were pruned with the successful evidence.
        self.assertFalse((fx.state / "evidence" / "req-a").exists())
        for req in states.values():
            self.assertTrue(req["validation"] and all(step["log"] is None for step in req["validation"]))
            self.assertIs(req["result"]["logs_retained"], False)

    def test_submit_wait_is_the_single_command_and_starts_the_runner_itself(self):
        fx = self.fx
        env = {k: v for k, v in fx.env.items() if k != "MERGE_QUEUE_NO_AUTORUNNER"}
        a_wt, a_sha = fx.agent("a", {"a.py": "A = 1\n"})
        result, data = fx.mqj("submit", "--wait", "--timeout", "60", cwd=a_wt, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((data["state"], data["result"]["landed_sha"]), ("landed", a_sha))
        self.assertEqual(fx.mq("status", cwd=a_wt, env=env, check=True).returncode, 0)
        second = fx.mq("runner", "--drain", env=env)  # another runner defers to the active one
        self.assertEqual(second.returncode, 0)

    def test_b_conflict_returns_to_owner_and_retry_lands_at_tail(self):
        fx = self.fx
        a_wt, _ = fx.agent("a", {"shared.py": "TITLE = 'alpha'\n"})
        b_wt, b_sha = fx.agent("b", {"shared.py": "TITLE = 'beta'\n"})
        c_wt, _ = fx.agent("c", {"c.py": "C = 1\n"})
        fx.start_runner()
        for wt, rid in ((a_wt, "req-a"), (b_wt, "req-b"), (c_wt, "req-c")):
            fx.submit(wt, rid)
        states = fx.wait_states(["req-a", "req-b", "req-c"])
        self.assertEqual([states[i]["state"] for i in ("req-a", "req-b", "req-c")], ["landed", "conflict", "landed"])
        entry, = fx.inbox("wt-b")
        self.assertEqual(entry["conflicting_files"], ["shared.py"])
        self.assertEqual(entry["failure_type"], "conflict")
        self.assertEqual(entry["source_commit_sha"], b_sha)
        self.assertEqual(entry["target_commit_sha"], states["req-b"]["target_sha"])
        self.assertTrue(entry["required_action"] and Path(entry["relevant_log_paths"][0]).exists())
        self.assertEqual(fx.mq("inbox", "--quiet", cwd=b_wt).returncode, 10)
        self.assertEqual(fx.mq("result", "req-b", cwd=b_wt).returncode, 10)
        self.assertEqual(fx.subjects(), ["c", "a", "base"])  # slot was free for c
        self.assertEqual(fx.git("worktree", "list").stdout.count("\n"), 4)
        # An unchanged commit is not retried and not resubmitted.
        self.assertEqual(fx.mq("retry", "req-b", cwd=b_wt).returncode, 4)
        self.assertEqual(fx.mq("submit", cwd=b_wt).returncode, 4)
        # The owner repairs in its own worktree and resubmits.
        rebase = fx.git("rebase", "main", cwd=b_wt, check=False)
        self.assertNotEqual(rebase.returncode, 0)
        fx.write(b_wt, "shared.py", "TITLE = 'alpha+beta'\n")
        fx.git("add", "shared.py", cwd=b_wt)
        fx.git("-c", "core.editor=true", "rebase", "--continue", cwd=b_wt)
        result, data = fx.mqj("retry", "req-b", "--wait", cwd=b_wt)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(data["state"], "landed")
        new_id = data["request_id"]
        self.assertGreater(fx.req(new_id)["seq"], fx.req("req-c")["seq"])  # back of the FIFO
        self.assertEqual((fx.primary / "shared.py").read_text(), "TITLE = 'alpha+beta'\n")
        self.assertEqual(fx.inbox("wt-b"), [])
        again, dup = fx.mqj("retry", "req-b", cwd=b_wt)
        self.assertEqual(dup["request_id"], new_id)  # idempotent
        self.assertEqual(fx.mq("ack", "--all", cwd=b_wt).returncode, 0)
        self.assertIsNone(fx.req("req-b"))
        self.assertTrue((fx.state / "tombstones" / f"{new_id}.json").exists())

    def test_c_validation_failure_blocks_publication(self):
        fx = self.fx
        one_wt, _ = fx.agent("one", {"lib.py": "def value():\n    return 2\n",
                                     "tests/test_lib.py": "import unittest\nfrom lib import value\n\n\nclass T(unittest.TestCase):\n"
                                                          "    def test_value(self):\n        self.assertEqual(value(), 2)\n"})
        two_wt, two_sha = fx.agent("two", {"tests/test_new.py": "import unittest\nfrom lib import value\n\n\n"
                                                              "class N(unittest.TestCase):\n    def test_new(self):\n"
                                                              "        self.assertEqual(value(), 1)\n"})
        fx.start_runner()
        fx.submit(one_wt, "req-one")
        fx.submit(two_wt, "req-two")
        states = fx.wait_states(["req-one", "req-two"])
        self.assertEqual(states["req-one"]["state"], "landed")
        self.assertEqual(states["req-two"]["state"], "validation_failed")
        self.assertEqual(fx.main(), states["req-one"]["result"]["landed_sha"])  # nothing published
        entry, = fx.inbox("wt-two")
        self.assertEqual(entry["failure_type"], "validation_failed")
        self.assertIn("AssertionError", entry["summary"])
        log = Path(entry["relevant_log_paths"][0])
        self.assertIn("test_new", log.read_text())
        self.assertEqual(states["req-two"]["validation"][-1]["exit"], 1)
        self.assertEqual(fx.worktrees().count(str(fx.primary)), 1)
        self.assertFalse(any((fx.state / "work").iterdir()))

    def test_d_five_concurrent_submissions_are_serialised_in_order(self):
        fx = self.fx
        agents = [fx.agent(f"m{i}", {f"m{i}.py": f"M = {i}\n"}) for i in range(5)]
        fx.start_runner()
        observed = {"max_running": 0, "max_waiting": 0}
        stop = threading.Event()

        def sample():
            while not stop.is_set():
                result = subprocess.run([TQ, "--data-dir", str(fx.state / "tq"), "list", "--json"],
                                        capture_output=True, text=True)
                try:
                    tasks = json.loads(result.stdout)["tasks"]
                except ValueError:
                    continue
                observed["max_running"] = max(observed["max_running"], sum(t["status"] == "running" for t in tasks))
                observed["max_waiting"] = max(observed["max_waiting"], sum(t["status"] == "waiting" for t in tasks))
                time.sleep(0.03)

        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        procs = [subprocess.Popen([*MQ_CMD, "submit", "--request-id", f"r{i}", "--json"],
                                  cwd=wt, env=fx.env, stdout=subprocess.PIPE, text=True)
                 for i, (wt, _) in enumerate(agents)]
        for proc in procs:
            proc.communicate()
            self.assertEqual(proc.returncode, 0)
        ids = [f"r{i}" for i in range(5)]
        states = fx.wait_states(ids)
        fx.wait_for(lambda: all("lock_released_at" in fx.req(i)["timings"] for i in ids), 20, "lock release stamps")
        states = {i: fx.req(i) for i in ids}
        stop.set()
        sampler.join()
        self.assertEqual({r["state"] for r in states.values()}, {"landed"})
        order = sorted(states.values(), key=lambda r: r["seq"])
        self.assertEqual(sorted(r["seq"] for r in order), [1, 2, 3, 4, 5])
        stamps = [(r["timings"]["lock_acquired_at"], r["timings"]["lock_released_at"]) for r in order]
        for (_, released), (acquired, _) in zip(stamps, stamps[1:]):
            self.assertLess(released, acquired)  # single writer, landing order == accepted order
        self.assertEqual(fx.subjects()[:-1][::-1], [f"m{int(r['request_id'][1:])}" for r in order])
        self.assertEqual(observed["max_running"], 1)
        self.assertGreaterEqual(observed["max_waiting"], 1)  # tq really queued waiters
        running = peak = 0
        for e in sorted((e for e in fx.tq_events() if e["event"] in ("task_started", "task_completed")),
                        key=lambda e: e["timestamp"]):
            running += 1 if e["event"] == "task_started" else -1
            peak = max(peak, running)
        self.assertEqual(peak, 1)
        fx.wait_for(lambda: sum(e["event"] == "task_completed" for e in fx.tq_events()) == 5, 20, "tq metrics")
        self.assertTrue(all((fx.primary / f"m{i}.py").exists() for i in range(5)))


class CrashAndRaces(QueueCase):
    def landed_once(self, fx, subject, ids):
        states = fx.wait_states(ids)
        fx.settled()
        states = {i: fx.req(i) for i in ids}
        self.assertEqual({r["state"] for r in states.values()}, {"landed"}, fx.debug())
        self.assertEqual(fx.subjects().count(subject), 1)
        self.assertEqual(fx.git("status", "--porcelain").stdout, "")
        self.assertEqual(fx.refs(), [])
        self.assertEqual(list((fx.state / "auth").glob("*.json")) if (fx.state / "auth").exists() else [], [])
        self.assertFalse([w for w in fx.worktrees() if str(fx.state) in w], fx.worktrees())
        return states

    def test_e_crash_at_every_stage_recovers_without_duplicates(self):
        for stage in ("before_prepare", "after_prepare", "after_validate", "after_receipt", "after_merge"):
            with self.subTest(stage=stage):
                fx = Fixture()
                try:
                    x_wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
                    y_wt, _ = fx.agent("y", {"y.py": "Y = 1\n"})
                    fx.start_runner(MERGE_QUEUE_FAULT=f"{stage}:exit:1")
                    fx.submit(x_wt, "req-x")
                    fx.submit(y_wt, "req-y")
                    states = self.landed_once(fx, "x", ["req-x", "req-y"])
                    self.assertEqual(fx.subjects().count("y"), 1)
                    self.assertEqual(states["req-x"]["attempts"], 2)
                    self.assertEqual(states["req-x"]["dispatches"], 2)
                    if stage == "after_merge":
                        self.assertIn("reconciled", states["req-x"]["result"]["note"])
                finally:
                    fx.close()

    def test_e_runner_killed_mid_landing_is_adopted_without_duplicate_dispatch(self):
        fx = self.fx
        gate = fx.root / "gate"
        x_wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        y_wt, _ = fx.agent("y", {"y.py": "Y = 1\n"})
        first = fx.start_runner(MERGE_QUEUE_FAULT=f"after_validate:wait={gate}:1")
        fx.submit(x_wt, "req-x")
        fx.wait_for(lambda: Path(str(gate) + ".reached").exists(), 30, "landing to reach validation end")
        first.kill()
        first.wait()
        self.assertEqual(fx.req("req-x")["state"], "running")
        fx.start_runner()
        fx.submit(y_wt, "req-y")
        time.sleep(1.5)  # a duplicate dispatch of x would show up by now
        self.assertEqual(fx.req("req-x")["dispatches"], 1)
        gate.write_text("go")
        states = self.landed_once(fx, "x", ["req-x", "req-y"])
        self.assertEqual(states["req-x"]["dispatches"], 1)
        self.assertEqual(sum(e["event"] == "task_queued" for e in fx.tq_events()), 2)

    def test_e_sigterm_on_tq_reaps_validation_and_requeues(self):
        marker, pids = self.fx.root / "marker", self.fx.root / "pids"
        script = self.fx.root / "slowcheck.py"
        script.write_text("import os, subprocess, sys, time\nfrom pathlib import Path\n"
                          f"marker, pids = Path({str(marker)!r}), Path({str(pids)!r})\n"
                          "if marker.exists():\n    sys.exit(0)\nmarker.write_text('1')\n"
                          "child = subprocess.Popen(['sleep', '60'])\n"
                          "pids.write_text(f'{os.getpid()} {child.pid}')\ntime.sleep(60)\n")
        self.fx.set_policy({**UNIT_POLICY, "checks": {"unit": {"argv": [sys.executable, str(script)], "timeout": 120}}})
        x_wt, _ = self.fx.agent("x", {"x.py": "X = 1\n"})
        self.fx.start_runner()
        self.fx.submit(x_wt, "req-x")
        self.fx.wait_for(pids.exists, 30, "validation to start")
        check_pid, child_pid = map(int, pids.read_text().split())
        os.kill(self.fx.req("req-x")["tq"]["pid"], signal.SIGTERM)

        def dead(pid):
            return subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip() \
                in ("", ) or subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True,
                                            text=True).stdout.strip().startswith("Z")
        self.fx.wait_for(lambda: dead(check_pid) and dead(child_pid), 20, "validation group to be reaped")
        states = self.landed_once(self.fx, "x", ["req-x"])
        self.assertEqual(states["req-x"]["dispatches"], 2)
        self.assertEqual(states["req-x"]["attempts"], 1)  # an interruption is not a failed attempt

    def test_f_target_changed_after_validation_refreshes(self):
        for stage in ("after_validate", "after_receipt"):
            with self.subTest(stage=stage):
                fx = Fixture()
                try:
                    gate = fx.root / "gate"
                    x_wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
                    fx.start_runner(MERGE_QUEUE_FAULT=f"{stage}:wait={gate}:1")
                    fx.submit(x_wt, "req-x")
                    fx.wait_for(lambda: Path(str(gate) + ".reached").exists(), 30, f"landing to reach {stage}")
                    stale_target = fx.req("req-x")["candidate"]["target_sha"]
                    # An external actor bypasses the queue (same-user override of the hook).
                    fx.write(fx.primary, "ext.py", "EXT = 1\n")
                    fx.git("add", "ext.py")
                    fx.git("-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "external")
                    external = fx.main()
                    gate.write_text("go")
                    state = fx.wait_states(["req-x"])["req-x"]
                    self.assertEqual(state["state"], "landed", fx.debug())
                    self.assertEqual(fx.subjects(), ["x", "external", "base"])
                    self.assertEqual(state["candidate"]["target_sha"], external)
                    self.assertNotEqual(external, stale_target)
                    events = (fx.state / "diagnostics" / "events.jsonl").read_text()
                    self.assertIn("target_moved", events)
                    self.assertEqual(fx.git("status", "--porcelain").stdout, "")
                finally:
                    fx.close()

    def test_g_duplicates_and_moving_source_branch(self):
        fx = self.fx
        x_wt, x_sha = fx.agent("x", {"x.py": "X = 1\n"})
        first, data = fx.submit(x_wt, "req-x")
        self.assertEqual(first.returncode, 0)
        again, dup = fx.submit(x_wt, "req-x")
        self.assertTrue(dup["duplicate"])
        other, same_sha = fx.submit(x_wt, "req-other-id")  # same exact commit, different id
        self.assertEqual(same_sha["request_id"], "req-x")
        self.assertEqual(len(list((fx.state / "requests").glob("*.json"))), 1)
        reuse = fx.mq("submit", "--request-id", "req-x", "--commit", fx.main(), cwd=x_wt)
        self.assertEqual(reuse.returncode, 4)  # ids are bound to one commit
        fx.write(x_wt, "dirty.py", "pending = 1\n")
        fx.git("add", "dirty.py", cwd=x_wt)
        self.assertEqual(fx.mq("submit", cwd=x_wt).returncode, 2)  # uncommitted tracked changes
        # The worker keeps committing after submission: only the pinned commit lands.
        fx.git("commit", "-q", "-m", "x-later", cwd=x_wt)
        fx.write(x_wt, "later.py", "L = 1\n")
        self.assertNotEqual(fx.git("rev-parse", "agent/x").stdout.strip(), x_sha)
        mismatch = fx.mq("submit", "--commit", x_sha, "--branch", "agent/x", cwd=x_wt)
        self.assertEqual(mismatch.returncode, 2)
        fx.start_runner()
        fx.wait_states(["req-x"])
        self.assertEqual(fx.req("req-x")["state"], "landed")
        self.assertEqual(fx.subjects(), ["x", "base"])
        self.assertFalse((fx.primary / "dirty.py").exists())
        # Resubmitting the landed commit is recognised, and the new tip is a new request.
        landed, known = fx.submit(x_wt, "req-new", "--commit", x_sha)
        self.assertEqual(known["request_id"], "req-x")
        tip, tip_data = fx.submit(x_wt, "req-tip")
        self.assertEqual(tip.returncode, 0)
        fx.wait_states(["req-tip"])
        self.assertEqual(fx.subjects(), ["x-later", "x", "base"])

    def test_infra_failures_retry_boundedly_then_surface_and_retry_same_commit(self):
        fx = self.fx
        fx.set_policy({**UNIT_POLICY, "checks": {"unit": {"argv": [sys.executable, "-c", "raise SystemExit(75)"]}}})
        x_wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        fx.start_runner()
        fx.submit(x_wt, "req-x")
        state = fx.wait_states(["req-x"])["req-x"]
        self.assertEqual(state["state"], "infra_failed")
        self.assertEqual(state["attempts"], 3)
        self.assertEqual(fx.main(), fx.git("rev-parse", "main").stdout.strip())
        self.assertEqual(fx.inbox("wt-x")[0]["failure_type"], "infra")
        self.assertEqual(fx.mq("result", "req-x", cwd=x_wt).returncode, 20)
        fx.set_policy(UNIT_POLICY)  # operator fixed the infrastructure
        result, data = fx.mqj("retry", "req-x", "--wait", cwd=x_wt)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(fx.subjects(), ["x", "base"])


class PolicyAndCheckout(QueueCase):
    fixture_args = {"install_args": ("--checkout-wait-limit", "0")}  # these tests pin the terminal classification

    def land_one(self, files, rid="req-x", wait=True):
        wt, sha = self.fx.agent("x", files)
        self.fx.start_runner()
        self.fx.submit(wt, rid)
        return wt, sha, self.fx.wait_states([rid])[rid]

    def test_unknown_scope_fails_closed_until_policy_covers_it(self):
        wt, sha, state = self.land_one({"data/blob.bin": "x"})
        self.assertEqual((state["state"], state["result"]["failure_type"]), ("blocked", "unknown_scope"))
        self.assertEqual(state["result"]["conflicting_files"], ["data/blob.bin"])
        self.assertEqual(self.fx.mq("result", "req-x", cwd=wt).returncode, 11)
        self.assertEqual(self.fx.subjects(), ["base"])
        policy = json.loads(self.fx.policy_path.read_text())
        policy["rules"].append({"match": ["data/*"], "checks": []})
        self.fx.set_policy(policy)
        result, data = self.fx.mqj("retry", "req-x", "--wait", cwd=wt)  # same commit allowed after operator fix
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.fx.subjects(), ["x", "base"])

    def test_dirty_or_unknown_checkout_blocks_and_never_resets_it(self):
        fx = self.fx
        fx.write(fx.primary, "lib.py", "def value():\n    return 99\n")
        wt, sha, state = self.land_one({"x.py": "X = 1\n"})
        self.assertEqual(state["result"]["failure_type"], "checkout_dirty")
        self.assertEqual((fx.primary / "lib.py").read_text(), "def value():\n    return 99\n")
        fx.git("checkout", "--", "lib.py")
        fx.write(fx.primary, "stray.txt", "unknown\n")
        retry, data = fx.mqj("retry", "req-x", "--wait", cwd=wt)
        self.assertEqual(fx.req(data["request_id"])["result"]["failure_type"], "checkout_untracked")
        self.assertEqual((fx.primary / "stray.txt").read_text(), "unknown\n")
        (fx.primary / "stray.txt").unlink()
        retry, data = fx.mqj("retry", data["request_id"], "--wait", cwd=wt)
        self.assertEqual(data["state"], "landed", fx.debug())
        self.assertEqual(fx.subjects(), ["x", "base"])

    def test_runtime_paths_survive_unless_the_incoming_change_overlaps_them(self):
        fx = Fixture(install_args=("--runtime-allow", "runtime/", "--checkout-wait-limit", "0"))
        self.addCleanup(fx.close)
        fx.write(fx.primary, "runtime/data.db", "keep\n")
        fx.write(fx.primary, "ignored/cache.bin", "keep\n")
        policy = json.loads(fx.policy_path.read_text())
        policy["rules"].append({"match": ["runtime/*", "ignored/*"], "checks": []})
        fx.set_policy(policy)
        fx.start_runner()
        ok_wt, _ = fx.agent("ok", {"ok.py": "OK = 1\n"})
        clash_wt, _ = fx.agent("clash", {"runtime/data.db": "overwrite\n"})
        ignored_wt, _ = fx.agent("ign", {"ignored/cache.bin": "overwrite\n"}, force=("ignored/cache.bin",))
        fx.submit(ok_wt, "req-ok")
        self.assertEqual(fx.wait_states(["req-ok"])["req-ok"]["state"], "landed")
        self.assertEqual((fx.primary / "runtime/data.db").read_text(), "keep\n")  # untracked runtime data stays
        self.assertEqual((fx.primary / "ignored/cache.bin").read_text(), "keep\n")
        fx.submit(clash_wt, "req-clash")
        fx.submit(ignored_wt, "req-ign")
        states = fx.wait_states(["req-clash", "req-ign"])
        self.assertEqual([states[i]["result"]["failure_type"] for i in ("req-clash", "req-ign")],
                         ["checkout_overlap", "checkout_overlap"])
        self.assertEqual(states["req-ign"]["result"]["conflicting_files"], ["ignored/"])
        self.assertEqual((fx.primary / "runtime/data.db").read_text(), "keep\n")
        self.assertEqual((fx.primary / "ignored/cache.bin").read_text(), "keep\n")
        self.assertEqual(fx.subjects(), ["ok", "base"])

    def test_doctor_reports_and_recovers_stale_state(self):
        fx = self.fx
        stable = fx.stable_env()  # the tool runs from a package copy that is neither temporary nor in a worktree
        failing = lambda out: [c["check"] for c in json.loads(out)["checks"] if not c["ok"]]
        healthy = fx.mq("doctor", "--json", env=stable)
        self.assertEqual(failing(healthy.stdout), [], healthy.stdout)
        # The same installation run from a path under the system temp dir is flagged as unstable.
        unstable = fx.mq("doctor", "--json", env=dict(fx.env, MERGE_QUEUE_TEMP_DIRS=str(ROOT)))
        self.assertEqual(failing(unstable.stdout), ["install_pointers"], unstable.stdout)
        stale = fx.state / "work" / "stale-1"
        stale.parent.mkdir(parents=True, exist_ok=True)
        fx.git("worktree", "add", "-q", "--detach", str(stale), "main")
        (fx.state / "auth").mkdir(exist_ok=True)
        (fx.state / "auth" / "old.json").write_text(json.dumps({"created": 1}))
        fx.git("update-ref", "refs/merge-queue/candidate/ghost", "main")
        self.assertEqual(failing(fx.mq("doctor", "--json", env=stable).stdout), ["stale_workspaces"])
        fixed = fx.mq("doctor", "--recover", "--json", env=stable)
        self.assertEqual(failing(fixed.stdout), [], fixed.stdout)
        self.assertFalse(stale.exists())
        self.assertEqual(fx.refs(), [])
        self.assertFalse((fx.state / "auth" / "old.json").exists())
        self.assertEqual(len(fx.worktrees()), 1)
        # A missing registry entry (e.g. a wrong MERGE_QUEUE_STATE_HOME) is reported, not silently accepted.
        (fx.state_home / "repos" / f"{fx.repo_id()}.json").unlink()
        self.assertEqual(failing(fx.mq("doctor", "--json", env=stable).stdout), ["registry"])


class SharedConfigAndLogging(QueueCase):
    def test_h_all_worktrees_share_one_config_and_queue(self):
        fx = self.fx
        a_wt, _ = fx.agent("a", {"a.py": "A = 1\n"})
        b_wt, _ = fx.agent("b", {"b.py": "B = 1\n"})
        (a_wt / "sub").mkdir()
        configs = [json.loads(fx.mq("config", cwd=where, check=True).stdout) for where in (fx.primary, a_wt, b_wt, a_wt / "sub")]
        self.assertTrue(all(c == configs[0] for c in configs))
        self.assertEqual(configs[0]["state_dir"], str(fx.state))
        self.assertEqual(configs[0]["tq_data_dir"], str(fx.state / "tq"))
        self.assertEqual(fx.git("config", "--get", "mergequeue.statedir").stdout.strip(), str(fx.state))
        self.assertEqual(fx.git("config", "--get", "mergequeue.statedir", cwd=b_wt).stdout.strip(), str(fx.state))
        fx.submit(a_wt, "req-a")
        fx.submit(b_wt, "req-b")
        self.assertEqual({fx.req("req-a")["queue"], fx.req("req-b")["queue"]}, {configs[0]["queue"]})
        fx.start_runner()
        fx.wait_states(["req-a", "req-b"])
        queued = {e["queue_name"] for e in fx.tq_events() if e["event"] == "task_queued"}
        self.assertEqual(queued, {configs[0]["queue"]})
        self.assertTrue(configs[0]["queue"].endswith("-main"))

    def test_i_optional_logging_can_be_disabled_viewed_and_cleared(self):
        fx = self.fx
        env = dict(fx.env, MQ_SECRET="topsecret-value")
        fx.env = env
        a_wt, _ = fx.agent("a", {"a.py": "A = 1\n"})
        b_wt, _ = fx.agent("b", {"shared.py": "TITLE = 'one'\n"})
        c_wt, _ = fx.agent("c", {"shared.py": "TITLE = 'two'\n"})
        events = fx.state / "diagnostics" / "events.jsonl"
        self.assertEqual(fx.mq("logs", "--level", "off").returncode, 0)
        fx.start_runner()
        fx.submit(a_wt, "req-a")
        fx.wait_states(["req-a"])
        self.assertFalse(events.exists())  # off writes no diagnostics
        fx.mq("logs", "--level", "debug", check=True)
        fx.submit(b_wt, "req-b")
        fx.submit(c_wt, "req-c")  # conflicts, leaving a pending inbox entry
        fx.wait_states(["req-b", "req-c"])
        text = events.read_text()
        self.assertIn('"integration_started"', text)
        self.assertIn('"check"', text)  # debug-level detail
        self.assertNotIn("topsecret-value", text)
        shown = fx.mq("logs", "-n", "3", check=True).stdout
        self.assertIn("level=debug", shown)
        inbox_before = fx.inbox("wt-c")
        self.assertEqual(len(inbox_before), 1)
        requests_before = {p.name: p.read_text() for p in (fx.state / "requests").glob("*.json")}
        self.assertEqual(fx.mq("logs-clear", check=True).returncode, 0)
        self.assertFalse(events.exists())
        self.assertFalse((fx.state / "tq" / "agent-task-queue-logs.json").exists())
        self.assertTrue((fx.state / "tq" / "queue.db").exists())
        self.assertEqual(fx.inbox("wt-c"), inbox_before)
        self.assertTrue(Path(inbox_before[0]["relevant_log_paths"][0]).exists())  # failure evidence is not a diagnostic
        self.assertEqual({p.name: p.read_text() for p in (fx.state / "requests").glob("*.json")}, requests_before)
        self.assertEqual(fx.mq("status", check=True).returncode, 0)
        # The queue keeps working after the logs are gone.
        d_wt, _ = fx.agent("d", {"d.py": "D = 1\n"})
        fx.submit(d_wt, "req-d")
        self.assertEqual(fx.wait_states(["req-d"])["req-d"]["state"], "landed")


class LogEvidenceRetention(QueueCase):
    def test_logs_clear_and_gc_keep_failure_tq_logs_until_ack_then_prune(self):
        fx = self.fx
        mq = load_module()
        fx.set_policy({**UNIT_POLICY, "checks": {"unit": {"argv": [sys.executable, "-c", "raise SystemExit(75)"]}}})
        x_wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        fx.start_runner()
        fx.submit(x_wt, "req-x")
        state = fx.wait_states(["req-x"])["req-x"]
        self.assertEqual(state["state"], "infra_failed")
        evidence = [Path(p) for p in state["result"]["log_paths"]]
        self.assertTrue(evidence and all(p.exists() for p in evidence))
        self.assertEqual(fx.inbox("wt-x")[0]["relevant_log_paths"], [str(p) for p in evidence])
        runner_dir = fx.state / "runner"
        stale_old = runner_dir / "tq-old-history-1.log"
        stale_new = runner_dir / "tq-new-history-1.log"
        for path in (stale_old, stale_new):
            path.write_text("optional history\n")
        os.utime(stale_old, (time.time() - 3 * 86400,) * 2)
        ctx = mq.load_ctx(str(fx.primary))
        mq.gc(ctx)  # prunes only old, unprotected history
        self.assertFalse(stale_old.exists())
        self.assertTrue(stale_new.exists())
        for path in evidence:
            os.utime(path, (time.time() - 3 * 86400,) * 2)
        mq.gc(ctx)
        self.assertEqual(fx.mq("logs-clear", check=True).returncode, 0)
        self.assertFalse(stale_new.exists())
        self.assertTrue(all(p.exists() for p in evidence))  # unacknowledged failure evidence survives both
        self.assertEqual(len(fx.inbox("wt-x")), 1)
        fx.mq("ack", "req-x", check=True)
        fx.mq("logs-clear", check=True)
        self.assertFalse(any(p.exists() for p in evidence))  # after ack it is ordinary optional history


class GitEnforcement(QueueCase):
    def test_direct_local_updates_are_allowed_without_manual_environment(self):
        fx = self.fx
        wt, sha = fx.agent("x", {"x.py": "X = 1\n"})
        fx.git("merge", "--ff-only", "agent/x")
        self.assertEqual(fx.main(), sha)
        fx.git("commit", "--allow-empty", "-q", "-m", "direct")
        self.assertEqual(fx.git("status", "--porcelain").stdout, "")
        fx.write(wt, "y.py", "Y = 1\n")
        fx.git("add", "y.py", cwd=wt)
        self.assertEqual(fx.git("commit", "-q", "-m", "more", cwd=wt, check=False).returncode, 0)
        self.assertEqual(fx.git("branch", "other", sha).returncode, 0)

    def test_direct_commits_fast_forward_and_merge_commits(self):
        fx = self.fx
        manual = fx.env
        fx.write(fx.primary, "manual.py", "VALUE = 1\n")
        fx.git("add", "manual.py")
        fx.git("commit", "-q", "-m", "manual commit", env=manual)
        _, first = fx.agent("first", {"first.py": "FIRST = 1\n"})
        fx.git("merge", "--ff-only", "agent/first", env=manual)
        self.assertEqual(fx.main(), first)
        _, second = fx.agent("second", {"second.py": "SECOND = 1\n"})
        fx.git("commit", "--allow-empty", "-q", "-m", "diverge", env=manual)
        fx.git("merge", "--no-ff", "-m", "manual merge", "agent/second", env=manual)
        parents = fx.git("rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
        self.assertEqual(len(parents), 3)
        self.assertEqual(parents[-1], second)
        self.assertEqual(fx.git("status", "--porcelain").stdout, "")
        # Deprecated manual variables are irrelevant to ordinary local Git.
        for value in (None, "0", "true"):
            env = fx.env if value is None else dict(fx.env, MERGE_QUEUE_MANUAL=value)
            self.assertEqual(fx.git("commit", "--allow-empty", "-q", "-m", "direct",
                                       env=env, check=False).returncode, 0)

    def test_manual_flag_does_not_approve_push_and_chained_hooks_still_apply(self):
        fx = self.fx
        manual = dict(fx.env, MERGE_QUEUE_MANUAL="1")
        remote = fx.root / "remote.git"
        fx.git("init", "-q", "--bare", str(remote), cwd=fx.root)
        denied = fx.git("push", "-q", str(remote), "main", env=manual, check=False)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("MERGE_QUEUE_PUSH_APPROVED", denied.stderr)
        approved = dict(fx.env, MERGE_QUEUE_PUSH_APPROVED="1")
        fx.git("push", "-q", str(remote), "main", env=approved)
        self.assertEqual(fx.git("rev-parse", "main", cwd=remote).stdout.strip(), fx.main())
        hooks = fx.primary / ".git" / "hooks"
        for name in ("pre-push", "reference-transaction"):
            chained = hooks / (name + ".merge-queue-chained")
            chained.write_text("#!/bin/sh\ncat >/dev/null\necho chained-refusal >&2\nexit 1\n")
            chained.chmod(0o755)
        # Reinstall to detect and chain the reference hook too.
        fx.mq("install", "--state-dir", str(fx.state), "--tq", TQ, "--policy", str(fx.policy_path),
              "--checkout", str(fx.primary), "--allow-unstable-paths", check=True)
        for args in (("commit", "--allow-empty", "-q", "-m", "manual"),
                     ("push", "-q", str(remote), "main")):
            result = fx.git(*args, env=approved, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("chained-refusal", result.stderr)

    def test_manual_override_cannot_relax_queue_publication_and_is_not_inherited(self):
        fx = self.fx
        env = dict(fx.env, MERGE_QUEUE_MANUAL="1", MERGE_QUEUE_PUBLICATION="missing-receipt")
        result = fx.git("commit", "--allow-empty", "-q", "-m", "invalid publication", env=env, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no live authorisation", result.stderr)
        from unittest.mock import patch
        mq = load_module()
        with patch.dict(os.environ, {"MERGE_QUEUE_MANUAL": "1"}):
            self.assertNotIn("MERGE_QUEUE_MANUAL", mq.base_env())
            self.assertNotIn("MERGE_QUEUE_MANUAL", mq.clean_env())

    def test_direct_git_can_recover_with_missing_queue_configuration(self):
        fx = self.fx
        fx.git("config", "--local", "--remove-section", "mergequeue")
        fx.git("commit", "--allow-empty", "-q", "-m", "recover")
        mq = load_module()
        lock = mq.FileLock(fx.state / "locks" / "integration.lock")
        with lock:
            self.assertNotEqual(fx.git("commit", "--allow-empty", "-q", "-m", "busy",
                                       check=False).returncode, 0)

    def test_existing_hooks_are_chained_and_restored_on_uninstall(self):
        def pre_install(fx):
            hooks = fx.primary / ".git" / "hooks"
            (hooks / "reference-transaction").write_text(f"#!/bin/sh\ncat >/dev/null\necho \"$1\" >> '{fx.root}/chain.log'\n")
            (hooks / "pre-push").write_text("#!/bin/sh\ncat >/dev/null\nexit 0\n")
            for name in ("reference-transaction", "pre-push"):
                (hooks / name).chmod(0o755)
            fx.original_hook = (hooks / "reference-transaction").read_text()
        fx = Fixture(pre_install=pre_install)
        self.addCleanup(fx.close)
        chain = fx.root / "chain.log"
        fx.git("branch", "feature")
        self.assertIn("prepared", chain.read_text())  # chained hook still sees transactions
        chain.write_text("")
        fx.git("commit", "--allow-empty", "-q", "-m", "direct", check=False)
        self.assertIn("prepared", chain.read_text())
        self.assertEqual(fx.git("commit", "--allow-empty", "-q", "-m", "direct", check=False).returncode, 0)
        uninstall = fx.mq("uninstall", check=True)
        hook = fx.primary / ".git" / "hooks" / "reference-transaction"
        self.assertEqual(hook.read_text(), fx.original_hook)
        self.assertFalse((fx.primary / ".git" / "hooks" / "reference-transaction.merge-queue-chained").exists())
        self.assertEqual(fx.git("commit", "--allow-empty", "-q", "-m", "direct again").returncode, 0)
        self.assertEqual(fx.mq("config").returncode, 3)

    def test_pushing_the_target_to_a_remote_needs_explicit_approval(self):
        fx = self.fx
        remote = fx.root / "remote.git"
        fx.git("init", "-q", "--bare", str(remote), cwd=fx.root)
        fx.git("remote", "add", "origin", str(remote))
        wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        self.assertNotEqual(fx.git("push", "-q", "origin", "main", check=False).returncode, 0)
        self.assertEqual(fx.git("push", "-q", "origin", "agent/x", cwd=wt, check=False).returncode, 0)
        approved = dict(fx.env, MERGE_QUEUE_PUSH_APPROVED="1")
        self.assertEqual(fx.git("push", "-q", "origin", "main", env=approved, check=False).returncode, 0)


class SafetyRegressions(QueueCase):
    # Wait limit 0: a not-ready checkout fails terminally at once (the wait itself is covered by CheckoutWaitAndBudget).
    fixture_args = {"install_args": ("--checkout-wait-limit", "0")}

    def test_rejected_direct_fast_forward_dirt_is_preserved_and_the_queue_fails_closed(self):
        fx = self.fx
        x_wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        y_wt, _ = fx.agent("y", {"y.py": "Y = 1\n"})
        before = fx.main()
        with load_module().FileLock(fx.state / "locks" / "integration.lock"):
            self.assertNotEqual(fx.git("merge", "--ff-only", "agent/x", check=False).returncode, 0)
        fx.write(fx.primary, "concurrent.txt", "someone else is editing here\n")
        status = fx.git("status", "--porcelain").stdout
        self.assertIn("A  x.py", status)
        fx.start_runner()
        fx.submit(y_wt, "req-y")
        state = fx.wait_states(["req-y"])["req-y"]
        self.assertEqual((state["state"], state["result"]["failure_type"], state["result"]["failure_class"]),
                         ("blocked", "checkout_dirty", "operator"))
        self.assertIn("x.py", state["result"]["conflicting_files"])
        self.assertEqual(fx.mq("result", "req-y", cwd=y_wt).returncode, 11)
        self.assertEqual(fx.inbox("wt-y")[0]["failure_type"], "checkout_dirty")  # durable operator notice
        # The queue touched nothing in the primary: same ref, same index/worktree, same concurrent file.
        self.assertEqual(fx.main(), before)
        self.assertEqual(fx.git("status", "--porcelain").stdout, status)
        self.assertEqual((fx.primary / "concurrent.txt").read_text(), "someone else is editing here\n")
        self.assertTrue((fx.primary / "x.py").exists())

    def test_forged_or_reused_pids_are_never_killed_but_verified_orphans_are(self):
        fx = self.fx
        mq = load_module()
        bystander = subprocess.Popen(["sleep", "300"])
        orphan = subprocess.Popen(["sleep", "301"], start_new_session=True)
        self.addCleanup(lambda: [(p.kill(), p.wait()) for p in (bystander, orphan) if p.poll() is None])
        attempts = fx.state / "attempts"
        attempts.mkdir(parents=True)
        wrong_start = {"pid": bystander.pid, "start": "Mon Jan  1 00:00:00 2001", "args": "sleep 300"}
        (attempts / "forged-start.json").write_text(json.dumps({"group": wrong_start}))
        (attempts / "no-identity.json").write_text(json.dumps({"group": {"pid": bystander.pid}, "pgid": bystander.pid}))
        (attempts / "orphan.json").write_text(json.dumps({"group": mq.proc_identity(orphan.pid)}))
        x_wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        fx.start_runner()
        fx.submit(x_wt, "req-x")
        fx.wait_states(["req-x"])
        fx.settled()
        self.assertEqual(fx.req("req-x")["state"], "landed")
        self.assertIsNone(bystander.poll())  # unrelated process survived every forged record
        self.assertIsNotNone(orphan.wait(timeout=10))  # the verified orphan was reaped
        self.assertEqual(list(attempts.glob("*.json")), [])

    def test_live_state_never_regresses(self):
        fx = self.fx
        mq = load_module()
        x_wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        fx.submit(x_wt, "req-x")
        ctx = mq.load_ctx(str(fx.primary))
        mq.mutate(ctx, "req-x", lambda r: r.update(state="running"))
        with self.assertRaises(mq.MqError):
            mq.mutate(ctx, "req-x", lambda r: r.update(state="dispatched"))  # a late dispatch record cannot win
        self.assertEqual(fx.req("req-x")["state"], "running")
        mq.mutate(ctx, "req-x", lambda r: r.update(state="landed"))
        for state in ("running", "dispatched", "queued"):
            with self.assertRaises(mq.MqError):
                mq.mutate(ctx, "req-x", lambda r, s=state: r.update(state=s))
        self.assertEqual(fx.req("req-x")["state"], "landed")

    def test_runner_dying_between_spawn_and_bookkeeping_causes_no_duplicate_dispatch(self):
        fx = self.fx
        x_wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        first = fx.start_runner(MERGE_QUEUE_FAULT="after_spawn:exit:1")
        fx.submit(x_wt, "req-x")
        self.assertEqual(first.wait(timeout=30), 137)
        fx.start_runner()
        state = fx.wait_states(["req-x"])["req-x"]
        fx.settled()
        self.assertEqual(state["state"], "landed", fx.debug())
        self.assertEqual(state["dispatches"], 1)
        self.assertEqual(fx.subjects(), ["x", "base"])
        self.assertEqual(sum(e["event"] == "task_queued" for e in fx.tq_events()), 1)

    def test_seq_survives_loss_of_the_counter_file(self):
        fx = self.fx
        for name, rid in (("a", "req-a"), ("b", "req-b")):
            wt, _ = fx.agent(name, {f"{name}.py": "V = 1\n"})
            fx.submit(wt, rid)
        (fx.state / "seq").unlink()
        c_wt, _ = fx.agent("c", {"c.py": "V = 1\n"})
        fx.submit(c_wt, "req-c")
        self.assertEqual([fx.req(i)["seq"] for i in ("req-a", "req-b", "req-c")], [1, 2, 3])

    def test_owner_resolution_is_shared_by_submit_inbox_and_the_session_start_adapter(self):
        fx = self.fx
        a_wt, _ = fx.agent("a", {"shared.py": "TITLE = 'one'\n"})
        b_wt, _ = fx.agent("b", {"shared.py": "TITLE = 'two'\n"})
        thread_env = dict(fx.env, CODEX_THREAD_ID="thread-123")
        fx.start_runner()
        fx.submit(a_wt, "req-a")
        fx.mq("submit", "--request-id", "req-b", cwd=b_wt, env=thread_env, check=True)
        self.assertEqual(fx.req("req-b")["owner_id"], "thread-123")
        fx.wait_states(["req-a", "req-b"])
        self.assertEqual(fx.req("req-b")["state"], "conflict")
        self.assertEqual(fx.mq("inbox", "--quiet", cwd=b_wt).returncode, 0)  # the worktree name is not the owner
        self.assertEqual(fx.mq("inbox", "--quiet", cwd=b_wt, env=thread_env).returncode, 10)
        self.assertEqual(fx.mq("inbox", "--quiet", "--owner", "thread-123", cwd=b_wt).returncode, 10)
        payload = json.dumps({"cwd": str(b_wt), "session_id": "thread-123", "hook_event_name": "SessionStart",
                              "source": "startup"})
        notice = fx.mq("hook", "session-start", cwd=fx.root, input=payload)
        self.assertEqual(notice.returncode, 0, notice.stderr)
        context = json.loads(notice.stdout)["hookSpecificOutput"]
        self.assertEqual(context["hookEventName"], "SessionStart")
        self.assertIn("req-b", context["additionalContext"])
        self.assertIn("shared.py", fx.inbox("thread-123")[0]["conflicting_files"])
        quiet = [
            fx.mq("hook", "session-start", cwd=fx.root,
                  input=json.dumps({"cwd": str(a_wt), "session_id": "someone-else"})),  # nothing pending
            fx.mq("hook", "session-start", cwd=fx.root, input=json.dumps({"cwd": str(fx.root), "session_id": "x"})),
            fx.mq("hook", "session-start", cwd=fx.root, input="not json"),
        ]
        for result in quiet:
            self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)

    def test_install_refuses_unstable_pointers_broad_runtime_paths_and_retargeting_while_pending(self):
        fx = self.fx
        base = ("install", "--tq", TQ, "--policy", str(fx.policy_path), "--checkout", str(fx.primary),
                "--state-dir", str(fx.state))
        # A package inside a linked worktree (of any repository) is refused: it disappears with that worktree.
        linked_wt, _ = fx.agent("pkg", {"keep.txt": "x\n"})
        linked = fx.mq(*base, env=fx.stable_env(linked_wt / "vendor"))
        self.assertEqual(linked.returncode, 2, linked.stderr)
        self.assertIn("linked Git worktree", linked.stderr)
        # So is one under the system temporary directory.
        temp = fx.mq(*base, env=dict(fx.stable_env(fx.root / "tmpcopy"), MERGE_QUEUE_TEMP_DIRS=str(fx.root)))
        self.assertEqual(temp.returncode, 2, temp.stderr)
        self.assertIn("temporary directory", temp.stderr)
        # A stable location (neither) installs fine without any override.
        ok = fx.mq(*base, env=fx.stable_env(fx.root / "stable-copy"))
        self.assertEqual(ok.returncode, 0, ok.stderr)
        for bad in ("*", ".", "../x", "/abs", "runtime/*"):
            self.assertEqual(fx.mq(*base, "--allow-unstable-paths", "--runtime-allow", bad).returncode, 2, bad)
        x_wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        fx.submit(x_wt, "req-x")  # pending, no runner
        elsewhere = fx.mq(*base[:-1], str(fx.root / "other-state"), "--allow-unstable-paths")
        self.assertEqual(elsewhere.returncode, 4, elsewhere.stderr)
        same = fx.mq(*base, "--allow-unstable-paths")
        self.assertEqual(same.returncode, 0, same.stderr)

    def test_direct_transaction_cannot_borrow_an_authorisation_during_publication(self):
        fx = self.fx
        wt, sha = fx.agent("x", {"x.py": "X = 1\n"})
        before = fx.main()
        (fx.state / "auth").mkdir(parents=True)
        (fx.state / "auth" / "forged.json").write_text(json.dumps(
            {"ref": "refs/heads/main", "old": before, "new": sha, "created": time.time()}))
        with load_module().FileLock(fx.state / "locks" / "integration.lock"):
            denied = fx.git("update-ref", "refs/heads/main", sha, check=False)
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual(fx.main(), before)

    def test_unrelated_worktree_inventory_is_never_pruned(self):
        fx = self.fx
        ghost = fx.root / "ghost-worktree"
        fx.git("worktree", "add", "-q", "--detach", str(ghost), "main")
        shutil.rmtree(ghost)  # a prunable registration owned by someone else
        x_wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        fx.start_runner()
        fx.submit(x_wt, "req-x")
        fx.wait_states(["req-x"])
        fx.settled()
        fx.mq("doctor", "--recover")
        self.assertIn(str(ghost), fx.git("worktree", "list", "--porcelain").stdout)
        self.assertEqual(fx.req("req-x")["state"], "landed")

    def switch_primary_during_publication(self, stage):
        """Another actor checks out an unrelated branch in the primary while a publication is in flight."""
        fx = self.fx
        gate = fx.root / f"gate-{stage}"
        x_wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        fx.git("branch", "unrelated")
        main_before, other_before = fx.main(), fx.git("rev-parse", "unrelated").stdout.strip()
        fx.start_runner(MERGE_QUEUE_FAULT=f"{stage}:wait={gate}:1")
        fx.submit(x_wt, "req-x")
        fx.wait_for(lambda: Path(str(gate) + ".reached").exists(), 30, f"the {stage} barrier")
        fx.git("checkout", "-q", "unrelated")
        gate.write_text("go")
        state = fx.wait_states(["req-x"])["req-x"]
        fx.settled()
        self.assertEqual((state["state"], state["result"]["failure_type"]), ("blocked", "checkout_branch"), fx.debug())
        self.assertEqual(fx.main(), main_before)
        self.assertEqual(fx.git("rev-parse", "unrelated").stdout.strip(), other_before)  # helper never moved it
        self.assertEqual(fx.git("status", "--porcelain").stdout, "")  # and left no staged dirt behind
        self.assertFalse((fx.primary / "x.py").exists())
        return state

    def test_branch_switch_after_the_receipt_is_caught_before_git_runs(self):
        self.switch_primary_during_publication("after_receipt")

    def test_branch_switch_after_authorisation_is_refused_by_the_publication_bound_hook(self):
        self.switch_primary_during_publication("after_auth")
        logs = list((self.fx.state / "evidence" / "req-x").glob("*-publish.log"))
        self.assertTrue(any("not refs/heads/main" in p.read_text() for p in logs), [p.read_text() for p in logs])

    def test_publication_never_autostashes_whatever_the_repository_configures(self):
        fx = self.fx
        real_git = shutil.which("git")
        (fx.root / "bin").mkdir()
        shim = fx.root / "bin" / "git"
        shim.write_text(f"#!/bin/sh\necho \"$*\" >> '{fx.root}/git-calls.log'\nexec {real_git} \"$@\"\n")
        shim.chmod(0o755)
        fx.env["PATH"] = f"{fx.root / 'bin'}{os.pathsep}{fx.env['PATH']}"
        fx.git("config", "merge.autoStash", "true")
        x_wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        fx.start_runner()
        fx.submit(x_wt, "req-x")
        self.assertEqual(fx.wait_states(["req-x"])["req-x"]["state"], "landed")
        merges = [l for l in (fx.root / "git-calls.log").read_text().splitlines() if l.startswith("merge ")]
        self.assertEqual(len(merges), 1, merges)
        self.assertTrue(merges[0].startswith("merge --ff-only --no-autostash "), merges)

    def test_land_matching_is_exact_per_request_and_per_repository(self):
        fx = self.fx
        mq = load_module()
        ctx = mq.load_ctx(str(fx.primary))

        def command(rid, repo=None):
            return shlex.join([*ctx.entry, "land", "--request-id", rid, "--repo-id", repo or ctx.repo_id])
        self.assertTrue(mq.command_matches(ctx, command("req"), "req"))
        self.assertFalse(mq.command_matches(ctx, command("req-extra"), "req"))  # prefix-related ids do not alias
        self.assertFalse(mq.command_matches(ctx, command("req", "0123456789ab"), "req"))  # another repository
        self.assertTrue(mq.args_match(ctx, command("any-id"), None))
        self.assertFalse(mq.args_match(ctx, command("any-id", "0123456789ab"), None))
        # Live decoys: neither an unrelated repository's landing nor a longer id may hold up req-x.
        sig = f"{ctx.entry_text} land --request-id"
        decoys = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", f"{sig} req-x --repo-id 0123456789ab"]),
                  subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)",
                                    f"{sig} req-x-extra --repo-id {ctx.repo_id}"])]
        self.addCleanup(lambda: [(p.kill(), p.wait()) for p in decoys])
        self.assertEqual(mq.land_processes(ctx, "req-x"), [])
        self.assertEqual(len(mq.land_processes(ctx, "req-x-extra")), 1)
        x_wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        fx.start_runner()
        fx.submit(x_wt, "req-x")
        self.assertEqual(fx.wait_states(["req-x"], timeout=60)["req-x"]["state"], "landed")

    def test_unverifiable_stale_tq_child_blocks_dispatch_until_cleared(self):
        real_tq = TQ
        canned = None

        def shim(fx):
            nonlocal canned
            canned = fx.root / "fake-list.json"
            path = fx.root / "tq-shim"
            path.write_text(f"#!{sys.executable}\nimport os, sys\ncanned = {str(canned)!r}\nargs = sys.argv[1:]\n"
                            "if os.path.exists(canned) and 'list' in args and '--json' in args:\n"
                            "    sys.stdout.write(open(canned).read())\n    sys.exit(0)\n"
                            f"os.execv({real_tq!r}, [{real_tq!r}, *args])\n")
            path.chmod(0o755)
            return str(path)
        fx = Fixture(tq=shim)
        self.addCleanup(fx.close)
        mq = load_module()
        ctx = mq.load_ctx(str(fx.primary))
        gone = subprocess.Popen(["true"])
        gone.wait()
        bystander = subprocess.Popen(["sleep", "300"])
        ours = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)",
                                 f"{ctx.entry_text} land --request-id ghost --repo-id {ctx.repo_id}"])
        self.addCleanup(lambda: [(p.kill(), p.wait()) for p in (bystander, ours)])

        def stale_row(child):
            row = {"id": 1, "queue_name": ctx.queue, "status": "running", "pid": gone.pid, "child_pid": child,
                   "command": "sleep 300", "created_at": "2026-10-23T10:00:00", "updated_at": datetime.now().isoformat()}
            canned.write_text(json.dumps({"tasks": [row], "summary": {"total": 1, "running": 1, "waiting": 0}}))
        stale_row(bystander.pid)  # a dead tq whose recorded child pid now belongs to an unrelated process
        x_wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        fx.start_runner()
        fx.submit(x_wt, "req-x")
        notice = fx.state / "notices" / "stale-tq.json"
        fx.wait_for(notice.exists, 30, "the operator notice")
        time.sleep(1.5)
        req = fx.req("req-x")
        self.assertEqual((req["state"], req["dispatches"]), ("queued", 0))  # tq was never started
        self.assertIsNone(bystander.poll())
        self.assertIn(str(bystander.pid), " ".join(json.loads(notice.read_text())["problems"]))
        self.assertIn("DISPATCH BLOCKED", fx.mq("status", check=True).stdout)
        doctor = json.loads(fx.mq("doctor", "--json").stdout)
        self.assertFalse(next(c for c in doctor["checks"] if c["check"] == "tq_stale_rows")["ok"])
        # A sleeper whose argv merely contains this repository's landing signature is not recorded ownership.
        stale_row(ours.pid)
        time.sleep(1.5)
        self.assertEqual(fx.req("req-x")["dispatches"], 0)
        self.assertIn(str(ours.pid), " ".join(json.loads(notice.read_text())["problems"]))
        self.assertIsNone(ours.poll())
        # The operator verifies and stops it, then clears the row; dispatch resumes on its own.
        ours.kill()
        ours.wait()
        canned.unlink()
        state = fx.wait_states(["req-x"], timeout=60)["req-x"]
        self.assertEqual(state["state"], "landed", fx.debug())
        self.assertFalse(notice.exists())
        self.assertIsNone(bystander.poll())

    def test_the_suite_runs_inside_a_queue_validation_without_recursing(self):
        """A candidate that changes the queue is validated by running (a queue-driven part of) this very
        suite from inside a landing: the inner fixture has its own repo/state/tq data, so nothing recurses."""
        fx = self.fx
        inner = [sys.executable, "-m", "unittest",
                 "tests.test_core.FifoAndOutcomes.test_a_two_clean_merges_land_in_fifo_order"]
        fx.set_policy({**UNIT_POLICY, "rules": [{"match": ["src/*", "tests/*"], "checks": ["suite"]}],
                       "checks": {"suite": {"argv": inner, "timeout": 280,
                                            "env": {"PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"}}}})
        files = {"src/merge_queue/__init__.py": (CORE_FILE.parent / "__init__.py").read_text(),
                 "src/merge_queue/core.py": CORE_FILE.read_text(),
                 "tests/fixture.py": (Path(__file__).parent / "fixture.py").read_text(),
                 "tests/test_core.py": Path(__file__).read_text()}
        wt, _ = fx.agent("suite", files)
        fx.start_runner()
        fx.submit(wt, "req-suite")
        state = fx.wait_states(["req-suite"], timeout=280)["req-suite"]
        self.assertEqual(state["state"], "landed", fx.debug())
        self.assertEqual(state["validation"][-1]["exit"], 0)
        self.assertTrue((fx.primary / "src" / "merge_queue" / "core.py").exists())
        self.assertEqual(fx.subjects(), ["suite", "base"])

    def test_dependency_setup_follows_the_projects_own_plan_and_unknown_scopes_fail_before_setup(self):
        fx = self.fx
        plan, boot, log = fx.root / "plan.json", fx.root / "boot.py", fx.root / "boot.log"
        (fx.root / "plan.py").write_text("import sys\nfrom pathlib import Path\nprint(Path(sys.argv[1]).read_text())\n")
        boot.write_text("import sys\nfrom pathlib import Path\n"
                        f"with open({str(log)!r}, 'a') as h:\n    h.write(sys.argv[1] + '\\n')\n")
        unit = dict(UNIT_POLICY["checks"]["unit"], plan=[sys.executable, str(fx.root / "plan.py"), str(plan)],
                    bootstrap={"backend": [sys.executable, str(boot), "backend"],
                               "frontend": [sys.executable, str(boot), "frontend"]})
        fx.set_policy({**UNIT_POLICY, "checks": {"unit": unit}})
        fx.start_runner()

        def run(name, planned):
            plan.write_text(json.dumps(planned))
            wt, _ = fx.agent(name, {f"{name}.py": "V = 1\n"})
            fx.submit(wt, f"req-{name}")
            return fx.wait_states([f"req-{name}"])[f"req-{name}"]
        planned = {"checks": ["tooling"], "unknown": [], "tooling_scope": {"root": ["test_leaf.py"], "app": []}}
        tool = run("tool", planned)
        self.assertEqual(tool["state"], "landed")
        self.assertEqual(tool["plan"]["gate_owner"], "merge-queue")
        self.assertFalse(tool["plan"]["results_reused"])
        self.assertEqual(tool["plan"]["details"]["unit"], planned)
        self.assertTrue(all(row["seconds"] >= 0 for row in tool["validation"]))
        self.assertFalse(log.exists())  # tooling-only changes install nothing
        self.assertEqual(run("back", {"checks": ["backend", "tooling"], "unknown": []})["state"], "landed")
        self.assertEqual(log.read_text(), "backend\n")  # only the needed dependency set
        blocked = run("odd", {"checks": ["backend"], "unknown": ["weird.bin"]})
        self.assertEqual((blocked["state"], blocked["result"]["failure_type"]), ("blocked", "unknown_scope"))
        self.assertEqual(log.read_text(), "backend\n")  # unknown scopes fail before any setup


class CheckoutWaitAndBudget(QueueCase):
    def runner(self, **env):
        return self.fx.start_runner(MERGE_QUEUE_CHECKOUT_WAIT_DELAY="0.2", **env)

    def test_dirty_primary_waits_without_penalty_then_lands_after_cleanup(self):
        fx = self.fx
        fx.write(fx.primary, "lib.py", "def value():\n    return 99\n")
        wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        self.runner()
        fx.submit(wt, "req-x")
        notice = fx.state / "notices" / "checkout-wait.json"
        fx.wait_for(notice.exists, 60, "the checkout-wait notice")
        req = fx.req("req-x")
        self.assertIn("checkout_dirty", req["checkout_wait_reason"])
        self.assertTrue(req["checkout_wait_since"])
        status = json.loads(fx.mq("status", "--json").stdout)
        self.assertEqual(status["checkout_wait"]["requests"], ["req-x"])
        self.assertIn("WAITING FOR CHECKOUT", fx.mq("status").stdout)
        doctor = {c["check"]: c for c in json.loads(fx.mq("doctor", "--json").stdout)["checks"]}
        self.assertFalse(doctor["checkout_wait"]["ok"])
        # Waiting for longer than the attempt/dispatch caps (3 / 9) would have failed it as infra before.
        fx.wait_for(lambda: len([e for e in fx.state.glob("diagnostics/events.jsonl")
                                 for l in e.read_text().splitlines() if '"checkout_wait"' in l]) >= 11, 120,
                    "more waits than the dispatch cap")
        waiting = fx.req("req-x")
        self.assertEqual(waiting["result"], {})
        self.assertLessEqual(waiting["attempts"], 1)
        self.assertLessEqual(waiting["dispatches"], 1)
        self.assertEqual((fx.primary / "lib.py").read_text(), "def value():\n    return 99\n")  # never reset
        fx.git("checkout", "--", "lib.py")
        done = fx.wait_states(["req-x"])["req-x"]
        self.assertEqual(done["state"], "landed", fx.debug())
        self.assertNotIn("checkout_wait_since", done)
        self.assertEqual((done["attempts"], done["dispatches"]), (1, 1))  # the waits consumed neither
        fx.wait_for(lambda: not notice.exists(), 30, "the notice to clear")
        self.assertEqual(fx.subjects(), ["x", "base"])

    def test_checkout_wait_limit_fails_terminally_with_the_original_failure(self):
        fx = Fixture(install_args=("--checkout-wait-limit", "2"))
        self.addCleanup(fx.close)
        fx.write(fx.primary, "lib.py", "def value():\n    return 99\n")
        wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        fx.start_runner(MERGE_QUEUE_CHECKOUT_WAIT_DELAY="0.2")
        fx.submit(wt, "req-x")
        state = fx.wait_states(["req-x"])["req-x"]
        self.assertEqual((state["state"], state["result"]["failure_type"], state["result"]["failure_class"]),
                         ("blocked", "checkout_dirty", "operator"))
        self.assertIn("waited 2s", state["result"]["message"])
        self.assertEqual(fx.inbox("wt-x")[0]["failure_type"], "checkout_dirty")
        fx.wait_for(lambda: not (fx.state / "notices" / "checkout-wait.json").exists(), 30, "the notice to clear")

    def test_land_budget_stops_validation_cleanly_as_infra_not_as_the_owners_failure(self):
        fx = self.fx
        fx.set_policy({**UNIT_POLICY, "checks": {"unit": {"argv": [sys.executable, "-c", "import time; time.sleep(60)"],
                                                          "timeout": 1800}}})
        wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        fx.start_runner(MERGE_QUEUE_LAND_BUDGET="2")
        fx.submit(wt, "req-x")
        state = fx.wait_states(["req-x"])["req-x"]
        self.assertEqual((state["state"], state["result"]["failure_type"]), ("infra_failed", "infra"), fx.debug())
        self.assertIn("land time budget exhausted during unit check", state["result"]["message"])
        self.assertEqual(state["attempts"], 3)
        self.assertEqual(state["validation"][-1]["exit"], None)
        self.assertEqual(fx.mq("result", "req-x", cwd=wt).returncode, 20)
        self.assertEqual(fx.subjects(), ["base"])

    def test_exhausted_budget_before_a_step_requeues_as_infra(self):
        fx = self.fx
        wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        fx.start_runner(MERGE_QUEUE_LAND_BUDGET="0.01")
        fx.submit(wt, "req-x")
        state = fx.wait_states(["req-x"])["req-x"]
        self.assertEqual(state["state"], "infra_failed")
        self.assertIn("budget exhausted before unit check", state["result"]["message"])

    def test_unexpected_exception_is_requeued_with_its_cause_and_a_traceback(self):
        fx = self.fx
        fx.set_policy({**UNIT_POLICY, "checks": {"unit": {"argv": [sys.executable, "-c", "{nope}"]}}})  # KeyError
        wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        fx.start_runner()
        fx.submit(wt, "req-x")
        state = fx.wait_states(["req-x"])["req-x"]
        self.assertEqual(state["state"], "infra_failed")
        self.assertIn("unexpected KeyError('nope')", state["result"]["message"])
        self.assertEqual(state["attempts"], 3)
        crash, = list((fx.state / "evidence" / "req-x").glob("*-crash.txt"))[-1:]
        self.assertIn("KeyError", crash.read_text())
        self.assertIn(str(crash.parent), state["result"]["message"])


class SupersededRetryAndRunner(QueueCase):
    def failed_then_fixed(self):
        fx = self.fx
        wt, _ = fx.agent("bad", {"lib.py": "def value():\n    return 2\n"})
        fx.start_runner()
        fx.submit(wt, "req-bad")
        self.assertEqual(fx.wait_states(["req-bad"])["req-bad"]["state"], "validation_failed")
        fx.write(wt, "lib.py", "def value():\n    return 1\n")
        fx.write(wt, "fixed.py", "FIXED = True\n")
        fx.git("add", "-A", cwd=wt)
        fx.git("commit", "-q", "-m", "repair", cwd=wt)
        return wt

    def test_superseded_has_its_own_exit_code_and_names_its_replacement(self):
        fx = self.fx
        wt = self.failed_then_fixed()
        result, data = fx.mqj("retry", "req-bad", "--new-request-id", "req-fixed", "--wait", cwd=wt)
        self.assertEqual((result.returncode, data["state"]), (0, "landed"), result.stdout + result.stderr)
        old, old_data = fx.mqj("result", "req-bad", cwd=wt)
        self.assertEqual((old.returncode, old_data["state"], old_data["superseded_by"]), (12, "superseded", "req-fixed"))
        mq = load_module()
        self.assertEqual(mq.EXIT_SUPERSEDED, 12)
        self.assertEqual(mq.outcome_code({"state": "landed"}), 0)

    def test_retry_with_an_id_bound_to_another_commit_leaves_the_old_request_untouched(self):
        fx = self.fx
        a_wt, _ = fx.agent("a", {"a.py": "A = 1\n"})
        fx.start_runner()
        fx.submit(a_wt, "req-a")
        fx.wait_states(["req-a"])
        wt = self.failed_then_fixed()
        before = fx.req("req-bad")
        result = fx.mq("retry", "req-bad", "--new-request-id", "req-a", cwd=wt)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        after = fx.req("req-bad")
        self.assertEqual((after["state"], after.get("superseded_by")), ("validation_failed", None))
        self.assertEqual(after, before)
        self.assertEqual(fx.inbox("wt-bad")[0]["request_id"], "req-bad")  # still actionable
        self.assertTrue(fx.refs())  # its source ref was not dropped
        ok = fx.mq("retry", "req-bad", "--new-request-id", "req-fixed", "--wait", cwd=wt)  # a free id still works
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)

    def test_a_runner_started_without_exit_options_waits_for_the_lock_instead_of_exiting(self):
        fx = self.fx
        mq = load_module()
        lock = mq.FileLock(fx.state / "locks" / "runner.lock", blocking=False)
        self.assertTrue(lock.acquire())  # e.g. an auto-runner spawned by submit
        try:
            proc = fx.start_runner(MERGE_QUEUE_RUNNER_LOCK_POLL="0.2")
            time.sleep(2)
            self.assertIsNone(proc.poll(), "launchd mode must not exit (KeepAlive would respawn it in a loop)")
            drain = fx.mq("runner", "--drain")  # probe/one-shot modes keep their behaviour
            self.assertEqual((drain.returncode, drain.stdout.strip()), (0, "runner already active"))
        finally:
            lock.release()
        wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        fx.submit(wt, "req-x")
        self.assertEqual(fx.wait_states(["req-x"])["req-x"]["state"], "landed", fx.debug())
        fx.stop_runner(proc)
        self.assertIn("waiting for the lock", (fx.root / "runner-0.log").read_text())


class StalePublicationHelper(QueueCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def pre_install(fx):
            hook = fx.primary / ".git" / "hooks" / "reference-transaction"
            hook.write_text("#!/bin/sh\ncat >/dev/null\n"
                            f"if [ \"$1\" = prepared ] && [ -e '{fx.root}/slow' ]; then\n"
                            f"  rm -f '{fx.root}/slow'; touch '{fx.root}/slow.hit'; sleep 2\nfi\n")
            hook.chmod(0o755)
        cls.fixture_args = {"pre_install": pre_install}

    def test_killed_helper_cannot_overlap_its_own_publication_and_is_reconciled(self):
        fx = self.fx
        mq = load_module()
        gate = fx.root / "gate"
        x_wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        fx.start_runner(MERGE_QUEUE_FAULT=f"after_receipt:wait={gate}:1")
        fx.submit(x_wt, "req-x")
        fx.wait_for(lambda: Path(str(gate) + ".reached").exists(), 30, "the receipt to be written")
        helper = json.loads((fx.state / "attempts" / "req-x.json").read_text())["identity"]["pid"]
        (fx.root / "slow").write_text("1")
        gate.write_text("go")
        fx.wait_for((fx.root / "slow.hit").exists, 30, "git to be inside the reference transaction")
        os.kill(helper, signal.SIGKILL)  # the helper dies while Git is publishing
        ctx = mq.load_ctx(str(fx.primary))
        self.assertTrue(mq.publication_lock_held(ctx))  # Git's inherited lock keeps every other helper out
        state = fx.wait_states(["req-x"])["req-x"]
        fx.settled()
        self.assertEqual(state["state"], "landed", fx.debug())
        self.assertIn("reconciled", state["result"]["note"])
        self.assertEqual(fx.subjects(), ["x", "base"])  # published exactly once
        self.assertEqual(fx.git("status", "--porcelain").stdout, "")


class RepoAgnostic(QueueCase):
    """State root, registry, stable-path rule, launchd label, policy default and `policy check`."""

    def test_state_root_follows_env_then_xdg_then_home(self):
        mq = load_module()
        with mock.patch.dict(os.environ, {"MERGE_QUEUE_STATE_HOME": str(self.fx.root / "explicit"),
                                          "XDG_STATE_HOME": "/ignored"}):
            self.assertEqual(mq.state_root(), (self.fx.root / "explicit").resolve())
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.fx.root / "xdg")}):
            os.environ.pop("MERGE_QUEUE_STATE_HOME", None)
            self.assertEqual(mq.state_root(), (self.fx.root / "xdg" / "merge-queue").resolve())
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "relative/dir"}):  # XDG: relative values are invalid
            os.environ.pop("MERGE_QUEUE_STATE_HOME", None)
            with mock.patch("pathlib.Path.home", return_value=self.fx.root):
                self.assertEqual(mq.state_root(), (self.fx.root / ".local/state/merge-queue").resolve())
        with mock.patch.dict(os.environ, {"MERGE_QUEUE_STATE_HOME": "relative"}):
            with self.assertRaises(mq.MqError):
                mq.state_root()

    def test_default_state_dir_is_per_repo_under_the_state_root_and_the_override_still_wins(self):
        fx = self.fx
        # The fixture installs with --state-dir; a second install without it lands in <root>/<repo-id>.
        dry = fx.mq("install", "--tq", TQ, "--policy", str(fx.policy_path), "--checkout", str(fx.primary),
                    "--allow-unstable-paths", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        plan = json.loads(dry.stdout)["config"]
        self.assertEqual(plan["stateDir"], str(fx.state_home.resolve() / fx.repo_id()))
        self.assertEqual(plan["registry"], str(fx.state_home.resolve() / "repos" / f"{fx.repo_id()}.json"))
        explicit = fx.mq("config", check=True)
        self.assertEqual(json.loads(explicit.stdout)["state_dir"], str(fx.state))  # --state-dir kept
        self.assertFalse((fx.state_home / fx.repo_id()).exists())  # dry-run and override create nothing there

    def test_install_registers_the_repository_and_uninstall_removes_only_its_own_entry(self):
        fx = self.fx
        other = Fixture(state_home=fx.state_home)  # a second repository sharing the same state root
        self.addCleanup(other.close)
        mine, theirs = (fx.state_home / "repos" / f"{f.repo_id()}.json" for f in (fx, other))
        self.assertNotEqual(mine, theirs)
        entry = json.loads(mine.read_text())
        common = Path(fx.git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()).resolve()
        self.assertEqual({k: entry[k] for k in ("repo_id", "common_dir", "checkout", "state_dir", "target")},
                         {"repo_id": fx.repo_id(), "common_dir": str(common), "checkout": str(fx.primary),
                          "state_dir": str(fx.state), "target": "refs/heads/main"})
        datetime.fromisoformat(entry["installed_at"])
        self.assertEqual(sorted(p.name for p in (fx.state_home / "repos").glob("*.json")),
                         sorted([mine.name, theirs.name]))
        self.assertFalse(list((fx.state_home / "repos").glob(".*.tmp")))  # written atomically
        report = fx.mq("uninstall", check=True)
        self.assertIn("registry", json.loads(report.stdout))
        self.assertFalse(mine.exists())
        self.assertTrue(theirs.exists())  # the other repository stays registered
        self.assertTrue(fx.state.exists())  # state is kept, as before
        self.assertEqual(fx.mq("status").returncode, 3)  # not installed any more
        # Reinstalling registers it again (the entry is rewritten, not duplicated).
        fx.mq("install", "--state-dir", str(fx.state), "--tq", TQ, "--policy", str(fx.policy_path),
              "--checkout", str(fx.primary), "--allow-unstable-paths", check=True)
        self.assertTrue(mine.exists())

    def test_pointers_use_the_installed_entry_never_a_checkout_path(self):
        fx = self.fx
        cfg = json.loads(fx.mq("config", check=True).stdout)
        self.assertEqual(shlex.split(cfg["entry"]), MQ_CMD)
        hooks = Path(fx.git("rev-parse", "--path-format=absolute", "--git-path", "hooks").stdout.strip())
        text = (hooks / "reference-transaction").read_text()
        self.assertIn(f"{shlex.join(MQ_CMD)} hook reference-transaction", text)
        for forbidden in (str(fx.primary), str(SRC), "scripts/"):
            self.assertNotIn(forbidden, text)

    def test_unstable_reason_covers_linked_worktrees_and_system_temp(self):
        mq = load_module()
        fx = self.fx
        wt, _ = fx.agent("linked", {"a.py": "A = 1\n"})
        with mock.patch.dict(os.environ, {"MERGE_QUEUE_TEMP_DIRS": ""}):
            self.assertIn("linked Git worktree", mq.unstable_reason(wt / "a.py"))
            self.assertIn("linked Git worktree", mq.unstable_reason(wt / "not" / "yet" / "there.py"))
            self.assertIsNone(mq.unstable_reason(fx.primary / "lib.py"))  # the primary checkout is stable
            self.assertIsNone(mq.unstable_reason(fx.root))  # not a repository at all
        with mock.patch.dict(os.environ, {"MERGE_QUEUE_TEMP_DIRS": str(fx.root)}):
            self.assertIn("temporary directory", mq.unstable_reason(fx.primary / "lib.py"))
        with mock.patch.dict(os.environ, {"MERGE_QUEUE_TEMP_DIRS": str(fx.root / "elsewhere")}):
            self.assertIsNone(mq.unstable_reason(fx.primary / "lib.py"))
        problems = mq.install_pointer_problems([sys.executable], wt / "a.py")
        self.assertTrue(any(p.startswith("merge_queue module:") for p in problems), problems)

    def test_launchd_label_prefix_defaults_to_local_and_is_configurable_without_loading_anything(self):
        fx = self.fx
        base = ("install", "--state-dir", str(fx.state), "--tq", TQ, "--policy", str(fx.policy_path),
                "--checkout", str(fx.primary), "--allow-unstable-paths")
        plist = fx.root / "agent.plist"
        fx.mq(*base, "--launchd-plist", str(plist), check=True)
        data = plistlib.loads(plist.read_bytes())
        self.assertEqual(data["Label"], f"local.merge-queue.{fx.repo_id()}")
        self.assertEqual(data["ProgramArguments"], [*MQ_CMD, "runner"])
        self.assertEqual(data["WorkingDirectory"], str(fx.primary))
        custom = fx.root / "custom.plist"
        fx.mq(*base, "--launchd-plist", str(custom), "--launchd-label-prefix", "org.example.mq", check=True)
        self.assertEqual(plistlib.loads(custom.read_bytes())["Label"], f"org.example.mq.{fx.repo_id()}")
        # --runtime-allow values must never leak into the label (they once shadowed the prefix variable).
        both = fx.root / "both.plist"
        fx.mq(*base, "--runtime-allow", "data/", "--runtime-allow", "cache/x.lock", "--launchd-plist", str(both),
              check=True)
        self.assertEqual(plistlib.loads(both.read_bytes())["Label"], f"local.merge-queue.{fx.repo_id()}")
        bad = fx.mq(*base, "--launchd-label-prefix", "bad prefix/x")
        self.assertEqual(bad.returncode, 2, bad.stderr)

    def test_policy_defaults_to_the_checkouts_dot_merge_queue_directory_and_must_exist(self):
        fx = self.fx
        base = ("install", "--state-dir", str(fx.state), "--tq", TQ, "--checkout", str(fx.primary),
                "--allow-unstable-paths")
        missing = fx.mq(*base)
        self.assertEqual(missing.returncode, 2, missing.stderr)
        self.assertIn(".merge-queue/policy.json", missing.stderr)
        fx.write(fx.primary, ".merge-queue/policy.json", json.dumps(UNIT_POLICY))
        fx.mq(*base, check=True)
        self.assertEqual(json.loads(fx.mq("config", check=True).stdout)["policy"],
                         str(fx.primary / ".merge-queue" / "policy.json"))

    def test_policy_check_validates_the_schema_and_that_every_tracked_path_is_routed(self):
        fx = self.fx
        self.assertEqual(fx.git("ls-files").stdout.split(), [".gitignore", "lib.py", "shared.py", "tests/test_lib.py",
                                                            "tests/test_sleep.py"])

        def check(*args, cwd=None):
            result = fx.mq("policy", "check", *args, cwd=cwd)
            return result.returncode, json.loads(result.stdout)
        # UNIT_POLICY routes *.py, tests/* and docs/* but not .gitignore: fails closed, listing the path.
        code, out = check("--policy", str(fx.policy_path))
        self.assertEqual((code, out["ok"], out["uncovered"]), (1, False, [".gitignore"]))
        self.assertEqual(out["files"], 5)
        covered = {**UNIT_POLICY, "rules": [*UNIT_POLICY["rules"], {"match": [".*"], "checks": []}]}
        fx.set_policy(covered)
        code, out = check("--policy", str(fx.policy_path), "--repo", str(fx.primary), cwd=fx.root)
        self.assertEqual((code, out["ok"], out["uncovered"], out["error"]), (0, True, [], None))
        # Default location: <repo>/.merge-queue/policy.json, relative to the checked repository.
        code, out = check()
        self.assertEqual((code, out["ok"]), (1, False))
        self.assertIn("unreadable or invalid", out["error"])
        wt, _ = fx.agent("pc", {".merge-queue/policy.json": json.dumps(covered)})  # a candidate carrying a policy
        code, out = check(cwd=wt)  # the checked repository is the worktree it runs in
        self.assertEqual((code, out["ok"], out["repo"], out["policy"]),
                         (0, True, str(wt), str(wt / ".merge-queue" / "policy.json")))
        self.assertEqual(out["files"], 6)  # the five base files plus the policy itself
        # Schema violations: unknown check name in a rule, a check without argv, wrong version.
        for broken in ({**covered, "rules": [{"match": ["*"], "checks": ["nope"]}]},
                       {**covered, "checks": {"unit": {"argv": []}}}, {**covered, "version": 2}):
            fx.set_policy(broken)
            code, out = check("--policy", str(fx.policy_path))
            self.assertEqual((code, out["ok"]), (1, False))
            self.assertTrue(out["error"])
        outside = fx.mq("policy", "check", cwd=fx.root)
        self.assertEqual(outside.returncode, 2, outside.stderr)

    def test_policy_check_works_without_an_installation(self):
        fx = self.fx
        fx.mq("uninstall", check=True)
        fx.set_policy({**UNIT_POLICY, "rules": [{"match": ["*"], "checks": []}]})
        result = fx.mq("policy", "check", "--policy", str(fx.policy_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_neutral_messages_name_the_installed_command_only(self):
        fx = self.fx
        a_wt, _ = fx.agent("a", {"shared.py": "TITLE = 'one'\n"})
        b_wt, _ = fx.agent("b", {"shared.py": "TITLE = 'two'\n"})
        fx.start_runner()
        fx.submit(a_wt, "req-a")
        fx.submit(b_wt, "req-b")
        fx.wait_states(["req-a", "req-b"])
        entry = fx.inbox("wt-b")[0]
        self.assertIn("`merge-queue retry <request_id>`", entry["required_action"])
        quiet = fx.mq("inbox", "--quiet", cwd=b_wt)
        self.assertIn("run `merge-queue inbox`", quiet.stdout)
        payload = json.dumps({"cwd": str(b_wt), "session_id": "wt-b"})
        notice = json.loads(fx.mq("hook", "session-start", cwd=fx.root, input=payload).stdout)
        self.assertIn("`merge-queue inbox`", notice["hookSpecificOutput"]["additionalContext"])
        for text in (entry["required_action"], quiet.stdout, json.dumps(notice)):
            self.assertNotIn("scripts/", text)


class PolicyUnits(unittest.TestCase):
    def test_overlap_checks_both_directions(self):
        mq = load_module()
        self.assertTrue(mq.overlaps("runtime/", ["runtime/data.db"]))
        self.assertTrue(mq.overlaps("a/b/", ["a"]))  # an incoming file where an on-disk directory lives
        self.assertTrue(mq.overlaps("a", ["a/b"]))
        self.assertFalse(mq.overlaps("ab", ["a/b", "a"]))

    def test_policy_routes_known_scopes_and_fails_closed(self):
        mq = load_module()
        policy = {"version": 1, "checks": {"syntax": {"argv": ["true"]}, "docs": {"argv": ["true"]}},
                  "rules": [{"match": ["docs/*", "*.md"], "checks": []}, {"match": ["src/*"], "checks": ["syntax"]},
                            {"match": ["src/docs/*"], "checks": ["docs", "syntax"]}]}
        self.assertEqual(mq.plan_checks(policy, ["docs/a.md", "README.md"]), ([], []))
        self.assertEqual(mq.plan_checks(policy, ["src/a.py", "src/deep/b.py"]), (["syntax"], []))  # '*' crosses '/'
        self.assertEqual(mq.plan_checks(policy, ["src/docs/x.txt"]), (["syntax", "docs"], []))  # policy order, a set
        self.assertEqual(mq.plan_checks(policy, ["src/a.py", "unlisted/blob.bin"]), (["syntax"], ["unlisted/blob.bin"]))

    def test_invalid_policy_is_an_operator_failure(self):
        mq = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "p.json"
            bad.write_text('{"version": 1, "rules": [{"match": ["*"], "checks": ["missing"]}], "checks": {}}')
            with self.assertRaises(mq.Fail) as caught:
                mq.load_policy(bad)
            self.assertEqual(caught.exception.klass, "operator")

    def test_request_ids_are_path_safe(self):
        mq = load_module()
        for bad in ("../x", "a/b", "", ".hidden", "x" * 65):
            with self.assertRaises(mq.MqError):
                mq.valid_id(bad)
        self.assertEqual(mq.valid_id("mq-2026.10_a"), "mq-2026.10_a")
