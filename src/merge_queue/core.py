"""Deterministic local integration queue for a shared target branch.

Owners run `merge-queue submit`; a thin runner dispatches every request through
Block's `tq` (one FIFO per repository+target); `land` prepares an isolated rebase
candidate, validates it with the operator-owned policy and publishes with
`git merge --ff-only`. No LLM is involved. See docs/architecture.md.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

SECTION = "mergequeue"
TERMINAL = {"landed", "conflict", "validation_failed", "blocked", "infra_failed", "superseded"}
FAILED = TERMINAL - {"landed", "superseded"}
# queued -> dispatched -> running; requeue goes back to queued. Never running -> dispatched.
LIVE_TRANSITIONS = {("queued", "dispatched"), ("queued", "running"), ("dispatched", "running"),
                    ("dispatched", "queued"), ("running", "queued")}
HOOK_MARK = "# managed by merge-queue"
CHAINED = ".merge-queue-chained"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
AUTH_TTL = 900
PUBLISH_TIMEOUT = int(os.environ.get("MERGE_QUEUE_PUBLISH_TIMEOUT", "300"))
REFRESH_LIMIT = 3
LAND_MARGIN = 120  # seconds kept free below tq's landTimeout
INFRA_EXIT = {75, 124, 126, 127}
EXIT_OWNER, EXIT_OPERATOR, EXIT_INFRA, EXIT_PENDING, EXIT_NOT_INSTALLED, EXIT_SUPERSEDED = 10, 11, 20, 30, 3, 12
DROP_ENV = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
            "MERGE_QUEUE_MANUAL"}


class MqError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


class NotInstalled(MqError):
    def __init__(self, message="merge queue is not installed for this repository; run `merge-queue install`"):
        super().__init__(message, EXIT_NOT_INSTALLED)


class Fail(Exception):
    """Terminal outcome that is handed back to a person (owner or operator)."""

    def __init__(self, state, ftype, klass, message, files=(), logs=(), summary="", action=""):
        super().__init__(message)
        self.state, self.ftype, self.klass, self.message = state, ftype, klass, message
        self.files, self.logs, self.summary, self.action = list(files), list(logs), summary, action


class Infra(Exception):
    """Transient infrastructure problem; the request is requeued with bounded retries."""


class TargetMoved(Exception):
    pass


class CheckoutWait(Exception):
    """The integration checkout is not ready yet (pre-publication): wait without penalty, bounded."""

    def __init__(self, fail: "Fail"):
        super().__init__(fail.message)
        self.fail = fail


class Terminated(BaseException):
    pass


# ---------------------------------------------------------------- basic helpers

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_ts(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def base_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
    env.update(extra or {})
    return env


def clean_env(extra: dict | None = None) -> dict:
    return base_env({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C", **(extra or {})})


def git(*args, cwd, check=True, env=None, timeout=300):
    result = subprocess.run(["git", *args], cwd=str(cwd), env=env or clean_env(), text=True,
                            errors="surrogateescape", capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise MqError(f"git {' '.join(args[:3])} failed ({result.returncode}): {result.stderr.strip()[:500]}")
    return result


def write_text(path: Path, text: str) -> None:
    """Durable atomic replace: fsync the file, rename, then fsync the directory entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    with open(tmp, "w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path: Path, value) -> None:
    write_text(path, json.dumps(value, sort_keys=True, indent=1) + "\n")


def read_json(path: Path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None


class FileLock:
    def __init__(self, path: Path, blocking=True):
        self.path, self.blocking, self.fd = path, blocking, None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB))
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        if not self.acquire():
            raise MqError(f"lock busy: {self.path.name}")
        return self

    def __exit__(self, *exc):
        self.release()


def proc_identity(pid) -> dict | None:
    """Verifiable identity of a live process: pid + start time + argv. None when it is gone."""
    if not pid:
        return None
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat=,lstart=,args="], capture_output=True, text=True)
    parts = result.stdout.strip().split(None, 6)
    if result.returncode or len(parts) < 7 or parts[0].startswith("Z"):
        return None
    return {"pid": int(pid), "start": " ".join(parts[1:6]), "args": parts[6]}


def proc_state(identity: dict | None) -> str:
    """'gone', 'same' (verified to be the recorded process) or 'other' (pid reused / unverifiable)."""
    if not identity or not identity.get("start"):
        return "other" if identity and proc_identity(identity.get("pid")) else "gone"
    now_ident = proc_identity(identity["pid"])
    if now_ident is None:
        return "gone"
    return "same" if (now_ident["start"], now_ident["args"]) == (identity["start"], identity["args"]) else "other"


def valid_id(value: str, what="id") -> str:
    if not ID_RE.match(value or ""):
        raise MqError(f"invalid {what} {value!r}: use letters, digits, '.', '_' or '-' (max 64)", 2)
    return value


# ---------------------------------------------------------------- configuration

def find_common(cwd) -> Path:
    result = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd, check=False,
                 env=dict(os.environ, LC_ALL="C"))  # keeps GIT_DIR when called from a hook
    if result.returncode:
        raise MqError(f"not inside a Git repository: {cwd}", 2)
    return Path(result.stdout.strip()).resolve()


def repo_id_for(common: Path) -> str:
    return hashlib.sha256(str(common).encode()).hexdigest()[:12]


def state_root() -> Path:
    """Root of all queue state: $MERGE_QUEUE_STATE_HOME, else ${XDG_STATE_HOME:-~/.local/state}/merge-queue."""
    explicit = os.environ.get("MERGE_QUEUE_STATE_HOME")
    if explicit:
        root = Path(explicit).expanduser()
        if not root.is_absolute():
            raise MqError(f"MERGE_QUEUE_STATE_HOME must be an absolute path, got {explicit!r}", 2)
        return root.resolve()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".local/state"  # XDG: relative values are invalid
    return (base / "merge-queue").resolve()


def default_state_dir(common: Path) -> Path:
    return state_root() / repo_id_for(common)


# ---------------------------------------------------------------- registry of installed repositories

def registry_path(repo_id: str) -> Path:
    return state_root() / "repos" / f"{valid_id(repo_id, 'repo id')}.json"


def read_registry(repo_id: str) -> dict | None:
    try:
        return read_json(registry_path(repo_id))
    except ValueError:
        return None


def registry_entries() -> list[dict]:
    directory = state_root() / "repos"
    entries = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            data = read_json(path)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("repo_id") == path.stem:
            entries.append(data)
    return entries


def registry_matches(entry: dict | None, common: Path, cfg: dict) -> bool:
    """A registry entry accepts a repository only when it names exactly this Git directory, state directory,
    checkout and target. Repository-local configuration alone is never enough (it can be forged by any clone)."""
    return bool(entry) and entry.get("repo_id") == repo_id_for(common) and entry.get("common_dir") == str(common) \
        and entry.get("state_dir") == cfg.get("stateDir") and entry.get("checkout") == cfg.get("checkout") \
        and entry.get("target") == cfg.get("target", "refs/heads/main")


class Ctx:
    def __init__(self, common: Path, cfg: dict):
        self.common = common
        self.repo_id = repo_id_for(common)
        self.state = Path(cfg["stateDir"])
        self.checkout = Path(cfg["checkout"])
        self.target = cfg.get("target", "refs/heads/main")
        self.tq = cfg["tq"]
        # argv prefix of this tool's installed entry (`<interpreter> -P -m merge_queue.core`), never a checkout path
        self.entry = shlex.split(cfg["entry"])
        self.entry_text = " ".join(self.entry)  # how ps and tq render it (argv joined by spaces)
        self.policy = Path(cfg["policy"])
        self.runtime_allow = cfg.get("runtimeAllow", [])
        self.max_attempts = int(cfg.get("maxAttempts", 4))
        self.land_timeout = int(cfg.get("landTimeout", 6600))
        self.checkout_wait_limit = int(cfg.get("checkoutWaitLimit", 14400))
        self.queue = f"merge-{self.repo_id}-{self.target.removeprefix('refs/heads/').replace('/', '_')}"

    def p(self, *parts) -> Path:
        return self.state.joinpath(*parts)

    @property
    def tq_dir(self) -> Path:
        return self.p("tq")

    def describe(self) -> dict:
        return {"repository": str(self.common), "repo_id": self.repo_id, "state_dir": str(self.state),
                "checkout": str(self.checkout), "target": self.target, "queue": self.queue,
                "tq": self.tq, "tq_data_dir": str(self.tq_dir), "entry": shlex.join(self.entry),
                "policy": str(self.policy), "runtime_allow": self.runtime_allow}


def read_config(common: Path) -> dict:
    result = subprocess.run(["git", "config", "--file", str(common / "config"), "-z", "--get-regexp",
                             f"^{SECTION}\\."], capture_output=True, text=True, env=clean_env())
    cfg: dict = {}
    for item in filter(None, result.stdout.split("\0")):
        key, _, value = item.partition("\n")
        key = key.split(".", 1)[1]
        # git lower-cases the variable name; map back to the camelCase keys used here.
        key = {"statedir": "stateDir", "runtimeallow": "runtimeAllow", "maxattempts": "maxAttempts",
               "landtimeout": "landTimeout", "checkoutwaitlimit": "checkoutWaitLimit"}.get(key, key)
        if key == "runtimeAllow":
            cfg.setdefault(key, []).append(value)
        else:
            cfg[key] = value
    return cfg


def load_ctx(cwd=None) -> Ctx:
    common = find_common(cwd or os.getcwd())
    cfg = read_config(common)
    if not all(k in cfg for k in ("stateDir", "checkout", "tq", "entry", "policy")):
        raise NotInstalled()
    return Ctx(common, cfg)


def set_config(common: Path, key: str, values: list[str]) -> None:
    subprocess.run(["git", "config", "--file", str(common / "config"), "--unset-all", f"{SECTION}.{key}"],
                   capture_output=True, env=clean_env())
    for value in values:
        subprocess.run(["git", "config", "--file", str(common / "config"), "--add", f"{SECTION}.{key}", value],
                       check=True, capture_output=True, env=clean_env())


# ---------------------------------------------------------------- diagnostics (optional)

def diag_level(ctx: Ctx) -> str:
    try:
        return ctx.p("diagnostics.level").read_text().strip() or "basic"
    except FileNotFoundError:
        return "basic"


