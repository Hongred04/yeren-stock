# -*- coding: utf-8 -*-
"""M9 题材主线五信号引擎。

一条题材主线行情的成立，需要 5 个信号按时间顺序依次共振，缺一环就是「一日游」：
  S1 消息定级别 —— 消息分量决定行情空间的上限；消息不够大，后面都不用看。
  S2 首日看合力 —— 开盘 3-60 分钟内，题材内 ≥3-5 家个股同日涨停，且板块指数带量
                  突破新高。多股齐涨停 = 资金集体行动，个股孤军奋战不算。
  S3 中军定容量 —— 板块内必须有百亿市值以上的大票参与；没有中军 = 游资脉冲，
                  当天冲高就结束。
  S4 老龙是前兆 —— 上一轮的龙头股先异动。资金有记忆和路径依赖，老龙异动是新行情的引信。
  S5 次日定生死 —— 主线不是当天确认的，次日开盘用真金白银检验：空间龙候选高溢价、
                  昨日首板活口留存率高、中军无负反馈，三者齐备主线地位才成立。

数据口径与降级：
  题材 = 涨停池自带的东财行业板块（hybk）做代理分组；
  板块指数 / 成分股 = 东财行业板块接口 best-effort，取不到时降级为「未验证/弱验证」；
  昨日涨停池 / 老龙档案 = 本地 data/pool_history/ 逐日归档积累，冷启动时对应信号
  显示「待验证/积累中」而非失败。
"""
from __future__ import annotations

from . import datasource as ds
from . import news as news_mod

# 消息级别定义：级别决定行情空间的上限
MESSAGE_LEVELS = {
    "S": {"name": "S级 · 国家级顶层战略", "ceiling": "空间上限：主线大级别", "gate_ok": True},
    "A": {"name": "A级 · 部委政策 + 产业事件共振", "ceiling": "空间上限：中级行情", "gate_ok": True},
    "B": {"name": "B级 · 行业级事件/景气数据", "ceiling": "空间上限：局部主线", "gate_ok": True},
    "C": {"name": "C级 · 个股公告/传闻", "ceiling": "撑不起主线：脉冲后回落", "gate_ok": False},
    "auto": {"name": "未录入", "ceiling": "未定级按观察处理：请在「调策略 → M9」录入消息级别", "gate_ok": False},
}

STATUS_CN = {
    "confirmed": "主线成立",
    "day1_pending": "首日启动 · 待次日验证",
    "watch": "信号不全 · 仅观察",
    "one_day": "一日游特征 · 回避接力",
    "disabled": "模块已关闭",
}

_SIGNAL_STATE_CN = {"pass": "过", "weak": "弱", "pending": "待验证",
                    "unknown": "积累中", "fail": "不过"}


def signal_state_cn(state: str) -> str:
    return _SIGNAL_STATE_CN.get(state, state)


# ---------------------------------------------------------------- 工具

def _parse_hhmmss(s: str, default: int = 103000) -> int:
    """'10:30' → 103000。"""
    try:
        hh, mm = str(s).split(":")
        return int(hh) * 10000 + int(mm) * 100
    except (ValueError, AttributeError):
        return default


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _resolve_bk(sector: str) -> str:
    """题材名 → 东财行业板块代码（BKxxxx），best-effort。"""
    if not sector:
        return ""
    boards = ds.fetch_board_list()
    if not boards:
        return ""
    if sector in boards:
        return boards[sector]
    for name, bk in boards.items():
        if name and (name in sector or sector in name):
            return bk
    return ""


# ---------------------------------------------------------------- 题材聚类

