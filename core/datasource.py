# -*- coding: utf-8 -*-
"""数据引擎：行情快照 / 日K线 / 情绪池（涨停、跌停、炸板）。

数据源（沙箱内实测可用）：
  1. 腾讯 qt.gtimg.cn     —— 批量实时快照（60只/请求，0.5s）
  2. 腾讯 web.ifzq.gtimg.cn —— 前复权日K线（单只/请求）
  3. 东财 push2ex         —— 涨停池 / 跌停池 / 炸板池
  4. 腾讯 qt.gtimg.cn     —— 指数快照（量能测算）
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_cache: dict = {}
_cache_ts: dict = {}
_last_td: tuple[str, float] | None = None
_pool_qdate: dict = {}


def _get(url: str, timeout: int = 12, referer: str = "") -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": referer or "https://gu.qq.com/",
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def cached(key: str, ttl: int, producer):
    """简易内存缓存。ttl<=0 表示强制刷新。"""
    now = time.time()
    if ttl > 0 and key in _cache and now - _cache_ts.get(key, 0) < ttl:
        return _cache[key]
    val = producer()
    _cache[key] = val
    _cache_ts[key] = now
    return val


# ---------------------------------------------------------------- 快照

# 腾讯 v_ 字段索引（已实测校准）
#  1 名称 2 代码 3 现价 4 昨收 5 今开 6 成交量(手) 7 外盘 8 内盘
#  9..18 买卖五档  30 时间 31 涨跌额 32 涨跌幅% 33 最高 34 最低
#  36 成交量(手) 37 成交额(万) 38 换手率 39 市盈率TTM
#  43 振幅 44 流通市值(亿) 45 总市值(亿) 46 市净率
#  47 涨停价 48 跌停价
SNAP_FIELDS = {
    "name": 1, "code": 2, "price": 3, "prev_close": 4, "open": 5,
    "volume": 6, "time": 30, "change": 31, "pct": 32,
    "high": 33, "low": 34, "amount": 37, "turnover": 38, "pe": 39,
    "amplitude": 43, "float_mv": 44, "total_mv": 45, "pb": 46,
    "limit_up": 47, "limit_down": 48,
}


def _parse_tx_line(line: str) -> dict | None:
    line = line.strip()
    if not line.startswith("v_") or "=" not in line:
        return None
    try:
        head, payload = line.split("=", 1)
    except ValueError:
        return None
    code = head[2:].strip()
    parts = payload.strip().strip(";").strip('"').split("~")
    if len(parts) < 50:
        return None
    name = parts[SNAP_FIELDS["name"]].strip()
    if not name:
        return None

    def f(key, default=0.0):
        try:
            return float(parts[SNAP_FIELDS[key]])
        except (ValueError, IndexError):
            return default

    def s(key, default=""):
        try:
            return parts[SNAP_FIELDS[key]].strip()
        except (IndexError,):
            return default

    price = f("price")
    if price <= 0:
        return None

    return {
        "code": code,
        "name": name,
        "price": price,
        "prev_close": f("prev_close"),
        "open": f("open"),
        "pct": f("pct"),
        "change": f("change"),
        "high": f("high"),
        "low": f("low"),
        "volume": f("volume"),          # 手
        "amount": f("amount") * 1e4,    # 成交额 -> 元
        "turnover": f("turnover"),      # 换手率 %
        "pe": f("pe"),
        "pb": f("pb"),
        "amplitude": f("amplitude"),
        "float_mv": f("float_mv") * 1e8,   # 流通市值 -> 元
        "total_mv": f("total_mv") * 1e8,   # 总市值 -> 元
        "limit_up": f("limit_up"),
        "limit_down": f("limit_down"),
        "time": s("time"),
    }


def fetch_snapshots(codes: list[str], batch: int = 60, workers: int = 10,
                    progress=None) -> dict[str, dict]:
    """批量拉取快照。返回 {code: snapshot}。"""
    batches = [codes[i:i + batch] for i in range(0, len(codes), batch)]
    result: dict[str, dict] = {}
    total = len(batches)
    done = 0

    def work(chunk: list[str]):
        url = "https://qt.gtimg.cn/q=" + ",".join(chunk)
        try:
            raw = _get(url, timeout=15).decode("gbk", errors="ignore")
        except Exception:
            return []
        out = []
        for line in raw.splitlines():
            rec = _parse_tx_line(line)
            if rec:
                out.append(rec)
        return out

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for recs in pool.map(work, batches):
            for r in recs:
                result[r["code"]] = r
            done += 1
            if progress and done % 20 == 0:
                progress(done, total)
    return result


def fetch_index_snapshots() -> dict:
    """指数快照 —— 用于测算全市场量能。"""
    codes = {
        "sh000001": "上证指数",
        "sz399001": "深证成指",
        "sz399006": "创业板指",
        "sh000688": "科创50",
        "sh000300": "沪深300",
        "sh000905": "中证500",
        "sh000852": "中证1000",
        "sz399303": "国证2000",
    }
    url = "https://qt.gtimg.cn/q=" + ",".join(codes.keys())
    try:
        raw = _get(url, timeout=12).decode("gbk", errors="ignore")
    except Exception:
        return {}
    out = {}
    for line in raw.splitlines():
        rec = _parse_tx_line(line)
        if not rec:
            continue
        # 指数的「成交额」在腾讯口径下单位为万元
        out[rec["code"]] = {
            "name": codes.get(rec["code"], rec["name"]),
            "price": rec["price"],
            "pct": rec["pct"],
            "amount": rec["amount"],
        }
    return out


# ---------------------------------------------------------------- K线

KLINE_HOSTS = [
    # (host, 是否前复权参数, referer)
    ("ifzq.gtimg.cn", True, "https://gu.qq.com/"),
    ("web.ifzq.gtimg.cn", True, "https://gu.qq.com/"),
]
SINA_KLINE = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "CN_MarketData.getKLineData?symbol={code}&scale=240&ma=5&datalen={n}")


def _kline_from_tx(code: str, n: int) -> list[dict]:
    """腾讯前复权日K。多域名自动降级（部分子域会限流返回 501）。"""
    for host, _, ref in KLINE_HOSTS:
        url = f"https://{host}/appstock/app/fqkline/get?param={code},day,,,{n},qfq"
        try:
            raw = _get(url, timeout=10, referer=ref).decode("utf-8", errors="ignore")
            data = json.loads(raw)
        except Exception:
            continue
        node = (data.get("data") or {}).get(code) or {}
        bars = node.get("qfqday") or node.get("day") or []
        out = []
        for b in bars:
            try:
                out.append({"date": b[0], "open": float(b[1]), "close": float(b[2]),
                            "high": float(b[3]), "low": float(b[4]), "volume": float(b[5])})
            except (ValueError, IndexError):
                continue
        if out:
            return out
    return []


def _kline_from_sina(code: str, n: int) -> list[dict]:
    """新浪日K 兜底源。"""
    try:
        raw = _get(SINA_KLINE.format(code=code, n=n), timeout=10,
                   referer="https://finance.sina.com.cn").decode("utf-8", errors="ignore")
        arr = json.loads(raw)
    except Exception:
        return []
    out = []
    for b in arr if isinstance(arr, list) else []:
        try:
            out.append({"date": b["day"], "open": float(b["open"]), "close": float(b["close"]),
                        "high": float(b["high"]), "low": float(b["low"]),
                        "volume": float(b["volume"]) / 100.0})  # 手
        except (KeyError, ValueError, TypeError):
            continue
    return out


def fetch_kline(code: str, n: int = 70) -> list[dict]:
    """前复权日K。返回 [{date,open,close,high,low,volume}]，按时间正序。"""
    def producer():
        bars = _kline_from_tx(code, n)
        if len(bars) < 25:
            bars = _kline_from_sina(code, n)
        return bars
    return cached(f"kline:{code}:{n}", 1800, producer)


KLINE_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache", "kline")

# 进程内二级缓存：{code: (mtime, bars)}，避免重复读磁盘
_MEM_KLINE: dict = {}


def _kline_cache_path(code: str) -> str:
    return os.path.join(KLINE_CACHE_DIR, f"{code}.json")


def load_kline_cache(code: str, max_age_h: float = 12.0) -> list[dict] | None:
    """读K线缓存。内存优先，其次磁盘。盘中12小时内不重复拉取。"""
    p = _kline_cache_path(code)
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        _MEM_KLINE.pop(code, None)
        return None
    if time.time() - mtime > max_age_h * 3600:
        return None
    hit = _MEM_KLINE.get(code)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(p, "r", encoding="utf-8") as f:
            bars = json.load(f)
        _MEM_KLINE[code] = (mtime, bars)
        return bars
    except (json.JSONDecodeError, OSError):
        return None


def save_kline_cache(code: str, bars: list[dict]) -> None:
    try:
        os.makedirs(KLINE_CACHE_DIR, exist_ok=True)
        p = _kline_cache_path(code)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(bars, f)
        os.replace(tmp, p)
        _MEM_KLINE[code] = (os.path.getmtime(p), bars)
    except OSError:
        pass


def prefetch_kline_cache(codes: list[str], max_age_h: float = 12.0) -> dict:
    """批量预载磁盘K线到内存。一次 listdir 替代 N 次 stat，显著提速。"""
    try:
        if not os.path.isdir(KLINE_CACHE_DIR):
            return {}
        now = time.time()
        hit = {}
        for fn in os.listdir(KLINE_CACHE_DIR):
            if not fn.endswith(".json"):
                continue
            code = fn[:-5]
            if code not in codes:
                continue
            p = os.path.join(KLINE_CACHE_DIR, fn)
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if now - mt > max_age_h * 3600:
                continue
            mem = _MEM_KLINE.get(code)
            if mem and mem[0] == mt:
                hit[code] = mem[1]
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    bars = json.load(f)
                _MEM_KLINE[code] = (mt, bars)
                hit[code] = bars
            except (json.JSONDecodeError, OSError):
                continue
        return hit
    except OSError:
        return {}


def fetch_klines(codes: list[str], n: int = 70, workers: int = 12,
                 progress=None) -> dict[str, list[dict]]:
    """批量拉取K线。磁盘缓存优先 + 多源降级 + 限速保护。"""
    result: dict[str, list[dict]] = {}
    code_set = set(codes)

    # 批量预载磁盘缓存（内存命中 + 磁盘）
    result.update(prefetch_kline_cache(code_set))
    todo = [c for c in codes if c not in result]

    done = 0
    total = len(todo)

    def one(c: str):
        bars = fetch_kline(c, n)
        if not bars:
            bars = _kline_from_sina(c, n)
        if bars:
            save_kline_cache(c, bars)
        return c, bars

    if todo:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # 用 as_completed 保证实时进度，不被 map 的惰性求值拖住
            futs = {pool.submit(one, c): c for c in todo}
            for fut in as_completed(futs):
                code, bars = fut.result()
                if bars:
                    result[code] = bars
                done += 1
                if progress:
                    progress(done, total)
    return result


# ---------------------------------------------------------------- 情绪池

POOL_BASE = "https://push2ex.eastmoney.com"
POOL_UT = "7eea3edcaed734bea9cbfc24409ed989"


def _fetch_pool(kind: str, date: str, sort: str, ttl: int = 120) -> list[dict]:
    """kind: ZT=涨停 DT=跌停 ZB=炸板

    注意：东财该接口会忽略入参 date，始终返回最近一个有数据交易日的数据。
    真实日期在响应体的 data.qdate 字段，由 pool_qdate() 单独读取。
    """
    def producer():
        url = (f"{POOL_BASE}/getTopic{kind}Pool?ut={POOL_UT}&dpt=wz.ztzt"
               f"&Pageindex=0&pagesize=600&sort={sort}&date={date}")
        try:
            raw = _get(url, timeout=15).decode("utf-8", errors="ignore")
            js = json.loads(raw)
        except Exception:
            return []
        data = js.get("data") or {}
        pool = data.get("pool") or []
        # 记录接口自报的真实交易日
        qd = data.get("qdate")
        if qd:
            _pool_qdate[kind] = str(qd)
        out = []
        for it in pool:
            try:
                mkt = "sh" if it.get("m") == 1 else "sz"
                out.append({
                    "code": f"{mkt}{it['c']}",
                    "name": it.get("n", ""),
                    "price": it.get("p", 0) / 1000.0,
                    "pct": it.get("zdp", 0.0),
                    "amount": it.get("amount", 0),
                    "float_mv": it.get("ltsz", 0),
                    "turnover": it.get("hs", 0.0),
                    "fund": it.get("fund", 0),
                    "first_time": it.get("fbt", 0),
                    "last_time": it.get("lbt", 0),
                    "break_count": it.get("zbc", 0),
                    "boards": it.get("lbc", 1),
                    "sector": it.get("hybk", ""),
                    "stat": (it.get("zttj") or {}),
                })
            except (KeyError, TypeError, ValueError):
                continue
        return out
    return cached(f"pool:{kind}:{date}", ttl, producer)


def pool_qdate() -> str:
    """接口自报的最新交易日（yyyyMMdd）。"""
    return _pool_qdate.get("ZT", "")


def last_trade_date() -> str:
    """最近有数据的交易日（yyyyMMdd），以涨停池接口自报的 qdate 为准。

    东财池接口会忽略 date 入参（始终返回最新交易日），因此不能靠传日期
    回溯，只能读取响应体中的 data.qdate。探测时传合法工作日，避免非法
    日期导致接口返回 data=null。
    """
    global _last_td
    if _last_td and (time.time() - _last_td[1]) < 1800:
        return _last_td[0]
    d = datetime.now()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    probe = d.strftime("%Y%m%d")
    _fetch_pool("ZT", probe, "fbt%3Aasc", ttl=0)
    qd = _pool_qdate.get("ZT") or probe
    _last_td = (qd, time.time())
    return qd


def fetch_limit_up_pool(date: str = "") -> list[dict]:
    return _fetch_pool("ZT", date or last_trade_date(), "fbt%3Aasc")


def fetch_limit_down_pool(date: str = "") -> list[dict]:
    return _fetch_pool("DT", date or last_trade_date(), "fund%3Aasc")


def fetch_broken_board_pool(date: str = "") -> list[dict]:
    """炸板池。"""
    return _fetch_pool("ZB", date or last_trade_date(), "fbt%3Aasc")


# ---------------------------------------------------------------- 历史情绪

def fetch_limit_up_history(days: int = 30, end_date: str = "") -> list[dict]:
    """近 N 个交易日的涨停家数（用于情绪周期趋势判断）。

    注意：池接口忽略 date 入参，始终返回最新交易日数据，因此历史序列
    通过「直接读取接口 + 记录 qdate」的方式无法回溯。这里退化为
    「最新交易日单点 + 本地累积缓存」——每次运行把当日涨停数追加到
    本地历史文件，形成可持续积累的涨停家数曲线。
    """
    end = end_date or last_trade_date()
    n = len(_fetch_pool("ZT", end, "fbt%3Aasc", ttl=300))
    if n <= 0:
        return []
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "zt_history.json")
    hist: list[dict] = []
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                hist = json.load(f)
    except (json.JSONDecodeError, OSError):
        hist = []
    if not any(h["date"] == end for h in hist):
        hist.append({"date": end, "limit_up": n})
        hist.sort(key=lambda x: x["date"])
        hist = hist[-250:]
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(hist, f)
        except OSError:
            pass
    return hist[-days:]


def market_amount_history(days: int = 40) -> list[dict]:
    """沪指日K（近 N 日），用于量能趋势。"""
    bars = _kline_from_tx("sh000001", days + 5)
    if not bars:
        return []
    return bars[-days:]


# ---------------------------------------------------------------- 本地涨停池归档
# 池接口忽略 date 入参、无法回溯历史，因此「昨日涨停池 / 老龙档案」只能靠
# 本地逐日归档积累（与 zt_history.json 同一思路）。

POOL_HIST_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "pool_history")


def save_pool_history(date: str, pool: list[dict]) -> None:
    """把当日涨停池快照归档到本地（M9 次日验证 / 老龙档案的数据底座）。

    已有同日存档不覆盖，避免盘中多次扫描产生不同快照互相打架。
    """
    if not date or not pool:
        return
    try:
        os.makedirs(POOL_HIST_DIR, exist_ok=True)
        path = os.path.join(POOL_HIST_DIR, f"{date}.json")
        if os.path.exists(path):
            return
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"date": date, "count": len(pool), "pool": pool}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def load_pool_history(date: str) -> list[dict]:
    """读取某日涨停池归档。无存档返回空列表。"""
    path = os.path.join(POOL_HIST_DIR, f"{date}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            js = json.load(f)
        return js.get("pool") or []
    except (OSError, json.JSONDecodeError):
        return []


def pool_history_dates() -> list[str]:
    """已归档的交易日列表（yyyyMMdd 升序）。"""
    try:
        return sorted(fn[:-5] for fn in os.listdir(POOL_HIST_DIR)
                      if fn.endswith(".json") and len(fn) == 13)
    except OSError:
        return []


# ---------------------------------------------------------------- 东财行业板块
# M9 主线五信号用：板块指数日K（带量新高判定）+ 板块成分股（中军识别）。
# 取不到时上层一律降级为「未验证」，不硬性否决信号。

EM_REFERER = "https://quote.eastmoney.com/"


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _norm_mv_yi(v: float) -> float:
    """东财市值字段单位不定（元/万元/亿），按量级归一为亿。"""
    if v >= 1e8:
        return v / 1e8
    if v >= 1e4:
        return v / 1e4
    return v


def fetch_board_list() -> dict[str, str]:
    """东财行业板块列表 {板块名: BK代码}。"""
    def producer():
        url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=600&po=1&np=1"
               "&fltt=2&invt=2&fid=f12&fs=m:90+t:2&fields=f12,f14")
        try:
            raw = _get(url, timeout=12, referer=EM_REFERER).decode("utf-8", errors="ignore")
            js = json.loads(raw)
        except Exception:
            return {}
        out: dict[str, str] = {}
        for it in (js.get("data") or {}).get("diff") or []:
            code, name = it.get("f12", ""), it.get("f14", "")
            if code and name:
                out[name] = code
        return out
    return cached("board_list", 86400, producer)


def fetch_board_kline(bk_code: str, n: int = 70) -> list[dict]:
    """东财行业板块指数日K，返回 [{date,open,close,high,low,volume,amount}]。"""
    def producer():
        url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
               f"secid=90.{bk_code}&klt=101&fqt=1&lmt={n}&end=20500101"
               "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57")
        try:
            raw = _get(url, timeout=12, referer=EM_REFERER).decode("utf-8", errors="ignore")
            js = json.loads(raw)
        except Exception:
            return []
        out = []
        for line in (js.get("data") or {}).get("klines") or []:
            p = line.split(",")
            try:
                out.append({"date": p[0], "open": float(p[1]), "close": float(p[2]),
                            "high": float(p[3]), "low": float(p[4]),
                            "volume": float(p[5]), "amount": float(p[6])})
            except (ValueError, IndexError):
                continue
        return out
    return cached(f"bk_kline:{bk_code}:{n}", 600, producer)


def fetch_board_constituents(bk_code: str) -> list[dict]:
    """东财行业板块成分股 [{code,name,pct,float_mv(亿)}]，按涨幅降序。"""
    def producer():
        url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=1000&po=1&np=1"
               "&fltt=2&invt=2&fid=f3&fs=b:" + bk_code + "&fields=f12,f14,f3,f21")
        try:
            raw = _get(url, timeout=12, referer=EM_REFERER).decode("utf-8", errors="ignore")
            js = json.loads(raw)
        except Exception:
            return []
        out = []
        for it in (js.get("data") or {}).get("diff") or []:
            code = str(it.get("f12", ""))
            if not code:
                continue
            if code.startswith(("4", "8")) or code.startswith("92"):
                mkt = "bj"
            elif code.startswith(("6", "9")):
                mkt = "sh"
            else:
                mkt = "sz"
            out.append({"code": mkt + code, "name": it.get("f14", ""),
                        "pct": _num(it.get("f3")),
                        "float_mv": _norm_mv_yi(_num(it.get("f21")))})
        return out
    return cached(f"bk_cons:{bk_code}", 300, producer)
