"""Create a private, self-contained VPS deployment without overwriting state."""
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess

from .config import Profile, encode_profile, validate_host, validate_name, validate_profile_fields


def write_private(path, text):
    with open(path, 'x', encoding='utf-8') as stream:
        os.chmod(path, 0o600)
        stream.write(text)


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def _openssl(output, *args):
    try:
        subprocess.run(['openssl', *args], cwd=output, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, subprocess.CalledProcessError):
        raise ValueError('OpenSSL certificate generation failed') from None


def surge_proxy_line(profile):
    """Canonical native Surge policy shared by provisioning and handoff export."""
    validate_profile_fields(profile)
    host = '[' + profile.host + ']' if ':' in profile.host else profile.host
    return (f'{profile.name} = snell, {host}, {profile.proxy_port}, psk={profile.snell_psk}, '
            f'version=6, mode=default, shadow-tls-password={profile.shadowtls_password}, '
            f'shadow-tls-sni={profile.shadowtls_host}, shadow-tls-version=3\n')


def surge_configuration(profiles):
    profiles = list(profiles)
    if not profiles or len({p.name for p in profiles}) != len(profiles):
        raise ValueError('Require unique Surge policies')
    names = {p.name for p in profiles}
    group, suffix = '日本出口', 1
    while group in names:
        group = '日本出口-选择' + (f'-{suffix}' if suffix > 1 else '')
        suffix += 1
    return ('[Proxy]\n' + ''.join(surge_proxy_line(p) for p in profiles)
            + '\n[Proxy Group]\n' + group + ' = select, ' + ', '.join(p.name for p in profiles)
            + '\n\n[Rule]\nFINAL,' + group + '\n')


def provision(output, host, name='JP', proxy_port=8443, tunnel_port=7000, shadowtls_host='www.cloudflare.com'):
    output = Path(output)
    host = validate_host(host)
    validate_name(name)
    shadowtls_host = validate_host(shadowtls_host)
    # Validate all caller inputs before creating state or invoking OpenSSL.
    p = Profile(name, host, tunnel_port, proxy_port, secrets.token_urlsafe(32),
                '', host,
                secrets.token_urlsafe(32), secrets.token_urlsafe(32), shadowtls_host)
    validate_profile_fields(p)
    try:
        output.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        raise ValueError('Output already exists; refusing to overwrite credentials') from None
    try:
        os.chmod(output, 0o700)
        try:
            ipaddress.ip_address(host)
            san = 'IP:' + host
        except ValueError:
            san = 'DNS:' + host
        write_private(output / 'ca.cnf', '[req]\ndistinguished_name=dn\nx509_extensions=ca\nprompt=no\n[dn]\nCN=proxy-jp private CA\n[ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n')
        write_private(output / 'server.ext', 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=' + san + '\n')
        _openssl(output, 'req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-days', '3650', '-sha256', '-keyout', 'ca.key', '-out', 'ca.pem', '-config', 'ca.cnf')
        _openssl(output, 'req', '-new', '-newkey', 'rsa:3072', '-nodes', '-sha256', '-keyout', 'server.key', '-out', 'server.csr', '-subj', '/CN=proxy-jp server')
        _openssl(output, 'x509', '-req', '-in', 'server.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key', '-CAcreateserial', '-out', 'server.pem', '-days', '825', '-sha256', '-extfile', 'server.ext')
        p = Profile(name, host, tunnel_port, proxy_port, p.token, (output / 'ca.pem').read_text(), host, p.snell_psk, p.shadowtls_password, shadowtls_host)
        uri = encode_profile(p)
        entry = {'tag': name, 'connection': uri}
        frps = {'bindAddr': '0.0.0.0', 'bindPort': tunnel_port,
                'auth': {'method': 'token', 'token': p.token, 'additionalScopes': ['HeartBeats', 'NewWorkConns']},
                'allowPorts': [{'single': proxy_port}], 'maxPortsPerClient': 1,
                'transport': {'tls': {'force': True, 'certFile': '/state/server.pem', 'keyFile': '/state/server.key'}},
                'log': {'to': 'console', 'level': 'info', 'disablePrintColor': True}, 'detailedErrorsToClient': False}
        write_private(output / 'frps.json', json_text(frps))
        owner = output.stat()
        compose = f'''services:
  vps:
    image: proxy-jp:0.1.0
    command: ["server"]
    user: "{owner.st_uid}:{owner.st_gid}"
    restart: unless-stopped
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    tmpfs:
      - /run/proxy-jp:uid={owner.st_uid},gid={owner.st_gid},mode=0700
      - /tmp
    volumes:
      - ./:/state:ro
    ports:
      - "{proxy_port}:{proxy_port}/tcp"
      - "{tunnel_port}:{tunnel_port}/tcp"
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
'''
        write_private(output / 'compose.vps.yaml', compose)
        write_private(output / 'connection.txt', uri + '\n')
        write_private(output / 'connection.json', json_text(entry))
        write_private(output / 'japan.env', "VPS_LIST='" + json.dumps([entry], ensure_ascii=False, separators=(',', ':')) + "'\n")
        write_private(output / 'surge-proxy.conf', surge_proxy_line(p))
        write_private(output / 'surge-example.conf', surge_configuration([p]))
        for filename in ('ca.cnf', 'server.ext', 'server.csr', 'ca.srl'):
            (output / filename).unlink(missing_ok=True)
        for file in output.iterdir():
            file.chmod(0o600)
        return output
    except Exception:
        shutil.rmtree(output)
        raise
