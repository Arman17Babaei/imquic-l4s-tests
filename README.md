# Gaussian streaming through QEMU

There are two experiment scenarios: ideal payload delivery at a specified bandwidth,
and QEMU scene streaming alongside unlimited BBR and Prague TCP traffic.

The maintained workflow is: **PLY → progressive splats → QEMU topology → player → video → SSIM/PSNR**.

For configurable scene, bandwidth, delay, camera-duration, ideal/QEMU and Reno/Prague sweeps with retries and automated VMAF plots, see [player experiment suites](docs/player-experiments.md) and [the example configuration](experiments/player.toml).

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
python3 -m venv .venvs/3dgs
.venvs/3dgs/bin/pip install numpy==2.5.2 plyfile==1.1.5
.venvs/3dgs/bin/python -m unittest tests.test_player_sh3
```

These exporter regressions are separate from the dependency-light player/bridge
CPU smoke, which uses simulated native delivery.

```sh
.venvs/3dgs/bin/python tools/l4s/prepare_player_sh3.py \
  --source data/scene.ply --output results/scene \
  --grid 10x10x3 --max-tile-gaussians 16384
```

The player uses **GSP2 splat objects**, retaining every Gaussian and all SH
coefficients. Inside each spatial tile, descending opacity defines four disjoint
25% layers: cumulative quality is 25%, 50%, 75%, then 100%. Ties retain source
order. This follows MoQSplat's opacity ordering while retaining its normally
pruned final quarter and full SH in the base layer. It does not claim identical
MoQSplat clustering or wire format. Objects are bounded and carry SHA-256 metadata for offline checks. The browser
skips payload integrity prechecks and logs actual object-processing failures;
GSP2 decoding and packing use a bounded Web Worker pool.

For compatibility with tools that require an ordinary `.splat` file:

```sh
.venvs/3dgs/bin/python tools/l4s/convert_3dgs_ply_to_splat.py \
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

The graph has exactly two switches. Three sender/receiver pairs attach on either
side: the scene server/player client, BBR TCP, and Prague TCP. All forward data
shares the `s1 → s2` HTB/DualPI2 bottleneck (default 50 Mbps); reverse traffic
uses an equivalent queue on `s2 → s1`. Access links are 300 Mbps. Both TCP senders
have one unlimited data connection, without an iperf bitrate or parallel-flow
limit. BBR requests classic ECN (ECT(0)); Prague requests Accurate ECN at both endpoints.
The guest must provide TCP BBR and Prague: setup and startup fail on missing support,
controller fallback, missing negotiated ECN, or missing background data.

`--bottleneck` overrides the shared rate. For direct runs, edit the bandwidth,
total one-way propagation, DualPI2 target/update/step/coupling and background
warmup constants in `player_topology.py`. Suites set the same values in TOML and
retain a standalone configured copy of this source. Every pair has the same
total one-way delay (default 40 ms), split equally across all three links;
propagation remains separate from the inter-switch dropping AQM. Background
receivers must see payload, then complete warmup before scene services start.

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
hardware WebGL is unavailable; similarity calculation defaults to CPU and
can use CUDA with `--quality-backend cuda`.
The native subscriber lives in the guest, so Gaussian bytes cross the guest
network. The host serves UI/metadata and records the player.

For a buffered MoQ demonstration, add `--start-after-base`. The player first
loads every spatial tile's base layer, holding the initial camera pose, then
starts the trace and recording while refinements continue arriving. This changes
the startup policy: the video excludes buffering time, which is recorded in
telemetry and `validation-summary.json`. Without this flag, MoQ playback starts
from an empty scene as soon as the first request batch is sent. The camera
clock, rendering and recording start together, with zero excluded loading time;
blank initial frames and request/transport delays count inside the video.
`connectionSetupMs` records browser-to-sidecar setup separately from the zero
`bufferingMs` of this default mode.
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

