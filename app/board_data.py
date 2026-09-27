"""
看板数据装配（需求①：布局不改，只换数据源）

产出与线上看板 JS 严格同构的两个东西：

  1) data_list.js  →  window.ANNO_LIST = [ {code,name,category,board,is_st,
                        reason,chg,chg5,chg_ann,market_cap,announcements:[{date,title}]} ]
  2) /api/kline?code= → { klines: [[日期,开,收,高,低,量], ...] }
     ★ 注意元组顺序是 [date, open, close, high, low, vol]
       原版 JS: ohlc = k.map(x => [x[1], x[2], x[4], x[3]])  // open, close, low, high

两条重要的内存约束（1核1G）：
  · K线不进 Python。涨跌幅全部用 SQL 窗口函数 + 相关子查询算，
    避免把 4219 只股票 × 120 天 ≈ 50 万行 K 线读进内存（约 200MB，1G 机器会 OOM）。
  · data_list.js 落盘 + 预压缩，请求时零 CPU（1 核算 gzip 是会卡的）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import text

from .db import get_session
from .rules import get_engine

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEN_DIR = os.path.join(BASE_DIR, "data", "board")
DATA_LIST_PATH = os.path.join(GEN_DIR, "data_list.js")

_lock = threading.Lock()
_last_payload_sig: Optional[str] = None


# ---------------- 配置 ----------------
def _display_cfg() -> dict:
    from .config import rules_config
    return (rules_config().get("display") or {})


def _fetch_cfg() -> dict:
    from .config import rules_config
    return (rules_config().get("fetch") or {})


# ---------------- 涨跌幅（纯 SQL，不读 K 线进内存） ----------------
def _window_dates(session, days: int) -> tuple[str, str]:
    """公告窗口：以库内最新公告日期为锚点往前推 days 天

    ★ 锚点用库内 MAX(date) 而不是"今天"：
      海外服务器 TZ=UTC 时"今天"会算错，用库内日期则完全不依赖机器时区。
    """
    from datetime import timedelta

    from .timeutil import today_cn

    mx = session.execute(text("SELECT MAX(date) FROM announcements")).scalar()
    end = mx or today_cn().isoformat()
    start = (date.fromisoformat(end) - timedelta(days=days - 1)).isoformat()
    return start, end


def _chg_stats(session, codes: list[str], min_ann_dates: dict[str, str]) -> dict:
    """
    批量算 chg / chg5 / chg_ann。

    chg     = (最后收盘 - 前一日收盘) / 前一日收盘        [与原版一致]
    chg5    = (最后收盘 - 5个交易日前收盘) / 5日前收盘    [与原版一致]
    chg_ann = 从「首个命中公告交易日」的前一日收盘 到 最后收盘
              若该交易日就是K线首日，则用当日开盘价为基准  [与原版一致]
    """
    if not codes:
        return {}
    # 每只股票最近 6 个交易日收盘（窗口函数，每只股票只回传 6 行）
    # ORDER BY rn 保证顺序为 [最新, 前1日, ..., 前5日]
    # 分批查询，避开 SQLite 参数上限（默认 999）
    by_code: dict[str, list] = {}
    for i in range(0, len(codes), 900):
        chunk = codes[i:i + 900]
        ph = ",".join(f":c{j}" for j in range(len(chunk)))
        params = {f"c{j}": c for j, c in enumerate(chunk)}
        rows = session.execute(text(f"""
            SELECT code, close FROM (
                SELECT code, close,
                       ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) rn
                FROM klines WHERE code IN ({ph})
            ) WHERE rn <= 6 ORDER BY code, rn
        """), params).fetchall()
        for code, close in rows:
            by_code.setdefault(code, []).append(close)

    out: dict[str, dict] = {}
    for code, arr in by_code.items():
        if not arr:
            continue
        last_close = arr[0]
        chg = round((last_close - arr[1]) / arr[1] * 100, 2) if len(arr) >= 2 and arr[1] else 0.0
        chg5 = round((last_close - arr[5]) / arr[5] * 100, 2) if len(arr) >= 6 and arr[5] else 0.0
        out[code] = {"last_close": last_close, "chg": chg, "chg5": chg5, "chg_ann": None}

    # 2) chg_ann 基准价：首个「不早于最早公告日的交易日」的前一日收盘
    #    走 (code, date) 索引，4219 只股票实测毫秒级
    #    min_ann_date 由外层传入，这里只需要按 code 分组即可
    todo = [c for c in min_ann_dates if c in out and min_ann_dates[c]]
    if todo:
        # 分批，避开 SQLite 参数上限（默认 999）
        for i in range(0, len(todo), 400):
            chunk = todo[i:i + 400]
            ph = ",".join(f":p{j}" for j in range(len(chunk)))
            p2 = {f"p{j}": c for j, c in enumerate(chunk)}
            sql = f"""
                SELECT m.code,
                       (SELECT k.close FROM klines k
                        WHERE k.code = m.code AND k.date < (
                            SELECT MIN(k2.date) FROM klines k2
                            WHERE k2.code = m.code AND k2.date >= m.min_date)
                        ORDER BY k.date DESC LIMIT 1) AS base_close,
                       (SELECT k.open FROM klines k
                        WHERE k.code = m.code AND k.date = (
                            SELECT MIN(k2.date) FROM klines k2
                            WHERE k2.code = m.code AND k2.date >= m.min_date)
                        LIMIT 1) AS anchor_open
                FROM (SELECT code, MIN(date) AS min_date FROM announcements
                      WHERE category <> 'other' AND code IN ({ph})
                      GROUP BY code) m
            """
            try:
                for code, base_close, anchor_open in session.execute(text(sql), p2).fetchall():
                    # 原版逻辑：anchor 不是K线首日 → 取前一日收盘；是首日 → 取当日开盘
                    base = base_close if base_close else anchor_open
                    if base and code in out:
                        lc = out[code]["last_close"]
                        out[code]["chg_ann"] = round((lc - base) / base * 100, 2)
            except Exception as exc:  # noqa: BLE001
                # chg_ann 算不出来不影响其它字段，但要把原因留下便于排查
                print(f"[board_data] chg_ann 计算失败: {exc}", flush=True)
    return out


# ---------------- 主装配 ----------------
def build_payload(session=None, days: int = 90) -> dict:
    """装配 ANNO_LIST（字段与原版 data_list.js 完全一致）"""
    own = session is None
    s = session or get_session()
    try:
        eng = get_engine()
        disp = _display_cfg()
        cap = int(disp.get("max_announcements_per_stock", 20))
        cat_src = str(disp.get("category_source", "latest"))

        d0, d1 = _window_dates(s, days)

        # 1) 窗口内的事件类公告（按 code 聚合）
        rows = s.execute(text("""
            SELECT code, name, date, title, category, board
            FROM announcements
            WHERE category <> 'other' AND date BETWEEN :d0 AND :d1
            ORDER BY code, date
        """), {"d0": d0, "d1": d1}).fetchall()

        agg: dict[str, dict] = {}
        for code, name, dt, title, category, board in rows:
            e = agg.get(code)
            if e is None:
                e = agg[code] = {
                    "code": code, "name": name, "board": board,
                    "category": category, "anns": [],
                }
            e["anns"].append({"date": dt, "title": title})
            if not e["name"]:
                e["name"] = name
            # 分类归属：latest=按最新公告（更准）/ earliest=按最早公告（原版行为）
            if cat_src == "earliest":
                pass                       # 保持首次写入的 category
            else:
                e["category"] = category   # ORDER BY date 升序，最后写入即最新

        if not agg:
            return {"range": {"start": d0, "end": d1}, "items": [], "list": []}

        codes = list(agg.keys())

        # 2) 市值（DB 存的是「亿元」，前端 fmtMv 要「元」→ ×1e8）
        mv_map: dict[str, float] = {}
        board_map: dict[str, str] = {}
        for i in range(0, len(codes), 900):
            chunk = codes[i:i + 900]
            ph = ",".join(f":c{j}" for j in range(len(chunk)))
            _rows = s.execute(text(f"""
                SELECT code, market_value, board FROM stocks
                WHERE code IN ({ph})
            """), {f"c{j}": c for j, c in enumerate(chunk)}).fetchall()
            for r in _rows:
                mv_map[r[0]] = r[1] or 0.0
                board_map[r[0]] = r[2] or ""

        # 3) 涨跌幅
        min_ann = {c: (agg[c]["anns"][0]["date"] if agg[c]["anns"] else "") for c in codes}
        stats = _chg_stats(s, codes, min_ann)

        # 4) 组装（严格对齐原版字段与顺序）
        items = []
        for code in codes:
            e = agg[code]
            anns = e["anns"]
            full_anns = anns
            shown = anns[-cap:] if cap > 0 else anns
            st = stats.get(code, {})
            items.append({
                "code": code,
                "name": e["name"] or "",
                "category": eng.label_of(e["category"]),
                "board": e.get("board") or board_map.get(code, ""),
                "is_st": "ST" in (e["name"] or "").upper(),
                "reason": (full_anns[-1]["title"] if full_anns else ""),   # 原版：最后一条公告标题
                "chg": st.get("chg", 0),
                "chg5": st.get("chg5", 0),
                "chg_ann": st.get("chg_ann"),
                # 亿 → 元；round 掉 float 尾数（否则会出现 116310999999.99998）
                "market_cap": round(float(mv_map.get(code, 0.0)) * 1e8, 2),
                "announcements": [{"date": a["date"], "title": a["title"]} for a in shown],
            })

        # 与线上一致：按公告日期倒序（原 gen_dashboard 按归档顺序，视觉上新的在前）
        items.sort(key=lambda x: (x["announcements"][-1]["date"] if x["announcements"] else ""),
                   reverse=True)

        all_dates = [a["date"] for it in items for a in it["announcements"] if a["date"]]
        return {
            "range": {
                "start": min(all_dates) if all_dates else d0,
                "end": max(all_dates) if all_dates else d1,
            },
            "items": items,
        }
    finally:
        if own:
            s.close()


def render_data_list_js(payload: dict) -> str:
    """渲染成 window.ANNO_LIST = [...]（与原 gen_dashboard.py 输出格式一致）"""
    body = json.dumps(payload["items"], ensure_ascii=False, separators=(",", ":"))
    return "window.ANNO_LIST = " + body + ";\n"


# ---------------- 落盘 + 预压缩（1核1G：请求期零 CPU） ----------------
def ensure_data_list_file(force: bool = False) -> str:
    """
    生成 data/data_list.js（若不存在或数据有变）。

    落盘而非每次现算的原因：
      · 3MB JSON 每次请求重新装配 + 压缩，1 核机器会明显卡顿
      · 落盘后配合预压缩 .br/.gz，请求期只是一次 sendfile
    """
    global _last_payload_sig
    with _lock:
        if not force and os.path.exists(DATA_LIST_PATH):
            return DATA_LIST_PATH
        s = get_session()
        try:
            payload = build_payload(s)
        finally:
            s.close()
        js = render_data_list_js(payload)
        os.makedirs(GEN_DIR, exist_ok=True)
        tmp = DATA_LIST_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(js)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, DATA_LIST_PATH)
        sig = hashlib.md5(js.encode("utf-8")).hexdigest()
        _last_payload_sig = sig
        # 落盘后立刻生成预压缩副本（构建期压缩，运行期零开销）
        try:
            from .static_assets import precompress_file
            precompress_file(DATA_LIST_PATH)
        except Exception:  # noqa: BLE001
            pass
        # 页签标题的日期范围也写进去
        _write_meta(payload["range"], len(payload["items"]))
        return DATA_LIST_PATH


def _write_meta(rng: dict, count: int) -> None:
    try:
        from .timeutil import now_cn
        meta = {**rng, "count": count, "generated_at": now_cn().isoformat()}
        with open(os.path.join(GEN_DIR, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def read_meta() -> dict:
    try:
        with open(os.path.join(GEN_DIR, "meta.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def apply_title(html: str, rng: dict) -> str:
    """把 <title> 里的日期范围换成实际数据范围（内容替换，不动任何布局）"""
    if not rng.get("start") or not rng.get("end"):
        return html
    span = f"{rng['start']} ~ {rng['end']}"
    return re.sub(r"<title>[^<]*</title>", f"<title>A股公告看板（{span}）</title>", html, count=1)


# ---------------- 遗漏检测 ----------------
def detect_gaps(days: int = 90, thin_below: int = 5) -> dict:
    """
    遗漏检测：最近 days 天里，哪些【真实交易日】缺公告数据。

    为什么需要这个：
      抓取是逐日请求第三方接口。网络抖动、被限流、接口悄悄改版，
      都可能让某一天静默抓空 —— 而你从看板上看不出来，
      它长得就跟"那天本来就没公告"一模一样。原站没有这层校验。

    ★ 交易日口径取 klines 表而不是"周一至周五"：
      行情源给的就是真实开市日（已排除春节/国庆这类连续休市），
      按 weekday 猜会把节假日全报成缺口，反而淹掉真问题。
      klines 为空时才回落 weekday。

    ★ 窗口锚点 = MAX(公告最新日, 行情最新日)，不用 today_cn()：
      · 不依赖服务器时区（需求②的延续）
      · 关键：只拿公告最新日会留一个瞎区 —— 万一最新那天的公告整个漏抓/被删，
        锚点会跟着退到前一天，于是"漏掉的那天"直接掉出回看窗口，
        检测永远报"无缺口"。这正是最该发现的情况，所以必须把行情日也拉进来
        （klines 是独立抓取的，不会被公告的增删带着走）。
    """
    from .timeutil import today_cn, weekday_range

    s = get_session()
    try:
        mx_ann = s.execute(text("SELECT MAX(date) FROM announcements")).scalar() or ""
        mx_kln = s.execute(text("SELECT MAX(date) FROM klines")).scalar() or ""
        end_iso = max(mx_ann, mx_kln) or today_cn().isoformat()
        start_iso = (date.fromisoformat(end_iso) - timedelta(days=days - 1)).isoformat()

        # 真实开市日（行情源保证，已剔除节假日/休市）
        mkt = [r[0] for r in s.execute(text(
            "SELECT DISTINCT date FROM klines WHERE date BETWEEN :a AND :b ORDER BY date"
        ), {"a": start_iso, "b": end_iso}).fetchall()]

        # 公告按日计数
        cnt = dict(s.execute(text(
            "SELECT date, COUNT(*) FROM announcements "
            "WHERE date BETWEEN :a AND :b GROUP BY date"
        ), {"a": start_iso, "b": end_iso}).fetchall())

        if mkt:
            calendar, cal_src = mkt, "klines"
        else:
            calendar = weekday_range(date.fromisoformat(start_iso),
                                     date.fromisoformat(end_iso))
            cal_src = "weekday"

        missing = [d for d in calendar if (cnt.get(d) or 0) == 0]
        thin = [d for d in calendar if 0 < (cnt.get(d) or 0) < thin_below]
        covered = [d for d in calendar if (cnt.get(d) or 0) >= thin_below]

        total_ann = sum(cnt.get(d) or 0 for d in calendar)
        return {
            "window": {"start": start_iso, "end": end_iso, "days": days},
            "calendar_source": cal_src,
            "trading_days": len(calendar),
            "covered_days": len(covered),
            "missing_days": missing,          # 完全没数据 → 真缺口
            "thin_days": thin,                # 有但极少 → 疑似抓了一半
            "thin_below": thin_below,
            "announcements": total_ann,
            "coverage": round(len(covered) / len(calendar) * 100, 1) if calendar else 0.0,
            "verdict": "ok" if not missing and not thin else ("有缺口" if missing else "存疑"),
            # 有缺口时前端直接提示该做什么
            "fix": {"mode": "full", "lookback_days": days} if (missing or thin) else None,
        }
    finally:
        s.close()


# ---------------- K线（按需，元组顺序 [date,open,close,high,low,vol]） ----------------
def kline_payload(code: str, days: int = 120) -> dict:
    s = get_session()
    try:
        rows = s.execute(text("""
            SELECT date, open, close, high, low, volume FROM (
                SELECT date, open, close, high, low, volume,
                       ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) rn
                FROM klines WHERE code = :code
            ) WHERE rn <= :lim ORDER BY date
        """), {"code": code, "lim": days}).fetchall()
        return {"code": code, "klines": [
            [r[0], r[1], r[2], r[3], r[4], r[5]] for r in rows
        ]}
    finally:
        s.close()