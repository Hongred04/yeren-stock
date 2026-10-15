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

from datetime import datetime

from . import cls as cls_mod
from . import datasource as ds
from . import news as news_mod

# 消息级别定义：级别决定行情空间的上限
MESSAGE_LEVELS = {
    "S": {"name": "S级 · 国家级顶层战略", "ceiling": "空间上限：主线大级别", "gate_ok": True},
    "A": {"name": "A级 · 部委政策 + 产业事件共振", "ceiling": "空间上限：中级行情", "gate_ok": True},
    "B": {"name": "B级 · 行业级事件/景气数据", "ceiling": "空间上限：局部主线", "gate_ok": True},
    "C": {"name": "C级 · 个股公告/传闻", "ceiling": "撑不起主线：脉冲后回落", "gate_ok": False},
    "auto": {"name": "未录入", "ceiling": "未定级按观察处理：请在「调策略」→「高级参数」→ M9 录入消息级别", "gate_ok": False},
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


def _fmt_date(d: str) -> str:
    """'20261002' → '10-02'。"""
    return f"{d[4:6]}-{d[6:8]}" if len(d) == 8 else d


def _sector_hit(theme: str, sector: str) -> bool:
    """题材名匹配（跨口径宽容版）。

    为什么不能精确匹配（2026-10-10 实测）：历史归档有两个来源 ——
    开盘红回补的是**概念口径**（"固态电池""并购重组"），东财实时池是
    **行业口径**（"电池""化学制品"）。精确匹配时「电池」在历史里查无此名，
    streak 直接断掉（手机冷启动就表现为"主线没有记忆"）。
    放宽为双向包含：「电池」↔「固态电池」视为同一方向 ✓，
    不相干的（如「并购重组」）仍然不匹配。
    """
    if not theme or not sector:
        return False
    return theme == sector or theme in sector or sector in theme


def _theme_recent_history(real_date: str, lookback: int = 10) -> list[tuple[str, list[dict]]]:
    """近 N 个已归档交易日的**活跃题材榜** [(date, [题材项,...])]，旧→新。

    活跃 = 当日合力榜前 3 且涨停 ≥2 家。**不能只记每天的第一名** ——
    题材连续几天在第 2、第 3 名爬坡（涨停家数、最高板一天天抬高）正是典型的
    「已在启动中」；只看冠军的话，等它登顶那天记忆里查无此题材，
    会把它当成「新题材首日」，等于没有记忆（电池 09-30→10-08→10-09 实测踩到）。
    """
    out: list[tuple[str, list[dict]]] = []
    for d in ds.pool_history_dates():
        if real_date and d >= real_date:
            continue
        pool = ds.load_pool_history(d)
        if not pool:
            continue
        th = _cluster_themes(pool, 103000)
        act = [t for t in th[:3] if _int(t.get("zt"), 0) >= 2]
        if act:
            out.append((d, act))
    return out[-max(2, lookback):]


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

    手动录入（message_level != auto）永远优先；未录入时采用「系统建议级别」：
    今日快讯自动定级 + **消息记忆**（近 S 20天 / A 10天 / B 5天内的重要消息），
    两者取更高者 —— 大级别政策不会因为快讯池翻不到旧闻就"消失"。
    证据（命中消息/记忆原文）随信号输出供人工复核。
    """
    lv = a.get("message_level") or "auto"
    info = MESSAGE_LEVELS.get(lv, MESSAGE_LEVELS["auto"])
    note = (a.get("message_note") or "").strip()
    # detail 首行必须是「本次判到了什么」，不能是「规则怎么写的」——
    # 用户要看结论，规则说明放「调策略」里就够了（2026-10-10 通读文案时改）
    rules: list[str] = []

    ev_head = (suggestion or {}).get("evidence", [])[:2]
    ev_str = "；".join(f"{str(e.get('time',''))[5:16]} {str(e.get('text',''))[:46]}"
                       for e in ev_head)

    def _lv_name(code: str) -> str:
        return MESSAGE_LEVELS.get(code, {}).get("name", code or "—")

    if lv == "auto":
        if suggestion and suggestion.get("found"):
            s_info = MESSAGE_LEVELS.get(suggestion["level"], MESSAGE_LEVELS["auto"])
            state = "pass" if s_info["gate_ok"] else "fail"
            mem = suggestion.get("memory")
            if mem:
                mem_day = _fmt_date(mem.get("date", ""))
                today_lv = suggestion.get("today_level") or ""
                ev = (f"消息记忆：{mem_day} 出现 {_lv_name(mem['level'])}"
                      f"（{mem.get('days', 0)} 天前，仍在有效期内）")
                if today_lv and today_lv != mem["level"]:
                    ev += f"；今日快讯最高仅 {today_lv} 级"
                ev += (f"：{ev_str}" if ev_str else "")
                detail = [f"命中消息记忆：{mem_day} 的 {_lv_name(mem['level'])}"
                          f"（{mem.get('days', 0)} 天前，仍在有效期内）"] + [
                    "大级别政策不会因为日历翻页失效（S 20天 / A 10天 / B 5天）；"
                    "快讯池只能翻两三天，靠消息记忆兜住"]
            else:
                since = suggestion.get("since") or ""
                ev = (f"自动定级 {s_info['name']}（依据{suggestion.get('scanned', 0)}条快讯"
                      f"与主题材的关联匹配，覆盖至 {since}）"
                      + (f"：{ev_str}" if ev_str else ""))
                detail = [f"自动定级：{s_info['name']}（关联快讯 {suggestion.get('scanned', 0)} 条）"] + [
                    "定级规则：国家战略S → 部委政策A → 行业事件B → 个股公告C；"
                    "只统计与主题材相关的快讯，有出入可手动录入覆盖"]
        else:
            state = "unknown"
            ev = "消息级别：未定级。" + (
                f"自动快讯未检测到与主题材「{theme_name}」直接相关的政策/事件消息，"
                f"消息记忆（近20日）里也没有"
                if a.get("auto_message", True)
                else "自动定级已关闭，请在「调策略」→「高级参数」→ M9 录入消息级别")
            detail = ["未发现达到门槛的消息 —— 按「未定级」处理",
                      "盘中快速轮动启动的题材，快讯可能滞后；人工确认后可手动录入"]
    else:
        state = "pass" if info["gate_ok"] else "fail"
        ev = f"消息级别：{info['name']}。{info['ceiling']}"
        if note:
            ev += f"｜今日消息：{note}"
        mem = (suggestion or {}).get("memory")
        if mem:
            ev += (f"｜消息记忆：{_fmt_date(mem.get('date', ''))} "
                   f"{_lv_name(mem['level'])}（有效期内）")
        elif suggestion and suggestion.get("found"):
            s_info = MESSAGE_LEVELS.get(suggestion["level"], MESSAGE_LEVELS["auto"])
            ev += f"｜快讯扫描也见{s_info['name']}" + (f"：{ev_str}" if ev_str else "")
        detail = [f"手动录入：{info['name']}" + (f"｜{note}" if note else ""),
                  info.get("ceiling", "")]

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
        bars, src_label = None, ""
        # 优先：题材 → 中证/国证代理指数（push2his 被拦截的网络环境下的可达源）
        idx = ds.resolve_sector_index(top["sector"])
        if idx:
            idx_code, idx_name = idx
            bars = ds._kline_from_tx(idx_code, 70)
            src_label = f"（代理指数 {idx_name}）"
        # 回退：东财行业板块指数（网络可用时的原口径）
        if not bars:
            bk = _resolve_bk(top["sector"])
            if bk:
                bars = ds.fetch_board_kline(bk, 70)
        if bars:
            # 腾讯日K只有 volume 字段，归一化为 amount —— 带量判定用的是
            # 量能比值，成交量口径同样成立
            bars = [{**b, "amount": b.get("amount", b.get("volume", 0))} for b in bars]
            idx_state, idx_note = _index_breakout(
                bars, _float(a.get("board_volume_surge"), 1.3))
            idx_note += src_label
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


def _signal_midarmy(a: dict, top: dict | None, zt: list[dict],
                    snaps: dict[str, dict] | None = None) -> dict:
    """S3 中军定容量：题材内百亿市值大票是否参与。

    ★ 涨幅一律取**今日快照**（v1.32.3 修）：`fetch_board_constituents` 的成员表是
      三天一建的缓存（`_BOARD_MEMBERS_TTL`），里面的 `pct` 是**建表那一刻**的涨幅。
      原来直接拿它比 `pct_min`，等于用三天前的异动判断"今天中军有没有在动" ——
      三天前涨过的票被算成今天在动（假 pass），而今天刚拉起来的票反而查不到。
      快照里拿不到的（停牌/退市/非当日有效名单）才退回缓存值，并如实标注。
    """
    mv_min = _float(a.get("midarmy_mv_min"), 100.0)
    pct_min = _float(a.get("midarmy_pct_min"), 3.0)
    min_cnt = _int(a.get("midarmy_min_count"), 1)
    if not top:
        return {"id": "S3", "name": "中军定容量", "state": "fail",
                "evidence": "无主题材，无法核查中军", "detail": [], "army": []}

    army: list[dict] = []
    src = "板块成分股"
    stale = 0
    try:
        bk = _resolve_bk(top["sector"])
        if bk:
            cons = ds.fetch_board_constituents(bk)
            cands = []
            for c in cons:
                snap = (snaps or {}).get(c.get("code"))
                sp = _float(snap.get("pct")) if (snap and snap.get("pct") is not None) else None
                pct = sp if sp is not None else _float(c.get("pct"))
                if sp is None:
                    stale += 1
                cands.append({"code": c.get("code", ""), "name": c.get("name", ""),
                              "float_mv": _float(c.get("float_mv")), "pct": pct,
                              "today": sp is not None})
            army = [c for c in cands
                    if c["float_mv"] >= mv_min and c["pct"] >= pct_min]
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
        if stale:
            # 快照里查不到的那部分用的是成分表缓存里的旧涨幅，如实说明
            detail.append(f"其中 {stale} 只在当日快照里查不到（停牌/非有效名单），"
                          "涨幅回退用板块缓存值，仅供参考")
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


def _trio_checks(base_pool: list[dict], snaps: dict[str, dict],
                 today_zt: dict[str, dict], mv_min: float, prem_min: float,
                 surv_min: float, surv_pct: float, fb_max: float) -> dict:
    """三项次日检验（空间龙溢价 / 首板留存 / 中军反馈）。

    base_pool 为基准日涨停池：昨日全池，或主线回归时该题材上次领衔日的池。
    """
    # ① 空间龙候选高溢价：基准日最高连板（≥2板）前5，今日平均高开幅度
    cands = sorted([r for r in base_pool if _int(r.get("boards"), 1) >= 2],
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

    # ② 首板活口留存率：今日再涨停或涨幅达标
    fb = [r for r in base_pool if _int(r.get("boards"), 1) <= 1]
    alive = 0
    for r in fb:
        snap = snaps.get(r.get("code", ""))
        if r.get("code") in today_zt or (snap and _float(snap.get("pct")) >= surv_pct):
            alive += 1
    surv_rate = (alive / len(fb)) if fb else None
    surv_ok = surv_rate is not None and surv_rate >= surv_min

    # ③ 中军无负反馈：基准日百亿大票今日最弱表现不破线
    army = [r for r in base_pool if _float(r.get("float_mv")) / 1e8 >= mv_min]
    worst: tuple[str, float] | None = None
    for r in army:
        snap = snaps.get(r.get("code", ""))
        if not snap:
            continue
        p = _float(snap.get("pct"))
        if worst is None or p < worst[1]:
            worst = (r.get("name", ""), p)
    army_ok = worst is None or worst[1] >= fb_max

    parts = []
    if avg_prem is not None:
        parts.append(f"空间龙平均高开 {avg_prem:+.1f}%（线 {prem_min:.0f}%）"
                     f"{'✓' if prem_ok else '✗'}")
    if surv_rate is not None:
        parts.append(f"首板留存 {surv_rate * 100:.0f}%（线 {surv_min * 100:.0f}%）"
                     f"{'✓' if surv_ok else '✗'}")
    if worst is not None:
        parts.append(f"中军最弱 {worst[0]} {worst[1]:+.1f}%（线 {fb_max:+.0f}%）"
                     f"{'✓' if army_ok else '✗'}")

    return {"avg_prem": avg_prem, "prem_ok": prem_ok,
            "surv_rate": surv_rate, "surv_ok": surv_ok,
            "worst": worst, "army_ok": army_ok,
            "fb_total": len(fb), "alive": alive,
            "cand_rows": cand_rows, "parts": parts,
            "any_data": avg_prem is not None or surv_rate is not None
                        or worst is not None}


def _signal_next_day(a: dict, real_date: str, snaps: dict[str, dict],
                     zt: list[dict], theme_name: str,
                     themes: list[dict] | None = None) -> tuple[dict, dict]:
    """S5 次日定生死：基准日涨停池 → 今日的三项真金白银检验。

    基准日的选取（题材不是凭空来的）：
      昨日活跃榜里有该题材      → 按延续口径检验；它就是昨日第一名时用昨日全池
                                 （标准次日口径），否则用该题材自己的昨日涨停池 ——
                                 题材连续多日爬坡（第3→第2→第1）也算延续，不算首日；
      今日主线近 N 日活跃过      → 基准 = 该题材上次活跃日的池 —— 主线回归（二波）；
      其他                      → 新题材首日，昨日池数据仅供参考。

    "活跃榜"按前 3 且涨停 ≥2 家记 —— 只记第一名会把爬坡中的题材忘掉
    （电池 09-30 第3 → 10-08 第2 → 10-09 第1 的实测教训）。
    """
    mv_min = _float(a.get("midarmy_mv_min"), 100.0)
    prem_min = _float(a.get("dragon_premium_min"), 3.0)
    surv_min = _float(a.get("firstboard_survive_min"), 0.5)
    surv_pct = _float(a.get("survive_pct_min"), 3.0)
    fb_max = _float(a.get("midarmy_feedback_max"), -2.0)
    today_zt = {r.get("code"): r for r in zt}
    themes = themes or []

    def pending(ev: str, detail: list[str]) -> tuple[dict, dict]:
        return ({"id": "S5", "name": "次日定生死", "state": "pending",
                 "evidence": ev, "detail": detail},
                {"available": False})

    # 今日该题材在合力榜里的位置（轨迹展示要用）
    today_entry = next((x for x in themes if x.get("sector") == theme_name), None)
    today_rank = _int((today_entry or {}).get("rank"), 0)

    dates = [d for d in ds.pool_history_dates() if not real_date or d < real_date]
    if not dates:
        return pending("无昨日涨停池存档 —— 主线不是当天确认的，明日开盘用真金白银检验",
                       ["明日看三件事：空间龙候选高溢价、昨日首板活口留存率、中军无负反馈"])
    ydate = dates[-1]
    yp = ds.load_pool_history(ydate)
    if not yp:
        return pending(f"{ydate} 涨停池存档为空，无法做次日检验",
                       ["明日看三件事：空间龙候选高溢价、昨日首板活口留存率、中军无负反馈"])

    ythemes = _cluster_themes(yp, 103000)
    ytop = ythemes[0]["sector"] if ythemes else ""
    y_active = [t["sector"] for t in ythemes[:3] if _int(t.get("zt"), 0) >= 2]
    theme_match = bool(theme_name) and any(_sector_hit(theme_name, s) for s in y_active)

    # 近 N 日活跃轨迹 + 连续活跃天数（含今日）
    lookback = max(2, _int(a.get("theme_lookback_days"), 10))
    hist = _theme_recent_history(real_date, lookback)
    track: list[dict] = []
    for d, act in hist:
        for t in act:
            if _sector_hit(theme_name, t["sector"]):
                track.append({"date": d, "rank": _int(t.get("rank"), 0),
                              "zt": _int(t.get("zt"), 0),
                              "max_boards": _int(t.get("max_boards"), 0)})
                break
    seen = [x["date"] for x in track]
    prev_streak = 0
    for _d, act in reversed(hist):
        if any(_sector_hit(theme_name, t["sector"]) for t in act):
            prev_streak += 1
        else:
            break
    streak = (prev_streak + 1) if theme_name else 0
    streak_since = (track[-prev_streak]["date"] if prev_streak
                    else (track[-1]["date"] if track else ""))
    track_full = list(track)
    if today_entry is not None:
        track_full.append({"date": real_date, "rank": today_rank,
                           "zt": _int(today_entry.get("zt"), 0),
                           "max_boards": _int(today_entry.get("max_boards"), 0)})

    return_day = bool(theme_name) and not theme_match and bool(seen)

    base_date, base_pool = ydate, yp
    base_kind = "昨日全池"
    if theme_match and theme_name != ytop:
        # 昨日活跃但不是第一名：用该题材自己的昨日涨停池做承接检验（延续口径）
        bp = [r for r in yp if (r.get("sector") or "") == theme_name]
        if bp:
            base_pool, base_kind = bp, "该题材昨日涨停池"
    if return_day:
        base_date = seen[-1]
        bp = [r for r in ds.load_pool_history(base_date)
              if (r.get("sector") or "") == theme_name]
        if bp:
            base_pool, base_kind = bp, "该题材上次活跃日涨停池"
        else:   # 该日归档损坏/无该题材记录，退回昨日口径
            base_date, base_pool, base_kind = ydate, yp, "昨日全池"
            return_day = False

    t = _trio_checks(base_pool, snaps, today_zt, mv_min, prem_min,
                     surv_min, surv_pct, fb_max)
    track_extra = {"theme_match": theme_match, "return_day": return_day,
                   "streak": streak, "streak_since": streak_since,
                   "theme_track": track_full, "base_kind": base_kind}
    if not t["any_data"]:
        return ({"id": "S5", "name": "次日定生死", "state": "pending",
                 "evidence": "基准日涨停池个股今日快照缺失（停牌/代码变更），待验证",
                 "detail": ["主线不是当天确认的：明日开盘再看空间龙溢价、首板留存、中军反馈"]},
                {"available": False, **track_extra})

    day2 = {"available": True, "yesterday_date": ydate, "yesterday_theme": ytop,
            "base_date": base_date, "times_in_window": len(seen),
            "theme_history": [{"date": d, "sectors": [x["sector"] for x in act]}
                              for d, act in hist[-6:]],
            "dragon_premium": None if t["avg_prem"] is None else round(t["avg_prem"], 2),
            "survive_rate": None if t["surv_rate"] is None else round(t["surv_rate"], 3),
            "firstboard_total": t["fb_total"], "firstboard_alive": t["alive"],
            "midarmy_worst": None if t["worst"] is None else {"name": t["worst"][0],
                                                              "pct": t["worst"][1]},
            "candidates": t["cand_rows"], **track_extra}

    parts = t["parts"]
    streak_txt = (f"该题材已连续 {streak} 日活跃（{_fmt_date(streak_since)} 起）"
                  if streak >= 2 else "")

    if theme_match:
        ok_cnt = sum([t["prem_ok"], t["surv_ok"], t["army_ok"]])
        state = "pass" if ok_cnt == 3 else "fail"
        lead = (f"「{theme_name}」" + (streak_txt + "，" if streak_txt else "")
                + f"{base_kind}：")
        ev = lead + ("；".join(parts) if parts else "数据不足")
        if state == "pass":
            detail = ["三者齐备，主线地位成立 —— 空间龙高溢价、首板留得住、中军不砸盘"]
        else:
            detail = ["三项检验未全过 —— 高开不能兑现就是坑，主线地位不成立，按一日游处理"]
        if streak_txt:
            detail.insert(0, streak_txt + " —— 不是首日，是延续行情，按延续题材料理")
    elif return_day:
        present = [x for x in (t["avg_prem"], t["surv_rate"], t["worst"])
                   if x is not None]
        ok_cnt = sum([t["prem_ok"], t["surv_ok"], t["army_ok"]])
        base_txt = f"基准 {_fmt_date(base_date)} {base_kind}"
        if len(present) >= 2 and ok_cnt == len(present):
            state = "pass"
            ev = (f"主线回归承接成立（{base_txt}，近{lookback}日第{len(seen)}次登场）："
                  + "；".join(parts))
            detail = ["回归日的检验基准是「该题材上次活跃日」的涨停池，不是昨日热点",
                      "三项齐备 = 二波承接成立，按主线节奏对待；老龙/中军高位注意随时切换口径"]
        elif len(present) >= 2:
            state = "fail"
            ev = (f"主线回归承接不成立（{base_txt}）：" + "；".join(parts)
                  + " —— 二波接不住就是坑")
            detail = ["回归检验未全过：上次买这个题材的人今天不赚钱，接力意愿会二次衰减",
                      "二波比首波更挑承接，接不住别硬做"]
        else:
            state = "weak"
            ev = (f"主线回归（上次活跃 {_fmt_date(base_date)}，近{lookback}日第{len(seen)}次），"
                  "承接数据不足：" + ("；".join(parts) if parts else "快照缺失")
                  + "，先按观察处理")
            detail = ["回归日承接数据不足 —— 盘中看老龙能否新高，有承接再定性"]
    else:
        state = "pending"
        ev = (f"今日主线「{theme_name}」与昨日活跃榜（{('、'.join(y_active) or ytop)}）"
              f"不同属，且近{lookback}日未活跃过 —— 新题材首日，明日开盘验证")
        detail = ["主线不是当天确认的：明日看该题材的空间龙高开溢价、首板留存、中军反馈",
                  "（参考）昨日热点次日承接：" + ("；".join(parts) if parts else "数据不足")]

    return ({"id": "S5", "name": "次日定生死", "state": state, "evidence": ev,
             "detail": detail}, day2)


# ---------------------------------------------------------------- 综合判定

def _one_day_note(base: str, day2: dict | None = None, short: str = "") -> str:
    """「一日游」只适用于**首日启动的新题材**。

    已经连续活跃 ≥2 日的题材谈不上一日游（页面上一边写着"连续3日活跃"
    一边判"一日游"，自相矛盾）。中军缺失 / 承接不成立在这种题材上的正确
    语义是「游资行情、容量有限」：可以做，但当短线看、不重仓、不格局，
    别用长线思维拿 —— 这正是野人说的存量资金博弈下的题材玩法。
    """
    streak = _int((day2 or {}).get("streak"), 0)
    if streak >= 2:
        # 连续活跃题材：缺环的含义是「容量有限、走不远」，不是「今天就死」——
        # base 里"当天冲高就结束"那种一步游措辞直接套会自相矛盾
        #（都连涨 4 天了，哪来"当天就结束"），所以这里改用 short 的简因。
        why = short or base
        return (f"该题材已连续 {streak} 日活跃、不是一日游；但主线地位不成立 —— {why}。"
                "按「游资行情、容量有限」对待：做短线、不重仓、不格局，别用长线思维拿")
    return base


def _verdict(s1: dict, s2: dict, s3: dict, s4: dict, s5: dict,
             day2: dict | None = None, theme_name: str = "") -> tuple[str, str]:
    """按用户的规则收敛为四种状态。缺一环就是一日游。

    「一日游」的判定对首日新题材成立；对已连续活跃多日的题材，
    缺环的含义降级为「游资行情、容量有限」（见 _one_day_note）。
    """
    s1v, s2v, s3v, s4v, s5v = s1["state"], s2["state"], s3["state"], s4["state"], s5["state"]

    if s1v == "fail":
        return "one_day", _one_day_note(
            "消息级别不够大（C级/个股公告）撑不起主线 —— 消息不够大，后面都不用看",
            day2, short="消息级别不够大，撑不起主线行情")
    if s2v == "fail":
        return "one_day", _one_day_note(
            "无合力：题材内没有多家齐涨停 = 资金没有集体行动，孤军奋战就是一日游",
            day2, short="题材内没有多家齐涨停，资金没形成集体行动")
    if s3v == "fail":
        return "one_day", _one_day_note(
            "无中军：百亿大票没参与 = 游资脉冲行情，容量不够，当天冲高就结束",
            day2, short="没有中军（百亿大票没参与），容量不够、行情走不远")
    if s5v == "fail":
        if day2 and day2.get("return_day"):
            return "one_day", _one_day_note(
                "主线回归承接不成立：空间龙溢价 / 首板留存 / 中军反馈未过线 —— 二波接不住",
                day2, short="二波承接不成立（空间龙溢价/首板留存/中军反馈未过线）")
        return "one_day", _one_day_note(
            "次日检验未通过：空间龙溢价 / 首板留存 / 中军反馈至少一项不合格 —— 主线地位不成立",
            day2, short="承接检验未过（空间龙溢价/首板留存/中军反馈）")

    if s5v == "pass" and s2v == "pass" and s4v != "weak":
        if s1v == "unknown":
            return "watch", "五环基本齐但消息级别未录入 —— 先按观察对待，在「调策略」→「高级参数」→ M9 录入后再定性"
        note = "五个信号依次共振，主线地位成立：按主线节奏做龙头与中军"
        if day2 and _int(day2.get("streak"), 0) >= 2:
            note += (f"（该题材已连续 {day2['streak']} 日活跃"
                     f"{'、' + _fmt_date(day2.get('streak_since', '')) + ' 起' if day2.get('streak_since') else ''}"
                     f"，是延续行情不是首日）")
        elif day2 and day2.get("return_day"):
            note += "（主线回归，承接已过检验）"
        return "confirmed", note

    if s5v == "weak" and day2 and (day2.get("return_day") or day2.get("theme_match")):
        return "watch", (f"主线「{theme_name}」已在活跃榜上"
                         + (f"连续 {day2['streak']} 日" if _int(day2.get("streak"), 0) >= 2 else "")
                         + "，但承接数据不足 —— 盘中看老龙能否新高、中军是否稳住再定性")

    if s5v == "pass" and s2v == "weak":
        return "watch", (f"「{theme_name}」承接检验已过（空间龙/首板留存/中军），"
                         f"但题材合力或板块指数还差一环 —— 降级观察，明日看续强与否")

    if s2v in ("pass", "weak") and s5v == "pending" and s4v != "weak":
        _st = _int((day2 or {}).get("streak"), 0)
        if _st >= 2:
            return "day1_pending", (
                f"「{theme_name}」已连续 {_st} 日活跃（不是首日）—— 今日合力+中军"
                "重新成立，明日开盘看承接：空间龙高溢价、首板活口留存、中军无负反馈")
        if s1v == "unknown":
            return "day1_pending", ("首日启动成立（合力+中军），主线明日开盘验证；"
                                    "另请在「调策略」→「高级参数」→ M9 录入今日消息级别")
        if day2 and day2.get("return_day"):
            return "day1_pending", ("主线回归日启动成立（合力+中军）—— 今日盘中检验承接："
                                    "空间龙高溢价、首板留存、中军无负反馈，接不住就得熄火")
        return "day1_pending", ("首日启动成立（合力+中军），但主线是次日确认的 —— 明日开盘看："
                                "空间龙高溢价、首板活口留存、中军无负反馈")

    return "watch", "信号不全，主线不成立 —— 按观察处理，等信号补齐"


def evaluate(cfg: dict, zt: list[dict], snaps: dict[str, dict],
             real_date: str = "") -> dict:
    """M9 主线五信号评估入口。

    zt: 当日涨停池；snaps: 全市场快照（供老龙/次日验证取涨幅与开盘价）。
    """
    a = cfg.get("M9_mainline", {})
    real_date = real_date or ds.last_trade_date()

    # 无论开关与否都归档当日涨停池 —— 次日验证/老龙档案的数据底座。
    # ★ 必须带上接口自报的 qdate：池接口**忽略 date 入参**、始终返回"最近一个
    #   有数据的交易日"，盘前扫描拿到的其实是上一交易日的池；不带 qdate 校验
    #   就会把昨天记成今天（实测 2026-10-13：本地 [20261013] 与复盘接口的
    #   20261012 是同一批，且含当天已停牌的新华传媒）。日期不符即拒写，
    #   缺口由 kph.repair_pool_history 在收盘复盘生成后补上。
    ds.save_pool_history(real_date, zt, qdate=ds.pool_qdate())

    if not a.get("enabled", True):
        return {"enabled": False, "status": "disabled", "label": STATUS_CN["disabled"],
                "verdict_note": "M9 已关闭", "theme": "", "signals": [], "themes": [],
                "dragons": [], "day2": {"available": False}, "checks": []}

    deadline = _parse_hhmmss(a.get("early_deadline", "10:30"))
    themes = _cluster_themes(zt, deadline)
    top = themes[0] if themes else None
    theme_name = top["sector"] if top else ""

    # 野人周末总结：「AI应用批量上板的时候，固态电池开始掉队」= 存量资金博弈。
    # 检测「题材跷跷板」：上一交易日活跃第一的题材，今日涨停家数骤减 ≥40%，
    # 且它已不是当前主线 —— 资金在切换，题材当短线看（不格局）。
    rotation_note = ""
    try:
        hist_dates = [d for d in ds.pool_history_dates() if d < real_date]
        if hist_dates and theme_name:
            prev_pool = ds.load_pool_history(hist_dates[-1])
            if prev_pool and len(prev_pool) >= 10:
                prev_themes = _cluster_themes(prev_pool, deadline)
                prev_top = prev_themes[0]["sector"] if prev_themes else ""
                prev_n = prev_themes[0]["zt"] if prev_themes else 0
                if prev_top and prev_top != theme_name and prev_n >= 3:
                    today_n = next((t["zt"] for t in themes
                                    if t["sector"] == prev_top), 0)
                    if today_n <= prev_n * 0.6:
                        rotation_note = (
                            f"存量资金博弈：上一交易日活跃第一「{prev_top}」今日掉队"
                            f"（{prev_n}→{today_n} 家涨停），资金切到「{theme_name}」"
                            "—— 题材当短线看，不格局")
    except Exception:
        rotation_note = ""

    # S1 消息定级别：手动录入优先；未录入且开启 auto_message 时自动抓快讯定级
    # 窗口按交易日展开：上上个交易日 00:00 起 —— 长假/周末部委发文，
    # 开市首日的题材定级必须能吃到假期消息（自然小时口径会漏）
    suggestion = None
    if a.get("auto_message", True):
        try:
            window_start = None
            hist_dates = [d for d in ds.pool_history_dates()
                          if not real_date or d < real_date]
            base_d = hist_dates[-2] if len(hist_dates) >= 2 else (
                hist_dates[-1] if hist_dates else "")
            if base_d:
                window_start = datetime.strptime(base_d, "%Y%m%d")
            suggestion = news_mod.grade(news_mod.fetch_news(), theme_name,
                                        window_start=window_start)
        except Exception:
            suggestion = None
    # 消息记忆：快讯池只翻两三天，S/A/B 级结果在有效期内继续参与定级
    try:
        mem = news_mod.lookup(theme_name, real_date) if theme_name else None
        suggestion = news_mod.merge_suggestion(suggestion, mem)
    except Exception:
        pass
    # 归档今天的定级（手动优先；只记 S/A/B —— C 级个股公告是噪声，记了反而干扰）
    try:
        lv_today = a.get("message_level") or "auto"
        ev_all = (suggestion or {}).get("evidence") or []
        lv_auto = ""
        if suggestion and suggestion.get("found"):
            lv_auto = suggestion.get("today_level") or suggestion.get("level") or ""
        if lv_today in news_mod.VALID_DAYS:
            news_mod.remember(real_date, theme_name, lv_today, ev_all,
                              note=a.get("message_note", "") or "")
        elif lv_auto in news_mod.VALID_DAYS:
            ev_auto = (suggestion.get("today_evidence")
                       or suggestion.get("evidence") or [])
            news_mod.remember(real_date, theme_name, lv_auto, ev_auto)
    except Exception:
        pass
    s1 = _signal_message(a, theme_name, suggestion)
    s2 = _signal_consensus(a, top)
    s3 = _signal_midarmy(a, top, zt, snaps)
    s4 = _signal_old_dragon(a, real_date, snaps, zt, theme_name)
    s5, day2 = _signal_next_day(a, real_date, snaps, zt, theme_name, themes)
    signals = [s1, s2, s3, s4, s5]

    status, note = _verdict(s1, s2, s3, s4, s5, day2, theme_name)
    # 连续活跃多日的题材不该在界面上写「一日游」—— 换成容量口径的标签
    streak_now = _int((day2 or {}).get("streak"), 0)
    label = STATUS_CN[status]
    if status == "one_day" and streak_now >= 2:
        label = f"游资行情 · 容量有限 · 已连续{streak_now}日活跃"

    checks = []
    if status == "one_day":
        if streak_now >= 2:
            checks.append({"level": "yellow",
                           "item": f"题材主线容量不足（已连续 {streak_now} 日活跃，非一日游）",
                           "action": f"{note}；盘面按短线节奏处理，去弱留强"})
        else:
            checks.append({"level": "yellow", "item": "题材主线一日游特征",
                           "action": f"{note}；题材内不接力，只看不动"})
    elif status == "day1_pending":
        if day2.get("return_day"):
            checks.append({"level": "yellow", "item": "题材主线回归日",
                           "action": "该题材近日领衔过、今天二波登场 —— 今日盘中检验承接："
                                     "空间龙溢价、首板留存、中军反馈，接不住就是一日游"})
        else:
            checks.append({"level": "yellow", "item": "题材主线待次日验证",
                           "action": "首日启动成立但主线未确认 —— 明日开盘检验空间龙溢价/"
                                     "首板留存/中军反馈后再定性"})
    elif status == "confirmed":
        checks.append({"level": "green", "item": "题材主线已确认",
                       "action": f"主线题材「{theme_name}」五信号共振，可按主线节奏参与"})

    return {
        "enabled": True, "status": status, "label": label,
        "verdict_note": note, "theme": theme_name,
        "message_auto": suggestion or {"found": False},
        "signals": signals, "themes": themes[:6],
        "rotation_note": rotation_note,
        "dragons": s4.get("dragons", []),
        "day2": day2,
        "streak": _int((day2 or {}).get("streak"), 0),
        "streak_since": (day2 or {}).get("streak_since", ""),
        "checks": checks,
        "note": "题材以涨停池行业板块为代理；每日涨停池自动归档（活跃榜前3都可回查），"
                "题材连续多日爬坡不会被当成首日；冷启动前几天相关信号显示待验证",
    }


# ---------------------------------------------------------------- 主线/支线/强势板块

def _board_lookup(boards: list[dict], name: str) -> dict | None:
    """在板块资金流列表里按名称找板块（板块名与涨停池题材名常能对上）。"""
    if not name or not boards:
        return None
    for b in boards:
        if b.get("name") == name:
            return b
    for b in boards:
        bn = b.get("name") or ""
        if bn and (bn in name or name in bn):
            return b
    return None


def sector_view(ml: dict, flow: dict | None = None,
                zt: list[dict] | None = None) -> dict:
    """主线 + 支线 + 当前最强板块结论。

    · 主线   = 涨停合力最强的题材（M9 的 theme）；
    · 支线   = 合力次之的题材（≥2 家涨停），以及「有资金、但还没打出涨停
               合力」的纯资金板块（轮动预备队）；主线退潮时支线先死，
               但轮动方向看支线；
    · 强弱   = 涨停家数/早封 | 板块涨幅 | 主力净额 三样合看，给明确档位。
    """
    flow = flow or {}
    zt = zt or []
    boards_all = flow.get("boards_all") or []
    themes = (ml or {}).get("themes") or []
    theme_name = (ml or {}).get("theme") or ""
    ml_status = (ml or {}).get("status") or ""

    # 财联社板块热度榜（排名变化 = 资金正在涌进的方向）——匹配不上就不显示
    try:
        heats = cls_mod.sector_heat()
    except Exception:
        heats = []
    # 财联社风口板块（编辑标注的"今天为什么涨"——催化剂一句话）
    try:
        winds = cls_mod.market_wind()
    except Exception:
        winds = []

    def _heat_fact(name: str) -> str:
        if not heats or not name:
            return ""
        h = cls_mod.match_heat(name, heats)
        if not h:
            return ""
        chg = h.get("rank_change") or 0
        if h.get("is_new"):
            tail = "新上榜"
        elif chg > 0:
            tail = f"升{chg}位"
        elif chg < 0:
            tail = f"降{abs(chg)}位"
        else:
            tail = "持平"
        return f"热度榜第{h['rank']}·{tail}"

    def _catalyst_fact(name: str) -> str:
        if not winds or not name:
            return ""
        hit = cls_mod.match_heat(name, winds)      # 复用匹配逻辑（精确/别名/唯一包含）
        if hit and hit.get("catalyst"):
            # 不再二次截断 —— cls 侧已按完整句子裁到 ≤78 字，
            # 这里再切会切出"…同比增"这种半句（2026-10-10 通读时抓到）
            return "催化：" + hit["catalyst"]
        return ""

    def _pct_txt(v) -> str:
        return f"{v:+.1f}%" if isinstance(v, (int, float)) else "—"

    items: list[dict] = []
    seen: set[str] = set()

    for i, t in enumerate(themes[:6]):
        name = t.get("sector") or ""
        if not name or name in seen:
            continue
        b = _board_lookup(boards_all, name)
        zt_n = _int(t.get("zt"), 0)
        early = _int(t.get("early"), 0) + _int(t.get("one_word"), 0)
        maxb = _int(t.get("max_boards"), 0)
        b_pct = b.get("pct") if b else None
        b_main = b.get("main") if b else None            # 亿

        score = zt_n * 2 + early * 1.5 + min(maxb, 5) * 2.5
        if isinstance(b_pct, (int, float)):
            score += max(b_pct, 0.0) * 1.2
        if isinstance(b_main, (int, float)):
            score += max(b_main, 0.0) * 0.4

        role = "主线" if (i == 0 or name == theme_name) else "支线"
        if role == "支线" and zt_n < 2:
            level = "观察"
        elif score >= 18:
            level = "强"
        elif score >= 10:
            level = "中"
        else:
            level = "弱"

        facts = [f"涨停 {zt_n} 家" + (f"（早封 {early} 家）" if early else ""),
                 f"最高 {maxb} 板"]
        if isinstance(b_pct, (int, float)):
            facts.append(f"板块 {_pct_txt(b_pct)}")
        if isinstance(b_main, (int, float)):
            facts.append(f"主力 {b_main:+.1f}亿")
        stk = (b or {}).get("streak")
        if isinstance(stk, int) and stk >= 2:
            facts.append(f"连续 {stk} 日净流入" if (b or {}).get("source") == "em"
                         else f"连续 {stk} 日走强")
        hf = _heat_fact(name)
        if hf:
            facts.append(hf)
        cf = _catalyst_fact(name)
        if cf:
            facts.append(cf)

        if role == "主线":
            judge = {
                "confirmed": "五信号共振，主线已确认 —— 明日看承接延续（空间龙溢价/首板留存）",
                "day1_pending": "首日启动成立 —— 明日开盘检验承接，接不住就是一日游",
                "one_day": "一日游特征 —— 不接力，等下一个方向",
                "watch": "信号不全，按观察处理",
            }.get(ml_status, "按观察处理")
        elif zt_n >= 2:
            judge = ("支线：有涨停合力 —— 主线走强时它能跟涨，主线一退它先死；"
                     "只做其最强的那一只，不铺开")
        else:
            judge = "支线（弱）：仅个别异动，等合力加厚再看"

        items.append({"name": name, "role": role, "level": level,
                      "score": round(score, 1), "facts": facts, "judge": judge})
        seen.add(name)

    # 纯资金板块：涨幅 + 主力净流入、但尚未形成涨停合力（轮动预备队）
    pure: list[dict] = []
    for b in boards_all:
        name = b.get("name") or ""
        if not name or name in seen:
            continue
        b_pct, b_main = b.get("pct"), b.get("main")
        if not isinstance(b_pct, (int, float)):
            continue
        if isinstance(b_main, (int, float)) and b_main <= 0:
            continue
        if b_pct >= 2.0:
            stk = b.get("streak")
            pure_facts = [f"板块 {_pct_txt(b_pct)}"
                          + (f"，主力 {b_main:+.1f}亿"
                             if isinstance(b_main, (int, float)) else "")]
            if isinstance(stk, int) and stk >= 2:
                pure_facts.append(f"连续 {stk} 日净流入" if b.get("source") == "em"
                                  else f"连续 {stk} 日走强")
            pure_facts.append("尚未形成涨停合力")
            hf = _heat_fact(name)
            if hf:
                pure_facts.append(hf)
            cf = _catalyst_fact(name)
            if cf:
                pure_facts.append(cf)
            pure.append({
                "name": name, "role": "资金支线", "level": "观察",
                "score": round(b_pct * 2 + max(b_main or 0.0, 0.0) * 0.4, 1),
                "facts": pure_facts,
                "judge": "有资金在动但还没打出涨停合力 —— 轮动预备队，见涨停再加进来",
            })
    pure.sort(key=lambda x: -x["score"])
    items += pure[:4]

    # 强势板块 = 综合分最高（分数并列时题材优先）
    strongest = None
    if items:
        strongest = sorted(
            items, key=lambda x: (-x["score"], 0 if x["role"] == "主线" else 1))[0]

    main_item = next((x for x in items if x["role"] == "主线"), None)
    branches = [x for x in items if x["role"] != "主线"]

    parts = []
    if main_item:
        parts.append(f"主线：{main_item['name']}（{main_item['level']}）")
    strong_b = [x for x in branches if x["level"] == "强"][:2]
    if strong_b:
        parts.append("支线：" + "、".join(f"{x['name']}({x['level']})" for x in strong_b))
    elif branches:
        parts.append("支线：" + "、".join(f"{x['name']}({x['level']})" for x in branches[:2]))

    src = flow.get("board_source") or ""
    if src == "sina":
        data_note = "东财板块接口受限，板块涨幅/资金为新浪口径（无主力净额）"
    elif src == "ths":
        data_note = "东财板块接口受限，板块涨幅/资金为同花顺口径（净流入近似主力净额）"
    elif src == "em":
        data_note = "板块数据来自东财 ulist.np（含主力净额）"
    else:
        data_note = "东财板块接口受限，暂无板块涨幅/资金数据，强弱仅按涨停合力判断"

    return {
        "available": bool(items),
        "main": main_item, "branches": branches,
        "strongest": strongest, "items": items[:10],
        "summary": "；".join(parts) if parts else "今日暂无明确的板块合力",
        "data_note": data_note,
    }