def _cluster_themes(zt: list[dict], deadline: int) -> list[dict]:
    """涨停池按行业板块聚类。早封 = 9:30-截止时间；一字 = 集合竞价即封。"""
    groups: dict[str, dict] = {}
    for r in zt:
        sec = (r.get("sector") or "").strip() or "未分类"
        g = groups.setdefault(sec, {"sector": sec, "zt": 0, "early": 0, "one_word": 0,
                                    "max_boards": 0, "amount": 0.0, "stocks": []})
        g["zt"] += 1
        ft = _int(r.get("first_time"))
        if ft <= 0:
            pass
        elif ft < 93000:
            g["one_word"] += 1
        elif ft <= deadline:
            g["early"] += 1
        b = _int(r.get("boards"), 1)
        g["max_boards"] = max(g["max_boards"], b)
        g["amount"] += _float(r.get("amount"))
        g["stocks"].append({"code": r.get("code", ""), "name": r.get("name", ""),
                            "boards": b, "first_time": ft})
    out = sorted(groups.values(),
                 key=lambda g: (g["early"] + g["one_word"], g["zt"], g["max_boards"]),
                 reverse=True)
    for i, g in enumerate(out):
        g["rank"] = i + 1
        g["stocks"].sort(key=lambda s: -s["boards"])
        g["stocks"] = g["stocks"][:8]
    return out


# ---------------------------------------------------------------- 五个信号

def _signal_message(a: dict, theme_name: str = "",
                    suggestion: dict | None = None) -> dict:
    """S1 消息定级别。

    手动录入（message_level != auto）永远优先；未录入时若开启 auto_message
    且系统从快讯中定级成功，则采用「系统建议级别」，证据（命中新闻）随信号输出。
    """
    lv = a.get("message_level") or "auto"
    info = MESSAGE_LEVELS.get(lv, MESSAGE_LEVELS["auto"])
    note = (a.get("message_note") or "").strip()
    rules = ["先判断消息分量：消息级别决定行情空间的上限，消息不够大，后面都不用看"]

    ev_head = (suggestion or {}).get("evidence", [])[:2]
    ev_str = "；".join(f"{e['time'][5:16]} {e['text'][:46]}" for e in ev_head)

    if lv == "auto":
        if a.get("auto_message", True) and suggestion and suggestion.get("found"):
            s_info = MESSAGE_LEVELS.get(suggestion["level"], MESSAGE_LEVELS["auto"])
            state = "pass" if s_info["gate_ok"] else "fail"
            ev = (f"自动定级 {s_info['name']}（依据近{suggestion.get('window_h', 36)}小时"
                  f"{suggestion.get('scanned', 0)}条快讯与主题材的关联匹配）"
                  + (f"：{ev_str}" if ev_str else ""))
            detail = rules + ["自动定级按关键词规则：国家战略S → 部委政策A → 行业事件B → "
                              "个股公告C；只统计与主题材相关的快讯，如有出入请手动录入覆盖"]
        else:
            state = "unknown"
            ev = "消息级别：未定级。" + (
                f"自动快讯未检测到与主题材「{theme_name}」直接相关的政策/事件消息"
                if a.get("auto_message", True)
                else "自动定级已关闭，请在「调策略 → M9」录入消息级别")
            detail = rules + ["盘中快速轮动启动的题材，快讯可能滞后 —— 人工确认后可手动录入"]
    else:
        state = "pass" if info["gate_ok"] else "fail"
        ev = f"消息级别：{info['name']}。{info['ceiling']}"
        if note:
            ev += f"｜今日消息：{note}"
        if suggestion and suggestion.get("found"):
            s_info = MESSAGE_LEVELS.get(suggestion["level"], MESSAGE_LEVELS["auto"])
            ev += f"｜快讯扫描也见{s_info['name']}" + (f"：{ev_str}" if ev_str else "")
        detail = rules

    return {"id": "S1", "name": "消息定级别", "state": state, "evidence": ev,
            "detail": detail}


