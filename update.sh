#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="${APP_DIR:-/opt/agu-board-v2}"
SERVICE_NAME="agu-board-v2"
BRANCH="${BRANCH:-main}"

[[ $(id -u) -eq 0 ]] || { echo "请使用 sudo/root 运行"; exit 1; }
[[ -d "$APP_DIR/.git" ]] || { echo "错误：$APP_DIR 不是 GitHub 克隆目录。请重新运行一键安装。" >&2; exit 1; }

systemctl stop "$SERVICE_NAME" || true
cd "$APP_DIR"
git fetch origin "$BRANCH"
git reset --hard "origin/$BRANCH"

# data/board.db、agu-board.env、logs 不被覆盖。
PYTHON_BIN="${PYTHON_BIN:-python3}"
[[ -x .venv/bin/python ]] || "$PYTHON_BIN" -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q

install -m 644 deploy/agu-board.service "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl start "$SERVICE_NAME"
systemctl is-active --quiet "$SERVICE_NAME"
echo "更新完成，当前版本：$(git rev-parse --short HEAD)"
