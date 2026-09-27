#!/usr/bin/env bash
set -Eeuo pipefail
REPO_URL="${REPO_URL:-https://github.com/baibiaowang/vps-server-deploy-board.git}"
WORK_DIR="/tmp/agu-board-install-$$"
cleanup(){ rm -rf "$WORK_DIR"; }; trap cleanup EXIT
[[ "$(id -u)" -eq 0 ]] || { echo "错误：请使用 root/sudo。" >&2; exit 1; }
command -v git >/dev/null 2>&1 || { apt-get update -y; DEBIAN_FRONTEND=noninteractive apt-get install -y git; }
mkdir -p "$WORK_DIR"; git clone --depth 1 --quiet "$REPO_URL" "$WORK_DIR/repo"; exec bash "$WORK_DIR/repo/install.sh" "$@"
