"""A not-ready integration checkout no longer stops validation: the request validates, then waits to publish.

Every repository, state directory and process is fixture-owned. Dirt is created and removed by writing file content
back, never by resetting the fixture's integration checkout.
"""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixture import Fixture, load_module

DIRTY = "def value():\n    return 99\n"
CLEAN = "def value():\n    return 1\n"


def counting_policy(marker: Path) -> dict:
    return {"version": 1, "rules": [{"match": ["*.py"], "checks": ["count"]}],
            "checks": {"count": {"argv": [sys.executable, "-c", f"open({str(marker)!r}, 'a').write('x\\n')"],
                                 "timeout": 30}}}


class CheckoutWaitsAfterValidation(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.marker = self.fx.root / "ran"
        self.fx.set_policy(counting_policy(self.marker))
        self.notice = self.fx.state / "notices" / "checkout-wait.json"

    def runs(self):
        return len(self.marker.read_text().split()) if self.marker.exists() else 0

    def runner(self, **env):
        return self.fx.start_runner(MERGE_QUEUE_CHECKOUT_WAIT_DELAY="0.3", **env)

    def waiting(self, rid="req-x"):
        req = self.fx.req(rid)
        return bool(req and req["state"] == "queued" and req.get("checkout_wait_reason"))

    def test_dirty_primary_is_validated_once_then_waits_visibly_and_lands_after_cleanup(self):
        fx = self.fx
        fx.write(fx.primary, "lib.py", DIRTY)
        wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        self.runner()
        fx.submit(wt, "req-x")
        fx.wait_for(lambda: self.waiting(), 60, "the request to validate and then wait for the checkout")
        self.assertEqual(self.runs(), 1)  # validated while the checkout was dirty
        result = json.loads(fx.mq("result", "req-x", "--json", cwd=wt).stdout)
        self.assertEqual(result["state"], "queued")
        self.assertIn("checkout_dirty", result["checkout_wait_reason"])
        self.assertTrue(result["checkout_wait_since"])
        self.assertIn("waiting for the integration checkout", result["last_failure"])
        text = fx.mq("result", "req-x", cwd=wt).stdout
        self.assertIn("waiting=checkout_dirty", text)
        # Re-dispatches during the wait publish-check only: no second preparation or validation.
        fx.wait_for(lambda: len([l for l in (fx.state / "diagnostics" / "events.jsonl").read_text().splitlines()
                                 if '"checkout_wait"' in l]) >= 4, 60, "several waits")
        self.assertEqual(self.runs(), 1)
        self.assertEqual((fx.primary / "lib.py").read_text(), DIRTY)  # never reset
        self.assertEqual(fx.subjects(), ["base"])  # nothing was published into the dirty checkout
        fx.write(fx.primary, "lib.py", CLEAN)  # restore the original content; never `git reset`
        done = fx.wait_states(["req-x"])["req-x"]
        self.assertEqual(done["state"], "landed", fx.debug())
        self.assertEqual(self.runs(), 1)
        self.assertEqual((done["attempts"], done["dispatches"]), (1, 1))
        self.assertTrue(done["plan"]["results_reused"])
        self.assertNotIn("checkout_wait_since", done)
        self.assertNotIn("checkout_wait_reason", done)
        self.assertFalse(self.notice.exists())  # cleared when the request landed, not on a later runner tick
        self.assertEqual(fx.subjects(), ["x", "base"])

    def test_checkout_dirtied_during_validation_waits_instead_of_blocking(self):
        fx = self.fx
        gate = fx.root / "gate"
        wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        self.runner(MERGE_QUEUE_FAULT=f"after_validate:wait={gate}:1")
        fx.submit(wt, "req-x")
        fx.wait_for(lambda: Path(str(gate) + ".reached").exists(), 30, "validation to finish")
        fx.write(fx.primary, "lib.py", DIRTY)
        gate.write_text("go")
        fx.wait_for(lambda: self.waiting(), 60, "the publication-time wait")
        req = fx.req("req-x")
        self.assertEqual(req["state"], "queued", fx.debug())
        self.assertIn("checkout_dirty", req["checkout_wait_reason"])
        self.assertIsNone(req.get("publication"))
        self.assertEqual((req["attempts"], req["dispatches"]), (0, 0))  # unpenalised
        self.assertTrue(self.notice.exists())
        self.assertEqual((fx.primary / "lib.py").read_text(), DIRTY)
        fx.write(fx.primary, "lib.py", CLEAN)
        done = fx.wait_states(["req-x"])["req-x"]
        self.assertEqual(done["state"], "landed", fx.debug())
        self.assertEqual(self.runs(), 1)
        self.assertTrue(done["plan"]["results_reused"])
        self.assertEqual(fx.subjects(), ["x", "base"])

    def test_checkout_dirtied_after_the_publication_receipt_waits_and_clears_the_receipt(self):
        fx = self.fx
        gate = fx.root / "gate"
        wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        self.runner(MERGE_QUEUE_FAULT=f"after_receipt:wait={gate}:1")
        fx.submit(wt, "req-x")
        fx.wait_for(lambda: Path(str(gate) + ".reached").exists(), 30, "the publication receipt")
        self.assertEqual(fx.req("req-x")["phase"], "publishing")
        fx.write(fx.primary, "lib.py", DIRTY)
        gate.write_text("go")
        fx.wait_for(lambda: self.waiting(), 60, "the publication-time wait")
        req = fx.req("req-x")
        self.assertIsNone(req.get("publication"))  # the helper retracted its intent before waiting
        self.assertEqual(fx.subjects(), ["base"])  # nothing was published into the dirty checkout
        fx.write(fx.primary, "lib.py", CLEAN)
        done = fx.wait_states(["req-x"])["req-x"]
        self.assertEqual(done["state"], "landed", fx.debug())
        self.assertEqual(self.runs(), 1)
        self.assertEqual(fx.subjects(), ["x", "base"])

    def test_a_wait_that_clears_during_validation_lands_without_stale_wait_fields(self):
        fx = self.fx
        gate = fx.root / "gate"
        fx.write(fx.primary, "lib.py", DIRTY)
        wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        self.runner(MERGE_QUEUE_FAULT=f"after_validate:wait={gate}:1")
        fx.submit(wt, "req-x")
        fx.wait_for(lambda: Path(str(gate) + ".reached").exists(), 30, "validation to finish")
        running = fx.req("req-x")
        self.assertEqual(running["state"], "running")  # the wait is recorded without stopping the work
        self.assertIn("checkout_dirty", running["checkout_wait_reason"])
        self.assertTrue(self.notice.exists())
        fx.write(fx.primary, "lib.py", CLEAN)
        gate.write_text("go")
        done = fx.wait_states(["req-x"])["req-x"]
        self.assertEqual(done["state"], "landed", fx.debug())
        self.assertEqual((done["attempts"], self.runs()), (1, 1))
        self.assertNotIn("checkout_wait_since", done)
        self.assertNotIn("checkout_wait_reason", done)
        self.assertFalse(self.notice.exists())

    def test_wait_limit_still_ends_in_the_original_terminal_failure_after_validation(self):
        fx = Fixture(install_args=("--checkout-wait-limit", "2"))
        self.addCleanup(fx.close)
        marker = fx.root / "ran"
        fx.set_policy(counting_policy(marker))
        fx.write(fx.primary, "lib.py", DIRTY)
        wt, _ = fx.agent("x", {"x.py": "X = 1\n"})
        fx.start_runner(MERGE_QUEUE_CHECKOUT_WAIT_DELAY="0.3")
        fx.submit(wt, "req-x")
        state = fx.wait_states(["req-x"])["req-x"]
        self.assertEqual((state["state"], state["result"]["failure_type"], state["result"]["failure_class"]),
                         ("blocked", "checkout_dirty", "operator"), fx.debug())
        self.assertIn("waited 2s", state["result"]["message"])
        self.assertEqual(len(marker.read_text().split()), 1)  # validated once, then waited out the limit
        self.assertEqual(fx.inbox("wt-x")[0]["failure_type"], "checkout_dirty")
        self.assertEqual((fx.primary / "lib.py").read_text(), DIRTY)
        fx.wait_for(lambda: not self.notice.exists(), 30, "the notice to clear")


class BriefAndShow(unittest.TestCase):
    def req(self, **extra):
        return {"request_id": "r1", "state": "queued", "source_commit_sha": "a" * 40, **extra}

    def test_brief_carries_the_wait_fields_only_while_the_request_is_live(self):
        core = load_module()
        waiting = self.req(checkout_wait_since="2026-01-01T00:00:00Z", checkout_wait_reason="checkout_dirty: lib.py",
                           last_failure="waiting for the integration checkout: checkout_dirty")
        out = core.brief(waiting)
        self.assertEqual((out["checkout_wait_since"], out["checkout_wait_reason"], out["last_failure"]),
                         ("2026-01-01T00:00:00Z", "checkout_dirty: lib.py",
                          "waiting for the integration checkout: checkout_dirty"))
        self.assertNotIn("checkout_wait_reason", core.brief(self.req()))
        self.assertNotIn("last_failure", core.brief(self.req()))
        self.assertNotIn("checkout_wait_reason", core.brief({**waiting, "state": "blocked"}))

    def test_show_prints_the_wait_reason(self):
        core = load_module()
        req = self.req(checkout_wait_since="2026-01-01T00:00:00Z", checkout_wait_reason="checkout_dirty: lib.py")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            core.show(req, type("A", (), {"json": False})())
        self.assertIn("r1 queued", buf.getvalue())
        self.assertIn("waiting=checkout_dirty: lib.py", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
