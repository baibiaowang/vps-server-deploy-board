#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/agu-board-v2}"
BACKUP_DIR="${BACKUP_DIR:-/opt/agu-board-backups}"
REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/baibiaowang/vps-server-deploy-board/main}"
WORK_DIR="${WORK_DIR:-/tmp/vps-server-deploy-board-update-$$}"
SERVICE_NAME="agu-board-v2"
cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT
[[ "$(id -u)" -eq 0 ]] || { echo "错误：请使用 sudo/root 运行" >&2; exit 1; }
[[ -d "$APP_DIR" ]] || { echo "错误：$APP_DIR 不存在，请先执行一键安装。" >&2; exit 1; }
for cmd in curl tar sha256sum python3; do command -v "$cmd" >/dev/null 2>&1 || { echo "错误：缺少 $cmd" >&2; exit 1; }; done
mkdir -p "$WORK_DIR" "$BACKUP_DIR"
manifest="$WORK_DIR/manifest.json"
curl -fLsS "$REPO_RAW/manifest.json" -o "$manifest"
mapfile -t INFO < <(python3 - "$manifest" <<'PY'
import json, sys
m=json.load(open(sys.argv[1], encoding='utf-8'))
print(m['package_name'])
print(m['sha256'])
PY
)
PACKAGE_NAME="${INFO[0]}"
PACKAGE_SHA256="${INFO[1]}"
PACKAGE_FILE="$WORK_DIR/$PACKAGE_NAME"
SRC_DIR="$WORK_DIR/src"
mkdir -p "$SRC_DIR"

if systemctl is-active --quiet "$SERVICE_NAME"; then systemctl stop "$SERVICE_NAME"; fi
"$APP_DIR/backup.sh"

curl -fL --retry 3 --retry-delay 2 "$REPO_RAW/$PACKAGE_NAME" -o "$PACKAGE_FILE"
echo "$PACKAGE_SHA256  $PACKAGE_FILE" | sha256sum -c -
tar -xzf "$PACKAGE_FILE" -C "$SRC_DIR"
if [[ -f "$SRC_DIR/install.sh" ]]; then PACKAGE_ROOT="$SRC_DIR"; elif [[ -f "$SRC_DIR/vps-server-deploy-board/install.sh" ]]; then PACKAGE_ROOT="$SRC_DIR/vps-server-deploy-board"; else echo "错误：发布包中未找到 install.sh" >&2; exit 1; fi
"$PACKAGE_ROOT/install.sh" --update-only
systemctl start "$SERVICE_NAME"
systemctl is-active --quiet "$SERVICE_NAME"
echo "更新完成：$(cat "$APP_DIR/VERSION" 2>/dev/null || echo unknown)"
