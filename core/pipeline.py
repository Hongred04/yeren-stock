# -*- coding: utf-8 -*-
"""分析管线：编排数据 → 策略判定 → 选股 → 买卖点，输出统一 JSON。"""
from __future__ import annotations

import re
import sys
import threading
import time
from datetime import datetime

from . import cls as cls_mod
from . import auction as auction_mod
from . import amount as amount_mod
from . import datasource as ds
from . import engine
from . import gene as gene_mod
from . import journal as journal_mod
from . import backtest as backtest_mod
from . import kph as kph_mod
from . import lhb as lhb_mod
from . import mainline
from . import moneyflow as flow_mod
from . import outlook
from . import portfolio as portfolio_mod
from . import regime
from . import rebound as rebound_mod
from . import industry as industry_mod
from . import substitute as substitute_mod
from . import sowing as sowing_mod
from . import sentiment_meter
from . import sentiment_meter as meter_mod
from . import strategy_config as sc
from . import swing as swing_mod
from . import ths as ths_mod
from . import trading
from . import universe
from .version import APP_VERSION

_lock = threading.Lock()          # 仅保护结果缓存的读写
_compute_lock = threading.Lock()  # 防止重复计算（后台任务串行）
_result_cache: dict = {}
_result_ts: float = 0
RESULT_TTL = 180  # 3分钟
_progress_cb = None  # 全局进度回调


def set_progress(cb):
    """注册进度回调 cb(stage, current, total)，用于前端实时反馈。"""
    global _progress_cb
    _progress_cb = cb


def _prog(stage, cur=0, total=0):
    if _progress_cb:
        try:
            _progress_cb(stage, cur, total)
        except Exception:
            pass


def has_cached() -> bool:
    """是否有可用结果缓存（不触发任何计算）。"""
    with _lock:
        return bool(_result_cache) and (time.time() - _result_ts) < RESULT_TTL


def get_cached() -> dict | None:
    """直接取当前结果缓存，不触发计算。

    ★ 这里就是「实时」路径的定义：TTL（3 分钟）内的内存缓存是**刚算完的**，
      所以贴 data_source="live" —— 时效门对它不适用（它连 TTL 都出不去，更不
      可能跨交易日）。来源标记在这一层统一贴，消费方（server / 前端）只读不管。
    """
    with _lock:
        if _result_cache and (time.time() - _result_ts) < RESULT_TTL:
            return tag_source(_result_cache, "live")
    return None


# ---------------------------------------------------------------- 磁盘缓存
# 为什么需要（2026-10-10 用户实测）：内存缓存的 TTL 只有 3 分钟，且进程重启就
# 丢失 —— 此时 /api/result 返回 204，桌面版能自动转"扫描中"，移动端却直接
# 报「取不到数据」（用户感受就是"界面加载不出来了"）。落一份盘：
# 过期/重启后先把**上次的结果**端出来（标 stale），后台照常重扫刷新。

def _disk_cache_path() -> str:
    try:
        from . import paths
        return paths.data_path("last_report.json")
    except Exception:
        return ""


def save_disk_report(report: dict) -> None:
    """扫描完成后落盘（失败静默——绝不影响主流程）。"""
    p = _disk_cache_path()
    if not p:
        return
    try:
        import json
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "report": report}, f, ensure_ascii=False)
    except Exception:
        pass


_disk_memo: tuple | None = None      # (mtime, report)，按文件 mtime 记忆


_WAIT_SENTIMENT = {
    "stage": "unknown",
    "label": "等待今日数据",
    "can_trade": False,
    "data_ready": False,
    "trial_only": True,
    "raw_ebb": False,
    "signals": [],
    "metrics": {},
    "note": "首屏用的是缓存，但它的数据日期不是当日，不构成今日结论。",
}


def _suppress_sentiment(rep: dict) -> dict:
    """把过期缓存里的结论压成**就绪态占位**，原件挪进 `_sent_kept` 备查。

    顶层既有字段（meta / data_health / picks / position / volume…）一个不删，
    只动 `sentiment` —— 那是前端唯一真正照着下单的字。隔夜结论留在 `_sent_kept`
    里不是为了再渲染，而是排查时能看出"拦掉的是哪一份"。
    """
    out = dict(rep)
    sent = rep.get("sentiment")
    if isinstance(sent, dict):
        out["_sent_kept"] = sent
    ph = dict(_WAIT_SENTIMENT)
    ph["note"] = (f"缓存数据日期 {cache_data_date(rep) or '（缺失）'} "
                  f"≠ 当日交易日 {today_trade_date() or '（未知）'}，"
                  "不构成今日结论 —— 正在后台刷新，稍候自动替换。")
    out["sentiment"] = ph
    out["_sent_suppressed"] = True
    out["_sent_suppressed_for"] = cache_data_date(rep)
    return out


def _unsuppress_sentiment(rep: dict) -> dict:
    """把**新鲜**缓存上可能残留的抑制占位还原掉（见 _suppress_sentiment）。

    内存缓存键控在文件 mtime 上，文件被重写成同日的新报告后 mtime 变了、必然
    重解析；这里是第二道保险：新鲜就一定给结论，不留占位。
    """
    if rep.get("_sent_suppressed"):
        out = dict(rep)
        out.pop("_sent_suppressed", None)
        out.pop("_sent_suppressed_for", None)
        if not out.get("sentiment") and isinstance(rep.get("_sent_kept"), dict):
            out["sentiment"] = rep["_sent_kept"]
        out.pop("_sent_kept", None)
        return out
    return rep


