"""Repository hygiene: nothing personal or machine-specific may be committed.

The repository is intended for public source publication. This guards leaks through
test fixtures, docs and examples:
absolute home-directory paths and real e-mail addresses.
"""
import os
import sys
import tempfile
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "__pycache__", "dist", "build"}
sys.path.insert(0, str(ROOT / "scripts"))
from audit_publication import HOME_PATH, EMAIL, allowed_email, audit  # noqa: E402


def repository_files():
    """Tracked files, plus new files that are not ignored (so the check also works before a commit)."""
    listing = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                             capture_output=True, text=True)
    if listing.returncode == 0 and listing.stdout:
        names = [n for n in listing.stdout.split("\0") if n]
        return [ROOT / n for n in names if (ROOT / n).is_file()]
    return [p for p in ROOT.rglob("*") if p.is_file() and not SKIP_DIRS & set(p.relative_to(ROOT).parts)]


class RepoHygiene(unittest.TestCase):
    def test_no_tracked_file_contains_an_absolute_home_path_or_a_real_email(self):
        files = repository_files()
        self.assertGreater(len(files), 10, "file discovery failed")
        problems = []
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # binary content
            rel = path.relative_to(ROOT)
            for number, line in enumerate(text.splitlines(), 1):
                for match in HOME_PATH.finditer(line):
                    problems.append(f"{rel}:{number}: absolute home path")
                for match in EMAIL.finditer(line):
                    if not allowed_email(match.group(0)):
                        problems.append(f"{rel}:{number}: non-public e-mail address")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_the_detectors_catch_what_they_are_meant_to_catch(self):
        for leaked in ("/" + "Users/someone/project", "cd /" + "home/someone", "x=/" + "Users/a.b"):
            self.assertTrue(HOME_PATH.search(leaked), leaked)
        for fine in ("~/.local/state", "/tmp/x", "/usr/local/Users", "$HOME/project", "https://example.invalid/Users"):
            self.assertFalse(HOME_PATH.search(fine), fine)
        self.assertFalse(allowed_email("someone" + "@private.example.io"))
        self.assertFalse(allowed_email("someone" + "@pm" + ".me"))
        for fine in ("fixture@example.invalid", "1234+name@users.noreply.github.com", "a@example.com"):
            self.assertTrue(allowed_email(fine), fine)
        for domain in ("notexample.com", "example.com.evil.invalid", "evilnoreply.github.com"):
            self.assertFalse(allowed_email("someone@" + domain), domain)
        self.assertTrue(allowed_email("fixture@sub.example.invalid"))
        self.assertTrue(HOME_PATH.search("C:" + "\\Users\\someone\\project"))
        for username in ("First Last", "名字", " First"):
            self.assertTrue(HOME_PATH.search("/" + "Users/" + username + "/repo"))
            self.assertTrue(HOME_PATH.search("C:" + "\\Users\\" + username + "\\repo"))
        at = "@"
        self.assertEqual(EMAIL.findall(f"mail a{at}b.co, not a{at}localhost"), [f"a{at}b.co"])


class HistoryAudit(unittest.TestCase):
    def test_removed_historical_leak_and_dangling_objects_are_checked_without_values(self):
        with tempfile.TemporaryDirectory(prefix="mq-privacy-") as directory:
            root = Path(directory)

            env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

            def git(*args, input=None):
                return subprocess.run(["git", *args], cwd=root, input=input, check=True, env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

            git("init", "-q")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            git("config", "commit.gpgsign", "false")
            sample = root / "sample.txt"
            sample.write_text("safe fixture\n")
            git("add", ".")
            git("commit", "-qm", "safe")
            self.assertTrue(audit(root)["passed"])
            private = "private" + "@personal.invalid"
            git("hash-object", "-w", "--stdin", input=private.encode())
            self.assertTrue(audit(root)["passed"])
            report = audit(root, all_objects=True)
            self.assertFalse(report["passed"])
            self.assertNotIn(private, str(report))
            sample.write_text("/" + "Users/someone/private\n")
            git("add", ".")
            git("commit", "-qm", "historical fixture")
            sample.unlink()
            git("add", "-u")
            git("commit", "-qm", "remove fixture")
            report = audit(root)
            self.assertFalse(report["passed"])
            self.assertEqual({f["category"] for f in report["findings"]}, {"absolute_home_path"})
            self.assertNotIn("someone", str(report))
            git("config", "remote.origin.promisor", "true")
            with self.assertRaises(ValueError):
                audit(root)
            git("config", "--unset", "remote.origin.promisor")
            git("config", "extensions.partialClone", "origin")
            with self.assertRaises(ValueError):
                audit(root)
            git("config", "--unset", "extensions.partialClone")
            gitdir = root / ".git"
            (gitdir / "shallow").write_bytes(git("rev-parse", "HEAD").strip() + b"\n")
            with self.assertRaises(ValueError):
                audit(root)


if __name__ == "__main__":
    unittest.main()
