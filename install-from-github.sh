#!/usr/bin/env bash
set -Eeuo pipefail

REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/baibiaowang/vps-server-deploy-board/main}"
PACKAGE_NAME="${PACKAGE_NAME:-}"
PACKAGE_SHA256="${PACKAGE_SHA256:-}"
WORK_DIR="${WORK_DIR:-/tmp/vps-server-deploy-board-install-$$}"
SRC_DIR="$WORK_DIR/src"

cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT

if [[ "$(id -u)" -eq 0 ]]; then
  SUDO_CMD=()
else
  command -v sudo >/dev/null 2>&1 || { echo "错误：当前用户不是 root，且未找到 sudo。" >&2; exit 1; }
  SUDO_CMD=(sudo)
fi

install_tools() {
  if command -v apt-get >/dev/null 2>&1; then
    "${SUDO_CMD[@]}" apt-get update -y
    "${SUDO_CMD[@]}" DEBIAN_FRONTEND=noninteractive apt-get install -y curl tar coreutils ca-certificates python3
  elif command -v dnf >/dev/null 2>&1; then
    "${SUDO_CMD[@]}" dnf install -y curl tar coreutils ca-certificates python3
  elif command -v yum >/dev/null 2>&1; then
    "${SUDO_CMD[@]}" yum install -y curl tar coreutils ca-certificates python3
  else
    echo "错误：未识别的包管理器，请先安装 curl/tar/coreutils/python3。" >&2
    exit 1
  fi
}

for cmd in curl tar sha256sum python3; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "==> 安装基础工具"; install_tools; break; }
done

mkdir -p "$SRC_DIR"
manifest="$WORK_DIR/manifest.json"
if curl -fLsS "$REPO_RAW/manifest.json" -o "$manifest" 2>/dev/null; then
  mapfile -t MI < <(python3 - "$manifest" <<'PY'
import json, sys
m=json.load(open(sys.argv[1], encoding='utf-8'))
print(m.get('package_name',''))
print(m.get('sha256',''))
PY
)
  [[ -n "$PACKAGE_NAME" ]] || PACKAGE_NAME="${MI[0]:-}"
  [[ -n "$PACKAGE_SHA256" ]] || PACKAGE_SHA256="${MI[1]:-}"
fi
[[ -n "$PACKAGE_NAME" ]] || { echo "错误：无法确定发布包名称。" >&2; exit 1; }
PACKAGE_FILE="$WORK_DIR/$PACKAGE_NAME"

echo "==> 下载 VPS 部署发布包：$PACKAGE_NAME"
curl -fL --retry 3 --retry-delay 2 "$REPO_RAW/$PACKAGE_NAME" -o "$PACKAGE_FILE"
if [[ -n "$PACKAGE_SHA256" ]]; then
  echo "$PACKAGE_SHA256  $PACKAGE_FILE" | sha256sum -c -
fi

echo "==> 解压发布包"
tar -xzf "$PACKAGE_FILE" -C "$SRC_DIR"

if [[ -f "$SRC_DIR/install.sh" ]]; then
  PACKAGE_ROOT="$SRC_DIR"
elif [[ -f "$SRC_DIR/vps-server-deploy-board/install.sh" ]]; then
  PACKAGE_ROOT="$SRC_DIR/vps-server-deploy-board"
else
  echo "错误：发布包中未找到 install.sh。" >&2
  exit 1
fi

"${SUDO_CMD[@]}" bash "$PACKAGE_ROOT/install.sh" "$@"
