# Historical experiment workflows

These workflows are retained for existing evidence replay. The maintained
player/video/similarity workflow is documented in [the main README](../README.md).
Commands below assume the repository root. Historical measurements remain scoped
to their recorded revisions and topology.

# IMQUIC L4S tests

This repository is a reproducible L4S/Prague experiment harness for IMQUIC.
It pins the transport implementations as submodules and owns the traffic
fixture, QEMU/Mininet runners, analyzers, reports, and reproducible evidence.

- `deps/imquic` — IMQUIC with the Prague configuration and metrics API.
- `deps/picoquic` — the authoritative, pinned picoquic checkout.
- `tests/l4s-test.c` — mirrored into `deps/imquic/src/l4s-test.c`; provides
  loopback and network traffic modes for the experiment binary.
- `tools/l4s/` — experiment runners and analyzers.
- `l4s/` — tracked reports and compact reference evidence.
- `results/` — ignored live-run output: captures, logs, CSVs, and plots.

Do not initialize IMQUIC's nested picoquic dependency: the top-level
`deps/picoquic` submodule is the checkout used by this harness.

## Scenarios inspected

The harness uses the same 4 MiB bidirectional QUIC STREAM fixture in all live
scenarios. The client sends a deterministic payload, the server echoes it,
and the client verifies every byte before reporting transport metrics. The
implementation is in [`tests/l4s-test.c`](../tests/l4s-test.c), mirrored in
[`deps/imquic/src/l4s-test.c`](../deps/imquic/src/l4s-test.c).

| Scenario | Purpose | Implementation method | Required evidence |
| --- | --- | --- | --- |
| Fixture and loopback smoke test | Verify Prague endpoint construction, option validation, payload integrity, and populated metrics without a bottleneck. | `imquic-l4s-test` with no arguments; built and invoked by `make check` in `deps/imquic/src/Makefile.am`. | Invalid Prague and ECN options reject; 256 KiB loopback payload is echoed; cwnd, pacing, and Prague metrics are populated. |
| Prague time series | Verify an actual L4S response, rather than only controller arithmetic. | [`run_timeseries_test.sh`](../tools/l4s/run_timeseries_test.sh) creates sender/router/receiver namespaces. Both router egresses use HTB plus real `sch_dualpi2`; the fixture uses Prague with ECT(1). | Forward ECT(1), reverse CE, ACK_ECN CE feedback, changing Prague alpha, and at least one CE-associated cwnd reduction. |
| Reno / Not-ECT coexistence | Establish the classic non-ECN control path under the same bottleneck and TCP background. | [`run_mininet_benchmark.py`](../tools/l4s/run_mininet_benchmark.py) launches the fixture as `l4s-off` (`reno`) in `client -- s1 -- server`. | No ECT(0), ECT(1), or CE feedback in QUIC; the background TCP flow remains Not-ECT. |
| Reno / ECT(0) coexistence | Separate classic ECN behaviour from Prague and L4S queue classification. | The same Mininet harness launches `l4s-ect0` (`reno-ect0`). DualPI2 classifies it in the classic queue. | Forward ECT(0), CE marks, and QUIC ACK_ECN CE feedback; no ECT(1). |
| Prague / ECT(1) coexistence | Measure L4S behaviour under the same competing classic TCP load. | The same Mininet harness launches `l4s-on` (`prague`) with ECT(1), which DualPI2 classifies in its L4S queue. | Forward ECT(1), CE marks, ACK_ECN feedback, and nonzero L4S qdisc packet/mark counters; no ECT(0). |

The coexistence matrix runs each mode at 0, 5, 10, and 20 Mbit/s requested TCP
background load, with five repetitions by default (60 cases).
The background is iperf3/TCP BBRv1 on port 5201 with ECN disabled. Each case
creates fresh HTB and DualPI2 qdiscs, so their counters do not carry into the
next case. Use `--background-congestion reno`, `cubic`, or `bbr2` to select an
alternative TCP baseline; the BBRv2 choice loads `tcp_bbr2` and verifies that
iperf3 reports `bbr2`.