def event(ctx: Ctx, name: str, request_id: str | None = None, level="basic", **fields) -> None:
    """Append a structured diagnostic line. Never records environment or secrets."""
    current = diag_level(ctx)
    if current == "off" or (level == "debug" and current != "debug"):
        return
    path = ctx.p("diagnostics", "events.jsonl")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1_000_000:
            os.replace(path, path.with_suffix(".jsonl.1"))
        with open(path, "a") as handle:
            handle.write(json.dumps({"ts": now(), "event": name, "request_id": request_id, **fields}) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- request store

def req_path(ctx: Ctx, rid: str) -> Path:
    return ctx.p("requests", f"{valid_id(rid, 'request id')}.json")


def load_req(ctx: Ctx, rid: str):
    return read_json(req_path(ctx, rid))


def mutate(ctx: Ctx, rid: str, fn, allow_terminal_change=False):
    with FileLock(ctx.p("locks", f"{rid}.rw")):
        req = read_json(req_path(ctx, rid))
        if req is None:
            raise MqError(f"unknown request {rid}")
        before = req["state"]
        fn(req)
        if before in TERMINAL and req["state"] != before and not allow_terminal_change:
            raise MqError(f"request {rid} is terminal ({before}); refusing state change to {req['state']}")
        if req["state"] != before and before not in TERMINAL and \
                (before, req["state"]) not in LIVE_TRANSITIONS and req["state"] not in TERMINAL:
            raise MqError(f"request {rid}: refusing {before} -> {req['state']}")
        write_json(req_path(ctx, rid), req)
        return req


def all_requests(ctx: Ctx) -> list[dict]:
    items = []
    directory = ctx.p("requests")
    if directory.is_dir():
        for path in directory.glob("*.json"):
            data = read_json(path)
            if data:
                items.append(data)
    return sorted(items, key=lambda r: r["seq"])


def tombstones(ctx: Ctx) -> list[dict]:
    directory = ctx.p("tombstones")
    return [d for d in (read_json(p) for p in directory.glob("*.json")) if d] if directory.is_dir() else []


def owner_dir(ctx: Ctx, owner: str) -> Path:
    return ctx.p("inbox", re.sub(r"[^A-Za-z0-9._-]", "_", owner)[:64] or "_")


def rev_parse(ctx: Ctx, ref: str, cwd=None) -> str:
    return git("rev-parse", "--verify", "-q", ref + "^{commit}", cwd=cwd or ctx.checkout).stdout.strip()


def is_ancestor(ctx: Ctx, a: str, b: str) -> bool:
    return git("merge-base", "--is-ancestor", a, b, cwd=ctx.checkout, check=False).returncode == 0


def ref_delete(ctx: Ctx, ref: str) -> None:
    git("update-ref", "-d", ref, cwd=ctx.checkout, check=False)


def source_ref(rid):
    return f"refs/merge-queue/source/{rid}"


def candidate_ref(rid):
    return f"refs/merge-queue/candidate/{rid}"


# ---------------------------------------------------------------- submit / retry / results

def clean_owner(owner: str) -> str:
    return valid_id(re.sub(r"[^A-Za-z0-9._-]", "_", owner)[:64].lstrip("._-") or "owner", "owner")


def worktree_owner(cwd) -> str:
    top = git("rev-parse", "--show-toplevel", cwd=cwd, check=False).stdout.strip()
    return clean_owner(Path(top or cwd).name)


def current_owner(args, cwd) -> str:
    """Stable id of the submitting agent: --owner, MERGE_QUEUE_OWNER, CODEX_THREAD_ID, else the worktree name."""
    owner = getattr(args, "owner", None) or os.environ.get("MERGE_QUEUE_OWNER") or os.environ.get("CODEX_THREAD_ID")
    return clean_owner(owner) if owner else worktree_owner(cwd)


def owner_candidates(args, cwd, extra=()) -> list[str]:
    """Every id this agent may have been submitted under (explicit/env/session ids, then the worktree fallback)."""
    explicit = getattr(args, "owner", None)
    names = [explicit] if explicit else [os.environ.get("MERGE_QUEUE_OWNER"), os.environ.get("CODEX_THREAD_ID"),
                                         *extra]
    out = [clean_owner(n) for n in names if n]
    if not explicit:
        out.append(worktree_owner(cwd))
    return list(dict.fromkeys(out))


def create_request(ctx: Ctx, *, rid, owner, sha, branch, started_at=None, retry_of=None) -> tuple[dict, bool]:
    """Persist a request unless an equivalent one exists. Returns (request, created)."""
    with FileLock(ctx.p("locks", "submit.lock")):
        existing = load_req(ctx, rid)
        if existing:
            if existing["source_commit_sha"] != sha:
                raise MqError(f"request id {rid} already used for {existing['source_commit_sha'][:12]}; "
                              "ids are bound to one exact commit", 4)
            return existing, False
        for tomb in tombstones(ctx):
            if tomb["request_id"] == rid:
                if tomb["source_commit_sha"] != sha:
                    raise MqError(f"request id {rid} already used for another commit", 4)
                return tomb, False
        for other in all_requests(ctx):
            if other["source_commit_sha"] != sha or other["state"] == "superseded":
                continue
            if other["state"] not in FAILED or other["result"].get("failure_class") == "owner":
                if other["state"] in FAILED:
                    raise MqError(f"commit {sha[:12]} already failed in {other['request_id']} "
                                  f"({other['state']}); change it and run `merge-queue retry {other['request_id']}`", 4)
                return other, False
        for tomb in tombstones(ctx):
            if tomb["source_commit_sha"] == sha and tomb["state"] == "landed":
                return tomb, False
        seq = next_seq(ctx)
        created = started_at
        if not created:
            log = git("log", "--format=%aI", "--reverse", f"{ctx.target}..{sha}", cwd=ctx.checkout, check=False)
            created = (log.stdout.split() or [None])[0]
        submitted = now()
        req = {"request_id": rid, "owner_id": owner, "repository": str(ctx.common), "target_branch": ctx.target,
               "source_branch": branch, "source_commit_sha": sha, "target_sha": None, "state": "queued",
               "phase": None, "attempt_id": None, "attempts": 0, "dispatches": 0, "seq": seq,
               "created_at": created or submitted, "submitted_at": submitted, "queue": ctx.queue,
               "not_before": None, "tq": None, "candidate": None, "publication": None, "validation": [],
               "timings": {}, "result": {}, "retry_of": retry_of}
        git("update-ref", source_ref(rid), sha, cwd=ctx.checkout)
        write_json(req_path(ctx, rid), req)
    event(ctx, "submitted", rid, owner=owner, source=sha, seq=seq)
    return req, True


def next_seq(ctx: Ctx) -> int:
    """Reserve the next FIFO number durably *before* the request exists; recoverable from any survivor."""
    try:
        recorded = int(ctx.p("seq").read_text())
    except (FileNotFoundError, ValueError):
        recorded = 0
    known = [r["seq"] for r in all_requests(ctx)] + [t.get("seq", 0) for t in tombstones(ctx)]
    seq = max([recorded, *known]) + 1
    write_text(ctx.p("seq"), str(seq))
    return seq


def resolve_source(args, ctx: Ctx, cwd) -> tuple[str, str | None]:
    sha = git("rev-parse", "--verify", "-q", (args.commit or "HEAD") + "^{commit}", cwd=cwd, check=False).stdout.strip()
    if not sha:
        raise MqError(f"cannot resolve commit {args.commit or 'HEAD'}", 2)
    branch = args.branch
    if not branch and not args.commit:
        branch = git("symbolic-ref", "-q", "--short", "HEAD", cwd=cwd, check=False).stdout.strip() or None
    if branch:
        tip = git("rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}", cwd=cwd, check=False).stdout.strip()
        if tip != sha:
            raise MqError(f"branch {branch} is at {tip[:12] or 'nothing'}, not the submitted {sha[:12]}", 2)
    if not args.commit and git("status", "--porcelain", "--untracked-files=no", cwd=cwd).stdout.strip():
        raise MqError("tracked changes are not committed; commit them first (the exact commit is what gets queued)", 2)
    return sha, branch


def ensure_runner(ctx: Ctx) -> None:
    if os.environ.get("MERGE_QUEUE_NO_AUTORUNNER"):
        return
    probe = FileLock(ctx.p("locks", "runner.lock"), blocking=False)
    if not probe.acquire():
        return
    probe.release()
    log = ctx.p("runner", "runner.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as out:
        subprocess.Popen([*ctx.entry, "runner", "--idle-exit", "120"], cwd=str(ctx.checkout),
                         stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
                         env=clean_env())


def outcome_code(req: dict) -> int:
    state = req["state"]
    if state == "landed":
        return 0
    if state == "superseded":
        return EXIT_SUPERSEDED  # replaced by a retry: not a success of this request
    if state not in TERMINAL:
        return EXIT_PENDING
    if state == "infra_failed":
        return EXIT_INFRA
    return EXIT_OPERATOR if req.get("result", {}).get("failure_class") == "operator" else EXIT_OWNER


def wait_terminal(ctx: Ctx, rid: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        req = load_req(ctx, rid) or next((t for t in tombstones(ctx) if t["request_id"] == rid), None)
        if req is None:
            raise MqError(f"unknown request {rid}")
        if req["state"] in TERMINAL or time.monotonic() >= deadline:
            return req
        time.sleep(0.3)


def brief(req: dict) -> dict:
    keys = ("request_id", "owner_id", "state", "phase", "source_commit_sha", "target_sha", "source_branch",
            "target_branch", "attempt_id", "attempts", "created_at", "submitted_at", "seq")
    out = {k: req.get(k) for k in keys}
    if req.get("result"):
        out["result"] = req["result"]
    if req.get("superseded_by"):
        out["superseded_by"] = req["superseded_by"]
    return out


def show(req: dict, args, extra=None) -> None:
    data = {**brief(req), **(extra or {})}
    if getattr(args, "json", False):
        print(json.dumps(data, sort_keys=True))
        return
    line = f"{data['request_id']} {data['state']} source={str(data['source_commit_sha'])[:12]}"
    res = data.get("result") or {}
    if res.get("failure_type"):
        line += f" {res['failure_type']}: {res.get('message', '')}"
    if res.get("landed_sha"):
        line += f" landed={res['landed_sha'][:12]}"
    if extra:
        line += " " + " ".join(f"{k}={v}" for k, v in extra.items())
    print(line)


def cmd_submit(args):
    cwd = os.getcwd()
    ctx = load_ctx(cwd)
    sha, branch = resolve_source(args, ctx, cwd)
    rid = valid_id(args.request_id, "request id") if args.request_id else \
        f"mq-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    req, created = create_request(ctx, rid=rid, owner=current_owner(args, cwd), sha=sha, branch=branch,
                                  started_at=args.started_at)
    ensure_runner(ctx)
    if args.wait:
        req = wait_terminal(ctx, req["request_id"], args.timeout)
    show(req, args, {"duplicate": True} if not created else None)
    return outcome_code(req) if args.wait else 0


def cmd_retry(args):
    cwd = os.getcwd()
    ctx = load_ctx(cwd)
    old = load_req(ctx, valid_id(args.request_id, "request id"))
    if old is None:
        raise MqError(f"unknown request {args.request_id} (already acknowledged?)")
    if old["state"] not in FAILED and old["state"] != "superseded":
        raise MqError(f"request {old['request_id']} is {old['state']}; only failed requests are retried", 4)
    sha, branch = resolve_source(args, ctx, cwd)
    for other in all_requests(ctx):
        if other.get("retry_of") == old["request_id"] and other["source_commit_sha"] == sha:
            show(other, args, {"duplicate": True})
            return 0
    if old["result"].get("failure_class") == "owner" and sha == old["source_commit_sha"]:
        raise MqError("no change since the failed submission: repair and commit first; identical commits are "
                      "not retried", 4)
    rid = valid_id(args.new_request_id, "request id") if args.new_request_id else \
        f"mq-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    bound = [x for x in (*all_requests(ctx), *tombstones(ctx)) if x["request_id"] == rid]
    if any(x["source_commit_sha"] != sha for x in bound):  # refuse before the old record is superseded
        raise MqError(f"request id {rid} already used for another commit; ids are bound to one exact commit", 4)
    # The old record must stop blocking its own commit before the identical-commit check runs.
    if old["state"] != "superseded":
        mutate(ctx, old["request_id"], lambda r: r.update(state="superseded", phase=None, superseded_by=rid)
               if r["state"] in FAILED else None, allow_terminal_change=True)
    req, created = create_request(ctx, rid=rid, owner=old["owner_id"], sha=sha, branch=branch,
                                  started_at=old["created_at"], retry_of=old["request_id"])
    with contextlib.suppress(FileNotFoundError):
        (owner_dir(ctx, old["owner_id"]) / f"{old['request_id']}.json").unlink()
    ref_delete(ctx, source_ref(old["request_id"]))
    ensure_runner(ctx)
    if args.wait:
        req = wait_terminal(ctx, req["request_id"], args.timeout)
    show(req, args)
    return outcome_code(req) if args.wait else 0


def latest_for_owner(ctx: Ctx, owners: list[str]):
    mine = [r for r in all_requests(ctx) if r["owner_id"] in owners]
    return mine[-1] if mine else None


def inbox_entries(ctx: Ctx, owners: list[str]) -> list[dict]:
    entries = []
    for owner in owners:
        directory = owner_dir(ctx, owner)
        entries += [e for e in (read_json(p) for p in sorted(directory.glob("*.json"))) if e] \
            if directory.is_dir() else []
    return entries


def cmd_result(args):
    cwd = os.getcwd()
    ctx = load_ctx(cwd)
    rid = args.request_id
    if not rid:
        latest = latest_for_owner(ctx, owner_candidates(args, cwd))
        if not latest:
            raise MqError("no request found for this owner")
        rid = latest["request_id"]
    req = wait_terminal(ctx, rid, args.timeout if args.wait else 0)
    show(req, args)
    if args.full and not args.json:
        print(json.dumps(req, sort_keys=True, indent=1))
    return outcome_code(req)


def tq_summary(ctx: Ctx) -> dict | None:
    result = subprocess.run([ctx.tq, "--data-dir", str(ctx.tq_dir), "list", "--json"], capture_output=True,
                            text=True, env=clean_env())
    try:
        tasks = [t for t in json.loads(result.stdout)["tasks"] if t["queue_name"] == ctx.queue]
    except (ValueError, KeyError):
        return None
    return {"running": sum(t["status"] == "running" for t in tasks),
            "waiting": sum(t["status"] == "waiting" for t in tasks)}


def cmd_status(args):
    ctx = load_ctx()
    reqs = [r for r in all_requests(ctx) if r["state"] not in ("superseded",)]
    live = [r for r in reqs if r["state"] not in TERMINAL]
    rows = []
    for r in reqs:
        row = brief(r)
        if r["state"] not in TERMINAL:
            row["position"] = live.index(r) + 1
        rows.append(row)
    blocked = read_json(ctx.p("notices", "stale-tq.json"))
    waiting = read_json(ctx.p("notices", "checkout-wait.json"))
    summary = {"config": ctx.describe(), "tq": tq_summary(ctx), "requests": rows, "dispatch_blocked": blocked,
               "checkout_wait": waiting}
    if args.json:
        print(json.dumps(summary, sort_keys=True))
        return 0
    print(f"queue {ctx.queue} state={ctx.state} tq={summary['tq']}")
    if blocked:
        print(f"  DISPATCH BLOCKED: {'; '.join(blocked['problems'])}\n  {blocked['required_action']}")
    if waiting:
        print(f"  WAITING FOR CHECKOUT: {waiting['reason']} ({', '.join(waiting['requests'])})\n  {waiting['required_action']}")
    for row in rows:
        print(f"  #{row.get('position', '-')} {row['request_id']} owner={row['owner_id']} {row['state']}"
              f"{'/' + row['phase'] if row['phase'] else ''} source={row['source_commit_sha'][:12]}")
    if not rows:
        print("  (empty)")
    return 0


def cmd_inbox(args):
    cwd = os.getcwd()
    ctx = load_ctx(cwd)
    owners = owner_candidates(args, cwd)
    entries = inbox_entries(ctx, owners)
    if args.quiet:
        if entries:
            print(f"merge-queue: {len(entries)} pending integration failure(s) for {'/'.join(owners)}; "
                  "run `merge-queue inbox`")
        return EXIT_OWNER if entries else 0
    if args.json:
        print(json.dumps(entries, sort_keys=True))
    else:
        for e in entries:
            print(f"{e['request_id']} {e['failure_type']} source={e['source_commit_sha'][:12]} "
                  f"target={str(e.get('target_commit_sha'))[:12]}\n  {e['required_action']}")
            for key in ("conflicting_files", "relevant_log_paths"):
                if e.get(key):
                    print(f"  {key}: {', '.join(e[key])}")
            if e.get("summary"):
                print("  " + e["summary"].replace("\n", "\n  "))
        if not entries:
            print("inbox empty")
    return 0


def cmd_ack(args):
    ctx = load_ctx()
    ids = [args.request_id] if args.request_id else []
    if args.all:
        owners = owner_candidates(args, os.getcwd())
        ids = [r["request_id"] for r in all_requests(ctx) if r["owner_id"] in owners and r["state"] in TERMINAL]
    for rid in ids:
        acknowledge(ctx, rid)
    print(f"acknowledged {len(ids)} request(s)")
    return 0


def acknowledge(ctx: Ctx, rid: str) -> None:
    req = load_req(ctx, rid)
    if req is None:
        return
    if req["state"] not in TERMINAL:
        raise MqError(f"request {rid} is still {req['state']}; only finished requests are acknowledged", 4)
    tomb = {k: req.get(k) for k in ("request_id", "owner_id", "source_commit_sha", "target_branch", "state", "seq")}
    tomb.update(landed_sha=req["result"].get("landed_sha"), failure_type=req["result"].get("failure_type"),
                acknowledged_at=now())
    write_json(ctx.p("tombstones", f"{rid}.json"), tomb)
    with contextlib.suppress(FileNotFoundError):
        (owner_dir(ctx, req["owner_id"]) / f"{rid}.json").unlink()
    req_path(ctx, rid).unlink(missing_ok=True)
    shutil.rmtree(ctx.p("evidence", rid), ignore_errors=True)
    ref_delete(ctx, source_ref(rid))
    ref_delete(ctx, candidate_ref(rid))
    prune_tombstones(ctx)


def prune_tombstones(ctx: Ctx, keep=500) -> None:
    items = sorted(ctx.p("tombstones").glob("*.json"), key=lambda p: p.stat().st_mtime)
    for path in items[:-keep]:
        path.unlink(missing_ok=True)


def protected_logs(ctx: Ctx) -> set[str]:
    """Logs that are failure evidence or belong to unfinished work: never removed as 'diagnostics'. Covers
    active requests, every unacknowledged failure (its tq log and result.log_paths) and pending inbox entries."""
    keep = set()
    for req in all_requests(ctx):
        if req["state"] not in ("landed", "superseded"):
            keep.add((req.get("tq") or {}).get("log"))
            keep.update(req.get("result", {}).get("log_paths", []))
    inbox = ctx.p("inbox")
    for entry_path in inbox.glob("*/*.json") if inbox.is_dir() else []:
        keep.update((read_json(entry_path) or {}).get("relevant_log_paths", []))
    return {str(p) for p in keep if p}


def gc(ctx: Ctx) -> None:
    """Landed receipts older than a day become tombstones; failures wait for acknowledgement. Old tq output
    that is not protected evidence is optional history and is pruned so it cannot grow without bound."""
    for req in all_requests(ctx):
        finished = req["result"].get("finished_at")
        if req["state"] in ("landed", "superseded") and finished and time.time() - parse_ts(finished) > 86400:
            acknowledge(ctx, req["request_id"])
    keep = protected_logs(ctx)
    for path in ctx.p("runner").glob("tq-*.log") if ctx.p("runner").is_dir() else []:
        if str(path) not in keep and time.time() - path.stat().st_mtime > 86400:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------- diagnostics commands

def cmd_logs(args):
    ctx = load_ctx()
    if args.level:
        ctx.state.mkdir(parents=True, exist_ok=True)
        ctx.p("diagnostics.level").write_text(args.level + "\n")
        print(f"diagnostics level: {args.level}")
        return 0
    path = ctx.p("diagnostics", "events.jsonl")
    lines = path.read_text().splitlines()[-args.n:] if path.exists() else []
    print(f"# level={diag_level(ctx)} file={path}")
    for line in lines:
        print(line)
    return 0


def cmd_logs_clear(args):
    ctx = load_ctx()
    removed = []
    for path in (ctx.p("diagnostics", "events.jsonl"), ctx.p("diagnostics", "events.jsonl.1"),
                 ctx.tq_dir / "agent-task-queue-logs.json", ctx.tq_dir / "agent-task-queue-logs.json.1"):
        if path.exists():
            path.unlink()
            removed.append(str(path))
    keep = protected_logs(ctx)
    for path in ctx.p("runner").glob("tq-*.log") if ctx.p("runner").is_dir() else []:
        if str(path) not in keep:
            path.unlink()
            removed.append(str(path))
    print(f"cleared {len(removed)} diagnostic file(s); queue state, requests and inbox untouched")
    return 0


# ---------------------------------------------------------------- policy

def load_policy(path: Path) -> dict:
    try:
        policy = json.loads(path.read_text())
        assert policy["version"] == 1 and isinstance(policy["rules"], list) and isinstance(policy["checks"], dict)
        for rule in policy["rules"]:
            assert isinstance(rule["match"], list) and all(c in policy["checks"] for c in rule["checks"])
        for spec in policy["checks"].values():
            assert isinstance(spec["argv"], list) and spec["argv"]
            assert spec.get("plan") is None or (isinstance(spec["plan"], list) and spec["plan"])
            assert all(isinstance(v, list) and v for v in spec.get("bootstrap", {}).values())
    except (OSError, ValueError, KeyError, AssertionError, TypeError) as exc:
        raise Fail("blocked", "policy_invalid", "operator", f"validation policy {path} is unreadable or invalid: {exc!r}",
                   action="Operator: fix the validation policy, then `merge-queue retry <id>`.")
    return policy


def plan_checks(policy: dict, paths: list[str]) -> tuple[list[str], list[str]]:
    selected, unknown = set(), []
    for path in paths:
        matched = [r for r in policy["rules"] if any(fnmatch.fnmatchcase(path, g) for g in r["match"])]
        if not matched:
            unknown.append(path)
        for rule in matched:
            selected.update(rule["checks"])
    return [name for name in policy["checks"] if name in selected], unknown


def cmd_policy_check(args):
    """Validate a policy file and that every tracked path is routed by some rule (unmatched paths fail closed at
    landing time). Works without an installation; consumers run it as their policy-coverage test."""
    repo = Path(args.repo).resolve() if args.repo else Path.cwd()
    top = git("rev-parse", "--show-toplevel", cwd=repo, check=False) if repo.is_dir() else None
    if top is None or top.returncode:
        raise MqError(f"not inside a Git repository: {repo}", 2)
    root = Path(top.stdout.strip())
    path = Path(args.policy).resolve() if args.policy else root / ".merge-queue" / "policy.json"
    result = {"ok": False, "repo": str(root), "policy": str(path), "files": 0, "uncovered": [], "error": None}
    try:
        policy = load_policy(path)
    except Fail as exc:
        result["error"] = exc.message
    else:
        files = sorted(filter(None, git("ls-files", "-z", cwd=root).stdout.split("\0")))
        result.update(files=len(files), uncovered=plan_checks(policy, files)[1])
        result["ok"] = not result["uncovered"]
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


def changed_paths(ctx: Ctx, base: str, head: str) -> list[str]:
    out = git("diff", "--name-only", "--no-renames", "-z", base, head, "--", cwd=ctx.checkout).stdout
    return sorted(filter(None, out.split("\0")))


# ---------------------------------------------------------------- landing

class Guard:
    critical = 0
    pending = False
    pgids: set = set()


def _on_term(signum, frame):
    if Guard.critical:
        Guard.pending = True
        return
    raise Terminated()


@contextlib.contextmanager
def critical():
    Guard.critical += 1
    try:
        yield
    finally:
        Guard.critical -= 1
        if not Guard.critical and Guard.pending:
            Guard.pending = False
            raise Terminated()


def fault(stage: str, attempts: int) -> None:
    """Test seam: MERGE_QUEUE_FAULT=stage:exit[:attempt] or stage:wait=PATH[:attempt]."""
    spec = os.environ.get("MERGE_QUEUE_FAULT")
    if not spec:
        return
    parts = spec.split(":")
    if parts[0] != stage or (len(parts) > 2 and int(parts[2]) != attempts):
        return
    if parts[1] == "exit":
        os._exit(137)
    if parts[1].startswith("wait="):
        path = Path(parts[1][5:])
        Path(str(path) + ".reached").write_text(str(os.getpid()))
        deadline = time.monotonic() + 120
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)


def registered_worktrees(ctx: Ctx) -> list[str]:
    out = git("worktree", "list", "--porcelain", cwd=ctx.checkout, check=False).stdout
    return [line[len("worktree "):] for line in out.splitlines() if line.startswith("worktree ")]


def remove_worktree(ctx: Ctx, path: Path | None) -> bool:
    """Remove one exact queue-owned candidate workspace. Never prunes Git's wider worktree inventory;
    a workspace that cannot be removed is preserved and reported by `doctor`."""
    if path is None or path.parent != ctx.p("work") or not path.exists():
        return True
    if git("worktree", "remove", "--force", str(path), cwd=ctx.checkout, check=False).returncode:
        if str(path) in registered_worktrees(ctx):
            event(ctx, "workspace_preserved", None, path=str(path))
            return False
        shutil.rmtree(path, ignore_errors=True)  # an unregistered leftover inside our own state directory
    return True


def kill_group(pgid: int, grace=5.0, proc: subprocess.Popen | None = None) -> None:
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(pgid, sig)
        except OSError:
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if proc is not None:
                proc.poll()  # reap the leader so an empty group is detectable
            try:
                os.killpg(pgid, 0)
            except OSError:
                return
            time.sleep(0.05)


def kill_verified_group(identity: dict | None, grace=1.0) -> bool:
    """Signal a recorded validation group only when the live process is verifiably the recorded one
    (same pid, start time and argv) and still leads its own group. A reused or unknown pid is left alone."""
    if not identity or proc_state(identity) != "same":
        return False
    try:
        if os.getpgid(identity["pid"]) != identity["pid"]:
            return False
    except OSError:
        return False
    kill_group(identity["pid"], grace)
    return True


def sweep_stale(ctx: Ctx) -> int:
    """Called with the integration lock held: no queue publication can be running (the lock is inherited by
    the Git child), so leftovers of dead attempts are ours to clear, but only after identity checks."""
    cleaned = 0
    attempts = ctx.p("attempts")
    for path in attempts.glob("*.json") if attempts.is_dir() else []:
        data = read_json(path) or {}
        if kill_verified_group(data.get("group")):
            cleaned += 1
        elif data.get("group") and proc_state(data["group"]) == "other":
            event(ctx, "stale_pid_ignored", None, pid=data["group"].get("pid"))
        path.unlink(missing_ok=True)
    work = ctx.p("work")
    for path in work.iterdir() if work.is_dir() else []:
        cleaned += remove_worktree(ctx, path)
    refs = git("for-each-ref", "--format=%(refname)", "refs/merge-queue/candidate", cwd=ctx.checkout, check=False)
    for ref in refs.stdout.split():
        rid = ref.rsplit("/", 1)[1]
        req = load_req(ctx, rid)
        if req is None or req["state"] in TERMINAL:
            ref_delete(ctx, ref)
            cleaned += 1
    for path in ctx.p("auth").glob("*.json") if ctx.p("auth").is_dir() else []:
        path.unlink(missing_ok=True)  # authorisations are only valid while the lock-holding publisher runs
        cleaned += 1
    return cleaned


def porcelain(cwd) -> list[tuple[str, str]]:
    out = git("status", "--porcelain=v1", "-z", "--untracked-files=normal", cwd=cwd).stdout.split("\0")
    entries, i = [], 0
    while i < len(out):
        record = out[i]
        i += 1
        if not record:
            continue
        xy, path = record[:2], record[3:]
        if xy[0] in "RC":
            i += 1
        entries.append((xy, path))
    return entries


def overlaps(entry: str, paths: list[str]) -> bool:
    """True when the on-disk entry and an incoming path are equal or one contains the other."""
    e = entry.rstrip("/")
    return any(p == e or p.startswith(e + "/") or e.startswith(p + "/") for p in paths)


def check_checkout(ctx: Ctx, incoming: list[str]) -> None:
    """Fresh safety check of the exact integration checkout before every publication."""
    head = git("symbolic-ref", "-q", "HEAD", cwd=ctx.checkout, check=False).stdout.strip()
    if head != ctx.target:
        raise Fail("blocked", "checkout_branch", "operator",
                   f"integration checkout {ctx.checkout} is on {head or 'a detached HEAD'}, not {ctx.target}",
                   action="Operator: check out the target branch in the integration checkout, then retry.")
    allowed = [a if a.endswith("/") else a + "/" for a in ctx.runtime_allow]
    tracked, unknown, clash = [], [], []
    for xy, path in porcelain(ctx.checkout):
        if xy != "??":
            tracked.append(path)
        elif not any((path.rstrip("/") + "/").startswith(a) for a in allowed):
            unknown.append(path)
        elif overlaps(path, incoming):
            clash.append(path)
    ignored = git("ls-files", "-o", "-i", "--exclude-standard", "--directory", "-z", cwd=ctx.checkout).stdout
    clash += [p for p in ignored.split("\0") if p and overlaps(p, incoming)]
    for kind, items, text in (("checkout_dirty", tracked, "tracked changes"),
                              ("checkout_untracked", unknown, "unrecognised untracked paths"),
                              ("checkout_overlap", clash, "runtime/ignored paths that the incoming change would overwrite")):
        if items:
            raise Fail("blocked", kind, "operator", f"integration checkout has {text}: {', '.join(items[:10])}",
                       files=items[:50], action="Operator: resolve the listed paths in the integration checkout "
                                                "(never reset it blindly), then `merge-queue retry <id>`.")


def step_env(extra: dict) -> dict:
    return base_env({k: str(v) for k, v in extra.items()})


def run_group(argv, cwd, env, log: Path, timeout: int, attempt_file: Path) -> int | None:
    """Run in its own process group so SIGTERM/timeout can reap every descendant."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "wb") as out:
        try:
            proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            out.write(f"cannot start {argv[0]}: {exc}\n".encode())
            return 127
        data = read_json(attempt_file) or {}
        write_json(attempt_file, {**data, "group": proc_identity(proc.pid) or {"pid": proc.pid}})
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    return proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    if time.monotonic() > deadline:
                        out.write(f"\n[merge-queue] timeout after {timeout}s\n".encode())
                        kill_group(proc.pid, 5.0, proc)
                        proc.wait()
                        return None
        except BaseException:
            kill_group(proc.pid, 5.0, proc)
            proc.wait()
            raise
        finally:
            kill_group(proc.pid, 0.2, proc)
            write_json(attempt_file, {**data, "group": None})


def tail(path: Path, lines=20, limit=2000) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])[-limit:]
    except OSError:
        return ""


def prepare(ctx: Ctx, req: dict, attempt: str, target: str, state: dict) -> tuple[Path, str, str]:
    src, rid = req["source_commit_sha"], req["request_id"]
    work = ctx.p("work", f"{rid}-{attempt}")
    work.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "--detach", str(work), src, cwd=ctx.checkout)
    state["work"] = work
    base = git("merge-base", target, src, cwd=ctx.checkout).stdout.strip()
    if base == target:
        return work, src, "fast-forward"
    if git("rev-list", "--merges", f"{base}..{src}", cwd=ctx.checkout).stdout.strip():
        raise Fail("blocked", "merge_commits", "owner",
                   "the submitted history contains merge commits and is not based on the current target",
                   action=f"Rebase your branch onto {target[:12]} (or merge the target into it) so it needs no "
                          "automatic rebase, run relevant checks, commit, then `merge-queue retry <id>`.")
    env = clean_env({"GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"})
    if not git("config", "user.email", cwd=ctx.checkout, check=False).stdout.strip():
        env.update(GIT_COMMITTER_NAME="merge-queue", GIT_COMMITTER_EMAIL="merge-queue@localhost",
                   GIT_AUTHOR_NAME="merge-queue", GIT_AUTHOR_EMAIL="merge-queue@localhost")
    result = git("rebase", target, cwd=work, check=False, env=env)
    if result.returncode:
        files = sorted(filter(None, git("diff", "--name-only", "--diff-filter=U", cwd=work, check=False).stdout.split("\n")))
        stopped = git("rev-parse", "-q", "--verify", "REBASE_HEAD", cwd=work, check=False).stdout.strip()
        git("rebase", "--abort", cwd=work, check=False)
        if not files:
            raise Infra(f"git rebase failed without conflicts: {(result.stderr or result.stdout).strip()[:300]}")
        evidence = ctx.p("evidence", rid, f"{attempt}-conflict.txt")
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(f"target {target}\nsource {src}\nstopped at {stopped}\n\n{result.stdout}\n{result.stderr}")
        raise Fail("conflict", "conflict", "owner", f"{len(files)} conflicting file(s) rebasing onto {target[:12]}",
                   files=files, logs=[str(evidence)],
                   action=f"In your worktree rebase or merge {ctx.target} (now {target[:12]}) into your branch, resolve "
                          "the conflicts in the listed files, run the relevant checks, commit, then "
                          "`merge-queue retry <request_id>`.")
    return work, git("rev-parse", "HEAD", cwd=work).stdout.strip(), "rebased"


def validate(ctx: Ctx, req: dict, attempt: str, work: Path, target: str, candidate: str,
             deadline: float | None = None) -> None:
    rid = req["request_id"]
    policy = load_policy(ctx.policy)
    paths = changed_paths(ctx, target, candidate)
    checks, unknown = plan_checks(policy, paths)
    if unknown:
        raise Fail("blocked", "unknown_scope", "operator",
                   f"{len(unknown)} changed path(s) have no validation policy: {', '.join(unknown[:5])}",
                   files=unknown[:50], action="Operator: add the scope to the validation policy (unknown scopes "
                                              "fail closed), then `merge-queue retry <id>`.")
    mutate(ctx, rid, lambda r: r.update(phase="validating", plan={"paths": len(paths), "checks": checks, "gate_owner": "merge-queue", "results_reused": False, "details": {}}))
    attempt_file = ctx.p("attempts", f"{rid}.json")
    results = []
    subst = {"target": target, "candidate": candidate, "worktree": str(work)}
    def resolve(argv, cwd):
        argv = [a.format(**subst) for a in argv]
        if "/" in argv[0] and not os.path.isabs(argv[0]):
            argv[0] = str(cwd / argv[0])
        return argv

    def run_step(name, label, argv, cwd, spec, kind):
        log = ctx.p("evidence", rid, f"{attempt}-{name}-{label}.log")
        started = time.monotonic()
        own = int(spec.get("timeout", 1800))
        remaining = None if deadline is None else deadline - started
        if remaining is not None and remaining <= 0:
            raise Infra(f"land time budget exhausted before {name} {label}; retrying later")
        limited = remaining is not None and remaining < own
        code = run_group(argv, cwd, step_env(spec.get("env", {})), log, min(own, remaining) if limited else own,
                         attempt_file)
        seconds = round(time.monotonic() - started, 3)
        results.append({"check": name, "step": label, "exit": code, "seconds": seconds, "log": str(log)})
        event(ctx, "check", rid, level="debug", check=name, step=label, exit=code, seconds=seconds)
        mutate(ctx, rid, lambda r: r.update(validation=results))
        # Setup/plan steps never blame the owner; only the check itself can be an owner failure.
        if code is None and limited:  # cut short by the land budget (not its own timeout): never the owner's fault
            raise Infra(f"land time budget exhausted during {name} {label} (stopped after {round(remaining)}s); "
                        f"log {log}")
        if code is None or (kind != "check" and code) or code < 0 or code in INFRA_EXIT:
            raise Infra(f"{name} {label} infrastructure failure (exit {code}); log {log}")
        if code:
            raise Fail("validation_failed", "validation_failed", "owner",
                       f"{name} failed with exit {code} on the combined candidate {candidate[:12]}",
                       logs=[str(log)], summary=tail(log),
                       action="Fix the failing checks in your worktree (the log shows the combined candidate), "
                              "run them locally, commit, then `merge-queue retry <request_id>`.")
        head = git("rev-parse", "HEAD", cwd=work).stdout.strip()
        if head != candidate or git("status", "--porcelain", "--untracked-files=no", cwd=work).stdout.strip():
            raise Fail("blocked", "candidate_mutated", "operator",
                       f"{name} {label} changed tracked files or HEAD in the candidate workspace", logs=[str(log)],
                       action="Operator: validation must not modify tracked files; fix the check.")
        return log

    for name in checks:
        spec = policy["checks"][name]
        cwd = work / spec.get("cwd", ".")
        setup = []
        if spec.get("plan"):  # the project's own change-aware planner decides which dependency sets are needed
            plan_log = run_step(name, "plan", resolve(spec["plan"], cwd), cwd, spec, "plan")
            try:
                planned = json.loads([l for l in plan_log.read_text().splitlines() if l.startswith("{")][-1])
            except (IndexError, ValueError, OSError):
                raise Infra(f"{name} plan produced no JSON; log {plan_log}")
            if planned.get("unknown"):  # fail before any dependency setup
                raise Fail("blocked", "unknown_scope", "operator",
                           f"{name} cannot route {len(planned['unknown'])} changed path(s): "
                           f"{', '.join(planned['unknown'][:5])}", files=planned["unknown"][:50], logs=[str(plan_log)],
                           action="Operator: extend the project validator/policy for these paths (unknown scopes "
                                  "fail closed), then `merge-queue retry <id>`.")
            mutate(ctx, rid, lambda r: r["plan"]["details"].update({name: planned}))
            for needed in planned.get("checks", []):
                argv = spec.get("bootstrap", {}).get(needed)
                if argv and argv not in setup:
                    setup.append(argv)
        for index, argv in enumerate(setup, 1):
            run_step(name, f"setup{index}", resolve(argv, cwd), cwd, spec, "setup")
        run_step(name, "check", resolve(spec["argv"], cwd), cwd, spec, "check")


def publish(ctx: Ctx, req: dict, attempt: str, target: str, candidate: str, attempts: int,
            integration: FileLock) -> None:
    """The only mutation of the primary checkout: `git merge --ff-only` of the validated candidate.

    The Git child inherits the integration lock, so even if this helper is killed no second helper can start
    (or be authorised by the hook) until Git has finished. A rejected fast-forward is never "repaired" here:
    Git may have staged the candidate into the checkout before the ref update failed, and the primary belongs
    to its operator, so the request fails closed with a durable notice."""
    rid = req["request_id"]
    check_checkout(ctx, changed_paths(ctx, target, candidate))
    if rev_parse(ctx, ctx.target) != target:
        raise TargetMoved()
    receipt = {"old": target, "new": candidate, "attempt": attempt, "at": now()}
    mutate(ctx, rid, lambda r: r.update(phase="publishing", publication=receipt))
    fault("after_receipt", attempts)
    try:  # last cheap race checks; nothing has touched the checkout yet (re-checks branch, dirt and overlap)
        if rev_parse(ctx, ctx.target) != target:
            raise TargetMoved()
        check_checkout(ctx, changed_paths(ctx, target, candidate))
    except (TargetMoved, Fail):
        mutate(ctx, rid, lambda r: r.update(publication=None))
        raise
    auth = ctx.p("auth", f"{rid}.json")
    write_json(auth, {"ref": ctx.target, "old": target, "new": candidate, "request_id": rid, "created": time.time(),
                      "helper": proc_identity(os.getpid())})
    timed_out = False
    log = ctx.p("evidence", rid, f"{attempt}-publish.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        fault("after_auth", attempts)  # test barrier: only the publication-bound hook can catch a switch here
        with critical(), open(log, "wb") as sink:
            # Own session (a SIGTERM aimed at our group must not interrupt Git half-way) that inherits the lock.
            # Output goes to a file, never a pipe: a dead helper must not SIGPIPE Git mid-publication.
            # --no-autostash: never stash/reapply someone's edits, whatever merge.autoStash says. The env var
            # binds every ref transaction of this Git process to this publication (see publication_bound_guard).
            merge = subprocess.Popen(["git", "merge", "--ff-only", "--no-autostash", candidate],
                                     cwd=str(ctx.checkout), stdout=sink, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL,
                                     env=clean_env({"GIT_MERGE_AUTOEDIT": "no", "MERGE_QUEUE_PUBLICATION": rid}),
                                     start_new_session=True, pass_fds=(integration.fd,))
            mutate(ctx, rid, lambda r: r["publication"].update(git=proc_identity(merge.pid)))
            try:
                merge.wait(timeout=PUBLISH_TIMEOUT)
            except subprocess.TimeoutExpired:
                timed_out = True
                merge.kill()  # our own, verified child
                merge.wait()
    finally:
        auth.unlink(missing_ok=True)
    err = tail(log, 10, 600)
    current = rev_parse(ctx, ctx.target)
    if merge.returncode == 0 and not timed_out:
        fault("after_merge", attempts)
        verify_published_checkout(ctx, rid, candidate)
        return
    dirty = [path for xy, path in porcelain(ctx.checkout) if xy != "??"]
    mutate(ctx, rid, lambda r: r.update(publication=None if current != candidate else r["publication"]))
    if current == candidate:  # the ref moved before Git was stopped
        return
    if dirty:
        raise Fail("blocked", "publication_checkout_dirty", "operator",
                   f"git merge --ff-only {candidate[:12]} was {'timed out' if timed_out else 'rejected'} "
                   f"(exit {merge.returncode}) and left the integration checkout with changes: {', '.join(dirty[:10])}. "
                   f"{err.strip()[:300]}", files=dirty[:50], logs=[str(log)],
                   action="Operator: the queue never resets the primary checkout. Inspect `git status` there; if the "
                          f"staged changes are exactly the rejected candidate {candidate}, undo them yourself (its exact "
                          f"inverse is `git read-tree -m -u {candidate} {current}`) once no one is editing, then "
                          "`merge-queue retry <id>`.")
    if timed_out:
        raise Fail("blocked", "publication_timeout", "operator",
                   f"git merge --ff-only exceeded {PUBLISH_TIMEOUT}s", action="Operator: inspect the checkout, then retry.")
    if current != target:
        raise TargetMoved()
    raise Infra(f"git merge --ff-only failed: {err.strip()[:300]}")


def verify_published_checkout(ctx: Ctx, rid: str, candidate: str) -> None:
    """After a successful merge the configured primary must still be on the target at the target's tip.
    Local edits are reported, never treated as failure (someone may legitimately be working there)."""
    head = git("symbolic-ref", "-q", "HEAD", cwd=ctx.checkout, check=False).stdout.strip()
    tip = rev_parse(ctx, ctx.target)
    at = git("rev-parse", "--verify", "-q", "HEAD", cwd=ctx.checkout, check=False).stdout.strip()
    if head != ctx.target or at != tip or not is_ancestor(ctx, candidate, tip):
        raise Fail("blocked", "publication_checkout_state", "operator",
                   f"after publishing, the integration checkout is on {head or 'a detached HEAD'} at {at[:12]} while "
                   f"{ctx.target} is {tip[:12]} (candidate {'is' if is_ancestor(ctx, candidate, tip) else 'is NOT'} in it)",
                   action="Operator: inspect the checkout; nothing was reset. If the target contains the candidate, "
                          "`merge-queue retry <id>` reports it landed.")
    dirt = [path for xy, path in porcelain(ctx.checkout) if xy != "??"]
    if dirt:
        event(ctx, "checkout_dirty_after_publication", rid, paths=dirt[:20])


def finish_landed(ctx: Ctx, rid: str, landed: str, note: str) -> None:
    final = rev_parse(ctx, ctx.target)
    if not is_ancestor(ctx, landed, final):
        raise Infra(f"post-publication verification failed: {landed[:12]} is not in {ctx.target}")
    finished = now()

    def done(r):
        r.update(state="landed", phase=None, publication=r.get("publication"))
        r["result"] = {"outcome": "landed", "landed_sha": landed, "target_after": final, "note": note,
                       "finished_at": finished, "logs_retained": False}
        for step in r.get("validation", []):  # successful evidence is pruned below: don't claim files that are gone
            step["log"] = None
    req = mutate(ctx, rid, done)
    for ref in (source_ref(rid), candidate_ref(rid)):
        ref_delete(ctx, ref)
    shutil.rmtree(ctx.p("evidence", rid), ignore_errors=True)
    event(ctx, "landed", rid, landed=landed, note=note, attempts=req["attempts"])


def check_checkout_ready(ctx: Ctx, req: dict, target: str) -> None:
    """Cheap pre-publication check of the integration checkout. A not-ready checkout (dirty, untracked, overlapping,
    wrong branch) is a bounded wait (CheckoutWait), not a terminal failure; publish() re-checks exactly."""
    rid, src = req["request_id"], req["source_commit_sha"]
    try:
        base = git("merge-base", target, src, cwd=ctx.checkout).stdout.strip()
        check_checkout(ctx, changed_paths(ctx, base, src))
    except Fail as f:
        raise CheckoutWait(f)
    if req.get("checkout_wait_since"):
        mutate(ctx, rid, lambda r: [r.pop(k, None) for k in ("checkout_wait_since", "checkout_wait_reason")])
        sync_checkout_notice(ctx)


def sync_checkout_notice(ctx: Ctx) -> None:
    """notices/checkout-wait.json mirrors the live requests that wait for the integration checkout."""
    path = ctx.p("notices", "checkout-wait.json")
    waiting = [r for r in all_requests(ctx) if r["state"] not in TERMINAL and r.get("checkout_wait_since")]
    if not waiting:
        path.unlink(missing_ok=True)
        return
    note = {"since": min(r["checkout_wait_since"] for r in waiting), "reason": waiting[0].get("checkout_wait_reason"),
            "requests": [r["request_id"] for r in waiting], "limit_seconds": ctx.checkout_wait_limit,
            "required_action": "Operator: resolve the listed paths in the integration checkout (never reset it blindly); "
                               "waiting requests resume automatically, or fail terminally after the wait limit."}
    old = read_json(path) or {}
    if (old.get("reason"), old.get("requests")) != (note["reason"], note["requests"]):
        write_json(path, note)


def checkout_wait_or_fail(ctx: Ctx, rid: str, f: Fail) -> int:
    """Requeue without penalty (no attempt, no dispatch counted) until the wait limit, then fail as before."""
    req = load_req(ctx, rid)
    since = req.get("checkout_wait_since") or now()
    if time.time() - parse_ts(since) >= ctx.checkout_wait_limit:
        record_failure(ctx, rid, Fail(f.state, f.ftype, f.klass, f"{f.message} (waited {ctx.checkout_wait_limit}s)",
                                      files=f.files, logs=f.logs, summary=f.summary, action=f.action))
        sync_checkout_notice(ctx)
        return EXIT_OPERATOR
    delay = float(os.environ.get("MERGE_QUEUE_CHECKOUT_WAIT_DELAY", "30"))
    mutate(ctx, rid, lambda r: r.update(
        state="queued", phase=None, tq=None, last_failure=f"waiting for the integration checkout: {f.ftype}",
        attempts=max(r["attempts"] - 1, 0), dispatches=max(r["dispatches"] - 1, 0), not_before=time.time() + delay,
        checkout_wait_since=since, checkout_wait_reason=f"{f.ftype}: {f.message}"[:500]))
    sync_checkout_notice(ctx)
    event(ctx, "checkout_wait", rid, failure_type=f.ftype, delay=delay)
    return 75


def integrate(ctx: Ctx, rid: str, attempt: str, attempts: int, state: dict, integration: FileLock) -> None:
    for refresh in range(REFRESH_LIMIT + 1):
        req = load_req(ctx, rid)
        target = rev_parse(ctx, ctx.target)
        pub = req.get("publication")
        if pub:  # an earlier attempt may have published before dying; the lock proves its Git child is gone
            if is_ancestor(ctx, pub["new"], target):
                return finish_landed(ctx, rid, pub["new"], "reconciled after interrupted publication")
            # Not published. A checkout left dirty by that attempt is caught by check_checkout before any retry.
            mutate(ctx, rid, lambda r: r.update(publication=None))
        src = req["source_commit_sha"]
        if is_ancestor(ctx, src, target):
            return finish_landed(ctx, rid, src, "already contained in the target")
        if refresh and state.get("deadline") is not None and state["deadline"] - time.monotonic() <= 0:
            raise Infra("land time budget exhausted before a refresh; retrying later")
        check_checkout_ready(ctx, req, target)
        mutate(ctx, rid, lambda r: r.update(phase="preparing", target_sha=target))
        t0 = time.monotonic()
        fault("before_prepare", attempts)
        _, candidate, mode = prepare(ctx, req, attempt, target, state)
        if candidate == target:
            return finish_landed(ctx, rid, target, "all changes already contained in the target")
        git("update-ref", candidate_ref(rid), candidate, cwd=ctx.checkout)
        prep = round(time.monotonic() - t0, 3)
        mutate(ctx, rid, lambda r: r.update(candidate={"sha": candidate, "target_sha": target, "mode": mode},
                                            timings={**r["timings"], "prepare_seconds": prep}))
        fault("after_prepare", attempts)
        t1 = time.monotonic()
        validate(ctx, req, attempt, state["work"], target, candidate, state.get("deadline"))
        val = round(time.monotonic() - t1, 3)
        mutate(ctx, rid, lambda r: r.update(timings={**r["timings"], "validation_seconds": val}))
        fault("after_validate", attempts)
        try:
            publish(ctx, req, attempt, target, candidate, attempts, integration)
        except TargetMoved:
            event(ctx, "target_moved", rid, expected=target)
            remove_worktree(ctx, state.pop("work", None))
            continue
        return finish_landed(ctx, rid, candidate, mode)
    raise Infra("the target moved on every refresh; retrying later")


def record_failure(ctx: Ctx, rid: str, f: Fail) -> None:
    req = load_req(ctx, rid)
    entry = {"request_id": rid, "owner_id": req["owner_id"], "repository": req["repository"],
             "source_commit_sha": req["source_commit_sha"], "target_commit_sha": req.get("target_sha"),
             "failure_type": f.ftype, "failure_class": f.klass, "message": f.message, "conflicting_files": f.files,
             "relevant_log_paths": f.logs, "summary": f.summary, "required_action": f.action, "created_at": now()}
    write_json(owner_dir(ctx, req["owner_id"]) / f"{rid}.json", entry)  # durable before the state flips

    def fin(r):
        r.update(state=f.state, phase=None)
        r["result"] = {"outcome": f.state, "failure_type": f.ftype, "failure_class": f.klass, "message": f.message,
                       "conflicting_files": f.files, "log_paths": f.logs, "required_action": f.action,
                       "finished_at": entry["created_at"]}
    mutate(ctx, rid, fin)
    event(ctx, f.state, rid, failure_type=f.ftype, files=len(f.files))


def requeue_or_fail(ctx: Ctx, rid: str, reason: str, penalty=True) -> None:
    req = load_req(ctx, rid)
    if req is None or req["state"] in TERMINAL:
        return
    if penalty and req["attempts"] >= ctx.max_attempts or req["dispatches"] >= ctx.max_attempts * 3:
        record_failure(ctx, rid, Fail("infra_failed", "infra", "infra", f"gave up after retries: {reason}",
                                      logs=[l for l in [(req.get("tq") or {}).get("log")] if l],
                                      action="Operator: inspect `merge-queue doctor`, fix the cause, then "
                                             "`merge-queue retry <request_id>` (same commit is allowed)."))
        return
    base = float(os.environ.get("MERGE_QUEUE_BACKOFF", "5"))
    delay = min(120, base * 2 ** max(req["attempts"] - 1, 0)) if penalty else min(2, base)
    mutate(ctx, rid, lambda r: r.update(state="queued", phase=None, tq=None, last_failure=reason,
                                        not_before=time.time() + delay))
    event(ctx, "requeued", rid, reason=reason, delay=delay)


def request_mismatch(ctx: Ctx, req: dict) -> str | None:
    """A request is only processed under the repository/target it was accepted for."""
    if req.get("repository") != str(ctx.common) or req.get("target_branch") != ctx.target:
        return (f"request was accepted for {req.get('repository')} {req.get('target_branch')} but the active "
                f"configuration is {ctx.common} {ctx.target}")
    return None


def cmd_land(args):
    ctx = load_ctx()
    rid = valid_id(args.request_id, "request id")
    if args.repo_id and args.repo_id != ctx.repo_id:
        print(f"{rid}: dispatched for repository {args.repo_id} but this checkout is {ctx.repo_id}; refusing")
        return EXIT_OPERATOR
    request_lock = FileLock(ctx.p("locks", f"{rid}.lock"), blocking=False)
    if not request_lock.acquire():
        print(f"{rid}: another landing process owns this request; duplicate dispatch ignored")
        return 0
    req = load_req(ctx, rid)
    if req is None or req["state"] in TERMINAL:
        print(f"{rid}: nothing to do ({req['state'] if req else 'unknown'})")
        return 0
    if request_mismatch(ctx, req):
        record_failure(ctx, rid, Fail("blocked", "config_mismatch", "operator", request_mismatch(ctx, req),
                                      action="Operator: the queue was reconfigured while this request was pending; "
                                             "restore the original configuration or resubmit from the owner."))
        return EXIT_OPERATOR
    integration = FileLock(ctx.p("locks", "integration.lock"), blocking=False)
    if not integration.acquire():
        print(f"{rid}: integration lock is held by another process; will be requeued")
        return 75
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    attempt = uuid.uuid4().hex[:8]
    # Everything (all refresh passes, plan/setup/check steps) must end before tq SIGKILLs this process group at
    # landTimeout, which would leave validation groups (own sessions) alive: stop cleanly inside this budget.
    budget = float(os.environ.get("MERGE_QUEUE_LAND_BUDGET") or
                   ctx.land_timeout - min(LAND_MARGIN, ctx.land_timeout * 0.1))
    state: dict = {"deadline": time.monotonic() + budget}
    code = 0
    acquired = now()
    try:
        sweep_stale(ctx)
        ctx.p("auth", f"{rid}.json").unlink(missing_ok=True)
        write_json(ctx.p("attempts", f"{rid}.json"), {"identity": proc_identity(os.getpid()), "attempt": attempt,
                                                      "group": None})

        def start(r):
            r.update(state="running", phase="preparing", attempt_id=attempt, attempts=r["attempts"] + 1, not_before=None)
            r["timings"].setdefault("started_at", acquired)
            r["timings"]["queue_wait_seconds"] = round(parse_ts(acquired) - parse_ts(r["submitted_at"]), 3)
            r["timings"]["lock_acquired_at"] = acquired
        req = mutate(ctx, rid, start)
        event(ctx, "integration_started", rid, attempt=attempt, number=req["attempts"],
              queue_wait=req["timings"]["queue_wait_seconds"])
        integrate(ctx, rid, attempt, req["attempts"], state, integration)
    except CheckoutWait as wait:
        code = checkout_wait_or_fail(ctx, rid, wait.fail)
    except Fail as f:
        record_failure(ctx, rid, f)
        code = EXIT_OPERATOR if f.klass == "operator" else EXIT_OWNER
    except (Infra, MqError, subprocess.SubprocessError, OSError) as exc:
        requeue_or_fail(ctx, rid, str(exc)[:500])
        code = 75
    except Exception as exc:  # unexpected bug (KeyError, TypeError...): requeue with a cause instead of "vanished"
        import traceback
        detail = f"unexpected {exc!r}"
        with contextlib.suppress(Exception):
            crash = ctx.p("evidence", rid, f"{attempt}-crash.txt")
            crash.parent.mkdir(parents=True, exist_ok=True)
            crash.write_text(traceback.format_exc()[-4000:])
            detail += f"; traceback {crash}"
        requeue_or_fail(ctx, rid, detail[:500])
        code = 75
    except Terminated:
        mutate(ctx, rid, lambda r: r.update(attempts=max(r["attempts"] - 1, 0)) if r["state"] not in TERMINAL else None)
        requeue_or_fail(ctx, rid, "terminated", penalty=False)
        code = 130
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with contextlib.suppress(Exception):
            remove_worktree(ctx, state.get("work"))
        with contextlib.suppress(Exception):
            ctx.p("attempts", f"{rid}.json").unlink(missing_ok=True)
            released = now()
            mutate(ctx, rid, lambda r: r["timings"].update(lock_released_at=released, finished_at=released))
            event(ctx, "integration_finished", rid, exit=code,
                  seconds=round(parse_ts(released) - parse_ts(acquired), 3))
        integration.release()
        request_lock.release()
    return code


# ---------------------------------------------------------------- runner (thin durability layer)

def land_signature(ctx: Ctx, rid: str) -> str:
    """The exact argv tail of a landing process for THIS repository: `<entry> land --request-id R --repo-id P`."""
    return f"{ctx.entry_text} land --request-id {rid} --repo-id {ctx.repo_id}"


def args_match(ctx: Ctx, args: str, rid: str | None) -> bool:
    """Exact match of a process command line (ps joins argv with spaces, so match on token boundaries).
    rid=None accepts any request id of this repository, never a different repository or a longer id."""
    if rid is not None:
        pattern = re.escape(land_signature(ctx, rid)) + r"(\s|$)"
    else:
        pattern = re.escape(f"{ctx.entry_text} land --request-id ") + r"[A-Za-z0-9][A-Za-z0-9._-]*" + \
            re.escape(f" --repo-id {ctx.repo_id}") + r"(\s|$)"
    return re.search(r"(^|\s)" + pattern, args) is not None


def command_matches(ctx: Ctx, command: str, rid: str | None) -> bool:
    """Same for tq's stored command string (a shlex.join of the argv)."""
    try:
        return args_match(ctx, " ".join(shlex.split(command or "")), rid)
    except ValueError:
        return False


def tq_tasks(ctx: Ctx) -> list[dict] | None:
    result = subprocess.run([ctx.tq, "--data-dir", str(ctx.tq_dir), "list", "--json"], capture_output=True,
                            text=True, env=clean_env())
    try:
        return [t for t in json.loads(result.stdout)["tasks"] if t.get("queue_name") == ctx.queue]
    except (ValueError, KeyError, TypeError):
        return None


def tq_registered(ctx: Ctx, rid: str) -> bool:
    """Public-CLI check that tq has inserted this request's task (insertion order == tq's FIFO order)."""
    return any(command_matches(ctx, t.get("command"), rid) for t in tq_tasks(ctx) or [])


def owned_pids(ctx: Ctx) -> set[int]:
    """Pids of live processes that match an identity (pid + start time + argv) this queue persisted: attempt
    helpers/validation groups, publication Git children and recorded tq processes. An argv that merely contains
    our signature is NOT ownership. (Crash gap: a child launched but not yet recorded is conservatively unowned.)"""
    identities = []
    for path in ctx.p("attempts").glob("*.json") if ctx.p("attempts").is_dir() else []:
        data = read_json(path) or {}
        identities += [data.get("identity"), data.get("group")]
    for req in all_requests(ctx):
        identities += [(req.get("publication") or {}).get("git"), (req.get("tq") or {}).get("identity")]
    return {i["pid"] for i in identities if i and i.get("start") and proc_state(i) == "same"}


def tq_preflight(ctx: Ctx) -> list[str]:
    """Block dispatch while Block's own dead-parent cleanup could signal a process we cannot verify.

    tq's cleanup SIGTERMs the recorded child pid of a running row whose parent is not a live tq (or that ran
    over 120 minutes) with no start-time check. We read only the public `tq list --json`; if such a child is
    alive and is not verifiably one of this repository's landing processes, nothing is dispatched."""
    tasks = tq_tasks(ctx)
    if tasks is None:
        return ["`tq list --json` is unreadable; refusing to start tq"]
    problems = []
    for task in tasks:
        child = task.get("child_pid")
        if task.get("status") != "running" or not child:
            continue  # waiting rows are only deleted by tq, never signalled
        parent = proc_identity(task.get("pid"))
        parent_is_tq = bool(parent and re.search(r"(^|/)tq(\.py)?(\s|$)", " ".join(parent["args"].split()[:2])))
        try:
            age = time.time() - datetime.fromisoformat(task["updated_at"]).timestamp()
        except (KeyError, ValueError, TypeError):
            age = 0
        if parent_is_tq and age < 6000:
            continue
        ident = proc_identity(child)
        if ident is not None and child not in owned_pids(ctx):
            problems.append(f"tq row #{task.get('id')}: its parent {task.get('pid')} is gone or stale, and child pid "
                            f"{child} ({ident['args'][:80]!r}) is alive but is not a process this queue recorded "
                            "(pid+start+argv); tq's own cleanup would SIGTERM it")
    return problems


def sync_stale_notice(ctx: Ctx, problems: list[str]) -> None:
    path = ctx.p("notices", "stale-tq.json")
    if not problems:
        path.unlink(missing_ok=True)
        return
    if (read_json(path) or {}).get("problems") != problems:
        write_json(path, {"created_at": now(), "problems": problems,
                          "required_action": "Operator: verify the listed pid(s) by hand. If a pid is unrelated, "
                                             "the stale tq row must be cleared by an operator after confirming no "
                                             "landing is running (the queue never edits tq's database or runs "
                                             "`tq clear`); dispatch resumes automatically once the row is gone."})
        event(ctx, "dispatch_blocked", None, problems=problems)


def spawn_tq(ctx: Ctx, req: dict) -> subprocess.Popen | None:
    rid = req["request_id"]
    log = ctx.p("runner", f"tq-{rid}-{req['dispatches'] + 1}.log")
    log.parent.mkdir(parents=True, exist_ok=True)

    def intend(r):  # persisted BEFORE launch: only a still-queued request can be dispatched, never a later state
        if r["state"] != "queued":
            raise MqError(f"{rid} is {r['state']}; not dispatching")
        r.update(state="dispatched", phase="waiting", dispatches=r["dispatches"] + 1,
                 tq={"pid": None, "log": str(log), "dispatched_at": now()})
    try:
        mutate(ctx, rid, intend)
    except MqError:
        return None
    cmd = [ctx.tq, "--data-dir", str(ctx.tq_dir), "run", "-q", ctx.queue, "-t", str(ctx.land_timeout),
           "-C", str(ctx.checkout), *ctx.entry, "land", "--request-id", rid, "--repo-id", ctx.repo_id]
    with open(log, "wb") as out:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True,
                                env=clean_env({"TASK_QUEUE_DATA_DIR": str(ctx.tq_dir), "PYTHONUNBUFFERED": "1"}))
    fault("after_spawn", req["dispatches"] + 1)
    task_id = None
    deadline, next_list = time.monotonic() + 30, time.monotonic() + 3
    while time.monotonic() < deadline and proc.poll() is None:  # keeps tq's own FIFO ids in accepted order
        match = re.search(r"Task #(\d+) queued", log.read_text(errors="replace"))
        if match:
            task_id = int(match.group(1))
            break
        if time.monotonic() > next_list:
            next_list = time.monotonic() + 3
            if tq_registered(ctx, rid):
                break
        time.sleep(0.02)
    identity = proc_identity(proc.pid)

    def record(r):  # bookkeeping only: never touches `state`, so it cannot overwrite running/terminal outcomes
        if r["state"] not in TERMINAL:
            r["tq"] = {**(r.get("tq") or {}), "pid": proc.pid, "identity": identity, "task_id": task_id}
    mutate(ctx, rid, record)
    event(ctx, "dispatched", rid, tq_task=task_id, tq_pid=proc.pid)
    return proc


