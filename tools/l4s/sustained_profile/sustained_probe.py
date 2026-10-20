"""Prague-only diagnostic on unchanged one-switch network; two unlimited Cubic background flows."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path.cwd()
sys.path.insert(0,str(ROOT/'tools/l4s'))
import importlib.util
spec=importlib.util.spec_from_file_location('probe_topology',ROOT/'inputs/probe_topology.py')
topology=importlib.util.module_from_spec(spec);spec.loader.exec_module(topology)
from run_camera_player_guest import write_tc, stop, device_command
from experiment_metadata import write_experiment_record

COUNTER_SCRIPT='''import sys,time,json
from pathlib import Path
interface,path=sys.argv[1:]
with open(path,'w',buffering=1) as f:
 while True:
  for line in Path('/proc/net/dev').read_text().splitlines():
   if ':' in line and line.split(':')[0].strip()==interface:
    v=line.split(':')[1].split()
    f.write(json.dumps(dict(unix_ns=time.time_ns(),monotonic_ns=time.monotonic_ns(),rx_bytes=int(v[0]),tx_bytes=int(v[8])))+'\\n')
  time.sleep(.1)
'''

def measure(node,interface):
    t0=time.monotonic_ns()
    text=device_command(node,['cat','/proc/net/dev'])
    line=next(l for l in text.splitlines() if ':' in l and l.split(':')[0].strip()==interface)
    v=line.split(':')[1].split()
    return dict(monotonic_ns=(t0+time.monotonic_ns())//2,rx_bytes=int(v[0]),tx_bytes=int(v[8]))


def run_case(out,mode,rate,sender_options):
    out.mkdir()
    os.environ['CAMERA_PLAYER_MODE']=mode
    net,client,server,devices=topology.build_topology()
    processes=[];handles=[];background=[]
    def launch(node,name,command,cwd=ROOT,env=None):
        if name in ('publisher','subscriber'):
            command=['nice','-n','-10','taskset','-c','0' if name=='publisher' else '1',*command]
        log=(out/(name+'.log')).open('wb');handles.append(log)
        proc=node.popen(command,cwd=str(cwd),stdout=log,stderr=subprocess.STDOUT,env=env);processes.append(proc);return proc
    try:
        net.start();topology.configure_topology(devices,rate)
        import guest_priority
        guest_priority.configure_switch(devices,out)
        for name, source_role, sink_role in [('main','server_egress','client_egress'),('background_5201','server_egress','background_sink_egress'),('background_5202','background_source_egress','background_sink2_egress')]:
            source, _ = devices[source_role];sink, _ = devices[sink_role]
            ping=device_command(source,['ping','-n','-c','5','-i','0.1',sink.IP()])
            (out/(name+'-idle-ping.txt')).write_text(ping)
        for phase in ['before']:
            with (out/('ingress-filters-'+phase+'.txt')).open('w') as evidence:
                for role in topology.ENDPOINT_ROLES:
                    node, interface=devices[role]
                    evidence.write('['+role+']\n'+device_command(node,['tc','-s','filter','show','dev',interface,'parent','ffff:'])+'\n')
        background=topology.start_background(devices,out)
        processes.extend(p for p,h in background);handles.extend(h for p,h in background)
        guest_priority.verify_background(out)
        write_tc(out,'before',devices)
        for role in ['client_egress','background_sink_egress','background_sink2_egress']:
            node,interface=devices[role]
            launch(node,role+'-sampler',[sys.executable,'-c',COUNTER_SCRIPT,interface,str(out/(role+'-samples.jsonl'))])
        binary=Path(os.environ['PROBE_BINARY'])
        (out/'publisher-options.json').write_text(json.dumps(dict(sender_congestion_options=sender_options))+'\n')
        publisher=launch(server,'publisher',[str(binary),'publisher',server.IP(),'4443',mode,str(out/'metrics.csv'),'21'],ROOT/'deps/imquic/src',env={**os.environ,'IMQUIC_SUSTAINED_CONGESTION_OPTIONS':sender_options})
        time.sleep(.3)
        # Receiver/controller/ECN policy is identical for every sender case.
        subscriber=launch(client,'subscriber',[str(binary),'subscriber',server.IP(),'4443','prague','23',str(out/'subscriber-result.json')],ROOT/'deps/imquic/src')
        guest_priority.verify_endpoints(publisher.pid,subscriber.pid,out)
        before=measure(client,devices['client_egress'][1]);start_wall=time.time_ns()
        before_bg={r:measure(*devices[r]) for r in ['background_sink_egress','background_sink2_egress']}
        deadline=time.monotonic()+19
        next_priority_check=0
        while time.monotonic()<deadline:
            if time.monotonic()>=next_priority_check:
                guest_priority.verify_runtime(publisher.pid,subscriber.pid,out)
                next_priority_check=time.monotonic()+1
            if publisher.poll() is not None or subscriber.poll() is not None:raise RuntimeError('main endpoint exited before horizon')
            if any(p.poll() is not None for p,h in background):raise RuntimeError('background exited')
            time.sleep(min(.1,max(0,deadline-time.monotonic())))
        after=measure(client,devices['client_egress'][1]);end_wall=time.time_ns()
        after_bg={r:measure(*devices[r]) for r in before_bg}
        active=topology.assert_background(devices)
        guest_priority.verify_background(out)
        assert publisher.wait(timeout=10)==0
        assert subscriber.wait(timeout=10)==0
        validated=json.loads((out/'subscriber-result.json').read_text());assert validated['validated']
        write_tc(out,'after',devices)
        elapsed=(after['monotonic_ns']-before['monotonic_ns'])/1e9
        result=dict(status='PASS',sender_congestion_options=sender_options,experimental_publisher_source_sha256=hashlib.sha256((ROOT/'inputs/sustained-moq-options.c').read_bytes()).hexdigest(),sender_mode=mode,receiver_mode='prague',downstream_rate=rate,provider_rate='20mbit',topology_kind='one-switch-all-links20',all_link_directions_mbps=20,coupling_factor_requested=1,dualpi2_step='5ms',
            measurement_start_unix_ns=start_wall,measurement_end_unix_ns=end_wall,
            measurement_seconds=elapsed,client_before=before,client_after=after,
            main_receive_interface_mbps=(after['rx_bytes']-before['rx_bytes'])*8/elapsed/1e6,
            background_before=before_bg,background_after=after_bg,background_connections=active,
            background_configuration=json.loads((out/'background-config.json').read_text()),
            subscriber=validated,publisher_duration_seconds=21,object_bytes=16384,outstanding_target_bytes=163840,
            metric_definition='Client interface received bytes, including headers/control; fixed approximately19s wall window. Subscriber final validated bytes include drain and are not divided by19.',
            topology_sha256=hashlib.sha256((ROOT/'inputs/probe_topology.py').read_bytes()).hexdigest(),base_topology_sha256=hashlib.sha256((ROOT/'tools/l4s/thesis_player_topology.py').read_bytes()).hexdigest(),
            binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),initial_cwnd_bytes={'reno':58500,'prague':47250},picoquic_header_sha256=hashlib.sha256((ROOT/'deps/picoquic/picoquic/picoquic_internal.h').read_bytes()).hexdigest(),prague_source_sha256=hashlib.sha256((ROOT/'deps/picoquic/picoquic/prague.c').read_bytes()).hexdigest())
        (out/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(dict(case=out.name,status='PASS',mode=mode,mbps=result['main_receive_interface_mbps'])),flush=True)
        return result
    finally:
        for proc in reversed(processes):stop(proc)
        for h in handles:h.close()
        net.stop()

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--rate',choices=['100mbit','20mbit'],required=True);a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=False)
    write_experiment_record(a.output,scenario='prague-two-cubic-backgrounds',configuration=dict(downstream_rate=a.rate,provider_rate='20mbit',topology_kind='one-switch-all-links20',all_link_directions_mbps=20,coupling_factor_requested=1,provider_burst='32k',provider_cburst='kernel default (historically1600B)',dualpi2_target='15ms',dualpi2_tupdate='16ms',dualpi2_step='5ms',background='two unlimited non-ECN Cubic flows',background_flows=2,warmup_seconds=2,settling_seconds=0,measurement_seconds=19,receiver_mode='prague',capture_mode='none',order=['ce_response=1/2,sudden_ce_threshold=off,pacing_interval=1/4000','ce_response=1/4,sudden_ce_threshold=off,pacing_interval=1/4000'],initial_cwnd_bytes={'reno':58500,'prague':47250},initial_cwnd_rtt_ms={'reno':36,'prague':21},initial_cwnd_target_mbps={'reno':13,'prague':18},added_base_rtt_ms=20,propagation_location='10ms host ingress via IFB in each direction'),topology=dict(path='server->s1->client; server->s1->bg_sink; bg_src->s1->bg_sink2'))
    import guest_priority
    guest_priority.reserve_cpu(a.output)
    results=[]
    for pair,sender_options in enumerate(['ce_response=1/2,sudden_ce_threshold=off,pacing_interval=1/4000','ce_response=1/4,sudden_ce_threshold=off,pacing_interval=1/4000'],1):
        results.append(run_case(a.output/f'pair-{pair:02d}-prague','prague',a.rate,sender_options))
        (a.output/'results.json').write_text(json.dumps(results,indent=2)+'\n')
    (a.output/'complete.json').write_text(json.dumps(dict(status='PASS',cases=len(results)))+'\n')
if __name__=='__main__':main()
