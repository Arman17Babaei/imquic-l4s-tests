"""One suite-owned QEMU VM with sequential, independently reset guest cases."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

from guest_cleanup import process_record, stop_record


class ResetFailure(RuntimeError):
    pass


def read_session(path):
    data=json.loads(Path(path).read_text())
    current=process_record(data['owner']['pid'])
    if not current or current['start_ticks']!=data['owner']['start_ticks']:
        raise RuntimeError('QEMU session owner is no longer running')
    return data


def reset_guest(runner,args,session,label):
    root=session['guest_root']
    output=f'{root}/results/qemu-session/reset-{label}.json'
    command=shlex.join(['python3','tools/l4s/guest_cleanup.py','--state',
                       root+'/results/qemu-session/ownership.json','--output',output])
    script=f"cd {shlex.quote(root)}\nprintf '%s\\n' {shlex.quote(args.password)} | sudo -S {command}\n"
    error=None
    try:runner.ssh_command(session['ssh_port'],args.user,args.password,script,timeout=90)
    except BaseException as failure:error=failure
    try:
        runner.copy_results(session['ssh_port'],args.user,args.password,output,
            Path(session['reset_failure_marker']).parent/f'reset-{label}-{time.monotonic_ns()}.json')
    except BaseException as failure:
        if error is None:error=failure
    if error:
        Path(session['reset_failure_marker']).write_text(f'{type(error).__name__}: guest reset failed\n')
        raise ResetFailure('guest reset failed; refusing further QEMU cases') from error
    return output


def serve(runner,args,port,guest_root,cache_evidence,vm):
    """Called after source staging; owns the VM until suite exit or interruption."""
    path=args.session_owner.resolve();path.parent.mkdir(parents=True,exist_ok=True)
    session=dict(owner=process_record(os.getpid()),ssh_port=port,
        bridge_port=args.forward_sidecar_port,guest_root=guest_root,
        base=str(args.base.resolve()),cpus=args.cpus,memory=args.memory,
        reset_failure_marker=str(path.parent/'reset-failed'),setup_cache=cache_evidence)
    reset_guest(runner,args,session,'initial')
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(session,indent=2)+'\n')
    temporary.chmod(0o600);temporary.replace(path)
    print('QEMU session ready',flush=True)
    try:
        while True:
            if vm.poll() is not None:raise RuntimeError("suite VM exited unexpectedly")
            try:os.kill(args.owner_pid,0)
            except ProcessLookupError:break
            time.sleep(.5)
    finally:path.unlink(missing_ok=True)


def run_case(runner,args,path):
    session=read_session(path)
    if Path(session['reset_failure_marker']).exists():raise ResetFailure('session reset previously failed')
    for key,value in (('base',str(args.base.resolve())),('cpus',args.cpus),
                      ('memory',args.memory),('bridge_port',args.forward_sidecar_port)):
        if session[key]!=value:raise RuntimeError(f'QEMU session {key} mismatch')
    lock=Path(path).with_suffix('.lock').open('a')
    with lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        root=session['guest_root'];port=session['ssh_port']
        guest_result=f'{root}/{args.guest_result_root}/{args.guest_result_name}'
        destination=runner.ROOT/args.destination_root/(args.destination_prefix+'-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()))
        error=None;reset_error=None
        reset_guest(runner,args,session,'before')
        try:
            installs=[]
            for index,(source,relative) in enumerate(args.guest_file):
                # Upload names are unique within this serialized case.
                name=f'case-{index:02d}-{source.name}'
                import tempfile
                with tempfile.TemporaryDirectory(prefix='qemu-case-upload-') as temporary:
                    staged=Path(temporary)/name;staged.symlink_to(source.resolve())
                    runner.copy_to_guest(port,args.user,args.password,staged)
                installs.append('install -D '+shlex.quote('/home/'+args.user+'/'+name)+' '+shlex.quote(str(Path(root)/relative)))
            variables=' '.join(shlex.quote(x) for x in args.make_variable)
            ownership=root+'/results/qemu-session/ownership.json'
            script=f'''set -e
cd {shlex.quote(root)}
printf '%s\\n' {shlex.quote(args.password)} | sudo -S rm -rf -- {shlex.quote(guest_result)} results/3dgs/camera-player-input
{os.linesep.join(installs)}
set +e
printf '%s\\n' {shlex.quote(args.password)} | sudo -S env CAMERA_PLAYER_OWNERSHIP={shlex.quote(ownership)} make {shlex.quote(args.make_target)} L4S_RESULT_DIR={shlex.quote(guest_result)} {variables}
status=$?
printf '%s\\n' {shlex.quote(args.password)} | sudo -S chown -R {shlex.quote(args.user)}:{shlex.quote(args.user)} {shlex.quote(guest_result)}
exit $status
'''
            runner.ssh_command(port,args.user,args.password,script,timeout=args.guest_timeout)
        except BaseException as failure:error=failure
        finally:
            try:reset_path=reset_guest(runner,args,session,'after')
            except BaseException as failure:
                reset_error=failure
                reset_path=session['guest_root']+'/results/qemu-session/reset-after.json'
            try:
                # A hard-killed guest can leave root-owned artifacts; preserve them too.
                script=f"printf '%s\\n' {shlex.quote(args.password)} | sudo -S chown -R {shlex.quote(args.user)}:{shlex.quote(args.user)} {shlex.quote(guest_result)}\n"
                runner.ssh_command(port,args.user,args.password,script,timeout=30)
                runner.copy_results(port,args.user,args.password,guest_result,destination)
                if reset_path:
                    runner.copy_results(port,args.user,args.password,reset_path,destination/'reset-after.json')
                (destination/'setup-cache.json').write_text(json.dumps(session['setup_cache'],indent=2)+'\n')
                (destination/'vm-session.json').write_text(json.dumps({k:session[k] for k in ('owner','ssh_port','bridge_port')},indent=2)+'\n')
            except BaseException as failure:
                if error is None:error=failure
        if reset_error:raise reset_error
        if error:
            print(f'Retrieved failed guest results: {destination}',file=sys.stderr)
            raise error
        print(f'QEMU {args.make_target}: PASS ({destination})',flush=True)


class SuiteSession:
    """Lazy owner outside per-case systemd scopes; always closed by run_batch."""
    def __init__(self,cfg):
        self.cfg=cfg;self.process=None;self.handle=None;self.owner_record=None
        self.path=Path(cfg['output'])/'qemu-session/session.json'
        self.bridge_port=None

    def environment(self):
        import run_qemu_player as runner
        if self.process is None:
            qemu=self.cfg['qemu'];self.path.parent.mkdir(parents=True,exist_ok=True)
            self.bridge_port=runner.free_port()
            launcher=(str(Path(self.cfg['repo_root'])/'tools/l4s/qemu_player_performance.py')
                      if qemu['performance_mode'] else qemu['runner'])
            command=[self.cfg['runner']['python'],launcher,'--base',qemu['base'],
                '--cpus',str(qemu['cpus']),'--memory',str(qemu['memory_mib']),'--allow-dirty',
                '--forward-sidecar-port',str(self.bridge_port),'--session-owner',str(self.path),
                '--owner-pid',str(os.getpid())]
            self.handle=(self.path.parent/'owner.log').open('w')
            environment={k:v for k,v in os.environ.items() if k!='IMQUIC_QEMU_SESSION'}
            environment['CAMERA_QEMU_PROFILE_OUTPUT']=str(self.path.parent/'performance')
            self.process=subprocess.Popen(command,stdout=self.handle,stderr=subprocess.STDOUT,env=environment,start_new_session=True)
            self.owner_record=process_record(self.process.pid,group=True)
            deadline=time.monotonic()+1800
            while not self.path.exists():
                if self.process.poll() is not None:raise ResetFailure('QEMU session setup failed; see qemu-session/owner.log')
                if time.monotonic()>deadline:raise ResetFailure('QEMU session setup timed out')
                time.sleep(.2)
        try:read_session(self.path)
        except (RuntimeError,OSError) as error:raise ResetFailure('QEMU session became unavailable') from error
        return {'IMQUIC_QEMU_SESSION':str(self.path),'IMQUIC_QEMU_BRIDGE_PORT':str(self.bridge_port)}

    def reset(self):
        if self.process is None or not self.path.exists():return
        import run_qemu_player as runner
        args=argparse.Namespace(user=os.environ.get('IMQUIC_QEMU_USER','p4'),
                               password=os.environ.get('IMQUIC_QEMU_PASSWORD','p4'))
        # A terminating case launcher may still be unwinding its own reset.
        # Wait for its lock instead of racing two guest cleanup commands.
        with self.path.with_suffix('.lock').open('a') as lock:
            deadline=time.monotonic()+120
            while True:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>deadline:raise ResetFailure('case did not release VM reset lock')
                    time.sleep(.1)
            session=read_session(self.path)
            reset_guest(runner,args,session,'supervisor')
            if Path(session['reset_failure_marker']).exists():
                raise ResetFailure('case reset failed; batch stopped even if supervisor recovered')

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.send_signal(signal.SIGINT)
                try:self.process.wait(timeout=90)
                except subprocess.TimeoutExpired:
                    # The owner performs its VM shutdown in finally. Escalate only
                    # this owned session group if that shutdown is unresponsive.
                    os.killpg(self.process.pid,signal.SIGKILL);self.process.wait()
            # QEMU can outlive an OOM/SIGKILL of the owner; reap its group too.
            if self.owner_record:stop_record(self.owner_record,timeout=5)
            self.path.unlink(missing_ok=True)
        if self.handle:self.handle.close()
