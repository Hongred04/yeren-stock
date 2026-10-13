# -*- coding: utf-8 -*-
"""「数据源」面板的回归护栏（用户 10-13 要求）：

  ① **去掉多余的描述** —— 面板只剩「所有源的状态列表 + 两个操作按钮」；
  ② **去掉东方财富 Cookie 配置** —— 限流发生在连接层、与 Cookie 无关，
     配了也没用（这是代码自己的实测结论），所以前端入口、后端接口、
     数据层注入**全部移除**，不留半截；
  ③ **列出所有源的实时状态** —— 每个源一条：状态 / 说明 / 厂商 / 主机 / 用途。

状态取自"最近一次真实使用"的记录（`_get` 是唯一出口，在那里记一笔），
**不另发探测请求** —— 面板里再打一轮探测既慢、又会消耗东财每分钟额度。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["STOCK_DATA_DIR"] = tempfile.mkdtemp(prefix="srcpanel_")

from core import datasource as ds      # noqa: E402

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


def main():
    print("[1] 东财 Cookie 已彻底移除（前端入口 / 后端接口 / 数据层注入）")
    for name in ("em_cookie", "cookie_status", "save_cookie", "auto_fetch_cookie",
                 "EM_COOKIE_FILE", "EM_COOKIE_ENV"):
        ok(not hasattr(ds, name), f"datasource 不再有 {name}")
    s = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "core", "datasource.py"), encoding="utf-8").read()
    get_body = s.split("def _get(")[1].split("def cached(")[0]
    ok("Cookie" not in get_body, "_get 不再注入 Cookie 头")
    ok("import http.cookiejar" not in s, "cookiejar 导入已清掉（不再有用途）")
    srv = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "server.py"), encoding="utf-8").read()
    ok("save_cookie" not in srv and "auto_fetch_cookie" not in srv,
       "server 不再暴露 save_cookie / auto 动作")
    ok('"cookie"' not in srv, "/api/sources 不再返回 cookie 字段")
    for f in ("web/index.html", "web/mobile.html"):
        h = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), f), encoding="utf-8").read()
        ok("srcCookieInput" not in h and "saveSrcCookie" not in h,
           f"{f} 不再有 Cookie 输入框 / 保存逻辑")
        ok("EAST_MONEY_COOKIE" not in h, f"{f} 不再提环境变量 Cookie")

    print("\n[2] 主机级状态采集：_get 是唯一出口，成功/失败都记一笔")
    ds._HOST_STAT.clear()
    ok(ds.host_stat("qt.gtimg.cn") == {}, "没发过请求 → 无记录")
    ds._host_note("qt.gtimg.cn", True, 42)
    st = ds.host_stat("qt.gtimg.cn")
    ok(st.get("ok") is True and st.get("ms") == 42 and st.get("fails") == 0,
       f"成功记录正确（{st}）")
    ds._host_note("qt.gtimg.cn", False, 900, OSError("boom"))
    st = ds.host_stat("qt.gtimg.cn")
    ok(st.get("ok") is False and st.get("fails") == 1, "失败会累加连续失败次数")
    ds._host_note("qt.gtimg.cn", False, 900, OSError("boom"))
    ok(ds.host_stat("qt.gtimg.cn").get("fails") == 2, "连续失败继续累加")
    ds._host_note("qt.gtimg.cn", True, 30)
    ok(ds.host_stat("qt.gtimg.cn").get("fails") == 0, "成功后连续失败清零")
    ds._host_note("", True, 1)
    ok(ds.host_stat("") == {}, "空 host 不记录（不产生垃圾键）")

    # 真实走一次 _get（指向必然连不上的地址）—— 验证出口确实记了失败
    ds._HOST_STAT.clear()
    try:
        ds._get("https://127.0.0.1:1/nope", timeout=1)
        ok(False, "预期请求失败")
    except Exception:
        pass
    bad = ds.host_stat("127.0.0.1")
    ok(bad.get("ok") is False, f"_get 失败后主机被记为不通（{bad.get('err')}）")

    print("\n[3] 注册表覆盖：HEALTH_LABELS 里的每个关键数据都要有归属")
    mapped = set()
    for spec in ds.SOURCE_REGISTRY:
        mapped.update(spec.get("health") or [])
    missing = set(ds.HEALTH_LABELS) - mapped
    ok(not missing, f"所有健康度项都挂在某个源上（漏掉的：{missing or '无'}）")
    keys = [x["key"] for x in ds.SOURCE_REGISTRY]
    ok(len(keys) == len(set(keys)), "每个源的 key 唯一")
    ok(all(x.get("name") and x.get("group") for x in ds.SOURCE_REGISTRY),
       "每个源都有名称与分组")
    hosts = [h for x in ds.SOURCE_REGISTRY for h in (x.get("hosts") or [])]
    ok(all((x.get("hosts") or x.get("tdx")) for x in ds.SOURCE_REGISTRY),
       "每个源都标了主机（通达信走 TCP，用 tdx 标记）")
    ok(all("." in h for h in hosts), "主机名都是合法域名")
    # 允许同一主机挂在多个源上（如 qt.gtimg.cn 同时供「行情快照」和「指数行情」、
    # push2ex 同时供涨停池和昨日涨停池）—— 同域名状态一致本来就是事实。
    shared = sorted({h for h in hosts if hosts.count(h) > 1})
    ok(True, f"共享主机（同域名不同用途，状态一致属正常）：{shared}")

    print("\n[4] sources_status 的形状与分组顺序")
    ds._HOST_STAT.clear()
    r = ds.sources_status()
    for k in ("items", "groups", "summary", "blocked", "push2", "tdx", "health"):
        ok(k in r, f"返回里有 {k}")
    ok(r["summary"]["total"] == len(ds.SOURCE_REGISTRY),
       f'源数量对得上（{r["summary"]["total"]}）')
    names = [g["name"] for g in r["groups"]]
    ok(names == ["行情", "K线", "资金", "情绪池", "消息"], f"分组顺序固定：{names}")
    flat = [x for g in r["groups"] for x in g["items"]]
    ok(len(flat) == len(r["items"]), "分组里的条目数 = 总条目数")
    for x in r["items"]:
        for k in ("key", "group", "name", "vendor", "host", "use", "state",
                  "state_cn", "detail"):
            if k not in x:
                ok(False, f'{x.get("key")} 缺字段 {k}')
                break
        else:
            continue
        break
    else:
        ok(True, "每条都带齐 key/名称/厂商/主机/用途/状态/说明")

    print("\n[5] 状态判定：未使用 / 正常 / 不通 / 降级 / 受限 / 未启用")
    ds._HOST_STAT.clear()
    r = ds.sources_status()
    by = {x["key"]: x for x in r["items"]}
    ok(by["quote"]["state"] == "unused", "没发过请求 → 未使用")
    ok(by["quote"]["detail"] == "本次运行还没用到", "未使用的说明直白")

    ds._host_note("qt.gtimg.cn", True, 55)
    by = {x["key"]: x for x in ds.sources_status()["items"]}
    ok(by["quote"]["state"] == "ok", "请求成功 → 正常")
    ok("最近成功" in by["quote"]["detail"] and "55ms" in by["quote"]["detail"],
       f'说明带时间与耗时（{by["quote"]["detail"]}）')

    ds._host_note("qt.gtimg.cn", False, 3000, OSError("timeout"))
    by = {x["key"]: x for x in ds.sources_status()["items"]}
    ok(by["quote"]["state"] == "down", "请求失败 → 不通")
    ok("最近失败" in by["quote"]["detail"], f'说明讲清失败（{by["quote"]["detail"]}）')

    ds._host_note("qt.gtimg.cn", True, 40)
    ds._health_set("index", False, "接口本次未取到，用的是 120 秒前的当日快照", 120)
    by = {x["key"]: x for x in ds.sources_status()["items"]}
    ok(by["index"]["state"] == "degraded", "关键数据降级 → 降级（比主机状态更靠前）")
    ok("指数行情" in by["index"]["detail"] and "120 秒前" in by["index"]["detail"],
       f'降级说明带上标签与原因（{by["index"]["detail"]}）')
    ds._health.clear()

    # 通达信是**软依赖**：没装 → 未启用；装了 → ok（选到能出数据的服务器）
    # 或 down（候选里没有能出数据的）。三种都合法，按环境断言。
    tdx_item = by["kline_tdx"]
    if not ds._TDX_AVAILABLE:
        ok(tdx_item["state"] == "disabled", "没装 mootdx → 未启用（不是故障）")
    else:
        ok(tdx_item["state"] in ("ok", "down", "unused"),
           f"装了 mootdx → {tdx_item['state']}（ok/down/unused 都合法）")
    ok(("退回" in tdx_item["detail"]) or ("首选源" in tdx_item["detail"]),
       f'说明讲清走向（{tdx_item["detail"]}）')

    # 东财熔断 → 该源标记为受限
    ds._em_down_until["/api/qt/clist/get"] = __import__("time").time() + 120
    ds._em_fail["/api/qt/clist/get"] = 3
    try:
        r2 = ds.sources_status()
        by2 = {x["key"]: x for x in r2["items"]}
        ok(by2["push2"]["state"] == "blocked", "接口熔断 → 受限")
        ok("熔断中" in by2["push2"]["detail"] and "冷却约" in by2["push2"]["detail"],
           f'说明带冷却时间（{by2["push2"]["detail"]}）')
        ok("本分钟额度" in by2["push2"]["detail"], "顺带带上限速闸门状态")
        ok(r2["summary"]["worst"] == "blocked", "总体严重度 = 受限")
        ok(r2["summary"]["worst_cn"] == "受限", "中文档位对得上")
    finally:
        ds._em_down_until.clear()
        ds._em_fail.clear()

    print("\n[6] 中性档位不污染总体严重度，且排序上正常优先")
    ds._HOST_STAT.clear()
    r = ds.sources_status()
    ok(r["summary"]["worst"] == "ok",
       f'只有"未使用/未启用"时总体仍是正常（{r["summary"]["worst"]}）')
    ok(r["summary"].get("unused", 0) > 0, "中性档位照常计数（未使用 > 0）")
    ok(r["summary"].get("ok", 0) + r["summary"].get("unused", 0)
       + r["summary"].get("disabled", 0) + r["summary"].get("down", 0)
       == r["summary"]["total"], "各档计数加起来 = 总源数")
    ds._host_note("qt.gtimg.cn", True, 50)
    r = ds.sources_status()
    grp = [g for g in r["groups"] if g["name"] == "行情"][0]
    states = [x["state"] for x in grp["items"]]
    ok(states[0] == "ok" and "unused" in states,
       f"同组里已确认正常的排在未使用之前（{states}）")
    ds._HOST_STAT.clear()

    print("\n[7] 通达信：选服 / 占位K线清洗 / 复权兼容 / 指数源")
    cands = ds._tdx_candidates()
    ok(len(cands) >= 1, f"候选服务器非空（{len(cands)} 台）")
    ok(len(cands) == len(set(cands)), "候选去重")
    ok(all(isinstance(ip, str) and isinstance(p, int) for ip, p in cands),
       "候选都是 (ip, port) 形式")
    ok(("117.34.114.14", 7709) in cands, "内置已知可用站点在候选里")
    st = ds.tdx_status()
    for k in ("available", "fail", "disabled", "server", "candidates", "picked"):
        ok(k in st, f"tdx_status 带 {k}")

    # ★ 占位K线必须剔除：服务器盘中（尤其开盘前）会回一根四价全等、
    #   成交量是 5.877471754111438e-39（协议里"空"字段的位模式）的假K线。
    #   拿 `vol <= 0` 判会漏（那个数 > 0），必须按 vol < 1 判。
    today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")

    class _Row(dict):
        pass

    class _DF:
        def __init__(self, rows):
            self._rows = rows

        def __len__(self):
            return len(self._rows)

        def reset_index(self):
            return self

        def iterrows(self):
            return list(enumerate(self._rows))

    class _Cli:
        def __init__(self, rows):
            self._rows = rows

        def bars(self, **kw):
            return _DF(self._rows)

        def index_bars(self, **kw):
            return _DF(self._rows)

    rows = [
        {"datetime": "2026-10-09", "open": 10, "close": 11, "high": 11.5,
         "low": 9.9, "vol": 12345},
        {"datetime": "2026-10-12", "open": 11, "close": 12, "high": 12.2,
         "low": 10.8, "vol": 23456},
        # 今日占位：四价全等 + 非规格化浮点成交量
        {"datetime": today, "open": 12, "close": 12, "high": 12, "low": 12,
         "vol": 5.877471754111438e-39},
    ]
    got = ds._tdx_bars(_Cli(rows), "600519", 5)
    ok(len(got) == 2, f"占位K线被剔除（剩 {len(got)} 根）")
    ok(all(b["date"] != today for b in got), "今日占位 bar 不在结果里")
    ok([b["date"] for b in got] == ["2026-10-09", "2026-10-12"], "顺序按日期升序")
    ok(ds._tdx_bars(_Cli([]), "600519", 5) == [], "空 DataFrame → 空列表（不抛）")

    # ★ 复权兼容垫片：pandas 2.2+ 移除了 fillna(method=)，mootdx 的复权还在用它。
    #   垫片只在调用期间生效，退出后必须还原。
    try:
        import pandas as pd
        before_df, before_s = pd.DataFrame.fillna, pd.Series.fillna
        with ds._pandas_legacy_fillna():
            df = pd.DataFrame({"a": [1.0, None, 3.0]})
            try:
                # mootdx 复权走的是 `data.factor.fillna(...)` —— **Series**，
                # 只补 DataFrame 等于没补（这个坑实测踩过）
                ok(list(df["a"].fillna(method="ffill")) == [1.0, 1.0, 3.0],
                   "垫片让 Series.fillna(method='ffill') 可用")
                ok(list(df[["a"]].fillna(method="ffill", axis=0)["a"]) == [1.0, 1.0, 3.0],
                   "DataFrame 同样可用")
            except TypeError as e:
                ok(False, f"垫片没生效：{e}")
        ok(pd.DataFrame.fillna is before_df and pd.Series.fillna is before_s,
           "退出后 DataFrame/Series 的 fillna 都已还原")
    except ImportError:
        ok(True, "（本解释器没有 pandas，跳过垫片用例）")

    # 指数源优先级：通达信 → 腾讯 → 新浪
    real_idx, real_tx = ds._kline_from_tdx_index, ds._kline_from_tx
    calls = []
    ds._kline_from_tdx_index = lambda sym, n: (calls.append("tdx") or
                                               [{"date": "2026-10-12", "open": 1,
                                                 "close": 2, "high": 2, "low": 1,
                                                 "volume": 9}])
    ds._kline_from_tx = lambda code, n: (calls.append("tx") or [])
    try:
        h = ds.market_amount_history(5)
        ok(calls[:1] == ["tdx"], f"沪指日K优先走通达信（实际 {calls}）")
        ok(len(h) == 1, "取到数据就返回")
    finally:
        ds._kline_from_tdx_index, ds._kline_from_tx = real_idx, real_tx

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
