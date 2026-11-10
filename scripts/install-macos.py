#!/usr/bin/env python3
"""Dedicated Lima Docker host with system boot autostart after disk unlock.

Use install-macos.sh for dependency bootstrap. Dry-run only reads and validates;
unattended mode requires FileVault off; after-unlock permits manual disk unlock.
Cold-boot acceptance always requires an authorized onsite power-cycle.
"""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_jp.config import read_profiles

class InstallError(ValueError):
    """A fixed diagnostic that never includes external command output or credentials."""


INSTANCE = 'proxy-jp'
LABEL = 'io.lima-vm.daemon.proxy-jp'


def command(argv, env=None, check=True):
    """Capture all child output privately; never forward Compose/daemon secrets."""
    result = subprocess.run(list(map(str, argv)), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, timeout=1800)
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, argv)
    return result.stdout


def native_arch(arm_capability, machine):
    if arm_capability.strip() == '1':
        return 'arm64', 'aarch64', '/opt/homebrew'
    if machine.strip() == 'x86_64':
        return 'amd64', 'x86_64', '/usr/local'
    raise InstallError('Unsupported physical architecture')


def private_write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('w') as stream:
        os.chmod(path, mode)
        stream.write(content)


def clean_env():
    return {k: v for k, v in os.environ.items() if not k.startswith(('DOCKER_', 'COMPOSE_', 'LIMA_'))
            and k not in ('VPS_LIST', 'PROXY_JP_IMAGE')}


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def power_values(text):
    # Read AC section only; do not report battery values as AC configuration.
    if 'AC Power:' in text:
        text = text.split('AC Power:', 1)[1].split('Battery Power:', 1)[0]
    return {key: int(value) for key, value in re.findall(r'^\s*(sleep|autorestartatconnect|autorestart)\s+(\d+)\b', text, re.M)}


def observe_power(report, env, target, boundary):
    """Persist a sanitized AC snapshot even when a write or its readback fails.

    None means currently unknown, never an assumed original or requested value.
    History preserves earlier confirmed observations if the final read fails.
    """
    keys = report['power']['requested_ac']
    try:
        observed = power_values(command(['pmset', '-g', 'custom'], env=env))
    except (OSError, ValueError, subprocess.SubprocessError):
        observed = {}
    values = {key: observed.get(key) for key in keys}
    known = sum(value is not None for value in values.values())
    status = 'confirmed' if known == len(values) else 'partial' if known else 'unknown'
    report['power']['applied_ac'] = values
    report['power']['applied_ac_observation'] = status
    report['power'].setdefault('observations_ac', []).append(
        {'after': boundary, 'status': status, 'values': values})
    private_write(target / 'installation-report.json', json.dumps(report, ensure_ascii=False, indent=2))
    return values


def lima_yaml(arch):
    return ('base: template:docker-rootful\nvmType: vz\narch: ' + arch + '\n'
            'cpus: 2\nmemory: 2GiB\ndisk: 20GiB\nmounts: []\n'
            'provision:\n  - mode: system\n    script: |\n      #!/bin/sh\n      set -eu\n      systemctl enable --now docker\n')


