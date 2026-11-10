import base64
import dataclasses
import json
import subprocess
import unittest
from pathlib import Path

from proxy_jp.config import Profile, encode_profile, decode_profile, read_profiles, client_configs


TEST_CA = subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', '/dev/null', '-days', '1', '-subj', '/CN=unit-test'], capture_output=True, text=True, check=True).stdout


def profile(i=0):
    return Profile(name=f'VPS-{i}', host=f'vps{i}.example.com', tunnel_port=7000,
                   proxy_port=8443, token=f'token-secret-{i}',
                   ca_pem=TEST_CA,
                   tls_server_name=f'vps{i}.example.com', snell_psk=f'snell-secret-{i}',
                   shadowtls_password=f'shadow-secret-{i}', shadowtls_host='www.cloudflare.com')


class ConfigTests(unittest.TestCase):
    def test_roundtrip_and_versioned_payload(self):
        p = profile()
        uri = encode_profile(p)
        self.assertEqual(decode_profile(uri), p)
        payload = json.loads(base64.urlsafe_b64decode(uri[10:] + '=' * (-len(uri[10:]) % 4)))
        self.assertEqual(payload['version'], 1)
        self.assertNotIn('PRIVATE KEY', json.dumps(payload))

    def test_malformed_payload_errors_never_echo_input(self):
        for value in ['proxyjp://invalid', 'https://secret', 'proxyjp://$$$$',
                      'proxyjp://' + base64.urlsafe_b64encode(b'{"version":99,"token":"TOPSECRET"}').decode()]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as cm:
                    decode_profile(value)
                self.assertNotIn(value, str(cm.exception))
                self.assertNotIn('TOPSECRET', str(cm.exception))

    def test_ordered_unicode_tags_replace_original_names(self):
        items = [{'tag': '香港A', 'connection': encode_profile(profile(2))},
                 {'tag': '东京B', 'connection': encode_profile(profile(0))}]
        result = read_profiles({'VPS_LIST': json.dumps(items)})
        self.assertEqual([p.name for p in result], ['香港A', '东京B'])
        self.assertEqual(result[0].host, 'vps2.example.com')

    def test_rejects_duplicate_or_unsafe_tags_and_duplicate_endpoints(self):
        for tags, ids in [(['A', 'A'], [0, 1]), (['A', 'B'], [0, 0]),
                          (['../bad'], [0]), (['a,b'], [0]), (['a\nb'], [0]),
                          (['a=b'], [0]), (['a b'], [0]), (['x' * 65], [0])]:
            with self.subTest(tags=tags):
                with self.assertRaises(ValueError):
                    read_profiles({'VPS_LIST': json.dumps([{'tag': t, 'connection': encode_profile(profile(i))} for t, i in zip(tags, ids)])})
        for raw in ['', '{}', '[]', '[null]', '[{"tag":"A"}]', 'TOPSECRET']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                read_profiles({'VPS_LIST': raw})

    def test_rejects_invalid_host_port_name_and_handshake(self):
        for key, val in [('host', 'x,psk=evil'), ('host', 'https://vps.example'),
                         ('host', '-evil'), ('host', 'bad\nhost'), ('name', 'A=B'),
                         ('proxy_port', 0), ('proxy_port', True), ('tunnel_port', 65536),
                         ('tunnel_port', '7000'), ('shadowtls_host', 'x:443'),
                         ('shadowtls_host', 'x\n[Proxy]'), ('tls_server_name', 'x/y'),
                         ('ca_pem', '-----BEGIN CERTIFICATE-----\nYWJj\n-----END CERTIFICATE-----\n'),
                         ('snell_psk', 'a,b'), ('shadowtls_password', 'a\nb')]:
            with self.subTest(key=key, val=val), self.assertRaises(ValueError):
                encode_profile(dataclasses.replace(profile(), **{key: val}))

    def test_independent_configs_for_one_three_ten_profiles(self):
        for count in (1, 3, 10):
            with self.subTest(count=count):
                bundle = client_configs([profile(i) for i in range(count)], Path('/run/proxy-jp'))
                self.assertEqual(len(bundle['frpc']), count)
                inbounds = bundle['sing_box']['inbounds']
                self.assertEqual(len(inbounds), count * 2)
                for i, entry in enumerate(bundle['frpc']):
                    c = entry['config']
                    self.assertEqual(entry['name'], f'VPS-{i}')
                    self.assertEqual(c['auth']['token'], f'token-secret-{i}')
                    self.assertEqual(c['auth']['additionalScopes'], ['HeartBeats', 'NewWorkConns'])
                    self.assertFalse(c['loginFailExit'])
                    self.assertEqual(c['proxies'][0]['localPort'], 18000 + i * 3)
                    self.assertEqual(c['proxies'][0]['remotePort'], 8443)
                    self.assertTrue(c['transport']['tls']['enable'])
                    self.assertEqual(c['transport']['tls']['serverName'], f'vps{i}.example.com')
                    self.assertEqual(c['transport']['tls']['trustedCaFile'], f'/run/proxy-jp/ca-{i}.pem')
                    self.assertEqual(c['webServer']['addr'], '127.0.0.1')
                    self.assertEqual(c['webServer']['port'], 18002 + i * 3)
                    self.assertTrue(c['webServer']['password'])
                    shadow, snell = inbounds[i * 2:i * 2 + 2]
                    self.assertEqual(shadow['version'], 3)
                    self.assertEqual(shadow['detour'], snell['tag'])
                    self.assertEqual(shadow['users'][0]['password'], f'shadow-secret-{i}')
                    self.assertEqual(snell['psk'], f'snell-secret-{i}')
                    self.assertEqual(snell['version'], 6)
                    self.assertEqual(snell['mode'], 'default')
                    self.assertEqual(snell['listen_port'], 18001 + i * 3)
                    self.assertEqual(snell['listen'], '127.0.0.1')
                self.assertEqual(len({e['config']['webServer']['password'] for e in bundle['frpc']}), count)
                self.assertEqual(bundle['sing_box']['dns']['servers'][0]['type'], 'local')
                self.assertEqual(bundle['sing_box']['outbounds'][0]['type'], 'direct')

    def test_direct_bundle_rejects_duplicates(self):
        for profiles in [[profile(), profile()], [profile(), dataclasses.replace(profile(1), name='VPS-0')]]:
            with self.assertRaises(ValueError):
                client_configs(profiles, '/run/proxy-jp')

    def test_allocation_accepts_last_available_triplet(self):
        # 15,845 profiles consume through 65534; next would exceed TCP range.
        ps = [profile(i) for i in range(15845)]
        self.assertEqual(client_configs(ps, '/run/proxy-jp')['frpc'][-1]['config']['webServer']['port'], 65534)
        with self.assertRaises(ValueError):
            client_configs(ps + [profile(15845)], '/run/proxy-jp')

    def test_snell_v6_psk_ascii_byte_boundaries(self):
        for size in (12, 255):
            with self.subTest(size=size):
                p = dataclasses.replace(profile(), snell_psk='A' * size)
                self.assertEqual(decode_profile(encode_profile(p)), p)
                snell = client_configs([p], '/run/proxy-jp')['sing_box']['inbounds'][1]
                self.assertEqual(snell['psk'], 'A' * size)
                self.assertEqual(snell['version'], 6)
                self.assertEqual(snell['mode'], 'default')
        for value in ('', 'A' * 11, 'A' * 256, 'A' * 512, 'é' * 12, '密' * 12):
            with self.subTest(size=len(value)):
                p = dataclasses.replace(profile(), snell_psk=value)
                payload = {'version': 1, **dataclasses.asdict(p)}
                raw_uri = 'proxyjp://' + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
                for operation in (lambda: encode_profile(p), lambda: decode_profile(raw_uri),
                                  lambda: client_configs([p], '/run/proxy-jp')):
                    with self.assertRaises(ValueError):
                        operation()

    def test_other_credentials_keep_existing_length_limits(self):
        for size in (1, 512):
            p = dataclasses.replace(profile(), token='T' * size, shadowtls_password='S' * size)
            self.assertEqual(decode_profile(encode_profile(p)), p)

    def test_udp_domains_are_resolved_before_direct_packet_routing(self):
        # Without this action real Snell6 UDP replies retain a hostname source;
        # Snell rejects it with "udp response source is not an ip".
        route = client_configs([profile()], '/run/proxy-jp')['sing_box']['route']
        self.assertEqual(route.get('rules'), [
            {'network': 'udp', 'action': 'resolve', 'server': 'local'}])
        self.assertEqual(route['final'], 'direct')
