# Five-worker Train experiment implementation plan

Goal: run one new Train Prague/BBR comparison, five repeats/controller, on
live4s.arman-babaei.ir using five disjoint resource-pinned QEMU workers.

Architecture: independently prepare a fresh upstream Ubuntu24 cloud guest with
the staged L4S6.12 kernel and needed topology tools; export Train from the
server-downloaded official PLY; build the pinned adapter and transport. Wire
optional host resource controls into the existing QEMU/capture launchers. Run
five simultaneous real captures per wave, then retain arrival/priority/native
queue evidence. Offline rendering/scoring stays outside capture.

Parameters inherited from current comparison: 150Mbps, 20ms base RTT, both
TCP BBR(classic ECN) and Prague backgrounds; scene Prague ECT1 and BBR ECT0;
20second replacement user8 Train trace; 100ms priority updates. Five workers,
each4vCPU/8GiB locked preallocated memory. CPUsets2-5/6-9 onNUMA0 and
12-15/16-19/20-23 onNUMA1. Host-service affinity is not globally changed.
New guest/kernel/server profile, never pooled with previous 5.15 laptop pilots.

- [ ] Add and test opt-in resource controls in existing launchers. Worker owns
      run_qemu_player.py/run_network_capture.py plus their focused tests.
- [ ] Parent downloads fresh guest image on server, creates secure ephemeral
      guest credentials/seed, installs staged L4S kernel and verifies topology
      kernel/qdisc/TCP background support. No host kernel change/reboot.
- [ ] Parent installs pinned Node/build requirements, exports matching Train
      payloads remotely, and stages exact replacement trace with hashes.
- [ ] Integrate and publish launcher changes, update server checkout and verify.
- [ ] Start persistent five-worker batch, five repeats each for Prague/BBR;
      inspect actual overlap and tick/queue/background health. Preserve failures.
- [ ] Validate received journals, report capture/quality/concurrency separately;
      do not claim offline quality before rendering/scoring completes.
