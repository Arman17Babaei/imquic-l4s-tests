---
name: experiment-evidence-records
description: Keep IMQUIC/L4S experiments reproducible without clutter. Use when planning, running, interpreting, preserving, or preparing a networking experiment for a paper.
---

# Experiment evidence records

Keep **runs** and **results** separate.

- A run is disposable execution output. Let the existing runner write its pcaps, logs, CSVs, JSON, and plots normally.
- A result is a run worth keeping because it supports, falsifies, or meaningfully tests a research hypothesis.

Do not create a large record hierarchy around every run.

## Before a meaningful experiment

State the question and a falsifiable hypothesis before interpreting the output. Identify the baseline/control, independent variables, dependent variables, repetitions, phase timing, and evidence that would make the result inconclusive.

Inspect the real runner rather than reconstructing configuration from prose.

## During the run

The real runner should call `write_experiment_record(...)` from `tools/l4s/experiment_metadata.py` once the Mininet topology is live.

`provenance.json` is the single run-level provenance file. It should contain:

- exact argv;
- top-level Git revision and dirty state;
- recursive submodule revisions;
- OS/kernel and experiment-critical tool versions;
- intended experiment configuration;
- topology;
- live client/server/switch state.

Keep detailed `tc -s -d qdisc` and `tc -s -d class` output with the existing per-case evidence. Do not invent another storage format for packet captures or analyzer outputs.

## When a run is worth keeping

Promote it with `tools/promote_result.py`.

Promotion should **move**, not duplicate, the run into `results/records/<result-id>/` and add:

- `README.md` — hypothesis, experiment, result, interpretation, caveats, evidence, and paper notes;
- `result.json` — the small machine-readable scientific summary.

`results/records/INDEX.md` lists only promoted results. Scratch runs do not belong in it.

Do not equate analyzer success with scientific support. The result may be `supports`, `falsifies`, `inconclusive`, or `method-failure`.

## Paper preparation

Use the promoted README as the bridge to the paper. Preserve a candidate claim and explicit caveats. Keep claims scoped to the recorded topology, controller choices, repetitions, and environment unless later experiments justify generalization.

## Research basis

This workflow follows the lightweight parts of established reproducibility practice: record hypotheses and metadata, version the software, retain raw evidence, and make the exact experiment replayable without imposing an experiment database or workflow service.

Relevant scientific references:

- Bajpai et al., “The Dagstuhl Beginners Guide to Reproducibility for Experimental Networking Research,” ACM SIGCOMM CCR, 2019, DOI 10.1145/3314212.3314217.
- Eide, Stoller, and Lepreau, “An Experimentation Workbench for Replayable Networking Research,” NSDI 2007.
- Dhruv and Dubey, “Managing Software Provenance to Enhance Reproducibility in Computational Research,” Computing in Science & Engineering, 2023, DOI 10.1109/MCSE.2023.3314288.
