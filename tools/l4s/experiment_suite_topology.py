"""Generate standalone Mininet topology source for the experiment suite.

This is a configuration contract. CPU checks cannot establish the guest kernel's
IFB/DualPI2 behavior or the measured propagation delay.
"""
from __future__ import annotations

import math
import re
from textwrap import dedent


def topology_source(switches: int, bandwidth_mbps: float, delay_ms: float, *,
                    background: bool = False, background_cc: str = 'cubic',
                    background_warmup_seconds: float = 4, background_link_delay_ms: float = 0,
                    performance_mode: bool = False, propagation_mode: str = 'endpoints') -> str:
    """Return a chain with endpoint-total or per-link one-way propagation.

    Each physical link has HTB plus DualPI2 in both directions. The link adjacent
    to the client uses bandwidth_mbps; all other links use 300 Mbps. Incoming
    traffic crosses separate IFBs carrying delay_ms of netem delay at the
    endpoints, or at every main-path receiving interface in per-link mode.
    Background-only access links add their own bidirectional delay. This keeps
    netem separate from the dropping AQM.
    """
    if isinstance(switches, bool) or not isinstance(switches, int) or switches < 1:
        raise ValueError("switches must be a positive integer")
    if not isinstance(background,bool) or (background and switches>254):
        raise ValueError('background must be boolean and supports at most 254 switches')
    if not isinstance(background_cc,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,31}',background_cc):
        raise ValueError('invalid background_cc')
    if (isinstance(background_warmup_seconds,bool) or not isinstance(background_warmup_seconds,(int,float))
            or not math.isfinite(background_warmup_seconds) or background_warmup_seconds<0):
        raise ValueError('background_warmup_seconds must be finite and nonnegative')
    for label, value, allow_zero in (
        ("bandwidth_mbps", bandwidth_mbps, False), ("delay_ms", delay_ms, True),
        ("background_link_delay_ms", background_link_delay_ms, True)
    ):
        if (isinstance(value, bool) or not isinstance(value, (float, int))
                or not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero)):
            raise ValueError(f"{label} must be finite and {'nonnegative' if allow_zero else 'positive'}")
    if propagation_mode not in ('endpoints','per-link'):raise ValueError('invalid propagation_mode')
    header = (
        '"""Generated suite topology; runtime acceptance requires guest evidence."""\n'
        "from __future__ import annotations\n\nimport subprocess\nimport json\nimport time\nimport re\nimport os\nfrom pathlib import Path\n\n"
        f"BACKGROUND = {background!r}\nBACKGROUND_CC = {background_cc!r}\n"
        f"BACKGROUND_WARMUP_SECONDS = {background_warmup_seconds!r}\n"
        f"BACKGROUND_RATE = {f'{bandwidth_mbps:g}M'!r}\n"
        f"SWITCHES = {switches!r}\n"
        f"PERFORMANCE_MODE = {performance_mode!r}\n"
        f"PROPAGATION_MODE = {propagation_mode!r}\n"
        f"BACKGROUND_LINK_DELAY_MS = {background_link_delay_ms!r}\n"
        f"PROPAGATION_DELAY_MS = {delay_ms!r}\n"
        f"BOTTLENECK = {f'{bandwidth_mbps:g}mbit'!r}\n"
        f"PROPAGATION_DELAY = {f'{delay_ms:g}ms'!r}\n"
        "LINK_RATE = '300mbit'\n"
        "BACKBONE_HTB_BURST = '512k'  # 512 KiB, matching the prior 300 Mbps profile.\n"
        "CLIENT_HTB_BURST = '32k'\n"
        "DUALPI2_TARGET = '15ms'\nDUALPI2_UPDATE = '16ms'\nDUALPI2_STEP = '5ms'\n"
    )
    source = header + dedent('''

        def background_pairs():
            return [dict(index=i, source=f'bgsrc{i}', sink=f'bgsink{i}',
                         source_role=f'bgsrc{i}_egress', sink_role=f'bgsink{i}_egress',
                         destination=f'10.64.{i}.2' if i<SWITCHES else f'10.65.{i}.2',
                         port=5200+i, shared_role=f's{i}_to_s{i+1}' if i<SWITCHES else f's{i}_to_client')
                    for i in range(1,SWITCHES+1)] if BACKGROUND else []


        def background_roles():
            return tuple(role for p in background_pairs() for role in
                         (p['source_role'],f"s{p['index']}_to_bgsrc",p['sink_role'],f"downstream_to_bgsink{p['index']}"))


        def is_client_link(role):
            return role in (f's{SWITCHES}_to_client',f'client_to_s{SWITCHES}')

        def physical_roles():
            names = ['server'] + [f's{i}' for i in range(1, SWITCHES + 1)] + ['client']
            return tuple(role for first, second in zip(names, names[1:])
                         for role in (f'{first}_to_{second}', f'{second}_to_{first}')) + background_roles()


        def background_delay_roles():
            for p in background_pairs():
                # Endpoint baseline supplies the same total path propagation as
                # the scene; the final forward hop already has client IFB delay.
                for role in (p['source_role'],p['sink_role']):
                    baseline = PROPAGATION_MODE=='endpoints' and not (role==p['sink_role'] and p['index']==SWITCHES)
                    yield role, PROPAGATION_DELAY_MS*baseline + BACKGROUND_LINK_DELAY_MS
                if BACKGROUND_LINK_DELAY_MS:
                    yield f"s{p['index']}_to_bgsrc", BACKGROUND_LINK_DELAY_MS
                    yield f"downstream_to_bgsink{p['index']}", BACKGROUND_LINK_DELAY_MS


        def main_delay_roles():
            endpoints = ((f'client_to_s{SWITCHES}','client_propagation'),('server_to_s1','server_propagation'))
            yield from endpoints
            if PROPAGATION_MODE=='per-link':
                for role in physical_roles():
                    if role not in background_roles() and role not in [r for r,_ in endpoints]:
                        yield role,role+'_propagation'


        METRIC_ROLES = physical_roles() + tuple(metric for _,metric in main_delay_roles()) + tuple(
            role+'_propagation' for role,delay in background_delay_roles() if delay)


        OUTPUT = None


        def set_output(output):
            global OUTPUT
            OUTPUT = output


        def process_command(node, name, command):
            if not PERFORMANCE_MODE:return command
            cpu = {'publisher':0,'relay':1,'bridge':3,'proxy':3}.get(name,3)
            return ['nice','-n','-10','taskset','-c',str(cpu),*command]


        def configure_performance(devices):
            # The host wrapper pins seven vCPUs. Keep OVS on guest CPU2,
            # transport endpoints on 0/1, background workers on 4/5/6.
            for role in physical_roles():
                node,device=devices[role]
                run_command(node,['ethtool','-K',device,'gro','off','gso','off','tso','off'])
            for pid in subprocess.check_output(['pgrep','-x','ovs-vswitchd|ovsdb-server'],text=True).split():
                for task in (Path('/proc')/pid/'task').iterdir():
                    os.sched_setaffinity(int(task.name),{2})
                    os.setpriority(os.PRIO_PROCESS,int(task.name),-10)
            for proc in Path('/proc').iterdir():
                if not proc.name.isdigit():continue
                try:
                    name=(proc/'comm').read_text().strip()
                    if name.startswith('ksoftirqd/') and name!='ksoftirqd/3':
                        os.setpriority(os.PRIO_PROCESS,int(proc.name),-10)
                except (FileNotFoundError,ProcessLookupError):pass
            if OUTPUT:
                (OUTPUT/'performance-profile.json').write_text(json.dumps(dict(
                    guest_cpus=7,ovs_cpu=2,nice=-10,offloads_disabled=['gro','gso','tso'],
                    main_process_cpus=dict(publisher=0,relay=1,bridge=3,proxy=3),
                    dualpi2_coupling_factor=1,dualpi2_target=DUALPI2_TARGET,dualpi2_update=DUALPI2_UPDATE,dualpi2_step=DUALPI2_STEP))+'\\n')
            # Preserve explicit caller congestion options; set the suite profile
            # before the guest runner constructs relay/subscriber commands.
            os.environ.setdefault('CAMERA_PLAYER_RELAY_CONGESTION_OPTIONS',
                'pacing_interval=1/4000,sudden_ce_threshold=off' if os.environ.get('CAMERA_PLAYER_MODE')=='prague' else '')
            os.environ.setdefault('CAMERA_PLAYER_SUBSCRIBER_CONGESTION_OPTIONS',
                'pacing_interval=1/4000,sudden_ce_threshold=1/1' if os.environ.get('CAMERA_PLAYER_MODE')=='prague' else '')


        def build_topology():
            from mininet.link import TCLink
            from mininet.net import Mininet
            from mininet.node import OVSBridge

            net = Mininet(controller=None, link=TCLink, switch=OVSBridge, autoSetMacs=True)
            server = net.addHost('server', ip='10.0.0.2/24')
            switches = [net.addSwitch(f's{i}', failMode='standalone')
                        for i in range(1, SWITCHES + 1)]
            client = net.addHost('client', ip='10.0.0.1/24')
            nodes = [server, *switches, client]
            devices = {}
            for first, second in zip(nodes, nodes[1:]):
                link = net.addLink(first, second)
                devices[f'{first.name}_to_{second.name}'] = (first, link.intf1.name)
                devices[f'{second.name}_to_{first.name}'] = (second, link.intf2.name)
            for p in background_pairs():
                i=p['index']
                src=net.addHost(p['source'],ip=f'10.64.{i}.1/24')
                sink=net.addHost(p['sink'],ip=p['destination']+'/24')
                first=net.addLink(src,switches[i-1])
                devices[p['source_role']]=(src,first.intf1.name)
                devices[f's{i}_to_bgsrc']=(switches[i-1],first.intf2.name)
                downstream=switches[i] if i<SWITCHES else client
                second=net.addLink(downstream,sink)
                devices[f'downstream_to_bgsink{i}']=(downstream,second.intf1.name)
                devices[p['sink_role']]=(sink,second.intf2.name)
            return net, client, server, devices


        def shaping_commands(device, rate, burst=CLIENT_HTB_BURST):
            return [
                ['tc', 'qdisc', 'replace', 'dev', device, 'root', 'handle', '10:', 'htb', 'default', '1'],
                ['tc', 'class', 'replace', 'dev', device, 'parent', '10:', 'classid', '10:1',
                 'htb', 'rate', rate, 'burst', burst, 'cburst', burst],
                ['tc', 'qdisc', 'replace', 'dev', device, 'parent', '10:1', 'handle', '20:',
                 'dualpi2', 'coupling_factor', '1', 'target', DUALPI2_TARGET, 'tupdate', DUALPI2_UPDATE,
                 'step_thresh', DUALPI2_STEP],
            ]


        def propagation_commands(device, ifb, delay=PROPAGATION_DELAY):
            # IFB lives in the endpoint's namespace. Physical egress has only
            # HTB/DualPI2; ingress redirects to a different device's netem root.
            return [
                ['ip', 'link', 'add', ifb, 'type', 'ifb'],
                ['ip', 'link', 'set', 'dev', ifb, 'up'],
                *([['ethtool','-K',ifb,'gro','off','gso','off','tso','off']] if PERFORMANCE_MODE else []),
                ['tc', 'qdisc', 'replace', 'dev', ifb, 'root', 'handle', '30:',
                 'netem', 'delay', delay, 'limit', '100000'],
                ['tc', 'qdisc', 'replace', 'dev', device, 'handle', 'ffff:', 'ingress'],
                ['tc', 'filter', 'replace', 'dev', device, 'parent', 'ffff:',
                 'protocol', 'all', 'pref', '1', 'u32', 'match', 'u32', '0', '0',
                 'action', 'mirred', 'egress', 'redirect', 'dev', ifb],
            ]


        def run_command(node, command):
            process = node.popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            output, _ = process.communicate()
            if process.returncode:
                raise RuntimeError(f"{' '.join(command)}: {output}")
            return output


        def configure_topology(devices, rate=None):
            subprocess.run(['modprobe', 'sch_dualpi2'], check=True)
            subprocess.run(['modprobe', 'ifb'], check=True)
            for role in physical_roles():
                node, device = devices[role]
                link_rate = (rate or BOTTLENECK) if is_client_link(role) else LINK_RATE
                # 32 KiB lasts only 0.87 ms at 300 Mbps; retain the larger
                # historical backbone bucket to tolerate guest scheduling gaps.
                burst = CLIENT_HTB_BURST if is_client_link(role) else BACKBONE_HTB_BURST
                for command in shaping_commands(device, link_rate, burst):
                    run_command(node, command)
            for index,(role,metric) in enumerate(main_delay_roles()):
                node, device = devices[role]
                ifb = 'ifb-delay' if metric in ('client_propagation','server_propagation') else f'ifb-main{index}'
                for command in propagation_commands(device, ifb):
                    run_command(node, command)
                devices[metric] = (node, ifb)
            if PERFORMANCE_MODE:configure_performance(devices)
            if BACKGROUND:
                configure_background(devices)
    ''')
    return source + (BACKGROUND_SOURCE if background else '')