## How the analysis works

### Time-series analysis

[`analyze_timeseries.py`](../tools/l4s/analyze_timeseries.py) reads the
10 ms `metrics.csv` written by the client fixture. It rejects a run unless its
timestamps increase, ECT(1)/CE feedback counters are monotonic and nonzero,
Prague alpha changes, cwnd changes, and at least one cwnd reduction follows CE
feedback. The runner independently uses tshark to require capture-visible
ECT(1) and CE packets. The output is `analysis.json`, packet counts, qdisc
counters, endpoint logs, and captures.

### Coexistence analysis

[`analyze_mininet_benchmark.py`](../tools/l4s/analyze_mininet_benchmark.py)
combines the per-case `metrics.csv`, metadata, DualPI2 counters, iperf3 JSON,
and switch captures. Captures are header-only (128-byte snap length), which
retains Ethernet/IP/TCP/UDP headers and `ip.len` while avoiding guest-disk
exhaustion during the complete matrix.

For every case the analyzer verifies:

- `l4s-off`: no QUIC ECN marking or CE feedback.
- `l4s-ect0`: capture-visible ECT(0), CE, and endpoint CE feedback; no ECT(1).
- `l4s-on`: capture-visible ECT(1), CE, endpoint CE feedback, and nonzero L4S
  qdisc classification; no ECT(0).
- The background TCP flow has no ECN-marked packets.
- Every selected mode reaches at least 70% observed bottleneck utilisation.
- The matrix contains every requested rate/mode/repetition exactly once.

QUIC goodput is payload bytes divided by the client metric interval. Concurrent
wire load is calculated from client-to-server QUIC and TCP IP bytes in the
exact wall-clock interval of the QUIC transfer, so differently timed averages
are not combined. QUIC forward drops are inferred from the packet-count deficit
between the two switch-port captures. TCP GSO can coalesce ingress segments, so
background TCP drops are inferred from forward retransmissions instead. The
forward DualPI2 drop counter is retained as an attribution cross-check.

Outputs are `summary.csv` (each run), `aggregate.csv` (mean and sample standard
deviation per mode/load), `analysis.json`, and `comparison.svg`.

## Recreate the results

### Build and fast validation

```sh
make init
make analyzer-check
make build
```

`make build` configures IMQUIC against the top-level picoquic checkout.
The QEMU commands below run the full `make check` suite in their configured
guest before each live network experiment. In a separately configured IMQUIC
checkout, `make -C deps/imquic check` runs the fixture and both analyzer
self-tests; it requires picoquic and picotls libraries to be exposed at the
paths selected by that checkout's `./configure --with-picoquic=...` command.

### Lightweight CPU-only player check

With Python 3 and Node.js 20.19+ (or 22.12+), install the locked dependencies once:

```sh
npm ci
npm --prefix deps/gaussian-player ci
npx --no-install playwright install chromium
make cpu-check
```

Chromium also needs its normal OS libraries (on supported Linux systems,
`npx --no-install playwright install-deps chromium` can install them). The check
itself runs without root, CUDA, a GPU, native transport builds, QEMU, Mininet,
or downloaded scenes. After setup it is intended to complete within a minute.
It builds the player and tests two synthetic hashed SH3 objects using forced
SwiftShader, asserting the renderer, Play gating, progressive loading, late
objects, reloads, and streamed/HTTP frame consistency. Focused player, bridge,
camera, and lifecycle unit tests run too. Browser case summaries go under
`results/cpu-check/`.

This checks the player and real WebSocket bridge with simulated native delivery;
it does not validate QUIC, L4S performance, ECN marking, or CUDA reference quality.
`make analyzer-check` remains the separate lightweight scientific-analyzer gate.

### Prerequisites for live tests

The preferred route is the QEMU wrapper. It requires:

- `qemu-system-x86_64`, `qemu-img`, `ssh`, `scp`, and Python `pexpect` on the
  host;
