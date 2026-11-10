# 日常运维与验收

日本端命令以默认安装目录 `~/.proxy-jp` 为准；自定义安装目录时同步替换。`bin/compose` 自动使用安装的 Compose、可选 env 和固定项目名，`bin/docker` 固定专用 Lima socket/config；不要把这些命令换成宿主现有 Desktop 的 Docker。VPS 的管理命令仍在其服务器上执行。首次源码获取/安装见 README，需安装器的 CLI bootstrap，无需图形 Docker 应用。

## 看状态与日志

```bash
"$HOME/.proxy-jp/bin/compose" ps
"$HOME/.proxy-jp/bin/compose" exec japan python -m proxy_jp health
"$HOME/.proxy-jp/bin/compose" logs --tail 100 japan
```

| 状态 | 含义 | health 退出码 |
| --- | --- | --- |
| online | 所有配置的 frpc 代理均已注册且 running | 0 |
| partial | 部分代理在线，可用节点仍工作 | 0 |
| offline | 所有代理都不在线 | 1 |
| unavailable | 本地运行状态缺失或无法读取 | 1 |

日本端输出包含各 tag 和 online 布尔值，不输出凭据。Docker `healthy` 包括 partial，监控若需全部在线应解析 JSON 的 `status`，不能只看退出码。VPS 端 health 只确认 frps 端口能接受连接；即使日本离线，VPS 也可能 healthy。

health 验证的是本地 frpc 注册状态，无法证明 Snell/ShadowTLS 密码一致、握手站可达、DNS 正常或目标能访问。现场务必在 Surge 逐个手动选节点，用可信回显服务查看出口公网 IP，应均为日本网络；再验证域名访问和实际需要的 UDP 应用。上海到 VPS 和 VPS 到日本的路径延迟、丢包及带宽均会影响体验。

## 故障定位

1. 日本整机离线、睡眠或容器运行时停止，会让全部节点失效。先检查远程管理通道，再看电源、网络和运行时状态。
2. 单个 tag 离线时检查该 VPS 的 Docker、frps 日志、隧道端口安全组、域名/IP、CA 和 token。独立 frpc 会自行重连，异常退出也会独立重启；其他节点继续工作。
3. 全部 tag 在线但访问失败，检查 Surge 的 PSK、Snell v6 / mode=default、ShadowTLS v3 密码/SNI，随后检查日本端 DNS、目标访问和握手站。
4. 默认 ShadowTLS 握手站为 `www.cloudflare.com:443`，日本端必须能连接它且能完成 TLS1.3 握手。可在生成时通过 `--shadowtls-host` 指定经验证的站点；Surge SNI 和日本端握手主机必须一致。站点证书、TLS行为或可达性变化可能影响代理。
5. 若容器日志指出外部连接却被导向宿主回环代理，本项目运行时会剔除子进程环境的 HTTP/HTTPS/ALL/NO_PROXY。检查自定义镜像或外部配置，不要依赖 Docker 自动注入的宿主回环代理建立隧道。

日志可能包含目标域名与连接元数据，分享前审查。不要公开 `docker inspect` 的环境、完整展开 Compose、URI、CA 私钥、PSK 或 token。

## 配置更新与凭据轮换

生成工具拒绝覆盖已有目录，以免丢失唯一的私钥或误改部署。轮换某台 VPS 时，在**新目录**重新执行 `init`，获得一套新 CA、证书、token、Snell PSK 和 ShadowTLS 密码。安全备份后，把新服务器文件更新到对应 VPS，将日本 `VPS_LIST` 中对应条目的完整 URI 替换，并更新 Surge 节点行。三端协调切换，该节点切换期间会短暂不可用；其他节点可继续使用。

更新日本安装副本后执行 `"$HOME/.proxy-jp/bin/compose" up -d --pull never --no-build`，Compose 会按配置变化重建容器，重新生成 runtime 文件。已由 Mac 安装器复制的配置需更新**安装目录中的副本**，修改原 checkout 不会自动同步。保留原 VM/socket 路径；移走目录不等于卸载 VM，不能借此绕过同名实例冲突。安装器拒绝覆盖已有目录。

