# Bursty Dual-Queue Video-Split Experiment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run a reproducible QEMU experiment that compares five importance-ranked Prague/Reno video splits while independent 7--13 ms Reno and Prague background bursts load both DualPI2 bottlenecks.

**Architecture:** Add a deterministic burst-schedule module and an optional scheduled mode to the existing sustained IMQUIC fixture, then integrate four persistent background connections into the current two-switch 3DGS runner without changing its legacy path. Capture both sides of each shaped egress with the QEMU guest clock, analyze class-specific residence and validity gates offline, and use a dedicated QEMU wrapper for the pilot and final matrix.

**Tech Stack:** Python 3, `unittest`, Mininet, Linux `tc`/DualPI2, QEMU, IMQUIC C/GLib fixture, AF_PACKET compact capture, JSON/CSV evidence, existing 3DGS renderer and SSIM tooling.

**Spec:** `docs/superpowers/specs/2026-09-29-bursty-dual-queue-video-split-experiment-design.md`

## Global Constraints

- Run the live network experiment only through QEMU with the pinned guest kernel; do not substitute the host kernel.
- Use two 100 Mbit/s DualPI2 bottlenecks and retain the 20 ms base video RTT.
- Use four persistent IMQUIC backgrounds: Prague/ECT(1) and Reno/Not-ECT at each bottleneck.
- Sample every ON and OFF duration independently from `{7, 8, 9, 10, 11, 12, 13}` ms with distinct recorded seeds.
- Offer 100 Mbit/s per background connection while ON; keep connections open and congestion-control state intact while OFF.
- Test video L4S shares `1.00,0.75,0.50,0.25,0.00`, with the remaining importance-ranked bytes assigned to Reno.
- Use a two-second background warm-up, a 30-second video deadline, and three repetitions after a valid one-repetition pilot.
- Generate every background schedule for the full 32-second warm-up-plus-measurement lifetime.
- Keep packet captures and live runs under ignored `results/`; promote only validated evidence.
- Preserve current CLI defaults and existing experiment behavior unless bursty mode is explicitly selected.
- Do not initialize IMQUIC's nested picoquic dependency or modify unrelated dirty submodules/worktree files.

## Review Focus

- A schedule ending mid-interval must be clipped exactly to the configured duration without emitting a zero-length interval; Task 1 tests this boundary.
- Different seeds may coincidentally produce one equal duration, but complete per-flow transition sequences must not be identical; Task 1 and Task 5 test the correct distinction.
- An OFF transition must stop new application admission without discarding queued or in-flight transport data; Task 2 tests this observable behavior.
- Packets whose ECN field changes from ECT(1) to CE must still match across a bottleneck; Task 4 excludes ECN from identity and tests the transition.
- Endpoint video cells with one empty media path must remain valid while mixed cells require evidence from both media paths; Task 5 tests endpoint and mixed-cell gates separately.

---

### Task 1: Deterministic independent burst schedules

**Files:**
- Create: `tools/l4s/bursty_background.py`
- Create: `tests/test_bursty_background.py`

**Interfaces:**
- Consumes: experiment duration, integer seed, ON rate, and interval bounds from the approved spec.
- Produces: `BurstInterval`, `BurstSchedule`, `generate_schedule(...)`, `write_schedule(...)`, `read_schedule(...)`, and `validate_schedule_set(...)` for the runner, fixture command builder, and analyzer.

- [ ] **Step 1: Write failing schedule-generation tests**

Add tests named:

```python
def test_generate_schedule_is_replayable_and_clips_final_interval(): ...
def test_intervals_are_alternating_and_between_7_and_13_ms(): ...
def test_distinct_seeds_produce_nonidentical_transition_sequences(): ...
def test_invalid_bounds_rate_seed_or_duration_are_rejected(): ...
def test_schedule_json_and_csv_round_trip_exactly(): ...
```

Use literal assertions for a fixed seed, require total coverage to equal the requested duration in microseconds, and require the last interval to be positive even when clipped.

- [ ] **Step 2: Run tests and confirm the expected import failure**

Run: `python3 -m unittest tests.test_bursty_background -v`

Expected: FAIL because `tools.l4s.bursty_background` does not exist.

- [ ] **Step 3: Implement the schedule model and serializers**

In `tools/l4s/bursty_background.py`, define:

```python
@dataclass(frozen=True)
class BurstInterval:
    state: Literal["on", "off"]
    start_us: int
    end_us: int
    byte_budget: int

@dataclass(frozen=True)
class BurstSchedule:
    seed: int
    on_rate_mbps: float
    duration_us: int
    intervals: tuple[BurstInterval, ...]

def generate_schedule(*, seed: int, duration_us: int,
                      on_rate_mbps: float = 100.0,
                      minimum_ms: int = 7,
                      maximum_ms: int = 13) -> BurstSchedule: ...
def write_schedule(schedule: BurstSchedule, json_path: Path, csv_path: Path) -> None: ...
def read_schedule(json_path: Path) -> BurstSchedule: ...
def validate_schedule_set(schedules: Mapping[str, BurstSchedule]) -> list[str]: ...
```

Use `random.Random(seed)`, independently sample the initial ON/OFF phase and first duration, then alternate states. Compute each ON byte budget from its exact duration and 100 Mbit/s rate, and clip only the final interval. Select and record four fixed seeds whose complete transition sequences differ; do not merely offset one shared schedule.

- [ ] **Step 4: Run the schedule tests**

Run: `python3 -m unittest tests.test_bursty_background -v`

Expected: PASS.

- [ ] **Step 5: Commit the schedule unit**

```bash
git add tools/l4s/bursty_background.py tests/test_bursty_background.py
git commit -m "feat: add replayable burst schedules"
```

### Task 2: Congestion-controlled scheduled IMQUIC fixture

**Files:**
- Modify: `tests/sustained-moq-test.c`
- Modify: `Makefile`
- Create: `tests/test_bursty_moq_fixture.py`

**Interfaces:**
- Consumes: Task 1's schedule CSV with `state,start_us,end_us,byte_budget`, a shared go-file path, a ready-file path, and a one-millisecond metrics interval.
- Produces: backward-compatible `imquic-sustained-moq` publisher/subscriber modes plus optional scheduled publisher evidence: transition CSV, offered-byte count, connection lifetime, and transport metrics.

- [ ] **Step 1: Write failing fixture CLI and schedule tests**

Add tests that invoke a new parser/self-test mode and assert:

```python
def test_fixture_accepts_legacy_publisher_arguments(): ...
def test_fixture_rejects_malformed_or_nonalternating_schedule(): ...
def test_off_interval_admits_no_new_objects_but_preserves_connection(): ...
def test_cumulative_on_admission_tracks_budget_with_less_than_one_object_deficit(): ...
def test_metrics_interval_accepts_1000_microseconds(): ...
```

The OFF assertion must use fixture-produced transition/offered-byte evidence, not source-text inspection.

- [ ] **Step 2: Run the fixture tests and verify they fail for missing scheduled mode**

Run: `python3 -m unittest tests.test_bursty_moq_fixture -v`

Expected: FAIL because the fixture has no schedule/gate options.

- [ ] **Step 3: Extend the C fixture without changing legacy behavior**

Add optional publisher arguments:

```text
--schedule CSV --ready-file PATH --go-file PATH
--transition-log CSV --summary-json PATH --metrics-interval-us 1000
```

Parse the schedule before starting the endpoint, write readiness only after the connection can publish, wait on the common go file, and measure schedule time with `g_get_monotonic_time()`. During OFF intervals, admit no new objects. During ON intervals, admit whole 16 KiB objects only while the existing congestion-window/transport-queue gate allows it. Carry each sub-object byte remainder into the next ON interval so cumulative admission never exceeds the cumulative scheduled byte budget and trails it by less than one object when transport capacity permits. Do not clear data already queued or in flight at an OFF boundary.

- [ ] **Step 4: Add the fixture build/check target**

Add `l4s-bursty-moq-fixture-check` to `Makefile`; copy the tracked C fixture into `deps/imquic/src`, build `imquic-sustained-moq`, and run its schedule self-test without Mininet.

- [ ] **Step 5: Run fixture and legacy checks**

Run:

```bash
python3 -m unittest tests.test_bursty_moq_fixture -v
make l4s-bursty-moq-fixture-check
make analyzer-check
```

Expected: all PASS; existing sustained fixture invocation remains accepted.

- [ ] **Step 6: Commit the fixture change**

```bash
git add tests/sustained-moq-test.c tests/test_bursty_moq_fixture.py Makefile
git commit -m "feat: add burst gating to sustained MoQ fixture"
```