The browser bridge permits 64 outstanding objects by default (`--object-window`),
and the native sidecar buffers up to 64 MiB before applying backpressure. These
queues absorb temporary browser-processing stalls; a sustained slow consumer
can still fill them. QEMU VMAF uses live rendered frames: arrival timestamps
are telemetry, not an offline replay that makes objects visible retroactively.

For bounded observations, `--fixed-horizon` closes delivery at the trace's last
timestamp and records a censored snapshot instead of draining the remaining
scene. Objects whose hash validation finishes after the deadline are excluded.
A partial scene is a valid outcome; received refinements waiting on earlier
layers are reported separately. `--video-hud` adds the tile `(x,y,z,q)` spec and
trailing 100 ms received application-payload goodput to the recorded video,
while quality PNGs remain clean. This rate excludes protocol overhead and is
not the configured link capacity.

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
Use `--quality-backend cuda` with a CUDA-enabled PyTorch Python environment
for FFmpeg-compatible RGB SSIM/PSNR after recording. The default remains CPU;
CUDA requests fail explicitly when unavailable. Quality reports record the device
and library versions.

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

For the ideal scenario without QEMU, use `--transport ideal` as shown below.
HTTP loading and native loopback remain internal reference/acceptance facilities. Use a fresh
output directory and `--gpu-mode software` when appropriate.

Without `--quality`, the launcher can still record video; `--no-record` disables
video. `--fixed-frame --no-record` retains the single-frame capture mode.
`--interactive --no-record` exposes the browser Play interface instead of running
an automatic trace. Existing endpoints are owned by their launcher.

The CPU smoke leaves a tiny example video and quality CSV under `results/cpu-check/`.
Historical runner, plotting, training and CUDA-reference code was removed from
the active tree. Recover it from Git revision `d7d6cab` if needed. Previous experiment outputs and reports were intentionally deleted for a clean baseline. Only the current player workflow and its
checks are maintained here.

Historical topology variants and standalone sustained-traffic diagnostics have
been removed. Old reports and captures have been deleted. Git history retains previously
tracked material; it is not a current runtime preset.

### Ideal application-level bandwidth and delay

Ideal mode runs deterministic offline rendering on top of the real IMQUIC MoQ
publisher, relay and subscriber. Local QUIC carries permitted objects; its
wall-clock throughput is not the modeled link. It needs the native MoQ binaries
and player build, but no QEMU, AQM or competing traffic. For example:

```sh
.venvs/3dgs/bin/python tools/l4s/run_camera_trace_player.py \
  --manifest data/scenes/train/manifest-v2.json \
  --trace data/traces/thesis-scenes-19s/train-trace-19s.json \
  --output results/my-ideal-train --transport ideal \
  --ideal-bandwidth-mbps 20 --ideal-data-delay-ms 40 \
  --format mp4 --headless
```

The delay is one-way data propagation, not RTT. Requests and priority controls
are immediate. One shared payload budget serializes an object in
`8 * payload_bytes / (bandwidth_mbps * 1e6)` seconds; propagation overlaps
serialization of later objects. Priority updates apply at object boundaries,
with IMQUIC ties ordered by publisher priority and request ID. Serialized and in-flight objects are not
preempted. Envelope/control bytes are outside this application payload budget.
Bounded queue overflow is a method failure, rather than an implicit rate change.

Time zero is the initial request with an empty scene. Each frame at `k / fps`
advances a rational byte clock, receives all objects due by that simulated time,
awaits object preparation and GPU sorting, and renders the exact trace pose.
Then its camera priorities are acknowledged before the next object selection.
No rendering, encoding or host wait consumes simulated time. The relay cache is
fully populated before the initial FETCHes; only one permitted object is sent
at a time, so host scheduling cannot change the selected object sequence.

