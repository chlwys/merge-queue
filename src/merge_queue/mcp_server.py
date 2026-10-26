"""Global stdio MCP completion adapter for locally installed merge queues.

One server serves every repository that ran `merge-queue install` (optionally restricted with
--allow-repo). Owner-bound tools submit, retry and reconnect; read-only tools let anyone watch a request or
the whole queue. No scheduler, shell command interface, session resume, or model is provided here.
See docs/mcp.md.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import aclosing
import json
import math
import os
from pathlib import Path
import re
from types import SimpleNamespace

# Do not load application .env files in an MCP service.
os.environ["FASTMCP_ENV_FILE"] = os.devnull

from fastmcp import Context, FastMCP
from watchfiles import awatch

from merge_queue import core

MAX_TIMEOUT = 82800  # 23 hours, below the documented 24-hour client timeout.
REPO_ID_RE = re.compile(r"[0-9a-f]{12}")
REQUIRED_CONFIG = ("stateDir", "checkout", "tq", "entry", "policy")
INSTRUCTIONS = (
    "Submit or retry once with a durable request_id and explicit stable owner_id, then wait for this tool's "
    "completion/background notification. Never loop on status. After interruption reconnect the same receipt "
    "with wait_for_result; never resubmit implicitly. To follow a request you do not own, use watch_request; to "
    "wait until a repository's queue has drained, use wait_for_queue_empty. list_queues is one-shot discovery, "
    "never a polling loop.")


def valid_id(value: str, label: str) -> str:
    if not core.ID_RE.fullmatch(value or ""):
        raise ValueError(f"invalid {label}: use 1-64 letters, digits, period, underscore or hyphen")
    return value


def checked_timeout(value: float) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or not 0 < value <= MAX_TIMEOUT:
        raise ValueError(f"timeout_seconds must be finite and greater than 0, at most {MAX_TIMEOUT}")
    return value


async def common_dir(worktree: str, root_only=True) -> tuple[Path, Path]:
    """(resolved path, real common Git dir). root_only demands the worktree root itself (owner-bound tools)."""
    path = Path(worktree)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("worktree must be an existing absolute directory")
    path = path.resolve()
    proc = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel",
        cwd=path, env=core.clean_env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode:
        raise ValueError(f"not a Git worktree: {err.decode(errors='replace').strip()[:500]}")
    lines = out.decode().splitlines()
    if len(lines) != 2 or (root_only and Path(lines[1]).resolve() != path):
        raise ValueError("worktree must name the Git worktree root")
    return path, Path(lines[0]).resolve()


def raw_receipt(ctx, request_id):
    return (core.load_req(ctx, request_id)
            or core.read_json(ctx.p("tombstones", f"{request_id}.json")))


def intent_path(ctx, request_id):
    return ctx.p("mcp-receipts", f"{request_id}.json")


def reserve(ctx, request_id, owner_id, path, sha, operation, retry_of=None):
    """Immutable acceptance intent precedes CLI launch, including lost-response windows."""
    intended = dict(request_id=request_id, owner_id=owner_id, worktree=str(path),
                    source_commit_sha=sha, operation=operation, retry_of=retry_of)
    with core.FileLock(ctx.p("locks", f"mcp-{request_id}.lock")):
        previous = core.read_json(intent_path(ctx, request_id))
        if previous:
            if any(previous.get(key) != value for key, value in intended.items()):
                raise ValueError("request_id is already bound to another acceptance intent")
            return False
        else:
            direct = raw_receipt(ctx, request_id)
            if direct and (direct["owner_id"] != owner_id or direct["source_commit_sha"] != sha):
                raise ValueError("request_id is already bound to another queue receipt")
            if direct and operation == "retry" and direct.get("retry_of") != retry_of:
                raise ValueError("new_request_id is already bound to an unrelated queue receipt")
            core.write_json(intent_path(ctx, request_id), intended)
            return True


def bind(ctx, request_id, queue_request_id):
    with core.FileLock(ctx.p("locks", f"mcp-{request_id}.lock")):
        intent = core.read_json(intent_path(ctx, request_id))
        if intent.get("queue_request_id", queue_request_id) != queue_request_id:
            raise ValueError("acceptance intent is already bound to another queue request")
        intent["queue_request_id"] = queue_request_id
        core.write_json(intent_path(ctx, request_id), intent)


def unbound_match(ctx, intent):
    """Recovery only reads durable receipts; it never submits a job. The queue's exact-SHA dedup may
    legitimately select another owner's existing receipt."""
    matches = [r for r in [*core.all_requests(ctx), *core.tombstones(ctx)]
               if r["source_commit_sha"] == intent["source_commit_sha"]
               and r["state"] != "superseded"
               and (intent["operation"] == "submit" or r.get("retry_of") == intent["retry_of"])]
    return matches[0] if len(matches) == 1 else None