def _index_breakout(bars: list[dict], surge_ratio: float) -> tuple[str, str]:
    """板块指数带量突破新高判定 → (状态, 说明)。"""
    if not bars or len(bars) < 21:
        return "unverified", "板块指数数据不足，未验证"
    last, prior = bars[-1], bars[:-1]
    window = prior[-60:]
    px_high = max(b["close"] for b in window)
    ma5_amt = sum(b["amount"] for b in prior[-5:]) / 5.0
    ratio = (last["amount"] / ma5_amt) if ma5_amt else 0.0
    if last["close"] >= px_high and ratio >= surge_ratio:
        return "pass", (f"板块指数创 {len(window)} 日新高，量能为前5日均量 "
                        f"{ratio:.1f} 倍（带量突破）")
    if last["close"] >= px_high:
        return "weak", f"板块指数创 {len(window)} 日新高，但量能仅前5日均量 {ratio:.1f} 倍（未带量）"
    gap = (px_high / last["close"] - 1) * 100
    return "fail", f"板块指数距 {len(window)} 日高点尚有 {gap:.1f}%，未突破"


def _signal_consensus(a: dict, top: dict | None) -> dict:
    """S2 首日看合力：题材内多家齐涨停（早封为主）+ 板块指数带量新高。"""
    min_zt = _int(a.get("min_theme_zt"), 3)
    min_early = _int(a.get("min_early_seal"), 2)
    if not top:
        return {"id": "S2", "name": "首日看合力", "state": "fail",
                "evidence": "今日涨停池为空或无题材标签，不存在题材合力", "detail": []}
    early_total = top["early"] + top["one_word"]
    consensus_ok = top["zt"] >= min_zt and early_total >= min_early

    idx_state, idx_note = "unverified", "板块指数未验证（数据源不可用）"
    try:
        bk = _resolve_bk(top["sector"])
        if bk:
            bars = ds.fetch_board_kline(bk, 70)
            idx_state, idx_note = _index_breakout(bars, _float(a.get("board_volume_surge"), 1.3))
    except Exception:
        pass

    detail = [
        f"最强题材「{top['sector']}」：全日涨停 {top['zt']} 家，开盘60分钟内封板 "
        f"{top['early']} 家（一字 {top['one_word']} 家），最高 {top['max_boards']} 板",
        f"板块指数：{idx_note}",
    ]
    if not consensus_ok:
        state = "fail"
        detail.append(f"合力不足：同日涨停/早封家数未达 {min_zt}/{min_early} —— "
                      f"孤军奋战不算启动")
    elif idx_state == "pass":
        state = "pass"
    else:
        state = "weak"
        if idx_state == "fail":
            detail.append("合力达标但板块指数未创新高 —— 先按启动观察，等指数确认")

    leader = top["stocks"][0] if top["stocks"] else None
    ev = (f"「{top['sector']}」同日涨停 {top['zt']} 家、早封 {early_total} 家"
          + (f"，高度龙头 {leader['name']} {leader['boards']} 板" if leader else ""))
    return {"id": "S2", "name": "首日看合力", "state": state, "evidence": ev,
            "detail": detail, "consensus_ok": consensus_ok, "index_state": idx_state}


