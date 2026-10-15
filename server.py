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
from core import engine
from core import paths
from core import portfolio as portfolio_mod
from core import strategy_config as sc
from core import trading

BASE_DIR = paths.ROOT
WEB_DIR = paths.WEB_DIR

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


_WAIT_SENTIMENT_VIEW = {
    "stage": "unknown",
    "label": "等待今日数据",
    "can_trade": False,
    "data_ready": False,
    "trial_only": True,
    "raw_ebb": False,
    "signals": [],
    "metrics": {},
    "note": "今日数据还没到手，暂不给出结论。",
}


def _apply_cached_fallback(view: dict, cached: dict) -> None:
    """把缓存底稿搬进 /api/live 的返回体 —— **只搬运，不判定**。

    时效门唯一判定点在 pipeline 读取层（get_cached / get_disk_report）：
      · 新鲜底稿 → sentiment 就是真结论，带 data_source("cache"/"disk") + data_ts；
      · 跨日底稿 → sentiment 已被换成「等待今日数据」占位、顶层带 stale=True。

    这里绝不再用 cache_is_fresh() 自己判一遍 —— 两套判定迟早走偏，而只要有一处
    漏判，隔夜结论就会被当成今日结论端出去。也绝不把 sentiment 兜成 `{}`：前端
    判空仓看的是 `sentiment.data_ready === false`，对 `{}` 会得到 undefined
    !== false，于是**不打叉**；宁可给就绪态占位，也不能给一个既没结论、又不报警
    的空白对象。data_source 打在**顶层**（T2 的前端会按顶层读）。
    """
    c = cached or {}
    sent = c.get("sentiment")
    if not isinstance(sent, dict) or not sent:
        sent = dict(_WAIT_SENTIMENT_VIEW)
        if not c:
            sent["note"] = "还没有任何缓存底稿，等待今日数据。"
    view["sentiment"] = sent
    stale = bool(c.get("stale") and sent.get("data_ready") is not True)
    view["data_source"] = "stale" if stale else (c.get("data_source") or "cache")
    view["data_ts"] = c.get("data_ts") if c.get("data_ts") else pipeline.cache_ts(c)
    if stale:
        view["stale"] = True


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
    def _send_stale(self):
        """缓存已跨日 / 压根没有缓存：明确的"等待今日数据"信号。

        沿用既有的 204 + {"cached": false} 形态（前端已有分支处理它 —— 转后台
        扫描/显示"扫描中"），并**新增** stale: true 说明这次不是首次使用、而是
        缓存过期。两者都不渲染任何结论，正是用户要的"数据没到手不说可以出手"。
        """
        body = json.dumps({"cached": False, "stale": True},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(204)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _index_view(self, code, period):
        """指数走势数据：分时 / 日K / 周K / 月K。

        ★ 顶栏原来只写一个末位价格，既不说是**哪个指数**、也不给涨跌点数 ——
          3707.48 这个数字涨跌了多少全靠用户自己心算。这里把口径一次补齐：
          name（是什么）/ price（现在多少）/ chg（涨跌点数）/ pct（涨跌幅）/
          prev_close（昨收基准，分时那条虚线要用）。

        ★ 分时是**独立链路**（新浪分钟线），日/周/月K 走项目已有的三路降级日K
          再本地聚合。两条路互不兜底：分钟级取不到时如实报空，绝不拿日K 假装
          分时 —— 那会把「今天的分时」画成「最近几十天」，是彻头彻尾的假图。
        """
        import datetime
        from core.version import APP_VERSION          # noqa: F401  （仅保持导入约定）

        code = (code or "sh000001").strip().lower()
        if not code:
            code = "sh000001"
        period = (period or "min1").strip().lower()
        alias = {"min": "min1", "1m": "min1", "m1": "min1", "today": "min1",
                 "d": "day", "daily": "day", "k": "day",
                 "w": "week", "weekly": "week",
                 "mo": "month", "monthly": "month", "y": "month"}
        period = alias.get(period, period)
        if period not in ("min1", "day", "week", "month"):
            period = "min1"

        name_map = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指",
                    "sh000300": "沪深300", "sh000905": "中证500", "sh000852": "中证1000",
                    "sh000016": "上证50", "sz399005": "中小100"}

        out = {"ok": False, "code": code, "period": period,
               "name": name_map.get(code, code.upper()),
               "ts": time.time(), "bars": [], "source": ""}
        try:
            if period == "min1":
                raw_min = ds.fetch_minute_line(code, 1, 300)
                # ★ 只保留**最后一个交易日**的分时。300 根 1 分钟线在盘中只够
                #   覆盖「当天 + 前一日尾盘」（实测：10-14 有 61 根 + 10-15 有
                #   239 根，合计正好 300）。两条日期的点连成一条折线，图会变成
                #   「昨天下午一路跌到今天收盘」的一条长斜线，而用户以为是今天
                #   的分时 —— 既失真又误导。分时的定义就是**一个交易日**。
                if raw_min:
                    _last_day = raw_min[-1].get("date")
                    bars = [x for x in raw_min if x.get("date") == _last_day]
                else:
                    bars = []
                out["source"] = "sina-min"
                out["trade_date"] = bars[-1]["date"] if bars else ""
            else:
                daily = ds.fetch_kline(code, 260)
                if period == "day":
                    bars = daily[-120:]
                    out["source"] = "kline"
                else:
                    bars = ds.resample_kline(daily, period)
                    bars = bars[-120:]
                    out["source"] = "kline-aggregated"
            out["bars"] = bars or []
        except Exception as e:                                # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {e}"

        bars = out["bars"]
        # 昨收 = **上一交易日收盘价**，四种周期都用这一个口径。
        #
        # ★ 踩过的坑（真实错误，别改回去）：分时那条分支原本没有单独取昨收，
        #   于是落到「退而取 bars[0].open」的兜底上 —— 而 300 根 1 分钟线在
        #   盘中只够覆盖**当天 + 前一日尾盘**，bars[0] 是「昨天 13:59」那一分钟
        #   的价，拿它当昨收就得出 -1.77%，而真实昨收（昨收 3767.23）是 -1.59%。
        #   同一屏里顶栏写 -1.59%、弹出的分时写 -1.77%，用户只会认为其中一个是错的。
        #   所以昨收**只从日K取**，绝不能从分时窗口的第一根推。
        prev_close = None
        try:
            full = ds.fetch_kline(code, 260)
            if len(full) >= 2:
                prev_close = float(full[-2]["close"])
        except Exception:                                     # noqa: BLE001
            prev_close = None
        if prev_close is None and period != "min1" and len(bars) >= 2:
            # 日K 拿不到（极少数）时才退到「本序列倒数第二根」；分时**不适用**
            # 这条兜底 —— 它的相邻两根是分钟，不是交易日。
            prev_close = float(bars[-2].get("close") or 0) or None
        out["prev_close"] = prev_close
        # 昨收是估算值时如实标出来（前端据此在图上写「基准推算」）
        out["prev_close_exact"] = prev_close is not None

        last = bars[-1] if bars else None
        if last:
            price = float(last.get("close") or 0)
            out["price"] = price
            if prev_close:
                out["chg"] = round(price - prev_close, 2)
                out["pct"] = round((price - prev_close) / prev_close * 100, 2)
        # 均线（周/月K 的 MA 也是按各自的根算，不是把日线均线搬过来）
        closes = [float(b.get("close") or 0) for b in bars]
        ma = {}
        for w in (5, 10, 20):
            if len(closes) >= w and w:
                ma[f"ma{w}"] = round(sum(closes[-w:]) / w, 2)
        out["ma"] = ma
        out["ok"] = bool(bars)
        if not bars:
            out["error"] = out.get("error") or "没取到该指数的走势数据（数据源未响应）"
        out["server_time"] = datetime.datetime.now().strftime("%H:%M:%S")
        return out

    def _live_view(self):
        """轻量实时刷新：只拉"当前关心的那些票"的行情 + 重算情绪阶段。

        **不触发全市场重扫**，所以毫秒到秒级返回，前端每分钟静默调一次即可。
        情绪阶段是用最新的涨停/跌停/炸板池重算的，而且过的是 stage_stabilizer
        的滞回逻辑 —— 盘中会随市场变化，但不会一跳一变。
        """
        from datetime import datetime
        # ★ 不能只读内存缓存：它有 3 分钟 TTL（RESULT_TTL=180），过期后
        #   `get_cached()` 返回 None —— `result_ts` 与 `picks` 双双丢失。
        #   而前端的静默刷新**正是靠 result_ts 变化**来判断"该换上新报告了"：
        #   丢了它，页面就永远停在打开时那一份上，行情也不再更新
        #   （用户实测："没变化啊还是昨天的数据"）。
        #   回退到磁盘那份 —— /api/result 本来就是这么兜底的，口径保持一致。
        cached = pipeline.get_cached() or pipeline.get_disk_report() or {}
        cfg = sc.load_config()
        view = {"ts": time.time(),
                "server_time": datetime.now().strftime("%H:%M:%S")}
        if cached.get("meta"):
            view["result_ts"] = cached["meta"].get("ts")

        # ① 情绪阶段：池接口（push2ex）不在被限速的 push2 家族里，可以放心调
        sess = trading.market_session()
        view["phase"] = sess.get("label", "")
        view["tmin"] = sess.get("tmin", 0)
        try:
            live_phase = sess["phase"] in ("morning", "noon", "afternoon", "tail")
            if live_phase:
                real_date = ds.pool_qdate() or cached.get("meta", {}).get("date", "")
                # ★ 三池取数之前先清一次健康度记账：这条链路不经过 pipeline，
                #   没人会替它 reset。不清的话 data_health() 拿到的可能是
                #   几十分钟前那次全量扫描留下的**旧结论**（旧降级会一直挂着，
                #   数据早到手了却还在打叉），而"本轮到底取没取到"只有清过之后
                #   的记账才说得清。注意 reset 会把上一轮降级留作兜底（见
                #   datasource.health_fallback），所以清掉不会让闸门失去判断依据。
                ds.reset_health()
                zt = ds.fetch_limit_up_pool()
                dt = ds.fetch_limit_down_pool()
                zb = ds.fetch_broken_board_pool()
                # ★ 实时重算也走同一套就绪闸门：这三个池接口在冷启动/抖动时同样
                #   会挂，而这条链路的结论**优先级更高** —— 前端每分钟静默刷新
                #   拿的就是它，一帧"没数据但可以出手"立刻就会被用户看到。
                #   取在本轮三池调用之后，_health 里的记账正是这一次的。
                view["sentiment"] = engine.evaluate_sentiment(
                    cfg, zt, dt, zb, [], real_date=real_date,
                    tmin=sess.get("tmin"), health=ds.data_health())
                view["live_sentiment"] = True
                view["data_source"] = "live"
                view["data_ts"] = time.time()
                # 实时链路自己也算一份数据健康度：前端据此打叉时不必去翻
                # 上一次全量报告里的那份（可能已经过期几十分钟了）。
                view["data_health"] = ds.data_health()
            else:
                # 非交易时段：只能回退缓存结论。**时效门不在这里判** —— 它在
                # pipeline 读取层就判完了（见 pipeline._suppress_sentiment）：
                # 跨日的底稿拿回来时 sentiment 已经是「等待今日数据」占位、
                # 顶层带 stale=True/data_source=data_ts。这里只做搬运。
                _apply_cached_fallback(view, cached)
                view["live_sentiment"] = False
        except Exception as e:
            _apply_cached_fallback(view, cached)
            view["live_sentiment"] = False
            view["sentiment_error"] = f"{type(e).__name__}: {e}"

        # ② 实时行情：候选 + 自选 + 持仓（腾讯批量，一两百只也就一两个请求）
        try:
            codes = [p.get("code") for p in (cached.get("picks") or [])]
            obj = portfolio_mod.load()
            codes += [h["code"] for h in obj.get("holdings", [])]
            codes += [w["code"] for w in obj.get("watchlist", [])]
            codes = sorted({c for c in codes if c})
            snaps = ds.fetch_snapshots(codes, batch=60, workers=8) if codes else {}
            view["quotes"] = {
                c: {"price": s.get("price"), "pct": s.get("pct"),
                    "turnover": s.get("turnover"),
                    "volume_ratio": s.get("volume_ratio"),
                    "amount": s.get("amount")}
                for c, s in snaps.items()
            }
        except Exception as e:
            view["quotes"] = {}
            view["quotes_error"] = f"{type(e).__name__}: {e}"
        return view

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
                # 只读缓存，绝不同步计算（否则会锁住 pipeline 导致后台任务卡死）。
                #
                # ★ 缓存优先供数（本轮用户口径）：首屏允许用上次缓存**照常显示
                #   结论**，但必须带来源标记 + 时间戳，且**仅同一交易日**可用 ——
                #   隔夜缓存的结论是昨天市场的读数，拿它当"现在能不能出手"就是
                #   另一种形态的"没数据却说可以出手"。
                #   ★ 时效门**不在这一层判**：get_cached / get_disk_report 回来时
                #   就已经判完了（见 pipeline._suppress_sentiment）—— 新鲜底稿带
                #   真结论 + 来源标记，跨日底稿的 sentiment 是「等待今日数据」占位。
                #   这里只读它的结论，绝不用 cache_is_fresh() 再判一遍（两套判定
                #   并存迟早走偏，漏一处就是隔夜结论泄漏）。data_source 在顶层。
                rep = pipeline.get_cached() or pipeline.get_disk_report()
                if rep:
                    self._send_json(rep)
                else:
                    # 204: 连磁盘底都没有（真正首次使用）。前端据此转后台任务
                    self._send_stale()
            elif path == "/api/config":
                self._send_json({"config": sc.load_config(),
                                 "presets": sc.list_presets()})
            elif path == "/api/presets":
                self._send_json({"presets": sc.list_presets()})
            elif path == "/api/live":
                # 静默刷新专用：轻量、不触发全市场重扫
                self._send_json(self._live_view())
            elif path == "/api/portfolio":
                self._send_json(self._portfolio_view())
            elif path == "/api/sources":
                # 所有数据源的实时状态（每个源一条：状态 / 说明 / 主机 / 用途）。
                # 状态取自最近一次真实使用，不另发探测请求 —— 详见
                # core/datasource.py 末尾的「数据源总览」。
                self._send_json({"ok": True, **ds.sources_status()})
            elif path == "/api/index":
                # 指数走势：分时 / 日K / 周K / 月K（顶栏点开看）
                self._send_json(self._index_view(
                    (qs.get("code") or ["sh000001"])[0],
                    (qs.get("period") or ["min1"])[0]))
            elif path == "/api/stock":
                # 单只股票详情：自选/持仓里点开不在当日候选中的股票也看得到
                code = (qs.get("code") or [""])[0]
                self._send_json(pipeline.stock_detail(code))
            elif path == "/api/search":
                # 名称/拼音关键词搜 A 股 —— 供「加自选」输入框用（也支持代码，
                # 但代码在前端就地识别、不必走这个接口）。
                q = (qs.get("q") or [""])[0]
                try:
                    limit = int((qs.get("limit") or ["8"])[0])
                except (TypeError, ValueError):
                    limit = 8
                limit = max(1, min(20, limit))
                self._send_json(ds.search_stocks(q, limit))
            elif path == "/api/health":
                self._send_json({"ok": True, "job": _job, "paths": paths.describe(),
                                 "version": __import__("core.version", fromlist=["x"]).APP_VERSION})
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
                    # ★ 成本/股数必须能转成正数才许录入。
                    #   原来直接 payload.get("cost", 0) 透传，成本 0 会被当成
                    #   合法持仓存下来 —— 后果不是报错，而是所有盈亏静默失真：
                    #   pnl_pct 算成 None、保本线不出现、「回落到成本附近」这类
                    #   提示全消失，用户看到一张「像是正常的」空卡片，
                    #   却不知道是因为自己漏填了成本。宁可当场拒绝。
                    try:
                        _c = float(payload.get("cost") or 0)
                        _s = float(payload.get("shares") or 0)
                    except (TypeError, ValueError):
                        self._send_json({"error": "成本/股数要填数字"}, 400)
                        return
                    if _c <= 0:
                        self._send_json({"error": "成本必须大于 0 —— 不填成本就算不出盈亏和保本线"}, 400)
                        return
                    if _s <= 0:
                        self._send_json({"error": "股数必须大于 0"}, 400)
                        return
                    portfolio_mod.upsert_holding(
                        code, payload.get("name", ""),
                        _c, _s,
                        payload.get("date", ""), payload.get("note", ""))
                self._send_json({"ok": True, "portfolio": self._portfolio_view()})
            elif path == "/api/sources":
                # 数据源治理：清除熔断 / 重连。
                # 原「保存/清空/自动获取东财 Cookie」三个动作已移除（v1.29.0）——
                # 东财的限流发生在连接层、与 Cookie 无关，配了也没用。
                act = payload.get("action", "")
                if act == "unblock":
                    n = ds.em_reset_blocks()
                    self._send_json({"ok": True, "msg": f"已清除 {n} 条熔断记录",
                                     "blocked": ds.em_blocked()})
                elif act == "reconnect":
                    # 丢弃 keep-alive 连接池：盘后长时间挂机后若取不到行情，
                    # 点这里强制重连（_get 已能自愈，这是多一层保险）
                    n = ds.reset_pool()
                    self._send_json({"ok": True, "closed": n,
                                     "msg": f"已丢弃 {n} 条闲置连接，下次请求将重新握手"})
                else:
                    self._send_json({"error": "未知 action"}, 400)
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

    安卓例外：App 重启很频繁，端口常处于 TIME_WAIT，此时必须允许复用，
    否则会出现「刚打开就报端口失败」。所以由启动参数控制。
    """
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        # 客户端提前断开（浏览器刷新/超时）不应刷屏
        exc = sys.exc_info()[0]
        if exc in (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            return
        traceback.print_exc()


def _port_busy(port: int) -> bool:
    """桌面上的端口占用预检。安卓没有 netstat，直接跳过。"""
    if paths.is_android():
        return False
    import subprocess
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             timeout=10).stdout.decode("gbk", errors="ignore")
        return any(f":{port}" in l and "LISTENING" in l for l in out.splitlines())
    except Exception:
        return False


def build_server(port: int = 8777, host: str = "127.0.0.1",
                 reuse: bool | None = None) -> "StrictServer":
    """建一个已绑定但还没 serve 的服务器实例。

    reuse=None 时按环境自动决定：安卓 True（容忍 TIME_WAIT），桌面 False。
    """
    if reuse is None:
        reuse = paths.is_android()
    if reuse:
        # 动态建子类，避免污染 StrictServer 的语义
        cls = type("ReusableServer", (StrictServer,), {"allow_reuse_address": True})
    else:
        cls = StrictServer
    return cls((host, port), Handler)


def serve_forever(port: int = 8777, host: str = "127.0.0.1",
                  log: bool = True) -> "StrictServer":
    """阻塞式启动（桌面 main 用）。"""
    if not paths.is_android() and _port_busy(port):
        print(f"\n[错误] 端口 {port} 已被占用。")
        print("       多实例共存会导致读到旧代码/旧缓存。")
        print("       解决：关闭旧窗口，或用 启动系统.py --port 8899")
        raise SystemExit(1)
    srv = build_server(port, host)
    if log:
        print(f"智能股票监控系统已启动 →  http://{host}:{port}")
        print("按 Ctrl+C 停止")
    return srv


def start_background(port: int = 8777, host: str = "127.0.0.1"):
    """在后台线程里跑服务，立即返回 (server, thread)。

    安卓侧用它：Java 启动 Python 后调这个，再把 WebView 指向 host:port。
    端口被占时自动往后找（App 反复重启时很常见）。
    """
    last_err = None
    for p in range(port, port + 20):
        try:
            srv = build_server(p, host, reuse=True)
        except OSError as e:
            last_err = e
            continue
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv, t
    raise OSError(f"{port}..{port + 19} 全部无法绑定：{last_err}")


def main(port: int = 8777):
    srv = serve_forever(port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