VPS 的只读 bind mount 若替换整个目录，需要 `docker compose ... up -d --force-recreate` 重新挂载。修改服务器 JSON 后也必须重建/重启 frps 才会生效。确认全部 tag 恢复 online，再在 Surge 对新节点做真实流量验证。旧凭据作废后安全删除旧副本，保留必要的加密备份。

## 停止、恢复、升级

```bash
# 仅此项目，不影响其他 Docker 工作负载
"$HOME/.proxy-jp/bin/compose" stop
"$HOME/.proxy-jp/bin/compose" up -d --pull never --no-build
```

升级前保存已验证镜像身份、旧包/不可变引用及私密配置。在线新引用必须与安装 Compose 完全一致；仅在专用引擎中 `"$HOME/.proxy-jp/bin/docker" pull --platform linux/arm64 "$INSTALL_IMAGE"`（Intel 为 linux/amd64），检查精确 RepoDigests、Linux/原生架构再更新安装副本并 `"$HOME/.proxy-jp/bin/compose" up -d --pull never --no-build --force-recreate`。离线则在专用引擎 load 并按对应 manifest 校验。失败可恢复旧配置和旧镜像身份。不要使用全局 Docker 清理。

系统启动服务和当前状态：

```sh
launchctl print system/io.lima-vm.daemon.proxy-jp
"$HOME/.proxy-jp/bin/docker" info
"$HOME/.proxy-jp/bin/compose" ps --all
"$HOME/.proxy-jp/bin/compose" logs --tail 100 japan
```

检查 `~/.proxy-jp/installation-report.json`、`/Library/LaunchDaemons/io.lima-vm.daemon.proxy-jp.plist` 和 plist 指定的 stdout/stderr 日志路径；分享前脱敏。报告记录专用 LIMA_HOME、socket、源文件哈希、架构、健康和电源原值/读回值。daemon 必须在 system 域 running 且有 PID，不能只看 plist 存在。容器日志限制每文件 10MB、3 个文件。安装失败保留报告和现有状态，按 failed_stage 排查，避免盲目重装。

`"$HOME/.proxy-jp/bin/uninstall"` 核对所有权后禁用此 VM 的系统启动项；它保留配置、凭据、镜像/VM 数据和电源设置。需要停止业务时先用专用 Compose stop；重新启用时按现场维护安排处理保留的实例，不覆盖已有安装目录。电源恢复由现场负责人依据报告保存的原值另行操作。

`restart: unless-stopped` 会记住显式 stop；`compose stop` 后必须显式 up，不能把故意停止后的行为当成自动冷启动失败，也不能把当前会话 restart 当成冷启动成功。

## 自动验证和真实交付边界

维护方在独立开发/CI 构建机先运行 `scripts/check.sh`，再运行 `python3 tests/e2e.py`；这些是开发验证，不要求接收人在现有 Desktop 上运行。后者不连接公开 VPS，无公开监听端口，使用随机名称的两个内部网络：消费者与三台 frps 在前端网络，DNS/HTTP/UDP/私有 TLS1.3 握手目标只在日本出口网络，日本容器连接两边。测试先证实消费者及各 VPS 无法直接解析目标或访问目标 IP，再从真实 Snell v6 / ShadowTLS v3 消费者逐节点访问目标并核对日本源地址。TCP 使用目标域名，UDP 分别使用目标域名和 IP 验证真实 SOCKS5 UDP 数据报与回包。日本端对 UDP 目标先执行显式 DNS resolve，确保 Snell 回包使用 IP 源地址。

隔离测试还验证错误 Snell/ShadowTLS 密码拒绝、三台 VPS 分别停机/重连、partial 状态、frpc 被 SIGKILL 后独立恢复且兄弟 PID 不变、日本停止后三条数据路径全部失败、错误 frp CA/token 的实际 TLS/鉴权拒绝。固定镜像 ID 防止并行构建覆盖标签。无论成功失败，finally 均删除本次随机前缀的容器与网络；可用输出的前缀检查有无残留。不要对其他项目做清理操作。

本地容器测试不能替代以下现场记录，目前均待真实环境验收：

