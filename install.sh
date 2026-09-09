#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="agu-board-v2"
APP_DIR="${APP_DIR:-/opt/$APP_NAME}"
SERVICE_NAME="agu-board-v2"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "错误：请使用 sudo/root 运行：sudo bash install.sh"
  exit 1
fi

install_os_deps() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip rsync curl ca-certificates gzip
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 python3-pip rsync curl ca-certificates gzip
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 python3-pip rsync curl ca-certificates gzip
  else
    echo "未识别的 Linux 包管理器，请手动安装 Python 3.9+、venv、rsync、curl、gzip。" >&2
  fi
}

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1 || ! "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
  echo "==> 安装系统依赖"
  install_os_deps
fi
command -v "$PYTHON_BIN" >/dev/null 2>&1 || { echo "错误：未找到 python3" >&2; exit 1; }
"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 9):
    raise SystemExit("错误：需要 Python 3.9+")
print("==> Python", sys.version.split()[0])
PY

PASSWORD="${BOARD_PASSWORD:-${1:-}}"
SECRET="${BOARD_SECRET:-${2:-}}"
if [[ -z "$PASSWORD" ]]; then
  read -r -s -p "设置看板登录密码：" PASSWORD
  echo
  [[ -n "$PASSWORD" ]] || { echo "错误：密码不能为空" >&2; exit 1; }
fi
if [[ -z "$SECRET" ]]; then
  SECRET="$($PYTHON_BIN -c 'import secrets; print(secrets.token_urlsafe(48))')"
fi

mkdir -p "$APP_DIR" "$APP_DIR/data" "$APP_DIR/logs"
echo "==> 部署程序文件（保留现有数据库和配置）"
rsync -a --delete --exclude '.git/' --exclude '.venv/' --exclude 'agu-board.env' --exclude 'logs/' --exclude 'data/' "$SRC_DIR/" "$APP_DIR/"

if [[ ! -f "$APP_DIR/data/board.db" ]]; then
  if [[ -f "$SRC_DIR/data/board.db.gz" ]]; then
    echo "==> 首次部署：解压并导入历史数据库种子"
    tmp_db="$APP_DIR/data/board.db.tmp"
    gzip -cd "$SRC_DIR/data/board.db.gz" > "$tmp_db"
    chmod 644 "$tmp_db"
    mv "$tmp_db" "$APP_DIR/data/board.db"
  elif [[ -f "$SRC_DIR/data/board.db" ]]; then
    echo "==> 首次部署：导入未压缩历史数据库"
    install -m 644 "$SRC_DIR/data/board.db" "$APP_DIR/data/board.db"
  else
    echo "==> 未发现历史数据库种子，将由程序初始化空数据目录"
  fi
else
  echo "==> 检测到已有数据库，保留：$APP_DIR/data/board.db"
fi

if [[ ! -f "$APP_DIR/agu-board.env" ]]; then
  echo "==> 创建环境配置"
  umask 077
  printf 'BOARD_SECRET=%s\nBOARD_PASSWORD=%s\n' "$SECRET" "$PASSWORD" > "$APP_DIR/agu-board.env"
  chmod 600 "$APP_DIR/agu-board.env"
else
  echo "==> 检测到已有 agu-board.env，保留原密码和密钥"
fi

cd "$APP_DIR"
echo "==> 创建/更新 Python 虚拟环境"
if [[ ! -x .venv/bin/python ]]; then
  "$PYTHON_BIN" -m venv .venv
fi
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q

echo "==> 安装 systemd 服务"
install -m 644 "$APP_DIR/deploy/agu-board.service" "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
systemctl restart "$SERVICE_NAME"

sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo
  echo "================================================"
  echo " 安装成功"
  echo " 服务：$SERVICE_NAME"
  echo " 访问：http://${IP:-服务器IP}:8766/"
  echo " 程序：$APP_DIR"
  echo " 数据库：$APP_DIR/data/board.db"
  echo "================================================"
else
  echo "服务启动失败，最近日志：" >&2
  journalctl -u "$SERVICE_NAME" -n 80 --no-pager >&2 || true
  exit 1
fi
