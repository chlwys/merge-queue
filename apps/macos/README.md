# Merge Queue for macOS

A small native menu-bar monitor for the existing local merge queue. Requires macOS 13 or later,
Apple's Swift compiler (Command Line Tools or Xcode), and Python 3 for the build helper. No web view,
JavaScript runtime, server, third-party Swift packages, or account is needed.

## Build and run

From this directory:

```sh
scripts/build-app.sh
open "dist/Merge Queue.app"
```

The app appears in the menu bar, without a Dock icon. Click its route icon to open the panel.
The bundle is ad-hoc signed for local use. Public distribution, notarization and automatic updates
are separate work. The build helper isolates known stale Command Line Tools interfaces inside
`.build/` when necessary; it never edits the installed toolchain.

Use the small repository menu at the bottom to switch repositories. Selection and theme are saved
in the app's preferences. The top-right menu switches between **Orbital** and **Minimal**, refreshes
the snapshot, or quits. The app does not start automatically at login.

## What the scene means

The satellite is the queue's departure point; Earth is the landing destination. An active request
appears as a dot on the curved route, accompanied by its source branch (or request ID), current stage
and elapsed time. The rocket is a small native vector drawing.

Route positions represent **preparing**, **validating** and **landing** stages, not a progress
percentage or an ETA. Elapsed time begins at the engine's first running timestamp, falling back to
submission time. Inline and normal work can overlap: the scene focuses a publisher first, otherwise
the earliest active request, and lists other active work below. Queued work stays in the list rather
than pretending to be running. This monitors local merge requests, not GitHub pull-request metadata.

## Data and overhead

The app reads the existing repository registry and request JSON files. State root
selection follows `MERGE_QUEUE_STATE_HOME`, then `XDG_STATE_HOME/merge-queue`, then
`~/.local/state/merge-queue`. Environment overrides must be supplied to the app process at launch;
normal Finder launches use the default location.

All state access is read-only. There are no Git, queue, network, submission, cancellation or runner
control operations. Malformed/unavailable queue data produces a warning and retains the last readable
snapshot, rather than silently presenting an empty queue.

While the panel is open, native filesystem notifications observe the registry and selected queue
directories, with coalesced reads on a background utility queue. When closed, observation stops;
reopening takes a fresh snapshot. Only an active request enables a once-per-second elapsed label.
There is no continuous animation or periodic queue polling. Visible lists and recent-result lists are bounded.
Acknowledged receipts are omitted because they no longer carry repository identity.

## Design boundary

- `Sources/QueueModel/`: typed durable-record reader and presentation values; no SwiftUI.
- `QueueStore.swift`: background snapshots, selection and native file observation.
- `QueuePanel.swift`: common panel layout and interactions.
- `QueueTheme.swift`: theme identifiers and palette.
- `SpaceArtwork.swift`: replaceable route artwork, satellite, Earth and rocket.

A new theme can extend the theme enum/palette and supply another artwork view without changing
queue reading or lifecycle semantics. Custom image packs and a theme plugin system are intentionally
outside this first version.

## Validation

```sh
scripts/swift.sh test --disable-xctest
```

Tests use temporary synthetic registries and records, including native filesystem-event refresh,
visibility and saved repository selection. Swift 6 is needed for the Swift Testing suites. For visual development, explicit synthetic
preview windows can be launched with `--preview-scenario active` (`idle`, `queued`, `failed`,
`multiple`, `long`, `empty`, `error` also supported), optionally with `--minimal`.
Preview windows are clearly titled and do not read or write queue state. Normal launches never
show sample requests.

For native-control testing with an isolated state root, `--inspect-window` hosts the same live panel
in a titled window. `--render-previews <directory>` exports the synthetic native panels as PNGs.
