# -*- coding: utf-8 -*-
"""E8 题材播种（事件 → 受益 → 标的）· 前置题材池（扩展模块，非野人原体系）。

一句话：**这个题材是今天刚播种的，还是已经长到第 N 天了？**

来历（用户 10-13 的原始想法）：
「打板要提前就找好题材才行，不然第二天打板看到那个封停打那个，不是完全没有
逻辑乱玩吗，现在缺的就是这个。」

系统原状：候选股是**当天量价形态打分** —— 形态是结果不是原因，天然马后炮。
缺的是「事件 → 受益品类 → 标的」的前瞻映射，与**题材生命周期**
（首日 / 第 N 日 / 退潮 / 重复启动）。本模块就是把这块补上。

做三件事（全部离线，零新增接口）：

  ① **题材日历** —— 今日「新播种 / 加固 / 持平 / 减弱 / 熄火」，并给每个题材标出
     首次出现日、窗口内出现天数、当日家数、最高板、代表票、事件依据。
  ② **独苗题材** —— 今日**只有 1 只票涨停、且是首板**的题材。语义是
     "题材刚冒头，只有一颗种子" —— 这正是"提前埋伏"最该看的一栏。
  ③ **重复启动** —— 同一只票在窗口内**启动 → 沉寂 → 再启动**（≥2 次启动），
     并标出**标签漂移**（同一票的题材标签发生迁移）。那是"逻辑在接力"的证据。

数据源：本地涨停池归档 `data/pool_history/*.json` 的 `reason` 字段 ——
东财给的**涨停原因**，格式通常是：

    「机器人概念+家电；1. 机器人概念：据2026年6月10日互动易…」
    「电子商务+家电；10月8日新闻联播：以人的需求为原点，激活消费大市场。…」

所以「；」前那一段就是**市场给它贴的题材标签**，后面是理由与出处 —— 这是本模块
做"标签漂移"与"事件抽取"的原料。

★ 关键实测（九阳股份 sz002242，本模块的三个信号它全中）：
    09-11  标签「家电+机器人概念」  ← 第 1 次启动
    09-28  标签「机器人概念+家电」  ← 第 2 次启动（间隔 7 个交易日）★ 提前埋伏的窗口
    09-30  标签漂移为「小家电」      09-30 公告：深思考 7.92% 股权 + 参股享刻智能
    10-09  标签漂移为「电子商务」    10/8 新闻联播 + 双11 档期（京东 10-12、淘宝 10-15）
    10-12  6 板 / 10-13 断板
  → **"提前"的判据 = 同一票在窗口内多次启动 ＋ 标签漂移。** 系统此前没在追这个。

已知限制（必须说清，否则会误导）：
  · `sector` 来自东财行业，粒度偏碎（出版 / 影视院线 / 电视广播 / 数字媒体各算一个），
    所以"题材家数"会**低估共振**。本模块不用它做硬判定，只用于排序与展示。
  · `reason` 的召回率决定上限：**事件还没变成涨停，就没有 reason**。所以纯政策前瞻
    （会议刚开完、明天谁会动）本模块**做不到**，需要另接政策/公告源。
    实测：10-13 实时快讯 600 条里「中美」「关税」「小家电」全无 —— 那条路的召回率不足。
  · 冷启动（归档不足 2 天）只能给当日快照，标不出"新播种/重复启动"。
"""
from __future__ import annotations

import re
from collections import Counter

from . import datasource as ds

# 事件标签表：从 reason 里识别"这条涨停是什么在驱动"。
# 顺序即展示优先级。刻意保持小而显式 —— 要加一类事件，加一行即可。
_EVENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("新闻联播", ("新闻联播",)),
    ("经贸/关税", ("磋商", "经贸", "关税", "贸易", "商务部", "豁免")),
    ("政策/规划", ("印发", "规划", "通知", "意见", "方案", "批复",
                   "发改委", "工信部", "国务院", "证监会")),
    ("并购重组", ("并购", "重组", "收购", "股权转让", "要约")),
    ("订单/中标", ("中标", "订单", "签订", "合同", "框架协议", "供货")),
    ("业绩", ("业绩", "半年报", "季报", "年报", "预增", "扭亏", "净利润")),
    ("涨价", ("涨价", "提价", "价格上调", "涨价函")),
    ("互动易", ("互动易", "投资者关系")),
    ("发布会/答记者问", ("答记者问", "发布会", "记者问", "记者独家")),
    ("新品/技术", ("量产", "突破", "专利", "研发", "首发")),
)

