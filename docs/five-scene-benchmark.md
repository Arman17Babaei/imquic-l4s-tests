# Five-scene render/VMAF batch

Goal: five distinct full scenes, each sampled over 20 seconds at 320×180,
1 FPS, including loading, rendering, encoding and encoding-fidelity VMAF in
90 seconds total. This is not 10/30 FPS video or separately rendered progressive
candidate/reference pairs.

The fixed set is train, room, truck, playroom and drjohnson (11,112,379 Gaussians
total). Each scene keeps its own original trace, full SH3 payload and provenance.

```sh
python3 tools/l4s/benchmark_scene_batch.py \
  --dist /opt/live4s/experiments/comprehensive-20261120/source/deps/gaussian-player/dist \
  --ffmpeg .toolchains/ffmpeg-7.0.2-amd64-static/ffmpeg \
  --output results/render-benchmark/five-scenes-NEW \
  --parallel-scenes 5 --render-workers 1 --timeout-seconds 300
```

`--parallel-scenes 1` runs sequentially; larger values allocate disjoint CPU
lanes to scenes. `--render-workers` subdivides each lane into independent frame
shards. No cached rendered frames are reused. The output directory must be new.
`--lane-cpus '0,1;2,3;4,5;6,7;8,9'` explicitly selects disjoint lanes;
`--scene-workers 1,1,2,2,2` permits unequal worker budgets. Lane lists must be
subsets of the invoking process's affinity, and workers must fit each lane.

## Initial baseline: target not met

On the 24-vCPU Xeon E5-4669 v4 VM (62 GiB RAM), five single-renderer scenes in
parallel took **253.93 seconds**, and only train, room and truck completed.
Playroom and drjohnson exceeded browser deadlines. Peak summed process RSS was
**10.14 GiB**; shared mappings are double-counted, so this is not physical RAM
consumption. The resource sampler observed at least 1,372.79 CPU-seconds in the
live tree, but exited children are absent from that estimate. Later batch runner
versions additionally collect completed-child CPU time through `getrusage`.

| Scene | Completed | Elapsed seconds |
| --- | --- | ---: |
| train | yes | 136.73 |
| room | yes | 208.03 |
| truck | yes | 235.61 |
| playroom | no | 253.65 |
| drjohnson | no | 246.65 |

Evidence: `results/render-benchmark/five-scenes-baseline/batch.json` and scene
subdirectories. These are incomplete batch results, not a 90-second success.
No hardware GPU is available to this workflow: VMware SVGA is exposed, and the
working renderer uses CPU Mesa lavapipe. More RAM alone will not resolve the
current bottleneck. No measured hardware sizing recommendation yet guarantees
five scenes in 90 seconds.

## Completed four-shard batch: target still not met

Five scenes × four render workers (20 independent browsers) completed all 100
samples, encoding and VMAF in **225.42 s**. Peak summed process RSS was
**39.35 GiB**. Evidence: `results/render-benchmark/five-scenes-four-shards/`.
The 90-second goal is not met. Loading the same full scenes four times increases
memory and initialization cost substantially. CPU time from `getrusage` does not
fully account for reparented Chromium processes; do not use its reported average
busy-core value as a reliable machine-utilization estimate.
Train, room and truck's 60 PNGs were also checked byte-for-byte against the
completed single-renderer captures, including poses and timestamps: all match.
The other two scenes lack completed single-renderer baselines.
Drjohnson has 16 distinct PNGs across 20 changing poses; five samples are black.
The first 18 PNGs, including all five black samples, are byte-identical to the
retained partial single-renderer run. That rules out sharding as their cause,
but does not establish whether the recorded camera is correctly aligned with
the scene. Treat visual correctness as unverified despite completed
Gaussian-count, timing-grid and VMAF checks.

The Mesa OpenGL/EGL backend failed to create a WebGL context in this environment.
Manual-update, synchronous-readback and sort-drain variants did not produce a
completed valid render; interrupted diagnostics are not performance evidence.

## Two-shard batch with two Mesa threads: best completed time

`LP_NUM_THREADS=2` with five concurrent scenes and two workers per scene completed
in **217.51 s**, using **18.94 GiB peak summed process RSS**. All 100 PNGs are
byte-identical to the four-shard batch, with matching timestamps, camera poses
and applied Gaussian counts. Evidence:
`results/render-benchmark/five-scenes-two-shards-mesa2/`.

This reduces memory by 51.9% and wall time by 3.5% relative to the four-shard
batch, but still requires a **2.42× wall-time speedup** to reach 90 seconds.
That ratio is not a prediction of CPU scaling or a guaranteed hardware request.

## Readback scheduling diagnostics

Train alone, 12 pinned CPUs per trial and `LP_NUM_THREADS=4`, completed in
135.05 s with Spark's read pause disabled and in **119.74 s** when also disabling
automatic updates after the first explicit seek. The combined variant's 20 PNGs,
poses, timestamps and applied counts are identical to the unmodified standalone
baseline. Trials ran concurrently on disjoint CPU sets, so these are diagnostic
timings, not a standalone scaling comparison or five-scene success.

The combined build is reproducible using:

```sh
python3 tools/l4s/prepare_benchmark_heartbeat.py \
  --dist /opt/live4s/experiments/comprehensive-20261120/source/deps/gaussian-player/dist \
  --output results/render-benchmark/manual-no-pause-NEW \
  --manual-updates --no-read-pause --profile
```

Every pose still performs an explicit camera-dependent Spark update. No previous
pose's sort is intentionally reused. A five-scene validation is required before
using this variant as a batch optimization.