### Task 3: Optional four-flow background mode in the 3DGS runner

**Files:**
- Modify: `tools/l4s/run_3dgs_reordering.py`
- Create: `tests/test_bursty_3dgs_runner.py`

**Interfaces:**
- Consumes: Task 1 schedule APIs and Task 2 fixture CLI.
- Produces: `--bursty-dual-queue-background`, four persistent background process groups, per-case schedule/evidence directories, and provenance fields used by Task 5.

- [ ] **Step 1: Write failing runner configuration tests**

Add tests for:

```python
def test_bursty_mode_requires_two_switch_dualpi2_and_disables_legacy_iperf(): ...
def test_bursty_mode_uses_100mbit_on_both_shaped_egresses(): ...
def test_background_pair_one_crosses_s1_egress_only(): ...
def test_background_pair_two_joins_after_s1_and_crosses_client_facing_s2_egress(): ...
def test_four_commands_use_reno_and_prague_with_distinct_ports_and_seeds(): ...
def test_case_order_is_counterbalanced_for_three_repetitions(): ...
```

Use small fake Mininet nodes/processes; do not require root or launch Mininet.

- [ ] **Step 2: Run the runner tests and confirm bursty mode is absent**

Run: `python3 -m unittest tests.test_bursty_3dgs_runner -v`

Expected: FAIL for missing CLI/configuration helpers.

- [ ] **Step 3: Add bursty-mode arguments and validation**

Add:

```text
--bursty-dual-queue-background
--background-on-rate-mbps 100
--background-interval-min-ms 7
--background-interval-max-ms 13
--background-seed-base INTEGER
--background-metrics-interval-us 1000
```

When enabled, require `two-switch`, `downstream-mode=dualpi2`, both shaped rates `100mbit`, and legacy `--dc-background-mbps 0`. Preserve every existing default outside this mode.

- [ ] **Step 4: Add the two local background pairs**

Create one source/sink host pair spanning the `s1` shaped egress and one source attached after `s1` whose two subscribers run on the existing client, thereby sharing the shaped `s2` receiver-facing egress. Use separate Reno and Prague ports on each pair. Generate four 32-second schedules per case, start all endpoints, wait for all ready files, create one go file, warm up for two seconds, and then release the video gate for the 30-second deadline.

- [ ] **Step 5: Record case and run provenance**

Record topology/interface ownership, ports, modes, ECN expectations, schedule hashes/seeds, ON rate, interval bounds, warm-up, process lifetimes, offered/delivered bytes, qdisc state, and exact case order. A background process ending before the video deadline fails the case.

- [ ] **Step 6: Run runner and existing semantics tests**

Run:

```bash
python3 -m unittest tests.test_bursty_3dgs_runner tests.test_3dgs_reordering_semantics -v
python3 -m compileall -q tools/l4s/run_3dgs_reordering.py tools/l4s/bursty_background.py
```

Expected: PASS.

- [ ] **Step 7: Commit the runner integration**

```bash
git add tools/l4s/run_3dgs_reordering.py tests/test_bursty_3dgs_runner.py
git commit -m "feat: load both DualPI2 bottlenecks with bursty backgrounds"
```

### Task 4: Compact same-clock residence capture

**Files:**
- Create: `tools/l4s/capture_queue_residence.py`
- Create: `tools/l4s/queue_residence.py`
- Create: `tests/test_queue_residence.py`
- Modify: `tools/l4s/run_3dgs_reordering.py`

**Interfaces:**
- Consumes: UDP packets from video and the four background connections at all ingress interfaces and both shaped egresses.
- Produces: compact packet rows, capture stats, per-packet matched residence records, one-millisecond class time series, and match-coverage statistics for Task 5.

- [ ] **Step 1: Write failing packet identity and matching tests**

Add tests using literal synthetic IPv4/UDP frames:

```python
def test_identity_survives_ect1_to_ce_marking(): ...
def test_repeated_packet_identities_match_fifo_with_deques(): ...
def test_s1_matcher_accepts_packets_from_video_and_local_background_ingresses(): ...
def test_s2_matcher_accepts_interswitch_and_local_background_ingresses(): ...
def test_one_millisecond_bins_separate_ect1_ce_and_not_ect(): ...
def test_negative_residence_and_low_coverage_fail_validation(): ...
```

- [ ] **Step 2: Run tests and verify missing capture/analyzer modules**

