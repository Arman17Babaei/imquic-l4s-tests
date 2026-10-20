# Current player and Prague experiment setup

The current native diagnostic uses one OVS switch, five hosts, every physical
link direction limited to20Mbps, and DualPI2 on all ten physical egresses:
**target15ms, update16ms, step5ms, coupling1, Classic protection10%**.
HTB burst is32KiB with kernel-default cburst (observed1600B). Each endpoint has
10ms ingress IFB propagation delay, giving **20ms added RTT** on the main and
both background paths. GRO/GSO/TSO are disabled.

## Run the committed setup

```sh
python3 tools/l4s/run_sustained_profile.py
# Stage source snapshots without starting a VM:
python3 tools/l4s/run_sustained_profile.py --prepare-only --output results/profile-inputs
```

The entry point creates a fresh directory, snapshots the inputs, launches the
cached QEMU setup with memory/CPU guards, monitors host affinity and priorities,
and validates the two completed cases. No manual companion monitor is needed.
It requires the existing repository QEMU prerequisites, user systemd, KVM and
host CPUs4–11 available. Keep the laptop in **performance** mode; the launcher
does not change host power settings. Large outputs remain under ignored
`results/`. Do not reuse an existing output directory.

The source bundle is `tools/l4s/sustained_profile/`. This native diagnostic is
separate from the default scene/video launcher and does not regenerate videos.
The existing two-switch thesis and historical shared-bottleneck presets remain
available for scene experiments; they are different network configurations.

## Retained transport changes

- Reno initial cwnd58,500B and Prague47,250B come from the requested expected
  rate×measured RTT/8:13Mbps×36ms and18Mbps×21ms. They seed initialization/reset,
  not a fixed target/minimum rate. Prague previously started at15,360B; Reno
  previously used256,804B. The common QUIC initial window remains unchanged.
- Prague alpha starts/resets at1 and retains the existing EWMA:
  `alpha = 15/16 * alpha + fraction_CE/16`. Unmarked slow-start growth now uses
  zero ECN suppression so conservative initial alpha cannot freeze startup.
  Focused tests exercise initialization, reset, decay, fractional CE and growth.
- The experiment disables `sudden_ce_threshold`, retaining the EWMA response
  without the existing abrupt/suspect-idle shortcuts. Library defaults remain
  unchanged. Two runs compare explicit `ce_response=1/2` and`1/4`; neither is
  inferred from a controller label.
- Publisher pacing uses existing option `pacing_interval=1/4000` (250us).
  Its byte budget is `clamp(rate_Bps/4000, one_MTU, 16*MTU)`. The observed1440B
  floor serializes in0.576ms at20Mbps: this reduces permitted burst size but
  does not cap average rate or guarantee a250us event-loop wakeup.

Objects are16KiB, outstanding target160KiB, publisher duration21s and receive
observation approximately19s. The receiver uses the same default Prague
settings in both cases. This is a research fork/profile, not a claim of complete
Prague draft conformance. Fixed-point precision and virtual-RTT behavior were
not rewritten. Broader `l4s_prague` / `l4s_prague_ewma` simulation bounds were
already failing in earlier comparisons; passing focused tests does not make
that whole suite green.

## CPU scheduling and background traffic

| Role | Guest CPU | Host CPU |
|---|---:|---:|
| Server: Prague publisher and first Cubic source |0|6|
| Main client |1|8|
| OVS switch/database and switch RX steering |2|10|
| Normal guest housekeeping/measurement |3|4|
| Second Cubic source |4|5|
| First Cubic sink |5|7|
| Second Cubic sink |6|9|
| QEMU main/I/O |—|11|

Network applications, OVS threads and the six networking CPUs' ksoftirqd workers
use guest nice−10. RX steering follows these assignments. QMP identifies and
pins vCPU threads to distinct physical host cores. Host CPUWeight1000 raises
relative scheduling weight; cores remain shared with other laptop applications.
Kernel links/qdiscs have no individual userspace PID. RPS does not pin every
transmit/timer operation, and kernel housekeeping is not fully isolated.

Two unlimited non-ECN Cubic flows use the original iperf commands, two-second
warmup and connection-readiness checks. TCP5201 shares the server's20Mbps
link with Prague. TCP5202 uses separate switch ports; its throughput is not
part of the first link's20Mbps budget. Both background paths get the same
20ms added RTT. Per-second checks cover all live OVS and application threads.

Guest RAM stays2GiB with7vCPUs; native builds use2jobs to reuse the source-keyed
setup cache. Unit memory high2800MiB, maximum3GiB, swap maximum128MiB;
preflight requires3.5GiB available and a guard stops this experiment if host
available memory drops below2GiB. Only this experiment's unit is stopped.

## What the observations showed, and why

Configuration equivalence came first: earlier scene comparisons used FIFO at
s2 for Reno and DualPI2 for Prague. Those saved videos retain their provenance;
current presets use DualPI2 for both. Controller labels alone cannot establish
comparable network conditions, nor guarantee a Prague advantage.

The250us pacing experiment improved the default CE-response observation from
9.65 to16.76Mbps, consistent with smaller burst-induced queues/marking, but the
gentler response changed differently. This was not repeated causal isolation.