def get_disk_report() -> dict | None:
    """读上次落盘的结果。没有/损坏返回 None。

    ★ 时效门在**这一层**收口（不再是由 server 各分支各自记得判）：
      · 新鲜（meta.date == 当日交易日）→ 照常返回报告 + data_source="disk"
        + data_ts（那份数据自己的时间戳），**不标 stale**；
      · 过期（隔夜/跨日，或日期取不到）→ 顶层既有字段一个不删地保留，但
        `sentiment` 被换成**就绪态占位**（can_trade=False、label="等待今日数据"），
        并标 stale=True。隔夜那份结论**绝不**当首屏结论端出去 —— 昨日高潮今天
        照渲染正是用户报的「数值偏热可以出售」。
      为什么是"替换 sentiment"而不是"保留 + 加个 suppressed 标记"：消费方很多
      （/api/result、/api/live 的两条回退、/api/report、_portfolio_view…），
      软标记要靠每个消费方自觉遵守，漏一处就是漏洞；而 sentiment.can_trade
      是**前端唯一真正照着下单的字**，把它压成就绪态，漏判的代价从"显示错的
      结论"降级为"多等一下"。硬安全侧。

    按文件 mtime 记忆：这份 JSON 有两百多 KB，而 `/api/live`（每分钟一次）和
    `/api/result` 都会走这里 —— 不能每次都重新解析。
    """
    global _disk_memo
    p = _disk_cache_path()
    if not p:
        return None
    try:
        import json
        import os
        if not os.path.exists(p):
            _disk_memo = None
            return None
        mt = os.path.getmtime(p)
        if _disk_memo is not None and _disk_memo[0] == mt:
            return _disk_memo[1]
        with open(p, encoding="utf-8") as f:
            obj = json.load(f)
        rep = obj.get("report")
        if not isinstance(rep, dict) or not rep.get("meta"):
            return None
        rep = dict(rep)
        rep["stale_ts"] = obj.get("ts")
        if cache_is_fresh(rep):
            rep = _unsuppress_sentiment(rep)
            rep.pop("stale", None)
            rep = tag_source(rep, "disk")
        else:
            rep = _suppress_sentiment(rep)
            rep["stale"] = True
            rep = tag_source(rep, "disk")
        _disk_memo = (mt, rep)
        return rep
    except Exception:
        return None


def invalidate():
    """使结果缓存失效。"""
    global _result_ts
    _result_ts = 0


# ---------------------------------------------------------------- 缓存时效门
# 用户口径（本轮敲定）：首屏允许用上次缓存照常显示结论，但
#   ① 必须带「缓存」来源标记 + 时间戳；
#   ② **仅同一交易日**可用 —— 隔夜/跨日缓存不得渲染结论。
# 为什么②是硬要求：收盘后情绪阶段、涨停家数、候选名单全都停在昨天的市场状态上，
# 拿它当"现在的结论"端出去，正是用户最初报的那类"没数据却说可以出手"的另一种形态。

def cache_data_date(rep: dict | None) -> str:
    """缓存/报告自带的数据日期（yyyyMMdd）。取 meta.date —— 那是**这份数据自己
    陈述的交易日**（pipeline 落报告时写的 real_date = ds.pool_qdate() or 请求日）。

    不取 meta.ts / 文件 ts：那是"算完的时刻"，跨零点重扫会与数据日期脱节。
    """
    m = (rep or {}).get("meta") or {}
    return str(m.get("date") or "").strip()


def cache_ts(rep: dict | None) -> float:
    """这份数据自己的时间戳（epoch 秒），供前端标注「缓存于 xx:xx」。

    优先 meta.ts（报告算完的时刻），回退磁盘外层 ts（get_disk_report 存在
    stale_ts）。**不是**响应时间。
    """
    m = (rep or {}).get("meta") or {}
    for v in (m.get("ts"), (rep or {}).get("stale_ts")):
        try:
            if v:
                return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


def today_trade_date() -> str:
    """当日交易日（yyyyMMdd）—— 时效门的比较基准。

    口径：datasource.last_trade_date() 优先（它读涨停池接口自报的 qdate，
    东财池接口忽略 date 入参、始终返回最新交易日，所以这是"市场认可的最新
    交易日"）；它取不到时退回本机最近工作日。

    ★ 为什么不用 pool_qdate()：它只回读 **已缓存** 的 qdate（_pool_qdate["ZT"]），
    新进程/还没调过池接口时是空串。时效门在冷启动首屏就会被调用，用它会得到
    ""，与任何 meta.date 都不等 → 把**当日**缓存误判成隔夜、白白丢弃可用缓存。
    last_trade_date() 会真的探一次接口拿权威值，且自带 1800 秒缓存，不会反复请求。
    """
    try:
        d = ds.last_trade_date()
        if d:
            return str(d)
    except Exception:                                        # noqa: BLE001
        pass
    try:
        from datetime import timedelta
        x = datetime.now()
        while x.weekday() >= 5:
            x -= timedelta(days=1)
        return x.strftime("%Y%m%d")
    except Exception:                                        # noqa: BLE001
        return ""


def cache_is_fresh(rep: dict | None) -> bool:
    """这份缓存/报告是不是**本交易日**的（时效门）。

    判据：它自己陈述的数据日期 == 当日交易日。任一取不到 → 判**不新鲜**（保守侧：
    宁可让界面等今日数据，也不能把隔夜结论当今天端出去）。
    盘后到次日开盘前，last_trade_date() 仍返回刚收盘的交易日，恰好等于当日报告的
    meta.date —— 同一交易日的晚间缓存照样可用，不会误伤。
    """
    d = cache_data_date(rep)
    if not d:
        return False
    return d == today_trade_date()


def tag_source(rep: dict | None, source: str) -> dict:
    """给要返回的缓存/磁盘报告贴来源标记（新增字段，不动任何既有字段）。

    data_source：cache（内存，3 分钟 TTL 内）/ disk（落盘的上次报告）/ live（实时）。
    data_ts：这份数据自己的时间戳（不是响应时间），前端据此渲染「缓存」角标。
    """
    out = dict(rep or {})
    out["data_source"] = source
    out["data_ts"] = cache_ts(rep)
    return out


# ---------------------------------------------------------------- 单只股票详情
# 自选 / 持仓里点开一只股票时用。它多半不在当日候选名单里，所以不能靠报告里的
# picks —— 这里现拉该股快照 + K线，按与候选股**完全相同**的口径打分，
# 并把它为什么没进候选逐条列出来。

_CODE_RE = re.compile(r"^(sh|sz|bj)\d{6}$")


