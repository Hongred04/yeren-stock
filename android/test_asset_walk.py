# -*- coding: utf-8 -*-
"""复现并验证 MainActivity 里 assets 解包的路径拼接逻辑。

为什么要单独测这个：v1.12.0 首版在真机上打开是 Python 的 404 错误页。
根因是 Kotlin 里 `"$assetPath/$name"` 在 assetPath 为空时拼出了 "/web"
（开头带斜杠），而 Android 的 AssetManager 对 "/web" 会返回空列表 ——
于是目录被误判成文件，open("/web") 抛异常，整个解包函数被 catch 掉、
静默返回 null，最后 Python 找不到 web/index.html 就回了 404。

之前的验证脚本是用 Python 重写了一遍解包流程，**没有覆盖真正的 Kotlin 逻辑**，
所以完全没拦住这个 bug。这里改用「模拟 AssetManager 语义 + 逐行照搬 Kotlin 逻辑」
的方式，把这条路径真正测起来。

FakeAssets 的语义按 Android 文档还原：
  list("")        -> 顶层条目 ["web", "defaults"]
  list("web")     -> ["index.html"]
  list("web/x")   -> []            （文件返回空列表）
  list("/web")    -> []            （带前导斜杠认不出来）
  open("/web")    -> 抛异常
"""
from __future__ import annotations

import os
import shutil
import tempfile

# 模拟 APK 的 assets 目录结构
ASSETS = {
    "web": ["index.html"],
    "web/index.html": b"<html>ok</html>",
    "defaults": ["strategy.json"],
    "defaults/strategy.json": b'{"preset":"yeren"}',
}

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


class FakeAssets:
    """按 Android AssetManager 的语义实现（含它那个前导斜杠的坑）。"""

    def list(self, path):
        if path in ("", "/"):
            return [k for k in ASSETS if "/" not in k]
        if path.startswith("/"):
            return []          # 带前导斜杠：不认
        v = ASSETS.get(path, [])
        if isinstance(v, bytes):
            return []          # 文件 → 空列表
        return list(v)

    def open(self, path):
        v = ASSETS.get(path)
        if isinstance(v, bytes):
            import io
            return io.BytesIO(v)
        raise FileNotFoundError(f"asset not found: {path}")


# ── 逐行照搬 Kotlin 的两个版本 ──

def copy_buggy(am, asset_path, dst):
    """v1.12.0 首版：childAsset = "$assetPath/$name"（空 path 时会带前导斜杠）。"""
    for name in am.list(asset_path):
        child = f"{asset_path}/{name}"
        if not am.list(child):
            out = os.path.join(dst, name)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(am.open(child).read())
        else:
            d = os.path.join(dst, name)
            os.makedirs(d, exist_ok=True)
            copy_buggy(am, child, d)


def copy_fixed(am, asset_path, dst):
    """修复版：空 path 时不加斜杠。"""
    for name in am.list(asset_path):
        child = name if asset_path == "" else f"{asset_path}/{name}"
        if not am.list(child):
            out = os.path.join(dst, name)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(am.open(child).read())
        else:
            d = os.path.join(dst, name)
            os.makedirs(d, exist_ok=True)
            copy_fixed(am, child, d)


KNOWN = ["web/index.html", "defaults/strategy.json"]


def extract_fixed(am, dst):
    """修复版 extractAssets：清单优先 + 目录扫描兜底。"""
    os.makedirs(dst, exist_ok=True)
    missing = []
    for rel in KNOWN:
        out = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        try:
            with open(out, "wb") as f:
                f.write(am.open(rel).read())
        except Exception:
            missing.append(rel)
    try:
        copy_fixed(am, "", dst)
    except Exception:
        pass
    idx = os.path.join(dst, "web", "index.html")
    if not os.path.exists(idx) or os.path.getsize(idx) == 0:
        raise RuntimeError(f"解包后没有可用页面；缺失={missing}")
    return dst


def main():
    am = FakeAssets()

    print("[1] 确认 FakeAssets 还原了那个坑")
    ok(am.list("") == ["web", "defaults"], 'list("") 返回顶层')
    ok(am.list("web") == ["index.html"], 'list("web") 正常')
    ok(am.list("/web") == [], 'list("/web") 返回空（前导斜杠的坑）')
    try:
        am.open("/web")
        ok(False, 'open("/web") 应抛异常')
    except Exception:
        ok(True, 'open("/web") 抛异常（正是首版静默失败的原因）')

    print("\n[2] 旧逻辑必然复现真机上的 404")
    d1 = tempfile.mkdtemp(prefix="buggy_")
    raised = False
    try:
        copy_buggy(am, "", d1)          # 顶层第一项 "web" 就会炸
    except Exception:
        raised = True
    ok(raised, "旧逻辑在第一个目录项上抛异常 → 被 catch 吞掉 → null → 404")
    ok(not os.path.exists(os.path.join(d1, "web", "index.html")),
       "旧逻辑确实没有产出 index.html")
    shutil.rmtree(d1, ignore_errors=True)

    print("\n[3] 修复后的逻辑产出完整资源")
    d2 = tempfile.mkdtemp(prefix="fixed_")
    extract_fixed(am, d2)
    idx = os.path.join(d2, "web", "index.html")
    sj = os.path.join(d2, "defaults", "strategy.json")
    ok(os.path.exists(idx), "web/index.html 已解出")
    ok(os.path.exists(sj), "defaults/strategy.json 已解出")
    with open(idx, "rb") as f:
        ok(f.read() == ASSETS["web/index.html"], "index.html 内容正确")
    shutil.rmtree(d2, ignore_errors=True)

    print("\n[4] 清单兜底：即使目录扫描全废也能出页面")
    class BrokenList(FakeAssets):
        def list(self, path):
            return [] if path != "" else super().list(path)
    d3 = tempfile.mkdtemp(prefix="fallback_")
    extract_fixed(BrokenList(), d3)      # 目录枚举残废，但 open() 还正常
    ok(os.path.exists(os.path.join(d3, "web", "index.html")),
       "目录枚举失效时，靠显式清单仍能解出页面")
    shutil.rmtree(d3, ignore_errors=True)

    print(f"\n===== {ok_cnt} 通过 / {bad_cnt} 失败 =====")
    return 1 if bad_cnt else 0


if __name__ == "__main__":
    raise SystemExit(main())
