#!/usr/bin/env python3
"""Promote one useful run into a durable, human-readable experiment record."""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECORDS = ROOT / "results" / "records"


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def configuration_table(provenance: dict[str, Any]) -> str:
    configuration = provenance.get("configuration", {})
    if not configuration:
        return "See `provenance.json`."
    scalar = {}
    structured = {}
    for key, value in configuration.items():
        (structured if isinstance(value, (dict, list)) else scalar)[key] = value
    lines = ["| Parameter | Value |", "| --- | --- |"]
    for key, value in scalar.items():
        text = str(value).replace("|", "\\|")
        lines.append(f"| `{key}` | {text} |")
    if structured:
        lines.extend([
            "", "Structured configuration:", "", "```json",
            json.dumps(structured, indent=2, sort_keys=True), "```",
        ])
    return "\n".join(lines)


def build_readme(destination: Path, summary: dict[str, str]) -> str:
    provenance = load_json(destination / "provenance.json")
    repository = provenance.get("repository", {})
    topology = provenance.get("topology", "See provenance.json")
    if not isinstance(topology, str):
        topology = json.dumps(topology, indent=2, sort_keys=True)
    command = provenance.get("command", {}).get("shell", "See provenance.json")
    return f"""# {summary['title']}

**Result ID:** `{summary['id']}`

**Conclusion:** `{summary['conclusion']}`

**Repository:** `{repository.get('commit', 'unknown')}`

## Question

{summary['question']}

## Hypothesis

{summary['hypothesis']}

## Experiment

### Topology

```text
{topology}
```

### Configuration

{configuration_table(provenance)}

The exact command, repository/submodule revisions, OS/kernel/tool versions,
and live Mininet state are preserved in `provenance.json`. Per-case metadata,
packet captures, qdisc snapshots, CSVs, logs, analyzer output, and figures stay
in this directory exactly as produced by the runner.

### Command

```sh
{command}
```

## Results

{summary['result']}

## Interpretation

**Conclusion:** `{summary['conclusion']}`

This is the scientific interpretation of the evidence, not merely the process
exit status or analyzer acceptance result.

## Limitations and caveats

{summary['caveats']}

## Paper notes

### Candidate claim

{summary['paper_claim']}

### Do not overclaim

Keep the claim scoped to the recorded topology, controller choices,
repetitions, and environment unless later experiments justify generalization.
"""


def update_index(records: Path) -> None:
    rows = []
    for child in sorted(records.iterdir()):
        data = load_json(child / "result.json") if child.is_dir() else {}
        if data:
            rows.append(data)
    lines = [
        "# Experiment records",
        "",
        "Only promoted results appear here; scratch runs stay out of this catalog.",
        "",
        "| ID | Title | Conclusion | Repository |",
        "| --- | --- | --- | --- |",
    ]
    for data in rows:
        lines.append(
            f"| `{data['id']}` | {data['title']} | `{data['conclusion']}` | "
            f"`{data.get('repository_commit', '')[:12]}` |"
        )
    (records / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def promote(source: Path, records: Path, **summary: str) -> Path:
    source = source.resolve()
    records = records.resolve()
    destination = records / summary["id"]
    if not source.is_dir():
        raise FileNotFoundError(source)
    if not (source / "provenance.json").is_file():
        raise ValueError(f"{source}: provenance.json is required before promotion")
    if destination.exists():
        raise FileExistsError(destination)

    records.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    provenance = load_json(destination / "provenance.json")
    summary = {
        **summary,
        "repository_commit": provenance.get("repository", {}).get("commit", ""),
        "promoted_utc": datetime.now(timezone.utc).isoformat(),
    }
    (destination / "result.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (destination / "README.md").write_text(
        build_readme(destination, summary), encoding="utf-8"
    )
    update_index(records)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    for name in ("id", "title", "question", "hypothesis", "result"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument(
        "--conclusion", required=True,
        choices=("supports", "falsifies", "inconclusive", "method-failure"),
    )
    parser.add_argument("--paper-claim", default="Not yet formulated.")
    parser.add_argument("--caveats", default="None recorded yet.")
    args = parser.parse_args()
    destination = promote(
        args.source,
        args.records,
        id=args.id,
        title=args.title,
        question=args.question,
        hypothesis=args.hypothesis,
        result=args.result,
        conclusion=args.conclusion,
        paper_claim=args.paper_claim,
        caveats=args.caveats,
    )
    print(destination)


if __name__ == "__main__":
    main()
