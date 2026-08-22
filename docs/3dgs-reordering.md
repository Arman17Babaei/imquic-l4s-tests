# Two-connection L4S spectrum experiment for 3DGS

## Research question

This experiment tests whether giving progressively more of a 3DGS workload L4S
service always improves rendered quality, or whether retaining some lower-ranked
traffic in the Classic queue can create useful post-send reordering opportunities
for later, higher-ranked data.

The hypothesis is intentionally application-level:

```text
more L4S
  -> less queueing for more objects
  -> but less Classic residence / less opportunity for later objects to overtake
  -> final frame availability changes
  -> SSIM can improve or degrade
```

The experiment therefore sweeps a continuous transport split instead of defining
one subgroup as permanently "Base".

## Exactly two media connections

Every case establishes the same two QUIC/MoQ connections before the workload
starts:

```text
UDP 4444: Prague + ECT(1)
UDP 4443: Reno + Not-ECT
```

This is true for every requested L4S fraction, including the endpoints.

At `--l4s-fractions 0`, the Prague connection is established but carries an
empty scene bundle.

At `--l4s-fractions 1`, the Reno connection is established but carries an empty
scene bundle.

Intermediate fractions still use exactly those same two connections. There is
never a third media connection.

## Ranking and the L4S spectrum

Every whole 3DGS object is ranked by:

```text
(progressive layer/subgroup ascending,
 mean encoded opacity descending,
 stable source order)
```

The subgroup/layer dominates opacity. Mean opacity is only a within-layer
ordering signal.

The requested fraction `alpha` is a payload-byte target. The highest-ranked
whole-object prefix whose byte volume is closest to that target is assigned to
Prague; all remaining objects are assigned to Reno.

Conceptually:

```text
alpha = 0.00  -> all scene objects Reno
alpha = 0.25  -> top ~25% of bytes by (layer, opacity) Prague
alpha = 0.50  -> top ~50% Prague
alpha = 0.75  -> top ~75% Prague
alpha = 1.00  -> all scene objects Prague
```

Because whole-object boundaries are preserved, the actual byte fraction is
recorded and can differ slightly from the requested fraction.

No rule forces all subgroup-0 objects onto Prague. A cutoff is allowed to fall
inside any layer. This is deliberate: the experiment must reveal a spectrum,
not encode the conclusion in the transport assignment.

## Frozen bicycle demand

The bicycle camera trace is used before the network experiment to derive the
first frame in which each track enters the viewport. That order and those
timestamps are frozen to:

```text
inputs/frozen-demand-order.json
```

Every repetition then uses the same fixed workload.

The trace controls **eligibility**: an object cannot be admitted before its
track's frozen first-visible time.

The `(layer, mean opacity)` rank controls **which currently eligible object is
chosen next**.

Thus a lower-ranked object from an already-visible track may be transmitted
before a future higher-ranked object exists. When the camera advances and that
new object becomes eligible, it can jump ahead of lower-ranked objects that are
still in the application queue.

This creates the condition needed to separate sender-side rescheduling from
post-send network correction:

```text
low-ranked object already left application
        |
        | later camera event
        v
higher-ranked object becomes eligible
        |
        +-- can preempt unsent application objects
        |
        +-- cannot retroactively reorder bytes already handed to QUIC/network
```

## Sender admission gate

The publisher does not preload the whole workload into IMQUIC.

One publisher owns both connections and applies a high-biased two-queue select:

1. try the Prague queue without blocking;
2. if Prague is empty, wait on both Prague and Classic eligibility;
3. recheck Prague after wake-up, so Prague wins a simultaneous release;
4. consult Classic only while Prague has no currently eligible object;
5. admit the selected object only if both transport conditions hold:

```text
bytes_in_flight < cwnd_bytes
```

and

```text
queued_stream_bytes < min(object_payload_bytes, transport_queue_slack_bytes)
```

The default is:

```text
--transport-queue-slack-bytes 4096
```

This intentionally keeps only a few kilobytes of unsent QUIC data beyond the
experiment scheduler's control.

An eligible Prague object whose transport gate is closed blocks Classic. If a
Classic object is waiting for transport room, a newly eligible Prague object
preempts it before either object is admitted. A future Prague release does not
block a Classic object that is available now.

