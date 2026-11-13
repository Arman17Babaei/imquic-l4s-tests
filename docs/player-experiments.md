# Configurable player experiments

Edit [`experiments/player.toml`](../experiments/player.toml), then run from the repository root with Python 3.11 or newer:

```sh
# Inspect every expanded case and exact command; creates no outputs or processes.
python3 tools/l4s/run_experiments.py --config experiments/player.toml --dry-run

# Run captures sequentially, then compute quality summaries and plots.
python3 tools/l4s/run_experiments.py --config experiments/player.toml \
  --output results/my-player-suite

# Regenerate plots and VMAF from retained evidence; starts no captures or VMs.
python3 tools/l4s/run_experiments.py --plot-only results/my-player-suite
```

An output directory must be new and below `results/`. Existing batches are never overwritten by capture runs. Replotting replaces derived files under `analysis/`; it preserves original captures. Dependencies for capture/build are described in the main README. Offline plots require NumPy, Pillow and Matplotlib; CUDA SSIM/PSNR requires PyTorch with CUDA, and CUDA VMAF requires a CUDA-enabled libvmaf executable. Select explicit CPU backends to use CPU quality computation; there is no silent CUDA fallback. Scene exports, their referenced objects, traces, and the QEMU image must already exist. The example uses this workspace's existing scene paths; override them when moving the configuration.

`[defaults]` applies to every `[[experiments]]` table. Each table expands `scenes × delays_ms × controllers × reruns`; controllers apply only to QEMU. Use `scene = "playroom"` for one scene, or `scenes = ["bicycle", "train", "playroom"]`. Set a scalar bandwidth, duration and switch count per table; add tables to sweep those values. Runs are sequential. For each scene and repeat, ideal delays run from highest to lowest; QEMU runs Prague then Reno adjacently at each fixed delay (using the configured subset). Each repeat preserves these pairs; there is no global order rotation.

`runner.python` selects the capture and reference-render interpreter. The example points to this workspace's `results/venvs/3dgs/bin/python` CUDA/PyTorch environment; change it for another environment. The suite CLI's interpreter needs the plotting dependencies for analysis.

For example, replace the example's experiment table with these two tables to compare the two paths for playroom:

```toml
[[experiments]]
name = "playroom-no-vm"
scene = "playroom"
path = "ideal"
duration_seconds = 5

[[experiments]]
name = "playroom-network"
scene = "playroom"
path = "qemu"
controllers = ["reno", "prague"]
switches = 3
duration_seconds = 5
```

`duration_seconds` scales timestamps so the last original camera pose occurs at exactly that duration; spatial poses stay unchanged. A 19-second source retimed to 5 seconds runs at 3.8× speed. Recordings use the requested FPS; sparse quality frames use `quality_fps` and a fixed camera-time grid. Rendering can still miss deadlines; retained actual timing and missing-frame coverage expose that limitation.

The suite defaults to the following capture policy:

```toml
[capture]
start_after_first_object = true
max_missing_snapshot_fraction = 0.05
```

With background traffic enabled, every sink must receive payload and the background flows must then run for four seconds before the guest starts scene services. The camera remains at its initial pose and recording stays idle until the first usable scene object is successfully applied. That moment is **time zero** for camera movement, recording, snapshots, the fixed delivery horizon and browser payload-rate bins. Ideal runs use the same first-object gate without a background requirement. This excludes startup loading from the measured video; it does not mean starting with a complete scene. Raw first-receipt/application/start timestamps and initial Gaussian/payload counts remain in telemetry. Delivery summaries subtract the initial counts for newly added data; ideal timing reports have a measurement window while retaining full-run structural audits. Background logs retain their original clocks and warmup evidence.

Set `start_after_first_object = false` to restore request-start playback. The direct launcher exposes `--start-after-first-object`; standalone defaults remain request-start. First-object mode and base-layer buffering are mutually exclusive.

Up to 5% missing snapshots are accepted without filling gaps: a 95-slot capture can miss four slots, while five exceeds the limit. Quality output retains raw `complete`, coverage, missing slots and the configured tolerance. Runtime failure or malformed evidence is not accepted by the tolerance. References and SSIM/PSNR are attempted for valid retained candidate frames even when capture/transport fails or loss exceeds the tolerance, while the original failure remains recorded. Failed selected runs still get plots; missing data panels are labeled. A failed member with no references cannot suppress usable members' VMAF: per-run fallback grids are explicitly marked and their means are not aggregated as comparable conditions. Old captures keep their original start policy and are not silently rebased or regraded by a new configuration.

