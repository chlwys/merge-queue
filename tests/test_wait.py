"""Receipt observation: deterministic race checks and actual native filesystem delivery."""
import builtins
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixture import SRC, load_module


class ReceiptWaiting(unittest.TestCase):
    def setUp(self):
        self.core = load_module()
        temporary = tempfile.TemporaryDirectory(prefix="mq-wait-")
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name).resolve()
        (self.state / "requests").mkdir()
        self.ctx = SimpleNamespace(state=self.state, p=lambda *parts: self.state.joinpath(*parts))
        self.pending = {"request_id": "receipt", "state": "queued"}
        self.terminal = dict(self.pending, state="landed")
        self.path = self.ctx.p("requests", "receipt.json")
        self.core.write_json(self.path, self.pending)

    def wait(self, timeout=5):
        return self.core.wait_terminal(self.ctx, "receipt", timeout)

    def test_immediate_results_and_nonpositive_timeout_do_not_import_watcher(self):
        original = builtins.__import__

        def imports(name, *args, **kwargs):
            if name == "watchfiles":
                self.fail("immediate receipt unexpectedly imported watchfiles")
            return original(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=imports):
            for timeout in (0, -1):
                self.assertEqual(self.wait(timeout), self.pending)
            self.core.write_json(self.path, self.terminal)
            self.assertEqual(self.wait(), self.terminal)
            tombstone = self.ctx.p("tombstones", "receipt.json")
            self.core.write_json(tombstone, self.terminal)
            self.path.unlink()
            self.assertEqual(self.wait(), self.terminal)
            tombstone.unlink()
            with self.assertRaisesRegex(self.core.MqError, "unknown request receipt"):
                self.wait()

    def test_ordinary_core_import_does_not_load_external_dependencies(self):
        result = subprocess.run([sys.executable, "-S", "-c",
            "import sys; import merge_queue.core; "
            "assert not {'watchfiles', 'fastmcp', 'mcp'} & sys.modules.keys()"],
            env=dict(os.environ, PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1"),
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_registration_checkpoint_closes_race_before_first_event(self):
        closed = []

        def events(*roots, **options):
            self.assertFalse(options["force_polling"])
            self.assertFalse(options["recursive"])
            self.assertFalse(options["ignore_permission_denied"])
            self.assertEqual(set(roots), {self.ctx.p("requests"), self.state})
            accept = options["watch_filter"]
            self.assertTrue(accept(1, str(self.path)))
            self.assertTrue(accept(1, str(self.ctx.p("tombstones"))))
            self.assertFalse(accept(1, str(self.ctx.p("requests", "another.json"))))
            # Change happened before registration, so there will be no event for it.
            self.core.write_json(self.path, self.terminal)
            try:
                yield set()
                self.fail("registration checkpoint missed terminal receipt")
            finally:
                closed.append(True)

        with mock.patch("watchfiles.watch", side_effect=events):
            self.assertEqual(self.wait(), self.terminal)
        self.assertEqual(closed, [True])
        self.assertFalse(self.ctx.p("tombstones").exists())

    def test_quiet_wakeups_do_not_read_receipts_and_timeout_reconciles(self):
        loads = []
        original = self.core.load_req

        def load(*args):
            loads.append(True)
            return original(*args)

        def events(*roots, **options):
            yield set()  # exactly one registration checkpoint
            self.assertEqual(len(loads), 2)
            for _ in range(4):
                yield set()
                self.assertEqual(len(loads), 2)
            # Completion races the deadline: no event is yielded, but final snapshot must see it.
            self.core.write_json(self.path, self.terminal)

        with mock.patch("watchfiles.watch", side_effect=events), mock.patch.object(self.core, "load_req", load):
            self.assertEqual(self.wait(), self.terminal)
        self.assertEqual(len(loads), 3)

    def test_directory_registration_race_rechecks_after_rebuild(self):
        registrations = []

        def events(*roots, **options):
            registrations.append(set(roots))
            if len(registrations) == 1:
                self.ctx.p("tombstones").mkdir()
                yield {(1, str(self.ctx.p("tombstones")))}
            else:
                # Acknowledge between the directory event snapshot and its new watcher registration.
                self.core.write_json(self.ctx.p("tombstones", "receipt.json"), self.terminal)
                self.path.unlink()
                yield set()

        with mock.patch("watchfiles.watch", side_effect=events):
            self.assertEqual(self.wait(), self.terminal)
        self.assertEqual(registrations, [{self.ctx.p("requests"), self.state},
                                         {self.ctx.p("requests"), self.ctx.p("tombstones")}])

    def test_native_watcher_failure_surfaces_without_polling_or_replay(self):
        with mock.patch("watchfiles.watch", side_effect=OSError("native watch failed")), \
                mock.patch.object(self.core.time, "sleep", side_effect=AssertionError("polling")):
            with self.assertRaisesRegex(OSError, "native watch failed"):
                self.wait()
        self.assertEqual(json.loads(self.path.read_text()), self.pending)

    def test_interrupt_closes_observer_and_preserves_receipt(self):
        closed = []

        def events(*roots, **options):
            try:
                yield set()
                raise KeyboardInterrupt
            finally:
                closed.append(True)

        with mock.patch("watchfiles.watch", side_effect=events):
            with self.assertRaises(KeyboardInterrupt):
                self.wait()
        self.assertEqual(closed, [True])
        self.assertEqual(json.loads(self.path.read_text()), self.pending)

    def test_missing_dependency_has_actionable_no_replay_error(self):
        original = builtins.__import__

        def imports(name, *args, **kwargs):
            if name == "watchfiles":
                raise ModuleNotFoundError(name)
            return original(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=imports):
            with self.assertRaisesRegex(self.core.MqError, "result receipt --wait.*do not resubmit"):
                self.wait()
        self.assertEqual(json.loads(self.path.read_text()), self.pending)

    def test_native_timeout_returns_pending_without_periodic_receipt_reads(self):
        with mock.patch.object(self.core, "load_req", wraps=self.core.load_req) as load:
            started = time.monotonic()
            self.assertEqual(self.wait(.12), self.pending)
            self.assertLess(time.monotonic() - started, 2)
            self.assertIn(load.call_count, (2, 3))  # initial, optional registration checkpoint, final
        self.assertFalse(self.ctx.p("tombstones").exists())

    def test_native_events_deliver_atomic_receipt_and_acknowledged_tombstone(self):
        for acknowledge in (False, True):
            with self.subTest(acknowledge=acknowledge):
                self.core.write_json(self.path, self.pending)
                registered = threading.Event()
                original = self.core.load_req
                reads = []
                results, errors = [], []

                def load(*args):
                    reads.append(True)
                    if len(reads) == 2:
                        registered.set()  # first quiet checkpoint proves the native watcher is registered
                    return original(*args)

                def wait():
                    try:
                        results.append(self.wait(5))
                    except BaseException as error:
                        errors.append(error)

                with mock.patch.object(self.core, "load_req", side_effect=load), \
                        mock.patch.dict(os.environ, WATCHFILES_FORCE_POLLING="true"):
                    thread = threading.Thread(target=wait)
                    thread.start()
                    try:
                        self.assertTrue(registered.wait(3), "native registration checkpoint never arrived")
                        count = len(reads)
                        self.core.write_json(self.ctx.p("requests", "unrelated.json"), self.terminal)
                        threading.Event().wait(.15)  # allow native delivery of the filtered unrelated event
                        self.assertEqual(len(reads), count)
                        if acknowledge:
                            self.core.write_json(self.ctx.p("tombstones", "receipt.json"), self.terminal)
                            self.path.unlink()
                        else:
                            self.core.write_json(self.path, self.terminal)
                    finally:
                        thread.join(6)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(results, [self.terminal])
