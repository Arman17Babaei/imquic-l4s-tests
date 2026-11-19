#!/usr/bin/env python3
"""Run the Gaussian player transport in an ephemeral QEMU overlay."""

import argparse
import base64
import json
import os
import platform
import re
import resource
import shlex
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from experiment_metadata import repository_snapshot
from qemu_setup_cache import SetupCache, archive_digest, base_identity, cache_key

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
            path = repository / relative
            if path.exists() or path.is_symlink():
                archive.add(path, arcname=relative, recursive=False)


def archive_repository(repository, destination):
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=repository,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    sparse = subprocess.run(
        ["git", "config", "--bool", "core.sparseCheckout"], cwd=repository,
        text=True, capture_output=True,
    ).stdout.strip() == "true"
    # git archive includes excluded blobs and can fetch private demo models in
    # a filtered clone. Stage only the files present in a sparse worker checkout.
    if status or sparse:
        archive_worktree(repository, destination)
    else:
        run(
            ["git", "archive", "--format=tar.gz", f"--output={destination}", "HEAD"],
            cwd=repository,
        )


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
    try:
        import pexpect
    except ImportError as exc:
        raise RuntimeError("QEMU provisioning requires python3-pexpect") from exc
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
        # The SSH command can contain a base64-encoded provisioning script with
        # sudo credentials. Keep that command out of exception tracebacks.
        raise RuntimeError(f"guest command exited with status {status}")


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


def setup_script(guest_root, user, password, cpus):
    """Preparation contains OS/native dependencies only; never experiment inputs."""
    return f'''set -e
if ! pkg-config --exists glib-2.0 openssl jansson libcurl; then
  printf '%s\\n' {shlex.quote(password)} | sudo -S apt-get update >/dev/null || true
  printf '%s\\n' {shlex.quote(password)} | sudo -S DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 install -y libglib2.0-dev libssl-dev libjansson-dev libcurl4-openssl-dev automake libtool pkg-config >/dev/null
fi
rm -rf {shlex.quote(guest_root)}
mkdir -p {shlex.quote(guest_root)}/deps/imquic {shlex.quote(guest_root)}/deps/picoquic
tar -xzf /home/{shlex.quote(user)}/imquic-source.tar.gz -C {shlex.quote(guest_root)}/deps/imquic
tar -xzf /home/{shlex.quote(user)}/picoquic.tar.gz -C {shlex.quote(guest_root)}/deps/picoquic
mkdir -p {shlex.quote(guest_root)}/deps/picoquic/_deps
tar -xzf /home/{shlex.quote(user)}/picotls-cache.tar.gz -C {shlex.quote(guest_root)}/deps/picoquic/_deps
mkdir -p {shlex.quote(guest_root)}/deps/picoquic/_deps/picotls-prefix/lib
cmake -S {shlex.quote(guest_root)}/deps/picoquic/_deps/picotls-src -B {shlex.quote(guest_root)}/deps/picoquic/_deps/picotls-build -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DWITH_FUSION=OFF >/dev/null
cmake --build {shlex.quote(guest_root)}/deps/picoquic/_deps/picotls-build -j{cpus} >/dev/null
cp -a {shlex.quote(guest_root)}/deps/picoquic/_deps/picotls-src/include {shlex.quote(guest_root)}/deps/picoquic/_deps/picotls-prefix/include
for library in libpicotls-core.a libpicotls-openssl.a libpicotls-minicrypto.a; do
  cp {shlex.quote(guest_root)}/deps/picoquic/_deps/picotls-build/$library {shlex.quote(guest_root)}/deps/picoquic/_deps/picotls-prefix/lib/$library
done
cd {shlex.quote(guest_root)}/deps/picoquic
cmake -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DPICOQUIC_FETCH_PTLS=N -DPTLS_PREFIX={shlex.quote(guest_root)}/deps/picoquic/_deps/picotls-prefix -DPTLS_INCLUDE_DIR={shlex.quote(guest_root)}/deps/picoquic/_deps/picotls-prefix/include . >/dev/null
cmake --build . --target picoquic-core picoquic-log picohttp-core -j{cpus} >/dev/null
cd {shlex.quote(guest_root)}/deps/imquic
autoreconf -fi >/dev/null
./configure --with-picoquic={shlex.quote(guest_root)}/deps/picoquic --enable-moq-examples >/dev/null
make -C src -j{cpus} >/dev/null
make -C examples imquic-moq-relay -j{cpus} >/dev/null
make -C src check
rm -f /home/{shlex.quote(user)}/imquic-source.tar.gz /home/{shlex.quote(user)}/picoquic.tar.gz /home/{shlex.quote(user)}/picotls-cache.tar.gz
'''