def land_processes(ctx: Ctx, rid: str) -> list[int]:
    """Live processes (tq, its shell, the helper) whose argv carries exactly this repository's landing of `rid`."""
    out = subprocess.run(["ps", "-axo", "pid=,stat=,args="], capture_output=True, text=True).stdout
    pids = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and not parts[1].startswith("Z") and args_match(ctx, parts[2], rid) \
                and int(parts[0]) != os.getpid():
            pids.append(int(parts[0]))
    return pids


def tick_request(ctx: Ctx, req: dict, children: dict, killing: dict, gate: dict) -> None:
    rid = req["request_id"]
    if req["state"] in ("dispatched", "running"):
        tq = req.get("tq") or {}
        child = children.get(rid)
        if child is not None:
            alive, code = child.poll() is None, child.returncode
        elif tq.get("identity"):
            alive, code = proc_state(tq["identity"]) == "same", None
        else:  # runner died between launching tq and recording it: tq's argv carries the request id
            alive, code = bool(land_processes(ctx, rid)), None
        if alive:
            return
        children.pop(rid, None)
        fresh = load_req(ctx, rid)
        if fresh["state"] in TERMINAL:
            return
        # tq died but its landing helper may survive: stop it only when verified, and never redispatch beside it.
        identity = (read_json(ctx.p("attempts", f"{rid}.json")) or {}).get("identity")
        if identity and proc_state(identity) == "same":
            first = killing.setdefault(rid, time.monotonic())
            with contextlib.suppress(OSError):
                os.kill(identity["pid"], signal.SIGKILL if time.monotonic() - first > 20 else signal.SIGTERM)
            return
        if land_processes(ctx, rid):
            return  # something running this exact landing still exists but cannot be verified: wait, never kill
        killing.pop(rid, None)
        if fresh["state"] == "queued":
            return  # land already requeued itself (interrupted)
        requeue_or_fail(ctx, rid, f"tq/landing process vanished (exit {code})", penalty=fresh["state"] == "running")
    elif req["state"] == "queued" and (req.get("not_before") or 0) <= time.time():
        if request_mismatch(ctx, req):
            return  # accepted under another repository/target: never dispatched; `land` would report it
        if land_processes(ctx, rid):
            return  # unrecorded dispatch from a previous runner is still alive
        if "problems" not in gate:  # once per tick: public tq state only, before any tq invocation
            gate["problems"] = tq_preflight(ctx)
            sync_stale_notice(ctx, gate["problems"])
        if gate["problems"]:
            return
        child = spawn_tq(ctx, req)
        if child is not None:
            children[rid] = child


