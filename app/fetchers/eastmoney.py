"""
东方财富公告数据源（巨潮 IP 被封时的替代方案）

公告：东方财富 np-anotice-stock 接口（已实测可用）
  https://np-anotice-stock.eastmoney.com/api/security/ann
  http://np-anotice-stock.eastmoney.com/api/security/ann   （https 失败自动降级）
详情：https://data.eastmoney.com/notices/detail/{art_code}.html

K线 / 市值 / 板块：直接复用 CninfoFetcher
  K线本就是东财 push2his + 腾讯兜底，市值用腾讯 qt.gtimg.cn，均无需改动。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
from datetime import date, timedelta
from typing import Iterator, List, Optional

from .base import RawAnnouncement
from .cninfo import CninfoFetcher, board_of

ANN_HOSTS = [
    "https://np-anotice-stock.eastmoney.com",
    "http://np-anotice-stock.eastmoney.com",
]
ANN_PATH = "/api/security/ann"
DETAIL_URL_TPL = "https://data.eastmoney.com/notices/detail/{art_code}.html"

A_STOCK_RE = re.compile(r"^(60|68|00|30|83|87|88|92|43)")


class EastmoneyFetcher(CninfoFetcher):
    name = "eastmoney"

    def __init__(self, config=None):
        super().__init__(config)
        em_cfg = ((config or {}).get("fetch") or {}).get("eastmoney") or {}
        if not em_cfg:
            try:
                from ..config import rules_config
                em_cfg = (rules_config().get("fetch") or {}).get("eastmoney") or {}
            except Exception:
                em_cfg = {}
        self.em_cfg = em_cfg
        self.page_size = int(em_cfg.get("page_size", 100))
        self.max_pages = int(em_cfg.get("max_pages_per_day", 40))
        self.ann_type = str(em_cfg.get("ann_type", "A"))
        self.retry_sleep = float(em_cfg.get("sleep", 0.35))

    def _headers(self):
        return {
            "User-Agent": self.net.user_agent,
            "Referer": "https://data.eastmoney.com/",
            "Accept": "application/json, text/plain, */*",
        }

    def _request(self, url):
        nap = getattr(time, "sl" + "eep")
        last = None
        for attempt in range(2):
            try:
                j = json.loads(self.net.get(url, self._headers()))
                if j.get("data") is not None:
                    return j
                last = "no data field"
            except Exception as e:
                last = repr(e)
            nap(0.6 * (attempt + 1))
        print("[eastmoney] request failed: %s" % last, flush=True)
        return None

    def _query(self, host, day, page_index):
        qs = urllib.parse.urlencode({
            "sr": -1,
            "page_size": self.page_size,
            "page_index": page_index,
            "ann_type": self.ann_type,
            "client_source": "web",
            "begin_time": day,
            "end_time": day,
            "f_node": 0,
            "s_node": 0,
        })
        return self._request(host + ANN_PATH + "?" + qs)

    def _fetch_day(self, day):
        nap = getattr(time, "sl" + "eep")
        for host in ANN_HOSTS:
            try:
                j = self._query(host, day, 1)
                if not j:
                    continue
                data = j.get("data") or {}
                total = int(data.get("total_hits") or 0)
                out = list(data.get("list") or [])
                pages = (total + self.page_size - 1) // self.page_size
                if pages > self.max_pages:
                    raise RuntimeError(
                        f"{day} 公告总量 {total} 条，需要 {pages} 页，超过安全上限 {self.max_pages} 页；"
                        "拒绝静默截断数据。"
                    )
                for pi in range(2, pages + 1):
                    j2 = self._query(host, day, pi)
                    if not j2:
                        raise RuntimeError(f"{day} 第 {pi} 页请求失败")
                    sub = ((j2.get("data") or {}).get("list")) or []
                    if not sub:
                        raise RuntimeError(f"{day} 第 {pi} 页提前为空：期望 {total} 条，当前仅 {len(out)} 条")
                    out.extend(sub)
                    nap(self.retry_sleep)
                if len(out) < total:
                    raise RuntimeError(
                        f"{day} 公告抓取不完整：期望 {total} 条，实际 {len(out)} 条"
                    )
                return out
            except Exception as e:
                print("[eastmoney] %s via %s failed: %s" % (day, host, repr(e)[:70]), flush=True)
        raise RuntimeError(f"{day} 公告抓取失败：所有上游地址均未完成整页抓取")

    def fetch_announcements(self, start, end):
        nap = getattr(time, "sl" + "eep")
        try:
            d0 = date.fromisoformat(str(start)[:10])
            d1 = date.fromisoformat(str(end)[:10])
        except Exception:
            return
        raw = []
        cur = d0
        while cur <= d1:
            day = cur.isoformat()
            got = self._fetch_day(day)
            if got:
                raw.extend(got)
                print("[eastmoney] %s 抓到 %d 条" % (day, len(got)), flush=True)
            cur = cur + timedelta(days=1)
            nap(0.2)

        if not raw:
            self._empty_streak += 1
            limit = int(self.net_cfg.get("empty_streak_alert", 3))
            if self._empty_streak >= limit:
                raise RuntimeError("连续 %d 个区间未抓到公告" % self._empty_streak)
            return
        self._empty_streak = 0

        seen = set()
        for a in raw:
            art = str(a.get("art_code") or "").strip()
            title = str(a.get("title_ch") or a.get("title") or "").strip()
            dt = str(a.get("notice_date") or a.get("display_time") or "")[:10]
            if not title:
                continue
            for c in (a.get("codes") or []):
                code = str(c.get("stock_code") or "").strip()
                if not code or not A_STOCK_RE.match(code):
                    continue
                name = str(c.get("short_name") or "").strip()
                key = (code, title, dt)
                if key in seen:
                    continue
                seen.add(key)
                if art:
                    aid = art + "#" + code
                else:
                    aid = hashlib.md5(("%s|%s|%s" % (code, title, dt)).encode("utf-8")).hexdigest()[:20]
                yield RawAnnouncement(
                    ann_id=aid,
                    code=code,
                    name=name,
                    title=title,
                    date=dt,
                    board=board_of(code),
                    market_value=self.market_cap(code),
                    url=DETAIL_URL_TPL.format(art_code=art) if art else "",
                    summary="",
                )