def _annotate_moneyflow_reasons(picks: list[dict]) -> int:
    """给候选补「低位 + 主力净流入刚转正」这条理由，返回补了几条。

    ★ 两个必须守住的口径（v1.28.0 修）：

    ① **调用时机**：必须在 `flow_mod.evaluate(...)` **之后**。`p["moneyflow"]`
       是它写进去的；原来这段写在候选循环里（循环早于资金流调用结束），
       `p.get("moneyflow")` 永远是 None → `_main` 恒为 0 → 这条理由
       **从未产生过**（实测确认，界面上一行都没出现过）。

    ② **单位**：`moneyflow["main"]` 已经是**亿**（见 moneyflow.evaluate 里的
       `round(f["main"] / 1e8, 2)`）。原来又按"元"做了一次 `/1e8`，
       即便时序修对也只会显示 `+0.0 亿`。
    """
    n = 0
    for p in picks:
        try:
            pos = float((p.get("graph_detail") or {}).get("base_pos") or 1.0)
            main = float((p.get("moneyflow") or {}).get("main") or 0)
        except (TypeError, ValueError):
            continue
        if 0 < pos <= 0.4 and main > 0:
            p.setdefault("reasons", []).append(
                f"低位（60日位置 {pos*100:.0f}%）+ 主力净流入 "
                f"{main:+.1f} 亿 —— 资金刚动，要信早信")
            n += 1
    return n


def stock_detail(code: str) -> dict:
    """单只股票完整详情。返回 {ok, stock, error}。"""
    raw = (code or "").strip()
    code = universe.normalize(raw)
    if not _CODE_RE.match(code):
        return {"ok": False, "error": f"代码格式不对：{raw or '(空)'}"
                                       "（应为 sh600519 / sz000001 / bj430047，"
                                       "或直接填 6 位数字如 600519）"}

    cfg = sc.load_config()
    diag: dict = {}
    try:
        snaps = ds.fetch_snapshots([code], batch=1, workers=1, diag=diag)
    except Exception as e:
        return {"ok": False, "retryable": True,
                "error": f"取行情失败：{type(e).__name__}（网络问题，点「重试」再取一次）"}
    snap = snaps.get(code)
    if not snap:
        # 必须区分「传输失败」和「这代码真取不到」：前者是网络抖动，重试就好。
        # 以前一律报"可能是停牌/已退市/代码填错"，实测把 30% 的网络抖动误报成
        # "这股票不存在"，还引导用户去删自选 —— 用户看到的正是"一会能查到一会查不到"。
        if diag.get("failed_batches"):
            return {"ok": False, "retryable": True,
                    "error": ("行情源这次没响应（网络抖动或限速），不是代码的问题 —— "
                              "点「重试」再取一次。")}
        return {"ok": False,
                "error": (f"没取到 {code} 的行情 —— 可能是停牌、已退市，"
                          f"或代码/交易所前缀填错了（{code[:2]} 开头）。"
                          f"若确认代码无误，可在「管理」里核对这条记录。")}

    try:
        kl = ds.fetch_klines([code], n=70, workers=1)
    except Exception:
        kl = {}
    bars = kl.get(code) or []

    zt = ds.fetch_limit_up_pool()
    sess = trading.market_session()
    preopen = sess["phase"] in ("pre_open", "auction", "rest")
    cached = get_cached() or {}
    sentiment = cached.get("sentiment")
    if not sentiment:
        # 还没有可复用的缓存（进程刚起、或缓存还没写出来）：**不能默认
        # {"can_trade": True}** —— 那是同一类"没数据就说可以出手"的漏洞，
        # 只不过换到了个股详情这条路上。就绪信息由 datasource 出（本轮记账 +
        # 上一轮兜底），拿不出结论时按未就绪处理。个股详情里正真要用到情绪的
        # 只有下面一个 can_trade，所以这里只补这一个字段，字段形状与
        # evaluate_sentiment 保持一致。
        try:
            _h = ds.health_best()
        except Exception:                                   # noqa: BLE001
            _h = {}
        _deg = [d for d in (_h.get("degraded") or [])
                if str(d.get("key") or "").startswith("pool:")]
        sentiment = {"can_trade": not _deg, "data_ready": not _deg,
                     "not_ready_reason": (
                         "、".join(str(d.get("label") or d.get("key") or "")
                                   for d in _deg) + "接口本次未取到，无法判断情绪"
                         if _deg else ""),
                     "data_note": ""}
    volume = cached.get("volume") or {"mode": "unknown"}

    p = engine.score_one(cfg, code, snap, bars, zt, preopen=preopen)
    # 今日涨停原因（财联社）—— 涨停股才有；匹配不上就不显示
    try:
        _rz = cls_mod.limit_up_reasons().get(code)
    except Exception:
        _rz = None
    if _rz:
        p["zt_reason"] = _rz
    # 同花顺人气榜排名（在榜才有）
    try:
        _h = ths_mod.hot_rank().get(code)
    except Exception:
        _h = None
    if _h:
        p["hot_rank"] = _h["rank"]
        if _h.get("tag"):
            p["hot_tag"] = _h["tag"]
    _rg_one = regime.classify(sentiment, volume)
    p["swing"] = swing_mod.evaluate(bars, code, cfg, p=p, rg=_rg_one)
    # 波段「能不能买」结论（节点 × 阶段）—— 与 trading 波段门禁同源，详情页照着渲染
    p["swing"]["verdict"] = swing_mod.verdict(p["swing"], _rg_one)
    p["buy_signals"] = trading.buy_signals(p, bars, sentiment, volume, cfg)
    p["sell_signals"] = trading.sell_signals(p, bars, sentiment, volume, cfg)
    p["kline_tail"] = bars[-30:]
    p["tradable"] = bool(sentiment.get("can_trade", True)) and volume.get("mode") != "dry"
    p["block_note"] = ""
    if not p["tradable"]:
        if sentiment.get("trial_only") and volume.get("mode") != "dry":
            tw = float(sentiment.get("ebb_trial_weight") or 0.05)
            p["tradable"] = int(p.get("zt_boards") or 0) <= 1
            p["trial_only"] = True
            p["block_note"] = (f"退潮期只做首板试错，上限 {trading.pos_cn(tw)}；"
                               f"连板股不接力" if not p["tradable"]
                               else f"退潮期允许 {trading.pos_cn(tw)} 试错仓，只做首板")
        else:
            p["block_note"] = ("情绪退潮期，今天不新开仓"
                               if not sentiment.get("can_trade", True)
                               else "量能极度萎缩，今天不新开仓")
    return {"ok": True, "stock": p, "date": datetime.now().strftime("%Y-%m-%d")}


