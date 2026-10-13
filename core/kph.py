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
import os
import sys
import time
from datetime import date as _date
from datetime import datetime, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import datasource as ds
from . import universe

HIST_URL = "https://apphis.kaipanhong.com/w1/api/index.php"      # 历史
RT_URL = "https://apphwshhq.kaipanhong.com/w1/api/index.php"     # 实时

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


def _post(params: dict, timeout: int = 12, url: str = HIST_URL) -> dict:
    _gate()
    body = urlencode({**_BASE, **params}).encode("utf-8")
    req = Request(url, data=body, headers={
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


def market_emotion() -> dict:
    """当日实时市场情绪（涨跌家数 / 涨停跌停）。

    push2 被限流时，这是 E1「涨跌家数」的**备用源**（东财 ulist.np 的
    f104/f105/f106 拿不到时顶上）。平盘数 = 本地有效代码总数 - 涨 - 跌。
    失败返回 {}。
    """
    def producer():
        js = _post({"a": "ZhangFuDetail", "c": "HomeDingPan"}, url=RT_URL)
        info = js.get("info") if isinstance(js.get("info"), dict) else {}
        up = _int(info.get("SZJS"))
        down = _int(info.get("XDJS"))
        if up + down <= 0:
            return {}
        try:
            codes = ds.load_valid_codes() or []
            total = len(codes) if len(codes) > up + down else up + down
        except Exception:
            total = up + down
        flat = max(total - up - down, 0)
        return {"up": up, "down": down, "flat": flat, "total": up + down + flat,
                "limit_up": _int(info.get("ZT")), "limit_down": _int(info.get("DT")),
                "source": "kph"}
    try:
        return ds.cached("kph:emotion", 120, producer)
    except Exception as e:
        print(f"[kph] 实时情绪抓取失败: {e}", file=sys.stderr)
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


def autobackfill(real_date: str, lookback: int = 20, max_try: int = 30,
                 budget_sec: float = 45.0) -> dict:
    """把最近 N 个工作日内**缺失**的涨停池归档自动补上。

    只补缺（已有归档不覆盖）；拉不到数据的日期（假期等）进会话级失败表，
    不再重试。

    冷启动（新装 App / 换机）的教训（2026-10-10）：原来一次只试 8 个日期，
    而国庆这类长假占掉一大半工作日 → 首轮只补到 4 天 → 题材 streak 算不出来
    （手机端表现就是「主线没有记忆」）。现在：
      · max_try 提到 30（覆盖 20 个工作日 + 夹在中间的假期）；
      · 加 budget_sec 总时长预算（默认 45 秒）——超时就停，剩下的下一轮继续；
      · 返回 rest 字段告知调用方还剩多少没补（用于界面提示「补历史中 N/M」）。
    """
    if not real_date:
        return {"checked": 0, "filled": [], "rest": 0}
    have = set(ds.pool_history_dates())
    cands = [d for d in _workdays_before(real_date, lookback)
             if d not in have and d not in _failed]
    filled: list[str] = []
    t0 = time.time()
    tried = 0
    for d in cands[:max_try]:
        if time.time() - t0 > budget_sec:
            break
        tried += 1
        pool = history_pool(d)
        if pool:
            ds.save_pool_history(d, pool)
            filled.append(d)
        else:
            _failed.add(d)
    rest = len([d for d in cands[tried:] if d not in _failed])
    return {"checked": tried, "filled": filled, "rest": rest}


# 每天只对账一次（进程内）。对账要发 3 个请求，不能每轮扫描都发。
_repaired: set[str] = set()


def _move_aside(d: str) -> bool:
    """把某日存档挪到 pool_history/_suspect/（不删除，可回溯）。

    子目录名长度不是 13，不会被 pool_history_dates() 当成交易日读进来。
    """
    src = os.path.join(ds.POOL_HIST_DIR, f"{d}.json")
    if not os.path.exists(src):
        return False
    try:
        dst_dir = os.path.join(ds.POOL_HIST_DIR, "_suspect")
        os.makedirs(dst_dir, exist_ok=True)
        os.replace(src, os.path.join(dst_dir, f"{d}.json"))
        return True
    except OSError:
        return False


def repair_pool_history(real_date: str, days: int = 3) -> dict:
    """用「涨停复盘」接口**强制重写**最近 N 个工作日的归档（修已经写坏的数据）。

    与 `autobackfill` 的分工：backfill 只补缺（已有就不动），因此
    **一旦写坏就永远修不回来** —— 实测 20261012 只存了 16 只（当日早扫的
    部分池），而那天真实涨停 46 只；`autobackfill` 因为"这日期已有"直接跳过，
    错值就一直留在档案里，并污染 M9 的"昨日涨停池/老龙档案"与 E6 的题材取值。

    三件事：
      ① 接口有数据 → **强制覆盖**本地（force=True 绕过"更完整才覆盖"的门槛，
         因为接口的收盘复盘才是权威口径）；
      ② 接口没数据、但本地有存档，且不是今天 → 该存档属于"错日期"
         （今天还没生成复盘是正常的，别的日子没有就是脏数据）→ 挪到 _suspect/；
      ③ 今天的存档若与上一交易日**完全同批** → 同样是错日期 → 挪走。

    每天最多跑一次（进程内去重），返回 {repaired, moved, missing}。
    """
    if not real_date or real_date in _repaired:
        return {"repaired": [], "moved": [], "missing": []}
    _repaired.add(real_date)
    repaired: list[str] = []
    moved: list[str] = []
    missing: list[str] = []
    counts: dict[str, int] = {}
    targets = [real_date] + _workdays_before(real_date, max(0, int(days) - 1))
    for d in targets:
        try:
            pool = history_pool(d)
        except Exception:                                    # noqa: BLE001
            continue
        if pool:
            before = len(ds.load_pool_history(d))
            if ds.save_pool_history(d, pool, force=True) and before != len(pool):
                repaired.append(d)
            counts[d] = len(pool)
        else:
            missing.append(d)
            if d != real_date and ds.load_pool_history(d) and _move_aside(d):
                moved.append(d)
    # 涨停家数曲线同步校正：归档条数就是那天的收盘涨停家数（复盘口径）。
    # 10-12 被首扫写成了 16 家，而它不会再作为 end 传进来 —— 只能在这里修。
    for d, n in counts.items():
        try:
            ds.fix_limit_up_count(d, n)
        except (OSError, ValueError):
            pass
    # 今天：本地存档若与上一交易日完全同批 → 错日期，挪走
    try:
        if ds.pool_history_duplicates(real_date,
                                      ds.load_pool_history(real_date)) and _move_aside(real_date):
            moved.append(real_date)
    except OSError:
        pass
    return {"repaired": repaired, "moved": moved, "missing": missing}
