# -*- coding: utf-8 -*-
"""数据引擎：行情快照 / 日K线 / 情绪池（涨停、跌停、炸板）。

数据源优先级（对齐 a-stock-data 技能：**能不碰东财就不碰**）：
  1. 通达信 mootdx (TCP)     —— 前复权日K线（**首选**，不在 IP 黑名单；软依赖，
                                未安装则自动跳过，不影响安卓包）
  2. 腾讯 qt.gtimg.cn        —— 批量实时快照 / 指数快照（60只/请求）
  3. 腾讯 web.ifzq.gtimg.cn  —— 日K线次选（多子域自动降级）
  4. 新浪 quote.../klc_kl.js —— 日K线兜底
  5. 东财（**仅独有数据，最后手段，必须限速**）：push2ex=涨停/跌停/炸板池；
     push2 ulist.np=板块/资金流/涨跌家数；datacenter-web=龙虎榜。
     东财是按频率封（1 分钟 ≥200 次即触发），不是按路径封 —— 少打、别撞。
"""
from __future__ import annotations

import http.client
import inspect
import json
import os
import queue
import random
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta

from . import paths
from . import universe

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
# 系统代理：**惰性刷新**（每 5 分钟重读一次）—— 用户随时会开/关 Clash、换端口，
# 服务进程里固化一份启动时的快照会跟现实脱节（端口都对不上）。
_SYS_PROXY: dict = {}
_SYS_PROXY_TS = 0.0
_SYS_PROXY_TTL = 300.0
_PROXY_BAD_UNTIL = 0.0     # 代理连不上时短暂拉黑（期间直连），避免每请求都先撞死端口
_conn_pool: dict[str, queue.Queue] = {}
_POOL_MAX = 32   # 每 host 池上限，防止长驻进程文件句柄膨胀
_pool_lock = threading.Lock()


def _sys_proxy() -> dict:
    global _SYS_PROXY, _SYS_PROXY_TS
    now = time.time()
    if now - _SYS_PROXY_TS > _SYS_PROXY_TTL:
        try:
            _SYS_PROXY = urllib.request.getproxies()
        except Exception:
            _SYS_PROXY = {}
        _SYS_PROXY_TS = now
    return _SYS_PROXY


def reset_pool(host: str = "") -> int:
    """丢弃 keep-alive 连接池（全部或指定 host），返回关闭条数。

    长驻进程跑过多轮扫描后，池里可能残留已被服务端关闭的连接；
    _get 现在会自愈，但提供一个显式出口便于「数据源」面板手动重连。
    """
    global _conn_pool
    closed = 0
    with _pool_lock:
        hosts = [host] if host else list(_conn_pool.keys())
        for h in hosts:
            q = _conn_pool.pop(h, None)
            if not q:
                continue
            while True:
                try:
                    c = q.get_nowait()
                except queue.Empty:
                    break
                try:
                    c.close()
                    closed += 1
                except Exception:
                    pass
    return closed


# 说明：这里原来有一套「东财 Cookie」注入（环境变量 / data/eastmoney_cookie.txt /
# 前端粘贴保存 / 自动获取匿名 Cookie）。已**整体移除**（v1.29.0）——
# 代码自己的实测结论就是它没用：「东财 push2 的 clist 接口按 URL 路径风控，
# 请求发出后服务端直接 RST；而同一域名同一时刻的其它接口全部 200；东财入口页
# 一个 Set-Cookie 都不下发 → 该限流发生在连接层，与 Cookie 无关，带不带都一样被掐」。
# 留着它只会让「数据源」面板多一块没人用得上的配置项。


# ---------------------------------------------------------------- 接口熔断
# clist 被风控时，每轮扫描都要在 3 台镜像上各撞一次超时才降级（最坏 36s），
# 既慢、又更容易把频控撞得更狠。这里按「接口路径」记账：一旦确认被掐，冷却期内
# 直接返回空、走各自降级链路。冷却时长随连续失败轮数指数退避（5→10→20→30 分封顶）。

_em_fail: dict[str, int] = {}          # 接口 -> 连续失败轮数
_em_down_until: dict[str, float] = {}  # 接口 -> 冷却截止（epoch 秒）
_em_lock = threading.Lock()
# v1.19：冷却时长整体下调。原来封顶 30 分钟 —— 一次限流就半小时看不到
# 板块资金/涨跌家数，逼着人去刷页面（越刷越触发风控，恶性循环）。
# 现在：接口级封顶 5 分钟，家族级封顶 4 分钟，且都支持半开探测提前恢复。
EM_BLOCK_BASE = 120      # 首次冷却 2 分钟
EM_BLOCK_MAX = 300       # 封顶 5 分钟
EM_BLOCK_MIN_FAILS = 2   # 软失败（超时等）连续几轮才熔断；硬失败（被 RST）立即熔断


def _api_key(path: str) -> str:
    return path.split("?", 1)[0]


def _is_hard_block(err: Exception) -> bool:
    """服务端把我们掐了（RST / 断流）—— 不会自己好，值得立刻熔断。"""
    return isinstance(err, (http.client.RemoteDisconnected, http.client.BadStatusLine,
                            ConnectionResetError, ssl.SSLError))


def em_blocked() -> list[dict]:
    """当前处于冷却中的东财接口（过期的顺手清掉）。"""
    now = time.time()
    out = []
    with _em_lock:
        for k in list(_em_down_until):
            until = _em_down_until[k]
            if until > now:
                out.append({"api": k, "fails": _em_fail.get(k, 0),
                            "seconds_left": int(until - now)})
            else:
                _em_down_until.pop(k, None)
                _em_fail.pop(k, None)
    return sorted(out, key=lambda x: -x["seconds_left"])


def em_reset_blocks() -> int:
    """清空熔断记录（数据源面板的「清除熔断记录」按钮）。"""
    with _em_lock:
        n = len(_em_down_until)
        _em_down_until.clear()
        _em_fail.clear()
    return n


def _new_conn(host: str, timeout: int, plain: bool = False):
    """创建连接。**优先走系统代理**，代理不可用时自动直连。

    为什么东财请求要走代理（实测 2026-10-10）：系统以前从不吃系统代理，
    几十轮扫描全从家宽 IP 直连打出去，东财把这个 IP 标记了 —— 接口间歇
    RST、网页弹滑块。走用户本地代理（Clash 节点）相当于换干净 IP，实测
    6/6 成功 vs 直连 3/6。代理连不上（没开 Clash）时自动回直连，并在
    120 秒内不再尝试代理，避免每个请求都先撞一次死端口。

    ★ plain=True 走明文 HTTP（80 端口、不套 TLS）。腾讯行情这么走快 10 倍：
      实测同样 20 批 80 只并发，https 0.73s vs http 0.26s；子代理在 60 并发
      下量到 12.0s vs 1.2s（5.0 → 51.5 req/s）。RemoteDisconnected 是
      **握手层**被掐，走明文就把这一层整个绕开 —— 快照因此不再靠 TLS 赌运气。
      只对腾讯行情这类公开只读接口开这个口子；东财/带鉴权的一律继续 HTTPS。
    """
    if plain:
        conn = http.client.HTTPConnection(host, 80, timeout=timeout)
        conn._via_proxy = False
        return conn
    px = _sys_proxy().get("https") or _sys_proxy().get("http")
    if px and time.time() < _PROXY_BAD_UNTIL:
        px = None                     # 刚刚连不上：这几分钟直连，别反复撞
    if px:
        try:
            pu = urllib.parse.urlsplit(px if "//" in px else "//" + px)
            conn = http.client.HTTPSConnection(pu.hostname, pu.port or 80,
                                               timeout=timeout, context=_SSL_CTX)
            conn.set_tunnel(host, 443)
            conn._via_proxy = True    # 标记：失败时区分「代理问题」还是「东财掐」
            return conn
        except (ValueError, OSError):
            pass                      # 代理地址畸形 → 直连
    conn = http.client.HTTPSConnection(host, 443, timeout=timeout, context=_SSL_CTX)
    conn._via_proxy = False
    return conn


# ---------------------------------------------------------------- 主机级实时状态
# 「数据源」面板要列出**每一个源**的实时状态。最省事也最准的做法不是在面板里
# 另发一轮探测请求，而是在 `_get` 这**唯一出口**上记一笔：谁、什么时候、成没成、
# 花了多久。这样面板看到的就是系统真实使用中的状态，零额外请求、零额外延迟。
_HOST_STAT: dict[str, dict] = {}
_HOST_LOCK = threading.Lock()


def _host_note(host: str, ok: bool, ms: int, err: Exception | None = None) -> None:
    """记录一次主机请求结果（成功/失败、耗时、连续失败次数）。"""
    if not host:
        return
    with _HOST_LOCK:
        old = _HOST_STAT.get(host) or {}
        _HOST_STAT[host] = {
            "ok": bool(ok), "ts": time.time(), "ms": int(ms),
            "fails": 0 if ok else int(old.get("fails", 0)) + 1,
            "err": "" if ok else f"{type(err).__name__}" if err else "请求失败",
        }


def host_stat(host: str) -> dict:
    """某主机的最近一次请求结果（没有记录返回 {}）。"""
    with _HOST_LOCK:
        return dict(_HOST_STAT.get(host) or {})


def _get(url: str, timeout: int = 12, referer: str = "", plain: bool = None) -> bytes:
    """HTTPS GET，keep-alive 连接池复用。

    服务端会掐掉闲置连接：第一次请求失败就丢弃旧连接重试一次，自愈。

    plain：明文 HTTP。默认按 URL 的 scheme 自动判定（http:// 就是明文），
    显式传 True/False 可覆盖。连接池按「scheme://host」分别缓存，避免
    明文与 TLS 连接混在同一个池里。
    """
    p = urllib.parse.urlsplit(url)
    host = p.hostname or ""
    path = p.path + (("?" + p.query) if p.query else "")
    if plain is None:
        plain = (p.scheme == "http")
    pool_key = ("http://" if plain else "https://") + host
    headers = {
        "User-Agent": UA,
        "Referer": referer or "https://gu.qq.com/",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Host": p.netloc,
    }
    q = _conn_pool.setdefault(pool_key, queue.Queue())
    last_err: Exception | None = None
    t0 = time.time()
    for attempt in range(3):
        conn = None
        reused = False
        try:
            try:
                conn = q.get_nowait()
                reused = True
            except queue.Empty:
                conn = _new_conn(host, timeout, plain=plain)
            if conn.sock is not None:
                conn.sock.settimeout(timeout)
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            # 坏包检测必须对**所有**连接生效，不能只管复用连接：
            #   ① 复用连接可能已被服务端单方面关闭（读到空 body / 缺状态行残包）；
            #   ② **新建连接**同样可能拿到空 body 或非 200 —— 长驻进程（看板服务）
            #      走代理出口时尤其常见。以前只判 reused，新建连接的空响应会被当
            #      成功返回 → 解析出 0 条 → 调用方以为"这只股票不存在"
            #      （实测 /api/stock 连打 20 次有 6 次报"可能是停牌/已退市"）。
            # 判定为坏包 → 丢弃连接、换一条重试，不让空数据静默流过。
            if not data or resp.status != 200:
                raise http.client.BadStatusLine(
                    f"bad response: status={getattr(resp, 'status', '?')} "
                    f"len={len(data)} reused={reused}")
            if q.qsize() < _POOL_MAX:
                q.put(conn)
            else:
                conn.close()
            _host_note(host, True, int((time.time() - t0) * 1000))
            return data
        except Exception as e:
            last_err = e
            # 代理连不上（端口拒绝/超时在 connect 阶段）≠ 东财掐我们：
            # 前者该立即回直连并短暂拉黑代理，后者该走预算冷却。区分开。
            if isinstance(e, (ConnectionRefusedError, OSError)) \
                    and "connect" in str(e).lower() \
                    and getattr(conn, "_via_proxy", False):
                global _PROXY_BAD_UNTIL
                _PROXY_BAD_UNTIL = time.time() + 120
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            # 已被复用的坏连接：立即再试一次（下一轮 get_nowait 拿到的要么是新连接，
            # 要么是池里另一条；池空则新建，天然自愈）
    _host_note(host, False, int((time.time() - t0) * 1000), last_err)
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


# ---------------------------------------------------------------- 数据健康 & 最后一份好数据
# 为什么需要（2026-10-12 用户实测「实时盘中成交额偶尔丢失」，并要求自查其他数据）：
# 关键抓取（涨停/跌停/炸板池、指数快照、涨跌家数）一次网络抖动就返回空，而**空值会被
# 当成"今天真的没有"**：跌停池空 → 亏钱面积算成 0 → 系统在真退潮日得出"可以出手"，
# 这是安全相关的静默失败。而 `cached()` 还会把这次失败的空值缓存住（池子 120 秒），
# 让一次抖动影响两分钟。
#
# 三层做法：
#   ① 区分「接口失败」与「接口正常回答、就是空」（合法空值）—— producer 返回 (ok, value)；
#   ② 失败时回退到**当日最后一份好数据**（内存 + 落盘，跨扫描/跨重启可用），绝不用空值
#      覆盖缓存；
#   ③ 降级情况记进 `data_health()`，报告里如实标注，不假装一切正常。

MEM_PATH = paths.data_path("fetch_memory.json")
_mem: dict[str, dict] = {}          # key -> {date, ts, value}
_mem_lock = threading.Lock()
_health: dict[str, dict] = {}       # key -> {ok, ts, note, stale_seconds?}

# 界面上给这些 key 一个中文名（数据源面板/健康提示要用）
HEALTH_LABELS = {
    "pool:ZT": "涨停池", "pool:DT": "跌停池", "pool:ZB": "炸板池",
    "index": "指数行情", "breadth": "涨跌家数", "yzt": "昨日涨停池",
    "klines": "个股K线", "cls_reason": "涨停原因", "hot_rank": "人气榜",
    "lhb": "龙虎榜", "board_flow": "板块资金", "stock_flow": "个股资金",
    "board_list": "板块清单",
}


def _mem_write(key: str, date: str, value) -> None:
    """记一份"当日好数据"（内存 + 落盘）。落盘是为了跨进程/跨重启也能兜底。"""
    ts = time.time()
    with _mem_lock:
        _mem[key] = {"date": date, "ts": ts, "value": value}
    try:
        os.makedirs(os.path.dirname(MEM_PATH), exist_ok=True)
        try:
            with open(MEM_PATH, encoding="utf-8") as f:
                obj = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            obj = {}
        # 只留当日及以后，防文件无限增长
        obj = {d: v for d, v in obj.items() if isinstance(d, str) and d >= date}
        obj.setdefault(date, {})[key] = {"ts": ts, "value": value}
        tmp = MEM_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, MEM_PATH)
    except (OSError, TypeError, ValueError):
        pass


def _mem_read(key: str, date: str):
    """取当日最后一份好数据 → (value, ts)；没有则 (None, 0)。"""
    with _mem_lock:
        hit = _mem.get(key)
    if hit and hit.get("date") == date:
        return hit.get("value"), hit.get("ts", 0)
    try:
        with open(MEM_PATH, encoding="utf-8") as f:
            obj = json.load(f)
        day = ((obj.get(date) or {}).get(key)) or {}
        if day and day.get("value") is not None:
            ts = day.get("ts", 0)
            with _mem_lock:
                _mem[key] = {"date": date, "ts": ts, "value": day["value"]}
            return day["value"], ts
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    return None, 0


def _health_set(key: str, ok: bool, note: str = "", stale_seconds: int = 0) -> None:
    _health[key] = {"key": key, "label": HEALTH_LABELS.get(key, key), "ok": ok,
                    "ts": time.time(), "note": note,
                    "stale_seconds": int(stale_seconds) if not ok else 0}


