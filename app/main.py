"""
A股公告看板 v4 — 「保持原版布局」版（FastAPI）

与 v3 的区别：不再自造前端，直接托管线上原版 dashboard.html（字节一致），
只替换数据层：

    /                 → 原版看板（+ 服务端注入的「更新数据」按钮与动态标题）
    /data_list.js     → window.ANNO_LIST = [...]（与原版字段完全同构）
    /api/kline?code=  → {klines:[[日期,开,收,高,低,量]]}（原版已在用的按需加载）
    /lib/echarts.min.js → 1 年强缓存 + brotli 预压缩

针对 1核1G / 1~3Mbps 的三条硬约束：
  1. 更新跑独立子进程，抓取内存峰值不进服务进程
  2. data_list.js 落盘 + 构建期预压缩，请求期零 CPU
  3. K线按需返回，不再全量打包（3.2MB → 每只 ~7KB）
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from . import auth, board_data, inject
from .config import get_host, get_port, rules_config
from .timeutil import iso_cn
from .db import Favorite, get_session, init_db, table_stats
from .pipeline import is_running, latest_run, run_via_cli
from .rules import get_engine
from .scheduler import list_jobs, shutdown_scheduler, start_scheduler
from .static_assets import etag_for, pick_variant
from .timeutil import tz_report

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
DASHBOARD_PATH = os.path.join(WEB_DIR, "dashboard.html")
STATIC_MAX_AGE = 31536000          # /lib 下资源：1 年不变


# ---------------- 生命周期 ----------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    # 首屏数据文件：不存在就现生成一次（之后由每次更新任务重建）
    try:
        board_data.ensure_data_list_file()
    except Exception:                                    # noqa: BLE001
        pass
    yield
    shutdown_scheduler()


_DOCS_ENABLED = os.getenv("BOARD_ENABLE_DOCS", "0") == "1"

app = FastAPI(
    title="A股公告看板",
    description="原版布局 · 可配置规则 · 自动更新 · 1核1G/小带宽可跑",
    version="4.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _DOCS_ENABLED else None,
    redoc_url="/redoc" if _DOCS_ENABLED else None,
)


# ---------------- 响应工具（预压缩 + 缓存） ----------------
def _send_file(
    request: Request,
    path: str,
    media_type: str,
    cache_control: str,
    download_name: str | None = None,
) -> Response:
    """
    发送静态文件：优先 .br → .gz → 原始，并带 ETag / 304。

    预压缩产物由 tools/build_assets.py 生成；这里只负责挑最优那份，
    不做任何压缩计算（1 核机器跑不动实时 gzip 1MB 文件）。
    """
    if not os.path.exists(path):
        return JSONResponse({"error": f"not found: {os.path.basename(path)}"}, status_code=404)

    chosen, encoding = pick_variant(path, request.headers.get("accept-encoding", ""))
    etag = etag_for(chosen)
    inm = request.headers.get("if-none-match", "")
    if inm and inm == etag:
        r = Response(status_code=304)
        r.headers["ETag"] = etag
        r.headers["Cache-Control"] = cache_control
        r.headers["Vary"] = "Accept-Encoding"
        return r

    resp = FileResponse(chosen, media_type=media_type, filename=download_name)
    resp.headers["Cache-Control"] = cache_control
    resp.headers["ETag"] = etag
    resp.headers["Vary"] = "Accept-Encoding"
    if encoding:
        resp.headers["Content-Encoding"] = encoding
    return resp


# ---------------- 页面 ----------------
_LOGIN_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · A股公告看板</title>
<style>
 body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
   background:linear-gradient(135deg,#1e3a8a,#2563eb 60%,#3b82f6);min-height:100vh;
   display:flex;align-items:center;justify-content:center;margin:0}
 .box{background:#fff;padding:32px 36px;border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.18);width:320px}
 h1{font-size:19px;margin:0 0 6px;color:#1f2430}
 p{margin:0 0 20px;font-size:13px;color:#6b7280}
 input{width:100%;padding:11px 14px;border:1px solid #e6e8ef;border-radius:10px;font-size:14px;
   outline:none;box-sizing:border-box}
 input:focus{border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.1)}
 button{width:100%;margin-top:14px;padding:11px;border:none;border-radius:10px;background:#2563eb;
   color:#fff;font-size:15px;font-weight:600;cursor:pointer}
 button:hover{background:#1d4ed8}
 .err{color:#dc2626;font-size:13px;margin-top:12px;display:none}
</style></head><body><div class="box">
<h1>A股公告看板</h1><p>请输入访问密码</p>
<input id="p" type="password" placeholder="密码" autofocus>
<button onclick="go()">进入看板</button>
<div class="err" id="e"></div></div>
<script>
function go(){fetch('/api/login',{method:'POST',credentials:'same-origin',
 headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('p').value})})
 .then(r=>{if(r.ok){location='/'}else{document.getElementById('e').style.display='block';
   document.getElementById('e').textContent='密码错误'}})}
document.getElementById('p').addEventListener('keydown',e=>{if(e.key==='Enter')go()});
</script></body></html>"""

