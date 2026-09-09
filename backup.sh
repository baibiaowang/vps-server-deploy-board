#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="${APP_DIR:-/opt/agu-board-v2}"
BACKUP_DIR="${BACKUP_DIR:-/opt/agu-board-backups}"
TS="$(date +%Y%m%d-%H%M%S)"
DB="$APP_DIR/data/board.db"
OUT="$BACKUP_DIR/agu-board-data-$TS.tar.gz"
SERVICE_NAME="agu-board-v2"
mkdir -p "$BACKUP_DIR"
[[ -f "$DB" ]] || { echo "错误：未找到数据库 $DB" >&2; exit 1; }
command -v sqlite3 >/dev/null 2>&1 || { echo "错误：需要 sqlite3 才能执行一致性备份。" >&2; exit 1; }

WAS_ACTIVE=0
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$SERVICE_NAME"; then
  WAS_ACTIVE=1
  systemctl stop "$SERVICE_NAME"
fi
restore() { if [[ "$WAS_ACTIVE" -eq 1 ]]; then systemctl start "$SERVICE_NAME" || true; fi; }
trap restore EXIT

check="$(sqlite3 "$DB" 'PRAGMA wal_checkpoint(TRUNCATE); PRAGMA integrity_check;')"
if [[ "$(printf '%s\n' "$check" | tail -n 1)" != "ok" ]]; then
  printf '%s\n' "$check" >&2
  exit 1
fi

tar -C "$APP_DIR" -czf "$OUT" data/board.db agu-board.env VERSION
ln -sfn "$OUT" "$BACKUP_DIR/latest.tar.gz"
echo "备份完成：$OUT"
