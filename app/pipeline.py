"""
编排层：抓取 → 规则分类 → 幂等入库 → 运行留痕

关键设计：
  - 幂等：ann_id 唯一键 + ON CONFLICT DO NOTHING，重复抓取不会产生脏数据，
          这是支持"定时自动跑"的前提（原站没有，所以才只能手动点）。
  - 批量：1000 条一批，7 万条数据可在数秒内写完。
  - 留痕：每次运行写 runs 表，成功/失败/耗时/新增数一目了然。
"""
from __future__ import annotations

import fcntl
import os
import time
import traceback
from datetime import date, datetime, timedelta
from contextlib import contextmanager
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .db import Announcement, Kline, Run, Stock, Session, get_session
from .fetchers import get_fetcher
from .fetchers.base import BaseFetcher
from .rules import get_engine
from .timeutil import now_naive, today_cn

BATCH_SIZE = 1000
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPDATE_LOCK_PATH = os.path.join(BASE_DIR, "data", "update.lock")


@contextmanager
def update_lock():
    """跨进程全局更新锁；覆盖手动、定时、补漏和直接 CLI 运行。"""
    os.makedirs(os.path.dirname(UPDATE_LOCK_PATH), exist_ok=True)
    fd = os.open(UPDATE_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def update_lock_held() -> bool:
    """检查是否存在真实的更新进程；锁释放后不会被旧 runs 记录误导。"""
    os.makedirs(os.path.dirname(UPDATE_LOCK_PATH), exist_ok=True)
    fd = os.open(UPDATE_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(fd)


# ---------------- 批量写入（幂等） ----------------
def _bulk_announcements(session: Session, rows: list[dict]) -> int:
    """批量插入公告，返回实际新增条数（重复自动忽略）"""
    if not rows:
        return 0
    stmt = sqlite_insert(Announcement).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=["ann_id"])
    result = session.execute(stmt)
    return result.rowcount or 0


def _bulk_klines(session: Session, rows: list[dict]) -> int:
    if not rows:
        return 0
    stmt = sqlite_insert(Kline).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=["code", "date"])
    result = session.execute(stmt)
    return result.rowcount or 0


def _bulk_stocks(session: Session, rows: list[dict]) -> None:
    if not rows:
        return
    stmt = sqlite_insert(Stock).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["code"],
        set_={
            "name": stmt.excluded.name,
            "board": stmt.excluded.board,
            "market_value": stmt.excluded.market_value,
            "updated_at": now_naive(),
        },
    )
    session.execute(stmt)


