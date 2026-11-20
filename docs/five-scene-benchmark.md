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