- 日本机器实际网络出口、目标 DNS、长时间运行和网络断开后恢复。
- 上海实际 Surge Mac 6.7.0+ / iOS 5.20.0+ 对每个节点的 TCP、UDP、远端解析和手动切换。
- 接收人 Mac 真实冷启动，所有用户仍未登录时，专用运行时/系统服务自动恢复并完成上海实际访问。
- 公网 VPS 的系统架构、Docker、防火墙/安全组、流量限额和远程管理可用性。

部署真实服务器与公开验收应在获得对应机器授权后进行。确保已有独立 SSH/远程桌面通道，避免只依赖待部署的代理管理日本端。

<a id="validated-profile-inventory"></a>
## 已验证的节点清单（不展开凭据）

在源码/交付根目录执行。此步骤只渲染配置，不访问 Docker engine；可在 VM 创建前完成，但必须已有原生 Python 与 standalone Compose。全新机器无需提前跑此命令：先运行 README 的单条安装入口，它会先 bootstrap CLI 并在创建 VM 前私下校验全部配置。若安装中断在 VM/拉取阶段而 CLI 已安装，下面仍可检查。缺少 CLI 时 `--dry-run` 只报告缺依赖；不要用 Desktop engine 补足本项目配置检查。

`read_profiles` 接收日本服务环境变量 dict。完整 Compose 输出/错误只留在内存；下面仅输出 tag、地址和端口，属于私密清单，请只在本机核对。设置实际私密 Compose 路径，可选 `PROFILE_ENV_FILE`；安装后可改为 `"$HOME/.proxy-jp/compose.japan.yaml"` 和实际安装 env 路径。在线无 manifest 时与私密交付清单核对；离线核对 manifest 全部 tags。

```sh
PROFILE_PREFIX=/usr/local
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ]; then PROFILE_PREFIX=/opt/homebrew; fi
PROFILE_COMPOSE=/absolute/private/compose.japan.yaml PROFILE_ENV_FILE='' \
PROFILE_COMPOSE_CLI="$PROFILE_PREFIX/bin/docker-compose" "$PROFILE_PREFIX/bin/python3" - <<'PYCODE'
import json
import os
from pathlib import Path
import subprocess
from proxy_jp.config import read_profiles

command = [os.environ['PROFILE_COMPOSE_CLI'], '--project-name', 'proxy-jp',
           '--env-file', os.environ.get('PROFILE_ENV_FILE') or os.devnull,
           '-f', str(Path(os.environ['PROFILE_COMPOSE']).resolve())]
env = {k: v for k, v in os.environ.items()
       if not k.startswith('COMPOSE_') and k not in ('VPS_LIST', 'PROXY_JP_IMAGE')}
try:
    result = subprocess.run(command + ['config', '--format', 'json'], env=env,
                            check=True, capture_output=True, text=True)
    environment = json.loads(result.stdout)['services']['japan']['environment']
    profiles = read_profiles(environment)
except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError):
    raise SystemExit('Profile inventory failed; inspect private configuration locally') from None
for p in profiles:
    print(f'{p.name}\t{p.host}\ttunnel={p.tunnel_port}\tproxy={p.proxy_port}')
PYCODE
```

保留全部条目，单条离线也不删除其配置。配置、env、Surge 原生节点只经可信私密渠道传输，不需要日本端持有 VPS 管理密钥。

<a id="onsite-traffic-acceptance"></a>
## 现场流量验收：日本与上海两端配合

每个 tag 独立填写 HTTPS、远端 DNS、UDP、出口 IP、Surge 请求所用策略和时间；依次手动选择，不用自动回退掩盖失败。若日本操作人不能控制上海设备，将本节交给上海操作人，相关字段保持 **NOT RUN**，记录负责人；软件/访问条件不满足则 **BLOCKED**。不要把本机容器模拟结果填入上海实测字段。以下终端命令适用于上海 Mac；iOS 操作人需用其真实应用访问和 Surge UDP 会话证据，不能把 Mac 命令结果移作 iOS 证明。

日本端后续 Python 小程序需要原生工具 PATH（安装器子进程的 PATH 不会自动修改当前终端），先在日本终端执行：

```sh
JP_TOOL_PREFIX=/usr/local
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ]; then JP_TOOL_PREFIX=/opt/homebrew; fi
export PATH="$JP_TOOL_PREFIX/bin:$PATH"
python3 --version
```

