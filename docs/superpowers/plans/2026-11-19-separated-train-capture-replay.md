# Train: independent network capture and offline arrival replay

User scope: implement and try Train BBR/Prague with real IMQUIC, 150 Mbps,
20 ms RTT and both TCP backgrounds. Measure safe parallelism on this host.
Preserve all existing dirty work and previous results. No full matrix rerun.

Timing contract:
- Real-time headless adapter runs inside the receiver guest namespace, without
  a renderer, using shared Three.js camera/ABR code and 100 ms priority ticks.
- All nonempty objects are FETCHed. Completion means fully received objects,
  not decoded/applied geometry. Native complete-object monotonic timestamps
  and adapter request-start clock share the guest clock.
- Journal contains object identity, payload size and SHA256, arrival time,
  priority events, tick lateness and native output-queue metrics. Immutable
  manifest payloads are verified offline; no GPU or PNG work occurs in capture.
- Offline replay at logical t exposes only complete objects with arrival <=t.
  Render after decode/dependencies/GPU sorting settle; await each PNG write.
  Rendering wall-clock time never advances logical delivery or priorities.
- This is network-arrival-limited visual quality, not real-time renderer QoE.

Tasks:
- [x] Add/test a headless capture client using shared ABR/camera code and existing
      MoqtSidecar. Test horizon filtering, completion tracking and tick schedule.
- [x] Add/test recorded-arrival replay backend and player transport. Validate
      manifest hashes, payload identities, monotonic time and no-future frames.
- [x] Wire isolated QEMU collection and host replay runner; provenance and
      cancellation preserve ownership and partial evidence.
- [x] Run CPU/native checks and a synthetic collect/replay smoke.
- [x] Train baseline: one Prague and one BBR capture, 20 s, 150 Mbps, RTT20,
      4 vCPU/2 GiB each. Replay serially at 5 fps; require 100/100 PNGs and
      frame-by-frame object availability audit before plotting.
- [x] Check 2 concurrent independent 4-vCPU capture VMs against baseline using
      update lateness, native queue blocking, background health and CPU/IO
      pressure. GPU replay stays outside capture during this comparison.
- [x] Measure one replay's GPU/RAM peak; test two replay jobs only if measured
      headroom supports it. Recommend only observed-safe concurrency.
- [x] Generate Train comparison, retain raw evidence and independent whole-change
      review. Report PASS/PARTIAL/BLOCKED separately with measured limits.

Implementation ownership (parent only): new collector/client/model tests in
player, replay backend + run_network_capture.py and focused tests in harness;
small opt-in hooks in existing guest runner, bridge and player launcher.

Ruling: use the existing network, publisher/relay, camera and priority code;
record immutable payload identity rather than writing another bulk payload copy.
Ruling: 100 ms priority cadence is explicit and independent of rendering. New
results are a new experiment profile and are never pooled with old live runs.

Completed evidence: `results/separated-train-20261119/README.md`, comparison
PDF/CSV/provenance and parallelism JSON. Four successful real network captures;
two serial replays plus the same two journals replayed concurrently; 100 frames
each, exact serial/parallel RGB agreement. One earlier staging failure retained
and excluded. Native audit and background health independently verified.

Validation: `make cpu-check` passed (124 Python tests, one skip; 31 harness Node
tests; player tests/build passed), `make camera-trace-player-check` passed
(native syntax/unit fixture and 24 Python tests). The CUDA-unavailable and
unsupported-playback-delay messages in test logs are expected negative tests;
real CUDA scoring passed separately. `git diff --check` passed for touched
tracked harness files and the player checkout. Independent read-only review
found four issues; all fixed with regressions and re-review found no material
remaining issues.

Measured limit: two capture VMs OR two candidate replays, not both stages
combined. Full references and scoring remain serial. Higher concurrency and
other scenes are untested; the pilot does not establish statistical equivalence
between serial and parallel network captures.
