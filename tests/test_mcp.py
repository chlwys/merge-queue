"""Actual stdio MCP integration tests with isolated repositories and real Block tq.

Each test starts the real `merge_queue.mcp_server` over stdio against temporary fixture repositories (their own
HOME, state root and tq data). Missing SDK dependencies fail with instructions, never skip.
"""
import asyncio
import hashlib
import json
import os
import shlex
import signal
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ["FASTMCP_ENV_FILE"] = os.devnull
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport
except ImportError as exc:  # pragma: no cover
    raise ImportError("MCP test dependencies unavailable. Install the project dependencies "
                      "(uv sync; fastmcp==4.1.3 mcp==2.3.0 watchfiles==1.3.0). Substantive MCP tests were "
                      "not run.") from exc
from fixture import MQ_CMD, TQ, Fixture, load_module  # noqa: E402

core = load_module()
import merge_queue.mcp_server as adapter  # noqa: E402


def tree(base: Path) -> dict:
    """Every entry under a directory with size, mtime and content hash: proof that nothing was written."""
    out = {}
    for path in sorted(base.rglob("*")):
        stat = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        out[str(path.relative_to(base))] = (stat.st_size, stat.st_mtime_ns, digest)
    return out


class Progress:
    async def report_progress(self, *args, **kwargs):
        pass


class MCPIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertTrue(os.access(TQ, os.X_OK), f"real tq missing at {TQ}")
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.log = self.fx.root / "mcp-stderr.log"
        self.progress = []

    def second_repo(self):
        other = Fixture(state_home=self.fx.state_home)  # shares the state root (and so the registry directory)
        self.addCleanup(other.close)
        return other

    def client(self, *server_args, **extra_env):
        transport = StdioTransport(command=sys.executable,
            args=["-P", "-m", "merge_queue.mcp_server", *server_args], cwd=str(self.fx.root),
            env=dict(self.fx.env, FASTMCP_ENV_FILE=os.devnull, **extra_env),
            keep_alive=False, log_file=self.log)
        return Client(transport, timeout=90)

    async def on_progress(self, current, total, message):
        self.progress.append((current, total, message))

    def args(self, wt, rid, **extra):
        return dict(worktree=str(wt), owner_id="chat-one", request_id=rid,
                    timeout_seconds=60, **extra)

    def data(self, result):
        self.assertFalse(result.is_error, str(result))
        return result.structured_content

    async def test_protocol_success_one_call_and_meaningful_progress(self):
        fx = self.fx
        wt, sha = fx.agent("success", {"a.py": "A = 1\n"})
        fx.start_runner()
        async with self.client(GIT_DIR="/nonexistent/git-dir", MERGE_QUEUE_MANUAL="1") as client:
            tools = await client.list_tools()
            self.assertEqual({tool.name for tool in tools}, {"submit", "retry", "wait_for_result", "watch_request",
                                                             "wait_for_queue_empty", "list_queues"})
            for tool in tools:
                self.assertNotIn("command", tool.input_schema["properties"])
                if tool.name in ("submit", "retry", "wait_for_result"):
                    self.assertIn("owner_id", tool.input_schema["required"])
                else:  # read-only tools take no owner: anyone may watch
                    self.assertNotIn("owner_id", tool.input_schema["properties"])
            instructions = client.instructions
            for phrase in ("watch_request", "wait_for_queue_empty", "list_queues", "never a polling loop",
                           "never resubmit implicitly", "one pending", "independent work",
                           "yield for the host", "short wrapper waits", "transport margin",
                           "cannot wake a closed session", "not a queue timeout"):
                self.assertIn(phrase, instructions)
            result = self.data(await client.call_tool("submit", self.args(wt, "mcp-success"),
                                                     progress_handler=self.on_progress))
            self.assertTrue(result["terminal"])
            self.assertEqual(result["state"], "landed")
            self.assertEqual(result["result"]["landed_sha"], fx.main())
            self.assertEqual(result["source_commit_sha"], sha)
            self.assertEqual(result["exit_code"], 0)
            self.assertTrue(self.progress)
            self.assertTrue(all(msg.startswith("mcp-success:") for _, _, msg in self.progress))
            self.assertEqual([n for n, _, _ in self.progress], list(range(1, len(self.progress) + 1)))
            self.assertEqual(len({msg for _, _, msg in self.progress}), len(self.progress))
            # Deliberate identical retry by id remains one durable queue request.
            duplicate = self.data(await client.call_tool("submit", self.args(wt, "mcp-success")))
            self.assertEqual(duplicate["request_id"], result["request_id"])
            self.assertEqual(len(list((fx.state / "requests").glob("*.json"))), 1)

    async def test_failure_delivered_then_explicit_repair_retry(self):
        fx = self.fx
        wt, _ = fx.agent("bad", {"lib.py": "def value():\n    return 2\n"})
        fx.start_runner()
        async with self.client() as client:
            failed = self.data(await client.call_tool("submit", self.args(wt, "bad-receipt")))
            self.assertEqual(failed["state"], "validation_failed")
            self.assertTrue(failed["terminal"])
            self.assertEqual(failed["exit_code"], 10)
            self.assertTrue(failed["result"]["log_paths"])
            self.assertTrue(failed["result"]["required_action"])
            fx.write(wt, "lib.py", "def value():\n    return 1\n")
            fx.write(wt, "fixed.py", "FIXED = True\n")
            fx.git("add", "-A", cwd=wt)
            fx.git("commit", "-q", "-m", "repair", cwd=wt)
            fixed = self.data(await client.call_tool("retry", self.args(wt, "bad-receipt",
                                                                       new_request_id="fixed-receipt")))
            self.assertEqual(fixed["state"], "landed")
            self.assertEqual(fixed["request_id"], "fixed-receipt")
            self.assertEqual(fx.req("bad-receipt")["state"], "superseded")
            old = self.data(await client.call_tool("wait_for_result", self.args(wt, "bad-receipt")))
            self.assertEqual((old["state"], old["exit_code"], old["superseded_by"]), ("superseded", 12, "fixed-receipt"))
            # Reusing the new ID for a different source must fail before CLI retry.
            fx.write(wt, "changed.py", "CHANGED = True\n")
            fx.git("add", "-A", cwd=wt)
            fx.git("commit", "-q", "-m", "later change", cwd=wt)
            refused = await client.call_tool("retry", self.args(wt, "bad-receipt",
                                            new_request_id="fixed-receipt"), raise_on_error=False)
            self.assertTrue(refused.is_error)
            self.assertEqual(len(list((fx.state / "requests").glob("*.json"))), 2)

    async def test_timeout_then_reattach_does_not_resubmit(self):
        fx = self.fx
        wt, _ = fx.agent("timeout", {"a.py": "A = 1\n"})
        async with self.client() as client:
            args = self.args(wt, "timeout-receipt")
            args["timeout_seconds"] = 0.1
            pending = self.data(await client.call_tool("submit", args))
            self.assertTrue(pending["timed_out"])
            self.assertFalse(pending["terminal"])
            self.assertEqual(pending["exit_code"], 30)
            self.assertIn("Do not resubmit", pending["next_action"])
            fx.start_runner()
            final = self.data(await client.call_tool("wait_for_result", self.args(wt, "timeout-receipt")))
            self.assertEqual(final["state"], "landed")
            self.assertEqual(len(list((fx.state / "requests").glob("*.json"))), 1)

    async def test_cancellation_and_transport_disconnect_leave_job_durable(self):
        fx = self.fx
        wt, _ = fx.agent("cancel", {"a.py": "A = 1\n"})
        accepted = asyncio.Event()

        async def progress(current, total, message):
            accepted.set()

        async with self.client() as client:
            task = asyncio.create_task(client.call_tool("submit", self.args(wt, "cancel-receipt"),
                                                        progress_handler=progress))
            await asyncio.wait_for(accepted.wait(), 20)
            self.assertEqual(fx.req("cancel-receipt")["state"], "queued")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        # The stdio server is now stopped; the independent queue job still exists.
        self.assertEqual(fx.req("cancel-receipt")["state"], "queued")
        fx.start_runner()
        async with self.client() as reconnect:
            done = self.data(await reconnect.call_tool("wait_for_result", self.args(wt, "cancel-receipt")))
            self.assertEqual(done["state"], "landed")
            self.assertEqual(len(list((fx.state / "requests").glob("*.json"))), 1)

    async def test_acceptance_survives_server_process_group_teardown(self):
        fx = self.fx
        wt, _ = fx.agent("group-death", {"a.py": "A = 1\n"})
        marker, release = fx.root / "acceptance-started.json", fx.root / "release"
        wrapper = fx.root / "acceptance-wrapper.py"
        wrapper.write_text("import json,os,sys,time\nfrom pathlib import Path\n"
            + f"marker=Path({str(marker)!r}); release=Path({str(release)!r})\n"
            + "marker.write_text(json.dumps(dict(pid=os.getpid(),sid=os.getsid(0),"
              "parent=os.getppid(),parent_sid=os.getsid(os.getppid()))))\n"
            + "deadline=time.monotonic()+25\n"
            + "while not release.exists() and time.monotonic()<deadline: time.sleep(0.05)\n"
            + "os.execv(sys.executable,[sys.executable,'-P','-m','merge_queue.core',*sys.argv[1:]])\n")
        fx.git("config", "mergequeue.entry", shlex.join([sys.executable, str(wrapper)]))
        async with self.client() as client:
            task = asyncio.create_task(client.call_tool("submit", self.args(wt, "group-receipt")))
            await asyncio.to_thread(fx.wait_for, marker.exists, 20, "acceptance child started")
            identity = json.loads(marker.read_text())
            self.assertEqual(identity["pid"], identity["sid"])
            self.assertNotEqual(identity["sid"], identity["parent_sid"])
            parent_group = os.getpgid(identity["parent"])
            self.assertNotEqual(parent_group, os.getpgrp(), "never kill the test runner group")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            os.killpg(parent_group, signal.SIGKILL)
        # Restore fixture configuration before the independent CLI loads it.
        fx.git("config", "mergequeue.entry", shlex.join(MQ_CMD))
        release.touch()
        await asyncio.to_thread(fx.wait_for, lambda: fx.req("group-receipt"), 20,
                                "acceptance survives MCP server death")
        fx.start_runner()
        async with self.client() as client:
            done = self.data(await client.call_tool("wait_for_result", self.args(wt, "group-receipt")))
            self.assertEqual(done["state"], "landed")
            self.assertEqual(len(list((fx.state / "requests").glob("*.json"))), 1)

    async def test_guards_repository_owner_ids_timeouts_and_no_shell(self):
        fx = self.fx
        wt, _ = fx.agent("guards", {"a.py": "A = 1\n"})
        async with self.client() as client:
            for update in ({"request_id": "../bad"}, {"request_id": "bad\n"}, {"owner_id": ""},
                           {"owner_id": "chat; touch bad"}, {"timeout_seconds": 0},
                           {"timeout_seconds": -1}, {"timeout_seconds": 82801},
                           {"worktree": "relative"}, {"command": "touch bad"}):
                args = self.args(wt, "guarded") | update
                result = await client.call_tool("submit", args, raise_on_error=False)
                self.assertTrue(result.is_error, str(result))
            self.assertFalse(list((fx.state / "requests").glob("*.json")))
            foreign = fx.root / "foreign"
            foreign.mkdir()
            fx.git("init", "-q", cwd=foreign)
            result = await client.call_tool("submit", self.args(foreign, "foreign"), raise_on_error=False)
            self.assertTrue(result.is_error)
            args = self.args(wt, "guarded") | {"timeout_seconds": 0.1}
            self.data(await client.call_tool("submit", args))
            for tool in ("submit", "wait_for_result"):
                result = await client.call_tool(tool, self.args(wt, "guarded") | {"owner_id": "other"},
                                                raise_on_error=False)
                self.assertTrue(result.is_error)
            self.assertEqual(fx.req("guarded")["owner_id"], "chat-one")
            self.assertFalse((fx.root / "bad").exists())

    async def test_dedup_preserves_caller_route_and_cannot_retry_other_owner(self):
        fx = self.fx
        wt, _ = fx.agent("dedup", {"a.py": "A = 1\n"})
        fx.submit(wt, "canonical", "--owner", "original-owner")
        async with self.client() as client:
            pending = self.data(await client.call_tool("submit", self.args(wt, "alias")
                                                      | {"timeout_seconds": 0.1}))
            self.assertEqual(pending["request_id"], "canonical")
            self.assertEqual(pending["owner_id"], "original-owner")
            self.assertEqual(pending["reconnect_request_id"], "alias")
            self.assertEqual(pending["reconnect_owner_id"], "chat-one")
            intent = json.loads((fx.state / "mcp-receipts/alias.json").read_text())
            self.assertEqual(intent["queue_request_id"], "canonical")
            refused = await client.call_tool("retry", self.args(wt, "alias", new_request_id="bad-retry"),
                                             raise_on_error=False)
            self.assertTrue(refused.is_error)
            fx.start_runner()
            final = self.data(await client.call_tool("wait_for_result", self.args(wt, "alias")))
            self.assertEqual(final["state"], "landed")
            self.assertEqual(len(list((fx.state / "requests").glob("*.json"))), 1)

    async def test_lost_acceptance_reply_reconciles_intent_without_resubmission(self):
        fx = self.fx
        wt, sha = fx.agent("lost-reply", {"a.py": "A = 1\n"})
        queue = core.load_ctx(fx.primary)
        # Intent persisted, then existing CLI accepted/deduplicated. Simulate
        # server death before it could bind the canonical response to the intent.
        adapter.reserve(queue, "lost-reply", "chat-one", wt, sha, "submit")
        fx.submit(wt, "existing", "--owner", "other-chat")
        intent_path = fx.state / "mcp-receipts/lost-reply.json"
        self.assertNotIn("queue_request_id", json.loads(intent_path.read_text()))
        fx.start_runner()
        async with self.client() as client:
            final = self.data(await client.call_tool("wait_for_result", self.args(wt, "lost-reply")))
            self.assertEqual(final["state"], "landed")
            self.assertEqual(final["request_id"], "existing")
            self.assertEqual(json.loads(intent_path.read_text())["queue_request_id"], "existing")
            self.assertEqual(len(list((fx.state / "requests").glob("*.json"))), 1)
            fx.mq("ack", "existing", check=True)
            acknowledged = self.data(await client.call_tool("wait_for_result", self.args(wt, "lost-reply")))
            self.assertEqual(acknowledged["state"], "landed")
            self.assertEqual(acknowledged["result"]["landed_sha"], fx.main())
            self.assertIn("tombstones", acknowledged["receipt_path"])

    async def test_registration_checkpoint_and_quiet_idle_receipt(self):
        # Exercise the observer directly to deterministically mutate in its startup gap;
        # wire/protocol behavior is covered by the actual stdio tests above.
        queue = core.load_ctx(self.fx.primary)
        wt, _ = self.fx.agent("race", {"a.py": "A = 1\n"})
        self.fx.submit(wt, "race", "--owner", "chat-one")
        original_watch = adapter.awatch
        snapshots = []
        read = adapter.receipt

        def counted(*args):
            snapshots.append(True)
            return read(*args)

        async def gap_watcher(*args, **kwargs):
            # Simulates the completion BEFORE native registration, with only a
            # checkpoint (empty yield), followed by quiet internal watcher timeouts.
            req = self.fx.req("race")
            req.update(state="landed", result={"landed_sha": "fixture"})
            core.write_json(queue.p("requests", "race.json"), req)
            yield set()

        with mock.patch.object(adapter, "receipt", counted), mock.patch.object(adapter, "awatch", gap_watcher):
            result = await adapter.await_result(queue, "race", "chat-one", 2, Progress())
            self.assertEqual(result["state"], "landed")
            self.assertEqual(len(snapshots), 2)
            req = self.fx.req("race")
            req.update(state="queued", result={})
            core.write_json(queue.p("requests", "race.json"), req)
            snapshots.clear()
        with mock.patch.object(adapter, "receipt", counted), mock.patch.object(adapter, "awatch", original_watch):
            pending = await adapter.await_result(queue, "race", "chat-one", 2.2, Progress())
            self.assertFalse(pending["terminal"])
            # Initial snapshot, registration checkpoint, final timeout snapshot only.
            self.assertEqual(len(snapshots), 3)

    # ------------------------------------------------------------------ one global server, many repositories

    async def test_one_server_serves_two_repositories_and_lists_both(self):
        fx = self.fx
        other = self.second_repo()
        a_wt, _ = fx.agent("one", {"a.py": "A = 1\n"})
        b_wt, _ = other.agent("two", {"b.py": "B = 1\n"})
        fx.start_runner()
        other.start_runner()
        async with self.client() as client:  # no --repo, no --allow-repo
            first = self.data(await client.call_tool("submit", self.args(a_wt, "multi-one")))
            second = self.data(await client.call_tool("submit", self.args(b_wt, "multi-two")))
            self.assertEqual((first["state"], second["state"]), ("landed", "landed"))
            self.assertNotEqual(first["repository"], second["repository"])
            self.assertEqual(fx.subjects(), ["one", "base"])
            self.assertEqual(other.subjects(), ["two", "base"])
            listing = self.data(await client.call_tool("list_queues", {}))
            self.assertEqual(listing["state_root"], str(fx.state_home.resolve()))
            self.assertEqual({q["repo_id"] for q in listing["queues"]}, {fx.repo_id(), other.repo_id()})
            self.assertTrue(all(q["pending"] == [] and not q.get("problem") for q in listing["queues"]))
            one = self.data(await client.call_tool("list_queues", {"repository": fx.repo_id()}))
            self.assertEqual([q["repo_id"] for q in one["queues"]], [fx.repo_id()])
            by_path = self.data(await client.call_tool("list_queues", {"repository": str(b_wt)}))
            self.assertEqual([q["repo_id"] for q in by_path["queues"]], [other.repo_id()])

    async def test_unregistered_or_mismatched_repositories_are_refused_even_with_forged_config(self):
        fx = self.fx
        wt, _ = fx.agent("honest", {"a.py": "A = 1\n"})
        # A clone that carries mergequeue.* config but was never installed (no registry entry): refused, and its
        # configured entry must never be executed.
        forged = fx.root / "forged"
        forged.mkdir()
        fx.git("init", "-q", cwd=forged)
        fx.git("commit", "-q", "--allow-empty", "-m", "x", cwd=forged)
        marker = fx.root / "executed"
        for key, value in (("stateDir", str(fx.root / "forged-state")), ("checkout", str(forged)), ("tq", TQ),
                           ("entry", f"touch {marker}"), ("policy", str(fx.policy_path))):
            fx.git("config", f"mergequeue.{key}", value, cwd=forged)
        async with self.client() as client:
            for tool, args in (("submit", self.args(forged, "forged")),
                               ("watch_request", dict(repository=str(forged), request_id="x")),
                               ("wait_for_queue_empty", dict(repository=str(forged))),
                               ("list_queues", dict(repository=str(forged)))):
                result = await client.call_tool(tool, args, raise_on_error=False)
                self.assertTrue(result.is_error, tool)
                self.assertIn("not registered", str(result), tool)
            listing = self.data(await client.call_tool("list_queues", {}))
            self.assertEqual([q["repo_id"] for q in listing["queues"]], [fx.repo_id()])
            # A registry entry that disagrees with the repository's own config is refused too.
            entry_path = fx.state_home / "repos" / f"{fx.repo_id()}.json"
            original = entry_path.read_text()
            entry = json.loads(original)
            entry["state_dir"] = str(fx.root / "somewhere-else")
            entry_path.write_text(json.dumps(entry))
            result = await client.call_tool("submit", self.args(wt, "mismatch"), raise_on_error=False)
            self.assertTrue(result.is_error)
            self.assertIn("not registered", str(result))
            entry_path.write_text(original)
            self.data(await client.call_tool("submit", self.args(wt, "honest") | {"timeout_seconds": 0.1}))
        self.assertFalse(marker.exists())
        self.assertFalse((fx.root / "forged-state").exists())

    async def test_allow_repo_restricts_every_tool_to_the_listed_repositories(self):
        fx = self.fx
        other = self.second_repo()
        a_wt, _ = fx.agent("one", {"a.py": "A = 1\n"})
        b_wt, _ = other.agent("two", {"b.py": "B = 1\n"})
        other.submit(b_wt, "theirs", "--owner", "x")
        async with self.client("--allow-repo", str(fx.primary)) as client:
            self.data(await client.call_tool("submit", self.args(a_wt, "mine") | {"timeout_seconds": 0.1}))
            for tool, args in (("submit", self.args(b_wt, "denied")),
                               ("wait_for_result", self.args(b_wt, "theirs")),
                               ("watch_request", dict(repository=str(b_wt), request_id="theirs")),
                               ("watch_request", dict(repository=other.repo_id(), request_id="theirs")),
                               ("wait_for_queue_empty", dict(repository=other.repo_id()))):
                result = await client.call_tool(tool, args, raise_on_error=False)
                self.assertTrue(result.is_error, tool)
                self.assertIn("--allow-repo", str(result), tool)
            listing = self.data(await client.call_tool("list_queues", {}))
            self.assertEqual([q["repo_id"] for q in listing["queues"]], [fx.repo_id()])
        self.assertIsNone(other.req("denied"))
        # A repeated --allow-repo (any path inside the repository) admits both.
        async with self.client("--allow-repo", str(fx.primary), "--allow-repo", str(b_wt)) as client:
            listing = self.data(await client.call_tool("list_queues", {}))
            self.assertEqual({q["repo_id"] for q in listing["queues"]}, {fx.repo_id(), other.repo_id()})

    # ------------------------------------------------------------------ read-only tools

    async def test_non_owner_watch_request_reaches_terminal_and_writes_nothing(self):
        fx = self.fx
        wt, _ = fx.agent("watched", {"a.py": "A = 1\n"})
        fx.submit(wt, "watched", "--owner", "somebody-else")
        before = tree(fx.state)
        self.assertFalse((fx.state / "tombstones").exists())  # the observer must not create it
        async with self.client() as client:
            # Pending and no runner: a short watch times out, non-terminal, and leaves the state untouched.
            timed = self.data(await client.call_tool("watch_request", dict(
                repository=str(wt), request_id="watched", timeout_seconds=1.5), progress_handler=self.on_progress))
            self.assertEqual((timed["terminal"], timed["timed_out"], timed["exit_code"]), (False, True, 30))
            self.assertEqual(timed["owner_id"], "somebody-else")
            self.assertNotIn("reconnect_owner_id", timed)
            self.assertIn("watch_request", timed["next_action"])
            self.assertEqual(tree(fx.state), before)
            # Now let it land while a different session (repo_id form, no owner) is watching.
            self.progress.clear()
            watch = asyncio.create_task(client.call_tool("watch_request", dict(
                repository=fx.repo_id(), request_id="watched", timeout_seconds=60), progress_handler=self.on_progress))
            await asyncio.sleep(1.0)
            self.assertFalse(watch.done())
            fx.start_runner()
            done = self.data(await asyncio.wait_for(watch, 60))
            self.assertEqual((done["state"], done["terminal"], done["timed_out"], done["exit_code"]),
                             ("landed", True, False, 0))
            self.assertEqual(done["result"]["landed_sha"], fx.main())
            self.assertEqual(done["repository"], str(Path(fx.git("rev-parse", "--path-format=absolute",
                                                                 "--git-common-dir").stdout.strip()).resolve()))
            self.assertTrue(done["receipt_path"].endswith("watched.json"))
            self.assertTrue(self.progress and all(m.startswith("watched:") for _, _, m in self.progress))
            self.assertIn("landed", self.progress[-1][2])
        fx.settled()
        # Watching a finished request (the runner is done; nothing may change): immediate and write-free.
        settled = tree(fx.state)
        async with self.client() as client:
            again = self.data(await client.call_tool("watch_request", dict(repository=str(wt), request_id="watched")))
            self.assertTrue(again["terminal"])
            listing = self.data(await client.call_tool("list_queues", {}))
            self.data(await client.call_tool("wait_for_queue_empty", dict(repository=str(wt))))
        self.assertEqual(listing["queues"][0]["pending"], [])
        self.assertEqual(tree(fx.state), settled)
        self.assertFalse((fx.state / "tombstones").exists())
        self.assertFalse((fx.state / "mcp-receipts").exists())

    async def test_watch_request_resolves_requested_ids_and_acknowledged_receipts_read_only(self):
        fx = self.fx
        wt, sha = fx.agent("alias", {"a.py": "A = 1\n"})
        fx.submit(wt, "canonical", "--owner", "original-owner")
        queue = core.load_ctx(fx.primary)
        # An intent that was never bound (lost acceptance reply) resolves by commit, and watching never binds it.
        adapter.reserve(queue, "requested-alias", "chat-one", wt, sha, "submit")
        before = tree(fx.state)
        async with self.client() as client:
            for name in ("canonical", "requested-alias"):
                seen = self.data(await client.call_tool("watch_request", dict(
                    repository=str(wt), request_id=name, timeout_seconds=0.3)))
                self.assertEqual((seen["request_id"], seen["timed_out"]), ("canonical", True), name)
            started = time.monotonic()
            for bad in ("nope", "../x"):
                result = await client.call_tool("watch_request", dict(repository=str(wt), request_id=bad),
                                                raise_on_error=False)
                self.assertTrue(result.is_error, bad)
            self.assertLess(time.monotonic() - started, 5)  # unknown ids are immediate errors, never a wait
        self.assertEqual(tree(fx.state), before)
        self.assertNotIn("queue_request_id", json.loads((fx.state / "mcp-receipts/requested-alias.json").read_text()))
        # After landing and acknowledgement the tombstone still answers, through the same read-only path.
        fx.start_runner()
        fx.wait_states(["canonical"])
        fx.settled()
        fx.mq("ack", "canonical", check=True)
        async with self.client() as client:
            late = self.data(await client.call_tool("watch_request", dict(repository=str(wt), request_id="canonical")))
            self.assertEqual((late["state"], late["terminal"], late["exit_code"]), ("landed", True, 0))
            self.assertIn("tombstones", late["receipt_path"])
            self.assertEqual(late["result"]["landed_sha"], fx.main())

    async def test_watch_request_notices_a_tombstone_directory_that_appears_while_watching(self):
        # The observer must not create `tombstones/`; it watches the state directory for it instead and rebuilds
        # the watcher. Drive the observer directly: the request leaves requests/ for tombstones/ mid-watch.
        fx = self.fx
        wt, _ = fx.agent("late", {"a.py": "A = 1\n"})
        fx.submit(wt, "late", "--owner", "somebody")
        queue = core.load_ctx(fx.primary)
        self.assertFalse((fx.state / "tombstones").exists())
        task = asyncio.create_task(adapter.await_watch(queue, "late", 30, Progress()))
        await asyncio.sleep(1.5)  # past the registration checkpoint
        self.assertFalse((fx.state / "tombstones").exists())  # still not created by the observer
        req = fx.req("late")
        req.update(state="landed", result={"landed_sha": "fixture", "finished_at": core.now()})
        core.write_json(queue.p("requests", "late.json"), req)
        done = await asyncio.wait_for(task, 20)
        self.assertEqual((done["state"], done["timed_out"]), ("landed", False))
        # Same again, but the terminal answer exists only as a tombstone (created by the CLI, not by us).
        req.update(state="queued", result={})
        core.write_json(queue.p("requests", "late.json"), req)
        task = asyncio.create_task(adapter.await_watch(queue, "late", 30, Progress()))
        await asyncio.sleep(1.5)
        req.update(state="landed", result={"landed_sha": "fixture", "finished_at": core.now()})
        core.write_json(queue.p("requests", "late.json"), req)
        fx.mq("ack", "late", check=True)
        done = await asyncio.wait_for(task, 20)
        self.assertTrue(done["terminal"])

    async def test_wait_for_queue_empty_is_immediate_when_empty_and_returns_after_a_landing(self):
        fx = self.fx
        wt, _ = fx.agent("drain", {"a.py": "A = 1\n"})
        async with self.client() as client:
            started = time.monotonic()
            idle = self.data(await client.call_tool("wait_for_queue_empty", dict(repository=str(wt))))
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual((idle["empty"], idle["timed_out"], idle["pending"]), (True, False, []))
            self.assertEqual(idle["repo_id"], fx.repo_id())
            self.assertTrue(idle["observed_at"].endswith("+00:00"))
            fx.submit(wt, "drain", "--owner", "someone")
            before = tree(fx.state)
            timed = self.data(await client.call_tool("wait_for_queue_empty", dict(
                repository=fx.repo_id(), timeout_seconds=1.2)))
            self.assertEqual((timed["empty"], timed["timed_out"], timed["pending"]), (False, True, ["drain"]))
            self.assertEqual(tree(fx.state), before)  # read-only
            self.progress.clear()
            waiter = asyncio.create_task(client.call_tool("wait_for_queue_empty", dict(
                repository=str(wt), timeout_seconds=60), progress_handler=self.on_progress))
            await asyncio.sleep(1.0)
            self.assertFalse(waiter.done())
            fx.start_runner()
            done = self.data(await asyncio.wait_for(waiter, 60))
            self.assertEqual((done["empty"], done["timed_out"], done["pending"]), (True, False, []))
            self.assertEqual(fx.req("drain")["state"], "landed")
            self.assertIn("1 pending: drain", [m for _, _, m in self.progress])
            self.assertEqual(self.progress[-1][2], "0 pending")
            # A failed-but-unacknowledged request is terminal: it does not keep the queue "busy".
            bad_wt, _ = fx.agent("bad", {"lib.py": "def value():\n    return 2\n"})
            fx.submit(bad_wt, "bad", "--owner", "someone")
            fx.wait_states(["bad"])
            idle_again = self.data(await client.call_tool("wait_for_queue_empty", dict(repository=str(wt))))
            self.assertTrue(idle_again["empty"])

    async def test_read_only_tools_validate_their_inputs(self):
        fx = self.fx
        wt, _ = fx.agent("inputs", {"a.py": "A = 1\n"})
        async with self.client() as client:
            cases = [("watch_request", dict(repository=str(wt), request_id="x", timeout_seconds=0)),
                     ("watch_request", dict(repository=str(wt), request_id="x", timeout_seconds=82801)),
                     ("watch_request", dict(repository="relative/path", request_id="x")),
                     ("watch_request", dict(repository="zzzzzzzzzzzz", request_id="x")),
                     ("watch_request", dict(repository="0123456789ab", request_id="x")),
                     ("watch_request", dict(repository=str(wt), request_id="bad\n")),
                     ("wait_for_queue_empty", dict(repository=str(wt), timeout_seconds=-1)),
                     ("wait_for_queue_empty", dict(repository=str(fx.root / "missing"))),
                     ("wait_for_queue_empty", dict(repository=str(fx.root))),  # not a repository
                     ("list_queues", dict(repository="nonsense"))]
            for tool, args in cases:
                result = await client.call_tool(tool, args, raise_on_error=False)
                self.assertTrue(result.is_error, (tool, args))
        self.assertFalse(list((fx.state / "requests").glob("*.json")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
