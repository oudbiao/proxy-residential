# proxy-jp：多个 VPS 中继，共用日本出口

日本 Mac 主动连接任意数量的 VPS。上海 Surge 手动选择一个 VPS，流量经 **Snell v6（mode=default）+ ShadowTLS v3 → VPS 的 frp TCP 转发 → 日本 Mac → 目标**。TCP 和 UDP 均由 Snell 承载；VPS 只需开放两个 TCP 端口。日本端无需入站端口或公网 IP。

运行组件固定为 frp 0.72.0、sing-box 1.14.4、Python 3.13 Alpine；提供 linux/arm64（Apple Silicon）和 linux/amd64（Intel Mac / 常见 VPS）镜像。Surge 需要 Mac **6.7.0+** 或 iOS **5.20.0+**，节点两端使用 Snell v6、mode=default，ShadowTLS 使用 v3。当前隔离测试使用真实 sing-box 客户端；真实 Surge 设备、日本网络、上海访问及收件人 Mac 重启仍需现场验收。

## 日本 Mac：在线安装（主要入口）

源码目标为 [oudbiao/proxy-residential](https://github.com/oudbiao/proxy-residential)。发布方须先完成公开源码与 GHCR 发布及匿名拉取验证，私下交付准确的不可变镜像引用、已填好全部带 tag 条目的私密 Compose、可选 env 文件及 Surge 配置。本说明不包含实际发布摘要或任何现场部署记录。

全新 Apple Silicon Mac 不必预装 Python、Homebrew 或 Docker。先获取源码：

```sh
git clone https://github.com/oudbiao/proxy-residential.git proxy-residential
cd proxy-residential
```

首次运行 Git 可能触发 Apple Command Line Tools 安装；尚无 Git/CLT 时也可由交付方提供已准备的源码压缩包，解压后进入根目录。首次安装需要联网和管理员认证（CLT、官方 Homebrew、原生 Python/Lima/Docker CLI/Compose、Lima 客体系统及 Docker）。使用普通用户运行以下**一个安装命令**，不要对整个命令加 sudo：

```sh
# INSTALL_IMAGE 必须先设为发布方私下提供的完整、真实引用。
bash scripts/install-macos.sh --image "$INSTALL_IMAGE" \
  --compose /absolute/private/compose.japan.yaml
```

`INSTALL_IMAGE` 必须严格为 `ghcr.io/oudbiao/proxy-residential@sha256:` 加 64 位小写十六进制摘要，且与私密 Compose 的 `services.japan.image` 完全一致；不接受可变标签。此模式无需 manifest 或离线镜像包。外部 env 配置增加 `--env-file /absolute/private/japan.env`。安装器校验原生 Linux 架构和精确 RepoDigests，再以 `--pull never --no-build` 启动。

安装器用物理 CPU 识别 Apple Silicon（含 Rosetta shell），使用 `/opt/homebrew` 原生 ARM 工具；Intel 使用 `/usr/local` 和 amd64。要求硬件虚拟化、ARM macOS 13.5+ 或 Intel macOS 15.5+、Lima >=2.2。支持的官方 VZ `docker-rootful` 模板创建专用 VM；已有 Docker Desktop/OrbStack 及当前 context、其他工作负载保留。独立 Docker socket/config 和系统 LaunchDaemon 可在所有用户仍未图形登录时启动，无需自动登录。工具依据：[Homebrew 安装](https://docs.brew.sh/Installation)、[Lima autostart](https://lima-vm.io/docs/config/autostart/)。

默认 `--startup-mode unattended` 要求 FileVault 明确为 Off；On/未知时安装器报告 BLOCKED，绝不自动关闭。需要保留 FileVault 时，明确选用人工解锁后的启动方式：

```sh
bash scripts/install-macos.sh --image "$INSTALL_IMAGE" \
  --compose /absolute/private/compose.japan.yaml --startup-mode after-unlock
```

`after-unlock` 允许 FileVault 明确 On 或 Off；状态未知仍 BLOCKED。FileVault 开启时，每次冷启动/断电恢复后先由授权人员解锁磁盘，系统启动后由同一个 Lima 系统服务自动启动 Docker 和代理。安装器不保存解锁口令、不启用自动登录、不修改 FileVault；不能把这种方式报告为完全无人解锁的冷启动。报告分别记录 `startup_mode`、`filevault`、`manual_unlock_required` 和对应的待验收状态。

`--dry-run` 只读，不下载依赖、不建目录/VM、不改电源或启动项；缺依赖时会直接报告，在线镜像拉取/摘要校验留到实际安装。已有安装或同名 VM/daemon 冲突应先检查，不能覆盖。

## 离线镜像回退与构建

维护方在有 Docker、Compose、Python3、OpenSSL 的构建机上执行（构建需要网络）：

```sh
mkdir -p dist
scripts/build-images.sh --platform linux/arm64 --output dist/proxy-jp-arm64.tar.gz
scripts/build-images.sh --platform linux/amd64 --output dist/proxy-jp-amd64.tar.gz
```

必要时增加 `--no-build-proxy` 清除仅本次构建的宿主回环代理。输出拒绝覆盖；两个平台同标签，逐个平台导出。用 [交接提示词](HANDOFF-PROMPT.md) 的 builder 命令生成含代码、两种镜像、私密 Compose、manifest 和校验和的包。接收人验证 zip 外部校验及目录内 `shasum -a 256 -c SHA256SUMS`，然后在包根目录执行：

```sh
bash scripts/install-macos.sh --compose "$PWD/compose.japan.yaml"
```

manifest 默认位于 Compose 旁，亦可显式 `--manifest /absolute/private/manifest.json`；与 `--image` 互斥。安装器只加载物理 CPU 对应的一份镜像，校验归档 SHA256、Linux 架构和 `accepted_image_ids`（legacy config 或已校验 OCI manifest 摘要）。离线回退只消除应用镜像下载；首次 Homebrew/Lima/客体系统 bootstrap 仍需网络，不是完全断网安装。日本 Mac 不需要 VPS root SSH 私钥。

## 为每台 VPS 生成独立配置

以下节点名称、域名和默认端口均为假设示例，不是部署记录。在可信管理机执行；将示例域名替换为 VPS 公网 IP 或实际域名。每个输出目录必须尚不存在。

```bash
umask 077
python3 -m proxy_jp init --host vps-a.example.com --name JP-A --output state/vps-a
python3 -m proxy_jp init --host vps-b.example.com --name JP-B --output state/vps-b
python3 -m proxy_jp init --host vps-c.example.com --name JP-C --output state/vps-c
```

默认 VPS 监听 `7000/tcp`（frp 控制及工作连接）和 `8443/tcp`（Surge 入口），可用 `--tunnel-port` / `--proxy-port` 修改。VPS 防火墙和云安全组均需允许相应 TCP 入站；日本端需能出站连接各 VPS 隧道端口、DNS、目标网络以及 ShadowTLS 握手站的 443。上海需能连接各 VPS 入口端口。所有节点最终出口都依赖日本 Mac 在线。

将每台 VPS 对应目录单独复制到服务器（保留权限）；服务器加载其 CPU 对应镜像，然后：

```bash
cd /opt/proxy-jp/vps-a
docker compose -f compose.vps.yaml config --quiet
docker compose -f compose.vps.yaml up -d
docker compose -f compose.vps.yaml exec vps python -m proxy_jp health
```

生成的 Compose user 与生成目录的 UID/GID 对应。跨机器复制后，保证该 UID 能读取挂载目录的 0700/0600 文件；可在目标服务器以部署账户重新生成，或将整个目录所有权与 Compose 的 user 一并调整。不要放宽私钥为全员可读。生成目录中的 `ca.key` 属于管理端私钥，可在安全备份后从 VPS 删除；运行需要 `server.pem`、`server.key`、`frps.json` 和 Compose。

## 维护方：生成日本端带标签连接（非接收人前置步骤）

`connection.txt` 是完整的 `proxyjp://...` URI，携带私有 CA、frp token、Snell PSK 和 ShadowTLS 密码。`connection.json` 是可直接加入数组的条目。只使用一个环境变量 `VPS_LIST`，其值为任意数量条目的 JSON 数组；tag 决定日本端状态输出名称，需唯一、可含中文、字母数字及 `._-`。

编辑 `compose.japan.yaml`，将 `VPS_LIST: ${VPS_LIST:-[]}` 替换为实际的字面量块：

```yaml
    environment:
      VPS_LIST: |-
        [
          {"tag":"JP-A", "connection":"proxyjp://此处粘贴A的完整URI"},
          {"tag":"JP-B", "connection":"proxyjp://此处粘贴B的完整URI"},
          {"tag":"JP-C", "connection":"proxyjp://此处粘贴C的完整URI"}
        ]
```

也可保留模板，用脚本合并本地生成的条目为 `japan.env`，无需手工转义 URI：

```bash
python3 - <<'PY'
import json
from pathlib import Path
items = [json.loads(Path('state/vps-' + x + '/connection.json').read_text()) for x in ('a','b','c')]
path = Path('japan.env')
with path.open('x') as stream:
    path.chmod(0o600)
    stream.write("VPS_LIST='" + json.dumps(items, ensure_ascii=False) + "'\n")
PY
```

此为维护方生成私密文件的示例；接收人应使用已交付配置和上述安装命令，增加 `--env-file /absolute/private/japan.env`。在线模式需把私密 Compose 的 image 设为准确交付引用。内联配置可直接使用安装入口。数组条目可任意增减，每个隧道端点和 tag 必须唯一，配置保持 0600；不要输出展开后的 Compose。安装前/后清点配置的可执行命令见 [节点清单](docs/operations.md#validated-profile-inventory)。

## Surge 手动选择

将每台 VPS 目录中的 `surge-proxy.conf` 内容合并进 Surge 配置的 `[Proxy]`。示意如下，实际凭据以生成文件为准：

```ini
[Proxy]
JP-A = snell, vps-a.example.com, 8443, psk=实际A密钥, version=6, mode=default, shadow-tls-password=实际A密码, shadow-tls-sni=www.cloudflare.com, shadow-tls-version=3
JP-B = snell, vps-b.example.com, 8443, psk=实际B密钥, version=6, mode=default, shadow-tls-password=实际B密码, shadow-tls-sni=www.cloudflare.com, shadow-tls-version=3
JP-C = snell, vps-c.example.com, 8443, psk=实际C密钥, version=6, mode=default, shadow-tls-password=实际C密码, shadow-tls-sni=www.cloudflare.com, shadow-tls-version=3

[Proxy Group]
日本出口 = select, JP-A, JP-B, JP-C

[Rule]
FINAL,日本出口
```

在 Surge 中手动选择节点；可将此策略组接入已有规则，不必替换现有全部规则。`proxyjp://` 是本项目的日本端连接格式；Surge 使用生成的原生节点行。目标域名应交由代理端解析，现场需用实际 Surge DNS/规则配置验收远端解析。

## 安装后的管理与无人登录启动验收

默认安装目录为 `~/.proxy-jp`，不依赖原始 checkout。日本端所有 Docker 操作用专用 wrapper：

```sh
"$HOME/.proxy-jp/bin/docker" info
"$HOME/.proxy-jp/bin/compose" ps --all
"$HOME/.proxy-jp/bin/compose" exec -T japan python3 -m proxy_jp health
launchctl print system/io.lima-vm.daemon.proxy-jp
```

`bin/docker` 固定专用 socket `~/.proxy-jp/lima/proxy-jp/sock/docker.sock` 和 `docker-config`，清除继承的 Docker context/host/TLS 变量；`bin/compose` 固定项目名及安装的私密 Compose/env。目录/wrapper 为 0700，私密配置和 `installation-report.json` 为 0600。自定义 `--install-dir` 时同步替换所有路径；Unix socket 总路径超过 100 字节会拒绝。

报告保存原始、请求和实际观察到的 AC 电源设置及每个 tag 状态。支持 `autorestartatconnect` 才能配置通电开机；只有 `autorestart` 时仅表示掉电恢复，不能承诺关机后接通电源开机；两者不支持时自动恢复未验证。支持时设置 AC `sleep=0`。不承诺笔记本合盖持续运行。卸载启动项使用 `"$HOME/.proxy-jp/bin/uninstall"`；保留凭据、VM 数据及电源设置，按保存的原值和现场决定另行恢复电源策略。

系统服务安装成功仅证明当前会话运行。实际冷启动需在现场负责人授权窗口，从上海验证全部节点注册和 HTTPS/DNS/UDP。`unattended` 验收要求**所有用户未登录且无需解锁**；FileVault 开启的 `after-unlock` 则记录人工解锁及后续自动恢复，不宣称无人解锁启动。无窗口保持 PENDING。FileVault、供电能力、独立远程管理和恢复安排见 [运维验收](docs/operations.md)。

## 验证

以下为开发/CI 构建机验证，使用其隔离测试引擎；接收人无需在现有 Desktop 引擎重跑，现场按运维文档专用 wrapper 验收。

```bash
scripts/check.sh
python3 tests/e2e.py
```

`check.sh` 包含单元测试、shell/Compose 检查以及镜像内实际 sing-box/frp 生成配置校验；Mac 上安装器测试还执行 plist 原生 lint。端到端测试仅新建带随机前缀的隔离容器/网络，无发布端口，无真实 VPS 操作，结束时清理。它用三台 frps、一台双网络日本端、一个真实 Snell6/ShadowTLS3 消费端、私有 TLS1.3 握手站和独立 DNS/HTTP/UDP 目标验证 TCP、UDP、日本出口源地址、远端 DNS、错误密码、单中继故障/恢复、日本端停机和 frp CA/token 拒绝。可设置 `PROXY_JP_TEST_IMAGE=sha256:...`；脚本启动时解析并固定镜像 ID。

本地 health 是隧道状态，不能替代实际访问验证。现场还必须通过 Surge 分别选每个节点核对日本公网出口、DNS、UDP；完成日本 Mac 重启恢复和上海网络访问验收后才能确认实际交付成功。

## 以后增加荷兰 Mac 出口

可以沿用现有镜像与命令：日本和荷兰各自运行独立客户端，Surge 按国家前缀命名的节点/手动 select 组选择出口。同一 VPS 可承载两个独立的 frps Compose 项目，但每个出口需要独立目录、端口、证书及凭据；不能让两台 Mac 使用同一条连接串抢占同一个 frps 代理端口。

以下全部是假设示例，未部署：日本使用默认 TCP8443/7000；荷兰另选空闲 TCP9443/8000，可在可信管理机生成独立配置：

```sh
umask 077
python3 -m proxy_jp init --host vps-a.example.com --name 荷兰A \
  --proxy-port 9443 --tunnel-port 8000 --output state/vps-nl-a
```

这里的地址和 TCP9443/8000 仅为示例，尚未分配或部署。将该独立目录按前述 UID/GID 与私钥权限要求复制到同一 VPS，确认端口及防火墙配置后，用独立项目名启动：

```sh
cd /opt/proxy-jp/vps-nl-a
docker compose --project-name proxy-nl -f compose.vps.yaml config --quiet
docker compose --project-name proxy-nl -f compose.vps.yaml up -d
```

各 Mac 的 `VPS_LIST` 只填属于其自身出口的连接条目；荷兰 Mac 使用新目录生成的连接，日本 Mac 保留原有日本条目。多台 VPS 时，每台都为荷兰生成自己的独立配置。合并原生 Surge 节点时保持 tag 唯一，例如 `日本A`、`荷兰A`，再按国家建立手动选择组；分别实测两地出口。通用 Skill/提示词只按私密交付的日本连接数组部署；此 FAQ 不执行荷兰部署。
