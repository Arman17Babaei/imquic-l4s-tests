# Partial-L4S post-send correction experiment

## Research question

This experiment tests a narrow transport hypothesis rather than ordinary L4S
latency reduction:

> Can a provider-side DualQ bottleneck correct an application decision *after*
> speculative Enhancement has already been sent, by allowing later-generated
> Base traffic to overtake it before a slower downstream Classic bottleneck;
> and can that corrected order improve rendered SSIM enough to compensate for
> the extra Enhancement delay?

The primary topology is deliberately a **partial L4S deployment**:

```text
server
  |
  v
s1: HTB + DualPI2                 provider / aggregation bottleneck
  |
  v
s2: HTB + pfifo                   slower downstream Classic bottleneck
  |
  v
client
```

A background receiver branches from `s2`. A controlled Classic TCP flow from
the server to that receiver traverses the `s1` provider egress but does not
traverse `s2 -> client`. It can therefore build aggregation pressure at DualPI2
without itself occupying the later client FIFO.

The experiment is designed around the causal sequence

```text
old-track Enhancement admitted/sent
        ->
later bicycle-trace demand event occurs
        ->
new-track Base becomes eligible
        ->
Base/Prague reaches s1 after some Enhancement/Reno packets
        ->
DualQ may emit Base first
        ->
s2 FIFO may preserve and enlarge the time value of that corrected order
        ->
more useful splats may be available at render time
```

A run that observes no inversion is a valid negative result. The packet-order
analyzer separates **measurement validity** from **hypothesis outcome**.

## Semantic traffic model

The earlier implementation split the whole scene by a global ranked fraction.
That was not coherent with the hypothesis: it could put Base objects on Reno,
and its `1.0` endpoint collapsed the experiment to one Prague connection.
Those two effects confounded semantic priority with connection count and
independent congestion windows.

The reviewed experiment instead uses the progressive structure already encoded
by the 3DGS preprocessing:

- subgroup `0` = **Base**;
- subgroups `1` and `2` = **Enhancement**.

Base is invariant throughout the sweep:

```text
Base -> dedicated Prague / ECT(1) connection, UDP source port 4444
```

Only Enhancement treatment changes:

```text
Classic Enhancement -> Reno / Not-ECT, source port 4443
L4S Enhancement     -> Prague / ECT(1), source port 4445
```

The main knob is therefore

```text
--enhancement-l4s-fractions
```

and means **fraction of Enhancement payload bytes placed on L4S**, using whole
MoQ objects. It is not the fraction of the complete scene that is Base/L4S.
The manifest records both the requested and achieved Enhancement fraction and
the resulting total L4S byte fraction.

The two endpoint controls are especially important:

| Enhancement L4S fraction | Base connection | Enhancement connection | Interpretation |
| ---: | --- | --- | --- |
| `0` | Prague | Reno | proposed mixed L4S/Classic treatment |
| `1` | Prague | Prague | all-L4S media control |

Both endpoints therefore retain **two media connections**. The comparison does
not turn a two-cwnd experiment into a one-cwnd experiment.

Intermediate fractions use three media connections because Prague requires
ECT(1) treatment consistently on its L4S connection. They are useful for
estimating a quality/delay curve, but the `0` versus `1` endpoint comparison is
the cleanest causal test.

## Frozen bicycle demand

The network run never makes a live viewport decision.

A preparation step combines the bicycle camera trace with the scene track
bounding boxes and records the first trace frame in which each track becomes
visible. The result is frozen in:

```text
inputs/frozen-demand-order.json
```

Every repetition reuses the same track order and timestamps.

All objects belonging to a track become application-eligible at that track's
frozen first-visible time. Within each transport path, the publisher always
admits the best global importance rank among currently eligible objects.
Nothing belonging to a future track can be admitted early.

This produces the post-send condition naturally. While Enhancement belonging
to an already-visible track is still being transmitted, the camera reaches a
later trace event. The new track's subgroup-0 Base then becomes eligible on the
dedicated Prague connection. An earlier Reno packet at `s1` followed by a later
Base/Prague packet at `s1` is therefore genuine already-sent Enhancement versus
later-generated Base.

After a satisfactory demand file has been produced from the real bicycle cache,
retain that exact JSON as the canonical hard-coded workload for all paper runs.
Do not regenerate it between treatments.

## Synchronized workload start

The media connections are created before the workload begins. Each scheduled
publisher:

1. establishes its QUIC/MoQ session;
2. receives the subscription;
3. indexes its bundle and frozen release schedule;
4. writes a `publisher-ready` marker;
5. waits behind a common gate.

The experiment runner waits until every active media publisher is ready, writes
a future absolute epoch into `workload-go.txt`, and releases all publishers at
that epoch.