上海命令也需要可用 Python3；缺软件/权限则记录 BLOCKED 并安排操作人，不以未运行的命令充作证据。

### 上海 HTTPS 与公网出口

先从上海 Surge 的当前设置/配置中发现其**实际 HTTP 监听地址与端口**，确认服务正在监听，再设置变量。不要假定 6152、7890 等默认端口。以下仅以回环地址表示本机监听；按实际设置修改，禁止把占位符原样执行：

```sh
export SURGE_HTTP_PROXY='http://127.0.0.1:替换为实际HTTP监听端口'
curl --fail --show-error --silent --noproxy '' \
  --connect-timeout 10 --max-time 30 --proxy "$SURGE_HTTP_PROXY" \
  https://api.ipify.org
curl --fail --show-error --silent --noproxy '' \
  --connect-timeout 10 --max-time 30 --proxy "$SURGE_HTTP_PROXY" \
  --output /dev/null --write-out 'https_code=%{http_code} seconds=%{time_total}\n' \
  https://example.com/
```

Surge 请求视图必须显示测试请求匹配交付手动 select 组、最终使用当前指定的 tag；记录目标、时间、策略链及成功状态。日本 Mac 同时执行 `curl -4 --fail --show-error --silent --noproxy '*' https://api.ipify.org`；上海也可对第一条 curl 加 `-4` 固定入口寻址，并记录实际服务端返回地址族。两边公网出口需按同一地址族比较，并以可信 IP 地理信息核对日本归属。代理入口 IP 的地址族不决定目标出口地址族，若不一致要继续取证，不能直接宣称一致。

### 远端 DNS：仅日本容器网络可见的临时域名

普通公共域名访问成功不足以证明域名在日本解析。下面在日本**已有 japan 容器的 Docker 网络**临时创建一个 HTTP 标记服务，随机 `.invalid` 别名仅在该网络注册、不发布任何宿主端口。使用正在运行的同一镜像，无需下载。它不更改现有服务、网络连接或生产配置。

在日本 Mac 的同一个终端执行并保留这些变量：

```sh
JP_DOCKER="$HOME/.proxy-jp/bin/docker"
JP_CID="$("$HOME/.proxy-jp/bin/compose" ps -q japan)"
[ -n "$JP_CID" ] || { echo 'Japan container is not running'; exit 1; }
JP_NETWORK="$("$JP_DOCKER" inspect --format '{{json .NetworkSettings.Networks}}' "$JP_CID" | python3 -c '
import json,sys
networks=json.load(sys.stdin)
if len(networks)!=1: raise SystemExit("Multiple networks: choose the actual Japan egress network explicitly")
print(next(iter(networks)))')"
[ -n "$JP_NETWORK" ] || exit 1
JP_IMAGE_ID="$("$JP_DOCKER" inspect --format '{{.Image}}' "$JP_CID")"
FIXTURE_NAME="proxyjp-accept-$(python3 -c 'import secrets; print(secrets.token_hex(6))')"
FIXTURE_ALIAS="$FIXTURE_NAME.invalid"
FIXTURE_MARKER="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
"$JP_DOCKER" run --detach --pull never --name "$FIXTURE_NAME" \
  --network "$JP_NETWORK" --network-alias "$FIXTURE_ALIAS" \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --entrypoint python3 "$JP_IMAGE_ID" -u -c '
from http.server import BaseHTTPRequestHandler, HTTPServer
import sys
marker = sys.argv[1].encode()
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(marker)))
        self.end_headers()
        self.wfile.write(marker)
    def log_message(self, *args): pass
HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
' "$FIXTURE_MARKER"
printf 'alias=%s\nmarker=%s\n' "$FIXTURE_ALIAS" "$FIXTURE_MARKER"
```

若网络有多个，先人工确认现有日本容器可用的出口网络，然后显式设置 `JP_NETWORK`；不要擅自新建或重连生产网络。先在日本确认 fixture 可达，再通知上海操作人其 alias、marker（均为本次随机测试标记，不是代理凭据）：

