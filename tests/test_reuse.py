"""Validation record, exact reuse and path-scoped carry-over; all repositories and processes are fixture-owned."""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixture import Fixture, load_module

POLICY = {
    "version": 1,
    "rules": [{"match": ["*.py"], "checks": ["a"]}, {"match": ["b/*"], "checks": ["b"]},
              {"match": ["docs/*"], "checks": []}],
    "checks": {},
}


class Reuse(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.policy = copy.deepcopy(POLICY)
        for name in ("a", "b"):
            marker = self.marker(name)
            self.policy["checks"][name] = {
                "argv": [sys.executable, "-c", f"open({str(marker)!r}, 'a').write('x\\n')"], "timeout": 30}
        self.fx.set_policy(self.policy)

    def marker(self, name):
        return self.fx.root / f"ran-{name}"

    def runs(self, name):
        path = self.marker(name)
        return len(path.read_text().split()) if path.exists() else 0

    def candidate(self, rid="req"):
        wt, sha = self.fx.agent(rid, {"w.py": "W = 1\n", "b/w.txt": "w\n"})
        self.fx.submit(wt, rid)
        return sha

    def external(self, files, subject="external"):
        for name, text in files.items():
            self.fx.write(self.fx.primary, name, text)
        self.fx.git("add", "-A")
        self.fx.git("-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", subject)

    def land_with_move(self, files, rid="req", during=None):
        """Land `rid` while `files` are committed straight onto main during its first validation."""
        fx = self.fx
        gate = fx.root / "gate"
        self.candidate(rid)
        fx.start_runner(MERGE_QUEUE_FAULT=f"after_validate:wait={gate}:1")
        fx.wait_for(lambda: Path(str(gate) + ".reached").exists(), 30, "first validation to finish")
        first = fx.req(rid)
        self.assertEqual(first["validated"]["checks"], ["a", "b"])
        self.external(files)
        if during:
            during()
        gate.write_text("go")
        req = fx.wait_states([rid])[rid]
        self.assertEqual(req["state"], "landed", fx.debug())
        self.assertEqual(fx.subjects()[:2], [rid, "external"])
        return first, req

    def test_record_is_written_after_validation_and_describes_the_candidate(self):
        fx = self.fx
        gate = fx.root / "gate"
        self.candidate()
        fx.start_runner(MERGE_QUEUE_FAULT=f"after_validate:wait={gate}:1")
        fx.wait_for(lambda: Path(str(gate) + ".reached").exists(), 30, "validation to finish")
        req = fx.req("req")
        record = req["validated"]
        self.assertEqual(record["target"], req["candidate"]["target_sha"])
        self.assertEqual(record["candidate"], req["candidate"]["sha"])
        self.assertEqual(record["paths"], ["b/w.txt", "w.py"])
        self.assertEqual(record["policy"], load_module().hashlib.sha256(fx.policy_path.read_bytes()).hexdigest())
        self.assertIn("at", record)
        self.assertFalse(req["plan"]["results_reused"])
        gate.write_text("go")
        self.assertEqual(fx.wait_states(["req"])["req"]["state"], "landed")
        self.assertEqual((self.runs("a"), self.runs("b")), (1, 1))

    def test_docs_only_move_carries_every_check(self):
        first, req = self.land_with_move({"docs/note.md": "note\n"})
        self.assertEqual((self.runs("a"), self.runs("b")), (1, 1))
        self.assertTrue(req["plan"]["results_reused"])
        self.assertEqual(req["plan"]["reused_checks"], ["a", "b"])
        self.assertEqual(req["plan"]["reused_from"], {"target": first["validated"]["target"],
                                                      "candidate": first["validated"]["candidate"]})
        self.assertEqual(req["validated"]["checks"], ["a", "b"])
        self.assertEqual(req["validated"]["target"], req["candidate"]["target_sha"])
        self.assertIn("validation_reused", (self.fx.state / "diagnostics" / "events.jsonl").read_text())

    def test_move_routed_to_the_same_check_reruns_only_that_check(self):
        _, req = self.land_with_move({"other.py": "O = 1\n"})
        self.assertEqual((self.runs("a"), self.runs("b")), (2, 1))
        self.assertEqual(req["plan"]["reused_checks"], ["b"])
        self.assertEqual(req["validated"]["checks"], ["a", "b"])

    def test_move_routed_to_the_other_check_reruns_only_that_check(self):
        _, req = self.land_with_move({"b/other.txt": "o\n"})
        self.assertEqual((self.runs("a"), self.runs("b")), (1, 2))
        self.assertEqual(req["plan"]["reused_checks"], ["a"])

    def test_move_through_an_unrouted_path_reruns_everything(self):
        _, req = self.land_with_move({"unrouted.dat": "u\n"})
        self.assertEqual((self.runs("a"), self.runs("b")), (2, 2))
        self.assertFalse(req["plan"]["results_reused"])
        self.assertEqual(req["plan"]["reused_checks"], [])

    def test_policy_change_in_between_reruns_everything(self):
        def change():
            self.policy["note"] = "changed"
            self.fx.set_policy(self.policy)
        _, req = self.land_with_move({"docs/note.md": "note\n"}, during=change)
        self.assertEqual((self.runs("a"), self.runs("b")), (2, 2))
        self.assertFalse(req["plan"]["results_reused"])

    def test_carry_validation_false_reruns_everything(self):
        self.policy["carry_validation"] = False
        self.fx.set_policy(self.policy)
        _, req = self.land_with_move({"docs/note.md": "note\n"})
        self.assertEqual((self.runs("a"), self.runs("b")), (2, 2))
        self.assertFalse(req["plan"]["results_reused"])

    def test_unknown_candidate_scope_still_fails_closed_after_a_move(self):
        fx = self.fx
        gate = fx.root / "gate"
        wt, _ = fx.agent("req", {"w.py": "W = 1\n"})
        fx.submit(wt, "req")
        fx.start_runner(MERGE_QUEUE_FAULT=f"after_validate:wait={gate}:1")
        fx.wait_for(lambda: Path(str(gate) + ".reached").exists(), 30, "first validation to finish")
        self.policy["rules"] = [r for r in self.policy["rules"] if r["match"] != ["*.py"]]  # w.py becomes unrouted
        fx.set_policy(self.policy)
        self.external({"docs/note.md": "note\n"})
        gate.write_text("go")
        req = fx.wait_states(["req"])["req"]
        self.assertEqual((req["state"], req["result"]["failure_type"]), ("blocked", "unknown_scope"))
        self.assertNotIn("validated", req)
        self.assertEqual(self.runs("a"), 1)

    def test_interrupted_after_validation_redispatch_reuses_the_result(self):
        fx = self.fx
        self.candidate()
        fx.start_runner(MERGE_QUEUE_FAULT="after_validate:exit:1")
        req = fx.wait_states(["req"])["req"]
        self.assertEqual(req["state"], "landed", fx.debug())
        self.assertEqual((req["attempts"], req["dispatches"]), (2, 2))
        self.assertEqual((self.runs("a"), self.runs("b")), (1, 1))
        self.assertTrue(req["plan"]["results_reused"])
        self.assertEqual(req["plan"]["reused_checks"], ["a", "b"])
        self.assertEqual(fx.subjects()[:1], ["req"])
        self.assertEqual(fx.git("status", "--porcelain").stdout, "")

    def test_redispatch_after_a_target_move_does_not_reuse_exactly(self):
        fx = self.fx
        self.candidate()
        fx.start_runner(MERGE_QUEUE_FAULT="after_validate:exit:1")
        fx.wait_for(lambda: fx.req("req")["attempts"] >= 1 and fx.req("req").get("validated"), 30, "validation")
        self.external({"other.py": "O = 1\n"})
        req = fx.wait_states(["req"])["req"]
        self.assertEqual(req["state"], "landed", fx.debug())
        self.assertEqual(self.runs("a"), 2)  # the target changed under an a-routed path: a is not carried

    def test_inline_lane_records_nothing(self):
        fx = self.fx
        self.policy["checks"]["presentation"] = {"argv": [sys.executable, "-c", "pass"], "timeout": 10}
        self.policy["rules"].append({"match": ["docs/*.css"], "checks": ["presentation"]})
        self.policy["fast_path"] = {"plan": [sys.executable, "-c", "import json; print(json.dumps(dict(eligible=True, reason='r')))"],
                                    "checks": ["presentation"], "timeout": 5}
        fx.set_policy(self.policy)
        wt, _ = fx.agent("fast", {"docs/x.css": "x\n"})
        fx.submit(wt, "fast", "--wait")
        req = fx.req("fast")
        self.assertEqual((req["state"], req["lane"]), ("landed", "inline"), fx.debug())
        self.assertEqual(req["plan"]["checks"], ["presentation"])
        self.assertNotIn("validated", req)
        self.assertFalse(req["plan"]["results_reused"])

    def test_load_policy_rejects_a_non_boolean_carry_validation(self):
        mq = load_module()
        path = self.fx.root / "p.json"
        for bad in ("no", 0, None):
            with self.subTest(value=bad):
                path.write_text(json.dumps({**self.policy, "carry_validation": bad}))
                with self.assertRaises(mq.Fail) as caught:
                    mq.load_policy(path)
                self.assertEqual(caught.exception.ftype, "policy_invalid")
        path.write_text(json.dumps({**self.policy, "carry_validation": False}))
        self.assertIs(mq.load_policy(path)["carry_validation"], False)


if __name__ == "__main__":
    unittest.main()
