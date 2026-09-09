#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="agu-board-v2"
APP_DIR="${APP_DIR:-/opt/$APP_NAME}"
SERVICE_NAME="$APP_NAME"
RUN_USER="agu-board"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="install"
for arg in "$@"; do [[ "$arg" == "--update-only" ]] && MODE="update"; done

[[ "$(id -u)" -eq 0 ]] || { echo "错误：请使用 sudo/root 运行：sudo bash install.sh" >&2; exit 1; }

install_os_deps() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip rsync curl ca-certificates gzip tar sqlite3
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 python3-pip rsync curl ca-certificates gzip tar sqlite
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 python3-pip rsync curl ca-certificates gzip tar sqlite
  else
    echo "未识别的 Linux 包管理器，请手动安装 Python 3.11+、venv、rsync、curl、tar、gzip、sqlite3。" >&2
    exit 1
  fi
}
for cmd in python3 rsync curl tar gzip sqlite3; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "==> 安装基础依赖"; install_os_deps; break; }
done
python3 -m venv --help >/dev/null 2>&1 || install_os_deps
python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("错误：本版本需要 Python 3.11+")
print("==> Python", sys.version.split()[0])
PY

if ! id -u "$RUN_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$RUN_USER"
fi

PASSWORD="${BOARD_PASSWORD:-}"
SECRET="${BOARD_SECRET:-}"
if [[ "$MODE" == "install" && ! -f "$APP_DIR/agu-board.env" ]]; then
  if [[ -z "$PASSWORD" ]]; then
    read -r -s -p "设置看板登录密码：" PASSWORD
    echo
  fi
  [[ -n "$PASSWORD" ]] || { echo "错误：BOARD_PASSWORD 不能为空" >&2; exit 1; }
  [[ -n "$SECRET" ]] || SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
fi

mkdir -p "$APP_DIR" "$APP_DIR/data" "$APP_DIR/logs"
chmod 755 "$APP_DIR"
rsync -a --delete --exclude '.git/' --exclude '.venv/' --exclude 'agu-board.env' --exclude 'logs/' --exclude 'data/' --exclude '*.bak' "$SRC_DIR/" "$APP_DIR/"

if [[ ! -f "$APP_DIR/data/board.db" && -f "$SRC_DIR/data/board.db" ]]; then
  echo "==> 首次部署：导入历史数据库种子"
  install -o "$RUN_USER" -g "$RUN_USER" -m 664 "$SRC_DIR/data/board.db" "$APP_DIR/data/board.db"
elif [[ -f "$APP_DIR/data/board.db" ]]; then
  echo "==> 检测到已有数据库，保持不变：$APP_DIR/data/board.db"
fi

if [[ ! -f "$APP_DIR/agu-board.env" ]]; then
  [[ -n "$PASSWORD" ]] || PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
  [[ -n "$SECRET" ]] || SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  umask 077
  cat > "$APP_DIR/agu-board.env" <<ENV
BOARD_AUTH=1
BOARD_PASSWORD=$PASSWORD
BOARD_SECRET=$SECRET
BOARD_COOKIE_SECURE=${BOARD_COOKIE_SECURE:-0}
BOARD_ENABLE_DOCS=0
ENV
else
  echo "==> 保留现有 agu-board.env"
fi

cd "$APP_DIR"
[[ -x .venv/bin/python ]] || python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q

chown -R root:root "$APP_DIR"
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR/data" "$APP_DIR/logs" "$APP_DIR/.venv"
chown "$RUN_USER":"$RUN_USER" "$APP_DIR/agu-board.env"
chmod 600 "$APP_DIR/agu-board.env"

install -o root -g root -m 644 "$APP_DIR/deploy/agu-board.service" "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
BOARD_AUTH=1 .venv/bin/python -m app.cli check
systemctl restart "$SERVICE_NAME"
sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo
  echo "================================================"
  echo " 安装/更新成功"
  echo " 服务：$SERVICE_NAME"
  echo " 访问：http://${IP:-服务器IP}:8766/"
  echo " 数据库：$APP_DIR/data/board.db"
  echo "================================================"
else
  echo "服务启动失败，最近日志：" >&2
  journalctl -u "$SERVICE_NAME" -n 100 --no-pager >&2 || true
  exit 1
fi
