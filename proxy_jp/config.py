"""Validated, versioned connection profiles and native runtime configuration."""
import base64
import binascii
import hashlib
import ipaddress
import json
import re
import ssl
from functools import lru_cache
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path


@dataclass(frozen=True)
class Profile:
    name: str
    host: str
    tunnel_port: int
    proxy_port: int
    token: str = field(repr=False)
    ca_pem: str = field(repr=False)
    tls_server_name: str
    snell_psk: str = field(repr=False)
    shadowtls_password: str = field(repr=False)
    shadowtls_host: str


def validate_name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or not all(c.isalpha() or c.isdigit() or c in '._-' for c in value) or value in ('.', '..'):
        raise ValueError('Invalid node name or tag')
    return value


def validate_host(value):
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ValueError('Invalid host')
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    if not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', part) for part in value.split('.')):
        raise ValueError('Invalid host')
    return value.lower()


def validate_profile_fields(profile):
    if not isinstance(profile, Profile):
        raise ValueError('Invalid profile')
    validate_name(profile.name)
    for host in (profile.host, profile.tls_server_name, profile.shadowtls_host):
        validate_host(host)
    for port in (profile.tunnel_port, profile.proxy_port):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Invalid TCP port')
    if profile.tunnel_port == profile.proxy_port:
        raise ValueError('Tunnel and proxy ports must differ')
    for secret in (profile.token, profile.snell_psk, profile.shadowtls_password):
        if not isinstance(secret, str) or not re.fullmatch(r'[A-Za-z0-9_+/=-]{1,512}', secret):
            raise ValueError('Invalid credential')
    # The credential character set above is ASCII, so character and byte counts agree.
    if not 12 <= len(profile.snell_psk) <= 255:
        raise ValueError('Invalid Snell v6 PSK length')
    return profile


@lru_cache(maxsize=128)
def _validate_ca(ca_pem):
    try:
        if not isinstance(ca_pem, str) or len(ca_pem) > 65536 or 'PRIVATE KEY' in ca_pem:
            raise ValueError
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=ca_pem)
    except (ValueError, TypeError, ssl.SSLError):
        raise ValueError('Invalid CA certificate') from None


def validate_profile(profile):
    validate_profile_fields(profile)
    if not isinstance(profile.ca_pem, str):
        raise ValueError('Invalid CA certificate')
    _validate_ca(profile.ca_pem)
    return profile


def encode_profile(profile):
    validate_profile(profile)
    payload = {'version': 1, **asdict(profile)}
    return 'proxyjp://' + base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()).decode().rstrip('=')


def decode_profile(uri):
    try:
        if not isinstance(uri, str) or len(uri) > 131072 or not uri.startswith('proxyjp://'):
            raise ValueError
        encoded = uri[10:]
        if not re.fullmatch(r'[A-Za-z0-9_-]+={0,2}', encoded):
            raise ValueError
        data = json.loads(base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True))
        if not isinstance(data, dict) or type(data.get('version')) is not int or data.pop('version') != 1:
            raise ValueError
        return validate_profile(Profile(**data))
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
        raise ValueError('Invalid or unsupported connection profile') from None


def _validate_profiles(profiles):
    if not profiles:
        raise ValueError('At least one VPS is required')
    if 18002 + (len(profiles) - 1) * 3 > 65535:
        raise ValueError('Too many VPS entries for available loopback ports')
    names, endpoints = set(), set()
    for profile in profiles:
        validate_profile(profile)
        endpoint = (validate_host(profile.host), profile.tunnel_port)
        if profile.name in names or endpoint in endpoints:
            raise ValueError('Duplicate VPS tag or tunnel endpoint')
        names.add(profile.name)
        endpoints.add(endpoint)


def read_profiles(env):
    try:
        items = json.loads(env.get('VPS_LIST', ''))
        if not isinstance(items, list):
            raise ValueError
        profiles = []
        for item in items:
            if not isinstance(item, dict) or set(item) != {'tag', 'connection'}:
                raise ValueError
            validate_name(item['tag'])
            profiles.append(replace(decode_profile(item['connection']), name=item['tag']))
        _validate_profiles(profiles)
        return profiles
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ValueError('Invalid VPS_LIST: require unique safe tags and tunnel endpoints') from None


def client_configs(profiles, runtime_dir):
    profiles = list(profiles)
    _validate_profiles(profiles)
    runtime_dir = Path(runtime_dir).absolute()
    inbounds, clients = [], []
    for i, p in enumerate(profiles):
        port = 18000 + i * 3
        snell_tag = f'snell-{i}'
        inbounds.extend([
            {'type': 'shadowtls', 'tag': f'shadowtls-{i}', 'listen': '127.0.0.1', 'listen_port': port,
             'version': 3, 'users': [{'name': p.name, 'password': p.shadowtls_password}],
             'handshake': {'server': p.shadowtls_host, 'server_port': 443}, 'strict_mode': True, 'detour': snell_tag},
            {'type': 'snell', 'tag': snell_tag, 'listen': '127.0.0.1', 'listen_port': port + 1,
             'version': 6, 'mode': 'default', 'psk': p.snell_psk},
        ])
        admin_password = hashlib.sha256(('proxy-jp admin\0' + p.token + '\0' + str(i)).encode()).hexdigest()
        config = {
            'serverAddr': p.host, 'serverPort': p.tunnel_port, 'loginFailExit': False,
            'auth': {'method': 'token', 'token': p.token, 'additionalScopes': ['HeartBeats', 'NewWorkConns']},
            'transport': {'protocol': 'tcp', 'tls': {'enable': True, 'serverName': p.tls_server_name,
                                                   'trustedCaFile': str(runtime_dir / f'ca-{i}.pem')}},
            'webServer': {'addr': '127.0.0.1', 'port': port + 2, 'user': 'proxy-jp', 'password': admin_password},
            'log': {'to': 'console', 'level': 'info', 'disablePrintColor': True},
            'proxies': [{'name': 'snell', 'type': 'tcp', 'localIP': '127.0.0.1', 'localPort': port, 'remotePort': p.proxy_port}],
        }
        clients.append({'name': p.name, 'config': config, 'ca_pem': p.ca_pem})
    return {'sing_box': {'log': {'level': 'info'}, 'dns': {'servers': [{'type': 'local', 'tag': 'local'}]},
                         'inbounds': inbounds, 'outbounds': [{'type': 'direct', 'tag': 'direct'}],
                         # Snell UDP replies require an IP source; resolve before packet routing
                         # so direct's reply address is not mapped back to a hostname.
                         'route': {'rules': [{'network': 'udp', 'action': 'resolve', 'server': 'local'}],
                                   'final': 'direct', 'default_domain_resolver': 'local'}}, 'frpc': clients}
