"""Retained Bicycle QEMU comparison: VMAF, QUIC stream throughput, RTT, cwnd, losses."""
from __future__ import annotations
import argparse,csv,hashlib,json,math,statistics
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT=Path(__file__).resolve().parents[2]
PARSER=argparse.ArgumentParser(description=__doc__)
PARSER.add_argument('--scene',required=True)
PARSER.add_argument('--output',type=Path,required=True)
PARSER.add_argument('--source',type=Path)
PARSER.add_argument('--controllers', nargs='+', choices=('prague','reno','reno-ect0','bbr'))
PARSER.add_argument('--bandwidth-mbps', type=float, default=50)
PARSER.add_argument('--one-way-delay-ms', type=float, default=15)
PARSER.add_argument('--allow-partial', action='store_true', help='include scored incomplete captures and disclose source status')
PARSER.add_argument('--bbr',action='store_true',help='one BBR sample instead of five Prague/Reno repeats')
ARGS=PARSER.parse_args()
SCENE=ARGS.scene
OUT=ARGS.output.resolve()
OUT.mkdir(parents=True,exist_ok=True)
REPEATS=1 if ARGS.bbr else 5
SOURCE=ROOT/'results/imquic-qemu-50mbps-rtt30ms-q5-queue16-20261117-131136/batch'
if ARGS.source:SOURCE=ARGS.source.resolve()
PALETTE={'prague':'#007C91','reno':'#B34F2B','reno-ect0':'#8D5AA5','bbr':'#AD8300'}
LABELS={'prague':'Prague','reno':'Reno Not-ECT','reno-ect0':'Reno ECT(0)','bbr':'BBR'}
COLORS={c:PALETTE[c] for c in (ARGS.controllers or (['bbr'] if ARGS.bbr else ['prague','reno']))}
METRICS=[('vmaf','VMAF'),('goodput','QUIC stream throughput\n(Mbps)'),
 ('rtt','Smoothed RTT\n(ms)'),('cwnd','cwnd\n(KiB)'),('ce','Sum CE\n(packets/100 ms)'),('loss','Sum loss\n(packets/100 ms)')]
INPUTS={}

def read(path):
 raw=path.read_bytes();INPUTS[str(path.resolve())]=hashlib.sha256(raw).hexdigest();return raw

def jread(path):return json.loads(read(path))

def csvread(path):return list(csv.DictReader(read(path).decode().splitlines()))

def csvwrite(path,rows):
 with path.open('w',newline='') as stream:
  writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)