The gate is object-granular, not intra-object preemption. Once an MoQ object is
handed to IMQUIC, its remaining bytes are committed to that transport. New
higher-ranked objects can preempt objects that remain in the application queue,
not bytes already inside an admitted object.

Each path writes:

```text
admission-order.csv
admission-order.csv.gate.csv
transport-metrics.csv
```

The gate sidecar records, for every admission:

```text
queued_stream_bytes_before
bytes_in_flight_before
cwnd_bytes_before
queue_threshold_bytes
```

so the sender behavior can be audited after the run.

The case-level `combined-admission-order.csv` adds a global admission index and
transport path. Its validator rejects any Classic admission made while an
eligible, unadmitted Prague object existed. `scheduler-result.json` records
Prague-blocked time and Classic preemptions.

## Prague warm-up

The default `--prague-warmup-ms 10000` sends and discards dummy objects only on
the Prague connection, including in the all-Classic endpoint control. The
Classic media connection is established but remains idle. Configured background
traffic starts before this warm-up and remains active through warm-up, drain,
and scene measurement. Capture and the scene workload begin only after the
Prague warm-up payload has drained. Use `--prague-warmup-ms 0` to disable it.

The background option `--dc-background-mbps unlimited` omits iperf3's `-b`
argument, allowing the TCP Reno flow to run at the available rate. The runner
records that mode and requires the flow to remain active through the warm-up
and measured workload.

## Network topology

The default path is:

```text
server
  |
  | fixed propagation delay
  v
s1: provider bottleneck
    HTB + DualPI2
  |
  v
s2: downstream bottleneck
    HTB + pfifo
  |
  v
client
```

A background sink also branches from `s2`. The optional background flow:

```text
server -> s1 -> s2 -> background_sink
```

shares the provider `s1` egress but does not traverse the client-facing `s2`
qdisc. It therefore creates provider aggregation pressure independently of the
later client bottleneck.

## Where base RTT is applied

`--base-rtt-ms` defaults to 20 ms.

It is not implemented by either bottleneck queue. The runner applies pure
`netem` delay as:

```text
server egress -> provider: base_rtt_ms / 2
client egress -> downstream: base_rtt_ms / 2
```

For the default:

```text
forward fixed delay = 10 ms
reverse ACK fixed delay = 10 ms
configured base RTT ~= 20 ms
```

The provider DualPI2 and downstream FIFO then add queueing delay on top.

The netem qdiscs have no configured rate limit and a deliberately large packet
limit; they are intended to represent propagation only.

## Packet-order evidence

Packet-order logs are taken at:

1. provider ingress;
2. provider egress;
3. downstream client-facing egress.

Only media UDP ports 4443 and 4444 are analyzed.

Relevant GRO/GSO/TSO/UDP segmentation offloads are disabled where supported so
that an unchanged encrypted UDP datagram can be fingerprinted consistently
across observation points.

The analyzer distinguishes an **opportunity**:

```text
Reno enters provider
  < Prague enters provider
  < same Reno leaves provider
```

from an actual **overtake**:

```text
Reno enters before Prague
but Prague leaves before Reno
```

A valid run with zero overtakes remains a valid negative result.

## Quality evaluation

The offline renderer replays the bicycle trace and, at every frame, reconstructs
only objects that had arrived by that logical time. It renders the partial scene
and compares it with the full-scene reference at the same camera pose.

The primary quality metric is SSIM; PSNR is also recorded.

The main spectrum is therefore:

```text
requested L4S byte fraction
  -> actual L4S byte fraction
  -> Classic/L4S residence and overtaking
  -> object arrival times
  -> frame SSIM
```

## Recommended first run

Prepare and freeze demand once:

```sh
results/venvs/3dgs/bin/python tools/l4s/prepare_3dgs_reordering.py \
  --cache results/l4s/3dgs-preparation/point_cloud.cache \
  --trace deps/3dgs_over_moq/assets/user102_bicycle_500.json \
  --output results/l4s/3dgs-reordering-demand/frozen-demand-order.json \
  --allow-unpinned-3dgs
```

Then run the spectrum in QEMU:

```sh
python3 tools/l4s/run_qemu_3dgs_reordering.py \
  --source-bundle results/l4s/3dgs-preparation/scene.bundle \
  --frozen-demand results/l4s/3dgs-reordering-demand/frozen-demand-order.json \
  --l4s-fractions 0,0.25,0.5,0.75,1 \
  --repetitions 3 \
  -- \
  --transport-queue-slack-bytes 4096
```

The direct Mininet form is:

```sh
sudo python3 tools/l4s/run_3dgs_reordering.py \
  --output results/l4s/3dgs-reordering \
  --source-bundle /path/to/scene.bundle \
  --frozen-demand /path/to/frozen-demand-order.json \
  --l4s-fractions 0,0.25,0.5,0.75,1 \
  --transport-queue-slack-bytes 4096 \
  --base-rtt-ms 20 \
  --l4s-rate 300mbit \
  --classic-rate 50mbit \
  --dc-background-mbps 280 \
  --repetitions 3
```

For a matched five-fraction downstream comparison, run each topology with one
repetition and isolated fraction cells. `--retention evidence` keeps packet
logs, timelines, admissions, transport metrics, and provenance while removing
regenerated split and received bundles after validation:

```sh
python3 tools/l4s/run_qemu_3dgs_reordering.py \
  --source-bundle results/l4s/3dgs-preparation/scene.bundle \
  --frozen-demand results/l4s/3dgs-reordering-demand/frozen-demand-order.json \
  --l4s-fractions 0,0.25,0.5,0.75,1 \
  --repetitions 1 --isolate-fractions \
  --destination-prefix qemu-3dgs-prague-first-l4s-l4s \
  -- \
  --downstream-mode dualpi2 \
  --dc-background-mbps unlimited \
  --prague-warmup-ms 10000 \
  --capture-mode packet-log --retention evidence
```

Repeat with `--downstream-mode classic` and a different destination prefix.
Pass the two run roots to `plot_3dgs_reordering_matrix.py`; its RTT and
goodput series align to the workload gate, so warm-up is recorded but not
counted as measured scene throughput.

Analyze packet order with:

```sh
python3 tools/l4s/analyze_3dgs_reordering.py \
  results/l4s/3dgs-reordering
```

Render quality with:

```sh
python3 tools/l4s/render_3dgs_reordering.py \
  results/l4s/3dgs-reordering \
  --source-bundle /path/to/scene.bundle \
  --cache /path/to/point_cloud.cache \
  --trace deps/3dgs_over_moq/assets/user102_bicycle_500.json \
  --3dgs-dir deps/3dgs_over_moq \
  --allow-unpinned-3dgs \
  --device cuda:0
```

SSIM uses the rendering device by default, so the command above computes SSIM
on `cuda:0` as well. Use `--ssim-device cpu` only when a separate CPU SSIM pass
is needed, for example when GPU memory is constrained.

## Important controls

At minimum compare:

1. the full `0 -> 1` L4S fraction spectrum;
2. `--dc-background-mbps 0`;
3. downstream rate equal to or above provider rate;
4. `--downstream-mode dualpi2`;
5. several sender slack values if the result appears sensitive to sender buffering.

For the slack ablation, useful values are:

```text
512, 1500, 4096, 16384 bytes
```

## Scientific basis

- K. De Schepper, O. Albisser, O. Tilmans, and B. Briscoe,
  "Dual Queue Coupled AQM: Deployable Very Low Queuing Delay for All", 2022.
- K. De Schepper et al., "PI²: A Linearized AQM for both Classic and Scalable
  TCP", ACM CoNEXT 2016.
- J. Iyengar and I. Swett, RFC 9002, "QUIC Loss Detection and Congestion
  Control", 2021.
- A. Langley et al., "The QUIC Transport Protocol: Design and Internet-Scale
  Deployment", ACM SIGCOMM 2017.
- E. Artioli et al., "MoQSplat: Adaptive Progressive Streaming of 3D Gaussian
  Splatting over MoQ", IEEE MMSP 2026.
- Z. Wang, A. C. Bovik, H. R. Sheikh, and E. P. Simoncelli,
  "Image Quality Assessment: From Error Visibility to Structural Similarity",
  IEEE TIP 13(4), 2004, DOI 10.1109/TIP.2003.819861.