def receipt(ctx, request_id: str, owner_id: str) -> dict:
    intent = core.read_json(intent_path(ctx, request_id))
    if intent:
        if intent["owner_id"] != owner_id:
            raise ValueError(f"request {request_id} belongs to another owner; use its original owner_id")
        canonical = intent.get("queue_request_id")
        req = raw_receipt(ctx, canonical or request_id)
        if req and req["source_commit_sha"] != intent["source_commit_sha"]:
            raise ValueError("acceptance intent does not match the queue receipt commit")
        if req is None and not canonical:
            req = unbound_match(ctx, intent)
        if req is None:
            raise ValueError(f"acceptance unresolved for {request_id}; intent retained at "
                             f"{intent_path(ctx, request_id)}. No resubmission was performed; "
                             "inspect the original acceptance before taking another action.")
        if not canonical:
            bind(ctx, request_id, req["request_id"])
    else:
        req = raw_receipt(ctx, request_id)
        if req is None:
            raise ValueError(f"unknown request {request_id}; use the original durable request_id")
        if req["owner_id"] != owner_id:
            raise ValueError(f"request {request_id} belongs to another owner; use its original owner_id")
    if req.get("repository", str(ctx.common)) != str(ctx.common):
        raise ValueError("receipt repository does not match configured repository")
    return {**req, "reconnect_request_id": request_id, "reconnect_owner_id": owner_id}


def lookup(ctx, request_id: str) -> dict:
    """Read-only twin of receipt() for non-owners: a canonical queue id or an MCP requested id resolves to its
    durable receipt. Binds nothing and creates nothing under the state directory."""
    valid_id(request_id, "request_id")
    intent = core.read_json(intent_path(ctx, request_id))
    if intent:
        canonical = intent.get("queue_request_id")
        req = raw_receipt(ctx, canonical or request_id)
        if req and req["source_commit_sha"] != intent["source_commit_sha"]:
            raise ValueError("acceptance intent does not match the queue receipt commit")
        if req is None and not canonical:
            req = unbound_match(ctx, intent)
        if req is None:
            raise ValueError(f"acceptance unresolved for {request_id}; no queue receipt exists yet")
    else:
        req = raw_receipt(ctx, request_id)
        if req is None:
            raise ValueError(f"unknown request {request_id}")
    if req.get("repository", str(ctx.common)) != str(ctx.common):
        raise ValueError("receipt repository does not match the repository")
    return req


