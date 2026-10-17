# Gaussian streaming through QEMU

The maintained workflow is: **PLY → progressive splats → QEMU topology → player → video → SSIM/PSNR**.

## Prepare and build

```sh
make init
npm ci
npm --prefix deps/gaussian-player ci
npx --no-install playwright install chromium
make cpu-check
make build-3dgs-priority-mvp
```

The CPU check needs Python 3, Node.js 20.19+ (or 22.12+), FFmpeg and Chromium's
normal OS libraries. It uses a tiny synthetic scene and forced SwiftShader;
no QEMU, Mininet, root, CUDA, GPU or downloaded scene is needed for that check.
Native MoQ builds additionally require the C/GLib/Jansson/OpenSSL build dependencies.
The QEMU runner expects an existing Linux image with Mininet, OVS, DualPI2 and
build dependencies; select it with `--qemu-base`. It provisions an ephemeral overlay.

## Convert and order a PLY

Use a standard Graphdeco 3DGS PLY with position, opacity, scales, rotations and
all degree-3 spherical-harmonic coefficients. Preparation needs NumPy and plyfile:

```sh
python3 -m venv results/venvs/3dgs
results/venvs/3dgs/bin/pip install numpy==2.5.2 plyfile==1.1.5
results/venvs/3dgs/bin/python -m unittest tests.test_player_sh3
```

These exporter regressions are separate from the dependency-light player/bridge
CPU smoke, which uses simulated native delivery.

```sh
results/venvs/3dgs/bin/python tools/l4s/prepare_player_sh3.py \
  --source data/scene.ply --output results/scene \
  --grid 10x10x3 --max-tile-gaussians 16384
```

The player uses **GSP2 splat objects**, retaining every Gaussian and all SH
coefficients. Inside each spatial tile, descending opacity defines four disjoint
25% layers: cumulative quality is 25%, 50%, 75%, then 100%. Ties retain source
order. This follows MoQSplat's opacity ordering while retaining its normally
pruned final quarter and full SH in the base layer. It does not claim identical
MoQSplat clustering or wire format. Objects are bounded and SHA-256 validated.

For compatibility with tools that require an ordinary `.splat` file:

```sh
results/venvs/3dgs/bin/python tools/l4s/convert_3dgs_ply_to_splat.py \
  data/scene.ply results/scene.splat
```

This export also sorts by descending opacity and keeps all Gaussians. The
32-byte `.splat` format contains only DC colour, so use GSP2 for the full-SH
streaming and similarity workflow. The conversion metadata lists omitted SH.

## Edit the network in Python

Edit [`tools/l4s/player_topology.py`](tools/l4s/player_topology.py) directly.
`build_topology()` creates the Mininet graph and returns `(net, client, server,
devices)`; `configure_topology(devices, rate)` applies link and queue settings.
The `devices` mapping supplies `(node, interface)` pairs for before/after evidence.
The player only needs the designated client/server nodes. There is no topology
configuration framework. For a different graph, edit both functions, including
interface roles and shaping placement.

The example is `client — s1 — server`, with a 20 Mbit/s HTB/DualPI2 bottleneck
and nominal 80 ms RTT. Rate, propagation and AQM constants are editable in that
script. Keep propagation and dropping AQM on separate egresses on the experiment
kernel. `--bottleneck` optionally overrides the script's rate. You can pass a
separate self-contained Python file with `--topology-script`.

## Produce video and similarity measurements

```sh
python3 tools/l4s/run_camera_trace_player.py \
  --manifest results/scene/manifest-v2.json \
  --trace path/to/camera-trace.json \
  --output results/scene-qemu-run \
  --transport moqt --network-path qemu --transport-mode prague \
  --qemu-base path/to/base.qcow2 \
  --topology-script tools/l4s/player_topology.py \
  --quality --quality-fps 2 --format mp4 --headless
```

The trace is a JSON array starting at timestamp zero, using either recorded
`view_matrix` poses or the camera-position/forward/up format accepted by the
player. One camera trace drives the entire run. Use `--gpu-mode software` if
hardware WebGL is unavailable; similarity calculation itself runs on the CPU.
The native subscriber lives in the guest, so Gaussian bytes cross the guest
network. The host serves UI/metadata and records the player.

Outputs under the new run directory:

- `camera-trace.mp4` and `camera-trace.webm`: video at `--fps` (default 30).
- `candidate-frames/`: clean sampled PNGs and their exact rendered trace
  timestamps, camera poses and applied Gaussian counts.
- `reference-frames/`: complete scene in the same player, at matching timestamps
  and camera poses, rendered after the streamed run has finished.
- `quality/frame-quality.csv`: sampled RGB SSIM and PSNR over time.
- `quality/comparison.json`: aggregate scores and capture-manifest hashes.
- `validation-summary.json`, telemetry, provenance and guest network evidence.

`--quality-fps` controls similarity sampling independently of video FPS. Scores
use clean PNGs before video compression, without HUD or dye, and compare against
the fully loaded version of the **same** scene export. They measure progressive
visual completeness, not accuracy against an independent PLY renderer. Successful
evaluation does not imply a universal SSIM threshold or transport-performance claim.
Every run uses a new directory; reference renders are not reused by file count.

To try the same video/metric path without QEMU, use `--transport http` and omit
`--network-path qemu`. This is also the small CPU acceptance path. Use a fresh
output directory and `--gpu-mode software` when appropriate.

Without `--quality`, the launcher can still record video; `--no-record` disables
video. `--fixed-frame --no-record` retains the single-frame capture mode.
`--interactive --no-record` exposes the browser Play interface instead of running
an automatic trace. Existing endpoints are owned by their launcher.

The CPU smoke leaves a tiny example video and quality CSV under `results/cpu-check/`.
Historical runner, plotting, training and CUDA-reference code was removed from
the active tree. Recover it from Git revision `d7d6cab` if needed. Stored evidence
under `l4s/` and `results/` is unchanged. Only the current player workflow and its
checks are maintained here.