# reason 里引用的事件日期（原文口径，如「10月8日」「2026年9月30日」）
_DATE_RE = re.compile(r"(?:20\d{2}年)?\d{1,2}月\d{1,2}日")

TREND_CN = {"new": "新播种", "add": "加固中", "hold": "持平",
            "drain": "减弱", "gone": "熄火"}
_TREND_ORDER = {"new": 0, "add": 1, "hold": 2, "drain": 3, "gone": 4}


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


def _sector(row: dict) -> str:
    return str(row.get("sector") or "").strip()


# ---------------------------------------------------------------- reason 解析

def label_of(reason) -> str:
    """涨停原因里最前面那一段 = 市场给它贴的题材标签。

    东财这个字段有两种分隔符，**必须都切**：
      · 冒号式：「机器人概念+家电；1. 机器人概念：据…」→「机器人概念+家电」
      · 竖线式：「福建+建材|公司主要从事水泥及熟料的生产及销售，拥有7条…」→「福建+建材」
    实测近 30 个交易日 1725 条涨停原因里，有 138 条（8.0%）用的是竖线，
    只切「；」的话这些标签会变成「…|公司主要从事水泥…」这种把**公司简介**
    截断 40 字后冒充题材的脏字符串（实测「福建+建材|公司主要从事水泥及熟料
    的生产及销售，拥有7条新型干法熟料水泥生产线，」直接上了看板，还被判成
    「标签漂移」）。竖线后一定是正文（简介/公告细节），切掉即得纯标签。

    第三种（切完还是不像标签）：有些原因**根本没有分隔符**，整段就是公告正文或
    简介，例如「富春染织公告称，预计2026年前三季度归属于上市公司股东的净利润
    为3.38亿元-…」。这时若照旧返回前 40 字，等于把一句业绩预告当成题材名。
    所以补一道**像不像标签**的判据：切出来还带公告/简介句式特征词，或夹着逗号
    顿号这类句子标点的，一律判"没有题材标签"返回空串 —— 宁可没有标签（调用方
    会跳过它、不参与漂移判定），也不能拿正文冒充题材。
    """
    s = str(reason or "").strip()
    if not s:
        return ""
    head = re.split(r"[；;|｜]", s)[0].strip()
    # 像不像一个"题材标签"：真标签是「AI应用+低空经济」「海峡两岸+水泥」这种
    # 名词短语，不含句子标点、不含公告/简介句式。
    if _PROSE_TAIL.search(head) or re.search(r"[，,。；：]", head):
        return ""
    return head[:40]


# 公告 / 公司简介的句式特征 —— 出现即说明这段是正文不是题材名
_PROSE_TAIL = re.compile(
    r"公司(主要)?(从事|是|已|拟|将|位于|依托|拥有|自成立)"
    r"|是一家|主营业务|生产线|万吨|公告称|全资子公司"
    r"|归属于上市公司|预计\d{4}年|净利润"
)


def events_of(reason) -> list[str]:
    """这条涨停原因命中的事件类型（可能多个）。"""
    s = str(reason or "")
    if not s:
        return []
    return [name for name, kws in _EVENT_RULES if any(k in s for k in kws)]


def cited_dates(reason) -> list[str]:
    """原因里引用的事件日期（原文口径，如「10月8日」）。"""
    return _DATE_RE.findall(str(reason or ""))


# ---------------------------------------------------------------- 归档读取

def _natural_days(a: str, b: str) -> int:
    """两个 YYYYMMDD 日期之间的自然日差（b - a）。解析失败返回 99（视为不连续）。"""
    try:
        import datetime as _dt
        da = _dt.date(int(a[:4]), int(a[4:6]), int(a[6:8]))
        db = _dt.date(int(b[:4]), int(b[4:6]), int(b[6:8]))
        return (db - da).days
    except (ValueError, TypeError):
        return 99


