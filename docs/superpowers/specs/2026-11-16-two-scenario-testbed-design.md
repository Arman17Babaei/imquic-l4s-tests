# Two-scenario scene testbed

Status: shared-path QEMU design previously approved. The deterministic IMQUIC
ideal revision is implemented. Prior experiment evidence was intentionally
cleared; fresh validation outputs belong under `results/`.

## Intent

Maintain exactly two experiment scenarios: ideal scene delivery at a specified
bandwidth, and a QEMU packet network carrying a traced IMQUIC scene alongside
one unlimited Cubic TCP stream and one unlimited Prague TCP stream. Keep two
switches and remove alternatives that do not support these scenarios. Favor
deletion and ordinary configuration over new abstractions.

## Approaches

The user selected both background streams sharing the scene path through
s1 → s2. The alternative, one background per switch sharing a different hop,
was rejected. Consolidate the existing topology implementations rather than
adding another selectable topology.

## QEMU layout

Three sender/receiver pairs attach to s1 and s2 respectively: the existing
scene server/client, a dedicated Cubic pair, and a dedicated Prague pair.
All forward data crosses the same s1 → s2 egress and DualPI2 queue. Exactly two
switches exist. There is one TCP data connection per background pair, without
iperf bitrate limits or parallel streams; protocol control connections are
not counted as background streams.

Builder proposal: move the configurable bottleneck to the inter-switch link,
in both directions, so it constrains all three streams. Access links remain
300 Mbps. This replaces the suite's client-only bottleneck, which the new
dedicated TCP receivers would otherwise bypass. Keep propagation on separate
endpoint egresses from dropping AQMs. One configured delay is the total
one-way propagation for each pair, split equally over all three links. The inter-switch link uses separate ingress
IFBs for propagation, preserving its DualPI2 egress queues.
Remove the alternative per-link and background-only delay modes.

Expose bandwidth, one-way delay, DualPI2 target, update interval, step threshold
and coupling factor in the existing experiment configuration. Default AQM
values are 15 ms, 16 ms, 1 ms and 3 respectively. The topology always has two
switches and both backgrounds; neither needs a configurable count or enable
flag. Retain the existing scene QUIC Reno/Prague selection as a parameter of
the QEMU scenario; each run still has exactly one scene stream. Default to
Prague for the example, avoiding an unsolicited comparison sweep.

Cubic uses classic non-ECN TCP. Prague uses the guest's TCP Prague algorithm
with ECN enabled at both endpoints. Select each algorithm explicitly and fail
if the kernel cannot provide it. Record requested settings and actual socket
state; do not equate a successful sysctl with negotiated ECN/L4S. Implementation
must inspect the available guest kernel before fixing any Prague-specific
sysctl values or claiming runtime acceptance.

Start both TCP receivers before their senders. Require established data
connections and received payload for both, then a configurable warmup before
scene services start. Preserve process monitoring, cleanup, interval logs,
socket state and qdisc evidence. A missing or exited background fails the run.

## Ideal scenario: deterministic offline rendering

Answer one question: what would this player display along this camera trace if
scene payload had a constant bandwidth and a fixed one-way delay? Compute object
availability using simulated time, then render offline. Wall-clock execution
speed must not affect camera poses, object selection, arrival times or frame
membership. Rendering may take longer or less time than the simulated duration.

### Approach and YAGNI boundary

The ideal scenario must remain on top of IMQUIC. Preserve its actual MoQ
publisher/subscriber object path and priority handling; a manifest-only Python
scheduler feeding the player directly does not satisfy this requirement.
Use one deterministic driver with the existing player, manifest and camera
trace. Reuse existing virtual-clock accounting only where it does not replace
IMQUIC's object selection or delivery semantics. Do not build a second renderer
or a general network simulation framework.

Selected integration: a private inherited control descriptor gates the real
relay's existing FETCH selection/send loop. The publisher fills the relay cache
before the first request. The driver waits for actual FETCH/REQUEST_UPDATE
acknowledgements before allowing another selection, then receives and verifies
that one actual IMQUIC object. Native QUIC runs locally as a reliable carrier;
its packets, timers, throughput and congestion state consume no model time.
No QUIC simulator or virtual-clock API is needed. This is stronger than gating
already-selected live output: selection itself cannot run ahead of the driver.

Native and same-player repeatability tests are maintained in
`tests/test_ideal_imquic.py` and `tests/test_ideal_playback.mjs`. The implementation
plan identifies interfaces and verification commands; generated evidence is disposable.

Keep only the existing bandwidth, one-way data delay, duration, capture and
quality settings needed here. Retain delay because the current experiments
already compare it; zero is supported by the same calculation. Controls remain
immediate. Do not add packet overhead, loss, RTT, congestion control, jitter,
transport queues, configurable buffering, prefetch prediction or new policies.

### Deterministic execution contract

- Simulated time zero is the initial request at the first trace pose, with an
  empty scene. Do not shift time zero to the first applied object: that would
  hide startup delivery delay. Initial requests and priorities are computed at
  time zero before transmission starts.
