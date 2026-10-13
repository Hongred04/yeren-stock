# -*- coding: utf-8 -*-
"""E6 停牌·补位推演（扩展模块，非野人原体系）。

一句话：**龙头停牌了，同题材里谁在补位？**

来历（用户 10-13 的真实复盘）：10-12 晚间新华传媒（sh600825，「出版」·
并购重组，**10 连板**）公告停牌核查。次日盘面：

  · 新华传媒全天零成交 —— 现价 13.78、成交额 **0**、换手 **0**（停牌）
  · 同题材「出版」6 家涨停（新经典 3 板、龙版传媒 3 板 …）
  · 「影视院线」4 家（慈文传媒 3 板）、「电视广播」2 家（广电网络 2 板）

**这套系统当时完全没接住**，原因全在设计层，不是数据缺失：
  ① `news.LEVEL_RULES` 的关键词表里没有「停牌」，而且个股公告（C 级）
     被**主动丢弃**（"C 级不记 —— 那是噪声"）→ 停牌消息在那边永远出不来；
  ② M9 主线只认自己聚类出的**主题材**（当天是"化学制品"），题材之外的
     个股事件一律不参与定级；
  ③ 没有任何模块把「某只高位股全天零成交」当成**事件信号** ——
     engine 只在选股时把它当"无成交，跳过该门槛"。

判定链（全部用现有数据，不新增接口）：

  ① **停牌** —— 交易时段内，某只**近期有连板基因**的票，今日快照
     「有报价、成交额 = 0、换手 = 0」→ 全天无撮合 = 停牌。
     ★ 只在**有撮合发生**的时段判定。盘前与集合竞价全市场成交额都是 0，
       区分不出来，硬用会把整个市场判成停牌 —— 那两个时段改走公告层（②）。
       一字板不会落进来：一字板在集合竞价有成交量，成交额 > 0。
  ② **题材与高度** —— 取它在历史涨停池里**最近一次**的 `sector`（东财所属
     行业）与连板高度；高度 < min_boards 的票不算"龙头停牌"，当噪声丢弃。
     盘前（pre_open/auction）快照这条不成立，改用**停牌公告**：从快讯里
     按「股票名 + 停牌」识别，再用本地名单反查代码 —— 这条路径才做得到
     "昨晚就知道、开盘前就能预判"（`news.fetch_news` 自带 5 分钟缓存，
     与 mainline 共用，不额外加请求）。
  ③ **补位** —— 今日涨停池里**同 sector 的其他票**，按"接棒概率"排序：
     早封优先 / 板数适中优先（1~2 板最像新龙头，≥4 板是接力风险）/
     封板未炸加分 / 封单额加分。并给出「同题材今日涨停家数 vs 上一交易日」
     作为补位强度的实测依据。

定位与 E5 一致：**提醒去看一眼**，不是买入指令。停牌不必然补位 ——
龙头停牌本身就是高度不确定，整条线也可能跟着退潮，所以卡片必须把
"停牌"与"已有资金补位"分成两句话说，不混成一句结论。

已知限制（如实标注，不粉饰）：
  · 补位候选只能覆盖**今日已涨停**的同题材票 —— 未涨停的同题材票需要
    "个股→所属概念"的完整成分表，而本地自建成份表是**抽样**的
    （每板块仅 2~4 只），拿它当候选源会漏掉大多数，故不使用。
  · 「停牌」判定依赖快照覆盖全市场（pipeline 用 load_valid_codes 全量拉），
    若某轮扫描降级成部分名单，会漏判 —— 属可接受的漏报，不会误报。
"""
from __future__ import annotations

import re
import time

from . import datasource as ds
from . import news as news_mod

# 有撮合发生的时段：零成交才有判别力
LIVE_PHASES = ("morning", "noon", "afternoon", "tail", "just_closed", "after_close")
# 盘前/集合竞价：快照区分不出停牌，改走公告层
PRE_PHASES = ("pre_open", "auction")

# 个股停牌公告的判据。`news.LEVEL_RULES` 里没有「停牌」，这里必须自成一套。
HALT_KWS = ("停牌", "暂停交易")

# 6 位代码（公告里常带）
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")

