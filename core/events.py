# -*- coding: utf-8 -*-
"""财经事件时间轴 —— 解禁 / 新股 / 交割 / 议息，并入「明日判断」。

## 为什么要有这个（用户 10-15 提的建议 3）

「明日判断」原来只看**已经发生**的量能/情绪/题材/资金/板块 —— 全是回看的。
但有些明日风险是**写在日历上的、今天就已经确定**的：

  · 明天有只票大额解禁 → 抛压是计划内的，跟情绪好坏无关；
  · 明天是股指期货交割日 → 波动率天然放大，别把交割日的震荡当趋势；
  · 明天海外议息 → 隔夜跳空风险，尾盘不该满仓过夜；
  · 明天有新股上市 → 分流存量资金。

这些不是"推演"，是**已知事实**。把它们并进明日预案，用户才知道明天
除了看盘面，还有哪些是今天就能提前准备的。

## 数据来源（全部公开、无需 key）

  · 解禁：东财 datacenter RPT_LIFT_STAGE（FREE_DATE <= 未来 N 天）
  · 新股：东财 datacenter RPTA_APP_IPOAPPLY（APPLY_DATE / LISTING_DATE）
  · 交割：**本地规律计算**，每月第三个周五（沪深300/上证50/中证500 股指期货
          交割日与 ETF 期权到期日同为第三个周五）—— 不需要接口
  · 议息：FOMC 日程是**公开固定**的，本模块内嵌已公布的会议日期表。
          ⚠️ 表有覆盖年限：超出范围时**明确返回"未知"**，绝不猜 ——
          见 _FOMC_DATES 的说明。

每一项都是**尽力而为**：单个源失败只影响它自己，不影响整张卡
（_safe 包住每个取数，失败返回空、由调用方决定措辞）。
"""
from __future__ import annotations

import datetime as _dt
import json

from . import datasource as ds

# ---------------------------------------------------------------- 议息（FOMC）
# ⚠️ 这张表必须保持"覆盖已知年份"，**超出范围时返回未知而不是外推**。
#    美联储每年 8 次会议、日期提前一年公布，规律是"大约每 6-7 周一次"，
#    但**具体日期无法用公式可靠外推**（有时是周二周三、有时周三周四）。
#    猜错日期比不显示更糟 —— 用户会按错误日期提前减仓。
#    维护：每年 12 月前后美联储公布次年日程，届时把新的一年补进来。
#    若当前年份不在表内，is_stale() 返回 True，界面显示"日程待公布"。
_FOMC_DATES: dict[int, list[str]] = {
    2024: ["0130", "0131", "0319", "0320", "0430", "0501", "0611", "0612",
           "0730", "0731", "0917", "0918", "1106", "1107", "1217", "1218"],
    2025: ["0128", "0129", "0318", "0319", "0506", "0507", "0617", "0618",
           "0729", "0730", "0916", "0917", "1028", "1029", "1209", "1210"],
    2026: ["0127", "0128", "0317", "0318", "0428", "0429", "0616", "0617",
           "0728", "0729", "0915", "0916", "1027", "1028", "1208", "1209"],
    2027: ["0126", "0127", "0316", "0317", "0427", "0428", "0615", "0616",
           "0727", "0728", "0914", "0915", "1102", "1103", "1214", "1215"],
}
# 表覆盖到的最晚年份（新增年份时同步上调）
_FOMC_MAX_YEAR = max(_FOMC_DATES)

# 取未来多少天内的事件（明日判断只关心"明天"，但给到 7 天让用户能提前准备）
LOOKAHEAD_DAYS = 7


def fomc_schedule_stale(year: int) -> bool:
    """该年的 FOMC 日程是否**不在**已维护的表内（界面据此说"待公布"）。"""
    return int(year) not in _FOMC_DATES


def _safe(fn, default):
    """单个取数失败不拖垮整张卡（网络抖动/接口变更都只影响它自己）。"""
    try:
        return fn()
    except Exception:
        return default


def _third_friday(year: int, month: int) -> _dt.date:
    """某月第三个周五 —— 股指期货交割日 / ETF期权到期日。"""
    d = _dt.date(year, month, 1)
    # 第一个周五
    d += _dt.timedelta(days=(4 - d.weekday()) % 7)
    return d + _dt.timedelta(days=14)      # 第三个周五


def _expiry_between(start: _dt.date, days: int) -> list[str]:
    """[start, start+days] 内的股指期货交割日（YYYYMMDD）。"""
    out = []
    for i in range(days + 1):
        d = start + _dt.timedelta(days=i)
        if d == _third_friday(d.year, d.month):
            out.append(d.strftime("%Y%m%d"))
    return out