- a base guest image at `~/sandbox/p4/work.qcow2` (override with `--base`);
- a guest with the Prague-enabled `sch_dualpi2` kernel module, Mininet, Open
  vSwitch, iperf3, tcpdump, and tshark;
- a clean Git worktree. The wrapper archives the committed revisions, creates
  a disposable qcow2 overlay, and removes that overlay after the run.

The wrapper provisions build dependencies in the guest when required. It uses a
three-minute authenticated-SSH wait by default; override it with
`--boot-timeout` if the guest needs longer.

### Run the Prague time-series scenario

```sh
python3 tools/l4s/run_qemu_timeseries_test.py \
  --make-target l4s-timeseries-guest-check \
  --destination-prefix qemu-timeseries
```

The completed output is copied to
`results/l4s/qemu-timeseries-<timestamp>/`.

### Run the complete coexistence matrix

```sh
python3 tools/l4s/run_qemu_timeseries_test.py \
  --make-target l4s-mininet-benchmark-guest-check \
  --guest-result-name mininet-run \
  --destination-prefix qemu-mininet
```

The completed output is copied to `results/l4s/qemu-mininet-<timestamp>/`.
Open `comparison.svg`, inspect `analysis.json`, or use `summary.csv` and
`aggregate.csv` for further analysis.

For a directly provisioned host that already has the required root-only
networking dependencies, run:

```sh
sudo python3 tools/l4s/run_mininet_benchmark.py \
  --output results/l4s/mininet-run
```

Use `--background-mbps`, `--background-congestion`, `--repetitions`,
`--foreground-seconds`, `--background-warmup-seconds`, `--transfer-bytes`,
`--bottleneck`, and `--modes` to select a smaller or custom matrix. The QUIC
fixture queues more data than the bottleneck can deliver, transmits continuously
for the foreground duration, and records echoed application bytes. Background
TCP starts before QUIC and remains active beyond the foreground interval. The
analyzer rejects cases whose captured QUIC traffic does not span at least 90%
of that interval. Use `unlimited` as a background rate to omit iperf's pacing
cap while retaining the selected TCP congestion controller. For example:

```sh
sudo python3 tools/l4s/run_mininet_benchmark.py \
  --output results/l4s/mininet-smoke \
  --background-mbps 0,10 --repetitions 1
```

Generated results remain ignored by Git. Only update the compact tracked
evidence in `l4s/` from a completed reproducible run, with the environment and
submodule revisions recorded in [`l4s/validation-report.md`](../l4s/validation-report.md).

### View camera-driven 3DGS loading

The browser visualization launcher uses the 10x10x3 spatial grid with four
disjoint quality refinements. It starts a new relay, publisher, native sidecar,
no-store HTTP server, and isolated Chromium context for each invocation. The
trace autoplays with smooth camera interpolation; ABR decisions remain aligned
to the exact 100 ms samples. Arriving refinements can be quality-tinted for one
second and the scene/HUD can be recorded to WebM, MP4, or GIF:

```sh
make build-3dgs-priority-mvp
python3 tools/l4s/run_camera_trace_player.py \
  --manifest results/3dgs/camera-priority-bicycle/manifest-v2.json \
  --trace results/3dgs/camera-priority-bicycle/camera-trace.json \
  --output results/camera-trace-player/bicycle-visual \
  --format all
```

The same launcher replaces the older MVP loopback demo and separate validation
launcher. For a software-rendered native MoQ smoke, use `--no-record --headless
--gpu-mode software` with a prepared manifest and trace. The distinct
`make priority-audit-loopback-check` gate remains available for native accepted
priority-update validation.

Use `--no-dye --no-record` when their rendering overhead is unwanted. Recorded
playback is explicitly visualization evidence and must not be treated as a
network-performance result.

### Full SH3 player validation

