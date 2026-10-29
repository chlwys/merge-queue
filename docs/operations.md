# Operations

## Install

```sh
uv tool install agent-task-queue==0.4.1            # tq, found via --tq, $PATH or ~/.local/bin/tq
uv tool install <path-or-git-url-of-this-repository>

cd <integration checkout>                          # the worktree where the target branch is checked out
merge-queue policy check                           # your policy: valid, covers every tracked file
merge-queue install [--target main] [--policy PATH] [--runtime-allow data/] [--state-dir DIR]
merge-queue doctor
```

`install` writes the `mergequeue.*` configuration into the repository's common Git config, installs the
`reference-transaction` and `pre-push` hooks (chaining any hook that already exists), and registers the
repository. It refuses to:

* run when the integration checkout is not on the target branch;
* record an interpreter or `merge_queue` module that lives in a **linked Git worktree** (of any repository)
  or under the **system temporary directory** (`--allow-unstable-paths` overrides, for temporary installs
  only). Hooks, the runner and the configuration reference the installed tool (`<python> -P -m
  merge_queue.core`), so they must not point at something that disappears with a task worktree;
* retarget the state directory, checkout or branch while requests are pending;
* accept `--runtime-allow` globs, `.`, `..` or absolute paths: it names one specific relative untracked
  runtime path (for example `data/`) that may live in the integration checkout. An incoming change that
  overlaps it, or any ignored file, still blocks.

`--policy` defaults to `<checkout>/.merge-queue/policy.json`; a missing file is an install error. Use
`--dry-run` to print the configuration without writing anything. Re-running `install` updates it in place.

`merge-queue doctor [--recover] [--json]` checks Git and `tq`, stale `tq` rows, the policy file, the
checkout branch and cleanliness, the hooks, the runner, stuck or mismatched requests, install pointers
(interpreter, importability of the module, stability of both), the registry entry, the inbox, and leftover
workspaces. `--recover` performs only safe repairs: dead, identity-verified queue leftovers and missing inbox
entries.

## State root and registry

