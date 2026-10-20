"""Reserve guest CPU2 for OVS control and switch receive processing."""
import json
import os
from pathlib import Path
import subprocess
import time


def tasks(pid):
    return list((Path('/proc')/str(pid)/'task').iterdir())


def reserve_cpu(output):
    assert len(os.sched_getaffinity(0))==7,'Need seven online CPUs'
    moved=[]
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():continue
        try:
            flags=int((proc/'stat').read_text().rsplit(')',1)[1].split()[6])
            if flags & 0x00200000:continue # PF_KTHREAD: keep per-CPU kernel threads intact.
            for task in tasks(proc.name):
                os.sched_setaffinity(int(task.name),{3})
            moved.append(int(proc.name))
        except (FileNotFoundError,ProcessLookupError):pass
    (output/'guest-housekeeping-affinity.json').write_text(json.dumps(dict(user_processes=moved,housekeeping_cpus=[3],network_cpu=2),indent=2)+'\n')


def configure_switch(devices,output):
    from run_camera_player_guest import device_command
    pids=subprocess.check_output(['pgrep','-x','ovs-vswitchd|ovsdb-server'],text=True).split()
    assert pids,'OVS daemon missing'
    priorities=[]
    for pid in pids:
        for task in tasks(pid):
            tid=int(task.name);os.sched_setaffinity(tid,{2});os.setpriority(os.PRIO_PROCESS,tid,-10)
            priorities.append(dict(pid=int(pid),tid=tid,affinity=sorted(os.sched_getaffinity(tid)),nice=os.getpriority(os.PRIO_PROCESS,tid)))
    kernel_workers=[]
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():continue
        try:
            name=(proc/'comm').read_text().strip()
            if name not in [f'ksoftirqd/{cpu}' for cpu in [0,1,2,4,5,6]]:continue
            pid=int(proc.name);cpu=int(name.rsplit('/',1)[1])
            os.setpriority(os.PRIO_PROCESS,pid,-10)
            affinity=sorted(os.sched_getaffinity(pid))
            assert affinity==[cpu]
            kernel_workers.append(dict(pid=pid,name=name,cpu=cpu,affinity=affinity,nice=os.getpriority(os.PRIO_PROCESS,pid)))
        except (FileNotFoundError,ProcessLookupError):pass
    assert len(kernel_workers)==6
    assert all(t['nice']==-10 for t in kernel_workers)
    steering=[]
    seen=set()
    for role,(node,interface) in devices.items():
        if interface=='ifb0' or interface in seen:continue
        seen.add(interface)
        cpu=0 if role=='server_egress' else 1 if role=='client_egress' else 2
        # Other endpoint roles are idle; keep them off the network CPU.
        if role in ('background_sink_egress','background_source_egress','background_sink2_egress'):
            cpu={'background_sink_egress':5,'background_source_egress':4,'background_sink2_egress':6}[role]
        code="""import json,sys
from pathlib import Path
interface,mask=sys.argv[1:];result={}
paths=list((Path('/sys/class/net')/interface/'queues').glob('rx-*/rps_cpus'))
assert paths,'RPS support missing'
for path in paths:
 path.write_text(mask);result[str(path)]=path.read_text().strip()
print(json.dumps(result))
"""
        actual=json.loads(device_command(node,['python3','-c',code,interface,format(1<<cpu,'x')]))
        assert all(int(v.replace(',',''),16)==1<<cpu for v in actual.values())
        steering.append(dict(role=role,cpu=cpu,actual=actual))
    assert all(r['affinity']==[2] and r['nice']==-10 for r in priorities)
    (output/'switch-priority.json').write_text(json.dumps(dict(ovs_threads=priorities,kernel_workers=kernel_workers,rps=steering),indent=2)+'\n')


def verify_endpoints(publisher,subscriber,output):
    deadline=time.monotonic()+2
    while True:
        result=[]
        for pid,cpu in [(publisher,0),(subscriber,1)]:
            for task in tasks(pid):
                tid=int(task.name)
                result.append(dict(pid=pid,tid=tid,expected_cpu=cpu,affinity=sorted(os.sched_getaffinity(tid)),nice=os.getpriority(os.PRIO_PROCESS,tid)))
        if result and all(r['affinity']==[r['expected_cpu']] and r['nice']==-10 for r in result):break
        assert time.monotonic()<deadline,result
        time.sleep(.01)
    (output/'endpoint-affinity.json').write_text(json.dumps(result,indent=2)+'\n')


def verify_runtime(publisher,subscriber,output):
    verify_endpoints(publisher,subscriber,output)
    verify_background(output)
    evidence=json.loads((output/'switch-priority.json').read_text())
    rows=[]
    for pid in sorted({item['pid'] for item in evidence['ovs_threads']}):
        assert Path('/proc',str(pid)).exists(),'OVS daemon exited'
        observed=0
        for task in tasks(pid):
            tid=int(task.name)
            try:
                actual=sorted(os.sched_getaffinity(tid));nice=os.getpriority(os.PRIO_PROCESS,tid)
            except ProcessLookupError:continue # Helper threads may exit between proc enumeration and readback.
            assert actual==[2] and nice==-10,(tid,actual,nice)
            rows.append(dict(tid=tid,affinity=actual,nice=nice,role='ovs'))
            observed+=1
        assert observed,'No live OVS threads'
    for item in evidence['kernel_workers']:
        tid=item['pid'];actual=sorted(os.sched_getaffinity(tid));nice=os.getpriority(os.PRIO_PROCESS,tid)
        assert actual==item['affinity'] and nice==-10
        rows.append(dict(tid=tid,affinity=actual,nice=nice,role='ksoftirqd'))
    with (output/'runtime-priority.jsonl').open('a') as log:
        log.write(json.dumps(dict(monotonic_ns=time.monotonic_ns(),workers=rows,endpoints=json.loads((output/'endpoint-affinity.json').read_text())))+'\n')


def verify_background(output):
    manifest=json.loads((output/'background-processes.json').read_text())
    rows=[]
    for item in manifest:
        for task in tasks(item['pid']):
            tid=int(task.name);affinity=sorted(os.sched_getaffinity(tid));nice=os.getpriority(os.PRIO_PROCESS,tid)
            assert affinity==[item['cpu']] and nice==-10,(item,tid,affinity,nice)
            rows.append(dict(tid=tid,pid=item['pid'],expected_cpu=item['cpu'],affinity=affinity,nice=nice))
    assert rows
    with (output/'background-priority.jsonl').open('a') as stream:
        stream.write(json.dumps(dict(monotonic_ns=time.monotonic_ns(),threads=rows))+'\n')
