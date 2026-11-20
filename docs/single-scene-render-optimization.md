# Single-scene rendering optimization

Keep the full train scene, original camera trace, 1280×720, 10 FPS and 20 seconds
(200 output frames). Do not reduce scene size, resolution or cadence. Encoding
and VMAF are not the optimization target and remain unchanged.

Goal: **10× lower loading + browser rendering + capture wall time**, relative to
an isolated baseline with identical CPU allocation. This is not a claim of 10×
pure GPU rendering speed. Browser time currently includes PNG compression,
readback, repeated stability captures, protocol transfer and frame persistence.

The completed isolated baseline is retained under
`results/render-benchmark/train-hd-isolated-baseline/`, using the unmodified
player, all 24 CPUs, two render workers and `LP_NUM_THREADS=2`. Loading + rendering
+ capture took **666.96 s**, establishing a **66.70 s** tenfold target. Encoding
took 26.50 s, VMAF 6.70 s, and overall wall time was 702.31 s. All 200 VMAF frames
completed with mean encoding-fidelity VMAF 99.967341.
The earlier concurrently measured train render
time of 729.90 s is not an equivalent resource baseline.

Both baseline browser workers loaded capture code before the detailed stage
instrumentation was added, so their logs only contain full per-frame wall time.
Do not infer missing seek/capture/transfer durations from this run. The isolated
manual-update/no-read-pause variant is being measured with current instrumentation
under `results/render-benchmark/train-hd-manual-profile/`. It is not yet a proven
speedup; it must complete and its 200 PNGs/poses must be checked against baseline.

The manual-update variant completed in **762.97 s** for loading/render/capture,
slower than baseline. All 200 PNGs, poses and Gaussian counts match exactly.
Across the two shards, median summed per-frame capture times were 3.18–4.29 s,
seek times 0.35–0.40 s, and evaluate overhead 0.79–0.93 s. These are per-shard
medians, not additive batch wall-time totals.

`L4S_RAW_CAPTURE=1` enables an experimental lossless path: intercept PNG capture,
read RGBA from the default framebuffer, transfer raw bytes as base64, then write
lossless level-1 PNGs in Node. It preserves two-probe stability checks and the
existing encode/VMAF pipeline. Raw frames are larger on the protocol, so this is
not guaranteed faster. The experiment is under
`results/render-benchmark/train-hd-raw-capture/`; decoded pixel equality rather
than compressed PNG byte equality is required before accepting its output.

The raw-over-base64 trial completed in **843.64 s** for loading/render/capture
(877.40 s overall), versus 762.97 s for the same manual-update player using
browser PNG capture and 666.96 s for the original baseline. It is not a speedup
and remains opt-in only. Median protocol/evaluate overhead increased to
1.13–1.30 s per frame, and median capture times were 3.55–4.61 s. Disabling
browser PNG compression did not remove the measured capture bottleneck; these
timings include synchronous readback and raw-to-base64 conversion. They do not
prove PNG compression itself takes seconds or rule out binary streaming.
The 66.70-second target remains unmet. VMAF completed for all 200 frames with
the same mean 99.967341, but equal VMAF alone is not a pixel-equality test.
An independent decoded-RGBA comparison subsequently confirmed exact equality
for all 200 1280×720 frames, including matching poses, timestamps and Gaussian
counts (`train-hd-raw-capture/validation.json`). Correctness passed, speed did not.

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
