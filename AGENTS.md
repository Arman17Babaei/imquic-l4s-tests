# Repository Guidelines

## Project Structure & Module Organization

This repository owns the reproducible L4S/Prague experiment harness. `tools/l4s/` contains the Python analyzers and Mininet/QEMU runners; `tests/l4s-test.c` is the C traffic fixture. Tracked reports, summaries, and plots live in `l4s/`, while large captures and live outputs belong in the ignored `results/` directory. `deps/imquic/` and `deps/picoquic/` are pinned submodules; the top-level picoquic checkout is authoritative, so do not initialize IMQUIC's nested picoquic dependency.

## Build, Test, and Development Commands

- `make init` initializes the two required submodules.
- `make analyzer-check` runs the Mininet analyzer's fast, dependency-light self-test.
- `make build` builds picoquic, configures IMQUIC against it, and produces the experiment binary.
- `cd deps/imquic && make check` runs the C fixture plus both analyzer self-tests after configuration.
- `sudo python3 tools/l4s/run_mininet_benchmark.py --output results/l4s/mininet-run` runs the privileged Mininet/DualPI2 benchmark. It requires Mininet, DualPI2, tcpdump, and tshark.

Set `JOBS`, for example `make build JOBS=8`, to control build parallelism. Consult `l4s/validation-report.md` before using the QEMU wrapper.

## Coding Style & Naming Conventions

Follow the surrounding code. Python uses four spaces, type hints, `pathlib.Path`, snake_case functions, and uppercase module constants. C uses tabs for indentation, lower_snake_case identifiers, and IMQUIC/GLib types. Keep shell scripts POSIX-oriented unless an existing Bash feature is required. Use descriptive L4S mode names such as `l4s-off`, `l4s-ect0`, and `l4s-on`. No repository-wide formatter is configured; keep imports grouped and avoid unrelated formatting changes.

## Testing Guidelines

Run `make analyzer-check` for analyzer changes and `make check` inside configured `deps/imquic` for transport or fixture changes. Analyzer tests are embedded behind `--self-test`; extend them when changing inference or aggregation logic. Place generated test artifacts under `results/`, not `l4s/`. Update tracked evidence only from a reproducible completed run, and record environment or submodule revisions in the relevant report.

## Commit & Pull Request Guidelines

History follows concise Conventional Commit subjects, primarily `fix:` and `chore:` (for example, `fix: wire runners to dependency submodules`). Keep each commit scoped and include submodule pointer changes only when intentionally revising the experiment. Pull requests should explain the hypothesis or harness change, list commands run, link related issues, and summarize evidence differences. Include updated plots or report links when results change; never commit secrets, VM credentials, packet captures, or bulky live-run directories.