def run_runner(ctx: Ctx, poll: float, idle_exit: float | None, drain: bool) -> int:
    lock = FileLock(ctx.p("locks", "runner.lock"), blocking=False)
    stop: list = []
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
    signal.signal(signal.SIGINT, lambda *a: stop.append(1))
    if not lock.acquire():
        if drain or idle_exit is not None:
            print("runner already active")
            return 0
        # launchd mode (KeepAlive): exiting would respawn us in a loop while an auto-runner holds the lock.
        wait = float(os.environ.get("MERGE_QUEUE_RUNNER_LOCK_POLL", "5"))
        print("runner already active; waiting for the lock", flush=True)
        while not stop and not lock.acquire():
            time.sleep(wait)
        if stop:
            return 0
    write_json(ctx.p("runner", "runner.json"), {"pid": os.getpid(), "started_at": now()})
    children: dict[str, subprocess.Popen] = {}
    killing: dict[str, float] = {}
    idle_since = time.monotonic()
    last_gc = 0.0
    while not stop:
        live = [r for r in all_requests(ctx) if r["state"] not in TERMINAL]
        for rid in [k for k, c in children.items() if c.poll() is not None and k not in {r["request_id"] for r in live}]:
            del children[rid]
        gate: dict = {}
        for req in live:
            try:
                tick_request(ctx, req, children, killing, gate)
            except Exception as exc:  # one broken request must not stop the runner
                print(f"runner: {req['request_id']}: {exc!r}", file=sys.stderr, flush=True)
        if ctx.p("notices", "checkout-wait.json").exists() or any(r.get("checkout_wait_since") for r in live):
            with contextlib.suppress(Exception):
                sync_checkout_notice(ctx)  # drops the notice once its requests landed/failed/were superseded
        if time.time() - last_gc > 600:
            last_gc = time.time()
            with contextlib.suppress(Exception):
                gc(ctx)
        if live:
            idle_since = time.monotonic()
        elif drain or (idle_exit is not None and time.monotonic() - idle_since > idle_exit):
            lock.release()  # a submit racing with this exit must not be stranded
            time.sleep(0.3)
            if not any(r["state"] not in TERMINAL for r in all_requests(ctx)) or not lock.acquire():
                break
            continue
        time.sleep(poll)
    for rid, child in children.items():
        child.poll()  # tq children keep running in their own session; a new runner adopts them
    return 0


