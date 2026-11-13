#!/usr/bin/env python3
"""Run configurable scene experiments or regenerate their offline plots.

See experiments/player.toml and docs/player-experiments.md. --dry-run never
starts a browser, native endpoint, QEMU, or network topology.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from experiment_suite_config import REPO, expand_cases, load_config, output_path
from experiment_suite_topology import topology_source


def write_json(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.tmp')
    temporary.write_text(json.dumps(value,indent=2,default=str)+'\n')
    temporary.replace(path)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scale_trace(source: Path, target: Path, duration_seconds: float):
    frames=json.loads(source.read_text())
    if not isinstance(frames,list) or len(frames)<2:raise ValueError('trace must contain at least two poses')
    timestamps=[frame['timestamp_ms'] for frame in frames]
    if any(isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) for value in timestamps):
        raise ValueError('trace timestamps must be finite')
    if timestamps[0]!=0 or any(b<=a for a,b in zip(timestamps,timestamps[1:])):
        raise ValueError('trace timestamps must start at zero and increase')
    factor=duration_seconds*1000/timestamps[-1]
    scaled=[{**frame,'timestamp_ms':timestamp*factor} for frame,timestamp in zip(frames,timestamps)]
    scaled[-1]['timestamp_ms']=duration_seconds*1000
    write_json(target,scaled)
    return dict(source=str(source),source_sha256=sha(source),scaled_sha256=sha(target),poses=len(frames),
                original_duration_seconds=timestamps[-1]/1000,duration_seconds=duration_seconds,
                playback_speed_factor=timestamps[-1]/1000/duration_seconds,spatial_poses_preserved=True)


def build_command(case,cfg,attempt_dir,trace_path,topology_path=None):
    capture=cfg['capture'];qemu=cfg['qemu']
    command=[cfg['runner']['python'],str(Path(cfg['repo_root'])/'tools/l4s/run_camera_trace_player.py'),
             '--manifest',case['manifest'],'--trace',str(trace_path),'--output',str(attempt_dir),
             '--quality','--quality-fps',str(capture['quality_fps']),'--quality-camera-timestamps','fixed',
             '--quality-backend',capture['quality_backend'],'--gpu-mode',capture['gpu_mode'],
             '--max-missing-snapshot-fraction',str(capture['max_missing_snapshot_fraction']),
             '--width',str(capture['width']),'--height',str(capture['height']),'--fps',str(capture['fps']),
             '--format','mp4','--no-dye','--fixed-horizon','--timeout-seconds',str(cfg['runner']['timeout_seconds'])]
    if capture['headless']:command.append('--headless')
    if capture['start_after_first_object']:command.append('--start-after-first-object')
    if case['path']=='ideal':
        command += ['--transport','ideal','--ideal-bandwidth-mbps',str(case['bandwidth_mbps']),
                    '--ideal-data-delay-ms',str(case['delay_ms'])]
    else:
        if topology_path is None:raise ValueError('QEMU case requires generated topology')
        command += ['--transport','moqt','--network-path','qemu','--transport-mode',case['controller'],
                    '--qemu-base',qemu['base'],'--qemu-runner',
                    (str(Path(cfg['repo_root'])/'tools/l4s/qemu_player_performance.py') if qemu['performance_mode'] else qemu['runner']),
                    '--qemu-cpus',str(qemu['cpus']),'--qemu-memory',str(qemu['memory_mib']),
                    '--topology-script',str(topology_path),'--bottleneck',f'{case["bandwidth_mbps"]:g}mbit']
    return command


def make_plan(cfg):
    batch=Path(cfg['output']);items=[]
    for case in expand_cases(cfg):
        inputs=batch/'inputs'/case['id']
        attempt=batch/'runs'/case['id']/'attempt-001'
        topology=inputs/'topology.py' if case['path']=='qemu' else None
        items.append({**case,'attempts':[],'selected_attempt':None,
                      'command':build_command(case,cfg,attempt,inputs/'trace.json',topology)})
    return dict(schema_version=1,config=cfg,items=items,execution_status='planned',status='NOT_RUN',
                delay_semantics={'ideal':'data-only one-way propagation; controls immediate',
                                 'qemu':'qemu_delay_mode selects endpoint total or per-physical-main-link one-way delay; background access links add background_link_delay_ms each direction'},
                topology_runtime_status='UNVERIFIED' if any(c['path']=='qemu' for c in items) else 'NOT_APPLICABLE')


def resources(cfg):
    settings=cfg['runner'];root=Path(cfg['repo_root'])
    memory_file=Path('/proc/meminfo')
    if memory_file.exists():
        available=next(int(line.split()[1])*1024 for line in memory_file.read_text().splitlines() if line.startswith('MemAvailable:'))
        if available<settings['min_available_memory_gib']*1024**3:
            raise RuntimeError('available memory below runner.min_available_memory_gib')
    if shutil.disk_usage(root).free<settings['min_free_disk_gib']*1024**3:
        raise RuntimeError('free disk below runner.min_free_disk_gib')


def process_executor(cfg):
    def execute(command,log,timeout):
        invocation=list(command);unit=None
        if cfg['runner']['memory_limit_mib']:
            if not shutil.which('systemd-run'):raise RuntimeError('systemd-run unavailable; set runner.memory_limit_mib=0 to opt out')
            unit=f'player-suite-{os.getpid()}-{time.monotonic_ns()}'
            invocation=['systemd-run','--user','--scope','--quiet',f'--unit={unit}',
                        '-p',f'MemoryMax={cfg["runner"]["memory_limit_mib"]}M','-p','MemorySwapMax=0',*invocation]
        process=None
        try:
            with log.open('w') as stream:
                process=subprocess.Popen(invocation,cwd=cfg['repo_root'],stdout=stream,stderr=subprocess.STDOUT,
                                         start_new_session=True,env={**os.environ,'PYTHONUNBUFFERED':'1'})
                return process.wait(timeout=timeout)
        except BaseException:
            # Terminate only this attempt's process group/scope, never unrelated experiments.
            if process is not None and process.poll() is None:
                os.killpg(process.pid,signal.SIGTERM)
                if unit:
                    try:
                        subprocess.run(['systemctl','--user','stop',unit+'.scope'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
                    except (OSError, subprocess.TimeoutExpired):
                        pass  # Still reap/kill the owned process group below.
                try:process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=15)
            raise
    return execute


def classify(directory,code,criterion,error=None):
    path=directory/'validation-summary.json'
    try:summary=json.loads(path.read_text()) if path.exists() else {}
    except (OSError,ValueError) as failure:
        summary={};error=error or f'cannot read validation summary: {failure}'
    video=summary.get('video_status','MISSING')
    quality=summary.get('quality_status','MISSING')
    timing=summary.get('model_status','NOT_APPLICABLE')
    capture=video=='PASS' and (directory/'camera-trace.mp4').is_file() and not error
    acceptable=capture
    if criterion=='quality':acceptable=capture and quality=='PASS'
    elif criterion=='all':acceptable=capture and quality=='PASS' and summary.get('status')=='PASS' and code==0
    full=capture and quality=='PASS' and summary.get('status')=='PASS' and code==0
    return dict(process_exit_code=code,capture_status='PASS' if capture else 'FAIL',quality_status=quality,
                timing_status=timing,status='PASS' if full else 'PARTIAL' if capture else 'FAIL',
                criterion_pass=bool(acceptable),error=error)


def refresh_status(ledger):
    selected=[];criterion_failed=False
    for item in ledger['items']:
        index=item.get('selected_attempt')
        if index is not None:
            attempt=next(a for a in item['attempts'] if a['index']==index)
            selected.append(attempt)
            criterion_failed |= not attempt['criterion_pass']
    if ledger['execution_status'] in ('stopped','interrupted') or criterion_failed:
        ledger['status']='FAIL'
    elif len(selected)!=len(ledger['items']):ledger['status']='PARTIAL' if selected else 'NOT_RUN'
    elif all(a['status']=='PASS' for a in selected):ledger['status']='PASS'
    else:ledger['status']='PARTIAL'


def save(batch,ledger):
    refresh_status(ledger)
    write_json(batch/'ledger.json',ledger)
    lines=['# Player experiment suite','',f'Execution: **{ledger["execution_status"]}**. Overall evidence: **{ledger["status"]}**.','',
           'Capture, quality, and modeled timing are tracked separately. A configured failure criterion controls retries; it never changes recorded source statuses.','',
           '| Item | Repeat | Capture | Quality | Timing | Attempt |','|---|---:|---|---|---|---|']
    for item in ledger['items']:
        selected=next((a for a in item['attempts'] if a['index']==item['selected_attempt']),None)
        if selected:
            lines.append(f'| {item["id"]} | {item["rerun"]} | {selected["capture_status"]} | {selected["quality_status"]} | {selected["timing_status"]} | [{selected["index"]}]({selected["directory"]}/) |')
        else:lines.append(f'| {item["id"]} | {item["rerun"]} | NOT_RUN | — | — | — |')
    lines += ['', '- [Resolved configuration](config.json)', '- [All attempts and exact commands](ledger.json)']
    if (batch/'analysis/README.md').is_file():
        lines.append('- [Automated analysis and plots](analysis/README.md)')
    lines += ['',
              'Ideal delays apply to data only with immediate controls. QEMU delays follow qemu_delay_mode: endpoints means total one-way, per-link means one-way on each physical main-path link; non-client-facing links are 300 Mbps, client-facing link uses the configured bottleneck. Enabled background traffic has one independent TCP source/sink per switch sharing only that forward hop, each targeting the bottleneck rate with the recorded background_cc. Each background-only access link adds background_link_delay_ms in each direction; each background path has two such links.', '',
              'Browser object prevalidation remains disabled as requested; workers prepare GSP2 objects. Offline quality and timing checks remain separate. Missing samples are never filled.']
    if ledger.get('analysis_error'):lines += ['',f'Analysis error: {ledger["analysis_error"]}']
    (batch/'README.md').write_text('\n'.join(lines)+'\n')


def run_batch(cfg,executor=None):
    batch=Path(cfg['output'])
    if batch.exists():raise FileExistsError(f'output already exists: {batch}; choose a new output')
    ledger=make_plan(cfg)
    for item in ledger['items']:
        for field in ('manifest','trace'):
            if not Path(item[field]).is_file():raise FileNotFoundError(f'{item["scene"]} {field}: {item[field]}')
    batch.mkdir(parents=True)
    write_json(batch/'config.json',cfg)
    shutil.copyfile(cfg['config_path'],batch/'config.toml')
    write_json(batch/'suite-source-provenance.json', {
        str(path.relative_to(REPO)): sha(path)
        for path in (Path(__file__), REPO/'tools/l4s/experiment_suite_config.py',
                     REPO/'tools/l4s/experiment_suite_topology.py', REPO/'tools/l4s/experiment_suite_analysis.py')})
    ledger['execution_status']='running';ledger['started_unix']=time.time();save(batch,ledger)
    execute=executor or process_executor(cfg)
    stop=False
    try:
        for item in ledger['items']:
            inputs=batch/'inputs'/item['id'];inputs.mkdir(parents=True)
            report=scale_trace(Path(item['trace']),inputs/'trace.json',item['duration_seconds'])
            report['manifest_sha256']=sha(Path(item['manifest']))
            write_json(inputs/'provenance.json',report)
            topology=None
            if item['path']=='qemu':
                topology=inputs/'topology.py'
                topology.write_text(topology_source(item['switches'],item['bandwidth_mbps'],item['delay_ms'],
                    background=item['background'],background_cc=item['background_cc'],
                    background_warmup_seconds=item['background_warmup_seconds'],
                    background_link_delay_ms=item['background_link_delay_ms'],
                    performance_mode=cfg['qemu']['performance_mode'],
                    propagation_mode=item['qemu_delay_mode']))
            count=1+item['max_retries'] if item['on_failure']=='retry' else 1
            for index in range(1,count+1):
                relative=Path('runs')/item['id']/f'attempt-{index:03d}'
                directory=batch/relative;directory.parent.mkdir(parents=True,exist_ok=True)
                command=build_command(item,cfg,directory,inputs/'trace.json',topology)
                log=directory.with_suffix('.log')
                write_json(directory.with_name(directory.name+'-command.json'),command)
                attempt=dict(index=index,directory=str(relative),command=command,started_unix=time.time(),
                             process_exit_code=None,capture_status='RUNNING',quality_status='MISSING',timing_status='NOT_APPLICABLE',
                             status='RUNNING',criterion_pass=False,error=None)
                item['attempts'].append(attempt);item['selected_attempt']=index;save(batch,ledger)
                print(f'RUN {item["id"]} attempt {index}/{count}',flush=True)
                code=None;error=None
                try:
                    resources(cfg)
                    if executor is None and item['path']=='qemu':
                        for field in ('base','runner'):
                            if not Path(cfg['qemu'][field]).is_file():raise FileNotFoundError(f'QEMU {field}: {cfg["qemu"][field]}')
                    code=execute(command,log,cfg['runner']['attempt_timeout_seconds'])
                except KeyboardInterrupt:
                    attempt.update(classify(directory,code,item['failure_criterion'],'interrupted'))
                    attempt['finished_unix']=time.time();ledger['execution_status']='interrupted';save(batch,ledger)
                    return ledger
                except Exception as failure:error=f'{type(failure).__name__}: {failure}'
                attempt.update(classify(directory,code,item['failure_criterion'],error));attempt['finished_unix']=time.time()
                # Failed launchers may not create their result directory.
                directory.mkdir(parents=True,exist_ok=True)
                write_json(directory/'suite-attempt.json',attempt);save(batch,ledger)
                print(f'END {item["id"]}: {attempt["status"]}, criterion={attempt["criterion_pass"]}',flush=True)
                if attempt['criterion_pass']:break
                if index<count:continue
                stop=item['on_failure']=='stop' or (item['on_failure']=='retry' and item['retry_exhausted']=='stop')
            if stop:break
        ledger['execution_status']='stopped' if stop else 'complete'
    except KeyboardInterrupt:
        ledger['execution_status']='interrupted'
    except Exception as error:
        ledger['execution_status']='stopped';ledger['setup_error']=f'{type(error).__name__}: {error}'
    ledger['finished_unix']=time.time();save(batch,ledger)
    if cfg['analysis']['enabled']:
        analyze(batch,ledger)
    return ledger


def analyze(batch,ledger):
    try:
        from experiment_suite_analysis import analyze_batch
        ledger['analysis']=analyze_batch(batch,ledger,ledger['config']['analysis'])
        ledger.pop('analysis_error',None)
    except Exception as error:
        ledger['analysis_error']=f'{type(error).__name__}: {error}'
        ledger['analysis']={'status':'FAIL'}
    save(batch,ledger)


def analysis_failed(ledger):
    result=ledger.get('analysis',{})
    return bool(ledger.get('analysis_error') or result.get('status') in ('FAIL','BLOCKED') or
                any(group.get('vmaf_status') == 'BLOCKED' or group.get('plots_status') == 'BLOCKED'
                    for group in result.get('groups',[])))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--output',help='new output directory; overrides TOML output')
    parser.add_argument('--dry-run',action='store_true',help='print expanded commands without running or creating output')
    parser.add_argument('--plot-only',type=Path,help='regenerate analysis from an existing suite batch, without captures')
    args=parser.parse_args(argv)
    try:
        if args.plot_only:
            if args.dry_run or args.config or args.output:parser.error('--plot-only cannot combine with configuration/run options')
            batch=args.plot_only.resolve();ledger=json.loads((batch/'ledger.json').read_text());analyze(batch,ledger)
            print(json.dumps(ledger.get('analysis',{}),indent=2));return 1 if analysis_failed(ledger) else 0
        if not args.config:parser.error('--config is required unless --plot-only is used')
        cfg=load_config(args.config)
        if args.output:cfg['output']=output_path(args.output,Path(cfg['repo_root']))
        if args.dry_run:
            print(json.dumps(make_plan(cfg),indent=2));return 0
        ledger=run_batch(cfg)
        print(f'{ledger["status"]}: {cfg["output"]}',flush=True)
        if ledger['execution_status']=='interrupted':return 130
        if ledger['execution_status']!='complete' or any(not a['criterion_pass'] for item in ledger['items'] for a in item['attempts'] if a['index']==item['selected_attempt']):return 1
        return 1 if analysis_failed(ledger) else 0
    except (ValueError,OSError,KeyError) as error:
        print(f'experiment suite: {error}',file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
