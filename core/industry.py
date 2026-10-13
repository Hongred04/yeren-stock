# -*- coding: utf-8 -*-
"""E7 逆势连板票（产业逻辑候选）· 独立通道（扩展模块，非野人原体系）。

一句话：**情绪退潮了，谁的连板还在延续？**

来历（用户 10-13 的九阳股份案例）：九阳股份（sz002242）在 **10-08（退潮日）
与 10-12（退潮日）两个情绪退潮的交易日里照常连板**（4 板 / 6 板），
一路从 09-28 首板走到 6 板（首板 10.33 → 6 板 16.64，**+61%**）。
而系统当时对它是**完全看不见**的：

  · M3 退潮期 `can_trade = False` → 不给任何买点；
  · `M4_aesthetic.max_candidate_boards = 1`（v1.32.4 加的）→ 6 板直接不进候选。

**缺口不是数据，是尺子。** 情绪周期那把尺子量的是"接力资金"——
退潮期没有接力，所以情绪票会集体消失；而它靠的是**独立于情绪的产业逻辑**
（持有深思考 AI 机器人 7.92% 股权），于是它照样连板。
一把尺子量两种东西，必然把后者判成"不存在"。

判定要素（全部用本地归档 + 快照，不新增接口）：

  ① **逆势** —— 连板链**跨越过逆势日**。逆势日 = 情绪阶段 `ebb`，
     或当日涨停家数较前一交易日骤降（≤ 60%）。这是本模块的核心：
     情绪票在退潮日撑不出连板链，能撑住的，大概率不是情绪驱动。
     实测九阳：链包含 10-08、10-12 两个退潮日（`sentiment_history` 均为 ebb）。
  ② **高度** —— 连板高度 ≥ `min_boards`（默认 3）。
  ③ **形态**（两条都是"主力没走"的证据）：
     · **缩量一字** —— 某板集合竞价即封（首封 ≤ 09:30）且 `break_count == 0`，
       换手较前一板显著萎缩（≤ `shrink_ratio`）。
       实测九阳 10-08：一字未炸、换手 **1.02%**（前板 4.59%）。
     · **炸板回封** —— `break_count ≥ reseal_breaks` 而当日仍收在涨停
       （只要它在归档里就说明收盘封住了）。
       实测九阳 10-09：炸 **6 次**、13:28 回封、换手 18.28%。
  ④ **位置** —— 首板日流通市值 ≤ `mv_max`（默认 100 亿）。
     ★ 市值用**当前快照反推**（股本不变 → 首板市值 = 今日流通市值 × 首板价 ÷ 今价），
     不用归档里的 `float_mv`：那个字段跨源口径不一致（实测同一只票
     09-28 记 25.6 亿、09-30 记 95.2 亿，股本基数不同），反推才可靠。
     实测九阳首板约 **79 亿** —— 合格。

定位（必须说清，否则就是变相荐股）：
**这不是"可以买了"的信号，而是"这类票不该被情绪闸门一刀切掉"的提醒。**
它只回答"谁的连板不是情绪撑的"，不回答"现在还能不能上"——
后者要看它今天是否**已经断板**（`still` 字段如实给），以及你自己看得懂看不懂。

已知限制：
  · 归档里 **10-12 那种"东财没抓到、只由复盘接口补上"的日期**，
    字段口径不同（复盘 `break_count` 恒为 0、换手/市值大 ~3 倍），
    形态判定会因此漏判 —— 已在 `_merge_pool_rows` 里尽量保留东财值，
    但仍无法凭空补出缺失的那一天。
  · 逆势日的判定依赖 `sentiment_history`（历史较短）与 `zt_history`
    （涨停家数骤降代理），冷启动当天可能标不出逆势日。
"""
from __future__ import annotations

import json

from . import datasource as ds
from . import paths

# 逆势日：当日涨停家数 ≤ 前一交易日 × 此值 → 视为"骤降日"
_ZT_DROP = 0.60


