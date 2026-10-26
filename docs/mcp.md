# MCP server

`merge-queue-mcp` is a narrow stdio [MCP](https://modelcontextprotocol.io) server built with FastMCP 4.1.3 and
watchfiles 1.3.0. One server process serves **every** repository that ran `merge-queue install`. It lets an
agent hand a commit to the queue and receive the outcome as the result of one pending tool call, and lets any
session watch a request or wait for a queue to drain. It exposes no shell, no arbitrary command, no
validation override, no scheduler, no session resume and calls no model. The queue runner, `tq`, the policy
and the Git gates still own acceptance, scheduling and landing; the server invokes the installed CLI with
argument lists.

## Which repositories are served

```sh
merge-queue-mcp                                   # every registered repository
merge-queue-mcp --allow-repo /path/in/repo-a --allow-repo /path/in/repo-b   # only these (repeatable)
```

A call is accepted only when **all** of these hold:

1. the path's real common Git directory has `mergequeue.*` configuration (the repository is installed);
2. the state root of this server contains a **registry entry** for that repository whose repo id, common
   directory, state directory, checkout and target equal the repository's own configuration
   (`<state root>/repos/<repo-id>.json`, written by `merge-queue install`);
3. if `--allow-repo` was given, the repository is one of the listed ones (any path inside a repository
   names it).

Repository-local configuration alone is not trusted: any clone can carry forged `mergequeue.*` keys, and
those name an executable. The registry lives outside the repository, so only an operator-run `install` can
make a repository acceptable. The server finds the registry through `$MERGE_QUEUE_STATE_HOME` /
`$XDG_STATE_HOME`; if you installed with a non-default root, give the server the same environment (see client
setup).

## Tools

IDs (`request_id`, `new_request_id`, `owner_id`) use 1 to 64 letters, digits, periods, underscores or
hyphens, starting with a letter or digit. `timeout_seconds` must be positive, finite and at most 82800
(23 hours, below a typical 24-hour client limit); the default is 7200. `worktree` is an absolute Git
worktree root.

### Owner-bound tools

| Tool | Contract |
|---|---|
| `submit(worktree, owner_id, request_id, timeout_seconds=7200)` | Save a new durable `request_id` before calling. Accepts the exact committed `HEAD` of the worktree once, then stays pending until the request is terminal. |
| `retry(worktree, owner_id, request_id, new_request_id, timeout_seconds=7200)` | After repair and commit, retries your failed receipt once under a new saved ID, then stays pending. |
| `wait_for_result(worktree, owner_id, request_id, timeout_seconds=7200)` | Reattaches an existing receipt after an actual interruption. Never submits. |

`owner_id` is required: the native chat or session id when there is one, otherwise a stable task id. Reuse it
on reconnect. A shared server process has no per-chat environment; the pending call itself routes completion
to the calling session. Existing exact-commit deduplication is preserved: the response keeps the canonical
`request_id` and `owner_id` and adds `reconnect_request_id` / `reconnect_owner_id` for your saved routing
identity. A deduplicated receipt owned by someone else can be observed through your accepted intent but not
retried by you.

Before CLI acceptance the server writes an immutable, locked acceptance intent
(`<state>/mcp-receipts/<requested-id>.json`: owner, worktree, exact SHA, operation, retry source). A lost
acceptance reply is reconciled from the intent and exactly one matching durable receipt; an ambiguous
acceptance returns an error naming the intent and never resubmits. IDs cannot be reused for a changed commit
or intent, and repeating an existing intent only reattaches. The finite acceptance CLI runs in its own
process session, so tearing down the server's process group cannot kill it.

### Read-only tools (no owner, nothing written)

These never bind an intent, create a lock, a directory or any file under the state directory.

| Tool | Contract |
|---|---|
| `watch_request(repository, request_id, timeout_seconds=7200)` | Follow **any** request until it is terminal. `repository` is an absolute path inside any worktree of the repo, or its 12-character repo id. `request_id` is a canonical queue id or a requested id from `submit`/`retry` (resolved through the intent without binding it). An unknown id is an immediate error. |
| `wait_for_queue_empty(repository, timeout_seconds=7200)` | Return once no request of the repository is in a non-terminal state. Returns immediately when already empty. |
| `list_queues(repository=None)` | One-shot snapshot of registered repositories (or one) with their pending requests. Discovery only. |

Results:

* `watch_request`: the request summary (`request_id`, `owner_id`, `state`, `phase`, `source_commit_sha`,
  `target_sha`, `seq`, `result`, ...) plus `terminal`, `timed_out`, `exit_code` (same codes as the CLI: `0`
  landed, `10` owner action, `11` operator action, `12` superseded, `20` infrastructure, `30` pending),
  `repository` (the common Git directory) and `receipt_path`. A non-terminal result carries `next_action`.
* `wait_for_queue_empty`: `{empty, timed_out, repository, repo_id, observed_at, pending: [request ids]}`.
  "Empty" means no request in a non-terminal state; failed but unacknowledged requests are terminal and do
  not count. On timeout `timed_out` is `true` and `pending` lists what remains.
* `list_queues`: `{observed_at, state_root, queues: [{repo_id, repository, checkout, target, state_dir,
  pending: [{request_id, owner_id, state, phase, seq}]}]}`. A registry entry that no longer matches its
  repository appears with a `problem` instead of data.

## Events, progress and timeouts

The server observes **native filesystem notifications** (watchfiles) of the nonrecursive `requests/` and
`tombstones/` directories of the state directory, never candidate worktrees or dependencies. A tool reads an
initial snapshot, then one snapshot at the watcher's first event or one-second startup checkpoint (closing the
read/registration race), then one per later event. Quiet internal wake-ups do not read queue state or notify
the client. Only meaningful changes emit MCP progress under the caller's progress token:

* per request: a change of `(state, phase, attempts)`, message `"<id>: <state> / <phase>"`;
* queue empty: a change of the pending set, message `"<n> pending: <id>, ..."`.

There are no periodic "still waiting" heartbeats and **no polling fallback**: a native watcher error surfaces
as a tool failure. No progress token means no progress notifications; the final result still completes
normally. A directory that does not exist yet (`tombstones/` appears with the first acknowledgement) is not
created by read-only tools; the server watches the state directory for it instead.

Timeouts, cancellation, MCP disconnects and closing a chat stop the **observer**, not the durable queue job.
A timed-out result is not a reason to resubmit: call `wait_for_result` (owner) or `watch_request` (anyone)
again with the same ids. Do not wrap the queue in a polling loop or repeatedly call CLI `status`/`result`;
`list_queues` is for discovery, not waiting. The server cannot wake a closed agent session; a later
explicit reconnect reads the same receipt.

## Server instructions

Clients receive these instructions on initialization:

> Submit or retry once with a durable request_id and explicit stable owner_id, then wait for this tool's
> completion/background notification. Never loop on status. After interruption reconnect the same receipt with
> wait_for_result; never resubmit implicitly. To follow a request you do not own, use watch_request; to wait
> until a repository's queue has drained, use wait_for_queue_empty. list_queues is one-shot discovery, never a
> polling loop.

## Client setup

Install the tool into a stable environment (`uv tool install <path-or-git-url-of-this-repository>`), then
register the console script by absolute path. `<tool-bin>` is the directory `uv tool` puts executables in
(for example `~/.local/bin`; write it out in full if your client does not expand `~`). Never point a
long-lived client configuration at a disposable task worktree.

Codex (`config.toml`, merge additively with existing servers):

```toml
[mcp_servers.merge-queue]
command = "<tool-bin>/merge-queue-mcp"
args = []                                  # or ["--allow-repo", "<repo>"]
tool_timeout_sec = 86400
[mcp_servers.merge-queue.env]
FASTMCP_ENV_FILE = "/dev/null"
PYTHONDONTWRITEBYTECODE = "1"
# MERGE_QUEUE_STATE_HOME = "<state-root>"  # only when you installed with a non-default state root
```

Claude Code (`.claude.json` / `claude mcp add-json`):

```json
{
  "mcpServers": {
    "merge-queue": {
      "type": "stdio",
      "command": "<tool-bin>/merge-queue-mcp",
      "args": [],
      "env": {"FASTMCP_ENV_FILE": "/dev/null", "PYTHONDONTWRITEBYTECODE": "1"},
      "timeout": 86400000
    }
  }
}
```

Configure the long per-server tool timeout before use: some clients default to 60 seconds. Claude Code can
automatically background a long-running MCP call and deliver its completion as a notification; that is client
behaviour, and the server does not require a task-status loop. The server disables FastMCP dotenv loading
before import so it cannot consume an application `.env` file. Clients must reload MCP after the
configuration changes.
