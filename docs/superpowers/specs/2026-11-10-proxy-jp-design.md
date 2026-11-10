# proxy-jp 部署设计

日期：2026-11-10  
状态：用户已选择 Snell v6 + TLS、frp 隧道，并要求一个日本端容器按需连接任意数量 VPS，在 Compose 中用带 tag 的数组配置，在 Surge 手动选择。

## 需求与数据流

上海 Surge → VPS 入口 → frp TLS 反向隧道 → 日本 Mac Docker → 目标网站。网站连接由日本端建立，使用日本网络出口。日本 Mac 主动连接各 VPS，不要求公网 IP 或入站映射。落地点 Mac 默认 Apple Silicon，自动检测物理架构（含 Rosetta 场景）并支持 Intel。使用命令行安装 Docker CLI、Compose 与专用 Lima Docker 引擎；系统启动后、任何用户登录前自动运行，不依赖自动登录。2026-11-10 用户最新要求覆盖先前的登录后启动方案。

日本端一个容器按需连接多个 VPS（例如 1、3、10 台），Compose 的 VPS_LIST 环境项填写 JSON 数组，每项格式为 {"tag":"香港A","connection":"proxyjp://..."}。数组长度按实际配置决定，不固定三个槽位。tag 必须唯一，允许 Unicode 字母、数字、点、横线和下划线，禁止配置分隔符、路径和控制字符。每台 VPS 有独立令牌、代理密码和 TLS 信任材料，互不要求共享身份文件。每台 VPS 输出自己的 Surge 节点；用户将节点加入 select 策略组后手动选择，不增加自动测速切换功能。

## 协议

入口使用 Snell v6 + ShadowTLS v3；TLS 指 Surge 官方支持的 ShadowTLS 包裹层，不使用已经不适用于现代 Snell 的 obfs=tls 参数。Snell 负责代理数据加密，ShadowTLS 提供真实 TLS 握手外观和额外认证。默认握手主机 www.cloudflare.com:443，可在 VPS 初始化时修改，无需拥有该域名或签发证书。

使用 sing-box 1.14.4 实现 ShadowTLS 和 Snell。Snell 服务端和客户端均显式使用 version=6、mode=default，保持加密与流量整形；PSK 为 12–255 字节的随机凭证。UDP 使用 UDP over TCP，Snell v6 无 QUIC Proxy Mode，无需额外 UDP 端口。实际固定版本二进制已通过 v6 入站/出站配置检查、三中继隔离端到端 TCP/UDP 验证及已授权公网中继 HTTPS 验证；真实 Surge/日本现场仍待验收。Surge 要求支持 Snell v6 的版本，官方手册列明 Mac 6.7.0+、iOS 5.20.0+；接收方应记录实际构建版本。

隧道使用 frp 0.72.0，TCP 传输、强制 TLS。日本端验证 VPS 证书，使用独立随机令牌认证。证书可由初始化程序创建私有 CA 签发。默认端口：TCP 8443 给 Surge，TCP 7000 给 frpc，均可调整。VPS 只允许注册指定代理端口。

VPS 只运行 frps，透明转发至日本端；入口的 ShadowTLS 与 Snell 处理、目标域名解析、目标连接都在日本容器内进行。交给代理的域名在日本解析，Surge 自身用于分流或入口寻址的 DNS 仍由其配置决定。

## 多 VPS 的独立性

每台 VPS 对应一个 frpc 进程和独立的本地 ShadowTLS/Snell 入站组合，监听日本容器的 loopback，不发布日本端端口。每条隧道仅转发其对应入口。frpc 使用持续重连模式；VPS 一直不可达也不导致容器退出。某个 frpc 异常退出时，只重启该进程；共享 sing-box 异常退出时，停止所有子进程并让容器重启。

停止时转发信号、回收子进程。日志输出标准输出，Docker 限制日志体积。健康检查输出每条链路的连接状态，至少一条隧道在线才标记容器健康；不能以进程存在冒充隧道在线。全部离线时容器仍持续重连，不用健康检查结果触发无意义的整体重启。

