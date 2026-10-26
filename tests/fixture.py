"""Shared fixtures for the merge-queue test suites.

Every scenario runs against temporary fixture Git repositories with the real Block `tq` (pinned 0.4.1)
scheduling the landings. Nothing here touches a real repository, a real state directory or a real HOME:
each fixture has its own repository, HOME, state root, tq data directory and validation policy, and the
fixture environment drops GIT_*, MERGE_QUEUE_*, TASK_QUEUE_*, XDG_* and agent session variables.
"""
import importlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CORE_FILE = SRC / "merge_queue" / "core.py"
TQ = os.environ.get("MERGE_QUEUE_TQ") or shutil.which("tq") or str(Path.home() / ".local/bin/tq")
MQ_CMD = [sys.executable, "-P", "-m", "merge_queue.core"]  # the installed entry form, run from the source tree

UNIT_POLICY = {
    "version": 1,
    "rules": [{"match": ["*.py", "tests/*"], "checks": ["unit"]}, {"match": ["docs/*"], "checks": []}],
    "checks": {"unit": {"argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                        "env": {"PYTHONPATH": ".", "PYTHONDONTWRITEBYTECODE": "1"}, "timeout": 60}},
}


def load_module():
    """The queue core as a module object (fresh import of the source tree)."""
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    return importlib.import_module("merge_queue.core")


class Fixture:
    """A primary repository, an installed queue and helpers to act as agents."""

    def __init__(self, policy=None, install_args=(), pre_install=None, tq=None, state_home=None):
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix="mq fixture ")))
        self.primary, self.state = self.root / "primary", self.root / "state"
        self.runners = []
        (self.root / "gitconfig").write_text("[user]\n\tname = Fixture\n\temail = fixture@example.invalid\n"
                                             "[init]\n\tdefaultBranch = main\n[advice]\n\tdetachedHead = false\n")
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("GIT_", "MERGE_QUEUE_", "TASK_QUEUE_", "CODEX_", "CLAUDE", "XDG_"))}
        self.state_home = Path(state_home) if state_home else self.root / "state-home"  # isolated state root (registry: repos/)
        self.env.update(GIT_CONFIG_GLOBAL=str(self.root / "gitconfig"), GIT_CONFIG_NOSYSTEM="1",
                        HOME=str(self.root), TASK_QUEUE_POLL_WAITING="0.1", MERGE_QUEUE_NO_AUTORUNNER="1",
                        MERGE_QUEUE_BACKOFF="0.2", PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(SRC),
                        MERGE_QUEUE_STATE_HOME=str(self.state_home))
        self.primary.mkdir()
        self.git("init", "-q")
        files = {"lib.py": "def value():\n    return 1\n", "shared.py": "TITLE = 'base'\n",
                 ".gitignore": "__pycache__/\nignored/\n",
                 "tests/test_lib.py": "import unittest\nfrom lib import value\n\n\nclass T(unittest.TestCase):\n"
                                      "    def test_value(self):\n        self.assertEqual(value(), 1)\n",
                 "tests/test_sleep.py": "import time\nimport unittest\n\n\nclass S(unittest.TestCase):\n"
                                        "    def test_sleep(self):\n        time.sleep(0.6)\n"}
        for name, text in files.items():
            self.write(self.primary, name, text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.policy_path = self.root / "policy.json"
        self.set_policy(policy or UNIT_POLICY)
        if pre_install:
            pre_install(self)
        self.mq("install", "--state-dir", str(self.state), "--tq", tq(self) if tq else TQ, "--policy", str(self.policy_path),
                "--checkout", str(self.primary), "--max-attempts", "3", "--allow-unstable-paths", *install_args,
                check=True)

    # -- plumbing
    def repo_id(self):
        return load_module().repo_id_for(Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir")
                                              .stdout.strip()).resolve())

    def stable_env(self, parent=None):
        """Environment that runs the tool from a private copy of the package in `parent` (default: a plain directory
        under the fixture root) with no system-temp roots: neither temporary nor inside a linked worktree."""
        parent = Path(parent) if parent else self.root / "stable-pkg"
        shutil.copytree(SRC / "merge_queue", parent / "merge_queue", ignore=shutil.ignore_patterns("__pycache__"),
                        dirs_exist_ok=True)
        return dict(self.env, PYTHONPATH=str(parent), MERGE_QUEUE_TEMP_DIRS="")

    def write(self, base: Path, name: str, text: str):
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def set_policy(self, policy):
        self.policy_path.write_text(json.dumps(policy))

    def git(self, *args, cwd=None, check=True, env=None):
        result = subprocess.run(["git", *args], cwd=cwd or self.primary, env=env or self.env, text=True,
                                capture_output=True)
        if check and result.returncode:
            raise AssertionError(f"git {args} failed: {result.stderr}")
        return result

    def mq(self, *args, cwd=None, env=None, check=False, input=None):
        result = subprocess.run([*MQ_CMD, *args], cwd=cwd or self.primary, text=True,
                                capture_output=True, env=env or self.env, timeout=180, input=input)
        if check and result.returncode:
            raise AssertionError(f"merge-queue {args} -> {result.returncode}\n{result.stdout}\n{result.stderr}")
        return result

    def mqj(self, *args, **kwargs):
        result = self.mq(*args, "--json", **kwargs)
        return result, (json.loads(result.stdout) if result.stdout.strip().startswith(("{", "[")) else None)

    def agent(self, name, files, base="main", force=()):
        path = self.root / f"wt-{name}"
        self.git("worktree", "add", "-q", "-b", f"agent/{name}", str(path), base)
        for file, text in files.items():
            self.write(path, file, text)
        self.git("add", "-A", cwd=path)
        if force:
            self.git("add", "-f", *force, cwd=path)
        self.git("commit", "-q", "-m", name, cwd=path)
        return path, self.git("rev-parse", "HEAD", cwd=path).stdout.strip()

    def submit(self, wt, rid, *extra):
        result, data = self.mqj("submit", "--request-id", rid, *extra, cwd=wt)
        return result, data

    def main(self):
        return self.git("rev-parse", "main").stdout.strip()

    def subjects(self):
        return self.git("log", "--format=%s", "main").stdout.split("\n")[:-1]

    def req(self, rid):
        path = self.state / "requests" / f"{rid}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def start_runner(self, **env):
        run_env = dict(self.env, **env)
        log = open(self.root / f"runner-{len(self.runners)}.log", "w")
        proc = subprocess.Popen([*MQ_CMD, "runner", "--poll", "0.1"], cwd=self.primary,
                                env=run_env, stdout=log, stderr=subprocess.STDOUT, text=True)
        log.close()
        self.runners.append(proc)
        return proc

    def stop_runner(self, proc, sig=signal.SIGTERM):
        if proc.poll() is None:
            proc.send_signal(sig)
            proc.wait(timeout=20)

    def wait_for(self, predicate, timeout=60, what="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        raise AssertionError(f"timed out waiting for {what}\n{self.debug()}")

    def wait_states(self, ids, states=("landed", "conflict", "validation_failed", "blocked", "infra_failed"), timeout=90):
        self.wait_for(lambda: all((self.req(i) or {}).get("state") in states for i in ids), timeout,
                      f"{ids} to reach {states}")
        return {i: self.req(i) for i in ids}

    def settled(self, timeout=30):
        """No landing is in flight: lock free, no attempt record, no candidate workspace."""
        import fcntl

        def quiet():
            lock = self.state / "locks" / "integration.lock"
            if not lock.exists():
                return True
            fd = os.open(lock, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            finally:
                os.close(fd)
            return not list((self.state / "attempts").glob("*")) and not list((self.state / "work").glob("*"))
        self.wait_for(quiet, timeout, "the integration to settle")

    def debug(self):
        parts = []
        for path in sorted((self.state / "requests").glob("*.json")):
            data = json.loads(path.read_text())
            parts.append(f"{data['request_id']}: {data['state']}/{data['phase']} attempts={data['attempts']} "
                         f"result={data['result']} last={data.get('last_failure')}")
        for path in sorted((self.state / "runner").glob("*.log")):
            parts.append(f"--- {path.name}\n{path.read_text()[-1500:]}")
        for path in sorted(self.root.glob("runner-*.log")):
            parts.append(f"--- {path.name}\n{path.read_text()[-1500:]}")
        return "\n".join(parts)

    def tq_events(self):
        path = self.state / "tq" / "agent-task-queue-logs.json"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def worktrees(self):
        out = self.git("worktree", "list", "--porcelain").stdout
        return [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree ")]

    def inbox(self, owner):
        directory = self.state / "inbox" / owner
        return [json.loads(p.read_text()) for p in sorted(directory.glob("*.json"))] if directory.is_dir() else []

    def refs(self):
        return self.git("for-each-ref", "--format=%(refname)", "refs/merge-queue").stdout.split()

    def close(self):
        for proc in self.runners:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        info = self.state / "runner" / "runner.json"
        if info.exists():  # a runner auto-started by `submit`
            try:
                os.kill(json.loads(info.read_text())["pid"], signal.SIGKILL)
            except (OSError, ValueError):
                pass
        out = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True).stdout
        for line in out.splitlines():
            pid, _, args = line.strip().partition(" ")
            if str(self.root) in args and int(pid) != os.getpid():
                try:
                    os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
                except OSError:
                    pass
        shutil.rmtree(self.root, ignore_errors=True)
