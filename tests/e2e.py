#!/usr/bin/env python3
"""Run real three-relay Snell6/ShadowTLS3 traffic on isolated Docker networks.

Requires Docker, local built image, Python3 and OpenSSL. No published ports,
external services, live VPS, or existing Docker resources are used. Set
PROXY_JP_TEST_IMAGE to a tag or immutable image ID; the resolved ID is pinned
for the whole run. All uniquely named Docker resources are removed in finally.
"""
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_jp.config import decode_profile, encode_profile
from proxy_jp.provision import provision

ROOT = Path(__file__).resolve().parents[1]


def command(*args, check=True):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=90)
    if check and result.returncode:
        # Commands can contain profiles; never include argv in diagnostic output.
        raise RuntimeError('Command failed: ' + result.stderr[-2000:])
    return result


def eventually(action, timeout=45):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            return action()
        except (AssertionError, RuntimeError, ValueError, KeyError) as error:
            last = error
            time.sleep(1)
    raise AssertionError('Timed out waiting for test condition: ' + str(last))


class Harness:
    def __init__(self):
        self.prefix = 'proxyjp-e2e-' + uuid.uuid4().hex[:10]
        self.containers = []
        self.networks = []
        self.image = command('docker', 'image', 'inspect', os.environ.get('PROXY_JP_TEST_IMAGE', 'proxy-jp:0.1.0'), '--format', '{{.Id}}').stdout.strip()
        self.temp = tempfile.TemporaryDirectory(prefix=self.prefix)
        self.base = Path(self.temp.name)
        self.front = self.prefix + '-front'
        self.egress = self.prefix + '-egress'

    def docker(self, *args, **kwargs):
        return command('docker', *args, **kwargs)

    def run(self, suffix, network, *args, entry=None, extra=()):
        name = self.prefix + '-' + suffix
        self.containers.append(name)
        opts = ['run', '-d', '--name', name, '--network', network, '--network-alias', suffix,
                '--user', '0:0', '--read-only', '--tmpfs', '/tmp', '--tmpfs', '/run/proxy-jp',
                '-v', str(self.base) + ':/fixture:ro', '-v', str(ROOT / 'tests/e2e_target.py') + ':/fixture-target.py:ro']
        if entry:
            opts += ['--entrypoint', entry]
        self.docker(*opts, *extra, self.image, *args)
        return name

    def exec(self, name, *args, **kwargs):
        return self.docker('exec', name, *args, **kwargs)

    def ip(self, name, network):
        return json.loads(self.docker('inspect', name).stdout)[0]['NetworkSettings']['Networks'][network]['IPAddress']

    def health(self, expected, name=None):
        result = self.exec(name or self.japan, 'python', '-m', 'proxy_jp', 'health', check=False)
        data = json.loads(result.stdout)
        assert data['status'] == expected, data
        return data

    def traffic(self, index, protocol='tcp', host='target.invalid'):
        result = self.exec(self.consumer, 'python', '/fixture-target.py', 'probe', str(1080 + index), protocol, host)
        data = json.loads(result.stdout)
        assert data['peer'] == self.japan_ip, data
        if protocol == 'udp':
            assert data['data'] == 'japan-udp', data
        return data

    def no_traffic(self, index):
        result = self.exec(self.consumer, 'python', '/fixture-target.py', 'probe', str(1080 + index), 'tcp', check=False)
        assert result.returncode != 0, 'Unexpected successful traffic'

    def profiles_env(self, profiles):
        return json.dumps([{'tag': p.name, 'connection': encode_profile(p)} for p in profiles], ensure_ascii=False)

    def setup(self):
        for network in (self.front, self.egress):
            self.docker('network', 'create', '--internal', network)
            self.networks.append(network)
        cover = provision(self.base / 'cover', 'cover.test', name='cover')
        # Start target before its IP is known; the fixture discovers its egress IP.
        self.target = self.run('target', self.egress, '-c',
            "import socket,sys;sys.path.insert(0,'/');from importlib.machinery import SourceFileLoader;m=SourceFileLoader('fixture','/fixture-target.py').load_module();m.serve(socket.gethostbyname(socket.gethostname()),'/fixture/cover/server.pem','/fixture/cover/server.key')", entry='python')
        self.target_ip = self.ip(self.target, self.egress)
        self.profiles, self.relays = [], []
        for i in range(3):
            state = provision(self.base / ('vps' + str(i)), 'vps' + str(i), name='中继' + str(i), shadowtls_host='cover.test')
            p = decode_profile((state / 'connection.txt').read_text().strip())
            self.profiles.append(p)
            relay = self.run('vps' + str(i), self.front, 'server', '--config', '/fixture/vps' + str(i) + '/frps.json',
                             extra=('-v', str(state) + ':/state:ro'))
            self.relays.append(relay)
            eventually(lambda: self.health('online', relay))
        self.japan = self.run('japan', self.egress, 'client', extra=(
            '--dns', self.target_ip, '--add-host', 'cover.test:' + self.target_ip,
            '-e', 'VPS_LIST=' + self.profiles_env(self.profiles)))
        self.docker('network', 'connect', self.front, self.japan)
        self.japan_ip = self.ip(self.japan, self.egress)
        eventually(lambda: self.health('online'))
        inbounds, outbounds, rules = [], [], []
        for i, p in enumerate(self.profiles):
            for variant in ('good', 'bad-snell', 'bad-shadowtls'):
                n = i if variant == 'good' else 3 + (variant == 'bad-shadowtls')
                if variant != 'good' and i != 0:
                    continue
                tag = 'node' + str(n)
                inbounds.append({'type': 'socks', 'tag': 'in' + str(n), 'listen': '127.0.0.1', 'listen_port': 1080 + n})
                outbounds += [
                    {'type': 'snell', 'tag': tag, 'server': p.host, 'server_port': p.proxy_port,
                     'version': 6, 'mode': 'default', 'psk': p.snell_psk if variant != 'bad-snell' else 'wrong-psk-123456', 'detour': tag + '-tls'},
                    {'type': 'shadowtls', 'tag': tag + '-tls', 'server': p.host, 'server_port': p.proxy_port,
                     'version': 3, 'password': p.shadowtls_password if variant != 'bad-shadowtls' else 'wrong-password',
                     'tls': {'enabled': True, 'server_name': 'cover.test', 'certificate_path': '/fixture/cover/ca.pem'}}]
                rules.append({'inbound': ['in' + str(n)], 'outbound': tag})
        config = {'log': {'level': 'info'}, 'inbounds': inbounds, 'outbounds': outbounds,
                  'dns': {'servers': [{'type': 'local', 'tag': 'local'}]},
                  'route': {'rules': rules, 'default_domain_resolver': 'local'}}
        (self.base / 'consumer.json').write_text(json.dumps(config))
        self.consumer = self.run('consumer', self.front, 'run', '-c', '/fixture/consumer.json', entry='sing-box')
        self.exec(self.consumer, 'sing-box', 'check', '-c', '/fixture/consumer.json')
        self.exec(self.japan, 'sing-box', 'check', '-c', '/run/proxy-jp/sing-box.json')
        for i in range(3):
            self.exec(self.japan, 'frpc', 'verify', '-c', '/run/proxy-jp/frpc-' + str(i) + '.json')
            self.exec(self.relays[i], 'frps', 'verify', '-c', '/state/frps.json')

    def scenarios(self):
        for name in [self.consumer] + self.relays:
            self.exec(name, 'python', '/fixture-target.py', 'isolate', self.target_ip)
        print('PASS frontend consumer/all VPS cannot resolve or reach target directly', flush=True)
        for i in range(3):
            eventually(lambda: self.traffic(i))
            eventually(lambda: self.traffic(i, 'udp'))
            self.traffic(i, 'udp', self.target_ip)
            print('PASS relay', i, 'Snell6/ShadowTLS3 TCP hostname + UDP hostname/IP; Japan source', self.japan_ip, flush=True)
        events = json.loads(self.exec(self.consumer, 'python', '/fixture-target.py', 'probe', '1080', 'tcp', self.target_ip, '/events').stdout)
        query = bytes.fromhex(events[0]['query']) if events else b''
        assert events and all(e['peer'] == self.japan_ip for e in events), events
        assert any(b'target\x07invalid' in bytes.fromhex(e['query']) for e in events), query
        print('PASS target hostname resolved only from Japan egress', flush=True)
        self.no_traffic(3)
        self.no_traffic(4)
        print('PASS bad Snell and ShadowTLS credentials rejected', flush=True)
        for i in range(3):
            self.docker('stop', '-t', '3', self.relays[i])
            data = eventually(lambda: self.health('partial'))
            assert [e['online'] for e in data['links']] == [j != i for j in range(3)], data
            self.no_traffic(i)
            for j in range(3):
                if i != j:
                    self.traffic(j)
            self.docker('start', self.relays[i])
            eventually(lambda: self.health('online'))
            eventually(lambda: self.traffic(i))
            print('PASS relay', i, 'individual outage / partial health / reconnect', flush=True)
        # Kill actual supervised frpc; unchanged siblings and a new PID prove isolation.
        pid_script = "import os,json;print(json.dumps({p:open('/proc/'+p+'/cmdline','rb').read().decode().replace(chr(0),' ') for p in os.listdir('/proc') if p.isdigit() and os.path.exists('/proc/'+p+'/cmdline')}))"
        def pids():
            data = json.loads(self.exec(self.japan, 'python', '-c', pid_script).stdout)
            return {next(x for x in v.split() if x.endswith('.json')): k for k, v in data.items() if v.startswith('frpc ')}
        before = pids()
        key = '/run/proxy-jp/frpc-0.json'
        self.exec(self.japan, 'python', '-c', 'import os,signal;os.kill(' + before[key] + ',signal.SIGKILL)')
        def recovered():
            after = pids()
            assert after.get(key) and after[key] != before[key], after
            assert all(after[k] == v for k, v in before.items() if k != key), after
            return self.health('online')
        eventually(recovered)
        self.traffic(0)
        print('PASS abrupt frpc death recovered; sibling PIDs retained', flush=True)
        self.docker('stop', '-t', '3', self.japan)
        for i in range(3):
            self.no_traffic(i)
        print('PASS Japan stopped: all three data paths fail', flush=True)
        # Reuse the same relay endpoint after Japan stops; no valid client masks rejection.
        for variant in ('ca', 'token'):
            bad = dataclasses.replace(self.profiles[0], **({'ca_pem': (self.base / 'cover/ca.pem').read_text()} if variant == 'ca' else {'token': 'wrong-token'}))
            reject = self.run('reject-' + variant, self.front, 'client', extra=('-e', 'VPS_LIST=' + self.profiles_env([bad]),))
            eventually(lambda: self.health('offline', reject))
            time.sleep(3)
            self.health('offline', reject)
            source = reject if variant == 'ca' else self.relays[0]
            result = self.docker('logs', source)
            logs = (result.stdout + result.stderr).lower()
            assert ('certificate signed by unknown authority' in logs if variant == 'ca' else "token in login doesn't match" in logs), 'Missing explicit rejection log'
            self.docker('stop', '-t', '3', reject)
            print('PASS wrong', variant, 'rejected by actual frp TLS/auth', flush=True)

    def cleanup(self):
        for name in reversed(self.containers):
            self.docker('rm', '-f', name, check=False)
        for network in reversed(self.networks):
            self.docker('network', 'rm', network, check=False)
        self.temp.cleanup()


def main():
    harness = Harness()
    print('Immutable test image:', harness.image, flush=True)
    print('Isolated resource prefix:', harness.prefix, flush=True)
    try:
        harness.setup()
        harness.scenarios()
        print('PASS all isolated three-relay scenarios', flush=True)
    except BaseException:
        for name in harness.containers:
            logs = harness.docker('logs', '--tail', '15', name, check=False)
            # Binary logs may include auth errors but never dump generated config/env.
            print(name, logs.stdout, logs.stderr, file=sys.stderr)
        raise
    finally:
        harness.cleanup()
        print('Cleaned isolated containers/networks', flush=True)


if __name__ == '__main__':
    main()
