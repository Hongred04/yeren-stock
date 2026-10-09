# -*- coding: utf-8 -*-
"""安卓启动引导 —— Java 侧调 start() 拉起内嵌的 HTTP 服务。

职责很窄：把 Android 的可写目录告诉 Python（core/paths.py 靠环境变量找）、
然后起服务、把真实端口回给 Java。

桌面工程里的 server.py / core/ 原封不动复用，不做任何安卓特判 ——
目录差异全部收在 core/paths.py 里。
"""
from __future__ import annotations

import os
import sys
import traceback

_started_port = 0
_server = None
_error = ""


def _setup_dirs(files_dir: str, cache_dir: str, assets_dir: str = "") -> None:
    """把可写目录注入环境。必须在 import server / core 之前调用 ——
    core/paths.py 是在导入时就把路径算成常量的。"""
    data = os.path.join(files_dir, "data")
    cache = os.path.join(cache_dir or files_dir, "cache")
    os.makedirs(data, exist_ok=True)
    os.makedirs(cache, exist_ok=True)
    os.environ["STOCK_DATA_DIR"] = data
    os.environ["STOCK_CACHE_DIR"] = cache
    os.environ["STOCK_APP_FILES"] = files_dir
    if assets_dir:
        web = os.path.join(assets_dir, "web")
        if os.path.isdir(web):
            os.environ["STOCK_WEB_DIR"] = web
    # OpenSSL 默认信任目录：安卓上 Python 找不到系统 CA 时会握手失败。
    # Chaquopy 自带 cacert.pem，这里指过去，保证 HTTPS 抓行情能过。
    for cand in _cacert_candidates():
        if os.path.exists(cand):
            os.environ.setdefault("SSL_CERT_FILE", cand)
            os.environ.setdefault("SSL_CERT_DIR", os.path.dirname(cand))
            break


def _cacert_candidates() -> list[str]:
    out = []
    try:
        import ssl
        base = os.path.dirname(os.path.dirname(os.path.abspath(ssl.__file__)))
        out.append(os.path.join(base, "chaquopy", "cacert.pem"))
    except Exception:
        pass
    out.append("/data/data/com.yeren.stock/files/cacert.pem")
    return out


def _seed_defaults(files_dir: str, assets_dir: str) -> None:
    """首次启动把随包的默认策略配置放进可写区。

    桌面调好的那套预设跟着 assets 一起发过来，手机首启就是同一份口径；
    之后用户在策略面板里改的都以可写区那份为准，升级 App 不会覆盖。
    """
    if not assets_dir:
        return
    src = os.path.join(assets_dir, "defaults", "strategy.json")
    dst = os.path.join(files_dir, "data", "strategy.json")
    try:
        if os.path.exists(src) and not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            import shutil
            shutil.copyfile(src, dst)
    except Exception:
        pass


def start(files_dir: str, cache_dir: str = "", port: int = 8777,
          assets_dir: str = "") -> int:
    """启动服务并返回它实际监听的端口；失败返回 0，错误信息由 last_error() 取。

    assets_dir：Java 侧从 APK assets 解出来的目录，里面应有
    web/index.html（看板页面）与 defaults/strategy.json（默认策略）。
    Chaquopy 只打包 .py，这两样进不了源码包，只能这样递进来。
    """
    global _started_port, _server, _error

    if _started_port:
        return _started_port

    try:
        _setup_dirs(files_dir, cache_dir, assets_dir)
        _seed_defaults(files_dir, assets_dir)

        # 源码根要进 sys.path：Chaquopy 一般已经加好，这里兜一层
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)

        import server

        srv, _t = server.start_background(port=int(port))
        _server = srv
        _started_port = int(srv.server_address[1])
        return _started_port
    except Exception:
        _error = traceback.format_exc()
        return 0


def last_error() -> str:
    return _error or ""


def port() -> int:
    return _started_port


def diag() -> str:
    """给 Java 侧打日志用：路径是否如预期、web 页面在不在。"""
    try:
        from core import paths
        d = paths.describe()
        return ("root=%(root)s\ndata=%(data_dir)s\ncache=%(cache_dir)s\n"
                "android=%(android)s web=%(web_dir)s web_exists=%(web_exists)s "
                "writable=%(data_writable)s" % d)
    except Exception:
        return traceback.format_exc()