# 页面渲染结果缓存（24KB 的小文件，缓存后连注入都不用重复做）
_page_cache: dict = {}


def _compress_bytes(data: bytes, quality: int | None = None) -> dict:
    """把一份字节压成 br / gz 两个变体，缺 brotli 时自动只留 gz"""
    out = {"raw": data}
    try:
        import brotli
        out["br"] = brotli.compress(data, quality=quality or 11)
    except Exception:                                        # noqa: BLE001
        pass
    out["gz"] = gzip.compress(data, 9)
    return out


def _pick_variant(bodies: dict, accept_encoding: str) -> tuple[bytes, str | None]:
    """按 Accept-Encoding 挑一个变体；客户端不支持压缩时退回原始字节"""
    ae = (accept_encoding or "").lower()
    if "br" in ae and "br" in bodies:
        return bodies["br"], "br"
    if "gzip" in ae and "gz" in bodies:
        return bodies["gz"], "gzip"
    return bodies["raw"], None


def _render_dashboard() -> dict:
    """
    返回 {"raw","br","gz"} 三份字节。

    ★ 为什么页面要在这里自带压缩：
      页面是「读模板 + 服务端注入」动态生成的，走不到静态文件的预压缩通道，
      实测之前一直裸传 28KB（而它的 .br 只有 6.9KB）。
      压缩跟页面生成绑在一起 —— 只在数据变更时做一次，
      运行期纯粹是内存取值，1 核机器上零 CPU 开销。
    """
    disp = (rules_config().get("display") or {})
    key = (os.path.getmtime(DASHBOARD_PATH),
           board_data.read_meta().get("generated_at", ""),
           bool(disp.get("patch_category_colors", False)),
           bool(disp.get("manual_update_button", True)),
           bool(disp.get("dedupe_kline_requests", True)),
           bool(disp.get("favorites", True)))
    if _page_cache.get("key") == key:
        return _page_cache["bodies"]

    with open(DASHBOARD_PATH, "r", encoding="utf-8") as f:
        html = f.read()
    eng = get_engine()
    colors = {it["label"]: it.get("display_color")
              for it in eng.taxonomy_raw if it.get("display_color")}
    out = inject.render_dashboard(html, board_data.read_meta(), colors)
    bodies = _compress_bytes(out.encode("utf-8"))
    _page_cache.update(key=key, bodies=bodies)
    return bodies


@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/dashboard.html", methods=["GET", "HEAD"])
def page_dashboard(request: Request):
    if auth.auth_enabled() and not auth.verify_token(request.cookies.get(auth.COOKIE_NAME)):
        return HTMLResponse(_LOGIN_PAGE)

    bodies = _render_dashboard()
    body, enc = _pick_variant(bodies, request.headers.get("accept-encoding", ""))
    # ETag 按「实际发出的那份字节」算，所以不同编码各自有独立 ETag，
    # 不会出现拿 br 的 ETag 去校验 gzip 表示这种错配。
    etag = f'"{len(body):x}-{hashlib.md5(body).hexdigest()}"'
    base = {"ETag": etag, "Cache-Control": "no-cache",
            "Vary": "Accept-Encoding, Cookie"}
    if request.headers.get("if-none-match", "") == etag:
        return Response(status_code=304, headers=base)
    if enc:
        base["Content-Encoding"] = enc
    return Response(content=body, media_type="text/html; charset=utf-8",
                    headers=base)


