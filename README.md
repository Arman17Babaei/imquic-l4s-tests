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

The coexistence matrix runs each mode at 0, 5, 10, and 20 Mbit/s requested
classic TCP background load, with five repetitions by default (60 cases).
The background is iperf3/TCP on port 5201 with ECN disabled. Each case creates
fresh HTB and DualPI2 qdiscs, so their counters do not carry into the next case.

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

Use `--background-mbps`, `--repetitions`, `--transfer-bytes`, `--bottleneck`,
and `--modes` to select a smaller or custom matrix. For example:

```sh
sudo python3 tools/l4s/run_mininet_benchmark.py \
  --output results/l4s/mininet-smoke \
  --background-mbps 0,10 --repetitions 1
```

Generated results remain ignored by Git. Only update the compact tracked
evidence in `l4s/` from a completed reproducible run, with the environment and
submodule revisions recorded in [`l4s/validation-report.md`](l4s/validation-report.md).

## Reference evidence

The tracked historical summary is [the Mininet report](l4s/mininet-benchmark.md)
and its [comparison plot](l4s/qemu-evidence/mininet-benchmark-comparison.svg).
It is descriptive evidence from the pinned environment, not a formal
statistical significance claim.