Run: `python3 -m unittest tests.test_queue_residence -v`

Expected: FAIL because the modules do not exist.

- [ ] **Step 3: Implement compact capture**

In `capture_queue_residence.py`, use AF_PACKET and retain packets whose source or destination port is one of the six experiment UDP service ports. Write timestamp, immutable IPv4/UDP identity fields, ECN, length, interface, and direction. Include the UDP checksum after disabling checksum offload, but exclude ECN and the mutable IPv4 checksum from identity so CE-marked packets match their ECT(1) ingress copy. Write socket-drop and row-count stats on shutdown.

- [ ] **Step 4: Implement offline residence matching**

In `queue_residence.py`, define:

```python
def packet_identity(row: Mapping[str, str]) -> tuple[str, ...]: ...
def match_residence(ingress_rows: Iterable[PacketRow],
                    egress_rows: Iterable[PacketRow]) -> ResidenceResult: ...
def bin_residence(rows: Iterable[ResidenceRow], bin_us: int = 1000) -> list[dict]: ...
```

Match repeated identities FIFO with deques, classify CE as originally L4S when the matched ingress was ECT(1), and calculate coverage against the larger ingress/egress count.

- [ ] **Step 5: Wire captures around both shaped egresses**

Start capture before the common background go gate and disable offloads on all captured interfaces. For each bottleneck, timestamp `PACKET_OUTGOING` on the shaped switch-side interface immediately before its egress qdisc and timestamp the same packet on the peer interface after dequeue; use the s2-side inter-switch ingress for `s1` and the client-side ingress for `s2`. Stop after the video deadline and preserve per-interface stats. Keep full pcaps optional and compact logs as the default evidence.

- [ ] **Step 6: Run capture, analyzer, and legacy packet tests**

Run:

```bash
python3 -m unittest tests.test_queue_residence tests.test_3dgs_reordering -v
python3 -m compileall -q tools/l4s/capture_queue_residence.py tools/l4s/queue_residence.py
```

Expected: PASS.

- [ ] **Step 7: Commit the capture unit**

```bash
git add tools/l4s/capture_queue_residence.py tools/l4s/queue_residence.py \
  tools/l4s/run_3dgs_reordering.py tests/test_queue_residence.py
git commit -m "feat: capture per-class queue residence at both switches"
```

### Task 5: Scientific validity gates and matrix aggregation

**Files:**
- Create: `tools/l4s/analyze_bursty_video_split.py`
- Create: `tests/test_analyze_bursty_video_split.py`

**Interfaces:**
- Consumes: schedule metadata, fixture summaries, compact residence logs, `tc` snapshots, spectrum manifests, video result JSON, and transport metrics from Tasks 1--4.
- Produces: per-case `bursty-analysis.json`, queue CSVs, root `controlled-summary.csv`, `validation-summary.json`, and an exit status that blocks invalid continuation.

- [ ] **Step 1: Write failing gate and aggregation tests**

Cover these named behaviors:

```python
def test_valid_case_requires_dualpi2_100mbit_at_both_egresses(): ...
def test_background_ecn_modes_and_four_live_connections_are_required(): ...
def test_offered_load_balance_allows_5_percent_inclusive_boundary(): ...
def test_nonidentical_schedules_pass_even_if_one_transition_coincides(): ...
def test_each_class_and_switch_requires_repeated_rise_and_drain_cycles(): ...
def test_endpoint_video_split_allows_one_empty_media_path(): ...
def test_mixed_video_split_requires_both_media_paths(): ...
def test_missing_objects_remain_in_fixed_completion_denominator(): ...
def test_three_repetitions_aggregate_as_median_and_min_max(): ...
def test_invalid_case_is_excluded_and_labelled_method_failure(): ...
```

- [ ] **Step 2: Run analyzer tests and confirm the module is missing**

Run: `python3 -m unittest tests.test_analyze_bursty_video_split -v`

Expected: FAIL because the analyzer does not exist.

- [ ] **Step 3: Implement per-case analysis**

Define `analyze_case(case: Path) -> dict[str, object]` to verify all ten spec gates, write per-switch/per-class one-millisecond CSVs, calculate queue mean/p95/p99/standard-deviation/peak-to-peak values, and preserve hypothesis outcomes separately from method validity.

- [ ] **Step 4: Implement root aggregation and CLI gates**