# ---------------- 数据 ----------------
@app.api_route("/data_list.js", methods=["GET", "HEAD"])
def data_list_js(request: Request):
    """原版看板的唯一数据源：window.ANNO_LIST"""
    path = board_data.DATA_LIST_PATH
    if not os.path.exists(path):
        board_data.ensure_data_list_file(force=True)
    # 数据会变，所以 no-cache + ETag（变更时返回新内容，未变更 304 只花几百字节）
    return _send_file(request, path, "application/javascript; charset=utf-8",
                      "no-cache")


@app.api_route("/api/kline", methods=["GET", "HEAD"])
def api_kline(
    request: Request,
    code: str = Query(..., description="股票代码"),
    days: int = Query(120, ge=1, le=500),
    _: None = Depends(auth.require_auth),
):
    """
    单只股票 K 线（原版看板已在用）。

    ★ 元组顺序必须是 [日期, 开, 收, 高, 低, 量]：
      原版 JS 里 ohlc = k.map(x => [x[1], x[2], x[4], x[3]])  // open, close, low, high

    ★ 运行期压缩：K 线裸 4.5KB → brotli 1.3KB（-72%），每点一次股票就省一次传输。
      载荷很小，所以用低压缩等级（brotli quality=4），1 核机器上单次 <1ms，
      比省下的 1Mbps 传输时间（约 26ms）划算得多。
      低于 1KB 的响应不压（压缩比不划算，还白搭 CPU）。
    """
    disp = (rules_config().get("display") or {})
    n = int(disp.get("kline_days", days))
    raw = json.dumps(board_data.kline_payload(code, n),
                     ensure_ascii=False).encode("utf-8")
    if len(raw) >= 1024:
        body, enc = _pick_variant(_compress_bytes(raw, quality=4),
                                  request.headers.get("accept-encoding", ""))
    else:
        body, enc = raw, None
    hdr = {"Vary": "Accept-Encoding", "Cache-Control": "no-cache"}
    if enc:
        hdr["Content-Encoding"] = enc
    return Response(content=body, media_type="application/json; charset=utf-8",
                    headers=hdr)


@app.api_route("/api/meta", methods=["GET", "HEAD"])
def api_meta(_: None = Depends(auth.require_auth)):
    """数据范围与生成时间（页签标题用的就是它）"""
    meta = board_data.read_meta()
    if not meta:
        board_data.ensure_data_list_file(force=True)
        meta = board_data.read_meta()
    return meta


# ---------------- 运维接口 ----------------
@app.api_route("/api/health", methods=["GET", "HEAD"])
def api_health():
    s = get_session()
    try:
        return {
            "ok": True,
            "auth_enabled": auth.auth_enabled(),
            "running": is_running(s),
            "db": table_stats(s),
            "last_run": latest_run(s),
            "jobs": list_jobs(),
            # 需求②：部署完第一件事就是看这里，确认时区没配错
            "timezone": tz_report(),
            "board": board_data.read_meta(),
            # 把 naive datetime 按东八区展示，避免看起来像 UTC
            "server_now": iso_cn(__import__("datetime").datetime.now()),
        }
    finally:
        s.close()


class LoginIn(BaseModel):
    password: str


class RunIn(BaseModel):
    mode: str = "incremental"
    start: str | None = None
    end: str | None = None
    lookback_days: int | None = None


class BackfillIn(BaseModel):
    """补漏重扫：重新拉取最近 days 天（默认 90 = 约 3 个月）"""
    days: int = 90


class FavoriteIn(BaseModel):
    """收藏/改原因。同一只股票重复提交 = 覆盖原因，不产生重复行。"""
    code: str
    name: str = ""
    reason: str = ""
    category: str = ""


@app.post("/api/login")
def api_login(body: LoginIn, response: Response):
    if auth.login(response, body.password):
        return {"ok": True}
    raise HTTPException(status_code=401, detail="密码错误")


@app.post("/api/logout")
def api_logout(response: Response):
    auth.logout(response)
    return {"ok": True}