def _signal_midarmy(a: dict, top: dict | None, zt: list[dict]) -> dict:
    """S3 中军定容量：题材内百亿市值大票是否参与。"""
    mv_min = _float(a.get("midarmy_mv_min"), 100.0)
    pct_min = _float(a.get("midarmy_pct_min"), 3.0)
    min_cnt = _int(a.get("midarmy_min_count"), 1)
    if not top:
        return {"id": "S3", "name": "中军定容量", "state": "fail",
                "evidence": "无主题材，无法核查中军", "detail": [], "army": []}

    army: list[dict] = []
    src = "板块成分股"
    try:
        bk = _resolve_bk(top["sector"])
        if bk:
            cons = ds.fetch_board_constituents(bk)
            army = [c for c in cons if c["float_mv"] >= mv_min and c["pct"] >= pct_min]
            army.sort(key=lambda c: -c["float_mv"])
    except Exception:
        pass
    if not army:
        # 降级：用题材内涨停大票弱验证（涨停池自带流通市值）
        src = "题材内涨停大票（弱验证）"
        for r in zt:
            if (r.get("sector") or "") != top["sector"]:
                continue
            mv = _float(r.get("float_mv")) / 1e8
            if mv >= mv_min:
                army.append({"code": r.get("code", ""), "name": r.get("name", ""),
                             "float_mv": mv, "pct": _float(r.get("pct"))})

    army = army[:6]
    names = "、".join(f"{c['name']}({c['float_mv']:.0f}亿 {c['pct']:+.1f}%)" for c in army) or "无"
    if len(army) >= min_cnt:
        state = "pass" if src == "板块成分股" else "weak"
        ev = f"{src}：{len(army)} 家百亿以上大票在动 —— {names}"
        detail = [f"大资金进场，行情才有容量和持续性（门槛：流通≥{mv_min:.0f}亿且涨幅≥{pct_min:.0f}%）"]
    else:
        state = "fail"
        ev = f"题材内未见流通 {mv_min:.0f} 亿以上大票异动 —— 没有中军，大概率是游资脉冲"
        detail = ["没有中军 = 大资金未进场，行情没有容量，当天冲高就结束"]
    return {"id": "S3", "name": "中军定容量", "state": state, "evidence": ev,
            "detail": detail, "army": army}


def _leader_archive(real_date: str, min_boards: int) -> list[dict]:
    """从本地涨停池归档构建「老龙档案」：历史上连板达到门槛的个股。"""
    leaders: dict[str, dict] = {}
    for d in ds.pool_history_dates():
        if real_date and d >= real_date:
            continue
        for r in ds.load_pool_history(d):
            b = _int(r.get("boards"), 1)
            if b < min_boards:
                continue
            code = r.get("code", "")
            cur = leaders.get(code)
            if not cur:
                leaders[code] = {"code": code, "name": r.get("name", ""),
                                 "sector": r.get("sector", ""),
                                 "max_boards": b, "last_date": d}
            else:
                if b > cur["max_boards"]:
                    cur["max_boards"] = b
                    cur["sector"] = r.get("sector", cur["sector"])
                cur["last_date"] = max(cur["last_date"], d)
    return sorted(leaders.values(), key=lambda x: -x["max_boards"])[:40]


def _signal_old_dragon(a: dict, real_date: str, snaps: dict[str, dict],
                       zt: list[dict], theme_name: str) -> dict:
    """S4 老龙是前兆：上一轮龙头今日是否先异动。"""
    min_boards = _int(a.get("old_dragon_min_boards"), 3)
    pct_min = _float(a.get("old_dragon_pct"), 5.0)
    archive = _leader_archive(real_date, min_boards)
    if not archive:
        return {"id": "S4", "name": "老龙是前兆", "state": "unknown",
                "evidence": "老龙档案积累中（系统会逐日记录涨停池，几天后即可验证）",
                "detail": ["资金有记忆和路径依赖 —— 上一轮龙头先异动，往往是新行情的引信"],
                "dragons": []}

    today_zt = {r.get("code") for r in zt}
    movers, watchers = [], 0
    for d in archive:
        snap = snaps.get(d["code"])
        pct = _float(snap.get("pct")) if snap else None
        hit = d["code"] in today_zt or (pct is not None and pct >= pct_min)
        row = {**d, "pct": pct, "today_zt": d["code"] in today_zt,
               "same_theme": bool(theme_name) and d.get("sector") == theme_name}
        if hit:
            movers.append(row)
        else:
            watchers += 1
    movers.sort(key=lambda r: (-r["same_theme"], -r["max_boards"]))

    if movers:
        lines = [f"{r['name']}({r['max_boards']}板"
                 f"{'·同题材' if r['same_theme'] else ''}"
                 f" 今日{_float(r['pct']):+.1f}%{'·再涨停' if r['today_zt'] else ''})"
                 for r in movers[:5]]
        state = "pass"
        ev = "老龙异动，引信已点燃：" + "；".join(lines)
        detail = ["资金有记忆和路径依赖 —— 上一轮龙头先动，是题材大爆发的前兆"]
    else:
        state = "weak"
        ev = (f"档案内 {len(archive)} 只历史龙头今日无一异动（涨幅≥{pct_min:.0f}%），"
              f"引信未点燃")
        detail = ["老龙不动不代表没有新行情，但缺了这环，先按观察处理"]
    return {"id": "S4", "name": "老龙是前兆", "state": state, "evidence": ev,
            "detail": detail, "dragons": movers[:8], "archive_size": len(archive)}