`path = "ideal"` runs the existing no-QEMU bandwidth-limited delivery path. `delays_ms` delays data delivery in one direction while priority controls remain immediate. It simulates a delivery schedule, not a congestion controller or packet network.

`path = "qemu"` runs full MoQ through a guest Mininet chain `server → s1 → … → sN → client`, once per requested Reno/Prague controller. Both directions of the client-facing link use `bandwidth_mbps`; every other physical link uses 300 Mbps. Every physical egress uses HTB/DualPI2 with target 15ms, update 16ms and step threshold 5ms. `qemu_delay_mode = "endpoints"` (the compatibility default) adds `delay_ms` once per direction using endpoint ingress IFBs. `qemu_delay_mode = "per-link"` adds it at the receiving interface of every physical main-path link in both directions, using separate ingress IFBs. The example QEMU experiment fixes `delays_ms = [10]` with `per-link`: two switches means three links, 30ms one-way and nominal propagation RTT 60ms, before queues and processing. Ideal experiments retain their 320/160ms data-delay comparison. Background traffic is optional and enabled in the example QEMU table. The guest needs IFB/mirred, HTB, netem and DualPI2 support, plus `iperf3` and `ss` for background traffic. Generated source and CPU tests establish the configuration contract; acceptance of guest kernel behavior and measured delay requires an actual QEMU run. Ideal and QEMU delays have different control-path semantics and should not be treated as identical experiments.

Enable background traffic per QEMU experiment:

```toml
background = true
background_cc = "cubic" # Linux TCP algorithm available in the guest: e.g. cubic, reno, bbr.
background_warmup_seconds = 4 # Active payload before scene/client startup.
```

Set `background = false` to disable it (the default). Ideal experiments require it disabled. Background congestion control is independent of the scene's `controllers = ["prague", "reno"]`; these select QUIC controllers, whereas `background_cc` selects the TCP sender algorithm. The guest must support the requested TCP algorithm; iperf failures are logged and fail the guest run, without silently substituting another algorithm. Background source/sink namespaces set `net.ipv4.tcp_ecn=0`.

With N switches, the generator adds N dedicated source/sink pairs. Source i attaches to switch i and its sink attaches to the next switch, so its data shares exactly the scene's forward egress queue at switch i, then leaves the main path. The final sink connects behind the client through a routed 300 Mbps link; no extra switch is added. The client namespace forwards that final background flow. All other added links use 300 Mbps. Each pair has separate processes and sockets; backgrounds do not form a single end-to-end flow sharing all queues.

Each TCP sender targets the experiment's `bandwidth_mbps` using iperf3's `-b`, with one data stream. This is offered target rate, not guaranteed throughput: TCP congestion control and competing traffic determine achieved rate. With the example's 50 Mbps bottleneck, each background aims for 50 Mbps. A 50 Mbps background plus the scene can contend at the client-facing 50 Mbps queue; it need not saturate an upstream 300 Mbps link. [iperf3 bitrate option](https://software.es.net/iperf/invoking.html).

`background_link_delay_ms` adds propagation in each direction on each of the two dedicated access links of every background pair. The example uses 50ms per link: 100ms extra one-way. In `per-link` mode the pair also crosses one shared 10ms main-path hop, so its propagation RTT is nominally 220ms. The final pair crosses the existing client ingress IFB, without duplicating that shared-hop delay at its sink. In `endpoints` mode the old baseline total one-way delay is retained, plus the two extra access-link delays. Access rates remain 300Mbps and each pair still shares exactly one forward scene queue. Backgrounds start before scene services. Startup waits for each flow's control and data sockets plus nonzero payload in every sink's interval log, then warms up for `background_warmup_seconds` (default four seconds); flows stay active until guest cleanup. Startup failures stop already-started background processes and retain logs. `background-config.json`, `background-processes.json`, client/server interval logs and socket snapshots record the requested rate, CC, shared queue, readiness and connections. The guest's existing monitoring catches process exits during the run. Original before/after qdisc snapshots and interface samples retain network evidence. These paths are covered by CPU topology contracts; actual throughput, kernel support and queue independence still require a live QEMU run.

