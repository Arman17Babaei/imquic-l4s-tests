# Render and VMAF wall-time benchmark

`tools/l4s/benchmark_render_vmaf.py` measures a **new complete-scene render**, PNG-to-MP4 encoding, and CPU VMAF. It does not reuse a reference cache. Every frame on the half-open `[0, 20s)` grid is evaluated; the default is 200 frames at 10 FPS, not a 30 FPS result.

```sh
python3 tools/l4s/benchmark_render_vmaf.py \
  --manifest /opt/live4s/data/scenes/train/manifest-v2.json \
  --trace /opt/live4s/data/traces/nthu-selected-150mbps/user8_train.json \
  --dist /opt/live4s/experiments/comprehensive-20261120/source/deps/gaussian-player/dist \
  --output results/render-benchmark/new-run \
  --ffmpeg .toolchains/ffmpeg-7.0.2-amd64-static/ffmpeg \
  --duration 20 --fps 10 --width 640 --height 360 --threads 8
```

The explicit player path above is an environment-specific existing build. Ordinarily build `deps/gaussian-player` and point `--dist` at its `dist/`. The selected FFmpeg must provide `libvmaf`; system FFmpeg on the benchmark host does not. No GPU is required: rendering explicitly uses Chromium SwiftShader.

`benchmark.json` records render, encode, VMAF and total wall seconds, dimensions, cadence, frame count, FFmpeg identity and command. Rendering also records input, scene and player hashes. `target_met` requires a completed run under 120 seconds. Installation/build time is excluded, but scene loading and render initialization are included.

With no candidate supplied, VMAF compares the encoded render to its clean PNG sequence: this measures **encoding fidelity**, not network/progressive quality. `--candidate path/to/video.mp4` instead evaluates an external candidate, whose generation time is explicitly excluded. Neither mode measures QEMU transport setup or makes claims about other scenes, resolutions or FPS. A future paired progressive benchmark must include the candidate rendering cost too.

Generated videos, PNGs, logs and summaries stay under ignored `results/`.

For concurrent diagnostic trials on the 24-core host, prefix their commands with
`taskset -c 0-11` and `taskset -c 12-23`. Affinity is inherited by browser,
SwiftShader workers and FFmpeg; `benchmark.json` records the allowed CPUs.
These sets follow the VM's two NUMA nodes. Memory bandwidth and storage are
still shared, so confirm final timing with a standalone run. Do not compare
a 12-core diagnostic timing against an unrestricted 24-core timing as if the
resource budgets were identical.

`--render-workers 2` instead splits one benchmark's exact frame grid into
contiguous shards, runs independent complete-scene browser captures on disjoint
CPU sets, validates and merges their original timestamps, then encodes and
evaluates VMAF once. Scene initialization is paid by every shard and included in
wall time. Shard directories retain scene/player/harness provenance and commands.
This parallelizes offline complete-scene rendering only, not live network runs.

## Initial host observations

The host exposes 24 Intel Xeon E5-4669 v4 vCPUs and about 62 GiB RAM.
Its VMware SVGA device did not provide a usable accelerated renderer through
the existing Chromium hardware flags: an explicit WebGL renderer probe still
reported SwiftShader. The available train scene contains 1,026,508 Gaussians.

An isolated synthetic 640×360, 10 FPS, 20-second VMAF self-comparison evaluated
all 200 frames in 0.93 seconds with eight threads. **This is not a completed
Gaussian render timing.** Full-scene trials stalled on the first seek; a
bounded 640×360 trial timed out after 241.76 seconds including the browser
shutdown allowance. Interrupted runs with `complete: false` and
`target_met: false` are failures, even when interrupted before 120 seconds.
An under-two-minute result has been established only for the explicitly reduced
1 FPS workload below, not for 10/30 FPS video or paired progressive rendering.

### Target reached: two pinned render shards

