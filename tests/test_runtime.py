import base64
import http.server
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.025)
    raise AssertionError('condition timed out')


class RuntimeTests(unittest.TestCase):
    def supervisor(self, directory, critical_code, recoverable_code, env=None):
        code = '''from proxy_jp.runtime import Child, Supervisor
import sys
raise SystemExit(Supervisor([
 Child('core', [sys.executable, '-c', sys.argv[2], sys.argv[1] + '/core'], True),
 Child('a', [sys.executable, '-c', sys.argv[3], sys.argv[1] + '/a']),
 Child('b', [sys.executable, '-c', sys.argv[3], sys.argv[1] + '/b'])], restart_delay=.1, shutdown_timeout=.3).run())'''
        return subprocess.Popen([sys.executable, '-c', code, str(directory), critical_code, recoverable_code], cwd=ROOT, env=env)

    def test_frpc_restart_preserves_sibling_and_sigterm_reaps_all(self):
        fixture = "import os,sys,time; open(sys.argv[1], 'a').write(str(os.getpid())+'\\n'); time.sleep(60)"
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            proc = self.supervisor(d, fixture, fixture)
            try:
                wait_for(lambda: all((d / x).exists() for x in ('core', 'a', 'b')))
                old = int((d / 'a').read_text().splitlines()[0])
                sibling = int((d / 'b').read_text().strip())
                os.kill(old, signal.SIGKILL)
                wait_for(lambda: len((d / 'a').read_text().splitlines()) == 2)
                os.kill(sibling, 0)
                proc.terminate()
                self.assertEqual(proc.wait(3), 0)
                for path in d.iterdir():
                    for pid in path.read_text().splitlines():
                        with self.assertRaises(ProcessLookupError):
                            os.kill(int(pid), 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def test_critical_exit_stops_stubborn_siblings_with_bounded_escalation(self):
        core = "import os,sys,time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(.4); sys.exit(7)"
        stubborn = "import os,sys,time,signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)"
        with tempfile.TemporaryDirectory() as tmp:
            proc = self.supervisor(Path(tmp), core, stubborn)
            wait_for(lambda: all((Path(tmp) / x).exists() for x in ('core', 'a', 'b')))
            self.assertEqual(proc.wait(4), 1)
            for path in Path(tmp).iterdir():
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(path.read_text()), 0)

    def test_supervised_children_ignore_inherited_proxy_environment(self):
        fixture = "import os,sys,time,json; open(sys.argv[1], 'w').write(json.dumps({'proxy_keys': [k for k in os.environ if k.lower() in ('http_proxy','https_proxy','all_proxy','no_proxy')], 'keep': os.environ.get('PROXYJP_KEEP')})); time.sleep(60)"
        env = dict(os.environ, HTTP_PROXY='http://127.0.0.1:9', https_proxy='http://127.0.0.1:9', PROXYJP_KEEP='preserved')
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            proc = self.supervisor(d, fixture, fixture, env)
            try:
                wait_for(lambda: all((d / x).exists() for x in ('core', 'a', 'b')))
                for path in d.iterdir():
                    self.assertEqual(json.loads(path.read_text()), {'proxy_keys': [], 'keep': 'preserved'})
            finally:
                proc.terminate()
                proc.wait(3)

    def test_health_authenticates_status_and_reports_partial_without_secrets(self):
        from proxy_jp import runtime
        expected = 'Basic ' + base64.b64encode(b'proxy-jp:SECRET').decode()
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get('Authorization') != expected or self.path != '/api/status':
                    self.send_error(401)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'tcp': [{'name': 'snell', 'status': 'running'}]}).encode())
            def log_message(self, *_): pass
        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                d = Path(tmp)
                config = {'webServer': {'addr': '127.0.0.1', 'port': server.server_port, 'user': 'proxy-jp', 'password': 'SECRET'}, 'proxies': [{'name': 'snell', 'type': 'tcp'}]}
                (d / 'frpc-0.json').write_text(json.dumps(config))
                (d / 'health.json').write_text(json.dumps({'role': 'client', 'names': ['日本A', '日本B']}))
                (d / 'frpc-1.json').write_text(json.dumps({**config, 'webServer': {**config['webServer'], 'password': 'wrong'}}))
                code, result = runtime.health(d)
                self.assertEqual(code, 0)
                self.assertEqual(result['status'], 'partial')
                self.assertEqual([x['online'] for x in result['links']], [True, False])
                self.assertNotIn('SECRET', json.dumps(result))
                server.shutdown()
                self.assertEqual(runtime.health(d)[0], 1)
        finally:
            server.server_close()
            thread.join(2)

    def test_noncritical_spawn_failure_retries_without_stopping_real_siblings(self):
        code = '''from pathlib import Path
import os,signal,subprocess,sys,time
from proxy_jp import runtime
root = Path(sys.argv[1])
real_popen = runtime.subprocess.Popen
attempts = 0
fixture = "import os,sys,time; open(sys.argv[1], 'a').write(str(os.getpid())+'\\\\n'); time.sleep(60)"
def spawn(argv, **kwargs):
 global attempts
 if argv[-1] == str(root / 'target'):
  attempts += 1
  if attempts == 2:
   (root / 'failed').write_text('spawn failed')
   raise OSError('controlled one-time resource failure')
 return real_popen(argv, **kwargs)
runtime.subprocess.Popen = spawn
raise SystemExit(runtime.Supervisor([
 runtime.Child('core', [sys.executable, '-c', fixture, str(root / 'core')], True),
 runtime.Child('sibling', [sys.executable, '-c', fixture, str(root / 'sibling')]),
 runtime.Child('target', [sys.executable, '-c', fixture, str(root / 'target')])], restart_delay=.1, shutdown_timeout=.3).run())'''
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            proc = subprocess.Popen([sys.executable, '-c', code, tmp], cwd=ROOT)
            try:
                wait_for(lambda: all((d / x).exists() for x in ('core', 'sibling', 'target')))
                original = {x: int((d / x).read_text().strip()) for x in ('core', 'sibling', 'target')}
                os.kill(original['target'], signal.SIGKILL)
                wait_for(lambda: (d / 'failed').exists())
                wait_for(lambda: len((d / 'target').read_text().splitlines()) == 2, timeout=2)
                self.assertIsNone(proc.poll())
                for x in ('core', 'sibling'):
                    self.assertEqual(int((d / x).read_text().strip()), original[x])
                    os.kill(original[x], 0)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(3)

    def test_health_streaming_apis_release_workers_for_healthy_final_tag(self):
        from proxy_jp import runtime
        expected = 'Basic ' + base64.b64encode(b'proxy-jp:SECRET').decode()
        stop = threading.Event()
        authenticated = []
        class Stalled(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get('Authorization') != expected or self.path != '/api/status':
                    self.send_error(401)
                    return
                authenticated.append(True)
                self.send_response(200)
                self.end_headers()
                # Keep individual socket reads alive to require an overall deadline.
                try:
                    while not stop.wait(.1):
                        self.wfile.write(b' ')
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *_): pass
        class Healthy(Stalled):
            def do_GET(self):
                if self.headers.get('Authorization') != expected or self.path != '/api/status':
                    self.send_error(401)
                    return
                authenticated.append('healthy')
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"tcp":[{"name":"snell","status":"running"}]}')
        class Server(http.server.ThreadingHTTPServer):
            request_queue_size = 64
        servers = [Server(('127.0.0.1', 0), handler) for handler in (Stalled, Healthy)]
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
        for thread in threads:
            thread.start()
        # Baseline must finish too: release the stalled responses after 6 seconds.
        timer = threading.Timer(6, stop.set)
        timer.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                d = Path(tmp)
                names = [f'日本{i}' for i in range(33)]
                (d / 'health.json').write_text(json.dumps({'role': 'client', 'names': names}))
                for i in range(33):
                    config = {'webServer': {'addr': '127.0.0.1', 'port': servers[i == 32].server_port, 'user': 'proxy-jp', 'password': 'SECRET'}, 'proxies': [{'name': 'snell', 'type': 'tcp'}]}
                    (d / f'frpc-{i}.json').write_text(json.dumps(config))
                start = time.monotonic()
                process = subprocess.run([sys.executable, '-m', 'proxy_jp', 'health', '--runtime-dir', str(d)], cwd=ROOT, capture_output=True, text=True, timeout=5.5)
                code, result = process.returncode, json.loads(process.stdout)
                elapsed = time.monotonic() - start
                self.assertEqual(code, 0)
                self.assertEqual(result['status'], 'partial')
                self.assertEqual([x['tag'] for x in result['links']], names)
                self.assertEqual([x['online'] for x in result['links']], [False] * 32 + [True])
                self.assertEqual(len(authenticated), 33)
                self.assertEqual(authenticated.count('healthy'), 1)
                self.assertLess(elapsed, 5, 'health must finish comfortably before Docker timeout')
        finally:
            stop.set()
            timer.cancel()
            timer.join()
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(2)

    def test_health_rejects_oversized_http_status_body(self):
        from proxy_jp import runtime
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                expected = 'Basic ' + base64.b64encode(b'proxy-jp:SECRET').decode()
                if self.headers.get('Authorization') != expected or self.path != '/api/status':
                    self.send_error(401)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b' ' * (64 * 1024) + b'{"tcp":[{"name":"snell","status":"running"}]}')
            def log_message(self, *_): pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                d = Path(tmp)
                (d / 'health.json').write_text(json.dumps({'role': 'client', 'names': ['日本']}))
                config = {'webServer': {'addr': '127.0.0.1', 'port': server.server_port, 'user': 'proxy-jp', 'password': 'SECRET'}, 'proxies': [{'name': 'snell', 'type': 'tcp'}]}
                (d / 'frpc-0.json').write_text(json.dumps(config))
                code, result = runtime.health(d)
                self.assertEqual(code, 1)
                self.assertEqual(result['links'], [{'tag': '日本', 'online': False}])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
