"""Verify matched sender-only observations and summarize every pair."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import statistics
import re


def section(text,role):return text.split('['+role+']',1)[1].split('\n[',1)[0]

def analyze(guest,output):
    complete=json.loads((guest/'complete.json').read_text())
    assert complete['status']=='PASS' and complete['cases']==2
    pair_count=2
    rows=[]
    for pair in range(1,pair_count+1):
        for mode in ['prague']:
            d=guest/f'pair-{pair:02d}-{mode}';s=json.loads((d/'summary.json').read_text())
            assert s['status']=='PASS' and s['sender_mode']==mode and s['receiver_mode']=='prague'
            assert s['downstream_rate']=='20mbit' and s['provider_rate']=='20mbit'
            assert s['object_bytes']==16384 and s['outstanding_target_bytes']==163840
            assert s['subscriber']['validated'] and s['subscriber']['received_objects']>0
            bg=s['background_configuration']
            assert bg['cc']=='cubic' and bg['ecn'] is False and bg['rate']=='unlimited'
            assert bg['warmup_seconds']==2 and bg['ports']==[5201,5202]
            assert set(s['background_connections'])=={'5201','5202'}
            assert all(s['background_connections'].values())
            assert set(bg['established_connections'])=={'5201','5202'}
            for name in ['background-5201-client','background-5202-client']:
                text=(d/(name+'.log')).read_text()
                assert 'connected' in text and 'Mbits/sec' in text
            endpoint_roles=['server_egress','client_egress','background_sink_egress','background_source_egress','background_sink2_egress']
            switch_roles=['provider_egress','downstream_egress','switch_to_background_sink','switch_to_background_source','switch_to_background_sink2']
            for phase in ['before','after']:
                state=(d/f'tc-state-{phase}.txt').read_text()
                assert len(re.findall(r'^\[',state,re.M))==15
                for role in endpoint_roles+switch_roles:
                    q=section(state,role)
                    assert 'qdisc dualpi2' in q and 'rate 20Mbit' in q
                    assert all(v in q for v in ['target 15ms','tupdate 16ms','step_thresh 5ms','burst 32Kb','cburst 1600b','coupling_factor 1 ','classic_protection 10%'])
                    assert 'netem' not in q
                for role in endpoint_roles:
                    q=section(state,role+'_propagation')
                    assert 'qdisc netem' in q and 'delay 10ms' in q
                    assert 'dualpi2' not in q
                    if phase=='after':
                        assert int(re.search(r'Sent (\d+) bytes',q)[1])>0
            filters=(d/'ingress-filters-before.txt').read_text()
            for role in endpoint_roles:
                q=section(filters,role)
                assert 'Redirect to device ifb0' in q and 'matchall' in q
            for flow in ['main','background_5201','background_5202']:
                ping=(d/(flow+'-idle-ping.txt')).read_text()
                assert '5 packets transmitted, 5 received, 0% packet loss' in ping
                avg=float(re.search(r'= [\d.]+/([\d.]+)/',ping)[1])
                assert 19<=avg<40, (flow,avg)
            assert s['topology_kind']=='one-switch-all-links20' and s['all_link_directions_mbps']==20
            duration=(s['client_after']['monotonic_ns']-s['client_before']['monotonic_ns'])/1e9
            assert duration>19 and duration<19.5
            mbps=(s['client_after']['rx_bytes']-s['client_before']['rx_bytes'])*8/duration/1e6
            assert abs(mbps-s['main_receive_interface_mbps'])<1e-9
            metrics=list(csv.DictReader((d/'metrics.csv').open()));assert len(metrics)>100
            active=[r for r in metrics if 5e6<=int(r['time_us'])<=19e6]
            assert all(int(r['pacing_interval_us'])==250 for r in active)
            assert all(0<int(r['pacing_quantum_bytes'])<=1536 for r in active)
            assert int(metrics[0]['alpha_numerator'])==int(metrics[0]['alpha_denominator'])>0
            options=['ce_response=1/2,sudden_ce_threshold=off,pacing_interval=1/4000','ce_response=1/4,sudden_ce_threshold=off,pacing_interval=1/4000'][pair-1]
            assert s['sender_congestion_options']==options
            assert json.loads((d/'publisher-options.json').read_text())['sender_congestion_options']==options
            assert s['experimental_publisher_source_sha256']==hashlib.sha256((output/'sustained-moq-options.c').read_bytes()).hexdigest()
            expected={'reno':58500,'prague':47250}
            assert s['initial_cwnd_bytes']==expected
            assert int(metrics[0]['cwnd_bytes'])==expected[mode], (mode, metrics[0])
            for filename,key in [('picoquic_internal.h','picoquic_header_sha256'),('prague.c','prague_source_sha256')]:
                assert hashlib.sha256((output/filename).read_bytes()).hexdigest()==s[key]
            assert int(metrics[-1]['ect1_packets'])>0 if mode=='prague' else int(metrics[-1]['ect1_packets'])==0
            if mode=='reno':assert int(metrics[-1]['ce_packets'])==0
            background_mbps=0
            for role,b in s['background_before'].items():
                a=s['background_after'][role];delta=a['rx_bytes']-b['rx_bytes']
                assert delta>0,role
                background_mbps+=delta*8/((a['monotonic_ns']-b['monotonic_ns'])/1e9)/1e6
            rows.append(dict(pair=pair,mode=mode,main_receive_interface_mbps=mbps,background_combined_interface_mbps=background_mbps,
                duration_seconds=duration,median_rtt_ms=statistics.median(int(r['rtt_us']) for r in metrics)/1000,
                sender_congestion_options=s['sender_congestion_options'],initial_observed_cwnd_bytes=int(metrics[0]['cwnd_bytes']),median_cwnd_bytes=statistics.median(int(r['cwnd_bytes']) for r in metrics),
                final_ce_packets=int(metrics[-1]['ce_packets']),binary_sha256=s['binary_sha256'],topology_sha256=s['topology_sha256'],directory=str(d.resolve())))
    assert len({r['binary_sha256'] for r in rows})==1
    assert len({r['topology_sha256'] for r in rows})==1
    assert not list(guest.rglob('*.pcap')) and not list(guest.rglob('*.pcapng'))
    result=dict(artifact_status='PASS',cases=rows,
                note='Two Prague-only observations starting alpha at one, comparing explicit CE response1/2 and1/4 with sudden CE off, with two unlimited non-ECN Cubic background flows; interface RX includes headers/control; not browser delivery.')
    (output/'analysis.json').write_text(json.dumps(result,indent=2)+'\n')
    with (output/'summary.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    print(json.dumps({k:v for k,v in result.items() if k!='cases'},indent=2))
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--guest',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();analyze(a.guest,a.output)
