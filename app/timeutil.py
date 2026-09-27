"""
时区工具（需求②：海外部署必须显式绑定东八区）

为什么需要这个文件：
  海外 VPS / Docker 镜像默认 TZ=UTC。A股是东八区市场，一旦按 UTC 取"今天"：
    · UTC 时间 00:00-08:00 之间跑更新 → 拿到的"今天"是北京时间的昨天
    · 交易日公告窗口整体错位一天 → 少抓一天、多抓一天
    · cron 按 UTC 触发 → 你以为是 18:30 收盘后跑，实际是北京时间凌晨 2:30
  原站全部用 datetime.date.today()，在 UTC 机器上就是错的。

设计：
  所有需要"当前日期/时间"的地方一律走本模块，不再直接调用 date.today()。
  时区以 rules.yaml 的 network.timezone 为准（默认 Asia/Shanghai），
  且做了降级：万一 tzdata 缺失（极简镜像常见），回落到固定 +08:00 偏移，
  保证不会退化成 UTC。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9（本项目要求 3.11，这里只是兜底）
    ZoneInfo = None  # type: ignore

FALLBACK_TZ = timezone(timedelta(hours=8), name="UTC+8")
_CN_TZ = None
_CACHED_NAME: Optional[str] = None


def _resolve(name: str):
    """解析时区，失败则用固定 +08:00，绝不退化成 UTC"""
    if ZoneInfo is None:
        return FALLBACK_TZ
    try:
        return ZoneInfo(name)
    except Exception:
        # tzdata 缺失时 zoneinfo 会抛 ZoneInfoNotFoundError
        return FALLBACK_TZ


def cn_tz():
    """东八区时区对象（按配置名缓存）"""
    global _CN_TZ, _CACHED_NAME
    name = tz_name()
    if _CN_TZ is None or name != _CACHED_NAME:
        _CN_TZ = _resolve(name)
        _CACHED_NAME = name
    return _CN_TZ


def tz_name() -> str:
    """时区名：rules.yaml network.timezone > 环境变量 TZ > Asia/Shanghai"""
    import os

    try:
        from .config import rules_config

        name = ((rules_config().get("network") or {}).get("timezone"))
        if name:
            return str(name)
    except Exception:
        pass
    env = os.getenv("TZ")
    if env and env not in ("UTC", "Etc/UTC"):
        return env
    return "Asia/Shanghai"


def now_cn() -> datetime:
    """当前东八区时间（tz-aware）"""
    return datetime.now(cn_tz())


def today_cn() -> date:
    """当前东八区日期 ★ 所有"今天"都用它，不要用 date.today()"""
    return now_cn().date()


def now_naive() -> datetime:
    """当前东八区时间（去 tz，用于写入 SQLite DateTime 列）

    SQLite 不存时区，统一写东八区本地时间，读取时按东八区解释，
    避免"存 UTC 读本地"导致的 8 小时偏移。
    """
    return now_cn().replace(tzinfo=None)


def parse_local(dt: Optional[datetime]) -> Optional[datetime]:
    """把库里的 naive datetime 按东八区解释（展示用）"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=cn_tz())
    return dt.astimezone(cn_tz())


def iso_cn(dt: Optional[datetime]) -> Optional[str]:
    """东八区 ISO 字符串（带 +08:00 偏移，前端/日志都能直接用）"""
    d = parse_local(dt)
    return d.isoformat() if d else None


def weekday_range(start: date, end: date) -> list[str]:
    """区间内的周一至周五（ISO 日期字符串列表）

    注意：这不含节假日剔除。因为有春节/国庆这种连续休市，
    所以本函数只在 klines 表取不到真实开市日时作为兜底。
    真实交易日请以行情源（klines 表）为准。
    """
    out: list[str] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def tz_report() -> dict:
    """时区自检信息（挂在 /api/health 上，部署后一眼确认有没有配错）"""
    import time

    n = now_cn()
    return {
        "configured": tz_name(),
        "resolved": str(cn_tz()),
        "utc_offset": n.utcoffset().total_seconds() / 3600 if n.utcoffset() else None,
        "now_cn": n.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "today_cn": today_cn().isoformat(),
        "system_tz": time.tzname[0] if hasattr(time, "tzname") else None,
        # True = 用 date.today() 会算错（服务器 TZ 与业务时区不一致）
        "server_tz_mismatch": (datetime.now().date() != today_cn()),
        "tzdata_ok": ZoneInfo is not None and str(cn_tz()) not in ("UTC+8",),
    }