Export the original PLY once into the existing spatial grid and disjoint
refinements. The manifest explicitly selects `payload_encoding: "gsp2"`;
manifests without that field retain legacy SPLAT decoding. GSP2 objects carry
all 16 RGB SH bases, original source IDs, log scales, opacity logits, and wxyz
quaternions. Each object is bounded to 64 KiB and validated by identity, size,
SHA-256, dimensions, and Gaussian count before application.

```sh
results/venvs/3dgs/bin/python tools/l4s/prepare_player_sh3.py \
  --source data/point_cloud.ply --output results/player-sh3-new
cd deps/gaussian-player
npm test
npm run build
cd ../..
python3 tools/l4s/run_camera_trace_player.py \
  --manifest results/player-sh3-new/manifest-v2.json \
  --trace deps/3dgs_over_moq/assets/user102_bicycle_500.json \
  --output results/player-http-new --transport http --fixed-frame --headless --no-record
results/venvs/3dgs/bin/python tools/l4s/compare_player_frame.py \
  --source data/point_cloud.ply \
  --trace deps/3dgs_over_moq/assets/user102_bicycle_500.json \
  --run results/player-http-new
```

The fixed capture uses source frame 0, black background, no overlays or dye,
and waits for complete ingestion and settled sorting. CUDA is required for
the repository's exact SH3 reference renderer. `comparison.json` records RGB
SSIM, PSNR, hashes, and the full comparison configuration; the rendering gate
is SSIM ≥ 0.90. Both images and the absolute RGB difference are retained.
On small GPUs, reference preparation uses gsplat's identical packed projection
to discard only zero-contribution Gaussians before one global render. A CUDA
regression compares this path with an unculled SH3 render byte for byte.
Add `--camera-check` to a fixed-frame run to capture sampled recorded poses and
the 0/15/25/50-second yaw knots; the runner checks each effective view matrix
against the source and saves the images and camera-validation JSON.

Geometry and bounds remain in source coordinates. The browser view transform
is `diag(-1,-1,1,1) × source_view`, derived from the reference renderer's
effective transform. Recorded motion uses position interpolation and quaternion
slerp. Regenerate older synthetic yaw traces with
`generate_camera_priority_trace.py`; they used a different coordinate rotation.
The 50-second sequence rotates about the converted source up axis.

For the isolated QEMU path, use the same export and trace with:

```sh
python3 tools/l4s/run_camera_trace_player.py \
  --manifest results/player-sh3-new/manifest-v2.json \
  --trace deps/3dgs_over_moq/assets/user102_bicycle_500.json \
  --output results/player-qemu-prague-new --transport moqt --network-path qemu \
  --transport-mode prague --fixed-frame --headless --bottleneck 100mbit --no-record
```

Repeat with `--transport-mode reno` and a new output directory for the control.
The default bottleneck is 20 Mbit/s; the explicit 100 Mbit/s setting permits a
bounded full-scene validation. The host serves UI and metadata only. Gaussian
bytes travel through the forwarded WebSocket to the guest IMQUIC subscriber,
across the shaped Mininet/DualPI2 path to the relay and publisher. Every run
allocates independent ports and records endpoint addresses, controller settings,
object hashes, transport metrics, and before/after qdisc counters. Delivery
success and ECT(1)/CE behavior must be assessed separately. Raw-file loading
supports HTTP; MoQ requires the validated manifest/object path.

The browser runner uses hardware acceleration by default and records the
graphics renderer in telemetry. Its `--gpu-mode software` option permits an
explicit SwiftShader capture when hardware WebGL is unavailable.

For gradual loading over a 10 Mbit/s QEMU path, serve an interactive endpoint:

```sh
results/venvs/3dgs/bin/python tools/l4s/prepare_player_sh3.py \
  --source data/point_cloud.ply --output results/player-sh3-interactive \
  --max-tile-gaussians 16384

python3 tools/l4s/run_camera_trace_player.py \
  --manifest results/player-sh3-interactive/manifest-v2.json \
  --trace deps/3dgs_over_moq/assets/user102_bicycle_500.json \
  --output results/player-moq-10mbps-new --transport moqt --network-path qemu \
  --transport-mode prague --bottleneck 10mbit --interactive --timeout-seconds 43200 --no-record
```