@app.api_route("/api/taxonomy", methods=["GET", "HEAD"])
def api_taxonomy(_: None = Depends(auth.require_auth)):
    return {"items": get_engine().taxonomy()}


@app.post("/api/rules/reload")
def api_rules_reload(_: None = Depends(auth.require_auth)):
    get_engine().reload()
    return {"ok": True, "items": get_engine().taxonomy()}


@app.api_route("/api/stats", methods=["GET", "HEAD"])
def api_stats(_: None = Depends(auth.require_auth)):
    s = get_session()
    try:
        eng = get_engine()
        by_cat = s.execute(__import__("sqlalchemy").text("""
            SELECT category, COUNT(*) FROM announcements
            WHERE category <> 'other' GROUP BY category
        """)).fetchall()
        total = s.execute(__import__("sqlalchemy").text(
            "SELECT COUNT(*) FROM announcements")).scalar() or 0
        n_stocks = s.execute(__import__("sqlalchemy").text("""
            SELECT COUNT(DISTINCT code) FROM announcements WHERE category <> 'other'
        """)).scalar() or 0
        return {
            "total_announcements": total,
            "event_stocks": n_stocks,
            "by_category": [{"id": c, "label": eng.label_of(c), "count": n}
                            for c, n in sorted(by_cat, key=lambda x: -x[1])],
        }
    finally:
        s.close()


@app.api_route("/api/runs", methods=["GET", "HEAD"])
def api_runs(limit: int = Query(1, ge=1, le=100), _: None = Depends(auth.require_auth)):
    """运行历史（默认只取 1 条，正好喂给看板上的更新按钮）"""
    from sqlalchemy import select

    from .db import Run
    s = get_session()
    try:
        rows = s.execute(select(Run).order_by(Run.id.desc()).limit(limit)).scalars().all()
        return {"running": is_running(s), "items": [{
            "id": r.id, "mode": r.mode, "status": r.status,
            # 库里的 DateTime 是东八区 naive 值，返回时补 +08:00
            "started_at": iso_cn(r.started_at),
            "finished_at": iso_cn(r.finished_at),
            "duration_ms": r.duration_ms, "fetched": r.fetched,
            "new_count": r.new_count, "peak_rss_mb": r.peak_rss_mb,
            "error": (r.error or "")[:300],
        } for r in rows]}
    finally:
        s.close()


@app.api_route("/api/jobs", methods=["GET", "HEAD"])
def api_jobs(_: None = Depends(auth.require_auth)):
    return {"enabled": bool(list_jobs()), "items": list_jobs()}


@app.post("/api/run")
def api_run(body: RunIn, _: None = Depends(auth.require_auth)):
    """
    手动触发更新（看板上「🔄 更新数据」按钮）。

    ★ 拉起独立子进程执行：抓取的内存峰值不进服务进程。
      这是 1核1G 能跑起来的前提（原站进程内起线程，2G 机器实测 858MB）。
    """
    s = get_session()
    try:
        if is_running(s):
            return JSONResponse({"ok": False, "error": "已有更新任务正在运行"},
                                status_code=409)
    finally:
        s.close()
    return run_via_cli(mode=body.mode, start=body.start, end=body.end,
                       lookback_days=body.lookback_days, wait=False)


@app.post("/api/board/rebuild")
def api_board_rebuild(_: None = Depends(auth.require_auth)):
    """强制重建 data_list.js（改了分类口径后用它刷新看板）"""
    board_data.ensure_data_list_file(force=True)
    _page_cache.clear()
    return {"ok": True, "meta": board_data.read_meta()}


def _default_gap_days() -> int:
    """遗漏检测的回看天数，取自 schedule.backfill.lookback_days（默认 90）

    ★ 必须定义在 api_gaps 之前：装饰器默认值在 import 时就求值，
      写在后面会 NameError（刚踩过）。
    """
    try:
        sc = (rules_config().get("schedule") or {})
        return int(((sc.get("backfill") or {}).get("lookback_days")) or 90)
    except Exception:
        return 90


