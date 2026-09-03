# 3DGS Prague versus BBRv2 diagnosis

## Scope

This note records why the first 30-second 3DGS matrix showed less delivered
scene data with Prague/ECT(1) than with Reno/Not-ECT, although the earlier
eight-second sustained MoQ experiment showed Prague retaining about 84% of a
20 Mbit/s bottleneck against unlimited BBRv2 TCP. It is a diagnosis of the
recorded single-repetition runs, not a general controller ranking.

## The experiments were not equivalent

The earlier sustained result used a 20 Mbit/s HTB bottleneck, unlimited BBRv2,
fixed 16 KiB MoQ objects, about 160 KiB of queued stream data, and kernel-default
DualPI2 parameters. Its effective qdisc was `target 15ms tupdate 16ms
step_thresh 1ms` with 10% Classic protection.

The initial 3DGS matrix used a 300 Mbit/s bottleneck, BBRv2 application-paced
to 0, 150, or 300 Mbit/s, heterogeneous 3DGS objects across three prioritized
subgroups, about 4 MiB of queued stream data, and an explicit `target 1ms
tupdate 1ms`. The latter changed the Classic PI target, not the L4S step
threshold: `step_thresh` remained at its 1 ms default. The update loop was also
made sixteen times more frequent.

The fixed 32 KiB HTB burst represented about 13 ms at 20 Mbit/s but only
0.87 ms at 300 Mbit/s. Combined with no configured propagation delay, this
created a materially different and burst-sensitive operating point.

## Checks against the raw evidence

Prague was configured correctly in IMQUIC: picoquic Prague plus ECT(1), with
the default Prague parameters (`alpha_gain=1/16`, `ce_response=1/2`,
`loss_beta=1/2`, and `sudden_ce_threshold=1/2`). BBRv2 was confirmed by iperf3,
and its TCP ECN was disabled with TOS forced to Not-ECT.

DualPI2 classification also worked. Reno produced no L4S packets or ECN
feedback. Prague packets entered the L4S queue and accumulated 41,388 CE marks
at 150 Mbit/s offered background load and 43,551 at 300 Mbit/s. Mean Prague
alpha rose from 0.048 without background traffic to 0.278 and 0.501 at the two
loaded points. Its mean cwnd fell to 11.6 KiB and 6.5 KiB, versus Reno's
28.6 KiB and 20.1 KiB.

The application adapter did not starve Prague. Under load, both modes averaged
4.06--4.08 MiB of queued stream data and were at or above the 4 MiB outstanding
limit for 91--93% of transport samples. Publisher-queued minus
subscriber-received data was about 4.2 MiB in every deadline-limited case,
matching the intended queue window. The largest source object was only about
236 KiB.

At the 150 Mbit/s point, BBRv2 achieved 149.8 Mbit/s against both foregrounds.
Because it was application-capped, it could not consume capacity left unused
when Prague reacted to CE; Reno's larger loss-driven window used more of that
remainder. At the 300 Mbit/s point, BBRv2 was competition-limited and achieved
206.9 Mbit/s against Reno versus 218.1 Mbit/s against Prague, consuming the
share Prague yielded.

The top-level repository and transport submodule revisions were the same for
the sustained and 3DGS comparisons. The observed reversal therefore does not
show a Prague implementation regression. It reflects changed AQM, shaping,
background-pacing, link-rate, and workload controls.

## Corrected profile

Future capture-free 3DGS shared-load runs use explicit values close to the
earlier validated DualPI2 operating profile:

- HTB rate: 300 Mbit/s;
- HTB `burst` and `cburst`: 512 KiB (about 14 ms at 300 Mbit/s);
- DualPI2 Classic `target`: 15 ms;
- DualPI2 `tupdate`: 16 ms;
- DualPI2 L4S `step_thresh`: 1 ms;
- Classic protection: kernel default, verified in `tc -s -d` evidence;
- packet capture: disabled.

This profile intentionally permits a deeper Classic queue while preserving the
1 ms L4S threshold, moving the scheduler back toward its intended L4S-favoring
DualQ operating regime.

## Corrected-profile validation

One capture-free repetition was run at each loaded point with the corrected
profile. Both endpoint timelines, bundles, transport metrics, iperf3 results,
interface counters, and qdisc snapshots validated.

| BBRv2 offered load | Reno completion | Prague completion | Reno TCP | Prague TCP |
|---:|---:|---:|---:|---:|
| 150 Mbit/s | 20.11 s | 16.92 s | 149.81 Mbit/s | 149.81 Mbit/s |
| 300 Mbit/s | 25.01 s | 16.58 s | 168.19 Mbit/s | 168.24 Mbit/s |

All 2,712,029 splats and all scene bytes arrived in all four cases. At the
150 Mbit/s point, Prague's mean alpha fell from 0.278 under the original
profile to 0.069, and its CE count fell from 41,384 to 24,020. At the corrected
300 Mbit/s point its mean alpha remained 0.069. Effective qdisc evidence
confirmed `target 15ms`, `tupdate 16ms`, `step_thresh 1ms`, 10% Classic
protection, and correct Classic/L4S classification.

These corrected single repetitions restore the direction seen in the earlier
sustained experiment: Prague completes the scene sooner while holding a much
lower mean RTT (about 0.66 ms) than Reno (about 9.8 ms). This supports the
configuration diagnosis, but repetitions are still required before treating
the completion-time deltas as statistically stable.

## Plot interpretation

The detailed comparison plot now uses four vertically aligned panels with a
wrapped legend: cumulative received splats, one-second splat arrival rate,
foreground payload throughput, and estimated queue delay. Foreground
throughput is computed from subscriber-received application payload bytes in
one-second arrival bins, so it is goodput rather than IP-layer wire rate.

Queue delay is estimated for each run as `smoothed RTT - minimum observed
smoothed RTT`. It shows the transport-visible delay trend, but it is not a
direct qdisc sojourn-time measurement and may include endpoint or transport
effects. The experiment only records qdisc snapshots before and after a case,
which are insufficient for a direct queue-delay time series.

| BBRv2 load / foreground | Mean goodput until last arrival | Peak 1 s goodput | Mean estimated queue delay | P95 estimated queue delay |
|---|---:|---:|---:|---:|
| 50% / Reno | 189.98 Mbit/s | 206.82 Mbit/s | 9.120 ms | 15.002 ms |
| 50% / Prague | 225.81 Mbit/s | 244.00 Mbit/s | 0.601 ms | 0.789 ms |
| 100% / Reno | 152.72 Mbit/s | 190.65 Mbit/s | 6.829 ms | 17.311 ms |
| 100% / Prague | 230.45 Mbit/s | 248.60 Mbit/s | 0.589 ms | 0.770 ms |

Derived plots generated from the promoted CSV evidence are named
`splat-arrivals-detailed.svg` in each corrected-profile record. A combined
four-case view is under
`results/l4s/3dgs-l4s-profile-plots/splat-arrivals-detailed.svg`. Future runs
produce the detailed layout as their standard `splat-arrivals.svg`.

## Evidence

- `results/records/moq-8s-unlimited-cc-matrix-20260817/`
- `results/records/3dgs-bg0-reno-prague-20260817/`
- `results/records/3dgs-bg50-reno-prague-20260817/`
- `results/records/3dgs-bg100-reno-prague-20260817/`
- `results/records/3dgs-l4s-profile-bg50-20260817/`
- `results/records/3dgs-l4s-profile-bg100-20260817/`
- `l4s/mininet-benchmark.md`
