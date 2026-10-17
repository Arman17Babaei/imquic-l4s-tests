# Experiments that could enter the thesis

This inventory separates results suitable for the main argument from results
that need a narrower role or another run. A completed runner or successful
analyzer alone does not make a scientific claim. Keep claims within the
recorded topology, controller choices, input workload, and repetitions.

## Strong main-text candidates

| Study | Evidence and possible use | Limits to state |
| --- | --- | --- |
| Prague queue delay and coexistence, Figures 6–8 | The promoted 24-case record has 12 matched Reno/Not-ECT versus Prague/ECT(1) pairs, three repetitions at each of 0, 1, 2, and 4 independent Cubic flows, and all method and support gates pass. With one Cubic flow, aggregate p95/p99 direct bottleneck residence was 21.47/49.17 ms for Reno and 1.89/2.27 ms for Prague. | One 20 Mbit/s HTB/DualPI2 QEMU/Mininet setup with the recorded 15/16/1 ms DualPI2 profile. Queue delay is matched same-clock packet residence, not an RTT estimate. See `results/records/qemu-thesis-network-figures-06-08-final-20260825T161734Z/README.md` and the Figures 6–10 verification summary. |
| 3DGS delivery tails and quality recovery, Figures 9–10 | Three matched, frozen cyclic 50-degree bicycle-trace comparisons have valid eligibility, admission, capture, provenance, delivery, and 465-frame rendering checks. Across the fixed 45-second horizons, L4S completed 7,361 objects versus 5,188 for Classic; aggregate completion-only p95/p99 were 36.91/40.11 s versus 38.42/41.60 s. Mean SSIM over the full post-wrap recovery window was 0.6720 versus 0.5783. | Completion-only medians are affected by censoring because L4S completes more late-eligible objects. Rep 1 had a user-accepted p99 exception. L4S did not win every frame; include the initial ramp-up and cumulative-quality crossover caveat. See `results/records/qemu-thesis-3dgs-figures-09-10-final-20260825/README.md`. |
| Two-switch reordering and rendering, one L4S then two L4S switches | The promoted paired-input record reports all 21,726 downstream-observed concrete provider inversions preserved by the second DualPI2 switch and mean trace-timed SSIM 0.388862 versus 0.299237 through the Classic FIFO. It provides a focused mechanism and topology comparison. | One repetition per topology, QEMU/Mininet, 0.25 L4S payload fraction, 280 Mbit/s Reno aggregation, and a controlled frozen first-visible schedule. Three-point packet match is 0.998911. This is descriptive evidence, not a general guarantee. See both promoted reordering READMEs. |

## Useful supporting or discussion material

| Study | Thesis role | Evidence limits |
| --- | --- | --- |
| Camera-driven priority experiment | A negative result that tests whether Prague shortens accepted-update-to-delivery time or reduces stale outside-viewport delivery. Its three-repetition summary falsifies that joint hypothesis: Reno's median next-object delay was about 48 ms versus about 479 ms for Prague, while the stale-fraction medians were 0.00209 and 0.00940. | Accepted priority state does not mean QUIC can reorder bytes already committed to a stream. Keep this as a scoped counterexample and explain its admission/scheduling contract. See `results/records/camera-priority-qemu-20260924T075512Z/README.md`. |
| MoQ coexistence against TCP controllers | Exploratory evidence that Prague retained about 84% of measured traffic against each tested non-ECN background, while Linux BBRv2 took the majority share against Reno and picoquic-BBR MoQ. It can motivate the controller question. | Only one eight-second observation per matrix cell; no propagation delay; picoquic BBR foreground differs from Linux BBRv1/v2 backgrounds. Do not present as statistically generalizable. See `results/records/moq-8s-unlimited-cc-matrix-20260817/README.md`. |
| Figures 12–15: L4S spectrum, bounds, workload applicability | Figure 12 measures two repetitions at each of five payload fractions; Figures 13–14 are explicitly analytical counterfactual upper bounds; Figure 15 maps 8,169 raw events into 2,723 unique events against that bound. The figures can connect network behavior to workload state if the assumptions are made explicit. | Figure 12 does not meet the planned three repetitions. Figure 15's inferred initial backlog uses residual backlog plus byte-weighted age divided by RTT under bottleneck-rate drainage. Only 0.99% of unique events fall inside the controlled region with positive gain bound. Label Figures 13–14 analytical; they are not measured network gains. See the final Persian manifest, summary, and captions under `results/figures/thesis-figures-12-15-fa-final/`. |
| Sustained 3DGS under corrected DualPI2 profile | The corrected single-run comparisons show Prague completing sooner and with much lower RTT at 150 and 300 Mbit/s BBRv2 offered load; useful as motivation or a small supporting observation. | One repetition per load point; explicitly require repetitions before making a stable completion-time claim. Queue delay is estimated from smoothed RTT minus its minimum, not measured qdisc sojourn. See `l4s/3dgs-prague-bbr2-diagnosis.md`. |
| Reno/Cubic/BBR step-join | Three-repetition 50-second, ten-stream staircase traces can support a separate discussion of TCP flow ramp-up and controller response. | A one-repetition Reno fairness result elsewhere is exploratory. Keep step-join separate from the L4S thesis claim unless a clear research question and matched comparison are added. See `results/l4s/qemu-{reno,cubic,bbr}-step-join-*` and each `analysis.json`. |
| Prague response validation | The QEMU time-series evidence demonstrates live ECT(1), CE feedback, Prague alpha evolution, and congestion-window response. It validates that the configured controller responds to DualPI2. | This is a transport validation, not a comparative performance experiment. See `l4s/validation-report.md` and `l4s/qemu-evidence/`. |

## Keep out of results claims until repaired or completed

- The 100,000-request FETCH run is a method failure: all requests were accepted,
  but the event loop stopped making network progress. The other concurrency
  cases also did not complete the offered workload. Use only to explain a
  harness limitation, not as evidence of a capacity limit. See
  `l4s/fetch-concurrency-status.md`.
- The original one-repetition Reno fairness pilot is exploratory and cannot
  establish a fairness result.
- The corrected BBRv2 profile points have one repetition apiece; extend the
  repetitions before estimating variation.
- Older result families without a promoted record, a complete raw-data audit,
  or matched repetitions should remain exploratory until their source files,
  validity checks, and comparison are reviewed.

## Suggested thesis outline

1. Establish the controlled queue-delay and coexistence result (Figures 6–8).
2. Test the same transport distinction against a trace-driven 3DGS workload
   (Figures 9–10), preserving censoring, ramp-up, and frame-level caveats.
3. Explain the packet-order mechanism with the single-versus-two-switch result.
4. Use the camera-priority falsification and analytical applicability bounds
   to show where the benefit does not follow automatically.
5. Put one-run coexistence matrices, profile diagnosis, implementation
   validation, and method failures in supporting material or limitations.
