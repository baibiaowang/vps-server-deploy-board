"""
Mock 数据源（本地跑通用，等真实源码接入后可删除）

数据规模刻意对齐你原站的真实体量，用于验证性能：
  - 股票池：80 只真实样本（从你线上站点抓取）+ 合成至 800 只
  - 公告：约 4380 条/交易日（2 天 ≈ 8760 条，对齐原站 8764 条）
  - K 线：800 只 × 90 交易日 ≈ 7.2 万条
"""
from __future__ import annotations

import hashlib
import json
import os
import random
from datetime import date, timedelta
from typing import Iterator, List, Tuple

from .base import BaseFetcher, RawAnnouncement, RawKline

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SAMPLE_PATH = os.path.join(BASE_DIR, "config", "sample_stocks.json")

# ---------------- 标题模板（按业务语义，供规则引擎分类） ----------------
TITLES: dict[str, List[str]] = {
    "merger": [
        "关于筹划重大资产重组事项的停牌公告",
        "关于发行股份购买资产暨关联交易的进展公告",
        "关于重大资产购买之标的资产过户完成的公告",
        "关于控股股东筹划控制权变更的提示性公告",
        "关于吸收合并全资子公司的公告",
    ],
    "disposal": [
        "关于公开挂牌出售全资子公司100%股权的公告",
        "关于转让参股公司股权的公告",
        "关于出售部分闲置资产的公告",
        "关于拟公开挂牌转让子公司股权的提示性公告",
    ],
    "personnel": [
        "关于董事长辞职暨选举新任董事长的公告",
        "关于聘任公司总经理的公告",
        "关于公司董事辞职的公告",
        "关于变更法定代表人的公告",
        "关于高级管理人员变动的公告",
    ],
    "pledge": [
        "关于控股股东部分股份质押的公告",
        "关于持股5%以上股东股份解除质押的公告",
        "关于股东股份质押式回购交易展期的公告",
        "关于控股股东补充质押的公告",
    ],
    "forecast": [
        "2026年半年度业绩预告",
        "2026年前三季度业绩预增公告",
        "2026年半年度业绩快报",
        "2026年年度业绩预亏公告",
    ],
    "litigation": [
        "关于重大诉讼进展的公告",
        "关于收到中国证券监督管理委员会立案告知书的公告",
        "关于公司及相关当事人收到行政处罚决定书的公告",
        "关于重大仲裁事项的公告",
    ],
    "dividend": [
        "关于2026年中期利润分配方案的公告",
        "关于以集中竞价交易方式回购股份的进展公告",
        "关于控股股东增持公司股份计划的公告",
        "关于2025年度权益分派实施公告",
    ],
    "delisting": [
        "关于股票可能被实施退市风险警示的提示性公告",
        "关于公司股票可能被终止上市的风险提示公告",
    ],
    "restructuring": [
        "关于公司预重整事项的公告",
        "关于法院裁定受理公司重整的公告",
        "关于重整计划执行进展的公告",
    ],
    # 澄清/辟谣：真实公告里几乎不会写"辟谣"，一律写"澄清/澄清公告/严正声明"，
    # 所以标题模板也照真实口径写 —— 顺便验证分类规则能不能从"澄清"认出辟谣。
    "clarify": [
        "关于媒体报道的澄清公告",
        "关于市场传闻的澄清公告",
        "关于网络传言的澄清说明",
        "关于媒体报道不实信息的声明",
        "关于股票交易异常波动暨媒体报道传闻的澄清公告",
    ],
    "other": [
        "关于召开2026年第二次临时股东大会的通知",
        "关于使用闲置募集资金进行现金管理的公告",
        "关于变更会计师事务所的公告",
        "关于获得政府补助的公告",
        "关于完成工商变更登记的公告",
        "关于修订《公司章程》的公告",
        "关于对外担保的公告",
        "关于日常关联交易的公告",
    ],
}

# 分类权重（贴近真实：例行公告占多数，事件类占少数）
# 注意：这里的 key 是「标题模板桶名」，不一定要等于 taxonomy 的 id
# （例如 litigation 桶生成"诉讼/仲裁"标题，最终分类由 rules.yaml 判成 lawsuit）。
# 改动后请保持合计 = 100.0。
CATEGORY_WEIGHTS: List[Tuple[str, float]] = [
    ("other", 60.0), ("dividend", 9.0), ("personnel", 7.5),
    ("forecast", 5.5), ("merger", 4.0), ("pledge", 3.5),
    ("litigation", 3.5), ("disposal", 3.0), ("clarify", 2.0),
    ("restructuring", 1.0), ("delisting", 1.0),
]