# 题材相邻扩展 —— 东财「所属行业」把同一条题材切得很碎。实测 10-13：
#   新华传媒 = 出版、慈文传媒 = 影视院线、广电网络 = 电视广播、
#   风语筑/返利科技 = 数字媒体、元隆雅图 = 广告营销 ——
# 它们在同一条「内容/传媒」题材上，行业标签却各不相同。只按行业**全等**匹配
# 会漏掉当天真正的补位龙头（慈文传媒 3 板就是传媒题材的高度领跑者）。
# 这张表**小而显式**（不做语义猜测，表里没有的行业一律退化为全等匹配），
# 且可被配置项 E6_substitute.theme_siblings 覆盖/扩充。
THEME_SIBLINGS: dict[str, list[str]] = {
    "出版": ["影视院线", "电视广播", "数字媒体", "广告营销"],
    "影视院线": ["出版", "电视广播", "数字媒体", "广告营销"],
    "电视广播": ["出版", "影视院线", "数字媒体", "广告营销"],
    "数字媒体": ["出版", "影视院线", "电视广播", "广告营销"],
}


def _siblings(theme: str, cfg: dict) -> list[str]:
    """该题材的相邻行业列表（配置可覆盖 THEME_SIBLINGS）。"""
    table = {k: list(v) for k, v in THEME_SIBLINGS.items()}
    extra = cfg.get("theme_siblings")
    if isinstance(extra, dict):
        for k, v in extra.items():
            if isinstance(v, (list, tuple)):
                table[str(k)] = [str(x) for x in v]
    return [x.strip() for x in (table.get(theme) or []) if str(x).strip()]


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