def _signal_next_day(a: dict, real_date: str, snaps: dict[str, dict],
                     zt: list[dict], theme_name: str) -> tuple[dict, dict]:
    """S5 次日定生死：昨日涨停池 → 今日开盘的三项真金白银检验。

    返回 (信号, day2数据)。day2.available=False 表示无昨日存档（首日/冷启动）。
    """
    mv_min = _float(a.get("midarmy_mv_min"), 100.0)
    prem_min = _float(a.get("dragon_premium_min"), 3.0)
    surv_min = _float(a.get("firstboard_survive_min"), 0.5)
    surv_pct = _float(a.get("survive_pct_min"), 3.0)
    fb_max = _float(a.get("midarmy_feedback_max"), -2.0)
    today_zt = {r.get("code"): r for r in zt}

    def pending(ev: str, detail: list[str]) -> tuple[dict, dict]:
        return ({"id": "S5", "name": "次日定生死", "state": "pending",
                 "evidence": ev, "detail": detail},
                {"available": False})

    dates = [d for d in ds.pool_history_dates() if not real_date or d < real_date]
    if not dates:
        return pending("无昨日涨停池存档 —— 主线不是当天确认的，明日开盘用真金白银检验",
                       ["明日看三件事：空间龙候选高溢价、昨日首板活口留存率、中军无负反馈"])
    ydate = dates[-1]
    yp = ds.load_pool_history(ydate)
    if not yp:
        return pending(f"{ydate} 涨停池存档为空，无法做次日检验",
                       ["明日看三件事：空间龙候选高溢价、昨日首板活口留存率、中军无负反馈"])

    # ① 空间龙候选高溢价：昨日最高连板（≥2板）前5，今日平均高开幅度
    cands = sorted([r for r in yp if _int(r.get("boards"), 1) >= 2],
                   key=lambda r: -_int(r.get("boards"), 1))[:5]
    premiums: list[float] = []
    cand_rows = []
    for r in cands:
        snap = snaps.get(r.get("code", ""))
        if not snap or not snap.get("prev_close"):
            continue
        prem = (snap["open"] - snap["prev_close"]) / snap["prev_close"] * 100
        premiums.append(prem)
        cand_rows.append({"code": r.get("code", ""), "name": r.get("name", ""),
                          "boards": _int(r.get("boards"), 1),
                          "premium": round(prem, 2), "promoted": r.get("code") in today_zt,
                          "now_pct": _float(snap.get("pct"))})
    avg_prem = sum(premiums) / len(premiums) if premiums else None
    prem_ok = avg_prem is not None and avg_prem >= prem_min

    # ② 昨日首板活口留存率：今日再涨停或涨幅达标
    fb = [r for r in yp if _int(r.get("boards"), 1) <= 1]
    alive = 0
    for r in fb:
        snap = snaps.get(r.get("code", ""))
        if r.get("code") in today_zt or (snap and _float(snap.get("pct")) >= surv_pct):
            alive += 1
    surv_rate = (alive / len(fb)) if fb else None
    surv_ok = surv_rate is not None and surv_rate >= surv_min

    # ③ 中军无负反馈：昨日百亿大票今日最弱表现不破线
    yarmy = [r for r in yp if _float(r.get("float_mv")) / 1e8 >= mv_min]
    worst: tuple[str, float] | None = None
    for r in yarmy:
        snap = snaps.get(r.get("code", ""))
        if not snap:
            continue
        p = _float(snap.get("pct"))
        if worst is None or p < worst[1]:
            worst = (r.get("name", ""), p)
    army_ok = worst is None or worst[1] >= fb_max

    # 昨日最强题材是否就是今日主线题材
    ythemes = _cluster_themes(yp, 103000)
    ytop = ythemes[0]["sector"] if ythemes else ""
    theme_match = bool(theme_name) and ytop == theme_name

    day2 = {"available": True, "yesterday_date": ydate, "yesterday_theme": ytop,
            "theme_match": theme_match,
            "dragon_premium": None if avg_prem is None else round(avg_prem, 2),
            "survive_rate": None if surv_rate is None else round(surv_rate, 3),
            "firstboard_total": len(fb), "firstboard_alive": alive,
            "midarmy_worst": None if worst is None else {"name": worst[0], "pct": worst[1]},
            "candidates": cand_rows}

    if avg_prem is None and surv_rate is None and worst is None:
        return pending("昨日涨停池个股今日快照缺失（停牌/代码变更），待验证",
                       ["主线不是当天确认的：明日开盘再看空间龙溢价、首板留存、中军反馈"])

    parts = []
    if avg_prem is not None:
        parts.append(f"空间龙平均高开 {avg_prem:+.1f}%（线 {prem_min:.0f}%）"
                     f"{'✓' if prem_ok else '✗'}")
    if surv_rate is not None:
        parts.append(f"首板留存 {surv_rate * 100:.0f}%（线 {surv_min * 100:.0f}%）"
                     f"{'✓' if surv_ok else '✗'}")
    if worst is not None:
        parts.append(f"昨日中军最弱 {worst[0]} {worst[1]:+.1f}%（线 {fb_max:+.0f}%）"
                     f"{'✓' if army_ok else '✗'}")

    if not theme_match:
        state = "pending"
        ev = (f"今日主线「{theme_name}」与昨日热点「{ytop}」不同 —— 新题材首日，明日开盘验证")
        detail = ["主线不是当天确认的：明日看该题材的空间龙高开溢价、首板留存、中军反馈",
                  "（参考）昨日热点次日承接：" + ("；".join(parts) if parts else "数据不足")]
    else:
        ok_cnt = sum([prem_ok, surv_ok, army_ok])
        state = "pass" if ok_cnt == 3 else "fail"
        ev = "；".join(parts) if parts else "数据不足"
        detail = (["三者齐备，主线地位成立 —— 空间龙高溢价、首板留得住、中军不砸盘"]
                  if state == "pass" else
                  ["三项检验未全过 —— 高开不能兑现就是坑，主线地位不成立，按一日游处理"])

    return ({"id": "S5", "name": "次日定生死", "state": state, "evidence": ev,
             "detail": detail}, day2)


