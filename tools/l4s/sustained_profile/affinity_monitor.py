import json
import os
from pathlib import Path
import time

OUT=Path(__file__).resolve().parent
deadline=time.monotonic()+300
while not list(OUT.glob('host-affinity-*.json')):
    if (OUT/'stop-monitor').exists():raise SystemExit('VM did not start')
    if time.monotonic()>deadline:raise TimeoutError('VM did not start')
    time.sleep(1)
evidence=json.loads(max(OUT.glob('host-affinity-*.json'),key=lambda p:p.stat().st_mtime_ns).read_text())
deadline=time.monotonic()+600
with (OUT/'host-affinity-samples.jsonl').open('w',buffering=1) as log:
    while time.monotonic()<deadline:
        import subprocess
        properties=subprocess.run(['systemctl','--user','show','scene-video-sustained-profile.service','-p','CPUWeight','-p','CPUAffinity','-p','MemoryPeak','-p','MemorySwapPeak','-p','ActiveState'],capture_output=True,text=True)
        if 'ActiveState=active' in properties.stdout:(OUT/'unit-properties.txt').write_text(properties.stdout)
        row=dict(unix_ns=time.time_ns(),threads=[])
        for pin in evidence['pins']:
            try:
                tid=pin['tid']
                actual=sorted(os.sched_getaffinity(tid))
                assert actual==[pin['host_cpu']]
                row['threads'].append(dict(guest_cpu=pin['guest_cpu'],tid=tid,affinity=actual,
                    nice=os.getpriority(os.PRIO_PROCESS,tid),schedstat=(Path('/proc')/str(tid)/'schedstat').read_text().strip()))
            except (ProcessLookupError,FileNotFoundError):
                pass
        log.write(json.dumps(row)+'\n')
        if not row['threads']:break
        time.sleep(1)
