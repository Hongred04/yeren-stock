# -*- coding: utf-8 -*-
"""HTTP 服务：REST API + 静态看板。无第三方依赖，纯标准库。"""
from __future__ import annotations

import json
import mimetypes
import os
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core import pipeline
from core import datasource as ds
from core import portfolio as portfolio_mod
from core import strategy_config as sc

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

# 后台分析任务状态
_job = {"running": False, "progress": "空闲", "pct": 0, "done": False,
        "error": "", "started": 0.0, "finished": 0.0}
_job_lock = threading.Lock()


def _run_analysis(cfg, date, limit, top):
    """后台执行分析，实时更新进度。"""
    t0 = time.time()
    with _job_lock:
        _job.update({"running": True, "progress": "启动中…", "pct": 2,
                     "done": False, "error": "", "started": t0, "finished": 0.0})

    # 阶段权重：情绪池 3% / 决策链 8% / 全市场快照 10-38% / 粗筛 40% / K线 48-94% / 打分 96%
    def on_prog(stage, cur=0, total=0):
        base, span = 96, 0
        if "情绪池" in stage:
            base = 3
        elif "宏观" in stage:
            base = 8
        elif "扫描全市场" in stage:
            base, span = 10, 28          # 快照阶段 10%→38%
        elif "粗筛" in stage:
            base = 40
        elif "K线" in stage:
            base, span = 48, 46          # K线阶段 48%→94%
        elif "打分" in stage:
            base = 96
        if span and total:
            pct = int(min(base + span, base + (cur / total) * span))
        else:
            pct = base
        with _job_lock:
            _job["progress"] = stage
            _job["pct"] = max(2, min(99, pct))

    pipeline.set_progress(on_prog)
    try:
        rep = pipeline.analyze(cfg=cfg, date=date, kline_limit=limit,
                               top_n=top, use_cache=False)
        with _job_lock:
            _job.update({"running": False, "progress": "完成", "pct": 100,
                         "done": True, "error": "",
                         "finished": time.time()})
        return rep
    except Exception as e:
        traceback.print_exc()
        with _job_lock:
            _job.update({"running": False, "progress": "失败", "pct": 0,
                         "done": False,
                         "error": f"{type(e).__name__}: {e}",
                         "finished": time.time()})
        raise
    finally:
        pipeline.set_progress(None)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 静默

    # ------------------------------------------------------------ 工具
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        if not os.path.isfile(path):
            self.send_error(404, "Not Found")
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 前端频繁改动，彻底禁用缓存 + 带上文件修改时间便于排查
        try:
            mtime = str(int(os.path.getmtime(path)))
        except OSError:
            mtime = "0"
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("X-File-Mtime", mtime)
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------ 路由
    def _portfolio_view(self):
        """按需体检持仓：仅拉持仓/自选相关快照与K线，不触发全市场重扫。"""
        obj = portfolio_mod.load()
        codes = [h["code"] for h in obj.get("holdings", [])] + \
                [w["code"] for w in obj.get("watchlist", [])]
        snaps = ds.fetch_snapshots(sorted(set(codes)), batch=60, workers=8) if codes else {}
        hold_codes = [h["code"] for h in obj.get("holdings", [])]
        kl = ds.fetch_klines(hold_codes, n=70, workers=8) if hold_codes else {}
        cached = pipeline.get_cached() or {}
        sent = cached.get("sentiment") or {}
        vol = cached.get("volume") or {}
        view = portfolio_mod.evaluate(snaps, kl, sent, vol, sc.load_config())
        view["raw"] = obj
        return view

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/report":
                # 兼容旧接口：只读缓存，不做同步计算
                cached = pipeline.get_cached()
                if cached:
                    self._send_json(cached)
                    return
                self._send_json({"cached": False,
                                 "hint": "请用 /api/start + /api/status + /api/result"},
                                status=202)
            elif path == "/api/has-cache":
                # 极轻量探测，毫秒级返回
                self._send_json({"cached": pipeline.has_cached(),
                                 "running": _job["running"],
                                 "pct": _job["pct"]})
            elif path == "/api/start":
                # 启动后台分析，立刻返回
                with _job_lock:
                    if _job["running"]:
                        self._send_json({"ok": True, "running": True,
                                         "progress": _job["progress"],
                                         "pct": _job["pct"]})
                        return
                cfg = sc.load_config()
                t = threading.Thread(
                    target=_run_analysis, daemon=True,
                    args=(cfg, qs.get("date", [""])[0],
                          int(qs.get("limit", ["260"])[0]),
                          int(qs.get("top", ["20"])[0])))
                t.start()
                self._send_json({"ok": True, "running": True,
                                 "progress": "已启动", "pct": 1})
            elif path == "/api/status":
                with _job_lock:
                    snap = dict(_job)
                snap["elapsed"] = round(
                    ((snap.get("finished") or time.time()) - snap["started"]), 1) \
                    if snap.get("started") else 0
                self._send_json(snap)
            elif path == "/api/result":
                # 只读缓存，绝不同步计算（否则会锁住 pipeline 导致后台任务卡死）
                cached = pipeline.get_cached()
                if cached:
                    self._send_json(cached)
                else:
                    # 204: 无缓存。前端据此转后台任务
                    body = b'{"cached": false}'
                    self.send_response(204)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            elif path == "/api/config":
                self._send_json({"config": sc.load_config(),
                                 "presets": sc.list_presets()})
            elif path == "/api/presets":
                self._send_json({"presets": sc.list_presets()})
            elif path == "/api/portfolio":
                self._send_json(self._portfolio_view())
            elif path == "/api/health":
                self._send_json({"ok": True, "job": _job})
            elif path in ("/", "/index.html"):
                self._send_file(os.path.join(WEB_DIR, "index.html"))
            else:
                rel = path.lstrip("/")
                target = os.path.normpath(os.path.join(WEB_DIR, rel))
                if target.startswith(WEB_DIR) and os.path.isfile(target):
                    self._send_file(target)
                else:
                    self.send_error(404)
        except Exception as e:
            traceback.print_exc()
            self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            payload = {}

        try:
            if path == "/api/invalidate":
                # 手动失效结果缓存，强制下次重新扫描
                pipeline.invalidate()
                self._send_json({"ok": True})
            elif path == "/api/config":
                # 保存策略配置（实现「随时改变选股策略」）
                cfg = payload.get("config")
                if not cfg:
                    self._send_json({"error": "缺少 config"}, 400)
                    return
                if payload.get("preset"):
                    cfg["preset"] = payload["preset"]
                    cfg["name"] = sc.PRESETS.get(payload["preset"], {}).get(
                        "name", cfg.get("name", ""))
                    cfg["desc"] = sc.PRESETS.get(payload["preset"], {}).get("desc", "")
                sc.save_config(cfg)
                # 配置变更后使结果缓存失效
                with _job_lock:
                    pass
                pipeline._result_ts = 0
                self._send_json({"ok": True, "config": cfg})
            elif path == "/api/preset":
                key = payload.get("preset", "wildman")
                cur = sc.load_config()
                new = sc.apply_preset(cur, key)
                sc.save_config(new)
                pipeline._result_ts = 0
                self._send_json({"ok": True, "config": new})
            elif path == "/api/portfolio":
                # 增/改持仓、加删自选
                act = payload.get("action", "add")
                code = (payload.get("code") or "").strip()
                if not code:
                    self._send_json({"error": "缺少 code"}, 400)
                    return
                if act == "remove":
                    portfolio_mod.remove_holding(code)
                elif act == "watch_add":
                    portfolio_mod.watch_upsert(code, payload.get("name", ""))
                elif act == "watch_remove":
                    portfolio_mod.watch_remove(code)
                else:
                    portfolio_mod.upsert_holding(
                        code, payload.get("name", ""),
                        payload.get("cost", 0), payload.get("shares", 0),
                        payload.get("date", ""), payload.get("note", ""))
                self._send_json({"ok": True, "portfolio": self._portfolio_view()})
            elif path == "/api/refresh":
                with _job_lock:
                    if _job["running"]:
                        self._send_json({"ok": False, "msg": "分析进行中"})
                        return
                t = threading.Thread(target=_run_analysis, daemon=True,
                                     args=(sc.load_config(),
                                           payload.get("date", ""),
                                           int(payload.get("limit", 260)),
                                           int(payload.get("top", 20))))
                t.start()
                self._send_json({"ok": True, "msg": "已开始后台分析"})
            else:
                self.send_error(404)
        except Exception as e:
            traceback.print_exc()
            self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)


