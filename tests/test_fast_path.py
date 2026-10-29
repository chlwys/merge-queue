"""Inline routing/publication regressions; all processes and repositories are fixture-owned."""
import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixture import Fixture, MQ_CMD, UNIT_POLICY, load_module


class FastPath(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.policy = copy.deepcopy(UNIT_POLICY)
        self.policy["checks"]["presentation"] = {"argv": [sys.executable, "-c", "pass"], "timeout": 10}
        self.policy["fast_path"] = {"plan": [sys.executable, "{checkout}/fast-plan.py", "{target}", "{candidate}"],
                                    "checks": ["presentation"], "timeout": 5}
        self.fx.write(self.fx.primary, "fast-plan.py",
                      "import json, subprocess, sys\n"
                      "paths = subprocess.check_output(['git', 'diff', '--name-only', *sys.argv[1:]]).decode().splitlines()\n"
                      "print(json.dumps(dict(eligible=bool(paths) and all(p.endswith('.css') for p in paths), "
                      "reason='CSS only', files=paths)))\n")
        self.fx.git("add", "fast-plan.py")
        self.fx.git("commit", "-q", "-m", "trusted planner")
        self.fx.set_policy(self.policy)

    def css(self, name="style"):
        return self.fx.agent(name, {f"docs/{name}.css": "body { color: blue; }\n"})

    def submit_process(self, wt, rid, wait=True, **extra):
        return subprocess.Popen([*MQ_CMD, "submit", "--request-id", rid, "--json", *(["--wait"] if wait else [])], cwd=wt,
                                env=dict(self.fx.env, **extra), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)

    def finish(self, proc):
        out, err = proc.communicate(timeout=40)
        self.assertEqual(proc.returncode, 0, out + err + self.fx.debug())
        return json.loads(out)

    def test_eligible_submit_lands_inline_without_tq_and_deduplicates(self):
        fx = self.fx
        wt, sha = self.css()
        result, data = fx.submit(wt, "fast", "--wait")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        req = fx.req("fast")
        self.assertEqual((data["state"], req["lane"], req["dispatches"], req["tq"]), ("landed", "inline", 0, None))
        self.assertEqual(req["plan"]["checks"], ["presentation"])
        self.assertEqual(fx.main(), sha)
        self.assertEqual(fx.tq_events(), [])
        duplicate, data = fx.submit(wt, "another-key")
        self.assertEqual(duplicate.returncode, 0)
        self.assertEqual(data["request_id"], "fast")
        self.assertTrue(data["duplicate"])
        self.assertEqual(fx.req("fast")["attempts"], 1)
        self.assertIsNone(fx.req("another-key"))

    def test_focused_check_failure_never_publishes(self):
        self.policy["checks"]["presentation"]["argv"] = [sys.executable, "-c", "raise SystemExit(2)"]
        self.fx.set_policy(self.policy)
        before = self.fx.main()
        wt, _ = self.css()
        self.fx.submit(wt, "failed", "--wait")
        req = self.fx.req("failed")
        self.assertEqual(req["state"], "validation_failed")
        self.assertEqual(self.fx.main(), before)
        self.assertEqual(self.fx.tq_events(), [])

    def test_ineligible_and_broken_planner_enter_normal_queue(self):
        fx = self.fx
        wt, _ = fx.agent("code", {"extra.py": "X = 1\n"})
        fx.submit(wt, "code")
        self.assertEqual((fx.req("code")["state"], fx.req("code")["lane"]), ("queued", "normal"))
        self.policy["fast_path"]["plan"] = [sys.executable, "-c", "print('not JSON')"]
        fx.set_policy(self.policy)
        css, _ = self.css()
        fx.submit(css, "broken")
        self.assertEqual(fx.req("broken")["lane"], "normal")
        self.assertIn("planner unavailable", fx.req("broken")["fast_path"]["reason"])
        fx.start_runner()
        states = fx.wait_states(["code", "broken"])
        self.assertEqual({r["state"] for r in states.values()}, {"landed"}, fx.debug())
        self.assertTrue(all(r["dispatches"] == 1 for r in states.values()))

    def test_candidate_cannot_replace_the_trusted_classifier(self):
        wt, _ = self.fx.agent("malicious", {"fast-plan.py": "print('{\"eligible\":true,\"reason\":\"candidate\"}')\n"})
        self.fx.submit(wt, "untrusted")
        self.assertEqual(self.fx.req("untrusted")["lane"], "normal")
        self.assertEqual(self.fx.req("untrusted")["fast_path"]["reason"], "CSS only")

    def test_fast_lane_passes_slow_fifo_validation_then_fifo_refreshes(self):
        fx = self.fx
        gate, reached = fx.root / "release-slow", fx.root / "slow-started"
        slow = fx.root / "slow.py"
        slow.write_text("import pathlib, time\n"
                        f"pathlib.Path({str(reached)!r}).touch()\n"
                        f"while not pathlib.Path({str(gate)!r}).exists(): time.sleep(.05)\n")
        self.policy["checks"]["unit"]["argv"] = [sys.executable, str(slow)]
        fx.set_policy(self.policy)
        normal, _ = fx.agent("normal", {"ordinary.py": "X = 1\n"})
        fast, _ = self.css()
        fx.submit(normal, "normal")
        fx.start_runner()
        fx.wait_for(reached.exists, 20, "slow validator started")
        work = next((fx.state / "work").iterdir())
        before = fx.main()
        result, _ = fx.submit(fast, "fast", "--wait")
        self.assertEqual(result.returncode, 0, result.stderr + fx.debug())
        self.assertEqual(fx.req("fast")["state"], "landed", fx.debug())
        self.assertEqual(fx.req("normal")["phase"], "validating")
        self.assertTrue(work.exists(), "fast executor must preserve live normal candidate")
        self.assertNotEqual(fx.main(), before)
        gate.touch()
        state = fx.wait_states(["normal"])["normal"]
        self.assertEqual(state["state"], "landed", fx.debug())
        self.assertEqual(state["candidate"]["target_sha"], fx.req("fast")["result"]["landed_sha"])
        self.assertEqual(fx.subjects()[:2], ["normal", "style"])

    def test_stale_source_routes_inline_and_target_move_revalidates(self):
        fx = self.fx
        wt, _ = self.css()
        fx.git("commit", "--allow-empty", "-q", "-m", "advance before submit")
        gate = fx.root / "pause"
        proc = self.submit_process(wt, "refresh", MERGE_QUEUE_FAULT=f"after_validate:wait={gate}:1")
        fx.wait_for(lambda: Path(str(gate) + ".reached").exists(), 20, "inline validation")
        old = fx.req("refresh")["candidate"]["target_sha"]
        fx.git("commit", "--allow-empty", "-q", "-m", "advance during validation")
        current = fx.main()
        self.assertNotEqual(old, current)
        gate.touch()
        self.finish(proc)
        req = fx.req("refresh")
        self.assertEqual((req["state"], req["lane"]), ("landed", "inline"), fx.debug())
        self.assertEqual(req["candidate"]["target_sha"], current)
        self.assertEqual(req["fast_path"]["target"], current)
        self.assertEqual(fx.tq_events(), [])

    def test_trusted_inputs_changed_during_validation_fall_back_without_publish(self):
        fx = self.fx
        wt, _ = self.css()
        before = fx.main()
        gate = fx.root / "pause"
        proc = self.submit_process(wt, "authority", wait=False, MERGE_QUEUE_FAULT=f"after_validate:wait={gate}:1")
        self.finish(proc)
        fx.wait_for(lambda: Path(str(gate) + ".reached").exists(), 20, "inline validation")
        self.policy["fast_path"]["checks"] = []
        fx.set_policy(self.policy)
        gate.touch()
        fx.wait_for(lambda: fx.req("authority")["lane"] == "normal", 20, "normal fallback")
        req = fx.req("authority")
        self.assertEqual((req["state"], req["lane"]), ("queued", "normal"), fx.debug())
        self.assertEqual(fx.main(), before)

    def test_disconnected_submit_and_duplicate_reconnect_do_not_duplicate_publication(self):
        fx = self.fx
        wt, _ = self.css()
        gate = fx.root / "pause"
        proc = self.submit_process(wt, "disconnect", MERGE_QUEUE_FAULT=f"after_validate:wait={gate}:1")
        fx.wait_for(lambda: Path(str(gate) + ".reached").exists(), 20, "inline validation")
        proc.kill()
        proc.communicate(timeout=10)
        # Simulate losing spawn bookkeeping as well as the observer. The real attempt still owns its lock.
        mq = load_module()
        req = fx.req("disconnect")
        req["inline_owner"] = None
        mq.write_json(fx.state / "requests" / "disconnect.json", req)
        common = Path(fx.git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
        mq.tick_request(mq.Ctx(common, mq.read_config(common)), req, {}, {}, {})
        self.assertEqual(fx.req("disconnect")["state"], "running")
        duplicate, data = fx.submit(wt, "disconnect")
        self.assertEqual(duplicate.returncode, 0)
        self.assertTrue(data["duplicate"])
        fx.start_runner()
        gate.touch()
        req = fx.wait_states(["disconnect"])["disconnect"]
        self.assertEqual((req["state"], req["attempts"], req["dispatches"]), ("landed", 1, 0), fx.debug())
        self.assertEqual(fx.subjects().count("style"), 1)

    def test_crashed_inline_executor_recovers_same_receipt_without_tq(self):
        fx = self.fx
        wt, _ = self.css()
        proc = self.submit_process(wt, "crash", wait=False, MERGE_QUEUE_FAULT="after_validate:exit:1")
        self.finish(proc)
        fx.wait_for(lambda: fx.req("crash")["state"] == "running"
                    and load_module().proc_state(fx.req("crash").get("inline_owner")) != "same", 20,
                    "crashed inline executor")
        self.assertEqual(fx.req("crash")["state"], "running")
        fx.start_runner()
        req = fx.wait_states(["crash"])["crash"]
        self.assertEqual((req["state"], req["attempts"], req["dispatches"]), ("landed", 2, 0), fx.debug())
        self.assertEqual(fx.subjects().count("style"), 1)
        self.assertEqual(fx.tq_events(), [])

    def test_prepared_candidate_is_reclassified_before_any_focused_check(self):
        fx = self.fx
        wt, _ = self.css()
        planner = fx.primary / "fast-plan.py"
        planner.write_text(planner.read_text() +
                           "current = subprocess.check_output(['git', 'rev-parse', 'main']).decode().strip()\n"
                           "if sys.argv[1] == current: print(json.dumps(dict(eligible=False, reason='combined rejected')))\n")
        fx.git("add", "fast-plan.py")
        fx.git("commit", "-q", "-m", "tighten trusted classifier")
        before = fx.main()
        fx.submit(wt, "reclassify")
        fx.wait_for(lambda: fx.req("reclassify")["lane"] == "normal", 20, "prepared classification fallback")
        req = fx.req("reclassify")
        self.assertEqual((req["state"], req["lane"]), ("queued", "normal"), fx.debug())
        self.assertEqual(req["fast_path"]["reason"], "combined rejected")
        self.assertEqual(req["validation"], [])
        self.assertEqual(fx.main(), before)


if __name__ == "__main__":
    unittest.main()
