import json
from pathlib import Path
import signal
import subprocess
import sys
ROOT=Path.cwd();OUT=Path(__file__).resolve().parent
unit='scene-video-sustained-profile'
mem={line.split(':')[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
assert mem['MemAvailable']>int(3.5*1024*1024),'Need3.5GiB available before launch'
def stop(signum,frame):raise KeyboardInterrupt
signal.signal(signal.SIGTERM,stop)
(OUT/'stop-monitor').unlink(missing_ok=True)
monitor=subprocess.Popen([sys.executable,str(OUT/'monitor_memory.py')])
affinity_monitor=subprocess.Popen([sys.executable,str(OUT/'affinity_monitor.py')])
command=['systemd-run','--user','--wait','--pipe','--collect','--unit='+unit,
 '-p','CPUWeight=1000','-p','CPUAffinity=4 5 6 7 8 9 10 11','-p','MemoryAccounting=yes','-p','MemoryHigh=2800M','-p','MemoryMax=3G','-p','MemorySwapMax=128M','-p','OOMPolicy=kill','--working-directory='+str(ROOT),
 sys.executable,str(OUT/'qemu_priority.py'),'--allow-dirty','--memory','2048','--cpus','7','--make-target=--file=inputs/probe.mk',
 '--guest-result-name','sustained-profile','--destination-prefix','sustained-profile','--guest-timeout','1200',
 '--guest-file',str(OUT/'probe.mk')+'=inputs/probe.mk','--guest-file',str(OUT/'sustained_probe.py')+'=inputs/sustained_probe.py',
 '--guest-file',str(OUT/'probe_topology.py')+'=inputs/probe_topology.py','--guest-file',str(OUT/'sustained-moq-options.c')+'=inputs/sustained-moq-options.c','--guest-file',str(OUT/'guest_priority.py')+'=inputs/guest_priority.py','--make-variable','PROBE_RATE=20mbit']
(OUT/'launch.json').write_text(json.dumps(dict(command=command,unit=unit),indent=2)+'\n')
status=None
try:
 with (OUT/'qemu.log').open('w') as log:status=subprocess.call(command,stdout=log,stderr=subprocess.STDOUT)
 (OUT/'exit.json').write_text(json.dumps(dict(exit_code=status))+'\n')
 sys.exit(status)
finally:
 subprocess.run(['systemctl','--user','stop','--no-block',unit],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 (OUT/'stop-monitor').touch()
 for child in [monitor,affinity_monitor]:
  try:child.wait(timeout=8)
  except subprocess.TimeoutExpired:child.terminate();child.wait()
 if status==0:
  assert all(child.returncode==0 for child in [monitor,affinity_monitor]),'A host monitor failed'
