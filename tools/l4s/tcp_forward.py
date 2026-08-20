#!/usr/bin/env python3
"""Small TCP forwarder used only to expose a Mininet namespace to QEMU SLIRP."""
from __future__ import annotations

import argparse
import ctypes
import os
import socket
import threading


CLONE_NEWNET = getattr(os, "CLONE_NEWNET", 0x40000000)
LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.setns.argtypes = [ctypes.c_int, ctypes.c_int]
LIBC.setns.restype = ctypes.c_int


def set_network_namespace(namespace_fd: int) -> None:
    setter = getattr(os, "setns", None)
    if setter is not None:
        setter(namespace_fd, CLONE_NEWNET)
        return
    if LIBC.setns(namespace_fd, CLONE_NEWNET) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def copy(source: socket.socket, destination: socket.socket) -> None:
    try:
        while data := source.recv(64 * 1024):
            destination.sendall(data)
    except OSError:
        pass
    finally:
        try:
            destination.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle(client: socket.socket, target: tuple[str, int], network_ns: int | None,
           root_ns: int | None) -> None:
    client.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    client.settimeout(None)
    try:
        if network_ns is not None:
            set_network_namespace(network_ns)
        upstream = socket.create_connection(target, timeout=10)
        upstream.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        upstream.settimeout(None)
    except OSError:
        client.close()
        return
    finally:
        if network_ns is not None and root_ns is not None:
            set_network_namespace(root_ns)
    threading.Thread(target=copy, args=(client, upstream), daemon=True).start()
    copy(upstream, client)
    upstream.close()
    client.close()


def endpoint(value: str) -> tuple[str, int]:
    host, port = value.rsplit(":", 1)
    return host, int(port)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", type=endpoint, default=endpoint("0.0.0.0:7790"))
    parser.add_argument("--target", type=endpoint, required=True)
    parser.add_argument("--network-pid", type=int,
                        help="enter this process's network namespace for upstream connects")
    args = parser.parse_args()
    root_ns = os.open("/proc/self/ns/net", os.O_RDONLY) if args.network_pid else None
    network_ns = (os.open(f"/proc/{args.network_pid}/ns/net", os.O_RDONLY)
                  if args.network_pid else None)
    with socket.create_server(args.listen, reuse_port=False) as server:
        while True:
            client, _ = server.accept()
            threading.Thread(target=handle,
                             args=(client, args.target, network_ns, root_ns),
                             daemon=True).start()


if __name__ == "__main__":
    main()
