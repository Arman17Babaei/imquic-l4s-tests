# 3DGS deadline workload

This workload turns `3dgs_over_moq` scene data into a deterministic application
trace and sends one complete scene per independent IMQUIC connection. A run can
repeat the same scene across several deadlines and congestion controllers. Each
case ends at the subscriber's deadline, so stale reliable-stream data cannot
spill into the next repetition.

## What is reused from `3dgs_over_moq`

The harness deliberately does **not** run that repository's Rust `moq-lite`
transport. It reuses the preprocessed `.cache` format, Gaussian object encoder,
client cache, camera trace loader, and `gsplat` render path. Transport is the
IMQUIC/picoquic stack pinned by this testbed.

The scene bundle is ordered progressively: subgroup 0 for the entire scene,
then subgroup 1, then subgroup 2. Within each cluster, higher
`publish_sequence` groups are sent first. Objects keep the binary format from
`streaming.transport.protocol.encode_cluster`. The C fixture carries the three
3DGS LOD subgroups on three MOQT subgroup streams in one outer group, with
publisher priorities 0, 64, and 128 respectively (lower is higher priority in
MOQT). Object IDs on each outer subgroup are monotonic transport IDs; the
original track/group/subgroup/object identity remains inside every payload.

`3dgs_over_moq` is pinned to commit
`f4b0806d056cf84e258eaae0f82d26278a5c4040`. Set `THREEDGS_DIR` or pass
`--3dgs-dir` to use an existing checkout. Exploratory runs may use
`--allow-unpinned-3dgs`; provenance records the actual revision and dirty state.

## Deadline semantics

The subscriber negotiates MOQT draft-18 and sends `OBJECT_DELIVERY_TIMEOUT` equal to the case `--deadlines-ms` value.
MOQT defines that timeout per object, so the subscriber additionally has a
scene-level monotonic cutoff and closes the independent connection at the case
deadline. That application cutoff is the experiment's actual scene deadline;
the MOQT parameter is recorded as protocol-level expiry evidence rather than
being treated as the scene timer.

The publisher bounds IMQUIC's queued plus in-flight bytes to 4 MiB rather than
loading the full scene into the transport immediately. This prevents an
application queue several scenes deep from dominating the deadline result.

Each subscriber also writes `arrival-timeline.csv`, with exactly one row per
complete MoQ object accepted before the scene cutoff. `arrival_time_us` uses the
subscriber's monotonic clock with endpoint start as time zero and is captured
before local bundle disk I/O. Each row maps to the corresponding
`received.bundle` record through `bundle_record_index` and records payload bytes,
Gaussian count, cumulative bytes/Gaussians, subgroup ID, and embedded object ID.
No per-splat timestamps are stored.

The subscriber stops accepting objects at the scene deadline before finalizing
the bundle, so a late callback cannot contaminate the preserved deadline-cut
artifact. The runner validates that timeline indices and arrival times are
monotonic and that its object, byte, and Gaussian totals agree with the endpoint
and bundle results.

This mapping is sufficient to reconstruct the exact bundle prefix available at
any later presentation timestamp. It is intended to support offline rendering
of a time-aligned degraded frame sequence for PSNR/SSIM/VMAF without putting
CUDA or rendering work in the network experiment's timing path.

## Build

The 3DGS C fixture is built outside the IMQUIC submodule and links against the
already-built `libimquic`:

```sh
make build-3dgs-fixture
make 3dgs-deadline-check
```

The dependency-light check validates the bundle format and the routing contract
without CUDA, Mininet, or the private 3DGS checkout.

Install the Python/CUDA environment of `3dgs_over_moq` separately. The testbed
does not automatically install PyTorch, CUDA, or `gsplat`.

## Run

```sh
sudo python3 tools/l4s/run_3dgs_deadline.py \
  --output results/l4s/3dgs-deadline \
  --cache /path/to/bicycle.cache \
  --trace /path/to/3dgs_over_moq/assets/user102_bicycle_500.json \
  --3dgs-dir /path/to/3dgs_over_moq \
  --deadlines-ms 250,500,1000 \
  --modes reno,prague \
  --repetitions 3
```

For a capture-free shared-workload comparison using a bundle prepared on the
host, run the `l4s-3dgs-shared-check` target in the Prague-kernel guest. It runs
one 30-second Reno case and one 30-second Prague case against the same 150
Mbit/s BBRv2 TCP flow on a 300 Mbit/s DualPI2 bottleneck:

```sh
make l4s-3dgs-shared-check \
  L4S_RESULT_DIR=results/l4s/3dgs-shared \
  THREEDGS_BUNDLE=/path/to/scene.bundle \
  THREEDGS_BACKGROUND_MBPS=150
```

`THREEDGS_BACKGROUND_MBPS` defaults to 150. Use 0, 150, and 300 Mbit/s
for the capture-free 0%, 50%, and 100% offered-background-load points on the
target's fixed 300 Mbit/s bottleneck, with a distinct `L4S_RESULT_DIR` for each
run.

The shared-workload target uses an explicit L4S-favoring profile: a 15 ms
DualPI2 Classic target, 16 ms controller update interval, 1 ms L4S step
threshold, and 512 KiB HTB `burst`/`cburst`. At 300 Mbit/s, 512 KiB represents
about 14 ms of traffic, close to the time-equivalent of the earlier 32 KiB
burst at 20 Mbit/s. Every effective value is retained in per-case `tc` evidence
and run provenance.

This target deliberately does not start `tcpdump`, `dumpcap`, or `tshark` and
does not create packet captures. It retains endpoint metrics, iperf3 JSON,
before/after interface and DualPI2 counters, and per-object arrival timelines.
The run root contains `splat-arrivals.svg`, comparing cumulative received
splats and one-second splat arrival rates for Reno and Prague.

For a transport-only guest without GPU access, add `--no-render`. The received
bundles remain in each case and can be rendered later with the same source
cache and trace.

Each ordinary deadline case stores the received bundle, `arrival-timeline.csv`,
endpoint logs/results, 10 ms IMQUIC transport metrics, DualPI2 counters,
metadata, rendered output, and a small `result.json`. The capture-free shared
target additionally stores iperf3 JSON and interface counters. `result.json` reports
object/byte coverage, received Gaussian count, first/last object arrival times,
the arrival timeline hash/summary and, when rendering is enabled, PSNR against
a full-scene reference render of the same camera frame. The run root contains
the source bundle, hashes/revisions, reference render, `provenance.json`, and
aggregate `summary.json`.

## Scientific basis

The experiment follows the same reproducibility discipline as the rest of this
repository: fixed software revisions, preserved raw evidence, explicit
configuration, and independent repetitions. See Bajpai et al., “The Dagstuhl
Beginners Guide to Reproducibility for Experimental Networking Research,” ACM
SIGCOMM CCR 49(1), 2019, DOI 10.1145/3314212.3314217. For MoQ experiment
design and object-priority motivation, see Z. Gurel, T. E. Civelek, A. Bodur,
S. Bilgin, D. Yeniceri, and A. C. Begen, “Media over QUIC: Initial Testing,
Findings and Results,” ACM MMSys 2023, DOI 10.1145/3587819.3593937. Deadline
parameter semantics follow draft-ietf-moq-transport-18, Section 8.
For full-reference time-aligned video-quality analysis, see C. G. Bampis,
Z. Li, and A. C. Bovik, “SpatioTemporal Feature Integration and Model Fusion
for Full Reference Video Quality Assessment,” IEEE Transactions on Circuits
and Systems for Video Technology, 2019, DOI 10.1109/TCSVT.2018.2868262.
