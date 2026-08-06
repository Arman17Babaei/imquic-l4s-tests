# IMQUIC L4S tests

This repository separates the L4S/Prague experiment architecture and evidence
from the IMQUIC implementation. The implementation dependencies are pinned as
submodules:

- `deps/imquic` — `imquic-l4s-prague` IMQUIC development branch
- `deps/picoquic` — `prague-params` managed picoquic development branch

The top-level picoquic submodule is authoritative; the nested picoquic entry
inside the IMQUIC submodule is intentionally not initialized.

The test repository owns the Mininet/DualPI2 harnesses under `tools/l4s/`, the
test fixture under `tests/`, and reproducible reports and summaries under
`l4s/`. Large captures and live run directories belong under the ignored
`results/` directory.

## Bootstrap

```sh
make init
make analyzer-check
make build
```

`make build` builds picoquic first, then configures IMQUIC against the top-level
`deps/picoquic` submodule. The Mininet runner expects the resulting
`deps/imquic/src/imquic-l4s-test` binary and requires root, Mininet, DualPI2,
tcpdump, and tshark. Run it with:

```sh
sudo python3 tools/l4s/run_mininet_benchmark.py \
  --output results/l4s/mininet-run
```

The QEMU wrapper remains available in `tools/l4s/run_qemu_timeseries_test.py`;
its guest provisioning contract is documented in `l4s/validation-report.md`.

## Evidence

The tracked comparison is [the Mininet report](l4s/mininet-benchmark.md) and
[the comparison plot](l4s/qemu-evidence/mininet-benchmark-comparison.svg).
QUIC forward drops are inferred from the client/server capture deficit, while
background TCP drops are inferred from retransmissions because ingress GSO
coalesces TCP packets. The analyzer also records the forward DualPI2 drop
counter as an attribution cross-check.

The submodule commits are intentionally recorded in the superproject. Update
them only when an experiment revision is being changed and record the resulting
commit in the validation report. These development branches are currently local
fork branches; publish the branches on GitHub before cloning this repository on
another machine.
