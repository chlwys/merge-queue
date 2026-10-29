# Contributing

Use a task branch or worktree and submit a focused pull request. Keep queue lifecycle,
publication guards and receipt ownership deterministic. Include a synthetic reproduction
and focused regression coverage for behavior changes.

## Development checks

Python 3.11+, Git, uv, and Block's `agent-task-queue` 0.4.1 are required:

```sh
uv sync --locked
uv tool install agent-task-queue==0.4.1
uv run python -m unittest discover -s tests
python3 scripts/audit_publication.py
```

The integration fixtures create their own temporary repositories and state. Do not test
against a personal queue or attach its raw registry, receipts or validation logs. These
can contain private repository paths, branch names, commit subjects and command output.
Replace them with synthetic examples before sharing.

For the optional macOS app, follow [its build and validation guide](apps/macos/README.md).
Source publication does not imply a signed or notarized binary release.

## Privacy checks

CI scans full Git history and the working tree for absolute home paths and non-placeholder
email addresses, and scans history with Gitleaks. The GitHub noreply attribution already
in this repository is intentional. Synthetic addresses must use a reserved example domain.
The audit prints finding locations and categories, without printing matched values.
Binary files require separate inspection and fail the text audit unless their exact SHA-256
is recorded in `scripts/audit_publication.py`. The README screenshots are inspected native
captures with synthetic queue data and nonessential metadata removed. Updating one requires
inspecting the new image and metadata before recording its digest; altered or unrelated
binaries still fail in both the worktree and Git history.

Run `python3 scripts/audit_publication.py --all-objects` for a local audit that also covers
reflog-only and dangling objects. Gitleaks is a separate dependency. With Gitleaks 8.30.1 installed, scan the complete history locally:

```sh
gitleaks git --log-opts="--all --full-history" --redact --no-banner .
```

The Python check refuses shallow or partial/promisor clones, does not fetch missing objects,
and fails if binary content needs review. It detects home paths and non-placeholder emails;
Gitleaks complements it with secret detection. A passing scan is evidence for those detectors,
not a guarantee that arbitrary personal prose is absent. Keep scan output and one-off review
notes outside commits and release assets.