def cmd_runner(args):
    return run_runner(load_ctx(), args.poll, args.idle_exit, args.drain)


# ---------------------------------------------------------------- git hooks

def publication_lock_held(ctx: Ctx) -> bool:
    """True while a queue publication (or its inherited Git child) holds the integration lock."""
    probe = FileLock(ctx.p("locks", "integration.lock"), blocking=False)
    if probe.acquire():
        probe.release()
        return False
    return True


def cmd_hook(args):
    if args.hook_name == "session-start":
        return session_start_notice()
    return reference_transaction_guard(args.state)


def publication_bound_guard(ctx: Ctx, rid: str, data: str) -> int:
    """Every transaction of a queue publication is bound to that publication: the checkout's symbolic HEAD must
    still be the target and the only branch ref that may move is the target, from the authorised old to new.
    This runs before Git touches the working tree (first transaction), so a refusal leaves no dirt."""
    def deny(reason):
        print(f"merge-queue: publication {rid} refused: {reason}", file=sys.stderr)
        return 1
    try:
        auth = read_json(ctx.p("auth", f"{valid_id(rid)}.json")) or {}
    except (MqError, ValueError):
        auth = {}
    if auth.get("ref") != ctx.target or time.time() - auth.get("created", 0) >= AUTH_TTL \
            or not publication_lock_held(ctx):
        return deny("no live authorisation (the publisher no longer holds the integration lock)")
    head = git("symbolic-ref", "-q", "HEAD", cwd=os.getcwd(), check=False, env=dict(os.environ, LC_ALL="C")).stdout.strip()
    if head != ctx.target:
        return deny(f"the checkout is on {head or 'a detached HEAD'}, not {ctx.target}")
    for line in data.splitlines():
        parts = line.split()
        if len(parts) != 3 or not parts[2].startswith("refs/heads/"):
            continue
        old, new, ref = parts
        if ref != ctx.target or (old, new) != (auth.get("old"), auth.get("new")) or subprocess.run(
                ["git", "--git-dir", str(ctx.common), "merge-base", "--is-ancestor", old, new],
                env=clean_env()).returncode != 0:
            return deny(f"{ref} {old[:12]}..{new[:12]} is not the authorised {ctx.target} fast-forward")
    return 0


