# Branch cleanup, 2026-10-17

The recoverable baseline retains the current player, native transport fixes,
regression tests, and compact thesis/cleanup documentation. Generated captures,
credentials, and build artifacts are excluded. No experiments are rerun and no
scientific claims are changed by this maintenance work.

## Starting state

| Repository | Starting revision | Changed/untracked status entries |
| --- | --- | ---: |
| `.` | `771559be617fa8faf32725071c4ae048acf24929` | 43 |
| `deps/picoquic` | `5cc4302e1c526fc17cd8b7d254dd2832f153792e` | 15 |
| `deps/imquic` | `26052a7d731c6d6415f639b0776ac46ac63bd763` | 19 |
| `deps/gaussian-player` | `68c4600a8fb9e333dc2ed9396db870487cf40f17` | 19 |

The starting inventory and binary diffs are saved locally under
`results/branch-cleanup/`. The subsequent baseline commits preserve new source
files as well as tracked edits. No submodule remote is pushed by this cleanup.

## Baseline verification

- Rebuilt picoquic's CPU test binary; `prague_options`, `prague_dynamics`,
  `l4s_prague`, `l4s_prague_ewma`, `add_to_stream_fin_only_wakes_cnx`,
  `send_stream_blocked`, `sockloop_basic`, `sockloop_eio`, and
  `sockloop_errsock`: 9 tests, zero failures.
- `make -C deps/imquic -j4 check` with `PICOQUIC_LIBS` pointing at the
  authoritative picoquic build directory: fixture, priority, timer, relay
  scheduler, and embedded analyzer checks pass.
- `make analyzer-check` and `make camera-trace-player-check`: pass, including
  the player unit tests, type check, build, and C sidecar unit check.
- Publisher supervisor, TCP forwarding, and CPU SH3 export tests: pass.

These checks validate software behavior, not congestion performance, ECN
marking, or exact CUDA reference-rendering quality. QEMU and Mininet are not
launched.

## Preservation and retirement decisions

The six entries in `results/records/INDEX.md`, the thesis candidate inventory,
and retained thesis figure generators are preservation roots. Supporting and
negative results have the same retention priority as positive results.
Further removals and their replacements are recorded below as they complete.

## CPU check

`make cpu-check` now uses repository-locked Playwright, rebuilds the player,
runs focused Python/player/bridge tests, and runs all four two-object browser
cases. Each case reports an ANGLE SwiftShader renderer; results are in
`results/cpu-check/`. The browser/bridge portion completed in about 20 seconds
on this host. Native builds and CUDA reference checks remain optional.

Prerequisite checks were verified to reject missing local Playwright with an
explicit `npm ci` instruction, rather than resolving the home-directory copy.
