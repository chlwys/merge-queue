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

The compact panel is 320 × 300 points. Use the top-right hamburger menu to switch repositories,
choose **Orbital** or **Minimal**, refresh, or quit. The header identifies the selected repository;
selection and theme are saved in the app's preferences. Long content scrolls inside the compact panel. The app does not start automatically at login.

## What the scene means

Earth is the launch point; successful work builds the space station. The rocket and active dot
travel from Earth toward the station as the request moves through preparing, validating and landing.
The active title remains readable across up to four lines, with elapsed execution time below it.

- **Earth badge:** a nonzero number of waiting merge requests, not finished commits. Zero is hidden,
  and the badge contains only the number. Hover or click Earth
  to read what remains waiting to launch. One request can contain several Git commits.
- **Rocket:** the current active request; hover or click it to inspect all in-flight work, including
  concurrent fast-lane requests.
- **Space station:** completed, landed requests fill station modules. Hover or click the station to
  read the completion list. This is the queue's currently retained history, not an all-time count;
  acknowledged records no longer contain enough repository identity to display safely.
- **Debris field:** recent failures appear as orange fragments. Hover or click to read names and
  failure summaries. The graphic expires five minutes after failure (or first observation when the
  timestamp is missing). This only clears the visual effect: it never acknowledges or deletes the
  underlying request, inbox or evidence. A recent failure already observed by this app can remain
  as transient debris after acknowledgement, until the same five-minute window ends.

The permanent queue box is removed. Inspection popovers also support keyboard activation and remain
scrollable while the pointer is over them.

**Est. to clear** is a coarse estimate for all current active and waiting normal requests. It uses
the median execution duration of up to 20 retained successful normal merges, requires at least three
valid samples, and subtracts elapsed time from the active request. It shows learning or unavailable
instead of inventing a duration when history is insufficient, data/readiness needs attention,
concurrent/inline work is present, timestamps/stages are missing, or an active merge has exceeded
the recent median. Route positions are lifecycle stages, not a measured percentage or an ETA.

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
reopening takes a fresh snapshot. An active request enables a once-per-second elapsed label; recent debris without active work uses a
five-second UI clock until expiry. Both clocks stop when the panel is hidden. There is no continuous
animation or periodic queue polling. Inspection lists scroll inside a bounded popover.
Acknowledged completed receipts are omitted because they no longer carry repository identity.
Recently observed failures may remain in memory only until their five-minute debris window expires.

## Design boundary

- `Sources/QueueModel/`: typed durable-record reader and presentation values; no SwiftUI.
- `QueueStore.swift`: background snapshots, selection and native file observation.
- `QueuePanel.swift`: common panel layout and interactions.
- `QueueTheme.swift`: theme identifiers and palette.
- `SpaceArtwork.swift`: replaceable route artwork, station, Earth, rocket and debris.
- `MissionInspection.swift`: shared accessible hover/click inspection popovers.
- `MissionState.swift`: completed/debris classification and conservative queue-time estimates.

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
`multiple`, `long`, `empty`, `error`, `learning`, `dense`, `preparing`, `debris-expired` also supported), optionally with `--minimal`.
Preview windows are clearly titled and do not read or write queue state. Normal launches never
show sample requests.

For native-control testing with an isolated state root, `--inspect-window` hosts the same live panel
in a titled window. `--render-previews <directory>` exports the synthetic native panels as PNGs.