def compose_config(args, path, env):
    argv = [args.compose_cli, '--project-name', INSTANCE, '-f', str(path), '--env-file',
            str(args.env_file.resolve()) if args.env_file else os.devnull, 'config', '--format', 'json']
    result = json.loads(command(argv, env=env))
    services = result['services']
    if set(services) != {'japan'}:
        raise InstallError('Require exactly the Japan service')
    service = services['japan']
    if service.get('volumes') or service.get('ports') or service.get('build') or service.get('restart') != 'unless-stopped':
        raise InstallError('Unsupported Compose service or restart policy')
    profiles = read_profiles(service.get('environment', {}))
    return service, [p.name for p in profiles]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compose', type=Path, default=Path(__file__).resolve().parents[1] / 'compose.japan.yaml')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--manifest', type=Path, help='Offline mode; default: manifest.json beside source Compose')
    source.add_argument('--image', help='Online mode: immutable ghcr.io/oudbiao/proxy-residential@sha256 digest')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--install-dir', type=Path, default=Path.home() / '.proxy-jp')
    parser.add_argument('--docker-cli', default=shutil.which('docker') or 'docker')
    parser.add_argument('--compose-cli', default=shutil.which('docker-compose') or 'docker-compose')
    parser.add_argument('--limactl', default=shutil.which('limactl') or 'limactl')
    parser.add_argument('--daemon-path', type=Path, default=Path('/Library/LaunchDaemons') / (LABEL + '.plist'), help=argparse.SUPPRESS)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--startup-mode', choices=['unattended', 'after-unlock'], default='unattended',
                        help='after-unlock keeps FileVault on and requires manual disk unlock after cold boot')
    parser.add_argument('--uninstall', action='store_true')
    args = parser.parse_args(argv)
    target = args.install_dir.expanduser().absolute()
    home = target / 'lima'
    socket = home / INSTANCE / 'sock/docker.sock'
    env = clean_env()
    env['LIMA_HOME'] = str(home)
    report = {'status': 'preflight', 'vm': INSTANCE, 'lima_home': str(home), 'socket': str(socket),
              'daemon': str(args.daemon_path), 'owner': getpass.getuser(), 'startup_mode': args.startup_mode,
              'cold_boot_acceptance': 'pending onsite power-cycle without login'}
    created = False
    stage = 'preflight'
    try:
        if sys.platform != 'darwin' or os.getuid() == 0:
            raise InstallError('Run as a non-root macOS user')
        if args.uninstall:
            if not (target / 'installation-report.json').is_file():
                raise InstallError('No managed installation report; refusing to disable another instance')
            if args.daemon_path.exists():
                installed_daemon = plistlib.loads(args.daemon_path.read_bytes())
                if installed_daemon.get('Label') != LABEL or installed_daemon.get('UserName') != getpass.getuser() or installed_daemon.get('EnvironmentVariables', {}).get('LIMA_HOME') != str(home):
                    raise InstallError('System daemon belongs to another installation; refusing to disable it')
            if not args.dry_run:
                command([args.limactl, 'autostart', 'disable', INSTANCE], env=env)
            print('proxy-jp: ' + ('uninstall validated; no changes' if args.dry_run else
                  'boot autostart disabled; configuration, credentials, VM data and power settings preserved'))
            return 0
        if len(str(socket).encode()) > 100:
            raise InstallError('Install directory too long for Unix socket; choose a shorter --install-dir')
        if target.exists() or args.daemon_path.exists():
            raise InstallError('Existing installation or system daemon found; refusing to overwrite')
        arch, vm_arch, brew = native_arch(command(['sysctl', '-n', 'hw.optional.arm64'], check=False), command(['uname', '-m']))
        macos = command(['sw_vers', '-productVersion']).strip()
        macos_version = tuple(map(int, macos.split('.')[:2]))
        if macos_version < ((13, 5) if arch == 'arm64' else (15, 5)) or command(['sysctl', '-n', 'kern.hv_support'], check=False).strip() != '1':
            raise InstallError('VZ requires ARM macOS13.5+ or Intel macOS15.5+ and hardware virtualization')
        version = command([args.limactl, '--version'], env=env).strip()
        match = re.search(r'(\d+)\.(\d+)\.(\d+)', version)
        if not match or tuple(map(int, match.groups())) < (2, 2, 0):
            raise InstallError('Lima 2.2 or later required')
        report.update(architecture=arch, lima_version=version, macos=command(['sw_vers', '-productVersion']).strip())
        fv = command(['fdesetup', 'status'], check=False).strip()
        report['filevault'] = {'FileVault is Off.': 'off', 'FileVault is On.': 'on'}.get(fv, 'unknown')
        if report['filevault'] == 'unknown':
            raise InstallError('BLOCKED: FileVault status is unknown; inspect locally before installation. Installer never changes FileVault.')
        if report['filevault'] == 'on' and args.startup_mode != 'after-unlock':
            raise InstallError('BLOCKED: FileVault is on. Unattended cold boot requires FileVault clearly off; use --startup-mode after-unlock to retain encryption and require manual unlock. Installer never disables FileVault.')
        report['manual_unlock_required'] = report['filevault'] == 'on'
        report['unattended_cold_boot'] = ('blocked until manual disk unlock' if report['manual_unlock_required']
                                          else 'pending onsite verification')
        if report['manual_unlock_required']:
            report['cold_boot_acceptance'] = 'pending onsite power-cycle and manual disk unlock'
        if args.image and not re.fullmatch(r'ghcr\.io/oudbiao/proxy-residential@sha256:[0-9a-f]{64}', args.image):
            raise InstallError('Require immutable ghcr.io/oudbiao/proxy-residential@sha256 digest reference')
        compose = args.compose.expanduser().resolve(strict=True)
        if args.env_file:
            args.env_file = args.env_file.expanduser().resolve(strict=True)
        report['source_sha256'] = {'compose': digest(compose)}
        if args.env_file:
            report['source_sha256']['env'] = digest(args.env_file)
        service, tags = compose_config(args, compose, env)
        if service.get('platform', 'linux/' + arch) != 'linux/' + arch:
            raise InstallError('Compose platform must match native Linux architecture')
        if args.image:
            if service['image'] != args.image:
                raise InstallError('Compose image differs from immutable image reference')
            report['registry'] = {'image': args.image, 'status': 'deferred',
                                  'checks': ['pull', 'Linux architecture', 'RepoDigests exact immutable reference']}
        else:
            manifest_path = (args.manifest or compose.parent / 'manifest.json').resolve(strict=True)
            manifest = json.loads(manifest_path.read_text())
            if manifest.get('format_version') != 1:
                raise InstallError('Unsupported offline image manifest version')
            image = manifest['images'][arch]
            archive = (manifest_path.parent / image['file']).resolve(strict=True)
            if not archive.is_relative_to(manifest_path.parent) or image['architecture'] != arch or image['os'] != 'linux' or digest(archive) != image['sha256'] or not image['accepted_image_ids']:
                raise InstallError('Offline image manifest/hash/architecture mismatch')
            report['source_sha256'].update(manifest=digest(manifest_path), image=image['sha256'])
            if service['image'] != image['tag'] or tags != manifest['tags']:
                raise InstallError('Compose image/tags differ from verified manifest')
        capabilities = set(command(['pmset', '-g', 'cap']).split())
        original = power_values(command(['pmset', '-g', 'custom']))
        desired = {}
        if 'autorestartatconnect' in capabilities:
            desired['autorestartatconnect'] = 1
            recovery = 'always start on AC connection supported; onsite test pending'
        elif 'autorestart' in capabilities:
            desired['autorestart'] = 1
            recovery = 'restart after power failure only; AC connection while shut down not guaranteed'
        else:
            recovery = 'unsupported; automatic power recovery unverified'
        if 'sleep' in capabilities:
            desired['sleep'] = 0
        if any(key not in original for key in desired):
            raise InstallError('Cannot save original supported power values')
        report['power'] = {'original_ac': original, 'requested_ac': desired, 'applied_ac': {}, 'recovery': recovery}
        report['docker_version'] = command([args.docker_cli, '--version'], env=env).strip()
        report['compose_version'] = command([args.compose_cli, 'version', '--short'], env=env).strip()
        if args.dry_run:
            print('proxy-jp: dry-run preflight passed; no files, VM, daemon or power settings changed; boot acceptance pending')
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        stage = 'private installation'
        target.mkdir(mode=0o700)
        created = True
        home.mkdir(mode=0o700)
        private_write(target / 'installation-report.json', json.dumps(report, indent=2))
        private_write(target / 'compose.japan.yaml', compose.read_text())
        if args.env_file:
            private_write(target / 'japan.env', args.env_file.read_text())
            args.env_file = target / 'japan.env'
        # Validate the actual copied configuration before creating VM or changing power.
        copied, copied_tags = compose_config(args, target / 'compose.japan.yaml', env)
        if digest(target / 'compose.japan.yaml') != report['source_sha256']['compose'] or (args.env_file and digest(args.env_file) != report['source_sha256']['env']) or copied != service or copied_tags != tags:
            raise InstallError('Copied Compose changed during installation')
        private_write(target / 'lima.yaml', lima_yaml(vm_arch))
        plugin_dir = str(Path(args.compose_cli).absolute().parent)
        # Homebrew docker-compose bin is also a valid plugin executable named docker-compose.
        private_write(target / 'docker-config/config.json', json.dumps({'cliPluginsExtraDirs': [plugin_dir]}))
        wrapper = ('#!/bin/bash\nset -eu\nunset DOCKER_CONTEXT DOCKER_HOST DOCKER_CONFIG DOCKER_TLS DOCKER_TLS_VERIFY DOCKER_CERT_PATH\n'
                   'exec ' + shlex.quote(args.docker_cli) + ' --config ' + shlex.quote(str(target / 'docker-config')) +
                   ' --host ' + shlex.quote('unix://' + str(socket)) + ' "$@"\n')
        private_write(target / 'bin/docker', wrapper, 0o700)
        # Portable management commands: no checkout dependency after install.
        # Match preflight's project isolation while leaving bin/docker general-purpose.
        # Bash 3.2 prefix expansion enumerates names only, never credential values.
        private_write(target / 'bin/compose', '#!/bin/bash\nset -eu\nunset VPS_LIST PROXY_JP_IMAGE\n'
                      'for compose_var in "${!COMPOSE_@}"; do\n  unset "$compose_var"\ndone\nexec ' + shlex.quote(str(target / 'bin/docker')) +
                      ' compose --project-name proxy-jp -f ' + shlex.quote(str(target / 'compose.japan.yaml')) +
                      ' --env-file ' + shlex.quote(str(args.env_file) if args.env_file else os.devnull) + ' "$@"\n', 0o700)
        private_write(target / 'bin/uninstall', '#!/bin/bash\nset -eu\nexport LIMA_HOME=' + shlex.quote(str(home)) +
                      '\ndaemon=' + shlex.quote(str(args.daemon_path)) + '\nif [ -e "$daemon" ]; then\n' +
                      '  actual_home="$(/usr/libexec/PlistBuddy -c \'Print :EnvironmentVariables:LIMA_HOME\' "$daemon")"\n' +
                      '  actual_owner="$(/usr/libexec/PlistBuddy -c \'Print :UserName\' "$daemon")"\n' +
                      '  if [ "$actual_home" != "$LIMA_HOME" ] || [ "$actual_owner" != ' + shlex.quote(getpass.getuser()) + ' ]; then\n' +
                      '    echo "Refusing to disable another installations boot daemon" >&2; exit 2\n  fi\nfi\n' +
                      shlex.quote(args.limactl) + ' autostart disable proxy-jp\necho "Boot autostart disabled; credentials, VM data and power settings preserved"\n', 0o700)
        stage = 'VM creation'
        command([args.limactl, 'validate', str(target / 'lima.yaml')], env=env)
        command([args.limactl, 'create', '--name', INSTANCE, '--tty=false', '--mount-none', str(target / 'lima.yaml')], env=env)
        command([args.limactl, 'start', '--tty=false', INSTANCE], env=env)
        command([args.limactl, 'shell', INSTANCE, 'sudo', 'systemctl', 'enable', '--now', 'docker'], env=env)
        stage = 'online image pull' if args.image else 'offline image load'
        docker = [str(target / 'bin/docker')]
        command(docker + ['info'], env=env)
        if args.image:
            command(docker + ['pull', '--platform', 'linux/' + arch, args.image], env=env)
            loaded = json.loads(command(docker + ['image', 'inspect', args.image], env=env))[0]
            if loaded['Architecture'] != arch or loaded['Os'] != 'linux' or args.image not in (loaded.get('RepoDigests') or []):
                raise InstallError('Pulled image architecture or immutable digest mismatch')
            report['registry']['status'] = 'verified'
        else:
            command(docker + ['load', '--input', str(archive)], env=env)
            loaded = json.loads(command(docker + ['image', 'inspect', image['tag']], env=env))[0]
            if loaded['Id'] not in image['accepted_image_ids'] or loaded['Architecture'] != arch or loaded['Os'] != 'linux':
                raise InstallError('Loaded image identity mismatch')
        stage = 'container startup'
        compose_cmd = [str(target / 'bin/compose')]
        command(compose_cmd + ['up', '-d', '--pull', 'never', '--no-build'], env=env)
        cid = command(compose_cmd + ['ps', '-q', 'japan'], env=env).strip()
        if not cid:
            raise InstallError('Japan container missing')
        state = json.loads(command(docker + ['inspect', cid], env=env))[0]
        if not state['State']['Running'] or state['HostConfig']['RestartPolicy']['Name'] != 'unless-stopped':
            raise InstallError('Japan container stopped or restart policy incorrect')
        stage = 'system boot registration'
        # A detached hostagent from initial provisioning must not retain ownership.
        # The official boot daemon's foreground process takes ownership of a stopped VM.
        command([args.limactl, 'stop', '--tty=false', INSTANCE], env=env)
        command([args.limactl, 'autostart', 'enable', '--condition=boot', '--user', getpass.getuser(), INSTANCE], env=env)
        daemon = plistlib.loads(args.daemon_path.read_bytes())
        pargs = daemon.get('ProgramArguments', [])
        if daemon.get('Label') != LABEL or daemon.get('UserName') != getpass.getuser() or daemon.get('EnvironmentVariables', {}).get('LIMA_HOME') != str(home) or pargs[1:] != ['start', INSTANCE, '--foreground']:
            raise InstallError('Generated system boot daemon failed ownership/environment/foreground validation')
        for attempt in range(90):
            try:
                command(docker + ['info'], env=env)
                break
            except subprocess.CalledProcessError:
                if attempt == 89:
                    raise InstallError('System daemon Docker socket did not become ready')
                time.sleep(2)
        launch_state = command(['launchctl', 'print', 'system/' + LABEL], env=env)
        if not re.search(r'\bstate = running\b', launch_state) or not re.search(r'\bpid = [1-9][0-9]*\b', launch_state):
            raise InstallError('Registered system daemon is not running with a PID')
        state = json.loads(command(docker + ['inspect', cid], env=env))[0]
        if not state['State']['Running'] or state['HostConfig']['RestartPolicy']['Name'] != 'unless-stopped':
            raise InstallError('Japan container did not restart under the system daemon')
        report['boot_daemon'] = 'running with PID after takeover from stopped VM'
        # Health command may exit 1 for all-offline. Keep the install and report exact tag booleans.
        health = command(docker + ['exec', cid, 'python3', '-m', 'proxy_jp', 'health'], env=env, check=False)
        try:
            observed = json.loads(health)
            links = {item['tag']: item['online'] is True for item in observed['links']}
            report['health'] = {'status': 'online' if all(links.get(t, False) for t in tags) else 'partial' if any(links.get(t, False) for t in tags) else 'offline',
                                'links': [{'tag': tag, 'online': links.get(tag, False)} for tag in tags]}
        except (ValueError, KeyError, TypeError):
            report['health'] = {'status': 'unavailable', 'links': [{'tag': t, 'online': False} for t in tags]}
        stage = 'power configuration'
        for key, value in desired.items():
            if original[key] != value:
                try:
                    command(['sudo', 'pmset', '-c', key, str(value)], env=env)
                finally:
                    # A failed command can still have changed host policy.
                    observe_power(report, env, target, key + ' mutation')
        applied = observe_power(report, env, target, 'final verification')
        if any(applied.get(key) != value for key, value in desired.items()):
            raise InstallError('Power setting readback mismatch; review saved original values')
        report['status'] = 'installed; boot acceptance pending'
        private_write(target / 'installation-report.json', json.dumps(report, ensure_ascii=False, indent=2))
        requirement = '; manual disk unlock required after each cold boot' if report['manual_unlock_required'] else ''
        print('proxy-jp: installed with system boot autostart; health=' + report['health']['status'] + requirement + '; onsite cold-boot acceptance pending')
        return 0
    except (OSError, ValueError, KeyError, TypeError, IndexError, subprocess.SubprocessError) as error:
        # Only our known FileVault guidance is safe to expose. Never print exception/command output.
        message = str(error) if isinstance(error, InstallError) else 'installation incomplete at ' + stage + '; check dependencies, sources, manifest, conflicts and private report'
        if created:
            if stage == 'power configuration':
                try:
                    observe_power(report, env, target, 'failure recovery')
                except OSError:
                    pass
            report.update(status='incomplete', failed_stage=stage, reason=message)
            try:
                private_write(target / 'installation-report.json', json.dumps(report, ensure_ascii=False, indent=2))
            except OSError:
                pass
        print('proxy-jp: ' + message + '; recoverable data preserved', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