@app.get("/api/gaps")
def api_gaps(days: int = Query(_default_gap_days(), ge=1, le=365),
             thin_below: int = Query(5, ge=1, le=1000),
             _: None = Depends(auth.require_auth)):
    """
    遗漏检测：最近 days 天里哪些真实交易日缺公告。

    解决的是"重拉三个月怕有遗漏"里的【怕】—— 光重拉不够，
    得能证明拉完之后确实没有洞。返回 missing_days（完全没数据）
    与 thin_days（有但少得可疑）两份清单。
    """
    return board_data.detect_gaps(days=days, thin_below=thin_below)


@app.post("/api/backfill")
def api_backfill(body: BackfillIn, _: None = Depends(auth.require_auth)):
    """
    补漏重扫：重新拉取最近 N 天（默认 90 天 ≈ 3 个月）。

    ★ 与「更新数据」的区别：
        更新数据 = incremental，只扫最近几天，快，日常用
        补漏重扫 = full + N 天回看，慢，但会把漏掉的日子重新拉一遍

    ★ 不需要先清空/重置：
        公告以 ann_id 做唯一键 + ON CONFLICT DO NOTHING，
        重拉区间内已存在的记录会被跳过，只有真正缺的才会补进来。
        所以这是【幂等补洞】，不是【推倒重来】，不会重复也不会丢数据。
    """
    s = get_session()
    try:
        if is_running(s):
            return JSONResponse({"ok": False, "error": "已有更新任务正在运行"},
                                status_code=409)
    finally:
        s.close()
    return run_via_cli(mode="full", lookback_days=body.days, wait=False)


# ---------------- 收藏夹 ----------------
def _fav_dict(r: Favorite) -> dict:
    return {
        "code": r.code, "name": r.name or "", "reason": r.reason or "",
        "category": r.category or "",
        "created_at": iso_cn(r.created_at), "updated_at": iso_cn(r.updated_at),
    }


@app.get("/api/favorites")
def api_favorites(_: None = Depends(auth.require_auth)):
    """收藏夹列表（按收藏时间倒序）"""
    s = get_session()
    try:
        rows = s.execute(select(Favorite).order_by(
            Favorite.created_at.desc())).scalars().all()
        return {"count": len(rows), "items": [_fav_dict(r) for r in rows]}
    finally:
        s.close()


@app.post("/api/favorites")
def api_favorite_add(body: FavoriteIn, _: None = Depends(auth.require_auth)):
    """收藏 / 改原因

    存独立的 favorites 表而不是写进 stocks：
      stocks 每次全量重扫都会被抓取结果覆盖，收藏属于人工标注，不能被更新抹掉。
    """
    s = get_session()
    try:
        f = s.execute(select(Favorite).where(
            Favorite.code == body.code)).scalar_one_or_none()
        if f is None:
            f = Favorite(code=body.code)
            s.add(f)
        f.name = (body.name or "").strip()[:32]
        f.reason = (body.reason or "").strip()[:500]
        f.category = (body.category or "").strip()[:32]
        s.commit()
        return {"ok": True, "item": _fav_dict(f)}
    finally:
        s.close()


@app.delete("/api/favorites/{code}")
def api_favorite_del(code: str, _: None = Depends(auth.require_auth)):
    """取消收藏"""
    s = get_session()
    try:
        f = s.execute(select(Favorite).where(
            Favorite.code == code)).scalar_one_or_none()
        if f:
            s.delete(f)
            s.commit()
        return {"ok": True, "code": code}
    finally:
        s.close()


# ---------------- 静态资源 ----------------
@app.api_route("/lib/{name}", methods=["GET", "HEAD"])
def lib_asset(name: str, request: Request):
    """/lib 下资源（echarts 等）1 年强缓存 —— 小带宽下二次打开完全不走网络"""
    if name not in ("echarts.min.js",):
        return JSONResponse({"error": "not found"}, status_code=404)
    path = os.path.join(WEB_DIR, "lib", name)
    return _send_file(request, path, "application/javascript; charset=utf-8",
                      f"public, max-age={STATIC_MAX_AGE}, immutable")


def main():
    import uvicorn
    uvicorn.run("app.main:app", host=get_host(), port=get_port(), reload=False)


if __name__ == "__main__":
    main()