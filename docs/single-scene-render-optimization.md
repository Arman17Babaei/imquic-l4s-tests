# Single-scene rendering optimization

Keep the full train scene, original camera trace, 1280×720, 10 FPS and 20 seconds
(200 output frames). Do not reduce scene size, resolution or cadence. Encoding
and VMAF are not the optimization target and remain unchanged.

Goal: **10× lower loading + browser rendering + capture wall time**, relative to
an isolated baseline with identical CPU allocation. This is not a claim of 10×
pure GPU rendering speed. Browser time currently includes PNG compression,
readback, repeated stability captures, protocol transfer and frame persistence.

An isolated baseline is running under
`results/render-benchmark/train-hd-isolated-baseline/`, using the unmodified
player, all 24 CPUs, two render workers and `LP_NUM_THREADS=2`. Its result will
establish the numeric target; the earlier concurrently measured train render
time of 729.90 s is not an equivalent resource baseline.

Per-frame diagnostics now sum every browser probe and report:

- `browserSeekMs`: seek/update/draw work before capture;
- `browserCaptureMs`: capture hook, readback and PNG encoding;
- `evaluateOverheadMs`: Node-side evaluate wall time minus browser execution,
  including protocol transfer and scheduling, not transfer alone;
- `frameWallMs`: entire settling loop before file persistence.

```sh
python3 tools/l4s/summarize_render_latency.py \
  results/render-benchmark/train-hd-isolated-baseline
```

Concurrent shard stage sums are not batch wall time. Initial frames captured
before instrumentation was updated may lack the new fields; missing measurements
are not silently estimated. Completed-run provenance must be checked when
comparing instrumentation revisions.

Any optimized pipeline must retain full-scene/pose checks and compare the same
200 output frames against the lossless baseline. Lossy JPEG/WebP must not be used
as the sole VMAF reference or silently substituted for lossless output.
