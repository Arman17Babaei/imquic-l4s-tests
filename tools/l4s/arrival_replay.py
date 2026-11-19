#!/usr/bin/env python3
"""Replay complete-object arrivals from a real network capture; no network model."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
from fractions import Fraction
from ideal_link import ManifestIndex, frame


class ArrivalReplay:
    def __init__(self, manifest: Path, journal: Path):
        self.index=ManifestIndex(manifest)
        self.capture=json.loads(Path(journal).read_text())
        if self.capture.get('status')!='PASS' or self.capture.get('clock')!='guest-monotonic':
            raise ValueError('requires a complete guest-monotonic capture')
        if hashlib.sha256(Path(manifest).read_bytes()).hexdigest()!=self.capture['manifest_sha256']:
            raise ValueError('manifest hash changed since network capture')
        objects={(o.tile_id,o.refinement,o.object_id):o for track in self.index.tracks.values() for o in track}
        self.rows=[];seen=set()
        for sequence,row in enumerate(self.capture['objects']):
            key=(row['tile_id'],row['refinement'],row['object_id'])
            if key in seen:raise ValueError('duplicate captured object')
            seen.add(key)
            obj=objects.get(key)
            if obj is None or obj.payload_bytes!=row['payload_bytes']:raise ValueError('object identity/size mismatch')
            if obj.sha256!=row['actual_sha256']:raise ValueError('captured payload hash mismatch')
            arrival=row['arrival_us']
            if isinstance(arrival,bool) or not isinstance(arrival,int) or not 0<=arrival<=self.capture['duration_ms']*1000:
                raise ValueError('arrival outside capture horizon')
            self.rows.append((arrival,sequence,row,obj))
        self.rows.sort(key=lambda r:r[:2]);self.cursor=0;self.now=Fraction(0)
    def advance(self,time_us):
        if time_us<self.now:raise ValueError('replay time must not decrease')
        if time_us>self.capture['duration_ms']*1000:raise ValueError('replay beyond capture horizon')
        result=[]
        while self.cursor<len(self.rows) and self.rows[self.cursor][0]<=time_us:
            arrival,_,row,obj=self.rows[self.cursor];self.cursor+=1
            result.append((dict(row,wire='object',recorded_arrival_us=arrival,ideal_arrival_ms=arrival/1000,monotonic_us=0),obj.payload()))
        self.now=time_us
        return result


def run(config):
    replay=ArrivalReplay(Path(config['manifest']),Path(config['journal']))
    output=Path(config['output']);output.mkdir(parents=True,exist_ok=True)
    requests={};dispatches=[];counts={};closed=False
    def emit(header,payload=b''):
        sys.stdout.buffer.write(frame(header,payload));sys.stdout.buffer.flush()
    def control(message):emit(dict(wire='control',message=message))
    control(dict(type='ready',transport='replay',delivery_model='recorded-arrivals-v1'))
    try:
        for line in sys.stdin:
            command=json.loads(line);kind=command['type']
            if kind=='open':
                for request in command['fetches']:
                    key=(request['tile_id'],request['refinement'])
                    if key in requests:raise ValueError('duplicate replay FETCH')
                    identifier=len(requests)*2;requests[key]=identifier
                    control(dict(type='fetch-opened',tile_id=key[0],refinement=key[1],request_id=identifier,initial_priority=request['priority'],epoch=command.get('epoch',0)))
            elif kind=='priorities':
                # Network priorities were already exercised in the recorded run.
                continue
            elif kind in ('advance','horizon'):
                if kind=='advance':
                    index=command['frame_index'];fps=command['fps']
                    if not isinstance(index,int) or index<0 or not isinstance(fps,int) or fps<=0:raise ValueError('invalid frame grid')
                    now=Fraction(index*1000000,fps)
                else:index=-1;now=Fraction(str(command['duration_ms']))*1000
                delivered=[]
                for header,payload in replay.advance(now):
                    key=(header['tile_id'],header['refinement'])
                    if key not in requests:raise ValueError('captured object has no replay FETCH')
                    header['request_id']=requests[key];emit(header,payload)
                    delivered.append([header['tile_id'],header['refinement'],header['object_id']])
                    counts[key]=counts.get(key,0)+1
                    track=next(v for v in replay.index.tracks.values() if v and (v[0].tile_id,v[0].refinement)==key)
                    if counts[key]==len(track):control(dict(type='fetch-status',state='complete',request_id=requests[key]))
                dispatches.append(dict(frame_index=index,timestamp_us=float(now),objects=delivered))
                control(dict(type='advance-complete',frame_index=index,timestamp_ms=float(now/1000)))
            elif kind=='close':closed=True;break
            else:raise ValueError('unknown replay command')
        if not closed:raise ValueError('replay ended without close')
        (output/'replay-summary.json').write_text(json.dumps(dict(status='PASS',source_journal=str(Path(config['journal']).resolve()),journal_sha256=hashlib.sha256(Path(config['journal']).read_bytes()).hexdigest(),dispatched_objects=replay.cursor,frame_dispatches=dispatches),indent=2)+'\n')
        control(dict(type='audit-complete',unresolved_updates=0))
        return 0
    except Exception as error:
        (output/'replay-failure.txt').write_text(str(error)+'\n');control(dict(type='error',message=str(error)));raise

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',type=Path,required=True)
    args=parser.parse_args();sys.exit(run(json.loads(args.config.read_text())))
