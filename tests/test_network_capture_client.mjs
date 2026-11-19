import assert from 'node:assert/strict';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import test from 'node:test';
const root=path.resolve(import.meta.dirname,'..');
test('headless capture rejects post-ready corrupted objects instead of publishing PASS', {timeout:15000},async()=>{
 const folder=fs.mkdtempSync(path.join(root,'results/capture-failure-test-'));
 const fixture=spawn('python3',['-c','from tests.test_ideal_imquic import player_fixture; import sys; player_fixture(sys.argv[1])',folder],{cwd:root});
 assert.equal((await once(fixture,'close'))[0],0);
 const manifest=JSON.parse(fs.readFileSync(path.join(folder,'manifest-v2.json')));const size=manifest.tiles[0].refinements[0].objects[0].bytes;
 const native=path.join(folder,'fake-native.mjs');
 fs.writeFileSync(native,`#!/usr/bin/env node
import readline from 'node:readline';
function emit(header,payload=Buffer.alloc(0)){const h=Buffer.from(JSON.stringify(header)),p=Buffer.alloc(4);p.writeUInt32BE(h.length);process.stdout.write(Buffer.concat([p,h,payload]));}
emit({wire:'control',message:{type:'ready'}});
readline.createInterface({input:process.stdin}).on('line',line=>{const c=JSON.parse(line);if(c.type==='open'){
emit({wire:'control',message:{type:'fetch-opened',tile_id:'tile0',refinement:0,request_id:0,initial_priority:0}});
setTimeout(()=>emit({wire:'object',tile_id:'tile0',refinement:0,object_id:0,payload_bytes:${size},monotonic_us:Number(process.hrtime.bigint()/1000n)},Buffer.alloc(${size})),30);
}if(c.type==='close'){emit({wire:'control',message:{type:'audit-complete'}});process.exit(0);}});`);
 fs.chmodSync(native,0o700);
 const server=net.createServer();server.listen(0,'127.0.0.1');await once(server,'listening');const port=server.address().port;await new Promise(r=>server.close(r));
 const bridge=spawn('node',['tools/l4s/priority_ws_bridge.mjs','--listen',String(port),'--native',native,'--origin',''],{cwd:root,stdio:['ignore','ignore','pipe']});
 let log='';bridge.stderr.on('data',d=>log+=d);
 try{
  for(let n=0;!log.includes('bridge ws://');n++){if(n>100)throw new Error(log);await new Promise(r=>setTimeout(r,20));}
  const output=path.join(folder,'capture');
  const capture=spawn('node',['--experimental-websocket','deps/gaussian-player/dist-headless/network-capture-client.js','--manifest',path.join(folder,'manifest-v2.json'),'--trace',path.join(folder,'trace.json'),'--sidecar',`ws://127.0.0.1:${port}`,'--output',output],{cwd:root,stdio:['ignore','ignore','pipe']});
  let error='';capture.stderr.on('data',d=>error+=d);
  const code=(await once(capture,'close'))[0];assert.notEqual(code,0,error);
  assert.equal(fs.existsSync(path.join(output,'arrivals.json')),false,'a failed collector cannot publish PASS');
  const failure=JSON.parse(fs.readFileSync(path.join(output,'capture-failure.json')));
  assert.match(failure.error,/hash|transport-error/);
 }finally{bridge.kill('SIGTERM');if(bridge.exitCode===null)await once(bridge,'close');}
});