def response(ctx, req: dict, timed_out=False) -> dict:
    terminal = req["state"] in core.TERMINAL
    summary = core.brief(req)
    if not req.get("result") and req.get("acknowledged_at"):
        summary["result"] = {key: req[key] for key in ("landed_sha", "failure_type") if req.get(key)}
        summary["acknowledged_at"] = req["acknowledged_at"]
    durable_path = ctx.p("requests", f"{req['request_id']}.json")
    if not durable_path.exists():
        durable_path = ctx.p("tombstones", f"{req['request_id']}.json")
    base = {**summary, "terminal": terminal, "timed_out": timed_out,
            "exit_code": core.outcome_code(req), "repository": str(ctx.common),
            "receipt_path": str(durable_path)}
    if "reconnect_request_id" not in req:  # a non-owner watcher: no owner route to reconnect through
        return {**base, **({"next_action": "Reconnect with watch_request using the same request_id; "
                                           "the durable job continues."} if not terminal else {})}
    return {**base, "reconnect_request_id": req["reconnect_request_id"],
            "reconnect_owner_id": req["reconnect_owner_id"],
            **({"next_action": "Reconnect with wait_for_result using reconnect_request_id and reconnect_owner_id; "
                               "the durable job continues. Do not resubmit."} if not terminal else {})}


async def observe(ctx, names, accept, stop, create=False):
    """Yield change batches of the nonrecursive state subdirectories `names` (paths accepted by `accept`).

    A directory that does not exist yet (tombstones appear with the first acknowledgement) is not created for
    read-only callers: the state directory is watched for its appearance and the watcher is rebuilt then.
    We explicitly disable watchfiles' environment polling fallback."""
    dirs = [ctx.p(name) for name in names]
    while True:
        if create:
            for directory in dirs:
                directory.mkdir(parents=True, exist_ok=True)
        missing = {str(d) for d in dirs if not d.is_dir()}
        roots = [d for d in dirs if d.is_dir()] + ([ctx.state] if missing else [])
        if not roots:
            raise ValueError("queue state directory does not exist")
        watcher = awatch(*roots, recursive=False, watch_filter=lambda change, path: accept(path) or path in missing,
                         force_polling=False, ignore_permission_denied=False, debounce=50, step=10,
                         rust_timeout=1000, yield_on_timeout=True, stop_event=stop)
        try:
            async for changes in watcher:
                yield changes
                if missing and any(path in missing for _, path in changes):
                    break  # the directory now exists: watch it directly
            else:
                return
        finally:
            await watcher.aclose()


async def watch_until(ctx, names, snapshot, finished, accept_for, timeout_seconds, create=False):
    """Read once, then observe native events. Returns (last snapshot, timed_out). Cancellation stops only this
    observer.

    awatch registers RustNotify before its first asynchronous wait. The first yielded event OR its one-second
    timeout is a registration checkpoint: take a second snapshot then, closing the read/register race. Later
    empty yields do not read queue state or notify the client. The final snapshot after a timeout makes a
    completion racing the timeout terminal, not falsely pending."""
    state = await snapshot()
    if finished(state):
        return state, False
    stop = asyncio.Event()
    try:
        async with asyncio.timeout(timeout_seconds):
            first = True
            async with aclosing(observe(ctx, names, accept_for(state), stop, create)) as events:
                async for changes in events:
                    if not first and not changes:
                        continue
                    first = False
                    state = await snapshot()
                    if finished(state):
                        return state, False
    except TimeoutError:
        pass
    finally:
        stop.set()
    state = await snapshot()
    return state, not finished(state)


async def follow_request(ctx, fetch, label: str, timeout_seconds: float, progress: Context, create: bool):
    last = None
    count = 0

    async def snapshot():
        nonlocal last, count
        req = fetch()
        meaningful = (req["state"], req.get("phase"), req.get("attempts"))
        if meaningful != last:
            last = meaningful
            count += 1
            await progress.report_progress(count, message=f"{label}: {req['state']}"
                                           + (f" / {req['phase']}" if req.get("phase") else ""))
        return req

    def accept_for(req):
        paths = {str(ctx.p(directory, f"{req['request_id']}.json")) for directory in ("requests", "tombstones")}
        return lambda path: path in paths

    return await watch_until(ctx, ("requests", "tombstones"), snapshot, lambda r: r["state"] in core.TERMINAL,
                             accept_for, timeout_seconds, create)