def main():
 summary=jread(SOURCE/'analysis/summary.json');ledger=jread(SOURCE/'ledger.json')
 data={metric:{c:[] for c in COLORS} for metric,_ in METRICS}
 raw_rows=[];run_rows=[];evidence=[]
 for controller in COLORS:
  runs=sorted([(index,r) for index,r in enumerate(summary['runs']) if r['path']=='qemu' and r['scene']==SCENE and r['controller']==controller],key=lambda pair:pair[1]['rerun'])
  assert [r['rerun'] for _,r in runs]==list(range(1,REPEATS+1))
  for index,run in runs:
   assert all(run[field]=='PASS' for field in ('capture_status','vmaf_status'))
   assert run['quality_status']=='PASS' or (ARGS.allow_partial and run['quality_status']=='FAIL')
   assert run['status']=='PASS' or (ARGS.allow_partial and run['status']=='PARTIAL') or (ARGS.bbr and run['status']=='PARTIAL' and (SOURCE/'recovery.json').is_file())
   assert run['bandwidth_mbps']==ARGS.bandwidth_mbps and run['delay_ms']==ARGS.one_way_delay_ms and run['duration_seconds']==20
   assert run['vmaf_grid']=='shared exact poses'
   item=next(i for i in ledger['items'] if i['id']==run['id'])
   attempt=next(a for a in item['attempts'] if a['index']==item['selected_attempt'])
   directory=SOURCE/attempt['directory']
   telemetry=jread(directory/'player-telemetry.json');start=telemetry['trace']['startedUnixMs']/1000
   assert telemetry['trace']['startPolicy']=='first-object'
   guest=Path(jread(directory/'guest-evidence.json')['directories'][0])
   native=csvread(guest/'transport-metrics.csv')
   assert len({row['connection'] for row in native})==1, 'ambiguous subscriber connection'
   anchors=[json.loads(line) for line in read(guest/'network-samples.jsonl').decode().splitlines()]
   offsets=[a['unix_ns']/1e9-a['monotonic_ns']/1e9 for a in anchors]
   offset=statistics.median(offsets)
   losses=np.zeros(200);ce_marks=np.zeros(200)
   previous=None
   for row in native:
    t=int(row['monotonic_us'])/1e6+offset-start
    lost=int(row['packets_lost']);sent=int(row['packets_sent'])
    if previous is not None:
     assert int(row['monotonic_us'])>int(previous['monotonic_us'])
     assert lost>=int(previous['packets_lost']) and sent>=int(previous['packets_sent'])
     assert int(row['ce_packets'])>=int(previous['ce_packets'])
    if 0<=t<20:
     slot=min(199,math.floor(t*10))
     if previous is not None:
      losses[slot]+=lost-int(previous['packets_lost'])
      ce_marks[slot]+=int(row['ce_packets'])-int(previous['ce_packets'])
     raw_rows.append({'id':run['id'],'controller':controller,'repeat':run['rerun'],
      'time_s':t,'native_monotonic_us':int(row['monotonic_us']),
      'rtt_ms':int(row['rtt_us'])/1000,'cwnd_kib':int(row['cwnd_bytes'])/1024,
      'data_sent_bytes':int(row['data_sent_bytes']),'packets_sent':sent,'packets_lost':lost,'ce_packets':int(row['ce_packets']),'spurious_losses':int(row['spurious_losses'])})
    previous=row
   data['loss'][controller].append(losses)
   data['ce'][controller].append(ce_marks)
   # Stream bytes are counted when first serialized by QUIC, not at object completion.
   native_times=np.asarray([int(r['monotonic_us'])/1e6+offset-start for r in native])
   sent_bytes=np.asarray([int(r['data_sent_bytes']) for r in native],dtype=np.float64)
   assert np.all(np.diff(native_times)>0) and np.all(np.diff(sent_bytes)>=0)
   edges=np.arange(201)/10
   assert native_times[0]<=edges[0] and native_times[-1]>=edges[-1], 'counter coverage must bracket the full trace'
   # Integrate last-observed RTT/cwnd over each bin. Native sampling jitter
   # otherwise leaves empty 100 ms buckets; holding state avoids zero filling.
   for metric,column,scale in [('rtt','rtt_us',1000),('cwnd','cwnd_bytes',1024)]:
    states=np.asarray([int(r[column])/scale for r in native])
    assert np.all(states>0),f'{metric} has invalid native state'
    area=np.r_[0,np.cumsum(np.diff(native_times)*states[:-1])]
    positions=np.searchsorted(native_times,edges,side='right')-1
    edge_area=area[positions]+(edges-native_times[positions])*states[positions]
    means=np.diff(edge_area)/.1
    assert np.isclose(np.sum(means)*.1,edge_area[-1]-edge_area[0])
    data[metric][controller].append(means)
   # Counters are sampled at about 100 ms with varying phase/jitter. Estimate
   # fixed-grid edge values linearly; this is not exact packet-level binning.
   edge_bytes=np.interp(edges,native_times,sent_bytes)
   bin_bytes=np.diff(edge_bytes)
   assert np.all(bin_bytes>=0)
   assert np.isclose(bin_bytes.sum(),edge_bytes[-1]-edge_bytes[0],atol=1e-6)
   goodput=bin_bytes*8/1e6/.1
   data['goodput'][controller].append(goodput)
   quality=np.full(100,np.nan)
   for row in run['vmaf_rows']:
    slot=round(row['timestamp_ms']/200)
    assert abs(slot*200-row['timestamp_ms'])<1e-6
    quality[slot]=row['vmaf']
   assert np.isfinite(quality).sum()>0
   data['vmaf'][controller].append(quality)
   for metric,_ in METRICS:
    values=data[metric][controller][-1]
    for slot,value in enumerate(values):
     run_rows.append({'id':run['id'],'controller':controller,'repeat':run['rerun'],
      'metric':metric,'time_s':slot*.2 if metric=='vmaf' else (slot+.5)/10,
      'value':float(value) if np.isfinite(value) else '',
      'included_in_common_grid':bool(np.isfinite(value))})
   evidence.append({'id':run['id'],'controller':controller,'repeat':run['rerun'],
    'guest_directory':str(guest),'connection':native[0]['connection'],
    'capture_start_unix':start,'guest_unix_minus_monotonic_s':offset,
    'clock_anchor_range_ms':(max(offsets)-min(offsets))*1000,
    'source_status':run['status'],
    'binned_loss_total':int(losses.sum()),'binned_ce_total':int(ce_marks.sum()),
    'quic_counter_bytes_in_trace':float(edge_bytes[-1]-edge_bytes[0]),
    'quic_reconstructed_bytes':float(np.sum(goodput)*.1*1e6/8),
    'native_sample_interval_ms_median':float(np.median(np.diff(native_times))*1000),
    'native_sample_interval_ms_max':float(np.max(np.diff(native_times))*1000)})
 rows=[];stats={}
 for metric,_ in METRICS:
  stats[metric]={}
  for controller in COLORS:
   values=np.asarray(data[metric][controller]);assert values.shape==(REPEATS,100 if metric=='vmaf' else 200)
   counts=np.isfinite(values).sum(axis=0)
   q=np.full((3,values.shape[1]),np.nan)
   common=counts==REPEATS
   q[:,common]=np.percentile(values[:,common],[25,50,75],axis=0,method='linear')
   sums=np.sum(values,axis=0) if metric in ('ce','loss') else None
   stats[metric][controller]=sums if sums is not None else q
   for slot,count in enumerate(counts):
    low,median,high=q[:,slot]
    assert not count or count==REPEATS
    if count:assert low<=median<=high
    rows.append({'metric':metric,'controller':controller,'time_s':slot*.2 if metric=='vmaf' else (slot+.5)/10,
     'n':int(count),'q25':float(low) if count and sums is None else '', 'median':float(median) if count and sums is None else '',
     'q75':float(high) if count and sums is None else '',
     'sum':int(sums[slot]) if sums is not None else '',
     'aggregation':'sum' if sums is not None else 'median and q25/q75'})
 csvwrite(OUT/'raw-transport.csv',raw_rows);csvwrite(OUT/'run-series.csv',run_rows);csvwrite(OUT/'plotted-data.csv',rows)
 plt.rcParams.update({'font.family':'DejaVu Sans','font.size':9,'axes.spines.top':False,
  'axes.spines.right':False,'pdf.fonttype':42,'svg.fonttype':'none'})
 def draw(ax,metric,legend=False):
  for controller,color in COLORS.items():
   if metric in ('ce','loss'):
    ax.plot((np.arange(200)+.5)/10,stats[metric][controller],color=color,lw=.75,label=LABELS[controller])
    continue
   low,median,high=stats[metric][controller]
   times=np.arange(100)/5 if metric=='vmaf' else (np.arange(200)+.5)/10
   ax.plot(times,median,color=color,lw=.75,label=LABELS[controller])
   ax.fill_between(times,low,high,color=color,alpha=.18,lw=0)
  ax.set_xlim(0,20);ax.set_ylim(bottom=0);ax.set_xticks([0,5,10,15,20]);ax.grid(axis='y',alpha=.2,lw=.6)
  if metric=='vmaf':ax.set_ylim(0,100)
  if legend:ax.legend(loc='upper left',ncol=2,frameon=False,fontsize=8)
 def save(fig,stem):
  for suffix in ('png','pdf','svg'):fig.savefig(OUT/f'{stem}.{suffix}',dpi=180)
  plt.close(fig)
 fig,axes=plt.subplots(6,1,figsize=(7.2,10.2),sharex=True,layout='constrained')
 for index,(metric,label) in enumerate(METRICS):
  draw(axes[index],metric,legend=index==0);axes[index].set_ylabel(label)
 axes[-1].set_xlabel('Time (s)');save(fig,f'{SCENE}-qemu-template')
 for metric,label in METRICS:
  fig,ax=plt.subplots(figsize=(7.2,3.8),layout='constrained');draw(ax,metric,legend=True)
  ax.set(xlabel='Time (s)',ylabel=label);save(fig,f'{SCENE}-qemu-{metric}')
 provenance={'source_batch':str(SOURCE),'scene':SCENE,'bandwidth_mbps':ARGS.bandwidth_mbps,'configured_base_rtt_ms':2*ARGS.one_way_delay_ms,
  'configured_one_way_total_ms':ARGS.one_way_delay_ms,'repetitions_per_controller':REPEATS,'duration_seconds':20,
  'aggregation':'At each common VMAF timestamp, median and q25/q75 across configured repeats. QUIC stream sending throughput uses estimated 100 ms bins; RTT, cwnd, CE and loss also use 100 ms bins. VMAF, throughput, RTT and cwnd show median and q25/q75 after per-run binning. CE and loss show per-bin packet-count sums across configured repeats per controller, without quartile bands; these are not cumulative totals over time.',
  'vmaf':'Exact common 5 fps timestamp/pose samples; excluded slots are left as gaps, not interpolated.',
  'goodput':'Relay-to-subscriber QUIC stream sending rate from data_sent_bytes. Counter values are linearly interpolated to fixed 100 ms edges, then differenced and divided by 0.1 s. Counts newly serialized stream bytes, including MoQ framing, but excludes packet headers and retransmissions; not wire throughput or delivered-byte rate.',
  'quic_counter_implementation':['deps/imquic/src/quic.c: imquic_get_transport_metrics -> picoquic_get_data_sent', 'deps/picoquic/picoquic/quicctx.c: picoquic_get_data_sent returns cnx->data_sent', 'deps/picoquic/picoquic/frames.c: data_sent increments by newly serialized stream bytes'],
  'transport_bin_width_ms':100,
  'transport_display':'Connected lines through 100 ms bin centers for all five transport panels.',
  'throughput_display':'Connected lines through 100 ms bin centers; quartile bands use the same centers. No change to the bin values.',
  'rtt_cwnd':'Time-weighted mean of last-observed native subscriber-connection smoothed RTT and cwnd within each 100 ms bin. State is held until the next native sample; native cadence is approximately 100 ms.',
  'ce':'Increments of cumulative QUIC ce_packets assigned to the right-hand sample time in 100 ms bins, summed across configured repeats per controller. ACK-reported CE marks, not exact marking times.',
  'loss':'Increments of cumulative QUIC packets_lost assigned to the right-hand sample time, counted within 100 ms bins and summed across configured repeats per controller (packets/100 ms). Includes subsequent spurious losses; not a packet-loss probability.',
  'clock_alignment':'Guest monotonic to guest UNIX via median network-sample anchors, then subtract host browser first-object trace start. Cross-VM/host clock skew is not independently bounded.',
  'one_way_delay':'Not measured; RTT/2 is not plotted as one-way delay.',
  'offline_recovery':jread(SOURCE/'recovery.json') if (SOURCE/'recovery.json').is_file() else None,
  'runs':evidence,'inputs_sha256':INPUTS,'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
 (OUT/'provenance.json').write_text(json.dumps(provenance,indent=2)+'\n')
 print('Created six-panel template and six standalone metric plots from retained quality-validated runs.',flush=True)

if __name__=='__main__':main()