日本端离线时所有对应节点失败，不在服务端设置 VPS 直连回退。用户现有 Surge 策略组是否选择其他节点由其规则决定。

## 配置生成和镜像

提供 Python 标准库命令行，依赖本机 openssl，或通过项目镜像运行初始化。每台 VPS 初始化输入公网 IP/域名、可选端口、可选节点名与握手域名，输出：服务端证书和 frps 配置、可直接启动的 compose.vps.yaml、一条日本端连接串 connection.txt、日本端示例环境文件、Surge 节点片段及手动选择示例。拒绝覆盖已有目录，不静默轮换凭证。输出目录和敏感文件权限分别 0700/0600。

连接串形如 proxyjp:// 后跟 URL-safe Base64 的版本化 JSON，仅用于封装而非加密。包含 VPS 地址/端口、节点名、令牌、公开 CA、Snell PSK、ShadowTLS 密码和握手主机；不包含 CA 私钥或 VPS 服务器私钥。解析和验证出错时不回显原始连接串或秘密。

一个 Docker 镜像包含 frps、frpc、sing-box 和 Python 管理程序；不同入口命令启动 VPS 或日本角色。固定依赖版本，支持 linux/arm64 和 linux/amd64，运行时不下载软件，不要求 privileged、TUN、宿主机网络或 Docker socket。服务配置在 tmpfs 内生成。日本端默认非 root，VPS 使用生成文件所有者对应的 UID/GID 读取私钥。

## macOS 启动和离线分发

提供日本端 Compose、环境文件模板、命令行 bootstrap 和安装/卸载脚本。默认安装到当前账户的 ~/.proxy-jp，使用专用 Lima Docker VM 和系统 LaunchDaemon，在登录前启动。保存工具绝对路径、专用 Unix socket 和独立 Docker CLI 配置，不切换现有全局 context。dry-run 只检查并输出计划，不创建安装文件或更改系统。具体要求见下文“最新开机要求”。

系统服务不依赖自动登录。安装器检测 FileVault，不能自行绕过磁盘解锁；检测并配置机器支持的通电/断电恢复和 AC 空闲睡眠选项，记录原值。仓库地址 192.168.50.2 是局域网地址，远端部署不能依赖能 clone 该地址。提供按架构构建导出的业务镜像离线包；首次引擎安装仍需网络获取工具和 Linux VM 基础镜像。镜像与生成的秘密不提交 Git。

## 验证与交付边界

使用真实 frp / sing-box 配置校验。Python 测试覆盖生成/解析、无效端口、重复入口、秘密权限、防覆盖、证书、子进程退出与关闭；只为真实风险编写测试，不测试文档文本。

端到端测试在隔离 Docker 网络运行三台 VPS（另有十条目配置生成测试）、一个日本端和测试客户端：目标服务仅在日本出口网络存在。验证三台 VPS 的 TCP/UDP、域名由日本端解析、错误凭证拒绝、停一台 VPS 不影响另两台、恢复后自动重连、日本容器退出后节点不可用。验证实际镜像启动和健康状态。

用户已授权实际 VPS 部署和中继测试；日本 Mac 通过 Skill 交接给现场人员。日本公网出口、上海真实 Surge、速度和无人登录的断电/冷启动恢复仍需现场实测。本地模拟不能冒充实际设备联调。

## 官方依据

- https://manual.nssurge.com/policies/tls.html
- https://manual.nssurge.com/policies/snell.html
- https://kb.nssurge.com/surge-knowledge-base/zh/release-notes/snell
- https://sing-box.sagernet.org/configuration/inbound/snell/
- https://sing-box.sagernet.org/configuration/inbound/shadowtls/
- https://gofrp.org/en/docs/features/common/network/network-tls/

## 实机部署与日本端交接补充

用户授权的实际 VPS 清单、地址、标签、端口、SSH 方式及部署状态仅保存在忽略的私密目录，不提交任何 Git 仓库或公共镜像。部署仅创建本项目服务和目录，不改现有服务。

