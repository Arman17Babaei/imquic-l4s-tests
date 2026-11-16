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

`[defaults]` applies to every `[[experiments]]` table. Each table expands `scenes × delays_ms × controllers × reruns`; controllers apply only to QEMU. Use `scene = "playroom"` for one scene, or `scenes = ["bicycle", "train", "playroom"]`. Set a scalar bandwidth and duration per table; add tables to sweep those values. Runs are sequential. For each scene and repeat, ideal delays run from highest to lowest; QEMU runs Prague then Reno adjacently at each fixed delay (using the configured subset). Each repeat preserves these pairs; there is no global order rotation.

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
controllers = ["prague"]
duration_seconds = 5
```

`duration_seconds` scales timestamps so the last original camera pose occurs at exactly that duration; spatial poses stay unchanged. A 19-second source retimed to 5 seconds runs at 3.8× speed. Recordings use the requested FPS; sparse quality frames use `quality_fps` and a fixed camera-time grid. Missing-frame coverage is recorded explicitly.

The suite defaults to the following capture policy:

```toml
[capture]
start_after_first_object = true
max_missing_snapshot_fraction = 0.05
```

With background traffic enabled, every sink must receive payload and the background flows must then run for four seconds before the guest starts scene services. The camera remains at its initial pose and recording stays idle until the first usable scene object is successfully applied. That moment is **time zero** for camera movement, recording, snapshots, the fixed delivery horizon and browser payload-rate bins. Ideal runs use the same first-object gate without a background requirement. This excludes startup loading from the measured video; it does not mean starting with a complete scene. Raw first-receipt/application/start timestamps and initial Gaussian/payload counts remain in telemetry. Delivery summaries subtract the initial counts for newly added data; object delivery is audited independently of snapshot capture. Background logs retain their original clocks and warmup evidence.

Set `start_after_first_object = false` to restore request-start playback. The direct launcher exposes `--start-after-first-object`; standalone defaults remain request-start. First-object mode and base-layer buffering are mutually exclusive.

Up to 5% missing snapshots are accepted without filling gaps: a 95-slot capture can miss four slots, while five exceeds the limit. Quality output retains raw `complete`, coverage, missing slots and the configured tolerance. Runtime failure or malformed evidence is not accepted by the tolerance. References and SSIM/PSNR are attempted for valid retained candidate frames even when capture/transport fails or loss exceeds the tolerance, while the original failure remains recorded. Failed selected runs still get plots; missing data panels are labeled. A failed member with no references cannot suppress usable members' VMAF: per-run fallback grids are explicitly marked and their means are not aggregated as comparable conditions. Old captures keep their original start policy and are not silently rebased or regraded by a new configuration.

`path = "ideal"` runs the existing no-QEMU bandwidth-limited delivery path. `delays_ms` delays data delivery in one direction while priority controls remain immediate. It simulates a delivery schedule, not a congestion controller or packet network.

`path = "qemu"` runs one traced IMQUIC scene stream alongside one unlimited
Cubic TCP stream and one unlimited Prague TCP stream. Exactly two switches,
`s1 → s2`, carry all three sender/receiver pairs. Each background has dedicated
endpoint namespaces. The inter-switch link is the shared `bandwidth_mbps`
HTB/DualPI2 bottleneck in both directions; all access links are 300 Mbps.
`delays_ms` is total one-way propagation for each pair, divided equally between
its two access egresses. Propagation and the dropping AQM use different devices.
There are no switch-count, optional-background, per-hop-background or alternative
delay-mode settings.

The QEMU table exposes the queue settings directly:

```toml
[[experiments]]
name = "scene-with-tcp-competition"
path = "qemu"
controllers = ["prague"] # Scene QUIC controller; Reno is also available.
bandwidth_mbps = 50
scenes = ["playroom"]
delays_ms = [10] # Total one-way for scene, Cubic TCP and Prague TCP.
dualpi2_target_ms = 15
dualpi2_update_ms = 16
dualpi2_step_ms = 1
dualpi2_coupling_factor = 1
background_warmup_seconds = 4
```

Both backgrounds use one TCP data connection with no iperf `-b` or `-P` limit.
Cubic disables ECN at both endpoints. Prague sets TCP Prague explicitly and
requests Accurate ECN with `net.ipv4.tcp_ecn=3`, as supported by the existing
5.15 Prague guest kernel. Startup checks actual algorithm and negotiated ECN in
`ss -tin` and payload in both receiver logs. Older iproute2 can omit the ECN
option: nonzero `delivered_ce` on the Prague data socket then provides direct
evidence of ECN feedback. Without either observation, startup fails. There is no algorithm fallback.
The guest needs TCP Prague, HTB, netem, DualPI2, Mininet/OVS, ethtool, iperf3 and ss.
The [L4S kernel project](https://github.com/L4STeam/linux) documents Prague/ECN
support; support in an arbitrary distribution kernel must be checked separately.

Both receivers start before either sender. Once both receive data, warmup runs
for `background_warmup_seconds` before scene services start. Both flows stay
active until guest cleanup. Process exit or lost data connection fails the run;
startup failures stop existing processes and preserve logs. `background-config.json`,
`background-processes.json`, interval logs, socket snapshots and qdisc evidence
record what was requested and observed. CPU checks establish the configuration
contract; live guest evidence establishes kernel and runtime behavior.

Ideal data-delay and QEMU packet-delay semantics differ because ideal controls
are immediate. Do not treat them as identical transport experiments.

`reruns` is the total number of repetitions, including the first. `on_failure` supports `continue`, `stop` and `retry`; `max_retries` counts additional attempts, and `retry_exhausted` controls what happens after retries. Every attempt, command, exit code, error and log is retained. The first attempt satisfying the configured criterion is selected; if all fail, the last is retained for diagnosis. Selection never uses quality scores.

The failure criteria are:

- `capture`: a recorded video marked PASS exists and the launcher did not raise an exception or time out.
- `quality`: capture plus the launcher's retained quality status must be PASS.
- `all`: capture and quality plus the launcher's overall PASS and zero process exit status.

Capture and quality failures remain visible as PARTIAL even when the chosen criterion permits continuing. Offline VMAF/plotting failures are reported separately after captures and do not rerun captures. CLI exit 0 means the configured capture criterion was met for every item and requested analysis did not block; it does not establish a transport-performance claim. Exit 1 reports execution/analysis failure, 2 a configuration/input error, and 130 an interruption. Interruptions preserve the ledger and partial artifacts; replot them later with `--plot-only`. There is no automatic resume of incomplete batches.

The default resource guard requires 4 GiB available RAM and 5 GiB free disk per attempt, with a 7168 MiB systemd user scope memory limit. Configure these in `[runner]`; set `memory_limit_mib = 0` to disable the scope. `timeout_seconds` is the inner browser/launcher deadline; `attempt_timeout_seconds` limits the whole capture and reference-render process. Per-attempt logs retain failures rather than prevalidating browser object payloads.

Each batch contains `config.json`, `ledger.json`, a README, scaled traces and their hashes under `inputs/`, and original evidence under `runs/<case>/attempt-NNN/`. Generated QEMU topology source is retained with each case. Automated `analysis/` output includes JSON/CSV summaries, PNG/PDF figures, per-run time series, and VMAF commands/provenance. Plots show sparse quality, browser object payload goodput, applied scene fraction where available. Payload goodput excludes network overhead.

VMAF uses exact timestamp/pose intersections within each scene/bandwidth/duration/path/switch/table group when available. If a failed member prevents a common grid, usable per-run exact pairs are plotted separately and cross-grid mean comparisons are suppressed. Missing samples are excluded without interpolation. Same-player full-scene references assess progressive completeness; sparse-frame VMAF is exploratory and its motion features depend on the retained sample grid. Repeat ranges are descriptive, not confidence intervals. Source capture and quality statuses remain separate from analysis status.

Plots compare Prague and Reno at one fixed QEMU delay per figure. Ideal figures compare the configured delays, ordered highest first. Both include signed ΔVMAF and ΔSSIM panels: Prague minus Reno for QEMU, and higher delay minus lower delay for ideal (320 minus160 for the example). Positive values favor the first named condition. With more than two ideal delays, every higher/lower pair is shown. Differences pair the same repeat, timestamp and camera pose with equal decoded RGB references; VMAF also requires the identical full temporal score grid. Missing members or incompatible grids are explicitly unavailable, without interpolation or substitution. The CSV next to each figure retains pair IDs, source statuses, exact differences and their means.

`[qemu].performance_mode = true` selects `tools/l4s/qemu_player_performance.py`
in place of the ordinary QEMU runner. It applies the host performance power
profile and restores the previous profile on exit. Seven guest vCPUs map onto
host CPUs 4–10, with QEMU I/O on CPU11 and two native build jobs. These CPUs must
be available. In the guest, OVS uses CPU2, the publisher/relay use CPUs0/1, the
bridge/proxy use CPU3, Cubic/Prague senders use CPUs4/5, and both receivers use
CPU6, with nice -10. Network softirq priority is raised. Prague relay/subscriber
use 250µs pacing unless explicitly overridden. Host affinity/power-profile and
guest CPU/AQM/congestion settings are retained with each attempt.

Physical interfaces disable GRO/GSO/TSO in **both** performance modes so batching
cannot bypass queue accounting. There are no IFBs in this topology. Setting
`performance_mode = false` uses `[qemu].runner` and omits host power/affinity and
guest CPU/priority/pacing tuning. A requested performance setting that cannot
be applied fails explicitly. These settings alone establish no throughput claim.

### Buffered ideal-link playback

Use `--transport ideal --receipt-thread worker --playback-delay-ms 4000` for
buffered trace playback. Main-thread, unbuffered receipt remains the default.
Buffered mode requires GSP2, first-request start (omit `--start-after-first-object`),
and a fixed horizon (enabled by ideal mode).

The receipt worker timestamps raw data and acknowledges bounded ownership.
Decoding starts immediately. The original trace clock drives priorities; the
four-second buffer affects wall-clock playback only. Both candidate and complete
reference sequences start at **camera trace time zero** and use the exact grid
`0, 100, 200, ... < trace_duration_ms`. The candidate renders a new frame for each
slot, with the camera interpolated at that exact trace time. Only objects whose
modeled arrival is at or before that slot enter its scene, even when a browser
callback executes late. Future buffered objects cannot leak into an earlier image.

Buffered capture and encoded video use **10 FPS**; omit `--fps` and `--quality-fps`
or specify 10 for both. Each frame waits for due received objects' preparation,
Spark upload/sort work, and rendering completion, then exports a PNG. Snapshot
export completes before the next logical frame advances, so slow encoding does
not silently drop slots. Slow wall-clock execution can postpone frames; every
logical slot is still rendered, rather than duplicated or relabeled. Missing or
failed exports fail capture instead of being filled.

Video is assembled from the complete PNG sequence at 10 FPS with time zero at
the first trace frame. This path uses no MediaRecorder wall-clock timestamps or
WebM time stretching. Candidate and reference comparisons use the PNGs and
strict matching timestamps/camera poses, independently of video encoding. The
unbuffered path continues using MediaRecorder on a manually requested canvas
stream. A 256 MiB conservative receipt reservation covers payload and prepared
arrays, including dependency-held layers; overflow/worker failure stops playback
and releases pending buffers and workers.

To regenerate an independent complete-scene reference directly from a trace:

```sh
results/venvs/3dgs/bin/python tools/l4s/render_trace_reference.py \
  --manifest results/scene-videos-20261018/train-scene/manifest-v2.json \
  --trace results/buffered-timing-pair-20261115/trace-6s.json \
  --output results/new-trace-reference --fps 10 --headless
```

The output contains `reference-frames/frames.json`, exact-pose PNGs,
`reference.mp4`, and hashes of the trace, source scene and player assets. Output
must be a new directory under `results/`. The reference has the complete scene at
every timestamp; the progressive candidate's time-zero scene can be empty.

Delivery validation checks publisher execution, modeled object metadata and
receipt identities/counts. Snapshot checks retain complete grids and exact
candidate/reference timestamps and camera poses. `object-applied` events identify
which objects enter each logical frame; `render-frame` events record its trace
timestamp and Gaussian count. There are no wall-clock timing grades, lateness
statistics or inferred first-visible-frame measurements.
