# -*- coding: utf-8 -*-
"""MCP 服务端（E12）—— 把本系统的分析结果暴露给 AI 助手。

-------------------------------------------------------------- 为什么做这个
本项目的分析结果一直只能靠人打开网页看。想让它进 AI 助手的对话
（"今天能不能出手""这只票什么时候卖"），就得有个标准接口 —— MCP
（Model Context Protocol）就是干这个的。

★ 只读：这里**只暴露读操作**。不做下单、不改持仓、不写策略配置。
  理由：MCP 的客户端是 AI，让它具备写权限，一次幻觉就能改掉你的真实持仓。

-------------------------------------------------------------- 传输方式
纯标准库实现的 stdio JSON-RPC 2.0 —— 不依赖 mcp 官方 SDK。
理由（实测）：本机环境没有 `mcp` 包，而装一个 SDK 只为跑一个 server
不值当；MCP 的 stdio 传输本身就是「一行一条 JSON-RPC」，协议面很小。
好处：Chaquopy（安卓）与桌面运行时都能跑，与 core/ 的其余模块一致。

用法（在 AI 助手的 MCP 配置里）：
    {
      "mcpServers": {
        "yeren-stock": {
          "command": "python",
          "args": ["C:/Users/macro/Desktop/股票源码/mcp_server.py"]
        }
      }
    }

手工调试（管道喂 JSON-RPC）：
    echo {"jsonrpc":"2.0","id":1,"method":"tools/list"} | python mcp_server.py

-------------------------------------------------------------- 数据从哪来
优先用**最近一次落盘的报告**（data/last_report.json）—— 秒回，不打网络。
需要实时的工具（如单只票详情）会自己取快照，并如实标注数据时间。
★ 不主动触发全市场扫描：那要 10 秒以上，AI 助手会以为服务卡死了。
"""
from __future__ import annotations

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "yeren-stock"
SERVER_VERSION = "1.0.0"


# ── 工具定义 ─────────────────────────────────────────────────────
# 每个工具的 description 是写给 AI 看的 —— 决定它什么时候会调用。
# 所以写「什么时候用」，不只写「返回什么」。
TOOLS = [
    {
        "name": "get_daily_brief",
        "description": (
            "获取今日盘面总览：今天能不能出手、目标仓位、情绪阶段、"
            "两市成交额、涨停跌停家数、题材主线、以及一句话结论。"
            "★ 当用户问「今天能不能买 / 今天什么情况 / 盘面怎么样」时用这个。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_candidates",
        "description": (
            "列出本轮选出的候选股票（含评分、买点、止损位、所属题材）。"
            "★ 当用户问「今天买什么 / 有哪些票入选」时用这个。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer",
                          "description": "最多返回几只，默认 10"},
            },
            "required": [],
        },
    },
    {
        "name": "get_stock",
        "description": (
            "查单只股票的完整分析：现价、涨跌、评分、买卖点、波段阶段、"
            "筹码、主导资金、涨停基因（历史股性）。"
            "★ 当用户问「某某股票怎么样 / 现在能不能买 / 什么时候卖」时用这个。"
            "code 可写 sh600519 / 600519 / 贵州茅台。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string",
                         "description": "股票代码或名称，如 sh600519 / 600519 / 贵州茅台"},
                "light": {"type": "boolean",
                          "description": ("true = 只返回结论，不带筹码/K线等明细"
                                          "（更快、更省 token）。默认 false")},
            },
            "required": ["code"],
        },
    },
    {
        "name": "get_portfolio",
        "description": (
            "查持仓体检：每只持仓的盈亏、卖出信号、行动建议（清仓/减仓/持有），"
            "以及自选股列表。"
            "★ 当用户问「我的持仓怎么样 / 该卖了吗」时用这个。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_market_heat",
        "description": (
            "查市场情绪温度计：温度读数、连板梯队分布、晋级率、"
            "封板率、亏钱效应，以及最近的历史温度曲线。"
            "★ 当用户问「市场情绪怎么样 / 冷不冷 / 高度多少」时用这个。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_auction",
        "description": (
            "查集合竞价早读：全市场高开/低开分布、竞价涨停家数、"
            "昨日涨停股今天竞价表现。"
            "★ 当用户问「竞价怎么样 / 今天高开的多吗」时用这个。"
            "仅在 09:15-09:25 前后有数据；非竞价时段返回的是上一轮快照。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_report_section",
        "description": (
            "按名字取报告里的原始区块，用于上面几个工具没覆盖到的部分。"
            "可用区块：macro 宏观 / volume 量能 / mainline 题材主线 / "
            "sectors 板块 / rebound 低位承接 / substitute 停牌补位 / "
            "industry 逆势连板 / sowing 题材播种 / outlook 明日判断 / "
            "flow 资金流 / journal 信号胜率 / backtest 组合回测 / "
            "discipline 纪律体检 / gene 涨停基因 / limit_up_pool 涨停梯队 / "
            "position 仓位计划。"
            "★ 当用户问到某个具体模块时用这个，不要用它取整份报告。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "section": {"type": "string", "description": "区块名，见上"},
                "max_chars": {"type": "integer",
                              "description": "返回内容上限（字符），默认 6000"},
            },
            "required": ["section"],
        },
    },
    {
        "name": "search_stock",
        "description": (
            "按名称或拼音搜索股票代码。"
            "★ 当用户只说了股票名（如「茅台」「宁德时代」）而你需要代码时先用这个。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string",
                            "description": "名称、拼音首字母或部分代码"},
                "limit": {"type": "integer", "description": "默认 10"},
            },
            "required": ["keyword"],
        },
    },
]

