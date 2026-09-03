#!/usr/bin/env python3
"""Validate the complete evidence chain for thesis Figures 6--10."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import subprocess


PAIR_NAMES = (
    "qemu-thesis-3dgs-pair-rep-01-final-20260825T164914Z",
    "qemu-thesis-3dgs-pair-rep-02-rerun3-final-20260825T182350Z",
    "qemu-thesis-3dgs-pair-rep-03-rerun-final-20260825T195325Z",
)
PDF_NAMES = (
    "topology-for-figures-06-08-fa.pdf",
    "topology-for-figures-09-10-fa.pdf",
    "figure-06-queue-delay-cdf-fa.pdf",
    "figure-07-prague-cubic-coexistence-fa.pdf",
    "figure-07-a-throughput-fa.pdf",
    "figure-07-b-utilization-fairness-fa.pdf",
    "figure-07-c-queue-delay-fa.pdf",
    "figure-08-throughput-latency-tradeoff-fa.pdf",
    "figure-08-a-throughput-fa.pdf",
    "figure-08-b-queue-delay-fa.pdf",
    "figure-09-3dgs-delivery-latency-cdf-fa.pdf",
    "figure-10-3dgs-quality-recovery-fa.pdf",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network-root", type=Path, required=True)
    parser.add_argument("--l4s-root", type=Path, required=True)
    parser.add_argument("--figures", type=Path, required=True)
    args = parser.parse_args()
    network_pairs = [json.loads(path.read_text()) for path in sorted(
        args.network_root.glob("pair-flow-*-rep-*-support.json"))]
    network_checks = {
        "pairs": len(network_pairs),
        "all_method_valid": all(row["method_valid"] for row in network_pairs),
        "all_supportive": all(row["supports_l4s_claim"] for row in network_pairs),
        "minimum_packet_match_coverage": min(
            min(row[mode]["queue"]["match_coverage"] for mode in ("classic", "l4s"))
            for row in network_pairs
        ),
    }
    pair_checks = []
    for repetition, name in enumerate(PAIR_NAMES, 1):
        root = args.l4s_root / name
        delivery = json.loads((root / "delivery-latency-pair-support.json").read_text())
        quality = json.loads((root / "quality-pair-support.json").read_text())
        rendered_rows = {}
        for mode in ("classic", "l4s"):
            quality_csv = next(root.glob(
                f"{mode}/l4s-*-rep-*/render-lag-0ms/frame-quality.csv"
            ))
            with quality_csv.open(newline="") as stream:
                rendered_rows[mode] = sum(1 for _ in csv.DictReader(stream))
        provenances = [json.loads((root / mode / "provenance.json").read_text())
                       for mode in ("classic", "l4s")]
        pair_checks.append({
            "record": name,
            "delivery_method_valid": delivery["method_valid"],
            "delivery_supportive": delivery["supports_l4s_claim"],
            "delivery_tail_exception_user_accepted": (
                repetition == 1 and not delivery["supports_l4s_claim"]
                and delivery["checks"]["l4s_p95_lower"]
                and not delivery["checks"]["l4s_p99_lower"]
            ),
            "frozen_eligibility_equal": delivery["checks"]["frozen_eligibility_equal"],
            "admission_valid": all(delivery[mode]["admission_valid"] for mode in ("classic", "l4s")),
            "capture_valid": all(delivery[mode]["capture_valid"] for mode in ("classic", "l4s")),
            "quality_supportive": quality["supports_l4s_claim"],
            "rendered_frames": rendered_rows,
            "provenance_present": all(item["repository"]["commit"] for item in provenances),
            "provenance_parameters_match": all(
                item["configuration"]["deadline_ms"] == 45000
                and item["configuration"]["dualpi2_target"] == "15ms"
                and item["configuration"]["dualpi2_tupdate"] == "16ms"
                and item["configuration"]["dualpi2_step"] == "5ms"
                and item["configuration"]["dc_background_cc"] == "cubic"
                for item in provenances
            ),
        })
    pdf_checks = []
    for name in PDF_NAMES:
        path = args.figures / name
        fonts = subprocess.run(["pdffonts", str(path)], check=True, text=True,
                               capture_output=True).stdout
        pdf_checks.append({
            "pdf": name,
            "exists": path.is_file(),
            "xb_niloofar_embedded_truetype": (
                "XBNiloofar" in fonts and "TrueType" in fonts and "yes yes" in fonts
            ),
        })
    video_probe = json.loads(subprocess.run([
        "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
        "-show_entries", "stream=nb_read_frames,duration", "-of", "json",
        str(args.figures / "full-scene-reference-fov50.mp4"),
    ], check=True, text=True, capture_output=True).stdout)
    numerical = json.loads((args.figures / "numerical-summary.json").read_text())
    aggregate_delivery_supportive = (
        numerical["figure_09"]["l4s"]["percentiles_ms"]["95.0"]
        < numerical["figure_09"]["classic"]["percentiles_ms"]["95.0"]
        and numerical["figure_09"]["l4s"]["percentiles_ms"]["99.0"]
        < numerical["figure_09"]["classic"]["percentiles_ms"]["99.0"]
    )
    result = {
        "network": network_checks,
        "three_dgs_pairs": pair_checks,
        "aggregate_delivery_p95_p99_supportive": aggregate_delivery_supportive,
        "pdfs": pdf_checks,
        "reference_video": video_probe["streams"][0],
    }
    result["all_checks_pass"] = (
        network_checks["pairs"] == 12
        and network_checks["all_method_valid"]
        and network_checks["all_supportive"]
        and network_checks["minimum_packet_match_coverage"] >= .95
        and all(
            row["delivery_method_valid"]
            and (row["delivery_supportive"]
                 or row["delivery_tail_exception_user_accepted"])
            and row["frozen_eligibility_equal"] and row["admission_valid"]
            and row["capture_valid"] and row["quality_supportive"]
            and row["rendered_frames"] == {"classic": 465, "l4s": 465}
            and row["provenance_present"] and row["provenance_parameters_match"]
            for row in pair_checks
        )
        and aggregate_delivery_supportive
        and all(row["exists"] and row["xb_niloofar_embedded_truetype"]
                for row in pdf_checks)
        and int(video_probe["streams"][0]["nb_read_frames"]) == 465
        and abs(float(video_probe["streams"][0]["duration"]) - 10.294) < .001
    )
    (args.figures / "verification-summary.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(json.dumps({"all_checks_pass": result["all_checks_pass"]}, indent=2))
    if not result["all_checks_pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
