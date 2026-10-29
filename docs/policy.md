# Validation policy

The policy is a JSON file owned by the operator of the integration checkout. It decides which checks
validate a candidate, and it is the only place that does. There is no submit-time override and no skip
switch: a contributor cannot choose, shorten or weaken the gate.

* It is read from the **integration checkout** (default `<checkout>/.merge-queue/policy.json`, or the
  `install --policy PATH` you configured), freshly at every landing. It is never read from a candidate, so a
  change cannot rewrite the rules it is judged by. A policy change reaches the gate once it has landed in
  the integration checkout.
* An unreadable or invalid policy blocks the request as `policy_invalid` (an operator failure, not the
  owner's).

## File format

```json
{
  "version": 1,
  "rules":  [ {"match": ["glob", "..."], "checks": ["name", "..."]} ],
  "checks": { "name": { "argv": ["cmd", "arg"], "cwd": ".", "env": {}, "timeout": 1800,
                        "plan": ["cmd", "arg"], "bootstrap": {"set": ["cmd", "arg"]} } }
}
```

Optional `fast_path` is described below. Other top-level keys (for example `comment`) are ignored. See
[`examples/policy.example.json`](../examples/policy.example.json).

### Rules: routing changed paths to checks

The changed paths of a landing are `git diff --name-only --no-renames <target> <candidate>`: every file the
combined candidate adds, modifies or deletes relative to the current target tip, as repository-relative
POSIX paths.

* A path **matches a rule** when it matches any of the rule's `match` patterns, using `fnmatch` semantics:
  `*` matches any characters **including `/`**, `?` one character, `[abc]` a set. Patterns are matched
  against the whole path and are case sensitive; `docs/*` covers everything below `docs/`; dotfiles need
  their own pattern (`.*` or `.github/*`).
* A path may match several rules; the selected checks are the union of all matching rules' `checks`.
* A rule with `"checks": []` declares a scope that needs no validation (documentation, data).
* **Unmatched paths fail closed.** If any changed path matches no rule, nothing runs and the request is
  blocked as `unknown_scope` (operator action: extend the policy). Use `merge-queue policy check` (below)
  to find uncovered paths before they block a landing.
* Selected checks run **in the order they appear in `checks`**, one after the other, each at most once.

### Checks: what runs

| Field | Meaning |
|---|---|
| `argv` | the check itself: a non-empty argument list (never a shell string). Exit 0 passes; any other exit is a `validation_failed` owner failure. |
| `cwd` | working directory, relative to the candidate worktree (default `.`). |
| `env` | extra environment variables, added to the runner's environment (minus `GIT_*` repository variables and `MERGE_QUEUE_MANUAL`). Values are converted to strings. |
| `timeout` | seconds per step (default 1800), additionally capped by what remains of the landing's time budget. |
| `plan` | optional planner command, see below. |
| `bootstrap` | optional map from set name to a setup command, see below. |

Substitutions in `argv`, `plan` and every `bootstrap` command are applied with Python `str.format`:
`{target}` (the target tip the candidate was built on), `{candidate}` (the candidate commit) and
`{worktree}` (the candidate worktree path), and `{checkout}` (the trusted integration checkout). A literal brace must be doubled (`{{`, `}}`). A first
element that contains a `/` and is not absolute is resolved against the check's `cwd`; a bare command name
is looked up on `PATH`.

Rules of the run: the check executes in the candidate worktree, in its own process group (so a timeout or
termination reaps every descendant). It must not modify tracked files or move `HEAD`; if it does, the
request is blocked as `candidate_mutated` (operator failure). Setup and planner steps never blame the owner:
a failing or timed-out step there, or one cut short by the landing's time budget, is an infrastructure
failure that is retried with a bounded number of attempts.

## Partial testing: the planner protocol

Large repositories rarely want to install every dependency set and run every test for every change. A check
can therefore delegate "what is needed for *this* change" to a planner that the project itself maintains.

A check that declares `plan` runs in three phases:

1. **Plan.** The planner command runs once (same `cwd`, `env`, `timeout`; placeholders substituted). It
   examines the candidate (typically the diff between `{target}` and `{candidate}`) and prints, as the
   **last line starting with `{`** of its combined standard output and error, one JSON object:

   ```json
   {"checks": ["backend", "tooling"], "unknown": []}
   ```

   * `checks`: the names of the setup **sets** this change needs.
   * `unknown`: changed paths the planner cannot route.
   * Any other keys are kept as recorded detail and otherwise ignored.

   A non-zero exit, a timeout, or output without such a JSON line is an infrastructure failure
   (retried, never blamed on the owner).
2. **Unknown paths block before any setup.** If `unknown` is non-empty, the request is blocked as
   `unknown_scope` (operator action: teach the planner or the policy about these paths). No dependency
   was installed and no check ran.
3. **Setup, then the check.** For each name in `checks` (in the planner's order) the command
   `bootstrap[<name>]` runs once; a name with no `bootstrap` entry needs no setup, and two names that map to
   an identical command run it once. Then `argv` runs exactly once. The planner's result is recorded on the
   request (`plan.details.<check>`) next to the duration and log of every step.

The planner is the project's own tool, so partial testing follows the project's real dependency graph, and
the queue stays generic. Keep in mind:

* The planner must be deterministic and conservative: when unsure, list the path in `unknown` (blocks) or
  include the set (more work), never silently skip a needed set.
* It runs inside the candidate, so it can use the project's own routing tables; but the policy that
  *invokes* it comes from the integration checkout.
* The check at the end should validate the combined candidate as a whole (for example the project's
  "changed since `{target}`" mode); the planner only decides which dependency sets exist beforehand.

Example (`web` check in the example policy):

```json
"web": {
  "plan": ["scripts/plan-web-checks", "--base", "{target}", "--candidate", "{candidate}"],
  "bootstrap": {"node": ["npm", "--prefix", "web", "ci"],
                "python": ["python3", "-m", "pip", "install", "--quiet", "-r", "web/requirements.txt"]},
  "argv": ["npm", "--prefix", "web", "test"]
}
```

with a planner that prints `{"checks": ["node"], "unknown": []}` for a front-end-only change.

## Automatic inline fast path

An operator may add a conservative presentation classifier and focused checks:

```json
"fast_path": {
  "plan": ["node", "scripts/merge-queue-fast-path.mjs", "--base", "{target}", "--head", "{candidate}"],
  "checks": ["presentation"],
  "timeout": 30
}
```

`checks` names existing check specifications. The planner runs from the **integration checkout**, never
from candidate code. Its final line starting with `{` must contain a JSON object with a boolean
`eligible` and a string `reason`; extra details are retained on the receipt. Timeout defaults to 30 seconds
and must be greater than zero and at most 120 seconds. Use `{checkout}` in focused check commands to invoke
trusted tooling while checking the isolated candidate. There is no contributor opt-in or skip flag.

Submission classifies source changes relative to the merge base with the current target. Eligible requests
run inline, with a durable receipt and no `tq` task or FIFO wait; only the listed focused checks run, without
the normal broad suites. After preparing/rebasing, the trusted planner classifies the actual net diff from
the target to the combined candidate again. Every target refresh repeats classification and focused checks.
Unknown path routing still blocks. Missing/ineligible/failing classification falls back to the normal queue
with its reason recorded; a failed focused check never publishes. The policy and directly referenced trusted
command files are fingerprinted across validation; changed authority falls back to normal validation.

Keep the classifier small, deterministic, and conservative: sensitive or ambiguous changes must return
`eligible: false`. The tool does not infer which languages or presentation edits are safe for a project.
Candidate changes to its own classifier cannot weaken the gate: classification uses installed policy and
the integration checkout's code. Dependency inputs of trusted scripts remain the operator's responsibility.

## `merge-queue policy check`

```sh
merge-queue policy check [--repo PATH] [--policy PATH]
```

Validates the schema (rule checks must exist, `argv` and `plan` non-empty lists, `bootstrap` commands
non-empty lists, `version` 1) and that every file from `git ls-files` in the repository is matched by some
rule. It needs no installation. Defaults: the repository containing the current directory (or `--repo`) and
`<repository root>/.merge-queue/policy.json`. Output is one JSON line:

```json
{"ok": false, "repo": "...", "policy": "...", "files": 42, "uncovered": ["path/a.bin"], "error": null}
```

Exit `0` when `ok`, `1` for an invalid policy or uncovered paths, `2` when the directory is not a Git
repository. Use it as the project's policy-coverage test, for example from a unit test or a CI step, so a new
directory cannot land without a rule (the landing would otherwise fail closed as `unknown_scope`).
