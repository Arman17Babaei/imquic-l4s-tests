# Maintained player workflow validation

Validated on 2026-10-17. Starting harness revision: `dda5c3b`;
starting player revision: `cf44e22`. The player capture fix is `1f8dc0b`;
the lossless opacity export change is `d30b468` in the harness.
IMQUIC `57c864b` and picoquic `61cd7886` remain unchanged.

The maintained entrypoint is `tools/l4s/run_camera_trace_player.py`.
It accepts an editable, standalone topology script and can record video and
sample clean frames over HTTP, native loopback, or QEMU. Similarity uses a
second complete-scene instance of the same player, seeking the original camera
trace at the captured timestamps. Camera matrices and frame counts are checked
before CPU RGB SSIM/PSNR calculation. Quality scores use PNGs before encoding;
a successful evaluation is not a transport-performance claim.

Verification:

- `make cpu-check`: PASS, 46.56 seconds after dependency installation.
  Uses repository-declared Playwright, local player build, forced SwiftShader,
  simulated native delivery, and six browser/bridge cases. Covers Play gating,
  incremental objects, late delivery, accounting, reload, streamed/HTTP parity,
  capture after a cleared display buffer, video decoding and matching reference
  frames. Does not build or run native transport.
- `make analyzer-check`: PASS; retained analyzer and priority bridge gates.
- `results/venvs/3dgs/bin/python -m unittest tests.test_player_sh3`: PASS,
  four exporter checks including opacity order, all Gaussian IDs and all SH.
- `python3 -m unittest tests.test_player_workflow tests.test_camera_trace_player`:
  PASS, thirteen checks including editable topology settings, custom script
  import without Mininet, dirty source archives, metric recomputation and
  rejection of mismatched/incomplete captures.
- Documentation links and both launcher help entrypoints: PASS.
- Independent read-only code review: no blocking findings. Exporter checks
  deliberately use their own NumPy/plyfile environment outside the CPU smoke.

The CPU sample is 320×180, one second, thirty MP4 frames. Three clean samples
scored SSIM 1.0 against the full-scene control. This validates wiring on a tiny
fixture, not rendering accuracy or network performance on a real scene.
Ignored artifacts are under `results/cpu-check/` (`cpu-workflow.mp4`,
`cpu-frame-quality.csv`, `cpu-comparison.json`, PNGs) and
`results/player-workflow/` (check logs).

QEMU/Mininet runtime acceptance was **not run**. No existing service was stopped.
The editable topology path is covered by import, shaping-command and launcher
checks; actual guest provisioning, shaping and native delivery still require a
separate authorized run with a suitable QEMU base image.

Subsequent source pruning removed historical runners, analyzers, figures code,
training/CUDA tooling and their dedicated tests/docs. The earlier analyzer result
above describes the pre-pruning tree. Recover historical source from `d7d6cab`;
stored evidence under `l4s/` and `results/` remains unchanged. The earlier
branch-wide usefulness inventory remains in `results/file-review/`.

After pruning, `make cpu-check` passed in 53.57 seconds. All 29 retained Python
checks passed in the NumPy/plyfile environment; native syntax/sidecar checks and
`make priority-audit-loopback-check` passed. The latter verified accepted
scheduling updates with 131,072 synthetic splats over native loopback. QEMU
remains unrun. Active imports, Make targets and documentation links were checked;
independent review found no blocking cleanup regressions. The source tree now
contains 19 pipeline tools and ten test files. Historical source is recoverable
at `d7d6cab`; no new maintenance or archival source hierarchy was added.
