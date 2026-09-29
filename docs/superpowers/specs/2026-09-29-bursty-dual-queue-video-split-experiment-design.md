# Bursty Dual-Queue Video-Split Experiment Design

## Purpose

Measure how assigning importance-ranked 3DGS video payload between a Classic
Reno/Not-ECT connection and an L4S Prague/ECT(1) connection behaves when both
DualPI2 queues at both network bottlenecks receive independently bursty,
congestion-controlled background traffic.

This is a new experiment. Existing records and presentation figures must not be
relabelled as evidence for this topology or workload.

## Research question and hypothesis

The independent variable is the fraction of total video payload assigned to the
Prague connection after ranking objects by `(layer, mean opacity)`. The tested
L4S shares are `1.00`, `0.75`, `0.50`, `0.25`, and `0.00`; the remaining payload
uses Reno.

The primary hypothesis is:

> Under independently bursty congestion in both DualPI2 bottlenecks, assigning
> the most important video objects to L4S while leaving less important objects
> on Classic can outperform both all-Classic and all-L4S endpoint
> configurations.

The hypothesis is falsified if none of the mixed splits provides a useful
latency, completion, or rendered-quality advantage over both endpoints. A
failed validity gate is a method failure or inconclusive result, not evidence
for or against the hypothesis.

## Execution environment

The experiment runs only through the repository's QEMU wrapper. The guest must
boot the pinned experiment kernel and pass preflight checks for:

- the `dualpi2` qdisc;
- Prague congestion control in IMQUIC;
- ECT(1) emission for Prague traffic;
- Not-ECT emission for Reno traffic;
- capture-safe interface offload settings; and
- the expected repository and recursive submodule revisions.

The host kernel is not an acceptable substitute. The QEMU and Mininet command
line, kernel version, qdisc parameters, tool versions, source hashes, random
seeds, and live topology are recorded in `provenance.json`.

## Network topology

The Mininet guest contains two serial 100 Mbit/s DualPI2 bottlenecks:

```text
video server -> s1 (DualPI2, 100 Mbit/s) -> s2 (DualPI2, 100 Mbit/s) -> video client
                    ^                         ^
                    |                         |
          background pair 1        background pair 2
```

The base video RTT is 20 ms. Propagation delay remains separate from the burst
generator.

Background pair 1 contains one Prague/ECT(1) connection and one Reno/Not-ECT
connection. It joins before the shaped `s1` egress and terminates after that
bottleneck. Background pair 2 is an independent Prague/Reno pair that joins
before the shaped receiver-facing `s2` egress and terminates at the client side.
Consequently, each physical bottleneck receives local Classic and L4S
background traffic without relying on traffic already shaped by the upstream
bottleneck.

Background endpoints use distinct ports and logs. They do not reuse video
tracks or alter the video object's priority ranking.

## Bursty background workload

All four background connections are persistent congestion-controlled IMQUIC
connections:

- L4S background uses Prague and ECT(1).
- Classic background uses Reno and Not-ECT.

Each connection has an independent deterministic schedule. Every ON and OFF
duration is sampled independently from the discrete uniform set
`{7, 8, 9, 10, 11, 12, 13}` ms. Connections are not synchronized and use
distinct recorded seeds.

During each ON interval, a connection offers 100 Mbit/s of application data.
During each OFF interval, it offers no new objects, but the connection stays
open and retains its congestion-control state. The transport may continue
draining data already admitted before the OFF transition; the schedule log and
transport queue metrics make that behavior observable.

Equal peak rates and identical duration distributions define a 50/50 Classic
and L4S offered-load target at each bottleneck. Actual offered bytes must be
within 5% across the two classes. Delivered throughput is an outcome, not forced
to 50/50, because Reno and Prague respond independently to congestion.

Schedules are generated before a case begins and stored as JSON containing the
seed, every state transition, sampled duration, configured ON rate, and planned
end time. This permits exact replay without synchronizing the four connections.

## Video workload and experimental matrix

The experiment reuses one frozen demand trace and one source bundle across all
cells. The split policy assigns the importance-ranked payload-byte prefix to
Prague and the remainder to Reno. A split therefore describes transport
assignment, not residual backlog.

The five L4S payload shares are:

| Cell | Prague/L4S | Reno/Classic |
|---|---:|---:|
| all-l4s | 100% | 0% |
| l4s-75 | 75% | 25% |
| l4s-50 | 50% | 50% |
| l4s-25 | 25% | 75% |
| all-classic | 0% | 100% |

Each cell has three repetitions. Case order is counterbalanced, and every case
starts with fresh Mininet state and new transport connections. The frozen demand,
source bundle, qdisc configuration, background rate distribution, video deadline,
and rendering settings remain identical across cells.

Background traffic warms up for two seconds before the video gate opens and
continues through the fixed 30-second video deadline. Missing video objects stay
in the completion denominator and are censored at the deadline.