The launcher prints the browser URL and writes `endpoint.json` once the guest
bridge is ready. The page uses `play=1&dye=1`: Play starts loading, and each
new validated object receives its quality color for one second. Before Play the page
requests no scene metadata or Gaussian objects and opens no MoQ WebSocket;
the native subscriber also starts lazily. UI assets still load normally.
Camera controls remain available during loading. Reloading returns to the
Play gate and starts a fresh subscriber after the next click. The endpoint is
bounded by `--timeout-seconds`; stop its launcher to stop its owned services.

Dense cells of the original grid are split spatially into bounded tiles, preserving
every source Gaussian and all SH3 coefficients. In-view refinements precede
off-screen base data; interactive camera changes update priorities even when the
trace supplies only the starting pose. Objects render as they validate, while
later refinement layers retain their dependencies. The bridge uses a bounded
16-object delivery window; the HUD reports payload MB and average Mbps. Add
`tileLabels=1` to display tile names when debugging priorities.

The player path places standalone 40 ms netem delay on host egress and HTB with
DualPI2 on switch egress, preserving nominal 80 ms RTT. This avoids the guest
kernel's netem parent accounting underflow on child AQM drops. Player relay
admission is bounded to 256 KiB plus one object; general relay runs retain the
64 KiB default. The player opts into Prague `pacing_interval=1/1000`: bursts hold one millisecond
of paced data, with a one-MTU minimum; small windows are paced too. Its congestion
response parameters and the general Prague default remain unchanged. This profile
avoids large packet trains that prematurely trigger the 1 ms L4S step threshold. Record the controller source revision and queue state
when comparing these runs with historical evidence.

Omit `play` or use `play=0` for automatic startup. Automated validation retains
its existing default of no Play gate, no overlays, and no dye. The browser
regression `node --test tests/test_player_playback.mjs` checks both startup
modes, pre-Play inactivity, dye activation, late objects, and bridge reuse.

## Sustained MoQ coexistence

The sustained test uses one server-side MoQ namespace/track and an explicit
client subscription. The publisher keeps ten bounded 16 KiB sequential
objects outstanding across QUIC's wire flight and stream-send queue, avoiding
application-side polling gaps while IMQUIC remains responsible for pacing and
the congestion-controlled wire flight during the 60-second transfer. The
subscriber validates object order and payload contents. Transport metrics,
including queued stream bytes, are sampled every 10 ms.

The default matrix crosses `l4s-off`, `l4s-ect0`, and `l4s-on` with two
classic TCP modes—Not-ECT and ECN-capable ECT(0)—and runs three repetitions,
for 18 cases. The ECN-capable TCP case enables conventional negotiated ECN;
it explicitly rewrites only the test flow to ECT(0), so it never uses ECT(1)
or an L4S congestion controller. The MoQ publisher and the paced 10 Mbit/s
TCP iperf3 sender both run on the server and send to the client, so their data
packets share the same server-to-client HTB + DualPI2 bottleneck. TCP starts
five seconds before the 60-second MoQ overlap and continues five seconds
afterward:

```sh
make build
sudo python3 tools/l4s/run_sustained_coexistence.py \\
  --output results/l4s/sustained-moq \\
  --tcp-ecn-modes not-ect,ect0
```

For the prepared QEMU guest, the host-side wrapper needs no privilege. It
archives the repository and submodule revisions with the run:

```sh
python3 tools/l4s/run_qemu_timeseries_test.py \\
  --make-target l4s-sustained-moq-check \\
  --guest-result-name sustained-moq
```

The full 18-case suite has a 30-minute guest-command allowance by default. To
override it explicitly, add `--guest-timeout 1800` to the wrapper command.