The actual start timestamps are retained and checked. Defaults reject a case if
publisher start skew exceeds 2 ms or if a publisher wakes more than 5 ms from
the requested epoch.

Offline SSIM uses the **common logical workload epoch**, not whichever process
happened to wake first.

## Sender-side queue control

The old fixture allowed up to 4 MiB of queued/in-flight data *per connection*.
That would give an intermediate three-connection case more sender buffering than
either endpoint.

The reviewed runner instead treats

```text
--application-queue-budget-bytes
```

as an aggregate experiment budget. A fixed share

```text
--base-queue-budget-fraction
```

is reserved for Base/Prague in every case. The remaining Enhancement budget is
divided only among the active Enhancement connections.

Consequently Base sender buffering is invariant across the sweep and total
sender buffering does not increase merely because a third connection exists.

## Fixed RTT and queueing

`--base-rtt-ms` controls a deterministic base RTT, default 20 ms. It is
implemented as pure `netem` fixed delay at sender egress and receiver ACK
egress, with no jitter and a deliberately large non-limiting `netem` queue.
The background TCP return path receives the same fixed delay.

The actual research queues remain:

- `s1 -> s2`: HTB + DualPI2;
- `s2 -> client`: HTB + finite `pfifo` in the primary treatment.

A separately known constant packet-processing component can be folded into the
fixed RTT because it is invariant across treatments. Variable queueing is not
folded into that term.

DualPI2 and the downstream qdisc are deleted and recreated before **every case**
so AQM controller state cannot leak between fractions or repetitions.

The default downstream FIFO is 128 packets, roughly 31 ms of 1500-byte packet
serialization at 50 Mbit/s. It is intentionally finite; the experiment should
not rely on a multi-hundred-millisecond artificial buffer.

## Packet-order evidence

By default the runner records compact kernel-timestamped packet identities at:

1. provider ingress;
2. provider egress after DualPI2;
3. downstream client egress.

Only the two causal flows are used for inversion analysis:

```text
4443 = Classic Enhancement / Reno
4444 = Base / Prague
```

Enhancement-Prague on 4445 is intentionally excluded from cross-class inversion
counts.

Before capture, GRO/GSO/TSO/LRO and relevant UDP segmentation/offload features
are disabled where supported on the media path. The final `ethtool -k` state is
stored in provenance. This is necessary because the matcher uses unchanged
encrypted UDP datagrams; host offload must not manufacture different packet
boundaries at different observation points.

An inversion is counted only when a specific Classic Enhancement/Reno datagram
reaches provider ingress before a Base/Prague datagram, but the Base/Prague
datagram leaves provider egress first.

The analyzer reports:

- Base and Classic-Enhancement provider sojourn distributions;
- inversion-pair count;
- unique earlier Classic Enhancement bytes overtaken by Base;
- fraction of Base/Prague packets that overtake at least one earlier Classic
  packet;
- persistence of concrete inversion witnesses at the downstream egress;
- provider versus downstream witness gaps and their ratio.

Capture loss, poor packet matching, or a missing expected flow makes a
measurement invalid. **Zero inversions do not.** This is essential for the
no-background and fully-L4S negative controls.

## SSIM evaluation

`render_3dgs_reordering.py` replays the bicycle trace offline. At every sampled
camera frame it inserts only objects whose measured arrival timestamps are at
or before the logical frame deadline, renders the accumulated splats, and
compares the result with a full-scene reference at the same camera pose.

Outputs include:

- `frame-quality.csv`;
- per-frame SSIM and PSNR;
- rendered PNGs;
- `trace.gif`;
- a per-case quality summary.

The renderer uses a fixed Gaussian budget rather than an adaptive live GPU
budget. `--evaluation-lag-ms` can evaluate explicit application deadlines such
as 10, 20, or 50 ms after each trace timestamp.

The causal chain to demonstrate is

```text
Classic Enhancement residence at s1
  -> Base/Prague overtakes already-sent Enhancement
  -> corrected order survives s2 FIFO
  -> Base-relevant objects arrive earlier at their camera frames
  -> SSIM gain exceeds the Enhancement-delay penalty
```

If SSIM does not improve, or if improvement occurs without measured Base versus
Classic-Enhancement inversions, the proposed mechanism is not supported by that
case.

## Primary experiment matrix

Do not start with a large parameter sweep. Establish causality with these cases
first, all using the same frozen bicycle workload.

1. **Mixed endpoint**: Enhancement fraction `0`, downstream Classic FIFO.
2. **All-L4S endpoint**: Enhancement fraction `1`, same topology and rates.
3. **No provider pressure**: repeat the mixed endpoint with
   `--dc-background-mbps 0`; provider-created overtaking should fall sharply.