def data_health() -> dict:
    """数据健康度自查：哪些关键数据这一轮没取到 / 用的是旧快照。

    前端据此如实标注（而不是把降级当正常）。ok=False 的项按 label 列出。
    """
    items = sorted(_health.values(), key=lambda x: (x["ok"], -(x.get("stale_seconds") or 0)))
    bad = [x for x in items if not x["ok"]]
    return {
        "ok": not bad,
        "degraded": bad,
        "checked": len(items),
        "summary": ("关键数据全部正常" if not bad else
                    "；".join(f"{x['label']}降级" for x in bad)),
    }


def reset_health() -> None:
    """每轮扫描开始时清一次，避免上一轮的降级一直挂着。"""
    _health.clear()


def _fetch_memo(key: str, date: str, ttl: int, producer, empty, valid=None):
    """带"最后一份好数据"记忆的抓取。

    producer() → (ok, value)：
      · ok=True  → 接口**正常回答**（value 可以是空列表/空 dict —— 那是合法空值，
                   比如今天真没有跌停股）；
      · ok=False → 传输/解析失败（value 忽略）。
    valid(value)：额外的有效性判定（例如"解析出 0 条且接口没自报交易日"也算失败）。
    empty：连当日历史快照都没有时返回什么（[] / {}）。
    """
    now = time.time()
    if ttl > 0 and key in _cache and now - _cache_ts.get(key, 0) < ttl:
        return _cache[key]
    ok, val = False, None
    try:
        ok, val = producer()
    except Exception:
        ok, val = False, None
    if ok and (valid is None or valid(val)):
        _cache[key] = val
        _cache_ts[key] = now
        _mem_write(key, date, val)
        _health_set(key, True)
        return val
    # 失败：回退当日最后一份好数据，**不用空值覆盖缓存**（否则抖动会被缓存住）
    old, ots = _mem_read(key, date)
    if old is not None:
        age = int(now - ots) if ots else 0
        _health_set(key, False, f"接口本次未取到，用的是 {age} 秒前的当日快照", age)
        _cache[key] = old          # 缓存旧的好数据，而不是空值
        _cache_ts[key] = now
        return old
    _health_set(key, False, "接口本次未取到，且当日还没有可用快照")
    return empty


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
    # 49 起这几个字段腾讯一直在返回，之前没解析（参考 easyquotation 的字段表补齐）。
    # 注意：ETF/部分品种字段数不足 54，取值必须容错。
    "volume_ratio": 49,   # 量比
    "bid_ask_diff": 50,   # 委差
    "avg_price": 51,      # 日内均价（VWAP）
    "pe_ttm": 52,         # 市盈(动)
    "pe_static": 53,      # 市盈(静)
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

    def f_opt(key):
        """字段不存在/取不到时返回 None（ETF 等品种字段数不足，别当成 0）。"""
        try:
            return float(parts[SNAP_FIELDS[key]])
        except (ValueError, IndexError):
            return None

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
        # 短线口径补充字段（取不到为 None，不参与计算时各自判空）
        "volume_ratio": f_opt("volume_ratio"),   # 量比
        "avg_price": f_opt("avg_price"),         # 日内均价 VWAP
        "bid_ask_diff": f_opt("bid_ask_diff"),   # 委差
        "pe_ttm": f_opt("pe_ttm"),               # 市盈(动)
        "pe_static": f_opt("pe_static"),         # 市盈(静)
    }


# ---------------------------------------------------------------- 有效代码名单
# enumerate_all() 按号段暴力枚举约 3.6 万码（绝大多数是空号），快照阶段
# 因此要发约 600 个请求。每天首次扫描后把「真实返回了行情的代码」落盘，
# 同日重扫只扫这 ~5 千只 —— 请求量降约 8 倍，这是快照阶段最大的提速。

VALID_CODES_PATH = paths.data_path("valid_codes.json")


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

MARKET_AMOUNT_PATH = paths.data_path("market_amount.json")
CURVE_PATH = paths.data_path("market_amount_curve.json")


def _amount_entries() -> list[dict]:
    try:
        with open(MARKET_AMOUNT_PATH, encoding="utf-8") as f:
            js = json.load(f)
        return js if isinstance(js, list) else []
    except (OSError, json.JSONDecodeError, ValueError):
        return []


def save_market_amount(date: str, amount_yi: float, final: bool = False) -> None:
    """记录两市成交额（亿）。

    同日多次扫描取最大值（当日累计额只会增不会减）。
    `final=True` 表示收盘后扫到的全天终值。**盘中也照样记**（记的是当日高水位）——
    原实现只在收盘后扫到才存，用户若收盘后再没扫过，这一天就永远进不了档案，
    次日盘前的"上一交易日全天额"只能退到更早的日期（基准越旧越不准）。
    """
    if not date or amount_yi <= 0:
        return
    entries = _amount_entries()
    for e in entries:
        if e.get("date") == date:
            if amount_yi > e.get("amount", 0):
                e["amount"] = round(amount_yi, 1)
            if final:
                e["final"] = True
            break
    else:
        entries.append({"date": date, "amount": round(amount_yi, 1), "final": bool(final)})
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


def recent_amount_median(days: int = 8, exclude_date: str = "") -> float:
    """近 N 个**已收盘**交易日两市成交额的中位数（亿）。样本不足返回 0。

    用途：给盘中的"节奏对比"当基准 —— 比"拿平均曲线外推全天额"稳得多。
    排除 exclude_date（今天，还是半天的数）以及 final=False 的盘中记录。

    ★ 「近 N 日」取的是**最近** N 个交易日（v1.32.3 修）：原实现
      `sorted(amts)[-days:]` 取的是**最大的 N 天**，与时间无关 —— 量能中枢
      下台阶后（前期 25000 亿、近期 12000 亿），它仍返回 25000 亿，
      基准被系统性抬高 → 节奏倍数偏低 → 明明正常放量的盘面被判成缩量。
    """
    amts = []
    for e in _amount_entries():
        d = str(e.get("date", ""))
        if not d or d == exclude_date:
            continue
        if not e.get("final", True):      # 盘中快照（偏低）不能当全天基准
            continue
        v = float(e.get("amount") or 0)
        if v > 0:
            amts.append(v)
    if len(amts) < 3:
        return 0.0
    # 先按"最近 days 天"截取（amts 已按日期升序），再排序求中位数 ——
    # 顺序反了就变成"最大的 days 天"，那是另一回事（见 docstring）。
    recent = amts[-max(1, int(days or 1)):]
    s = sorted(recent)
    n = len(s)
    return round(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2, 1)


def load_last_complete_amount(date_limit: str) -> float:
    """<= date_limit 的最近一个完整交易日两市成交额（亿）。无记录返回 0。"""
    return load_last_amount_meta(date_limit)["amount"]


def load_last_amount_meta(date_limit: str) -> dict:
    """<= date_limit 的最近一条**上一交易日**成交额记录 → {date, amount, final}。

    final=False 说明那天只扫到盘中（值偏低），界面要如实说明 —— 不能当成
    "上一完整交易日全天"。老档案没有 final 字段，按 True 处理（那时只有
    收盘后才写，值本来就是全天终值）。

    ★ 排除「当日自己的盘中快照」：档案会把今天的高水位也写进去（final=False），
      那不是"上一交易日"；盘前兜底若取到它，等于拿今天的半个数当昨天全天
      （实测 2026-10-13 盘前会把当日写下的 15378 亿当成"上一交易日全天额"）。
      当日记录只有在**已收盘（final=True）**时才代表一个完整交易日 ——
      周末/节假日的 real_date 正落在最近一个已收盘交易日上，那天必须能被采用。
    """
    limit = date_limit or "99999999"
    best = {"date": "", "amount": 0.0, "final": True}
    for e in _amount_entries():
        d = str(e.get("date", ""))
        if d > limit:
            continue
        if d == limit and not e.get("final", True):
            continue                      # 今天的盘中快照 ≠ 上一交易日
        best = {"date": e.get("date", ""),
                "amount": float(e.get("amount") or 0),
                "final": bool(e.get("final", True))}
    return best


# ---------------------------------------------------------------- 日内量能曲线记忆
# 盘中预估全天额原来靠一条**写死的经验曲线**（_DAY_PROFILE）折算，而不同交易日的
# 量能形态差别很大（放量日开盘更集中、缩量日更平），固定曲线必然有系统偏差。
# 这里逐日归档"已交易 tmin 分钟时的累计额"，用近 N 日**实测占比**去校准。

def _curve_load() -> dict:
    try:
        with open(CURVE_PATH, encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _curve_save(obj: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CURVE_PATH), exist_ok=True)
        tmp = CURVE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        os.replace(tmp, CURVE_PATH)
    except OSError:
        pass


