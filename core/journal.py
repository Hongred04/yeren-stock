# -*- coding: utf-8 -*-
"""E4 信号复盘档案 —— 买卖信号的胜率闭环（带回测正确性约束）。

每次扫描把**可执行的买入信号**记进档案（data/signal_journal.json），
之后用同一套约束回验它们的 1/3/5 日表现，逐日累计出每个信号的
样本数 / 胜率 / 平均收益 —— 回答「B1 首板、B2 回踩这些信号到底准不准」。

回测正确性清单（借鉴同类项目的踩坑结论，逐条落实）：
  · t+1 成交：信号是收盘后给的 → 一律按**次日开盘价**进场（防未来函数）；
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
    d0 = e.get("date", "")
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
    for h in (1, 3, 5):
        j = idx + h
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
        "overall": _agg(entries),
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