def _fomc_between(start: _dt.date, days: int) -> tuple[list[str], bool]:
    """[start, start+days] 内的 FOMC 决议日。返回 (日期列表, 日程是否已维护)。

    决议日 = 会议最后一天（表里每年 16 个日期，成对出现）。
    """
    merged = []
    dated = True
    for i in range(days + 1):
        d = start + _dt.timedelta(days=i)
        if fomc_schedule_stale(d.year):
            dated = False
            continue
        if any(d.strftime("%Y%m%d").endswith(md) for md in _FOMC_DATES[d.year]):
            merged.append(d.strftime("%Y%m%d"))
    return merged, dated


def _lift_between(start: _dt.date, days: int) -> list[dict]:
    """[start, start+days] 的限售解禁（按解禁市值降序）。

    LIFT_MARKET_CAP 单位是**万元**（已用 TOTAL_RATIO 反推核对过：实测
    莱宝高科 213.14/0.000259482006 = 82.1 亿，与真实总市值吻合）。
    """
    end = start + _dt.timedelta(days=days)
    to = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
          "reportName=RPT_LIFT_STAGE&columns=ALL&pageSize=500&pageNumber=1"
          "&sortColumns=FREE_DATE&sortTypes=1&source=WEB&client=WEB"
          "&filter=(FREE_DATE%3E%3D%27{s}%27)(FREE_DATE%3C%3D%27{e}%27)")
    url = to.format(s=start.strftime("%Y-%m-%d"), e=end.strftime("%Y-%m-%d"))
    raw = ds._get(url, timeout=15, referer="https://data.eastmoney.com/")
    obj = json.loads(raw.decode("utf-8", errors="ignore"))
    rows = ((obj.get("result") or {}).get("data") or [])
    out = []
    for r in rows:
        try:
            cap = float(r.get("LIFT_MARKET_CAP") or 0) / 10000.0   # 万元 -> 亿元
        except (TypeError, ValueError):
            cap = 0.0
        d = str(r.get("FREE_DATE") or "")[:10].replace("-", "")
        if len(d) != 8:
            continue
        out.append({"date": d, "code": str(r.get("SECURITY_CODE") or ""),
                    "name": str(r.get("SECURITY_NAME_ABBR") or ""),
                    "cap_yi": round(cap, 2),
                    "kind": str(r.get("FREE_SHARES_TYPE") or "")[:20]})
    out.sort(key=lambda x: -x["cap_yi"])
    return out


def _ipo_between(start: _dt.date, days: int) -> list[dict]:
    """[start, start+days] 的新股申购 / 上市。

    APPLY_DATE=申购日、LISTING_DATE=上市日，两个都收 —— 上市日分流资金，
    申购日抽血，对盘面的意义不同，界面分开说。
    """
    end = start + _dt.timedelta(days=days)
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           "reportName=RPTA_APP_IPOAPPLY&columns=ALL&pageSize=500&pageNumber=1"
           "&sortColumns=APPLY_DATE&sortTypes=1&source=WEB&client=WEB")
    raw = ds._get(url, timeout=15, referer="https://data.eastmoney.com/")
    obj = json.loads(raw.decode("utf-8", errors="ignore"))
    rows = ((obj.get("result") or {}).get("data") or [])
    out = []
    for r in rows:
        for fld, ev in (("LISTING_DATE", "上市"), ("APPLY_DATE", "申购")):
            d = str(r.get(fld) or "")[:10].replace("-", "")
            if len(d) != 8:
                continue
            try:
                dd = _dt.datetime.strptime(d, "%Y%m%d").date()
            except ValueError:
                continue
            if start <= dd <= end:
                out.append({"date": d, "code": str(r.get("SECURITY_CODE") or ""),
                            "name": str(r.get("SECURITY_NAME_ABBR") or ""),
                            "event": ev})
    out.sort(key=lambda x: x["date"])
    return out