```sh
"$JP_DOCKER" exec "$JP_CID" python3 -c \
  'import sys,urllib.request; opener=urllib.request.build_opener(urllib.request.ProxyHandler({})); print(opener.open(sys.argv[1],timeout=5).read().decode())' \
  "http://$FIXTURE_ALIAS:8080/"
```

在上海 Mac，将收到的 alias/marker 赋给同名变量。先用 `python3 -c 'import socket,sys; print(socket.getaddrinfo(sys.argv[1],8080))' "$FIXTURE_ALIAS"` 做消费者本地解析基线，预期解析失败；若可解析，需要调查缓存/本地 DNS，不能据此宣称证明成立。在 Surge 现有配置中临时添加精确的 `DOMAIN,实际alias,交付select组名` 规则，置于会截获它的其他规则之前，保留现有配置和回滚记录。**组名从交付配置读取，不能猜测**。选择一个 tag 后执行：

```sh
curl --fail --show-error --silent --noproxy '' \
  --connect-timeout 10 --max-time 30 --proxy "$SURGE_HTTP_PROXY" \
  "http://$FIXTURE_ALIAS:8080/"
```

应得到完全一致的 marker，且 Surge 请求视图显示原始域名、临时精确规则和选中的 tag。域名由 HTTP 代理请求交给 Surge，再经选中节点交给日本 sing-box；仅日本 Docker DNS 能解析本次私有别名。不要使用 `--resolve`、本地 hosts 映射或先解析后替换 URL 为 IP，它们会破坏此项证明。逐个 tag 重复，记录结果。所有操作人完成后，在**日本原终端**清理且只清理本次 fixture：

```sh
"$JP_DOCKER" rm --force "$FIXTURE_NAME"
```

同时移除本次精确临时 Surge 规则。失败也按记录的随机容器名清理；不删除已有网络、生产容器或其他项目。

### UDP：Surge 实际承载的独立 STUN 数据报

