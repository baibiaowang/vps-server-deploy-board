"""
轻量鉴权：登录态 Cookie（替代原站 ?token=明文密码）

改进点：
  - 密码不再出现在 URL（不再进浏览器历史 / Referer / 访问日志）
  - HttpOnly + SameSite Cookie，前端 JS 读不到
  - 通过环境变量 BOARD_AUTH=1 一键启用；内网自用可保持关闭

生产建议：设置环境变量 BOARD_PASSWORD 与 BOARD_SECRET。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

from fastapi import Cookie, HTTPException, Response, status

from .config import get_password, get_secret

COOKIE_NAME = "board_session"
DEFAULT_TTL = 7 * 24 * 3600      # 登录态 7 天


def auth_enabled() -> bool:
    return os.getenv("BOARD_AUTH", "0") == "1"


# ---------------- Token ----------------
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(ttl: int = DEFAULT_TTL) -> str:
    payload = {"exp": int(time.time()) + ttl}
    raw = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(get_secret(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def verify_token(token: Optional[str]) -> bool:
    if not token or "." not in token:
        return False
    raw, sig = token.rsplit(".", 1)
    expect = hmac.new(get_secret(), raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return False
    try:
        payload = json.loads(_unb64(raw))
        return int(payload.get("exp", 0)) > time.time()
    except Exception:  # noqa: BLE001
        return False


# ---------------- 登录 / 登出 ----------------
def check_password(pwd: str) -> bool:
    import hmac as _h
    return _h.compare_digest(pwd or "", get_password())


def login(response: Response, password: str) -> bool:
    if not check_password(password):
        return False
    response.set_cookie(
        COOKIE_NAME, make_token(),
        httponly=True, samesite="lax", max_age=DEFAULT_TTL,
        path="/", secure=(os.getenv("BOARD_COOKIE_SECURE", "0") == "1"),
    )
    return True


def logout(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


# ---------------- FastAPI 依赖 ----------------
def require_auth(board_session: Optional[str] = Cookie(default=None)) -> None:
    """保护 API；BOARD_AUTH=0 时自动放行（内网自用）"""
    if not auth_enabled():
        return
    if not verify_token(board_session):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="未登录或登录已过期")