The 20-second full train trace at **320×180, 1 FPS (20 frames)** completed in
**89.52 seconds** with two render workers on CPUs 0–11 and 12–23:

| Stage | Wall seconds |
| --- | ---: |
| Two complete-scene render shards, including loading | 88.17 |
| Merge, encode and validation | 1.06 |
| Full sampled-frame VMAF | 0.17 |
| Total including orchestration | **89.52** |

This is 29.4% faster than the 126.76-second standalone baseline. **All 20 PNGs
are byte-identical to the standalone result**, with identical camera poses,
timestamps and all 1,026,508 Gaussians applied. Mean encoding-fidelity VMAF is
99.846 in both runs. Evidence and per-shard provenance are under
`results/render-benchmark/lavapipe-sharded-1fps/`; `validation.json` records the
cross-check. No reference cache or stale-sort shortcut was used.

Reproduce using the initial command with `--gpu-mode llvmpipe --fps 1
--width 320 --height 180 --render-workers 2` and a new output directory.
This is a low-cadence quality/render benchmark, **not fluid video**. VMAF compares
the encoded complete-scene render to its clean PNGs, not a separately rendered
progressive candidate. Higher-cadence and paired workloads remain unproven.

### First completed 20-second run

`results/render-benchmark/lavapipe-2fps-320/benchmark.json` completed using the
unmodified external player build, full train scene, Mesa lavapipe, CPUs 0–11,
320×180 pixels and **2 FPS (40 frames)**:

| Stage | Wall seconds |
| --- | ---: |
| Browser loading and rendering | 204.36 |
| MP4 encoding and validation | 1.62 |
| VMAF, every sampled frame | 0.26 |
| Total including orchestration | **208.00** |

Mean encoding-fidelity VMAF was 99.924. This run used an encoder process
started before the explicit BT.709 fix was loaded, so that score should not be
used as color-corrected evidence. Timing establishes that rendering dominates;
it does not meet the 120-second target and is not a 10/30 FPS result.
The retained manifest has 40 distinct camera poses and 40 distinct PNGs,
timestamps 0–19,500 ms, and all 1,026,508 Gaussians applied at every sample.

A standalone run with the original player, all 24 CPUs and **1 FPS (20
frames)** at 320×180 completed in **126.76 s**: browser render 123.80 s,
encoding 0.93 s and VMAF 0.18 s. It used the explicit BT.709 conversion and
returned mean encoding-fidelity VMAF 99.846. Evidence is in
`results/render-benchmark/lavapipe-1fps-fullcpu/benchmark.json`. This is a
near-miss, not an under-two-minute result or a fluid video workload.

The isolated render-heartbeat variant can be reproduced without editing the
external build:

```sh
python3 tools/l4s/prepare_benchmark_heartbeat.py \
  --dist /opt/live4s/experiments/comprehensive-20261120/source/deps/gaussian-player/dist \
  --output results/render-benchmark/heartbeat-player
```

This guarded adapter supports only the known bundle implementation and records
before/after hashes. It did **not** fix the stall in its initial lavapipe trial
and is not the recommended renderer or evidence of a speedup.

Instrumented copies of the player further localized the first-seek stall:
the accumulator generation returns, then Spark's depth readback for sorting
does not return. This is stronger evidence than the earlier live-debounce
hypothesis; readiness changes alone have not fixed the actual runtime blocker.

`--gpu-mode llvmpipe` selects ANGLE Vulkan with Mesa's lavapipe ICD (override
the ICD path using `L4S_LAVAPIPE_ICD`). This is still CPU rendering, not hardware
acceleration. The first lavapipe trial eventually returned from its first seek,
but did not establish a completed sequence or an under-two-minute result.

Focused Python benchmark/reference/cache/video tests and Node capture unit
tests pass. Full browser playback tests require the missing local player
submodule build and failed in this checkout; they are not a passing acceptance
gate. The environment-specific external player build is recorded in each run.
