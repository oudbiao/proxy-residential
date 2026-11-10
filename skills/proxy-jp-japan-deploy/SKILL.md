---
name: proxy-jp-japan-deploy
description: Use when deploying or accepting a supplied proxy-jp Japan configuration on a fresh or existing Mac, including unattended or FileVault manual-unlock boot, immutable images, offline deliveries, or Shanghai Surge acceptance.
---

# Deploy a supplied Japan configuration

Work from the source/package root. Read [README](../../README.md) for acquisition and installation, then the relevant [operations](../../docs/operations.md) sections for inventory, diagnosis and actual traffic recipes. These paths resolve from this Skill; package-root paths are `README.md` and `docs/operations.md`. Deploy the supplied architecture: Snell v6 **mode=default**, ShadowTLS3 and certificate-verified frp TLS; Japan runs a dedicated Docker VM, Shanghai runs Surge Mac 6.7.0+ / iOS 5.20.0+. Japan needs no Surge app or VPS root SSH keys.

## Inputs and fresh-host path

Obtain the actual private Compose, optional env file, all supplied tagged connections and Surge configuration through the trusted handoff. Public source is `https://github.com/oudbiao/proxy-residential.git`; if Git/CLT is missing, clone can trigger Apple's CLT installation or use a prepared source archive. Do not assume Python, Homebrew or Docker exists. A private Skill/prompt, if provided, lives outside Git and supplies actual paths/identity; this generic reference contains no deployment inventory.

Primary online input is the **real published** `ghcr.io/oudbiao/proxy-residential@sha256:` reference with exactly 64 lowercase hex digits, identical to the private Compose image. Require the delivery's actual reference; do not invent a digest or substitute a mutable tag. Source publication alone does not prove GHCR public availability. Online mode needs no manifest or archive. From the root, as the normal target user:

```sh
bash scripts/install-macos.sh --image "$INSTALL_IMAGE" \
  --compose /absolute/private/compose.japan.yaml
```

Set `INSTALL_IMAGE` from the trusted delivery first; add `--env-file /absolute/private/japan.env` when supplied. Use the handoff's existing authorization for the target Mac's CLI dependencies, dedicated VM/system daemon and supported AC power settings; clarify only if that scope is absent. Initial bootstrap uses official Homebrew/CLI and may need internet, CLT download and admin authentication; do not prefix the whole command with sudo. It installs native Homebrew Python, Lima, Docker CLI and standalone Compose, then performs private configuration preflight before VM mutation. Physical Apple Silicon is ARM even in a Rosetta shell (`hw.optional.arm64`), with `/opt/homebrew`; Intel uses `/usr/local`. Require virtualization, ARM macOS13.5+ / Intel macOS15.5+ and Lima>=2.2.

For the offline fallback, verify the external zip checksum then `shasum -a 256 -c SHA256SUMS` at package root and run `bash scripts/install-macos.sh --compose "$PWD/compose.japan.yaml"`. Its manifest defaults beside Compose; explicit `--manifest` and `--image` are mutually exclusive. Installer selects only the native archive, verifies SHA256 and Linux architecture, and accepts the manifest's verified legacy config or OCI image identity. Application images are offline; first host/VM bootstrap still needs network.

