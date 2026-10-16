# -*- coding: utf-8 -*-
"""E4 信号复盘档案 —— 买卖信号的胜率闭环（带回测正确性约束）。

每次扫描把**可执行的买入信号**记进档案（data/signal_journal.json），
之后用同一套约束回验它们的 1/3/5 日表现，逐日累计出每个信号的
样本数 / 胜率 / 平均收益 —— 回答「B1 首板、B2 回踩这些信号到底准不准」。

回测正确性清单（借鉴同类项目的踩坑结论，逐条落实）：
  · t+1 成交：信号是收盘后给的 → 一律按**次日开盘价**进场（防未来函数）；
  · 持有期口径：进场在 T+1 开盘，「持有 h 日」= 卖在 T+1+h 的**收盘**
    （v1.41.4 修正：原先误取 T+h 收盘，h=1 时与进场同日 → 算成日内涨跌
      却标成「1日」，胜率被系统性低估）；
  · 涨停不可买：次日开盘贴涨停（一字/秒板）→ 记「未成交」，不计入胜率；
  · 停牌过滤：次日无K线（停牌/退市）→ 记「未成交」；
  · 成本计入：佣金万2.5×2 + 印花税 0.05%（卖出）+ 滑点 0.05%×2 ≈ 0.2%，
    直接从收益扣（cost_pct 可配）；
  · 逐日结算：ret1/ret3/ret5 能算多少先落多少，ret5 出来才算最终结算；
  · 口径如实：不复权价、收盘结算、短线窗口内的保守近似（详情页标注）。

与主扫描的关系：只读 picks 与 K线，**永不改写买卖点**；结算走 K线缓存，
每轮最多补拉 max_settle_per_scan 只，控制数据源压力。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

from . import datasource as ds
from . import indicators as ind
from . import paths

JOURNAL_PATH = paths.data_path("signal_journal.json")
MAX_ENTRIES = 800                 # 档案上限（超出丢最老的）


def _f(v, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _norm_date(v) -> str:
    """'2026-10-09' → '20261009'。"""
    return str(v or "").replace("-", "").replace("/", "")[:8]


def _days_between(d0: str, d1: str) -> int:
    try:
        a = datetime.strptime(_norm_date(d0), "%Y%m%d")
        b = datetime.strptime(_norm_date(d1), "%Y%m%d")
        return (b - a).days
    except ValueError:
        return 0


# ---------------------------------------------------------------- 存取

def _load() -> list[dict]:
    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
            js = json.load(f)
        return js.get("entries") or []
    except (OSError, json.JSONDecodeError, ValueError):
        return []


def _save(entries: list[dict]) -> None:
    try:
        os.makedirs(os.path.dirname(JOURNAL_PATH), exist_ok=True)
        tmp = JOURNAL_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "entries": entries}, f, ensure_ascii=False)
        os.replace(tmp, JOURNAL_PATH)
    except OSError:
        pass


# ---------------------------------------------------------------- 记录

def record(entries: list[dict], real_date: str, picks: list[dict]) -> int:
    """把本轮可执行的买入信号记进档案（同日同股同信号去重）。"""
    seen = {e.get("id") for e in entries}
    n = 0
    for p in picks or []:
        sig = None
        for s in p.get("buy_signals") or []:
            if s.get("blocked") or s.get("type") == "HOLD":
                continue
            sig = s
            break
        if not sig:
            continue                     # 全是禁买/涨停不追 → 不记
        eid = f"{real_date}:{p.get('code')}:{sig.get('type')}"
        if eid in seen:
            continue
        entries.append({
            "id": eid, "date": real_date, "code": p.get("code"),
            "name": p.get("name", ""), "sector": p.get("sector", ""),
            "signal": sig.get("type"), "signal_name": sig.get("name"),
            "score": p.get("score"), "close": p.get("price"), "pct": p.get("pct"),
            "status": "open", "ret1": None, "ret3": None, "ret5": None,
            "entry_open": None, "entry_gap": None, "skip_reason": "",
            "recorded_ts": time.time(),
        })
        seen.add(eid)
        n += 1
    return n


# ---------------------------------------------------------------- 结算

def _settle_one(e: dict, bars: list[dict], real_date: str, cost: float) -> str:
    """结算一条档案。返回 'settled' / 'skip' / 'pending'。"""
    # ★ 日期归一化后再比（v1.41.4）：原来写 `_norm_date(b.date) == d0`，
    #   左边归一化成 'YYYYMMDD'，右边 d0 是**原样**的 `e["date"]`。生产环境
    #   `record()` 存的 real_date 来自 `ds.pool_qdate()`，本来就是 8 位无分隔，
    #   所以一直能匹配上 —— 但只要档案里的日期带连字符（'YYYY-MM-DD'，
    #   即 `_norm_date` 存在就是为了处理的那种形式），匹配**恒为 False** →
    #   走到 idx is None 分支，被当成「K线窗口里没有信号日」。
    #   后果：≤25 天时静默返回 pending（永远结算不了、胜率样本永远不增长），
    #   >25 天时判成「超出结算窗口」并被 skip 掉 —— 明明有完整K线却算不出来。
    #   是自己写测试夹具时用 'YYYY-MM-DD' 才暴露的：夹具没错，比较写错了。
    d0 = _norm_date(e.get("date", ""))
    idx = None
    for i, b in enumerate(bars):
        if _norm_date(b.get("date")) == d0:
            idx = i
            break
    if idx is None:
        # K线窗口（70 根）里没有信号日：太老 / 期间退市，无法结算 → 过期归档
        if _days_between(d0, real_date) > 25:
            e["status"] = "skip"
            e["skip_reason"] = "超出结算窗口（K线覆盖不足），无法结算"
            return "skip"
        return "pending"

    # ① 次日 bar 不存在：可能是还没走出来，也可能是停牌/退市
    if idx + 1 >= len(bars):
        if _days_between(d0, real_date) > 10:
            e["status"] = "skip"
            e["skip_reason"] = "次日停牌/退市：无K线（买不到）"
            return "skip"
        return "pending"
    nxt = bars[idx + 1]
    prev_close = _f(bars[idx].get("close"))
    entry_open = _f(nxt.get("open")) or _f(nxt.get("close"))
    if prev_close <= 0 or entry_open <= 0:
        return "pending"

    # ② 涨停不可买：次日开盘贴涨停 → 记未成交
    gap = (entry_open / prev_close - 1) * 100
    th = ind.limit_up_threshold(e.get("code", ""))
    if gap >= th - 0.2:
        e["status"] = "skip"
        e["entry_open"] = round(entry_open, 2)
        e["entry_gap"] = round(gap, 2)
        e["skip_reason"] = f"次日开盘贴涨停（高开 {gap:.1f}%），按买不到处理"
        return "skip"

    # ③ t+1 开盘进场，逐档结算（含成本）
    e["entry_open"] = round(entry_open, 2)
    e["entry_gap"] = round(gap, 2)
    progress = False
    # ★ 持有期口径修正（v1.41.4）：进场在 idx+1 的**开盘**，所以「持有 h 个交易日」
    #   的卖出点必须是 idx+1+h 的收盘。原来写 j = idx + h —— h=1 时卖出点
    #   = idx+1 的**收盘**，与进场是**同一天**，算出来的是「进场当日日内涨跌」，
    #   而不是持有 1 日的收益。列名却标着「1日/3日/5日」，用户看到的口径是错的。
    #   实测（同 28 笔样本）：旧「1日」胜率 14.3%、均 -1.80%；
    #   真·持有 1 日 胜率 32.1%、均 -0.08% —— 胜率被低估一半以上。
    for h in (1, 3, 5):
        j = idx + 1 + h
        if j < len(bars):
            c = _f(bars[j].get("close"))
            if c > 0:
                e[f"ret{h}"] = round(c / entry_open - 1 - cost, 4)
                progress = True
    if e.get("ret5") is not None:
        e["status"] = "settled"
        return "settled"
    if progress:
        e["status"] = "partial"
        return "settled"                 # 有进展，记为一次结算动作
    # ③' 一直算不出来且已过很久：停牌/窗口不足，放弃
    if _days_between(d0, real_date) > 25:
        e["status"] = "skip"
        e["skip_reason"] = "长时间无K线（停牌/退市），无法结算"
        return "skip"
    return "pending"


def settle(entries: list[dict], real_date: str, a: dict,
           kl: dict | None = None) -> tuple[int, int]:
    """结算到期的档案。返回 (settled_or_progress, skipped)。

    每轮最多补拉 max_settle_per_scan 只 K线；本轮扫描已有的 K线直接复用。
    """
    cost = _f(a.get("cost_pct"), 0.002)
    cap = int(_f(a.get("max_settle_per_scan"), 30)) or 30
    todo = [e for e in entries
            if e.get("status") in ("open", "partial")
            and e.get("date", "") < real_date]
    if not todo:
        return 0, 0
    # open（还没进过场）优先、老档案优先 —— 先把最早的样本结出来
    todo.sort(key=lambda e: (0 if e.get("status") == "open" else 1, e.get("date", "")))

    codes: list[str] = []
    for e in todo:
        c = e.get("code")
        if c and c not in codes:
            if len(codes) >= cap:
                break
            codes.append(c)
    data: dict[str, list[dict]] = {}
    for c in codes:
        bars = (kl or {}).get(c)
        if bars:
            data[c] = bars
    need = [c for c in codes if c not in data]
    if need:
        try:
            data.update(ds.fetch_klines(need, n=70, workers=8))
        except Exception:
            pass

    settled = skipped = 0
    for e in todo:
        bars = data.get(e.get("code")) or []
        if not bars:
            continue
        r = _settle_one(e, bars, real_date, cost)
        if r == "settled":
            settled += 1
        elif r == "skip":
            skipped += 1
    return settled, skipped


# ---------------------------------------------------------------- 统计

def _agg(group: list[dict]) -> dict:
    res = {"n": len(group)}
    for h in (1, 3, 5):
        vals = [e[f"ret{h}"] for e in group if e.get(f"ret{h}") is not None]
        res[f"n{h}"] = len(vals)
        res[f"win{h}"] = round(sum(1 for v in vals if v > 0) / len(vals), 3) if vals else None
        res[f"avg{h}"] = round(sum(vals) / len(vals), 4) if vals else None
        res[f"best{h}"] = round(max(vals), 4) if vals else None
        res[f"worst{h}"] = round(min(vals), 4) if vals else None
    res["n_settled"] = sum(1 for e in group if e.get("status") == "settled")
    res["n_skip"] = sum(1 for e in group if e.get("status") == "skip")
    res["n_open"] = sum(1 for e in group if e.get("status") in ("open", "partial"))
    return res


def _traps(entries: list[dict]) -> list[dict]:
    """踩坑雷达 —— 野人：「打交割单，找出反复踩坑的那个点，一定规避掉」。

    对每类信号看最近 8 笔已结算样本（按时间倒序）：亏损 ≥5 笔（胜率≤37.5%）
    就标红 —— 这不是信号烂，是**你用它的方式在亏钱**，先停用、复盘、规避。
    """
    by_key: dict[tuple, list[dict]] = {}
    for e in entries:
        # ★ 状态口径修正（v1.28.0）：`_settle_one` 只会写
        #   open / settled / partial / skip —— 从来没有 win / loss（那是更早版本的
        #   设想，全代码库搜不到第二处）。原来按 ("win","loss") 过滤 → by_key 恒空
        #   → 踩坑雷达**永远返回空列表**，前端「反复踩同一个坑」那张卡片从未显示过。
        #   正确口径 = "已结算且有 1 日收益"：settled（1/3/5 日都出来了）+
        #   partial（已有 1/3 日、5 日还没到，ret1 已经能算）。skip 是买不到/停牌，
        #   不参与胜率统计。
        if e.get("status") not in ("settled", "partial"):
            continue
        if e.get("ret1") is None:
            continue
        by_key.setdefault((e.get("signal"), e.get("signal_name")), []).append(e)
    out = []
    for (t, name), g in by_key.items():
        g = sorted(g, key=lambda e: (e.get("date", ""), e.get("id", "")),
                   reverse=True)[:8]
        if len(g) < 5:
            continue
        losses = sum(1 for e in g if _f(e.get("ret1")) < 0)
        if losses >= 5:
            out.append({
                "type": t or "?", "name": name or t or "?",
                "recent_n": len(g), "recent_losses": losses,
                "note": f"最近 {len(g)} 笔亏 {losses} 笔 —— 反复踩同一个坑，"
                        "先停用这个信号，想清楚为什么亏再碰",
            })
    out.sort(key=lambda x: -x["recent_losses"])
    return out


def _dedup_by_stock(entries: list[dict]) -> list[dict]:
    """按 (日期, 代码) 去重，同一票同一天只留一条（保留信号优先级最高的一条）。

    ★ 为什么 overall 必须去重（v1.41.4）：`record()` 的去重键是
      `f"{date}:{code}:{sig.type}"` —— 含信号类型。同一只票同一天被 B0/B1/B2
      多路命中，就会记成多条**完全相同收益**的记录。按信号分组统计时这是对的
      （每个信号各自算自己的命中率），但 `overall` 把它们加总会把同一笔交易
      重复加权：实测 201 条记录里只有 158 只唯一票，样本虚增 21%。
      `backtest.py` 早就记过这条教训（不去重时胜率 65.3% → 去重后明显不同），
      但 journal 的 overall 一直没跟上。

    保留哪一条：按信号在 `_SIGNAL_RANK` 里的优先级，取最「靠前」的那个信号，
    让 overall 反映的是「这天我实际会做什么」，而不是随便取一条。
    """
    rank = {"B2": 0, "W1": 1, "W2": 2, "B1": 3, "B0": 4}
    best: dict[tuple, dict] = {}
    for e in entries:
        k = (e.get("date", ""), e.get("code", ""))
        if k not in best:
            best[k] = e
            continue
        a, b = best[k], e
        ra = rank.get(a.get("signal"), 9)
        rb = rank.get(b.get("signal"), 9)
        if rb < ra:
            best[k] = b
    return list(best.values())


def stats(entries: list[dict]) -> dict:
    by_key: dict[tuple, list[dict]] = {}
    for e in entries:
        by_key.setdefault((e.get("signal"), e.get("signal_name")), []).append(e)
    rows = []
    for (t, name), g in sorted(by_key.items(), key=lambda kv: -len(kv[1])):
        rows.append({"type": t or "?", "name": name or t or "?", **_agg(g)})
    recent = sorted(entries, key=lambda e: (e.get("date", ""), e.get("id", "")),
                    reverse=True)[:40]
    keys = ("date", "code", "name", "signal", "signal_name", "close",
            "entry_open", "entry_gap", "ret1", "ret3", "ret5",
            "status", "skip_reason")
    return {
        "stats": rows,
        "traps": _traps(entries),
        # overall 按票去重（见 _dedup_by_stock）；rows/_traps 仍按信号各算各的
        "overall": _agg(_dedup_by_stock(entries)),
        "recent": [{k: e.get(k) for k in keys} for e in recent],
    }


# ---------------------------------------------------------------- 入口

def run(cfg: dict | None = None, real_date: str = "",
        picks: list[dict] | None = None,
        kl: dict | None = None) -> dict:
    """记录 + 结算 + 统计。任何异常都不允许影响主扫描。"""
    a = (cfg or {}).get("E4_journal", {})
    if not a.get("enabled", True):
        return {"enabled": False, "stats": [], "recent": [],
                "overall": _agg([]), "note": "信号复盘模块已关闭"}
    try:
        entries = _load()
        n_new = record(entries, real_date, picks or [])
        n_settled, n_skip = settle(entries, real_date, a, kl or {})
        if len(entries) > MAX_ENTRIES:
            entries = entries[-MAX_ENTRIES:]
        _save(entries)
        st = stats(entries)
        return {
            "enabled": True, "n_entries": len(entries), "n_new": n_new,
            "n_settled": n_settled, "n_skip": n_skip,
            "cost_pct": _f(a.get("cost_pct"), 0.002),
            **st,
            "note": "T+1 开盘进场 · 涨停不可买 · 停牌过滤 · 已扣交易成本 —— "
                    "样本积累需要时间，新信号的 3/5 日结果要等几天才有",
        }
    except Exception as e:                       # noqa: BLE001（绝不带崩主扫描）
        return {"enabled": True, "stats": [], "recent": [],
                "overall": _agg([]),
                "error": f"{type(e).__name__}: {e}"}
