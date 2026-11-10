"""External commands are simulated; installer files and wrapper execute for real."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from test_config import profile
from proxy_jp.config import encode_profile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('macos_installer', ROOT / 'scripts/install-macos.py')
mac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mac)


class MacTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(mac, 'native_arch') and hasattr(mac, 'command'), 'headless native architecture and command orchestration are missing')
        self.tmp = tempfile.TemporaryDirectory(prefix='pj-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / 'install'
        self.compose = self.root / 'compose.yaml'
        self.compose.write_text('private source')
        archive = self.root / 'arm.tar.gz'
        archive.write_bytes(b'offline arm')
        amd = self.root / 'amd.tar.gz'
        amd.write_bytes(b'offline intel')
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text(json.dumps({'format_version': 1, 'tags': ['JP'], 'images': {
            arch: {'architecture': arch, 'os': 'linux', 'tag': 'proxy-jp:0.1.0', 'file': path.name,
                   'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'accepted_image_ids': ['sha256:verified']}
            for arch, path in [('arm64', archive), ('amd64', amd)]}}))
        self.config = {'services': {'japan': {'image': 'proxy-jp:0.1.0', 'restart': 'unless-stopped',
            'environment': {'VPS_LIST': json.dumps([{'tag': 'JP', 'connection': encode_profile(profile())}])}}}}
        self.calls = []
        self.fv = 'FileVault is Off.'
        self.cap = ' sleep\n autorestart\n'
        self.power = ' sleep 0\n autorestart 1\n'
        self.running = True
        self.loaded_id = 'sha256:verified'
        self.loaded_arch = 'arm64'
        self.repo_digests = ['ghcr.io/oudbiao/proxy-residential@sha256:' + 'a' * 64]
        self.loaded_os = 'linux'
        self.arm = '1'
        self.macos = '26.0'
        self.lima_version = '2.3.0'
        self.health = {'status': 'partial', 'links': [{'tag': 'JP', 'online': False}]}
        self.fail_on = None
        self.launch_state = 'state = running\n pid = 1234\n'
        self.daemon = self.root / 'daemon.plist'

    def command(self, argv, env=None, check=True):
        a = list(map(str, argv))
        self.calls.append((a, env))
        if self.fail_on and self.fail_on in a:
            raise subprocess.CalledProcessError(1, a, output='TOPSECRET', stderr='TOPSECRET')
        if a[-2:] == ['hw.optional.arm64', '-n']: return self.arm
        if 'hw.optional.arm64' in a: return self.arm
        if 'kern.hv_support' in a: return '1'
        if a[0] == 'sw_vers': return self.macos
        if a[0] == 'uname': return 'x86_64'
        if a[0] == 'fdesetup': return self.fv
        if a[:3] == ['pmset', '-g', 'cap']: return self.cap
        if a[:3] == ['pmset', '-g', 'custom']: return self.power
        if '--version' in a: return 'limactl version ' + self.lima_version
        if 'config' in a and '--format' in a: return json.dumps(self.config)
        if 'list' in a: return ''
        if 'autostart' in a and 'enable' in a:
            self.daemon.write_bytes(plistlib.dumps({'Label': 'io.lima-vm.daemon.proxy-jp', 'UserName': mac.getpass.getuser(),
                'EnvironmentVariables': {'LIMA_HOME': str(self.target / 'lima')},
                'ProgramArguments': ['/fake/limactl', 'start', 'proxy-jp', '--foreground']}))
        if 'image' in a and 'inspect' in a: return json.dumps([{'Id': self.loaded_id, 'Architecture': self.loaded_arch, 'Os': self.loaded_os, 'RepoDigests': self.repo_digests}])
        if 'ps' in a: return 'container-id'
        if 'inspect' in a: return json.dumps([{'State': {'Running': self.running}, 'HostConfig': {'RestartPolicy': {'Name': 'unless-stopped'}}}])
        if a[:2] == ['launchctl', 'print']: return self.launch_state
        if 'exec' in a: return json.dumps(self.health)
        return ''

    def install(self, *extra):
        output = io.StringIO()
        with patch.object(mac, 'command', self.command), patch.object(mac.os, 'getuid', return_value=501), patch.object(mac.sys, 'platform', 'darwin'), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                code = mac.main(['--compose', str(self.compose), *([] if '--image' in extra else ['--manifest', str(self.manifest)]), '--install-dir', str(self.target),
                                 '--docker-cli', '/fake/docker', '--compose-cli', '/fake/docker-compose', '--limactl', '/fake/limactl',
                                 '--daemon-path', str(self.daemon), *extra])
            except SystemExit as error:
                code = error.code
        return code, output.getvalue()

    def online(self):
        ref = 'ghcr.io/oudbiao/proxy-residential@sha256:' + 'a' * 64
        self.config['services']['japan']['image'] = ref
        self.compose.write_text(json.dumps(self.config))
        self.manifest.unlink()
        (self.root / 'arm.tar.gz').unlink()
        (self.root / 'amd.tar.gz').unlink()
        return ref

    def test_online_dry_run_needs_no_manifest_archive_or_registry(self):
        ref = self.online()
        code, out = self.install('--image', ref, '--dry-run')
        self.assertEqual(code, 0, out)
        self.assertIn('deferred', out)
        self.assertIn(ref, out)
        self.assertFalse(self.target.exists())
        self.assertFalse(any(set(a) & {'pull', 'load', 'create', 'start', 'up'} for a, _ in self.calls))

    def test_online_immutable_pull_uses_only_owned_engine_and_exact_copied_reference(self):
        ref = self.online()
        self.arm, self.loaded_arch = '0', 'amd64'
        with patch.dict(os.environ, {'DOCKER_CONTEXT': 'desktop', 'DOCKER_HOST': 'bad', 'DOCKER_CONFIG': 'bad'}):
            code, out = self.install('--image', ref)
        self.assertEqual(code, 0, out)
        calls = [a for a, _ in self.calls]
        self.assertIn([str(self.target / 'bin/docker'), 'pull', '--platform', 'linux/amd64', ref], calls)
        self.assertIn([str(self.target / 'bin/docker'), 'image', 'inspect', ref], calls)
        self.assertIn([str(self.target / 'bin/compose'), 'up', '-d', '--pull', 'never', '--no-build'], calls)
        self.assertFalse(any('load' in a or 'context' in a for a in calls))
        for a, env in self.calls:
            if 'pull' in a or 'inspect' in a:
                self.assertEqual(a[0], str(self.target / 'bin/docker'))
                self.assertNotIn('DOCKER_CONTEXT', env)
                self.assertNotIn('DOCKER_HOST', env)
                self.assertNotIn('DOCKER_CONFIG', env)
        copied = json.loads((self.target / 'compose.japan.yaml').read_text())
        self.assertEqual(copied['services']['japan']['image'], ref)
        report = json.loads((self.target / 'installation-report.json').read_text())
        self.assertEqual(report['registry']['status'], 'verified')
        self.assertEqual(report['registry']['image'], ref)

    def test_online_rejects_mutable_malformed_or_other_repository_before_writes(self):
        for ref in ['ghcr.io/oudbiao/proxy-residential:latest', 'ghcr.io/oudbiao/proxy-residential@sha256:' + 'A' * 64,
                    'ghcr.io/other/proxy-residential@sha256:' + 'a' * 64, 'ghcr.io/oudbiao/proxy-residential@sha256:abc']:
            code, out = self.install('--image', ref)
            self.assertEqual(code, 2, out)
            self.assertIn('immutable', out)
            self.assertFalse(self.target.exists())

    def test_online_rejects_compose_with_different_image_before_vm(self):
        ref = self.online()
        self.config['services']['japan']['image'] = 'proxy-jp:0.1.0'
        code, out = self.install('--image', ref)
        self.assertEqual(code, 2, out)
        self.assertFalse(self.target.exists())

    def test_online_compose_cannot_request_emulated_architecture(self):
        ref = self.online()
        self.config['services']['japan']['platform'] = 'linux/amd64'
        code, out = self.install('--image', ref)
        self.assertEqual(code, 2, out)
        self.assertFalse(self.target.exists())

    def test_online_wrong_arch_os_or_digest_prevents_container_start(self):
        ref = self.online()
        for arch, os_name, digests in [('amd64', 'linux', [ref]), ('arm64', 'windows', [ref]),
                                     ('arm64', 'linux', ['ghcr.io/oudbiao/proxy-residential@sha256:' + 'b' * 64]), ('arm64', 'linux', [])]:
            with self.subTest(arch=arch, os=os_name, digests=digests):
                self.loaded_arch, self.loaded_os, self.repo_digests = arch, os_name, digests
                code, out = self.install('--image', ref)
                self.assertEqual(code, 2, out)
                self.assertFalse(any('up' in a for a, _ in self.calls))
                report = json.loads((self.target / 'installation-report.json').read_text())
                self.assertEqual(report['failed_stage'], 'online image pull')
                shutil.rmtree(self.target)
                self.calls.clear()

    def test_online_pull_failure_preserves_sanitized_recovery_report(self):
        ref = self.online()
        self.fail_on = 'pull'
        code, out = self.install('--image', ref)
        self.assertEqual(code, 2, out)
        report_text = (self.target / 'installation-report.json').read_text()
        self.assertNotIn('TOPSECRET', out + report_text)
        self.assertNotIn('proxyjp://', report_text)
        self.assertEqual(json.loads(report_text)['failed_stage'], 'online image pull')
        self.assertTrue((self.target / 'compose.japan.yaml').exists())
        self.assertFalse(any('up' in a for a, _ in self.calls))

    def test_rosetta_and_intel_native_architecture(self):
        self.assertEqual(mac.native_arch('1', 'x86_64'), ('arm64', 'aarch64', '/opt/homebrew'))
        self.assertEqual(mac.native_arch('0', 'x86_64'), ('amd64', 'x86_64', '/usr/local'))
        with self.assertRaises(ValueError): mac.native_arch('0', 'sparc')

    def test_dry_run_has_no_files_or_mutating_commands(self):
        code, out = self.install('--dry-run')
        self.assertEqual(code, 0, out)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.daemon.exists())
        self.assertFalse(any(set(a) & {'sudo', 'create', 'start', 'enable', 'load', 'up', '-a', '-c'} for a, _ in self.calls))
        self.assertIn('pending', out)

    def test_filevault_on_and_unknown_block_before_writes(self):
        for status in ['FileVault is On.', 'unknown']:
            self.fv = status
            code, out = self.install()
            self.assertEqual(code, 2, out)
            self.assertIn('BLOCKED', out)
            self.assertFalse(self.target.exists())

    def test_after_unlock_filevault_on_installs_and_records_manual_recovery(self):
        self.fv = 'FileVault is On.'
        ref = self.online()
        code, out = self.install('--image', ref, '--startup-mode', 'after-unlock')
        self.assertEqual(code, 0, out)
        report = json.loads((self.target / 'installation-report.json').read_text())
        self.assertEqual(report['filevault'], 'on')
        self.assertEqual(report['startup_mode'], 'after-unlock')
        self.assertTrue(report['manual_unlock_required'])
        self.assertEqual(report['cold_boot_acceptance'], 'pending onsite power-cycle and manual disk unlock')
        self.assertIn('manual disk unlock', out)
        self.assertIn('running with PID', report['boot_daemon'])
        self.assertEqual([a for a, _ in self.calls if 'fdesetup' in a], [['fdesetup', 'status']])

    def test_after_unlock_filevault_on_dry_run_is_read_only_and_explicit(self):
        self.fv = 'FileVault is On.'
        code, out = self.install('--startup-mode=after-unlock', '--dry-run')
        self.assertEqual(code, 0, out)
        self.assertIn('"manual_unlock_required": true', out)
        self.assertIn('manual disk unlock', out)
        self.assertFalse(self.target.exists())
        self.assertFalse(any(set(a) & {'sudo', 'create', 'start', 'enable', 'load', 'up'} for a, _ in self.calls))

    def test_after_unlock_unknown_filevault_blocks_before_writes(self):
        self.fv = 'unknown'
        code, out = self.install('--startup-mode', 'after-unlock')
        self.assertEqual(code, 2, out)
        self.assertIn('BLOCKED', out)
        self.assertFalse(self.target.exists())

    def test_shell_filevault_gate_requires_explicit_after_unlock_and_known_state(self):
        shell = '''source "$1"
fv_fixture="$2"
fdesetup() { [ "$*" = status ] || return 99; printf '%s\\n' "$fv_fixture"; }
check_filevault "$3"
'''
        for status, mode, expected in [('FileVault is Off.', 'unattended', 0),
                                       ('FileVault is On.', 'unattended', 2),
                                       ('FileVault is On.', 'after-unlock', 0),
                                       ('unknown', 'after-unlock', 2)]:
            with self.subTest(status=status, mode=mode):
                result = subprocess.run(['/bin/bash', '-c', shell, 'test',
                    str(ROOT / 'scripts/install-macos.sh'), status, mode], text=True, capture_output=True)
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_shell_startup_mode_parsing_reaches_gate_before_dependency_install(self):
        shell = '''source "$1"
shift
uname() { echo Darwin; }
id() { echo 501; }
choose_arch() { image_arch=arm64; }
sw_vers() { echo 26.0; }
sysctl() { echo 1; }
fdesetup() { echo unknown; }
check_filevault() { printf 'gate-mode=%s\\n' "$1"; return 2; }
main "$@"
'''
        for flags in (['--startup-mode', 'after-unlock'], ['--startup-mode=after-unlock']):
            result = subprocess.run(['/bin/bash', '-c', shell, 'test',
                str(ROOT / 'scripts/install-macos.sh'), *flags], text=True, capture_output=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('gate-mode=after-unlock', result.stdout)

    def test_boot_install_is_private_portable_and_only_loads_native_image(self):
        code, out = self.install()
        self.assertEqual(code, 0, out)
        calls = [a for a, _ in self.calls]
        self.assertTrue(any(a[-5:] == ['enable', '--condition=boot', '--user', mac.getpass.getuser(), 'proxy-jp'] for a in calls))
        self.assertTrue(any(a[-2:] == ['--input', str((self.root / 'arm.tar.gz').resolve())] for a in calls))
        self.assertFalse(any('amd.tar.gz' in ' '.join(a) or 'context' in a or 'open' in a or 'bootstrap' in a for a in calls))
        report = json.loads((self.target / 'installation-report.json').read_text())
        self.assertEqual(report['health']['status'], 'offline')
        self.assertEqual(report['cold_boot_acceptance'], 'pending onsite power-cycle without login')
        self.assertEqual((self.target / 'compose.japan.yaml').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('proxyjp://', json.dumps(report))
        self.assertIn('mounts: []', (self.target / 'lima.yaml').read_text())
        self.assertIn('systemctl enable --now docker', (self.target / 'lima.yaml').read_text())
        self.assertEqual(self.install()[0], 2)

    def test_generated_wrapper_executes_argv_with_isolated_context(self):
        fake = self.root / 'fake docker'
        log = self.root / 'wrapper.json'
        fake.write_text('#!' + sys.executable + '\nimport os,json,sys\nopen(' + repr(str(log)) + ',"w").write(json.dumps([sys.argv[1:],dict(os.environ)]))\n')
        fake.chmod(0o700)
        with patch.object(mac, 'command', self.command), patch.object(mac.os, 'getuid', return_value=501), patch.object(mac.sys, 'platform', 'darwin'):
            code = mac.main(['--compose', str(self.compose), '--manifest', str(self.manifest), '--install-dir', str(self.target), '--docker-cli', str(fake), '--compose-cli', '/fake/docker-compose', '--limactl', '/fake/limactl', '--daemon-path', str(self.daemon)])
        self.assertEqual(code, 0)
        ambient = {**os.environ, 'DOCKER_CONTEXT': 'desktop-linux', 'DOCKER_HOST': 'bad', 'DOCKER_CONFIG': 'bad',
                   'VPS_LIST': '[]', 'PROXY_JP_IMAGE': 'local-override:example', 'COMPOSE_PROFILES': 'ambient'}
        subprocess.run([str(self.target / 'bin/docker'), 'compose', 'ps', 'space argument'], env=ambient, check=True)
        argv, env = json.loads(log.read_text())
        self.assertEqual(argv[-3:], ['compose', 'ps', 'space argument'])
        self.assertIn('unix://' + str(self.target / 'lima/proxy-jp/sock/docker.sock'), argv)
        self.assertNotIn('DOCKER_CONTEXT', env)
        for key in ('VPS_LIST', 'PROXY_JP_IMAGE', 'COMPOSE_PROFILES'):
            self.assertEqual(env[key], ambient[key])
        self.assertEqual(json.loads((self.target / 'docker-config/config.json').read_text())['cliPluginsExtraDirs'], ['/fake'])
        subprocess.run([str(self.target / 'bin/compose'), 'config', 'space argument'], env=ambient, check=True)
        argv, env = json.loads(log.read_text())
        self.assertEqual(argv[-2:], ['config', 'space argument'])
        self.assertEqual(argv[argv.index('--env-file') + 1], os.devnull)
        self.assertFalse(any(key.startswith('COMPOSE_') or key in ('VPS_LIST', 'PROXY_JP_IMAGE') for key in env))

    def test_generated_compose_wrapper_renders_installed_env_despite_ambient_overrides(self):
        # Only host/engine operations are simulated: the generated wrapper runs
        # actual Docker Compose config, which never needs a running engine.
        docker = shutil.which('docker')
        self.assertIsNotNone(docker, 'Docker CLI with Compose is required for configuration checks')
        compose_cli = shutil.which('docker-compose') or docker
        ref = self.online()
        self.compose.write_text(json.dumps({'services': {'japan': {
            'image': '${PROXY_JP_IMAGE:?}', 'restart': 'unless-stopped',
            'environment': {'VPS_LIST': '${VPS_LIST:?}'}}}}))
        installed_profiles = self.config['services']['japan']['environment']['VPS_LIST']
        env_file = self.root / 'private inputs.env'
        env_file.write_text("VPS_LIST='" + installed_profiles + "'\nPROXY_JP_IMAGE=" + ref + '\n')
        with patch.object(mac, 'command', self.command), patch.object(mac.os, 'getuid', return_value=501), patch.object(mac.sys, 'platform', 'darwin'):
            code = mac.main(['--compose', str(self.compose), '--env-file', str(env_file), '--image', ref,
                             '--install-dir', str(self.target), '--docker-cli', docker, '--compose-cli', compose_cli,
                             '--limactl', '/fake/limactl', '--daemon-path', str(self.daemon)])
        self.assertEqual(code, 0)
        caller = self.root / 'caller'
        caller.mkdir()
        (caller / '.env').write_text('VPS_LIST=[]\nPROXY_JP_IMAGE=caller-dotenv:example\n')
        caller_config = caller / 'docker-config'
        caller_config.mkdir()
        context = '{"currentContext":"caller-context"}'
        (caller_config / 'config.json').write_text(context)
        baseline = {k: v for k, v in os.environ.items() if not k.startswith('COMPOSE_')
                    and k not in ('VPS_LIST', 'PROXY_JP_IMAGE')}
        overrides = {'VPS_LIST': '[]', 'PROXY_JP_IMAGE': 'local-override:example',
                     'COMPOSE_PROJECT_NAME': 'ambient-project', 'COMPOSE_FILE': '/missing/ambient.yaml',
                     'COMPOSE_ENV_FILES': '/missing/ambient.env', 'COMPOSE_PROFILES': 'ambient',
                     'COMPOSE_REMOVE_ORPHANS': '1', 'DOCKER_CONFIG': str(caller_config),
                     'DOCKER_CONTEXT': 'caller-context', 'DOCKER_HOST': 'unix:///missing/docker.sock'}
        for ambient in ({}, overrides):
            with self.subTest(ambient=bool(ambient)), patch.dict(os.environ, {**baseline, **ambient}, clear=True):
                before = dict(os.environ)
                cwd = Path.cwd()
                result = subprocess.run([str(self.target / 'bin/compose'), 'config', '--format', 'json'],
                                        cwd=caller, capture_output=True, text=True, check=True)
                rendered = json.loads(result.stdout)
                self.assertEqual(len(json.loads(rendered['services']['japan']['environment']['VPS_LIST'])), 1)
                self.assertEqual(json.loads(rendered['services']['japan']['environment']['VPS_LIST']),
                                 json.loads(installed_profiles))
                self.assertEqual(rendered['services']['japan']['image'], ref)
                self.assertEqual(rendered['name'], 'proxy-jp')
                self.assertEqual(dict(os.environ), before)
                self.assertEqual(Path.cwd(), cwd)
                self.assertEqual((caller_config / 'config.json').read_text(), context)

    def test_invalid_compose_and_hash_refuse_before_install(self):
        self.config['services']['japan']['environment']['VPS_LIST'] = 'TOPSECRET'
        code, out = self.install()
        self.assertEqual(code, 2)
        self.assertNotIn('TOPSECRET', out)
        self.assertFalse(self.target.exists())
        self.setUp()
        (self.root / 'arm.tar.gz').write_bytes(b'bad')
        self.assertEqual(self.install()[0], 2)
        self.assertFalse(self.target.exists())

    def test_stopped_container_and_partial_failure_keep_recoverable_data(self):
        self.running = False
        code, out = self.install()
        self.assertEqual(code, 2)
        self.assertNotIn('recovered', out)
        self.assertTrue((self.target / 'compose.japan.yaml').exists())
        self.assertEqual(json.loads((self.target / 'installation-report.json').read_text())['status'], 'incomplete')

    def test_unsupported_power_and_uninstall_preserve_secrets_and_vm(self):
        self.cap = ' other\n'
        self.assertEqual(self.install()[0], 0)
        report = json.loads((self.target / 'installation-report.json').read_text())
        self.assertEqual(report['power']['recovery'], 'unsupported; automatic power recovery unverified')
        self.assertFalse(any(a[:2] == ['sudo', 'pmset'] for a, _ in self.calls))
        self.calls.clear()
        self.assertEqual(self.install('--uninstall')[0], 0)
        self.assertTrue((self.target / 'compose.japan.yaml').exists())
        self.assertTrue((self.target / 'lima').exists())
        self.assertTrue(any('disable' in a and 'autostart' in a for a, _ in self.calls))

    def test_vz_macos_and_lima_version_gates_before_mutation(self):
        for arm, version in [('1', '13.4'), ('0', '15.4')]:
            self.arm, self.macos = arm, version
            code, _ = self.install()
            self.assertEqual(code, 2)
            self.assertFalse(self.target.exists())
        self.arm, self.macos, self.lima_version = '1', '26.0', '2.1.9'
        self.assertEqual(self.install()[0], 2)

    def test_failed_command_output_is_private_and_recovery_report_survives(self):
        self.fail_on = 'load'
        code, out = self.install()
        self.assertEqual(code, 2)
        self.assertNotIn('TOPSECRET', out)
        self.assertTrue((self.target / 'compose.japan.yaml').exists())
        self.assertEqual(json.loads((self.target / 'installation-report.json').read_text())['failed_stage'], 'offline image load')

    def test_nonzero_health_output_keeps_exact_per_tag_snapshot(self):
        with patch.object(mac.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, stdout='{"links":[]}', stderr='secret')):
            self.assertEqual(mac.command(['health'], check=False), '{"links":[]}')

    def test_existing_daemon_refuses_before_mutation(self):
        self.daemon.write_bytes(b'other daemon')
        self.assertEqual(self.install()[0], 2)
        self.assertEqual(self.daemon.read_bytes(), b'other daemon')
        self.assertFalse(self.target.exists())


    def test_uninstall_refuses_another_installations_boot_daemon(self):
        self.assertEqual(self.install()[0], 0)
        daemon = plistlib.loads(self.daemon.read_bytes())
        daemon['EnvironmentVariables']['LIMA_HOME'] = '/another/installation/lima'
        self.daemon.write_bytes(plistlib.dumps(daemon))
        self.calls.clear()
        code, _ = self.install('--uninstall')
        self.assertEqual(code, 2)
        self.assertFalse(any('disable' in a for a, _ in self.calls))

    def test_partial_health_is_derived_from_exact_validated_tags(self):
        items = [{'tag': 'JP', 'connection': encode_profile(profile())}, {'tag': 'HK', 'connection': encode_profile(profile(1))}]
        self.config['services']['japan']['environment']['VPS_LIST'] = json.dumps(items)
        manifest = json.loads(self.manifest.read_text()); manifest['tags'] = ['JP', 'HK']
        self.manifest.write_text(json.dumps(manifest))
        self.health = {'status': 'online', 'links': [{'tag': 'JP', 'online': True}, {'tag': 'HK', 'online': False}]}
        self.assertEqual(self.install()[0], 0)
        report = json.loads((self.target / 'installation-report.json').read_text())
        self.assertEqual(report['health'], {'status': 'partial', 'links': [{'tag': 'JP', 'online': True}, {'tag': 'HK', 'online': False}]})


    def test_system_daemon_takes_ownership_after_stopping_detached_vm(self):
        self.assertEqual(self.install()[0], 0)
        calls = [a for a, _ in self.calls]
        stops = [i for i, a in enumerate(calls) if 'stop' in a and 'proxy-jp' in a]
        enables = [i for i, a in enumerate(calls) if 'enable' in a and '--condition=boot' in a]
        self.assertTrue(stops, 'boot daemon must take over a stopped VM')
        self.assertLess(stops[-1], enables[-1])
        self.assertTrue(any(i > enables[-1] and 'inspect' in a for i, a in enumerate(calls)), 'verify container after system daemon starts VM')

    def test_idle_registered_daemon_is_not_reported_as_managing_vm(self):
        self.launch_state = 'state = not running\n'
        self.assertEqual(self.install()[0], 2)


    def test_loaded_image_identity_mismatch_prevents_container_start(self):
        self.loaded_id = 'sha256:wrong'
        self.assertEqual(self.install()[0], 2)
        self.assertFalse(any('up' in a for a, _ in self.calls))
        self.assertTrue((self.target / 'compose.japan.yaml').exists())

    def test_supported_power_values_are_saved_applied_and_verified(self):
        self.power = ' sleep 5\n autorestart 0\n'
        original_command = self.command
        def with_readback(argv, env=None, check=True):
            result = original_command(argv, env, check)
            if list(argv) == ['sudo', 'pmset', '-c', 'autorestart', '1']:
                self.power = ' sleep 5\n autorestart 1\n'
            if list(argv) == ['sudo', 'pmset', '-c', 'sleep', '0']:
                self.power = ' sleep 0\n autorestart 1\n'
            return result
        with patch.object(self, 'command', with_readback):
            self.assertEqual(self.install()[0], 0)
        report = json.loads((self.target / 'installation-report.json').read_text())
        self.assertEqual(report['power']['original_ac'], {'sleep': 5, 'autorestart': 0})
        self.assertEqual(report['power']['applied_ac'], {'autorestart': 1, 'sleep': 0})
        self.assertIn('not guaranteed', report['power']['recovery'])


    def test_partial_power_write_failure_persists_confirmed_change_before_next_write(self):
        self.power = ' sleep 5\n autorestart 0\n'
        original_command = self.command
        def partial_write(argv, env=None, check=True):
            if list(argv) == ['sudo', 'pmset', '-c', 'sleep', '0']:
                persisted = json.loads((self.target / 'installation-report.json').read_text())
                self.assertEqual(persisted['power']['applied_ac'], {'autorestart': 1, 'sleep': 5})
                raise subprocess.CalledProcessError(1, argv, stderr='TOPSECRET')
            result = original_command(argv, env, check)
            if list(argv) == ['sudo', 'pmset', '-c', 'autorestart', '1']:
                self.power = ' sleep 5\n autorestart 1\n'
            return result
        with patch.object(self, 'command', partial_write):
            code, out = self.install()
        self.assertEqual(code, 2)
        self.assertNotIn('TOPSECRET', out)
        report = json.loads((self.target / 'installation-report.json').read_text())
        self.assertEqual(report['power']['original_ac'], {'sleep': 5, 'autorestart': 0})
        self.assertEqual(report['power']['applied_ac'], {'autorestart': 1, 'sleep': 5})
        self.assertEqual(report['power']['applied_ac_observation'], 'confirmed')
        self.assertEqual(report['failed_stage'], 'power configuration')

    def test_power_readback_mismatch_records_observed_values(self):
        self.power = ' sleep 5\n autorestart 0\n'
        # Commands succeed without changing the simulated policy.
        code, _ = self.install()
        self.assertEqual(code, 2)
        report = json.loads((self.target / 'installation-report.json').read_text())
        self.assertEqual(report['power']['applied_ac'], {'autorestart': 0, 'sleep': 5})
        self.assertEqual(report['power']['applied_ac_observation'], 'confirmed')
        self.assertEqual(report['power']['original_ac'], {'sleep': 5, 'autorestart': 0})

    def test_unreadable_failure_snapshot_marks_unknown_and_keeps_prior_observation(self):
        self.power = ' sleep 5\n autorestart 0\n'
        original_command = self.command
        failed = False
        def unreadable_after_failure(argv, env=None, check=True):
            nonlocal failed
            if list(argv) == ['sudo', 'pmset', '-c', 'sleep', '0']:
                failed = True
                raise subprocess.CalledProcessError(1, argv, stderr='TOPSECRET')
            if failed and list(argv) == ['pmset', '-g', 'custom']:
                raise subprocess.CalledProcessError(1, argv, stderr='TOPSECRET')
            result = original_command(argv, env, check)
            if list(argv) == ['sudo', 'pmset', '-c', 'autorestart', '1']:
                self.power = ' sleep 5\n autorestart 1\n'
            return result
        with patch.object(self, 'command', unreadable_after_failure):
            code, out = self.install()
        self.assertEqual(code, 2)
        self.assertNotIn('TOPSECRET', out)
        power = json.loads((self.target / 'installation-report.json').read_text())['power']
        self.assertEqual(power['applied_ac'], {'autorestart': None, 'sleep': None})
        self.assertEqual(power['applied_ac_observation'], 'unknown')
        self.assertTrue(any(snapshot['values'] == {'autorestart': 1, 'sleep': 5} and snapshot['status'] == 'confirmed'
                            for snapshot in power['observations_ac']))


class BootstrapTests(unittest.TestCase):
    def test_shell_bootstrap_selects_native_formulae_and_never_desktop(self):
        script = ROOT / 'scripts/install-macos.sh'
        self.assertTrue(script.exists(), 'fresh-host shell bootstrap is missing')
        # Source definitions only; replace external execution at the shell boundary.
        shell = '''source "$1"
sysctl() { echo 1; }
uname() { echo x86_64; }
choose_arch
printf '%s|%s|%s\\n' "$image_arch" "$native_arch" "$brew_prefix"
brew_run() { printf 'BREW'; printf ' <%s>' "$@"; printf '\\n'; if [ "$1" = list ]; then return 1; fi; }
bootstrap_formulae
'''
        result = subprocess.run(['bash', '-c', shell, 'test', str(script)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('arm64|arm64|/opt/homebrew', result.stdout)
        for formula in ['python', 'lima', 'docker', 'docker-compose']:
            self.assertIn('BREW <install> <' + formula + '>', result.stdout)
        self.assertNotIn('cask', result.stdout)

    def test_shell_dry_run_missing_dependencies_does_not_install(self):
        script = ROOT / 'scripts/install-macos.sh'
        self.assertTrue(script.exists(), 'read-only dependency preflight is missing')
        shell = '''source "$1"
brew_run() { echo MUTATION; return 1; }
dry_run=1
bootstrap_formulae
'''
        result = subprocess.run(['bash', '-c', shell, 'test', str(script)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('<install>', result.stdout)
        self.assertIn('missing', result.stderr)
