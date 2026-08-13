# IMQUIC L4S tests

This repository is a reproducible L4S/Prague experiment harness for IMQUIC.
It pins the transport implementations as submodules and owns the traffic
fixture, QEMU/Mininet runners, analyzers, reports, and reproducible evidence.

- `deps/imquic` — IMQUIC with the Prague configuration and metrics API.
- `deps/picoquic` — the authoritative, pinned picoquic checkout.
- `tests/l4s-test.c` — mirrored into `deps/imquic/src/l4s-test.c`; provides
  loopback and network traffic modes for the experiment binary.
- `tools/l4s/` — experiment runners and analyzers.
- `l4s/` — tracked reports and compact reference evidence.
- `results/` — ignored live-run output: captures, logs, CSVs, and plots.

Do not initialize IMQUIC's nested picoquic dependency: the top-level
`deps/picoquic` submodule is the checkout used by this harness.

## Scenarios inspected

The harness uses the same 4 MiB bidirectional QUIC STREAM fixture in all live
scenarios. The client sends a deterministic payload, the server echoes it,
and the client verifies every byte before reporting transport metrics. The
implementation is in [`tests/l4s-test.c`](tests/l4s-test.c), mirrored in
[`deps/imquic/src/l4s-test.c`](deps/imquic/src/l4s-test.c).

| Scenario | Purpose | Implementation method | Required evidence |
| --- | --- | --- | --- |
| Fixture and loopback smoke test | Verify Prague endpoint construction, option validation, payload integrity, and populated metrics without a bottleneck. | `imquic-l4s-test` with no arguments; built and invoked by `make check` in `deps/imquic/src/Makefile.am`. | Invalid Prague and ECN options reject; 256 KiB loopback payload is echoed; cwnd, pacing, and Prague metrics are populated. |
| Prague time series | Verify an actual L4S response, rather than only controller arithmetic. | [`run_timeseries_test.sh`](tools/l4s/run_timeseries_test.sh) creates sender/router/receiver namespaces. Both router egresses use HTB plus real `sch_dualpi2`; the fixture uses Prague with ECT(1). | Forward ECT(1), reverse CE, ACK_ECN CE feedback, changing Prague alpha, and at least one CE-associated cwnd reduction. |
| Reno / Not-ECT coexistence | Establish the classic non-ECN control path under the same bottleneck and TCP background. | [`run_mininet_benchmark.py`](tools/l4s/run_mininet_benchmark.py) launches the fixture as `l4s-off` (`reno`) in `client -- s1 -- server`. | No ECT(0), ECT(1), or CE feedback in QUIC; the background TCP flow remains Not-ECT. |
| Reno / ECT(0) coexistence | Separate classic ECN behaviour from Prague and L4S queue classification. | The same Mininet harness launches `l4s-ect0` (`reno-ect0`). DualPI2 classifies it in the classic queue. | Forward ECT(0), CE marks, and QUIC ACK_ECN CE feedback; no ECT(1). |
| Prague / ECT(1) coexistence | Measure L4S behaviour under the same competing classic TCP load. | The same Mininet harness launches `l4s-on` (`prague`) with ECT(1), which DualPI2 classifies in its L4S queue. | Forward ECT(1), CE marks, ACK_ECN feedback, and nonzero L4S qdisc packet/mark counters; no ECT(0). |

The coexistence matrix runs each mode at 0, 5, 10, and 20 Mbit/s requested TCP
background load, with five repetitions by default (60 cases).
The background is iperf3/TCP BBR on port 5201 with ECN disabled. Each case
creates fresh HTB and DualPI2 qdiscs, so their counters do not carry into the
next case. Use `--background-congestion reno` or
`--background-congestion cubic` to reproduce alternative TCP baselines.

## How the analysis works

### Time-series analysis

[`analyze_timeseries.py`](tools/l4s/analyze_timeseries.py) reads the
10 ms `metrics.csv` written by the client fixture. It rejects a run unless its
timestamps increase, ECT(1)/CE feedback counters are monotonic and nonzero,
Prague alpha changes, cwnd changes, and at least one cwnd reduction follows CE
feedback. The runner independently uses tshark to require capture-visible
ECT(1) and CE packets. The output is `analysis.json`, packet counts, qdisc
counters, endpoint logs, and captures.

### Coexistence analysis

[`analyze_mininet_benchmark.py`](tools/l4s/analyze_mininet_benchmark.py)
combines the per-case `metrics.csv`, metadata, DualPI2 counters, iperf3 JSON,
and switch captures. Captures are header-only (128-byte snap length), which
retains Ethernet/IP/TCP/UDP headers and `ip.len` while avoiding guest-disk
exhaustion during the complete matrix.

For every case the analyzer verifies:

- `l4s-off`: no QUIC ECN marking or CE feedback.
- `l4s-ect0`: capture-visible ECT(0), CE, and endpoint CE feedback; no ECT(1).
- `l4s-on`: capture-visible ECT(1), CE, endpoint CE feedback, and nonzero L4S
  qdisc classification; no ECT(0).
- The background TCP flow has no ECN-marked packets.
- Every selected mode reaches at least 70% observed bottleneck utilisation.
- The matrix contains every requested rate/mode/repetition exactly once.

QUIC goodput is payload bytes divided by the client metric interval. Concurrent
wire load is calculated from client-to-server QUIC and TCP IP bytes in the
exact wall-clock interval of the QUIC transfer, so differently timed averages
are not combined. QUIC forward drops are inferred from the packet-count deficit
between the two switch-port captures. TCP GSO can coalesce ingress segments, so
background TCP drops are inferred from forward retransmissions instead. The
forward DualPI2 drop counter is retained as an attribution cross-check.

Outputs are `summary.csv` (each run), `aggregate.csv` (mean and sample standard
deviation per mode/load), `analysis.json`, and `comparison.svg`.

## Recreate the results

### Build and fast validation

```sh
make init
make analyzer-check
make build
```

`make build` configures IMQUIC against the top-level picoquic checkout.
The QEMU commands below run the full `make check` suite in their configured
guest before each live network experiment. In a separately configured IMQUIC
checkout, `make -C deps/imquic check` runs the fixture and both analyzer
self-tests; it requires picoquic and picotls libraries to be exposed at the
paths selected by that checkout's `./configure --with-picoquic=...` command.

### Prerequisites for live tests

The preferred route is the QEMU wrapper. It requires:

- `qemu-system-x86_64`, `qemu-img`, `ssh`, `scp`, and Python `pexpect` on the
  host;
- a base guest image at `~/sandbox/p4/work.qcow2` (override with `--base`);
- a guest with the Prague-enabled `sch_dualpi2` kernel module, Mininet, Open
  vSwitch, iperf3, tcpdump, and tshark;
- a clean Git worktree. The wrapper archives the committed revisions, creates
  a disposable qcow2 overlay, and removes that overlay after the run.

The wrapper provisions build dependencies in the guest when required. It uses a
three-minute authenticated-SSH wait by default; override it with
`--boot-timeout` if the guest needs longer.

### Run the Prague time-series scenario

```sh
python3 tools/l4s/run_qemu_timeseries_test.py \
  --make-target l4s-timeseries-guest-check \
  --destination-prefix qemu-timeseries
```

The completed output is copied to
`results/l4s/qemu-timeseries-<timestamp>/`.

### Run the complete coexistence matrix

```sh
python3 tools/l4s/run_qemu_timeseries_test.py \
  --make-target l4s-mininet-benchmark-guest-check \
  --guest-result-name mininet-run \
  --destination-prefix qemu-mininet
```

The completed output is copied to `results/l4s/qemu-mininet-<timestamp>/`.
Open `comparison.svg`, inspect `analysis.json`, or use `summary.csv` and
`aggregate.csv` for further analysis.

For a directly provisioned host that already has the required root-only
networking dependencies, run:

```sh
sudo python3 tools/l4s/run_mininet_benchmark.py \
  --output results/l4s/mininet-run
```

Use `--background-mbps`, `--background-congestion`, `--repetitions`,
`--transfer-bytes`, `--bottleneck`, and `--modes` to select a smaller or custom
matrix. For example:

```sh
sudo python3 tools/l4s/run_mininet_benchmark.py \
  --output results/l4s/mininet-smoke \
  --background-mbps 0,10 --repetitions 1
```

Generated results remain ignored by Git. Only update the compact tracked
evidence in `l4s/` from a completed reproducible run, with the environment and
submodule revisions recorded in [`l4s/validation-report.md`](l4s/validation-report.md).

## Sustained MoQ coexistence

The sustained test uses one server-side MoQ namespace/track and an explicit
client subscription. The publisher keeps ten bounded 16 KiB sequential
objects outstanding across QUIC's wire flight and stream-send queue, avoiding
application-side polling gaps while IMQUIC remains responsible for pacing and
the congestion-controlled wire flight during the 60-second transfer. The
subscriber validates object order and payload contents. Transport metrics,
including queued stream bytes, are sampled every 10 ms.

The default matrix crosses `l4s-off`, `l4s-ect0`, and `l4s-on` with two
classic TCP modes—Not-ECT and ECN-capable ECT(0)—and runs three repetitions,
for 18 cases. The ECN-capable TCP case enables conventional negotiated ECN;
it explicitly rewrites only the test flow to ECT(0), so it never uses ECT(1)
or an L4S congestion controller. The MoQ publisher and the paced 10 Mbit/s
TCP iperf3 sender both run on the server and send to the client, so their data
packets share the same server-to-client HTB + DualPI2 bottleneck. TCP starts
five seconds before the 60-second MoQ overlap and continues five seconds
afterward:

```sh
make build
sudo python3 tools/l4s/run_sustained_coexistence.py \\
  --output results/l4s/sustained-moq \\
  --tcp-ecn-modes not-ect,ect0
```

For the prepared QEMU guest, the host-side wrapper needs no privilege. It
archives the repository and submodule revisions with the run:

```sh
python3 tools/l4s/run_qemu_timeseries_test.py \\
  --make-target l4s-sustained-moq-check \\
  --guest-result-name sustained-moq
```

The full 18-case suite has a 30-minute guest-command allowance by default. To
override it explicitly, add `--guest-timeout 1800` to the wrapper command.

Each case stores metrics, iperf3 JSON, packet captures, timestamps, DualPI2
counters, `timeline.csv`, and `timeline.svg`. The analyzer aligns TCP
intervals to the recorded start time and resamples IMQUIC's 10 ms samples into
one-second overlap bins. Cwnd values are compared diagnostically as
byte-valued sender reports, not as identical controller semantics. The
timeline's capture panel shows per-second ECT(0), ECT(1), and CE packet counts
separately for MoQ and TCP. It also writes `summary.json` with per-repetition
evidence and foreground/TCP-ECN aggregates, plus `aggregate.svg` with scenario
mean bars and individual-repetition points for foreground/TCP rate, bottleneck
utilisation/share, and maximum cwnd:

```sh
python3 tools/l4s/analyze_sustained_coexistence.py --self-test
```

The analyzer enforces 18 completed cases, 60-second foreground coverage,
non-empty captures, TCP measurements in at least 90% of overlap bins, and a
14--21 Mbit/s combined-wire-rate tolerance. TCP's measured overlap rate and
the number of bins with delivered TCP wire traffic are reported as coexistence
outcomes, rather than treated as fairness pass/fail thresholds. It also checks
the foreground Not-ECT, ECT(0), and Prague ECN/CE invariants, Prague's L4S
classification plus CE-associated cwnd reduction, and that TCP is either
wholly Not-ECT or conventional ECT(0) without ECT(1), as selected. If an
acceptance condition fails, it still writes every timeline,
SVG, and `summary.json`; the latter records `acceptance_passed: false` and the
exact per-case reasons, while the analyzer exits nonzero.

## Incremental independent TCP streams

The step-join experiment starts ten independent, unlimited iperf3 TCP
connections across ten phases. `stream_01` starts with phase 1, `stream_02`
joins at phase 2, and so on; every connection remains active through the common
experiment endpoint. Each client has its own process, socket, client port, and
server port, so every stream has an independent congestion window rather than
sharing transport state through iperf3 parallel mode. Reno is the default.

Phases are five seconds and the experiment runs three repetitions by default,
giving a 50-second timeline per repetition. The topology and 20 Mbit/s
HTB+pfifo bottleneck match the Reno fairness baseline, and all test traffic is
forced to Not-ECT. Run it directly on a provisioned host with:

```sh
sudo python3 tools/l4s/run_reno_step_join.py \
  --output results/l4s/reno-step-join
```

Use `--phase-seconds`, `--repetitions`, `--bottleneck`, and
`--fifo-limit-packets` to override the defaults. Use `--congestion cubic` or
`--congestion bbr` to run the same independent-connection schedule with those
TCP controllers; Reno remains the default. BBR is loaded with `tcp_bbr` when
available, and every selected controller is verified against the guest kernel
before a run starts. The equivalent QEMU-wrapper invocation is:

```sh
python3 tools/l4s/run_qemu_timeseries_test.py \
  --make-target l4s-reno-step-join-check \
  --guest-result-name reno-step-join \
  --destination-prefix qemu-reno-step-join
```

Each repetition contains the iperf3 JSON reports, packet capture, qdisc
statistics, event log, metadata, `timeline.csv`, and two-panel SVG/PNG plots of
per-stream throughput and cwnd. The root additionally contains
`aggregate_timeline.csv`, aggregate SVG/PNG plots with mean and sample-standard-
deviation bands, and `analysis.json`. Throughput is calculated in 250 ms bins
from forward IPv4 bytes observed at the bottleneck; cwnd comes from each
sender's one-second TCP_INFO reports.

The dependency-light schedule, parser, aggregation, and rendering checks are:

```sh
make reno-step-join-check
python3 tools/l4s/analyze_reno_step_join.py --self-test
```

## Reference evidence

The tracked historical summary is [the Mininet report](l4s/mininet-benchmark.md)
and its [comparison plot](l4s/qemu-evidence/mininet-benchmark-comparison.svg).
It is descriptive evidence from the pinned environment, not a formal
statistical significance claim.
