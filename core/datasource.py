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
import http.cookiejar
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
from datetime import datetime, timedelta

from . import paths

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


# ---------------------------------------------------------------- 东财 Cookie
# 实测（2026-10-08）东财 push2 的 clist 接口会被**按 URL 路径**风控：请求发出后
# 服务端直接 RST（RemoteDisconnected），而同一域名、同一时刻的
# ulist.np / ulist / stock 接口全部 200；东财入口页（www / quote / gridlist）
# 也一个 Set-Cookie 都不下发。
#   → 该限流发生在连接层，与 Cookie 无关；带不带 Cookie 都一样被掐。
# 仍然保留 Cookie 注入能力，因为：
#   1) 东财部分 datacenter 报表接口对已登录会话的频控阈值更宽；
#   2) 将来若东财改成校验 Cookie，改配置即可生效，不必改代码。
# 配置来源（优先级从高到低）：
#   1) 环境变量 EAST_MONEY_COOKIE
#   2) 文件 data/eastmoney_cookie.txt（支持热加载：改文件即生效，无需重启进程）
EM_COOKIE_ENV = "EAST_MONEY_COOKIE"
EM_COOKIE_FILE = paths.data_path("eastmoney_cookie.txt")

_ck_memo: tuple[float, str] | None = None   # (文件 mtime, 拼好的 cookie)


def _norm_cookie(txt: str) -> str:
    """统一 Cookie 文本：既接受单行 `a=b; c=d`，也接受从 DevTools 复制来的多行。

    注意不能把换行直接换成空格 —— 那样 cookie 之间就没有分隔符了。
    """
    parts = []
    for chunk in (txt or "").replace("\r", "\n").split("\n"):
        for piece in chunk.split(";"):
            s = piece.strip()
            if s:
                parts.append(s)
    return "; ".join(parts)


def em_cookie() -> str:
    """当前生效的东财 Cookie：环境变量优先，否则读文件（按 mtime 热加载）。"""
    global _ck_memo
    env = (os.environ.get(EM_COOKIE_ENV) or "").strip()
    if env:
        return _norm_cookie(env)
    try:
        mt = os.path.getmtime(EM_COOKIE_FILE)
    except OSError:
        _ck_memo = None
        return ""
    if _ck_memo is not None and _ck_memo[0] == mt:
        return _ck_memo[1]
    try:
        with open(EM_COOKIE_FILE, encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return ""
    txt = _norm_cookie(txt)
    _ck_memo = (mt, txt)
    return txt


def cookie_status() -> dict:
    """Cookie 当前状态（给前端/自检看）。"""
    env = (os.environ.get(EM_COOKIE_ENV) or "").strip()
    ck = em_cookie()
    return {
        "configured": bool(ck),
        "source": "环境变量" if env else ("文件" if ck else "未配置"),
        "env_var": EM_COOKIE_ENV,
        "file": EM_COOKIE_FILE,
        "length": len(ck),
        "preview": (ck[:28] + "…") if len(ck) > 28 else ck,
    }


def save_cookie(text: str) -> dict:
    """把粘贴的 Cookie 落盘（前端「保存」用），立即生效、无需重启。"""
    global _ck_memo
    txt = _norm_cookie(text)
    os.makedirs(os.path.dirname(EM_COOKIE_FILE), exist_ok=True)
    with open(EM_COOKIE_FILE, "w", encoding="utf-8") as f:
        f.write(txt)
    _ck_memo = None
    return cookie_status()


def auto_fetch_cookie(timeout: int = 12) -> dict:
    """尝试自动获取东财**匿名设备** Cookie（走 CookieJar 收 Set-Cookie）。

    实测当前东财入口页不下发任何 Set-Cookie，所以通常返回 found=False ——
    登录态 Cookie 没有自动途径，只能手动导出一次。保留此函数是为了东财将来
    启用设备 Cookie 时能自动接上，不必改数据层。
    """
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPSHandler(context=_SSL_CTX))
    op.addheaders = [("User-Agent", UA), ("Accept", "text/html,*/*")]
    for url in ("https://quote.eastmoney.com/center/gridlist.html",
                "https://www.eastmoney.com/"):
        try:
            with op.open(url, timeout=timeout):
                pass
        except Exception:
            continue
        got = [f"{c.name}={c.value}" for c in cj]
        if got:
            return {"found": True, "cookies": got, "header": "; ".join(got),
                    "from": url}
    return {"found": False, "cookies": [], "header": "", "from": ""}


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
    """清空熔断记录（例如刚配好 Cookie，想立刻重试）。"""
    with _em_lock:
        n = len(_em_down_until)
        _em_down_until.clear()
        _em_fail.clear()
    return n


def _new_conn(host: str, timeout: int) -> http.client.HTTPSConnection:
    """创建 HTTPS 连接。**优先走系统代理**，代理不可用时自动直连。

    为什么东财请求要走代理（实测 2026-10-10）：系统以前从不吃系统代理，
    几十轮扫描全从家宽 IP 直连打出去，东财把这个 IP 标记了 —— 接口间歇
    RST、网页弹滑块。走用户本地代理（Clash 节点）相当于换干净 IP，实测
    6/6 成功 vs 直连 3/6。代理连不上（没开 Clash）时自动回直连，并在
    120 秒内不再尝试代理，避免每个请求都先撞一次死端口。
    """
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
    if "eastmoney.com" in host:
        ck = em_cookie()
        if ck:
            headers["Cookie"] = ck
    q = _conn_pool.setdefault(host, queue.Queue())
    last_err: Exception | None = None
    for attempt in range(3):
        conn = None
        reused = False
        try:
            try:
                conn = q.get_nowait()
                reused = True
            except queue.Empty:
                conn = _new_conn(host, timeout)
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
        url = "https://qt.gtimg.cn/q=" + ",".join(chunk)
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
            return []
        data = js.get("data") or {}
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
        return out
    return cached("pool:YZT", ttl, producer)


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
    path = paths.data_path("zt_history.json")
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

POOL_HIST_DIR = paths.data_path("pool_history")


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


def fetch_breadth() -> dict:
    """全市场涨跌家数 {up, down, flat, ...}。取不到返回空 dict。

    push2 拿不到时（被限流 / 预算用尽）退**开盘红实时情绪**——它直接给
    涨跌家数，是独立于东财的一条路；结果带 source 字段供界面如实标注。
    """
    js = _push2_json("/api/qt/ulist.np/get?fltt=2&invt=2"
                     "&secids=1.000001,0.399001&fields=f12,f14,f104,f105,f106")
    diff = (js.get("data") or {}).get("diff") or []
    if not diff:
        return _breadth_fallback()
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
    """东财口径：带主力净额。一次拿全部板块，受 push2 限速闸门约束。"""
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
    out.sort(key=lambda r: -(r["main"] or 0))
    return out[:limit]


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

    codes = (load_valid_codes() or [])[:_BOARD_MEMBERS_MAX_CODES]
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
