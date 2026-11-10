"""Command-line boundary. Runtime imports are intentionally lazy."""
import argparse
import os
import shutil
from pathlib import Path
import sys

from .config import client_configs, read_profiles
from .provision import json_text, provision, write_private


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes untrusted argument values, including secrets.
        self.print_usage(sys.stderr)
        self.exit(2, 'proxy-jp: invalid command arguments; use --help for usage\n')


def render(output, env):
    output = Path(output).absolute()
    bundle = client_configs(read_profiles(env), output)
    if output.exists():
        raise ValueError('Render output already exists; refusing to overwrite')
    output.mkdir(mode=0o700, parents=True)
    try:
        write_private(output / 'sing-box.json', json_text(bundle['sing_box']))
        for i, entry in enumerate(bundle['frpc']):
            write_private(output / f'frpc-{i}.json', json_text(entry['config']))
            write_private(output / f'ca-{i}.pem', entry['ca_pem'])
        return output
    except Exception:
        shutil.rmtree(output)
        raise


def main(argv=None):
    parser = SafeArgumentParser(prog='proxy-jp')
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init')
    init.add_argument('--host', required=True)
    init.add_argument('--name', default='JP')
    init.add_argument('--proxy-port', type=int, default=8443)
    init.add_argument('--tunnel-port', type=int, default=7000)
    init.add_argument('--shadowtls-host', default='www.cloudflare.com')
    init.add_argument('--output', type=Path, required=True)
    renderer = commands.add_parser('render')
    renderer.add_argument('--output', type=Path, required=True)
    for name in ('server', 'client', 'health'):
        command = commands.add_parser(name)
        command.add_argument('--runtime-dir', type=Path, default=Path('/run/proxy-jp'))
        command.add_argument('--config', type=Path, default=Path('/state/frps.json'))
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            provision(args.output, args.host, args.name, args.proxy_port, args.tunnel_port, args.shadowtls_host)
        elif args.command == 'render':
            render(args.output, os.environ)
        else:
            from . import runtime
            return runtime.main(args.command, args)
        return 0
    except (ValueError, OSError):
        print('proxy-jp: operation failed; check input, output permissions and required tools', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
