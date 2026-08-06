# IMQUIC L4S environment validation

Date: 2026-07-23

## Revisions

- IMQUIC base: `0b4def337956f039753f9358fbdd194db0311e8d`
- IMQUIC environment commit: `c903440b7520c5bd6fe202fe6eaf991af78ad271`
- IMQUIC Prague feature commit: `21edc9cc0db447f45ff041427f9f0f8814d03357`
- IMQUIC dynamics pin commit: `648ab2809b14a3fd293c6446263c32e44e9a9d63`
- IMQUIC loopback traffic commit: `dc626ee28b820dde185ad7b7786ae14747960c00`
- IMQUIC repeated Mininet benchmark commit: `0f13beb1bbfa685808d758df6990a1fd45455b06`
- IMQUIC classic-ECN benchmark commit: `b5bec70d7640f1da80467b36191bbbdcf0e7daba`
- IMQUIC branch: `codex/imquic-l4s-prague`
- IMQUIC fork: `https://github.com/Arman17Babaei/imquic.git`
- picoquic upstream: `https://github.com/private-octopus/picoquic`
- picoquic fork: `https://github.com/Arman17Babaei/picoquic.git`
- picoquic pin: `13671ce7bdf58c278a29da2d49a32f76c21d6c6d`
- picoquic parameter commit: `bbe86f4e6b9d08524a920cac852889dc5ac06496`
- picoquic metrics commit: `04ee27f57212064ec0f2ada2ec3dbf2f7d1fe255`
- picoquic dynamics test commit: `581b2841c2d651a045985999a1e319c64996ff58`
- picoquic generic ECN metrics commit: `6014183ca0c73c2d71995ebad2e377054ca6b2d5`
- picoquic local branch: `codex/prague-params`
- picoquic tracking: Git submodule at `.deps/picoquic-l4s`
- picotls pin: `bfa67875982afc4c24f21e146cef4747fa189c2f`

Both repositories were clean when the results below were recorded.

Initialize the tracked dependency in a fresh checkout with:

```sh
git submodule update --init --recursive
```

## Selected profile

`picoquic-observed-default` renders as:

```text
alpha_gain=1/16,ce_response=1/2,loss_beta=1/2,sudden_ce_threshold=1/2
```

The profile is installed, strictly validated, and propagated to picoquic when
`IMQUIC_CONGESTION_PRAGUE` is selected. Missing options preserve the pinned
picoquic defaults. The controller keeps its dynamic fixed-point alpha estimator.

## Commands and results

```sh
cmake -S . -B build-l4s \
  -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
  -DPICOQUIC_FETCH_PTLS=Y \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build-l4s -j"$(nproc)"
ctest --test-dir build-l4s --output-on-failure
```

Result: picoquic built successfully; `picoquic_ct` and `picohttp_ct` passed
(2/2, 22.16 seconds). The focused `prague_options`, `prague_dynamics`, and
existing `l4s_prague` simulation also passed.

The deterministic synthetic traffic sample can be run directly:

```sh
./picoquic_ct prague_dynamics
```

It covers an unmarked ECT(1) epoch, a 75 ECT(1)/25 CE epoch, and a 40 ECT(1)/60
CE sudden-marking epoch. Exact fixed-point assertions cover default and fast
`alpha_gain`, default and gentle `ce_response`, default and gentle `loss_beta`,
the `sudden_ce_threshold`, and the pinned upstream default formulas.

The dependency was also built in place with PIC so the current IMQUIC static-library discovery can link it:

```sh
cmake -S . -B . \
  -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
  -DPICOQUIC_FETCH_PTLS=Y \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build . -j"$(nproc)"
```

IMQUIC validation:

```sh
sh autogen.sh
./configure --with-picoquic="$PWD/.deps/picoquic-l4s"
make -j"$(nproc)"
make check
```

Result: configuration, compilation, and linking passed. The added
`imquic-l4s-test` passed (1/1). It proves invalid Prague options reject endpoint
creation, then creates Prague server/client endpoints on IPv4 loopback, sends a
deterministic 256 KiB bidirectional STREAM payload, echoes and byte-validates all
262144 bytes, and obtains a populated Prague transport-metrics snapshot.

A representative run reported:

```text
IMQUIC Prague traffic: sent=262144, echoed=262144, rtt_us=706,
cwin=125008, pacing_Bps=221331444, ect1=0, ce=0, alpha=0/1
```

RTT, congestion window, and pacing values vary by run. Zero ECT(1)/CE counters
on this unshaped loopback run are not packet-marking evidence.

Profile validation:

```sh
python3 tools/l4s/prague_profile.py validate config/l4s-prague.yaml
python3 tools/l4s/prague_profile.py render-options \
  config/l4s-prague.yaml picoquic-observed-default
```

Result: validation passed and produced the normalized string shown above.

## Privileged QEMU packet validation

Packet validation ran in an ephemeral qcow2 overlay backed by `work.qcow2`; the
base image was not modified. The guest ran kernel
`5.15.72-48b3db6b4-prague-111` and loaded its real `sch_dualpi2` module. Three
network namespaces formed this routed topology:

```text
l4s-sender (10.10.1.2) -- l4s-router -- (10.10.2.2) l4s-receiver
                                |
                  HTB 2 Mbit/s + DualPI2 on both egress links
```

The complete experiment and analysis run as one unprivileged host command. It
boots an ephemeral overlay of `~/sandbox/p4/work.qcow2`; root-only namespaces,
DualPI2, and packet capture run exclusively inside that guest:

```sh
make l4s-timeseries-check
```