SECTION_ALIASES = {
    "macro": "macro", "宏观": "macro",
    "volume": "volume", "量能": "volume",
    "mainline": "mainline", "题材": "mainline", "题材主线": "mainline",
    "sectors": "sectors", "板块": "sectors",
    "rebound": "rebound", "低位承接": "rebound",
    "substitute": "substitute", "停牌补位": "substitute",
    "industry": "industry", "逆势连板": "industry",
    "sowing": "sowing", "题材播种": "sowing",
    "outlook": "outlook", "明日判断": "outlook",
    "flow": "flow", "资金流": "flow",
    "journal": "journal", "信号胜率": "journal",
    "backtest": "backtest", "回测": "backtest", "组合回测": "backtest",
    "discipline": "discipline", "纪律": "discipline",
    "gene": "gene", "涨停基因": "gene", "股性": "gene",
    "limit_up_pool": "limit_up_pool", "涨停梯队": "limit_up_pool",
    "position": "position", "仓位": "position", "仓位计划": "position",
    "sentiment": "sentiment", "情绪": "sentiment",
    "meter": "meter", "温度计": "meter",
    "auction": "auction", "竞价": "auction",
    "portfolio": "portfolio", "持仓": "portfolio",
    "picks": "picks", "候选": "picks",
}


# ── 报告读取 ─────────────────────────────────────────────────────
_REPORT_CACHE: dict = {}


def _report(refresh: bool = False) -> dict:
    """取最近一次报告。默认用进程内缓存 —— 一次会话里读多次不必反复读盘。"""
    if not refresh and _REPORT_CACHE.get("r"):
        return _REPORT_CACHE["r"]
    try:
        from core import pipeline
        r = pipeline.get_disk_report()
        if r:
            _REPORT_CACHE["r"] = r
            return r
    except Exception:
        pass
    # 落盘报告不可用（首次运行/文件损坏）时退回内存缓存
    try:
        from core import pipeline
        r = pipeline.get_cached()
        if r:
            _REPORT_CACHE["r"] = r
            return r
    except Exception:
        pass
    return {}


