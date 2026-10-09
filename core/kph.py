# -*- coding: utf-8 -*-
"""开盘红数据源（扩展数据源）—— 历史回溯专用。

协议知识借鉴自开源项目 electkismet/AxData 的 provider 声明（Apache-2.0），
按其公开接口用标准库重写，不引入任何第三方依赖。

补的核心能力是**历史回溯**：
  · history_pool(date)    任意历史日的涨停池 —— 转成与本地归档完全同构的
    格式，用于冷启动 / 归档断裂时的自动回补（autobackfill）。东财池接口
    忽略 date 入参、只能攒当天，接上开盘红后「缺哪天补哪天」；
  · history_emotion(date) 任意历史日的涨跌停家数 / 成交额快照（备用口径）。

约定：失败静默返回空值 —— 回补是锦上添花，失败绝不能影响主扫描。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date as _date
from datetime import datetime, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import datasource as ds
from . import universe

HIST_URL = "https://apphis.kaipanhong.com/w1/api/index.php"

_BASE = {
    "PhoneOSNew": "1", "DeviceID": "1a609dd6-b2b8-3bf9-ac40-a77581551454",
    "VerSion": "6.0.6", "Token": "0", "UserID": "0", "Red": "1", "apiv": "w45",
}
_UA = "Dalvik/2.1.0"

_MIN_INTERVAL = 0.8          # 回补是低频动作，保持礼貌间隔
_last_req = 0.0
_failed: set[str] = set()    # 本会话已试过且无数据的日期，不再重试


def _int(v, d: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _f(v, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _gate() -> None:
    global _last_req
    gap = _MIN_INTERVAL - (time.time() - _last_req)
    if gap > 0:
        time.sleep(gap)
    _last_req = time.time()


def _post(params: dict, timeout: int = 12) -> dict:
    _gate()
    body = urlencode({**_BASE, **params}).encode("utf-8")
    req = Request(HIST_URL, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": _UA, "Accept": "application/json,text/plain,*/*",
    }, method="POST")
    with urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", errors="ignore")
    js = json.loads(raw)
    if not isinstance(js, dict):
        raise ValueError("kph: payload is not an object")
    return js


def _hms(v) -> int:
    """封板时间归一：epoch 秒 → HHMMSS。已是 HHMMSS 则原样返回。"""
    n = _int(v, 0)
    if n <= 0:
        return 0
    if n > 10 ** 9:                      # epoch 秒
        try:
            t = datetime.fromtimestamp(n)
            return t.hour * 10000 + t.minute * 100 + t.second
        except (OSError, OverflowError, ValueError):
            return 0
    return n if n <= 235959 else 0


def _to_pool_row(raw) -> dict | None:
    """开盘红历史涨停池行（位置数组）→ 与本地归档同构的行。

    行结构（实测 2026-09-30）：
      [0]代码 [1]名称 [6]封板时间(epoch) [8]封单额 [10]连板数 [11]题材串
      [13]成交额 [14]换手率 [15]流通市值 [16]题材名 [17]涨停原因全文
    """
    if not isinstance(raw, list) or len(raw) < 17:
        return None
    code = universe.normalize(str(raw[0] or "").strip())
    name = str(raw[1] or "").strip()
    if not code or not name:
        return None
    if "ST" in name.upper():               # 对齐东财口径：ST 不入池
        return None
    fbt = _hms(raw[6])
    return {
        "code": code, "name": name,
        "price": 0.0, "pct": 0.0,              # 历史列表口径无价量，占位
        "amount": _f(raw[13]), "float_mv": _f(raw[15]),
        "turnover": _f(raw[14]), "fund": _f(raw[8]),
        "first_time": fbt, "last_time": fbt, "break_count": 0,
        "boards": max(_int(raw[10], 1), 1),
        "sector": str(raw[16] or "").strip() or "未分类",
        "reason": str(raw[17] or "").strip()[:160] if len(raw) > 17 else "",
        "stat": {"source": "kph"},
    }


def history_pool(date: str) -> list[dict]:
    """任意历史日（yyyyMMdd）的涨停池，格式与本地归档同构。

    用「涨停复盘」接口（含一字板与 ST —— 实测其余接口会漏一字板：
    HisDaBanList 47 只 vs 复盘 56 只，差的 5 只全是一字板）。
    单页 st=1000 实测足够全量（56 行）；失败/非交易日返回 []。
    """
    dash = f"{date[:4]}-{date[4:6]}-{date[6:8]}" if "-" not in date else date
    try:
        js = _post({"a": "GetPlateInfo_w38", "c": "HisLimitResumption",
                    "st": "1000", "Index": "0", "Date": dash})
        groups = js.get("list") or []
        out: list[dict] = []
        seen: set[str] = set()
        for pl in groups:
            if not isinstance(pl, dict):
                continue
            for raw in pl.get("StockList") or []:
                row = _to_pool_row(raw)
                if row and row["code"] not in seen:
                    seen.add(row["code"])
                    out.append(row)
        return out
    except Exception as e:
        print(f"[kph] 历史涨停池抓取失败({date}): {e}", file=sys.stderr)
        return []


def history_emotion(date: str) -> dict:
    """任意历史日的情绪快照（涨停/跌停/涨跌家数/成交额）。失败返回 {}。"""
    dash = f"{date[:4]}-{date[4:6]}-{date[6:8]}" if "-" not in date else date
    try:
        js = _post({"a": "HisZhangFuDetail", "c": "HisHomeDingPan", "Day": dash})
        info = js.get("info") if isinstance(js.get("info"), dict) else {}
        return {
            "date": date,
            "limit_up": _int(info.get("ZT")), "limit_down": _int(info.get("DT")),
            "real_limit_up": _int(info.get("SJZT")),
            "rise": _int(info.get("SZJS")), "fall": _int(info.get("XDJS")),
        }
    except Exception as e:
        print(f"[kph] 历史情绪抓取失败({date}): {e}", file=sys.stderr)
        return {}


def _workdays_before(real_date: str, n: int) -> list[str]:
    """real_date 之前的 n 个工作日（周一~周五），新→旧。"""
    try:
        d = datetime.strptime(real_date, "%Y%m%d").date() - timedelta(days=1)
    except ValueError:
        return []
    out: list[str] = []
    guard = 0
    while len(out) < n and guard < n * 4:
        guard += 1
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return out


def autobackfill(real_date: str, lookback: int = 20, max_try: int = 8) -> dict:
    """把最近 N 个工作日内**缺失**的涨停池归档自动补上。

    只补缺（已有归档不覆盖）；拉不到数据的日期（假期等）进会话级失败表，
    不再重试。每次调用最多尝试 max_try 个日期 —— 首次扫描把缺的补完，
    之后零请求。lookback 默认 20 个工作日（约 4 周），让题材记忆的
    连续活跃统计有尽量长的数据底座。
    """
    if not real_date:
        return {"checked": 0, "filled": []}
    have = set(ds.pool_history_dates())
    cands = [d for d in _workdays_before(real_date, lookback)
             if d not in have and d not in _failed]
    filled: list[str] = []
    for d in cands[:max_try]:
        pool = history_pool(d)
        if pool:
            ds.save_pool_history(d, pool)
            filled.append(d)
        else:
            _failed.add(d)
    return {"checked": min(len(cands), max_try), "filled": filled}