def _to_min(hhmmss) -> int:
    """92500 → 565（当日第几分钟）。

    HHMMSS **不是**线性刻度（9:59:59→10:00:00 差 1 秒却差 4059），
    必须先拆出时分再算，否则每跨一个整点就多算 40 分钟。
    """
    t = _int(hhmmss, 0)
    return (t // 10000) * 60 + (t // 100) % 100


def _deadline_min(txt: str) -> int:
    try:
        h, m = str(txt).split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return 10 * 60 + 30


# ---------------------------------------------------------------- 停牌识别

def snapshot_halted(snap: dict) -> bool:
    """全天无撮合 = 停牌：**有报价，但成交额与换手都是 0**。

    三个条件缺一不可：
      · price > 0 —— 停牌股仍挂昨收价；退市/无数据的票连价都没有，不算
      · amount = 0 —— 全天零成交
      · turnover = 0 —— 与 amount 互为印证，挡"成交额字段缺失被填 0"的假象
    一字板不落进来（集合竞价有成交量，amount > 0）。
    """
    if not snap:
        return False
    return (_f(snap.get("price")) > 0
            and _f(snap.get("amount")) <= 0
            and _f(snap.get("turnover")) <= 0)


def _name_to_code() -> dict[str, str]:
    """{股票名: 代码} —— 由本地名单（code→name）反转。"""
    out: dict[str, str] = {}
    try:
        idx = ds._name_index_load() or {}
    except Exception:                                        # noqa: BLE001
        return out
    for code, nm in idx.items():
        nm = str(nm or "").strip()
        if len(nm) >= 2:
            out.setdefault(nm, code)
    return out


def _is_halt_text(text: str) -> bool:
    return any(k in text for k in HALT_KWS)


def news_halt_signals(news: list[dict], hours: float = 36.0) -> list[dict]:
    """从快讯里挑出**个股停牌公告** → [{code, name, text, time, src}]。

    只在含「停牌/暂停交易」的条目里再去匹配股票名（那类条目本来就少，
    不做全量 × 全名单的笛卡尔扫描）。名字取**最长匹配**：避免
    「新华传媒」被「新华」这类短名抢先命中。
    """
    if not news:
        return []
    names = _name_to_code()
    cut = None
    if hours:
        cut = time.time() - hours * 3600
    out: list[dict] = []
    seen: set[str] = set()
    for it in news:
        text = str(it.get("text") or "")
        if not _is_halt_text(text):
            continue
        if cut is not None:
            ts = _parse_ts(str(it.get("time") or ""))
            if ts and ts < cut:
                continue
        hit_name, hit_code = "", ""
        # ① 公告里带 6 位代码 → 优先（最可靠）
        m = _CODE_RE.search(text)
        if m:
            cand = _with_market(m.group(1))
            if cand:
                hit_code = cand
                hit_name = _code_to_name(cand)
        # ② 否则在名字表里找最长匹配
        if not hit_code:
            for nm in names:
                if nm in text and len(nm) > len(hit_name):
                    hit_name, hit_code = nm, names[nm]
        if not hit_code or hit_code in seen:
            continue
        seen.add(hit_code)
        out.append({"code": hit_code, "name": hit_name or hit_code,
                    "text": text[:90], "time": str(it.get("time") or ""),
                    "src": str(it.get("source") or "")})
    return out


def _parse_ts(t: str) -> float:
    """'2026-10-12 21:03:11' → 时间戳；解析失败返回 0。"""
    try:
        from datetime import datetime
        return datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return 0.0


def _with_market(num: str) -> str:
    """6 位数字 → 带市场前缀的规范化代码（与 universe.normalize 同口径）。"""
    try:
        from . import universe
        return universe.normalize(num)
    except Exception:                                        # noqa: BLE001
        return ""


def _code_to_name(code: str) -> str:
    try:
        return str((ds._name_index_load() or {}).get(code) or "")
    except Exception:                                        # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 历史与题材

def _history_index(days: int, upto: str = "") -> dict[str, dict]:
    """{code: 最近一次涨停池记录（附 _date）}。

    按日期升序遍历、后写覆盖 → 留下的就是每只票**最新**的那条，
    `sector` 与 `boards` 都取最新口径（东财会随题材演化改 sector，
    实测新华传媒从「并购重组」漂到「出版」）。

    upto：只看 **≤ 该日期**的归档。不截断的话，回放历史日期时会把
    之后的归档也读进来，判定与"当天"不一致（实测：用 09-01 回放，
    仍能读到 10 月的归档并报出停牌）。
    """
    out: dict[str, dict] = {}
    dates = [d for d in ds.pool_history_dates() if not upto or d <= upto]
    for date in dates[-days:]:
        for row in ds.load_pool_history(date):
            code = str(row.get("code") or "")
            if code:
                out[code] = dict(row, _date=date)
    return out


def _theme_zt_count(date: str, theme: str, exclude_code: str = "",
                    siblings: list[str] | None = None) -> int:
    """某日涨停池里属于该题材的票数（补位强度的实测口径）。

    ★ 必须能排除停牌股自己：本地当日归档里**往往还留着它**（实测
      pool_history/20261013.json 里新华传媒仍记为「出版·10 板」），
      不排除就会把"停牌"算成"涨停"，家数凭空 +1，甚至让
      "同题材今日无涨停"这个分支永远走不到。
    """
    if not date or not theme:
        return 0
    allow = {theme, *(siblings or [])}
    return sum(1 for r in ds.load_pool_history(date)
               if str(r.get("sector") or "").strip() in allow
               and str(r.get("code") or "") != exclude_code)


# ---------------------------------------------------------------- 补位打分

def peer_score(peer: dict, cfg: dict) -> tuple[float, list[str]]:
    """同题材候选的"接棒概率"（0~100）与依据。

    权重：早封 40 / 板数 30 / 未炸板 20 / 封单额 10。
    ★ 板数**适中**最优：1 板最像"新龙头"，2 板次之；≥4 板是接力风险 ——
      体系对高位连板一贯"不接力"，补位推演不能反着来。
    """
    dl = _deadline_min(cfg.get("early_deadline", "10:30"))
    first = _to_min(peer.get("first_time"))
    boards = _int(peer.get("boards"), 1)
    why: list[str] = []
    score = 0.0
    if first and first <= dl:
        score += 40
        why.append(f"早封 {first // 60:02d}:{first % 60:02d}")
    if boards <= 1:
        score += 30
        why.append("首板（最像新龙头）")
    elif boards == 2:
        score += 24
        why.append("2 板")
    elif boards == 3:
        score += 12
        why.append("3 板")
    else:
        why.append(f"{boards} 板（接力风险）")
    if _int(peer.get("break_count")) == 0:
        score += 20
        why.append("封板未炸")
    fund = _f(peer.get("fund"))
    if fund >= 0.5e8:
        score += 10
        why.append(f"封单 {fund / 1e8:.1f} 亿")
    return score, why


# ---------------------------------------------------------------- 主入口

def evaluate(cfg: dict | None = None, snaps: dict | None = None,
             zt: list[dict] | None = None, session: dict | None = None,
             real_date: str = "", news: list[dict] | None = None) -> dict:
    """停牌·补位推演。

    返回 {enabled, active, src, reason, halts[], summary, note}。
    halts 为空时 active=False，并说明为什么不出这张卡（不硬凑）。
    """
    a = (cfg or {}).get("E6_substitute", {}) or {}
    enabled = bool(a.get("enabled", True))
    empty = {"enabled": enabled, "active": False, "src": "", "reason": "",
             "halts": [], "summary": "", "note": ""}
    if not enabled:
        empty["reason"] = "模块已关闭"
        return empty

    snaps = snaps or {}
    zt = zt or []
    phase = str((session or {}).get("phase") or "")
    days = _int(a.get("lookback_days"), 5)
    min_boards = _int(a.get("min_boards"), 2)

    hist = _history_index(days, upto=real_date)
    if not hist:
        empty["reason"] = ("本地还没有涨停池归档（冷启动）—— "
                           "判不出谁是龙头，等积累几个交易日")
        return empty

    # ---- ① 找出停牌股（两条路径，按时段分派）----
    src = ""
    cands: list[tuple[str, dict, str]] = []      # (code, 历史记录, 公告原文)
    if phase in LIVE_PHASES:
        src = "零成交快照"
        for code, rec in hist.items():
            if snapshot_halted(snaps.get(code)):
                cands.append((code, rec, ""))
    elif phase in PRE_PHASES:
        src = "盘前停牌公告"
        if news is None:
            try:
                news = news_mod.fetch_news()
            except Exception:                                # noqa: BLE001
                news = []
        for sig in news_halt_signals(news):
            rec = hist.get(sig["code"])
            if rec:
                cands.append((sig["code"], rec, sig.get("text") or ""))
    else:
        empty["reason"] = "非交易日（周末休市），不判停牌"
        return empty

    # ---- ② 只留"龙头级"的停牌（高度不够的是噪声）----
    halts: list[dict] = []
    seen: set[str] = set()
    for code, rec, note_text in cands:
        if code in seen:
            continue
        seen.add(code)
        boards = _int(rec.get("boards"), 0)
        if boards < min_boards:
            continue
        theme = str(rec.get("sector") or "").strip()
        snap = snaps.get(code) or {}
        halts.append({
            "code": code,
            "name": str(rec.get("name") or snap.get("name") or code),
            "theme": theme,
            "hist_boards": boards,
            "last_date": str(rec.get("_date") or ""),
            "last_pct": round(_f(rec.get("pct")), 2),
            "reason_txt": str(rec.get("reason") or "")[:60],
            "news_text": note_text,
            "peers": [],
        })
    halts.sort(key=lambda h: -h["hist_boards"])

    # ---- ③ 同题材补位：今日涨停池里同 sector 的其他票 ----
    dates = ds.pool_history_dates()
    prev_date = ""
    for d in dates:
        if d < real_date:
            prev_date = d
    max_halts = _int(a.get("max_halts"), 3)
    top_peers = _int(a.get("top_peers"), 4)
    for h in halts[:max_halts]:
        theme = h["theme"]
        sibs = _siblings(theme, a) if theme else []
        allow = ({theme, *sibs} if theme else set())
        # 今日家数取**实时涨停池**（停牌股不在其中，天然不重复计它）；
        # 昨日家数取归档，并显式排除停牌股自己 —— 两边口径都是
        # "除停牌股之外，同题材（含相邻行业）还有几家涨停"。
        today = (sum(1 for z in zt
                     if str(z.get("sector") or "").strip() in allow)
                 if theme else 0)
        prev = (_theme_zt_count(prev_date, theme, exclude_code=h["code"],
                                siblings=sibs) if theme else 0)
        peers = []
        if theme:
            for z in zt:
                if str(z.get("code")) == h["code"]:
                    continue
                sec = str(z.get("sector") or "").strip()
                if sec not in allow:
                    continue
                sc, why = peer_score(z, a)
                rel = "same" if sec == theme else "near"
                peers.append({
                    "code": str(z.get("code") or ""),
                    "name": str(z.get("name") or ""),
                    "boards": _int(z.get("boards"), 1),
                    "pct": round(_f(z.get("pct")), 2),
                    "first_time": _int(z.get("first_time"), 0),
                    "break_count": _int(z.get("break_count"), 0),
                    "fund": round(_f(z.get("fund")) / 1e8, 2),
                    "float_mv": round(_f(z.get("float_mv")) / 1e8, 1),
                    "score": round(sc, 1), "why": why,
                    # 同行业 / 相邻题材：相邻的排在同行业之后，且界面上标出来，
                    # 不让"扩了匹配范围"这件事躲在线下
                    "rel": rel,
                    "note": ("同行业" if rel == "same" else f"相邻题材·{sec}"),
                })
        # ★ 相邻题材**不直接沉底、也不与同行业同权**：扣一个固定分再参与排序。
        #   沉底会把真正的补位龙头挤掉（实测 10-13：出版同行业只有 2 只，
        #   而传媒题材的高度领跑者慈文传媒挂在「影视院线」下）；同权又会
        #   让标签差异消失。扣分 = near_penalty（默认 8 分），可由配置调。
        penalty = _f(a.get("near_penalty"), 8.0)
        peers.sort(key=lambda p: (-(p["score"] - (0.0 if p["rel"] == "same" else penalty)),
                                  0 if p["rel"] == "same" else 1, -p["boards"]))
        h["peers"] = peers[:top_peers]
        h["theme_zt_today"] = today
        h["theme_zt_prev"] = prev
        h["how"] = _how_text(h, theme, top_peers)
    halts = halts[:max_halts]

    if not halts:
        empty["src"] = src
        empty["reason"] = (f"扫描了 {len(hist)} 只近期涨停股的快照，"
                           f"没有发现「近期 ≥{min_boards} 连板 + 今日零成交」"
                           f"的停牌龙头 —— 今天没有可推演的补位")
        return empty

    act = [h for h in halts if h["theme_zt_today"] > 0]
    names = "、".join(f"{h['name']}（{h['hist_boards']} 板·{h['theme'] or '题材未归档'}）"
                      for h in halts)
    if act:
        lead = act[0]
        top = (lead["peers"][0]["name"] if lead.get("peers") else "")
        line = (f"停牌龙头 {lead['name']} 的同题材「{lead['theme']}」今日 "
                f"{lead['theme_zt_today']} 家涨停（上一交易日 {lead['theme_zt_prev']} 家）"
                f" —— {_verdict(lead['theme_zt_today'], lead['theme_zt_prev'])}"
                + (f"，其中 {top} 最像接棒" if top else ""))
    else:
        line = ("停牌龙头的同题材今天还没有涨停票 —— 补位尚未发生，"
                "只作为观察，不要提前埋伏")
    summary = f"发现 {len(halts)} 只停牌龙头：{names}。{line}"

    return {
        "enabled": True, "active": True, "src": src, "reason": "",
        "halts": halts, "summary": summary,
        "note": ("停牌 ≠ 一定补位：龙头停牌本身就是高度不确定（核查 / 重组"
                 "含义截然不同），整条线也可能跟着退潮。这张卡只回答「同题材里"
                 "有没有资金已经在动」，是提醒去看一眼，不是买入指令。"
                 "口径：停牌原因要自己看公告确认 —— 系统只给事实，不做定性。"),
    }


def _verdict(n: int, p: int) -> str:
    """用「今日 vs 上一交易日」的同题材涨停家数说清补位到底有没有发生。

    ★ 只写"有涨停就算补位"是不诚实的：实测 10-13 出版题材今日 2 家、
      上一交易日也是 2 家 —— **持平**，谈不上补位。措辞必须跟着数字走。
    """
    if n <= 0:
        return "补位尚未发生"
    if n > p:
        return "资金在往这里补位"
    if n == p:
        return "与上一交易日持平 —— 看不出补位，先当观察"
    return f"比上一交易日少 {p - n} 家 —— 同题材在退潮，不是补位"


def _how_text(h: dict, theme: str, top_peers: int) -> str:
    """每只停牌股的"怎么用"口径：停牌与补位分两句，不混成一句结论。"""
    n, p = h.get("theme_zt_today", 0), h.get("theme_zt_prev", 0)
    head = (f"{h['name']} 停牌前 {h['hist_boards']} 连板"
            + (f"，题材「{theme}」" if theme else "")
            + (f"，最近一次记录在 {h['last_date']}" if h.get("last_date") else "")
            + "。")
    if not theme:
        return head + "该股历史记录里没有题材归属，无法推演同题材补位。"
    if n <= 0:
        return (head + f"同题材「{theme}」今日尚无涨停票（上一交易日 {p} 家）"
                       f"—— 补位未发生，先观察，别提前埋伏。")
    delta = f"（上一交易日 {p} 家）" if p else ""
    return (head + f"同题材「{theme}」（含相邻行业）今日 {n} 家涨停{delta} —— "
                   f"{_verdict(n, p)}（家数均不含停牌股自己）。"
                   f"下面按「接棒概率」排的是今天已经在涨停的同题材票，"
                   f"最多列 {top_peers} 只：同行业排前，相邻题材会标出来。"
                   f"口径：只做自己能看懂的那一只、用试错仓；"
                   f"停牌股复牌前走势不确定，别赌复牌。")