def reference_transaction_guard(state: str | None) -> int:
    """Guard agent updates; an explicit operator override allows ordinary manual Git.
    Queue publications always retain their exact-SHA, expiry and lock checks."""
    data = sys.stdin.read()
    if state != "prepared":
        return 0
    # Check before loading configuration so a person can also recover a broken installation.
    # Never relax a queue publication, even when a runner inherited a manual shell environment.
    if os.environ.get("MERGE_QUEUE_MANUAL") == "1" and not os.environ.get("MERGE_QUEUE_PUBLICATION"):
        return 0
    try:
        ctx = load_ctx(os.getcwd())
    except MqError as exc:
        print(f"merge-queue: cannot verify target update ({exc}); refusing", file=sys.stderr)
        return 1
    if os.environ.get("MERGE_QUEUE_PUBLICATION"):
        return publication_bound_guard(ctx, os.environ["MERGE_QUEUE_PUBLICATION"], data)
    for line in data.splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[2] != ctx.target:
            continue
        old, new = parts[0], parts[1]
        ok = False
        auth_dir = ctx.p("auth")
        for path in auth_dir.glob("*.json") if auth_dir.is_dir() else []:
            auth = read_json(path) or {}
            if auth.get("ref") == ctx.target and auth.get("old") == old and auth.get("new") == new \
                    and time.time() - auth.get("created", 0) < AUTH_TTL and publication_lock_held(ctx):
                ok = subprocess.run(["git", "--git-dir", str(ctx.common), "merge-base", "--is-ancestor", old, new],
                                    env=clean_env()).returncode == 0
        if not ok:
            print(f"merge-queue: {ctx.target} may only be updated by the integration queue "
                  f"(submit with `merge-queue submit`); refusing {old[:12]}..{new[:12]}\n"
                  "merge-queue: for a deliberate manual Git command, set MERGE_QUEUE_MANUAL=1.\n"
                  "merge-queue: Git may already have staged the new tree in this checkout before the refusal; "
                  "nothing resets it automatically. Inspect `git status` and recover deliberately.", file=sys.stderr)
            return 1
    return 0


