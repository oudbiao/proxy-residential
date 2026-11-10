# 日本 Mac 部署交接提示词（通用公开模板）

交付方在 Git 外生成私密副本，填写源码/配置实际路径、真实已发布不可变引用、全部带 tag 的连接清单及 Surge 配置位置。不要把私密 Skill、连接 URI、服务器地址或现场部署记录提交公开源码。该模板不代表镜像已发布或现场已验收。

将下面提示词填写后交给日本 Mac 的操作 agent；首次安装授权范围按现场实际确认：

```text
请在这台日本 Mac 部署已有 proxy-jp 方案。
源码/包根目录：<实际绝对路径；或从 https://github.com/oudbiao/proxy-residential.git clone>
私密 Compose：<实际绝对路径>
可选私密 env：<实际路径或无>
在线 INSTALL_IMAGE：<发布方提供的真实 ghcr.io/oudbiao/proxy-residential@sha256:64位小写摘要；离线填无>
私密连接清单和 Surge 配置：<私密交付位置，以实际全部 tags 为准>

先读取 skills/proxy-jp-japan-deploy/SKILL.md、README.md，以及 docs/operations.md 的相应现成命令。方案是 Snell v6 mode=default + ShadowTLS3 + 验证私有 CA 的 frp TLS；不重新设计架构。上海需 Surge Mac 6.7.0+ / iOS 5.20.0+，日本不需 Surge 或 VPS root SSH 密钥。

我授权这台日本 Mac 的首次 CLI 依赖安装、独立 Lima Docker VM、系统 boot LaunchDaemon 与支持的 AC 电源设置。使用普通用户，不对整个安装命令加 sudo；首次需要网络、管理员认证和可能的 CLT 下载。全新机器可无 Homebrew/Python/Docker；Git clone 本身可能触发 CLT，Git 不可用时先取得准备好的源码包。进入根目录，设置真实 INSTALL_IMAGE，再运行一个命令：
bash scripts/install-macos.sh --image "$INSTALL_IMAGE" --compose /absolute/private/compose.japan.yaml
外部 env 时增加 --env-file 的实际绝对路径。Compose image 必须完全相同，不用可变标签、不编造摘要。在线不需 manifest/镜像包；离线验证交付校验后运行 bash scripts/install-macos.sh --compose "$PWD/compose.japan.yaml"，只由安装器加载原生架构对应包。

识别物理 CPU，Rosetta 不能使 Apple Silicon 变成 amd64。保留现有 Desktop/OrbStack、context 和工作负载；安装器创建独立引擎，所有日本 Docker 操作都使用 ~/.proxy-jp/bin/docker 或 bin/compose（自定义 install-dir 时替换）。不要迁移/清理已有工作负载。检查已有目录/VM/daemon，冲突不覆盖。--dry-run 是只读且不安装缺失依赖；registry 校验延后到正式安装。

FileVault 必须明确 Off，否则 BLOCKED；不要自动禁用 FileVault、开自动登录或保存解锁口令。区分 autorestartatconnect 的通电开机能力、仅 autorestart 的掉电恢复和两者不支持；核对报告的原始/实际电源值。不承诺笔记本合盖。当前安装成功不能当作冷启动通过。只有现场负责人另行授权并安排中断窗口后，才在窗口内做关机/断电接电测试；全程保持所有用户未登录，确认系统服务、全部 tag 注册和实际上海流量。无窗口则 PENDING，不自行重启。

检查安装副本权限、installation-report.json、system/io.lima-vm.daemon.proxy-jp 的 running/PID、专用 socket、容器和每个 tag；partial 健康退出0不等于全在线。保留全部条目，某一不可用时保留其他链路并按 tag 排障。不要输出完整 Compose、URI 或凭据。

与上海操作人按 docs/operations.md 实际 HTTPS、仅日本 Docker 网络可解析的 DNS fixture 和独立 UDP STUN 命令逐 tag 验收，保留 Surge 选择策略/会话证据，对比同地址族日本直接出口。缺上海访问则交给其操作人并保留 NOT RUN/BLOCKED；不能以容器模拟代替。最后给出脱敏报告，逐项列镜像/权限/隔离引擎/服务/电源/注册/HTTPS/DNS/UDP/出口/无人登录冷启动的状态、时间、证据和负责人。
```

维护方的离线回退 builder（开发/构建机有 Python、Docker/Compose；输出必须不存在）：

```sh
umask 077
python3 scripts/make-handoff.py --connections /absolute/private/connections.json \
  --image-arm64 dist/proxy-jp-arm64.tar.gz \
  --image-amd64 dist/proxy-jp-amd64.tar.gz \
  --output /absolute/private/japan-handoff
```

输入为任意长度的带 `tag`、`connection` 数组。私密输出目录含完整内联 Compose、两种镜像、Surge 配置、通用源码/Skill/提示词及 `SHA256SUMS`，旁边 zip 和外部 checksum。manifest 保留协议版本、架构、归档 SHA、成熟的 `accepted_image_ids`（legacy config/已校验 OCI manifest 摘要）及标签；声明默认 ARM、system daemon boot 和 pending 无人登录冷启动。镜像身份不等于真实流量或硬件验收。
