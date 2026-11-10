import configparser
import json
import os
import shlex
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from proxy_jp.config import decode_profile, read_profiles
from proxy_jp.__main__ import render
from proxy_jp.provision import provision, write_private, surge_configuration, surge_proxy_line


class ProvisionTests(unittest.TestCase):
    def test_shared_surge_formatter_handles_multi_tag_group_collisions(self):
        from dataclasses import replace
        from test_config import profile
        profiles = [replace(profile(i), name=name) for i, name in enumerate(('日本出口', '日本出口-选择', '日本出口-选择-2'))]
        text = surge_configuration(profiles)
        parsed = configparser.ConfigParser(allow_no_value=True)
        parsed.optionxform = str
        parsed.read_string(text)
        group = next(iter(parsed['Proxy Group']))
        self.assertNotIn(group, parsed['Proxy'])
        self.assertEqual(parsed['Proxy Group'][group], 'select, ' + ', '.join(p.name for p in profiles))
        self.assertEqual(text.count('version=6, mode=default'), 3)
        self.assertIn(surge_proxy_line(profiles[0]), text)

    def test_certificates_outputs_modes_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            for host, checkarg in [('203.0.113.1', '-verify_ip'), ('vps.example.com', '-verify_hostname'), ('2001:db8::1', '-verify_ip')]:
                output = Path(tmp) / host
                result = provision(output, host, name='香港A')
                self.assertEqual(result, output)
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
                for f in output.iterdir():
                    self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600, f.name)
                verify = subprocess.run(['openssl', 'verify', '-CAfile', str(output / 'ca.pem'), checkarg, host, str(output / 'server.pem')], capture_output=True, text=True)
                self.assertEqual(verify.returncode, 0, verify.stderr)
                uri = (output / 'connection.txt').read_text().strip()
                p = decode_profile(uri)
                self.assertEqual(p.host, host)
                self.assertEqual(p.ca_pem, (output / 'ca.pem').read_text())
                self.assertEqual(json.loads((output / 'connection.json').read_text()), {'tag': '香港A', 'connection': uri})
                env = dict(x.split('=', 1) for x in shlex.split((output / 'japan.env').read_text()))
                self.assertEqual(read_profiles(env)[0], p)
                self.assertNotIn('PRIVATE KEY', p.ca_pem)
                self.assertNotIn('key', p.__dict__)
                frps = json.loads((output / 'frps.json').read_text())
                self.assertEqual(frps['bindPort'], 7000)
                self.assertEqual(frps['allowPorts'], [{'single': 8443}])
                self.assertTrue(frps['transport']['tls']['force'])
                self.assertEqual(frps['auth']['token'], p.token)
                compose = (output / 'compose.vps.yaml').read_text()
                self.assertIn('proxy-jp:0.1.0', compose)
                self.assertIn('"server"', compose)
                self.assertIn(f'{output.stat().st_uid}:{output.stat().st_gid}', compose)
                self.assertEqual(compose.count('/tcp'), 2)
                surge = (output / 'surge-proxy.conf').read_text()
                self.assertIn('version=6', surge)
                self.assertIn('mode=default', surge)
                self.assertGreaterEqual(len(p.snell_psk.encode('ascii')), 12)
                self.assertLessEqual(len(p.snell_psk.encode('ascii')), 255)
                self.assertIn('shadow-tls-version=3', surge)
                self.assertIn('shadow-tls-password=' + p.shadowtls_password, surge)
                with self.assertRaises(ValueError):
                    provision(output, host)
                self.assertEqual((output / 'connection.txt').read_text().strip(), uri)

    def test_invalid_input_leaves_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            for kwargs in [{'host': 'x\nsubjectAltName=DNS:evil'}, {'host': 'vps.example', 'name': 'a,b'}, {'host': 'vps.example', 'proxy_port': 7000}, {'host': 'vps.example', 'shadowtls_host': 'evil:443'}]:
                output = Path(tmp) / 'out'
                with self.assertRaises(ValueError):
                    provision(output, **kwargs)
                self.assertFalse(output.exists())

    def test_cli_init_render_and_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'vps'
            init = subprocess.run(['python3', '-m', 'proxy_jp', 'init', '--host', '203.0.113.1', '--output', str(output)], capture_output=True, text=True)
            self.assertEqual(init.returncode, 0, init.stderr)
            uri = (output / 'connection.txt').read_text().strip()
            env = dict(os.environ, VPS_LIST=json.dumps([{'tag': '东京B', 'connection': uri}]))
            target = Path(tmp) / 'rendered'
            render = subprocess.run(['python3', '-m', 'proxy_jp', 'render', '--output', str(target)], env=env, capture_output=True, text=True)
            self.assertEqual(render.returncode, 0, render.stderr)
            self.assertTrue((target / 'sing-box.json').exists())
            config = json.loads((target / 'frpc-0.json').read_text())
            self.assertEqual(config['transport']['tls']['trustedCaFile'], str(target / 'ca-0.pem'))
            self.assertEqual((target / 'ca-0.pem').read_text(), decode_profile(uri).ca_pem)
            error = subprocess.run(['python3', '-m', 'proxy_jp', 'render', '--output', str(target)], env=dict(os.environ, VPS_LIST='TOPSECRET'), capture_output=True, text=True)
            self.assertNotEqual(error.returncode, 0)
            self.assertNotIn('TOPSECRET', error.stderr)
            self.assertNotIn('Traceback', error.stderr)

    def test_cli_invalid_argument_does_not_echo_secret(self):
        error = subprocess.run(['python3', '-m', 'proxy_jp', 'init', '--host', '203.0.113.1', '--output', '/tmp/unused-proxyjp', '--proxy-port', 'TOPSECRET'], capture_output=True, text=True)
        self.assertNotEqual(error.returncode, 0)
        self.assertNotIn('TOPSECRET', error.stderr)
        self.assertNotIn('Traceback', error.stderr)

    def test_nested_output_creates_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'state' / 'vps-1'
            self.assertEqual(provision(target, '203.0.113.1'), target)

    def test_surge_group_never_collides_with_accepted_proxy_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i, name in enumerate(('日本出口', '日本出口-选择', 'JP')):
                output = Path(tmp) / str(i)
                provision(output, '203.0.113.1', name=name)
                config = configparser.ConfigParser(allow_no_value=True)
                config.optionxform = str
                config.read_string((output / 'surge-example.conf').read_text())
                self.assertEqual(list(config['Proxy']), [name])
                groups = list(config['Proxy Group'])
                self.assertEqual(len(groups), 1)
                self.assertNotIn(groups[0], config['Proxy'])
                self.assertEqual(config['Proxy Group'][groups[0]], 'select, ' + name)
                self.assertEqual(list(config['Rule']), ['FINAL,' + groups[0]])
                self.assertEqual(decode_profile((output / 'connection.txt').read_text().strip()).name, name)

    def test_render_write_failure_removes_only_new_output_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provision(root / 'vps', '203.0.113.1')
            env = {'VPS_LIST': json.dumps([json.loads((root / 'vps' / 'connection.json').read_text())])}
            target = root / 'rendered'
            sentinel = root / 'keep.txt'
            sentinel.write_text('keep')

            def fail_after_first_write(path, text):
                if path.name == 'frpc-0.json':
                    raise OSError('controlled disk write failure')
                write_private(path, text)

            with patch('proxy_jp.__main__.write_private', side_effect=fail_after_first_write):
                with self.assertRaises(OSError):
                    render(target, env)
            self.assertFalse(target.exists())
            self.assertEqual(sentinel.read_text(), 'keep')
            self.assertTrue((root / 'vps' / 'connection.txt').exists())
            self.assertEqual(render(target, env), target)
            (target / 'keep.txt').write_text('existing')
            with self.assertRaises(ValueError):
                render(target, env)
            self.assertEqual((target / 'keep.txt').read_text(), 'existing')