class StrictServer(ThreadingHTTPServer):
    """禁止 SO_REUSEADDR —— 避免多实例静默共存抢占同一端口。

    ThreadingHTTPServer 默认 allow_reuse_address=1，会导致端口被占用时
    第二个实例照样启动成功，两者同时监听同一端口，浏览器随机连到旧实例，
    表现为「改了代码不生效」「页面卡住」等极难排查的问题。
    """
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        # 客户端提前断开（浏览器刷新/超时）不应刷屏
        exc = sys.exc_info()[0]
        if exc in (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            return
        traceback.print_exc()


def main(port: int = 8777):
    # 端口占用预检
    import subprocess
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             timeout=10).stdout.decode("gbk", errors="ignore")
        busy = [l.split()[-1] for l in out.splitlines()
                if f":{port}" in l and "LISTENING" in l]
        if busy:
            print(f"\n[错误] 端口 {port} 已被占用（PID {', '.join(set(busy))}）")
            print("       多实例共存会导致读到旧代码/旧缓存。")
            print("       解决：关闭旧窗口，或用 python 启动系统.py --port 8899")
            return
    except Exception:
        pass

    try:
        srv = StrictServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"\n[错误] 端口 {port} 启动失败: {e}")
        print("       换个端口: python 启动系统.py --port 8899")
        return

    print(f"智能股票监控系统已启动 →  http://127.0.0.1:{port}")
    print("按 Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