def _stale_note(r: dict) -> str:
    """如实标注这份数据是什么时候的 —— 不能让 AI 把昨天的数据当今天用。"""
    m = (r or {}).get("meta") or {}
    if not m:
        return ("⚠ 暂时没有可用的分析报告（服务可能还没跑过一轮扫描）。"
                "请先在网页端点一次「重新扫描」，或直接说「先跑一轮」。")
    return (f"数据时间：{m.get('time', '?')}（交易日 {m.get('date', '?')}，"
            f"用时 {m.get('elapsed', '?')}s，扫描 {m.get('scanned', '?')} 只）")


def _norm_code(raw: str) -> str:
    from core import universe
    return universe.normalize((raw or "").strip())


def _is_code(s: str) -> bool:
    """判断归一结果是不是一个真的带前缀代码。

    ★ 不能直接 `if not code`：universe.normalize() 对识别不了的输入
      **原样返回**（normalize('贵州茅台') == '贵州茅台'），所以名字会被
      当成「有效结果」混过去（踩过：名字查询走到「代码格式不对」分支）。
    """
    s = (s or "").strip().lower()
    return len(s) >= 8 and s[:2] in ("sh", "sz", "bj") and s[2:8].isdigit()


def _text(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}]}


def _fmt_pct(v, digits: int = 2) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{f:+.{digits}f}%"


def _fmt_num(v, digits: int = 2) -> str:
    try:
        return f"{float(v):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _metrics(r: dict) -> dict:
    """涨停/跌停/炸板这些读数在 sentiment.metrics 里，不在 sentiment 顶楼。

    踩过：直接在 sentiment 上取 limit_up 拿到 None，页面就显示「涨停 ? / 跌停 ?」。
    meter.metrics 更全（多 broken_ratio / promote_rate / breadth），优先用它。
    """
    m = ((r or {}).get("meter") or {}).get("metrics") or {}
    s = ((r or {}).get("sentiment") or {}).get("metrics") or {}
    out = dict(s)
    out.update({k: v for k, v in m.items() if v is not None})
    return out


def _stage_cn(r: dict) -> str:
    """阶段中文名：报告里是 sentiment.label（如「退潮期 · 无条件清仓」）。"""
    sen = (r or {}).get("sentiment") or {}
    return sen.get("label") or sen.get("stage") or "?"


def _meter_level_cn(r: dict) -> str:
    """温度档位说明。meter 里只有 label 字段，不是 level_cn。"""
    m = (r or {}).get("meter") or {}
    return m.get("label") or m.get("stage") or ""


# ── 各工具实现 ───────────────────────────────────────────────────
def t_daily_brief(_args: dict) -> dict:
    r = _report()
    if not r:
        return _text(_stale_note(r))
    sen = r.get("sentiment") or {}
    vol = r.get("volume") or {}
    pos = r.get("position") or {}
    mm = {}  # 指标卡口径由前端拼；这里直接给结构化数字
    lines = [f"# 今日盘面总览（{r.get('meta', {}).get('date', '?')}）", ""]
    lines.append(_stale_note(r))
    lines.append("")

    can = sen.get("can_trade")
    stage = _stage_cn(r)
    verdict = ("能做" if can else "先看戏")
    lines.append(f"## 结论：{verdict}（阶段：{stage}）")
    if sen.get("note"):
        lines.append(f"理由：{sen['note']}")
    lines.append("")

    mt = _metrics(r)
    lines.append("## 关键数字")
    lines.append(f"- 目标总仓位：{_fmt_num((pos.get('target_total') or 0) * 100, 1)}%")
    amt = vol.get("amount")
    if amt:
        lines.append(f"- 两市成交额：{_fmt_num(float(amt) / 1e8, 0)} 亿")
    if mt.get("limit_up") is not None:
        lines.append(f"- 涨停 {mt.get('limit_up', '?')} / 跌停 {mt.get('limit_down', '?')}"
                     f" / 炸板 {mt.get('broken', '?')}")
    if mt.get("hurt_area") is not None:
        lines.append(f"- 亏钱面：{_fmt_num(float(mt['hurt_area']) * 100, 1)}%")
    if mt.get("max_boards"):
        lines.append(f"- 最高连板：{mt['max_boards']} 板")
    if mt.get("seal_rate") is not None:
        lines.append(f"- 封板率：{_fmt_num(float(mt['seal_rate']) * 100, 0)}%")
    if mt.get("promote_rate") is not None:
        lines.append(f"- 晋级率：{_fmt_num(float(mt['promote_rate']) * 100, 0)}%")

    ml = r.get("mainline") or {}
    if ml.get("summary"):
        lines.append("")
        lines.append(f"## 题材主线\n{ml['summary']}")

    picks = r.get("picks") or []
    lines.append("")
    lines.append(f"## 候选股票：{len(picks)} 只")
    if picks:
        lines.append(r.get("picks_note") or "")
    return _text("\n".join(lines))


