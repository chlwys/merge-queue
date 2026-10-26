"""Repository hygiene: nothing personal or machine-specific may be committed.

The repository is public. This guards the two leaks that creep in through test fixtures, docs and examples:
absolute home-directory paths and real e-mail addresses.
"""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "__pycache__", "dist", "build"}
HOME_PATH = re.compile(r"(?<![\w.~-])/(?:Users|home)/[A-Za-z0-9._-]+")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
ALLOWED_EMAIL_DOMAINS = ("example.invalid", "example.com", "example.org", "example.net")


def repository_files():
    """Tracked files, plus new files that are not ignored (so the check also works before a commit)."""
    listing = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                             capture_output=True, text=True)
    if listing.returncode == 0 and listing.stdout:
        names = [n for n in listing.stdout.split("\0") if n]
        return [ROOT / n for n in names if (ROOT / n).is_file()]
    return [p for p in ROOT.rglob("*") if p.is_file() and not SKIP_DIRS & set(p.relative_to(ROOT).parts)]


def allowed_email(address: str) -> bool:
    domain = address.rsplit("@", 1)[1].lower()
    return domain.endswith(ALLOWED_EMAIL_DOMAINS) or domain.endswith("noreply.github.com")


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
                    problems.append(f"{rel}:{number}: absolute home path {match.group(0)!r}")
                for match in EMAIL.finditer(line):
                    if not allowed_email(match.group(0)):
                        problems.append(f"{rel}:{number}: e-mail address {match.group(0)!r}")
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
        at = "@"
        self.assertEqual(EMAIL.findall(f"mail a{at}b.co, not a{at}localhost"), [f"a{at}b.co"])


if __name__ == "__main__":
    unittest.main()
