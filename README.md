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

For a buffered MoQ demonstration, add `--start-after-base`. The player first
loads every spatial tile's base layer, holding the initial camera pose, then
starts the trace and recording while refinements continue arriving. This changes
the startup policy: the video excludes buffering time, which is recorded in
telemetry and `validation-summary.json`. Without this flag, MoQ playback starts
when requests are open and can show incomplete geometry during startup.
Spatial tiles share bounded 32,768-Gaussian render buffers, so small tiles do not
require a separate set of GPU textures and shaders for every refinement.
Optional quality dye highlights an updated shared buffer for one second after
its latest append. Buffers only contain one refinement level, but can contain
several spatial tiles; dye therefore indicates buffer activity, not individual
Gaussian arrival age. Use `--no-dye` for clean scene recordings.

`validation-summary.json` reports `recording_delivery` separately from eventual
scene delivery. A successful session can finish receiving its scene after the
video stops; `complete_scene_at_video_end` identifies that distinction.

The QEMU wrapper caches its package setup and compiled IMQUIC/picoquic/picotls
in `results/qemu-setup-cache/`. Source content (including dirty/untracked changes),
the build recipe, architecture, QEMU version and base-image file identities
select the cache entry automatically. A new dependency version builds a new
entry. Only successfully prepared, cleanly powered-off images are published;
each experiment uses a fresh child overlay with current scene, topology,
player and harness files, and reruns native checks. Guest `setup-cache.json`
records the key and hit/miss. The base identity uses file metadata rather than
a full image hash. Direct wrapper options `--setup-cache-dir` and
`--no-setup-cache` allow a separate cache or an uncached run.

For bounded observations, `--fixed-horizon` closes delivery at the trace's last
timestamp and records a censored snapshot instead of draining the remaining
scene. Objects whose hash validation finishes after the deadline are excluded.
A partial scene is a valid outcome; received refinements waiting on earlier
layers are reported separately. `--video-hud` adds the tile `(x,y,z,q)` spec and
trailing 100 ms received application-payload goodput to the recorded video,
while quality PNGs remain clean. This rate excludes protocol overhead and is
not the configured link capacity.

`--topology-script tools/l4s/thesis_player_topology.py` selects the Figures 9–10
two-switch graph: 300 Mbps provider and 100 Mbps downstream, both using DualPI2
for both Reno and Prague. Both AQMs use `target 15ms tupdate 16ms step_thresh 1ms`,
matching the earlier corrected L4S profile. There is 10 ms at each endpoint and two unlimited non-ECN Cubic
background flows after a 2-second warmup. Guest queue/link snapshots use a
100 ms target interval; actual sample timestamps and before/after state are
retained with transport telemetry, without packet captures. A single pair is
descriptive evidence rather than a repeated statistical comparison.

The October 18 scene observations used a different profile: step 5ms on both
AQMs, with FIFO 128 at s2 for Reno. Their saved videos and measurements retain
that provenance; they do not validate the corrected configuration. Plot labels
come from each run's `tc-state-before.txt`, including historical FIFO/step 5ms runs.

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

For the same two-switch topology with a 20 Mbps downstream link, add
`--bottleneck 20mbit` with the thesis topology script. The provider stays at
300 Mbps; downstream burst/cburst remain 128k, and the delays, background flows
and shared DualPI2 queue policy remain the same.

After a run, audit delivery and native sender progress before interpreting a
flat payload-rate interval:

```sh
python3 tools/l4s/analyze_player_stalls.py --run results/my-run --output results/my-run/bandwidth-audit.json
```

The default gate flags pending-transfer gaps of at least one second for review,
including native sender pauses masked by browser buffering. Shorter gaps remain
in the report. Exit 0 means no sustained candidate was observed; exit 2 requires
review and exit 1 indicates missing/invalid input. Full transfer followed by idle
time is expected; later draining cannot prove completion at the video horizon.

`plot_scene_experiments.py --manifest CASES.json --output results/my-plots`
creates PNG/PDF comparisons and CSV/JSON data from explicit retained cases.
The manifest contains a `cases` array with `scene`, `controller` (reno/prague),
`downstream_mbps` (100/20), and `directory` fields. Payload plots retain all 100 ms
bins, including zeros, and identify envelope goodput separately from accepted
payload counters and link capacity. Native transport progress, quality and
sampled queue statistics are retained alongside the topology/rate context.

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

For the earlier shared-bottleneck L4S operating profile, select
`--topology-script tools/l4s/historical_shared_topology.py` and omit
`--bottleneck` (the downstream link defaults to 100 Mbps). This puts the 20 Mbps
bottleneck on s1->s2, shared by the main stream and both existing unlimited
non-ECN Cubic flows. It uses the historical 32 KiB HTB burst, implicit cburst,
DualPI2 target 15ms/update 16ms/step 1ms, and no added propagation delay. Both
controllers use exactly the same network configuration. This transfers the
historical network profile to the current two-switch/two-background graph;
it does not recreate the older single-background experiment or its revisions.
Use a fresh output directory; existing scene videos retain their original profile.
The [current setup and experimental findings](l4s/current-experiment.md) records
the measured Prague advantage, retained negative comparisons and its limits.

The current 20 Mbps, step 5 ms native Prague/Cubic diagnostic is runnable with
`python3 tools/l4s/run_sustained_profile.py`. See [the consolidated setup and
evidence](l4s/current-experiment.md) for CPU assignments, resource guards,
transport changes and measured effects. This is separate from the scene presets.