def t_list_candidates(args: dict) -> dict:
    r = _report()
    picks = (r or {}).get("picks") or []
    if not picks:
        return _text(_stale_note(r) + "\n\n本轮没有选出候选股票（可能退潮/量能不足，"
                                     "也可能扫描还没跑）。")
    n = int(args.get("limit") or 10)
    lines = [f"# 候选股票（共 {len(picks)} 只，列出前 {min(n, len(picks))} 只）", "",
             _stale_note(r), ""]
    for i, p in enumerate(picks[:n], 1):
        sc = p.get("scores") or {}
        lines.append(f"## {i}. {p.get('name')}（{p.get('code')}） 评分 {p.get('score')}")
        meta = [x for x in [p.get("board"), p.get("sector")] if x]
        if meta:
            lines.append(f"- {' · '.join(meta)}")
        if p.get("price"):
            lines.append(f"- 现价 {_fmt_num(p['price'])}（{_fmt_pct(p.get('pct'))}）")
        # 买卖点说人话 —— 这是用户最需要的两个数字
        bs = p.get("buy_signals") or []
        ss = p.get("sell_signals") or []
        if bs:
            b = bs[0]
            lines.append(f"- 怎么买：{b.get('action') or b.get('trigger') or b.get('name')}")
        if ss:
            s0 = ss[0]
            lines.append(f"- 什么时候卖：{s0.get('action') or s0.get('trigger') or s0.get('name')}")
        if sc:
            parts = [f"{k} {v}" for k, v in sc.items() if v is not None]
            if parts:
                lines.append(f"- 分项：{' / '.join(parts)}")
        why = [x for x in (p.get("reasons") or [])][:4]
        if why:
            lines.append(f"- 入选理由：{'；'.join(str(x) for x in why)}")
        warn = p.get("warnings") or []
        if warn:
            lines.append(f"- ⚠ 注意：{'；'.join(str(x) for x in warn[:2])}")
        lines.append("")
    return _text("\n".join(lines))


