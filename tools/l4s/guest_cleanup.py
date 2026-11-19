"""Owned guest process teardown and an independent idle check for VM reuse."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def process_record(pid: int, *, group: bool = False):
    try:
        fields=(Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()
        return dict(pid=pid,start_ticks=int(fields[19]),group=group,
                    pgid=int(fields[2]),state=fields[0])
    except (FileNotFoundError,ProcessLookupError):
        return None


def alive(pid: int):
    record=process_record(pid)
    return record is not None and record['state']!='Z'


def members(record):
    current=process_record(record['pid'])
    if current and current['start_ticks']!=record['start_ticks']:
        return []  # PID was reused; never signal the replacement.
    if not record['group']:
        return [current] if current and current['state']!='Z' else []
    if record['pgid']!=record['pid']:
        raise RuntimeError('refusing to terminate an unowned process group')
    found=[]
    for path in Path('/proc').iterdir():
        if path.name.isdigit():
            child=process_record(int(path.name))
            if child and child['pgid']==record['pid'] and child['state']!='Z':
                found.append(child)
    return found


def stop_record(record, timeout: float = 5):
    if not record or not members(record):return
    def send(sig):
        try:
            if record['group']:os.killpg(record['pid'],sig)
            else:os.kill(record['pid'],sig)
        except ProcessLookupError:pass
    send(signal.SIGTERM)
    deadline=time.monotonic()+timeout
    while members(record) and time.monotonic()<deadline:time.sleep(.02)
    if members(record):send(signal.SIGKILL)
    deadline=time.monotonic()+2
    while members(record) and time.monotonic()<deadline:time.sleep(.02)
    if members(record):raise RuntimeError(f"owned processes survived cleanup: {record['pid']}")


class Ownership:
    def __init__(self,path: Path,hosts):
        self.path=path
        self.data=dict(boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                       runner=process_record(os.getpid()),
                       hosts=[process_record(node.pid) for node in hosts],processes=[])
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.data,indent=2)+'\n');temporary.replace(self.path)

    def add(self,pid: int,*,group: bool=True):
        record=process_record(pid,group=group)
        # Mininet's mnexec -d creates the session in the child after Popen.
        deadline=time.monotonic()+2
        while record and group and record['pgid']!=pid and record['state']!='Z':
            if time.monotonic()>deadline:raise RuntimeError('child did not establish its owned process group')
            time.sleep(.01);record=process_record(pid,group=group)
        if record:
            if record['state']=='Z':record['group']=False
            self.data['processes'].append(record);self.save()


def idle_state():
    root=os.readlink('/proc/1/ns/net')
    namespaces={}
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():continue
        try:
            ns=os.readlink(path/'ns/net')
            if ns!=root:namespaces.setdefault(ns,[]).append(int(path.name))
        except (FileNotFoundError,ProcessLookupError):pass
    links=json.loads(subprocess.check_output(['ip','-j','link'],text=True))
    bridges=subprocess.check_output(['ovs-vsctl','list-br'],text=True).split()
    leftovers=[item['ifname'] for item in links
               if '-eth' in item['ifname'] or item['ifname'] in ('ifb-s1','ifb-s2')]
    listeners=subprocess.check_output(['ss','-Hlnut'],text=True).splitlines()
    ports=[line for line in listeners if any(line.split()[4].endswith(':'+str(port))
                for port in (7790,8787,4443))]
    return dict(extra_network_namespaces=namespaces,bridges=bridges,
                test_interfaces=leftovers,test_listeners=ports)


def reset(state_path: Path,output: Path):
    """For a dedicated disposable test VM only; fail closed if isolation is dirty."""
    output.parent.mkdir(parents=True,exist_ok=True)
    evidence={'before':idle_state()}
    try:
        if state_path.exists():
            data=json.loads(state_path.read_text())
            boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            if data['boot_id']!=boot:raise RuntimeError('ownership belongs to another VM boot')
            runner=data['runner']
            if runner['pid']!=os.getpid():stop_record(runner,timeout=15)
            for process in reversed(data['processes']):stop_record(process,timeout=1)
            for host in data['hosts']:stop_record(host,timeout=1)
        cleanup=subprocess.run(['mn','-c'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=30)
        evidence['mininet_cleanup']=dict(exit_code=cleanup.returncode,log=cleanup.stdout)
        for device in ('ifb-s1','ifb-s2'):
            if (Path('/sys/class/net')/device).exists():
                subprocess.run(['ip','link','delete',device],check=True)
        evidence['after']=idle_state()
        if cleanup.returncode or any(evidence['after'].values()):
            raise RuntimeError('guest reset left network state behind')
        evidence['status']='PASS'
        state_path.unlink(missing_ok=True)
    except BaseException as error:
        evidence.update(status='FAIL',error=f'{type(error).__name__}: {error}')
        raise
    finally:output.write_text(json.dumps(evidence,indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();reset(args.state,args.output)