- At frame time `t = k / fps`, advance serialization and arrivals through `t`
  using the priorities established at earlier simulated timestamps. Apply all
  objects arriving at or before `t`, set the trace pose for `t`, finish object
  preparation and render. Then compute requests/priority updates for `t` using
  the existing policy for subsequent selections. Use this same ordering at
  every frame; arrivals at a boundary precede that frame's rendering and updates.
- A rate of `x` Mbps supplies `x * 1_000_000 / 8` payload bytes per simulated
  second while work is pending. Count actual encoded object bytes. An object
  selected at `s` finishes at `s + bytes / rate` and becomes available at
  `finish + delay_ms / 1000`.
- Model serialization one
  object at a time without preemption. Carry partial transmission
  across frame boundaries and select the next object immediately on completion,
  through IMQUIC using the latest established priorities and a deterministic
  tie order (subscriber priority, publisher priority, request ID). Do not introduce a separate policy that competes with IMQUIC's
  scheduler. Do not round each frame's byte allowance, bank idle link capacity, skip
  a large object to fit a frame, or reveal partially transmitted objects.
- Camera-driven updates use fixed simulated frame timestamps, never browser
  callback timing or future poses. Decode/upload/render completion is awaited
  without advancing simulated time. Local payload reads are preparation, not
  simulated network delivery; the player may use only objects already available.
- Capture every requested frame; derive quality samples from fixed frame indices
  and render full-scene references at the exact same poses. Advance delivery
  through the trace horizon after the last video frame, keeping horizon counters
  separate from last-video-frame counts. A missing frame is
  a failure, not an accepted timing gap. There are no real-time pacing deadlines
  or ideal-link host lateness checks.

Keep a small object schedule (identity, payload bytes, selection/finish/arrival
times) and per-frame applied-object deltas/counts and pose records alongside existing provenance and
quality outputs. Identical inputs must produce identical schedules and frame
membership. Pixel and quality repeatability is scoped to the same pinned
renderer, assets and environment; cross-GPU bitwise equality is not promised.
One execution per ideal case is sufficient; do not expand ideal cases by scene
congestion controller or rerun count. QEMU retains its repeated live trials.

### Shared pipeline and removal

Reuse export, player object preparation, full-scene reference rendering and
quality analysis. HTTP scene loading remains an internal reference/test facility,
not a third experiment scenario. The ideal path does not launch QEMU. Retain
the native IMQUIC publisher/subscriber and the bridge if the existing player
interface needs it; their presence alone must not make modeled delivery depend
on wall time. Remove obsolete live ideal pacing, buffered playback options and
host timing-validation machinery only after the deterministic IMQUIC path is
verified. Do not retain a selectable manifest-only ideal replacement or add
compatibility adapters. Retain QEMU setup cache and performance tuning for the
QEMU scenario. Keep analysis capable of distinguishing offline and legacy ideal semantics.
Old saved results were intentionally deleted during the clean-baseline reset.

## Cleanup

Use one maintained player topology implementation for direct and suite runs;
the suite may emit a self-contained configured copy for guest provenance.
Remove obsolete thesis/historical topology variants and their tests, the
standalone sustained-profile probe bundle and launcher, and obsolete switches,
background-controller, background-disable and delay-mode options. Update
callers, Makefile checks, examples and documentation together. Do not introduce
compatibility adapters for removed configuration keys; report clear errors.

Remove other unreachable utilities only after checking references. Keep source tools needed for reproducible analysis. Old experiment outputs,
reports and figures were intentionally deleted during the clean-baseline reset. Do not alter submodule implementations
or pointers unless a demonstrated requirement makes it necessary.

## Verification and limits

Add focused regression checks for the exact two-switch graph, shared bottleneck,
one unlimited Cubic and Prague data stream, ECN separation, configurable AQM,
startup failure cleanup, and both supported scenario commands. Run
`make experiment-suite-check` and `make cpu-check`, plus native syntax/fixture
checks if native sources change. Check deletion references and dry-run the
example configuration. Do not add tests solely to mirror deleted code.

For the revised ideal path, verify byte accounting across frame boundaries,
whole-object availability, fixed delay, stable priority ties and frame-boundary
ordering with small fixtures. Prove objects and priority changes traverse the
real IMQUIC path, rather than a replacement scheduler. Verify identical schedules and frame membership
when artificial host waits are inserted between steps, and one short same-player
render with exact reference poses and all requested frames. Run the relevant
player/bridge CPU checks when implementation changes. No research batch or new
plots are needed to validate this design revision.

Attempt a short QEMU smoke with an existing local scene, trace and base image
when available. Verify both live TCP algorithms, ECN negotiation evidence,
background payload, qdisc settings and player scene delivery. Preserve partial
artifacts and distinguish CPU configuration checks from guest runtime evidence.
No repeated research batch, general performance claims, pushes or changes to
existing experiment evidence are part of this implementation request. A local
commit was authorized in the follow-up after the example run.