def t_get_stock(args: dict) -> dict:
    raw = (args.get("code") or "")
    code = _norm_code(raw)
    if not _is_code(code):
        # ★ 名字也能直接查：AI 助手经常直接拿用户说的名字调过来。
        #   与其回一句「格式不对」，不如自己先搜一次（踩过：说「贵州茅台」被拒）。
        try:
            from core import datasource as ds
            hits = (ds.search_stocks(raw, limit=3) or {}).get("results") or []
        except Exception:                                     # noqa: BLE001
            hits = []
        if len(hits) == 1:
            code = hits[0]["code"]
        elif len(hits) > 1:
            lines = [f"「{raw}」匹配到多只，请指明是哪一个：", ""]
            for x in hits:
                lines.append(f"- {x.get('name')}（{x.get('code')}）")
            return _text("\n".join(lines))
        else:
            return _text(f"没能识别代码或名称：{raw!r}。"
                         f"可写 sh600519 / 600519 / 贵州茅台。")
    try:
        from core import pipeline
        res = pipeline.stock_detail(code)
    except Exception as e:                                    # noqa: BLE001
        return _text(f"取 {code} 详情失败：{type(e).__name__}: {e}")
    if not res.get("ok"):
        return _text(f"取不到 {code}：{res.get('error', '未知原因')}")
    p = res.get("stock") or {}
    light = bool(args.get("light"))

    lines = [f"# {p.get('name')}（{p.get('code')}）", ""]
    lines.append("（实时快照，不是缓存的报告）")
    lines.append("")
    lines.append(f"- 现价 {_fmt_num(p.get('price'))}（{_fmt_pct(p.get('pct'))}）")
    if p.get("board") or p.get("sector"):
        lines.append(f"- {' · '.join(x for x in [p.get('board'), p.get('sector')] if x)}")
    if p.get("zt_boards"):
        lines.append(f"- 今日 {p['zt_boards']} 连板")
    if p.get("score") is not None:
        lines.append(f"- 综合评分 {p['score']}")

    # ★ 买点/卖点放最前，且说人话 —— 这是用户真正要的两件事
    ss = p.get("sell_signals") or []
    bs = p.get("buy_signals") or []
    if ss:
        lines.append("")
        lines.append("## 什么时候卖")
        for s in ss:
            act = s.get("action") or s.get("trigger") or ""
            lines.append(f"- 【{s.get('name', '')}】{act}")
            if s.get("trigger") and s.get("trigger") != act:
                lines.append(f"  触发条件：{s['trigger']}")
    if bs:
        lines.append("")
        lines.append("## 什么时候买")
        for b in bs:
            act = b.get("action") or b.get("trigger") or ""
            lines.append(f"- 【{b.get('name', '')}】{act}")

    sw = p.get("swing") or {}
    if sw.get("ok"):
        lines.append("")
        lines.append(f"## 波段体系：{sw.get('stage_cn', '?')}")
        if sw.get("verdict"):
            v = sw["verdict"]
            lines.append(f"- 能不能做：{v.get('can_cn') or v.get('can', '?')}"
                         f"（{v.get('why', '')}）")
        if sw.get("entry"):
            e = sw["entry"]
            lines.append(f"- 入场参考：{e.get('name', '')}"
                         f"（{e.get('desc') or e.get('note') or ''}）")

    pb = sw.get("playbook") if isinstance(sw, dict) else None
    if pb and pb.get("label"):
        lines.append(f"- 打法：{pb['label']} —— {pb.get('reason', '')}")

    if p.get("zt_reason"):
        lines.append("")
        lines.append(f"## 今日涨停原因\n{p['zt_reason']}")

    if not light:
        fs = p.get("fund_style") or {}
        if fs.get("label"):
            lines.append("")
            lines.append(f"## 主导资金：{fs['label']}")
            if fs.get("hold_note"):
                lines.append(f"- {fs['hold_note']}")
        cg = p.get("chip") or p.get("chips") or {}
        if cg.get("profit_ratio") is not None:
            lines.append("")
            lines.append("## 筹码（估算）")
            lines.append(f"- 获利盘 {_fmt_num(float(cg['profit_ratio']) * 100, 1)}%"
                         f"，平均成本 {_fmt_num(cg.get('avg_cost'))}")
        reasons = p.get("reasons") or []
        if reasons:
            lines.append("")
            lines.append("## 为什么选它")
            for x in reasons[:8]:
                lines.append(f"- {x}")
        warns = p.get("warnings") or []
        if warns:
            lines.append("")
            lines.append("## 风险提示")
            for x in warns[:6]:
                lines.append(f"- ⚠ {x}")
    return _text("\n".join(lines))


