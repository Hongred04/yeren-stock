# -*- coding: utf-8 -*-
"""两市成交额长序列 —— 「退潮期」最直接的读数。

为什么需要这个模块
------------------
量能是野人体系里**模式总开关**（M2 量能定模式），而 M2 判断用的
`market_amount_history()` 返回的是**上证指数自己的成交量（手）**，
既不是两市口径、也没有深证那一半。实测 20261014：
  · 系统 volume_history 里那条 = 445962554（上证成交量，手）
  · 真实两市成交额            = 14144 亿
字段名与实际含义完全不符，且**只有上海一半**。任何拿它做的
「量能趋势」判断都是错的。

数据源选型（试过 4 个，只有 1 个给成交额）
------------------------------------------
  ✗ 东财 push2his        —— 本机 IP 被 push2 族封禁（RemoteDisconnected）
  ✗ 腾讯 ifzq / 新浪     —— 日K **只给成交量（手）**，没有成交额
  ✗ 通达信（mootdx）     —— 未安装（core.datasource._TDX_AVAILABLE=False）
  ✓ 同花顺 d.10jqka.com.cn —— 日K 第 7 个字段就是成交额（元），
     可回溯数年（接口 total=8740 天），GBK 解码，无需 cookie。
     实测 20261013 上证 = 779302290000 元 = 7793 亿，与快照口径吻合。

口径：两市成交额 = 上证指数(1A0001) 成交额 + 深证成指(399001) 成交额。
      实测 20261014：上证 6625 亿 + 深证 7519 亿 = 14144 亿 ——
      与报告里 volume.amount=14144.0 完全一致，确认这是系统真口径。

★ 实测现状（20260317~20261013，140 个交易日）—— 为什么这个模块重要
------------------------------------------------------------------
两市成交额从 5 月的 31801 亿/日均一路降到 10 月的 16991 亿/日均，
**五个月腰斩过半**（峰值 20260622 的 37372 亿 → 最新 16759 亿，-55.2%）。
逐月日均：5月 31801 / 6月 31117 / 7月 26828 / 8月 22390 / 9月 18148 / 10月 16991。

而 M2 量能档位的绝对阈值是 `peak 15000 / comfort 12000 / shrink 8000` 亿。
拿真实序列对照：**comfort 12000 亿与 shrink 8000 亿在 140 天里 100% 达标** ——
"缩量存量""极度缩量"两档从未触发过，连最低的 14092 亿那天都判"量能舒适"。
`15000 亿` 位于 1.4% 分位。也就是说绝对档位**只剩"峰值/舒适"两档在动**，
这正是报告里出现「成交额绝对值够（14144亿）」这句反直觉文案的原因：
在一个成交额已腰斩过半的市场里，系统说"钱够"。

★ 但**不要**简单地把阈值按分位重标（n=140 验证过，是错的）
------------------------------------------------------------
按 80/50/20 分位重标为 30692/24315/19460 亿，最近 15 天会有 11 天判
`dry`（advice = "极度缩量…建议空仓等待量能恢复"）。而同期涨停家数是
32~101 只 —— 市场显然不冷。分位阈值是**相对**的，它随市场整体下移而
下移，"正常日"会被系统性地判成"极端日"。

真正的修法是**绝对档位只回答"钱够不够撬动大票"（物理约束，不该随
时间漂移）**，而"相对最近是多是少"交给已有的相对分位修正层
（core/engine.py `_relative_volume`，rel<0.45 降档）去表达。
所以本模块只负责**提供真实数据与分位读数供展示**，不改判定阈值。
"""
from __future__ import annotations

import json
import statistics
import urllib.request

THS_URL = "https://d.10jqka.com.cn/v6/line/hs_{code}/01/last.js"
_HDRS = {"User-Agent": "Mozilla/5.0",
         "Referer": "https://stockpage.10jqka.com.cn/"}
_SH = "1A0001"      # 上证指数
_SZ = "399001"      # 深证成指

# 同花顺日K字段：0日期 1开 2高 3低 4收 5成交量(手) 6成交额(元) ...
_IDX_AMOUNT = 6


def _ths_amounts(code: str) -> dict[str, float]:
    """取同花顺指数日K → {日期(YYYYMMDD): 成交额(元)}。失败返回空字典。"""
    req = urllib.request.Request(THS_URL.format(code=code), headers=_HDRS)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            txt = r.read().decode("gbk", "replace")
        obj = json.loads(txt[txt.index("(") + 1:txt.rindex(")")])
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    out: dict[str, float] = {}
    for line in str(obj.get("data") or "").split(";"):
        p = line.split(",")
        if len(p) <= _IDX_AMOUNT:
            continue
        try:
            out[p[0]] = float(p[_IDX_AMOUNT])
        except ValueError:
            continue
    return out


def two_market_series(days: int = 0) -> list[dict]:
    """两市成交额日序列（升序）。

    返回 [{date:'YYYYMMDD', sh:元, sz:元, total:元, yi:亿元}, ...]
    days>0 时只返回最近 days 天。两个指数没有共同日期（接口改版）返回 []。
    """
    sh, sz = _ths_amounts(_SH), _ths_amounts(_SZ)
    if not sh or not sz:
        return []
    rows = []
    for d in sorted(set(sh) & set(sz)):
        tot = sh[d] + sz[d]
        rows.append({"date": d, "sh": sh[d], "sz": sz[d],
                     "total": tot, "yi": tot / 1e8})
    return rows[-days:] if days and days > 0 else rows


def summary(rows: list[dict] | None = None) -> dict:
    """给报告用的量能趋势摘要。rows 为空时自己去取。"""
    rows = rows if rows is not None else two_market_series()
    if not rows:
        return {"ok": False, "reason": "取不到两市成交额（同花顺接口不可用）"}
    yi = [r["yi"] for r in rows]
    cur, med = yi[-1], statistics.median(yi)
    out = {
        "ok": True, "days": len(rows),
        "start": rows[0]["date"], "end": rows[-1]["date"],
        "latest": round(cur, 0), "median": round(med, 0),
        "min": round(min(yi), 0), "max": round(max(yi), 0),
        "pct_rank": round(sum(1 for x in yi if x < cur) / len(yi) * 100, 0),
        "vs_median_pct": round((cur / med - 1) * 100, 1) if med else None,
    }
    hi = max(rows, key=lambda r: r["yi"])
    out["peak_date"] = hi["date"]
    out["peak"] = round(hi["yi"], 0)
    out["from_peak_pct"] = round((cur / hi["yi"] - 1) * 100, 1)
    for w in (5, 10, 20, 60):
        if len(yi) >= 2 * w:
            a = statistics.mean(yi[-w:])
            b = statistics.mean(yi[-2 * w:-w])
            out[f"ma{w}"] = round(a, 0)
            out[f"chg{w}_pct"] = round((a / b - 1) * 100, 1)
    return out
