# -*- coding: utf-8 -*-
"""数据引擎：行情快照 / 日K线 / 情绪池（涨停、跌停、炸板）。

数据源（沙箱内实测可用）：
  1. 腾讯 qt.gtimg.cn     —— 批量实时快照（60只/请求，0.5s）
  2. 腾讯 web.ifzq.gtimg.cn —— 前复权日K线（单只/请求）
  3. 东财 push2ex         —— 涨停池 / 跌停池 / 炸板池
  4. 腾讯 qt.gtimg.cn     —— 指数快照（量能测算）
"""
from __future__ import annotations

import http.client
import json
import os
import queue
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_cache: dict = {}
_cache_ts: dict = {}
_last_td: tuple[str, float] | None = None
_pool_qdate: dict = {}

# ---------------------------------------------------------------- 连接池
# 全系统一次扫描要发几百个 HTTPS 请求，纯 urllib 每请求都要完整走一遍
# DNS + TCP + TLS 握手（占单请求耗时一半以上）—— 这是最大的速度瓶颈。
# 这里按 host 维护常驻 keep-alive 连接，跨阶段、跨扫描复用。

_SSL_CTX = ssl.create_default_context()
_SYS_PROXY = urllib.request.getproxies()   # 导入时读一次系统代理
_conn_pool: dict[str, queue.Queue] = {}
_POOL_MAX = 32   # 每 host 池上限，防止长驻进程文件句柄膨胀


def _new_conn(host: str, timeout: int) -> http.client.HTTPSConnection:
    px = _SYS_PROXY.get("https") or _SYS_PROXY.get("http")
    if px:
        # 走系统代理：连代理端口 + CONNECT 隧道到目标 443，行为与 urllib 一致
        pu = urllib.parse.urlsplit(px if "//" in px else "//" + px)
        conn = http.client.HTTPSConnection(pu.hostname, pu.port or 80,
                                           timeout=timeout, context=_SSL_CTX)
        conn.set_tunnel(host, 443)
    else:
        conn = http.client.HTTPSConnection(host, 443,
                                           timeout=timeout, context=_SSL_CTX)
    return conn


def _get(url: str, timeout: int = 12, referer: str = "") -> bytes:
    """HTTPS GET，keep-alive 连接池复用。

    服务端会掐掉闲置连接：第一次请求失败就丢弃旧连接重试一次，自愈。
    """
    p = urllib.parse.urlsplit(url)
    host = p.hostname or ""
    path = p.path + (("?" + p.query) if p.query else "")
    headers = {
        "User-Agent": UA,
        "Referer": referer or "https://gu.qq.com/",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Host": p.netloc,
    }
    q = _conn_pool.setdefault(host, queue.Queue())
    last_err: Exception | None = None
    for _ in range(2):
        conn = None
        try:
            try:
                conn = q.get_nowait()
            except queue.Empty:
                conn = _new_conn(host, timeout)
            if conn.sock is not None:
                conn.sock.settimeout(timeout)
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            if q.qsize() < _POOL_MAX:
                q.put(conn)
            else:
                conn.close()
            return data
        except Exception as e:
            last_err = e
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    raise last_err  # type: ignore[misc]


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


# ---------------------------------------------------------------- 有效代码名单
# enumerate_all() 按号段暴力枚举约 3.6 万码（绝大多数是空号），快照阶段
# 因此要发约 600 个请求。每天首次扫描后把「真实返回了行情的代码」落盘，
# 同日重扫只扫这 ~5 千只 —— 请求量降约 8 倍，这是快照阶段最大的提速。

VALID_CODES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "valid_codes.json")


def load_valid_codes() -> list[str] | None:
    """当日有效代码名单。按自然日失效：每天第一扫仍是全量枚举。

    当日新上市/盘中恢复交易的票要等次日首扫才进名单 —— 涨停池标的
    本来就逐日全量刷新，影响可忽略。
    """
    try:
        with open(VALID_CODES_PATH, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if obj.get("date") == datetime.now().strftime("%Y%m%d") and obj.get("codes"):
            return obj["codes"]
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return None


def save_valid_codes(codes: list[str]) -> None:
    try:
        os.makedirs(os.path.dirname(VALID_CODES_PATH), exist_ok=True)
        tmp = VALID_CODES_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"date": datetime.now().strftime("%Y%m%d"), "codes": codes}, f)
        os.replace(tmp, VALID_CODES_PATH)
    except OSError:
        pass


# ---------------------------------------------------------------- 两市成交额档案
# 量能阈值是"全天成交额"口径。盘前/集合竞价时当日累计额约等于 0，
# 必须退回上一完整交易日的全天额 —— 这个数只能靠本地逐日积累。

