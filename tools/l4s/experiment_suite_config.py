"""TOML configuration and deterministic case expansion for player experiments."""
from __future__ import annotations

import itertools
import math
import os
from pathlib import Path
import re
import sys
import tomllib

REPO = Path(__file__).resolve().parents[2]
SCENES = {
    'bicycle': {'manifest': 'results/player-sh3-adaptive-16k-20261014/manifest-v2.json',
                'trace': 'results/thesis-scenes-19s-20261018/bicycle-trace-19s.json'},
    'train': {'manifest': 'results/scene-videos-20261018/train-scene/manifest-v2.json',
              'trace': 'results/thesis-scenes-19s-20261018/train-trace-19s.json'},
    'playroom': {'manifest': 'results/scene-videos-20261018/playroom-scene/manifest-v2.json',
                 'trace': 'results/thesis-scenes-19s-20261018/playroom-trace-19s.json'},
}
DEFAULTS = dict(scenes=['playroom'], bandwidth_mbps=50, delays_ms=[160,320], duration_seconds=19,
                path='ideal', controllers=['prague','reno'], switches=2, reruns=1,
                background=False, background_cc='cubic', background_warmup_seconds=4,
                background_link_delay_ms=0, qemu_delay_mode='endpoints',
                on_failure='continue', max_retries=0, retry_exhausted='continue', failure_criterion='capture')
RUNNER = dict(python=sys.executable, timeout_seconds=900, attempt_timeout_seconds=3000,
              memory_limit_mib=7168, min_available_memory_gib=4, min_free_disk_gib=5)
CAPTURE = dict(width=960, height=540, fps=30, quality_fps=5, quality_backend='cuda',
               gpu_mode='hardware', headless=True, start_after_first_object=True, max_missing_snapshot_fraction=0.05)
QEMU = dict(base='~/sandbox/p4/work.qcow2', runner='tools/l4s/run_qemu_player.py', cpus=7, memory_mib=2048, performance_mode=False)
ANALYSIS = dict(enabled=True, vmaf=True, vmaf_backend='cuda', plots=True,
                vmaf_binary='results/toolchains/vmaf-cuda-src/libvmaf/build/tools/vmaf',
                vmaf_model='results/toolchains/vmaf-cuda-src/model/vmaf_v0.6.1.json')


def unknown(values, allowed, context):
    if not isinstance(values, dict):
        raise ValueError(f'{context} must be a TOML table')
    extra = set(values) - set(allowed)
    if extra:
        raise ValueError(f'unknown {context} keys: {", ".join(sorted(extra))}')


def number(value, name, minimum=0, *, strictly=False, integer=False):
    if isinstance(value, bool) or not isinstance(value, (float,int)) or not math.isfinite(value):
        raise ValueError(f'{name} must be finite numeric')
    if (value <= minimum if strictly else value < minimum) or (integer and not isinstance(value,int)):
        raise ValueError(f'invalid {name}: {value}')
    return value


def choice(value, options, name):
    if value not in options:
        raise ValueError(f'{name} must be one of {", ".join(options)}')


def resolve(value, root):
    if not isinstance(value,str) or not value:
        raise ValueError('paths must be nonempty strings')
    path = Path(value).expanduser()
    return str((root/path).resolve() if not path.is_absolute() else path.resolve())


def output_path(value, root):
    path=Path(resolve(value,root))
    if not path.is_relative_to((root/'results').resolve()) or path==(root/'results').resolve():
        raise ValueError('output must be a new directory below repository results/')
    return str(path)


