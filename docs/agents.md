# Agent adoption

Merge Queue is a local, deterministic integration queue. Its queue engine does not need an LLM, model
provider, API key, or hosted service. An agent prepares and commits work as usual, then submits the exact
commit. The installed runner and trusted validation policy control scheduling and publication.

## One durable submission

Before submission, create and save a stable `request_id` and `owner_id` (1–64 letters, digits, `.`, `_`, or
`-`, starting with a letter or digit). Use the native chat/session ID as owner when available; otherwise use
a stable task ID. Preserve both across interruption and reconnect. A worktree contributes its committed
`HEAD`; keep changes committed before submission.

For MCP, call `submit(worktree, owner_id, request_id, timeout_seconds=7200)` once. Leave one long-running
call pending and do independent work while the host waits. On an actual interruption or timeout, call
`wait_for_result` with the same worktree, owner, and request IDs. A caller observing another owner's receipt
uses `watch_request`. `list_queues` is a one-shot discovery snapshot; do not loop on status, results, list,
sleep, or short waits. A host task/session handle means the same wait remains pending. Closing a session does
not stop the durable queue job or wake that session later.

Without MCP, submit once with caller-chosen IDs and wait:

```sh
merge-queue submit --request-id feature-task-001 --owner feature-task --wait --timeout 7200
```

If the wait is interrupted, observe the same receipt:

```sh
merge-queue result feature-task-001 --owner feature-task --wait --timeout 7200
```

Never resubmit to recover an uncertain response. Preserve the receipt and inspect its terminal outcome.
Statuses and list commands are diagnostic snapshots, not waiting mechanisms.

## Repair and retry

If a request fails, read its result and follow the reported owner or operator action. Repair the source in
the worktree, run relevant checks, commit the repair, then retry the failed receipt once using a new
`new_request_id` (MCP) or `--new-request-id` (CLI). Retry is a new queue request linked to the failed one;
it follows the queue's retry rules and ordering. Do not submit the repaired commit as an unrelated request
or repeat retry to recover a lost response. Reconnect to the retry receipt with `wait_for_result` using the
new request ID and original stable owner ID.

## Connecting clients

Install this package into a stable environment and register the absolute path to `merge-queue-mcp`, not a
disposable checkout. One global MCP server can serve every repository installed under its state root. Each
repository is identified and queued separately by its Git common directory and configured target branch.
If the installation uses a custom `MERGE_QUEUE_STATE_HOME`, set the same absolute path for the server.
The server disables dotenv loading; `PYTHONDONTWRITEBYTECODE=1` avoids package bytecode writes in the
client process.

Codex `config.toml` (merge with existing settings):

```toml
[mcp_servers.merge-queue]
command = "/absolute/path/to/tool-bin/merge-queue-mcp"
args = []
tool_timeout_sec = 86400

[mcp_servers.merge-queue.env]
FASTMCP_ENV_FILE = "/dev/null"
PYTHONDONTWRITEBYTECODE = "1"
# MERGE_QUEUE_STATE_HOME = "/absolute/path/to/state-root" # only if customized
```

Claude Code project `.mcp.json` (add to the top-level `mcpServers` object):

```json
{
  "mcpServers": {
    "merge-queue": {
      "type": "stdio",
      "command": "/absolute/path/to/tool-bin/merge-queue-mcp",
      "args": [],
      "env": {
        "FASTMCP_ENV_FILE": "/dev/null",
        "PYTHONDONTWRITEBYTECODE": "1"
      },
      "timeout": 86400000
    }
  }
}
```

For user-scoped Claude registration, run:

```sh
claude mcp add --scope user --transport stdio merge-queue -- \
  /absolute/path/to/tool-bin/merge-queue-mcp
```

Configure the environment and any custom state root through the client's supported settings. MCP client
tool timeouts must exceed the requested queue wait; server waits accept at most 82800 seconds. The examples
use a 24-hour client timeout. Reload or reconnect the MCP server after changing configuration or upgrading
the package. See [MCP setup and tool details](mcp.md).

## Local and remote push approval

Queue landing is a local integration-checkout fast-forward governed by the installed policy and Git gates.
Remote pushes are separate: they require `MERGE_QUEUE_PUSH_APPROVED=1`. MCP registration, an agent request,
and queue success do not grant remote push approval. The deprecated `MERGE_QUEUE_MANUAL` variable does not
authorize a push.

## Portable repository instruction

Copy this small instruction into a downstream repository's `AGENTS.md` or `CLAUDE.md` and adapt its local
links as needed:

> For committed work intended for the shared target, use Merge Queue. Save a stable request ID and owner ID,
> submit once, and keep one long wait pending. After a real interruption, reconnect to the same receipt;
> never duplicate a submission. For a failed request, repair, check, commit, then retry its receipt once with
> a new request ID. Do not poll snapshots when event-driven waiting is available. Remote publication still
> requires the repository's explicit `MERGE_QUEUE_PUSH_APPROVED=1` gate.

## Primary client references

- [Codex MCP server configuration](https://developers.openai.com/codex/mcp)
- [Claude Code MCP](https://code.claude.com/docs/en/mcp)
- [Claude Code memory and imports](https://code.claude.com/docs/en/memory)
