# Native menu-bar app: implementation plan

## Scope

Deliver a lightweight local monitor in `apps/macos`, showing one repository at a time. A small
bottom dropdown changes the selected repository. Orbital is the default theme: satellite, curved
route, rocket and Earth, with the active request dot, branch/request name and elapsed time beside it.
Keep graphics native and simple; preserve a clean seam for alternate themes and future artwork.

## Work and ownership

1. Parent: isolate the task from clean local main; own Git, model/store, build and final integration.
2. Luna: inspect the durable record contract, implement synthetic model tests, and investigate
   build-tool compatibility independently.
3. Terra: implement theme palettes, vector scene and common panel with disjoint source ownership.
4. Parent: compile, review contracts and complete visual/runtime acceptance; return focused repairs
   to the original owner when needed.

## Data contract

Read the shared repository registry, selected repository's request files. Acknowledged tombstones lack repository identity and are omitted.
No engine mutation or extra daemon. Preserve last readable data with a visible error on read failure.
Unknown nonterminal states remain visible. Focus a publishing request before other active requests;
show concurrent work separately. Elapsed uses running/submission timestamps, never commit age.
Route location is stage-based and never represents a measured percentage.

## Lightweight lifecycle

Native SwiftUI menu-bar window, fixed compact layout, no Dock icon or web runtime. Background reads
are triggered by native filesystem changes only while visible. Debounce events, refresh on reopen,
support custom state directories, and stop observation on close. Tick elapsed text only while active.

## Completion evidence

- Native app builds into an ad-hoc signed local `.app`.
- Synthetic contract tests cover timing, selection, unknown states, concurrent work, malformed records,
  missing stores and acknowledgement.
- Inspect active/idle/error/long-name artwork and bottom selector.
- Exercise repository switching, persistence, theme switching and a live synthetic file transition.
- Run repository regression checks, review full diff and integrate locally; no remote publication.
