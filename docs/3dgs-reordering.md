# Partial-L4S post-send reordering experiment

## Question

This experiment tests a narrower hypothesis than ordinary L4S latency reduction:

> Can a provider-side DualPI2 bottleneck act as a final correction point after speculative Enhancement data has already been sent, allowing later Base data to overtake it before a slower downstream Classic FIFO bottleneck?

The test deliberately uses a **partial L4S path**:

```text
server
  |
  v
s_l4s: HTB + DualPI2
  |
  v
s_classic: HTB + pfifo
  |
  v
client
```

An extra background sink branches from `s_classic`. A controlled Classic TCP flow from the server to that sink shares the provider DualPI2 output, but does **not** enter the client's downstream FIFO. This lets the experiment model aggregation pressure at a datacenter/WAN egress independently from the later client-side narrowing link.

## Traffic split

The source scene is ranked using the same object-importance code as `3dgs-priority-split.md`.

`--l4s-fractions` is a **payload-byte fraction**, not an object-count fraction. For each requested fraction, the highest-ranked whole-object prefix closest to that byte target is sent over:

- Prague + ECT(1) + UDP port 4444 -> L4S queue;
- Reno + Not-ECT + UDP port 4443 -> Classic queue.

Object boundaries are never changed. The actual byte fraction is recorded because an exact split is generally impossible without re-chunking.

## Frozen bicycle-trace demand

The network run does not depend on live camera feedback.

Before the first repetition, `run_3dgs_reordering.py` reads the bicycle trace and the cached track bounding boxes, computes the first frame at which each track enters the view frustum, and writes the resulting fixed order and timestamps to:

```text
inputs/frozen-demand-order.json
```

Every repetition then reuses this fixed workload. The high-priority bundle is reordered by that track order and its records are released at the frozen first-visible trace timestamps. The low-priority Enhancement bundle is released immediately at time zero, creating the intended situation in which Enhancement was a rational speculative send before later Base demand exists.

This is intentionally a controlled approximation of a dynamic viewport client. Once a satisfactory bicycle sequence has been produced on the test machine, the generated JSON can be retained as the canonical hard-coded experiment workload.

## Build and dependency-light checks

Build IMQUIC/picoquic as usual, then build the scheduled publisher wrapper:

```sh
make build
sh tools/l4s/build_3dgs_scheduled_fixture.sh
```

Run the pure Python checks:

```sh
python3 -m unittest discover -s tests -p 'test_3dgs_reordering.py' -v
```

The scheduled fixture extends the existing `3dgs-moq-test.c` fixture. Both QUIC connections are established before the workload begins. The low/Reno connection can therefore start sending Enhancement immediately, while Base objects become eligible later without adding a new QUIC handshake at each camera event.

## Example network run

```sh
sudo python3 tools/l4s/run_3dgs_reordering.py \
  --output results/l4s/3dgs-reordering \
  --source-bundle /path/to/scene.bundle \
  --cache /path/to/bicycle-cache.pt \
  --trace deps/3dgs_over_moq/assets/user102_bicycle_500.json \
  --3dgs-dir deps/3dgs_over_moq \
  --allow-unpinned-3dgs \
  --l4s-fractions 0.10,0.25,0.50 \
  --l4s-rate 300mbit \
  --classic-rate 50mbit \
  --dc-background-mbps 280 \
  --repetitions 3
```

Important knobs:

- `--l4s-fractions`: amount of scene payload placed on Prague/L4S;
- `--demand-time-scale`: scales the hard-coded bicycle timestamps without changing track order;
- `--dc-background-mbps`: aggregation pressure at the provider DualPI2 output; use `0` as a no-standing-provider-queue ablation;
- `--l4s-rate`: provider egress rate;
- `--classic-rate`: slower downstream Classic bottleneck rate;
- `--classic-buffer-packets`: downstream FIFO capacity;
- DualPI2 `target`, `tupdate`, and L4S `step` remain explicit knobs.

The default 280 Mbit/s aggregation flow on a 300 Mbit/s provider output is intentionally aggressive. It is a controlled stress point, not a claim about typical datacenter utilization. Sweep it rather than treating the default as representative.

## Proving that reordering actually occurred

The runner captures three locations:

1. provider/L4S-switch ingress;
2. provider/L4S-switch egress;
3. downstream Classic-switch egress.

