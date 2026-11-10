#!/usr/bin/env python3
"""Build a private portable Japan delivery; never print connection credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from proxy_jp.config import read_profiles
from proxy_jp.provision import json_text, surge_configuration, write_private

# Explicit source allowlist: no recursive checkout copy and no deployment state.
SOURCES = (
    'README.md', 'docs/operations.md', 'HANDOFF-PROMPT.md',
    'skills/proxy-jp-japan-deploy/SKILL.md',
    'scripts/install-macos.py', 'scripts/install-macos.sh',
    'proxy_jp/__init__.py', 'proxy_jp/__main__.py', 'proxy_jp/config.py',
    'proxy_jp/provision.py', 'proxy_jp/runtime.py',
)


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def image_metadata(path, architecture, tag):
    """Read Docker-save metadata without extracting any archive paths."""
    with tarfile.open(path, 'r:*') as archive:
        def read_member(name):
            member = archive.getmember(name)
            if not member.isfile() or member.size > 4 * 1024 * 1024:
                raise ValueError('Invalid image metadata')
            with archive.extractfile(member) as stream:
                return stream.read()
        manifest = json.loads(read_member('manifest.json'))
        if not isinstance(manifest, list) or len(manifest) != 1:
            raise ValueError('Require a single-image Docker-save archive')
        entry = manifest[0]
        if not isinstance(entry, dict):
            raise ValueError('Invalid image manifest')
        if entry.get('RepoTags') != [tag]:
            raise ValueError('Image tag does not match Compose')
        config_name = entry['Config']
        if not isinstance(config_name, str) or PurePosixPath(config_name).is_absolute() or '..' in PurePosixPath(config_name).parts:
            raise ValueError('Invalid image config path')
        config_bytes = read_member(config_name)
        config = json.loads(config_bytes)
        config_sha = hashlib.sha256(config_bytes).hexdigest()
        # Legacy Docker-save and current OCI-backed Docker-save config names.
        if config_name not in (config_sha + '.json', 'blobs/sha256/' + config_sha):
            raise ValueError('Image config content digest mismatch')
        if not isinstance(config, dict) or not isinstance(config.get('config', {}), dict):
            raise ValueError('Invalid image config')
        if config.get('architecture') != architecture or config.get('os') != 'linux':
            raise ValueError('Image architecture mismatch')
        layers = entry.get('Layers')
        if not isinstance(layers, list) or not layers:
            raise ValueError('Image layers missing')
        for name in layers:
            if not archive.getmember(name).isfile():
                raise ValueError('Image layer missing')
        oci_digest = None
        if 'index.json' in archive.getnames():
            index = json.loads(read_member('index.json'))
            descriptors = index.get('manifests', [])
            if index.get('schemaVersion') != 2 or not isinstance(descriptors, list) or len(descriptors) != 1:
                raise ValueError('Require a single-image OCI index')
            descriptor = descriptors[0]
            if descriptor.get('mediaType') != 'application/vnd.oci.image.manifest.v1+json':
                raise ValueError('Unsupported OCI descriptor')
            oci_digest = descriptor.get('digest', '')
            if not isinstance(oci_digest, str) or not oci_digest.startswith('sha256:') or len(oci_digest) != 71:
                raise ValueError('Invalid OCI digest')
            blob = read_member('blobs/sha256/' + oci_digest[7:])
            if hashlib.sha256(blob).hexdigest() != oci_digest[7:] or descriptor.get('size') != len(blob):
                raise ValueError('OCI manifest blob digest mismatch')
            annotations = descriptor.get('annotations', {})
            expected_names = {tag, 'docker.io/library/' + tag}
            if annotations.get('io.containerd.image.name') not in expected_names:
                raise ValueError('OCI descriptor tag mismatch')
            if 'platform' in descriptor and (descriptor['platform'].get('os') != 'linux' or descriptor['platform'].get('architecture') != architecture):
                raise ValueError('OCI descriptor platform mismatch')
            oci = json.loads(blob)
            if oci.get('schemaVersion') != 2 or oci.get('config', {}).get('digest') != 'sha256:' + config_sha or oci['config'].get('size') != len(config_bytes):
                raise ValueError('OCI manifest config mismatch')
            oci_layers = oci.get('layers', [])
            if not isinstance(oci_layers, list) or len(oci_layers) != len(layers):
                raise ValueError('OCI manifest layers mismatch')
            for layer, name in zip(oci_layers, layers):
                if ('blobs/' + layer.get('digest', '').replace(':', '/')) != name or layer.get('size') != archive.getmember(name).size:
                    raise ValueError('OCI layer descriptor mismatch')
    return {'architecture': architecture, 'os': 'linux', 'tag': tag,
            'image_id': 'sha256:' + config_sha, 'config_sha256': config_sha,
            'oci_manifest_digest': oci_digest,
            'accepted_image_ids': ['sha256:' + config_sha] + ([oci_digest] if oci_digest else []),
            'sha256': sha256(path), 'file': 'images/proxy-jp-linux-' + architecture + '.tar.gz',
            'labels': config.get('config', {}).get('Labels') or {}}


def compose_config(path, *, output_format=None):
    command = ['docker', 'compose', '--env-file', os.devnull, '--project-name', 'proxy-jp', '-f', str(path), 'config', '--no-interpolate']
    if output_format:
        command += ['--format', output_format]
    # Capture all diagnostics: expanded config and arbitrary CLI errors may contain secrets.
    return subprocess.check_output(command, stderr=subprocess.PIPE, env={k: v for k, v in os.environ.items() if not k.startswith('COMPOSE_')})


def build(args):
    output = args.output.expanduser().absolute()
    zipped = output.parent / (output.name + '.zip')
    sums = output.parent / (output.name + '.SHA256SUMS')
    if any(p.exists() or p.is_symlink() for p in (output, zipped, sums)):
        raise ValueError('Output folder, zip or checksums already exist')
    items = json.loads(args.connections.read_text())
    profiles = read_profiles({'VPS_LIST': json.dumps(items)})
    # Ask the real Compose implementation to normalize the source runtime schema.
    source_env = {k: v for k, v in os.environ.items()
                  if not k.startswith('COMPOSE_') and k not in ('VPS_LIST', 'PROXY_JP_IMAGE')}
    source_env['VPS_LIST'] = '[]'
    config = json.loads(subprocess.check_output(['docker', 'compose', '--env-file', os.devnull,
        '--project-name', 'proxy-jp', '-f', str(ROOT / 'compose.japan.yaml'), 'config', '--format', 'json'],
        env=source_env, stderr=subprocess.PIPE))
    service = config['services']['japan']
    service['environment']['VPS_LIST'] = json.dumps(items, ensure_ascii=False, indent=2)
    images = {arch: image_metadata(path, arch, service['image'])
              for arch, path in (('arm64', args.image_arm64), ('amd64', args.image_amd64))}
    for name in SOURCES:
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            raise ValueError('Required source missing or symlinked')
    output.parent.mkdir(parents=True, exist_ok=True)
    created = []
    try:
        output.mkdir(mode=0o700)
        created.append(output)
        output.chmod(0o700)
        for name in SOURCES:
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())
        for arch, path in (('arm64', args.image_arm64), ('amd64', args.image_amd64)):
            target = output / images[arch]['file']
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as destination, path.open('rb') as source:
                os.chmod(target, 0o600)
                shutil.copyfileobj(source, destination)
            if sha256(target) != images[arch]['sha256']:
                raise ValueError('Image changed during copy')
        # Compose itself renders the populated JSON to editable YAML (literal VPS_LIST).
        with tempfile.TemporaryDirectory(prefix='proxyjp-compose-') as tmp:
            raw = Path(tmp) / 'compose.json'
            write_private(raw, json_text(config))
            rendered = compose_config(raw).decode()
        write_private(output / 'compose.japan.yaml', rendered)
        compose_config(output / 'compose.japan.yaml', output_format='json')
        write_private(output / 'surge-select.conf', surge_configuration(profiles))
        write_private(output / 'manifest.json', json_text({
            'format_version': 1, 'tags': [p.name for p in profiles], 'images': images,
            'protocol': {'snell': 6, 'mode': 'default', 'shadowtls': 3, 'frp_tls': 'verified-private-ca'},
            'consumer_minimum': {'Surge_Mac': '6.7.0', 'Surge_iOS': '5.20.0'},
            'boot': {'mode': 'system-launchdaemon-lima', 'default_architecture': 'arm64',
                     'requires_gui_login': False, 'filevault': 'must be off'},
            'acceptance': {'Japan_egress': 'pending onsite per-tag test',
                           'cold_boot': 'pending onsite power-cycle without login'},
        }))
        files = sorted(p for p in output.rglob('*') if p.is_file())
        write_private(output / 'SHA256SUMS', ''.join(sha256(p) + '  ' + p.relative_to(output).as_posix() + '\n' for p in files))
        for p in output.rglob('*'):
            p.chmod(0o700 if p.is_dir() or p.suffix == '.sh' else 0o600)
        with zipped.open('xb') as stream:
            created.append(zipped)
            zipped.chmod(0o600)
            with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                # Explicit root entry preserves the private delivery directory mode.
                archive.write(output, output.name + '/')
                for p in sorted(output.rglob('*')):
                    archive.write(p, output.name + '/' + p.relative_to(output).as_posix())
        with sums.open('x') as stream:
            created.append(sums)
            sums.chmod(0o600)
            stream.write(sha256(zipped) + '  ' + zipped.name + '\n')
        return output
    except Exception:
        for path in reversed(created):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('connections', 'output', 'image-arm64', 'image-amd64'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    try:
        build(args)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, subprocess.SubprocessError):
        print('proxy-jp: handoff build failed; check inputs, image metadata, Compose and existing output', file=sys.stderr)
        return 2
    print('proxy-jp: private folder, zip and checksum file created')
    return 0


if __name__ == '__main__':
    sys.exit(main())