def t_get_portfolio(_args: dict) -> dict:
    r = _report()
    port = (r or {}).get("portfolio") or {}
    hold = port.get("holdings") or []
    watch = port.get("watchlist") or []
    if not hold and not watch:
        return _text("现在还没有持仓和自选。可以在网页端个股详情里点「＋记持仓」。")
    lines = ["# 持仓体检", "", _stale_note(r), ""]
    if port.get("total_mv"):
        lines.append(f"- 总市值 {_fmt_num(float(port['total_mv']) / 1e4, 1)} 万，"
                     f"总盈亏 {_fmt_num(float(port.get('total_pnl') or 0) / 1e4, 2)} 万"
                     f"（{_fmt_pct(port.get('total_pnl_pct'))}）")
        lines.append("")
    if hold:
        lines.append(f"## 持仓 {len(hold)} 只")
        for h in hold:
            lines.append(f"### {h.get('name')}（{h.get('code')}）"
                         f" —— {h.get('verdict', '?')}")
            if h.get("cost"):
                lines.append(f"- 成本 {_fmt_num(h['cost'])}，"
                             f"现价 {_fmt_num(h.get('price'))}，"
                             f"盈亏 {_fmt_pct(h.get('pnl_pct'))}"
                             f"（{_fmt_num(float(h.get('pnl') or 0) / 1e4, 2)} 万）")
            else:
                lines.append(f"- ⚠ 这只票没记成本价，算不出盈亏 —— "
                             f"现价 {_fmt_num(h.get('price'))}")
            for s in (h.get("signals") or [])[:3]:
                lines.append(f"- 【{s.get('name', '')}】"
                             f"{s.get('action') or s.get('trigger') or ''}")
            if h.get("unwind"):
                lines.append(f"- 解套参考：{h['unwind']}")
            if h.get("swing_alert"):
                lines.append(f"- ⚠ {h['swing_alert']}")
            lines.append("")
    if watch:
        lines.append(f"## 自选 {len(watch)} 只")
        for w in watch:
            lines.append(f"- {w.get('name')}（{w.get('code')}）")
    return _text("\n".join(lines))


def t_get_market_heat(_args: dict) -> dict:
    r = _report()
    if not r:
        return _text(_stale_note(r))
    sen = r.get("sentiment") or {}
    meter = r.get("meter") or {}
    mt = _metrics(r)
    lines = ["# 市场情绪", "", _stale_note(r), ""]
    if meter.get("temperature") is not None:
        lv = _meter_level_cn(r)
        lines.append(f"## 温度 {meter['temperature']}°" + (f"（{lv}）" if lv else ""))
        if meter.get("advice"):
            lines.append(f"**{meter['advice']}**")
        if meter.get("note"):
            lines.append(meter["note"])
    lines.append("")
    lines.append("## 情绪读数")
    for k, label in [("limit_up", "涨停"), ("limit_down", "跌停"),
                     ("broken", "炸板")]:
        if mt.get(k) is not None:
            lines.append(f"- {label}：{mt[k]}")
    if mt.get("seal_rate") is not None:
        lines.append(f"- 封板率：{_fmt_num(float(mt['seal_rate']) * 100, 0)}%")
    if mt.get("broken_ratio") is not None:
        lines.append(f"- 炸板率：{_fmt_num(float(mt['broken_ratio']) * 100, 0)}%")
    if mt.get("promote_rate") is not None:
        lines.append(f"- 晋级率：{_fmt_num(float(mt['promote_rate']) * 100, 0)}%")
    if mt.get("max_boards"):
        lines.append(f"- 最高连板：{mt['max_boards']} 板")
    if mt.get("one_word") is not None:
        lines.append(f"- 一字板：{mt['one_word']} 只")
    if mt.get("money_effect") is not None:
        lines.append(f"- 赚钱效应：{_fmt_pct(mt['money_effect'])}")
    if mt.get("height_premium") is not None:
        lines.append(f"- 高度溢价：{_fmt_pct(mt['height_premium'])}")
    if mt.get("hurt_area") is not None:
        lines.append(f"- 亏钱面：{_fmt_num(float(mt['hurt_area']) * 100, 1)}%")
    bd = mt.get("breadth") or {}
    if bd.get("up") is not None:
        lines.append(f"- 涨跌家数：{bd.get('up')} 涨 / {bd.get('down')} 跌"
                     f"（{bd.get('source', '?')}）")
    space = meter.get("space") or {}
    if space.get("note"):
        lines.append(f"- 空间：{space['note']}")
    zt = r.get("limit_up_pool") or []
    if zt:
        lines.append("")
        lines.append(f"## 连板梯队（涨停池 {len(zt)} 只，按板数）")
        for row in (meter.get("ladder") or []):
            names = "、".join(row.get("names") or [])
            lines.append(f"- {row.get('boards')} 板：{row.get('count')} 只"
                         + (f"（{names}）" if names else ""))
        if not meter.get("ladder"):
            from collections import Counter
            cnt = Counter(int(x.get("boards") or 1) for x in zt)
            for b in sorted(cnt, reverse=True):
                lines.append(f"- {b} 板：{cnt[b]} 只")
    hs = meter.get("heat_series") or []
    if hs:
        tail = hs[-10:]
        lines.append("")
        lines.append(f"## 最近温度曲线（共 {len(hs)} 个交易日）")
        lines.append(" / ".join(f"{str(x.get('date', ''))[-4:]}:{x.get('temperature', '?')}"
                                for x in tail))
    return _text("\n".join(lines))


