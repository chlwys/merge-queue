# Security reporting

For a suspected vulnerability, use this repository's GitHub **Security → Advisories →
Report a vulnerability** link when private vulnerability reporting is available. If that
option is unavailable, open an issue asking for a private reporting channel without
including vulnerability details or private logs.

Reports should identify the affected commit or version and provide a minimal synthetic
reproduction. Never include credentials, private repository paths, or real queue state.

This is an early-stage local tool. It executes validation commands from an operator-trusted
policy and reads installed repository configuration. Run it only for repositories and
policies you trust; it is not a sandbox for hostile validation commands. See the
[documented safety model](README.md#safety-model).

Fixes target the current `main` branch. Older tags do not have a separate maintenance
commitment.
