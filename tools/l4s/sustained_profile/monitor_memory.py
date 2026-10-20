"""Watch only the isolated recording units; abort them if host reserve is low."""
import json,subprocess,time
from pathlib import Path
OUT=Path(__file__).resolve().parent
log=OUT/'memory-samples.jsonl'
while True:
    units=subprocess.check_output(['systemctl','--user','list-units','scene-video-sustained-profile.service','--state=running','--plain','--no-legend'],text=True).splitlines()
    info={line.split(':')[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
    entries=[]
    for line in units:
        unit=line.split()[0]
        if not unit.endswith('.service'):continue
        data=subprocess.check_output(['systemctl','--user','show',unit,'-p','MemoryCurrent','-p','MemoryPeak','-p','MemoryMax','-p','ControlGroup'],text=True)
        values=dict(item.split('=',1) for item in data.splitlines())
        entries.append(dict(unit=unit,**values))
        if info['MemAvailable'] < 2*1024*1024:
            subprocess.run(['systemctl','--user','stop',unit],check=True)
            entries[-1]['stopped']='Host MemAvailable below 2 GiB'
    with log.open('a') as stream:stream.write(json.dumps(dict(time=time.time(),available_kib=info['MemAvailable'],units=entries))+'\n')
    if (OUT/'stop-monitor').exists():break
    time.sleep(3)