`--dry-run` is read-only: missing tools are reported without installing, and online registry verification is deferred. The [profile inventory](../../docs/operations.md#validated-profile-inventory) uses standalone Compose and native Python without an engine; run it once tools exist, not as a prerequisite on a fresh machine. Capture configuration privately; `read_profiles` takes the service environment dict. Compare every supplied tag with the private inventory/manifest. Do not expose expanded Compose, URI, env, tokens or private keys in reports.

## Runtime isolation and boot constraints

Preserve existing Desktop/OrbStack context and workloads. The installer intentionally creates an independent rootful Lima VM with no host mounts and a boot **system LaunchDaemon**, not a GUI-login runtime. Do not switch Docker contexts or use the old engine to load/start the Japan service. Investigate an existing `~/.proxy-jp`, same-name VM/daemon or partial failed install before changing it; installer refuses overwrite. Keep recoverable files/report on failure.

Default `--startup-mode unattended` requires FileVault clearly Off. When the handoff specifies retaining FileVault with manual unlock, add `--startup-mode after-unlock` to the installation command (online or offline). This permits clearly On or Off; unknown is BLOCKED in both modes. With FileVault On, an authorized person must unlock the disk after each cold boot/power loss before macOS and the system daemon can recover Docker/proxy. Keep encryption enabled; never enable auto-login or store unlock secrets. Check `startup_mode`, `filevault`, `manual_unlock_required` and `cold_boot_acceptance` in the report. An after-unlock installation is not proof of unattended cold boot.

`pmset` capability determines the power claim: supported `autorestartatconnect` means configured AC-connect startup; `autorestart` alone means only failure recovery; neither means automatic recovery unverified. These power settings cannot unlock FileVault. Installer saves original/requested/observed AC values and sets supported sleep=0. Unsupported AC-connect startup cannot satisfy a power-connect requirement. Do not promise laptop lid-closed operation.

Installed private directory is `~/.proxy-jp` (or actual `--install-dir`): wrappers/directories 0700, private configs/report 0600. All Mac Docker operations use its wrappers, including temporary acceptance containers:

```sh
"$HOME/.proxy-jp/bin/docker" info
"$HOME/.proxy-jp/bin/compose" ps --all
"$HOME/.proxy-jp/bin/compose" exec -T japan python3 -m proxy_jp health
"$HOME/.proxy-jp/bin/compose" logs --tail 100 japan
launchctl print system/io.lima-vm.daemon.proxy-jp
```

`bin/docker` fixes dedicated `docker-config` and `lima/proxy-jp/sock/docker.sock`, clears inherited context/host/TLS variables; `bin/compose` fixes installed config/env and project. Verify `installation-report.json`, system daemon running/PID and containers after daemon takeover. `bin/uninstall` disables owned boot registration while retaining private config, VM and power settings; power restoration is a separate owner decision using saved originals. Custom install paths must be short enough for the socket limit.

## Registration and actual acceptance

Preserve every supplied tag, including unavailable nodes. Health `partial` exits 0 and Docker healthy includes partial; require JSON status and exact per-tag registration. One failed link should not stop functioning siblings. Diagnose tag-specific frp connectivity/credentials and Japan DNS/handshake reachability; recipient deployment does not imply server administration permission.

Use the runnable [onsite recipes](../../docs/operations.md#onsite-traffic-acceptance): discover the Shanghai Surge HTTP listener, test HTTPS, use the temporary Japan-Docker-only hostname/marker for remote DNS, and send independent UDP STUN under verified Surge UDP routing. Import native Surge lines/select group privately into its existing profile and manually select every supplied tag. Record policy-chain evidence and compare Japan direct vs Shanghai proxied egress in the same address family. Curl/CONNECT is TCP evidence only. Registration/health is not actual egress proof. Keep changes and cleanup scoped to the temporary fixture/rules.

Cold boot requires a separately authorized onsite interruption window with recovery access. For unattended acceptance, test with **all users still logged out** and no disk unlock. For FileVault On after-unlock acceptance, record the authorized manual disk unlock, then confirm the system daemon, Docker, all registrations and Shanghai HTTPS/DNS/UDP recover without manually starting the VM/container. Report unattended cold boot as requiring manual unlock, not PASS. Daemon installation/current-session restart is not cold-boot evidence; explicit Docker stop changes unless-stopped semantics and needs explicit up. Without a window keep PENDING. Without Shanghai access provide the exact operations recipes/owner and keep those fields NOT RUN/BLOCKED.

Report sanitized time/location, native CPU, image identity, dedicated engine/socket, existing context preservation, permissions, system daemon/PID, FileVault/power capability and originals, every tag's registration and real data path, observed egress/family, evidence/owner, and cold-boot status. Keep PASS/PARTIAL/FAIL/BLOCKED/PENDING/NOT RUN per item. Never fill actual Japan/Shanghai or hardware acceptance from simulated local tests.