def t_get_auction(_args: dict) -> dict:
    r = _report()
    au = (r or {}).get("auction") or {}
    if not au:
        return _text(_stale_note(r) + "\n\n这份报告里没有竞价数据。")
    if not au.get("enabled"):
        return _text("竞价早读模块当前是关闭的（可在「调策略」里打开 E9）。")
    lines = ["# 集合竞价早读", "", _stale_note(r), ""]
    lines.append(f"- 阶段：{au.get('phase_cn', '?')}"
                 f"（快照时间 {au.get('snapshot_time', '?')}）")
    if au.get("summary"):
        lines.append("")
        lines.append(au["summary"])
    temp = au.get("temperature") or {}
    bands = temp.get("bands") or []
    if bands:
        lines.append("")
        lines.append("## 高开/低开分布")
        for b in bands:
            lines.append(f"- {b.get('label', '')}：{b.get('n', 0)} 只")
    mv = au.get("movers") or {}
    for key, title in [("sealed", "竞价一字（封死涨停）"),
                       ("highs", "高开榜"), ("lows", "低开榜")]:
        rows = mv.get(key) or []
        if rows:
            lines.append("")
            lines.append(f"## {title}")
            for x in rows[:10]:
                # 竞价用 price（匹配价），不是 match_price —— 踩过：写成 match_price 显示「—」
                px = x.get("price") or x.get("match_price")
                lines.append(f"- {x.get('name', '')}（{x.get('code', '')}）"
                             f" {_fmt_pct(x.get('gap'))}"
                             f" 匹配价 {_fmt_num(px)}")
    rev = au.get("review") or {}
    rows = rev.get("rows") if isinstance(rev, dict) else rev
    # ★ review 是 {"n": 67, "rows": [...]}，不是裸列表 —— 别直接切片（踩过 KeyError: slice）
    if rows:
        lines.append("")
        lines.append(f"## 昨日涨停股今天竞价表现（共 {rev.get('n', len(rows))} 只）")
        for x in rows[:12]:
            lines.append(f"- {x.get('name', '')}（{x.get('code', '')}）"
                         f" {_fmt_pct(x.get('gap'))} —— {x.get('state_cn', '')}"
                         + (f"；{x.get('why')}" if x.get("why") else ""))
    if au.get("note"):
        lines.append("")
        lines.append(f"（口径限制：{au['note']}）")
    return _text("\n".join(lines))


def t_get_report_section(args: dict) -> dict:
    key = (args.get("section") or "").strip()
    sec = SECTION_ALIASES.get(key) or SECTION_ALIASES.get(key.lower()) or key
    r = _report()
    if not r:
        return _text(_stale_note(r))
    if sec not in r:
        avail = ", ".join(sorted(r.keys()))
        return _text(f"报告里没有「{key}」这一块。可用区块：{avail}")
    raw = json.dumps(r[sec], ensure_ascii=False, indent=1)
    cap = int(args.get("max_chars") or 6000)
    if len(raw) > cap:
        raw = (raw[:cap] +
               f"\n\n…（内容过长已截断，原长 {len(raw)} 字符。"
               f"需要更多可调大 max_chars）")
    return _text(f"# {sec}\n\n{raw}")


