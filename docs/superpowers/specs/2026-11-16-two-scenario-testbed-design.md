# Two-scenario scene testbed

Status: approved; shared-path approach selected by the user.

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
one-way propagation for each pair, split equally over its two endpoints.
Remove the alternative per-link and background-only delay modes.

Expose bandwidth, one-way delay, DualPI2 target, update interval, step threshold
and coupling factor in the existing experiment configuration. Default AQM
values are 15 ms, 16 ms, 1 ms and 1 respectively. The topology always has two
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

## Ideal scenario and shared pipeline

Retain ideal bandwidth serialization and its existing data-delay option,
trace-driven priorities, player capture, full-scene reference rendering and
quality analysis. Retain buffered ideal playback because it directly supports
this scenario. HTTP scene loading remains an internal reference/test facility,
not a third experiment scenario. Preserve export, native publisher/subscriber,
bridge, QEMU setup cache, optional performance tuning, and suite analysis where
they support these two scenarios.

## Cleanup

Use one maintained player topology implementation for direct and suite runs;
the suite may emit a self-contained configured copy for guest provenance.
Remove obsolete thesis/historical topology variants and their tests, the
standalone sustained-profile probe bundle and launcher, and obsolete switches,
background-controller, background-disable and delay-mode options. Update
callers, Makefile checks, examples and documentation together. Do not introduce
compatibility adapters for removed configuration keys; report clear errors.

Remove other unreachable utilities only after checking references. Preserve
saved results, tracked historical reports/figures, and tools necessary to
reproduce analysis of retained evidence. Do not alter submodule implementations
or pointers unless a demonstrated requirement makes it necessary.

## Verification and limits

Add focused regression checks for the exact two-switch graph, shared bottleneck,
one unlimited Cubic and Prague data stream, ECN separation, configurable AQM,
startup failure cleanup, and both supported scenario commands. Run
`make experiment-suite-check` and `make cpu-check`, plus native syntax/fixture
checks if native sources change. Check deletion references and dry-run the
example configuration. Do not add tests solely to mirror deleted code.

Attempt a short QEMU smoke with an existing local scene, trace and base image
when available. Verify both live TCP algorithms, ECN negotiation evidence,
background payload, qdisc settings and player scene delivery. Preserve partial
artifacts and distinguish CPU configuration checks from guest runtime evidence.
No repeated research batch, general performance claims, pushes or changes to
existing experiment evidence are part of this implementation request. A local
commit was authorized in the follow-up after the example run.