Delivery advances through the full trace horizon for counters; video excludes
the endpoint and keeps its final captured scene count separately. Every video
frame is captured as a PNG and encoded at `--fps` without wall-clock
retiming. `--quality-fps` must divide video FPS; quality uses those exact frame
indices and matching full-scene reference poses. Defaults are 30 FPS and CUDA
quality comparison. Missing frames fail the run. `--start-after-first-object`,
base buffering and the removed `--playback-delay-ms` do not apply to ideal runs.

`ideal-events.jsonl` retains native control acknowledgements, serialization,
modeled arrivals and dispatches. `ideal-summary.json` records model
`offline-imquic-v1`; `ideal-delivery.json` reconciles object identities, bytes and
simulated arrivals with browser receipts. `render-frame` telemetry records poses
and applied-object deltas/counts for repeatability checks. Native logs retain operational
transport diagnostics; those are not ideal-link metrics. `validation-summary.json`
keeps video, quality and delivery status independent. Run `make ideal-player-check`
for the real native/player repeatability fixtures. Identical inputs yield
identical modeled delivery and frame membership; pixel repeatability is scoped
to the same renderer and environment.


### Continuous camera priorities

Camera ranking predicts 100 ms ahead from the last 100 ms of motion. Visible
refinements receive the full-precision score
`40 * layer + 8 * screen_distance + 16 * normalized_depth` (lower first).
`screen_distance` is the minimum L-infinity distance from screen center to the
clipped projected tile bounding rectangle: zero when it covers the center, one
at the viewport boundary. Box faces are clipped against all six camera-frustum
planes before perspective division, including tiles crossing the near plane.
This is a bounding-box approximation, not an occlusion test.

`normalized_depth` is camera-to-tile-centroid distance divided by the maximum
centroid distance among all visible tiles. Completed tiles remain in this scale;
outside tiles do not contribute. With zero maximum distance the depth term is
zero. Layer spacing remains 40, so all visible base layers precede visible
higher layers. Outside priorities remain `240 + layer` for layers 0–3. Explicit
legacy base buffering still assigns enhancements 255. Old center/periphery and
near/middle/far labels are retained for diagnostics and do not determine scores.

The wire priority is `floor(score)` for both ideal and QEMU. IMQUIC orders
pending FETCH objects by subscriber priority, publisher priority and request ID.
Initial request lists use the full score to resolve integer ties. In-flight or
serializing objects are not preempted. Ideal priorities update at fixed simulated
frame timestamps; QEMU priorities follow the live trace clock. New run provenance
records the policy and ranking-source hash.

## Local inputs and disposable outputs

`results/` is reserved for new generated experiments and can be cleared after
runs. Persistent local inputs and dependencies live outside it:

- `data/datasets/nthu/`: 13 final source models, all raw NTHU traces and the
  reproducible five-per-scene selection (40 selected traces).
- `data/scenes/`: prepared payloads for all eight configured NTHU scenes. Bicycle
  uses `bicycle-30000/` to match the copied final checkpoint.
- `data/traces/nthu-selected/`: one normalized selected NTHU trace per scene,
  plus input hashes and the reproduction script.
- `data/traces/thesis-scenes-19s/`: the three earlier camera input traces.
- `data/reproduction-scripts/`: preserved script/configuration sources; historical
  paths may need adapting before reuse with fresh data.
- `.venvs/` and `.toolchains/`: Python environments and VMAF build dependencies.

Old experiment captures, metrics, logs, reports and cached QEMU run artifacts
were intentionally removed. Source code, datasets and dependencies remain.

QEMU experiment `controllers` can include `"prague"` (Prague with ECT(1)),
`"reno"` (Reno with ECN disabled), and `"reno-ect0"` (classic Reno with ECT(0)).
`experiments/player.toml` includes all three; each uses the same BBR/Prague
background flows. The launcher also accepts `--transport-mode reno-ect0`.
Reno ECT(0) is a distinct condition in case IDs and reports, including paired
quality comparisons; existing Reno results remain the Not-ECT condition.

