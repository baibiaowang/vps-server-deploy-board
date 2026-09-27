"""
规则引擎（可配置 / 统一口径 / 支持热重载）

解决的问题：
  原站首页卡片与报告页各用一套分类标签，互不一致（首页有退市风险/破产重整，
  报告页没有；报告页有立案/处罚，首页没有）。这里全部收敛到 config/rules.yaml
  这一份 taxonomy，前后端共用，口径天然统一。

用法：
  engine = RuleEngine()
  engine.classify("关于重大资产重组的公告")   -> 'merger'
  engine.extract_numbers("交易金额12.5亿元")  -> ['12.5亿元']
  engine.reload()                            # 改完 YAML 无需重启
"""
from __future__ import annotations

import os
import re
import threading
from typing import Dict, List, Optional

import yaml

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_RULES_PATH = os.path.join(BASE_DIR, "config", "rules.yaml")

# 金额单位换算（统一到「元」）
_AMOUNT_UNIT = {
    "亿元": 1e8,
    "万元": 1e4,
    "元": 1.0,
}


class RuleEngine:
    def __init__(self, path: str = DEFAULT_RULES_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._mtime: float = 0.0
        self.load(force=True)

    # ---------- 加载 ----------
    def load(self, force: bool = False) -> None:
        with self._lock:
            mtime = os.path.getmtime(self.path) if os.path.exists(self.path) else 0
            if not force and mtime == self._mtime:
                return
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
            except Exception:  # noqa: BLE001
                if getattr(self, "_mtime", 0.0):
                    return
                cfg = {}

            self._mtime = mtime
            self.version = cfg.get("version", 1)
            self.cfg = cfg
            self.taxonomy_raw: List[dict] = cfg.get("taxonomy", [])
            self.match_fields: List[str] = cfg.get("classify", {}).get(
                "match_fields", ["title", "summary"]
            )
            self.multi_label: bool = cfg.get("classify", {}).get("multi_label", False)
            self.default_category: str = cfg.get("classify", {}).get(
                "default_category", "other"
            )

            # 编译规则（按 priority 降序，便于单标签取最高优先级）
            self.rules: List[dict] = []
            for item in self.taxonomy_raw:
                if not item.get("enabled", True):
                    continue
                self.rules.append({
                    "id": item["id"],
                    "label": item.get("label", item["id"]),
                    "keywords": item.get("keywords", []) or [],
                    "patterns": [re.compile(p) for p in (item.get("patterns") or [])],
                    "priority": int(item.get("priority", 0)),
                    "min_amount": item.get("min_amount"),
                })
            self.rules.sort(key=lambda r: r["priority"], reverse=True)
            self._by_id: Dict[str, dict] = {r["id"]: r for r in self.rules}

            # 关键数字提取规则
            nx = cfg.get("numeric_extraction", {}) or {}
            self._num_max = int(nx.get("max_items", 4))
            self._num_patterns: List[re.Pattern] = []
            for key in ("money", "percent", "share"):
                for p in (nx.get(key) or []):
                    self._num_patterns.append(re.compile(p))
            self._amount_patterns: List[re.Pattern] = [
                re.compile(p) for p in (nx.get("money") or [])
            ]

            # 噪声过滤（原 scripts/gen_dashboard.py 的 is_noise）
            nz = cfg.get("noise") or {}
            self.noise_on: bool = bool(nz.get("enabled", True))
            self._noise_titles: List[str] = list(nz.get("titles") or [])
            self._pledge_white: List[str] = list(nz.get("pledge_whitelist") or [])

    def reload(self) -> None:
        """热重载：修改 YAML 后调用，无需重启服务"""
        self.load(force=True)

    def _auto_reload_if_changed(self) -> None:
        """文件被改动则自动重载（开发体验）"""
        try:
            if os.path.exists(self.path) and os.path.getmtime(self.path) != self._mtime:
                self.load(force=True)
        except OSError:
            pass

    # ---------- 对外：分类体系（前端共用） ----------
    def taxonomy(self) -> List[dict]:
        """返回给前端的分类清单（首页卡片 + 报告页筛选共用同一份）"""
        self._auto_reload_if_changed()
        out = []
        for item in self.taxonomy_raw:
            if not item.get("enabled", True):
                continue
            out.append({
                "id": item["id"],
                "label": item.get("label", item["id"]),
                "short": item.get("short") or item.get("label", item["id"]),
                "icon": item.get("icon", ""),
                "order": int(item.get("order", 99)),
                "priority": int(item.get("priority", 0)),
                "risk": bool(item.get("risk", False)),
            })
        out.sort(key=lambda x: x["order"])
        return out

    def label_of(self, category_id: str) -> str:
        for item in self.taxonomy_raw:
            if item.get("id") == category_id:
                return item.get("label", category_id)
        return category_id

    # ---------- 对外：分类 ----------
    def classify(self, title: str, summary: str = "") -> str:
        """
        单标签：命中多类时取 priority 最高者；未命中返回 default_category。
        若分类配置了 min_amount（如质押需大额），金额不足则跳过该类。
        """
        self._auto_reload_if_changed()
        text = f"{title or ''} {summary or ''}".strip()
        if not text:
            return self.default_category

        hits: List[dict] = []
        amount = self._max_amount(text) if any(r["min_amount"] for r in self.rules) else None

        for rule in self.rules:
            matched = False
            for kw in rule["keywords"]:
                if kw and kw in text:
                    matched = True
                    break
            if not matched:
                for pat in rule["patterns"]:
                    if pat.search(text):
                        matched = True
                        break
            if not matched:
                continue
            # 金额门槛校验
            if rule["min_amount"] and amount is not None:
                if amount < float(rule["min_amount"]):
                    continue
            hits.append(rule)
            if not self.multi_label:
                # rules 已按 priority 降序，第一个命中即最高优先级
                return rule["id"]

        return hits[0]["id"] if hits else self.default_category

    # ---------- 对外：噪声过滤 ----------
    def noise_enabled(self) -> bool:
        self._auto_reload_if_changed()
        return self.noise_on

    def is_noise(self, title: str) -> bool:
        """
        例行公告判定：命中关键词但毫无看板价值，直接丢弃。

        原 scripts/gen_dashboard.py is_noise() 的逻辑，两条规则：
          1. 标题含噪声词（章程/议事规则/回购进展/债券/审计报告…）
          2. 质押类特例：只保留 控股股东 / 第一大股东 / 5%以上股东 / 解除质押，
             其余千篇一律的"关于股份质押的公告"全部过滤
        """
        if not self.noise_on:
            return False
        t = title or ""
        for kw in self._noise_titles:
            if kw and kw in t:
                return True
        if "质押" in t and not any(k in t for k in self._pledge_white):
            return True
        return False

    # ---------- 对外：关键数字 ----------
    def extract_numbers(self, text: str) -> List[str]:
        """提取关键数字（金额 / 比例 / 股数），去重保序"""
        if not text:
            return []
        found: List[str] = []
        for pat in self._num_patterns:
            for m in pat.finditer(text):
                val = m.group(0).strip()
                if val not in found:
                    found.append(val)
                if len(found) >= self._num_max:
                    return found
        return found

    def _max_amount(self, text: str) -> Optional[float]:
        """从文本中提取最大金额（统一换算为「元」），用于阈值判断"""
        best: Optional[float] = None
        for pat in self._amount_patterns:
            for m in pat.finditer(text):
                try:
                    num = float(m.group(1))
                except (ValueError, IndexError):
                    continue
                # 依据匹配到的单位换算
                seg = m.group(0)
                unit = 1.0
                for u, mul in _AMOUNT_UNIT.items():
                    if u in seg:
                        unit = mul
                        break
                val = num * unit
                if best is None or val > best:
                    best = val
        return best


# 全局单例
_engine: Optional[RuleEngine] = None


def get_engine() -> RuleEngine:
    global _engine
    if _engine is None:
        _engine = RuleEngine()
    return _engine