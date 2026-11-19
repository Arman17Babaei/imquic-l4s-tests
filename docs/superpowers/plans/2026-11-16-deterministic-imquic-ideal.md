# Deterministic IMQUIC Ideal Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for inline execution.

**Goal:** Deterministic offline ideal delivery and rendering on top of actual IMQUIC.

**Architecture:** A private control descriptor gates the existing relay's FETCH
selection loop. The driver waits for actual MoQ acknowledgements and selected
payload receipt, accounts for bytes/delay in simulated time, and releases due
objects before rendering exact trace poses in the existing player. Native QUIC
is an unmodeled reliable carrier, so no QUIC simulator is needed.

**Tech Stack:** C IMQUIC, existing authoritative picoquic, Python, TypeScript/player.

**Spec:** `docs/superpowers/specs/2026-11-16-two-scenario-testbed-design.md`.

## Constraints and rulings

- Preserve actual publisher/relay/subscriber and priority handling; no replacement scheduler.
- Constant payload bandwidth and fixed one-way data delay; immediate controls.
- Fixed frame times and empty initial request; no live ideal buffer or pacing.
- Host waits cannot change schedules, poses or applied-object membership.
- Preserve current QEMU edits and evidence; no research batch, commits or pushes.
- Work in the existing feature checkout to retain dependencies and pending edits.
- Source changes inside IMQUIC/player are intentional; submodule pointers remain unchanged.

## Completed tasks

### 1. Prove the minimal integration boundary

- [x] Trace actual FETCH, REQUEST_UPDATE, selection/send and subscriber callbacks.
- [x] Identify wall-time dependence; gate selection itself instead of already-selected output.
- [x] Establish explicit cache/control/object barriers without modeling QUIC packet time.
- [x] Verify actual native ordering and payload hashes under host waits.

### 2. Native driver and byte clock

Files: `deps/imquic/examples/moq-relay.c`, `tools/l4s/ideal_imquic.py`,
`tools/l4s/ideal_link.py`, `tests/test_ideal_imquic.py`, `tests/test_ideal_link.py`.

- [x] Relay descriptor protocol: `cache N` / `cached N`, `next` / selected identity or `empty`.
- [x] `ImquicPeer.open(command)`, `.update(command)`, `.next_object()` wait for actual native completion.
- [x] `OfflineDelivery.advance(Fraction)` accounts for exact encoded bytes and delay.
- [x] Verify boundary ordering, partial objects, independent propagation, no idle credit and queue failure.
- [x] Remove obsolete manifest-only scheduler and wall-clock delivery adapter.

### 3. Existing player and suite integration

Files: player `src/{main,moqt,receipt-socket,receipt-worker,render}.ts`,
`tools/l4s/{priority_ws_bridge,run_camera_trace_player,snapshot_video,run_experiments,
experiment_suite_config,experiment_suite_analysis,camera_trace_player_server}`
(with their existing language extensions), associated tests, README and example config.

- [x] `MoqtSidecar.advance(index, fps)` and `.horizon(durationMs)` complete after ordered object application.
- [x] Await mesh initialization and GPU sorting; record applied-object deltas/counts and poses.
- [x] Export every half-open video-grid frame, then account through the full delivery horizon.
- [x] Encode PNG video directly; quality sampling is an integral FPS stride shared by SSIM and VMAF.
- [x] Reject ideal buffering and first-object starts; remove obsolete buffered player code/tests.
- [x] Execute ideal cases once, preserve QEMU repeats, separate legacy analysis and use modeled payload bins.
- [x] Ensure requested scene metadata takes precedence over bundled assets.

### 4. Verification and review

- [x] `make cpu-check`.
- [x] `make camera-trace-player-check`.
- [x] `make experiment-suite-check` and example dry run.
- [x] `make -C deps/imquic check` and `make experiment-record-check`.
- [x] `make ideal-player-check`: actual native tests and real GSP2/SPLAT browser repeatability.
- [x] Direct launcher smoke: independent video, delivery and quality PASS.
- [x] Independent review and regression fixes for all important findings.
- [x] Root and changed-submodule diff checks and obsolete-reference audit.

Previous validation artifacts were intentionally cleared during the clean-baseline
reset. The commands above regenerate fresh checks under `results/`. Models,
traces and selected inputs are under `data/`; preserved script sources are under
`data/reproduction-scripts/`. No prior numerical research results remain here.
