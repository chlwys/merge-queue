#!/usr/bin/env python3
"""Report home paths and non-placeholder emails without printing matched values.

Checks the worktree and all ref-reachable blobs, commit messages/identities and tags.
Use --all-objects to include reflog-only and dangling local objects. Secret scanning
is a separate Gitleaks check; this is not a general personal-information detector.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess

HOME_PATH = re.compile(
    r"(?<![\w.~-])/(?:Users|home)/[^/\\\r\n\"'<>]+|[A-Za-z]:[\\/]Users[\\/][^/\\\r\n\"'<>]+")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
ALLOWED_EMAIL_DOMAINS = ("example.invalid", "example.com", "example.org", "example.net")
PUBLIC_EMAIL_DOMAINS = ("noreply.github.com", "users.noreply.github.com")
METADATA_EMAILS = {"noreply" + "@anthropic.com"}


def allowed_email(address):
    domain = address.rsplit("@", 1)[1].lower()
    return (any(domain == suffix or domain.endswith("." + suffix) for suffix in ALLOWED_EMAIL_DOMAINS)
            or domain in PUBLIC_EMAIL_DOMAINS)


def inspect_text(data, location, metadata=False):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [], True
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        if HOME_PATH.search(line):
            findings.append({"location": location, "line": number, "category": "absolute_home_path"})
        if any(not allowed_email(m.group()) and not (metadata and m.group() in METADATA_EMAILS)
               for m in EMAIL.finditer(line)):
            findings.append({"location": location, "line": number, "category": "non_public_email"})
    return findings, False


def audit(root, all_objects=False):
    root = Path(root)

    def git(*args, input=None):
        return subprocess.run(["git", *args], cwd=root, input=input, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=dict(os.environ, GIT_NO_LAZY_FETCH="1", GIT_OPTIONAL_LOCKS="0")).stdout

    if git("rev-parse", "--is-shallow-repository").strip() != b"false":
        raise ValueError("Full history is required; use a complete clone (CI fetch-depth: 0).")
    # A history audit must not silently download promised objects or accept gaps.
    for name in git("config", "--name-only", "--list").decode().splitlines():
        if name.lower() == "extensions.partialclone" or (
                name.lower().startswith("remote.") and name.lower().endswith(".promisor")
                and git("config", "--get", name).strip().lower() == b"true"):
            raise ValueError("A full clone without partial/promisor configuration is required.")
    head = git("rev-parse", "HEAD").decode().strip()
    refs = git("for-each-ref", "--format=%(refname)").decode().splitlines()
    if all_objects:
        records = git("cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)")
    else:
        ids = [line.split(b" ", 1)[0] for line in git("rev-list", "--objects", "--all").splitlines()]
        records = git("cat-file", "--batch-check=%(objectname) %(objecttype)", input=b"\n".join(ids) + b"\n")
    objects = [line.decode().split() for line in records.splitlines()]
    if any(kind not in ("blob", "tree", "commit", "tag") for _, kind in objects):
        raise ValueError("An object is missing or unreadable; audit is incomplete.")
    counts = Counter(kind for _, kind in objects)
    findings, binaries = [], []
    for oid, kind in objects:
        if kind not in ("blob", "commit", "tag"):
            continue
        location = kind + ":" + oid
        found, binary = inspect_text(git("cat-file", kind, oid), location, kind in ("commit", "tag"))
        findings.extend(found)
        if binary:
            binaries.append(location)
    names = git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0")
    worktree_files = 0
    for raw in filter(None, names):
        name = os.fsdecode(raw)
        path = root / name
        if not path.is_file() and not path.is_symlink():
            continue
        worktree_files += 1
        data = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
        found, binary = inspect_text(data, "worktree:" + name)
        findings.extend(found)
        if binary:
            binaries.append("worktree:" + name)
    return {"head": head, "scope": "all_local_objects" if all_objects else "all_refs",
            "refs": refs, "objects": dict(counts), "worktree_files": worktree_files,
            "binary_objects_or_files": binaries, "findings": findings,
            "passed": not findings and not binaries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--all-objects", action="store_true")
    args = parser.parse_args()
    try:
        result = audit(args.repo, args.all_objects)
    except (OSError, ValueError, subprocess.CalledProcessError):
        print(json.dumps({"passed": False, "error": "Audit incomplete: require a readable full Git checkout."}))
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