4. **No downstream narrowing**: set downstream capacity equal to or above the
   provider capacity; downstream gap amplification should disappear.
5. **Full-L4S path control**: replace the downstream FIFO with DualPI2 using
   `--downstream-mode dualpi2`.

Only after those cases behave coherently should intermediate Enhancement L4S
fractions be used to look for an SSIM optimum.

Recommended first sweep:

```text
Enhancement L4S fraction: 0, 0.25, 0.5, 0.75, 1
provider rate:            300 Mbit/s
later bottleneck:          50 Mbit/s
base RTT:                  20 ms
DualPI2 Classic target:    15 ms
DualPI2 L4S step:           1 ms
```

The 280 Mbit/s provider aggregation background is a stress point, not a claim
about normal datacenter utilization. Sweep it after establishing the mechanism.

## Preparation and run

Freeze bicycle demand once on a host with the scene cache:

```sh
results/venvs/3dgs/bin/python tools/l4s/prepare_3dgs_reordering.py \
  --cache results/l4s/3dgs-preparation/point_cloud.cache \
  --trace deps/3dgs_over_moq/assets/user102_bicycle_500.json \
  --output results/l4s/3dgs-reordering-demand/frozen-demand-order.json \
  --allow-unpinned-3dgs
```

Run dependency-light semantics tests:

```sh
python3 -m unittest discover -s tests -p 'test_3dgs_reordering*.py' -v
```

Build the synchronized scheduled fixture:

```sh
sh tools/l4s/build_3dgs_scheduled_fixture.sh
build/imquic-3dgs-moq-scheduled schedule-self-test
```

Run in the validated QEMU guest:

```sh
python3 tools/l4s/run_qemu_3dgs_reordering.py \
  --source-bundle results/l4s/3dgs-preparation/scene.bundle \
  --frozen-demand results/l4s/3dgs-reordering-demand/frozen-demand-order.json \
  --enhancement-l4s-fractions 0,0.25,0.5,0.75,1 \
  --repetitions 3
```

Direct Mininet invocation on a prepared guest is:

```sh
sudo python3 tools/l4s/run_3dgs_reordering.py \
  --output results/l4s/3dgs-reordering \
  --source-bundle /path/to/scene.bundle \
  --frozen-demand /path/to/frozen-demand-order.json \
  --enhancement-l4s-fractions 0,0.25,0.5,0.75,1 \
  --l4s-rate 300mbit \
  --classic-rate 50mbit \
  --base-rtt-ms 20 \
  --dc-background-mbps 280 \
  --repetitions 3
```

Analyze packet order:

```sh
python3 tools/l4s/analyze_3dgs_reordering.py \
  results/l4s/3dgs-reordering
```

Render quality after copying results to the GPU host:

```sh
python3 tools/l4s/render_3dgs_reordering.py \
  results/l4s/3dgs-reordering \
  --source-bundle /path/to/scene.bundle \
  --cache /path/to/bicycle-cache.pt \
  --trace deps/3dgs_over_moq/assets/user102_bicycle_500.json \
  --3dgs-dir deps/3dgs_over_moq \
  --allow-unpinned-3dgs \
  --device cuda:0 \
  --ssim-device cuda:0
```

## Scientific basis

- K. De Schepper, O. Albisser, O. Tilmans, and B. Briscoe,
  “Dual Queue Coupled AQM: Deployable Very Low Queuing Delay for All,” 2022.
  The experiment relies on DualQ's separate Classic/L4S queues and
  short-timescale scheduling isolation; it does not treat L4S as an application
  priority service.
- K. De Schepper et al., “PI²: A Linearized AQM for both Classic and Scalable
  TCP,” ACM CoNEXT 2016. This is the control-law basis underlying DualPI2.
- E. Artioli et al., “MoQSplat: Adaptive Progressive Streaming of 3D Gaussian
  Splatting over MoQ,” IEEE MMSP 2026. The experiment reuses the scene's existing
  progressive subgroup semantics instead of inventing a second Base metric.
- B. Kerbl, G. Kopanas, T. Leimkühler, and G. Drettakis, “3D Gaussian Splatting
  for Real-Time Radiance Field Rendering,” ACM TOG 42(4), 2023,
  DOI 10.1145/3592433.
- Z. Wang, A. C. Bovik, H. R. Sheikh, and E. P. Simoncelli, “Image Quality
  Assessment: From Error Visibility to Structural Similarity,” IEEE TIP 13(4),
  2004, DOI 10.1109/TIP.2003.819861. The offline quality evaluation uses SSIM.