def _snapshot_prefilter(snaps: dict, cfg: dict, zt: list[dict], limit: int,
                        preopen: bool = False) -> list[str]:
    """快照层预筛 + 排序，返回优先拉K线的候选。

    preopen：盘前/集合竞价/休市时全市场换手与成交额都是 0（当日数据
    未形成），流动性门槛会让候选清空 —— 放开这两道门槛，名单按
    昨收状态作观察名单。
    """
    a = cfg.get("M4_aesthetic", {})
    ex = set(a.get("exclude_boards", []))
    pmin, pmax = a.get("price_min", 5.0), a.get("price_max", 10.0)
    mv_max, mv_mid = a.get("mv_max", 100.0), a.get("mv_mid_threshold", 500.0)
    zt_map = {r["code"]: r for r in zt}

    pool = []
    for code, s in snaps.items():
        if a.get("exclude_st") and ("ST" in s["name"].upper() or "退" in s["name"]):
            continue
        if universe.board_of(code) in ex:
            continue
        to = s.get("turnover", 0)
        if not preopen and (to < a.get("min_turnover", 1.0) or to > a.get("max_turnover", 25.0)):
            continue
        if not preopen and s.get("amount", 0) <= 0:
            continue
        mv = s.get("float_mv", 0) / 1e8
        if mv > mv_mid:
            continue
        pre = 0.0
        if pmin <= s["price"] <= pmax:
            pre += 40
        elif pmin * 0.7 <= s["price"] <= pmax * 1.4:
            pre += 24
        if mv <= mv_max:
            pre += 40
        elif mv <= mv_max * 2:
            pre += 20
        if code in zt_map:
            pre += 25 + min(zt_map[code].get("boards", 1), 7) * 4
        if to >= 3:
            pre += 10
        pool.append((pre, code))
    pool.sort(reverse=True)
    return [c for _, c in pool[:limit]]


