# Architecture

## Overview

```text
submit  ->  requests/<id>.json (state=queued, seq=N)  ->  runner  ->  tq run -q merge-<repo-id>-<target>
                                                                         |
                                                       merge-queue land --request-id <id>  (one at a time)
                                                         1. prepare   detached worktree, rebase onto target
                                                         2. validate  policy checks on the combined candidate
                                                         3. publish   git merge --ff-only in the checkout
```

* **Submit** pins the exact source commit (`refs/merge-queue/source/<id>`), reserves the next FIFO number
  durably and writes the request. It is idempotent by request id and by exact SHA.
* **The runner** is a thin durability layer. For each `queued` request it persists its intent
  (`dispatched`) *before* launching `tq run`, records the `tq` process identity, adopts still-running `tq`
  processes after a restart and requeues requests whose `tq` or landing process vanished (bounded retries).
  It never schedules and never rewrites a later state.
* **`tq`** (Block's `agent-task-queue` 0.4.1, an external command) provides the FIFO and mutual exclusion:
  one queue per repository and target branch, named `merge-<repo-id>-<target>`, with one landing at a time.
  The queue only reads the public `tq list --json`; it never edits `tq`'s database or runs `tq clear`.
* **`land`** is what `tq` runs for one request. It prepares, validates and publishes under the
  integration lock, then records the outcome.

## Repository identity and configuration

The repository id is the first 12 hex characters of the SHA-256 of the real common Git directory. Every
worktree of a repository therefore resolves the same queue. Configuration lives in the repository's common
Git config (`mergequeue.*`), written by `merge-queue install`:

| Key | Meaning |
|---|---|
| `stateDir` | where durable state lives (see below) |
| `checkout` | the integration checkout: the worktree where the target branch is checked out |
| `target` | the target ref, `refs/heads/<name>` (default `main`) |
| `tq` | absolute path of the `tq` executable |
| `entry` | argv prefix of the installed tool: `<interpreter> -P -m merge_queue.core` |
| `policy` | validation policy file (default `<checkout>/.merge-queue/policy.json`) |
| `maxAttempts`, `landTimeout`, `checkoutWaitLimit` | retry and time limits |
| `runtimeAllow` (multi-valued) | untracked runtime paths allowed to stay in the checkout |
| `registry` | the registry file written for this repository (removed again by `uninstall`) |

`entry` points at the installed interpreter and module, never at a path inside a checkout. The installer
refuses to record a module or interpreter that lives in a *linked* Git worktree (of any repository) or
under the system temporary directory (override: `--allow-unstable-paths`, for temporary installs only);
`merge-queue doctor` reports the same condition. The `-P` flag keeps the current directory off `sys.path`,
so a project's own `merge_queue` directory cannot shadow the tool when hooks run inside that project.

## State layout

The state root is `$MERGE_QUEUE_STATE_HOME`, else `${XDG_STATE_HOME:-~/.local/state}/merge-queue`. Each
repository gets `<root>/<repo-id>/` (override with `install --state-dir`), and installed repositories are
listed in `<root>/repos/<repo-id>.json` (see the [operations guide](operations.md#registry)).

| Path under the state directory | Content |
|---|---|
| `requests/<id>.json` | the durable request record (state, phase, attempts, validation results, result) |
| `tombstones/<id>.json` | minimal record kept after `ack` (landed SHA or failure type) |
| `inbox/<owner>/<id>.json` | failure notices for the owner: conflicting files, log paths, required action |
| `evidence/<id>/` | validation, conflict and publication logs of unfinished or failed requests |
| `locks/` | `integration.lock`, `runner.lock`, per-request and submit locks |
| `attempts/`, `auth/`, `work/` | live landing identity, publication authorisation, candidate worktrees |
| `runner/` | runner and `tq` logs, `runner.json` |
| `notices/` | `stale-tq.json` (dispatch blocked), `checkout-wait.json` |
| `diagnostics/`, `diagnostics.level` | optional structured event log (`off`, `basic`, `debug`) |
| `mcp-receipts/<id>.json` | MCP acceptance intents (written by the owner-bound MCP tools only) |
| `tq/` | `tq`'s own data directory for this repository |
| `seq` | FIFO counter (recoverable from the surviving requests if lost) |

All writes are atomic (temp file, `fsync`, rename, directory `fsync`). Evidence and `refs/merge-queue/*`
pins are removed on landing or acknowledgement.

## Request states

`queued -> dispatched -> running -> (landed | conflict | validation_failed | blocked | infra_failed)`;
a retry marks the old request `superseded`. Live transitions only go forward, except requeue back to
`queued`; a terminal state is never rewritten.

| State / type | Who acts | Meaning |
|---|---|---|
| `conflict` | owner | rebasing onto the target conflicts; files are listed in the inbox |
| `validation_failed` | owner | a check failed on the combined candidate; the log is in the inbox |
| `blocked: merge_commits` | owner | the history contains merges and is not based on the current target |
| `blocked: unknown_scope`, `policy_invalid`, `config_mismatch` | operator | policy does not cover the paths (fail closed), is unreadable, or the queue was reconfigured mid-flight |
| `blocked: checkout_*` | operator | the integration checkout stayed dirty, untracked, overlapping or on the wrong branch for the whole wait limit |
| `blocked: publication_*` | operator | Git rejected the fast-forward, or the checkout changed between the final check and publication |
| `infra_failed` | operator | bounded retries exhausted (setup, planner or tool failures, `tq` timeouts) |

CLI exit codes of `submit --wait`, `result` and `retry --wait`: `0` landed, `10` owner action, `11` operator
action, `12` superseded (replaced by a retry; the result names `superseded_by`), `20` infrastructure,
`30` still pending, `3` not installed, `2` usage error, `4` conflicting request state.

## Landing in detail

1. **Prepare.** `git worktree add --detach` of the source commit under `work/`. If the target is not an
   ancestor, `git rebase <target>` runs in that disposable worktree. Merge commits that would need a rebase
   block (`merge_commits`); conflicts abort the rebase and yield a structured owner failure.
2. **Validate.** The changed paths between the target and the candidate select checks from the policy
   (see [policy.md](policy.md)). Every step runs in its own process group with a timeout. Setup and planner
   failures are infrastructure failures; only the check step itself can blame the owner. A check must not
   modify tracked files (`candidate_mutated`).
3. **Publish.** With the integration lock held, the checkout is re-checked (branch, tracked changes,
   unknown untracked paths, overlap of incoming paths with runtime or ignored files). A receipt
   (`old`, `new`) is written, then `git merge --ff-only --no-autostash <candidate>` runs with
   `MERGE_QUEUE_PUBLICATION=<id>`. If the target moved meanwhile, the candidate is refreshed (at most three
   times per attempt). A crash after publication is reconciled by ancestry; accepted changes are never
   replayed.

### Integration lock and publication binding

The integration lock (`locks/integration.lock`) is a safety interlock, not the scheduler. The Git child of a
publication inherits it, so a killed helper can neither overlap its own publication nor be replaced until Git
has finished. The `reference-transaction` hook has two jobs:

* Any transaction that touches the target, outside a publication, is refused unless an unexpired
  authorisation for exactly that `old -> new` fast-forward exists *and* the lock is held. `MERGE_QUEUE_MANUAL=1`
  bypasses this for a person (never for a queue publication).
* Every ref transaction of a publication (`MERGE_QUEUE_PUBLICATION` set) must keep the checkout's symbolic
  `HEAD` on the target and may move only the target, from the authorised old to the authorised new SHA.
  A checkout switched to another branch mid-publication is refused before Git touches the working tree.

A `pre-push` hook refuses pushes of the target unless `MERGE_QUEUE_PUSH_APPROVED=1` or the manual override is
set: publishing to a remote is a separate decision from landing locally. Pre-existing hooks are chained
(`<hook>.merge-queue-chained`) and restored by `uninstall`.

### Stale `tq` rows

`tq`'s own cleanup sends SIGTERM to the recorded child pid of a running row whose parent is gone, without
checking that the pid still belongs to the same program. Before every dispatch the runner reads
`tq list --json`; if such a child is alive and is not verifiably a process this repository recorded (pid,
start time and argv all match), nothing is dispatched, `notices/stale-tq.json` names the pid and
`status`/`doctor` report it. Dispatch resumes once an operator clears the row.

### A not-ready checkout waits, then fails

Before preparing a candidate, each pass checks the integration checkout. If it is dirty, has unrecognised
untracked paths, overlaps the incoming paths or is on another branch, the request is requeued **without
penalty** (no attempt or dispatch is consumed) and re-dispatched after about 30 seconds
(`MERGE_QUEUE_CHECKOUT_WAIT_DELAY`). `notices/checkout-wait.json` names the waiting requests. After
`checkoutWaitLimit` seconds (default 14400) the request fails terminally with the original `checkout_*`
failure.

### A rejected fast-forward leaves the checkout dirty

Git may stage a fast-forward into the working tree before the ref update, so a refusal after that point can
leave staged changes while the target is unchanged. The queue never resets, restores or read-trees the
integration checkout: it checks for the race before merging, and if Git still fails it records a durable
`publication_checkout_dirty` operator failure naming the paths. The operator decides how to undo it.

### Time budget

`land` computes one deadline at lock acquisition (`landTimeout` minus a margin of 120 seconds). Each step is
capped at `min(step timeout, remaining budget)`. A step cut short by the budget stops its own process group
and requeues as a penalised infrastructure failure; it never blames the owner. This keeps validation groups
from outliving `tq`'s SIGKILL at `landTimeout`.

## Owners, inbox and acknowledgement

The owner id of a submission is `--owner`, else `$MERGE_QUEUE_OWNER`, else `$CODEX_THREAD_ID` (an agent
session id), else the worktree directory name. `inbox`, `result` and the session-start hook look up the same
candidates. Failures write an inbox entry *before* the request flips to its failed state. `ack` replaces a
finished request with a tombstone; landed and superseded requests are acknowledged automatically after a day.
Old runner logs that are not failure evidence are pruned; `logs-clear` removes optional diagnostics only.

## Process safety

* Recorded process groups are signalled only after pid, start time and argv are verified; an unverifiable
  pid is left alone and the record dropped.
* A live child of a dangerous `tq` row counts as ours only if it matches an identity this queue persisted;
  a matching argv alone is not enough.
* Landing matches are exact per request and per repository id, so a similarly named request or another
  repository's landing never blocks or is confused with this one.

## Limits

* One local machine, one integration checkout per repository, one target branch per installation.
* The Git hooks and `core.hooksPath`/`-c` overrides are same-user speed bumps, not a security boundary; a
  changed target is detected and re-validated, never overwritten.
* No remote or mobile delivery; no automatic wake-up of a closed agent session (the next session sees the
  inbox).