def stage_runtime_script(guest_root, user, source_name, player_name, provenance_name, install_guest_files):
    return f'''set -e
cd {shlex.quote(guest_root)}
find . -mindepth 1 -maxdepth 1 ! -name deps -exec rm -rf -- {{}} +
rm -rf deps/gaussian-player
mkdir -p deps/gaussian-player
tar -xzf /home/{shlex.quote(user)}/{source_name} -C {shlex.quote(guest_root)}
tar -xzf /home/{shlex.quote(user)}/{player_name} -C {shlex.quote(guest_root)}/deps/gaussian-player
cp /home/{shlex.quote(user)}/{provenance_name} .source-provenance.json
{install_guest_files}
'''


def add_host_resource_arguments(parser):
    parser.add_argument('--host-cpus', help='host CPU list, for example 2-5')
    parser.add_argument('--host-numa-node', type=int, help='bind guest RAM to this host NUMA node')
    parser.add_argument('--preallocate-memory', action='store_true')
    parser.add_argument('--lock-memory', action='store_true', help='lock QEMU memory; requires unlimited memlock')


def parse_cpu_list(value):
    cpus = set()
    for part in value.split(','):
        if not re.fullmatch(r'\d+(?:-\d+)?', part):
            raise ValueError('CPU list must contain CPU numbers or ascending ranges')
        ends = [int(number) for number in part.split('-')]
        first, last = ends[0], ends[-1]
        if first > last:
            raise ValueError('CPU ranges must be ascending')
        cpus.update(range(first, last + 1))
    return sorted(cpus)


def validate_host_resources(args):
    if args.cpus <= 0 or args.memory <= 0:
        raise ValueError('--cpus and guest memory must be positive')
    value = getattr(args, 'host_cpus', None)
    cpus = parse_cpu_list(value) if value is not None else None
    if cpus is not None:
        if not set(cpus) <= os.sched_getaffinity(0):
            raise ValueError('--host-cpus must be contained in the current allowed affinity')
        if len(cpus) < args.cpus:
            raise ValueError('--host-cpus must provide at least one host CPU per guest vCPU')
    node = getattr(args, 'host_numa_node', None)
    if node is not None:
        node_path = Path(f'/sys/devices/system/node/node{node}/cpulist')
        if node < 0 or not node_path.is_file():
            raise ValueError('--host-numa-node must name an existing NUMA node')
        if cpus is not None and not set(cpus) <= set(parse_cpu_list(node_path.read_text().strip())):
            raise ValueError('--host-cpus must belong to --host-numa-node')
        status = Path('/proc/self/status').read_text()
        allowed = re.search(r'^Mems_allowed_list:\s*(.*)$', status, re.MULTILINE)
        if allowed and node not in parse_cpu_list(allowed.group(1).strip()):
            raise ValueError('--host-numa-node is outside the allowed memory nodes')
    return cpus


def host_resource_record(args):
    return {name: getattr(args, name, default) for name, default in (
        ('host_cpus', None), ('host_numa_node', None),
        ('preallocate_memory', False), ('lock_memory', False))}