def experiment(values, context):
    unknown(values, set(DEFAULTS)|{'scene','name'}, context)
    merged = {**DEFAULTS, **values}
    if 'scene' in values:
        if 'scenes' in values: raise ValueError('use scene or scenes, not both')
        merged['scenes']=[values['scene']]
        merged.pop('scene',None)
    scenes=merged['scenes']
    if not isinstance(scenes,list) or not scenes or not all(isinstance(s,str) and re.fullmatch(r'[a-zA-Z0-9_-]+',s) for s in scenes):
        raise ValueError('scenes must be a nonempty list of scene names')
    if len(set(scenes))!=len(scenes): raise ValueError('duplicate scene in experiment')
    number(merged['bandwidth_mbps'],'bandwidth_mbps',strictly=True)
    number(merged['duration_seconds'],'duration_seconds',strictly=True)
    delays=merged['delays_ms']
    if not isinstance(delays,list) or not delays: raise ValueError('delays_ms must be a nonempty list')
    for delay in delays: number(delay,'delay_ms')
    if len(set(delays))!=len(delays): raise ValueError('duplicate delay in experiment')
    choice(merged['path'],('ideal','qemu'),'path')
    if not isinstance(merged['background'],bool):raise ValueError('background must be boolean')
    number(merged['background_warmup_seconds'],'background_warmup_seconds')
    number(merged['background_link_delay_ms'],'background_link_delay_ms')
    choice(merged['qemu_delay_mode'],('endpoints','per-link'),'qemu_delay_mode')
    if not isinstance(merged['background_cc'],str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,31}',merged['background_cc']):
        raise ValueError('background_cc must be a Linux TCP congestion-control name')
    if merged['background'] and merged['path']!='qemu':raise ValueError('background traffic requires the QEMU path')
    controllers=merged['controllers']
    if not isinstance(controllers,list) or not controllers or len(set(controllers))!=len(controllers):
        raise ValueError('controllers must be a nonempty distinct list')
    for controller in controllers: choice(controller,('reno','prague'),'controller')
    for field,minimum in (('switches',1),('reruns',1),('max_retries',0)):
        number(merged[field],field,minimum,integer=True)
    if merged['background'] and merged['switches']>254:raise ValueError('background supports at most 254 switches')
    choice(merged['on_failure'],('continue','stop','retry'),'on_failure')
    choice(merged['retry_exhausted'],('continue','stop'),'retry_exhausted')
    choice(merged['failure_criterion'],('capture','quality','all'),'failure_criterion')
    return merged


