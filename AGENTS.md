# Contributor guidance

This repository implements a local deterministic queue for integrating committed Git work. Preserve the
receipt lifecycle, FIFO and target freshness guarantees, policy ownership, isolated candidate validation,
and guarded fast-forward publication. Keep credentials, machine-specific paths, and runtime state out of
source and documentation. Prefer generic path placeholders in examples.

## Source map

- `src/merge_queue/core.py`: CLI, receipt/state lifecycle, runner, candidate rebase and validation,
  publication checks, and Git hooks.
- `src/merge_queue/mcp_server.py`: stdio MCP tools, receipt intents, event observers, and client timeouts.
- `docs/architecture.md`: lifecycle and state model; `docs/operations.md`: installation and operations;
  `docs/policy.md`: validation policy; `docs/mcp.md` and `docs/agents.md`: agent/client contracts.
- `tests/`: isolated temporary repository fixtures and regression coverage.
- `apps/macos/`: native SwiftUI monitor, typed read-only queue model, themes and synthetic previews.
  For native changes, run `scripts/swift.sh test --disable-xctest` from `apps/macos`; inspect changed visual states.

## Change and validation

Make the smallest change that preserves the contracts above. For source or behavior changes, run the focused
tests for the affected area, then the complete suite when feasible:

```sh
uv run python -m unittest discover -s tests
```

The test suite requires `tq` 0.4.1 on `PATH` or at `~/.local/bin/tq` (or `MERGE_QUEUE_TQ`). Tests use
temporary fixture repositories, their own HOME, state root, and `tq` data directory. For documentation-only
changes, check links, command/config syntax, and consistency with the source contract; do not run live queue
operations just to validate prose.

## Safety to preserve

- Submission accepts an exact committed SHA and produces a durable receipt. A wait observes that receipt;
  interruption never implies resubmission.
- Retry follows repair and a new commit where applicable, links to the original receipt, and receives a new
  request ID. Do not conflate retry with duplicate submit.
- Queue execution and policy own validation. MCP is a narrow adapter, not a shell or policy override.
- Candidate validation is against the combined result. Publication remains a guarded local fast-forward;
  remote push approval is separate.
- Keep readers event-driven with native filesystem notifications. Do not add status polling loops or
  background session-resume machinery.
- Keep examples portable. Do not add automatic installs, global settings edits, credentials, home paths,
  private repository names, or machine identifiers.