State root: `$MERGE_QUEUE_STATE_HOME`, else `${XDG_STATE_HOME:-~/.local/state}/merge-queue`
(absolute paths only). A repository's state lives in `<root>/<repo-id>/` unless `install --state-dir`
overrides it; see [architecture.md](architecture.md#state-layout) for the contents.

### Registry

`install` writes `<root>/repos/<repo-id>.json` atomically:

```json
{"repo_id": "...", "common_dir": "...", "checkout": "...", "state_dir": "...",
 "target": "refs/heads/main", "installed_at": "2026-01-01T00:00:00.000000+00:00"}
```

A custom `--state-dir` is still registered under the default root's `repos/`. `uninstall` removes only that
repository's entry. The [MCP server](mcp.md) accepts a repository only when this entry agrees with the
repository's own configuration, and uses the directory to list queues. If you change `MERGE_QUEUE_STATE_HOME`
between install and use, run `install` again under the new root.

## Running the runner

The runner dispatches normal requests to `tq` and recovers detached inline executors. `submit` starts one on demand (it exits after 120 idle
seconds), so a supervisor is optional but recommended on a busy machine:

```sh
merge-queue runner                  # long-running; waits for the lock if another runner is active
merge-queue runner --drain          # exit once nothing is pending
merge-queue runner --idle-exit 300
```

Run it under any supervisor (systemd user unit, launchd, tmux, a process manager). The runner holds a lock,
so a second one waits instead of exiting, which keeps a restarting supervisor from looping. launchd is
entirely optional:

```sh
merge-queue install --launchd-plist ~/Library/LaunchAgents/local.merge-queue.<repo-id>.plist \
                    [--launchd-label-prefix org.example.mq]
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.merge-queue.<repo-id>.plist
```

The plist label is `<prefix>.<repo-id>` (default prefix `local.merge-queue`); the tool writes the file but
never loads it. It runs `<python> -P -m merge_queue.core runner` with `RunAtLoad` and `KeepAlive`, and the
`PATH` of the installing shell. Make sure that `PATH` includes everything your checks need (for example the
directory of `git`, `uv` or `npm`): a supervisor-run runner does not read your shell profile.

## Daily use

| Command | Use |
|---|---|
| `submit [--wait] [--request-id ID] [--owner ID]` | queue `HEAD` of this worktree (clean, committed); idempotent by id and by exact SHA |
| `retry <id> [--wait] [--new-request-id ID]` | resubmit after repair; goes to the FIFO tail; an unchanged commit is refused for owner failures |
| `status`, `result [id] [--wait]` | diagnostics or one blocking fallback wait, never a polling loop |
| `inbox [--quiet]`, `ack <id>\|--all` | failure notices (conflict files, log paths, required action); `ack` keeps only a tombstone |
| `logs [--level off\|basic\|debug] [-n N]`, `logs-clear` | optional diagnostics; never removes requests, inbox entries or failure evidence |
| `doctor [--recover]`, `config`, `policy check` | diagnosis, resolved configuration, policy validation |
| `uninstall` | remove hooks (restoring chained ones), configuration and registry entry; state is kept |

Owner id: `--owner`, else `$MERGE_QUEUE_OWNER`, else `$CODEX_THREAD_ID`, else the worktree directory name.

For agent waiting, prefer the MCP pending call described in [docs/mcp.md](mcp.md). Without MCP, run one
`merge-queue result <id> --wait --timeout 7200` invocation. It waits on native receipt events, not periodic
receipt reads. A command host yielding a session handle leaves that invocation pending; wait for host
completion instead of issuing another command or repeated short wrapper waits. An actual timeout or
interruption stops observation only: reattach the same receipt, never resubmit.

## Direct local Git and remote publication

Ordinary local commits, fast-forward merges, merge commits, and ref updates need no special variable:

```sh
git commit -m "My changes"
git merge --ff-only topic
```

Agents may use an authorized direct workflow and record the reason for bypassing queued validation. The
hook does not require a queue receipt for local Git and permits recovery with broken queue configuration.
Direct target updates are refused while a queue publication holds the integration lock; queue publications
always retain exact receipt, SHA, expiry, and lock checks. Git and chained hooks still run. The deprecated
`MERGE_QUEUE_MANUAL` flag has no authorization effect and is stripped from queue child environments.

Pushing the target branch is refused unless `MERGE_QUEUE_PUSH_APPROVED=1` is set for that
push: publishing to a remote is a separate decision from landing locally.

Avoid editing the integration checkout while a landing runs (check `status` first). For a long manual
session, let active work finish and stop the runner; afterwards leave a clean checkout on the target branch
and start it again. Pending requests remain durable and are revalidated against the changed target.

An installed `fast_path` policy automatically routes eligible tiny presentation changes inline. The
`submit`/`retry` CLI returns after acceptance unless `--wait` is requested, for both lanes.
The executor runs in a separate process session, so disconnecting an observer leaves
the same receipt running. Reconnect with `result <id> --wait` or MCP `wait_for_result`, never resubmit to
recover an unknown outcome. Only configured focused checks run; planner errors enter the normal queue.

### Upgrading to the inline-capable runner

Do not enable fast-path policy while an older runner/helper remains active: older stale cleanup assumes
exclusive candidate ownership. Drain existing requests with one `wait_for_queue_empty` call (or a single
`runner --drain` when no runner is active), then stop the owning supervisor/runner with SIGTERM and verify
its exit. SIGTERM on the runner preserves in-flight `tq` children; draining first avoids mixed-version
helpers. Upgrade the stable installation, re-run `install` to replace hooks, restart the runner, and only
then enable fast-path policy. Reconnect any saved receipt; never delete queue state or running jobs.

## Startup notice (optional)

`merge-queue hook session-start` reads an agent client's SessionStart JSON (`cwd`, `session_id`) and, only when
that agent has pending failures, prints `hookSpecificOutput.additionalContext` (exit 0); it is silent
otherwise, outside a repository, and when not installed. Register it as a separate SessionStart command in
your agent client's hook configuration using the absolute path of the console script
(`<tool-bin>/merge-queue hook session-start`). Nothing wakes a finished session: the notice appears on the
owner's next session, and `inbox` always works.

## Environment variables

| Variable | Effect |
|---|---|
| `MERGE_QUEUE_STATE_HOME` | state root (absolute) |
| `MERGE_QUEUE_OWNER`, `CODEX_THREAD_ID` | owner id sources (see above) |
| `MERGE_QUEUE_MANUAL` | deprecated; grants no local or remote authorization |
| `MERGE_QUEUE_PUSH_APPROVED=1` | allow pushing the target for that push |
| `MERGE_QUEUE_NO_AUTORUNNER` | `submit` does not start an on-demand runner |
| `MERGE_QUEUE_PUBLISH_TIMEOUT` | seconds `git merge --ff-only` may run (default 300) |
| `MERGE_QUEUE_LAND_BUDGET` | override the landing time budget in seconds |
| `MERGE_QUEUE_CHECKOUT_WAIT_DELAY` | seconds before a request waiting for the checkout is retried (default 30) |
| `MERGE_QUEUE_BACKOFF` | base of the requeue backoff in seconds (default 5) |
| `MERGE_QUEUE_RUNNER_LOCK_POLL` | seconds a waiting runner re-checks the runner lock (default 5) |
| `MERGE_QUEUE_TEMP_DIRS` | replaces the system temp roots used by the stable-path check (test seam) |
| `MERGE_QUEUE_FAULT` | fault injection used by the tests; never set it in production |

## Troubleshooting

* **`dispatch blocked`** in `status`/`doctor`: a stale `tq` row names a pid this queue cannot verify as its
  own. Check the pid by hand; if unrelated, an operator clears the row with `tq` itself (the queue never
  edits `tq`'s database or runs `tq clear`). Dispatch resumes once it is gone.
* **`WAITING FOR CHECKOUT`**: the integration checkout is dirty, has unrecognised untracked paths, overlaps
  the incoming change or is on another branch. Resolve the listed paths; requests resume by themselves, or
  fail after the wait limit.
* **`unknown_scope`**: add the path to the policy (`merge-queue policy check` lists uncovered paths), land
  that change, then `merge-queue retry <id>`.
* **`publication_checkout_dirty`**: a rejected fast-forward left staged changes. Inspect `git status` in the
  integration checkout; the exact inverse of the staged fast-forward is `git read-tree -m -u <candidate>
  <head>`, to be used only when nobody is editing there. Then `merge-queue retry <id>`.
* **`install_pointers` fails in `doctor`**: the configured interpreter or module moved or is unstable;
  reinstall the tool into a stable environment and run `merge-queue install` again.

## Rollback

1. Make sure nothing is pending or active (`merge-queue status`; wait for or resolve every request).
2. Stop the runner and unload any supervisor entry (`launchctl bootout gui/$(id -u) <plist>`).
3. `merge-queue uninstall`: chained hooks are restored, `mergequeue.*` configuration and the registry entry
   are removed. State and data are kept.
4. Remove any SessionStart hook entry you added.
