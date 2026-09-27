"""
数据源契约层（Adapter 接口）

★ 这是接入你现有源码的关键接口 ★
你原来的抓取脚本只要包一层适配器，实现下面三个方法即可接入新架构，
无需改动服务层、存储层、前端的任何代码。

对接示例（详见 README）：
    class MyCninfoFetcher(BaseFetcher):
        name = "cninfo"
        def fetch_announcements(self, start, end):
            for row in your_old_crawler(start, end):   # 你原来的函数
                yield RawAnnouncement(
                    ann_id=row["id"], code=row["code"], name=row["name"],
                    title=row["title"], date=row["date"], ...
                )
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator, Optional


@dataclass
class RawAnnouncement:
    """公告原始结构（数据源 → 系统的统一中间格式）"""
    ann_id: str                      # 唯一 ID，用于幂等去重
    code: str                        # 股票代码
    name: str                        # 股票简称
    title: str                       # 公告标题
    date: str                        # YYYY-MM-DD
    board: str = ""                  # 板块：主板/创业板/科创板/北交所/其他
    market_value: float = 0.0        # 市值（亿元）
    url: str = ""
    summary: str = ""


@dataclass
class RawKline:
    """K线原始结构"""
    code: str
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    change_pct: float = 0.0


class BaseFetcher(ABC):
    """所有数据源的抽象基类"""

    name: str = "base"

    @abstractmethod
    def fetch_announcements(self, start: str, end: str) -> Iterator[RawAnnouncement]:
        """按日期区间抓取公告（start/end 均为 YYYY-MM-DD，含端点）"""
        raise NotImplementedError

    @abstractmethod
    def fetch_klines(self, code: str, start: str, end: str) -> Iterator[RawKline]:
        """抓取单只股票的 K 线（按需调用，不再全量打包）"""
        raise NotImplementedError

    def close(self) -> None:
        """释放资源（可选实现）"""
        pass