def session_start_notice() -> int:
    """Native SessionStart adapter (Codex and Claude Code): stdout JSON with additionalContext when this agent has
    pending integration failures; silent (exit 0) otherwise, including outside a repository or when not installed."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        cwd = payload.get("cwd") or os.getcwd()
        ctx = load_ctx(cwd)
        owners = owner_candidates(argparse.Namespace(owner=None), cwd, extra=[payload.get("session_id")])
        entries = inbox_entries(ctx, owners)
    except (MqError, ValueError, OSError, TypeError):
        return 0
    if not entries:
        return 0
    lines = [f"- {e['request_id']}: {e['failure_type']} for {e['source_commit_sha'][:12]}: {e['required_action']}"
             for e in entries[:5]]
    text = (f"Pending integration queue failures ({len(entries)}) need repair; run `merge-queue inbox` "
            "for files and logs, repair, run the relevant checks, commit, then `merge-queue retry <id>`.\n"
            + "\n".join(lines))
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
    return 0


def wrapper_ref_transaction(ctx_vals: dict, chained: bool) -> str:
    target, entry = shlex.quote(ctx_vals["target"]), ctx_vals["entry"]  # entry: an already shell-quoted argv prefix
    hook_dir = shlex.quote(ctx_vals["hook_dir"])
    chain = ""
    if chained:
        chain = f'if [ -x {hook_dir}/reference-transaction{CHAINED} ]; then\n' \
                f'  printf \'%s\\n\' "$input" | {hook_dir}/reference-transaction{CHAINED} "$@" || exit $?\nfi\n'
    # The guard runs for any transaction touching the target and for EVERY transaction of a queue publication
    # (MERGE_QUEUE_PUBLICATION is set by the publisher only; setting it elsewhere merely adds restrictions).
    return (f"#!/bin/sh\n{HOOK_MARK} (reference-transaction)\ninput=$(cat)\n{chain}"
            'if [ "$1" = prepared ]; then\n  run=0\n  [ -n "${MERGE_QUEUE_PUBLICATION:-}" ] && run=1\n'
            f'  case "$input" in\n    *" "{target}*) run=1 ;;\n  esac\n  if [ "$run" = 1 ]; then\n'
            f'    printf \'%s\\n\' "$input" | {entry} hook reference-transaction "$@" || exit $?\n  fi\nfi\nexit 0\n')


def wrapper_pre_push(ctx_vals: dict) -> str:
    target = ctx_vals["target"]
    hook_dir = shlex.quote(ctx_vals["hook_dir"])
    return (f"#!/bin/sh\n{HOOK_MARK} (pre-push)\ninput=$(cat)\n"
            f'if [ -x {hook_dir}/pre-push{CHAINED} ]; then\n'
            f'  printf \'%s\\n\' "$input" | {hook_dir}/pre-push{CHAINED} "$@" || exit $?\nfi\n'
            f"if printf '%s\\n' \"$input\" | awk '$3 == \"{target}\" {{f=1}} END {{exit f?0:1}}'; then\n"
            '  if [ "${MERGE_QUEUE_PUSH_APPROVED:-}" != 1 ] && [ "${MERGE_QUEUE_MANUAL:-}" != 1 ]; then\n'
            f'    echo "merge-queue: pushing {target} is a separate, explicitly authorised step '
            '(set MERGE_QUEUE_PUSH_APPROVED=1 or MERGE_QUEUE_MANUAL=1 for that push)" >&2\n    exit 1\n  fi\nfi\nexit 0\n')


def hooks_dir(checkout: Path) -> Path:
    path = git("rev-parse", "--path-format=absolute", "--git-path", "hooks", cwd=checkout).stdout.strip()
    return Path(path)


def install_hook(directory: Path, name: str, content: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    chained = directory / (name + CHAINED)
    status = "installed"
    if path.exists() or path.is_symlink():
        if HOOK_MARK in (path.read_text(errors="replace") if path.is_file() else ""):
            status = "updated"
        else:
            if chained.exists():
                raise MqError(f"{chained} already exists; resolve manually")
            os.replace(path, chained)
            status = "installed (existing hook chained)"
    path.write_text(content)
    path.chmod(0o755)
    return status


def self_entry() -> list[str]:
    """argv prefix that runs this tool from its installed environment. -P keeps the current directory (a repository
    being operated on) off sys.path, so a project's own `merge_queue` directory can never shadow the tool."""
    return [sys.executable, "-P", "-m", "merge_queue.core"]


def temp_roots() -> list[Path]:
    """System temporary directories (MERGE_QUEUE_TEMP_DIRS, os.pathsep separated, replaces the default; test seam)."""
    override = os.environ.get("MERGE_QUEUE_TEMP_DIRS")
    names = override.split(os.pathsep) if override is not None else [tempfile.gettempdir(), "/tmp", "/var/tmp"]
    return [Path(n).resolve() for n in names if n]


def unstable_reason(path) -> str | None:
    """Why a path must not be baked into hooks, runner and config: it lives in a *linked* Git worktree (of any
    repository; it disappears with that task worktree) or under the system temporary directory. None when stable."""
    try:
        real = Path(path).resolve()
    except OSError:
        return f"{path} cannot be resolved"
    for root in temp_roots():
        if real.is_relative_to(root):
            return f"{real} is under the system temporary directory {root}"
    here = real if real.is_dir() else real.parent
    while not here.exists() and here != here.parent:
        here = here.parent
    result = git("rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", cwd=here, check=False)
    lines = result.stdout.splitlines()
    if result.returncode == 0 and len(lines) == 2 and Path(lines[0]).resolve() != Path(lines[1]).resolve():
        return f"{real} is inside the linked Git worktree {git('rev-parse', '--show-toplevel', cwd=here).stdout.strip()}"
    return None


def install_pointer_problems(entry: list[str], module_file) -> list[str]:
    """Stability problems of the pointers an installation depends on (interpreter and module file)."""
    problems = []
    for what, path in (("interpreter", entry[0]), ("merge_queue module", module_file)):
        reason = unstable_reason(path) if path else None
        if reason:
            problems.append(f"{what}: {reason}")
    return problems


