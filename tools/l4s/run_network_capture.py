#!/usr/bin/env python3
"""Collect real QEMU object arrivals using a renderer-free adapter inside the guest."""
from __future__ import annotations
import argparse
import io
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
from run_camera_trace_player import ROOT, PLAYER, scene_archive, completed_qemu_result, free_port, validate_manifest
from experiment_suite_topology import topology_source
from run_experiments import scale_trace
from guest_cleanup import process_record, stop_record
from run_qemu_player import add_host_resource_arguments, validate_host_resources, host_resource_record


def adapter_archive(target: Path):
    with tarfile.open(target,'w') as archive:
        # The harness root is ESM; compiled headless clients are CommonJS.
        package=json.dumps({'type':'commonjs'}).encode()
        entry=tarfile.TarInfo('package.json');entry.size=len(package)
        archive.addfile(entry,io.BytesIO(package))
        archive.add(PLAYER/'dist-headless',arcname='dist-headless')
        archive.add(PLAYER/'node_modules/three',arcname='node_modules/three')


def main():
    def interrupted(signum,frame):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,interrupted)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True);parser.add_argument('--trace',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--controller',choices=['prague','bbr'],required=True)
    parser.add_argument('--bandwidth-mbps',type=float,default=150);parser.add_argument('--one-way-delay-ms',type=float,default=10)
    parser.add_argument('--duration-seconds',type=float,default=20);parser.add_argument('--cpus',type=int,default=4);parser.add_argument('--memory-mib',type=int,default=2048)
    parser.add_argument('--base',type=Path,default=Path('~/sandbox/p4/work.qcow2').expanduser())
    add_host_resource_arguments(parser)
    args=parser.parse_args()
    args.memory=args.memory_mib
    try:validate_host_resources(args)
    except ValueError as error:parser.error(str(error))
    out=args.output.resolve();manifest=args.manifest.resolve()
    if out.exists() or not out.is_relative_to(ROOT/'results'):raise ValueError('output must be new under results')
    validate_manifest(manifest);out.mkdir(parents=True)
    provenance=scale_trace(args.trace.resolve(),out/'trace.json',args.duration_seconds)
    topology=out/'topology.py';topology.write_text(topology_source(args.bandwidth_mbps,args.one_way_delay_ms,dualpi2_coupling_factor=2))
    scene_archive(manifest,out/'scene.tar')
    adapter_archive(out/'adapter.tar')
    command=[sys.executable,str(ROOT/'tools/l4s/run_qemu_player.py'),'--base',str(args.base.resolve()),
        '--make-target','l4s-camera-player-guest-check','--allow-dirty','--memory',str(args.memory_mib),'--cpus',str(args.cpus),
        '--guest-timeout','1200','--guest-result-name','player','--guest-result-root','results/l4s',
        '--destination-root','results/l4s','--destination-prefix',f'network-{out.name}',
        '--forward-sidecar-port',str(free_port(socket.SOCK_STREAM))]
    for name in ('host_cpus','host_numa_node'):
        if getattr(args,name) is not None:command += ['--'+name.replace('_','-'),str(getattr(args,name))]
    for name in ('preallocate_memory','lock_memory'):
        if getattr(args,name):command += ['--'+name.replace('_','-')]
    for path,target in [(out/'scene.tar','inputs/player-scene.tar'),(out/'adapter.tar','inputs/adapter.tar'),(out/'trace.json','inputs/capture-trace.json'),(topology,'inputs/player-topology.py'),(Path(shutil.which('node')),'inputs/node')]:
        command+=['--guest-file',f'{path}={target}']
    for setting in ['CAMERA_PRIORITY_SCENE_ARCHIVE=inputs/player-scene.tar','CAMERA_PRIORITY_NODE=inputs/node',
            'CAMERA_PLAYER_TOPOLOGY_SCRIPT=inputs/player-topology.py','CAMERA_PLAYER_CAPTURE_TRACE=inputs/capture-trace.json',
            'CAMERA_PLAYER_ADAPTER_ARCHIVE=inputs/adapter.tar',f'CAMERA_PLAYER_MODE={args.controller}',
            'CAMERA_PLAYER_DURATION=180','CAMERA_PLAYER_ORIGIN=','CAMERA_PLAYER_ADMISSION_DELAY_US=0',f'CAMERA_PLAYER_BOTTLENECK={args.bandwidth_mbps:g}mbit']:
        command+=['--make-variable',setting]
    record=dict(schema_version=1,mode='separated-network-capture',controller=args.controller,ecn_mode='ect0' if args.controller=='bbr' else 'ect1',bandwidth_mbps=args.bandwidth_mbps,
        one_way_delay_ms=args.one_way_delay_ms,base_rtt_ms=2*args.one_way_delay_ms,cpus=args.cpus,memory_mib=args.memory_mib,
        host_resources=host_resource_record(args),manifest=str(manifest),manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),trace=provenance,
        command=command,started_unix=time.time(),source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),ROOT/'tools/l4s/priority_moq_sidecar.c',ROOT/'tools/l4s/priority_ws_bridge.mjs',PLAYER/'src/network-capture-client.ts',PLAYER/'src/network-capture.ts']})
    (out/'provenance.json').write_text(json.dumps(record,indent=2)+'\n')
    with (out/'qemu.log').open('w') as log:
        child=subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        owned=process_record(child.pid,group=True)
        try:code=child.wait(timeout=1500)
        finally:
            signal.signal(signal.SIGTERM,signal.SIG_IGN)
            stop_record(owned,timeout=10)
            child.wait(timeout=5)
    if code:raise RuntimeError(f'QEMU network capture failed ({code}); see {out}/qemu.log')
    guest=completed_qemu_result(out/'qemu.log')
    capture=guest/'network-capture/arrivals.json'
    data=json.loads(capture.read_text());assert data['status']=='PASS' and data['manifest_sha256']==record['manifest_sha256']
    if data['trace_sha256']!=hashlib.sha256((out/'trace.json').read_bytes()).hexdigest():raise ValueError('guest trace differs')
    (out/'guest-evidence.json').write_text(json.dumps({'directories':[str(guest)]},indent=2)+'\n')
    shutil.copy2(capture,out/'arrivals.json')
    record.update(status='PASS',finished_unix=time.time(),objects=len(data['objects']),arrival_journal_sha256=hashlib.sha256(capture.read_bytes()).hexdigest())
    (out/'provenance.json').write_text(json.dumps(record,indent=2)+'\n')
    print(f'CAPTURE PASS: {args.controller}, {len(data["objects"])} objects; {out}',flush=True)
if __name__=='__main__':main()