def _window(days: int, upto: str = "") -> list[str]:
    """最近 days 个已归档交易日（升序）。`upto` 用于回放历史日期。"""
    dates = [d for d in ds.pool_history_dates() if not upto or d <= upto]
    return dates[-max(2, int(days)):]


def archives(days: int, upto: str = "") -> dict[str, list[dict]]:
    """{code: [各归档日记录（附加 `_date`，按日期升序）]}。"""
    out: dict[str, list[dict]] = {}
    for date in _window(days, upto):
        for row in ds.load_pool_history(date):
            code = str(row.get("code") or "")
            if code:
                out.setdefault(code, []).append(dict(row, _date=date))
    for rows in out.values():
        rows.sort(key=lambda r: str(r.get("_date") or ""))
    return out


def _names(rows: list[dict], limit: int = 3) -> list[str]:
    rows = sorted(rows, key=lambda r: -_int(r.get("boards")))
    return [str(r.get("name") or r.get("code") or "") for r in rows[:limit]]


# ---------------------------------------------------------------- ① 题材日历

def theme_calendar(days: int, upto: str, today: str,
                   today_rows: list[dict], prev_date: str) -> list[dict]:
    """题材生命周期：今日新播种 / 加固 / 持平 / 减弱 / 熄火。"""
    hist = [d for d in _window(days, upto) if d < today]
    counts = {d: Counter(_sector(r) for r in ds.load_pool_history(d)) for d in hist}
    appeared: dict[str, str] = {}          # 题材 → 窗口内首次出现日
    seen_days: dict[str, int] = {}
    for d in hist:
        for th in counts[d]:
            if th:
                appeared.setdefault(th, d)
                seen_days[th] = seen_days.get(th, 0) + 1

    today_c = Counter(_sector(r) for r in today_rows)
    prev_c = counts.get(prev_date) or Counter()

    out = []
    for th in set(today_c) | set(prev_c):
        if not th:
            continue
        t, p = today_c.get(th, 0), prev_c.get(th, 0)
        if t > 0 and th not in appeared:
            trend = "new"
        elif t > p:
            trend = "add"
        elif t > 0 and t == p:
            trend = "hold"
        elif t > 0:
            trend = "drain"
        else:
            trend = "gone"
        rows = [r for r in today_rows if _sector(r) == th]
        ev: list[str] = []
        for r in rows:
            for e in events_of(r.get("reason")):
                if e not in ev:
                    ev.append(e)
        out.append({
            "theme": th, "trend": trend, "label_cn": TREND_CN[trend],
            "today_n": t, "prev_n": p,
            "first_date": appeared.get(th, ""), "seen_days": seen_days.get(th, 0),
            "max_boards": max([_int(r.get("boards")) for r in rows] or [0]),
            "leaders": _names(rows, 3), "events": ev,
        })

    out.sort(key=lambda x: (_TREND_ORDER[x["trend"]], -x["today_n"],
                            -x["prev_n"], x["theme"]))
    return out


def _theme_why(th: dict, days: int) -> str:
    t, p, tr = th["today_n"], th["prev_n"], th["trend"]
    who = "、".join(th["leaders"]) or "—"
    if tr == "new":
        return (f"今日首次出现：{t} 家涨停（{who}）"
                f"—— 题材刚播种，还没有历史梯队可参考")
    if tr == "add":
        return f"今日 {t} 家（上一交易日 {p} 家，{who}）—— 在加固"
    if tr == "hold":
        return f"今日 {t} 家，与上一交易日持平（{who}）—— 未加固也未退"
    if tr == "drain":
        return (f"今日 {t} 家（上一交易日 {p} 家，{who}）—— 在减弱，"
                f"别把它当成「还在发酵」")
    return (f"上一交易日 {p} 家，今日 0 家 —— 熄火。"
            f"该题材在近 {days} 个交易日里出现过 {th['seen_days']} 天")


# ---------------------------------------------------------------- ② 独苗题材

