# -*- coding: utf-8 -*-
"""路径解析 —— 全系统唯一的目录出处。

桌面运行时，数据落在项目根目录下的 data/ 与 cache/。
Android（Chaquopy 内嵌）运行时，代码被打进 APK 只读区，
只有 App 私有目录可写，所以数据/缓存必须改道到那里。

判定顺序（先到先用）：
  1. 环境变量 STOCK_DATA_DIR / STOCK_CACHE_DIR / STOCK_WEB_DIR —— 安卓侧由 Java
     启动前注入，桌面侧也可用它把数据搬到别处（比如放移动硬盘）。
  2. 检测到安卓（Chaquopy 会往 sys 上加 getandroidapilevel）
     → 用 App 私有目录，再兜底到 $HOME / 临时目录。
  3. 否则 = 项目根目录。

只读资源（web/index.html）在桌面跟着代码走；安卓上它进不了 APK 的
Python 源码包（Chaquopy 只打包 .py），所以由 Java 侧从 assets 拷到私有目录，
再用 STOCK_WEB_DIR 指过去。
"""
from __future__ import annotations

import os
import sys
import tempfile

# 项目根：web/ 与 core/ 的父目录（APK 里就是 Chaquopy 解出的源码根）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_android() -> bool:
    """是否跑在 Android 上（Chaquopy 提供的探针）。"""
    return hasattr(sys, "getandroidapilevel") or bool(
        os.environ.get("ANDROID_DATA"))


def _first_writable(*cands: str) -> str:
    for c in cands:
        if not c:
            continue
        try:
            os.makedirs(c, exist_ok=True)
            probe = os.path.join(c, ".w")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("1")
            os.remove(probe)
            return c
        except Exception:
            continue
    # 全失败：退回临时目录，至少不让程序崩
    d = os.path.join(tempfile.gettempdir(), "stock_data")
    os.makedirs(d, exist_ok=True)
    return d


def _resolve(env_key: str, name: str) -> str:
    env = os.environ.get(env_key, "").strip()
    if env:
        os.makedirs(env, exist_ok=True)
        return env
    if is_android():
        # Java 侧通常会把私有目录写进 HOME；再兜几个已知位置
        home = os.environ.get("HOME", "")
        cands = []
        if home:
            cands.append(os.path.join(home, name))
        files_dir = os.environ.get("STOCK_APP_FILES", "")
        if files_dir:
            cands.append(os.path.join(files_dir, name))
        cands.append(os.path.join(tempfile.gettempdir(), name))
        return _first_writable(*cands)
    return os.path.join(ROOT, name)


# 可写目录
DATA_DIR = _resolve("STOCK_DATA_DIR", "data")
CACHE_DIR = _resolve("STOCK_CACHE_DIR", "cache")

# 只读资源。安卓上由 Java 把 assets/web 拷到私有目录后经 STOCK_WEB_DIR 指定；
# 桌面/未指定时就是项目里的 web/。
_env_web = os.environ.get("STOCK_WEB_DIR", "").strip()
WEB_DIR = os.path.normpath(_env_web) if _env_web else os.path.join(ROOT, "web")


def data_path(*parts: str) -> str:
    """拼一个 data 目录下的路径（不自动建目录，交给写入方）。"""
    return os.path.join(DATA_DIR, *parts)


def cache_path(*parts: str) -> str:
    return os.path.join(CACHE_DIR, *parts)


def ensure_data_dir() -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    return DATA_DIR


def ensure_cache_dir() -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return CACHE_DIR


def describe() -> dict:
    """给 /api/health 之类的接口用，便于排查安卓上的路径问题。"""
    return {
        "root": ROOT,
        "data_dir": DATA_DIR,
        "cache_dir": CACHE_DIR,
        "web_dir": WEB_DIR,
        "android": is_android(),
        "web_exists": os.path.exists(os.path.join(WEB_DIR, "index.html")),
        "data_writable": os.access(DATA_DIR, os.W_OK),
    }