def cmd_install(args):
    cwd = os.getcwd()
    common = find_common(cwd)
    if args.checkout:
        checkout = Path(args.checkout).resolve()
    else:
        listing = git("worktree", "list", "--porcelain", cwd=cwd).stdout
        checkout = Path(listing.split("\n", 1)[0].removeprefix("worktree ")).resolve()
    target = f"refs/heads/{args.target}"
    head = git("symbolic-ref", "-q", "HEAD", cwd=checkout, check=False).stdout.strip()
    if head != target:
        raise MqError(f"integration checkout {checkout} is on {head or 'a detached HEAD'}, not {target}", 2)
    tq = args.tq or shutil.which("tq") or str(Path.home() / ".local/bin/tq")
    if not os.access(tq, os.X_OK):
        raise MqError(f"tq not found at {tq}; install: uv tool install agent-task-queue==0.4.1", 2)
    entry = self_entry()
    unstable = install_pointer_problems(entry, Path(__file__))
    if unstable and not args.allow_unstable_paths:
        raise MqError("refusing to install: hooks, runner and config would point at a path that can disappear "
                      f"({'; '.join(unstable)}). Install the tool into a stable environment (for example "
                      "`uv tool install`) and run it from there, or pass --allow-unstable-paths for a temporary "
                      "pointer", 2)
    policy = str(Path(args.policy).resolve()) if args.policy else str(checkout / ".merge-queue" / "policy.json")
    if not Path(policy).is_file():
        raise MqError(f"validation policy not found: {policy} (pass --policy PATH, or add "
                      ".merge-queue/policy.json to the integration checkout; see examples/policy.example.json)", 2)
    prefix = args.launchd_label_prefix
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", prefix):
        raise MqError(f"invalid --launchd-label-prefix {prefix!r}: use letters, digits, '.', '_' or '-'", 2)
    for allowed in args.runtime_allow or []:  # not `prefix`: that name holds the launchd label prefix
        pure = PurePosixPath(allowed)
        if pure.is_absolute() or ".." in pure.parts or pure.parts in ((), (".",)) or any(c in allowed for c in "*?["):
            raise MqError(f"--runtime-allow {allowed!r} must be one specific relative runtime path "
                          "(e.g. data/), never a glob, '.', or '..'", 2)
    state = Path(args.state_dir).resolve() if args.state_dir else default_state_dir(common)
    previous = read_config(common)
    if previous.get("stateDir") and all(k in previous for k in ("checkout", "tq", "entry", "policy")):
        old_ctx = Ctx(common, previous)
        changed = (str(old_ctx.state), str(old_ctx.checkout), old_ctx.target) != (str(state), str(checkout), target)
        busy = [r["request_id"] for r in all_requests(old_ctx) if r["state"] not in TERMINAL]
        if changed and busy:
            raise MqError(f"refusing to retarget the queue (state/checkout/target) while requests are pending: "
                          f"{', '.join(busy[:5])}", 4)
    registry = registry_path(repo_id_for(common))  # always under the default root, even for a custom --state-dir
    plan = {"stateDir": str(state), "checkout": str(checkout), "target": target, "tq": tq, "entry": shlex.join(entry),
            "policy": policy, "maxAttempts": str(args.max_attempts), "landTimeout": str(args.land_timeout),
            "checkoutWaitLimit": str(args.checkout_wait_limit), "registry": str(registry)}
    values = {"runtimeAllow": args.runtime_allow or []}
    hdir = hooks_dir(checkout)
    vals = {"target": target, "entry": shlex.join(entry), "hook_dir": str(hdir)}
    if args.dry_run:
        print(json.dumps({"config": plan, "runtimeAllow": values["runtimeAllow"], "hooks_dir": str(hdir)}, indent=1))
        return 0
    for key, value in plan.items():
        set_config(common, key, [value])
    set_config(common, "runtimeAllow", values["runtimeAllow"])
    for sub in ("requests", "inbox", "locks", "tq"):
        (state / sub).mkdir(parents=True, exist_ok=True)
    write_json(registry, {"repo_id": repo_id_for(common), "common_dir": str(common), "checkout": str(checkout),
                          "state_dir": str(state), "target": target, "installed_at": now()})
    report = {"config": "written to " + str(common / "config"), "registry": str(registry)}
    if not args.no_hooks:
        chained_rt = (hdir / ("reference-transaction" + CHAINED)).exists() or \
            ((hdir / "reference-transaction").exists() and HOOK_MARK not in (hdir / "reference-transaction").read_text(errors="replace"))
        chained_pp = (hdir / ("pre-push" + CHAINED)).exists() or \
            ((hdir / "pre-push").exists() and HOOK_MARK not in (hdir / "pre-push").read_text(errors="replace"))
        report["reference-transaction"] = install_hook(hdir, "reference-transaction", wrapper_ref_transaction(vals, chained_rt))
        report["pre-push"] = install_hook(hdir, "pre-push", wrapper_pre_push(vals))
    if args.launchd_plist:
        label = f"{prefix}.{repo_id_for(common)}"
        plist = {"Label": label, "ProgramArguments": [*entry, "runner"], "WorkingDirectory": str(checkout),
                 "RunAtLoad": True, "KeepAlive": True,
                 "StandardOutPath": str(state / "runner" / "launchd.log"), "StandardErrorPath": str(state / "runner" / "launchd.log"),
                 "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home())}}
        import plistlib
        (state / "runner").mkdir(parents=True, exist_ok=True)
        Path(args.launchd_plist).write_bytes(plistlib.dumps(plist))
        report["launchd_plist"] = f"{args.launchd_plist} (load with: launchctl bootstrap gui/$(id -u) {args.launchd_plist})"
    print(json.dumps(report, indent=1))
    return 0


def cmd_uninstall(args):
    common = find_common(os.getcwd())
    cfg = read_config(common)
    checkout = Path(cfg.get("checkout", os.getcwd()))
    hdir = hooks_dir(checkout)
    report = {}
    for name in ("reference-transaction", "pre-push"):
        path, chained = hdir / name, hdir / (name + CHAINED)
        if path.is_file() and HOOK_MARK in path.read_text(errors="replace"):
            path.unlink()
            report[name] = "removed"
            if chained.exists():
                os.replace(chained, path)
                report[name] = "removed; original hook restored"
    for key in ("stateDir", "checkout", "target", "tq", "entry", "policy", "maxAttempts", "landTimeout",
                "checkoutWaitLimit", "runtimeAllow", "registry"):
        set_config(common, key, [])
    report["config"] = f"removed (state kept at {cfg.get('stateDir')})"
    # Only this repository's own registry file: the recorded path must be <...>/repos/<its repo id>.json.
    recorded = Path(cfg["registry"]) if cfg.get("registry") else registry_path(repo_id_for(common))
    if recorded.name == f"{repo_id_for(common)}.json" and recorded.parent.name == "repos":
        if recorded.exists():
            recorded.unlink()
            report["registry"] = f"removed {recorded}"
    with contextlib.suppress(Exception):
        busy = [r["request_id"] for r in all_requests(Ctx(common, cfg)) if r["state"] not in TERMINAL]
        if busy:
            report["warning"] = f"requests still pending in the kept state and will not be processed: {busy[:5]}"
    print(json.dumps(report, indent=1))
    return 0


# ---------------------------------------------------------------- doctor

def entry_module_file(ctx: Ctx) -> tuple[Path | None, str]:
    """Where the configured interpreter finds merge_queue (only for the standard `<python> -P -m merge_queue.core`
    entry): (module file, "") or (None, problem). Other entries (wrappers) are not inspected."""
    if len(ctx.entry) != 4 or ctx.entry[1:] != self_entry()[1:]:
        return None, ""
    probe = subprocess.run([ctx.entry[0], "-P", "-c", "import merge_queue.core as m; print(m.__file__)"],
                           capture_output=True, text=True, env=clean_env(), timeout=60)
    if probe.returncode:
        return None, f"merge_queue is not importable by {ctx.entry[0]}: {probe.stderr.strip()[-200:]}"
    return Path(probe.stdout.strip()), ""


def cmd_doctor(args):
    ctx = load_ctx()
    checks = []

    def add(name, ok, detail="", fixed=False):
        checks.append({"check": name, "ok": bool(ok), "detail": detail, **({"fixed": True} if fixed else {})})

    version = git("--version", cwd=ctx.checkout).stdout.strip()
    add("git", True, version)
    tq = subprocess.run([ctx.tq, "--data-dir", str(ctx.tq_dir), "list", "--json"], capture_output=True, text=True)
    add("tq", tq.returncode == 0, f"{ctx.tq} {tq.stderr.strip()[:200]}")
    stale_rows = tq_preflight(ctx)
    add("tq_stale_rows", not stale_rows, "; ".join(stale_rows))
    add("policy", Path(ctx.policy).is_file(), str(ctx.policy))
    head = git("symbolic-ref", "-q", "HEAD", cwd=ctx.checkout, check=False).stdout.strip()
    add("checkout_branch", head == ctx.target, f"{ctx.checkout} on {head}")
    try:
        check_checkout(ctx, [])
        add("checkout_clean", True)
    except Fail as f:
        add("checkout_clean", False, f.message)
    waiting = read_json(ctx.p("notices", "checkout-wait.json"))
    add("checkout_wait", not waiting, f"{waiting['reason']} (waiting: {', '.join(waiting['requests'])}; "
                                      f"{waiting['required_action']})" if waiting else "")
    hdir = hooks_dir(ctx.checkout)
    for name in ("reference-transaction", "pre-push"):
        path = hdir / name
        add(f"hook_{name}", path.is_file() and HOOK_MARK in path.read_text(errors="replace"), str(path))
    runner_lock = FileLock(ctx.p("locks", "runner.lock"), blocking=False)
    runner_alive = not runner_lock.acquire()
    runner_lock.release()
    pending = [r for r in all_requests(ctx) if r["state"] not in TERMINAL]
    add("runner", runner_alive or not pending, "running" if runner_alive else
        ("not running; work pending (submit/launchd will start it)" if pending else "idle"))
    stuck = [r["request_id"] for r in pending if r["state"] in ("dispatched", "running") and not runner_alive
             and proc_state((r.get("tq") or {}).get("identity")) != "same"]
    add("stuck_requests", not stuck, ", ".join(stuck))
    mismatched = [r["request_id"] for r in pending if request_mismatch(ctx, r)]
    add("request_configuration", not mismatched, ", ".join(mismatched))
    unstable = [f"{name} missing: {path}" for name, path in (("policy", ctx.policy), ("interpreter", ctx.entry[0]),
                                                           ("tq", ctx.tq)) if not Path(path).exists()]
    module, import_error = entry_module_file(ctx)
    if import_error:
        unstable.append(import_error)
    unstable += install_pointer_problems(ctx.entry, module)
    add("install_pointers", not unstable, "; ".join(unstable))
    registered = read_registry(ctx.repo_id)
    add("registry", registry_matches(registered, ctx.common, {"stateDir": str(ctx.state), "checkout": str(ctx.checkout),
                                                            "target": ctx.target}), str(registry_path(ctx.repo_id)))
    missing = [r["request_id"] for r in all_requests(ctx) if r["state"] in FAILED
               and not (owner_dir(ctx, r["owner_id"]) / f"{r['request_id']}.json").exists()]
    if missing and args.recover:
        for rid in missing:
            req = load_req(ctx, rid)
            res = req["result"]
            write_json(owner_dir(ctx, req["owner_id"]) / f"{rid}.json", {
                "request_id": rid, "owner_id": req["owner_id"], "repository": req["repository"],
                "source_commit_sha": req["source_commit_sha"], "target_commit_sha": req.get("target_sha"),
                "failure_type": res.get("failure_type"), "failure_class": res.get("failure_class"),
                "message": res.get("message"), "conflicting_files": res.get("conflicting_files", []),
                "relevant_log_paths": res.get("log_paths", []), "summary": "", "required_action": res.get("required_action"),
                "created_at": res.get("finished_at")})
    add("inbox_complete", not missing or args.recover, ", ".join(missing), fixed=bool(missing and args.recover))
    integration = FileLock(ctx.p("locks", "integration.lock"), blocking=False)
    if integration.acquire():
        try:
            leftovers = list(ctx.p("work").iterdir()) if ctx.p("work").is_dir() else []
            leftovers += [p for p in ctx.p("auth").glob("*.json")] if ctx.p("auth").is_dir() else []
            if args.recover:
                cleaned = sweep_stale(ctx)  # exact owned workspaces/refs only; never prunes or touches the checkout
                remaining = [p for p in ctx.p("work").iterdir()] if ctx.p("work").is_dir() else []
                add("stale_workspaces", not remaining, f"cleaned {cleaned}; preserved {[str(p) for p in remaining]}",
                    fixed=bool(cleaned))
            else:
                add("stale_workspaces", not leftovers, ", ".join(map(str, leftovers)))
            gone = [p for p in registered_worktrees(ctx) if p.startswith(str(ctx.p("work"))) and not Path(p).exists()]
            add("registered_missing_workspaces", not gone,
                (f"{gone}: dangling Git registrations are never pruned by the queue; review `git worktree list` "
                 "and remove them deliberately") if gone else "")
        finally:
            integration.release()
    else:
        add("stale_workspaces", True, "a landing is in progress; skipped")
    ok = all(c["ok"] for c in checks)
    if args.json:
        print(json.dumps({"ok": ok, "checks": checks, "config": ctx.describe()}, sort_keys=True))
    else:
        for c in checks:
            print(f"{'ok  ' if c['ok'] else 'FAIL'} {c['check']}: {c['detail']}")
    return 0 if ok else 1


def cmd_config(args):
    print(json.dumps(load_ctx().describe(), sort_keys=True))
    return 0


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="merge-queue", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, json_flag=True):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        if json_flag:
            p.add_argument("--json", action="store_true")
        return p

    def source_args(p):
        p.add_argument("--commit", help="exact commit to submit (default: HEAD of this worktree)")
        p.add_argument("--branch", help="source branch name (default: current branch)")
        p.add_argument("--owner", help="owner id (default: $MERGE_QUEUE_OWNER, else $CODEX_THREAD_ID, "
                                       "else the worktree directory name)")
        p.add_argument("--wait", action="store_true", help="block until the request reaches a final state")
        p.add_argument("--timeout", type=float, default=7200, help="--wait limit in seconds")

    p = add("submit", cmd_submit, "queue committed work for integration into the target")
    source_args(p)
    p.add_argument("--request-id", help="caller-chosen idempotency key")
    p.add_argument("--started-at", help="when the agent started the task (ISO; informational)")
    p = add("retry", cmd_retry, "resubmit after repairing a failed request")
    p.add_argument("request_id")
    p.add_argument("--new-request-id")
    source_args(p)
    p = add("result", cmd_result, "show the outcome of a request (default: this owner's latest)")
    p.add_argument("request_id", nargs="?")
    p.add_argument("--owner")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--timeout", type=float, default=7200)
    p.add_argument("--full", action="store_true")
    add("status", cmd_status, "show queue and unacknowledged requests")
    p = add("inbox", cmd_inbox, "show pending failure notifications for this owner")
    p.add_argument("--owner")
    p.add_argument("--quiet", action="store_true", help="one line, exit 10 when something is pending (for hooks)")
    p = add("ack", cmd_ack, "acknowledge finished requests (drops them to a tombstone)", json_flag=False)
    p.add_argument("request_id", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--owner")
    p = add("logs", cmd_logs, "view optional diagnostics or set their level", json_flag=False)
    p.add_argument("-n", type=int, default=50)
    p.add_argument("--level", choices=["off", "basic", "debug"])
    add("logs-clear", cmd_logs_clear, "delete optional diagnostics only", json_flag=False)
    p = add("doctor", cmd_doctor, "diagnose configuration; --recover performs safe repairs")
    p.add_argument("--recover", action="store_true")
    add("config", cmd_config, "print resolved shared configuration", json_flag=False)
    policy_cmd = sub.add_parser("policy", help="validation policy tools")
    policy_sub = policy_cmd.add_subparsers(dest="policy_cmd", required=True)
    p = policy_sub.add_parser("check", help="validate a policy and that it routes every tracked path (JSON; exit 0/1)")
    p.set_defaults(fn=cmd_policy_check)
    p.add_argument("--repo", help="repository to check (default: the current directory)")
    p.add_argument("--policy", help="policy file (default: <repo>/.merge-queue/policy.json)")
    p = add("install", cmd_install, "write shared config, git hooks and optional launchd plist")
    p.add_argument("--checkout", help="integration checkout (default: the repository's first worktree)")
    p.add_argument("--state-dir", help="override <state root>/<repo id>")
    p.add_argument("--tq", help="path to tq (default: $PATH, then ~/.local/bin/tq)")
    p.add_argument("--policy", help="validation policy (default: <checkout>/.merge-queue/policy.json)")
    p.add_argument("--target", default="main")
    p.add_argument("--runtime-allow", action="append", metavar="PREFIX",
                   help="untracked runtime path prefix that may stay in the integration checkout")
    p.add_argument("--max-attempts", type=int, default=4)
    p.add_argument("--land-timeout", type=int, default=6600)
    p.add_argument("--checkout-wait-limit", type=int, default=14400, metavar="SECONDS",
                   help="how long a request waits for a dirty/overlapping/off-branch integration checkout before failing")
    p.add_argument("--no-hooks", action="store_true")
    p.add_argument("--allow-unstable-paths", action="store_true",
                   help="allow pointers into a linked worktree or the system temp dir (temporary installs only)")
    p.add_argument("--launchd-plist", help="also write a launchd agent plist to this path (macOS; optional)")
    p.add_argument("--launchd-label-prefix", default="local.merge-queue",
                   help="launchd label is <prefix>.<repo id> (default: local.merge-queue)")
    p.add_argument("--dry-run", action="store_true")
    add("uninstall", cmd_uninstall, "remove hooks (restoring chained ones) and config; keeps state", json_flag=False)
    p = add("runner", cmd_runner, "dispatch queued requests through tq (run under launchd)", json_flag=False)
    p.add_argument("--poll", type=float, default=1.0)
    p.add_argument("--idle-exit", type=float)
    p.add_argument("--drain", action="store_true", help="exit once nothing is pending")
    p = add("land", cmd_land, "internal: run by tq for one request", json_flag=False)
    p.add_argument("--request-id", required=True)
    p.add_argument("--repo-id", help="canonical repository id (binds a dispatch to this repository)")
    p = add("hook", cmd_hook, "internal: Git reference-transaction guard and agent SessionStart notice",
            json_flag=False)
    p.add_argument("hook_name", choices=["reference-transaction", "session-start"])
    p.add_argument("state", nargs="?")
    return parser


def cli() -> None:
    """Console-script entry point."""
    raise SystemExit(main())


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    except MqError as exc:
        print(f"merge-queue: {exc}", file=sys.stderr)
        return exc.code
    except subprocess.TimeoutExpired as exc:
        print(f"merge-queue: command timed out: {exc.cmd}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    cli()
