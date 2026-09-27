#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="${APP_DIR:-/opt/agu-board-v2}"
BACKUP_DIR="${BACKUP_DIR:-/opt/agu-board-backups}"
SERVICE_NAME="agu-board-v2"
TS="$(date +%Y%m%d-%H%M%S)"
DB="$APP_DIR/data/board.db"
OUT="$BACKUP_DIR/agu-board-data-$TS.tar.gz"
mkdir -p "$BACKUP_DIR"
[[ -f "$DB" ]] || { echo "错误：未找到数据库。" >&2; exit 1; }
command -v sqlite3 >/dev/null 2>&1 || { echo "错误：需要 sqlite3。" >&2; exit 1; }
WAS_ACTIVE=0
if systemctl is-active --quiet "$SERVICE_NAME"; then WAS_ACTIVE=1; systemctl stop "$SERVICE_NAME"; fi
restore(){ [[ "$WAS_ACTIVE" -eq 1 ]] && systemctl start "$SERVICE_NAME" || true; }
trap restore EXIT
check="$(sqlite3 "$DB" 'PRAGMA wal_checkpoint(TRUNCATE); PRAGMA integrity_check;')"
[[ "$(printf "%s\n" "$check" | tail -n 1)" == "ok" ]] || { printf "%s\n" "$check" >&2; exit 1; }
tar -C "$APP_DIR" -czf "$OUT" data/board.db agu-board.env VERSION 2>/dev/null || tar -C "$APP_DIR" -czf "$OUT" data/board.db agu-board.env
ln -sfn "$OUT" "$BACKUP_DIR/latest.tar.gz"
echo "备份完成：$OUT"
