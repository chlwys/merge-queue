"""Opt-in parallel validation checks (`"parallel": N`); all repositories and processes are fixture-owned."""
import copy
import json
import os
import signal
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixture import Fixture, load_module

# usage: probe.py MARKS NAME MODE [OTHER]; every check records its pid and a "started" file in MARKS.
#   meet:  wait (bounded) for OTHER's started file, exit 0 when it appears and 1 when it does not
#   fail:  like meet, but always exit 1 once OTHER has started
#   loop:  stay alive (bounded) and write a "finished" file if nothing stopped it
PROBE = r"""
import os, sys, time
from pathlib import Path
marks, name, mode = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
other = sys.argv[4] if len(sys.argv) > 4 else None
(marks / f"pid-{name}").write_text(str(os.getpid()))
(marks / f"started-{name}").write_text(str(time.time()))
if mode == "loop":
    for _ in range(300):
        time.sleep(0.1)
    (marks / f"finished-{name}").write_text("x")
    sys.exit(0)
deadline = time.time() + float(os.environ.get("PROBE_WAIT", "20"))
while not (marks / f"started-{other}").exists() and time.time() < deadline:
    time.sleep(0.05)
seen = (marks / f"started-{other}").exists()
if mode == "fail":
    sys.exit(1 if seen else 2)
(marks / f"finished-{name}").write_text("x")
sys.exit(0 if seen else 1)
"""


class ParallelChecks(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.marks = self.fx.root / "marks"
        self.marks.mkdir()
        self.probe = self.fx.root / "probe.py"
        self.probe.write_text(PROBE)

    def configure(self, modes, **top):
        """`modes` maps check name -> (mode, other); checks are listed in this (policy) order."""
        policy = {"version": 1, "rules": [{"match": ["*.py"], "checks": list(modes)}], "checks": {}, **top}
        for name, (mode, other) in modes.items():
            argv = [sys.executable, str(self.probe), str(self.marks), name, mode] + ([other] if other else [])
            policy["checks"][name] = {"argv": argv, "timeout": 60, "env": {"PROBE_WAIT": "6"}}
        self.fx.set_policy(policy)

    def land(self, rid="req"):
        wt, _ = self.fx.agent(rid, {"w.py": "W = 1\n"})
        self.fx.submit(wt, rid)
        self.fx.start_runner()
        return self.fx.wait_states([rid])[rid]

    def mark(self, kind, name):
        return self.marks / f"{kind}-{name}"

    def alive(self, name):
        try:
            os.kill(int(self.mark("pid", name).read_text()), 0)
            return True
        except (OSError, ValueError):
            return False

    def test_checks_overlap_and_the_request_lands(self):
        self.configure({"a": ("meet", "b"), "b": ("meet", "a")}, parallel=2)
        req = self.land()
        self.assertEqual(req["state"], "landed", self.fx.debug())
        self.assertTrue(self.mark("finished", "a").exists() and self.mark("finished", "b").exists())
        self.assertEqual(sorted(s["check"] for s in req["validation"]), ["a", "b"])
        self.assertEqual(req["validated"]["checks"], ["a", "b"])

    def test_without_the_key_checks_run_one_after_the_other(self):
        for top in ({}, {"parallel": 1}):
            with self.subTest(top=top):
                self.setUp()
                self.configure({"a": ("meet", "b"), "b": ("meet", "a")}, **top)
                req = self.land()
                self.assertEqual(req["state"], "validation_failed", self.fx.debug())
                self.assertTrue(req["result"]["message"].startswith("a failed"), req["result"]["message"])
                self.assertFalse(self.mark("started", "b").exists())

    def test_a_failing_check_stops_the_other_and_names_the_failure(self):
        for failing, looping in (("a", "b"), ("b", "a")):  # the failure is chosen by outcome, not by position
            with self.subTest(failing=failing):
                self.setUp()
                self.configure({failing: ("fail", looping), looping: ("loop", None)}
                               if failing == "a" else {looping: ("loop", None), failing: ("fail", looping)}, parallel=2)
                req = self.land()
                self.assertEqual(req["state"], "validation_failed", self.fx.debug())
                self.assertTrue(req["result"]["message"].startswith(f"{failing} failed"), req["result"]["message"])
                self.assertEqual(req["result"]["failure_class"], "owner")
                self.assertFalse(self.mark("finished", looping).exists())
                self.assertFalse(self.alive(looping))
                self.fx.settled()

    def test_termination_stops_every_live_group_that_the_attempt_file_tracks(self):
        fx = self.fx
        self.configure({"a": ("loop", None), "b": ("loop", None)}, parallel=2)
        wt, _ = fx.agent("req", {"w.py": "W = 1\n"})
        fx.submit(wt, "req")
        runner = fx.start_runner()
        fx.wait_for(lambda: self.mark("started", "a").exists() and self.mark("started", "b").exists(), 60, "both checks")
        attempt = fx.state / "attempts" / "req.json"
        data = fx.wait_for(lambda: (json.loads(attempt.read_text()) if attempt.exists() else {}).get("groups")
                           and json.loads(attempt.read_text()), 30, "both groups in the attempt file")
        self.assertEqual(len(data["groups"]), 2, data)
        self.assertIsNone(data["group"])
        pids = {int(self.mark("pid", n).read_text()) for n in ("a", "b")}
        self.assertEqual({g["pid"] for g in data["groups"]}, pids)
        os.kill(data["identity"]["pid"], signal.SIGTERM)
        fx.wait_for(lambda: not self.alive("a") and not self.alive("b"), 30, "both checks to be stopped")
        self.assertFalse(self.mark("finished", "a").exists() or self.mark("finished", "b").exists())
        fx.stop_runner(runner)

    def test_owned_pids_reads_every_recorded_group(self):
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        mod = load_module()
        stub = types.SimpleNamespace(p=lambda *parts: self.fx.root.joinpath("stub", *parts))
        write = lambda data: mod.write_json(stub.p("attempts", "r.json"), data)
        mod.write_json(stub.p("requests", "r.json"), {"request_id": "r", "seq": 1, "state": "queued"})
        write({"identity": None, "attempt": "x", "group": None})
        self.assertNotIn(proc.pid, mod.owned_pids(stub))
        write({"identity": None, "attempt": "x", "group": None, "groups": [mod.proc_identity(proc.pid)]})
        self.assertIn(proc.pid, mod.owned_pids(stub))
        write({"identity": None, "attempt": "x", "group": mod.proc_identity(proc.pid)})  # the old single key
        self.assertIn(proc.pid, mod.owned_pids(stub))

    def test_policy_rejects_invalid_parallel(self):
        mod = load_module()
        base = {"version": 1, "rules": [], "checks": {}}
        path = self.fx.root / "p.json"
        for value in (0, -1, True, "2", 1.5, None):
            with self.subTest(value=value):
                path.write_text(json.dumps({**base, "parallel": value}))
                with self.assertRaises(mod.Fail) as caught:
                    mod.load_policy(path)
                self.assertEqual(caught.exception.ftype, "policy_invalid")
        for value in (1, 2, 8):
            path.write_text(json.dumps({**base, "parallel": value}))
            self.assertEqual(mod.load_policy(path)["parallel"], value)
        path.write_text(json.dumps(base))
        self.assertNotIn("parallel", mod.load_policy(path))


if __name__ == "__main__":
    unittest.main()
