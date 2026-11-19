#!/usr/bin/env python3
"""Render a completed network-arrival journal on an independent logical clock."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import signal
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode
from run_camera_trace_player import ROOT, PLAYER, browser_command, free_port, wait_port, validate_trace
from guest_cleanup import process_record, stop_record


def validate_replay_inputs(manifest,trace,journal):
    data=json.loads(Path(journal).read_text())
    if data.get('status')!='PASS' or data.get('clock')!='guest-monotonic':raise ValueError('capture not complete')
    if hashlib.sha256(Path(manifest).read_bytes()).hexdigest()!=data['manifest_sha256']:raise ValueError('manifest hash mismatch')
    if hashlib.sha256(Path(trace).read_bytes()).hexdigest()!=data.get('trace_sha256'):raise ValueError('trace hash mismatch')
    frames=validate_trace(Path(trace))
    if frames[-1]['timestamp_ms']!=data['duration_ms']:raise ValueError('trace horizon mismatch')
    return data


def audit_frames(journal,telemetry,frames,manifest,replay_summary):
    arrivals={(r['tile_id'],r['refinement'],r['object_id']):r['arrival_us'] for r in journal['objects']}
    rendered=[e for e in telemetry['events'] if e['type']=='render-frame']
    if len(rendered)!=len(frames['frames']):raise ValueError('rendered frame count mismatch')
    dispatches={r['frame_index']:r['objects'] for r in replay_summary['frame_dispatches'] if r['frame_index']>=0}
    applied=set();dispatched=set();gaussians={}
    for tile in manifest['tiles']:
        for layer in tile['refinements']:
            for obj in layer['objects']:gaussians[tile['id'],layer['index'],obj['id']]=obj['gaussian_count']
    for index,(event,frame) in enumerate(zip(rendered,frames['frames'])):
        if event['timestampMs']!=frame['timestampMs']:raise ValueError('frame timestamp mismatch')
        for row in dispatches.get(index,[]):
            key=tuple(row)
            if key in dispatched:raise ValueError('duplicate replay dispatch')
            dispatched.add(key)
        eligible={key for key,arrival in arrivals.items() if arrival<=event['timestampMs']*1000}
        if dispatched!=eligible:raise ValueError('dispatch availability differs from arrival journal')
        expected=set()
        for tile in manifest['tiles']:
            for layer in sorted(tile['refinements'],key=lambda l:l['index']):
                keys={(tile['id'],layer['index'],obj['id']) for obj in layer['objects']}
                complete=keys<=eligible
                if manifest.get('payload_encoding')=='gsp2' or complete:expected.update(keys & eligible)
                if not complete:break
        for identifier in event['objectsAdded']:
            _,tile,layer,obj=identifier.split(':');key=(tile,int(layer),int(obj))
            if key in applied:raise ValueError('object applied twice')
            if arrivals[key]>event['timestampMs']*1000:raise ValueError('future object in rendered frame')
            applied.add(key)
        if applied!=expected:raise ValueError('applied membership differs from dependency-ready arrivals')
        if frame['appliedGaussians']!=sum(gaussians[key] for key in expected):raise ValueError('applied Gaussian count mismatch')
    return {'status':'PASS','frames':len(rendered),'applied_objects_in_video':len(applied),'no_future_objects':True,'all_eligible_objects_dispatched':True,'dependency_ready_membership_verified':True,'logical_clock_independent_of_wall_clock':True}


def main():
    def interrupted(signum,frame):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,interrupted)
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['manifest','trace','journal','output']:parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--fps',type=int,default=5);parser.add_argument('--width',type=int,default=960);parser.add_argument('--height',type=int,default=540)
    parser.add_argument('--gpu-mode',choices=['hardware','software'],default='hardware');parser.add_argument('--skip-reference',action='store_true')
    args=parser.parse_args();out=args.output.resolve();manifest=args.manifest.resolve();trace=args.trace.resolve();journal=args.journal.resolve()
    if out.exists() or not out.is_relative_to(ROOT/'results'):raise ValueError('output must be new under results')
    data=validate_replay_inputs(manifest,trace,journal)
    if min(args.fps,args.width,args.height)<=0:raise ValueError('invalid frame dimensions/FPS')
    out.mkdir(parents=True);shutil.copytree(PLAYER/'dist',out/'player-ui')
    config=dict(manifest=str(manifest),journal=str(journal),output=str(out))
    (out/'replay-config.json').write_text(json.dumps(config,indent=2)+'\n')
    web_port,bridge_port=free_port(socket.SOCK_STREAM),free_port(socket.SOCK_STREAM)
    children=[];logs=[]
    def start(name,command,env=None):
        log=(out/f'{name}.log').open('w');logs.append(log)
        child=subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,env=env,start_new_session=True);children.append((child,process_record(child.pid,group=True)));return child
    began=time.time()
    try:
        web=start('web',[sys.executable,str(ROOT/'tools/l4s/camera_trace_player_server.py'),'--dist',str(out/'player-ui'),'--manifest',str(manifest),'--trace',str(trace),'--port',str(web_port),'--metadata-only'])
        wait_port(web_port,[web])
        bridge=start('replay-bridge',['node',str(ROOT/'tools/l4s/priority_ws_bridge.mjs'),'--replay-config',str(out/'replay-config.json'),'--listen',str(bridge_port),'--origin',f'http://127.0.0.1:{web_port}'],env={**os.environ,'IDEAL_LINK_PYTHON':sys.executable})
        wait_port(bridge_port,[web,bridge])
        url=f'http://127.0.0.1:{web_port}/?'+urlencode(dict(transport='replay',sidecar=f'ws://127.0.0.1:{bridge_port}',trace='/camera-trace.json',autoplay=1,fixedHorizon=1,record=0,recordFps=args.fps,overlays=0,dye=0))
        command=browser_command(url,out/'unused.webm',out/'player-telemetry.json',timeout_ms=900000,width=args.width,height=args.height,headless=True,record=False,gpu_mode=args.gpu_mode)
        command+=['--capture-frames',str(out/'candidate-frames'),'--capture-fps',str(args.fps),'--capture-mode','render-loop','--capture-camera-timestamps','fixed']
        browser=start('browser',command)
        if browser.wait(timeout=1000):raise RuntimeError('offline replay browser failed')
        frames=json.loads((out/'candidate-frames/frames.json').read_text());telemetry=json.loads((out/'player-telemetry.json').read_text())
        data=json.loads(journal.read_text());expected=math.ceil(data['duration_ms']*args.fps/1000-1e-9)
        if not frames['complete'] or len(frames['frames'])!=expected or telemetry['failure']:raise ValueError('incomplete replay frames')
        audit=audit_frames(data,telemetry,frames,json.loads(manifest.read_text()),json.loads((out/'replay-summary.json').read_text()));(out/'membership-audit.json').write_text(json.dumps(audit,indent=2)+'\n')
        render_finished=time.time()
        result=dict(status='PASS',mode='recorded-arrivals-v1',frames=expected,fps=args.fps,render_wall_seconds=render_finished-began,
            journal=str(journal),journal_sha256=hashlib.sha256(journal.read_bytes()).hexdigest(),manifest=str(manifest),trace=str(trace),rendering_complete_unix=render_finished,
            renderer_gpu_mode=args.gpu_mode,membership=audit)
        # Reference and scoring are optional so resource tests can isolate candidate replay.
        if not args.skip_reference:
            ref_config=dict(cache_root=str(out.parent/'references'),manifest=str(manifest),trace=str(trace),dist=str(out/'player-ui'),fps=args.fps,width=args.width,height=args.height,gpu_mode=args.gpu_mode,headless=True,timeout_seconds=900)
            code="import sys,json;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from render_trace_reference import cached_trace_reference;c=json.loads(sys.argv[2]);c.update({k:Path(c[k]) for k in ['cache_root','manifest','trace','dist']});r=cached_trace_reference(**c);Path(sys.argv[3]).write_text(str(r))"
            worker=start('reference-worker',[sys.executable,'-c',code,str(ROOT/'tools/l4s'),json.dumps(ref_config),str(out/'reference-cache-path.txt')])
            if worker.wait(timeout=1000):raise RuntimeError('reference worker failed')
            reference=Path((out/'reference-cache-path.txt').read_text())
            for name in ['reference-frames','reference-telemetry.json','reference-summary.json','reference.mp4']:(out/name).symlink_to(os.path.relpath(reference/name,out),target_is_directory=(name=='reference-frames'))
            post_code="import sys,json;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from compare_player_sequence import compare_sequence;from snapshot_video import encode_snapshot_video;o=Path(sys.argv[2]);q=compare_sequence(o/'candidate-frames/frames.json',o/'reference-frames/frames.json',o/'quality',backend=sys.argv[3]);assert q['status']=='PASS',q;encode_snapshot_video(o/'candidate-frames/frames.json',o/'camera-trace.mp4',fps=int(sys.argv[4]))"
            post=start('quality-worker',[sys.executable,'-c',post_code,str(ROOT/'tools/l4s'),str(out),'cuda' if args.gpu_mode=='hardware' else 'cpu',str(args.fps)])
            if post.wait(timeout=1000):raise RuntimeError('replay quality/video worker failed')
            result['quality_status']='PASS'
        result['total_wall_seconds']=time.time()-began
        (out/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
        print(f'REPLAY PASS: {expected} frames in {render_finished-began:.1f}s; {out}',flush=True)
    finally:
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        for child,record in reversed(children):
            stop_record(record,timeout=5)
            child.wait(timeout=5)
        for log in logs:log.close()
if __name__=='__main__':main()
