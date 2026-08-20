#!/usr/bin/env python3
"""Run the privileged IMQUIC Prague test in an ephemeral QEMU overlay."""

import argparse
import base64
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from experiment_metadata import repository_snapshot

try:
    import pexpect
except ImportError as exc:
    raise SystemExit("run_qemu_timeseries_test.py requires python3-pexpect") from exc


ROOT = Path(__file__).resolve().parents[2]


def run(command, **kwargs):
    return subprocess.run(command, check=True, text=True, **kwargs)


def archive_worktree(repository, destination):
    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    with tarfile.open(destination, "w:gz") as archive:
        for encoded in tracked:
            if not encoded:
                continue
            relative = os.fsdecode(encoded)
            archive.add(repository / relative, arcname=relative, recursive=False)


def parse_guest_file(value):
    if "=" not in value:
        raise argparse.ArgumentTypeError("guest files must use SOURCE=RELATIVE_DEST")
    source_text, destination_text = value.split("=", 1)
    source = Path(source_text).expanduser().resolve()
    destination = Path(destination_text)
    if not source.is_file():
        raise argparse.ArgumentTypeError(f"guest file source not found: {source}")
    if destination.is_absolute() or ".." in destination.parts or not destination.parts:
        raise argparse.ArgumentTypeError(
            "guest file destination must be a safe path relative to the guest checkout"
        )
    return source, destination


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def password_command(command, password, timeout, stream=True, accepted=(0,)):
    child = pexpect.spawn("/bin/sh", ["-c", command], encoding="utf-8", timeout=timeout)
    if stream:
        child.logfile_read = sys.stdout
    while True:
        match = child.expect([r"(?i)password:", pexpect.EOF, pexpect.TIMEOUT])
        if match == 0:
            child.sendline(password)
        elif match == 1:
            break
        else:
            child.close(force=True)
            raise RuntimeError(f"command timed out: {command.split()[0]}")
    child.close()
    status = child.exitstatus if child.exitstatus is not None else child.signalstatus
    if status not in accepted:
        raise subprocess.CalledProcessError(status, command)


def ssh_command(port, user, password, script, timeout=1200, accepted=(0,)):
    payload = base64.b64encode(script.encode()).decode()
    remote = f"echo {payload} | base64 -d | bash"
    command = " ".join(
        [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-p", str(port),
            f"{shlex.quote(user)}@127.0.0.1",
            shlex.quote(remote),
        ]
    )
    password_command(command, password, timeout, accepted=accepted)


def copy_to_guest(port, user, password, source):
    command = " ".join(
        [
            "scp", "-q", "-P", str(port),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            shlex.quote(str(source)),
            f"{shlex.quote(user)}@127.0.0.1:/home/{shlex.quote(user)}/",
        ]
    )
    password_command(command, password, 180)


