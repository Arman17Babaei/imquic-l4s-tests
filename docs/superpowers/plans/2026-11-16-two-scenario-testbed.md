# Two-scenario Testbed Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Keep ideal delivery and a two-switch QEMU scene path with unlimited Cubic and Prague TCP competition.

**Architecture:** `player_topology.py` is the single topology implementation. The suite emits configured standalone copies; existing capture, reference and analysis pipelines remain.

**Tech Stack:** Python, Mininet/OVS, Linux tc/DualPI2, iperf3, QEMU, existing IMQUIC and browser player.

**Spec:** `docs/superpowers/specs/2026-11-16-two-scenario-testbed-design.md` (approved).

## Global Constraints

- Exactly two switches; both TCP backgrounds share s1 → s2 with the scene.
- One unlimited data stream per TCP background; no rate or parallel-flow options.
- Separate propagation from dropping AQM; defaults target 15ms/update 16ms/step 1ms/coupling 1.
- Preserve experiment evidence and submodules; commit locally as requested in the follow-up, without pushing.
- Execute in this checkout on a local branch to retain existing dependencies and results.

## Review Focus

- Guest without TCP Prague must fail explicitly; never substitute Cubic.
- Socket control connections alone must not satisfy background readiness.
- Startup errors must stop existing processes and preserve diagnostics.
- Invalid or removed config keys must fail before launching experiments.
- Old retained ledgers must retain their original labels and grouping in offline analysis.

### Task 1: Shared topology

Files: `tools/l4s/player_topology.py`, `tools/l4s/experiment_suite_topology.py`, `tests/test_experiment_suite_topology.py`.
Interface: `topology_source(bandwidth_mbps, delay_ms, *, dualpi2_target_ms=15, dualpi2_update_ms=16, dualpi2_step_ms=1, dualpi2_coupling_factor=1, background_warmup_seconds=4, performance_mode=False) -> str`.

- [x] Write failing tests for exact graph, bottleneck, delay, mixed unlimited commands, ECN, readiness and cleanup.
- [x] Inspect guest TCP Prague requirements and implement one reusable standalone topology.
- [x] Verify focused topology tests and a short guest smoke when prerequisites are available.

### Task 2: Configuration and analysis integration

Files: `experiment_suite_config.py`, `run_experiments.py`, `experiment_suite_analysis.py` and their tests.

- [x] Replace obsolete configurable switch/background variants with fixed graph and numeric DualPI2 parameters; tests reject obsolete keys, nonfinite values and invalid ranges.
- [x] Emit topology copies from the maintained source, record profile and use it to distinguish analysis groups.
- [x] Test exact QEMU/ideal commands, default Prague scene controller and legacy analysis provenance.
- [x] Run `make experiment-suite-check` and example dry run.

### Task 3: Prune and document

Files: Makefile, README, docs/player-experiments.md, experiments/player.toml; remove thesis/historical topology modules/tests and sustained-profile launcher/bundle.

- [x] Remove superseded code and update references without changing saved results.
- [x] Update direct workflow tests and documentation for two scenarios and the shared bottleneck.
- [x] Run `make cpu-check`, deletion reference audit, `git diff --check` and short scene QEMU capture if feasible.
- [x] Review final diff and report runtime evidence separately from CPU checks.

## Execution evidence

See `results/two-scenario-validation/README.md`. CPU/suite/full Python checks and
live shared topology passed. Traced Train video and guest transport audit passed;
full-scene software reference timed out, so its quality and overall run remain FAIL.
Independent review found no Critical/Important issues. Documentation corrections
were applied. The user subsequently requested a local commit; no push was requested.