def load_config(path: Path, repo_root: Path=REPO):
    root=repo_root.resolve()
    with Path(path).open('rb') as stream: raw=tomllib.load(stream)
    unknown(raw,('version','output','defaults','runner','capture','qemu','analysis','scenes','experiments'),'configuration')
    if raw.get('version',1)!=1: raise ValueError('unsupported configuration version')
    cfg={'version':1,'repo_root':str(root),'config_path':str(Path(path).resolve()),
         'output':output_path(raw.get('output','results/player-experiments'),root)}
    defaults=raw.get('defaults',{})
    unknown(defaults,set(DEFAULTS)|{'scene'},'defaults')
    entries=raw.get('experiments',[{}])
    if not isinstance(entries,list) or not entries: raise ValueError('experiments must be a nonempty array of tables')
    cfg['experiments']=[]
    for index,entry in enumerate(entries):
        unknown(entry,set(DEFAULTS)|{'scene','name'},f'experiment {index}')
        merged={**defaults,**entry}
        if 'scene' in entry: merged.pop('scenes',None)
        elif 'scenes' in entry: merged.pop('scene',None)
        cfg['experiments'].append(experiment(merged,f'experiment {index}'))
    for name,base in (('runner',RUNNER),('capture',CAPTURE),('qemu',QEMU),('analysis',ANALYSIS)):
        table=raw.get(name,{})
        unknown(table,base,name);cfg[name]={**base,**table}
    for field in ('width','height','fps'):
        number(cfg['capture'][field],field,1,integer=True)
    number(cfg['capture']['quality_fps'],'quality_fps',strictly=True)
    if cfg['capture']['quality_fps']>60: raise ValueError('quality_fps must be <=60')
    for name in ('quality_backend','gpu_mode'):
        choice(cfg['capture'][name],('cpu','cuda') if name=='quality_backend' else ('hardware','software'),name)
    for field in ('headless','start_after_first_object'):
        if not isinstance(cfg['capture'][field],bool):raise ValueError(f'{field} must be boolean')
    number(cfg['capture']['max_missing_snapshot_fraction'],'max_missing_snapshot_fraction')
    if cfg['capture']['max_missing_snapshot_fraction']>=1:raise ValueError('max_missing_snapshot_fraction must be <1')
    for field in ('enabled','vmaf','plots'):
        if not isinstance(cfg['analysis'][field],bool):raise ValueError(f'analysis.{field} must be boolean')
    choice(cfg['analysis']['vmaf_backend'],('cuda','cpu'),'vmaf_backend')
    for field in ('timeout_seconds','attempt_timeout_seconds'):
        number(cfg['runner'][field],field,strictly=True)
    for field in ('memory_limit_mib',):number(cfg['runner'][field],field,integer=True)
    for field in ('min_available_memory_gib','min_free_disk_gib'):number(cfg['runner'][field],field)
    if not isinstance(cfg['qemu']['performance_mode'],bool):raise ValueError('qemu.performance_mode must be boolean')
    if cfg['qemu']['performance_mode'] and cfg['qemu']['cpus']!=7:raise ValueError('QEMU performance mode requires seven vCPUs')
    for field in ('cpus','memory_mib'):number(cfg['qemu'][field],field,1,integer=True)
    # Keep the virtualenv entrypoint: resolving its symlink would select the
    # system interpreter and discard the environment's installed CUDA packages.
    interpreter=cfg['runner']['python']
    if not isinstance(interpreter,str) or not interpreter:
        raise ValueError('runner.python must be a nonempty path')
    cfg['runner']['python']=os.path.abspath(root/Path(interpreter).expanduser())
    for field in ('base','runner'):cfg['qemu'][field]=resolve(cfg['qemu'][field],root)
    for field in ('vmaf_binary','vmaf_model'):cfg['analysis'][field]=resolve(cfg['analysis'][field],root)
    catalog={name:dict(value) for name,value in SCENES.items()}
    custom=raw.get('scenes',{})
    if not isinstance(custom,dict):raise ValueError('scenes must be a table')
    for name,entry in custom.items():
        if not re.fullmatch(r'[a-zA-Z0-9_-]+',name):raise ValueError('invalid scene name')
        unknown(entry,('manifest','trace'),f'scene {name}')
        catalog[name]={**catalog.get(name,{}),**entry}
    cfg['scenes']={}
    selected={name for item in cfg['experiments'] for name in item['scenes']}
    for name in selected:
        if name not in catalog or set(catalog[name])!={'manifest','trace'}:raise ValueError(f'unknown or incomplete scene {name}')
        cfg['scenes'][name]={key:resolve(value,root) for key,value in catalog[name].items()}
    return cfg


def expand_cases(cfg):
    cases=[]
    for index,entry in enumerate(cfg['experiments']):
        delays=sorted(entry['delays_ms'],reverse=True) if entry['path']=='ideal' else entry['delays_ms']
        controllers=([c for c in ('prague','reno') if c in entry['controllers']]
                     if entry['path']=='qemu' else ['ideal'])
        variants=list(itertools.product(entry['scenes'],delays,controllers))
        for repeat in range(entry['reruns']):
            # Keep each requested comparison adjacent, including across reruns.
            for scene,delay,controller in variants:
                switches=entry['switches'] if entry['path']=='qemu' else 0
                identifier=(f'e{index+1:02d}-{scene}-{entry["path"]}-{controller}-'
                            f'{entry["bandwidth_mbps"]:g}m-{delay:g}ms-{entry["duration_seconds"]:g}s-s{switches}-r{repeat+1:02d}')
                if entry['background']:identifier+=f'-bg-{entry["background_cc"]}'
                cases.append({**entry,'id':identifier,'scene':scene,'delay_ms':delay,'controller':controller,
                              'switches':switches,'rerun':repeat+1,'experiment_index':index,
                              **cfg['scenes'][scene]})
    return cases
