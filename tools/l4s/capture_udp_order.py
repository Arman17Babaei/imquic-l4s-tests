#!/usr/bin/env python3
"""Capture compact kernel-timestamped UDP packet identities from an interface."""

from __future__ import annotations

import argparse
import csv
import hashlib
import ipaddress
import json
import signal
import socket
import struct
from pathlib import Path

RENO_PORT = 4443
PRAGUE_PORT = 4444
ETH_P_ALL = 0x0003
SOL_PACKET = 263
PACKET_STATISTICS = 6
SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35)

running = True


def stop_capture(_signum, _frame) -> None:
    global running
    running = False


def parse_udp_packet(frame: bytes, server_address: bytes):
    if len(frame) < 14:
        return None
    offset = 14
    ether_type = struct.unpack("!H", frame[12:14])[0]
    while ether_type in (0x8100, 0x88A8):
        if len(frame) < offset + 4:
            return None
        ether_type = struct.unpack("!H", frame[offset + 2:offset + 4])[0]
        offset += 4
    if ether_type != 0x0800 or len(frame) < offset + 20:
        return None
    version_ihl = frame[offset]
    if version_ihl >> 4 != 4:
        return None
    ip_header_length = (version_ihl & 0x0F) * 4
    if ip_header_length < 20 or len(frame) < offset + ip_header_length:
        return None
    if frame[offset + 9] != 17:
        return None
    if frame[offset + 12:offset + 16] != server_address:
        return None
    fragment = struct.unpack("!H", frame[offset + 6:offset + 8])[0]
    if fragment & 0x1FFF:
        return None
    udp_offset = offset + ip_header_length
    if len(frame) < udp_offset + 8:
        return None
    source_port, _, udp_length, _ = struct.unpack(
        "!HHHH", frame[udp_offset:udp_offset + 8]
    )
    if source_port not in (RENO_PORT, PRAGUE_PORT) or udp_length < 8:
        return None
    udp_end = udp_offset + udp_length
    if udp_end > len(frame):
        return None
    payload = frame[udp_offset + 8:udp_end]
    digest = hashlib.sha256()
    digest.update(str(source_port).encode("ascii"))
    digest.update(b"|")
    digest.update(str(udp_length).encode("ascii"))
    digest.update(b"|")
    digest.update(payload)
    return source_port, len(payload), digest.hexdigest()


def kernel_timestamp_ns(ancillary) -> int:
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == SO_TIMESTAMPNS:
            seconds, nanoseconds = struct.unpack("qq", data[:16])
            return seconds * 1_000_000_000 + nanoseconds
    raise RuntimeError("packet arrived without SO_TIMESTAMPNS metadata")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stats", type=Path, required=True)
    parser.add_argument("--server-ip", default="10.0.0.1")
    args = parser.parse_args()

    server_address = ipaddress.IPv4Address(args.server_ip).packed
    args.output.parent.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGINT, stop_capture)
    signal.signal(signal.SIGTERM, stop_capture)

    packet_socket = socket.socket(
        socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL)
    )
    packet_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    packet_socket.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
    packet_socket.bind((args.interface, 0))
    packet_socket.settimeout(0.2)

    occurrences: dict[str, int] = {}
    captured_packets = 0
    captured_payload_bytes = 0
    with args.output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            ("time_s", "source_port", "payload_bytes", "fingerprint", "occurrence")
        )
        while running:
            try:
                frame, ancillary, _, _ = packet_socket.recvmsg(65535, 128)
            except (TimeoutError, socket.timeout):
                continue
            parsed = parse_udp_packet(frame, server_address)
            if parsed is None:
                continue
            source_port, payload_bytes, fingerprint = parsed
            occurrence = occurrences.get(fingerprint, 0)
            occurrences[fingerprint] = occurrence + 1
            timestamp_ns = kernel_timestamp_ns(ancillary)
            writer.writerow(
                (
                    f"{timestamp_ns / 1_000_000_000:.9f}",
                    source_port,
                    payload_bytes,
                    fingerprint,
                    occurrence,
                )
            )
            captured_packets += 1
            captured_payload_bytes += payload_bytes

    socket_packets = None
    socket_drops = None
    try:
        raw_stats = packet_socket.getsockopt(SOL_PACKET, PACKET_STATISTICS, 8)
        socket_packets, socket_drops = struct.unpack("II", raw_stats)
    except OSError:
        pass
    packet_socket.close()
    args.stats.write_text(
        json.dumps(
            {
                "capture_format": "packet-order-csv-v1",
                "interface": args.interface,
                "server_ip": args.server_ip,
                "captured_packets": captured_packets,
                "captured_payload_bytes": captured_payload_bytes,
                "socket_packets": socket_packets,
                "socket_drops": socket_drops,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
