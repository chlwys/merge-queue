# merge-queue

A deterministic, local merge queue for Git repositories. Many people or agents work in their own
worktrees; the queue lands their committed work on one shared branch (usually `main`) one request at a
time, in first-come order, after validating the **combined** result. An optional trusted policy sends tiny
presentation changes through an inline fast lane, ahead of FIFO validation. No model is involved on the success
path, and the queue never modifies a contributor's branch or worktree.

```text
merge-queue submit        exact commit -> durable request (FIFO number) -> runner -> Block `tq`
                          -> land: detached candidate worktree (rebase onto the target)
                          -> your validation policy runs on the COMBINED candidate
                          -> fresh checkout checks -> git merge --ff-only in the integration checkout
```

What you get:

* **FIFO landing through [`tq`](https://github.com/block/agent-task-queue)** (one queue per repository and
  target branch), with durable request files, crash recovery and no duplicate publication.
* **Validation of what will actually land**: the candidate is the contributor's commits rebased onto the
  current target tip, checked by a policy file that you own. Unknown paths fail closed.
* **Local Git remains available**: ordinary commits and merges need no special environment variable.
  The `reference-transaction` guard protects active queue publication and binds its exact receipt and SHAs.
* **An optional automatic fast lane**: trusted classification and focused checks in an isolated candidate,
  with the same durable receipt, cleanliness and stale-target guards, without a `tq` task or full suites.
* **An optional MCP server** (`merge-queue-mcp`) that lets agents submit once and receive the outcome as the
  result of a single pending tool call, and lets anyone watch a request or wait for a queue to drain.

## Native macOS app

An optional lightweight menu-bar monitor shows one repository at a time, with interchangeable
Orbital and Minimal themes. See [apps/macos/README.md](apps/macos/README.md) for building and running it.

## Requirements

* Python 3.11 or newer and Git. POSIX only (file locks, process groups). Developed and tested on macOS;
  nothing macOS-specific is required except the optional launchd plist.
* [Block's `agent-task-queue`](https://github.com/block/agent-task-queue) **0.4.1** (the `tq` command,
  Apache-2.0). It is an external dependency of this project and is not vendored or modified here:

  ```sh
  uv tool install agent-task-queue==0.4.1      # puts `tq` in ~/.local/bin
  ```

## Install

Install the tool into a stable, long-lived environment (not into a task worktree or a temporary directory;
the installer refuses those because hooks and the runner would point at paths that disappear):

```sh
uv tool install git+https://github.com/chlwys/merge-queue.git
merge-queue --help
merge-queue-mcp --help
```

The dependencies (`fastmcp==4.1.3`, `mcp==2.3.0`, `watchfiles==1.3.0`) are installed with the package.
The core imports only the Python standard library for ordinary commands and immediate results; a pending
CLI `--wait` lazily loads `watchfiles` for native filesystem notifications, with no polling fallback.

## Quickstart

1. Write a validation policy in the integration checkout (see [docs/policy.md](docs/policy.md) and
   [examples/policy.example.json](examples/policy.example.json)) and check it covers every tracked file:

   ```sh
   cd <repo>                                  # the integration checkout, on its target branch
   mkdir -p .merge-queue && cp <this-repo>/examples/policy.example.json .merge-queue/policy.json
   merge-queue policy check                   # exit 0 when valid and complete
   ```

2. Install the queue for this repository (writes `mergequeue.*` into the repository's Git config, two Git
   hooks and a registry entry; see [docs/operations.md](docs/operations.md)):

   ```sh
   merge-queue install --runtime-allow data/  # optional: untracked runtime paths that may stay in the checkout
   merge-queue doctor
   ```

3. From any worktree of the repository, commit and submit:

   ```sh
   merge-queue submit --wait                  # exit 0 landed, 10 owner action, 11 operator action, ...
   ```

   Failures come back with the conflicting files or the validation log; repair, commit, and run
   `merge-queue retry <request-id>`.

A runner is started on demand by `submit`, or you can supervise one yourself (`merge-queue runner`, for
example under launchd or systemd; see the operations guide).

## Safety model

* Queue publications move the target only by `git merge --ff-only` of a candidate that was validated against the exact
  tip it extends, with fresh checks of the integration checkout (branch, clean tracked files, no clashing
  untracked or ignored paths) immediately before publication.
* The Git hook binds every ref transaction of a publication to that publication (authorised old and new
  SHAs, held integration lock, expiry). Ordinary local target updates are allowed when no queue publication
  holds the lock. Remote target pushes still require `MERGE_QUEUE_PUSH_APPROVED=1`; the deprecated
  `MERGE_QUEUE_MANUAL` flag grants no push approval. Git may stage a merge before a hook refusal; this guard
  does not serialize arbitrary checkout writes. Inspect the checkout after any rejected merge.
* Policy is read from the integration checkout, never from a candidate, so a change cannot weaken its own
  gate.
* Process groups are signalled only after pid, start time and argv are verified; nothing is ever deleted
  from `tq`'s database; the queue never resets the integration checkout.

## Documentation

| Document | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Request lifecycle, state layout, locks, publication, recovery, exit codes |
| [docs/policy.md](docs/policy.md) | Policy file, path routing, the partial-testing planner protocol, `policy check` |
| [docs/mcp.md](docs/mcp.md) | The global MCP server: tools, events, client setup |
| [docs/operations.md](docs/operations.md) | Install, state root, registry, runner supervision, direct Git, rollback |

## Development

```sh
uv sync --locked
uv run python -m unittest discover -s tests
```

The suite needs the real `tq` (0.4.1) on `PATH` or at `~/.local/bin/tq` (or `MERGE_QUEUE_TQ`). Every test
uses temporary fixture repositories with their own HOME, state root and `tq` data directory; nothing touches
a real installation.

See [CONTRIBUTING.md](CONTRIBUTING.md) for privacy checks and contribution guidance, and
[SECURITY.md](SECURITY.md) for vulnerability reporting.

## Credits and license

Scheduling is done by Block's [`agent-task-queue`](https://github.com/block/agent-task-queue) (`tq`),
Apache-2.0, used as an unmodified external command-line tool. This project is independent of Block.

Licensed under the MIT License; see [LICENSE](LICENSE).
