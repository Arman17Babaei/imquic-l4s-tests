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