def qemu_child_setup(cpus, lock_memory):
    def setup():
        if cpus is not None:
            os.sched_setaffinity(0, cpus)
        if lock_memory:
            resource.setrlimit(resource.RLIMIT_MEMLOCK,
                               (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    return setup


def pin_vcpu_threads(process, cpus, count, *, task_root=None, timeout=10):
    tasks = task_root or Path(f'/proc/{process.pid}/task')
    pinned = set()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for task in sorted(tasks.iterdir()):
            try:
                match = re.fullmatch(r'CPU (\d+)/KVM', (task / 'comm').read_text().strip())
                if match and int(match[1]) < count:
                    index = int(match[1])
                    os.sched_setaffinity(int(task.name), {cpus[index]})
                    pinned.add(index)
            except FileNotFoundError:
                continue
        if len(pinned) == count:
            return
        if process.poll() is not None:
            raise RuntimeError('QEMU exited before vCPU affinity could be applied')
        time.sleep(0.05)
    raise RuntimeError('timed out identifying QEMU KVM vCPU threads for CPU pinning')


def boot_qemu(args, overlay, serial_log, port):
    host_forwards = f"hostfwd=tcp:127.0.0.1:{port}-:22"
    if args.forward_sidecar_port:
        host_forwards += f",hostfwd=tcp:127.0.0.1:{args.forward_sidecar_port}-:7790"
    cpus = validate_host_resources(args)
    command = [
        "qemu-system-x86_64", "-enable-kvm", "-cpu", "host",
        "-m", str(args.memory), "-smp", str(args.cpus),
        "-drive", f"file={overlay},format=qcow2,if=ide,cache=none,aio=native,discard=unmap",
        "-netdev", f"user,id=n0,{host_forwards}",
        "-device", "e1000,netdev=n0", "-boot", "c", "-display", "none",
        "-serial", f"file:{serial_log}",
    ]
    node = getattr(args, 'host_numa_node', None)
    preallocate = getattr(args, 'preallocate_memory', False)
    lock = getattr(args, 'lock_memory', False)
    if node is not None or preallocate or lock:
        backend = f'memory-backend-ram,id=ram,size={args.memory}M,merge=off,prealloc={"on" if preallocate else "off"}'
        if node is not None:
            backend += f',host-nodes={node},policy=bind'
        command += ['-object', backend, '-numa', 'node,memdev=ram']
    if lock:
        command += ['-overcommit', 'mem-lock=on']
    options = {}
    if cpus is not None:
        command += ['-name', 'live4s,debug-threads=on']
    if cpus is not None or lock:
        options['preexec_fn'] = qemu_child_setup(cpus, lock)
    try:
        process = subprocess.Popen(command, **options)
    except subprocess.SubprocessError as error:
        raise RuntimeError('cannot configure QEMU CPU affinity or unlimited RLIMIT_MEMLOCK; '
                           'memory locking requires permission to raise the hard memlock limit') from error
    if cpus is not None:
        try:
            pin_vcpu_threads(process, cpus, args.cpus,
                             timeout=getattr(args, 'boot_timeout', 180))
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
    return process


def stop_qemu(args, port, qemu, *, require_clean=False):
    """Publishable preparation requires successful SSH poweroff and QEMU exit."""
    clean = False
    try:
        if qemu.poll() is None:
            shutdown = f"printf '%s\\n' {shlex.quote(args.password)} | sudo -S poweroff"
            ssh_command(port, args.user, args.password, shutdown, timeout=30, accepted=(0, 255))
            clean = qemu.wait(timeout=60) == 0
    except Exception as error:
        if require_clean:
            raise
        print(f'QEMU graceful shutdown failed: {error}', file=sys.stderr)
    finally:
        if qemu.poll() is None:
            qemu.terminate()
            try:
                qemu.wait(timeout=10)
            except subprocess.TimeoutExpired:
                qemu.kill()
                qemu.wait()
    if require_clean and not clean:
        raise RuntimeError('setup cache requires a clean guest poweroff')


def create_overlay(base, overlay):
    run(['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2',
         '-b', str(base.resolve()), str(overlay)])


def main():
    def terminate_run(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate_run)
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
    parser.add_argument("--make-target", default="l4s-camera-player-guest-check")
    parser.add_argument("--guest-result-name", default="qemu-run")
    parser.add_argument("--guest-result-root", default="results/l4s")
    parser.add_argument("--destination-root", default="results/l4s")
    parser.add_argument("--destination-prefix", default="qemu-player")
    parser.add_argument("--make-variable", action="append", default=[])
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--setup-cache-dir", type=Path, default=ROOT / "results/qemu-setup-cache")
    parser.add_argument("--no-setup-cache", action="store_true",
                        help="prepare native dependencies anew inside the disposable run overlay")
    parser.add_argument(
        "--forward-sidecar-port", type=int, default=0,
        help="forward this localhost TCP port to guest port 7790 for an interactive 3DGS sidecar",
    )
    parser.add_argument(
        "--guest-file", action="append", type=parse_guest_file, default=[],
        metavar="SOURCE=RELATIVE_DEST",
        help="copy a host input into the ephemeral guest checkout",
    )
    parser.add_argument("--session-owner",type=Path,help="publish a suite-owned VM session")
    parser.add_argument("--owner-pid",type=int,default=os.getppid())
    add_host_resource_arguments(parser)
    args = parser.parse_args()
    try:
        validate_host_resources(args)
    except ValueError as error:
        parser.error(str(error))

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

    if os.environ.get('IMQUIC_QEMU_SESSION') and not args.session_owner:
        if (args.host_cpus is not None or args.host_numa_node is not None
                or args.preallocate_memory or args.lock_memory):
            raise SystemExit('host resource flags require a new VM; unset IMQUIC_QEMU_SESSION or configure its session owner')
        from qemu_session import run_case
        return run_case(sys.modules[__name__],args,Path(os.environ['IMQUIC_QEMU_SESSION']))

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
        gaussian_player_archive = temporary / "gaussian-player.tar.gz"
        picotls_archive = temporary / "picotls-cache.tar.gz"
        source_provenance = temporary / "source-provenance.json"
        serial_log = temporary / "serial.log"
        if status:
            archive_worktree(ROOT, source_archive)
        else:
            run(["git", "archive", "--format=tar.gz", f"--output={source_archive}", "HEAD"], cwd=ROOT)
        source_provenance.write_text(
            json.dumps(dict(repository_snapshot(ROOT), host_resources=host_resource_record(args)), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        archive_repository(ROOT / "deps" / "picoquic", picoquic_archive)
        picotls_source = ROOT / "deps" / "picoquic" / "build" / "_deps" / "picotls-src"
        if not (picotls_source / "include" / "picotls" / "minicrypto.h").exists():
            raise RuntimeError("QEMU validation needs the cached picotls tree; run 'make build' first")
        run([
            "tar", "-czf", str(picotls_archive),
            "-C", str(picotls_source.parent), picotls_source.name,
        ])
        archive_repository(ROOT / "deps" / "imquic", imquic_archive)
        archive_repository(ROOT / "deps" / "gaussian-player", gaussian_player_archive)
        setup = setup_script(guest_root, args.user, args.password, args.cpus)
        cache_evidence = {'enabled': not args.no_setup_cache, 'hit': False, 'key': None,
                          'host_resources': host_resource_record(args)}
        prepared_base = args.base.resolve()
        if not args.no_setup_cache:
            configuration = {
                'schema': 1, 'base_chain': base_identity(args.base),
                'architecture': platform.machine(), 'guest_user': args.user,
                'qemu_version': subprocess.check_output(['qemu-system-x86_64', '--version'], text=True).splitlines()[0],
                'recipe_sha256': cache_key(setup_script(guest_root, args.user, '', args.cpus)),
                'imquic_sources': archive_digest(imquic_archive),
                'picoquic_sources': archive_digest(picoquic_archive),
                'picotls_sources': archive_digest(picotls_archive),
            }
            cache = SetupCache(args.setup_cache_dir, configuration)
            def prepare(image):
                create_overlay(args.base, image)
                preparation_port = free_port()
                preparation_args = argparse.Namespace(**vars(args))
                preparation_args.forward_sidecar_port = 0
                preparation = boot_qemu(preparation_args, image, temporary / 'setup-serial.log', preparation_port)
                succeeded = False
                try:
                    wait_for_ssh(preparation_port, preparation)
                    wait_for_authenticated_ssh(preparation_port, args.user, args.password,
                                              preparation, timeout=args.boot_timeout)
                    for archive in (imquic_archive, picoquic_archive, picotls_archive):
                        copy_to_guest(preparation_port, args.user, args.password, archive)
                    ssh_command(preparation_port, args.user, args.password, setup, timeout=args.guest_timeout)
                    succeeded = True
                finally:
                    stop_qemu(args, preparation_port, preparation, require_clean=succeeded)
                run(['qemu-img', 'check', str(image)], capture_output=True)
            with cache.locked():
                prepared_base, hit = cache.obtain(prepare)
            cache_evidence.update({'key': cache.key, 'hit': hit,
                                   'configuration': configuration, 'image': str(prepared_base)})
            print(f"QEMU setup cache: {'HIT' if hit else 'MISS'} {cache.key}", flush=True)
        create_overlay(prepared_base, overlay)
        qemu = boot_qemu(args, overlay, serial_log, port)
        try:
            wait_for_ssh(port, qemu)
            wait_for_authenticated_ssh(
                port, args.user, args.password, qemu, timeout=args.boot_timeout
            )
            if args.forward_sidecar_port:
                print(f"3DGS sidecar forwarding: ws://127.0.0.1:{args.forward_sidecar_port}")
            if args.no_setup_cache:
                for archive in (imquic_archive, picoquic_archive, picotls_archive):
                    copy_to_guest(port, args.user, args.password, archive)
                ssh_command(port, args.user, args.password, setup, timeout=args.guest_timeout)
            copy_to_guest(port, args.user, args.password, source_archive)
            copy_to_guest(port, args.user, args.password, gaussian_player_archive)
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
            if args.session_owner:
                from qemu_session import serve
                stage=stage_runtime_script(guest_root,args.user,source_archive.name,
                    gaussian_player_archive.name,source_provenance.name,install_guest_files)
                ssh_command(port,args.user,args.password,stage,timeout=args.guest_timeout)
                return serve(sys.modules[__name__],args,port,guest_root,cache_evidence,qemu)
            provision = stage_runtime_script(guest_root, args.user, source_archive.name,
                gaussian_player_archive.name, source_provenance.name, install_guest_files) + f'''set -e
make -C deps/imquic/src check
set +e
printf '%s\\n' {shlex.quote(args.password)} | sudo -S make {shlex.quote(args.make_target)} L4S_RESULT_DIR={shlex.quote(guest_result)} {make_variables}
make_status=$?
printf '%s\\n' {shlex.quote(args.password)} | sudo -S chown -R {shlex.quote(args.user)}:{shlex.quote(args.user)} {shlex.quote(guest_result)}
exit $make_status
'''
            try:
                ssh_command(port, args.user, args.password, provision,
                            timeout=args.guest_timeout)
            except Exception:
                try:
                    copy_results(port, args.user, args.password, guest_result, destination)
                    (destination / 'setup-cache.json').write_text(json.dumps(cache_evidence, indent=2, sort_keys=True) + '\n')
                    print(f"Retrieved failed guest results: {destination}", file=sys.stderr)
                except Exception:
                    pass
                raise
            copy_results(port, args.user, args.password, guest_result, destination)
            (destination / 'setup-cache.json').write_text(json.dumps(cache_evidence, indent=2, sort_keys=True) + '\n')
            print(f"QEMU {args.make_target}: PASS ({destination})")
        finally:
            stop_qemu(args, port, qemu)


def shutil_which(command):
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / command
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


if __name__ == "__main__":
    main()
