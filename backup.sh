#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR="${APP_DIR:-/opt/agu-board-v2}"
BACKUP_DIR="${BACKUP_DIR:-/opt/agu-board-backups}"
TS="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

[[ -f "$APP_DIR/data/board.db" ]] || { echo "错误：未找到数据库 $APP_DIR/data/board.db" >&2; exit 1; }
tar -C "$APP_DIR" -czf "$BACKUP_DIR/agu-board-data-$TS.tar.gz" data agu-board.env 2>/dev/null || {
  echo "错误：备份失败，请检查 $APP_DIR/data 和 agu-board.env" >&2
  exit 1
}
ln -sfn "$BACKUP_DIR/agu-board-data-$TS.tar.gz" "$BACKUP_DIR/latest.tar.gz"
echo "备份完成：$BACKUP_DIR/agu-board-data-$TS.tar.gz"
