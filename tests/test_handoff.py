import configparser
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import unittest
import zipfile

from proxy_jp.config import encode_profile, read_profiles
from proxy_jp.provision import surge_configuration
from test_config import profile

ROOT = Path(__file__).resolve().parents[1]


def archive(path, architecture, tag='proxy-jp:0.1.0', oci=False, corrupt=False):
    layer_buffer = io.BytesIO()
    with tarfile.open(fileobj=layer_buffer, mode='w'):
        pass
    layer = layer_buffer.getvalue()
    config = json.dumps({'architecture': architecture, 'os': 'linux', 'config': {},
                         'rootfs': {'type': 'layers', 'diff_ids': ['sha256:' + hashlib.sha256(layer).hexdigest()]}}).encode()
    digest = hashlib.sha256(config).hexdigest()
    contents = {digest + '.json': config, 'layer/layer.tar': layer,
                'manifest.json': json.dumps([{'Config': digest + '.json', 'RepoTags': [tag], 'Layers': ['layer/layer.tar']}]).encode()}
    if oci:
        manifest = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                    'config': {'digest': 'sha256:' + digest, 'size': len(config)},
                    'layers': [{'digest': 'sha256:' + hashlib.sha256(layer).hexdigest(), 'size': len(layer)}]}
        manifest_bytes = json.dumps(manifest).encode()
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        manifest_path = 'blobs/sha256/' + manifest_digest
        layer_path = 'blobs/sha256/' + hashlib.sha256(layer).hexdigest()
        contents = {'blobs/sha256/' + digest: config, layer_path: layer,
                    'manifest.json': json.dumps([{'Config': 'blobs/sha256/' + digest, 'RepoTags': [tag], 'Layers': [layer_path]}]).encode(),
                    manifest_path: manifest_bytes + (b' ' if corrupt else b''),
                    'index.json': json.dumps({'schemaVersion': 2, 'manifests': [{
                        'mediaType': manifest['mediaType'], 'digest': 'sha256:' + manifest_digest,
                        'size': len(manifest_bytes), 'platform': {'os': 'linux', 'architecture': architecture},
                        'annotations': {'io.containerd.image.name': 'docker.io/library/' + tag}}]}).encode()}
    with tarfile.open(path, 'w:gz') as tar:
        for name, value in contents.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(value)
            tar.addfile(entry, io.BytesIO(value))
    return 'sha256:' + digest


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.connections = self.root / 'connections.json'
        self.items = [{'tag': tag, 'connection': encode_profile(profile(i))}
                      for i, tag in enumerate(('日本出口', '日本出口-选择', 'JP-A', 'JP-B'))]
        self.connections.write_text(json.dumps(self.items))
        self.images = {}
        self.ids = {}
        for arch in ('arm64', 'amd64'):
            self.images[arch] = self.root / (arch + '.tar.gz')
            self.ids[arch] = archive(self.images[arch], arch)
        self.output = self.root / 'delivery'

    def run_builder(self):
        return subprocess.run(['python3', str(ROOT / 'scripts/make-handoff.py'),
                               '--connections', str(self.connections), '--output', str(self.output),
                               '--image-arm64', str(self.images['arm64']), '--image-amd64', str(self.images['amd64'])],
                              capture_output=True, text=True)

    def test_real_compose_profiles_manifest_checksums_permissions_and_zip(self):
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stderr)
        for item in self.items:
            self.assertNotIn(item['connection'], result.stdout + result.stderr)
        compose = self.output / 'compose.japan.yaml'
        self.assertIn('VPS_LIST: |', compose.read_text())
        cfg = json.loads(subprocess.check_output(['docker', 'compose', '-f', str(compose), 'config', '--format', 'json']))
        profiles = read_profiles(cfg['services']['japan']['environment'])
        self.assertEqual([p.name for p in profiles], [x['tag'] for x in self.items])
        self.assertEqual((self.output / 'surge-select.conf').read_text(), surge_configuration(profiles))
        manifest = json.loads((self.output / 'manifest.json').read_text())
        self.assertEqual(manifest['tags'], [x['tag'] for x in self.items])
        self.assertEqual(manifest['boot'], {
            'mode': 'system-launchdaemon-lima', 'default_architecture': 'arm64',
            'requires_gui_login': False, 'filevault': 'must be off',
        })
        self.assertNotIn('login_reboot', manifest['acceptance'])
        self.assertEqual(manifest['acceptance']['cold_boot'], 'pending onsite power-cycle without login')
        # The recipient must be able to bootstrap from the portable copy alone.
        for name in ('scripts/install-macos.sh', 'scripts/install-macos.py',
                     'skills/proxy-jp-japan-deploy/SKILL.md', 'docs/operations.md', 'HANDOFF-PROMPT.md'):
            self.assertEqual((self.output / name).read_bytes(), (ROOT / name).read_bytes())
        subprocess.run(['bash', '-n', str(self.output / 'scripts/install-macos.sh')], check=True)
        for arch, entry in manifest['images'].items():
            self.assertEqual(entry['architecture'], arch)
            self.assertEqual(entry['tag'], cfg['services']['japan']['image'])
            self.assertEqual(entry['image_id'], self.ids[arch])
            self.assertEqual(entry['config_sha256'], self.ids[arch][7:])
            self.assertEqual(entry['sha256'], hashlib.sha256((self.output / entry['file']).read_bytes()).hexdigest())
        for row in (self.output / 'SHA256SUMS').read_text().splitlines():
            digest, name = row.split('  ', 1)
            self.assertEqual(digest, hashlib.sha256((self.output / name).read_bytes()).hexdigest())
        for path in self.output.rglob('*'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() or path.suffix == '.sh' else 0o600)
            self.assertFalse(path.is_symlink())
            if path.is_file() and path.suffix not in ('.gz',):
                text = path.read_text()
                self.assertNotIn(str(ROOT), text)
                self.assertNotRegex(text, r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')
        names = {p.name for p in self.output.rglob('*')}
        self.assertFalse(names & {'ca.key', 'server.key', 'server.pem', '.git', 'state', '__pycache__'})
        with zipfile.ZipFile(self.output.with_suffix('.zip')) as zipped:
            root = zipped.getinfo('delivery/')
            self.assertTrue(root.is_dir())
            self.assertEqual(stat.S_IMODE(root.external_attr >> 16), 0o700)
            for member in zipped.infolist():
                self.assertEqual(stat.S_IMODE(member.external_attr >> 16),
                                 0o700 if member.is_dir() or member.filename.endswith('.sh') else 0o600)
            self.assertTrue(all(n.startswith('delivery/') for n in zipped.namelist()))
            for name in zipped.namelist():
                if not name.endswith('/'):
                    self.assertEqual(zipped.read(name), (self.root / name).read_bytes())
        external = self.root / 'delivery.SHA256SUMS'
        self.assertIn(hashlib.sha256(self.output.with_suffix('.zip').read_bytes()).hexdigest(), external.read_text())

    def test_oci_manifest_identity_accepts_containerd_and_legacy_forms(self):
        for arch in ('arm64', 'amd64'):
            archive(self.images[arch], arch, oci=True)
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((self.output / 'manifest.json').read_text())
        for arch, image in manifest['images'].items():
            self.assertEqual(image['accepted_image_ids'][0], self.ids[arch])
            self.assertEqual(len(image['accepted_image_ids']), 2)
            self.assertEqual(image['accepted_image_ids'][1], image['oci_manifest_digest'])
            with tarfile.open(self.images[arch]) as tar:
                index = json.load(tar.extractfile('index.json'))
            self.assertEqual(image['oci_manifest_digest'], index['manifests'][0]['digest'])

    def test_corrupt_oci_descriptor_blob_is_rejected(self):
        archive(self.images['amd64'], 'amd64', oci=True, corrupt=True)
        result = self.run_builder()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('handoff build failed', result.stderr)
        self.assertFalse(self.output.exists())

    def test_refuses_existing_folder_zip_or_checksum_without_modifying_them(self):
        for name in ('delivery', 'delivery.zip', 'delivery.SHA256SUMS'):
            with self.subTest(name=name):
                sentinel = self.root / name
                sentinel.write_text('KEEP')
                result = self.run_builder()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('handoff build failed', result.stderr)
                self.assertEqual(sentinel.read_text(), 'KEEP')
                sentinel.unlink()

    def test_rejects_wrong_arch_tag_invalid_profile_without_secret_output_or_partial_artifacts(self):
        for mutation in ('arch', 'tag', 'profile'):
            with self.subTest(mutation=mutation):
                archive(self.images['amd64'], 'arm64' if mutation == 'arch' else 'amd64', 'wrong:tag' if mutation == 'tag' else 'proxy-jp:0.1.0')
                self.connections.write_text('TOPSECRET' if mutation == 'profile' else json.dumps(self.items))
                result = self.run_builder()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('handoff build failed', result.stderr)
                self.assertNotIn('TOPSECRET', result.stdout + result.stderr)
                self.assertFalse(self.output.exists())
                self.assertFalse(self.output.with_suffix('.zip').exists())


if __name__ == '__main__':
    unittest.main()
