#!/usr/bin/env python3
"""Print the key packet-level post-send reordering metrics for 3DGS runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _pct(value):
    if value is None:
        return "n/a"
    return f"{100.0 * float(value):.2f}%"


def _cases(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("cases"), list):
        return data["cases"]
    return [data]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "analysis",
        type=Path,
        help="root reordering-analysis.json or a single case reordering-analysis.json",
    )
    args = parser.parse_args()

    for case in _cases(args.analysis):
        counts = case["capture_counts"]
        opportunity = case["provider_opportunity"]
        reorder = case["provider_reordering"]
        persistence = case["downstream_persistence"]
        acceptance = case.get("acceptance", {})

        print(f"case: {case.get('case', '<unknown>')}")
        print(f"measurement: {acceptance.get('status', 'unknown')}")
        print(f"outcome: {acceptance.get('hypothesis_outcome', 'unknown')}")
        print(
            "L4S/Prague packets that overtook Classic packets: "
            f"{reorder['prague_packets_with_overtake']} / "
            f"{counts['prague_provider_matched_packets']} "
            f"({_pct(reorder['fraction_prague_packets_with_overtake'])})"
        )
        print(
            "Classic/Reno packets overtaken at least once: "
            f"{reorder['unique_reno_packets_overtaken']}"
        )
        print(
            "Classic/Reno payload bytes overtaken: "
            f"{reorder['unique_reno_payload_bytes_overtaken']} "
            f"({_pct(reorder['fraction_reno_payload_bytes_overtaken'])} of matched Classic bytes)"
        )
        print(
            "L4S/Classic overlap opportunities: "
            f"{opportunity['opportunity_pairs']} packet pairs"
        )
        print(
            "successful inversions / opportunities: "
            f"{reorder['inversion_pairs']} / {opportunity['opportunity_pairs']} "
            f"({_pct(reorder['inversion_success_fraction_of_opportunities'])})"
        )
        print(
            "overtaken Classic opportunity bytes actually corrected: "
            f"{_pct(reorder['unique_overtaken_fraction_of_opportunity_bytes'])}"
        )
        print(
            "corrected witness pairs still ordered the same after downstream FIFO: "
            f"{persistence['witness_pairs_preserved']} / "
            f"{persistence['unique_overtaken_reno_witness_pairs_observed_downstream']} "
            f"({_pct(persistence['preservation_fraction'])})"
        )
        print()

    print(
        "Note: these are packet-level network metrics. Encrypted QUIC captures do not "
        "identify exact MoQ object boundaries, so this is not an exact count of Base objects."
    )


if __name__ == "__main__":
    main()
