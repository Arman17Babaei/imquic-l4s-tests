# BBR sample, scene overviews and paper integration

> **For agentic workers:** Use superpowers:executing-plans inline. Do not ask questions, commit unrelated edits, or start additional experiment batches.

**Goal:** Add BBR, run one 20 s Bicycle QEMU case detached, produce eight separate scene overview images/PDFs, and integrate all into the LIVE4S appendix with limited main-text figures and factual wording corrections only.

**Architecture:** Extend the existing mode path end-to-end (BBR/Not-ECT), retaining the two fixed TCP backgrounds. Reuse retained ideal and five-repeat QEMU evidence for the figures; keep BBR separate as a single sample. Build the paper after copying reproducible plot exports into its figure directory.

**Tech stack:** Python, C/IMQUIC/picoquic, Node bridge, systemd user services, Matplotlib, LaTeX.

**Spec:** User goal in this thread, including no questions and minimal paper wording changes.

## Constraints and review focus
- Retain other dirty work in both repositories; do not rewrite existing measurements.
- BBR sample uses 50 Mbps, 30 ms base RTT, 5 fps quality/capture and queue 16, one repeat.
- Confirm actual detached process/case progress; never equate a launched unit with a passing run.
- Separate missing snapshots, black recorded images, capture/quality validity and transport evidence.
- Label ideal one-way delay, QEMU RTT, different time origins, sums and repeat counts accurately.

## Tasks
- [x] Add BBR parsing/configuration/endpoint support; add regression tests and run focused checks/build.
- [x] Create one-case configuration, dry-run exactly one BBR Bicycle case, start persistent background service and verify live progress.
- [x] Generate 8 scene overview PNG/PDF/SVGs from retained data, with full-reference/ideal/Prague/Reno frames, 100 ms network plots and positive VMAF-loss percentiles; include completed BBR Bicycle evidence separately.
- [x] Numerically validate plotted values and source provenance, inspect every rendered overview, retain per-metric exports for LaTeX.
- [x] Add all scene figures to LIVE4S appendix; use only limited informative main-text figures. Correct only factually wrong experiment statements.
- [x] Compile and inspect paper pages, audit exact source diff and deliver artifacts plus detached-run outcome.

Ruling: Preserve the successful 100-frame BBR network capture and recover offline using the validated exact reference after launcher OOM. Use CPU SSIM/VMAF because the NVIDIA driver is unavailable; retain original FAIL and mark derived record PARTIAL. No second network sample.

Completed: exact evidence and limitations are recorded in results/scene-overviews-20261118/completion-audit.json. All eight PNG/PDF pages verified, paper builds with two main experimental figures and eight appendix pages; independent review corrections applied.
