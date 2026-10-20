import csv
import json
import statistics
import sys
from pathlib import Path

out=Path(__file__).resolve().parent
guest=Path(sys.argv[1])
properties=dict(line.split('=',1) for line in (out/'unit-properties.txt').read_text().splitlines())
assert properties['CPUWeight']=='1000'
assert properties['MemorySwapPeak']=='0'
assert json.loads((out/'exit.json').read_text())['exit_code']==0
host=json.loads(max(out.glob('host-affinity-*.json'),key=lambda p:p.stat().st_mtime_ns).read_text())
assert {(p['guest_cpu'],p['host_cpu']) for p in host['pins']}=={(0,6),(1,8),(2,10),(3,4),(4,5),(5,7),(6,9)}
samples=[json.loads(line) for line in (out/'host-affinity-samples.jsonl').read_text().splitlines()]
assert len(samples)>10
for row in samples:
 for thread in row['threads']:
  assert thread['affinity']==[{0:6,1:8,2:10,3:4,4:5,5:7,6:9}[thread['guest_cpu']]]
host_pin_samples=len(samples)
rows=[]
for case in sorted(guest.glob('pair-*-prague')):
 priority=json.loads((case/'switch-priority.json').read_text())
 assert priority['ovs_threads'] and all(t['affinity']==[2] and t['nice']==-10 for t in priority['ovs_threads'])
 assert len(priority['rps'])==10
 for r in priority['rps']:
  expected=2 if r['role'] in ['provider_egress','downstream_egress'] or r['role'].startswith('switch_to_') else 1 if r['role']=='client_egress' else 0 if r['role']=='server_egress' else {'background_source_egress':4,'background_sink_egress':5,'background_sink2_egress':6}[r['role']]
  assert r['cpu']==expected and all(int(v.replace(',',''),16)==1<<expected for v in r['actual'].values())
 endpoints=json.loads((case/'endpoint-affinity.json').read_text())
 assert {t['expected_cpu'] for t in endpoints}=={0,1}
 assert all(t['affinity']==[t['expected_cpu']] and t['nice']==-10 for t in endpoints)
 bg_samples=[json.loads(l) for l in (case/'background-priority.jsonl').read_text().splitlines()]
 assert len(bg_samples)>=20
 manifest=json.loads((case/'background-processes.json').read_text())
 assert sorted(r['cpu'] for r in manifest)==[0,4,5,6]
 for sample in bg_samples:
  assert all(t['affinity']==[t['expected_cpu']] and t['nice']==-10 for t in sample['threads'])
  assert {t['pid'] for t in sample['threads']}=={r['pid'] for r in manifest}
 runtime=[json.loads(l) for l in (case/'runtime-priority.jsonl').read_text().splitlines()]
 assert len(runtime)>=18
 assert len(priority['kernel_workers'])==6
 for sample in runtime:
  assert all(t['nice']==-10 for t in sample['workers']+sample['endpoints'])
  assert all(t['affinity']==[t['expected_cpu']] for t in sample['endpoints'])
  kernel={t['pid']:t['affinity'] for t in priority['kernel_workers']}
  assert all(t['affinity']==([2] if t['role']=='ovs' else kernel[t['tid']]) for t in sample['workers'])
 assert json.loads((guest/'guest-housekeeping-affinity.json').read_text())['housekeeping_cpus']==[3]
 summary=json.loads((case/'summary.json').read_text())
 metrics=list(csv.DictReader((case/'metrics.csv').open()))
 active=[r for r in metrics if 5e6<=int(r['time_us'])<=19e6]
 after1=[r for r in metrics if int(r['time_us'])>=1e6]
 samples=[json.loads(line) for line in (case/'client_egress-samples.jsonl').read_text().splitlines()]
 start=summary['client_before']['monotonic_ns']
 late=[r for r in samples if start+10e9<=r['monotonic_ns']<=summary['client_after']['monotonic_ns']]
 a,b=late[0],late[-1]
 rows.append(dict(case=case.name,receive_mbps=summary['main_receive_interface_mbps'],late_mbps=(b['rx_bytes']-a['rx_bytes'])*8000/(b['monotonic_ns']-a['monotonic_ns']),rtt_median_5_19_ms=statistics.median(int(r['rtt_us']) for r in active)/1000,rtt_max_after1_ms=max(int(r['rtt_us']) for r in after1)/1000,max_metric_gap_ms=max(int(b['time_us'])-int(a['time_us']) for a,b in zip(metrics,metrics[1:]))/1000,final_ce_packets=int(metrics[-1]['ce_packets']),final_ect1_packets=int(metrics[-1]['ect1_packets']),final_packets_lost=int(metrics[-1]['packets_lost']),final_spurious_losses=int(metrics[-1]['spurious_losses']),ovs_threads=len(priority['ovs_threads'])))
assert len(rows)==2
result=dict(status='PASS',host_pin_samples=host_pin_samples,host_cores_exclusive=False,guest_priority='Endpoints CPU0/1, OVS CPU2, ksoftirqd0/1/2/4/5/6 nice -10; housekeeping CPU3; background host CPUs4/5/6',cases=rows)
(out/'priority-validation.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
