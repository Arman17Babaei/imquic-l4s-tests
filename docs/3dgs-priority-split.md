# 3DGS priority split: Prague for important objects, Reno for the remainder

This experiment keeps the existing IMQUIC fixture unchanged and runs two independent
QUIC/MoQ connections concurrently through the same DualPI2 bottleneck:

- the higher-ranked half of 3DGS objects uses Prague with ECT(1), port 4444;
- the lower-ranked half uses Reno with Not-ECT, port 4443.

The split is by **whole transport object**, not by individual Gaussian. Each object's
original bytes are copied unchanged into exactly one output bundle. Object boundaries,
embedded identity, and source order within each path are therefore preserved. This
changes the transport path assignment without introducing a second application-ordering
change.

## Importance rule

MoQSplat, Artioli et al., *MoQSplat: Adaptive Progressive Streaming of 3D Gaussian
Splatting over MoQ*, IEEE MMSP 2026, constructs progressive-quality Subgroups and
compares opacity-based and scale-based pruning strategies. The pinned `3dgs_over_moq`
preprocessing already materializes that progressive ordering in `subgroup_id`: subgroup
0 is the coarse/high-contribution skeleton, subgroup 1 is enhancement, and subgroup 2
is fine detail. Its WFPS/scale path uses 5%/15%/80% progressive tiers; its opacity path
sorts Gaussians by descending opacity into progressive tiers.

For that reason the experiment defaults to `native-tier`, not to a new global opacity
sort. It ranks subgroup 0 before subgroup 1 before subgroup 2 and uses mean opacity only
as a deterministic tie-break inside the same tier when opacity is carried by the object.
This preserves the importance decision made when the source cache was prepared.

Three explicit ablations remain available:

- `opacity`: mean encoded opacity logit per object;
- `scale`: mean maximum encoded log-scale axis per object;
- `opacity-scale`: equal-weight percentile-rank fusion of those two object statistics.

The parser follows the actual `3dgs_over_moq` binary protocol: each of means, opacities,
SH coefficients, scales, and rotations is preceded by a little-endian uint32 byte
length, and zero-length optional arrays are valid. Opacity is stored pre-sigmoid and
scale is stored in log-space in the pinned preprocessing code; the ablation modes use
those encoded values only as monotone ordering statistics and do not modify payloads.

This is still an **object-level approximation** of splat prioritization. It deliberately
avoids re-chunking because changing object boundaries would also change MoQ stream
behavior and confound the transport comparison.

References:

- E. Artioli, M. Ghafari, M. T. Islam, F. Tashtarian, C. Rothenberg, and C. Timmerer,
  “MoQSplat: Adaptive Progressive Streaming of 3D Gaussian Splatting over MoQ,” IEEE
  28th International Workshop on Multimedia Signal Processing, 2026.
- B. Kerbl, G. Kopanas, T. Leimkühler, and G. Drettakis, “3D Gaussian Splatting for
  Real-Time Radiance Field Rendering,” ACM Transactions on Graphics 42(4), 2023,
  DOI 10.1145/3592433.
- Z. Gurel, T. E. Civelek, A. Bodur, S. Bilgin, D. Yeniceri, and A. C. Begen,
  “Media over QUIC: Initial Testing, Findings and Results,” ACM MMSys 2023,
  DOI 10.1145/3587819.3593937.

## Run

Build the existing fixture first:

```sh
make build-3dgs-fixture-only
```

Then run the priority experiment using a scene bundle already prepared by the existing
3DGS workflow:

```sh
sudo python3 tools/l4s/run_3dgs_priority_split.py \
  --output results/l4s/3dgs-priority-split \
  --source-bundle /path/to/scene.bundle \
  --importance native-tier \
  --deadline-ms 30000 \
  --repetitions 3
```

The default network profile matches the branch's corrected 300 Mbit/s DualPI2 profile:
512 KiB HTB burst/cburst, 15 ms Classic target, 16 ms update interval, and 1 ms L4S
step threshold. These remain command-line options and are retained in provenance.

The two subscriber processes cannot be started in the same CPU instruction. Launch
order therefore alternates across repetitions, and the endpoint-reported wall-clock
start timestamps are checked after each case. By default, a repetition fails if their
start skew exceeds 50 ms (`--max-start-skew-ms`). The measured skew is retained in
`result.json` rather than silently treated as simultaneous.

## Split balance

The user-requested split is exactly by **object count**: the best `floor(N/2)` objects
are assigned to Prague and the remainder to Reno. Equal object counts do not imply equal
bytes or equal numbers of Gaussians because object sizes vary. `priority-manifest.json`
therefore records, for each half:

- object count;
- payload bytes;
- Gaussian count;
- object and Gaussian counts per subgroup;
- high-half object, byte, and Gaussian fractions.

This makes any byte/splat imbalance visible instead of accidentally describing the test
as a 50/50 traffic-volume split.

## Artifacts and timeline

The run root contains:

- `inputs/high-priority.bundle`;
- `inputs/low-priority.bundle`;
- `inputs/priority-manifest.json`, mapping every source object to its rank, score,
  subgroup, source index, and assigned connection;
- `provenance.json` and `summary.json`.

Each repetition contains independent `high-prague/` and `low-reno/` endpoint results,
transport metrics, received bundles, and per-object arrival timelines. It also writes
`combined-arrival-timeline.csv` in wall-clock order across both connections. The
combined timeline resolves each arrival using the embedded `(track, group, subgroup,
object)` identity and records the original `source_record_index` and `importance_rank`;
no assumption is made that arrival order equals source order across MoQ subgroup streams.

The combined timeline plus the two received bundles is sufficient to reconstruct the
union of available 3DGS objects at any presentation timestamp for later frame rendering
and VMAF/PSNR/SSIM analysis. Rendering remains outside the network experiment.
