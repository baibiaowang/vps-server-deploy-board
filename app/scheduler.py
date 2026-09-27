"""
调度层：自动定时更新（替代原站"手动点按钮"）

调度规则写在 config/rules.yaml 的 schedule 段，改配置即可改时间，无需改代码：
    incremental.cron = "30 18 * * 1-5"   # 交易日收盘后增量更新
    full_rescan.cron = "0 2 * * 0"       # 每周日全量重扫
    retry.max_attempts = 3               # 失败自动重试
"""
from __future__ import annotations

import logging
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import get_schedule_config
from .pipeline import run_via_cli

log = logging.getLogger("scheduler")

_scheduler: Optional[BackgroundScheduler] = None


def _make_job(mode: str, lookback_days: int, retry: dict):
    max_attempts = int((retry or {}).get("max_attempts", 3))
    delay_seconds = int((retry or {}).get("delay_seconds", 60))

    def _job():
        log.info("[scheduler] 开始执行 %s 更新（lookback=%s 天，独立进程）",
                 mode, lookback_days)
        # 独立子进程执行：抓取峰值内存不进入服务进程（1核1G 部署的关键）
        result = run_via_cli(mode=mode, lookback_days=lookback_days, wait=True,
                             max_attempts=max_attempts, retry_delay=delay_seconds)
        if result.get("ok"):
            log.info("[scheduler] %s 更新完成：抓取 %s 条，新增 %s 条，耗时 %sms，峰值内存 %sMB",
                     mode, result.get("fetched"), result.get("new"),
                     result.get("duration_ms"), result.get("peak_rss_mb"))
        else:
            log.error("[scheduler] %s 更新失败：%s", mode,
                      result.get("error") or result.get("stderr"))
        return result

    _job.__name__ = f"job_{mode}"
    return _job


def start_scheduler() -> Optional[BackgroundScheduler]:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    cfg = get_schedule_config()
    if not cfg.get("enabled", True):
        log.info("[scheduler] 未启用（schedule.enabled=false）")
        return None

    # ★ 用 timeutil.cn_tz() 而不是字符串：镜像缺 tzdata 时 APScheduler 会直接崩，
    #   而 cn_tz() 有 +08:00 兜底，保证 cron 永远按东八区触发。
    from .timeutil import cn_tz, tz_name

    tz = cn_tz()
    log.info("[scheduler] 调度时区：%s（UTC%+g）", tz_name(),
             (tz.utcoffset(__import__("datetime").datetime.now()).total_seconds() / 3600)
             if tz.utcoffset(__import__("datetime").datetime.now()) else 8)
    retry = cfg.get("retry", {}) or {}
    inc = cfg.get("incremental", {}) or {}
    full = cfg.get("full_rescan", {}) or {}

    sched = BackgroundScheduler(timezone=tz)

    if inc.get("cron"):
        sched.add_job(
            _make_job("incremental", int(inc.get("lookback_days", 2)), retry),
            CronTrigger.from_crontab(inc["cron"], timezone=tz),
            id="incremental", name="增量更新",
            coalesce=True, max_instances=1, misfire_grace_time=3600,
        )
        log.info("[scheduler] 已注册增量更新：%s (%s)", inc["cron"], tz)

    if full.get("cron"):
        sched.add_job(
            _make_job("full", int(full.get("lookback_days", 90)), retry),
            CronTrigger.from_crontab(full["cron"], timezone=tz),
            id="full_rescan", name="全量重扫",
            coalesce=True, max_instances=1, misfire_grace_time=3600,
        )
        log.info("[scheduler] 已注册全量重扫：%s (%s)", full["cron"], tz)

    sched.start()
    _scheduler = sched
    return sched


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("[scheduler] 已停止")


def list_jobs() -> list[dict]:
    """供 API 展示调度状态（原站完全看不到调度是否在跑）"""
    if _scheduler is None:
        return []
    out = []
    for job in _scheduler.get_jobs():
        out.append({
            "id": job.id,
            "name": job.name,
            "cron": str(job.trigger),
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
        })
    return out


