#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="${REPO_URL:-https://github.com/baibiaowang/vps-server-deploy-board.git}"
BRANCH="${BRANCH:-main}"
WORK_DIR="${WORK_DIR:-/tmp/vps-server-deploy-board-install}"

if [[ $(id -u) -eq 0 ]]; then
  echo "请不要直接用 root 执行。使用普通用户运行本脚本，由脚本在需要时调用 sudo。"
  exit 1
fi
command -v sudo >/dev/null 2>&1 || { echo "错误：未找到 sudo，请先安装 sudo。" >&2; exit 1; }

if ! command -v git >/dev/null 2>&1; then
  echo "==> 安装 git"
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y git ca-certificates
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y git ca-certificates
  elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y git ca-certificates
  else
    echo "错误：未识别的包管理器，无法自动安装 git。" >&2; exit 1
  fi
fi

rm -rf "$WORK_DIR"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "==> 从 GitHub 获取 VPS 部署项目"
echo "仓库：$REPO_URL"
echo "分支：$BRANCH"
git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$WORK_DIR"
cd "$WORK_DIR"

sudo bash ./install.sh "$@"
