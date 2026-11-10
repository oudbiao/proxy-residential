#!/bin/bash
set -euo pipefail
platform=''
output=''
no_build_proxy=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --platform) [ "$#" -ge 2 ] || exit 2; platform="$2"; shift 2 ;;
        --no-build-proxy) no_build_proxy=1; shift ;;
        --output) [ "$#" -ge 2 ] || exit 2; output="$2"; shift 2 ;;
        *) echo 'Usage: build-images.sh --platform linux/arm64|linux/amd64 --output PATH [--no-build-proxy]' >&2; exit 2 ;;
    esac
done
case "$platform" in linux/arm64|linux/amd64) ;; *) echo 'Select linux/arm64 or linux/amd64' >&2; exit 2 ;; esac
[ -n "$output" ] || { echo '--output is required' >&2; exit 2; }
[ ! -e "$output" ] || { echo 'Refusing to overwrite existing image archive' >&2; exit 2; }
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
build_args=(buildx build --platform "$platform" --load --tag proxy-jp:0.1.0)
if [ -n "${PYTHON_IMAGE:-}" ]; then build_args+=(--build-arg "PYTHON_IMAGE=$PYTHON_IMAGE"); fi
# Opt in when Docker injects a host-loopback proxy unreachable inside builds.
if [ "$no_build_proxy" -eq 1 ]; then
    for proxy_var in HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy; do
        build_args+=(--build-arg "$proxy_var=")
    done
fi
docker "${build_args[@]}" "$repo_dir"
# Reserve output atomically. A failed save never leaves an apparently valid archive.
(set -o noclobber; : > "$output")
trap 'rm -f -- "$output"' EXIT
docker save proxy-jp:0.1.0 | gzip > "$output"
trap - EXIT
