"""数据源适配器

选择优先级：环境变量 BOARD_FETCHER > rules.yaml fetch.source > mock

  mock    内置假数据，离线演示 / CI 用，零网络依赖
  cninfo  真实抓取（巨潮公告 + 东财/腾讯K线 + 腾讯市值），见 cninfo.py
"""
from __future__ import annotations

import logging
import os

from .base import BaseFetcher, RawAnnouncement, RawKline  # noqa: F401

log = logging.getLogger("fetchers")


def get_fetcher() -> BaseFetcher:
    name = os.getenv("BOARD_FETCHER", "").lower()
    if not name:
        try:
            from ..config import rules_config
            name = str((rules_config().get("fetch") or {}).get("source", "")).lower()
        except Exception:  # noqa: BLE001
            name = ""
    if not name:
        name = "eastmoney"
        log.warning("未配置 fetch.source，默认使用 eastmoney（真实数据）")

    if name == "mock":
        from .mock import MockFetcher
        try:
            from ..config import rules_config
            m = (rules_config().get("fetch") or {}).get("mock") or {}
            return MockFetcher(
                seed=int(m.get("seed", 20260828)),
                pool_size=int(m.get("pool_size", 800)),
                ann_per_day=int(m.get("ann_per_day", 150)),
                kline_days=int(m.get("kline_days", 120)),
            )
        except Exception:  # noqa: BLE001
            return MockFetcher()

    if name == "cninfo":
        from .cninfo import CninfoFetcher
        return CninfoFetcher()

    if name in ("eastmoney", "em"):
        from .eastmoney import EastmoneyFetcher
        return EastmoneyFetcher()

    raise ValueError("未知的数据源: %s (可选: mock / cninfo / eastmoney)" % name)