def copy_results(port, user, password, guest_path, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = " ".join(
        [
            "scp", "-q", "-r", "-P", str(port),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{shlex.quote(user)}@127.0.0.1:{shlex.quote(guest_path)}",
            shlex.quote(str(destination)),
        ]
    )
    password_command(command, password, 180)


def wait_for_ssh(port, process, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"QEMU exited early with status {process.returncode}")
        with socket.socket() as sock:
            sock.settimeout(1)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(2)
    raise RuntimeError("QEMU SSH did not become ready")


def wait_for_authenticated_ssh(port, user, password, process, timeout=60):
    deadline = time.monotonic() + timeout
    command = " ".join(
        [
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-p", str(port),
            f"{shlex.quote(user)}@127.0.0.1", "true",
        ]
    )
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"QEMU exited early with status {process.returncode}")
        try:
            password_command(command, password, 10, stream=False)
            return
        except (RuntimeError, subprocess.CalledProcessError):
            time.sleep(2)
    raise RuntimeError("QEMU SSH authentication did not become ready")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base",
        type=Path,
        default=Path("~/sandbox/p4/work.qcow2").expanduser(),
    )
    parser.add_argument("--user", default=os.environ.get("IMQUIC_QEMU_USER", "p4"))
    parser.add_argument(
        "--password", default=os.environ.get("IMQUIC_QEMU_PASSWORD", "p4")
    )
    parser.add_argument("--memory", type=int, default=4096)
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument(
        "--boot-timeout", type=int, default=180,
        help="seconds to wait for authenticated guest SSH",
    )
    parser.add_argument(
        "--guest-timeout", type=int, default=1800,
        help="seconds to allow the guest provisioning and test command (default: 1800)",
    )
    parser.add_argument("--make-target", default="l4s-timeseries-guest-check")
    parser.add_argument("--guest-result-name", default="qemu-run")
    parser.add_argument("--guest-result-root", default="results/l4s")
    parser.add_argument("--destination-root", default="results/l4s")
    parser.add_argument("--destination-prefix", default="qemu-timeseries")
    parser.add_argument("--make-variable", action="append", default=[])
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument(
        "--forward-sidecar-port", type=int, default=0,
        help="forward this localhost TCP port to guest port 7790 for an interactive 3DGS sidecar",
    )
    parser.add_argument(
        "--guest-file", action="append", type=parse_guest_file, default=[],
        metavar="SOURCE=RELATIVE_DEST",
        help="copy a host input into the ephemeral guest checkout",
    )
    args = parser.parse_args()

    if not args.base.is_file():
        raise SystemExit(f"QEMU base image does not exist: {args.base}")
    if args.guest_timeout <= 0:
        raise SystemExit("--guest-timeout must be positive")
    if args.forward_sidecar_port < 0 or args.forward_sidecar_port > 65535:
        raise SystemExit("--forward-sidecar-port must be a valid TCP port")
    for command in ("qemu-img", "qemu-system-x86_64", "git", "ssh", "scp"):
        if not shutil_which(command):
            raise SystemExit(f"missing command: {command}")
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    if status and not args.allow_dirty:
        raise SystemExit("refusing to test a dirty worktree; commit the milestone first")
    for assignment in args.make_variable:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=[^\n]*", assignment):
            raise SystemExit(f"invalid make variable: {assignment}")
    for label, value in (("guest result root", args.guest_result_root),
                         ("destination root", args.destination_root)):
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise SystemExit(f"{label} must be a safe relative path")

    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    destination = ROOT / args.destination_root / f"{args.destination_prefix}-{timestamp}"
    guest_root = f"/home/{args.user}/imquic-qemu-test"
    guest_result = f"{guest_root}/{Path(args.guest_result_root).as_posix()}/{args.guest_result_name}"
    port = free_port()

    with tempfile.TemporaryDirectory(prefix="imquic-l4s-qemu-") as temporary:
        temporary = Path(temporary)
        overlay = temporary / "overlay.qcow2"
        source_archive = temporary / "imquic.tar.gz"
        imquic_archive = temporary / "imquic-source.tar.gz"
        picoquic_archive = temporary / "picoquic.tar.gz"
        three_dgs_archive = temporary / "3dgs-over-moq.tar.gz"
        picotls_archive = temporary / "picotls-cache.tar.gz"
        source_provenance = temporary / "source-provenance.json"
        serial_log = temporary / "serial.log"
        run(
            [
                "qemu-img", "create", "-f", "qcow2", "-F", "qcow2",
                "-b", str(args.base.resolve()), str(overlay),
            ]
        )
        if status:
            archive_worktree(ROOT, source_archive)
        else:
            run(["git", "archive", "--format=tar.gz", f"--output={source_archive}", "HEAD"], cwd=ROOT)
        source_provenance.write_text(
            json.dumps(repository_snapshot(ROOT), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        run(
            ["git", "archive", "--format=tar.gz", f"--output={picoquic_archive}", "HEAD"],
            cwd=ROOT / "deps" / "picoquic",
        )
        picotls_source = ROOT / "deps" / "picoquic" / "build" / "_deps" / "picotls-src"
        if not (picotls_source / "include" / "picotls" / "minicrypto.h").exists():
            raise RuntimeError("QEMU validation needs the cached picotls tree; run 'make build' first")
        run([
            "tar", "-czf", str(picotls_archive),
            "-C", str(picotls_source.parent), picotls_source.name,
        ])
        run(
            ["git", "archive", "--format=tar.gz", f"--output={imquic_archive}", "HEAD"],
            cwd=ROOT / "deps" / "imquic",
        )
        three_dgs_root = ROOT / "deps" / "3dgs_over_moq"
        three_dgs_status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=three_dgs_root, check=True, text=True, capture_output=True,
        ).stdout
        if three_dgs_status:
            archive_worktree(three_dgs_root, three_dgs_archive)
        else:
            run(["git", "archive", "--format=tar.gz", f"--output={three_dgs_archive}", "HEAD"],
                cwd=three_dgs_root)
        host_forwards = f"hostfwd=tcp:127.0.0.1:{port}-:22"
        if args.forward_sidecar_port:
            host_forwards += f",hostfwd=tcp:127.0.0.1:{args.forward_sidecar_port}-:7790"
        qemu = subprocess.Popen(
            [
                "qemu-system-x86_64", "-enable-kvm", "-cpu", "host",
                "-m", str(args.memory), "-smp", str(args.cpus),
                "-drive", f"file={overlay},format=qcow2,if=ide,cache=none,aio=native,discard=unmap",
                "-netdev", f"user,id=n0,{host_forwards}",
                "-device", "e1000,netdev=n0", "-boot", "c", "-display", "none",
                "-serial", f"file:{serial_log}",
            ]
        )
        try:
            wait_for_ssh(port, qemu)
            wait_for_authenticated_ssh(
                port, args.user, args.password, qemu, timeout=args.boot_timeout
            )
            if args.forward_sidecar_port:
                print(f"3DGS sidecar forwarding: ws://127.0.0.1:{args.forward_sidecar_port}")
            copy_to_guest(port, args.user, args.password, source_archive)
            copy_to_guest(port, args.user, args.password, imquic_archive)
            copy_to_guest(port, args.user, args.password, picoquic_archive)
            copy_to_guest(port, args.user, args.password, three_dgs_archive)
            copy_to_guest(port, args.user, args.password, picotls_archive)
            copy_to_guest(port, args.user, args.password, source_provenance)
            staged_guest_files = []
            for index, (source, relative) in enumerate(args.guest_file):
                staged_name = f"codex-guest-input-{index:02d}-{source.name}"
                staged = temporary / staged_name
                staged.symlink_to(source)
                copy_to_guest(port, args.user, args.password, staged)
                staged_guest_files.append((staged_name, relative))
            make_variables = " ".join(shlex.quote(value) for value in args.make_variable)
            install_guest_files = "\n".join(
                "install -D "
                f"/home/{shlex.quote(args.user)}/{shlex.quote(staged_name)} "
                f"{shlex.quote(str(Path(guest_root) / relative))}"
                for staged_name, relative in staged_guest_files
            )
            provision = f'''set -e
if ! pkg-config --exists glib-2.0 openssl jansson libcurl; then
  printf '%s\\n' {shlex.quote(args.password)} | sudo -S apt-get update >/dev/null || true
  printf '%s\\n' {shlex.quote(args.password)} | sudo -S DEBIAN_FRONTEND=noninteractive apt-get install -y libglib2.0-dev libssl-dev libjansson-dev libcurl4-openssl-dev automake libtool pkg-config >/dev/null
fi
rm -rf {shlex.quote(guest_root)}
mkdir -p {shlex.quote(guest_root)}/deps/imquic {shlex.quote(guest_root)}/deps/picoquic {shlex.quote(guest_root)}/deps/3dgs_over_moq
tar -xzf /home/{shlex.quote(args.user)}/{source_archive.name} -C {shlex.quote(guest_root)}
cp /home/{shlex.quote(args.user)}/{source_provenance.name} {shlex.quote(guest_root)}/.source-provenance.json
{install_guest_files}
tar -xzf /home/{shlex.quote(args.user)}/{imquic_archive.name} -C {shlex.quote(guest_root)}/deps/imquic
tar -xzf /home/{shlex.quote(args.user)}/{picoquic_archive.name} -C {shlex.quote(guest_root)}/deps/picoquic
tar -xzf /home/{shlex.quote(args.user)}/{three_dgs_archive.name} -C {shlex.quote(guest_root)}/deps/3dgs_over_moq
mkdir -p {shlex.quote(guest_root)}/deps/picoquic/_deps
tar -xzf /home/{shlex.quote(args.user)}/{picotls_archive.name} -C {shlex.quote(guest_root)}/deps/picoquic/_deps
mkdir -p {shlex.quote(guest_root)}/deps/picoquic/_deps/picotls-prefix/lib
cmake -S {shlex.quote(guest_root)}/deps/picoquic/_deps/picotls-src -B {shlex.quote(guest_root)}/deps/picoquic/_deps/picotls-build -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DWITH_FUSION=OFF >/dev/null
cmake --build {shlex.quote(guest_root)}/deps/picoquic/_deps/picotls-build -j{args.cpus} >/dev/null
cp -a {shlex.quote(guest_root)}/deps/picoquic/_deps/picotls-src/include {shlex.quote(guest_root)}/deps/picoquic/_deps/picotls-prefix/include
for library in libpicotls-core.a libpicotls-openssl.a libpicotls-minicrypto.a; do
  cp {shlex.quote(guest_root)}/deps/picoquic/_deps/picotls-build/$library {shlex.quote(guest_root)}/deps/picoquic/_deps/picotls-prefix/lib/$library
done
cd {shlex.quote(guest_root)}/deps/picoquic
cmake -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DPICOQUIC_FETCH_PTLS=N -DPTLS_PREFIX={shlex.quote(guest_root)}/deps/picoquic/_deps/picotls-prefix -DPTLS_INCLUDE_DIR={shlex.quote(guest_root)}/deps/picoquic/_deps/picotls-prefix/include . >/dev/null
cmake --build . --target picoquic-core picoquic-log picohttp-core -j{args.cpus} >/dev/null
cd {shlex.quote(guest_root)}/deps/imquic
autoreconf -fi >/dev/null
./configure --with-picoquic={shlex.quote(guest_root)}/deps/picoquic >/dev/null
make -j{args.cpus} >/dev/null
make check
cd {shlex.quote(guest_root)}
printf '%s\\n' {shlex.quote(args.password)} | sudo -S make {shlex.quote(args.make_target)} L4S_RESULT_DIR={shlex.quote(guest_result)} {make_variables}
printf '%s\\n' {shlex.quote(args.password)} | sudo -S chown -R {shlex.quote(args.user)}:{shlex.quote(args.user)} {shlex.quote(guest_result)}
'''
            try:
                ssh_command(port, args.user, args.password, provision,
                            timeout=args.guest_timeout)
            except Exception:
                try:
                    copy_results(port, args.user, args.password, guest_result, destination)
                    print(f"Retrieved failed guest results: {destination}", file=sys.stderr)
                except Exception:
                    pass
                raise
            copy_results(port, args.user, args.password, guest_result, destination)
            print(f"QEMU {args.make_target}: PASS ({destination})")
        finally:
            if qemu.poll() is None:
                try:
                    shutdown = f"printf '%s\\n' {shlex.quote(args.password)} | sudo -S poweroff"
                    ssh_command(port, args.user, args.password, shutdown, timeout=30, accepted=(0, 255))
                except Exception as exc:
                    print(f"QEMU graceful shutdown failed: {exc}", file=sys.stderr)
                try:
                    qemu.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    qemu.terminate()
                    try:
                        qemu.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        qemu.kill()
                        qemu.wait()


def shutil_which(command):
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / command
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


if __name__ == "__main__":
    main()