用户要求日本 Mac 交接给其他人部署，交付可移植 Skill、可复制的交接提示词和离线包。离线包包含 arm64/amd64 镜像、已填私密配置的 Compose 数组、安装程序、Surge 配置和 SHA256清单；不包含 VPS root SSH私钥、CA私钥或服务端私钥。接收方在日本Mac执行；本次不要求其远程访问资料。实际日本出口与设备重启验收由交接执行方完成并回报。

用户最新协议要求：Snell 使用 v6。此要求覆盖先前 v4 客户端/v5 服务端兼容方案；镜像、生成配置、测试与交接内容必须全部按 v6 交付。

实机部署记录与验收证据单独私下交付，不放入公共文档。实际日本出口、上海 Surge 与 Mac 无登录冷启动仍需现场验收。


## 最新开机要求：无需登录的命令行部署

默认使用 Lima >=2.2 的官方 `limactl autostart enable --condition=boot` 系统 LaunchDaemon（当前稳定版本2.3.0）。专用 `LIMA_HOME`、专用 `proxy-jp` VM 和显式 Docker Unix socket 与用户原有 Docker Desktop/OrbStack/context 隔离。Docker 引擎在 Linux VM 中由 systemd 启用；容器通过 `restart: unless-stopped` 随引擎恢复，无 GUI 应用或用户 LaunchAgent 依赖。安装数据默认放在 `~/.proxy-jp`，Unix socket 路径须在 macOS 长度限制内。

提供一个 shell 入口：检测 macOS、物理 CPU、Homebrew、可执行 Python3、Lima、Docker CLI 与 Compose；缺少时通过官方 Homebrew 命令行流程安装，Apple Silicon 使用原生 arm64 工具链。安装需要首次管理员权限及联网下载工具和 Linux VM 基础镜像，业务镜像从交付离线包只加载对应的一个架构。已有工具可复用，已有 VM/同名安装不得覆盖；不切换全局 Docker context、不影响其他工作负载。

安装器检测 FileVault、硬件型号、电源能力及现状。FileVault 启用/状态不明时，不能保证无人解锁的冷启动，必须明确 BLOCKED；不得自行关闭加密或保存账户密码。支持 `autorestartatconnect` 的机器配置通电启动；仅支持 `autorestart` 时只能声明断电恢复后启动，二者不得混同。按设备支持配置接通电源时不自动睡眠，记录原值以便恢复；不修改 NVRAM，不承诺合盖笔记本持续运行。电源能力不满足时记录实际限制。操作系统服务配置不代替现场断电冷启动验证：须在现场授权窗口内保持所有用户未登录，验证 VM、Docker、全部 tag 注册与上海流量，分别记录证据。

参考：[Lima 系统启动](https://lima-vm.io/docs/usage/autostart/)、[Lima Docker](https://lima-vm.io/docs/examples/containers/docker/)、[Apple 通电开机支持范围](https://support.apple.com/en-gb/125517)、[Apple FileVault](https://support.apple.com/en-mn/guide/mac-help/-mh11785/mac)。


## GitHub 与镜像公开分发

用户追加镜像仓库 https://github.com/oudbiao/proxy-residential.git，原 Git 同步保留。公共代码、文档、CI 和通用部署参考不得包含实际 VPS 清单/地址/tag/端口/SSH/凭据/日志；已有未发布历史中的真实样例也须从将发布的历史排除。具体现场 Skill 和配置放在忽略目录单独编辑供用户复制，不上传 GitHub、原 Git、Actions artifact 或镜像。

提供 GHCR linux/arm64+linux/amd64 镜像，接收方使用不可变 digest 和正常 docker 命令拉取。安装器保留离线模式，并新增 --image ghcr.io/oudbiao/proxy-residential@sha256:<digest> 在线模式，在专用引擎创建后只拉取当前物理架构，验证镜像平台及 RepoDigest。GitHub Actions 使用仓库 GITHUB_TOKEN 发布通用镜像；发布成功和匿名可拉取性必须实测，不能从 CI 文件或公开源码仓库推断。首次 GHCR 包可能默认私有；目标是不向日本部署人员分发维护方的 GitHub 凭据。