MARKET_AMOUNT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "market_amount.json")


def save_market_amount(date: str, amount_yi: float) -> None:
    """记录完整交易日两市成交额（亿）。同日多次扫描取最大值。"""
    if not date or amount_yi <= 0:
        return
    try:
        with open(MARKET_AMOUNT_PATH, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        entries = []
    for e in entries:
        if e.get("date") == date:
            if amount_yi > e.get("amount", 0):
                e["amount"] = round(amount_yi, 1)
            break
    else:
        entries.append({"date": date, "amount": round(amount_yi, 1)})
    entries.sort(key=lambda x: x["date"])
    entries = entries[-60:]
    try:
        os.makedirs(os.path.dirname(MARKET_AMOUNT_PATH), exist_ok=True)
        tmp = MARKET_AMOUNT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f)
        os.replace(tmp, MARKET_AMOUNT_PATH)
    except OSError:
        pass


def load_last_complete_amount(date_limit: str) -> float:
    """<= date_limit 的最近一个完整交易日两市成交额（亿）。无记录返回 0。"""
    try:
        with open(MARKET_AMOUNT_PATH, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return 0.0
    best = 0.0
    for e in entries:
        if str(e.get("date", "")) <= (date_limit or "99999999"):
            best = e.get("amount", 0.0)
    return best


def fetch_snapshots(codes: list[str], batch: int = 80, workers: int = 16,
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


# ---------------------------------------------------------------- 市场宽度（涨跌家数）
# 沪综指/深成指行情附带的统计字段（f104 上涨 / f105 下跌 / f106 平）是
# 全市场口径，两市相加即全市场涨跌家数 —— 情绪温度计最直接的一根温度柱。

# 东财 push2 有多台镜像，clist 接口偶发 RemoteDisconnected —— 逐台轮换提高成功率
EM_PUSH2_HOSTS = ["push2.eastmoney.com", "1.push2.eastmoney.com",
                  "82.push2.eastmoney.com"]


def _push2_json(path: str, referer: str = "https://quote.eastmoney.com/") -> dict:
    """东财 push2 接口（clist / ulist.np）：镜像主机轮换，返回解析后的 JSON。"""
    for host in EM_PUSH2_HOSTS:
        try:
            raw = _get(f"https://{host}{path}", timeout=12, referer=referer).decode(
                "utf-8", errors="ignore")
            return json.loads(raw)
        except Exception:
            continue
    return {}


def fetch_breadth() -> dict:
    """全市场涨跌家数 {up, down, flat, ...}。取不到返回空 dict。"""
    js = _push2_json("/api/qt/ulist.np/get?fltt=2&invt=2"
                     "&secids=1.000001,0.399001&fields=f12,f14,f104,f105,f106")
    diff = (js.get("data") or {}).get("diff") or []
    if not diff:
        return {}
    up = down = flat = 0
    for it in diff:
        up += int(_num(it.get("f104")))
        down += int(_num(it.get("f105")))
        flat += int(_num(it.get("f106")))
    total = up + down + flat
    if total <= 0:
        return {}
    return {"up": up, "down": down, "flat": flat, "total": total,
            "up_ratio": round(up / total, 3),
            "up_down_ratio": round(up / down, 2) if down else 0.0}


# ---------------------------------------------------------------- 主力资金流
# 参考 GitHub 项目 a-stock-data / slingshot 的「资金面」口径：
# 东财 push2 的资金流字段（f62 主力净额、f184 主力净占比、f66/72/78/84 四档）
# 免鉴权、可批量 —— 给候选股与行业板块各补一条「钱往哪走」的线。

_FLOW_FIELDS = "f2,f3,f12,f14,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87"


def _meta_of(code: str) -> str:
    """个股代码 → 东财 secid（0=深/北、1=沪）。"""
    if code.startswith(("6", "9", "5")) or code.startswith("sh"):
        return "1." + code[2:] if code.startswith("sh") else "1." + code
    if code.startswith("sz"):
        return "0." + code[2:]
    if code.startswith("bj"):
        return "0." + code[2:]
    return "0." + code


def fetch_stock_moneyflow(codes: list[str]) -> dict[str, dict]:
    """批量个股主力资金流 {code: {main,main_pct,super,big,mid,small}}，单位元。"""
    if not codes:
        return {}
    out: dict[str, dict] = {}
    batch = 50
    chunks = [codes[i:i + batch] for i in range(0, len(codes), batch)]

    def work(chunk):
        secids = ",".join(_meta_of(c) for c in chunk)
        js = _push2_json("/api/qt/ulist.np/get?fltt=2&invt=2"
                         f"&secids={secids}&fields={_FLOW_FIELDS}")
        diff = (js.get("data") or {}).get("diff") or []
        rows = []
        for it in diff:
            code = str(it.get("f12", ""))
            if not code:
                continue
            mkt = "bj" if code.startswith(("43", "83", "87", "88", "92")) else \
                  ("sh" if code.startswith(("6", "9")) else "sz")
            rows.append((mkt + code, {
                "main": _num(it.get("f62")), "main_pct": _num(it.get("f184")),
                "super": _num(it.get("f66")), "big": _num(it.get("f72")),
                "mid": _num(it.get("f78")), "small": _num(it.get("f84")),
            }))
        return rows

    with ThreadPoolExecutor(max_workers=8) as pool:
        for rows in pool.map(work, chunks):
            for code, d in rows:
                out[code] = d
    return out


def fetch_board_moneyflow(limit: int = 30) -> list[dict]:
    """行业板块主力资金流排行（按主力净额降序）。
    返回 [{code,name,pct,main,main_pct,lead,lead_code}]，main 单位元。

    失败不落缓存：接口偶发 RemoteDisconnected，若把空结果缓存 3 分钟，
    面板会整段时间显示「不可用」—— 这里只在拿到数据时才写缓存。
    """
    key = f"board_flow:{limit}"
    now = time.time()
    hit = _cache.get(key)
    if hit and now - _cache_ts.get(key, 0) < 180:
        return hit

    def producer():
        js = _push2_json("/api/qt/clist/get?pn=1"
                         f"&pz={limit}&po=1&np=1&fltt=2&invt=2&fid=f62&fs=m:90+t:2"
                         "&fields=f12,f14,f3,f62,f184,f204,f205")
        diff = (js.get("data") or {}).get("diff") or []
        out = []
        for it in diff:
            out.append({"code": it.get("f12", ""), "name": it.get("f14", ""),
                        "pct": _num(it.get("f3")), "main": _num(it.get("f62")),
                        "main_pct": _num(it.get("f184")),
                        "lead": it.get("f204", ""), "lead_code": it.get("f205", "")})
        return out

    rows = producer()
    if not rows:                       # 单次失败重试一次（接口抖动）
        rows = producer()
    if rows:
        _cache[key] = rows
        _cache_ts[key] = now
    return rows


# ---------------------------------------------------------------- 龙虎榜
# 参考 GitHub 项目 a-stock-data / slingshot / aiagents-stock 的「龙虎榜·游资席位」层：
# 东财 datacenter 报表接口免鉴权，给「谁在买」提供最硬的证据 —— 直接喂给 M10
# 主导资金分类（机构/游资），是打板体系里判断离场节奏的关键输入。

_DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_DC_REFERER = "https://data.eastmoney.com/"


def _dc_get(report: str, extra: str = "", page_size: int = 200) -> list[dict]:
    url = (f"{_DC}?reportName={report}&columns=ALL&pageNumber=1&pageSize={page_size}"
           f"&source=WEB&client=WEB&{extra}")
    try:
        raw = _get(url, timeout=15, referer=_DC_REFERER).decode("utf-8", errors="ignore")
        js = json.loads(raw)
    except Exception:
        return []
    return ((js.get("result") or {}).get("data")) or []


def fetch_lhb(date: str = "") -> list[dict]:
    """某一交易日的龙虎榜。date 形如 '2026-09-30'；空则取最近一个交易日。"""
    flt = f"&filter=(TRADE_DATE%3D%27{date}%27)" if date else ""
    rows = _dc_get("RPT_DAILYBILLBOARD_DETAILSNEW",
                   "sortColumns=TRADE_DATE&sortTypes=-1" + flt, 500)
    out = []
    for r in rows:
        code = str(r.get("SECURITY_CODE", ""))
        if not code:
            continue
        mk = str(r.get("SECUCODE", "")).split(".")[-1].lower()
        out.append({
            "code": (mk if mk in ("sh", "sz", "bj") else "sz") + code,
            "name": r.get("SECURITY_NAME_ABBR", ""),
            "date": str(r.get("TRADE_DATE", ""))[:10],
            "price": _num(r.get("CLOSE_PRICE")),
            "pct": round(_num(r.get("CHANGE_RATE")), 2),
            "turnover": round(_num(r.get("TURNOVERRATE")), 2),
            "buy": _num(r.get("BILLBOARD_BUY_AMT")),
            "sell": _num(r.get("BILLBOARD_SELL_AMT")),
            "net": _num(r.get("BILLBOARD_NET_AMT")),
            "amount": _num(r.get("ACCUM_AMOUNT")),
            "reason": r.get("EXPLANATION", ""),
            "free_mv": _num(r.get("FREE_MARKET_CAP")),
            # 上榜后 1/2/5/10 日涨幅（东财已回填，用于复盘席位胜率）
            "d1": _num(r.get("D1_CLOSE_ADJCHRATE")) if r.get("D1_CLOSE_ADJCHRATE") is not None else None,
            "d5": _num(r.get("D5_CLOSE_ADJCHRATE")) if r.get("D5_CLOSE_ADJCHRATE") is not None else None,
        })
    return out


def fetch_lhb_seats(date: str = "") -> tuple[list[dict], list[dict]]:
    """某日龙虎榜买卖席位明细 → (买入席位, 卖出席位)。

    每条 {code,name,dept,type,buy,sell,net,reason}，dept 为营业部全称。
    """
    flt = f"&filter=(TRADE_DATE%3D%27{date}%27)" if date else ""
    extra = "sortColumns=TRADE_DATE&sortTypes=-1" + flt

    def parse(rows, side):
        out = []
        for r in rows:
            name = r.get("OPERATEDEPT_NAME", "")
            if not name:
                continue
            mk = str(r.get("SECUCODE", "")).split(".")[-1].lower()
            code = (mk if mk in ("sh", "sz", "bj") else "sz") + str(r.get("SECURITY_CODE", ""))
            net = _num(r.get("NET"))
            # 买入榜的 NET 取正、卖出榜取负，统一为「净买入额（元）」
            out.append({
                "code": code,
                "dept": name,
                "buy": _num(r.get("BUY")),
                "sell": _num(r.get("SELL")),
                "net": abs(net) * (1 if side == "buy" else -1),
                "reason": r.get("EXPLANATION", ""),
                "pct": round(_num(r.get("CHANGE_RATE")), 2),
            })
        return out

    buys = parse(_dc_get("RPT_BILLBOARD_DAILYDETAILSBUY", extra, 800), "buy")
    sells = parse(_dc_get("RPT_BILLBOARD_DAILYDETAILSSELL", extra, 800), "sell")
    return buys, sells


# ---------------------------------------------------------------- 东财行业板块
# M9 主线五信号用：板块指数日K（带量新高判定）+ 板块成分股（中军识别）。
# 取不到时上层一律降级为「未验证」，不硬性否决信号。
#
# 注意：push2 / push2his（东财板块接口）在部分网络环境下被 TLS 层拦截，
# 此时板块指数改走「题材 → 中证/国证主题指数」映射（见 SECTOR_INDEX_MAP，
# 日K用腾讯 ifzq，实测 399xxx 系列有完整历史），映射不上的题材维持未验证。

EM_REFERER = "https://quote.eastmoney.com/"

# 题材关键词 → 腾讯行情指数代码（代理口径，日K经 ifzq 实测完整）。
# 只放"验证过有完整日K且语义贴近"的映射，宁缺毋滥 —— 没映射的题材
# 走东财原路径，再不行就降级「未验证」，与旧行为一致。
SECTOR_INDEX_MAP: list[tuple[tuple[str, ...], str, str]] = [
    (("电池", "锂电", "储能", "新能源车", "新能源汽车"), "sz399976", "CS新能车"),
    (("军工", "国防", "航空航天"), "sz399967", "中证军工"),
    (("券商", "证券"), "sz399975", "证券公司"),
    (("银行", "大金融"), "sz399986", "中证银行"),
    (("白酒", "酿酒"), "sz399997", "中证白酒"),
    (("房地产", "地产"), "sz399393", "国证地产"),
    (("新能车", "汽车产业"), "sz399976", "CS新能车"),
]


def resolve_sector_index(sector: str) -> tuple[str, str] | None:
    """题材名 → (指数代码, 指数名)。双向子串匹配，命中返回 None 表示无映射。"""
    if not sector:
        return None
    for keys, code, name in SECTOR_INDEX_MAP:
        if any(k in sector or sector in k for k in keys):
            return code, name
    return None


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
        js = _push2_json("/api/qt/clist/get?pn=1&pz=600&po=1&np=1"
                         "&fltt=2&invt=2&fid=f12&fs=m:90+t:2&fields=f12,f14", EM_REFERER)
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
        js = _push2_json("/api/qt/clist/get?pn=1&pz=1000&po=1&np=1"
                         "&fltt=2&invt=2&fid=f3&fs=b:" + bk_code + "&fields=f12,f14,f3,f21",
                         EM_REFERER)
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
