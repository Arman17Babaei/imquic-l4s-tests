"""Experiment-scoped QMP CPU pinning; use the unchanged two-job native build cache."""
import atexit
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

OUT=Path(__file__).resolve().parent
sys.path.insert(0,str(Path.cwd()/'tools/l4s'))
import run_qemu_player as runner

original_setup=runner.setup_script
runner.setup_script=lambda root,user,password,cpus:original_setup(root,user,password,2)
directories=[]
atexit.register(lambda:[d.cleanup() for d in directories])


def boot(args,overlay,serial_log,port):
    assert args.cpus==7
    temporary=tempfile.TemporaryDirectory(prefix='prague-qmp-');directories.append(temporary)
    address=Path(temporary.name)/'qmp.sock'
    command=['qemu-system-x86_64','-enable-kvm','-cpu','host','-m',str(args.memory),'-smp',str(args.cpus),
        '-drive',f'file={overlay},format=qcow2,if=ide,cache=none,aio=native,discard=unmap',
        '-netdev',f'user,id=n0,hostfwd=tcp:127.0.0.1:{port}-:22','-device','e1000,netdev=n0',
        '-boot','c','-display','none','-serial',f'file:{serial_log}',
        '-qmp',f'unix:{address},server=on,wait=off']
    process=subprocess.Popen(command)
    try:
        deadline=time.monotonic()+15
        while not address.exists():
            assert process.poll() is None
            if time.monotonic()>deadline:raise TimeoutError('QMP socket not ready')
            time.sleep(.05)
        sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);sock.settimeout(10);sock.connect(str(address))
        stream=sock.makefile('rwb',buffering=0)
        greeting=json.loads(stream.readline())
        def request(name):
            stream.write((json.dumps(dict(execute=name))+'\n').encode())
            while True:
                reply=json.loads(stream.readline())
                if 'error' in reply:raise RuntimeError(reply)
                if 'return' in reply:return reply['return']
        request('qmp_capabilities')
        cpus=request('query-cpus-fast')
        assert {r['cpu-index'] for r in cpus}==set(range(7))
        # QEMU I/O and auxiliary threads use CPU11; each vCPU gets one host core.
        for task in (Path('/proc')/str(process.pid)/'task').iterdir():
            os.sched_setaffinity(int(task.name),{11})
        pins=[]
        for cpu in cpus:
            host_cpu={0:6,1:8,2:10,3:4,4:5,5:7,6:9}[cpu['cpu-index']]
            os.sched_setaffinity(cpu['thread-id'],{host_cpu})
            actual=sorted(os.sched_getaffinity(cpu['thread-id']))
            assert actual==[host_cpu]
            pins.append(dict(guest_cpu=cpu['cpu-index'],host_cpu=host_cpu,tid=cpu['thread-id'],affinity=actual))
        evidence=dict(pid=process.pid,command=command,guest_vcpus=7,native_build_jobs=2,
            greeting=greeting,pins=pins,io_cpu=11,
            host_cores_exclusive=False,unit_cpu_weight=1000)
        (OUT/f'host-affinity-{process.pid}.json').write_text(json.dumps(evidence,indent=2)+'\n')
        stream.close();sock.close()
        return process
    except BaseException:
        process.terminate();process.wait(timeout=10);raise


runner.boot_qemu=boot
runner.main()
