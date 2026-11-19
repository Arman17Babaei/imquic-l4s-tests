# Player workflow checks

The maintained entrypoint is `tools/l4s/run_camera_trace_player.py`. It captures
fixed camera traces and evaluates clean PNGs against the same player with the
complete scene. Ideal mode uses the deterministic IMQUIC path; QEMU runs the
live shared-bottleneck scenario.

Run checks when changing the relevant components:

- `make cpu-check`: synthetic player/browser/bridge checks using SwiftShader.
- `make camera-trace-player-check`: native source/fixture and launcher checks.
- `make experiment-suite-check`: suite configuration, topology and analysis checks.
- `make ideal-player-check`: actual IMQUIC and deterministic GSP2/SPLAT playback.
- `make experiment-record-check`: provenance schema checks.
- `.venvs/3dgs/bin/python -m unittest tests.test_player_sh3`: exporter checks.

Fresh artifacts belong under `results/`. Previous captures, scores, validation
logs and reports were deleted at the user's request. This document lists the
checks; it does not claim retained runtime evidence. Dataset inputs are under
`data/`, environments under `.venvs/`, and VMAF dependencies under `.toolchains/`.