def analyze(cfg: dict | None = None, date: str = "", kline_limit: int = 260,
            top_n: int = 20, use_cache: bool = True) -> dict:
    """完整分析。返回可直接 JSON 序列化的报告。

    并发约定：结果缓存的读写用 _lock（极短临界区），实际计算不持锁，
    避免长任务把 /api/status 等轻量接口一起卡死。
    """
    global _result_cache, _result_ts

    # 1) 缓存检查（持锁，极短）
    if use_cache:
        with _lock:
            if _result_cache and (time.time() - _result_ts) < RESULT_TTL:
                return _result_cache

    # 2) 实际计算（不持缓存锁，用独立计算锁防止重复劳动）
    with _compute_lock:
        # 双检：等锁期间可能已有其他线程算完
        if use_cache:
            with _lock:
                if _result_cache and (time.time() - _result_ts) < RESULT_TTL:
                    return _result_cache

        cfg = cfg or sc.load_config()
        t0 = time.time()
        date = date or ds.last_trade_date()

        # ---- 数据层 ----
        ds.reset_health()            # 每轮自查清零（数据健康度随报告一起给前端）
        _prog("拉取情绪池数据…", 0, 100)
        zt = ds.fetch_limit_up_pool(date)
        dt = ds.fetch_limit_down_pool(date)
        zb = ds.fetch_broken_board_pool(date)
        # 池接口忽略 date 入参，以接口自报的 qdate 为准
        real_date = ds.pool_qdate() or date

        # ---- 历史归档回补（开盘红）：缺哪天补哪天，补过/试过就不再发请求 ----
        hist_info = {"days": 0, "rest": 0, "filled": 0}
        try:
            bf = kph_mod.autobackfill(real_date)
            if bf.get("filled"):
                print(f"[pipeline] 历史归档回补: {', '.join(bf['filled'])}", file=sys.stderr)
            hist_info = {"days": len(ds.pool_history_dates()),
                         "rest": int(bf.get("rest") or 0),
                         "filled": len(bf.get("filled") or [])}
            # 对账修复：backfill 只补缺，写坏的归档它修不回来（实测 20261012
            # 只存了 16 只而真实 46 只）。这里用收盘复盘接口强制校正最近几天。
            # 每天只跑一次（kph 内部去重），失败不影响主流程。
            rp = kph_mod.repair_pool_history(real_date, days=3)
            if rp.get("repaired") or rp.get("moved"):
                print(f"[pipeline] 归档对账: 修正 {rp.get('repaired')} / "
                      f"移出错日期 {rp.get('moved')}", file=sys.stderr)
            hist_info["days"] = len(ds.pool_history_dates())
        except Exception as e:
            print(f"[pipeline] 归档回补失败: {e}", file=sys.stderr)
            hist_info["days"] = len(ds.pool_history_dates())

        # ---- 财联社涨停原因：挂到每只涨停股（详情页展示 + 随归档留存） ----
        try:
            _reasons = cls_mod.limit_up_reasons()
        except Exception:
            _reasons = {}
        if _reasons:
            for r in zt:
                r.setdefault("reason", _reasons.get(r.get("code", ""), ""))

        # ---- 同花顺人气榜：挂到涨停股（散户关注度，界面上只显示一行） ----
        try:
            _hot = ths_mod.hot_rank()
        except Exception:
            _hot = {}
        if _hot:
            for r in zt:
                h = _hot.get(r.get("code"))
                if h:
                    r["hot_rank"] = h["rank"]
                    if h.get("tag"):
                        r["hot_tag"] = h["tag"]

        idx = ds.fetch_index_snapshots(real_date)
        amount = sum(v["amount"] for v in idx.values()
                     if v["name"] in ("上证指数", "深证成指"))
        hist = ds.market_amount_history(20)

        # ---- 决策链 ----
        _prog("评估宏观/量能/情绪…", 0, 100)
        sess = trading.market_session()
        preopen = sess["phase"] in ("pre_open", "auction", "rest")
        # 涨停家数历史：★ 只有收盘后才是终值。原实现是"某日只要已存在就永不更新"，
        # 于是当天**首扫看板上几个就定稿** —— 实测 20261012 被钉死在 16 家，
        # 而当天真实涨停 46 家，整条"涨停家数曲线"与"昨日涨停"类比基准都被带偏。
        zth = ds.fetch_limit_up_history(
            10, real_date,
            final=sess["phase"] in ("just_closed", "after_close", "rest"))
        macro = engine.evaluate_macro(cfg)
        amt_yi = amount / 1e8 if amount > 1e5 else amount
        # 当日累计成交额只会增不会减 → 取高水位。挡两类丢失：
        #   · 只回了上证、没回深证 → sum 直接腰斩（比"没数据"更危险，会判成极度缩量）
        #   · 接口整次抖动 → 回退当日最后一份好数据（fetch_index_snapshots 已兜）
        #
        # ★ 但**盘前/集合竞价**的指数快照给的是**上一交易日**的全天额，不是今日累计。
        #   旧实现不看时段直接取高水位 → 今日一开盘就被昨收全天额锁死：实测
        #   2026-10-13 08:48 盘前把上一日 15378 亿写进今日高水位，11:29 实际已成交
        #   仅 9987 亿，界面却仍显示「两市已成交 15378 亿 · 比近期同期快 104%」，
        #   节奏倍数与进度条同步被放大。故只有真正形成当日累计的时段才允许入库；
        #   盘前/竞价把读数清零，由 load_last_amount_meta 的"上一交易日全天额"兜底。
        #   （休市/收盘后照常入库 —— 那时快照对应的正是 real_date 这一天的完整值。）
        live_read = sess["phase"] not in ("pre_open", "auction")
        amt_yi = ds.market_amount_hwm(real_date, amt_yi, live=live_read)
        amount = amt_yi * 1e8
        fb = ds.load_last_amount_meta(real_date)
        # 盘中折算用**近 N 日实测占比曲线**（写死的经验曲线在不同量能形态下偏差明显）；
        # ref_amount 给"与近期同期的节奏对比"当基准。注意：盘中**不外推全天额**
        # （见 engine.evaluate_volume 的说明），界面上只报已成交额 + 节奏。
        # 同期基准一次算齐（节奏对比与折算共用同一份样本，避免两处口径漂移）：
        #   · cum      实测「过去几天在同一时刻已成交多少钱」——节奏对比用它（钱对钱）
        #   · frac     实测「截至当前时刻的累计占比」——模式折算用它（≥3 天才给）
        #   · 两者都排除当日自身样本（今天的分子分母是同一个高水位值，比值恒≈1）
        # 两市成交额长序列（core/amount.py）：给界面显示「现在多少钱、处于
        # 最近多少分位、距峰值多远」。★ 它只用于**展示**，不参与 volume 的
        # 档位判定 —— 绝对档位是「钱够不够撬动大票」的物理约束，不该随市场
        # 整体下移而漂移；相对冷热已由 _relative_volume 的降档层表达。
        # 失败一律不拖垮主扫描（返回 ok:False，界面自己降级）。
        try:
            amt_series = amount_mod.summary()
        except Exception as e:      # noqa: BLE001
            amt_series = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        ibase = ds.intraday_baseline(sess.get("tmin", 240), exclude_date=real_date)
        volume = engine.evaluate_volume(cfg, amount, hist, session=sess,
                                        fallback_amount=fb["amount"],
                                        fallback_final=fb["final"],
                                        frac_hint=ibase["frac"],
                                        ref_amount=ds.recent_amount_median(
                                            8, exclude_date=real_date),
                                        ref_cum=ibase["cum"],
                                        ref_cum_days=ibase["days"])
        # 归档当日高水位：既进长期档案（次日盘前的基准），也进日内曲线（供折算校准）
        if amt_yi > 0:
            ds.save_market_amount(
                real_date, amt_yi,
                final=sess["phase"] in ("just_closed", "after_close", "rest"))
            if sess["phase"] in ("morning", "noon", "afternoon", "tail"):
                ds.save_intraday_amount(real_date, sess.get("tmin", 0), amt_yi)
        # 数据健康度：**必须在算情绪之前取** —— evaluate_sentiment 就绪闸门
        # 要拿它判"三池这一轮有没有取到"（见 core/engine.py 的说明）。此刻
        # 三池接口刚刚调过，_health 里的记账正是本轮的；之后 report 里再取一次
        # 也仍是同一份（后续健康项只会更多，不会更少）。
        _health_now = ds.data_health()
        sentiment = engine.evaluate_sentiment(cfg, zt, dt, zb, zth,
                                              real_date=real_date,
                                              tmin=sess.get("tmin"),
                                              health=_health_now)

        # ---- 选股 ----
        codes = ds.load_valid_codes()
        from_enum = not codes
        if from_enum:
            codes = universe.enumerate_all()
        _prog("扫描全市场快照…", 0, 100)
        snap_batch = 80
        diag: dict = {}
        snaps = ds.fetch_snapshots(codes, batch=snap_batch, workers=24, diag=diag,
                                   progress=lambda d, t: _prog(
                                       f"扫描全市场 {d*snap_batch}/{t*snap_batch} 只…", d, t))
        # ★ 腾讯这条链路偶发 RemoteDisconnected（用户实测「行情快照经常不通」）。
        #   整批失败时**不能让全市场扫描就此空手而归** —— 退到新浪快照：
        #   链路完全独立，字段是同一套（成交量单位已统一成手），只是慢一些
        #   （全市场约 2.2s vs 0.6s）。只在真丢数据时触发，正常情况零开销。
        #   判据用 failed_batches：它统计的是「整批传输失败」，
        #   不含「腾讯明确回答没有这个代码」（那是输入问题，换源也没用）。
        if diag.get("failed_batches") and len(snaps) < len(codes) * 0.9:
            _prog("腾讯快照不通，改用新浪备用源…", 0, 100)
            fb = ds.fetch_snapshots_sina(codes)
            if len(fb) > len(snaps):
                snaps = fb
                diag["fellback"] = "sina"
        if from_enum and snaps:
            # 当日首扫：把真实标的名单落盘，同日重扫少发 ~8 倍请求
            ds.save_valid_codes(sorted(snaps))
        # 「代码 → 名称」名单：名称搜索（加自选）的本地兜底数据源。
        # 每次全市场扫描顺手更新，行情源的名字永远比冷启动的板块表新
        # （加 ST 前缀、更名都能跟上）。失败不影响主流程。
        try:
            ds.remember_names(snaps)
        except Exception:                                  # noqa: BLE001
            pass
        _prog(f"粗筛候选（已扫 {len(snaps)} 只）…", 0, 100)
        pre = _snapshot_prefilter(snaps, cfg, zt, kline_limit, preopen=preopen)
        _prog(f"拉取 {len(pre)} 只K线…", 0, len(pre))
        kl = ds.fetch_klines(pre, n=70, workers=24,
                             progress=lambda d, t: _prog(
                                 f"拉取K线 {d}/{t}…", d, t))
        _prog("打分选股…", 0, 100)
        # 多取一截备筛：下面还要按"有没有可执行买点"过一遍，才能取满 top_n 只
        _want = max(int(top_n or 0), 1)
        # ★ 涨停基因表（E11，v1.38.0）：从归档算个股股性，接进打分。
        #   全表用「截至最后一个归档日」的累积 —— 实盘选股就该用截至昨天的全部历史。
        #   失败一律不拖垮主扫描（读归档是本地 IO，但仍防目录缺失/文件损坏）。
        gene_table = {}
        _gdates = []
        try:
            _gdates = sorted(ds.pool_history_dates())
            if _gdates:
                _gpools = {d: (ds.load_pool_history(d) or []) for d in _gdates}
                gene_table = gene_mod.build({}, _gdates, _gpools)
                _prog(f"股性表 {len(gene_table)} 只…", 0, 100)
        except Exception:                                  # noqa: BLE001
            gene_table = {}
        # ★ 股性表摘要（E11）：进报告供前端展示与自查。
        #   只放汇总数字，不放整张表（3441 只 × 10 字段会把报告撑爆）。
        #   口径：eligible = 首板历史够 MIN_FIRST 次的票（能参与判定的池子）；
        #        scored = 其中 seal 够到 TOP_SEAL、真会拿到加分的票。
        #   这两个数字之差是"有历史但股性一般"，不是错误 —— 大多数票都在这里。
        _a_gene = cfg.get("M4_aesthetic", {}) or {}
        _top_seal = float(_a_gene.get("gene_top_seal", gene_mod.TOP_SEAL)
                          or gene_mod.TOP_SEAL)
        _min_first = int(_a_gene.get("gene_min_first", gene_mod.MIN_FIRST)
                         or gene_mod.MIN_FIRST)
        try:
            _elig = [v for v in gene_table.values()
                     if (v.get("n_first") or 0) >= _min_first]
            _scored = [v for v in _elig
                       if (v.get("seal") or 0) >= _top_seal]
            gene_report = {
                "enabled": bool(_a_gene.get("use_gene_seal", True)),
                "weight": _a_gene.get("weight_gene_seal", 8),
                "days": len(_gdates),
                "tracked": len(gene_table),
                "eligible": len(_elig),
                "scored": len(_scored),
                "top_seal": _top_seal,
                "min_first": _min_first,
                "summary": (
                    f"按 {len(_gdates)} 个归档日建了 {len(gene_table)} 只票的股性；"
                    f"其中 {len(_elig)} 只有足够首板历史参与判定，"
                    f"{len(_scored)} 只股性够好（会拿到 +{_a_gene.get('weight_gene_seal', 8)} 分）"
                ),
            }
        except Exception:                                  # noqa: BLE001
            gene_report = {"enabled": False, "summary": "股性表本轮不可用"}
        scored = engine.screen(cfg, {c: snaps[c] for c in kl if c in snaps}, kl,
                               zt, macro, volume, sentiment, top_n=_want * 2,
                               preopen=preopen, gene_table=gene_table)

        # ---- 买卖点 ----
        for p in scored:
            bars = kl.get(p["code"], [])
            # 波段体系先算（买卖点要按打法排序，得先有 swing / style_fit）
            # p / rg 传进去，顺带算出 playbook（该用波段还是短线）——
            # p 里此时已有 zt_boards（engine.screen 写的），高位判定要用。
            _rg_one = regime.classify(sentiment, volume)
            p["swing"] = swing_mod.evaluate(bars, p["code"], cfg, p=p, rg=_rg_one)
            p["swing"]["verdict"] = swing_mod.verdict(p["swing"], _rg_one)
            p["buy_signals"] = trading.buy_signals(p, bars, sentiment, volume, cfg)
            p["sell_signals"] = trading.sell_signals(p, bars, sentiment, volume, cfg)
            p["kline_tail"] = bars[-30:]
            if _reasons.get(p["code"]):
                p["zt_reason"] = _reasons[p["code"]]
            h = _hot.get(p["code"])
            if h:
                p["hot_rank"] = h["rank"]
                if h.get("tag"):
                    p["hot_tag"] = h["tag"]

        # ★ 候选股 = 「今天有可执行买点」的票（用户 10-13 反馈）：
        #   原来榜单只按分数排前 N，导致两类票白占候选位、被读成推荐 ——
        #     · 已涨停的连板股（体系对它们一律"不接力/不追"）
        #     · 尚未涨停、买点写着"不是首板就别碰"的票
        #   现在只保留 buy_signals 里真的存在 kind=buy 的票；只有观察/禁买提示的
        #   不进候选（它们在"涨停池"表与详情页里仍然可见，信息没丢）。
        picks = [p for p in scored
                 if any((b or {}).get("kind") == "buy"
                        for b in (p.get("buy_signals") or []))][:_want]
        excluded_n = len(scored) - len(picks)
        pool_b2 = sum(1 for z in zt if int(z.get("boards") or 0) >= 2)
        picks_note = ""
        if excluded_n or pool_b2:
            bits = []
            if pool_b2:
                bits.append(f"涨停名单里有 {pool_b2} 只 2 板以上的票 —— "
                            "体系一律不接力，未列入候选")
            if excluded_n:
                bits.append(f"另有 {excluded_n} 只打分靠前但今日没有可执行买点，"
                            "也未列入")
            picks_note = "；".join(bits) + "。"

        plan = trading.position_plan(cfg, picks, sentiment, volume, macro)

        # ---- M9 题材主线五信号（消息定级→合力→中军→老龙→次日验证） ----
        _prog("评估题材主线五信号…", 0, 100)
        ml = mainline.evaluate(cfg, zt=zt, snaps=snaps, real_date=real_date)

        # ---- 业绩披露期 ----
        m7 = engine.evaluate_earnings(cfg)

        # 题材证伪警示（题材/配置二分）：情绪题材碰上披露期或非做长节点，
        # 按「披露期是照妖镜」的逻辑进 warnings —— 负向结论只进 warnings，
        # 绝不进「为什么选它」的 reasons（项目规矩：详情页理由只放正向）。
        _rg = regime.classify(sentiment, volume)
        _in_disc = bool(m7.get("in_window"))
        for p in picks:
            _nat = ((p.get("style_fit") or {}).get("nature") or {})
            if _nat.get("key") == "theme" and (_in_disc or not _rg["allow_swing"]):
                p.setdefault("warnings", []).append(
                    "题材证伪风险：情绪题材 + 披露期/退潮节点 —— 照妖镜下无业绩"
                    "题材易被证伪（龙头可 -30%）；只按短线纪律做，别做长")

        m6 = engine.evaluate_message(cfg, zt)

        # ---- E1 情绪温度计（连板梯队/炸板率/晋级率/涨跌家数） ----
        _prog("构建情绪温度计…", 0, 100)
        meter = sentiment_meter.build(zt, dt, zb, snaps, real_date, cfg,
                                      health=_health_now)

        # ---- 盘中实时异动（打开涨停板 = 正在炸板；仅交易时段拉取） ----
        intraday = {}
        if sess["phase"] in ("morning", "afternoon", "tail"):
            try:
                ch = ds.fetch_stock_changes("8208", ttl=45)
                seen_codes, opened = set(), []
                for it in ch:
                    if it["code"] in seen_codes:
                        continue
                    seen_codes.add(it["code"])
                    opened.append(it)
                    if len(opened) >= 6:
                        break
                if opened:
                    intraday["opened"] = opened
            except Exception:
                pass

        # ---- E2 龙虎榜·游资席位（数据只进「资金在干什么」结论，不上界面） ----
        _prog("拉取龙虎榜…", 0, 100)
        lhb_data = lhb_mod.evaluate(cfg, real_date, zt)

        # ---- E3 主力资金流（板块 + 候选股 + 资金结论 + 板块资金记忆） ----
        _prog("拉取主力资金流…", 0, 100)
        flow = flow_mod.evaluate(cfg, picks, snaps, ml.get("theme", ""), zt,
                                 lhb=lhb_data, real_date=real_date)

        # 野人：资金代表异动，「要信早信」—— 低位 + 主力净流入刚转正 = 资金刚动。
        # ★ 必须在 flow_mod.evaluate **之后**（p["moneyflow"] 是它写进去的），
        #   详见 _annotate_moneyflow_reasons 的说明。
        _annotate_moneyflow_reasons(picks)

        # ---- 板块主线/支线/强势判断 ----
        sectors = mainline.sector_view(ml, flow, zt)
        # 板块轮动（近 N 日每日领涨板块）—— 财联社数据源。抓不到就留空列表，
        # 界面据此隐藏该块；绝不编造"轮动方向"。
        try:
            sectors["rotation"] = cls_mod.sector_rotation(4)
        except Exception:                                   # noqa: BLE001
            sectors["rotation"] = []

        # ---- E5 低位承接方向（退潮期机构兑现的钱去哪） ----
        # 输入全部是上面已经算好的东西（情绪/量能/资金/板块强弱），不新增数据源。
        # ★ 必须在 flow / sectors / lhb_data 都就绪之后。
        rebound = rebound_mod.evaluate(cfg, sentiment, volume, flow, sectors,
                                       lhb=lhb_data, ml=ml, real_date=real_date)

        # ---- E6 停牌·补位推演（龙头停牌了，同题材里谁在接棒） ----
        # 用户 10-13：新华传媒（出版·10 连板）昨晚公告停牌核查，今天
        # 「出版」6 家涨停（新经典/龙版传媒/新华文轩…），系统当时完全没接住。
        # 数据全部来自**已经拉到的东西**：全市场快照（有报价 + 成交额 0 +
        # 换手 0 = 全天无撮合 = 停牌）与本地涨停池归档（谁有连板基因、
        # 属于哪个题材），不新增接口。盘前/集合竞价那段快照区分不出停牌
        # （全市场都是 0），改走停牌公告 —— news 自带 5 分钟缓存，与
        # mainline 共用，不额外增加源站压力。
        substitute = substitute_mod.evaluate(cfg, snaps=snaps, zt=zt, session=sess,
                                             real_date=real_date)

        # ---- E7 逆势连板票（产业逻辑候选）· 独立通道 ----
        # 用户 10-13 的九阳股份案例：它在 10-08 / 10-12 两个退潮日照常连板
        # 走到 6 板（+61%），而系统因「退潮 can_trade=False + 候选不超过 1 板」
        # 完全看不见它。缺口不是数据是尺子 —— 情绪周期量的是"接力资金"，
        # 它靠的是独立于情绪的产业逻辑。这个模块只回答"谁的连板不是情绪撑的"。
        # 放在 substitute 之后：两者都依赖已归档的涨停池，且都不需要额外数据源。
        industry = industry_mod.evaluate(cfg, snaps=snaps, zt=zt, session=sess,
                                         real_date=real_date)

        # ---- E8 题材播种（事件 → 受益 → 标的）· 前置题材池 ----
        # 用户 10-13「打板要提前找题材，不然看谁封板打谁是乱玩」。候选股是
        # 当天量价打分，形态是结果不是原因；缺的是"事件→受益→标的"的前瞻映射
        # 与题材生命周期（首日/N日/退潮/重复启动）。全部离线：从涨停池 reason
        # 富文本抽标签与事件，从归档算题材日历/独苗/重复启动，零新增接口。
        sowing = sowing_mod.evaluate(cfg, snaps=snaps, zt=zt, session=sess,
                                     real_date=real_date)

        # ---- E9 竞价早读（集合竞价 09:15–09:25 → 今天该盯什么）----
        # 用户 10-14「做个当前集合竞价时的分析板块」。系统原先把竞价只当**时段
        # 标签**（market_session 的 phase="auction"、pipeline 在此段把成交额清零），
        # 竞价本身从没被当成数据读过 —— 用户在 09:20 打开界面看到的还是昨天的结论。
        # 腾讯快照里竞价匹配价（price[3]）一直在拉，本模块把它读出来。
        # 昨日涨停池从归档取（"昨涨停今天怎么开"是打板体系最该看的一栏）。
        # 复用 sentiment_meter._prev_pool —— 它已经定义了"上一个归档日"的口径
        # （池子归档只在收盘后落盘，盘中取的必然是更早的交易日），
        # 不再另写一份，免得两处对"昨天"的定义漂移。
        # 放在 sowing 之后：同样只依赖 snaps + 归档，零新增接口。
        _prog("读集合竞价…", 0, 100)
        prev_zt_date, prev_zt = meter_mod._prev_pool(real_date)
        auction = auction_mod.evaluate(cfg, snaps=snaps, zt=zt, session=sess,
                                       real_date=real_date, prev_zt=prev_zt)
        auction["prev_zt_date"] = prev_zt_date

        # ---- 明日判断（情绪/量能/题材/资金/板块 → 次日预案） ----
        _prog("推演明日判断…", 0, 100)
        next_outlook = outlook.build(cfg, real_date, sentiment, volume,
                                     meter=meter, ml=ml, flow=flow,
                                     sectors=sectors, macro=macro, plan=plan,
                                     sowing=sowing)

        # ---- E4 信号复盘档案（胜率闭环；t+1/涨停不可买/停牌/成本约束） ----
        _prog("结算信号档案…", 0, 100)
        journal_data = journal_mod.run(cfg, real_date, picks, kl)

        # ---- E10 轻量回测（把 E4 的逐笔收益聚合成组合级净值曲线） ----
        # 纯本地计算（读 signal_journal.json），无网络、无 K线拉取。
        # 放在 E4 之后：要用刚结算完的档案，否则最新一批样本进不来。
        _prog("回测组合表现…", 0, 100)
        backtest_data = backtest_mod.run(cfg)

        # ---- 持仓体检（本地持仓/自选） ----
        _prog("持仓体检…", 0, 100)
        port_raw = portfolio_mod.load()
        hold_codes = [h["code"] for h in port_raw.get("holdings", [])]
        kl_hold = ds.fetch_klines(hold_codes, n=70, workers=8) if hold_codes else {}
        port = portfolio_mod.evaluate(snaps, kl_hold, sentiment, volume, cfg)
        port["raw"] = port_raw   # 原始记录（代码/成本/股数），前端按钮状态与面板要用
        # 野人：去弱留强 —— 手里是后排、市场有更强方向，就要换（退潮期更要换）
        rot_checks = portfolio_mod.rotation_check(
            port.get("holdings") or [], ml,
            stage=(sentiment or {}).get("stage", ""))
        if rot_checks:
            port["rotation_checks"] = rot_checks
            port["checks"] = (port.get("checks") or []) + rot_checks

        # ---- M8 纪律体检：并入 E1-E3 与持仓的告警 ----
        m8 = engine.evaluate_discipline(cfg, sentiment, volume, macro, m7, ml)
        extra = list(lhb_data.get("checks", [])) + list(flow.get("checks", [])) \
            + list(port.get("checks", []))
        if extra:
            m8["checks"] = m8["checks"] + extra
            worst = m8.get("status", "green")
            for c in extra:
                if c.get("level") == "red":
                    worst = "red"
                    break
                if c.get("level") == "yellow" and worst != "red":
                    worst = "yellow"
            m8["status"] = worst

        report = {
            "meta": {
                "version": APP_VERSION,
                "date": real_date,
                "requested_date": date,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                # epoch 秒：前端的静默刷新靠它判断"服务器上有没有更新的一份"
                "ts": time.time(),
                "session": sess,
                "preset": cfg.get("preset"),
                "preset_name": cfg.get("name", ""),
                "elapsed": round(time.time() - t0, 1),
                "scanned": len(snaps),
                "scan_list": "全量枚举" if from_enum else "当日有效名单",
                "kline_ok": len(kl),
                "candidate": len(pre),
            },
            "index": idx,
            "macro": macro,
            "macro_events": engine.MACRO_EVENTS,
            "volume": volume,
            "market_amount": amt_series,
            # ★ 这里原来写的是 market_amount_history() 的 **volume** 字段 ——
            #   那是**上证指数自己的成交量（手）**，实测 20261014 为 445962554，
            #   而同日真实两市成交额是 14144 亿。既不含深证那一半、也不是成交额，
            #   字段名与含义不符。改为直接透传 core/amount.py 的真实两市额，
            #   同时保留 volume 字段（上证成交量）不删 —— 老调用方仍能读到。
            "volume_history": hist[-20:],
            "amount_history": (
                [{"date": r["date"], "yi": round(r["yi"], 1)} for r in
                 (amount_mod.two_market_series(20) or [])]
                if amt_series.get("ok") else []),
            "sentiment": sentiment,
            "meter": meter,
            "intraday": intraday,
            "history": hist_info,
            "mainline": ml,
            "gene": gene_report,
            "sectors": sectors,
            "rebound": rebound,
            "substitute": substitute,
            "industry": industry,
            "sowing": sowing,
            "auction": auction,
            "outlook": next_outlook,
            "flow": flow,
            "journal": journal_data,
            "backtest": backtest_data,
            "portfolio": port,
            "picks": picks,
            # 候选口径说明：哪些票因为"今日无买点/连板不接力"没进候选，如实告知
            "picks_note": picks_note,
            "position": plan,
            "earnings": m7,
            "message": m6,
            "discipline": m8,
            "limit_up_pool": sorted(zt, key=lambda x: -x.get("boards", 1))[:30],
            # 数据健康度自查：哪些关键数据这轮降级/用的是旧快照（前端如实标注）
            "data_health": _health_now,
            # ★ 就绪闸门结论**提到顶层**：报告里 sentiment.data_ready 只覆盖 M3
            #   情绪那一块，而"这一份报告到底是不是在缺数据的情况下算出来的"
            #   是整份报告的性质（候选池、仓位链、纪律检查都建立在同一批数据上）。
            #   顶层给一个显式字段，前端与自查脚本不必知道它藏在 sentiment 里。
            "data_ready": bool(sentiment.get("data_ready", True)),
            "config": cfg,
        }
        # 写缓存（持锁，极短）；同时落盘 —— 过期/重启后还能先端出上次结果
        with _lock:
            _result_cache = report
            _result_ts = time.time()
        save_disk_report(report)
        return report
