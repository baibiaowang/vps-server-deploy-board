#!/usr/bin/env bash
set -Eeuo pipefail
APP_NAME="agu-board-v2"
APP_DIR="${APP_DIR:-/opt/$APP_NAME}"
SERVICE_NAME="$APP_NAME"
RUN_USER="agu-board"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="install"
for arg in "$@"; do [[ "$arg" == "--update-only" ]] && MODE="update"; done
[[ "$(id -u)" -eq 0 ]] || { echo "错误：请使用 root/sudo 运行。" >&2; exit 1; }
install_os_deps() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip rsync curl ca-certificates gzip tar sqlite3 git
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 python3-pip rsync curl ca-certificates gzip tar sqlite git
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 python3-pip rsync curl ca-certificates gzip tar sqlite git
  else
    echo "未识别的 Linux 包管理器。" >&2; exit 1
  fi
}
for cmd in python3 rsync curl tar gzip sqlite3; do command -v "$cmd" >/dev/null 2>&1 || { install_os_deps; break; }; done
python3 -m venv --help >/dev/null 2>&1 || install_os_deps
PYTHON_BIN="python3"
if [[ "$MODE" == "update" && -x "$APP_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$APP_DIR/.venv/bin/python"
  echo "==> 更新模式：复用现有 .venv 的 $($PYTHON_BIN --version)"
else
  python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("错误：首次安装需要 Python 3.11+；已有部署更新会复用现有 .venv。")
print("==> Python", sys.version.split()[0])
PY
fi
if ! id -u "$RUN_USER" >/dev/null 2>&1; then useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$RUN_USER"; fi
PASSWORD="${BOARD_PASSWORD:-}"
SECRET="${BOARD_SECRET:-}"
if [[ "$MODE" == "install" && ! -f "$APP_DIR/agu-board.env" ]]; then
  if [[ -z "$PASSWORD" ]]; then read -r -s -p "设置看板登录密码：" PASSWORD; echo; fi
  [[ -n "$PASSWORD" ]] || { echo "错误：BOARD_PASSWORD 不能为空" >&2; exit 1; }
  [[ -n "$SECRET" ]] || SECRET="$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")"
fi
mkdir -p "$APP_DIR/data" "$APP_DIR/logs"; chmod 755 "$APP_DIR"
rsync -a --delete   --exclude ".git/" --exclude ".github/" --exclude ".venv/" --exclude "agu-board.env"   --exclude "logs/" --exclude "data/" --exclude "*.tar.gz" --exclude "manifest.json"   --exclude "install-from-github.sh" --exclude "update.sh"   "$SRC_DIR/" "$APP_DIR/"
if [[ ! -f "$APP_DIR/agu-board.env" ]]; then
  [[ -n "$PASSWORD" ]] || PASSWORD="$(python3 -c "import secrets; print(secrets.token_urlsafe(18))")"
  [[ -n "$SECRET" ]] || SECRET="$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")"
  umask 077
  cat > "$APP_DIR/agu-board.env" <<ENV
BOARD_AUTH=1
BOARD_PASSWORD=$PASSWORD
BOARD_SECRET=$SECRET
BOARD_COOKIE_SECURE=${BOARD_COOKIE_SECURE:-0}
BOARD_ENABLE_DOCS=${BOARD_ENABLE_DOCS:-0}
ENV
else
  echo "==> 保留现有 agu-board.env"
fi
cd "$APP_DIR"
[[ -x .venv/bin/python ]] || python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -q
.venv/bin/python tools/build_assets.py >/dev/null
chown -R root:root "$APP_DIR"
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR/data" "$APP_DIR/logs" "$APP_DIR/.venv"
chown "$RUN_USER":"$RUN_USER" "$APP_DIR/agu-board.env"; chmod 600 "$APP_DIR/agu-board.env"
install -o root -g root -m 644 "$APP_DIR/deploy/agu-board.service" "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
BOARD_AUTH=1 .venv/bin/python -m app.cli check
.venv/bin/python -m compileall -q app
systemctl restart "$SERVICE_NAME"
sleep 2
systemctl is-active --quiet "$SERVICE_NAME" || { journalctl -u "$SERVICE_NAME" -n 100 --no-pager >&2 || true; exit 1; }
echo "安装/更新成功：$(cat "$APP_DIR/VERSION" 2>/dev/null || echo unknown)"