# ---------------- 主流程 ----------------
def run_pipeline(
    mode: str = "incremental",
    start: Optional[str] = None,
    end: Optional[str] = None,
    lookback_days: int = 2,
    fetcher: Optional[BaseFetcher] = None,
    with_klines: bool = True,
    session: Optional[Session] = None,
) -> dict:
    """
    执行一次完整流水线。

    mode: incremental（增量，默认近2天） / full（全量重扫，近 lookback_days 天）
    """
    own_session = session is None
    s = session or get_session()
    fetcher = fetcher or get_fetcher()
    engine = get_engine()

    # 日期区间
    # ★ today_cn() 而不是 date.today()：海外服务器 TZ=UTC 时会整体错位一天
    if end is None:
        end = today_cn().isoformat()
    if start is None:
        d1 = date.fromisoformat(end)
        start = (d1 - timedelta(days=max(1, lookback_days - 1))).isoformat()

    run = Run(mode=mode, status="running", started_at=now_naive())
    s.add(run)
    s.commit()

    t0 = time.time()
    stats = {"fetched": 0, "new": 0, "klines": 0, "stocks": 0}

    try:
        # ---- 1) 抓取 + 分类 + 入库 ----
        batch: list[dict] = []
        stock_map: dict[str, dict] = {}
        event_codes: set[str] = set()

        noise_on = engine.noise_enabled()
        for raw in fetcher.fetch_announcements(start, end):
            stats["fetched"] += 1
            # 噪声过滤（原 gen_dashboard.py 的 is_noise，例行公告直接丢）
            if noise_on and engine.is_noise(raw.title):
                stats.setdefault("noise", 0)
                stats["noise"] += 1
                continue
            category = engine.classify(raw.title, raw.summary)
            nums = engine.extract_numbers(f"{raw.title} {raw.summary}")

            batch.append({
                "ann_id": raw.ann_id,
                "code": raw.code,
                "name": raw.name,
                "title": raw.title,
                "date": raw.date,
                "category": category,
                "board": raw.board,
                "key_numbers": ",".join(nums) if nums else "",
                "url": raw.url,
                "summary": raw.summary,
                "created_at": now_naive(),
            })
            stock_map[raw.code] = {
                "code": raw.code, "name": raw.name,
                "board": raw.board, "market_value": raw.market_value,
                "updated_at": now_naive(),
            }
            # 只给「有事件」的股票同步 K 线（避免全市场全量拉取，省时省空间）
            if category != "other":
                event_codes.add(raw.code)

            if len(batch) >= BATCH_SIZE:
                stats["new"] += _bulk_announcements(s, batch)
                s.commit()
                batch.clear()

        if batch:
            stats["new"] += _bulk_announcements(s, batch)
            s.commit()
            batch.clear()

        # ---- 2) 股票主数据 ----
        if stock_map:
            _bulk_stocks(s, list(stock_map.values()))
            s.commit()
            stats["stocks"] = len(stock_map)

        # ---- 3) K 线（仅重点股票，按需） ----
        if with_klines and event_codes:
            k_start = (date.fromisoformat(end) - timedelta(days=120)).isoformat()
            kbuf: list[dict] = []
            for code in sorted(event_codes):
                for k in fetcher.fetch_klines(code, k_start, end):
                    kbuf.append({
                        "code": k.code, "date": k.date, "open": k.open,
                        "high": k.high, "low": k.low, "close": k.close,
                        "volume": k.volume, "change_pct": k.change_pct,
                    })
                    if len(kbuf) >= BATCH_SIZE:
                        stats["klines"] += _bulk_klines(s, kbuf)
                        s.commit()
                        kbuf.clear()
            if kbuf:
                stats["klines"] += _bulk_klines(s, kbuf)
                s.commit()

        # ---- 4) 重建看板数据文件（有变化才重建，避免白烧 CPU） ----
        rebuild_ms = 0
        if stats["new"] or stats["klines"] or mode == "full":
            from . import board_data

            _t1 = time.time()
            board_data.ensure_data_list_file(force=True)
            rebuild_ms = int((time.time() - _t1) * 1000)
            stats["rebuild_ms"] = rebuild_ms

        # ---- 5) 成功留痕 ----
        run.status = "success"
        run.finished_at = now_naive()
        run.duration_ms = int((time.time() - t0) * 1000)
        run.fetched = stats["fetched"]
        run.new_count = stats["new"]
        s.commit()

        try:
            fetcher.close()
        except Exception:  # noqa: BLE001
            pass

        return {
            "ok": True, "run_id": run.id, "mode": mode,
            "start": start, "end": end, **stats,
            "duration_ms": run.duration_ms,
        }

    except Exception as exc:  # noqa: BLE001
        s.rollback()
        run.status = "failed"
        run.finished_at = now_naive()
        run.duration_ms = int((time.time() - t0) * 1000)
        run.error = f"{exc}\n{traceback.format_exc()[-800:]}"
        s.commit()
        try:
            fetcher.close()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "run_id": run.id, "error": str(exc),
                "traceback": traceback.format_exc()[-800:]}

    finally:
        if own_session:
            s.close()


def run_with_retry(max_attempts: int = 3, delay_seconds: int = 60, **kw) -> dict:
    """失败自动重试（原站完全没有这个，跑挂了只能干瞪眼）"""
    import time as _t

    last: dict = {}
    for attempt in range(1, max_attempts + 1):
        last = run_pipeline(**kw)
        if last.get("ok"):
            last["attempt"] = attempt
            return last
        if attempt < max_attempts:
            _t.sleep(delay_seconds)
    last["attempt"] = max_attempts
    return last


