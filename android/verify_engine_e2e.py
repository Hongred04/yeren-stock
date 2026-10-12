# -*- coding: utf-8 -*-
"""端到端：用打进 APK 的那份代码跑一次完整选股，验证抓数/计算/接口全链路。

跑的是 android_boot.start() 拉起的那份代码（和手机上完全一致），
数据落在临时目录，模拟 App 私有空间。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request

# 从脚本位置反推工程根（脚本在 <工程根>/android/ 下），不写死绝对路径
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYNCED = os.path.join(PROJ, "android", "app", "src", "main", "python")
APK = os.path.join(PROJ, "android", "app", "build", "outputs", "apk",
                   "debug", "app-debug.apk")

sys.path.insert(0, SYNCED)


def post(base, path, payload):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def get(base, path, timeout=60):
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        if r.status == 204:
            return None
        return json.loads(r.read().decode())


def main() -> int:
    import zipfile
    sim = tempfile.mkdtemp(prefix="ande2e_")
    files_dir = os.path.join(sim, "files")
    cache_dir = os.path.join(sim, "cache")
    assets_dir = os.path.join(files_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)

    # 复刻 MainActivity 的解包动作
    with zipfile.ZipFile(APK) as z:
        for n in z.namelist():
            if not n.startswith("assets/") or z.getinfo(n).is_dir():
                continue
            out = os.path.join(assets_dir, n[len("assets/"):])
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with z.open(n) as src, open(out, "wb") as dst:
                dst.write(src.read())

    import android_boot
    port = android_boot.start(files_dir, cache_dir, 8777, assets_dir)
    if port <= 0:
        print("启动失败:\n" + android_boot.last_error())
        return 1
    base = f"http://127.0.0.1:{port}"
    print(f"服务端口 {port}\n")

    print("触发一次完整扫描（抓东财/腾讯行情 → 打分 → 选股）…")
    print(" ", post(base, "/api/refresh", {"limit": 260, "top": 20}))
    t0 = time.time()
    last = ""
    while time.time() - t0 < 480:
        st = get(base, "/api/status", timeout=30)
        msg = f"{st.get('pct')}% {st.get('progress')}"
        if msg != last:
            print(f"  [{time.time() - t0:5.1f}s] {msg}")
            last = msg
        if st.get("done"):
            break
        if st.get("error"):
            print("扫描出错:", st["error"])
            return 1
        time.sleep(4)

    rep = get(base, "/api/result?top=20&limit=260", timeout=120)
    if not rep:
        print("❌ 没拿到结果")
        return 1

    okc = badc = 0

    def ok(c, m):
        nonlocal okc, badc
        if c:
            okc += 1; print("  OK  " + m)
        else:
            badc += 1; print("  XX  " + m)

    meta = rep.get("meta") or {}
    ok(bool(rep.get("picks")), f"选股结果 {len(rep.get('picks') or [])} 只")
    ok(meta.get("scanned", 0) > 1000, f"扫描标的 {meta.get('scanned')} 只")
    sen = rep.get("sentiment") or {}
    ok(bool(sen.get("stage")), f"情绪阶段: {sen.get('stage')} / 可交易={sen.get('can_trade')}")
    ok(bool((rep.get("volume") or {}).get("mode")),
       f"量能模式: {(rep.get('volume') or {}).get('mode')}")

    p0 = (rep.get("picks") or [{}])[0]
    if p0:
        print(f"\n  首只候选: {p0.get('name')} {p0.get('code')} 评分 {p0.get('score')}")
        ok(bool(p0.get("kline_tail")), "候选带K线")
        ok(bool(p0.get("reasons")), "候选带入选理由")

    # 单股详情接口（自选股点开走的就是它）
    detail = get(base, "/api/stock?code=sh603466", timeout=90)
    ok(bool(detail and detail.get("ok")),
       f"单股详情可用: {(detail or {}).get('stock', {}).get('name')}")

    # 数据落地在 App 私有目录
    data_dir = os.path.join(files_dir, "data")
    ok(os.path.exists(os.path.join(data_dir, "strategy.json")), "策略配置在可写区")
    ok(os.path.exists(os.path.join(data_dir, "valid_codes.json")),
       "当日有效代码名单已落盘（下次扫描提速用）")

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    shutil.rmtree(sim, ignore_errors=True)
    return 1 if badc else 0


if __name__ == "__main__":
    sys.exit(main())
