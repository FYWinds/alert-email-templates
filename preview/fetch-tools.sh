#!/usr/bin/env bash
# Copyright (c) 2026 FYWinds
#
# Download the pinned amtool and argocd CLIs and build proxmox-render (needs
# cargo) into preview/bin/. The preview renders with these, so it matches what
# the servers send.
set -euo pipefail

ALERTMANAGER_VERSION="${ALERTMANAGER_VERSION:-0.34.1}"
ARGOCD_VERSION="${ARGOCD_VERSION:-3.5.5}"

bin="$(cd "$(dirname "$0")" && pwd)/bin"
mkdir -p "$bin"

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$(uname -m)" in
  x86_64 | amd64) arch=amd64 ;;
  arm64 | aarch64) arch=arm64 ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

am="alertmanager-${ALERTMANAGER_VERSION}.${os}-${arch}"
curl -fsSL "https://github.com/prometheus/alertmanager/releases/download/v${ALERTMANAGER_VERSION}/${am}.tar.gz" \
  | tar -xz -C "$bin" --strip-components=1 "${am}/amtool"

curl -fsSL -o "$bin/argocd" \
  "https://github.com/argoproj/argo-cd/releases/download/v${ARGOCD_VERSION}/argocd-${os}-${arch}"
chmod +x "$bin/argocd" "$bin/amtool"

"$bin/amtool" --version 2>&1 | head -n 1
"$bin/argocd" version --client --short

here="$(cd "$(dirname "$0")" && pwd)"
cargo build --quiet --release --locked --manifest-path "$here/proxmox-render/Cargo.toml"
cp "$here/proxmox-render/target/release/proxmox-render" "$bin/"
echo "proxmox-render built"
