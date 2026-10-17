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

## Consolidated player launcher

`run_camera_trace_player.py` now owns one process lifecycle for visualization,
recording, HTTP validation, loopback MoQ, and QEMU MoQ, including interactive
Play gating and fixed-frame/camera captures. `run_player_validation.py` is
removed; its README commands now use the consolidated launcher with
`--no-record`. Both QEMU resource controls and `--gpu-mode` reach their consumers.

Regression coverage rejects nonpositive browser/launcher timeouts and QEMU
budgets before opening inputs. The CPU browser fixture also invokes the real
HTTP fixed-frame launcher, verifies a saved image and software renderer, and
checks that browser failure telemetry survives an unwritable failure screenshot.
A timing-sensitive frame assertion now waits for settled captures before
comparing streamed and one-shot images. No QEMU runtime acceptance is claimed.

## Retired MVP demo and probe

Removed `run_priority_moq_loopback.py`, `priority_moq_browser_probe.mjs`, and
their two dedicated tests. Their replacement is the consolidated launcher:
use a prepared manifest and camera trace with `--transport moqt
--network-path loopback --no-record --headless --gpu-mode software`. The
priority-audit loopback gate is retained because it validates accepted
scheduling updates rather than browser playback alone.

The preservation audit inspected 19 metadata files across all six promoted
records. Its conservative source/import closure contains 59 files from 48
evidence/figure roots; none of the three retired tools appears in that closure.
The local audit is saved in `results/branch-cleanup/evidence-dependencies.json`.

Both reordering workload modules remain imported. The historical camera-priority
runner supports a retained negative result. Coexistence and Reno runners,
fetch-concurrency diagnostics, backlog/bounds analysis, and Persian/English
figure generators remain available for retained or candidate thesis evidence.
Older optional native/deadline/training paths have no confirmed replacement and
are retained rather than inferred obsolete from age or naming.

## Loose diagnostics

Moved 15 inactive root/picoquic qlogs (13,891,781 bytes) into
`results/branch-cleanup/loose-qlogs/`, preserving per-repository filenames.
`results/branch-cleanup/qlog-archive.json` records the original paths, sizes,
and SHA-256 hashes; every archived hash was verified after moving. Open file
descriptors were checked before moving. The harness and picoquic now ignore
`*.qlog`, keeping future generated diagnostics out of source status. No retained
result data was deleted and no existing services were stopped.