`tools/l4s/run_qemu_timeseries_test.py` refuses a dirty worktree, creates the
overlay, transfers archived IMQUIC and picoquic revisions, provisions and
builds the disposable guest, retrieves results, shuts QEMU down, and removes
the overlay even on failure. `tools/l4s/run_timeseries_test.sh` then creates and cleans the three namespaces,
configures the two shaped DualPI2 egress links, starts packet capture, runs the
Prague-configured IMQUIC server and client, records transport metrics every
10 ms, and invokes `tools/l4s/analyze_timeseries.py`. Results are written below
the ignored `results/l4s/` directory.

The analyzer is part of `make check` through a deterministic positive and
negative self-test. On live data it requires strictly increasing timestamps,
monotonic ECT(1)/CE feedback counters, nonzero ECT(1) and CE, evolving Prague
alpha and congestion window, and at least one CE-associated window reduction.
The runner independently fails if its packet captures contain no ECT(1) or CE.

A fresh ephemeral QEMU run produced:

```json
{
  "alpha_states": 27,
  "ce_associated_cwnd_reduction": true,
  "cwnd_max_bytes": 19627,
  "cwnd_min_bytes": 3120,
  "duration_us": 1601222,
  "final_ce_packets": 170,
  "final_ect1_packets": 155,
  "samples": 157,
  "status": "pass"
}
```

The compact live CSV, JSON result, packet counts, qdisc counters, and endpoint
logs are retained in `l4s/qemu-evidence/`. Full captures from future runs stay
under `results/l4s/` rather than being added to Git.

The current evidence level is **Live Prague response**. The deterministic
picoquic test asserts exact controller arithmetic; the time-series test asserts
the live ordering and direction of ECT(1), CE feedback, alpha evolution, and
congestion-window response without making timing-fragile exact-value claims.

## Mininet L4S coexistence benchmark

The paired coexistence benchmark runs in the same ephemeral QEMU environment:

```sh
make l4s-mininet-benchmark-check
```

It creates a `client -- s1 -- server` Mininet topology. The Open vSwitch in the
middle applies HTB at 20 Mbit/s and the kernel's real DualPI2 qdisc in both
directions. For each requested classic TCP background rate (0, 5, 10, and 20
Mbit/s by default), the benchmark runs the same 4 MiB IMQUIC transfer five times
with Reno/Not-ECT, Reno/ECT(0), and Prague/ECT(1). The iperf3 TCP flow has ECN
disabled, and each case starts with fresh qdiscs and counters.

The 40-case Not-ECT/ECT(1) baseline and incremental 20-case ECT(0) QEMU run
passed. The earlier modes were not rerun. Every ECT(0) run contained
capture-visible ECT(0) and CE plus QUIC ACK_ECN CE feedback; ECT(0) captures
contained 8,094--9,197 ECT(0) packets and QUIC reported 124--541 CE packets.
Every Prague run contained ECT(1), CE, and ACK_ECN feedback. All Not-ECT runs
contained zero ECN feedback and every background TCP capture contained zero
ECN-marked packets.

During the exact QUIC intervals, combined client-to-server QUIC and TCP IP load
averaged 90.6--92.6% of the 20 Mbit/s bottleneck for Not-ECT Reno,
91.3--94.6% for ECT(0) Reno, and 93.0--95.9% for Prague at nonzero loads. The
20 Mbit/s background request achieved only about 14.14 Mbit/s alongside either
Reno mode and 14.34 Mbit/s alongside Prague. The resulting plot therefore
shows the shared bottleneck directly instead of summing rates measured over
different time intervals.

ECT(0) Reno averaged 17.30, 15.55, 15.85, and 16.78 Mbit/s QUIC goodput at the
0, 5, 10, and 20 Mbit/s background targets. Prague averaged 18.34, 13.59,
13.72, and 14.39 Mbit/s. Prague's lower goodput is therefore limited to the
loaded cases and is not an ECN-bit overhead: ECT(0) Reno also processed CE but
received far fewer marks in the classic queue. Prague's scalable CE response
kept a smaller final cwnd and a much lower final smoothed RTT. These are
descriptive results from short transfers, not a formal significance claim; the
ECT(0) mode was collected later rather than interleaved with the baseline.

The compact result is tracked as
`l4s/qemu-evidence/mininet-benchmark-analysis.json`, the raw and aggregate CSVs,
and `l4s/qemu-evidence/mininet-benchmark-comparison.svg`. Full per-case metrics,
iperf3 JSON, qdisc counters, endpoint logs, and pcaps remain under the ignored
`results/l4s/qemu-mininet-benchmark-<timestamp>/` directory. See
`l4s/mininet-benchmark.md` for the exact observed table and tunable command.
The comparison now includes inferred forward drops for QUIC and background TCP:
QUIC uses the packet-count deficit across the two switch captures, while TCP
uses retransmissions because ingress GSO prevents direct packet-count matching.
The forward DualPI2 drop counter is retained in the CSV and JSON as an
attribution cross-check.

This adds **live L4S/classic coexistence evidence**. It verifies traffic
classification and marking behavior under several offered classic loads; it
does not claim statistically significant throughput superiority from five
repetitions.

## Remaining work

None for the requested single-profile live trajectory. Profile comparisons can
be added later as a separate experiment matrix.

## Scientific and standards basis

The experimental interpretation should follow the L4S architecture and ECN protocol in RFC 9330 and RFC 9331, the DualQ AQM requirements in RFC 9332, and QUIC transport and recovery behavior in RFC 9000 and RFC 9002. Prague estimator choices should be discussed as implementation behavior, informed by Briscoe and De Schepper's 2019 scaling analysis and the DCTCP estimator background, rather than presented as standardized parameter values.