def lonely_seeds(today_rows: list[dict], days: int, upto: str, today: str,
                 max_peers: int, top_n: int) -> list[dict]:
    """今日只有 1 只票涨停、且是首板的题材 —— "题材刚冒头，只有一颗种子"。"""
    today_c = Counter(_sector(r) for r in today_rows)
    hist = [d for d in _window(days, upto) if d < today]
    seen: dict[str, int] = {}
    for d in hist:
        for r in ds.load_pool_history(d):
            s = _sector(r)
            if s:
                seen[s] = seen.get(s, 0) + 1

    out = []
    for r in today_rows:
        if _int(r.get("boards")) != 1:          # 只认首板 —— "刚播种"
            continue
        sec = _sector(r)
        if not sec or today_c.get(sec, 0) > max_peers:
            continue
        ev = events_of(r.get("reason"))
        first = _int(r.get("first_time"))
        early = bool(first) and first <= 100000          # 10:00 前封
        unbroken = _int(r.get("break_count")) == 0
        is_new = sec not in seen
        score = 30.0 if ev else 0.0                      # 有事件依据
        score += 20.0 if early else 0.0                  # 早封
        score += 15.0 if unbroken else 0.0               # 未炸板
        score += (15.0 if is_new else 20.0)              # 处女地 / 老树新芽
        out.append({
            "code": str(r.get("code") or ""), "name": str(r.get("name") or ""),
            "theme": sec, "label": label_of(r.get("reason")),
            "pct": round(_f(r.get("pct")), 2),
            "first_time": first, "break_count": _int(r.get("break_count")),
            "events": ev, "new_theme": is_new,
            "hist_days": int(seen.get(sec, 0)),
            "score": round(score, 1),
        })

    out.sort(key=lambda x: (-x["score"], x["name"]))
    return out[:max(1, top_n)]


def _lonely_why(seed: dict, max_peers: int) -> str:
    ev = ("、".join(seed["events"])) if seed["events"] else "未见明确事件依据"
    base = (f"「{seed['theme']}」今日只有 {max_peers} 只以内的票涨停，"
            f"它是唯一那（几）只里的首板")
    pos = ("属**全新题材**，之前没培育过 —— 是纯播种，成不成看明天有没有第二只跟上"
           if seed["new_theme"] else
           "这个题材之前出现过，属于**老树新芽**（有旧筹码，也有旧套牢盘）")
    return f"{base}；{pos}。事件依据：{ev}。"


# ---------------------------------------------------------------- ③ 重复启动

