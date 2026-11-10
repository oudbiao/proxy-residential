#!/bin/bash
# Native, non-GUI bootstrap for immutable online or offline images; Homebrew/Lima guest
# bootstrap requires Internet, and may require admin authentication/CLT download.
set -euo pipefail

dry_run=0
choose_arch() {
    if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" = 1 ]; then
        image_arch=arm64; native_arch=arm64; brew_prefix=/opt/homebrew
    elif [ "$(uname -m)" = x86_64 ]; then
        image_arch=amd64; native_arch=x86_64; brew_prefix=/usr/local
    else
        echo 'proxy-jp: unsupported physical architecture' >&2; return 2
    fi
}
brew_run() {
    /usr/bin/arch "-$native_arch" "$brew_prefix/bin/brew" "$@"
}
bootstrap_formulae() {
    local formula
    for formula in python lima docker docker-compose; do
        if ! brew_run list --versions "$formula" >/dev/null 2>&1; then
            if [ "$dry_run" = 1 ]; then
                echo "proxy-jp: missing Homebrew dependency $formula; dry-run performed no installation" >&2
                return 2
            fi
            brew_run install "$formula"
        fi
    done
}
check_filevault() {
    local status
    status="$(fdesetup status 2>/dev/null || true)"
    case "$status" in
        'FileVault is Off.') return 0;;
        'FileVault is On.')
            if [ "$1" = after-unlock ]; then
                echo 'proxy-jp: FileVault stays on; manual disk unlock is required after each cold boot' >&2
                return 0
            fi
            echo 'proxy-jp: BLOCKED: FileVault on prevents unattended cold boot; use --startup-mode after-unlock to retain encryption and require manual unlock.' >&2;;
        *) echo 'proxy-jp: BLOCKED: FileVault status unknown; inspect locally before installation.' >&2;;
    esac
    return 2
}
main() {
    local arg uninstall=0 product major minor setup_file script_dir startup_mode=unattended expect_mode=0
    for arg in "$@"; do
        if [ "$expect_mode" = 1 ]; then startup_mode="$arg"; expect_mode=0; continue; fi
        case "$arg" in
            --dry-run) dry_run=1;;
            --uninstall) uninstall=1;;
            --startup-mode) expect_mode=1;;
            --startup-mode=*) startup_mode="${arg#*=}";;
        esac
    done
    if [ "$expect_mode" = 1 ] || { [ "$startup_mode" != unattended ] && [ "$startup_mode" != after-unlock ]; }; then
        echo 'proxy-jp: --startup-mode must be unattended or after-unlock' >&2; return 2
    fi
    if [ "$(uname -s)" != Darwin ] || [ "$(id -u)" = 0 ]; then
        echo 'proxy-jp: run as a non-root macOS user' >&2; return 2
    fi
    choose_arch
    product="$(sw_vers -productVersion)"
    major="${product%%.*}"; minor="${product#*.}"; minor="${minor%%.*}"
    if { [ "$image_arch" = arm64 ] && { [ "$major" -lt 13 ] || { [ "$major" -eq 13 ] && [ "$minor" -lt 5 ]; }; }; } ||
       { [ "$image_arch" = amd64 ] && { [ "$major" -lt 15 ] || { [ "$major" -eq 15 ] && [ "$minor" -lt 5 ]; }; }; } ||
       [ "$(sysctl -n kern.hv_support 2>/dev/null || true)" != 1 ]; then
        echo 'proxy-jp: VZ requires hardware virtualization, ARM macOS13.5+ or Intel macOS15.5+' >&2; return 2
    fi
    if [ "$uninstall" = 0 ]; then check_filevault "$startup_mode" || return 2; fi
    if [ ! -x "$brew_prefix/bin/brew" ]; then
        if [ "$dry_run" = 1 ] || [ "$uninstall" = 1 ]; then
            echo 'proxy-jp: native Homebrew missing; no dependencies installed' >&2; return 2
        fi
        echo 'proxy-jp: installing official native Homebrew; Internet/admin password or CLT download may be required'
        setup_file="$(mktemp -t proxy-jp-homebrew)"
        /usr/bin/curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh -o "$setup_file" || { rm -f "$setup_file"; return 2; }
        /usr/bin/arch "-$native_arch" /bin/bash "$setup_file" || { rm -f "$setup_file"; return 2; }
        rm -f "$setup_file"
    fi
    if [ "$uninstall" = 0 ]; then bootstrap_formulae; fi
    export PATH="$brew_prefix/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    exec "$brew_prefix/bin/python3" "$script_dir/install-macos.py" \
        --docker-cli "$brew_prefix/bin/docker" --compose-cli "$brew_prefix/bin/docker-compose" \
        --limactl "$brew_prefix/bin/limactl" "$@"
}
if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
