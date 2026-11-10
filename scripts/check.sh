#!/bin/bash
# Focused offline checks. Build the image first; e2e is explicitly separate.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m unittest discover -s tests -p 'test_*.py' -v
bash -n scripts/build-images.sh scripts/install-macos.sh scripts/check.sh
docker compose -f compose.japan.yaml config --quiet
# Pin once so another platform build cannot replace the image mid-check.
check_image="$(docker image inspect "${PROXY_JP_TEST_IMAGE:-proxy-jp:0.1.0}" --format '{{.Id}}')"
check_dir="$(mktemp -d -t proxyjp-check.XXXXXXXX)"
trap 'rm -rf -- "$check_dir"' EXIT
python3 - "$check_dir" <<'PY'
import json
from pathlib import Path
import sys
from proxy_jp.config import client_configs, decode_profile
from proxy_jp.provision import provision
root = Path(sys.argv[1])
state = provision(root / 'vps', 'vps.test')
p = decode_profile((state / 'connection.txt').read_text().strip())
bundle = client_configs([p], '/checks/runtime')
runtime = root / 'runtime'
runtime.mkdir()
(runtime / 'sing-box.json').write_text(json.dumps(bundle['sing_box']))
(runtime / 'frpc.json').write_text(json.dumps(bundle['frpc'][0]['config']))
(runtime / 'ca-0.pem').write_text(p.ca_pem)
# Generated production server paths refer to /state; mount that exact location.
PY
docker compose -f "$check_dir/vps/compose.vps.yaml" config --quiet
docker run --rm --user 0:0 --entrypoint sing-box -v "$check_dir:/checks:ro" "$check_image" check -c /checks/runtime/sing-box.json
docker run --rm --user 0:0 --entrypoint frpc -v "$check_dir:/checks:ro" "$check_image" verify -c /checks/runtime/frpc.json
docker run --rm --user 0:0 --entrypoint frps -v "$check_dir/vps:/state:ro" "$check_image" verify -c /state/frps.json
echo 'PASS unit, shell syntax, Compose, generated sing-box/frpc/frps configuration checks'
echo 'macOS installer tests simulate host mutations; real boot acceptance remains onsite.'
echo 'Run python3 tests/e2e.py separately for isolated real TCP/UDP and failure scenarios.'