def save_intraday_amount(date: str, tmin: int, cum_yi: float) -> None:
    """归档"截至第 tmin 分钟的当日累计成交额"（5 分钟分桶取最大值）。"""
    if not date or cum_yi <= 0:
        return
    tmin = max(0, min(240, int(tmin)))
    bucket = str((tmin // 5) * 5)
    obj = _curve_load()
    day = obj.setdefault(date, {})
    if cum_yi > float(day.get(bucket) or 0):
        day[bucket] = round(cum_yi, 1)
    for d in sorted(obj)[:-20]:          # 只留最近 20 个交易日
        obj.pop(d, None)
    _curve_save(obj)


def _session_minutes(day: dict, tmin: int, tol: int = 10) -> float | None:
    """某日曲线里「截至 tmin 分钟的累计成交额」（亿）；无贴近采样返回 None。

    tol：允许的采样时差（分钟）。桶是 5 分钟粒度、采样时刻也不规则，如果最近的
    一个采样比 tmin 早了 10 分钟以上，拿它当"截至 tmin"会把基准压低（时间越早
    累计越小）→ 分母偏小 → 节奏倍数被高估成"放量"。宁可弃用该日，不要用一个
    时刻对不上的样本。
    """
    best_kv, best = None, 0.0
    for k, v in day.items():
        try:
            kv = int(k)
        except (TypeError, ValueError):
            continue
        if kv <= tmin and (best_kv is None or kv > best_kv):
            best_kv, best = kv, float(v or 0)
    if best_kv is None or (tmin - best_kv) > tol:
        return None
    return best


def _full_day_amounts() -> dict[str, float]:
    """有**收盘终值**的交易日 → 全天成交额（亿）。

    盘中快照（final=False）记的是当日高水位、不是全天额，拿它当分母会让
    "同期占比"虚高到接近 1.0，所以只认收盘终值。
    """
    out: dict[str, float] = {}
    for e in _amount_entries():
        d = str(e.get("date", ""))
        v = float(e.get("amount") or 0)
        if d and v > 0 and e.get("final", True):
            out[d] = v
    return out


def intraday_baseline(tmin: int, days: int = 8, min_days: int = 3,
                      exclude_date: str = "", tol: int = 10) -> dict:
    """近 N 日的**盘中同期基准**，一次算齐（节奏对比与折算共用同一份样本）。

    返回 {"cum", "days", "frac", "frac_days", "days_total"}：
      cum       截至 tmin 的**实测累计额**中位数（亿）—— 钱对钱，最硬的口径
      days      参与 cum 的天数（≥1 即给数，调用方按天数决定措辞强度）
      frac      截至 tmin 的累计额 ÷ 当日全天额 的中位数；天数 < min_days 时为 None
      frac_days 参与 frac 的天数
      days_total 有可用同期采样的天数（= days，供界面显示）

    两条**边界**必须挡住，否则基准会被自己的样本污染：

      ① **排除当日**（exclude_date）：今天的分子分母是同一个"当日高水位"值，
         比值恒 ≈ 1.0。这是自证样本，混进来只会把占比整体推高
         （实测：样本 [0.672(10-12), 1.000(今天)] 中位数被抬到 0.836）。
      ② **分母必须是收盘终值**：盘中快照记的是高水位，不是全天额。
         同时只采信**采样时刻贴近 tmin** 的日子（见 _session_minutes 的 tol）。
    """
    obj = _curve_load()
    tmin = max(0, min(240, int(tmin)))
    if not obj:
        return {"cum": 0.0, "days": 0, "frac": None, "frac_days": 0, "days_total": 0}
    totals = _full_day_amounts()
    newest = max(obj)
    cums: list[float] = []
    fr: list[float] = []
    for date in sorted(obj)[-days:]:
        if exclude_date and date == exclude_date:
            continue
        # ③ "进行中"的那一天（曲线里最新日期 + 成交额档案还没标收盘终值）一律不用：
        #    它的分子分母是同一个高水位值，比值恒 ≈ 1.0。调用方漏传 exclude_date 时
        #    也要挡得住，所以这里再判一次（而不是只依赖调用方）。
        if date == newest and date not in totals:
            continue
        cum = _session_minutes(obj.get(date) or {}, tmin, tol)
        if cum is None or cum <= 0:
            continue
        cums.append(cum)
        total = totals.get(date)
        if total and cum <= total * 1.001:
            fr.append(cum / total)

    def _med(xs: list[float]) -> float:
        s = sorted(xs)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    frac = round(_med(fr), 4) if len(fr) >= min_days else None
    return {
        "cum": round(_med(cums), 1) if cums else 0.0,
        "days": len(cums),
        "frac": frac,
        "frac_days": len(fr),
        "days_total": len(cums),
    }


def intraday_fraction(tmin: int, days: int = 8, min_days: int = 3,
                      exclude_date: str = "", tol: int = 10):
    """近 N 日「截至 tmin 的累计额 ÷ 当日全天额」的中位数（≥min_days 天才给）。

    保留为薄封装：折算层只需要这个比例，而节奏对比还需要原始累计额，
    两者共用 intraday_baseline 的同一份样本，避免各算各的、口径漂移。
    """
    return intraday_baseline(tmin, days=days, min_days=min_days,
                             exclude_date=exclude_date, tol=tol)["frac"]


def fetch_snapshots(codes: list[str], batch: int = 80, workers: int = 16,
                    progress=None, diag: dict | None = None) -> dict[str, dict]:
    """批量拉取快照。返回 {code: snapshot}。

    diag（可选）：回填 {"failed_batches": n, "attempts": n}。
    为什么要这个：以前整批传输失败被静默吞掉（`except: return []`），调用方只能
    看到"这个 code 没数据"，于是把**网络抖动**误报成"停牌/已退市/代码填错"——
    实测 /api/stock 连打 20 次有 6 次这么误报，还引导用户去删自选。
    有了 diag，调用方就能区分「传输失败（重试即可）」和「代码本身取不到」。
    """
    batches = [codes[i:i + batch] for i in range(0, len(codes), batch)]
    result: dict[str, dict] = {}
    total = len(batches)
    done = 0
    failed = 0        # 传输层失败（网络抖动/限速）—— 重试有意义
    nomatch = 0       # 腾讯明确回答"没有这个代码"—— 重试没意义
    attempts = 0
    _dlock = threading.Lock()

    def work(chunk: list[str]):
        nonlocal failed, nomatch, attempts
        # ★ 走明文 http（不是 https）：腾讯行情这套接口是公开只读的，
        #   而 RemoteDisconnected 出在 **TLS 握手层** —— 换明文实测快 ~3 倍
        #   （20 批并发 0.73s → 0.26s；子代理 60 并发测得 12.0s → 1.2s），
        #   而且不再靠握手赌运气。全市场扫描是这一路的重头戏，收益直接落在
        #   「启动时的全市场扫描」上。
        url = "http://qt.gtimg.cn/q=" + ",".join(chunk)
        # 至多两次：_get 内部已重试 3 次，这里再兜一层，只治"刚好抖一下"
        for attempt in range(2):
            with _dlock:
                attempts += 1
            raw = ""
            try:
                raw = _get(url, timeout=15).decode("gbk", errors="ignore")
            except Exception:
                raw = ""
            if raw.strip():
                out = []
                for line in raw.splitlines():
                    rec = _parse_tx_line(line)
                    if rec:
                        out.append(rec)
                if out:
                    return out
                # 腾讯对不存在的代码固定回 `v_pv_none_match="1";` ——
                # 这是"接口有应答、只是没这个代码"，不是传输失败。
                # 不区分的话，查一个真不存在的代码会被误报成"网络抖动"。
                if "v_pv_none_match" in raw:
                    with _dlock:
                        nomatch += 1
                    return []
            if attempt == 0:
                time.sleep(0.25 + random.uniform(0.0, 0.15))
        with _dlock:
            failed += 1
        return []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for recs in pool.map(work, batches):
            for r in recs:
                result[r["code"]] = r
            done += 1
            if progress and done % 20 == 0:
                progress(done, total)
    if diag is not None:
        diag["failed_batches"] = failed
        diag["no_match_batches"] = nomatch
        diag["attempts"] = attempts
    return result


# ---------------------------------------------------------------- 新浪快照（备用）
# 腾讯快照的**独立备用源**（2026-10-14 加）。腾讯偶发 RemoteDisconnected 时，
# 整条快照链路不能就此断掉 —— 新浪给同一批字段，链路完全独立。
#
# 实测（本机，2026-10-14）：
#   · 单请求可塞 **800 只**（1000 只会被 431 拒绝），全市场 6173 只 = 8 个请求，
#     合计约 2.2 秒 —— 比腾讯慢，所以只做备用、不做主链路。
#   · **必须带 Referer**（https://finance.sina.com.cn/），不带一律 403。
#   · 编码是 GB18030（不是 GBK，虽然多数时候一样）。
#   · 字段数**随市场不同**（实测 SH=34 / SZ=33 / BJ=33），不能按下标硬取尾部。
#
# 字段口径（新浪）：
#   0=名称 1=今开 2=昨收 3=现价 4=最高 5=最低 6=买一 7=卖一
#   8=成交量(股) 9=成交额(元) 30=日期 31=时间
# 与腾讯的差异（下游要留意）：
#   · 成交量单位：新浪是**股**，腾讯是**手** → 这里统一折成「手」（÷100）。
#   · 新浪不给涨跌幅，用 (现价-昨收)/昨收 现算；昨收为 0 时留空。
#   · 新浪不给涨停/跌停价、换手率、流通市值 —— 这些字段只能缺省，
#     所以备用源的数据**不完整**，界面/下游必须能容忍缺字段。
def _parse_sina_line(line: str) -> dict | None:
    """一行 `var hq_str_sh600519="名称,今开,昨收,现价,...";` → 快照 dict。

    返回结构与 _parse_tx_line 对齐（下游无感切换），但只有上面列的字段。
    """
    if "hq_str_" not in line or '="' not in line:
        return None
    try:
        head, body = line.split('="', 1)
        code = head.split("hq_str_")[-1].strip()
        body = body.rstrip(';').rstrip('"')
        if not code or not body:
            return None
        p = body.split(",")
        if len(p) < 10:
            return None
        prev = _num(p[2])
        price = _num(p[3])
        # 全 0 = 停牌/未开盘/该票无数据，与腾讯的 none_match 类似，不算传输失败
        if price <= 0 and prev <= 0:
            return None
        pct = round((price - prev) / prev * 100, 2) if prev > 0 else None
        return {
            "code": code,
            "name": p[0],
            "price": price,
            "prev_close": prev,
            "open": _num(p[1]),
            "high": _num(p[4]),
            "low": _num(p[5]),
            "pct": pct,
            # 新浪成交量单位是股，腾讯是手 —— 统一折成手，免得下游量纲串台
            "volume": _num(p[8]) / 100.0,
            "amount": _num(p[9]),
            "time": (p[31] if len(p) > 31 else ""),
            "source": "sina",
        }
    except (IndexError, ValueError, AttributeError):
        return None


SINA_SNAP_URL = "https://hq.sinajs.cn/list="
SINA_SNAP_REFERER = "https://finance.sina.com.cn/"
SINA_BATCH = 800            # 实测上限：1000 只被 431 拒


def fetch_snapshots_sina(codes: list[str], batch: int = SINA_BATCH,
                         workers: int = 4) -> dict[str, dict]:
    """新浪快照（备用源）。返回 {code: snapshot}，失败返回 {}。

    只在腾讯整条链路失败时调用 —— 它比腾讯慢（全市场约 2.2s vs 0.6s），
    但链路独立，是「腾讯不通时至少还有数」的兜底。
    """
    if not codes:
        return {}
    batches = [codes[i:i + batch] for i in range(0, len(codes), batch)]
    result: dict[str, dict] = {}
    _lock = threading.Lock()

    def work(chunk: list[str]):
        url = SINA_SNAP_URL + ",".join(chunk)
        try:
            raw = _get(url, timeout=20, referer=SINA_SNAP_REFERER).decode(
                "gb18030", errors="ignore")
        except Exception:
            return []
        out = []
        for line in raw.splitlines():
            rec = _parse_sina_line(line)
            if rec:
                out.append(rec)
        return out

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for recs in pool.map(work, batches):
                with _lock:
                    for r in recs:
                        result[r["code"]] = r
    except Exception:
        return result
    return result


def fetch_index_snapshots(date: str = "", ttl: int = 30) -> dict:
    """指数快照 —— 用于测算全市场量能。

    这里必须带记忆（用户实测「实时盘中成交额偶尔丢失」）：腾讯一次抖动就返回 {}，
    下游 sum(...) = 0 → 量能模式退化成 pending，界面上成交额直接空掉。
    另外要求**至少两只**（上证+深证）都拿到：只回一只时 sum 会腰斩，
    那比"没有数据"更危险（会得出"极度缩量"的假结论）。
    """
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
    url = "http://qt.gtimg.cn/q=" + ",".join(codes.keys())

    def producer():
        out = {}
        for attempt in range(2):            # 一次兜底重试：只治"刚好抖一下"
            try:
                raw = _get(url, timeout=12).decode("gbk", errors="ignore")
            except Exception:
                raw = ""
            out = {}
            for line in raw.splitlines():
                rec = _parse_tx_line(line)
                if not rec:
                    continue
                # 指数的「成交额」单位是**元**（v1.32.3 更正注释）：
                # 实测 上证指数 4.65e11 = 4649.6 亿，且下游一律按 `>1e5 ? /1e8 : 原值`
                # 处理（见 pipeline）。原注释写的是"万元"，照着它做 ×1e4 会凭空放大一万倍。
                # 解析层统一走 _parse_tx_line 的成交额字段（第 37 位）。
                out[rec["code"]] = {
                    "name": codes.get(rec["code"], rec["name"]),
                    "price": rec["price"],
                    "pct": rec["pct"],
                    "amount": rec["amount"],
                }
            if len(out) >= 2:
                return True, out
            if attempt == 0:
                time.sleep(0.3)
        return False, out

    return _fetch_memo("index", date or last_trade_date(), ttl, producer,
                       empty={}, valid=lambda v: len(v or {}) >= 2)


def market_amount_hwm(date: str, current_yi: float, live: bool = True) -> float:
    """当日两市成交额的**高水位**（亿）。

    盘中累计成交额只会增不会减，所以取 max 天然安全；而"部分响应"
    （只回了上证没回深证）会让 sum 直接腰斩 —— 高水位正好挡住这种丢失。
    落盘保存，跨扫描/跨重启都有效。

    ★ live：这次读数是不是**今日盘中/收盘后**的真实累计额。盘前/集合竞价/休市
      时，指数快照里的成交额是**上一交易日**的全天额 —— 若当今日累计记下来，
      今日一开盘就被昨收全天额**锁死**（实测 2026-10-13：08:48 盘前扫到上一日
      全天 15378 亿并入选今日高水位，11:29 实际已成交仅 9987 亿，界面却仍显示
      15378 亿、"比近期同期快 104%（明显放量）"，节奏与进度条全被放大）。所以
      live=False 直接返回 0 —— 由调用方改走"上一交易日全天额"兜底，不碰今日档案。

    自愈：旧实现无 live 概念，落盘的是裸数值——无法证明它记的是"今日累计"，
    读取时一律不采信（视为 0 重新起算）。这样历史遗留的污染值会被自动丢弃，
    不需要手工清档；新写法落盘 {"value","live":True}，重读即可正常续用。
    """
    cur = float(current_yi or 0)
    if not live:
        return 0.0
    old, _ = _mem_read("mkt_amount", date)
    old_val = 0.0
    if isinstance(old, dict) and old.get("live"):
        old_val = float(old.get("value") or 0)
    best = max(cur, old_val)
    if best > 0 and best != old_val:
        _mem_write("mkt_amount", date, {"value": best, "live": True})
    return best


# ---------------------------------------------------------------- K线

KLINE_HOSTS = [
    # (host, 是否前复权参数, referer)
    ("ifzq.gtimg.cn", True, "https://gu.qq.com/"),
    ("web.ifzq.gtimg.cn", True, "https://gu.qq.com/"),
]
SINA_KLINE = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "CN_MarketData.getKLineData?symbol={code}&scale=240&ma=5&datalen={n}")


# ---------------------------------------------------------------- 通达信（mootdx）
# 数据源优先级（对齐 a-stock-data 技能）：**通达信(mootdx) TCP 不封 IP → 首选**；
# 腾讯 → 次选；新浪 → 兜底。东财只用于它**独有**的数据（涨停/跌停/炸板池、
# 龙虎榜、资金流、研报、新闻），且必须限速 —— 东财是按频率封，不是按路径封。
#
# mootdx 是**软依赖**：装了就用它打头阵，没装（或连不上 / 连续失败到阈值）
# 自动退回腾讯/新浪，不硬依赖、不影响安卓打包（安卓包不带 mootdx，直接走腾讯）。
try:
    from mootdx.quotes import Quotes as _TdxQuotes
    _TDX_AVAILABLE = True
except Exception:
    _TdxQuotes = None
    _TDX_AVAILABLE = False

_tdx_local = threading.local()
_tdx_lock = threading.Lock()
_tdx_fail = 0
_tdx_probed = False
TDX_FAIL_LIMIT = 3      # 连续失败到此数 → 本次会话弃用通达信，防拖慢全市场扫描

# ★ 选服务器（v1.30.0 修）—— 这是「通达信一直显示不可用」的**真正原因**。
#
# mootdx 不传 server 时会用 `~/.mootdx/config.json` 里的 `BESTIP`，而那份 BESTIP
# 可能指向一台**早已下线**的机器（实测本机 BESTIP=218.6.170.47：连不上 → 每次
# K 线调用白等 4~15 秒后返回空）。更麻烦的是 `mootdx.config.set()` **只改内存、
# 不落盘**，所以每次启动都会重新读回那台死服务器 —— 通达信等于永远没用上。
#
# 而且「TCP 连得上」≠「能出数据」：实测 55 台候选里 14 台 TCP 通，
# **只有 1 台真能返回 K 线**（通达信服务端协议近年改过，大量老站点只应答
# 「股票列表」这类请求，K线/行情一律回空）。所以选服必须**真拉一次 K 线**才算数。
TDX_SERVERS = [                     # 已知可用站点（社区维护，实测有效）
    ("117.34.114.14", 7709),
]
TDX_PROBE_TIMEOUT = 2.5             # 单台探测超时（秒）
TDX_CACHE_PATH = paths.data_path("tdx_server.json")


def _tdx_candidates() -> list[tuple[str, int]]:
    """候选服务器：内置已知可用 → 上次选中的 → mootdx 配置里的 SERVER.HQ。

    顺序即优先级；去重。配置表通常有几十台，一并纳入 —— 哪天内置那台挂了，
    还能从配置里挑到活的。
    """
    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()

    def add(ip, port=7709):
        try:
            key = (str(ip).strip(), int(port))
        except (TypeError, ValueError):
            return
        if key[0] and key not in seen:
            seen.add(key)
            out.append(key)

    for ip, port in TDX_SERVERS:
        add(ip, port)
    try:
        with open(TDX_CACHE_PATH, encoding="utf-8") as f:
            c = json.load(f)
        add(c.get("ip"), c.get("port", 7709))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    try:                                        # mootdx 自带的站点表
        cfg = os.path.join(os.path.expanduser("~"), ".mootdx", "config.json")
        with open(cfg, encoding="utf-8") as f:
            hq = (json.load(f).get("SERVER") or {}).get("HQ") or []
        for row in hq:
            if isinstance(row, (list, tuple)) and len(row) >= 3:
                add(row[1], row[2])
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    return out


def _tcp_ok(ip: str, port: int, timeout: float = TDX_PROBE_TIMEOUT) -> bool:
    import socket as _s
    try:
        c = _s.create_connection((ip, int(port)), timeout=timeout)
        c.close()
        return True
    except OSError:
        return False


def _tdx_data_ok(ip: str, port: int, timeout: float = TDX_PROBE_TIMEOUT) -> bool:
    """真拉一次 K 线 —— 只有能出数据才算「可用」。

    用 tdxpy 的裸客户端探测（mootdx 内部就是它），**不走 mootdx 的 factory** ——
    factory 会写 mootdx 的全局配置，多线程并行探测时会互相覆盖。
    """
    try:
        from tdxpy.hq import TdxHq_API
    except Exception:
        return False
    api = TdxHq_API(raise_exception=False)
    try:
        if not api.connect(ip, int(port), time_out=timeout):
            return False
        return bool(api.get_security_bars(9, 1, "600519", 0, 3))
    except Exception:
        return False
    finally:
        try:
            api.disconnect()
        except Exception:
            pass


def _tdx_pick_server(force: bool = False) -> tuple[str, int] | None:
    """选一台**真能出数据**的服务器，结果落盘复用。选不到返回 None。

    两步：① 并行 TCP 探活（便宜，先砍掉大半）；② 对活下来的逐个做数据探测。
    整个过程加锁只做一次；之后走缓存，不再重复探测（`_tdx_picked_once` 记住
    "已经探过了"，免得在确实没有可用服务器的环境里每次开面板都白等几秒）。
    """
    global _tdx_server, _tdx_picked_once
    with _tdx_lock:
        if _tdx_server and not force:
            return _tdx_server
        if _tdx_picked_once and not force:
            return None
        cands = _tdx_candidates()
        if not cands:
            _tdx_server = None
            return None
        live: list[tuple[str, int]] = []
        with ThreadPoolExecutor(max_workers=min(24, len(cands))) as ex:
            for c, good in zip(cands, ex.map(lambda x: _tcp_ok(*x), cands)):
                if good:
                    live.append(c)
        for c in live:                      # 数据探测：按候选顺序取第一台能出数据的
            if _tdx_data_ok(*c):
                _tdx_server = c
                try:
                    os.makedirs(os.path.dirname(TDX_CACHE_PATH), exist_ok=True)
                    with open(TDX_CACHE_PATH, "w", encoding="utf-8") as f:
                        json.dump({"ip": c[0], "port": c[1], "ts": int(time.time())}, f)
                except OSError:
                    pass
                _tdx_picked_once = True
                return c
        _tdx_server = None
        _tdx_picked_once = True
        return None


_tdx_server: tuple[str, int] | None = None
_tdx_picked_once = False        # 是否已经探过一轮（避免反复白等）


def _tdx_client():
    """本线程的 mootdx 连接（惰性建、线程内复用）。不可用/已弃用 → None。

    ★ 一次性健康探测：先选到一台能出数据的服务器，再试拉一次真实K线；
    拿不到就立刻全局弃用 —— 否则在连不上的网络里，每次 K 线调用都要白等几秒
    （全市场扫描会明显变慢）。探测只做一次、加锁串行，失败后 _tdx_fail 顶到上限。
    """
    global _tdx_fail, _tdx_probed
    if not _TDX_AVAILABLE or _tdx_fail >= TDX_FAIL_LIMIT:
        return None
    cli = getattr(_tdx_local, "cli", None)
    if cli is not None:
        return cli
    with _tdx_lock:
        if _tdx_fail >= TDX_FAIL_LIMIT:
            return None
    srv = _tdx_pick_server()
    if srv is None:
        _tdx_fail = TDX_FAIL_LIMIT
        return None
    with _tdx_lock:
        try:
            cli = _TdxQuotes.factory(market="std", server=srv, timeout=6)
        except Exception:
            _tdx_fail = TDX_FAIL_LIMIT
            return None
        if not _tdx_probed:
            _tdx_probed = True
            try:
                df = cli.bars(symbol="600519", frequency=9, offset=3)
                if df is None or len(df) == 0:
                    _tdx_fail = TDX_FAIL_LIMIT
                    return None
            except Exception:
                _tdx_fail = TDX_FAIL_LIMIT
                return None
    _tdx_local.cli = cli
    return cli


def tdx_status() -> dict:
    """通达信可用性（给数据源面板用）。"""
    srv = _tdx_server
    return {"available": _TDX_AVAILABLE, "fail": _tdx_fail,
            "disabled": _tdx_fail >= TDX_FAIL_LIMIT,
            "server": (f"{srv[0]}:{srv[1]}" if srv else ""),
            "candidates": len(_tdx_candidates()) if _TDX_AVAILABLE else 0,
            "picked": bool(srv)}


def tdx_server_probe() -> dict:
    """重新选服（面板/自检用）：清掉缓存与弃用标记，再挑一台能出数据的。"""
    global _tdx_fail, _tdx_probed, _tdx_server, _tdx_picked_once
    with _tdx_lock:
        _tdx_fail = 0
        _tdx_probed = False
        _tdx_server = None
        _tdx_picked_once = False
    _tdx_local.cli = None
    _tdx_pick_server(force=True)
    return tdx_status()


@contextmanager
def _pandas_legacy_fillna():
    """临时给 pandas 的 `fillna` 补回 1.x 的 `method=` 参数。

    mootdx 的**复权**实现里调了 `fillna(method='ffill', axis=0)`，而 pandas 2.2+
    已移除该参数（`TypeError: NDFrame.fillna() got an unexpected keyword argument
    'method'`）→ 走 `adjust="qfq"` 必崩。降级 pandas 不可行：Python 3.13 没有
    pandas<2.2 的 wheel，只能源码编译（实测 3 分钟没编完）。

    所以只在这一个调用点上打补丁，用完立刻还原 —— 不动进程里其它地方的 pandas 行为。
    """
    try:
        import pandas as pd
    except Exception:                                  # noqa: BLE001
        yield
        return
    # ★ DataFrame 和 Series 都要打：mootdx 复权里是 `data.factor.fillna(...)`，
    #   而 `data.factor` 是 **Series** —— 只补 DataFrame 等于没补。
    targets = [pd.DataFrame, pd.Series]
    origs = {}
    for cls in targets:
        orig = cls.fillna
        try:
            if "method" in inspect.signature(orig).parameters:
                continue                               # 老版本本来就支持
        except (TypeError, ValueError):
            continue

        def _fillna(self, value=None, *, method=None, axis=None, inplace=False,
                    limit=None, _orig=orig, **kw):
            if method in ("ffill", "pad"):
                return self.ffill(limit=limit, inplace=inplace)
            if method in ("bfill", "backfill"):
                return self.bfill(limit=limit, inplace=inplace)
            return _orig(self, value, axis=axis, inplace=inplace, limit=limit, **kw)

        origs[cls] = orig
        cls.fillna = _fillna
    try:
        yield
    finally:
        for cls, orig in origs.items():
            cls.fillna = orig


def _tdx_bars(cli, sym: str, n: int, index: bool = False) -> list[dict]:
    """把 mootdx 的 K 线 DataFrame 转成我们的 bar 列表（含两处必要的清洗）。

    ① **前复权优先、失败退回不复权**：mootdx 的复权依赖 pandas，旧版 pandas
       里 `fillna(method=...)` 还在、新版已移除 —— 一旦复权路径抛异常，
       退回不复权至少还能给出可用数据（调用方只关心形态与量能）。
    ② **丢掉未走完的当日 bar**：盘中（尤其开盘前）服务器会返回一根
       `open==close==high==low` 且 `vol≈0` 的占位K线。把它当成真K线喂给
       形态判定，会凭空多出一根"一字板"，`_graph_score` / `swash` 全被带偏。
       判据：成交量<=0 或 四价全等 且 日期==今天。
    """
    df = None
    for kwargs in ({"adjust": "qfq"}, {}):
        try:
            with _pandas_legacy_fillna():              # 复权要用到，见该函数说明
                df = (cli.index_bars(symbol=sym, frequency=9, offset=n, **kwargs)
                      if index else cli.bars(symbol=sym, frequency=9, offset=n, **kwargs))
            if df is not None and len(df):
                break
        except TypeError:
            try:                       # 极老版本 mootdx 用 category=4
                df = (cli.index_bars(symbol=sym, category=4, offset=n)
                      if index else cli.bars(symbol=sym, category=4, offset=n))
                if df is not None and len(df):
                    break
            except Exception:                          # noqa: BLE001
                pass
        except Exception:                              # noqa: BLE001
            pass
    if df is None or len(df) == 0:
        return []
    try:
        df = df.reset_index()          # datetime 可能是索引，摊平成列
    except Exception:
        pass
    today = datetime.now().strftime("%Y-%m-%d")
    out = []
    try:
        rows = df.iterrows()
    except AttributeError:
        rows = enumerate(df)
    for _, r in rows:
        try:
            d = str(r.get("datetime") or r.get("date") or "")[:10]
            if len(d) < 10:
                continue
            vol = r.get("vol")
            if vol is None:
                vol = r.get("volume", 0)
            vol = float(vol or 0)
            o, c = float(r["open"]), float(r["close"])
            h, low = float(r["high"]), float(r["low"])
            # 未走完的当日占位 bar（四价全等 + 无成交量）—— 丢掉，别当一字板。
            # ★ 判据不能用 `vol <= 0`：服务器给的是 5.877471754111438e-39 这种
            #   **非规格化浮点**（协议里"空"字段的位模式），它 > 0，会漏判。
            #   真实成交量以手/股计，永远 >= 1，所以用 < 1 当"没有量"。
            if d == today and vol < 1 and o == c == h == low:
                continue
            if vol < 1 and o <= 0:
                continue
            out.append({"date": d, "open": o, "close": c, "high": h, "low": low,
                        "volume": vol})
        except Exception:
            continue
    out.sort(key=lambda x: x["date"])
    return out


def _kline_from_tdx(code: str, n: int) -> list[dict]:
    """通达信(mootdx) 前复权日K —— 首选源。失败返回 []，调用方自动降级。"""
    global _tdx_fail
    cli = _tdx_client()
    if cli is None:
        return []
    sym = code[2:] if code[:2].lower() in ("sh", "sz", "bj") else code
    out = _tdx_bars(cli, sym, n)
    if not out:
        _tdx_fail += 1
        return []
    _tdx_fail = 0
    return out


def _kline_from_tdx_index(symbol: str, n: int) -> list[dict]:
    """通达信指数日K（沪指 000001 / 深成指 399001）—— 指数走 index_bars。"""
    global _tdx_fail
    cli = _tdx_client()
    if cli is None:
        return []
    out = _tdx_bars(cli, symbol, n, index=True)
    if out:
        _tdx_fail = 0
    return out


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
    """前复权日K。返回 [{date,open,close,high,low,volume}]，按时间正序。

    源优先级：**通达信(mootdx, TCP 不封 IP)** → 腾讯 → 新浪。
    通达信是软依赖，未装 / 连不上时自动退回腾讯/新浪（安卓包即走此路径）。
    """
    def producer():
        bars = _kline_from_tdx(code, n)
        if len(bars) < 25:
            bars = _kline_from_tx(code, n)
        if len(bars) < 25:
            bars = _kline_from_sina(code, n)
        return bars
    return cached(f"kline:{code}:{n}", 1800, producer)


KLINE_CACHE_DIR = paths.cache_path("kline")

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
    # K线静默丢失要能自查：缺 K线的票会被直接踢出候选，用户看不到"少了几只"。
    # 只有真的去拉过（todo 非空）且缺口明显时才记降级，避免全是缓存命中时报假警。
    if todo and len(result) < len(code_set):
        miss = len(code_set) - len(result)
        _health_set("klines", False, f"{miss} 只票的K线没取到（已被排除在候选外）")
    else:
        _health_set("klines", True)
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
            return False, None          # 传输/解析失败 —— 不是"今天没有涨停"
        data = js.get("data")
        # data=null：接口没给数据（日期非法 / 被限速）。**不能当成合法空值** ——
        # 以前这里直接 `data or {}` → 空池 → 亏钱面积算成 0 → 真退潮日判成"可以出手"。
        if not isinstance(data, dict):
            return False, None
        # 记录接口自报的真实交易日
        qd = data.get("qdate")
        if qd:
            _pool_qdate[kind] = str(qd)
        pool = data.get("pool") or []
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
        # 接口**正常回答**（out 可能为空 —— 比如今天真没有跌停股，那是合法空值）
        return True, out
    return _fetch_memo(f"pool:{kind}", date, ttl, producer, empty=[])


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


def fetch_yesterday_zt_pool(ttl: int = 120) -> list[dict]:
    """昨日涨停池 + 今日表现 —— 打板溢价率的直接数据源。

    每行：zdp=该股今日涨幅、ylbc=昨日连板数、yfbt=昨日封板时间。
    打板溢价（昨日涨停股今天赚不赚钱）是情绪最灵敏的单指标：
    它不依赖本地归档与全市场快照，冷启动当天就有数。
    """
    def producer():
        d = datetime.now()
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        url = (f"{POOL_BASE}/getYesterdayZTPool?ut={POOL_UT}&dpt=wz.ztzt"
               f"&Pageindex=0&pagesize=600&sort=zs%3Adesc"
               f"&date={d.strftime('%Y%m%d')}")
        try:
            raw = _get(url, timeout=15).decode("utf-8", errors="ignore")
            js = json.loads(raw)
        except Exception:
            return False, None
        data = js.get("data")
        if not isinstance(data, dict):
            return False, None          # 同 _fetch_pool：data=null 是失败，不是"昨天没有涨停"
        out = []
        for it in (data.get("pool") or []):
            try:
                mkt = "sh" if it.get("m") == 1 else "sz"
                out.append({
                    "code": f"{mkt}{it['c']}",
                    "name": (it.get("n") or "").strip(),
                    "price": (it.get("p") or 0) / 1000.0,
                    "pct": it.get("zdp") or 0.0,       # 今日涨幅
                    "boards_y": it.get("ylbc", 1),      # 昨日连板数
                    "yfbt": it.get("yfbt", 0),          # 昨日封板时间
                    "sector": it.get("hybk", ""),
                })
            except (KeyError, TypeError, ValueError):
                continue
        return True, out
    return _fetch_memo("yzt", last_trade_date(), ttl, producer, empty=[])


# 盘口异动类型（东财 getAllStockChanges），只收录本系统用到的
CHANGE_TYPES = {
    "8208": "打开涨停板",
    "8205": "封涨停板",
    "8201": "火箭发射",
    "8193": "大笔买入",
}


def fetch_stock_changes(change_type: str = "8208", ttl: int = 60) -> list[dict]:
    """盘口异动列表（实时事件流，最新在前）。

    默认「打开涨停板」—— 盘中实时炸板监测：炸板池接口给的是"已炸板归档"，
    这个给的是"此刻正在炸"的事件流，盯盘时更早一步看到风险。
    收盘后该接口数据有限（事件已清理），调用方自行判断是否需要。

    字段格式实测（2026-10-09）：i 为逗号拼接串，位置因类型而异 ——
    打开涨停板/火箭发射类："涨幅比率,价格,涨幅比率"（比率 ×100 得百分数）；
    大笔买卖类："量,价格,涨幅比率,金额"。
    """
    def producer():
        url = (f"{POOL_BASE}/getAllStockChanges?type={change_type}&ut={POOL_UT}"
               f"&pageindex=0&pagesize=200&dpt=wzchanges")
        try:
            raw = _get(url, timeout=12, referer="https://quote.eastmoney.com/").decode(
                "utf-8", errors="ignore")
            js = json.loads(raw)
        except Exception:
            return []
        items = (js.get("data") or {}).get("allstock") or []
        out = []
        for it in items:
            try:
                code6 = str(it.get("c") or "")
                if not code6 or len(code6) != 6:
                    continue
                mkt = "sh" if it.get("m") == 1 else "sz"
                tm = str(it.get("tm") or "")
                if len(tm) == 5:
                    tm = "0" + tm
                parts = str(it.get("i") or "").split(",")
                pct = None
                try:
                    if change_type in ("8201", "8202", "8203", "8204",
                                       "8205", "8206", "8207", "8208"):
                        pct = round(float(parts[0]) * 100, 2)
                    elif change_type in ("8193", "8194") and len(parts) >= 3:
                        pct = round(float(parts[2]) * 100, 2)
                except (ValueError, IndexError):
                    pct = None
                out.append({
                    "code": f"{mkt}{code6}",
                    "name": str(it.get("n") or "").strip(),
                    "time": tm,                       # HHMMSS
                    "pct": pct,
                    "type": change_type,
                })
            except (KeyError, TypeError, ValueError):
                continue
        return out
    return cached(f"changes:{change_type}", ttl, producer)


# ---------------------------------------------------------------- 历史情绪

def fetch_limit_up_history(days: int = 30, end_date: str = "",
                           final: bool = False) -> list[dict]:
    """近 N 个交易日的涨停家数（用于情绪周期趋势判断）。

    注意：池接口忽略 date 入参，始终返回最新交易日数据，因此历史序列
    通过「直接读取接口 + 记录 qdate」的方式无法回溯。这里退化为
    「最新交易日单点 + 本地累积缓存」——每次运行把当日涨停数追加到
    本地历史文件，形成可持续积累的涨停家数曲线。

    ★ final：这次读数是不是**收盘后的终值**（由调用方按交易时段传入）。
    原实现是"某日期只要已存在就永不更新"，于是**当天首扫看板上写了几个
    就定稿**：实测 20261012 记的是 16 家（那次扫描在盘中早段），而当天
    真实涨停 46 家 —— 涨停家数曲线与"昨日涨停"类比基准全都被这条错值污染。
    现在改成：未定稿（final=False）的记录**允许被后续读数更新**，
    收盘终值（final=True）一旦写入就不再被盘中读数覆盖。
    老档案没有 final 字段 → 视为未定稿（可被修正），这样历史错值也能自愈。
    """
    end = end_date or last_trade_date()
    n = len(_fetch_pool("ZT", end, "fbt%3Aasc", ttl=300))
    if n <= 0:
        return []
    path = paths.data_path("zt_history.json")
    hist: list[dict] = []
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                hist = json.load(f)
    except (json.JSONDecodeError, OSError):
        hist = []
    cur = next((h for h in hist if h.get("date") == end), None)
    if cur is None:
        hist.append({"date": end, "limit_up": n, "final": bool(final)})
        changed = True
    elif not cur.get("final"):
        changed = (int(cur.get("limit_up") or 0) != n) or (bool(final) and not cur.get("final"))
        cur["limit_up"] = n
        cur["final"] = bool(final)
    else:
        changed = False                       # 已是收盘终值 → 不再被盘中读数改写
    if changed:
        hist.sort(key=lambda x: x["date"])
        hist = hist[-250:]
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(hist, f, ensure_ascii=False)
        except OSError:
            pass
    return hist[-days:]


def fix_limit_up_count(date: str, n: int) -> bool:
    """对账修复：把 zt_history 里某日的涨停家数改成 n（收盘复盘口径）。

    为什么需要单独一个函数：`fetch_limit_up_history` 只在 `end == 传入日期`
    时才会去更新那一行，而**过去的日子不会再被当成 end 传进来** ——
    一旦当日首扫写错（实测 20261012 写成 16、收盘其实 46），
    靠正常路径永远修不回来。
    """
    if not date or n <= 0:
        return False
    path = paths.data_path("zt_history.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            hist = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    cur = next((h for h in hist if isinstance(h, dict) and h.get("date") == date), None)
    if cur is None:
        hist.append({"date": date, "limit_up": int(n), "final": True})
    elif int(cur.get("limit_up") or 0) == int(n) and cur.get("final"):
        return False
    else:
        cur["limit_up"] = int(n)
        cur["final"] = True
    hist.sort(key=lambda x: x.get("date", ""))
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hist[-250:], f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def market_amount_history(days: int = 40) -> list[dict]:
    """沪指日K（近 N 日），用于量能趋势。

    源优先级与个股日K同一套：**通达信（TCP 不被 IP 封）→ 腾讯 → 新浪**。
    量能分位/环比全靠这条数据（见 engine._relative_volume），多一层首选源
    就多一层保险 —— 腾讯一限流，量能判定就只剩新浪一条腿。
    """
    bars = _kline_from_tdx_index("000001", days + 5)
    if not bars:
        bars = _kline_from_tx("sh000001", days + 5)
    if not bars:
        bars = _kline_from_sina("sh000001", days + 5)
    if not bars:
        return []
    return bars[-days:]


# ---------------------------------------------------------------- 本地涨停池归档
# 池接口忽略 date 入参、无法回溯历史，因此「昨日涨停池 / 老龙档案」只能靠
# 本地逐日归档积累（与 zt_history.json 同一思路）。

POOL_HIST_DIR = paths.data_path("pool_history")

# 允许覆盖的"更完整"倍数：新池条数 ≥ 旧池 × 此值才覆盖。
# 1.15 的由来：盘中抖动（排除了 ST / 异常行）通常只差几只；而"盘中早扫的
# 部分池"与"当日全部涨停"差的是数量级（实测 16 → 46，2.9 倍）。
_POOL_HIST_GROW = 1.15


# 覆盖归档时要**保留老行字段值**的键（新行只借"成员集合"）。
# 为什么：两个源的字段口径不同，实测（九阳股份 20261009）——
#   · 开盘红复盘：break_count **恒为 0**（真实炸 6 次）、换手 56.29%、
#     流通市值 41.2 亿
#   · 东财涨停池：break_count 6、换手 18.28%、流通市值 126.8 亿
# 用 K 线独立核对：10-09 成交 139.2 万手 ÷ 流通股本 7.62 亿股 = 18.3%
# → **东财是对的，复盘的换手与市值大 ~3.08 倍**（股本基数不同）。
# 复盘的价值在**覆盖完整**（10-12 它给 46 只、东财当时只存了 16 只），
# 所以：成员取复盘、字段值取东财 —— 否则会"补全了票数、修坏了换手与炸板次数"，
# 而 E7 的形态判定（缩量一字 / 炸板回封）正靠这两个字段。
_POOL_FIELD_KEEP = ("name", "sector", "reason", "turnover", "float_mv",
                    "break_count", "fund", "first_time", "last_time",
                    "price", "pct")


def _merge_pool_rows(old: list[dict], new: list[dict]) -> list[dict]:
    """以 new 的成员为准，同代码行优先保留 old 里**有值**的字段。"""
    old_by = {r.get("code"): r for r in (old or []) if r.get("code")}
    out: list[dict] = []
    for row in new:
        m = dict(row)
        o = old_by.get(row.get("code"))
        if o:
            for k in _POOL_FIELD_KEEP:
                v = o.get(k)
                if v not in (None, "", 0, 0.0):
                    m[k] = v
        out.append(m)
    return out


def pool_history_prev_date(date: str) -> str:
    """比 date 更早的、最近一个有归档的交易日（无则空串）。"""
    ds_ = [d for d in pool_history_dates() if d < date]
    return ds_[-1] if ds_ else ""


def pool_history_duplicates(date: str, pool: list[dict]) -> bool:
    """新池是否与**上一个已归档交易日**完全同批（代码集合相同）。

    连续两天涨停池完全相同在真实市场里几乎不可能出现；出现即说明拿到的是
    **上一交易日的池**。实测（2026-10-13）：本地 `[20261013]` 的 46 只代码
    集合与「复盘接口查 20261012」**完全相同**，而且里面还含
    **当天已停牌的新华传媒** —— 它不可能出现在今天的涨停池里，是硬证据。
    """
    prev = pool_history_prev_date(date)
    if not prev or not pool:
        return False
    old = {r.get("code") for r in load_pool_history(prev) if r.get("code")}
    new = {r.get("code") for r in pool if r.get("code")}
    return bool(old) and old == new


def save_pool_history(date: str, pool: list[dict], qdate: str = "",
                      force: bool = False) -> bool:
    """把某交易日的涨停池归档到本地（M9 次日验证 / 老龙档案的数据底座）。

    返回 True 表示确实写了盘。★ 两道门禁（2026-10-13 实测教训，
    与「盘前指数快照污染今日高水位」是同一类错误：拿错时点的数据当某一天用）：

    ① **日期归属**（qdate）：池接口会**忽略 date 入参**、始终返回"最近一个
       有数据的交易日"（见 _fetch_pool 说明）。所以盘前扫描拿到的其实是
       **上一交易日**的池 —— 直接按 real_date 落盘就把昨天记成了今天。
       实测：本地 pool_history/20261013.json 与「接口查 20261012」的复盘
       **是同一批 46 只**（代码集合完全相同），而 10-13 的池当时还没生成
       （接口查 10-13 返回 0 只）。传入接口自报的 qdate 后，日期不一致即拒写。

    ② **覆盖率**：原实现是"已有同日存档就不覆盖"，本意防盘中多次扫描互相
       打架，但它把**盘中早期的残缺快照永久钉死**了 —— 实测 20261012 只存了
       16 只（当日早扫时的部分池），当天真实涨停 46 只，却因为"不覆盖"，
       后来那次完整的扫描白跑，残缺再也修不回来（并直接污染了 M9 的
       "昨日涨停池/老龙档案"与 E6 的题材取值）。
       现在改为：**新池明显更完整（≥_POOL_HIST_GROW 倍）才允许覆盖**，
       否则保持原样 —— 既挡住抖动，又能自愈残缺。
    """
    if not date or not pool:
        return False
    if qdate and str(qdate) != str(date):
        return False                    # 拿到的是别的交易日的池，不写
    if not force and pool_history_duplicates(date, pool):
        return False                    # 与上一交易日完全同批 = 错日期的池，不写
    try:
        os.makedirs(POOL_HIST_DIR, exist_ok=True)
        path = os.path.join(POOL_HIST_DIR, f"{date}.json")
        old = load_pool_history(date)
        if old and not force and len(pool) < len(old) * _POOL_HIST_GROW:
            return False                # 没更完整 → 保持原样（防盘中抖动打架）
        if old:
            pool = _merge_pool_rows(old, pool)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"date": date, "count": len(pool), "pool": pool},
                      f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


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

# 东财 push2 有多台镜像，clist 接口偶发 RemoteDisconnected —— 逐台轮换提高成功率。
# v1.17.0 增补 push2delay（延时行情域名）：实测其 ulist.np 路径与主站相互独立
# （主站盘中被路径风控时它仍可达）；clist 在该域名上同样可能被掐，
# 且量级风控触发时全家族一起歇 —— 所以它是「多一次独立重试机会」，不是免死金牌。
EM_PUSH2_HOSTS = ["push2.eastmoney.com", "1.push2.eastmoney.com",
                  "82.push2.eastmoney.com", "push2delay.eastmoney.com"]

# ── push2 限速闸门 ──────────────────────────────────────────────────────
# 实测（2026-10-09）：东财对**行情列表类**接口（clist/get、ulist.np/get）的
# 风控是按「单位时间的请求量」触发的，而且一旦触发是**整个 push2 家族一起 RST**
# —— push2 / 1.push2 / 82.push2 / push2his / push2delay 全灭，连查单只股票都
# 取不到，持续数分钟。换镜像主机完全没用（教训：多镜像≠多额度）。
# 所以这里做两件事：
#   ① 串行 + 最小间隔 + 抖动，把突发削平（原来 8 个 worker 并发轰是主因）
#   ② 被 RST 时记一次**家族级**冷却，所有 push2 调用一起退避，
#      不再让每个接口各自撞一遍（那样只会把冷却越撞越长）
PUSH2_MIN_INTERVAL = 0.35      # 同族请求最小间隔（秒）
PUSH2_COOLDOWN = 240           # 被掐后家族级冷却上限（v1.19：900 → 240）
PUSH2_PROBE_AFTER = 60         # 冷却开始多久后放行一次「半开探测」
PUSH2_BUDGET = 15              # 滑动窗口预算：60 秒内最多多少次 push2 请求
_p2_lock = threading.Lock()
_p2_last = 0.0
_p2_down_until = 0.0
_p2_probe = 0.0
_budget_log: list[float] = []
_budget_skip = 0               # 被预算挡下的次数（数据源面板展示用）


def _push2_allow() -> bool:
    """滑动窗口预算：60 秒窗口内还有额度才放行。

    东财是按「单位时间请求量」限流的 —— 等被 RST 再退避已经晚了（会连坐
    整个 push2 家族好几分钟，页面一串"冷却中"）。主动设预算：额度用完就
    **不发请求、不记失败**，直接让调用方走降级源 —— 从根源上不触发风控。
    """
    global _budget_skip
    now = time.time()
    with _p2_lock:
        _budget_log[:] = [t for t in _budget_log if now - t < 60.0]
        if len(_budget_log) >= PUSH2_BUDGET:
            _budget_skip += 1
            return False
        _budget_log.append(now)
        return True


def _push2_gate() -> bool:
    """push2 请求前的闸门。False = 冷却中或预算用尽，调用方应走降级。"""
    global _p2_last, _p2_probe
    now = time.time()
    if now < _p2_down_until:
        # 冷却中：每隔 PUSH2_PROBE_AFTER 放行一次半开探测 —— 东财恢复后
        # 几十秒内自己就能回来，不用干等满冷却（原来最坏要等 15 分钟）
        if now - _p2_probe >= PUSH2_PROBE_AFTER:
            _p2_probe = now
        else:
            return False
    if not _push2_allow():
        return False
    with _p2_lock:
        gap = PUSH2_MIN_INTERVAL - (time.time() - _p2_last)
        if gap > 0:
            time.sleep(gap + random.uniform(0.0, 0.08))
        _p2_last = time.time()
    return True


def _push2_trip(seconds: int = PUSH2_COOLDOWN) -> None:
    """记一次家族级冷却（被 RST / 限流时调用）。"""
    global _p2_down_until
    _p2_down_until = max(_p2_down_until, time.time() + seconds)


def _push2_recover() -> None:
    """一次成功就清零家族冷却（半开探测成功即恢复）。"""
    global _p2_down_until, _p2_probe
    _p2_down_until = 0.0
    _p2_probe = 0.0


def push2_status() -> dict:
    """给「数据源」面板看：冷却剩余、每分钟预算用量、代理出口状态。"""
    now = time.time()
    with _p2_lock:
        _budget_log[:] = [t for t in _budget_log if now - t < 60.0]
        used = len(_budget_log)
    px = _sys_proxy().get("https") or _sys_proxy().get("http") or ""
    proxy_on = bool(px) and now >= _PROXY_BAD_UNTIL
    return {"cooling_seconds": max(0, round(_p2_down_until - now)),
            "min_interval": PUSH2_MIN_INTERVAL,
            "hosts": len(EM_PUSH2_HOSTS),
            "budget_used": used, "budget_max": PUSH2_BUDGET,
            "budget_skipped": _budget_skip,
            "proxy": (px.split("//")[-1] if px else ""),
            "proxy_on": proxy_on}


def _push2_json(path: str, referer: str = "https://quote.eastmoney.com/") -> dict:
    """东财 push2 接口（clist / ulist.np）：镜像轮换 + 限速 + 接口级熔断。

    熔断命中时直接返回 {}，由调用方走自己的降级链路 —— 不再空等 3 台镜像各自超时。
    """
    key = _api_key(path)
    if time.time() < _em_down_until.get(key, 0):
        return {}
    if not _push2_gate():          # 家族级冷却中，别浪费请求
        return {}
    soft = False
    for host in EM_PUSH2_HOSTS:
        if not _push2_gate():
            break
        try:
            raw = _get(f"https://{host}{path}", timeout=12, referer=referer).decode(
                "utf-8", errors="ignore")
            js = json.loads(raw)
            with _em_lock:
                _em_fail.pop(key, None)
                _em_down_until.pop(key, None)
            _push2_recover()         # 半开探测/正常请求成功 → 家族冷却清零
            return js
        except Exception as e:
            if not _is_hard_block(e):
                soft = True          # 有超时类失败 → 可能只是网络抖动，别急着熔断
                continue
            # ★ 硬失败（RemoteDisconnected / RST / SSLError）= 整个家族把我们掐了，
            #   换镜像没意义 —— 4 台都在同一个族里。立即拉冷却并**跳出循环**，
            #   不要接着把剩下 3 台也各撞一次。
            #   实测（2026-10-14）：不 break 时 fetch_breadth 冷取要 2.5 秒、
            #   烧掉 5 个预算位（实测 budget_used 0→5），而这一路本来就注定
            #   失败、后面还有免费备用口径。break 之后同样情形降到 0.4 秒、
            #   0 个预算位。全市场扫描每次都要经过这里，这 2 秒是白丢的。
            _push2_trip(min(60 * (2 ** min(_em_fail.get(key, 0), 2)), PUSH2_COOLDOWN))
            break
    with _em_lock:
        n = _em_fail.get(key, 0) + 1
        _em_fail[key] = n
        if not soft or n >= EM_BLOCK_MIN_FAILS:
            _em_down_until[key] = time.time() + min(
                EM_BLOCK_BASE * (2 ** (n - 1)), EM_BLOCK_MAX)
    # 家族级冷却：行情列表接口被掐时，整个 push2 一起歇一会儿
    # 60 / 120 / 240 秒三档封顶（原来 180→900 秒，太狠）
    _push2_trip(min(60 * (2 ** min(_em_fail.get(key, 1) - 1, 2)), PUSH2_COOLDOWN))
    return {}


def _breadth_fallback() -> dict:
    """push2 拿不到涨跌家数时的备用口径 —— 开盘红实时情绪（独立链路）。

    返回结构与 fetch_breadth 一致，多一个 source='kph' 供界面如实标注。
    """
    try:
        from . import kph as _kph
        em = _kph.market_emotion()
    except Exception:
        return {}
    up = int(em.get("up") or 0)
    down = int(em.get("down") or 0)
    flat = int(em.get("flat") or 0)
    if up + down <= 0:
        return {}
    total = up + down + flat
    return {"up": up, "down": down, "flat": flat, "total": total,
            "up_ratio": round(up / total, 3),
            "up_down_ratio": round(up / down, 2) if down else 0.0,
            "source": "kph"}


def _push2_cheap_skip() -> bool:
    """现在打 push2 是否注定白打（家族冷却中 / 一分钟预算已用尽）。

    只做判断、不发请求。给「有免费备用口径」的调用方用：明知打不通就走备用，
    省下 4 台镜像各轮一遍的时间（实测 3 秒）与预算位。
    """
    if time.time() < _p2_down_until:
        return True
    now = time.time()
    with _p2_lock:
        _budget_log[:] = [t for t in _budget_log if now - t < 60.0]
        return len(_budget_log) >= PUSH2_BUDGET


def fetch_breadth(date: str = "", ttl: int = 60) -> dict:
    """全市场涨跌家数 {up, down, flat, ...}。取不到返回空 dict。

    push2 拿不到时（被限流 / 预算用尽）退**开盘红实时情绪**——它直接给
    涨跌家数，是独立于东财的一条路；结果带 source 字段供界面如实标注。
    两条路都不通时，回退**当日最后一份好数据**（涨跌家数偶尔丢失会让
    情绪温度的第 4 项退化，不该因此把整根温度计算歪）。

    ★ 2026-10-14 提速：进 push2 之前先看家族是不是在冷却/预算用尽。
      实测被东财掐住时这一路要 **3.01 秒**才降级（4 台镜像各轮一遍，烧掉
      5 个预算位），而备用口径 _breadth_fallback 只要 0.24~0.8 秒、且完全
      不碰 push2。冷却期明知打不通还去打，纯粹是拿扫描时间换白等 ——
      所以冷却/超预算时**直接走备用**，把那个 3 秒省下来。
    """
    def producer():
        if not _push2_cheap_skip():
            js = _push2_json("/api/qt/ulist.np/get?fltt=2&invt=2"
                             "&secids=1.000001,0.399001&fields=f12,f14,f104,f105,f106")
            diff = (js.get("data") or {}).get("diff") or []
            if diff:
                up = down = flat = 0
                for it in diff:
                    up += int(_num(it.get("f104")))
                    down += int(_num(it.get("f105")))
                    flat += int(_num(it.get("f106")))
                total = up + down + flat
                if total > 0:
                    return True, {"up": up, "down": down, "flat": flat, "total": total,
                                  "up_ratio": round(up / total, 3),
                                  "up_down_ratio": round(up / down, 2) if down else 0.0,
                                  "source": "em"}
        fb = _breadth_fallback()
        if fb:
            return True, fb
        return False, None

    return _fetch_memo("breadth", date or last_trade_date(), ttl, producer, empty={})


# ---------------------------------------------------------------- 主力资金流
# 参考 GitHub 项目 a-stock-data / slingshot 的「资金面」口径：
# 东财 push2 的资金流字段（f62 主力净额、f184 主力净占比、f66/72/78/84 四档）
# 免鉴权、可批量 —— 给候选股与行业板块各补一条「钱往哪走」的线。
#
# ★★ 2026-10-14 实测纠错（v1.35.2）：本段原先写着「clist 被按 URL 路径风控、
#    而同一个 push2 服务的 ulist.np 完全可用」，据此把资金流整体改走 ulist.np。
#    这个前提**已经反了**，且据它做的绕行方案全是白做的功夫：
#      · clist        —— 通。实测拿到板块 496 个、个股 5562 只，四档齐全。
#      · ulist.np     —— 4 台镜像全灭，一次都没成功过
#                        （push2 / 1.push2 / 2.push2 / push2delay 全是
#                         RemoteDisconnected）。
#    后果是实打实的：`fetch_stock_moneyflow()` 对 100 只票返回 **0 条**，
#    `fetch_board_moneyflow()` 一路降级到财联社 —— 而财联社那份只有主力净额，
#    没有四档、没有主力净占比（main_pct 恒为 None）。
#    所以现在把主链路改回 clist，ulist.np 降为备用（不删，万一哪天又反过来）。
#
#    还有一条必须一起记的教训：这一轮为了摸清接口，用**裸 urllib 绕过
#    `_push2_gate`** 连打了十几次，直接把本机 IP 打进 push2 家族封禁
#    （clist / ulist.np / push2his 全 RST，等 4 分钟 + 90 秒都没恢复；
#    同期 datacenter-web 与 webpage 域仍正常，说明封的是 push2 这个族）。
#    → 排查东财接口时**不要绕闸门**，PUSH2_BUDGET 那个滑动窗口预算就是防这个的。

_FLOW_FIELDS = "f2,f3,f12,f14,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87"

# clist 的市场筛选表达式：沪深主板 + 创业板 + 科创板 + 北交所。
# 与 ulist.np 按 secid 点名不同，clist 用 fs= 描述「要哪个市场段」，
# 一次返回一整段并支持分页 —— 这是它比 ulist.np 强的地方。
_FS_ALL_STOCK = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
_FS_ALL_BOARD = "m:90+t:2,m:90+t:3"      # t:2=行业板块、t:3=概念板块


def _mk_code(code: str) -> str:
    """纯 6 位代码 → 带市场前缀（sh/sz/bj）。clist 只给 f12 裸代码。"""
    if code.startswith(("43", "83", "87", "88", "92")):
        return "bj" + code
    if code.startswith(("6", "9", "5")):
        return "sh" + code
    return "sz" + code


def _flow_row(it: dict) -> dict:
    """clist/ulist 的一行 → 资金流字段。两个接口字段名完全一致。"""
    return {"main": _num(it.get("f62")), "main_pct": _num(it.get("f184")),
            "super": _num(it.get("f66")), "big": _num(it.get("f72")),
            "mid": _num(it.get("f78")), "small": _num(it.get("f84"))}


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
    """批量个股主力资金流 {code: {main,main_pct,super,big,mid,small}}，单位元。

    走 clist 的**全市场排行**接口按页取，而不是 ulist.np 按 secid 点名取 ——
    实测 ulist.np 已全灭，clist 反而通（见本段顶部 2026-10-14 的纠错记录）。

    取法：按主力净额降序翻页拉全市场，再按调用方给的 code 集合筛。
    这样一次请求 500 只，全市场 ~6000 只只要 12 页，比按 50 只一组点名
    （120 个请求）省一个数量级 —— 而 push2 的预算是 60 秒 15 次。
    代价是拿到了用不上的票，但网络开销远小于请求数的开销。

    只想要很少几只时（< 一页）反而点名更划算，所以保留 ulist.np 作备用：
    小集合先试 clist，拿不到再退回 ulist.np。
    """
    if not codes:
        return {}
    want = set(codes)

    # ① 主链路：clist 分页拉全市场，按 want 筛
    #
    # ★ 判断「clist 到底通没通」必须靠 saw_data，不能靠 out 是否为空（v1.35.2 修）：
    #   曾经写成 `if out: return out` —— 一旦 want 里的票一只都没匹配上
    #   （代码写错、或那几只票当天没进排行），out 就是空的，于是白白翻满
    #   20 页之后、还要再往下降级打一轮 ulist.np。实测 20 次请求全废。
    #   接口返回了数据 = 链路是好的，没匹配上是用输入的问题，不该降级。
    out: dict[str, dict] = {}
    saw_data = False
    page_size = 500
    max_pages = 20                      # 20 × 500 = 10000 只，覆盖全市场有余
    total = None                        # 接口报的全市场总数，用来判断是否翻到底
    for pn in range(1, max_pages + 1):
        js = _push2_json(
            f"/api/qt/clist/get?pn={pn}&pz={page_size}&po=1&np=1&fltt=2&invt=2"
            f"&fid=f62&fs={_FS_ALL_STOCK}&fields={_FLOW_FIELDS}")
        data = js.get("data") or {}
        diff = data.get("diff") or []
        if not diff:
            break
        saw_data = True
        if total is None:
            total = data.get("total")
        for it in diff:
            code = str(it.get("f12", ""))
            if not code:
                continue
            key = _mk_code(code)
            if key not in want:
                continue
            out[key] = _flow_row(it)
        if len(out) >= len(want):
            break                       # 要的票都齐了，不必再翻
        # 已翻到最后一页就停 —— 不看 total 的话，某些镜像会把同一页
        # 反复吐回来（或被 mock/缓存劫持），白翻满 max_pages 次。
        if total and pn * page_size >= int(total):
            break
    if saw_data:
        return out                      # 链路通 —— 就算一只没匹配上也不降级

    # ② 备用：ulist.np 按 secid 点名（2026-10-14 实测不可用，留着以防反转）
    batch = 50
    chunks = [codes[i:i + batch] for i in range(0, len(codes), batch)]

    def work(chunk):
        secids = ",".join(_meta_of(c) for c in chunk)
        js = _push2_json("/api/qt/ulist.np/get?fltt=2&invt=2"
                         f"&secids={secids}&fields={_FLOW_FIELDS}")
        return [(_mk_code(str(it.get("f12", ""))), _flow_row(it))
                for it in ((js.get("data") or {}).get("diff") or [])
                if it.get("f12")]

    with ThreadPoolExecutor(max_workers=8) as pool:
        for rows in pool.map(work, chunks):
            for code, d in rows:
                out[code] = d
    return out


# ---------------------------------------------------------------- 板块数据
# 东财 clist 接口被**按 URL 路径**风控（三台镜像全部 RemoteDisconnected，实测
# 2026-10-09 复现），但同一个 push2 服务的 ulist.np 完全可用、且字段一模一样。
# 所以板块这块整体改走 ulist.np：
#   ① 板块清单 —— 枚举 90.BKxxxx 号段自己发现（17 个请求扫出 731 个板块，1 秒）
#   ② 板块资金流 —— ulist.np 批量一次拿全（含领涨股 f204/f205）
#   ③ 板块成分股 —— 用个股的 f100(所属行业)/f103(所属概念) 自建成员表
# 三者都落盘，日常运行零 clist 依赖。

BOARD_LIST_PATH = paths.data_path("board_list.json")
BOARD_MEMBERS_PATH = paths.data_path("board_members.json")
_BOARD_LIST_TTL = 7 * 86400        # 板块代码变动极少，一周扫一次足够
_BOARD_RETRY_AFTER = 3600          # 刷新失败后的重试间隔
_BOARD_MEMBERS_TTL = 3 * 86400     # 成分表三天重建一次
_BOARD_MEMBERS_MAX_CODES = 1200    # 成员表只扫前 N 只，别为它打上百个请求

# 号段：90.BK0001 ~ 90.BK1399 覆盖东财全部行业 + 概念板块
_BOARD_CODE_RANGE = range(1, 1400)
_BOARD_SECTYPE = 90


def _ulist_secids(secids: list[str], fields: str, batch: int = 80,
                  workers: int = 6) -> list[dict]:
    """ulist.np 批量取数 —— clist 的可用替代（同源、同字段、免鉴权）。

    一次请求塞 80 个 secid；实测比 clist 更稳，也没有路径风控。
    """
    if not secids:
        return []
    chunks = [secids[i:i + batch] for i in range(0, len(secids), batch)]

    def work(chunk: list[str]) -> list[dict]:
        js = _push2_json("/api/qt/ulist.np/get?fltt=2&invt=2&secids="
                         + ",".join(chunk) + "&fields=" + fields)
        return (js.get("data") or {}).get("diff") or []

    out: list[dict] = []
    if len(chunks) == 1:
        return work(chunks[0])
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for part in pool.map(work, chunks):
            out.extend(part)
    return out


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _write_json(path: str, obj) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def _discover_boards() -> dict[str, str]:
    """枚举 90.BKxxxx 号段 → {BK代码: 板块名}。

    板块清单本来靠 clist（fs=m:90+t:2），但该路径已被风控；改用 ulist.np
    按号段探——服务端对不存在的 secid 直接不返回，等于免费帮我们做了筛选。
    """
    codes = [f"{_BOARD_SECTYPE}.BK{n:04d}" for n in _BOARD_CODE_RANGE]
    found: dict[str, str] = {}
    for it in _ulist_secids(codes, "f12,f14", batch=80, workers=6):
        bk, name = str(it.get("f12", "")), it.get("f14", "")
        if bk and name:
            found[bk] = name
    return found


def _seed_boards() -> dict[str, str]:
    """随包发布的板块代码种子表（零请求）。"""
    try:
        from . import board_seed
        return dict(board_seed.BOARDS)
    except Exception:
        return {}


def fetch_board_codes(force: bool = False) -> dict[str, str]:
    """{BK代码: 板块名}。

    优先级：磁盘缓存（7 天内）→ 联网枚举刷新 → 磁盘旧值 / 随包种子。
    种子表让**首启与换机都是零请求**；联网枚举只在缓存过期时做一次，
    且受 push2 限速闸门约束（~18 个请求 ≈ 6s），失败就退回种子，不硬撞。
    """
    now = time.time()
    disk = _read_json(BOARD_LIST_PATH)
    ts = _num(disk.get("_ts", 0))
    boards = disk.get("boards") or {}
    if boards and not force and now - ts < _BOARD_LIST_TTL:
        return boards

    if not boards and not force:
        # 首启 / 换机：磁盘上什么都没有。直接用随包种子，**零请求**秒回，
        # 并把时间戳摆成"1 小时后再刷新"——没必要为了板块清单在启动时
        # 打 18 个请求（那正是触发限速的典型动作）。
        seed = _seed_boards()
        if seed:
            _write_json(BOARD_LIST_PATH,
                        {"_ts": now - _BOARD_LIST_TTL + _BOARD_RETRY_AFTER,
                         "boards": seed})
            return seed

    fresh = _discover_boards()
    if fresh:
        _write_json(BOARD_LIST_PATH, {"_ts": now, "boards": fresh})
        return fresh

    fallback = boards or _seed_boards()
    if fallback:
        # 刷新失败：把时间戳往前推，让下次重试落在 1 小时后，
        # 而不是每轮扫描都去撞一次（那只会把限速撞得更死）
        _write_json(BOARD_LIST_PATH,
                    {"_ts": now - _BOARD_LIST_TTL + _BOARD_RETRY_AFTER,
                     "boards": fallback})
    return fallback


def fetch_board_list() -> dict[str, str]:
    """{板块名: BK代码} —— 题材名 → 板块代码的映射表（mainline 用）。"""
    return {name: bk for bk, name in fetch_board_codes().items() if name}


# ── 新浪板块（独立主机，push2 被掐时的兜底）──────────────────────────────
# 新浪的板块接口在另一台主机、另一套风控，push2 家族被 RST 时它照样能用。
# 代价：只有涨跌幅/成交额/领涨股，**没有主力净额**（那是东财独有口径）。
# 所以它是「面板不至于空掉」的兜底，主口径仍然是东财。
_SINA_HY = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
_SINA_GN = ("https://vip.stock.finance.sina.com.cn/q/view/newFLJK.php"
            "?param=class")
_SINA_REF = "https://finance.sina.com.cn/"


def fetch_sina_boards(kind: str = "all") -> list[dict]:
    """新浪行业/概念板块 → [{name,pct,amount,lead,lead_code}]。

    行格式（实测）：代码,名称,家数,均价,涨跌额,涨跌幅,成交量,成交额,
                   领涨股代码,领涨股涨跌幅,...,领涨股名
    """
    urls = []
    if kind in ("all", "industry"):
        urls.append(_SINA_HY)
    if kind in ("all", "concept"):
        urls.append(_SINA_GN)

    out: list[dict] = []
    for url in urls:
        try:
            raw = _get(url, timeout=12, referer=_SINA_REF).decode("gbk", "ignore")
        except Exception:
            continue
        body = raw.split("=", 1)[-1].strip().rstrip(";")
        try:
            obj = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            continue
        for _, line in (obj or {}).items():
            p = str(line).split(",")
            if len(p) < 13:
                continue
            name = p[1].strip()
            if not name:
                continue
            out.append({
                "name": name,
                "pct": round(_num(p[5]), 2),
                "amount": _num(p[7]),
                "lead": p[12].strip(),
                "lead_code": p[8].strip(),
            })
    return out


def fetch_board_moneyflow(limit: int = 30) -> list[dict]:
    """板块资金流排行（按主力净额降序），**行业 + 概念一起排**。
    返回 [{code,name,pct,main,main_pct,lead,lead_code,source}]，main 单位元。

    短线真正驱动行情的是概念板块，原来只排行业板块反而漏掉了主线；
    现在同一批请求把两类都拿回来，主升题材能直接冲到榜首。

    东财被风控时**先退财联社**（同样有主力净额），再退新浪：那条链路
    没有主力净额（main=None），但涨跌幅/成交额/领涨股都有，面板至少
    能告诉你钱在往哪个方向走。三条路都带 source 供界面如实标注。
    """
    key = f"board_flow:{limit}"
    now = time.time()
    hit = _cache.get(key)
    if hit and now - _cache_ts.get(key, 0) < 600:      # 10 分钟：少打接口
        return hit

    rows = (_board_flow_em(limit) or _board_flow_cls(limit)
            or _board_flow_sina(limit))
    if rows:
        _cache[key] = rows
        _cache_ts[key] = now
    return rows


def _board_flow_em(limit: int) -> list[dict]:
    """东财口径：带主力净额。走 clist 一次拿全部板块，受 push2 限速闸门约束。

    ★ 2026-10-14 改：原先这里是把 `fetch_board_codes()` 枚举出来的 BK 号段
      拼成 secids 交给 ulist.np 打分。现在 ulist.np 全灭，而 clist 的
      `fs=m:90+t:2,m:90+t:3` 直接就能返回**全部**行业+概念板块（实测 496 个），
      还自带领涨股 f204/f205 —— 一次请求顶原来十几个，也不再依赖那份
      自建的 board_list.json（枚举 1400 个号段扫出来的）。

    保留 fetch_board_codes 作为降级：clist 拿不到时，退回按号段点名。
    """
    out = []
    saw_data = False                    # 同 fetch_stock_moneyflow：靠这个判通断
    for pn in (1, 2, 3):                # 500 × 3 = 1500，覆盖 496 个板块有余
        js = _push2_json(
            f"/api/qt/clist/get?pn={pn}&pz=500&po=1&np=1&fltt=2&invt=2&fid=f62"
            f"&fs={_FS_ALL_BOARD}&fields=f12,f14,f3,f62,f184,f204,f205")
        diff = (js.get("data") or {}).get("diff") or []
        if not diff:
            break
        saw_data = True
        for it in diff:
            out.append({"code": str(it.get("f12", "")),
                        "name": it.get("f14", ""),
                        "pct": _num(it.get("f3")),
                        "main": _num(it.get("f62")),
                        "main_pct": _num(it.get("f184")),
                        "lead": it.get("f204", ""),
                        "lead_code": it.get("f205", ""),
                        "source": "em"})
    if not saw_data:
        out = _board_flow_em_ulist(limit)
    out.sort(key=lambda r: -(r["main"] or 0))
    return out[:limit]


def _board_flow_em_ulist(limit: int) -> list[dict]:
    """降级：按自建板块清单逐个点名（原实现，clist 不可用时才走）。"""
    codes = fetch_board_codes()
    if not codes:
        return []
    secids = [f"{_BOARD_SECTYPE}.{bk}" for bk in codes]
    diff = _ulist_secids(secids, "f12,f14,f3,f62,f184,f204,f205",
                         batch=80, workers=2)
    out = []
    for it in diff:
        out.append({"code": str(it.get("f12", "")),
                    "name": it.get("f14", ""),
                    "pct": _num(it.get("f3")),
                    "main": _num(it.get("f62")),
                    "main_pct": _num(it.get("f184")),
                    "lead": it.get("f204", ""),
                    "lead_code": it.get("f205", ""),
                    "source": "em"})
    return out


def _board_flow_cls(limit: int) -> list[dict]:
    """财联社口径：push2 被限流时的**中间层**——有主力净额（新浪兜底没有）。

    东财 ulist.np 给 f62、财联社给 main_fund_diff，两者同口径的另一条路；
    板块名是财联社口径（"影视院线"等），与东财行业名略有出入但语义一致。
    """
    try:
        from . import cls as _cls
        rows = _cls.sector_industry()
    except Exception:
        return []
    out = []
    for it in rows:
        if not it.get("name"):
            continue
        out.append({"code": "", "name": it["name"], "pct": it.get("pct"),
                    "main": it.get("main"), "main_pct": None,
                    "lead": it.get("lead"), "lead_code": "", "source": "cls"})
    out.sort(key=lambda r: -(r["main"] or 0))
    return out[:limit]


def _board_flow_sina(limit: int) -> list[dict]:
    """新浪兜底：没有主力净额，按成交额降序（钱多的地方=关注度高）。"""
    rows = fetch_sina_boards("all")
    out = []
    for b in rows:
        out.append({"code": "", "name": b["name"], "pct": b["pct"],
                    "main": None, "main_pct": None,
                    "lead": b["lead"], "lead_code": b["lead_code"],
                    "amount": b["amount"], "source": "sina"})
    out.sort(key=lambda r: -(r.get("amount") or 0))
    return out[:limit]


def _sample_codes_by_market(codes: list[str], limit: int) -> list[str]:
    """从全市场代码里抽 limit 只，**按市场占比分层**，并保留原始顺序。

    ★ 不能直接 `codes[:limit]`（v1.32.3 修）：`load_valid_codes()` 返回的是**排序后**
      的名单，按 `bj < sh < sz` 排；实测 6173 只取前 1200 = 沪 598 + 北 602 +
      **深 0** —— 深市一只都进不了板块成分表。后果是 `board_members` 里
      深市板块（几乎是全部题材）拿不到任何成分股，S3 中军定容量、S6 厚度
      只能一路走"弱验证/数据不足"，用户看到的是「查不到中军」「板块数据不足」。

    抽样而不是取头部：成分表的用途是判断"板块内有没有百亿大票在动"，
    均匀覆盖每个市场的头尾比只取排序靠前的代码更有代表性。
    市场占比取整后至少留 1 只，避免小众市场（北交所）被完全抹掉。
    """
    if limit <= 0 or len(codes) <= limit:
        return list(codes)
    buckets: dict[str, list[str]] = {}
    for c in codes:
        buckets.setdefault((c or "")[:2], []).append(c)
    picked: set[str] = set()
    for mk in sorted(buckets):
        arr = buckets[mk]
        k = max(1, int(round(limit * len(arr) / len(codes))))
        step = max(1, len(arr) // k)
        picked.update(arr[::step][:k])
    # 按原始顺序返回，保证抽样结果稳定可复现
    return [c for c in codes if c in picked][:limit]


def _board_members_index() -> dict[str, list[dict]]:
    """{板块名: [成员...]}，由全市场个股的 f100/f103 自建。

    clist 的成分股接口（fs=b:BKxxxx）同样被风控；换个思路——个股自己就带着
    「所属行业」(f100) 和「所属概念」(f103) 两个字段，把全市场扫一遍按板块
    归类，等于自己拼出成分表。附带好处：**概念板块也有成分股了**，
    原来 clist 只喂行业板块，M9 中军验证遇概念题材只能走弱验证。

    落盘缓存：全市场 ~6000 只需 ~75 个请求（数秒），一天最多重建一次。
    """
    now = time.time()
    disk = _read_json(BOARD_MEMBERS_PATH)
    if disk.get("members") and now - _num(disk.get("_ts", 0)) < _BOARD_MEMBERS_TTL:
        return disk["members"]

    codes = _sample_codes_by_market(load_valid_codes() or [],
                                    _BOARD_MEMBERS_MAX_CODES)
    if not codes:
        return disk.get("members") or {}

    secids = [to_secid_from_code(c) for c in codes]
    diff = _ulist_secids(secids, "f12,f14,f3,f100,f103,f21",
                         batch=80, workers=2)
    members: dict[str, list[dict]] = {}
    for it in diff:
        raw = str(it.get("f12", ""))
        if not raw:
            continue
        name = it.get("f14", "")
        pct = _num(it.get("f3"))
        mv = _norm_mv_yi(_num(it.get("f21")))
        row = {"code": _with_market(raw), "name": name, "pct": pct,
               "float_mv": mv}
        for board in _split_boards(it.get("f103")):
            members.setdefault(board, []).append(row)
        ind = (it.get("f100") or "").strip()
        if ind:
            members.setdefault(ind, []).append(row)

    if members:
        _write_json(BOARD_MEMBERS_PATH, {"_ts": now, "members": members})
    return members or (disk.get("members") or {})


def _split_boards(v) -> list[str]:
    """f103 是逗号分隔的概念名串。"""
    if not v:
        return []
    return [s.strip() for s in str(v).replace("，", ",").split(",") if s.strip()]


def _with_market(num: str) -> str:
    """6 位代码补交易所前缀（北交所 4/8/92 开头）。"""
    if num.startswith(("4", "8")) or num.startswith("92"):
        return "bj" + num
    if num.startswith(("6", "9", "5")):
        return "sh" + num
    return "sz" + num


def to_secid_from_code(code: str) -> str:
    """内部代码（sh600519 / 600519）→ 东财 secid（1.600519 / 0.000001）。"""
    c = code
    if c.startswith(("sh", "sz", "bj")):
        c = c[2:]
    return ("1." if code.startswith(("sh", "6", "9", "5")) else "0.") + c


def fetch_board_constituents(bk_code: str) -> list[dict]:
    """板块成分股 [{code,name,pct,float_mv(亿)}]，按涨幅降序。

    走自建成员表（ulist.np + f100/f103），不再依赖被风控的 clist。
    """
    if not bk_code:
        return []
    name = fetch_board_codes().get(bk_code, "")
    if not name:
        return []
    rows = _board_members_index().get(name) or []
    if not rows:
        return []
    out = sorted(rows, key=lambda r: -(r.get("pct") or 0))
    return list(out)



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


def fetch_board_constituents_raw(bk_code: str) -> list[dict]:
    """（已废弃）原 clist 版成分股，保留仅作对照，不再被调用。"""
    js = _push2_json("/api/qt/clist/get?pn=1&pz=1000&po=1&np=1"
                     "&fltt=2&invt=2&fid=f3&fs=b:" + bk_code + "&fields=f12,f14,f3,f21",
                     EM_REFERER)
    out = []
    for it in (js.get("data") or {}).get("diff") or []:
        code = str(it.get("f12", ""))
        if not code:
            continue
        out.append({"code": _with_market(code), "name": it.get("f14", ""),
                    "pct": _num(it.get("f3")),
                    "float_mv": _norm_mv_yi(_num(it.get("f21")))})
    return out


# ---------------------------------------------------------------- 股票名称搜索
# 自选股原来只能输代码（用户 10-13 反馈：记不住代码，想直接搜名字）。
# 名称 → 代码 的解析放在这里，因为只有这一步需要联网：
#   ① 主源：腾讯 smartbox 联想接口（与行情同域，支持**名称子串**与**拼音首字母**）；
#   ② 兜底：本地名单 data/name_index.json —— 由全市场扫描的快照累积而成，
#      主源被限流/断网时仍可搜（覆盖范围 = 历史扫描过的代码，通常是全市场）。
# 写入路径（portfolio.watch_upsert）不改：前端先搜到代码，再按原接口提交。

SEARCH_URL = "https://smartbox.gtimg.cn/s3/?t=all&q={q}"
NAME_INDEX_PATH = paths.data_path("name_index.json")
_NAME_INDEX_LOCK = threading.Lock()

# smartbox 返回的是 \uXXXX 转义串，用正则还原（比 unicode_escape 稳：
# 后者会把已解码的多字节字符再拆一遍，中文名容易变乱码）。
_HINT_ESC = re.compile(r"\\u([0-9a-fA-F]{4})")


def _hint_unescape(s: str) -> str:
    return _HINT_ESC.sub(lambda m: chr(int(m.group(1), 16)), s or "")


def _parse_search_hint(raw: str) -> list[dict]:
    """解析 smartbox 的 v_hint 串 → [{code, name, pinyin, board}]（只留 A 股）。

    单条记录形如 `sh~600519~\\u8d35\\u5dde\\u8305\\u53f0~gzmt~GP-A`，
    字段依次是 市场 / 代码 / 名称 / 拼音首字母 / 类型，记录之间用 `^` 分隔。
    港股（GP）、美股、权证（QZ）等一律剔除 —— 自选股只认 A 股。
    """
    m = re.search(r'v_hint="(.*?)"', raw or "", re.S)
    if not m:
        return []
    out = []
    for rec in m.group(1).split("^"):
        p = rec.split("~")
        if len(p) < 5:
            continue
        market, num, name, py, typ = p[0].strip(), p[1].strip(), p[2], p[3], p[4]
        if market not in ("sh", "sz", "bj"):
            continue                       # 港股 / 美股 / 其他市场
        if not typ.upper().startswith("GP-A"):
            continue                       # 权证、基金以外的非 A 股类型
        if not re.fullmatch(r"\d{6}", num):
            continue
        code = market + num
        out.append({"code": code, "name": _hint_unescape(name),
                    "pinyin": (py or "").strip().lower(),
                    "board": universe.board_of(code)})
    return out


def _rank_search(rows: list[dict], q: str, limit: int) -> list[dict]:
    """统一排序：名称前缀 > 名称包含 > 拼音前缀 > 拼音包含；同级短名优先、再按代码。

    排序必须稳定可复现 —— 同一个关键词每次给出同样的顺序，否则用户
    "上一条还是它、这一条就变了"，点错就加错票。
    """
    ql = (q or "").strip().lower()

    def key(r):
        n = str(r.get("name", "")).lower()
        py = str(r.get("pinyin", "")).lower()
        if n.startswith(ql):
            tier = 0
        elif ql and ql in n:
            tier = 1
        elif py.startswith(ql):
            tier = 2
        elif ql and ql in py:
            tier = 3
        else:
            tier = 4
        return (tier, len(n), str(r.get("code", "")))

    return sorted(rows, key=key)[:max(1, int(limit))]


def _name_index_read_file() -> dict:
    try:
        with open(NAME_INDEX_PATH, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _name_index_seed() -> dict:
    """用本地板块成员表冷启动名单（全市场 6000+ 只）。

    为什么需要冷启动：`name_index.json` 要等一次全市场扫描才会生成，而用户
    可能装完就想加自选。板块成员表（data/board_members.json）已经覆盖全市场，
    正好拿来当初始名单。
    名字带「(已切换)」的是**已作废的旧代码**（如北交所 835xxx → 920xxx），
    必须剔除 —— 否则用户搜出来加进去的是一只查不到行情的死票。
    """
    try:
        with open(paths.data_path("board_members.json"), "r", encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    idx: dict[str, str] = {}
    for arr in (obj.get("members") or {}).values():
        for it in arr or []:
            c = it.get("code")
            n = (it.get("name") or "").strip()
            if not c or not n or "(已切换)" in n:
                continue
            idx.setdefault(c, n)
    return idx


# 名单内存缓存：搜索框是**逐字触发**的，不能每敲一下都去读 4MB 的板块表。
_IDX_CACHE: dict = {"ts": 0.0, "data": None}


def _name_index_write(idx: dict) -> bool:
    try:
        with _NAME_INDEX_LOCK:
            os.makedirs(os.path.dirname(NAME_INDEX_PATH), exist_ok=True)
            tmp = NAME_INDEX_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(idx, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, NAME_INDEX_PATH)
        return True
    except OSError:
        return False


def _name_index_load(max_age: float = 30.0) -> dict:
    """本地名单 {code: name}（带 30 秒内存缓存）。

    文件不存在或明显不全（< 500 条）时用板块成员表冷启动，并落盘 ——
    下次直接读小文件，不再碰 4MB 的板块表。
    """
    now = time.time()
    if _IDX_CACHE["data"] is not None and now - _IDX_CACHE["ts"] < max_age:
        return _IDX_CACHE["data"]
    idx = _name_index_read_file()
    if len(idx) < 500:
        seeded = _name_index_seed()
        if len(seeded) > len(idx):
            idx = seeded
            _name_index_write(idx)
    _IDX_CACHE["data"], _IDX_CACHE["ts"] = idx, now
    return idx


def _name_index_reset_cache() -> None:
    """让下一次读取重新落盘（测试与 remember_names 写盘后调用）。"""
    _IDX_CACHE["data"], _IDX_CACHE["ts"] = None, 0.0


def remember_names(snaps: dict) -> int:
    """把一次全市场快照的「代码 → 名称」并进本地名单，返回新增条数。

    只在**全市场扫描**后调用（见 pipeline）。名字变了（加 ST 前缀、更名）
    也一并更新 —— 行情源的名字永远比冷启动的板块表新。
    无变化则不落盘，避免每轮扫描都写盘。
    """
    if not snaps or len(snaps) < 100:      # 单只详情那种调用不更新名单
        return 0
    idx = dict(_name_index_load())
    before = dict(idx)
    added = 0
    for code, s in snaps.items():
        nm = (s or {}).get("name")
        if not code or not nm:
            continue
        if idx.get(code) != nm:
            if code not in idx:
                added += 1
            idx[code] = nm
    if idx == before:
        return 0
    if _name_index_write(idx):
        _name_index_reset_cache()
    return added


def _search_local(keyword: str, limit: int = 20) -> list[dict]:
    """本地全市场名单子串匹配（名称命中；名单不含拼音，故不参与匹配）。"""
    ql = (keyword or "").strip().lower()
    if not ql:
        return []
    rows = []
    for code, name in _name_index_load().items():
        nm = str(name)
        if ql in nm.lower():
            rows.append({"code": code, "name": nm, "pinyin": "",
                         "board": universe.board_of(code)})
    return _rank_search(rows, ql, limit)


def name_index_size() -> int:
    """本地名单条数（数据源面板/自检展示用）。"""
    return len(_name_index_load())


def search_stocks(keyword: str, limit: int = 8) -> dict:
    """按**名称/拼音**关键词搜 A 股 → {ok, results, source, note}。

    ★ 为什么是「本地名单 + 在线联想」两个源合并，而不是只用在线：
      腾讯 smartbox 是**联想**接口 —— 结果条数有限（约 10 条）且**前缀优先**，
      搜"银行"会被「银行ETF…」占满，真正的招商银行/工商银行一个都出不来；
      且它不覆盖全部北交所（实测"贝特瑞"直接返回无结果）。
      本地名单是**全市场 6000+ 只**的名称子串匹配，召回完整，正好补上这块。
      反过来，拼音首字母（"gzmt"）只有在线源支持 —— 两边都不能少。

    匹配规则（合并后统一排序，见 `_rank_search`）：
      名称前缀 > 名称包含 > 拼音前缀 > 拼音包含；同级**短名优先**、再按代码升序。
      只保留 A 股（sh/sz/bj，含 `GP-A-KCB` 科创板）；港股/美股/ETF/基金/权证一律剔除。

    source 取值：`live`（仅在线）/ `local`（仅本地）/ `live+local`（两者都有）/
    `error`（在线失败且本地也没命中）/ `none`（关键词为空）。
    """
    q = (keyword or "").strip()
    if not q:
        return {"ok": True, "results": [], "source": "none", "note": ""}

    # ① 本地全市场名单：召回完整（子串匹配），但无拼音
    local = _search_local(q, max(int(limit), 20))
    # ② 在线联想：唯一支持拼音首字母，且名称比冷启动的板块表新
    live: list[dict] = []
    err = ""
    try:
        url = SEARCH_URL.format(q=urllib.parse.quote(q))
        raw = _get(url, timeout=8, referer="https://gu.qq.com/").decode(
            "utf-8", errors="ignore")
        live = _parse_search_hint(raw)
    except Exception as e:                       # noqa: BLE001 —— 网络类异常一律兜底
        err = f"搜索接口这次没响应（{type(e).__name__}）"

    # 合并：同一代码以在线源为准（名称更新、且带拼音），本地补齐在线没召回的部分
    merged: dict[str, dict] = {}
    for r in local:
        merged[r["code"]] = r
    for r in live:
        merged[r["code"]] = r
    rows = _rank_search(list(merged.values()), q, limit)

    if rows:
        src = ("live+local" if (live and local) else
               "live" if live else "local")
        note = ""
        if err:
            note = f"{err} —— 拼音搜索暂不可用，名称/代码不受影响"
        return {"ok": True, "results": rows, "source": src, "note": note}

    if err:
        return {"ok": False, "results": [], "source": "error",
                "note": err + "，本地名单也没匹配到；也可以直接输 6 位代码（如 600519）"}
    return {"ok": True, "results": [], "source": "live+local", "note": ""}


# ---------------------------------------------------------------- 数据源总览
# 「数据源」面板的唯一数据出处：把系统用到的**每一个**源列出来 + 实时状态。
#
# 状态全部取自"最近一次真实使用"的记录，**不另发探测请求** ——
# 面板里再打一轮探测既慢、又会消耗东财的每分钟额度（甚至把熔断撞出来），
# 得不偿失。数据来源：
#   · _HOST_STAT      —— _get 每次请求的结果（成功/失败/耗时/连续失败次数）
#   · em_blocked()    —— 东财 push2 接口级熔断（冷却剩余、连续失败轮数）
#   · push2_status()  —— push2 每分钟预算、代理出口
#   · tdx_status()    —— 通达信（TCP，不走 _get）
#   · data_health()   —— 关键数据是否降级（回退到当日旧快照）
#
# 放在文件末尾是因为要引用 EM_PUSH2_HOSTS 等常量（它们定义在中段）。

SOURCE_REGISTRY: list[dict] = [
    # ---- 行情 ----
    {"key": "quote", "group": "行情", "name": "行情快照", "vendor": "腾讯",
     "hosts": ["qt.gtimg.cn"], "use": "全市场快照 · 自选/持仓行情"},
    {"key": "index", "group": "行情", "name": "指数行情", "vendor": "腾讯",
     "hosts": ["qt.gtimg.cn"], "health": ["index"], "use": "两市成交额 · 指数涨跌"},
    {"key": "search", "group": "行情", "name": "名称搜索", "vendor": "腾讯",
     "hosts": ["smartbox.gtimg.cn"], "use": "加自选时按名称/拼音搜股票"},
    # ---- K线 ----
    {"key": "kline_tdx", "group": "K线", "name": "日K线（首选）", "vendor": "通达信",
     "hosts": [], "tdx": True, "use": "前复权日K，走 TCP 不走 HTTP"},
    {"key": "kline_tx", "group": "K线", "name": "日K线（备用）", "vendor": "腾讯",
     "hosts": ["ifzq.gtimg.cn", "web.ifzq.gtimg.cn"], "health": ["klines"],
     "use": "通达信不可用时接管"},
    {"key": "kline_sina", "group": "K线", "name": "日K线（兜底）", "vendor": "新浪",
     "hosts": ["money.finance.sina.com.cn"], "health": ["klines"],
     "use": "前两者都失败时"},
    # ---- 资金 ----
    {"key": "push2", "group": "资金", "name": "板块 / 个股资金流", "vendor": "东财",
     "hosts": list(EM_PUSH2_HOSTS) + ["push2his.eastmoney.com"], "push2": True,
     "health": ["board_flow", "stock_flow", "breadth"],
     "use": "主力净额 · 涨跌家数 · 板块清单"},
    {"key": "lhb", "group": "资金", "name": "龙虎榜", "vendor": "东财",
     "hosts": ["datacenter-web.eastmoney.com"], "health": ["lhb"],
     "use": "席位买卖明细"},
    {"key": "sina_hy", "group": "资金", "name": "板块分类（兜底）", "vendor": "新浪",
     "hosts": ["vip.stock.finance.sina.com.cn"], "health": ["board_list"],
     "use": "行业 / 概念板块清单"},
    # ---- 情绪池 ----
    {"key": "pool", "group": "情绪池", "name": "涨停 / 跌停 / 炸板池", "vendor": "东财",
     "hosts": ["push2ex.eastmoney.com"], "health": ["pool:ZT", "pool:DT", "pool:ZB"],
     "use": "情绪周期 · 亏钱面积的地基"},
    {"key": "yzt", "group": "情绪池", "name": "昨日涨停池", "vendor": "东财",
     "hosts": ["push2ex.eastmoney.com"], "health": ["yzt"],
     "use": "打板溢价 · 晋级率"},
    {"key": "kph", "group": "情绪池", "name": "开盘红（历史回溯）", "vendor": "开盘红",
     "hosts": ["apphwshhq.kaipanhong.com", "apphis.kaipanhong.com"],
     "use": "历史涨停池 · 实时情绪（涨跌家数备用口径）"},
    # ---- 消息 ----
    {"key": "cls", "group": "消息", "name": "财联社快讯 / 涨停原因", "vendor": "财联社",
     "hosts": ["api3.cls.cn", "x-quote.cls.cn"], "health": ["cls_reason"],
     "use": "消息定级 · 涨停原因 · 市场情绪"},
    {"key": "cls_plate", "group": "消息", "name": "板块热度 / 风口 / 轮动", "vendor": "财联社",
     "hosts": ["x-quote.cls.cn"], "use": "板块轮动 · 热度榜 · 催化"},
    {"key": "sina_news", "group": "消息", "name": "新浪 7x24 快讯", "vendor": "新浪",
     "hosts": ["zhibo.sina.com.cn", "feed.mix.sina.com.cn"], "use": "快讯备用源"},
    {"key": "cctv", "group": "消息", "name": "央视新闻", "vendor": "央视",
     "hosts": ["news.cctv.com"], "use": "部委 / 政策原文"},
    {"key": "ths_news", "group": "消息", "name": "同花顺快讯", "vendor": "同花顺",
     "hosts": ["news.10jqka.com.cn"], "use": "快讯备用源"},
    {"key": "em_news", "group": "消息", "name": "东财快讯", "vendor": "东财",
     "hosts": ["np-listapi.eastmoney.com"], "use": "快讯兜底源"},
    {"key": "ths_hot", "group": "消息", "name": "同花顺人气榜", "vendor": "同花顺",
     "hosts": ["dq.10jqka.com.cn", "www.10jqka.com.cn"], "health": ["hot_rank"],
     "use": "散户关注度"},
]

# 排序权重：**有问题的排最前**，然后是"确认正常"，最后才是中性档
# （未启用/未使用 —— 它们不是故障，但也没提供"一切正常"的确认）。
# 注意：ok 要排在 unused 前面，否则同组里"本次运行还没用到"会挤掉已确认正常的源。
_STATE_RANK = {"blocked": 0, "down": 1, "degraded": 2, "ok": 3,
               "disabled": 4, "unused": 5}
# 只有这三档算「异常」—— 决定顶栏标签与总体严重度。
# 未启用（如没装通达信）/ 未使用（本次运行还没轮到）都不该被报成故障。
_PROBLEM_STATES = ("blocked", "down", "degraded")
_STATE_CN = {"blocked": "受限", "down": "不通", "degraded": "降级",
             "disabled": "未启用", "unused": "未使用", "ok": "正常"}


def _ago(sec: int) -> str:
    if sec < 60:
        return f"{sec} 秒前"
    if sec < 3600:
        return f"{sec // 60} 分钟前"
    return f"{sec // 3600} 小时前"


def _source_state(spec: dict, now: float, blocked: list[dict], p2: dict,
                  tdx: dict, bad_health: dict) -> tuple[str, str]:
    """算一个源的 (状态, 说明)。优先级：熔断 > 数据降级 > 主机最近一次请求。"""
    # ① 接口级熔断（目前只有东财 push2 家族会进这个名单）
    if spec.get("push2"):
        hit = [b for b in blocked if b.get("api")]
        if hit:
            left = max(int(b.get("seconds_left") or 0) for b in hit)
            fails = max(int(b.get("fails") or 0) for b in hit)
            return "blocked", (f"熔断中 · 冷却约 {max(1, -(-left // 60))} 分钟"
                               f"（连续失败 {fails} 轮，每 60 秒半开探测）")
    # ② 通达信（TCP，没有 _get 记录）
    if spec.get("tdx"):
        if not tdx.get("available"):
            return "disabled", "未安装 mootdx —— 已自动退回腾讯/新浪K线"
        srv = tdx.get("server") or ""
        if tdx.get("disabled"):
            n = tdx.get("candidates") or 0
            why = (f"{n} 台候选里没有一台能出数据" if n else "连不上任何服务器")
            return "down", (f"暂不可用（{why}）—— 已自动退回腾讯/新浪K线")
        if not tdx.get("picked"):
            return "unused", "尚未选服（首次用K线时才探测）"
        return "ok", f"可用（K线首选源）· {srv}" if srv else "可用（K线首选源）"
    # ③ 关键数据降级（比"主机通不通"更贴近用户关心的事）
    bad = [bad_health[k] for k in (spec.get("health") or []) if k in bad_health]
    if bad:
        labels = "、".join(str(x.get("label") or x.get("key")) for x in bad)
        return "degraded", f"{labels}：{bad[0].get('note') or '本轮未取到'}"
    # ④ 主机最近一次真实请求
    last: tuple[str, dict] | None = None
    for h in spec.get("hosts") or []:
        st = host_stat(h)
        if st and (last is None or st["ts"] > last[1]["ts"]):
            last = (h, st)
    if last is None:
        return "unused", "本次运行还没用到"
    host, st = last
    age = int(now - st["ts"])
    if st.get("ok"):
        return "ok", f"最近成功 {_ago(age)} · {st.get('ms')}ms"
    return "down", (f"最近失败 {_ago(age)}（{st.get('err') or '请求失败'}）"
                    f" · 连续 {st.get('fails')} 次")


def sources_status() -> dict:
    """所有数据源的实时状态 —— 给「数据源」面板用。

    返回 {items, groups, summary, blocked, push2, tdx, health}：
      · items   —— 每个源一条（含 group / state / detail / host / use）
      · summary —— 各状态的计数 + 总体严重度（决定顶栏标签颜色）
    """
    now = time.time()
    # 首次开面板时顺手探一次通达信服务器（之后就走上限缓存）—— 否则那一行永远
    # 显示"尚未选服"，用户看不到它到底通不通。只探一次，失败也不会反复白等。
    if _TDX_AVAILABLE and not _tdx_picked_once:
        try:
            _tdx_pick_server()
        except Exception:                                  # noqa: BLE001
            pass
    blocked = em_blocked()
    p2 = push2_status()
    tdx = tdx_status()
    health = data_health()
    bad_health = {x.get("key"): x for x in (health.get("degraded") or [])}

    items = []
    for spec in SOURCE_REGISTRY:
        state, detail = _source_state(spec, now, blocked, p2, tdx, bad_health)
        # 东财 push2 额外带上限速闸门与出口信息（这是它最容易受限的原因）
        if spec.get("push2") and p2.get("budget_max"):
            extra = f"本分钟额度 {p2.get('budget_used', 0)}/{p2['budget_max']}"
            if p2.get("cooling_seconds"):
                extra += f" · 家族冷却 {p2['cooling_seconds']}s"
            extra += (" · 出口走代理" if p2.get("proxy_on") else " · 出口直连")
            if p2.get("budget_skipped"):
                extra += f" · 已让出 {p2['budget_skipped']} 次"
            detail = f"{detail} · {extra}" if detail else extra
        hosts = spec.get("hosts") or []
        items.append({
            "key": spec["key"], "group": spec.get("group") or "其它",
            "name": spec["name"], "vendor": spec.get("vendor") or "",
            # ★ 只给**主域名**，多域名/多镜像折叠成"等 N 个"（完整列表在 hosts 里，
            #   前端放 title 提示）。原来把 5 个 push2 镜像拼成一行 112 字符的串，
            #   而面板名称列是内容撑开的 —— 那一行把说明列挤成一条缝、文字竖着排，
            #   整行被拉得很高，下面全是空白（用户 10-13 实测："资金接口下面空白很多"）。
            "host": (hosts[0] if hosts else ("TCP" if spec.get("tdx") else "")),
            "host_n": len(hosts),
            "hosts": hosts,
            "use": spec.get("use") or "",
            "state": state, "state_cn": _STATE_CN.get(state, state),
            "detail": detail,
        })

    # 同组内：严重的排前面；组之间按注册顺序（行情 → K线 → 资金 → 情绪池 → 消息）
    order = {}
    for i, spec in enumerate(SOURCE_REGISTRY):
        order.setdefault(spec.get("group") or "其它", i)
    items.sort(key=lambda x: (order.get(x["group"], 99), _STATE_RANK.get(x["state"], 9)))

    cnt: dict[str, int] = {}
    for x in items:
        cnt[x["state"]] = cnt.get(x["state"], 0) + 1
    worst = "ok"
    for st in _PROBLEM_STATES:
        if cnt.get(st):
            worst = st
            break
    groups = []
    for x in items:
        if not groups or groups[-1]["name"] != x["group"]:
            groups.append({"name": x["group"], "items": []})
        groups[-1]["items"].append(x)

    return {
        "items": items, "groups": groups,
        "summary": {"total": len(items), "worst": worst,
                    "worst_cn": _STATE_CN.get(worst, worst), **cnt},
        "blocked": blocked, "push2": p2, "tdx": tdx,
        "health": health,
    }
