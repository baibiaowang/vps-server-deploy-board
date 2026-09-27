"""
命令行入口（用于「独立进程」执行更新）

为什么必须独立进程：
  原站是 server.py 里 threading.Thread 跑更新，导致
    1) 抓取峰值内存常驻在服务进程内（2G 机器实测 858MB，1G 机器会 OOM）
    2) 1 核机器上抓取会拖慢在线访问
  现在由 API 通过 subprocess 拉起本文件，跑完进程退出，内存完全释放。

用法：
    python -m app.cli run --mode incremental
    python -m app.cli run --mode full --lookback-days 90
    python -m app.cli run --mode full --start 2026-08-27 --end 2026-08-28
    python -m app.cli seed
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import sys

# 保证直接 `python -m app.cli` 或以脚本方式运行都能 import 到 app 包
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def _mem_mb() -> float:
    """本次进程历史最大 RSS（MB）；Linux ru_maxrss 单位为 KB。"""
    try:
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:  # noqa: BLE001
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return round(int(line.split()[1]) / 1024, 1)
        except Exception:  # noqa: BLE001
            pass
    return -1.0


def _record_peak(mb: float) -> None:
    """把本次抓取的峰值内存写回 runs 表（供 /api/runs 展示，1G 机器上便于监控）"""
    try:
        from sqlalchemy import select

        from app.db import Run, get_session

        s = get_session()
        try:
            r = s.execute(select(Run).order_by(Run.id.desc()).limit(1)).scalar_one_or_none()
            if r is not None:
                r.peak_rss_mb = int(mb)
                s.commit()
        finally:
            s.close()
    except Exception:  # noqa: BLE001
        pass


def _limit_memory(max_mb: int | None) -> None:
    """
    给抓取进程加内存上限（保险丝）。

    1核1G 机器上，一旦抓取（尤其 PDF 抽取）失控，最坏情况是整台机器 OOM。
    限死在子进程上，超限只会让本次更新失败，常驻服务不受影响。
    通过环境变量 MAX_RSS_MB 开启，默认不限制。
    """
    if not max_mb:
        return
    try:
        import resource

        lim = int(max_mb) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
        print(f"[cli] 已设置内存上限 {max_mb} MB", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[cli] 内存上限设置失败（忽略）: {exc}", flush=True)


def cmd_run(args: argparse.Namespace) -> int:
    from app.db import init_db
    from app.pipeline import run_with_retry, update_lock
    from app.timeutil import tz_report

    _limit_memory(int(os.getenv("MAX_RSS_MB") or 0) or None)
    # 需求②：每次更新都打印时区，海外部署排错时一眼看出日期窗口有没有错位
    rep = tz_report()
    print(f"[cli] 时区={rep['resolved']} 现在={rep['now_cn']} "
          f"今天={rep['today_cn']} 与服务器TZ不一致={rep['server_tz_mismatch']}", flush=True)
    init_db()
    kw = {"mode": args.mode}
    if args.start:
        kw["start"] = args.start
    if args.end:
        kw["end"] = args.end
    if args.lookback_days:
        kw["lookback_days"] = args.lookback_days

    with update_lock() as acquired:
        if not acquired:
            result = {"ok": False, "error": "已有更新任务正在运行（全局更新锁）"}
            print(json.dumps(result, ensure_ascii=False))
            return 2
        result = run_with_retry(
            max_attempts=args.max_attempts,
            delay_seconds=args.retry_delay,
            **kw,
        )
    rss = _mem_mb()
    result["peak_rss_mb"] = rss
    _record_peak(rss)
    print(json.dumps(result, ensure_ascii=False))

    if not result.get("ok"):
        return 1
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    from app.db import init_db
    from app.fetchers.mock import MockFetcher
    from app.pipeline import run_pipeline

    init_db()
    result = run_pipeline(
        mode="full",
        start=args.start or "2026-08-27",
        end=args.end or "2026-08-28",
        fetcher=MockFetcher(),
        with_klines=True,
    )
    result["peak_rss_mb"] = round(_mem_mb(), 1)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def cmd_tz(args: argparse.Namespace) -> int:
    """时区自检（海外部署第一件事就是跑它）"""
    from app.timeutil import tz_report

    rep = tz_report()
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    if rep["server_tz_mismatch"]:
        print("\n⚠️ 服务器本地时区与业务时区不一致：")
        print("   本项目所有日期计算已强制使用东八区，功能不受影响；")
        print("   但若你还有别的脚本用 date.today()，它们会算错。")
        print("   建议同时在 systemd 里加 Environment=TZ=Asia/Shanghai。")
    if not rep["tzdata_ok"]:
        print("\n⚠️ 未找到 tzdata，已回落到固定 UTC+8 偏移（结果正确，但夏令时/历史时区不精确）。")
        print("   修复：apt install tzdata  或  pip install tzdata")
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    """强制重建 data_list.js（改完 rules.yaml 分类口径后跑一次）"""
    from app import board_data

    path = board_data.ensure_data_list_file(force=True)
    meta = board_data.read_meta()
    print(json.dumps({"path": path, "size": os.path.getsize(path), **meta},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """
    数据源连通性自检（需求②：海外部署必跑）

    验证巨潮 / 东财 / 腾讯三个上游是否可达，不写库。
    """
    import time as _t

    from app.fetchers import get_fetcher
    from app.timeutil import today_cn

    f = get_fetcher()
    print(f"数据源: {f.name}")
    end = today_cn().isoformat()
    start = (today_cn() - __import__("datetime").timedelta(days=1)).isoformat()
    t0 = _t.time()
    try:
        n = sum(1 for _ in f.fetch_announcements(start, end))
        print(f"  公告 {start}~{end}: 抓到 {n} 条  耗时 {_t.time()-t0:.1f}s")
    except Exception as exc:                              # noqa: BLE001
        print(f"  公告抓取失败: {exc}")
        print("  → 海外服务器常见原因：巨潮对境外 IP 限流/封禁。"
              "请在 rules.yaml 的 network.proxy 配代理。")
    for code in ("600000", "000001"):
        t1 = _t.time()
        try:
            rows = list(f.fetch_klines(code, "2000-01-01", end))
            print(f"  K线 {code}: {len(rows)} 条  耗时 {_t.time()-t1:.1f}s")
        except Exception as exc:                          # noqa: BLE001
            print(f"  K线 {code} 失败: {exc}")
    try:
        f.close()
    except Exception:                                     # noqa: BLE001
        pass
    return 0


def cmd_reclassify(args: argparse.Namespace) -> int:
    """
    按当前 rules.yaml 重算全部分类（改了分类口径后必须跑一次）

    场景：taxonomy 里的 id/关键词/优先级改了，库里存量数据还是旧分类，
    看板上就会显示旧标签（甚至显示英文 id）。本命令按新规则重刷一遍，
    同时把新纳入噪声的公告清掉。
    """
    from sqlalchemy import text

    from app.db import init_db, get_session
    from app.rules import get_engine

    init_db()
    eng = get_engine()
    s = get_session()
    try:
        rows = s.execute(text("SELECT id, title, summary FROM announcements")).fetchall()
        print(f"待重分类：{len(rows)} 条", flush=True)
        cat_map, del_ids = {}, []
        for aid, title, summary in rows:
            if eng.is_noise(title or ""):
                del_ids.append(aid)
                continue
            cat_map[aid] = eng.classify(title or "", summary or "")

        batch = 0
        for aid, cat in cat_map.items():
            s.execute(text("UPDATE announcements SET category=:c WHERE id=:i"),
                      {"c": cat, "i": aid})
            batch += 1
            if batch % 2000 == 0:
                s.commit()
                print(f"  已处理 {batch}/{len(cat_map)}", flush=True)
        s.commit()

        if del_ids:
            for i in range(0, len(del_ids), 500):
                chunk = del_ids[i:i + 500]
                ph = ",".join(f":d{j}" for j in range(len(chunk)))
                s.execute(text(f"DELETE FROM announcements WHERE id IN ({ph})"),
                          {f"d{j}": v for j, v in enumerate(chunk)})
            s.commit()

        from collections import Counter
        cnt = Counter(cat_map.values())
        print("\n新分类分布：")
        for cid, n in cnt.most_common():
            print(f"  {eng.label_of(cid):<16}{n:>7}")
        print(f"\n噪声清理：删除 {len(del_ids)} 条")
    finally:
        s.close()

    from app import board_data
    board_data.ensure_data_list_file(force=True)
    print(f"\n已重建 {board_data.DATA_LIST_PATH}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """部署自检：数据库完整性、核心表、时区与配置。"""
    from sqlalchemy import text
    from app.db import init_db, get_session, table_stats
    from app.timeutil import tz_report

    init_db()
    s = get_session()
    try:
        integrity = s.execute(text("PRAGMA integrity_check")).scalar()
        result = {
            "ok": integrity == "ok",
            "integrity": integrity,
            "db": table_stats(s),
            "timezone": tz_report(),
        }
    finally:
        s.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def cmd_gaps(args: argparse.Namespace) -> int:
    """
    遗漏检测：最近 N 天里哪些真实交易日缺公告。

    用法：
      python -m app.cli gaps              # 默认回看 90 天
      python -m app.cli gaps --days 180   # 回看半年

    发现缺口就跑：python -m app.cli run --mode full --lookback-days 90
    （幂等补洞，不用先清空）
    """
    from app import board_data
    from app.inject import default_backfill_days

    days = args.days or default_backfill_days()
    g = board_data.detect_gaps(days=days, thin_below=args.thin_below)
    w = g["window"]
    print(f"回看窗口: {w['start']} ~ {w['end']}（{w['days']} 天）")
    print(f"交易日口径: {g['calendar_source']}  共 {g['trading_days']} 个交易日")
    print(f"公告总数: {g['announcements']}   覆盖率: {g['coverage']}%")

    if g["verdict"] == "ok":
        print(f"\n✓ 无缺口：{g['covered_days']}/{g['trading_days']} 个交易日都有数据")
        return 0

    if g["missing_days"]:
        print(f"\n✗ 完全缺数据的交易日（{len(g['missing_days'])} 天）：")
        for d in g["missing_days"]:
            print(f"    {d}")
    if g["thin_days"]:
        print(f"\n⚠ 数据偏少的交易日（<{g['thin_below']} 条，{len(g['thin_days'])} 天）：")
        for d in g["thin_days"]:
            print(f"    {d}")

    print("\n补漏命令（幂等，不会重复也不会清空）：")
    print(f"  python -m app.cli run --mode full --lookback-days {days}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="A股公告看板 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="执行一次更新")
    p_run.add_argument("--mode", default="incremental", choices=["incremental", "full"])
    p_run.add_argument("--start", default=None)
    p_run.add_argument("--end", default=None)
    p_run.add_argument("--lookback-days", type=int, default=None)
    p_run.add_argument("--max-attempts", type=int, default=3)
    p_run.add_argument("--retry-delay", type=int, default=5)
    p_run.set_defaults(func=cmd_run)

    p_seed = sub.add_parser("seed", help="灌入示例数据")
    p_seed.add_argument("--start", default=None)
    p_seed.add_argument("--end", default=None)
    p_seed.set_defaults(func=cmd_seed)

    p_tz = sub.add_parser("tz", help="时区自检（海外部署必跑）")
    p_tz.set_defaults(func=cmd_tz)

    p_rb = sub.add_parser("rebuild", help="重建 data_list.js")
    p_rb.set_defaults(func=cmd_rebuild)

    p_pb = sub.add_parser("probe", help="数据源连通性自检（海外部署必跑）")
    p_pb.set_defaults(func=cmd_probe)

    p_rc = sub.add_parser("reclassify", help="按 rules.yaml 重算全部分类")
    p_rc.set_defaults(func=cmd_reclassify)

    p_ck = sub.add_parser("check", help="部署自检：数据库/时区/核心表")
    p_ck.set_defaults(func=cmd_check)

    p_gap = sub.add_parser("gaps", help="遗漏检测：最近 N 天哪些交易日缺公告")
    p_gap.add_argument("--days", type=int, default=None,
                       help="回看天数，默认取 schedule.backfill.lookback_days（90）")
    p_gap.add_argument("--thin-below", type=int, default=5,
                       help="少于该条数的交易日算'存疑'，默认 5")
    p_gap.set_defaults(func=cmd_gaps)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())