# ---------------------------------------------------------------- 综合判定

def _verdict(s1: dict, s2: dict, s3: dict, s4: dict, s5: dict) -> tuple[str, str]:
    """按用户的规则收敛为四种状态。缺一环就是一日游。"""
    s1v, s2v, s3v, s4v, s5v = s1["state"], s2["state"], s3["state"], s4["state"], s5["state"]

    if s1v == "fail":
        return "one_day", "消息级别不够大（C级/个股公告）撑不起主线 —— 消息不够大，后面都不用看"
    if s2v == "fail":
        return "one_day", "无合力：题材内没有多家齐涨停 = 资金没有集体行动，孤军奋战就是一日游"
    if s3v == "fail":
        return "one_day", "无中军：百亿大票没参与 = 游资脉冲行情，容量不够，当天冲高就结束"
    if s5v == "fail":
        return "one_day", ("次日检验未通过：空间龙溢价 / 首板留存 / 中军反馈至少一项不合格 —— "
                           "主线地位不成立，按一日游处理")

    if s5v == "pass" and s2v == "pass" and s4v != "weak":
        if s1v == "unknown":
            return "watch", "五环基本齐但消息级别未录入 —— 先按观察对待，在「调策略 → M9」录入后再定性"
        return "confirmed", "五个信号依次共振，主线地位成立：按主线节奏做龙头与中军"

    if s2v in ("pass", "weak") and s5v == "pending" and s4v != "weak":
        if s1v == "unknown":
            return "day1_pending", ("首日启动成立（合力+中军），主线明日开盘验证；"
                                    "另请在「调策略 → M9」录入今日消息级别")
        return "day1_pending", ("首日启动成立（合力+中军），但主线是次日确认的 —— 明日开盘看："
                                "空间龙高溢价、首板活口留存、中军无负反馈")

    return "watch", "信号不全，缺一环就是一日游 —— 按观察处理，等信号补齐"