def t_search_stock(args: dict) -> dict:
    kw = (args.get("keyword") or "").strip()
    if not kw:
        return _text("请给一个关键词，如「茅台」「gzmt」「600519」。")
    n = int(args.get("limit") or 10)
    try:
        from core import datasource as ds
        # ★ 函数在 core/datasource.py 里叫 search_stocks（不是 core.stock_search，
        #   那个模块不存在 —— 踩过：ImportError 被 except 吞掉，退化成只回显关键词）
        res = ds.search_stocks(kw, limit=n)
    except Exception as e:                                   # noqa: BLE001
        c = _norm_code(kw)
        if c:
            return _text(f"{kw} → {c}（在线搜索不可用："
                         f"{type(e).__name__}，这是按代码归一的结果）")
        return _text(f"搜不了：{type(e).__name__}: {e}")
    rows = (res or {}).get("results") or []
    if not rows:
        return _text(f"没找到匹配「{kw}」的股票"
                     + (f"（来源：{res.get('source')}）" if res.get("source") else ""))
    lines = [f"# 「{kw}」的搜索结果（{len(rows)} 条，来源 {res.get('source', '?')}）", ""]
    for x in rows[:n]:
        extra = " · ".join(v for v in [x.get("board"), x.get("pinyin")] if v)
        lines.append(f"- {x.get('name', '')}（{x.get('code', '')}）"
                     + (f" —— {extra}" if extra else ""))
    lines.append("")
    lines.append("★ 拿到代码后可用 get_stock 查详情。")
    return _text("\n".join(lines))


HANDLERS = {
    "get_daily_brief": t_daily_brief,
    "list_candidates": t_list_candidates,
    "get_stock": t_get_stock,
    "get_portfolio": t_get_portfolio,
    "get_market_heat": t_get_market_heat,
    "get_auction": t_get_auction,
    "get_report_section": t_get_report_section,
    "search_stock": t_search_stock,
}


# ── JSON-RPC 协议层 ──────────────────────────────────────────────
def _result(mid, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": payload}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle(msg: dict) -> dict | None:
    """一条请求 → 一条响应。通知（无 id）返回 None。"""
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return _result(mid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        fn = HANDLERS.get(name)
        if not fn:
            return _error(mid, -32602, f"未知工具：{name}")
        try:
            out = fn(params.get("arguments") or {})
        except Exception as e:                                # noqa: BLE001
            # ★ 工具内部异常不返回 JSON-RPC error，而是当「内容」返回 ——
            #   返回 error 会让部分客户端直接断连；把错误当文本给 AI，
            #   它能自己决定要不要重试或换个工具。
            return _result(mid, _text(
                f"工具 {name} 执行失败：{type(e).__name__}: {e}\n\n"
                + traceback.format_exc()[-700:]))
        # 工具级错误标记（不影响协议）
        return _result(mid, out)
    if method in ("resources/list", "prompts/list"):
        # 声明了 tools 能力但没声明 resources/prompts —— 按协议可返回空
        return _result(mid, {"resources": []} if "resources" in method
                       else {"prompts": []})
    return _error(mid, -32601, f"不支持的方法：{method}")


def main() -> int:
    # stdio 传输：一行一条 JSON-RPC。用 utf-8 且不缓冲 —— Windows 控制台
    # 默认 GBK，中文内容会直接抛 UnicodeEncodeError（本项目踩过多次）。
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            # 解析不了就没法知道 id，只能按 null 回
            sys.stdout.write(json.dumps(
                _error(None, -32700, "JSON 解析失败")) + "\n")
            sys.stdout.flush()
            continue
        try:
            resp = handle(msg)
        except Exception as e:                                # noqa: BLE001
            resp = _error(msg.get("id"), -32603,
                          f"内部错误：{type(e).__name__}: {e}")
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