The QEMU mode `bbr` selects picoquic BBR with ECN ECT(0) on both relay and subscriber. Earlier retained BBR runs explicitly used Not-ECT; do not pool those with the ECN-enabled condition. The final entry in `experiments/player.toml` schedules one Bicycle BBR sample at 50 Mbps and 30 ms base RTT, separate from the replicated controller comparisons.
## Independent network capture and offline arrival replay

The opt-in `recorded-arrivals-v1` workflow separates real IMQUIC/QEMU receiving
from rendering. The receiver guest runs a Node adapter with the shared camera
and `PriorityAbr` implementation, updating priorities every 100 ms. It FETCHes
all nonempty objects and records their native complete-object arrival times,
identities, byte lengths and verified payload hashes. It has no renderer.

Build the player with `cd deps/gaussian-player && npm run build`, then run from
this repository root (output directories must be new):

```sh
.venvs/3dgs/bin/python tools/l4s/run_network_capture.py \
  --manifest data/scenes/train/manifest-v2.json \
  --trace data/traces/nthu-selected-150mbps/user8_train.json \
  --controller prague --bandwidth-mbps 150 --one-way-delay-ms 10 \
  --cpus 4 --memory-mib 2048 --output results/train-prague-capture

.venvs/3dgs/bin/python tools/l4s/render_arrival_replay.py \
  --manifest data/scenes/train/manifest-v2.json \
  --trace results/train-prague-capture/trace.json \
  --journal results/train-prague-capture/arrivals.json \
  --fps 5 --output results/train-prague-replay
```

Use `--controller bbr` with a separate output for picoquic BBR. Capture uses the
existing topology with both Prague and BBR TCP backgrounds. Defaults are a
20-second trace, 150 Mbps and 10 ms one-way propagation delay (20 ms base RTT).
Replay defaults to hardware GPU, 960x540 and 5 fps. `--skip-reference` isolates
candidate rendering; otherwise a trace-defined cached full reference, strict
SSIM/PSNR comparison and MP4 are generated. VMAF is a separate analysis step.

For dedicated capture workers, both `run_network_capture.py` and the direct
`run_qemu_player.py` wrapper accept `--host-cpus 2-5 --host-numa-node 0
--preallocate-memory --lock-memory`. Combine these with `--cpus 4` and
`--memory-mib 8192` (direct wrapper: `--memory 8192`). The CPU set must be within
the launcher's allowed affinity, contain at least one CPU per guest vCPU, and
belong to the selected NUMA node. QEMU threads inherit the worker CPU set; each
KVM vCPU is pinned to a distinct CPU in ascending order. Guest RAM uses an
unmerged memory backend, optional preallocation, and strict binding to the
requested NUMA node. Locking requires permission to set unlimited
`RLIMIT_MEMLOCK` (usually root); setup fails explicitly if this cannot be set.
These settings also apply during setup-cache preparation and are recorded in
capture provenance and guest source/cache evidence. Set resource flags on the
session owner when using reusable VMs; a case cannot change an existing VM's
placement. Omitting the flags preserves the normal launch behavior.

Replay verifies the manifest and exact trace hashes, then exposes only complete
objects whose recorded arrival time is at or before each logical frame time.
It waits for decoding, dependencies, GPU sorting and PNG persistence without
advancing logical time. A frame membership audit reconciles every eligible
object dispatch and every dependency-ready applied Gaussian against the journal.
This measures network-arrival-limited visual quality, excluding real-time
renderer/decode stalls. Keep it separate from earlier live-render experiments.

The Train pilot in `results/separated-train-20261119/` validated two concurrent
4-vCPU/2-GiB capture VMs with the earlier CUBIC + Prague backgrounds, or two
candidate replay jobs. Capture concurrency with the current BBR + Prague
backgrounds needs separate validation. Both parallel replays
matched their serial RGB frames exactly (100/100 each). Full-reference generation
and quality scoring were tested serially; combined capture/replay load and three
or more workers have not been validated. See that directory's `README.md`,
`parallelism.json` and reproduction scripts for measured timing/resource limits.