`reruns` is the total number of repetitions, including the first. `on_failure` supports `continue`, `stop` and `retry`; `max_retries` counts additional attempts, and `retry_exhausted` controls what happens after retries. Every attempt, command, exit code, error and log is retained. The first attempt satisfying the configured criterion is selected; if all fail, the last is retained for diagnosis. Selection never uses quality scores.

The failure criteria are:

- `capture`: a recorded video marked PASS exists and the launcher did not raise an exception or time out.
- `quality`: capture plus the launcher's retained quality status must be PASS.
- `all`: capture and quality plus the launcher's overall PASS and zero process exit status.

Timing failures remain visible as PARTIAL even when the chosen criterion permits continuing. Offline VMAF/plotting failures are reported separately after captures and do not rerun captures. CLI exit 0 means the configured capture criterion was met for every item and requested analysis did not block; it does not imply timing-valid evidence. Exit 1 reports execution/analysis failure, 2 a configuration/input error, and 130 an interruption. Interruptions preserve the ledger and partial artifacts; replot them later with `--plot-only`. There is no automatic resume of incomplete batches.

The default resource guard requires 4 GiB available RAM and 5 GiB free disk per attempt, with a 7168 MiB systemd user scope memory limit. Configure these in `[runner]`; set `memory_limit_mib = 0` to disable the scope. `timeout_seconds` is the inner browser/launcher deadline; `attempt_timeout_seconds` limits the whole capture and reference-render process. Per-attempt logs retain failures rather than prevalidating browser object payloads.

Each batch contains `config.json`, `ledger.json`, a README, scaled traces and their hashes under `inputs/`, and original evidence under `runs/<case>/attempt-NNN/`. Generated QEMU topology source is retained with each case. Automated `analysis/` output includes JSON/CSV summaries, PNG/PDF figures, per-run time series, and VMAF commands/provenance. Plots show sparse quality, browser object payload goodput, actual capture timing and applied scene fraction where available. Payload goodput excludes network overhead.

VMAF uses exact timestamp/pose intersections within each scene/bandwidth/duration/path/switch/table group when available. If a failed member prevents a common grid, usable per-run exact pairs are plotted separately and cross-grid mean comparisons are suppressed. Missing samples are excluded without interpolation. Same-player full-scene references assess progressive completeness; sparse-frame VMAF is exploratory and its motion features depend on the retained sample grid. Repeat ranges are descriptive, not confidence intervals. Source capture, quality and timing statuses remain separate from analysis status; timing-invalid captures do not support causal delay claims.

Plots compare Prague and Reno at one fixed QEMU delay per figure. Ideal figures compare the configured delays, ordered highest first. Both include signed ΔVMAF and ΔSSIM panels: Prague minus Reno for QEMU, and higher delay minus lower delay for ideal (320 minus160 for the example). Positive values favor the first named condition. With more than two ideal delays, every higher/lower pair is shown. Differences pair the same repeat, timestamp and camera pose with equal decoded RGB references; VMAF also requires the identical full temporal score grid. Missing members or incompatible grids are explicitly unavailable, without interpolation or substitution. The CSV next to each figure retains pair IDs, source statuses, exact differences and their means.

`[qemu].performance_mode = true` selects `tools/l4s/qemu_player_performance.py` in place of the ordinary QEMU runner. It applies the host `powerprofilesctl` performance profile during the run and restores the previous profile on exit. It uses the established seven-vCPU mapping onto host CPUs 4–10, with QEMU I/O on CPU11, and two native build jobs. These CPUs must be available to the process. The guest disables GRO/GSO/TSO on physical and IFB interfaces, pins OVS to guest CPU2, assigns transport and background processes to the recorded guest CPUs with nice -10, and raises network softirq worker priority. Prague relay and subscriber use `pacing_interval=1/4000` (250µs), unless explicitly overridden by their congestion-option environment variables. Both controllers use the same performance profile and DualPI2 target 15ms/update 16ms/step 5ms/coupling factor 1. Host affinity and power-profile evidence is saved under each attempt's `qemu-performance/`; guest profile, exact congestion options, qdisc states and background link delays are retained with guest results. Failures applying these settings surface in the normal run logs. Setting `performance_mode = false` uses `[qemu].runner` and omits guest CPU/priority/offload tuning. This configuration does not establish a throughput improvement without a new run.
