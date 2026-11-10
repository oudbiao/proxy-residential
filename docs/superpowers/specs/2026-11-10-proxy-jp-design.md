# proxy-jp 部署设计

日期：2026-11-10  
状态：用户已选择 Snell + TLS、frp 隧道，并要求一个日本端容器同时连接三台 VPS，在 Compose 中配置，在 Surge 手动选择。

## 需求与数据流

上海 Surge → VPS 入口 → frp TLS 反向隧道 → 日本 Mac Docker → 目标网站。网站连接由日本端建立，使用日本网络出口。日本 Mac 主动连接各 VPS，不要求公网 IP 或入站映射。日本 Mac 可以自动登录专用账户，登录后启动 Docker 和容器。

日本端一个容器同时连接三台 VPS，Compose 明确提供 VPS_1、VPS_2、VPS_3 环境项，每项填写对应 VPS 生成的一条连接串。允许只填一台或两台，运行时按编号读取非空的 VPS_N 项。每台 VPS 有独立令牌、代理密码和 TLS 信任材料，互不要求共享身份文件。每台 VPS 输出自己的 Surge 节点；用户将节点加入 select 策略组后手动选择，不增加自动测速切换功能。

## 协议

入口使用 Snell v4 + ShadowTLS v3；TLS 指 Surge 官方支持的 ShadowTLS 包裹层，不使用已经不适用于现代 Snell 的 obfs=tls 参数。Snell 负责代理数据加密，ShadowTLS 提供真实 TLS 握手外观和额外认证。默认握手主机 www.cloudflare.com:443，可在 VPS 初始化时修改，无需拥有该域名或签发证书。

使用 sing-box 1.14.4 实现 ShadowTLS 和 Snell。该版本的 Snell 入站 version=5 支持 v4 的线协议；Surge 输出显式使用 version=4，所有 UDP 使用 UDP over TCP，不启用需要另外开放 UDP 端口的 Snell v5 QUIC Proxy Mode。此兼容关系应以官方文档及真实链路测试验证。

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

提供日本端 Compose、环境文件模板、安装/卸载 LaunchAgent 脚本。安装到当前账户独立目录，保存 Docker CLI 的绝对路径，启动所选 Docker Desktop 或 OrbStack，等待引擎就绪，再启动 Compose。支持当前 Docker context；不默认切换用户其他工作负载的 context。支持安装配置检查模式，在不注册系统任务时生成并检查 plist。

自动登录、FileVault 解锁、电源和休眠由设备管理员配置；脚本不修改这些系统策略。仓库地址 192.168.50.2 是局域网地址，远端部署不能依赖能 clone 该地址。提供按架构构建并导出 Docker 镜像的脚本，以及离线复制、docker load 和部署说明。镜像与生成的秘密不提交 Git。

## 验证与交付边界

使用真实 frp / sing-box 配置校验。Python 测试覆盖生成/解析、无效端口、重复入口、秘密权限、防覆盖、证书、子进程退出与关闭；只为真实风险编写测试，不测试文档文本。

端到端测试在隔离 Docker 网络运行三台 VPS、一个日本端和测试客户端：目标服务仅在日本出口网络存在。验证三台 VPS 的 TCP/UDP、域名由日本端解析、错误凭证拒绝、停一台 VPS 不影响另两台、恢复后自动重连、日本容器退出后节点不可用。验证实际镜像启动和健康状态。

实际 VPS、日本 Mac、上海 Surge 的出口 IP、线路速度和设备重启恢复，需要设备地址/访问方式后实测。本地模拟不能冒充实际设备联调。

## 官方依据

- https://manual.nssurge.com/policies/tls.html
- https://manual.nssurge.com/policies/snell.html
- https://kb.nssurge.com/surge-knowledge-base/zh/release-notes/snell
- https://sing-box.sagernet.org/configuration/inbound/snell/
- https://sing-box.sagernet.org/configuration/inbound/shadowtls/
- https://gofrp.org/en/docs/features/common/network/network-tls/