## Instrumentation

### Video and transport

For both video connections, record:

- object readiness, admission, transmission, and receive-completion times;
- assigned, received, and censored payload bytes;
- throughput, smoothed RTT, congestion window, bytes in flight, transport-queued
  bytes, pacing rate, ECN counters, and Prague alpha; and
- exact object-to-transport assignment from the spectrum manifest.

### Background

For every background connection, record:

- its generated schedule and seed;
- planned and actual ON/OFF transitions;
- application-offered and receiver-delivered bytes;
- connection lifetime and overlap with warm-up and video measurement; and
- transport metrics at one-millisecond resolution.

### Switch queues

Capture packet headers immediately before enqueue and after dequeue using the
same QEMU guest clock. Disable relevant offloads before capture. Match packets
across each shaped egress and classify them by five-tuple and ECN codepoint.

The primary queue-delay evidence is direct same-clock packet residence, not
RTT-minus-minimum. Produce one-millisecond time bins separately for ECT(1) and
Not-ECT traffic at both switches. Preserve pre-case and post-case
`tc -s -d qdisc` and class output for queue configuration, CE marks, drops, and
aggregate counters. Packet captures remain ignored run artifacts and are not
committed.

## Outcomes and analysis

Primary video outcomes are:

- ready-to-receive-completion latency per object;
- weighted important-byte completion versus time;
- delivered object and byte coverage at 30 seconds;
- rendered SSIM over time; and
- post-viewpoint-change quality recovery.

Mechanism outcomes are:

- Classic and L4S queue-delay mean, p95, p99, standard deviation, and
  peak-to-peak range at each switch;
- repeated queue rise-and-drain cycles;
- CE marking and drop rates by class and switch;
- foreground and background throughput by connection; and
- background schedule-to-wire timing and delivered-share differences.

Aggregate repetitions with the median and show the minimum-to-maximum range.
With three repetitions, the report does not claim strong statistical
significance. Any suggested optimal split must be described as scoped to this
topology, trace, burst distribution, and controller configuration.

## Validity gates

A case is valid only if all of the following hold:

1. Both shaped egresses report the requested 100 Mbit/s DualPI2 configuration.
2. Prague packets are ECT(1), and Reno packets are Not-ECT.
3. The four background connections use distinct seeds and non-identical
   transition sequences.
4. Classic and L4S background offered bytes differ by no more than 5% at each
   bottleneck.
5. Both traffic classes at both switches show repeated, resolved rise-and-drain
   cycles in the residence-time series.
6. Every background connection remains alive through warm-up and the video
   deadline.
7. The spectrum manifest's requested and actual video byte shares are within
   the existing split tolerance.
8. Video release schedules and source hashes match across cells.
9. Capture completeness, packet matching, and clock consistency pass.
10. Rendering uses only successfully received objects and preserves censored
    objects in network-level denominators.

If a gate fails, preserve the run as diagnostic evidence but exclude it from the
scientific comparison.

## Staged execution and stopping rules

1. Build and test the QEMU guest fixture and analyzers without running the full
   matrix.
2. Run one repetition of all five video splits as a pilot.
3. Stop and inspect topology, markings, offered-load balance, connection
   lifetimes, packet matching, and queue volatility.
4. If any pilot cell is invalid, repair the method and repeat the pilot rather
   than continuing the matrix.
5. If the pilot is valid, run the remaining two repetitions.
6. Analyze network outcomes before rendering.
7. Render and calculate SSIM only for the final valid matrix.
8. Promote the completed result with its hypothesis, interpretation, caveats,
   `result.json`, and exact regeneration commands.

The workflow must pause if a valid pilot falsifies the expected mixed-split
benefit. Scientifically negative evidence must not be treated as a harness
failure or hidden by continuing to tune parameters.

## Implementation boundaries

The implementation may extend the sustained IMQUIC MoQ fixture with a replayable
ON/OFF schedule and application-rate budget, add the four local background
connections to the existing 3DGS QEMU/Mininet runner, and add analysis and
plotting support for the new records. It must not change existing promoted
records, silently replace the current Figure 12 evidence, or initialize the
nested picoquic dependency under IMQUIC.

All live outputs stay under `results/`. Only a validated promoted result and
publication-ready summaries or plots may be considered for tracked evidence.

## Acceptance criteria

- Unit and self-tests cover schedule generation, deterministic replay, traffic
  classification, split construction, validity gates, and aggregation.
- The QEMU preflight proves the required kernel, DualPI2, and Prague behavior.
- The five-cell pilot produces valid, independently bursty L4S and Classic
  queue evidence at both switches.
- The completed matrix contains three valid repetitions per split or explicitly
  reports why completion stopped.
- Every reported chart can be regenerated from the promoted record without a
  new network run.
