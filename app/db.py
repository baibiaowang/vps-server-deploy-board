"""
数据层：SQLAlchemy 2.0 + SQLite（WAL 并发模式）

针对"性能与数据量"优先级的设计：
  - WAL 模式：读写不互斥，解决原站单线程阻塞问题
  - 复合索引：支撑 (日期,分类)、(板块,日期) 等高频筛选组合
  - ann_id 唯一键：天然幂等，重复抓取不会产生脏数据
"""
from __future__ import annotations

import os
from typing import Optional

from .timeutil import now_naive

from sqlalchemy import (
    Column, DateTime, Float, Index, Integer, String, Text,
    create_engine, event, func, select, text,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "board.db")
DB_URL = f"sqlite:///{DB_PATH}"


class Base(DeclarativeBase):
    pass


class Stock(Base):
    """股票主数据"""
    __tablename__ = "stocks"

    code = Column(String(16), primary_key=True)          # 000001
    name = Column(String(32), index=True, nullable=False)
    board = Column(String(16), index=True)               # 主板/创业板/科创板/北交所/其他
    market_value = Column(Float)                         # 市值（亿元）
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive)


class Announcement(Base):
    """公告（核心表）"""
    __tablename__ = "announcements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ann_id = Column(String(64), unique=True, index=True, nullable=False)  # 去重键
    code = Column(String(16), index=True, nullable=False)
    name = Column(String(32))
    title = Column(String(512), nullable=False)
    date = Column(String(10), index=True, nullable=False)      # YYYY-MM-DD
    category = Column(String(32), index=True, nullable=False)  # 分类 id
    board = Column(String(16), index=True)
    key_numbers = Column(String(256))                          # 关键数字（逗号分隔）
    url = Column(String(512))
    summary = Column(Text)
    created_at = Column(DateTime, default=now_naive)

    __table_args__ = (
        Index("idx_date_cat", "date", "category"),     # 高频：按日期+分类筛选
        Index("idx_board_date", "board", "date"),      # 高频：按板块+日期
        Index("idx_code_date", "code", "date"),        # 单股历史
    )


class Kline(Base):
    """K线（按标的懒加载，不再全量打包进 JS）"""
    __tablename__ = "klines"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(16), index=True, nullable=False)
    date = Column(String(10), nullable=False)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)
    change_pct = Column(Float)                          # 涨跌幅 %

    __table_args__ = (
        Index("idx_kline_code_date", "code", "date", unique=True),
    )


class Run(Base):
    """任务运行日志（可观测：跑挂了不再无人知晓）"""
    __tablename__ = "runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(16), nullable=False)           # incremental / full
    status = Column(String(16), index=True)             # running / success / failed
    started_at = Column(DateTime, default=now_naive)
    finished_at = Column(DateTime)
    fetched = Column(Integer, default=0)                # 抓取条数
    new_count = Column(Integer, default=0)              # 新增条数
    duration_ms = Column(Integer)
    peak_rss_mb = Column(Integer)        # 抓取峰值内存（1核1G 部署必须能观测）
    error = Column(Text)


class Favorite(Base):
    """自选收藏（看板人工沉淀，与抓取数据解耦）

    为什么单独建表而不是塞进 stocks：
      stocks 是抓取的客观数据，每次全量重扫都会被 upsert 覆盖；
      收藏是你的人工标注，必须独立存储，否则一次更新就被抹掉。

    code 唯一：同一只股票只留一条，重复收藏=改原因。
    """
    __tablename__ = "favorites"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(16), unique=True, index=True, nullable=False)
    name = Column(String(32), default="")
    reason = Column(Text, default="")                 # 收藏原因（简短自注）
    category = Column(String(32), default="")         # 收藏时的分类，便于回看语境
    created_at = Column(DateTime, default=now_naive, index=True)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive)


# ---------------- 引擎与会话 ----------------
engine = create_engine(DB_URL, future=True, echo=False)


@event.listens_for(Engine := engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _):  # noqa: N802
    """SQLite 性能调优"""
    cur = dbapi_conn.cursor()
    # --- 低内存（1核1G）调优 ---
    # 原站进程 ~858MB RSS，目标机仅 1G，这里把 SQLite 缓存压到 16MB：
    # 实测 1.3 万条公告 / 6.8 万条 K 线下查询仍为毫秒级（见 README 性能表）。
    cur.execute("PRAGMA journal_mode=WAL")     # 读写并发（配合单 writer）
    cur.execute("PRAGMA synchronous=NORMAL")   # 兼顾安全与速度
    cur.execute("PRAGMA cache_size=-16000")    # 16MB（默认 64MB 对 1G 机器过于奢侈）
    cur.execute("PRAGMA temp_store=FILE")      # 临时数据落盘而非驻留内存
    cur.execute("PRAGMA mmap_size=0")          # 关闭 mmap，避免占用非常驻内存
    cur.execute("PRAGMA busy_timeout=5000")    # 写锁等待，避免并发写直接报错
    cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _migrate() -> None:
    """轻量迁移：为已存在的库补齐新增列（避免重新灌数据）"""
    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(runs)")).fetchall()}
        if cols and "peak_rss_mb" not in cols:
            conn.execute(text("ALTER TABLE runs ADD COLUMN peak_rss_mb INTEGER"))
            conn.commit()


def init_db() -> None:
    """建表（幂等）+ 自动补齐新增列"""
    Base.metadata.create_all(engine)
    _migrate()


def get_session() -> Session:
    return SessionLocal()


def table_stats(session: Optional[Session] = None) -> dict:
    """各表行数，用于健康自检"""
    own = session is None
    s = session or SessionLocal()
    try:
        return {
            "stocks": s.execute(select(func.count()).select_from(Stock)).scalar() or 0,
            "announcements": s.execute(select(func.count()).select_from(Announcement)).scalar() or 0,
            "klines": s.execute(select(func.count()).select_from(Kline)).scalar() or 0,
            "runs": s.execute(select(func.count()).select_from(Run)).scalar() or 0,
        }
    finally:
        if own:
            s.close()