def build(real_date: str, days: int = LOOKAHEAD_DAYS,
          cache_ttl: int = 3600) -> dict:
    """构建财经事件时间轴。real_date 是**今天**（YYYYMMDD），从明天开始看。

    返回：
      { "days": [ {date, mmdd, items:[{k,v,note}], tone} ... ],
        "next":  { ... 明天的那个 day，没有则为 {} },
        "notes": [ ... 需要如实说明的（如"议息日程待公布"） ],
        "source": "东财 datacenter / 本地规律" }

    带 1h 缓存：解禁/新股一天才变一次，但盘中有新股补充公告，所以不用 24h。
    """
    try:
        today = _dt.datetime.strptime(str(real_date), "%Y%m%d").date()
    except (ValueError, TypeError):
        today = _dt.date.today()
    start = today + _dt.timedelta(days=1)      # 从"明天"开始

    def producer():
        lifts = _safe(lambda: _lift_between(start, days), []) or []
        ipos = _safe(lambda: _ipo_between(start, days), []) or []
        exp = _expiry_between(start, days)
        fomc, fomc_dated = _fomc_between(start, days)

        notes: list[str] = []
        if not fomc_dated:
            notes.append(f"{start.year} 年美联储议息日程尚未维护（表覆盖到 "
                         f"{_FOMC_MAX_YEAR} 年），本年度的议息日**不做提示** —— "
                         f"宁可不说，也不猜错日期")

        # 按日归并
        by_date: dict[str, list[dict]] = {}

        def _add(d, item):
            by_date.setdefault(d, []).append(item)

        # 解禁：按日聚合成一条，列出最大的几只
        lift_by_date: dict[str, list[dict]] = {}
        for x in lifts:
            lift_by_date.setdefault(x["date"], []).append(x)
        for d, xs in lift_by_date.items():
            xs.sort(key=lambda v: -v["cap_yi"])
            total = sum(v["cap_yi"] for v in xs)
            big = [v for v in xs if v["cap_yi"] >= 10]     # 10 亿以上才算大额
            names = "、".join(f"{v['name']}({v['cap_yi']:.0f}亿)"
                             for v in (big or xs)[:3])
            note = (f"共 {len(xs)} 只、合计约 {total:.0f} 亿" +
                    (f"，大额：{names}" if big else f"，最大：{names}"))
            if big:
                note += "　※ 大额解禁是计划内抛压，别把它当「利空出尽」去接"
            # 权重按**合计规模**分档，而不是"几只大额" —— 实测反例：10-21 只有
            # 中船特气一只 610 亿（big 长度 1 → min(2,1)=1，被标成 info），
            # 但 610 亿单只就是全市场量级的抛压，必须升级成 risk。
            #   合计 >= 300 亿 → risk（全市场量级）
            #   合计 30~300 亿 → 1（提示但不升级 tone）
            #   合计 < 30 亿   → 0（列出来，但别制造噪音）
            if total >= 300:
                lw = 2
            elif total >= 30:
                lw = 1
            else:
                lw = 0
            _add(d, {"k": "解禁", "v": f"{len(xs)} 只 / {total:.0f} 亿",
                     "note": note, "weight": lw})

        for x in ipos:
            _add(x["date"], {"k": f"新股{x['event']}",
                             "v": x["name"] or x["code"],
                             "note": ("新股上市：分流存量资金，注意当日开板节奏"
                                      if x["event"] == "上市" else
                                      "新股申购：小幅抽血，影响通常有限"),
                             "weight": 1})

        for d in exp:
            _add(d, {"k": "期指交割", "v": "股指期货交割日",
                     "note": "交割日前后波动天然放大 —— 别把交割日的震荡当趋势，"
                             "尾盘尤其容易出现异动",
                     "weight": 2})

        for d in fomc:
            _add(d, {"k": "海外议息", "v": "美联储决议",
                     "note": "隔夜有议息结果 —— 满仓过夜等于赌方向，"
                             "尾盘按自己的持仓成本决定留多少",
                     "weight": 2})

        days_out = []
        for i in range(days):
            d = start + _dt.timedelta(days=i)
            if d.weekday() >= 5:            # 周末不排（事件极少落周末）
                continue
            ds_ = d.strftime("%Y%m%d")
            items = by_date.get(ds_) or []
            if not items:
                continue
            items.sort(key=lambda v: -int(v.get("weight") or 0))
            tone = "risk" if any(int(v.get("weight") or 0) >= 2 for v in items) else "info"
            days_out.append({"date": ds_, "mmdd": d.strftime("%m-%d"),
                             "items": items, "tone": tone})

        return {"days": days_out,
                "next": days_out[0] if days_out and days_out[0]["date"] ==
                        (start.strftime("%Y%m%d")) else {},
                "notes": notes,
                "source": "东财 datacenter（解禁/新股）· 交割日本地规律 · FOMC 内嵌日程"}

    return ds.cached(f"events:{real_date}:{days}", cache_ttl, producer) or {}


def brief(ev: dict, limit: int = 3) -> str:
    """把事件时间轴压成一句话，供明日判断/界面摘要用。没有事件返回 ""。"""
    if not ev:
        return ""
    ds_ = (ev.get("days") or [])[:limit]
    if not ds_:
        return ""
    bits = []
    for d in ds_:
        ks = "、".join(sorted({str(it.get("k") or "") for it in (d.get("items") or [])}))
        if ks:
            bits.append(f"{d.get('mmdd')} {ks}")
    return "；".join(bits)
