#!/usr/bin/env bash
set -Eeuo pipefail

REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/baibiaowang/vps-server-deploy-board/main}"
PACKAGE_NAME="${PACKAGE_NAME:-vps-server-deploy-board-1.0.0.tar.gz}"
PACKAGE_SHA256="${PACKAGE_SHA256:-46e527451fc351ab1c0670037c30fb3abe03f39515cf0a0b186da73427156679}"
WORK_DIR="${WORK_DIR:-/tmp/vps-server-deploy-board-install}"
PACKAGE_FILE="$WORK_DIR/$PACKAGE_NAME"
SRC_DIR="$WORK_DIR/src"

if [[ $(id -u) -eq 0 ]]; then
  echo "请不要直接用 root 执行。使用普通用户运行本脚本，由脚本在需要时调用 sudo。"
  exit 1
fi
command -v sudo >/dev/null 2>&1 || { echo "错误：未找到 sudo。" >&2; exit 1; }

if ! command -v curl >/dev/null 2>&1 || ! command -v tar >/dev/null 2>&1 || ! command -v sha256sum >/dev/null 2>&1; then
  echo "==> 安装基础工具"
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y curl tar coreutils ca-certificates
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y curl tar coreutils ca-certificates
  elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y curl tar coreutils ca-certificates
  else
    echo "错误：未识别的包管理器。" >&2; exit 1
  fi
fi

rm -rf "$WORK_DIR"
mkdir -p "$SRC_DIR"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "==> 从 GitHub 下载部署发布包"
echo "$REPO_RAW/$PACKAGE_NAME"
curl -fL --retry 3 --retry-delay 2 "$REPO_RAW/$PACKAGE_NAME" -o "$PACKAGE_FILE"

if [[ -n "$PACKAGE_SHA256" ]]; then
  echo "$PACKAGE_SHA256  $PACKAGE_FILE" | sha256sum -c -
fi

echo "==> 解压部署包"
tar -xzf "$PACKAGE_FILE" -C "$SRC_DIR"

# 发布包本身带有 ./ 前缀，找到真正的项目根目录。
if [[ -f "$SRC_DIR/install.sh" ]]; then
  PACKAGE_ROOT="$SRC_DIR"
elif [[ -f "$SRC_DIR/vps-server-deploy-board/install.sh" ]]; then
  PACKAGE_ROOT="$SRC_DIR/vps-server-deploy-board"
else
  echo "错误：发布包中未找到 install.sh。" >&2
  exit 1
fi

sudo bash "$PACKAGE_ROOT/install.sh" "$@"