Switch diagnostics found most ordinary queued data at the shaped **server**,
not at switch→client. Sampled switch L4S delay peaked around2.9ms; server delay
around12.6ms. Large83–113ms RTT episodes coincided with guest runnable wait,
host vCPU wait and guest steal. Thus `RTT - propagation` cannot all be assigned
to serialized switch backlog. Profiling itself consumed significant guest CPU,
so these diagnostics support a scheduling explanation without proving every
original spike had that cause. Raw corrected netlink decodes are retained.

With CPU pinning, a subsequent performance-mode retest raised throughput from
13.85/17.49 to18.98/19.56Mbps and reduced post-start peak RTT from51.3/26.8 to
23.2/22.5ms. The old power mode was unrecorded and host memory/load also changed;
power mode alone was not isolated. Prioritizing all network processes and
separating housekeeping then produced similar18.97/19.47Mbps observations.

| CE response | No background, step1ms | No background, step5ms | Step5ms with backgrounds | Sharing Cubic | Independent Cubic |
|---|---:|---:|---:|---:|---:|
|1/2|18.971|19.601|16.354|3.610|19.956|
|1/4|19.469|19.760|16.955|2.918|19.910|

Values are received-interface Mbps, including headers/control. Raising step
1→5ms slightly increased throughput but median RTT rose about4ms
(20.61→24.68 and20.90→25.10ms). Restoring competition gave part of the shared
link to Cubic; main+sharing Cubic was approximately19.96/19.87Mbps.
Background hosts also gained separate vCPUs, so this is descriptive evidence,
not a single-factor estimate or statistically established controller ranking.

Latest background runs: median RTT20.83/21.26ms, post-first-second peaks
23.15/25.06ms; publisher loss/spurious counters zero. Payload, live queue/delay,
connection, pacing, priority and affinity checks passed. Host peak memory was
about1.31GiB with zero swap; VM and monitors stopped cleanly.

## Player and harness changes retained

Playroom's slow/incomplete start was addressed with shared bounded SH3 render
buffers (32,768 Gaussians) and optional `--start-after-base`: buffer every tile's
base layer before starting the camera trace. This changes startup policy and
excludes buffering time from the video; telemetry records it. It does not mean
all refinements were delivered. Buffers can contain multiple spatial tiles;
quality dye indicates recent buffer activity, not exact per-splat arrival age.

Fixed-horizon delivery now distinguishes completion at video end from eventual
completion, including objects whose asynchronous hashing crosses the deadline.
The HUD displays tile(x,y,z,q) and trailing100ms application payload goodput;
this excludes transport overhead and is not the configured bandwidth. Reference
videos use the whole scene in the same browser renderer. Queue commands use
separate processes to avoid races in Mininet's shared command shell. Plot
captions derive queue types/parameters from each recorded run.

The QEMU cache is invalidated by actual imquic/picoquic/picotls contents and
setup recipe/environment identity. Runtime player/topology inputs are staged
on fresh child overlays; native checks still rerun. Only cleanly powered-off,
successfully prepared setup images are published. This saves repeated native
builds without treating old setup metadata as proof of a new result.

## Preserved evidence

Raw results/videos remain untouched under `results/`, including:

- `thesis-scenes-resume-20261018/`: original scene matrix, videos and reviews.
- `prague-switch-diagnostics-20261020/` and `prague-switch-fast-20261020/`:
  queue/CPU diagnostics, corrected decodes, plots and instrumentation caveats.
- `prague-network-cpu-power-20261020/`, `prague-all-network-priority-20261020/`,
  `prague-all-network-step5-20261020/`: comparisons above.
- `prague-step5-background-20261020/`: latest analyses, source snapshots,
  background process manifest, priorities and host memory/affinity samples.
- `l4s/prague-step5-background-20261020T134005Z/`: completed guest payload,
  tc/IFB state, background connections, metrics and provenance.

The many intermediate diary reports and resume plan were consolidated here;
failed attempts remain in raw results. The comparison data above predates consolidation; fresh entry-point validation
is recorded below.


## Commit validation

- `make cpu-check`: PASS, including65 Python tests, player tests/build and
  bridge/playback browser checks.
- Native sidecar unit fixture and both publisher/sidecar C syntax checks: PASS.
- Rebuilt `picoquic_ct prague_options prague_dynamics`:2tests,0failures.
- Full host `make -C deps/imquic check`: PASS with `PICOQUIC_LIBS` pointing
  at `deps/picoquic/build` and its `_deps/picotls-build` libraries. The plain
  command initially failed because generated configure settings referenced
  absent root-level library symlinks; no transport source change was required.
- The new tracked entry point completed both background cases, automatic host
  monitors and both artifact validators: PASS. Fresh output is
  `results/current-setup-commit-validation-20261020/`; `complete.json` records
  the separate raw guest directory. Peak swap0; VM/monitors stopped cleanly.
- Preparation-only staging matched the validated guest inputs and transport
  source snapshots. Whitespace checks passed in the root and both changed
  submodules. Older broad Prague simulation limits remain as described above.
