# -*- coding: utf-8 -*-
"""桌面版入口 —— 把看板装进一个独立窗口，不再丢给浏览器。

窗口优先级：
  1. pywebview  —— 真正的原生窗口（走系统 WebView2），装了就用；
  2. Edge / Chrome 的 --app 模式 —— 无地址栏、无标签页，看着就是个 App；
  3. 系统默认浏览器 —— 最后兜底。

关掉窗口即停服务、退出进程。打包成 exe 后，数据落到 exe 同级的
data/ 与 cache/（持久），网页资源从打包内解出（只读）。

用法：
    python desktop_app.py            # 默认 8777
    python desktop_app.py --port 8899
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time

# ── 冻结（PyInstaller）时的路径修正 —— 必须赶在 import core 之前 ──
# 冻结后 ROOT 会指向临时解包目录（退出即删），所以数据目录要显式改到
# exe 同级；网页资源仍从解包目录读（只读，随 exe 分发）。
if getattr(sys, "frozen", False):
    _exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    _bundle = getattr(sys, "_MEIPASS", _exe_dir)
    os.environ.setdefault("STOCK_DATA_DIR", os.path.join(_exe_dir, "data"))
    os.environ.setdefault("STOCK_CACHE_DIR", os.path.join(_exe_dir, "cache"))
    os.environ.setdefault("STOCK_WEB_DIR", os.path.join(_bundle, "web"))

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

APP_TITLE = "野人体系 · 选股看板"


def _wait_health(port: int, timeout: float = 40.0) -> bool:
    """等后端起来（最多 timeout 秒）。"""
    import urllib.request
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def _browser_exe() -> str | None:
    """找一个能用 --app 模式的 Chromium 内核浏览器（Windows 必带 Edge）。"""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    lad = os.environ.get("LOCALAPPDATA", "")
    cands = [
        os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(lad, r"Google\Chrome\Application\chrome.exe"),
    ]
    return next((p for p in cands if p and os.path.exists(p)), None)


def open_window(url: str) -> str:
    """开窗口，阻塞到窗口关闭。返回用了哪种方式。"""
    # ① 原生窗口（pywebview）
    try:
        import webview  # type: ignore
        webview.create_window(APP_TITLE, url, width=1440, height=920,
                              min_size=(1000, 660))
        webview.start()
        return "pywebview"
    except Exception:
        pass

    # ② Edge/Chrome 的 --app 模式：独立进程 + 独立配置目录，无地址栏/标签页
    exe = _browser_exe()
    if exe:
        prof = os.path.join(os.environ.get("LOCALAPPDATA") or BASE,
                            "YerenStock", "win")
        os.makedirs(prof, exist_ok=True)
        proc = subprocess.Popen([
            exe, f"--app={url}", "--window-size=1440,920",
            f"--user-data-dir={prof}", "--no-first-run",
            "--no-default-browser-check", "--disable-features=Translate",
        ])
        proc.wait()          # 窗口关掉 → 进程退出 → 下面的服务一起收摊
        return "app-mode"

    # ③ 兜底：系统默认浏览器
    import webbrowser
    webbrowser.open(url)
    input("  已用默认浏览器打开。按回车结束服务…")
    return "browser"


def main() -> int:
    ap = argparse.ArgumentParser(description="野人体系 · 桌面版")
    ap.add_argument("--port", type=int, default=8777, help="看板端口")
    ap.add_argument("--keep-stale", action="store_true",
                    help="不清理占用端口的旧进程")
    ap.add_argument("--no-window", action="store_true",
                    help="只起后端并自检，不开窗口（打包后验证用）")
    ap.add_argument("--serve", action="store_true",
                    help="只跑后端、阻塞不弹窗（无头运行/调试）")
    args = ap.parse_args()

    if not args.keep_stale:
        try:
            import launcher
            launcher.kill_stale(args.port)
        except Exception:
            pass

    import server

    port = args.port
    if args.serve:
        print(f"  无头模式：http://127.0.0.1:{port}（Ctrl+C 停止）")
        server.main(port)          # 阻塞在主线程
        return 0

    threading.Thread(target=lambda: server.main(port), daemon=True).start()

    url = f"http://127.0.0.1:{port}"
    if not _wait_health(port):
        print(f"[ERROR] 后端没起来（端口 {port} 可能被占）。换端口重试："
              f"desktop_app.py --port 8899")
        return 1

    if args.no_window:
        # 自检：把路径与数据源状态打出来，确认打包后目录/资源都对
        from core import paths, datasource as ds
        from core import version
        print("[selfcheck] version =", version.APP_VERSION)
        print("[selfcheck] paths   =", paths.describe())
        print("[selfcheck] tdx     =", ds.tdx_status())
        print("[selfcheck] frozen  =", getattr(sys, "frozen", False))
        return 0

    how = open_window(url)
    print(f"[{how}] {url} —— 窗口已关闭，服务停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