# 合成股票名称用字
_NAME_A = ["华", "中", "泰", "恒", "瑞", "鑫", "博", "天", "宏", "嘉", "安", "金",
           "正", "通", "联", "海", "汇", "盛", "兴", "昌", "德", "光", "宇", "创"]
_NAME_B = ["股份", "科技", "集团", "药业", "电子", "能源", "材料", "智能",
           "生物", "环保", "传媒", "控股", "实业", "建设", "发展"]


def board_of(code: str) -> str:
    """按代码前缀判定板块"""
    if code.startswith(("600", "601", "603", "605")):
        return "主板"
    if code.startswith(("000", "001", "002", "003")):
        return "主板"
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("8", "4", "9")):
        return "北交所"
    return "其他"


def _workdays(start: date, end: date) -> List[date]:
    """区间内的工作日列表"""
    days, cur = [], start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


class MockFetcher(BaseFetcher):
    """确定性 Mock 数据源（相同 seed → 相同结果，便于验证）"""

    name = "mock"

    def __init__(self, seed: int = 20260828, pool_size: int = 800,
                 ann_per_day: int = 4380, kline_days: int = 90):
        self.seed = seed
        self.rnd = random.Random(seed)
        self.pool_size = pool_size
        self.ann_per_day = ann_per_day
        self.kline_days = kline_days
        self._pool: List[dict] = self._build_pool()

    # ---------- 股票池 ----------
    def _build_pool(self) -> List[dict]:
        pool: List[dict] = []
        # 1) 真实样本（从你线上站点抓取的 80 只）
        if os.path.exists(SAMPLE_PATH):
            with open(SAMPLE_PATH, "r", encoding="utf-8") as f:
                for s in json.load(f):
                    pool.append({
                        "code": s["code"],
                        "name": s["name"],
                        "board": board_of(s["code"]),
                        "market_value": round(self.rnd.uniform(30, 3000), 2),
                        "change_pct": s.get("change_pct", 0.0),
                        "real": True,
                    })
        # 2) 合成补足
        used = {s["code"] for s in pool}
        while len(pool) < self.pool_size:
            prefix = self.rnd.choice(["600", "601", "603", "000", "002", "300", "688"])
            code = prefix + "".join(str(self.rnd.randint(0, 9)) for _ in range(3))
            if code in used:
                continue
            used.add(code)
            name = self.rnd.choice(_NAME_A) + self.rnd.choice(_NAME_B)
            pool.append({
                "code": code,
                "name": name,
                "board": board_of(code),
                "market_value": round(self.rnd.uniform(15, 2000), 2),
                "change_pct": round(self.rnd.uniform(-9.9, 9.9), 2),
                "real": False,
            })
        return pool

    def pool(self) -> List[dict]:
        return self._pool

    # ---------- 公告 ----------
    def fetch_announcements(self, start: str, end: str) -> Iterator[RawAnnouncement]:
        d0 = date.fromisoformat(start)
        d1 = date.fromisoformat(end)
        if d0 > d1:
            return
        cats = [c for c, _ in CATEGORY_WEIGHTS]
        weights = [w for _, w in CATEGORY_WEIGHTS]

        # ★ 公告必须「按交易日独立播种」，不能按整个窗口播一条随机流顺序消费。
        #
        #   为什么：真实数据源（巨潮）是逐日请求，某天返回什么跟"这次窗口多大"无关。
        #   若按 (start,end) 播一条流顺序消费，同一天在 [A,B] 与 [A,C] 两个窗口下
        #   会产出完全不同的内容 —— 后果是补漏重扫无法还原缺失那天的数据：
        #   实测删掉 2026-08-31 的 273 条后重扫，只补回 137 条，凭空少 136 条。
        #   按日播种后任意窗口下某天内容恒定，补漏才是真的"补回原样"。
        #
        #   确定性随机源：保证 mock 下重复更新幂等（new=0）。
        #   注意：不能用 Python 内建 hash()，它默认按进程随机化。

        seq = 0
        for day in _workdays(d0, d1):
            day_seed = int(hashlib.md5(
                f"{self.seed}|{day.isoformat()}".encode("utf-8")).hexdigest(), 16)
            drnd = random.Random(day_seed)
            for _ in range(self.ann_per_day):
                st = drnd.choice(self._pool)
                cat = drnd.choices(cats, weights=weights, k=1)[0]
                title = drnd.choice(TITLES[cat])

                # 金额上下文（供"关键数字"提取 & 质押阈值判断）
                amount = None
                if cat in ("pledge", "merger", "disposal", "dividend", "litigation"):
                    if drnd.random() < 0.75:
                        amount = round(drnd.uniform(0.3, 60), 2)
                        title = f"{title}（涉及金额{amount}亿元）"

                summary = ""
                if cat == "pledge" and amount:
                    shares = round(drnd.uniform(500, 8000), 1)
                    summary = (f"本次质押股份{shares}万股，占其所持股份比例"
                               f"{round(drnd.uniform(5, 60), 2)}%，融资金额{amount}亿元。")

                key = f"{st['code']}|{day.isoformat()}|{title}|{summary}|{seq}"
                seq += 1
                yield RawAnnouncement(
                    # ★ ann_id 必须由「内容」决定，不能由生成顺序决定：
                    #   幂等入库靠 ann_id 唯一键。用序号的话，换个日期区间重新抓取
                    #   就会得到一套新 id，同一条公告被重复写入（实测 new=4015）。
                    ann_id="MOCK" + hashlib.md5(key.encode("utf-8")).hexdigest()[:20],
                    code=st["code"],
                    name=st["name"],
                    title=title,
                    date=day.isoformat(),
                    board=st["board"],
                    market_value=st["market_value"],
                    url=f"http://www.cninfo.com.cn/mock/{seq}",
                    summary=summary,
                )

    # ---------- K 线 ----------
    # 生成规则：以固定纪元 EPOCH 为唯一起点，按代码确定性游走。
    #
    # ★ 为什么不能用「从 start 开始游走」：
    #   那样同一 (code, date) 在不同调用里会得到不同价格。多次增量更新叠加后，
    #   新旧两段游走在接壤处会出现断崖（实测见过 +45% 的假单日涨跌）。
    #   固定纪元后，任意一天的价格只由 (code, date) 决定，永远自洽。
    EPOCH = date(2025, 1, 1)
    # A股涨跌停：主板 ±10%、创业板/科创板 ±20%、北交所 ±30%
    _LIMIT = {"主板": 0.10, "创业板": 0.20, "科创板": 0.20, "北交所": 0.30}

    def fetch_klines(self, code: str, start: str, end: str) -> Iterator[RawKline]:
        if start > end:
            return
        st = next((s for s in self._pool if s["code"] == code), None)
        base_price = 10.0 if st is None else max(3.0, min(180.0, st["market_value"] / 8))
        limit = self._LIMIT.get((st or {}).get("board", "主板"), 0.10)
        # 同样避开 hash() 的进程随机化，让同一股票永远产生同一条 K 线
        k_seed = int(hashlib.md5(f"kline|{self.seed}|{code}".encode("utf-8")).hexdigest(), 16)
        rnd = random.Random(k_seed)

        price = base_price
        prev_close = None
        # 从固定纪元一路走到 end，但只 yield [start, end] 区间。
        # 区间之前的天数只用来推进随机游走，不占内存。
        for day in _workdays(self.EPOCH, date.fromisoformat(end)):
            # 均值回复：纯随机游走跑了 400+ 天会漂到离谱价位
            drift = rnd.uniform(-0.024, 0.024)
            reversion = (base_price - price) / base_price * 0.05
            step = max(-limit, min(limit, drift + reversion))
            price = max(1.0, price * (1 + step))

            open_p = price * (1 + rnd.uniform(-0.008, 0.008))
            close_p = price * (1 + rnd.uniform(-0.008, 0.008))
            # 单日振幅压在涨跌停内（A股不可能一天涨 45%）
            close_p = max(open_p * (1 - limit), min(open_p * (1 + limit), close_p))
            high_p = max(open_p, close_p) * (1 + abs(rnd.uniform(0, 0.010)))
            low_p = min(open_p, close_p) * (1 - abs(rnd.uniform(0, 0.010)))
            volume = round(rnd.uniform(1e5, 9e6), 0)
            iso = day.isoformat()

            if iso < start:
                prev_close = round(close_p, 2)
                continue
            pct = round((close_p - prev_close) / prev_close * 100, 2) if prev_close else 0.0
            prev_close = round(close_p, 2)
            yield RawKline(
                code=code, date=iso,
                open=round(open_p, 2), high=round(high_p, 2),
                low=round(low_p, 2), close=round(close_p, 2),
                volume=volume, change_pct=pct,
            )