Define `aggregate(root: Path) -> dict[str, object]` and CLI options `--pilot` and `--require-repetitions`. Pilot mode requires one valid repetition for all five shares. Full mode requires three. Emit `supports`, `falsifies`, `inconclusive`, or `method-failure`; exit nonzero for missing/invalid cells and return success for scientifically negative but method-valid evidence while printing an explicit pause message.

- [ ] **Step 5: Run analyzer tests**

Run: `python3 -m unittest tests.test_analyze_bursty_video_split -v`

Expected: PASS.

- [ ] **Step 6: Commit the analyzer**

```bash
git add tools/l4s/analyze_bursty_video_split.py tests/test_analyze_bursty_video_split.py
git commit -m "feat: validate bursty video split evidence"
```

### Task 6: Dedicated QEMU and Make entrypoints

**Files:**
- Create: `tools/l4s/run_qemu_bursty_video_split.py`
- Create: `tests/test_qemu_bursty_video_split.py`
- Modify: `Makefile`

**Interfaces:**
- Consumes: existing `run_qemu_timeseries_test.py`, the scheduled 3DGS fixture, and the bursty runner/analyzer modes from Tasks 2--5.
- Produces: an explicit one-repetition pilot command and a pilot-resuming completion command whose results return under `results/l4s/` with complete provenance.

- [ ] **Step 1: Write failing QEMU command-construction tests**

Assert that the wrapper:

```python
def test_pilot_command_uses_one_repetition_and_all_five_splits(): ...
def test_completion_command_reuses_pilot_and_adds_two_repetitions(): ...
def test_command_pins_two_dualpi2_100mbit_bottlenecks_and_disables_iperf(): ...
def test_command_forwards_source_and_frozen_demand_as_guest_files(): ...
def test_dirty_checkout_requires_explicit_allow_dirty(): ...
```

- [ ] **Step 2: Run tests and verify the wrapper is missing**

Run: `python3 -m unittest tests.test_qemu_bursty_video_split -v`

Expected: FAIL because the wrapper does not exist.

- [ ] **Step 3: Implement the wrapper and Make targets**

Create wrapper modes `--pilot` and `--complete-from-pilot PATH`. Both invoke `run_qemu_timeseries_test.py`; pilot runs the first counterbalanced repetition, while completion verifies the supplied pilot's immutable parameters and schedules only repetitions two and three. Add `l4s-bursty-video-split-guest-check` and `l4s-bursty-video-split-qemu-check` targets. The guest target builds fixtures, runs the requested stage, and invokes Task 5's analyzer with the matching repetition requirement.

- [ ] **Step 4: Run wrapper and repository checks without QEMU**

Run:

```bash
python3 -m unittest tests.test_qemu_bursty_video_split -v
make analyzer-check
git diff --check
```

Expected: PASS.

- [ ] **Step 5: Commit the entrypoints**

```bash
git add tools/l4s/run_qemu_bursty_video_split.py \
  tests/test_qemu_bursty_video_split.py Makefile
git commit -m "feat: add QEMU bursty video split entrypoint"
```

### Task 7: Network plots and quality integration

**Files:**
- Create: `tools/l4s/plot_bursty_video_split.py`
- Create: `tests/test_plot_bursty_video_split.py`
- Modify: `tools/l4s/render_3dgs_reordering.py`

**Interfaces:**
- Consumes: Task 5's validated summary and existing per-case received bundles/render pipeline.
- Produces: queue-volatility, delivery, throughput, and SSIM PDF/SVG/PNG figures plus machine-readable plotting summaries.

- [ ] **Step 1: Write failing plot-data and render-selection tests**

Test that invalid cases are rejected; five shares remain ordered `100,75,50,25,0`; queue plots separate switch and ECN class; completion uses a fixed denominator; bands are min--max around the repetition median; and rendering selects only the final valid matrix.

- [ ] **Step 2: Run tests and verify plotting support is missing**

Run: `python3 -m unittest tests.test_plot_bursty_video_split -v`

Expected: FAIL because the plot module does not exist.

- [ ] **Step 3: Implement network-only plots**

Create functions for per-switch L4S/Classic queue time series, queue-volatility summaries, video completion curves, delivery-latency distributions, and absolute per-connection throughput. Label direct packet residence as queue delay and keep analytical/RTT-derived values out of these panels.

- [ ] **Step 4: Integrate final-matrix rendering and SSIM**