def run_via_cli(
    mode: str = "incremental",
    start: Optional[str] = None,
    end: Optional[str] = None,
    lookback_days: Optional[int] = None,
    wait: bool = True,
    timeout: int = 3600,
    max_attempts: int = 3,
    retry_delay: int = 5,
) -> dict:
    """
    通过「独立子进程」执行更新。

    这是 1核1G 部署的关键：抓取（尤其 PDF 抽取）的内存峰值发生在子进程，
    跑完即释放，不会像原站那样常驻在服务进程里（原站 2G 机器实测 858MB）。

    wait=True  阻塞等待结果（供调度器使用）
    wait=False 立刻返回（供 API 手动触发使用）
    """
    import json as _json
    import subprocess
    import sys

    cmd = [sys.executable, "-m", "app.cli", "run", "--mode", mode,
           "--max-attempts", str(max_attempts), "--retry-delay", str(retry_delay)]
    if start:
        cmd += ["--start", start]
    if end:
        cmd += ["--end", end]
    if lookback_days:
        cmd += ["--lookback-days", str(lookback_days)]

    if not wait:
        # 输出落盘到 data/update.log（而非丢弃），方便生产环境排错
        log_dir = os.path.join(BASE_DIR, "data")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "update.log")
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(f"\n===== {datetime.now().isoformat()} 启动更新 mode={mode} =====\n")
            proc = subprocess.Popen(
                cmd, cwd=BASE_DIR, stdout=lf, stderr=lf, start_new_session=True,
            )
        # 关键：用守护线程 wait() 回收子进程。
        # 否则子进程结束后会变成 <defunct> 僵尸进程并逐渐堆积（实测确实出现了）。
        import threading

        threading.Thread(target=proc.wait, daemon=True).start()
        return {"ok": True, "pid": proc.pid, "mode": mode, "log": "data/update.log",
                "message": "已在独立进程启动更新（内存不占用服务进程）"}

    try:
        proc = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True,
                              text=True, timeout=timeout)
        lines = [l for l in (proc.stdout or "").strip().splitlines() if l.strip()]
        if lines:
            try:
                return _json.loads(lines[-1])
            except Exception:  # noqa: BLE001
                pass
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-500:],
            "stderr": (proc.stderr or "")[-600:],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"更新超时（>{timeout}s）"}


def is_running(session: Optional[Session] = None, stale_seconds: int = 21600) -> bool:
    """真实运行状态以文件锁为准；释放锁后自动回收遗留 running 记录。"""
    own = session is None
    s = session or get_session()
    try:
        if update_lock_held():
            return True
        rows = s.execute(
            select(Run).where(Run.status == "running").order_by(Run.id.asc())
        ).scalars().all()
        if rows:
            now = now_naive()
            for r in rows:
                r.status = "failed"
                r.finished_at = now
                r.duration_ms = int(max(0.0, (now - r.started_at).total_seconds()) * 1000) if r.started_at else None
                r.error = (r.error or "") + ("\n" if r.error else "") + "任务进程已结束，系统自动回收遗留 running 状态。"
            s.commit()
        return False
    finally:
        if own:
            s.close()
def latest_run(session: Optional[Session] = None) -> Optional[dict]:
    own = session is None
    s = session or get_session()
    try:
        r = s.execute(select(Run).order_by(Run.id.desc()).limit(1)).scalar_one_or_none()
        if not r:
            return None
        from .timeutil import iso_cn
        return {
            "id": r.id, "mode": r.mode, "status": r.status,
            "started_at": iso_cn(r.started_at),
            "finished_at": iso_cn(r.finished_at),
            "duration_ms": r.duration_ms, "fetched": r.fetched,
            "new_count": r.new_count, "error": r.error,
        }
    finally:
        if own:
            s.close()