Each case stores metrics, iperf3 JSON, packet captures, timestamps, DualPI2
counters, `timeline.csv`, and `timeline.svg`. The analyzer aligns TCP
intervals to the recorded start time and resamples IMQUIC's 10 ms samples into
one-second overlap bins. Cwnd values are compared diagnostically as
byte-valued sender reports, not as identical controller semantics. The
timeline's capture panel shows per-second ECT(0), ECT(1), and CE packet counts
separately for MoQ and TCP. It also writes `summary.json` with per-repetition
evidence and foreground/TCP-ECN aggregates, plus `aggregate.svg` with scenario
mean bars and individual-repetition points for foreground/TCP rate, bottleneck
utilisation/share, and maximum cwnd:

```sh
python3 tools/l4s/analyze_sustained_coexistence.py --self-test
```

The analyzer enforces 18 completed cases, 60-second foreground coverage,
non-empty captures, TCP measurements in at least 90% of overlap bins, and a
14--21 Mbit/s combined-wire-rate tolerance. TCP's measured overlap rate and
the number of bins with delivered TCP wire traffic are reported as coexistence
outcomes, rather than treated as fairness pass/fail thresholds. It also checks
the foreground Not-ECT, ECT(0), and Prague ECN/CE invariants, Prague's L4S
classification plus CE-associated cwnd reduction, and that TCP is either
wholly Not-ECT or conventional ECT(0) without ECT(1), as selected. If an
acceptance condition fails, it still writes every timeline,
SVG, and `summary.json`; the latter records `acceptance_passed: false` and the
exact per-case reasons, while the analyzer exits nonzero.

## Incremental independent TCP streams

The step-join experiment starts ten independent, unlimited iperf3 TCP
connections across ten phases. `stream_01` starts with phase 1, `stream_02`
joins at phase 2, and so on; every connection remains active through the common
experiment endpoint. Each client has its own process, socket, client port, and
server port, so every stream has an independent congestion window rather than
sharing transport state through iperf3 parallel mode. Reno is the default.

Phases are five seconds and the experiment runs three repetitions by default,
giving a 50-second timeline per repetition. The topology and 20 Mbit/s
HTB+pfifo bottleneck match the Reno fairness baseline, and all test traffic is
forced to Not-ECT. Run it directly on a provisioned host with:

```sh
sudo python3 tools/l4s/run_reno_step_join.py \
  --output results/l4s/reno-step-join
```

Use `--phase-seconds`, `--repetitions`, `--bottleneck`, and
`--fifo-limit-packets` to override the defaults. Use `--congestion cubic` or
`--congestion bbr` to run the same independent-connection schedule with those
TCP controllers; Reno remains the default. BBR is loaded with `tcp_bbr` when
available, and every selected controller is verified against the guest kernel
before a run starts. The equivalent QEMU-wrapper invocation is:

```sh
python3 tools/l4s/run_qemu_timeseries_test.py \
  --make-target l4s-reno-step-join-check \
  --guest-result-name reno-step-join \
  --destination-prefix qemu-reno-step-join
```

Each repetition contains the iperf3 JSON reports, packet capture, qdisc
statistics, event log, metadata, `timeline.csv`, and two-panel SVG/PNG plots of
per-stream throughput and cwnd. The root additionally contains
`aggregate_timeline.csv`, aggregate SVG/PNG plots with mean and sample-standard-
deviation bands, and `analysis.json`. Throughput is calculated in 250 ms bins
from forward IPv4 bytes observed at the bottleneck; cwnd comes from each
sender's one-second TCP_INFO reports.

The dependency-light schedule, parser, aggregation, and rendering checks are:

```sh
make reno-step-join-check
python3 tools/l4s/analyze_reno_step_join.py --self-test
```

## Reference evidence

The tracked historical summary is [the Mininet report](../l4s/mininet-benchmark.md)
and its [comparison plot](../l4s/qemu-evidence/mininet-benchmark-comparison.svg).
It is descriptive evidence from the pinned environment, not a formal
statistical significance claim.