Analyze them with:

```sh
python3 tools/l4s/analyze_3dgs_reordering.py results/l4s/3dgs-reordering
```

Packets are matched across capture points using the unchanged encrypted UDP payload. An order inversion is counted only when a Reno packet reaches provider ingress before a Prague packet, but that Prague packet leaves the provider before the Reno packet.

The main causal metrics are:

- unique earlier Reno payload bytes overtaken at DualQ;
- fraction of Prague packets that overtake at least one earlier Reno packet;
- inversion-pair count;
- fraction of concrete inversions still preserved at the downstream FIFO;
- provider versus downstream packet-gap distributions and their ratio.

This prevents an SSIM difference caused by ECMP, independent congestion windows, throughput allocation, or launch skew from being mislabeled as an in-network reordering result.

## Frame-by-frame SSIM and GIF

After the network run:

```sh
python3 tools/l4s/render_3dgs_reordering.py \
  results/l4s/3dgs-reordering \
  --source-bundle /path/to/scene.bundle \
  --cache /path/to/bicycle-cache.pt \
  --trace deps/3dgs_over_moq/assets/user102_bicycle_500.json \
  --3dgs-dir deps/3dgs_over_moq \
  --allow-unpinned-3dgs \
  --device cuda:0 \
  --ssim-device cuda:0 \
  --frame-step 1
```

For every sampled bicycle frame, the renderer reconstructs exactly the 3DGS objects that had arrived by that logical trace time, renders the accumulated scene, and compares it with a full-scene reference at the same camera pose. It writes:

- `frame-quality.csv` with SSIM, PSNR, received objects/bytes/Gaussians;
- rendered PNG frames;
- `trace.gif`;
- per-case summary JSON.

`--evaluation-lag-ms` can shift the evaluation deadline after each logical trace time to test application latency budgets such as 10, 20, or 50 ms.

## Essential controls

The proposed mixed Prague/Reno treatment should not be trusted from one configuration. At minimum, run these controls using the same frozen workload:

1. provider aggregation load disabled (`--dc-background-mbps 0`): little provider reordering should occur;
2. downstream rate equal to or above provider rate: downstream amplification should disappear;
3. multiple L4S byte fractions: tests whether extra Base coverage helps enough to offset Enhancement delay;
4. a fully L4S baseline from the existing 3DGS harness: tests the claim that very-low queueing for all data can leave too little post-send correction opportunity;
5. single-connection/application-priority baseline when available: distinguishes network correction from sender scheduling.

The causal result of interest is not simply lower Prague RTT. It is the chain:

```text
Classic residence at provider
  -> later Base overtakes already-sent Enhancement
  -> corrected order survives the downstream narrowing FIFO
  -> Base-relevant data is present earlier at its camera frame
  -> SSIM improves enough to exceed the delayed-Enhancement penalty
```

A negative result is useful: if SSIM monotonically worsens as more Enhancement is delayed, the mixed Prague/Reno architecture is not justified for this workload.

## Scientific basis

- K. De Schepper, O. Albisser, O. Tilmans, and B. Briscoe, “Dual Queue Coupled AQM: Deployable Very Low Queuing Delay for All,” 2022. The experiment relies on DualQ's separate Classic/L4S queues and short-timescale scheduling isolation; it does not assume that L4S itself is an application priority service.
- K. De Schepper et al., “PI²: A Linearized AQM for both Classic and Scalable TCP,” ACM CoNEXT 2016. This is the AQM/control basis underneath DualPI2.
- E. Artioli et al., “MoQSplat: Adaptive Progressive Streaming of 3D Gaussian Splatting over MoQ,” IEEE MMSP 2026. The native progressive subgroup ordering is reused rather than inventing a second visual-importance metric.
- B. Kerbl et al., “3D Gaussian Splatting for Real-Time Radiance Field Rendering,” ACM TOG 42(4), 2023, DOI 10.1145/3592433.
- Z. Wang, A. C. Bovik, H. R. Sheikh, and E. P. Simoncelli, “Image Quality Assessment: From Error Visibility to Structural Similarity,” IEEE TIP 13(4), 2004, DOI 10.1109/TIP.2003.819861. The quality analyzer uses the conventional 11x11 Gaussian-window SSIM formulation.
