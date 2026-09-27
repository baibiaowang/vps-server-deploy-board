"""
巨潮资讯 + 东财/腾讯 真实数据源（需求④：沿用原 scripts 的抓取办法）

来源对照（全部来自你提供的原始文件，未改变抓取口径）：
  公告    scripts/cninfo_fetch.py  → fetch_page / fetch_range / fetch_all
  K线     scripts/gen_dashboard.py → fetch_kline（东财优先，腾讯兜底）
  市值    scripts/gen_dashboard.py → fetch_market_cap（腾讯 qt.gtimg.cn）
  板块    scripts/cninfo_fetch.py  → board_of()

原脚本踩过的三个坑，这里全部保留处理：
  1. 巨潮无视 pageSize，固定每页 30 条        → 按 total 反推页数
  2. 宽日期区间深分页会被服务端截断，首日公告丢失 → split_by_day 按天拆
  3. 并发 > 4 会触发巨潮 IP 限流(403)          → workers 默认 4，沪深串行

新增（原脚本没有，海外/弱网必备）：
  4. 双 scheme 重试（https→http）+ 403 指数退避   [原脚本已有]
  5. 代理支持（rules.yaml network.proxy / HTTPS_PROXY）
  6. 显式超时，杜绝"一个请求卡死整个更新"
  7. 连续 N 个区间 0 条 → 抛明确异常，不再静默写入空数据
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Iterator, List, Optional

from .base import BaseFetcher, RawAnnouncement, RawKline

# ---------------- 常量 ----------------
CNINFO_URL_TPL = "{scheme}://www.cninfo.com.cn/new/hisAnnouncement/query"
PAGE_SIZE = 30          # 巨潮无视此参数，固定 30/页

EASTMONEY_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TENCENT_KLINE = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
TENCENT_QUOTE = "https://qt.gtimg.cn/q="

BJ_PREFIXES = ("83", "87", "88", "43", "92")   # 北交所：东财无数据


# ---------------- 工具 ----------------
def board_of(code: str) -> str:
    """板块识别（原 scripts/cninfo_fetch.py board_of，原样搬）"""
    code = str(code or "").strip()
    if re.match(r"^(688|689)", code):
        return "科创板"
    if re.match(r"^30", code):
        return "创业板"
    if re.match(r"^(60|00)", code):
        return "主板"
    if re.match(r"^(83|87|88|92|43)", code):
        return "北交所"
    return "其他"


def is_st(name: str) -> bool:
    return "ST" in str(name or "").upper()


def secid(code: str) -> str:
    """东财 secid：沪市(60/688/689) 用 1. 前缀，其余用 0.（原 gen_dashboard.py）"""
    if code.startswith(("60", "688", "689")):
        return f"1.{code}"
    return f"0.{code}"


def tx_symbol(code: str) -> str:
    """腾讯行情符号：sh/sz/bj + 代码（原 gen_dashboard.py）"""
    if code.startswith(BJ_PREFIXES):
        return "bj" + code
    if code.startswith("6"):
        return "sh" + code
    return "sz" + code


def fmt_time(ts) -> str:
    """公告时间戳(ms) → 'YYYY-MM-DD HH:MM'（原 cninfo_fetch.py）"""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts) / 1000))
    except Exception:
        return ""


class _Net:
    """网络层：代理 / 超时 / 重试 / 退避，全部按 rules.yaml network 配置"""

    def __init__(self, cfg: Optional[dict] = None):
        from ..config import rules_config

        net = (cfg or (rules_config().get("network") or {}))
        self.connect_timeout = float(net.get("connect_timeout", 10))
        self.read_timeout = float(net.get("read_timeout", 25))
        self.timeout = self.connect_timeout + self.read_timeout
        self.max_attempts = int(net.get("max_attempts", 3))
        self.backoff_base = float(net.get("backoff_base", 2.0))
        self.user_agent = net.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        )
        self.verify_ssl = bool(net.get("verify_ssl", True))
        self.proxy = net.get("proxy") or (
            os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
            or os.getenv("HTTP_PROXY") or os.getenv("http_proxy")
            if net.get("proxy_env_fallback", True) else None
        )
        self._opener = self._build_opener()

    def _build_opener(self):
        import ssl

        handlers = []
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler({
                "http": self.proxy, "https": self.proxy,
            }))
        ctx = ssl.create_default_context()
        if not self.verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        if handlers or not self.verify_ssl:
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
            return urllib.request.build_opener(*handlers)
        return urllib.request.build_opener()

    def post_json(self, url: str, body: bytes, headers: dict,
                  dual_scheme: bool = False) -> dict:
        """POST 取 JSON，403 指数退避；dual_scheme 时先 https 再 http"""
        schemes = ("https", "http") if dual_scheme else (None,)
        last_err: Optional[Exception] = None
        for scheme in schemes:
            full = url.format(scheme=scheme) if scheme else url
            for attempt in range(self.max_attempts):
                try:
                    req = urllib.request.Request(full, data=body, headers=headers)
                    with self._opener.open(req, timeout=self.timeout) as resp:
                        return json.loads(resp.read().decode("utf-8"))
                except urllib.error.HTTPError as e:
                    last_err = e
                    if e.code == 403:                       # 巨潮限流
                        time.sleep(self.backoff_base * (attempt + 1))
                        continue
                    break
                except Exception as e:                      # noqa: BLE001
                    last_err = e
                    time.sleep(0.3)
        raise RuntimeError(f"请求失败 {url}：{last_err}")

    def get(self, url: str, headers: dict, encoding: str = "utf-8") -> str:
        """GET 文本（腾讯行情是 GBK）"""
        last_err: Optional[Exception] = None
        for attempt in range(self.max_attempts):
            try:
                req = urllib.request.Request(url, headers=headers)
                with self._opener.open(req, timeout=self.timeout) as resp:
                    return resp.read().decode(encoding, errors="replace")
            except Exception as e:                          # noqa: BLE001
                last_err = e
                time.sleep(self.backoff_base * (attempt + 1))
        raise RuntimeError(f"请求失败 {url}：{last_err}")


class CninfoFetcher(BaseFetcher):
    """真实数据源：巨潮公告 + 东财/腾讯K线 + 腾讯市值"""

    name = "cninfo"

    def __init__(self, config: Optional[dict] = None):
        from ..config import rules_config

        root = config or rules_config()
        fetch_cfg = root.get("fetch") or {}
        self.cninfo_cfg = fetch_cfg.get("cninfo") or {}
        self.kline_cfg = fetch_cfg.get("kline") or {}
        self.mv_cfg = fetch_cfg.get("market_cap") or {}
        self.net_cfg = root.get("network") or {}

        self.columns = self.cninfo_cfg.get("columns") or ["sse", "szse"]
        self.workers = int(self.cninfo_cfg.get("workers", 4))
        self.split_by_day = bool(self.cninfo_cfg.get("split_by_day", True))
        self.kline_days = int(self.kline_cfg.get("days", 120))
        self.kline_workers = int(self.kline_cfg.get("kline_workers", self.kline_cfg.get("workers", 6)))
        self.eastmoney_first = bool(self.kline_cfg.get("eastmoney_first", True))

        self.net = _Net(self.net_cfg)
        self._mv_cache_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "data", "market_cap_cache.json",
        )
        self._mv_cache = self._load_mv_cache()
        self._empty_streak = 0

    # ---------------- 公告 ----------------
    def _fetch_page(self, column: str, se_date: str, page: int) -> dict:
        body = urllib.parse.urlencode({
            "pageNum": str(page), "pageSize": str(PAGE_SIZE),
            "column": column, "tabName": "fulltext",
            "plate": "", "stock": "", "searchkey": "", "secid": "",
            "category": "", "trade": "", "seDate": se_date,
            "sortName": "", "sortType": "", "isHLtitle": "true",
        }).encode("utf-8")
        headers = {
            "User-Agent": self.net.user_agent,
            "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json",
        }
        return self.net.post_json(CNINFO_URL_TPL, body, headers, dual_scheme=True)

    def _fetch_range(self, column: str, se_date: str, workers: int) -> List[dict]:
        """单个日期区间的全部页：首页拿 total，其余页并发"""
        first = self._fetch_page(column, se_date, 1)
        total = int(first.get("totalAnnouncement") or 0)
        items = list(first.get("announcements") or [])
        total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        if total_pages <= 1:
            return items

        def work(p: int) -> List[dict]:
            try:
                return self._fetch_page(column, se_date, p).get("announcements") or []
            except Exception:                                # noqa: BLE001
                return []       # 单页失败静默跳过，内部已重试

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(work, p) for p in range(2, total_pages + 1)]
            for f in as_completed(futs):
                items.extend(f.result())
        return items

    def _fetch_column(self, column: str, start: str, end: str) -> List[dict]:
        if not self.split_by_day:
            return self._fetch_range(column, f"{start}~{end}", self.workers)

        # ★ 按天拆分：规避宽区间深分页被服务端截断导致首日公告丢失
        seen, merged = set(), []
        cur = date.fromisoformat(start)
        last = date.fromisoformat(end)
        while cur <= last:
            day = cur.isoformat()
            for it in self._fetch_range(column, f"{day}~{day}", self.workers):
                key = (it.get("secCode"),
                       it.get("announcementTitle") or it.get("shortTitle"),
                       it.get("announcementTime"))
                if key in seen:
                    continue
                seen.add(key)
                merged.append(it)
            cur += timedelta(days=1)
        return merged

    def fetch_announcements(self, start: str, end: str) -> Iterator[RawAnnouncement]:
        raw: List[dict] = []
        # ★ 沪深串行：两所同时并发会叠加请求量，易触发巨潮 IP 限流(403)
        for column in self.columns:
            try:
                raw.extend(self._fetch_column(column, start, end))
            except Exception as e:                            # noqa: BLE001
                # 单个交易所失败不拖垮整次更新
                print(f"[cninfo] {column} 抓取失败: {e}", flush=True)

        if not raw:
            self._empty_streak += 1
            limit = int(self.net_cfg.get("empty_streak_alert", 3))
            if self._empty_streak >= limit:
                raise RuntimeError(
                    f"连续 {self._empty_streak} 个区间未抓到任何公告。"
                    "大概率是网络问题：海外服务器直连巨潮常被限速/封禁，"
                    "请在 rules.yaml 的 network.proxy 配置代理，"
                    "或确认所选日期区间内有交易日。"
                )
            return
        self._empty_streak = 0

        seen = set()
        for a in raw:
            code = str(a.get("secCode") or "").strip()
            name = str(a.get("secName") or "")
            title = (a.get("shortTitle") or a.get("announcementTitle") or "").strip()
            ts = a.get("announcementTime")
            if not code or not title:
                continue
            dt = fmt_time(ts)
            key = (code, title, dt)
            if key in seen:
                continue
            seen.add(key)

            # ann_id：优先用巨潮自己的 ID，没有就用内容哈希（保证幂等）
            aid = a.get("announcementId") or hashlib.md5(
                f"{code}|{title}|{ts}".encode("utf-8")).hexdigest()[:20]

            adjunct = a.get("adjunctUrl") or ""
            yield RawAnnouncement(
                ann_id=str(aid),
                code=code,
                name=name,
                title=title,
                date=(dt or "")[:10],
                board=board_of(code),
                market_value=self.market_cap(code),
                url=f"http://static.cninfo.com.cn/{adjunct}" if adjunct else "",
                summary="",
            )

    # ---------------- K线 ----------------
    def fetch_klines(self, code: str, start: str, end: str) -> Iterator[RawKline]:
        rows = self._fetch_kline_rows(code)
        prev_close = None
        for r in rows:
            try:
                d, o, c, h, l, v = r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
            except Exception:                                 # noqa: BLE001
                continue
            if start and d < start:
                continue
            if end and d > end:
                continue
            pct = round((c - prev_close) / prev_close * 100, 2) if prev_close else 0.0
            prev_close = c
            yield RawKline(code=code, date=d, open=o, high=h, low=l,
                           close=c, volume=v, change_pct=pct)

    def _fetch_kline_rows(self, code: str) -> List[list]:
        """东财优先，失败切腾讯（原 gen_dashboard.py fetch_kline）"""
        is_bj = code.startswith(BJ_PREFIXES)
        last_err: Optional[Exception] = None

        if self.eastmoney_first and not is_bj:
            url = (f"{EASTMONEY_KLINE}?secid={secid(code)}"
                   "&fields1=f1,f2,f3,f4,f5,f6"
                   "&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
                   f"&klt=101&fqt=1&end=20500101&lmt={self.kline_days}")
            headers = {
                "User-Agent": self.net.user_agent,
                "Referer": "https://quote.eastmoney.com/",
                "Accept": "*/*", "Connection": "close",
            }
            try:
                data = json.loads(self.net.get(url, headers)).get("data") or {}
                klines = data.get("klines") or []
                if klines:
                    return [k.split(",") for k in klines]
                last_err = RuntimeError(f"{code} 东财返回空K线")
            except Exception as e:                            # noqa: BLE001
                last_err = e

        # 腾讯兜底（北交所东财必失败，直接走这里）
        sym = tx_symbol(code)
        url = f"{TENCENT_KLINE}?param={sym},day,,,{self.kline_days},qfq"
        headers = {"User-Agent": self.net.user_agent, "Referer": "https://gu.qq.com/"}
        try:
            res = json.loads(self.net.get(url, headers))
            kd = (res.get("data") or {}).get(sym, {})
            if kd.get("qfqday") or kd.get("day"):
                return list(kd.get("qfqday") or kd.get("day"))
            last_err = RuntimeError(f"{code} 无K线数据（可能停牌/转板）")
        except Exception as e:                                # noqa: BLE001
            last_err = e

        print(f"[kline] {code} 无K线数据，跳过", flush=True)
        return []

    def fetch_klines_batch(self, codes: List[str], start: str, end: str):
        """并发拉多只K线（更新任务用）"""
        with ThreadPoolExecutor(max_workers=self.kline_workers) as ex:
            futs = {ex.submit(self._safe_klines, c, start, end): c for c in codes}
            for f in as_completed(futs):
                code = futs[f]
                try:
                    yield code, list(f.result())
                except Exception:                             # noqa: BLE001
                    yield code, []

    def _safe_klines(self, code: str, start: str, end: str) -> Iterator[RawKline]:
        try:
            yield from self.fetch_klines(code, start, end)
        except Exception as e:                                # noqa: BLE001
            print(f"[cninfo] K线失败 {code}: {e}", flush=True)

    # ---------------- 市值 ----------------
    def _load_mv_cache(self) -> dict:
        try:
            with open(self._mv_cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:                                     # noqa: BLE001
            return {}

    def _save_mv_cache(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._mv_cache_path), exist_ok=True)
            tmp = self._mv_cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._mv_cache, f, ensure_ascii=False)
            os.replace(tmp, self._mv_cache_path)
        except Exception:                                     # noqa: BLE001
            pass

    def market_cap(self, code: str) -> float:
        """总市值（亿元）。腾讯行情字段 45 = 总市值(亿)，带 7 天本地缓存。"""
        if not self.mv_cfg.get("enabled", True):
            return 0.0
        hit = self._mv_cache.get(code)
        if hit and time.time() - float(hit.get("t", 0)) < self.mv_cfg.get("cache_days", 7) * 86400:
            return float(hit.get("v", 0))
        try:
            raw = self.net.get(TENCENT_QUOTE + tx_symbol(code),
                               {"User-Agent": self.net.user_agent}, encoding="gbk")
            parts = raw.split("~")
            val = float(parts[45]) if len(parts) > 45 else 0.0
        except Exception:                                     # noqa: BLE001
            val = 0.0
        if val > 0:
            self._mv_cache[code] = {"v": val, "t": time.time()}
            if len(self._mv_cache) % 50 == 0:
                self._save_mv_cache()
        return val

    def close(self) -> None:
        self._save_mv_cache()