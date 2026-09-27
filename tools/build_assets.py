"""
构建期资源处理（需求③：1~3Mbps 小带宽 + 1核 CPU）

做的事：
  1. brotli + gzip 预压缩 web/ 与 data/board/ 下所有文本资源
     —— 运行期只挑现成的产物发送，CPU 开销接近于 0
  2. 可选的 ECharts 按需裁剪（1006KB → ~400KB）
     需要 Node.js，没有就跳过，功能不受影响

用法：
    python tools/build_assets.py              # 预压缩
    python tools/build_assets.py --echarts    # 额外裁剪 ECharts
    python tools/build_assets.py --report     # 只打印体积报告
"""
from __future__ import annotations

import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

WEB_DIR = os.path.join(BASE_DIR, "web")
GEN_DIR = os.path.join(BASE_DIR, "data", "board")

# 需要在看板里用到的 ECharts 组件（按原版 dashboard.html 实际用到的来裁剪）
ECHARTS_MODULES = """
import * as echarts from 'echarts/core';
import { CandlestickChart, LineChart, BarChart } from 'echarts/charts';
import {
  GridComponent, TooltipComponent, DataZoomComponent, MarkPointComponent,
  MarkLineComponent, AxisPointerComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

echarts.use([
  CandlestickChart, LineChart, BarChart,
  GridComponent, TooltipComponent, DataZoomComponent,
  MarkPointComponent, MarkLineComponent, AxisPointerComponent,
  CanvasRenderer,
]);
window.echarts = echarts;
"""


def _human(n: int) -> str:
    return f"{n/1024:.1f} KB" if n < 1024 * 1024 else f"{n/1024/1024:.2f} MB"


def compress_all(force: bool = False) -> None:
    from app.static_assets import precompress_tree

    print("=" * 68)
    print("预压缩静态资源（brotli + gzip）")
    print("=" * 68)
    rows = []
    for root in (WEB_DIR, GEN_DIR):
        if not os.path.isdir(root):
            continue
        for r in precompress_tree(root, force=force):
            r["root"] = os.path.basename(root)
            rows.append(r)

    if not rows:
        print("  没有需要压缩的文件（或压缩产物已是最新）")
        return

    print(f"{'文件':<28}{'原始':>12}{'gzip':>12}{'brotli':>12}   节省")
    print("-" * 68)
    tot_src = tot_gz = tot_br = 0
    for r in sorted(rows, key=lambda x: -x["source"]):
        src, gz = r["source"], r.get("gz", 0)
        br = r.get("br", 0)
        tot_src += src
        tot_gz += gz
        tot_br += br
        save = (1 - br / src) * 100 if br else 0
        print(f"{r.get('path','')[:28]:<28}{_human(src):>12}{_human(gz):>12}"
              f"{_human(br):>12}   {save:.0f}%")
    print("-" * 68)
    if tot_src:
        print(f"{'合计':<28}{_human(tot_src):>12}{_human(tot_gz):>12}{_human(tot_br):>12}"
              f"   {(1-tot_br/tot_src)*100:.0f}%")
    print()


def build_echarts() -> bool:
    """裁剪 ECharts（需要 Node.js + pnpm/npm）"""
    src = os.path.join(WEB_DIR, "lib", "echarts.min.js")
    if not os.path.exists(src):
        print("未找到 web/lib/echarts.min.js，跳过")
        return False
    if not _has_node():
        print("未检测到 Node.js，跳过 ECharts 裁剪（不影响功能，只是文件大一些）")
        return False

    work = os.path.join(BASE_DIR, "data", ".echarts_build")
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, "entry.js"), "w", encoding="utf-8") as f:
        f.write(ECHARTS_MODULES)
    with open(os.path.join(work, "package.json"), "w", encoding="utf-8") as f:
        f.write('{"name":"eb","private":true,"type":"module"}')

    print("安装 echarts + esbuild（首次较慢）…")
    pm = "pnpm" if _has("pnpm") else "npm"
    r = subprocess.run([pm, "add", "echarts", "esbuild"], cwd=work,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"依赖安装失败：{r.stderr[-500:]}")
        return False

    out = os.path.join(work, "echarts.custom.js")
    r = subprocess.run(
        [os.path.join(work, "node_modules", ".bin", "esbuild"),
         os.path.join(work, "entry.js"),
         "--bundle", "--minify", "--format=iife",
         "--target=es2015", f"--outfile={out}"],
        cwd=work, capture_output=True, text=True,
    )
    if r.returncode != 0 or not os.path.exists(out):
        print(f"打包失败：{r.stderr[-500:]}")
        return False

    old, new = os.path.getsize(src), os.path.getsize(out)
    if new >= old:
        print(f"裁剪后没有变小（{_human(old)} → {_human(new)}），保留原文件")
        return False

    backup = src + ".full.bak"
    if not os.path.exists(backup):
        os.replace(src, backup)
    os.replace(out, src)
    print(f"ECharts 裁剪完成：{_human(old)} → {_human(new)} "
          f"（省 {(1-new/old)*100:.0f}%）")
    print(f"完整版已备份到 {os.path.basename(backup)}")
    return True


def _has(cmd: str) -> bool:
    try:
        subprocess.run([cmd, "--version"], capture_output=True, timeout=10)
        return True
    except Exception:  # noqa: BLE001
        return False


def _has_node() -> bool:
    return _has("node") and (_has("npm") or _has("pnpm"))


def report() -> None:
    """体积与带宽耗时报告（按 1Mbps / 3Mbps 估算首屏）"""
    print("=" * 68)
    print("首屏体积与加载耗时估算")
    print("=" * 68)
    items = [
        ("看板 HTML", os.path.join(WEB_DIR, "dashboard.html")),
        ("data_list.js", os.path.join(GEN_DIR, "data_list.js")),
        ("echarts.min.js", os.path.join(WEB_DIR, "lib", "echarts.min.js")),
    ]
    tot_raw = tot_br = 0
    print(f"{'资源':<20}{'原始':>12}{'首屏传输(br)':>16}")
    print("-" * 68)
    for label, p in items:
        if not os.path.exists(p):
            print(f"{label:<20}{'(未生成)':>12}")
            continue
        raw = os.path.getsize(p)
        brp = p + ".br"
        br = os.path.getsize(brp) if os.path.exists(brp) else raw
        tot_raw += raw
        tot_br += br
        print(f"{label:<20}{_human(raw):>12}{_human(br):>16}")
    print("-" * 68)
    print(f"{'合计':<20}{_human(tot_raw):>12}{_human(tot_br):>16}")
    if tot_br:
        for mbps in (1, 3):
            # 1 Mbps ≈ 125 KB/s，再打个 0.85 的协议开销折损
            secs = tot_br / 1024 / (mbps * 125) / 0.85
            print(f"  {mbps} Mbps 首次加载（无缓存）≈ {secs:.1f} 秒")
        print("  之后：echarts 走 1 年强缓存，data_list.js 走 ETag 304，"
              "二次打开通常 < 0.5 秒")
    print()


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--echarts", action="store_true", help="裁剪 ECharts")
    ap.add_argument("--force", action="store_true", help="强制重新压缩")
    ap.add_argument("--report", action="store_true", help="只打印体积报告")
    args = ap.parse_args()

    if args.report:
        report()
        return 0

    if args.echarts:
        build_echarts()
        print()
    compress_all(force=args.force)
    report()
    return 0


if __name__ == "__main__":
    sys.exit(main())