def evaluate(cfg: dict, zt: list[dict], snaps: dict[str, dict],
             real_date: str = "") -> dict:
    """M9 主线五信号评估入口。

    zt: 当日涨停池；snaps: 全市场快照（供老龙/次日验证取涨幅与开盘价）。
    """
    a = cfg.get("M9_mainline", {})
    real_date = real_date or ds.last_trade_date()

    # 无论开关与否都归档当日涨停池 —— 次日验证/老龙档案的数据底座
    ds.save_pool_history(real_date, zt)

    if not a.get("enabled", True):
        return {"enabled": False, "status": "disabled", "label": STATUS_CN["disabled"],
                "verdict_note": "M9 已关闭", "theme": "", "signals": [], "themes": [],
                "dragons": [], "day2": {"available": False}, "checks": []}

    deadline = _parse_hhmmss(a.get("early_deadline", "10:30"))
    themes = _cluster_themes(zt, deadline)
    top = themes[0] if themes else None
    theme_name = top["sector"] if top else ""

    # S1 消息定级别：手动录入优先；未录入且开启 auto_message 时自动抓快讯定级
    suggestion = None
    if a.get("auto_message", True):
        try:
            suggestion = news_mod.grade(news_mod.fetch_news(), theme_name)
        except Exception:
            suggestion = None
    s1 = _signal_message(a, theme_name, suggestion)
    s2 = _signal_consensus(a, top)
    s3 = _signal_midarmy(a, top, zt)
    s4 = _signal_old_dragon(a, real_date, snaps, zt, theme_name)
    s5, day2 = _signal_next_day(a, real_date, snaps, zt, theme_name)
    signals = [s1, s2, s3, s4, s5]

    status, note = _verdict(s1, s2, s3, s4, s5)

    checks = []
    if status == "one_day":
        checks.append({"level": "yellow", "item": "题材主线一日游特征",
                       "action": f"{note}；题材内不接力，只看不动"})
    elif status == "day1_pending":
        checks.append({"level": "yellow", "item": "题材主线待次日验证",
                       "action": "首日启动成立但主线未确认 —— 明日开盘检验空间龙溢价/"
                                 "首板留存/中军反馈后再定性"})
    elif status == "confirmed":
        checks.append({"level": "green", "item": "题材主线已确认",
                       "action": f"主线题材「{theme_name}」五信号共振，可按主线节奏参与"})

    return {
        "enabled": True, "status": status, "label": STATUS_CN[status],
        "verdict_note": note, "theme": theme_name,
        "message_auto": suggestion or {"found": False},
        "signals": signals, "themes": themes[:6],
        "dragons": s4.get("dragons", []),
        "day2": day2, "checks": checks,
        "note": "题材以涨停池行业板块为代理；老龙档案与昨日涨停池为本地逐日积累，"
                "冷启动时对应信号显示待验证",
    }
