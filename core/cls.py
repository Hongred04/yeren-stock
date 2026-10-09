# -*- coding: utf-8 -*-
"""财联社数据源（扩展数据源）。

协议知识借鉴自开源项目 electkismet/AxData（Apache-2.0，仅作接口研究参考），
按其公开接口声明用标准库重写 —— 不引入任何第三方依赖，也不打包它的框架。

补四块数据（全部只进结论层，界面上不摆原始表格）：
  1. telegraph()         电报快讯（重要分类）—— 并入 S1 消息定级源，
                         政策类消息比新浪/央视更快、更贴盘面；
  2. limit_up_reasons()  涨停池含原因 —— 每只涨停股的「题材|解读」，
                         挂到个股详情；顺带可做题材聚类的交叉参考；
  3. market_emotion()    市场情绪快照 —— 封板率 / 触及涨停 / 昨日涨停今日
                         表现（打板溢价），给 E1 温度计做独立口径兜底；
  4. sector_heat()       板块热度榜（含排名变化）—— 进板块强弱表的
                         「热度」维度：排名飙升=资金正在进场的方向。

约定：所有函数失败返回空值（{} / []），静默不抛 —— 数据源是锦上添花，
任何一路挂掉都不能影响主扫描链路。请求走 datasource 的连接池与内存缓存。

请求协议（2026-10-09 实测可用）：
  · 签名 = md5(sha1("k1=v1&k2=v2…"（按键名排序拼接)))；
  · 部分接口参数固定，直接带现成 sign；
  · 电报接口带 category 时需重算签名。
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

from . import datasource as ds

# ---- 接口地址（实测自 AxData 的 provider 声明）----
EMOTION_URL = "https://x-quote.cls.cn/v2/quote/a/stock/emotion"
ZT_POOL_URL = "https://x-quote.cls.cn/quote/index/up_down_analysis"
HEAT_URL = "https://x-quote.cls.cn/v2/quote/a/plate/plate_heat_list"
TELEGRAPH_URL = "https://api3.cls.cn/v1/roll/get_roll_list"

# 固定签名的接口（参数不随请求变化）
_EMOTION_PARAMS = {"app": "CailianpressWeb", "os": "web", "sv": "8.4.6",
                   "sign": "9f8797a1f4de66c2370f7a03990d2737"}
_ZT_POOL_PARAMS = {"app": "CailianpressWeb", "os": "web", "rever": "1",
                   "sv": "8.4.6", "type": "up_pool", "way": "last_px",
                   "sign": "a6ab28604a6dbe891cdbd7764799eda1"}

# 动态签名的接口用这套基础参数
_BASE_APP = {
    "app": "cailianpress", "sv": "8.7.4", "os": "android",
    "mb": "Xiaomi-2206123SC", "ov": "32", "channel": "8", "motif": "0",
    "net": "", "province_code": "3205", "token": "", "uid": "",
}

_UA_WEB = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_UA_MOBILE = "okhttp/4.9.0"

# 轻量闸门：财联社不是我们的主数据源，保持最小请求间隔即可（缓存兜底）
_MIN_INTERVAL = 0.6
_last_req = 0.0


def _sign(params: dict) -> str:
    text = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hashlib.md5(hashlib.sha1(text.encode()).hexdigest().encode()).hexdigest()


def _signed(**extra) -> dict:
    p = {**_BASE_APP, **extra}
    p["sign"] = _sign(p)
    return p


def _gate() -> None:
    global _last_req
    gap = _MIN_INTERVAL - (time.time() - _last_req)
    if gap > 0:
        time.sleep(gap)
    _last_req = time.time()


def _fetch_json(url: str, params: dict, mobile: bool = False) -> dict:
    """带签名的 GET → JSON。失败抛异常，由各函数自行兜底。

    财联社响应有两种成功格式：{"code": 200, "data": …} 与
    {"errno": 0, "data": …}，两种都认。
    """
    _gate()
    full = url + "?" + urlencode({k: v for k, v in params.items() if v is not None})
    raw = ds._get(full, timeout=10, referer="https://www.cls.cn/").decode(
        "utf-8", errors="ignore")
    js = json.loads(raw)
    if not isinstance(js, dict):
        raise ValueError("cls: payload is not an object")
    if js.get("code") not in (None, 200):
        raise ValueError(f"cls: code={js.get('code')}")
    if js.get("errno") not in (0, "0", None):
        raise ValueError(f"cls: errno={js.get('errno')}")
    return js


def _f(v, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


# ---------------------------------------------------------------- 电报快讯

def telegraph(limit: int = 50) -> list[dict]:
    """财联社电报「重要」分类快讯。转为 news 源格式 {time,text,tags,source}。

    重要电报是财经编辑人工筛选过的政策/宏观/产业大事 —— 比通达式快讯精度高。
    last_time 取"今天 23:59:59"，返回该截止时间之前的最新 N 条，天然覆盖
    近一两天。失败返回 []。

    实测坑（2026-10-09）：rn 参数存在服务端上限 —— 31/35/40/50 均正常，
    60 直接返回空列表（不报错，静默空）。这里保守封顶 50。

    抓两轮（今天末 / 昨天末各一轮再合并去重），覆盖近 2-3 天，
    对齐 S1 的「上上个交易日」定级窗口。
    """
    def producer():
        now = datetime.now()
        merged: dict[str, dict] = {}
        for back in (0, 1):
            d = now - timedelta(days=back)
            day_end = int(datetime(d.year, d.month, d.day, 23, 59, 59).timestamp())
            try:
                p = _signed(refresh_type="1", last_time=str(day_end), rn=str(min(limit, 50)))
                p.pop("sign", None)
                p["category"] = "red"          # 重要分类
                p["sign"] = _sign(p)
                js = _fetch_json(TELEGRAPH_URL, p, mobile=True)
            except Exception as e:
                print(f"[cls] 电报抓取失败(back={back}): {e}", file=sys.stderr)
                continue
            data = js.get("data") or {}
            for it in data.get("roll_data") or []:
                if not isinstance(it, dict):
                    continue
                title = (it.get("title") or "").strip()
                content = (it.get("content") or "").strip()
                text = title or content
                if title and content and title not in content:
                    text = f"{title}。{content[:120]}"
                if not text:
                    continue
                ctime = it.get("ctime")
                try:
                    tstr = datetime.fromtimestamp(int(ctime)).strftime("%Y-%m-%d %H:%M:%S")
                except (TypeError, ValueError, OSError):
                    continue
                merged.setdefault(text, {"time": tstr, "text": text,
                                         "tags": [], "source": "财联社电报"})
        return list(merged.values())
    try:
        return ds.cached("cls:telegraph", 420, producer)
    except Exception as e:
        print(f"[cls] 电报快讯抓取失败: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------- 涨停池含原因

def limit_up_reasons() -> dict[str, str]:
    """今日涨停池的涨停原因 {code: "题材|解读"}。

    实测（10-09）：72 只涨停全带原因，如 雷赛智能 →「机器人|公司在人形机器人
    领域定位是伺服控制产品与解决方案提供商…」。失败返回 {}。
    """
    def producer():
        js = _fetch_json(ZT_POOL_URL, dict(_ZT_POOL_PARAMS))
        rows = js.get("data") or []
        out = {}
        for it in rows:
            if not isinstance(it, dict):
                continue
            code = (it.get("secu_code") or "").strip().lower()
            reason = (it.get("up_reason") or "").strip()
            if code and reason:
                out[code] = reason
        return out
    try:
        return ds.cached("cls:zt_reasons", 180, producer)
    except Exception as e:
        print(f"[cls] 涨停池原因抓取失败: {e}", file=sys.stderr)
        return {}


# ---------------------------------------------------------------- 市场情绪

def market_emotion() -> dict:
    """市场情绪快照：封板率 / 触及涨停数 / 昨日涨停今日表现（打板溢价）。

    打板溢价是短线情绪最灵敏的单指标 —— 本地算法依赖归档+快照，
    冷启动时为空；财联社直接给现成数字，作为兜底口径。
    失败返回 {}。
    """
    def producer():
        js = _fetch_json(EMOTION_URL, dict(_EMOTION_PARAMS))
        d = js.get("data") or {}
        if not isinstance(d, dict):
            return {}
        perf_raw = str(d.get("performance") or "").strip()
        perf = None
        try:
            perf = float(perf_raw.replace("%", "").replace("+", ""))
        except ValueError:
            pass
        up_ratio_raw = str(d.get("up_ratio") or "").strip()
        seal_rate = None
        try:
            seal_rate = round(float(up_ratio_raw.replace("%", "")) / 100.0, 3)
        except ValueError:
            pass
        udd = d.get("up_down_dis") if isinstance(d.get("up_down_dis"), dict) else {}
        degree = d.get("market_degree")
        return {
            "market_degree": _f(degree) if degree not in (None, "") else None,
            "seal_rate": seal_rate,                    # 封板率 0-1
            "up_open": int(_f(d.get("up_open_num"), 0)) or None,   # 触及涨停（含炸板）
            "premium": perf,                           # 昨日涨停今日表现 %
            "premium_text": perf_raw,
            "rise": int(_f(udd.get("rise_num"), 0)) or None,
            "fall": int(_f(udd.get("fall_num"), 0)) or None,
        }
    try:
        return ds.cached("cls:emotion", 240, producer)
    except Exception as e:
        print(f"[cls] 市场情绪抓取失败: {e}", file=sys.stderr)
        return {}


# ---------------------------------------------------------------- 板块热度

def sector_heat() -> list[dict]:
    """板块热度排行 [{name, rank, heat, rank_change, is_new}]。

    rank_change 是核心：排名大幅上升（▲几十位）说明资金正在涌进这个方向 ——
    支线变主线的第一现场。失败返回 []。
    """
    def producer():
        js = _fetch_json(HEAT_URL, _signed(), mobile=True)
        rows = js.get("data") or []
        out = []
        for it in rows:
            if not isinstance(it, dict):
                continue
            name = (it.get("plate_name") or "").strip()
            if not name:
                continue
            out.append({
                "name": name,
                "rank": int(_f(it.get("rank"), 0)),
                "heat": round(_f(it.get("cur_heat"), 0.0), 1),
                "rank_change": int(_f(it.get("rank_change"), 0)),
                "is_new": bool(it.get("is_new")),
            })
        return out
    try:
        return ds.cached("cls:heat", 300, producer)
    except Exception as e:
        print(f"[cls] 板块热度抓取失败: {e}", file=sys.stderr)
        return []


# 东财行业名 → 财联社板块名的常见映射。
# 两套板块命名体系不同（「化学制品」vs「化工」），只收高置信度对应，
# 宁缺勿错 —— 板块名张冠李戴比没有更糟。
_HEAT_ALIASES = {
    "化学制品": ["化工"],
    "化学制药": ["医药", "创新药"],
    "种植业": ["农业种植", "农业"],
    "饮料乳品": ["食品饮料", "酿酒", "白酒"],
    "汽车零部": ["汽车零部件", "汽车"],
    "基础建设": ["基建"],
    "房屋建设": ["基建", "房地产概念"],
    "半导体": ["半导体芯片", "芯片"],
    "通信设备": ["通信", "光通信"],
    "煤炭开采": ["煤炭"],
    "贵金属": ["黄金"],
    "航天装备": ["军工"],
    "航空装备": ["军工"],
    "电网设备": ["智能电网"],
    "电力": ["电力", "智能电网"],
}


def match_heat(name: str, heats: list[dict] | None = None) -> dict | None:
    """在热度榜里匹配一个板块名。精确 → 别名表 → 唯一包含；否则返回 None。"""
    if not name:
        return None
    heats = heats if heats is not None else sector_heat()
    if not heats:
        return None
    by_name = {h["name"]: h for h in heats}
    if name in by_name:
        return by_name[name]
    for alias in _HEAT_ALIASES.get(name, []):
        if alias in by_name:
            return by_name[alias]
    cands = [h for h in heats if name in h["name"] or h["name"] in name]
    if len(cands) == 1:
        return cands[0]
    return None


def sector_rotation(days: int = 4) -> list[dict]:
    """近 N 日每日涨幅靠前板块（板块轮动视角）。

    返回 [{date, plates:[{name, pct, rank}]}]，旧→新。失败返回 []。
    """
    def producer():
        js = _fetch_json("https://x-quote.cls.cn/v2/quote/a/plate/rotation",
                         _signed(days=str(days)), mobile=True)
        rows = js.get("data") or []
        out = []
        for day in rows:
            if not isinstance(day, dict):
                continue
            plates = []
            for i, p in enumerate(day.get("plates") or [], start=1):
                if isinstance(p, dict) and p.get("plate_name"):
                    plates.append({"name": p.get("plate_name"),
                                   "pct": _f(p.get("change"), 0.0), "rank": i})
            out.append({"date": str(day.get("trade_date") or day.get("date") or ""),
                        "plates": plates})
        return out
    try:
        return ds.cached("cls:rotation", 600, producer)
    except Exception as e:
        print(f"[cls] 板块轮动抓取失败: {e}", file=sys.stderr)
        return []