def _f(v, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _int(v, d: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


# ---------------------------------------------------------------- 逆势日

def ebb_days() -> set[str]:
    """逆势日集合 = 情绪阶段 ebb ∪ 涨停家数骤降日。

    两个来源互补：`sentiment_history` 是权威口径但历史短（冷启动只有几天）；
    `zt_history` 的涨停家数曲线长，用环比骤降做代理。实测 10-08 两者都命中，
    10-12 只有 sentime 命中（家数 46 vs 前日 69 = 0.67，没过 0.6 的线）。
    """
    out: set[str] = set()
    hist = _read_json(paths.data_path("sentiment_history.json"), [])
    for h in hist if isinstance(hist, list) else []:
        if isinstance(h, dict) and str(h.get("stage") or "") == "ebb":
            out.add(str(h.get("date") or ""))
    zt = _read_json(paths.data_path("zt_history.json"), [])
    zt = sorted([h for h in (zt if isinstance(zt, list) else [])
                 if isinstance(h, dict) and h.get("date")], key=lambda x: x["date"])
    for a, b in zip(zt, zt[1:]):
        na, nb = _int(a.get("limit_up")), _int(b.get("limit_up"))
        if na > 0 and nb <= na * _ZT_DROP:
            out.add(str(b["date"]))
    out.discard("")
    return out


# ---------------------------------------------------------------- 连板链

def chains(days: int, upto: str = "") -> dict[str, list[dict]]:
    """{code: [该票在各归档日的记录（按日期升序）]}。"""
    out: dict[str, list[dict]] = {}
    dates = [d for d in ds.pool_history_dates() if not upto or d <= upto]
    for date in dates[-days:]:
        for row in ds.load_pool_history(date):
            code = str(row.get("code") or "")
            if code:
                out.setdefault(code, []).append(dict(row, _date=date))
    return out


# ---------------------------------------------------------------- 形态

def _shrink_limit(chain: list[dict], ratio: float) -> dict | None:
    """缩量一字：集合竞价即封 + 未炸 + 换手较前一板显著萎缩。"""
    for prev, cur in zip(chain, chain[1:]):
        first = _int(cur.get("first_time"))
        if not first or first > 93000:
            continue                     # 不是"开盘即封"
        if _int(cur.get("break_count")) != 0:
            continue                     # 炸过板就不是一字
        t_prev, t_cur = _f(prev.get("turnover")), _f(cur.get("turnover"))
        if t_prev > 0 and 0 < t_cur <= t_prev * ratio:
            return {"date": cur.get("_date"), "boards": _int(cur.get("boards")),
                    "turnover": round(t_cur, 2),
                    "prev_turnover": round(t_prev, 2)}
    return None


def _reseal(chain: list[dict], min_breaks: int) -> dict | None:
    """炸板回封：炸了 N 次以上仍收在涨停（在归档里即收盘封住）。"""
    best = None
    for cur in chain:
        b = _int(cur.get("break_count"))
        if b >= min_breaks and (best is None or b > _int(best.get("breaks"))):
            best = {"date": cur.get("_date"), "boards": _int(cur.get("boards")),
                    "breaks": b}
    return best


# ---------------------------------------------------------------- 主入口

def evaluate(cfg: dict | None = None, snaps: dict | None = None,
             zt: list[dict] | None = None, session: dict | None = None,
             real_date: str = "") -> dict:
    """逆势连板票（产业逻辑候选）。

    返回 {enabled, active, reason, mode, picks[], summary, note}。
    冷启动/无逆势日时 active=False 并说明原因，不硬凑。
    """
    a = (cfg or {}).get("E7_industry", {}) or {}
    enabled = bool(a.get("enabled", True))
    empty = {"enabled": enabled, "active": False, "reason": "", "mode": "逆势连板",
             "picks": [], "summary": "", "note": ""}
    if not enabled:
        empty["reason"] = "模块已关闭"
        return empty

    snaps = snaps or {}
    zt = zt or []
    days = _int(a.get("lookback_days"), 10)
    min_boards = _int(a.get("min_boards"), 3)
    top_n = _int(a.get("top_n"), 5)
    mv_max = _f(a.get("mv_max"), 100.0)
    shrink_ratio = _f(a.get("shrink_ratio"), 0.7)
    reseal_breaks = _int(a.get("reseal_breaks"), 3)

    chains_ = chains(days, upto=real_date)
    if not chains_:
        empty["reason"] = ("本地还没有涨停池归档（冷启动）—— "
                           "判不出「谁的连板跨过了退潮」，等积累几个交易日")
        return empty

    ebb = ebb_days()
    zt_codes = {str(z.get("code") or "") for z in zt}

    picks = []
    for code, chain in chains_.items():
        chain.sort(key=lambda r: str(r.get("_date") or ""))
        max_boards = max(_int(r.get("boards")) for r in chain)
        if max_boards < min_boards:
            continue
        dates = [str(r.get("_date")) for r in chain]
        hit = sorted(d for d in dates if d in ebb)
        if not hit:
            continue                     # 没跨过逆势日 → 不属本模块射程
        named = [r for r in chain if r.get("name")]
        name = str((named[-1] if named else chain[-1]).get("name") or code)
        sector = ""
        for r in reversed(chain):
            if r.get("sector"):
                sector = str(r["sector"])
                break
        sl = _shrink_limit(chain, shrink_ratio)
        rs = _reseal(chain, reseal_breaks)
        flags = []
        if sl:
            flags.append(f"缩量一字（{sl['date']}，{sl['boards']}板，换手 "
                         f"{sl['turnover']}% ← 前板 {sl['prev_turnover']}%）")
        if rs:
            flags.append(f"炸板回封（{rs['date']}，{rs['boards']}板，"
                         f"炸 {rs['breaks']} 次仍封住）")

        score = 20.0                                  # 跨过逆势日 = 入场券
        score += min(30.0, 8.0 * max_boards)          # 高度
        score += 25.0 if sl else 0.0                  # 缩量一字
        score += 20.0 if rs else 0.0                  # 炸板回封
        # 首板市值那 15 分放到后面算 —— 要用日K里的**真实首板价**（见下）

        picks.append({
            "code": code, "name": name, "sector": sector,
            "max_boards": max_boards, "first_date": dates[0], "last_date": dates[-1],
            "ebb_days": hit, "flags": flags, "mv_first": None, "mv_ok": True,
            "still": code in zt_codes, "score": round(score, 1), "how": "",
        })

    picks.sort(key=lambda p: (-p["score"], -p["max_boards"]))
    picks = picks[:max(top_n * 2, top_n)]

    # ---- 首板市值：用日K里 **首板那天的收盘价** 反推 ----
    # ★ 为什么不用归档里的 float_mv：那个字段跨源口径不一致（实测同一只票
    #   09-28 记 25.6 亿、09-30 记 95.2 亿，股本基数不同），拿它比毫无意义。
    #   股本不变 → 首板市值 = 今日流通市值 × 首板价 ÷ 今价。实测九阳 ≈ 79 亿。
    if picks and a.get("mv_check", True):
        try:
            kl = ds.fetch_klines([p["code"] for p in picks], n=60, workers=8)
        except Exception:                                    # noqa: BLE001
            kl = {}
        for p in picks:
            close0 = 0.0
            for b in (kl.get(p["code"]) or []):
                if str(b.get("date", "")).replace("-", "") == p["first_date"]:
                    close0 = _f(b.get("close"))
                    break
            snap = snaps.get(p["code"]) or {}
            p_now, mv_now = _f(snap.get("price")), _f(snap.get("float_mv"))
            if close0 > 0 and p_now > 0 and mv_now > 0:
                p["mv_first"] = round(mv_now / 1e8 * close0 / p_now, 1)
                p["mv_ok"] = p["mv_first"] <= mv_max
            else:
                p["mv_first"] = None
                p["mv_ok"] = True            # 取不到就不扣分（不猜）
            p["score"] = round(p["score"] + (15.0 if p["mv_ok"] else 0.0), 1)
    for p in picks:
        p["how"] = _how(p["name"], p["max_boards"], p["ebb_days"], p["flags"],
                        p["still"], p["mv_first"], mv_max)

    picks.sort(key=lambda p: (-p["score"], -p["max_boards"]))
    picks = picks[:top_n]

    if not picks:
        empty["reason"] = (f"扫了近 {days} 个归档交易日，没有连板 ≥{min_boards} 板"
                           f"且跨过逆势日的票 —— 这一轮没有逆势连板")
        return empty

    names = "、".join(f"{p['name']}（{p['max_boards']} 板）" for p in picks)
    alive = [p for p in picks if p["still"]]
    summary = (f"逆势连板 {len(picks)} 只：{names}。"
               + (f"其中 {len(alive)} 只今天仍在涨停。" if alive else
                  "今天都已不在涨停池里（断板/停牌）。")
               + "这批票的驱动不是情绪周期，情绪闸门对它们不适用 —— "
                 "单独看，不要混进情绪票的仓位口径里。")

    return {
        "enabled": True, "active": True, "reason": "", "mode": "逆势连板",
        "picks": picks, "summary": summary,
        "note": ("这张卡只回答「谁的连板不是情绪撑的」—— **不代表现在能买**。"
                 "逆势连板票的赔率与情绪票完全不同：它要么是产业逻辑驱动"
                 "（可以给独立通道），要么是资金硬做（断起来同样快）。"
                 "务必先看是否仍在涨停池：今天已断板的，形态再好也只是复盘。"
                 "停牌/退市风险照常存在，本卡不构成买入指令。"),
    }


def _how(name: str, boards: int, hit: list[str], flags: list[str],
         still: bool, mv0, mv_max: float) -> str:
    """单只票的口径：把"证据"与"现状"分开说。"""
    head = (f"{name} 的连板链跨过了 {len(hit)} 个逆势日（{'、'.join(hit)}），"
            f"最高 {boards} 板"
            + (f"，首板流通市值约 {mv0:.0f} 亿" if mv0 else "") + "。")
    ev = ("证据：" + "；".join(flags) + "。") if flags else \
        "还没出现「缩量一字 / 炸板回封」这类强封板形态。"
    now = ("它今天仍在涨停池里。" if still else
           "它今天已不在涨停池（断板或停牌）—— 下面这些只当复盘看。")
    tail = ("口径：逆势连板不是买点，是「这只票的驱动可能不在情绪周期里」的提醒；"
            "真要参与，按产业逻辑的节奏做（看逻辑破没破），"
            "别用情绪票的「打板/接力」口径，也别因为「退了潮」就无视它。")
    return head + ev + now + tail
