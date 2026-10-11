# -*- coding: utf-8 -*-
"""模拟安卓启动路径，验证打进 APK 的那份 Python 代码能跑起来。

不依赖 Chaquopy：直接调 android_boot.start()（App 里走的就是这个入口），
数据目录/页面目录按安卓的方式从外部注入，然后把服务真的 curl 一遍。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

PROJ = r"C:\Users\macro\Desktop\杂物\股票"
SYNCED = os.path.join(PROJ, "android", "app", "src", "main", "python")
APK = os.path.join(PROJ, "android", "app", "build", "outputs", "apk",
                   "debug", "app-debug.apk")

ok_cnt = 0
bad_cnt = 0


def ok(cond, msg):
    global ok_cnt, bad_cnt
    if cond:
        ok_cnt += 1
        print("  OK  " + msg)
    else:
        bad_cnt += 1
        print("  XX  " + msg)


def main():
    sim = tempfile.mkdtemp(prefix="andsim_")
    files_dir = os.path.join(sim, "files")
    cache_dir = os.path.join(sim, "cache")
    os.makedirs(files_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)

    print("[1] 从 APK 里解出 assets（模拟 MainActivity.extractAssets）")
    # MainActivity.knownAssets 声明的关键资源 —— 必须与 APK 内的实际路径对得上，
    # 否则清单会静默失配（v1.12.0 的 404 就是这类问题）。
    # mobile.html 是 App 真正加载的页面，必须在清单里。
    KNOWN = ["assets/web/index.html", "assets/web/mobile.html",
             "assets/defaults/strategy.json"]
    assets_dir = os.path.join(files_dir, "assets")
    web_dir = os.path.join(assets_dir, "web")
    os.makedirs(web_dir, exist_ok=True)
    with zipfile.ZipFile(APK) as z:
        names = set(z.namelist())
        for k in KNOWN:
            ok(k in names, f"APK 内含 MainActivity 清单声明的 {k}")
        entries = [n for n in names if n.startswith("assets/")]
        ok(bool(entries), f"APK assets 条目: {len(entries)} 个")
        for n in entries:
            if z.getinfo(n).is_dir():
                continue
            rel = n[len("assets/"):]
            out = os.path.join(assets_dir, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with z.open(n) as src, open(out, "wb") as dst:
                dst.write(src.read())
    idx = os.path.join(web_dir, "index.html")
    ok(os.path.exists(idx) and os.path.getsize(idx) > 1000,
       f"index.html 落盘 ({os.path.getsize(idx)} 字节)")
    ok(os.path.exists(os.path.join(assets_dir, "defaults", "strategy.json")),
       "默认策略 strategy.json 随包下发")

    print("[2] 以 App 的入口启动服务")
    sys.path.insert(0, SYNCED)
    import android_boot

    port = android_boot.start(files_dir, cache_dir, 8777, assets_dir)
    ok(port > 0, f"服务启动成功，端口 {port}")
    if port <= 0:
        print(android_boot.last_error())
        return 1

    print("[3] 诊断信息")
    print("     " + android_boot.diag().replace("\n", "\n     "))

    base = f"http://127.0.0.1:{port}"
    print("[4] 真实 HTTP 验证")
    with urllib.request.urlopen(base + "/", timeout=20) as r:
        page_bytes = r.read()
        status = r.status
    ok(status == 200 and b"<html" in page_bytes[:400].lower(), "首页返回 HTML")
    with open(idx, "rb") as f:
        apk_html = f.read()
    ok(page_bytes == apk_html,
       f"服务返回的页面与 APK 内资源逐字节一致 ({len(page_bytes)}B vs {len(apk_html)}B)")
    ok(len(page_bytes) == os.path.getsize(os.path.join(PROJ, "web", "index.html")),
       "与桌面 web/index.html 同源同版本")

    # App 实际加载的是 mobile.html —— 必须也能取到、且与 APK 内资源一致
    with urllib.request.urlopen(base + "/mobile.html", timeout=20) as r:
        mob_bytes = r.read()
        mob_status = r.status
    ok(mob_status == 200 and b"<html" in mob_bytes[:400].lower(),
       f"mobile.html 可访问（{len(mob_bytes)}B）")
    with open(os.path.join(web_dir, "mobile.html"), "rb") as f:
        apk_mob = f.read()
    ok(mob_bytes == apk_mob, "mobile.html 与 APK 内资源逐字节一致")
    ok(len(mob_bytes) == os.path.getsize(os.path.join(PROJ, "web", "mobile.html")),
       "与桌面 web/mobile.html 同源同版本")

    with urllib.request.urlopen(base + "/api/health", timeout=20) as r:
        h = json.loads(r.read().decode("utf-8"))
    ok(h.get("ok") is True, "健康检查通过")
    p = h.get("paths") or {}
    ok(os.path.normcase(p.get("data_dir", "")) == os.path.normcase(
        os.path.join(files_dir, "data")), f"数据目录指向 App 私有目录: {p.get('data_dir')}")
    ok(p.get("web_exists") is True, "Python 侧认得页面目录")
    ok(p.get("data_writable") is True, "数据目录可写")
    print(f"     版本: {h.get('version')}")

    print("[5] 策略配置首启落盘")
    ok(os.path.exists(os.path.join(files_dir, "data", "strategy.json")),
       "strategy.json 已从只读区复制到可写区")

    print("[6] 前端 API 冒烟（不依赖外网的那几个）")
    for api in ["/api/config", "/api/sources", "/api/portfolio", "/api/has-cache",
                "/api/live"]:
        try:
            with urllib.request.urlopen(base + api, timeout=25) as r:
                body = r.read()
            ok(r.status in (200, 204), f"{api} → {r.status} ({len(body)}B)")
        except Exception as e:
            ok(False, f"{api} 失败: {type(e).__name__}: {e}")

    print(f"\n===== {ok_cnt} 通过 / {bad_cnt} 失败 =====")
    shutil.rmtree(sim, ignore_errors=True)
    return 1 if bad_cnt else 0


if __name__ == "__main__":
    sys.exit(main())