HTTP CONNECT/curl 只验证 TCP，不能当作 UDP 成功。上海 Mac 可在其负责人授权下使用 Surge **Enhanced Mode** 捕获下面 Python 程序的 UDP；确认该进程未被 bypass，并临时添加测试 STUN IPv4 `/32` 到交付手动 select 组的精确规则（例如 `IP-CIDR,实际STUN_IP/32,实际组名,no-resolve`），优先于其他截获规则。保留原配置以便撤销。Surge 官方说明 Enhanced Mode 可处理 UDP；其 SOCKS5 监听也支持 UDP ASSOCIATE，但下面程序使用原生 UDP socket，**不是** SOCKS5 客户端，需要 Enhanced Mode 捕获。[Surge UDP 文档](https://manual.nssurge.com/policies/udp.html)、[Enhanced Mode](https://manual.nssurge.com/features/enhanced-mode.html)。

使用 WebRTC 官方测试同样采用的 `stun.l.google.com:19302` 作为公共 STUN 目标；公共服务不可达时记录 BLOCKED，并与负责人选定另一个可用 STUN 目标，不能把超时记作代理不支持 UDP。[WebRTC 官方测试源](https://webrtc.googlesource.com/src/+/refs/heads/main/pc/peer_connection_factory_unittest.cc)。先在日本 Mac 发现其一个公网 IPv4，将同一值交给上海操作人设置 `STUN_IP` 并填入上述精确规则；避免上海 Enhanced Mode 的 DNS 返回测试用虚拟 IP：

```sh
export STUN_IP="$(python3 -c 'import ipaddress,socket; ip=socket.gethostbyname("stun.l.google.com"); assert ipaddress.ip_address(ip).is_global, "Need a real public IPv4"; print(ip)')"
printf 'UDP test destination=%s:19302\n' "$STUN_IP"
```

对每个手选 tag，在上海执行以下独立 UDP Binding 请求；同一段代码也在日本 Mac 以相同 `STUN_IP` 运行，作为直接 UDP 出口基线。它按事务 ID 和标准 XOR-MAPPED-ADDRESS 解码服务端观察到的公网 IPv4；单次超时 8 秒，无无限重试。

```sh
python3 - <<'PY'
import os
import socket
import struct

host = os.environ['STUN_IP']
socket.inet_pton(socket.AF_INET, host)
cookie = 0x2112A442
transaction = os.urandom(12)
request = struct.pack('!HHI12s', 0x0001, 0, cookie, transaction)
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
    udp.settimeout(8)
    udp.connect((host, 19302))
    udp.send(request)
    response = udp.recv(4096)
if len(response) < 20:
    raise SystemExit('Invalid STUN response')
kind, length, magic, received = struct.unpack('!HHI12s', response[:20])
if (kind, magic, received) != (0x0101, cookie, transaction) or len(response) < 20 + length:
    raise SystemExit('STUN transaction did not match')
offset = 20
while offset + 4 <= 20 + length:
    attribute, size = struct.unpack('!HH', response[offset:offset + 4])
    value = response[offset + 4:offset + 4 + size]
    if attribute in (0x0020, 0x0001) and len(value) == 8 and value[1] == 1:
        address = value[4:8]
        if attribute == 0x0020:
            address = bytes(a ^ b for a, b in zip(address, cookie.to_bytes(4, 'big')))
        print('UDP observed IPv4=' + socket.inet_ntop(socket.AF_INET, address))
        break
    offset += 4 + ((size + 3) // 4) * 4
else:
    raise SystemExit('No IPv4 mapped address in STUN response')
PY
```

必须同时记录 Surge UDP 会话/请求视图中的目标 `STUN_IP:19302`、UDP 类型、匹配规则、选中 tag 和收发字节；**有 STUN 返回、没有指定策略链证据，不能证明通过该节点**。对比日本直接 UDP 基线和公网归属；若多出口/NAT 导致不同，保留不确定性并调查。撤销本次 STUN 精确规则；按先前状态恢复临时开启的 Enhanced Mode。iOS 或缺少 Enhanced Mode 操作权限时，该程序不具备本机代理路由条件，标记 UDP NOT RUN/BLOCKED，并安排具备条件的操作人执行真实 UDP 应用和 Surge 会话取证。

### 无人登录冷启动与通电恢复窗口

安装器要求 FileVault 明确 Off；On/未知时 BLOCKED，不能自动关闭，也不启用自动登录或保存解锁凭据。先看 `fdesetup status`、`pmset -g cap`、`pmset -g custom` 和安装报告：

| 电源能力 | 可作的结论 |
| --- | --- |
| 支持 autorestartatconnect | 安装器设为 1；接通 AC 开机仍须真实断电/接电验收 |
| 仅支持 autorestart | 安装器设为 1；只承诺配置掉电恢复，不能据此承诺正常关机后接电开机 |
| 两者均不支持 | 自动电源恢复未验证；通电开机目标 BLOCKED，需现场确定其他方案 |

支持时安装器设 AC sleep=0，保存修改前和每次修改后的观察值；失败/未知读回需按报告恢复或调查。此策略不保证笔记本合盖运行。

当前安装/会话启动可先验证；冷启动仅在现场负责人明确授权的窗口执行，先安排独立管理通道、现场观察人和失败恢复。按实际支持的能力测试关机/断电再接 AC（或仅掉电恢复），**整个验收期间所有用户保持未登录**。现场记录开机/登录状态、时间、电源动作；由上海操作人确认全部交付 tag 注册，并逐个重复 HTTPS、仅日本可见 DNS marker、UDP STUN 和策略链/出口取证。必要的系统诊断由已授权的非 GUI 管理通道读取。若先登录才检查，不能证明无人登录恢复；不得为完成测试自动登录。

无授权窗口时保持 PENDING，不自行重启/断电。只有掉电恢复能力时，把通电开机目标标为 BLOCKED/未满足，并单独记录可做的测试。`RunAtLoad`、daemon 已安装、当前 running、Compose restart 或 health 不能代替这一现场证据。

每个 tag 结果至少为：`tag | registration | HTTPS | Japan-only DNS marker | UDP + selected-policy evidence | observed egress/family | time | evidence/owner`。整机另记文件权限、镜像身份、system daemon/PID、专用 socket、当前启动、FileVault/电源能力与原值、所有用户未登录的冷启动证据。未操作的一侧 NOT RUN，条件缺失 BLOCKED，无窗口 PENDING，部分成功 PARTIAL。此文不声称任何现场步骤已完成。