def restarts(days: int, upto: str, today_codes: set[str],
             top_n: int, drifted_only: bool = False) -> list[dict]:
    """同一只票在窗口内「启动 → 沉寂 → 再启动」（≥2 次启动）。

    连板链本身算**一次**启动（连续涨停日）；中间隔了非涨停日才算新的一次。
    实测九阳：09-11 一次、09-28~10-12 一次 → 2 次启动，间隔 7 个交易日。
    """
    w = _window(days, upto)
    idx = {d: i for i, d in enumerate(w)}
    out = []
    for code, rows in archives(days, upto).items():
        dates = sorted(str(r.get("_date") or "") for r in rows)
        # ★ 用**自然日差**判"连续"，不能只看归档下标：相邻两个归档日可能相隔
        #   多天（周末/节假日，或中间那几天该票根本没涨停）。实测九阳 09-11 →
        #   09-28 中间隔着 09-14~09-24 多个交易日。间隔 ≥ 2 个自然日 = 中间至少
        #   空了一天 = 不是一路连板。
        runs: list[list[str]] = []                 # 每次启动的日期列表
        cur = [dates[0]]
        for a, b in zip(dates, dates[1:]):
            gap = _natural_days(a, b)
            if gap <= 1:
                cur.append(b)                       # 相邻自然日 → 同一段连板
            else:
                runs.append(cur)
                cur = [b]
        runs.append(cur)
        if len(runs) < 2:
            continue                              # 一路连板 = 一次启动，不是"再启动"

        # ★ 标签漂移要按**词集合**判定，不能按字符串：实测九阳 09-11 是
        #   「家电+机器人概念」、09-28 是「机器人概念+家电」—— 只是顺序不同，
        #   不是漂移。真正的漂移是后来 {小家电} / {电子商务,家电} 那种换逻辑。
        labs: dict[frozenset, str] = {}
        for r in rows:
            lb = label_of(r.get("reason"))
            if not lb:
                continue
            sig = frozenset(t.strip() for t in re.split(r"[+＋]", lb) if t.strip())
            if sig:
                labs.setdefault(sig, lb)
        labels = list(labs.values())
        drifted = len(labels) >= 2
        if drifted_only and not drifted:
            continue

        ev: list[str] = []
        for r in rows:
            for e in events_of(r.get("reason")):
                if e not in ev:
                    ev.append(e)

        names = [str(r.get("name") or "") for r in rows if r.get("name")]
        name = names[-1] if names else code
        by_date = {str(r.get("_date")): _int(r.get("boards")) for r in rows}
        lbl_date = {str(r.get("_date")): label_of(r.get("reason")) for r in rows}
        launches = [{"date": run[0], "boards": by_date.get(run[0], 0),
                     "label": lbl_date.get(run[0], "")}
                    for run in runs]
        # 两次启动之间的空档（自然日）：上一次启动的最后一天 → 下一次启动的第一天
        gaps = [_natural_days(runs[i][-1], runs[i + 1][0]) - 1
                for i in range(len(runs) - 1)]
        max_boards = max(_int(r.get("boards")) for r in rows)
        still = code in today_codes

        # 打分刻意**不重高度** —— 这是个前瞻模块，高度是后验的（九阳 09-28
        # 那天只有 1 板，但正是它该被看见的时候）。权重给：
        # 启动次数 / 标签漂移 / 事件依据 / 沉寂时长（洗过再发动） / 今天是否仍在池。
        score = 30.0 * min(3, len(runs))
        score += min(15.0, 5.0 * max_boards)
        score += 25.0 if drifted else 0.0
        score += 20.0 if ev else 0.0
        score += min(20.0, 2.0 * (max(gaps) if gaps else 0))
        score += 10.0 if still else 0.0
        out.append({
            "code": code, "name": name, "label": labels[-1] if labels else "",
            "labels": labels, "launches": launches, "launch_count": len(runs),
            "gap_days": max(gaps) if gaps else 0,
            "max_boards": max_boards, "events": ev, "drifted": drifted,
            "still": still, "score": round(score, 1),
        })

    out.sort(key=lambda x: (-x["score"], -x["launch_count"], -x["max_boards"]))
    return out[:max(1, top_n)]


def _restart_why(item: dict, days: int) -> str:
    ds_ = "、".join(f"{l['date']}（{l['boards']}板）" for l in item["launches"][:4])
    head = (f"{item['name']} 在近 {days} 个交易日里启动了 {item['launch_count']} 次"
            f"（{ds_}），两次之间空 {item['gap_days']} 个交易日 —— "
            f"不是一路连板，是「启动→沉寂→再启动」")
    if item["drifted"]:
        drift = (f"。题材标签从「{item['labels'][0]}」漂移到"
                 f"「{item['labels'][-1]}」—— 资金在给它换逻辑接力，"
                 f"这通常比单纯的「再来一波」更持久")
    else:
        drift = "。标签没变，属于同一逻辑的二次发动"
    ev = (f"。事件依据：{'、'.join(item['events'])}") if item["events"] else \
        "。未见明确事件依据（纯资金行为，留意随时熄火）"
    now = ("。它今天仍在涨停池里。" if item["still"] else
           "。它今天已不在涨停池（断板或停牌）—— 这一条只当复盘。")
    return head + drift + ev + now


# ---------------------------------------------------------------- 主入口