BACKGROUND_SOURCE = dedent('''

    def configure_background(devices):
        # The final dedicated sink sits behind the client, without another switch.
        # Its data already crosses client_propagation on the shared final hop.
        client,main_dev=devices[f'client_to_s{SWITCHES}']
        _,tail_dev=devices[f'downstream_to_bgsink{SWITCHES}']
        last=background_pairs()[-1]
        src,src_dev=devices[last['source_role']]
        sink,sink_dev=devices[last['sink_role']]
        commands=[(client,['sysctl','-w','net.ipv4.ip_forward=1']),
                  (client,['ip','addr','add',f'10.64.{SWITCHES}.254/24','dev',main_dev]),
                  (client,['ip','addr','add',f'10.65.{SWITCHES}.254/24','dev',tail_dev]),
                  (src,['ip','route','replace',f'10.65.{SWITCHES}.0/24','via',f'10.64.{SWITCHES}.254','dev',src_dev]),
                  (sink,['ip','route','replace',f'10.64.{SWITCHES}.0/24','via',f'10.65.{SWITCHES}.254','dev',sink_dev])]
        for node,command in commands:run_command(node,command)
        for p in background_pairs():
            for role in (p['source_role'],p['sink_role']):
                node,dev=devices[role]
                run_command(node,['sysctl','-w','net.ipv4.tcp_ecn=0'])
        for index,(role,delay) in enumerate(background_delay_roles()):
            if not delay:continue
            node,dev=devices[role]
            ifb=f'ifb-bg{index}'
            for command in propagation_commands(dev,ifb,f'{delay:g}ms'):
                run_command(node,command)
            devices[role+'_propagation']=(node,ifb)



    def background_command(pair):
        return ['iperf3','-4','-c',pair['destination'],'-p',str(pair['port']),
                '-C',BACKGROUND_CC,'-b',BACKGROUND_RATE,'-t','86400','-i','0.1','--forceflush']


    def assert_background(devices):
        connections={}
        for p in background_pairs():
            node,_=devices[p['source_role']]
            state=run_command(node,['ss','-Htin','state','established','dport','=',str(p['port'])])
            # iperf3 has a control socket as well as its one data socket.
            sockets=re.findall(r'\\d+\\.\\d+\\.\\d+\\.\\d+:\\d+\\s+\\d+\\.\\d+\\.\\d+\\.\\d+:'+str(p['port'])+r'\\b',state)
            if len(sockets)<2:raise RuntimeError(f"background flow {p['port']} lacks its established TCP data connection")
            connections[str(p['port'])]=state.strip()
        return connections


    def received_background_data(log):
        if not log.is_file():return False
        return any(float(value)>0 for value in re.findall(
            r'\\[\\s*\\d+\\]\\s+[\\d.]+-\\s*[\\d.]+\\s+sec\\s+([\\d.]+)\\s+[KMGT]?Bytes\\b',
            log.read_text(errors='replace')))


    def start_background(devices,output):
        processes=[]
        records=[]
        config=dict(flow_count=SWITCHES,cc=BACKGROUND_CC,tcp_ecn_sysctl=0,target_rate=BACKGROUND_RATE,
                    one_way_delay=f'{PROPAGATION_DELAY_MS+2*BACKGROUND_LINK_DELAY_MS:g}ms',
                    scene_one_way_delay=f'{PROPAGATION_DELAY_MS*(SWITCHES+1 if PROPAGATION_MODE=="per-link" else 1):g}ms',
                    propagation_mode=PROPAGATION_MODE,background_link_delay_ms=BACKGROUND_LINK_DELAY_MS,
                    performance_mode=PERFORMANCE_MODE,warmup_seconds=BACKGROUND_WARMUP_SECONDS,pairs=background_pairs(),status='STARTING')
        try:
            # Start every listener before any client; logs are usable on failure too.
            for server in (True,False):
                for p in background_pairs():
                    node,_=devices[p['sink_role'] if server else p['source_role']]
                    name=f"background-{p['port']}-{'server' if server else 'client'}"
                    command=(['iperf3','-4','-s','-1','-B',p['destination'],'-p',str(p['port']),'-i','0.1','--forceflush']
                             if server else background_command(p))
                    handle=(output/f'{name}.log').open('wb')
                    if PERFORMANCE_MODE:
                        cpu=(4 if p['index']%2 else 0) if not server else (5 if p['index']%2 else 6)
                        command=['nice','-n','-10','taskset','-c',str(cpu),*command]
                    try:process=node.popen(command,stdout=handle,stderr=subprocess.STDOUT)
                    except BaseException:
                        handle.close()
                        raise
                    processes.append((process,handle))
                    records.append(dict(name=name,node=node.name,pid=process.pid,command=command))
                if server:time.sleep(.2)
            deadline=time.monotonic()+max(10,6*float(PROPAGATION_DELAY[:-2])/1000+5)
            last_error='receiver has not logged any payload'
            while True:
                if any(p.poll() is not None for p,_ in processes):
                    raise RuntimeError('background iperf exited during startup; see background client/server logs')
                try:
                    assert_background(devices)
                    if all(received_background_data(output/f"background-{p['port']}-server.log") for p in background_pairs()):
                        break
                except RuntimeError as error:last_error=str(error)
                if time.monotonic()>=deadline:
                    raise RuntimeError(f'background data did not become ready: {last_error}; see background logs')
                time.sleep(.1)
            config.update(data_ready_unix_ns=time.time_ns(),data_ready_monotonic_ns=time.monotonic_ns())
            time.sleep(BACKGROUND_WARMUP_SECONDS)  # Starts only after all receivers see data.
            if any(p.poll() is not None for p,_ in processes):
                raise RuntimeError('background iperf exited during startup; see background client/server logs')
            config.update(status='PASS',established_connections=assert_background(devices),
                          ready_unix_ns=time.time_ns(),ready_monotonic_ns=time.monotonic_ns())
            return processes
        except BaseException as error:
            config.update(status='FAIL',error=f'{type(error).__name__}: {error}')
            (output/'background-failure.txt').write_text(config['error']+'\\n')
            for process,handle in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=3)
                    except subprocess.TimeoutExpired:process.kill();process.wait()
                handle.close()
            raise
        finally:
            (output/'background-config.json').write_text(json.dumps(config,indent=2)+'\\n')
            (output/'background-processes.json').write_text(json.dumps(records,indent=2)+'\\n')
''')