async def await_result(ctx, request_id: str, owner_id: str, timeout_seconds: float,
                       progress: Context) -> dict:
    """Owner-bound: observe one receipt until terminal, reading through receipt() (which may bind an intent)."""
    req, timed_out = await follow_request(ctx, lambda: receipt(ctx, request_id, owner_id), request_id,
                                          timeout_seconds, progress, create=True)
    return response(ctx, req, timed_out=timed_out)


async def await_watch(ctx, request_id: str, timeout_seconds: float, progress: Context) -> dict:
    """Read-only: the same observer for anyone, resolved through lookup(); writes nothing."""
    req, timed_out = await follow_request(ctx, lambda: lookup(ctx, request_id), request_id,
                                          timeout_seconds, progress, create=False)
    return response(ctx, req, timed_out=timed_out)


def pending_requests(ctx) -> list[dict]:
    return [r for r in core.all_requests(ctx) if r["state"] not in core.TERMINAL]


async def await_empty(ctx, timeout_seconds: float, progress: Context) -> dict:
    """Read-only: return once no request of this repository is in a non-terminal state."""
    last = None
    count = 0

    async def snapshot():
        nonlocal last, count
        ids = [r["request_id"] for r in pending_requests(ctx)]
        if ids != last:
            last = ids
            count += 1
            await progress.report_progress(
                count, message=f"{len(ids)} pending" + (f": {', '.join(ids[:5])}" if ids else ""))
        return ids

    def accept_for(_):
        directories = {str(ctx.p("requests")), str(ctx.p("tombstones"))}
        return lambda path: os.path.dirname(path) in directories and path.endswith(".json") \
            and not os.path.basename(path).startswith(".")

    pending, timed_out = await watch_until(ctx, ("requests", "tombstones"), snapshot, lambda ids: not ids,
                                           accept_for, timeout_seconds)
    return {"empty": not pending, "timed_out": timed_out, "repository": str(ctx.common), "repo_id": ctx.repo_id,
            "observed_at": core.now(), "pending": pending}


def queue_view(ctx) -> dict:
    return {"repo_id": ctx.repo_id, "repository": str(ctx.common), "checkout": str(ctx.checkout),
            "target": ctx.target, "state_dir": str(ctx.state),
            "pending": [{key: r.get(key) for key in ("request_id", "owner_id", "state", "phase", "seq")}
                        for r in pending_requests(ctx)]}


async def accepted_ctx(common: Path, allowed: set[Path] | None):
    """A repository is served only when it is allowed, configured AND registered by `merge-queue install`.
    Repository-local config alone proves nothing: any clone can carry forged `mergequeue.*` keys."""
    if allowed is not None and common not in allowed:
        raise ValueError("repository is not in this server's --allow-repo set")
    cfg = await asyncio.to_thread(core.read_config, common)
    if not all(key in cfg for key in REQUIRED_CONFIG):
        raise ValueError("merge queue is not installed for this repository")
    entry = await asyncio.to_thread(core.read_registry, core.repo_id_for(common))
    if not core.registry_matches(entry, common, cfg):
        raise ValueError(f"repository is not registered with this server's state root {core.state_root()} "
                         "(run `merge-queue install`; set MERGE_QUEUE_STATE_HOME to match the installation)")
    return core.Ctx(common, cfg)


async def resolve_repository(repository: str, allowed: set[Path] | None):
    """`repository` is an absolute path inside any worktree of the repo, or its 12-hex repo_id."""
    if os.path.isabs(repository):
        _, common = await common_dir(repository, root_only=False)
    elif REPO_ID_RE.fullmatch(repository):
        entry = await asyncio.to_thread(core.read_registry, repository)
        if not entry:
            raise ValueError(f"unknown repo_id {repository}")
        common = Path(entry["common_dir"])
        if core.repo_id_for(common) != repository:
            raise ValueError(f"registry entry for {repository} is inconsistent")
    else:
        raise ValueError("repository must be an absolute path inside the repository or its 12-character repo_id")
    return await accepted_ctx(common, allowed)


