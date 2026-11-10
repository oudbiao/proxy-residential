"""Signal-aware process supervision and local, authenticated readiness checks."""
import base64
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import queue
import signal
import socket
import subprocess
import threading
import time
import urllib.request

from .config import client_configs, read_profiles
from .provision import json_text, write_private


@dataclass
class Child:
    name: str
    argv: list
    critical: bool = False
    process: object = field(default=None, init=False)
    restart_at: float = field(default=0, init=False)


class Supervisor:
    def __init__(self, children, restart_delay=2, shutdown_timeout=8):
        self.children = children
        self.restart_delay = restart_delay
        self.shutdown_timeout = shutdown_timeout
        self.stopping = False

    def _signal(self, *_):
        self.stopping = True

    def run(self):
        previous = {sig: signal.signal(sig, self._signal) for sig in (signal.SIGINT, signal.SIGTERM)}
        result = 0
        # frpc honors host proxy variables; the generated tunnel and direct outbound
        # configuration must determine routing even when Docker injects a proxy.
        child_env = {key: value for key, value in os.environ.items()
                     if key.lower() not in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')}
        try:
            while not self.stopping:
                now = time.monotonic()
                for child in self.children:
                    if self.stopping:
                        break
                    if child.process is not None and child.process.poll() is not None:
                        child.process.wait()
                        child.process = None
                        if child.critical:
                            result = 1
                            self.stopping = True
                            break
                        child.restart_at = now + self.restart_delay
                        print('proxy-jp: tunnel process exited; retrying', flush=True)
                    if child.process is None and now >= child.restart_at:
                        try:
                            child.process = subprocess.Popen(child.argv, start_new_session=True, env=child_env)
                        except OSError:
                            if child.critical:
                                raise
                            child.restart_at = time.monotonic() + self.restart_delay
                            print('proxy-jp: unable to start tunnel process; retrying', flush=True)
                time.sleep(.05)
        except OSError:
            result = 1
            print('proxy-jp: unable to start required process', flush=True)
        finally:
            processes = [c.process for c in self.children if c.process is not None]
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            deadline = time.monotonic() + self.shutdown_timeout
            for process in processes:
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    process.kill()
            for process in processes:
                process.wait()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        return result


def _online(config, health_deadline):
    probe_deadline = min(health_deadline, time.monotonic() + 1)
    admin = config['webServer']
    # Never connect to a host supplied outside the generated loopback admin API.
    if admin['addr'] != '127.0.0.1':
        return False
    auth = base64.b64encode((admin['user'] + ':' + admin['password']).encode()).decode()
    request = urllib.request.Request(f"http://127.0.0.1:{int(admin['port'])}/api/status", headers={'Authorization': 'Basic ' + auth})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    budget = probe_deadline - time.monotonic()
    if budget <= 0:
        return False
    with opener.open(request, timeout=budget) as response:
        # read1 returns currently available data instead of waiting for EOF. Check
        # time and size between chunks so a streaming API releases its worker.
        body = bytearray()
        while time.monotonic() < probe_deadline:
            chunk = response.read1(min(8192, 65537 - len(body)))
            if not chunk:
                status = json.loads(body)
                break
            body.extend(chunk)
            if len(body) > 65536:
                return False
        else:
            return False
    if not isinstance(status, dict):
        return False
    expected = {(p['type'], p['name']) for p in config['proxies']}
    running = {(kind, p.get('name')) for kind, proxies in status.items() if isinstance(proxies, list)
               for p in proxies if isinstance(p, dict) and p.get('status') == 'running'}
    return bool(expected) and expected <= running


def _client_links(directory, names, deadline):
    links = [{'tag': name, 'online': False} for name in names]
    pending = queue.Queue()
    completed = queue.Queue()
    for index in range(len(links)):
        pending.put(index)

    def probe():
        while time.monotonic() < deadline:
            try:
                index = pending.get_nowait()
            except queue.Empty:
                return
            try:
                config = json.loads((directory / f'frpc-{index}.json').read_text())
                online = _online(config, deadline)
            except (OSError, ValueError, KeyError, TypeError):
                online = False
            completed.put((index, online))

    # One-shot Docker health processes must be able to exit even if an API keeps
    # streaming bytes and never finishes JSON. Workers only publish to the queue;
    # they cannot mutate the returned snapshot after the overall deadline.
    for _ in range(min(32, len(links))):
        threading.Thread(target=probe, daemon=True).start()
    remaining = len(links)
    while remaining:
        budget = deadline - time.monotonic()
        if budget <= 0:
            break
        try:
            index, online = completed.get(timeout=budget)
        except queue.Empty:
            break
        links[index]['online'] = online
        remaining -= 1
    return links


def health(runtime_dir=Path('/run/proxy-jp')):
    """Return (Docker exit code, sanitized JSON): partial availability is healthy."""
    deadline = time.monotonic() + 4
    directory = Path(runtime_dir)
    try:
        state = json.loads((directory / 'health.json').read_text())
        if state['role'] == 'server':
            config = json.loads(Path(state['config']).read_text())
            host = config.get('bindAddr', '0.0.0.0')
            if host in ('0.0.0.0', '::'):
                host = '127.0.0.1' if host == '0.0.0.0' else '::1'
            with socket.create_connection((host, config.get('bindPort', 7000)), timeout=1):
                return 0, {'role': 'server', 'status': 'online'}
        links = _client_links(directory, state['names'], deadline)
        count = sum(link['online'] for link in links)
        return (0 if count else 1), {'role': 'client', 'status': 'online' if count == len(links) and count else 'partial' if count else 'offline', 'links': links}
    except (OSError, ValueError, KeyError, TypeError):
        return 1, {'status': 'unavailable'}


def main(command, args):
    directory = Path(args.runtime_dir).absolute()
    if command == 'health':
        code, status = health(directory)
        print(json.dumps(status, ensure_ascii=False))
        return code
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    if command == 'server':
        config = Path(args.config).absolute()
        write_private(directory / 'health.json', json_text({'role': 'server', 'config': str(config)}))
        return Supervisor([Child('frps', ['frps', '-c', str(config)], True)]).run()
    bundle = client_configs(read_profiles(os.environ), directory)
    write_private(directory / 'sing-box.json', json_text(bundle['sing_box']))
    children = [Child('sing-box', ['sing-box', 'run', '-c', str(directory / 'sing-box.json')], True)]
    for i, entry in enumerate(bundle['frpc']):
        path = directory / f'frpc-{i}.json'
        write_private(path, json_text(entry['config']))
        write_private(directory / f'ca-{i}.pem', entry['ca_pem'])
        children.append(Child(entry['name'], ['frpc', '-c', str(path)]))
    write_private(directory / 'health.json', json_text({'role': 'client', 'names': [entry['name'] for entry in bundle['frpc']]}))
    return Supervisor(children).run()
