"""
配置加载（rules.yaml + 环境变量）

配置优先级：环境变量 > config/rules.yaml > 默认值
"""
from __future__ import annotations

import os
import threading
from typing import Any, Dict

import yaml

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES_PATH = os.path.join(BASE_DIR, "config", "rules.yaml")

_lock = threading.RLock()
_cache: Dict[str, Any] = {}
_mtime: float = 0.0


def rules_config() -> Dict[str, Any]:
    """加载 rules.yaml（带 mtime 热重载：改文件即时生效，无需重启）"""
    global _cache, _mtime
    with _lock:
        mtime = os.path.getmtime(RULES_PATH) if os.path.exists(RULES_PATH) else 0
        if mtime != _mtime or not _cache:
            try:
                with open(RULES_PATH, "r", encoding="utf-8") as f:
                    _cache = yaml.safe_load(f) or {}
            except Exception:  # noqa: BLE001
                if not _cache:
                    _cache = {}
            _mtime = mtime
        return _cache


def get_schedule_config() -> Dict[str, Any]:
    return (rules_config().get("schedule") or {}) if os.path.exists(RULES_PATH) else {}


# ---------------- 服务与安全 ----------------
def get_password() -> str:
    """
    访问密码。
    原站把明文密码放在 URL 里（?token=xxx），会进浏览器历史、Referer、日志。
    这里改为环境变量注入 + 登录态 Cookie。
    """
    return os.getenv("BOARD_PASSWORD", "AguBoard2026!")


def get_secret() -> bytes:
    """Cookie 签名密钥；生产环境请设置环境变量 BOARD_SECRET"""
    return os.getenv("BOARD_SECRET", "agu-board-dev-secret-change-me").encode()


def get_host() -> str:
    return os.getenv("BOARD_HOST", "0.0.0.0")


def get_port() -> int:
    return int(os.getenv("BOARD_PORT", "8766"))


def get_db_path() -> str:
    return os.path.join(BASE_DIR, "data", "board.db")