def evaluate(cfg: dict | None = None, snaps: dict | None = None,
             zt: list[dict] | None = None, session: dict | None = None,
             real_date: str = "") -> dict:
    """题材播种（事件 → 受益 → 标的）。

    返回 {enabled, active, reason, mode, date, themes[], lonely[], restarts[],
          summary, note}。冷启动时 active=False 并说明原因，不硬凑。
    """
    a = (cfg or {}).get("E8_sowing", {}) or {}
    enabled = bool(a.get("enabled", True))
    empty = {"enabled": enabled, "active": False, "reason": "", "mode": "题材播种",
             "date": real_date, "themes": [], "lonely": [], "restarts": [],
             "summary": "", "note": ""}
    if not enabled:
        empty["reason"] = "模块已关闭"
        return empty

    dates = ds.pool_history_dates()
    if not dates:
        empty["reason"] = ("本地还没有涨停池归档（冷启动）—— 题材生命周期与"
                           "「重复启动」都要靠历史归档，等积累几个交易日")
        return empty

    today = real_date if real_date in dates else dates[-1]
    # ★ 默认窗口给 30 个交易日：实测九阳两次启动间隔 9 个交易日（09-11 → 09-28），
    #   15 天窗口会把第一次启动挤出视野，导致"两次启动"被误判成"一路连板"。
    days = _int(a.get("lookback_days"), 30)
    max_peers = _int(a.get("lonely_max_peers"), 1)
    top_themes = _int(a.get("top_themes"), 8)
    top_lonely = _int(a.get("top_lonely"), 3)
    top_restarts = _int(a.get("top_restarts"), 6)
    drifted_only = bool(a.get("drifted_only", False))

    live = [dict(z) for z in (zt or [])]
    today_rows = live if live else ds.load_pool_history(today)
    if not today_rows:
        empty["reason"] = (f"{today} 没有可用的涨停池数据 —— 判不出题材播种情况")
        return empty

    prev_date = ds.pool_history_prev_date(today)
    themes = theme_calendar(days, today, today, today_rows, prev_date)
    lonely = lonely_seeds(today_rows, days, today, today, max_peers, top_lonely)
    today_codes = {str(r.get("code") or "") for r in today_rows}
    rsts = restarts(days, today, today_codes, top_restarts, drifted_only)

    for th in themes:
        th["why"] = _theme_why(th, days)
    for sd in lonely:
        sd["why"] = _lonely_why(sd, max_peers)
    for it in rsts:
        it["why"] = _restart_why(it, days)

    themes = themes[:max(1, top_themes)]
    active = bool(themes or lonely or rsts)
    if not active:
        empty["reason"] = "今日涨停池为空，或全部题材都没有可判读的生命周期"
        return empty

    new_th = [t["theme"] for t in themes if t["trend"] == "new"]
    gone_th = [t["theme"] for t in themes if t["trend"] == "gone"]
    drifted = [r["name"] for r in rsts if r["drifted"]]
    parts = [f"题材共 {len(themes)} 个可判读"]
    if new_th:
        parts.append(f"新播种 {len(new_th)} 个（{'、'.join(new_th[:4])}）")
    if gone_th:
        parts.append(f"熄火 {len(gone_th)} 个（{'、'.join(gone_th[:3])}）")
    if lonely:
        parts.append(f"独苗题材 {len(lonely)} 个（{'、'.join(x['name'] for x in lonely)}）")
    if rsts:
        parts.append(f"重复启动 {len(rsts)} 只"
                     + (f"，其中标签漂移 {len(drifted)} 只（{'、'.join(drifted)}）"
                        if drifted else ""))
    summary = "；".join(parts) + "。"

    return {
        "enabled": True, "active": True, "reason": "", "mode": "题材播种",
        "date": today, "themes": themes, "lonely": lonely, "restarts": rsts,
        "summary": summary,
        "note": ("这张卡是**备选池**，不是买入信号 —— 它只回答"
                 "「明天该盯哪几条线」，不回答「买哪只」。"
                 "口径与限制：① 题材名来自东财行业，粒度偏碎（出版 / 影视院线 / "
                 "电视广播各算一个），所以「家数」会低估共振；"
                 "② 只认**已涨停**的票 —— 事件还没变成涨停就没有涨停原因，"
                 "所以纯政策前瞻（会议刚开完、明天谁会动）这里给不出来，"
                 "需要另接政策/公告源；"
                 "③ 「重复启动」要求两次启动之间有空档，一路连板不算。"),
    }
