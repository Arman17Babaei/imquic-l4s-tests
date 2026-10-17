# Repository Guidelines

## Project Structure & Module Organization

This repository owns the PLY-to-splats, configurable QEMU topology, Gaussian player, video and same-player similarity workflow. `tools/l4s/` contains its launcher, exporter, bridge and helpers; `tests/priority_moq_sidecar_unit.c` checks the native sidecar. Tracked reports, summaries, and plots live in `l4s/`, while large captures and live outputs belong in the ignored `results/` directory. `deps/imquic/` and `deps/picoquic/` are pinned submodules; the top-level picoquic checkout is authoritative, so do not initialize IMQUIC's nested picoquic dependency.

## Build, Test, and Development Commands

- `make init` initializes the two required submodules.
- `make cpu-check` builds the player and runs focused Python/browser/bridge checks using forced SwiftShader.
- `make camera-trace-player-check` additionally checks native source syntax and the C sidecar unit fixture.
- `make experiment-record-check` checks run provenance metadata.
- `make build` builds picoquic, configures IMQUIC against it, and produces the experiment binary.
- `cd deps/imquic && make check` runs the C fixture plus both analyzer self-tests after configuration.
- Edit `tools/l4s/player_topology.py` directly to configure the guest graph, link rates, delay and DualPI2 queues.
- Launch video/quality runs with `tools/l4s/run_camera_trace_player.py`; see `README.md` for commands.

Set `JOBS`, for example `make build JOBS=8`, to control build parallelism. Consult `l4s/validation-report.md` before using the QEMU wrapper.

## Coding Style & Naming Conventions

Follow the surrounding code. Python uses four spaces, type hints, `pathlib.Path`, snake_case functions, and uppercase module constants. C uses tabs for indentation, lower_snake_case identifiers, and IMQUIC/GLib types. Keep shell scripts POSIX-oriented unless an existing Bash feature is required. Use descriptive L4S mode names such as `l4s-off`, `l4s-ect0`, and `l4s-on`. No repository-wide formatter is configured; keep imports grouped and avoid unrelated formatting changes.

## Testing Guidelines

Run `make cpu-check` for player/bridge changes and `make check` inside configured `deps/imquic` for transport changes. Run `results/venvs/3dgs/bin/python -m unittest tests.test_player_sh3` for exporter changes. Historical experiment implementations are recoverable at Git revision `d7d6cab`. Place generated test artifacts under `results/`, not `l4s/`. Update tracked evidence only from a reproducible completed run, and record environment or submodule revisions in the relevant report.

## Commit & Pull Request Guidelines

History follows concise Conventional Commit subjects, primarily `fix:` and `chore:` (for example, `fix: wire runners to dependency submodules`). Keep each commit scoped and include submodule pointer changes only when intentionally revising the experiment. Pull requests should explain the hypothesis or harness change, list commands run, link related issues, and summarize evidence differences. Include updated plots or report links when results change; never commit secrets, VM credentials, packet captures, or bulky live-run directories.
