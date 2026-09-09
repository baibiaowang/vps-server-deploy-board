#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/agu-board-v2}"
SERVICE_NAME="agu-board-v2"
PACKAGE_NAME="${PACKAGE_NAME:-vps-server-deploy-board-1.0.0.tar.gz}"
PACKAGE_SHA256="${PACKAGE_SHA256:-46e527451fc351ab1c0670037c30fb3abe03f39515cf0a0b186da73427156679}"
REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/baibiaowang/vps-server-deploy-board/main}"
WORK_DIR="$(mktemp -d /tmp/vps-server-deploy-board-update.XXXXXX)"
trap 'rm -rf "$WORK_DIR"' EXIT

[[ $(id -u) -eq 0 ]] || { echo "请使用 sudo/root 运行"; exit 1; }

# 更新前自动备份现有数据库和本地配置。
if [[ -x "$APP_DIR/backup.sh" && -f "$APP_DIR/data/board.db" ]]; then
  "$APP_DIR/backup.sh"
fi

PACKAGE_FILE="$WORK_DIR/$PACKAGE_NAME"
SRC_DIR="$WORK_DIR/src"
mkdir -p "$SRC_DIR"

echo "==> 下载最新发布包"
curl -fL --retry 3 --retry-delay 2 "$REPO_RAW/$PACKAGE_NAME" -o "$PACKAGE_FILE"

echo "$PACKAGE_SHA256  $PACKAGE_FILE" | sha256sum -c -

echo "==> 解压"
tar -xzf "$PACKAGE_FILE" -C "$SRC_DIR"
if [[ -f "$SRC_DIR/install.sh" ]]; then
  PACKAGE_ROOT="$SRC_DIR"
elif [[ -f "$SRC_DIR/vps-server-deploy-board/install.sh" ]]; then
  PACKAGE_ROOT="$SRC_DIR/vps-server-deploy-board"
else
  echo "错误：发布包中未找到 install.sh。" >&2
  exit 1
fi

# install.sh 已设计为：程序文件更新时排除 data/、logs/ 和 agu-board.env，
# 因此服务器本地数据库、密码和密钥不会被发布包覆盖。
sudo bash "$PACKAGE_ROOT/install.sh"

echo "更新完成。"