Reuse the existing renderer and reference frames after the network matrix passes. Produce per-repetition SSIM time series, fixed post-viewpoint-change recovery summaries, and median/min--max aggregation across three repetitions. Do not render pilot cells that will be replaced.

- [ ] **Step 5: Run plot and rendering unit checks**

Run:

```bash
python3 -m unittest tests.test_plot_bursty_video_split -v
python3 -m compileall -q tools/l4s/plot_bursty_video_split.py \
  tools/l4s/render_3dgs_reordering.py
```

Expected: PASS.

- [ ] **Step 6: Commit plotting support**

```bash
git add tools/l4s/plot_bursty_video_split.py tests/test_plot_bursty_video_split.py \
  tools/l4s/render_3dgs_reordering.py
git commit -m "feat: plot bursty video split outcomes"
```

### Task 8: Offline verification and one-repetition QEMU pilot

**Files:**
- Modify only if verification exposes defects in Tasks 1--7.
- Output: ignored `results/l4s/qemu-bursty-video-split-pilot-*`

**Interfaces:**
- Consumes: all prior implementation tasks.
- Produces: either a valid five-cell pilot and an evidence-backed continuation decision, or a preserved diagnostic run and a stopped workflow.

- [ ] **Step 1: Run the complete offline verification suite**

Run:

```bash
make analyzer-check
python3 -m unittest discover -s tests -p 'test_*.py'
git diff --check
```

Expected: all relevant tests PASS. Report the known optional `torch` import failure explicitly if that environment still lacks PyTorch; do not hide any other failure.

- [ ] **Step 2: Run the QEMU pilot**

Run the dedicated wrapper with `--pilot`, the approved source bundle and frozen-demand paths, and a timestamped destination prefix. Do not use `--allow-dirty` unless the exact dirty-state manifest has been reviewed and intentionally accepted.

- [ ] **Step 3: Inspect every pilot validity gate**

Verify both qdiscs/rates, four live backgrounds, distinct schedules, <=5% offered-load imbalance, ECT(1)/Not-ECT classification, capture coverage, repeated rise/drain cycles for four switch/class combinations, five correct video split manifests, and fixed-denominator completion.

- [ ] **Step 4: Apply the stopping rule**

If any method gate fails, preserve the run, diagnose, fix with a new failing test, and rerun the full pilot. If the method is valid but the expected mixed-split benefit is falsified, stop and report the scientifically negative result before any tuning or full run. Continue only when the pilot is valid and its outcome has been explicitly reviewed.

- [ ] **Step 5: Commit any pilot-driven method fixes**

Use a scoped `fix:` commit containing the new regression test and minimal repair. Do not commit live captures or raw QEMU output.

### Task 9: Complete, render, and promote the validated matrix

**Files:**
- Modify only if a reproducible analysis/plot defect is found.
- Output: ignored full run under `results/l4s/`, promoted record under `results/records/`, and publication outputs selected afterward.

**Interfaces:**
- Consumes: a reviewed valid pilot from Task 8.
- Produces: three valid repetitions per split, final plots/SSIM, promoted result metadata, and regeneration instructions.

- [ ] **Step 1: Run the QEMU full matrix**

Invoke the dedicated wrapper with `--complete-from-pilot PATH` and the same source/frozen-demand inputs. Require the wrapper's immutable-parameter comparison to pass before it schedules the remaining two repetitions.

- [ ] **Step 2: Require the full validity gate**

Run `analyze_bursty_video_split.py --require-repetitions 3`; require five valid cells with exactly three valid repetitions each. Preserve and explain any excluded run rather than silently replacing data.

- [ ] **Step 3: Render and calculate quality**

Run the final-matrix rendering and SSIM path, then regenerate all network and quality plots from the retained evidence.

- [ ] **Step 4: Promote the result**

Use `tools/promote_result.py` with a result ID describing the bursty dual-queue video split. Write the promoted README with hypothesis, exact matrix, result classification, interpretation, caveats, evidence paths, and exact regeneration commands.

- [ ] **Step 5: Final verification**

Verify checksums, manifest completeness, plot regeneration, `make analyzer-check`, focused tests, full test discovery, and visual inspection of every final PDF before considering presentation updates.

- [ ] **Step 6: Commit only reproducible tracked artifacts**

Commit code, tests, the promoted small summaries/index update, and approved publication figures. Do not commit packet captures, VM credentials, or bulky live-run directories.
