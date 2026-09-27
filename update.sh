#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="${APP_DIR:-/opt/agu-board-v2}"
BACKUP_DIR="${BACKUP_DIR:-/opt/agu-board-backups}"
REPO_URL="${REPO_URL:-https://github.com/baibiaowang/vps-server-deploy-board.git}"
SERVICE_NAME="agu-board-v2"
WORK_DIR="/tmp/agu-board-update-$$"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOCK_FILE="/run/lock/agu-board-v2-update.lock"
cleanup(){ rm -rf "$WORK_DIR"; }
CODE_BACKUP=""; DB_BACKUP=""
rollback(){ rc=$?; if [[ $rc -ne 0 && -f "$CODE_BACKUP" ]]; then echo "!! 更新失败，自动回滚"; systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true; rm -rf "$WORK_DIR/rollback"; mkdir -p "$WORK_DIR/rollback"; tar -xzf "$CODE_BACKUP" -C "$WORK_DIR/rollback"; rsync -a --delete --exclude "data/" --exclude "logs/" --exclude ".venv/" --exclude "agu-board.env" "$WORK_DIR/rollback/" "$APP_DIR/"; [[ -f "$DB_BACKUP" ]] && cp -f "$DB_BACKUP" "$APP_DIR/data/board.db" || true; systemctl daemon-reload >/dev/null 2>&1 || true; systemctl start "$SERVICE_NAME" >/dev/null 2>&1 || true; fi; cleanup; exit $rc; }; trap rollback EXIT
[[ "$(id -u)" -eq 0 ]] || { echo "错误：请使用 root/sudo。" >&2; exit 1; }
command -v flock >/dev/null 2>&1 || { echo "错误：缺少 flock。" >&2; exit 1; }
command -v git >/dev/null 2>&1 || { apt-get update -y; DEBIAN_FRONTEND=noninteractive apt-get install -y git; }
command -v rsync >/dev/null 2>&1 || { apt-get update -y; DEBIAN_FRONTEND=noninteractive apt-get install -y rsync; }
command -v sqlite3 >/dev/null 2>&1 || { apt-get update -y; DEBIAN_FRONTEND=noninteractive apt-get install -y sqlite3; }
mkdir -p "$BACKUP_DIR"; exec 9>"$LOCK_FILE"; flock -n 9 || { echo "已有更新任务正在执行。" >&2; exit 2; }
mkdir -p "$WORK_DIR"
echo "==> 从 GitHub 获取源码"
git clone --depth 1 --quiet "$REPO_URL" "$WORK_DIR/repo"
[[ -f "$WORK_DIR/repo/app/main.py" && -f "$WORK_DIR/repo/install.sh" ]] || { echo "GitHub 发布内容不完整。" >&2; exit 1; }
NEW_VERSION="$(cat "$WORK_DIR/repo/VERSION" 2>/dev/null || echo unknown)"; echo "==> 目标版本：$NEW_VERSION"
CODE_BACKUP="$BACKUP_DIR/agu-board-code-$STAMP.tar.gz"; DB_BACKUP="$BACKUP_DIR/board-db-$STAMP.db"
tar -C "$APP_DIR" -czf "$CODE_BACKUP" --exclude data --exclude logs --exclude .venv --exclude agu-board.env .
if [[ -f "$APP_DIR/data/board.db" ]]; then
  systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
  check="$(sqlite3 "$APP_DIR/data/board.db" 'PRAGMA wal_checkpoint(TRUNCATE); PRAGMA integrity_check;')"
  [[ "$(printf "%s\n" "$check" | tail -n 1)" == "ok" ]] || { echo "更新前数据库完整性检查失败。" >&2; exit 1; }
  cp -f "$APP_DIR/data/board.db" "$DB_BACKUP"
else
  systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
fi
bash "$WORK_DIR/repo/install.sh" --update-only
systemctl is-active --quiet "$SERVICE_NAME"
for _ in $(seq 1 20); do
  if curl -fsS -o /dev/null "http://127.0.0.1:8766/api/health"; then echo "健康检查通过。当前版本：$(cat "$APP_DIR/VERSION" 2>/dev/null || echo unknown)"; trap - EXIT; cleanup; exit 0; fi
  sleep 1
done
exit 1