def make_server(allow_repos: list[str] | None = None) -> FastMCP:
    # Resolve once at startup, before stdio serves requests. No global chdir.
    allowed: set[Path] | None = None
    if allow_repos:
        allowed = {asyncio.run(common_dir(path, root_only=False))[1] for path in allow_repos}
    server = FastMCP("merge-queue", instructions=INSTRUCTIONS)

    async def context(worktree, owner_id, request_id, timeout_seconds):
        valid_id(owner_id, "owner_id (chat/session or stable task ID)")
        valid_id(request_id, "request_id")
        checked_timeout(timeout_seconds)
        path, common = await common_dir(worktree)
        # find_common normally preserves hook Git variables. Resolve config from our clean
        # subprocess result instead, so MCP process environment cannot reroute this call.
        return path, await accepted_ctx(common, allowed)

    async def cli(ctx, path, argv, requested_id):
        # Shield protects logical tool cancellation. A separate process session protects
        # the finite acceptance CLI from stdio server process-group teardown. Lost pipe
        # replies are reconciled from the durable intent, never implicitly resubmitted.
        # The independent queue runner owns integration after acceptance.
        async def invoke():
            proc = await asyncio.create_subprocess_exec(
                *ctx.entry, *argv, "--json", cwd=path, env=core.clean_env(),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
            out, err = await proc.communicate()
            if proc.returncode:
                raise ValueError(f"merge-queue refused request ({proc.returncode}): "
                                 f"{err.decode(errors='replace').strip()[:2000]}")
            accepted = json.loads(out)
            canonical = valid_id(accepted["request_id"], "returned request_id")
            await asyncio.to_thread(bind, ctx, requested_id, canonical)
            return accepted
        task = asyncio.create_task(invoke())
        # Retrieve exceptions if the client disappears while acceptance is completing.
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return await asyncio.shield(task)

    @server.tool()
    async def submit(worktree: str, owner_id: str, request_id: str, ctx: Context,
                     timeout_seconds: float = 7200) -> dict:
        """Submit committed HEAD once, then stay pending until terminal. owner_id is a stable chat/session or task ID.

        Save request_id before calling. On timeout/disconnect use wait_for_result with the same
        receipt; do not submit another request. Existing exact-commit deduplication is preserved.
        """
        path, queue = await context(worktree, owner_id, request_id, timeout_seconds)
        sha, branch = await asyncio.to_thread(core.resolve_source,
            SimpleNamespace(commit=None, branch=None), queue, path)
        fresh = await asyncio.to_thread(reserve, queue, request_id, owner_id, path, sha, "submit")
        if not fresh:
            return await await_result(queue, request_id, owner_id, timeout_seconds, ctx)
        argv = ["submit", "--owner", owner_id, "--request-id", request_id, "--commit", sha]
        if branch:
            argv += ["--branch", branch]
        await cli(queue, path, argv, request_id)
        return await await_result(queue, request_id, owner_id, timeout_seconds, ctx)

    @server.tool()
    async def retry(worktree: str, owner_id: str, request_id: str, new_request_id: str,
                    ctx: Context, timeout_seconds: float = 7200) -> dict:
        """After repair and commit, retry a failed receipt once and await completion.

        Supply and save a new durable idempotency key; retry never changes the source worktree.
        A disconnected retry is reattached with wait_for_result(new_request_id), not resubmitted.
        """
        path, queue = await context(worktree, owner_id, request_id, timeout_seconds)
        valid_id(new_request_id, "new_request_id")
        if new_request_id == request_id:
            raise ValueError("new_request_id must differ from request_id")
        old = receipt(queue, request_id, owner_id)
        if old["owner_id"] != owner_id:
            raise ValueError("cannot retry another owner's canonical queue receipt")
        sha, branch = await asyncio.to_thread(core.resolve_source,
            SimpleNamespace(commit=None, branch=None), queue, path)
        fresh = await asyncio.to_thread(reserve, queue, new_request_id, owner_id, path, sha, "retry", old["request_id"])
        if not fresh:
            return await await_result(queue, new_request_id, owner_id, timeout_seconds, ctx)
        argv = ["retry", old["request_id"], "--owner", owner_id,
                "--new-request-id", new_request_id, "--commit", sha]
        if branch:
            argv += ["--branch", branch]
        await cli(queue, path, argv, new_request_id)
        return await await_result(queue, new_request_id, owner_id, timeout_seconds, ctx)

    @server.tool()
    async def wait_for_result(worktree: str, owner_id: str, request_id: str, ctx: Context,
                              timeout_seconds: float = 7200) -> dict:
        """Reconnect one existing durable receipt and await its terminal outcome without resubmitting.

        Use after an interrupted/timed-out call, not as a periodic status loop.
        """
        _, queue = await context(worktree, owner_id, request_id, timeout_seconds)
        return await await_result(queue, request_id, owner_id, timeout_seconds, ctx)

    @server.tool()
    async def watch_request(repository: str, request_id: str, ctx: Context, timeout_seconds: float = 7200) -> dict:
        """Read-only: follow any request of an installed repository until it is terminal (no owner needed).

        repository is an absolute path inside any worktree of the repo, or its repo_id. request_id is a
        canonical queue id or a requested id from submit/retry. Sends progress when the state changes and
        returns the request summary with terminal, timed_out and exit_code. Writes nothing and never
        resubmits; an unknown id is an immediate error. On timeout call it again; do not poll.
        """
        valid_id(request_id, "request_id")
        checked_timeout(timeout_seconds)
        queue = await resolve_repository(repository, allowed)
        return await await_watch(queue, request_id, timeout_seconds, ctx)

    @server.tool()
    async def wait_for_queue_empty(repository: str, ctx: Context, timeout_seconds: float = 7200) -> dict:
        """Read-only: return once the repository's queue has no request in a non-terminal state.

        repository is an absolute path inside any worktree of the repo, or its repo_id. Returns at once when
        already empty; otherwise waits for native file events and sends progress when the pending set
        changes. Result: empty, repository, repo_id, observed_at, pending (request ids) and timed_out.
        """
        checked_timeout(timeout_seconds)
        queue = await resolve_repository(repository, allowed)
        return await await_empty(queue, timeout_seconds, ctx)

    @server.tool()
    async def list_queues(repository: str | None = None) -> dict:
        """Read-only one-shot snapshot of installed queues with their pending requests (discovery only).

        Without repository, lists every registered repository this server may serve. Never use it as a
        polling loop: use watch_request or wait_for_queue_empty to wait.
        """
        if repository is not None:
            return {"observed_at": core.now(), "queues": [queue_view(await resolve_repository(repository, allowed))]}
        queues = []
        for entry in await asyncio.to_thread(core.registry_entries):
            try:
                queues.append(queue_view(await accepted_ctx(Path(entry["common_dir"]), allowed)))
            except (ValueError, KeyError, OSError) as exc:
                if allowed is None or Path(entry.get("common_dir", "")) in allowed:
                    queues.append({"repo_id": entry.get("repo_id"), "repository": entry.get("common_dir"),
                                   "problem": str(exc)})
        return {"observed_at": core.now(), "state_root": str(core.state_root()), "queues": queues}

    return server


def main():
    parser = argparse.ArgumentParser(prog="merge-queue-mcp", description=__doc__.strip().splitlines()[0])
    parser.add_argument("--allow-repo", action="append", metavar="PATH",
                        help="serve only this repository (an absolute path inside it); repeatable. "
                             "Default: every repository registered by `merge-queue install`.")
    args = parser.parse_args()
    try:
        server = make_server(args.allow_repo)
    except ValueError as exc:
        parser.error(str(exc))
    server.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
