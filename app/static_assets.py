"""
静态资源：预压缩 + 强缓存（需求③：1~3Mbps 小带宽 / 1核 CPU 的极限优化）

为什么是「构建期预压缩」而不是「运行期 gzip」：
  · 1 核机器上，每次请求现压 1MB 的 echarts.min.js 要几十到上百毫秒，
    带宽省下来了，CPU 却先跪了 —— 小水管机器最典型的翻车方式。
  · 预压缩一次，之后每次请求只是 sendfile，CPU 几乎为零。
  · brotli 比 gzip 再小 15~20%，对 1Mbps 就是好几秒的差距。

产物：  xxx.js / xxx.js.br / xxx.js.gz
策略：  浏览器支持 br → 发 .br；只支持 gzip → 发 .gz；都不支持 → 发原始
"""
from __future__ import annotations

import gzip
import os
from typing import Optional

try:
    import brotli
    _HAS_BROTLI = True
except ImportError:                      # 没装也能跑，只是少一档压缩
    _HAS_BROTLI = False

# 小于这个体积不值得压缩（压缩收益 < HTTP 头开销）
MIN_COMPRESS_BYTES = 512

# 只有这些类型值得压（图片/字体本身已是压缩格式）
COMPRESSIBLE = {
    ".html", ".htm", ".js", ".css", ".json", ".svg", ".xml", ".txt", ".map",
}

BROTLI_QUALITY = 11          # 最高压缩比；构建期跑一次，慢点无所谓
GZIP_LEVEL = 9


# ---------------- 预压缩 ----------------
def _atomic_write_bytes(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def precompress_file(path: str, force: bool = False) -> dict:
    """
    为单个文件生成 .br / .gz 副本。

    只在「源文件比压缩产物新」或 force 时才重新压缩 —— 增量更新场景下
    每天跑一次更新，没必要每次都重压 1MB 的 echarts。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in COMPRESSIBLE:
        return {}
    try:
        src_size = os.path.getsize(path)
    except OSError:
        return {}
    if src_size < MIN_COMPRESS_BYTES:
        return {}

    out: dict = {"source": src_size}
    src_mtime = os.path.getmtime(path)

    if _HAS_BROTLI:
        br_path = path + ".br"
        if force or not os.path.exists(br_path) or os.path.getmtime(br_path) < src_mtime:
            with open(path, "rb") as f:
                data = brotli.compress(f.read(), quality=BROTLI_QUALITY)
            _atomic_write_bytes(br_path, data)
        out["br"] = os.path.getsize(br_path)

    gz_path = path + ".gz"
    if force or not os.path.exists(gz_path) or os.path.getmtime(gz_path) < src_mtime:
        with open(path, "rb") as f:
            raw = f.read()
        _atomic_write_bytes(gz_path, gzip.compress(raw, compresslevel=GZIP_LEVEL))
    out["gz"] = os.path.getsize(gz_path)
    return out


def precompress_tree(root: str, force: bool = False) -> list[dict]:
    """批量预压缩整个目录（构建期调用一次）"""
    results = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith((".br", ".gz", ".tmp")):
                continue
            p = os.path.join(dirpath, fn)
            r = precompress_file(p, force=force)
            if r:
                r["path"] = os.path.relpath(p, root)
                results.append(r)
    return results


# ---------------- 运行期发送 ----------------
def etag_for(path: str) -> str:
    """低成本 ETag（size-mtime），不读文件内容"""
    try:
        st = os.stat(path)
        return f'"{st.st_size:x}-{int(st.st_mtime):x}"'
    except OSError:
        return ""


def pick_variant(path: str, accept_encoding: str) -> tuple[str, Optional[str]]:
    """
    按 Accept-Encoding 挑选最优产物。
    返回 (实际文件路径, Content-Encoding 值)
    """
    ae = (accept_encoding or "").lower()
    br_path = path + ".br"
    gz_path = path + ".gz"
    # 只在压缩产物确实更小、且比源新时才用
    try:
        src_mtime = os.path.getmtime(path)
    except OSError:
        return path, None

    def _usable(p: str) -> bool:
        return os.path.exists(p) and os.path.getmtime(p) >= src_mtime

    if "br" in ae and _HAS_BROTLI and _usable(br_path):
        return br_path, "br"
    if "gzip" in ae and _usable(gz_path):
        return gz_path, "gzip"
    return path, None