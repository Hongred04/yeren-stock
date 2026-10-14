# -*- coding: utf-8 -*-
"""扩展数据源回归测试：财联社（cls）与开盘红（kph）。

纯逻辑离线测试：签名算法 / 字段映射 / 板块热度匹配 / 归档回补调度 /
情绪温度计的「打板溢价」兜底分支。不请求网络 —— 外部接口的可用性
由运行时容错 + probes 脚本保证。测试钉住的是协议知识与调度逻辑。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["STOCK_DATA_DIR"] = tempfile.mkdtemp(prefix="dsc_")

from core import cls                      # noqa: E402
from core import datasource as ds         # noqa: E402
from core import kph                      # noqa: E402
from core import sentiment_meter as sm    # noqa: E402
from core import ths                      # noqa: E402

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
    print("[1] 财联社签名算法（已知向量）")
    ok(cls._sign({"a": "1", "b": "2"}) == "4e13ecb1934db939e390065a9b63ecd8",
       "md5(sha1('a=1&b=2')) 与预期一致")
    ok(cls._sign({"sv": "8.7.4", "app": "cailianpress"})
       == "dddf20546c32e0d2ed78ba60b6cad383", "参数按键名排序，顺序无关")

    print("[2] 板块热度匹配（精确/别名/唯一包含/歧义）")
    heats = [{"name": "化工", "rank": 3, "rank_change": 5, "is_new": False, "heat": 100.0},
             {"name": "半导体芯片", "rank": 9, "rank_change": -2, "is_new": False, "heat": 80.0},
             {"name": "电池", "rank": 5, "rank_change": -3, "is_new": False, "heat": 90.0},
             {"name": "固态电池", "rank": 2, "rank_change": 1, "is_new": True, "heat": 95.0}]
    ok(cls.match_heat("电池", heats)["rank"] == 5, "精确匹配优先（不被'固态电池'抢走）")
    ok(cls.match_heat("化学制品", heats)["name"] == "化工", "别名表命中（化学制品→化工）")
    ok(cls.match_heat("半导体", heats)["name"] == "半导体芯片", "唯一包含匹配")
    ok(cls.match_heat("跨境支付", heats) is None, "无匹配返回 None")
    ok(cls.match_heat("", heats) is None, "空名返回 None")

    print("[3] 开盘红封板时间归一（epoch ↔ HHMMSS）")
    ok(kph._hms(1790748529) > 0, "epoch 秒转为 HHMMSS")
    ok(kph._hms(93000) == 93000, "HHMMSS 原样返回")
    ok(kph._hms(0) == 0 and kph._hms(None) == 0, "空值返回 0")

    print("[4] 历史池行转换与 ST 过滤")
    raw = ["600131", "国网信通", 0, "", 0, 0, 1790748529, 0, 26475540, "首板", 1,
           "智能电网、基础建设", 64260307, 401873347, 5.41, 7654631283,
           "智能电网", "北京国资"]
    row = kph._to_pool_row(raw)
    ok(row and row["code"] == "sh600131" and row["name"] == "国网信通",
       "代码补前缀 / 名称映射")
    ok(row["boards"] == 1 and row["sector"] == "智能电网", "连板数 / 板块映射")
    ok(row["first_time"] == kph._hms(1790748529), "封板时间映射")
    ok(kph._to_pool_row(["000001", "*ST测试", 0, "", 0, 0, 1790748529, 0, 1,
                         "首板", 1, "x", 1, 1, 1, 1, "y", "z"]) is None,
       "ST 过滤（对齐东财口径）")
    ok(kph._to_pool_row(["600000"]) is None, "行过短返回 None")

    print("[5] 归档回补调度（monkeypatch 数据层）")
    ds.save_pool_history("20260921", [{"code": "sh600001", "name": "已有",
                                       "boards": 1}])

    def fake_pool(date):
        if date == "20260922":
            return [{"code": "sh600002", "name": "新补", "boards": 1,
                     "first_time": 93000, "sector": "电池"}]
        return []
    kph.history_pool = fake_pool
    r = kph.autobackfill("20260925", lookback=4, max_try=4)
    ok("20260922" in r["filled"], "缺失日期被补上")
    got = ds.load_pool_history("20260921")
    ok(len(got) == 1 and got[0]["name"] == "已有", "已有归档不被覆盖")
    r2 = kph.autobackfill("20260925", lookback=4, max_try=4)
    ok(r2["filled"] == [], "补过/试过的日期二轮零请求")

    print("[6] 情绪温度计：打板溢价东财口径优先")
    ds.fetch_breadth = lambda *a, **k: {}
    ds.fetch_yesterday_zt_pool = lambda *a, **k: [
        {"code": "sh600001", "name": "甲", "pct": 5.0, "boards_y": 1,
         "yfbt": 93000, "sector": "X"},
        {"code": "sh600002", "name": "乙", "pct": -3.0, "boards_y": 2,
         "yfbt": 93000, "sector": "X"},
    ]
    m = sm.build([], [], [], {}, "20260920", {"E1_meter": {"enabled": True}})
    me = m["metrics"]
    ok(me["money_effect"] == 1.0, "打板溢价 = 东财均值 (5 + -3)/2 = 1.0")
    ok(me["height_premium"] == -3.0, "连板溢价只统计昨日≥2板")
    ok(me["premium_src"] == "em", "来源标记 em")

    print("[7] 全零涨幅视为无效（盘前特征），不进口径")
    ds.fetch_yesterday_zt_pool = lambda *a, **k: [
        {"code": "sh600001", "name": "甲", "pct": 0.0, "boards_y": 1,
         "yfbt": 93000, "sector": "X"},
    ]
    m2 = sm.build([], [], [], {}, "20260921", {"E1_meter": {"enabled": True}})
    ok(m2["metrics"]["money_effect"] is None
       and m2["metrics"]["premium_src"] == "",
       "全零不进口径（无本地兜底数据时为空）")

    print("[8] 同花顺人气榜解析（mock 数据层）")
    _fake_hot = json.dumps({
        "status_code": 0,
        "data": {"stock_list": [
            {"code": "600127", "name": "金健米业",
             "tag": {"concept_tag": ["玉米"], "popularity_tag": "首板涨停"}},
            {"code": "002242", "name": "九阳股份",
             "tag": {"popularity_tag": "5天5板"}},
            {"code": "600825", "name": "新华传媒", "tag": {}},
        ]},
    }).encode()
    ds._get = lambda *a, **k: _fake_hot
    hot = ths.hot_rank()
    ok(hot.get("sh600127", {}).get("rank") == 1
       and hot["sh600127"]["tag"] == "首板涨停", "代码归一 + 人气标签")
    ok(hot.get("sz002242", {}).get("rank") == 2, "排名连续")
    ok(hot.get("sh600825", {}).get("tag") == "", "无标签返回空串")

    print("[9] 盘口异动解析（i 字段三段式 → 百分数）")
    _fake_ch = json.dumps({"rc": 0, "data": {"allstock": [
        {"tm": 92422, "c": "300668", "m": 0, "n": "杰恩股份", "t": 8208,
         "i": "-0.019542,29.10000,-0.019542"},
        {"tm": 145145, "c": "600573", "m": 1, "n": "惠泉啤酒", "t": 8201,
         "i": "0.072007,11.91000,0.072007"},
    ]}}).encode()
    ds._get = lambda *a, **k: _fake_ch
    ch = ds.fetch_stock_changes("8208")
    ok(ch and ch[0]["time"] == "092422" and ch[0]["code"] == "sz300668",
       "时间补零 + 代码前缀")
    ok(ch[0]["pct"] == -1.95, "比率 ×100（-0.0195 → -1.95%）")
    ch2 = ds.fetch_stock_changes("8201")
    hq = next((x for x in ch2 if x["name"] == "惠泉啤酒"), None)
    ok(hq and hq["pct"] == 7.2, "火箭发射同规则（0.072 → 7.2%）")

    print("[10] 风口板块匹配（复用 match_heat 别名表）")
    winds = [{"name": "短剧", "catalyst": "AI电影定档"},
             {"name": "农业种植", "catalyst": "粮价上涨"}]
    hit = cls.match_heat("出版", winds)
    ok(hit and hit["catalyst"] == "AI电影定档", "出版→短剧（别名）")
    hit2 = cls.match_heat("种植业", winds)
    ok(hit2 and hit2["name"] == "农业种植", "种植业→农业种植（别名）")

    print("[11] push2 请求预算（主动限流，不等被掐）")
    _old_budget = ds.PUSH2_BUDGET
    ds.PUSH2_BUDGET = 3
    ds._budget_log.clear()
    allow = [ds._push2_allow() for _ in range(5)]
    ok(allow == [True, True, True, False, False],
       "额度用完即让行（前3次放行、后2次挡下且不记失败）")
    ok(ds._budget_skip >= 2, "被挡次数有记录（面板展示用）")
    ds.PUSH2_BUDGET = _old_budget
    ds._budget_log.clear()

    print("[12] push2 挂掉 → 备用源顶上（mock）")
    from core import kph as _kph
    _kph.market_emotion = lambda: {"up": 3000, "down": 2000, "flat": 200,
                                   "total": 5200, "limit_up": 60,
                                   "limit_down": 5, "source": "kph"}
    cls.sector_industry = lambda: [{"name": "影视院线", "pct": 6.77,
                                    "main": 8.37e8, "up": 5, "down": 0,
                                    "lead": "捷成股份"}]
    ds._push2_json = lambda *a, **k: {}
    ds._cache.pop("kph:emotion", None)      # 清掉可能由前面用例留下的真实缓存
    ds._cache_ts.pop("kph:emotion", None)
    b = ds._breadth_fallback()
    ok(b.get("source") == "kph" and b.get("up") == 3000,
       "涨跌家数退到开盘红（并标注来源）")
    ok(round(b.get("up_ratio", 0), 2) == 0.58,
       "上涨占比按 up/(up+down+flat) 计算")
    for k in [k for k in ds._cache if str(k).startswith("board_flow")]:
        ds._cache.pop(k, None)
        ds._cache_ts.pop(k, None)
    flow = ds.fetch_board_moneyflow(5)
    ok(flow and flow[0]["source"] == "cls"
       and flow[0]["name"] == "影视院线", "板块资金退到财联社（仍有主力净额）")

    print("[13] 资金流主链路走 clist（2026-10-14 改：clist 主 / ulist.np 备）")
    # 为什么钉这一条：本段原来断言「clist 被路径风控、ulist.np 可用」，
    # 实测完全反了 —— ulist.np 四台镜像全灭，个股资金流整个空转。
    # 这里用假的 _push2_json 断言「确实是走 clist 拿到的」，防止再被改回去。
    _real_push2 = ds._push2_json
    _stocks = [{"f12": "600519", "f14": "贵州茅台", "f62": 1.0e8, "f184": 3.5,
                "f66": 6.0e7, "f72": 4.0e7, "f78": -1.0e7, "f84": -2.0e7},
               {"f12": "000001", "f14": "平安银行", "f62": -5.0e7, "f184": -2.1,
                "f66": -3.0e7, "f72": -2.0e7, "f78": 1.0e7, "f84": 2.0e7}]
    _seen = []

    def _fake(path, referer="https://quote.eastmoney.com/"):
        _seen.append(path)
        if "/api/qt/clist/get" in path:
            return {"data": {"total": len(_stocks), "diff": _stocks}}
        return {"data": None}                 # ulist.np 全灭
    ds._push2_json = _fake
    mf = ds.fetch_stock_moneyflow(["sh600519", "sz000001"])
    ok(set(mf) == {"sh600519", "sz000001"}, "两只票都取到（clist 主链路）")
    ok(all("clist" in p for p in _seen), "确实走的 clist，没回退到 ulist.np")
    ok(mf["sh600519"]["main"] == 1.0e8 and mf["sh600519"]["main_pct"] == 3.5,
       "主力净额/净占比字段映射正确")
    ok(mf["sh600519"]["super"] == 6.0e7 and mf["sh600519"]["small"] == -2.0e7,
       "四档（超大/大/中/小）字段映射正确")
    ok(mf["sz000001"]["main"] == -5.0e7, "负值（净流出）不被当成缺失")

    print("[14] clist 拿不到时降级到 ulist.np")
    _seen.clear()

    def _fake_clist_dead(path, referer="https://quote.eastmoney.com/"):
        _seen.append(path)
        if "/api/qt/clist/get" in path:
            return {"data": None}
        return {"data": {"diff": [_stocks[0]]}}
    ds._push2_json = _fake_clist_dead
    mf2 = ds.fetch_stock_moneyflow(["sh600519"])
    ok(mf2.get("sh600519", {}).get("main") == 1.0e8, "clist 死时 ulist.np 顶上")

    print("[15] 代码归一与空输入边界")
    # §14 把 _push2_json 换成了 _fake_clist_dead，这里必须换回「clist 正常」的
    # 假实现，否则 §15 实际测的是降级分支（踩过：断言全 XX，查半天是测试自己的锅）。
    ds._push2_json = _fake
    ok(ds._mk_code("600519") == "sh600519" and ds._mk_code("000001") == "sz000001",
       "沪深代码加前缀")
    ok(ds._mk_code("300750") == "sz300750" and ds._mk_code("688981") == "sh688981",
       "创业板/科创板前缀正确")
    ok(ds._mk_code("430017") == "bj430017" and ds._mk_code("920001") == "bj920001",
       "北交所前缀正确（43/92 号段）")
    ok(ds.fetch_stock_moneyflow([]) == {}, "空输入返回空字典（不打接口）")
    _seen.clear()
    ok(ds.fetch_stock_moneyflow(["sh999999"]) == {}, "全不匹配返回空字典")
    # ★ 钉住一个真踩过的坑：clist 通了但一只都没匹配上时，不能当成「链路失败」
    #   去降级，更不能白翻满 20 页（total 已经说明翻到底了）。
    #   原来写成 `if out:` 判断，实测 20 次请求全废且还多打一轮 ulist.np。
    ok(sum(1 for p in _seen if "clist" in p) == 1,
       "clist 通但没匹配上 → 按 total 判断翻到底，只打 1 次")
    ok(not any("ulist" in p for p in _seen),
       "clist 通但没匹配上 → 不降级打 ulist.np（区分「接口失败」与「没匹配」）")
    ds._push2_json = _real_push2

    print("[16] 板块资金流走 clist 拿全集（含领涨股）")
    _boards = [{"f12": "BK1340", "f14": "印制电路板", "f3": 1.98, "f62": 1.0e9,
                "f184": 4.15, "f204": "兴森科技", "f205": "002436"},
               {"f12": "BK0459", "f14": "元件", "f3": 1.69, "f62": 9.8e8,
                "f184": 3.61, "f204": "兴森科技", "f205": "002436"}]
    ds._push2_json = lambda p, referer="x": ({"data": {"diff": _boards}}
                                             if "/api/qt/clist/get" in p
                                             else {"data": None})
    for k in [k for k in ds._cache if str(k).startswith("board_flow")]:
        ds._cache.pop(k, None)
        ds._cache_ts.pop(k, None)
    bd = ds.fetch_board_moneyflow(5)
    ok(bd and bd[0]["name"] == "印制电路板" and bd[0]["source"] == "em",
       "板块资金流走 clist（source=em）")
    ok(bd[0]["main"] == 1.0e9 and bd[0]["main_pct"] == 4.15, "板块主力净额/净占比正确")
    ok(bd[0]["lead"] == "兴森科技" and bd[0]["lead_code"] == "002436",
       "领涨股 f204/f205 正确")
    ok(bd[0]["main"] >= bd[1]["main"], "按主力净额降序")
    ds._push2_json = _real_push2

    print("[17] 腾讯不通时退新浪备用源（快照链路独立性）")
    # 用户实测「行情快照经常不通」：腾讯偶发 RemoteDisconnected。
    # 备用源必须能顶上，且字段口径要和腾讯对齐（否则下游量纲串台）。
    _sina_line = ('var hq_str_sh600519="贵州茅台,1270.100,1274.890,1284.330,'
                  '1286.750,1266.000,1284.320,1284.330,1196653,1529287044.000,'
                  '100,1284.330,200,1284.320,300,1284.310,400,1284.300,'
                  '500,1284.290,600,1284.280,700,1284.270,800,1284.260,'
                  '900,1284.250,1000,1284.240,2026-10-14,11:03:20,00";')
    rec = ds._parse_sina_line(_sina_line)
    ok(rec and rec["code"] == "sh600519" and rec["name"] == "贵州茅台",
       "新浪行解析出代码与名称")
    ok(rec["price"] == 1284.33 and rec["prev_close"] == 1274.89, "现价/昨收正确")
    ok(rec["pct"] == 0.74, "涨跌幅由 现价/昨收 现算（新浪不直接给）")
    # ★ 量纲：新浪是股、腾讯是手。这里必须折成手，否则成交量会差 100 倍。
    ok(abs(rec["volume"] - 11966.53) < 0.01,
       "成交量从「股」折成「手」（÷100，与腾讯同量纲）")
    ok(rec["source"] == "sina", "带 source 标记，便于界面如实标注")
    ok(ds._parse_sina_line("garbage") is None
       and ds._parse_sina_line('var hq_str_x="";') is None,
       "畸形行/空行安全返回 None")
    ok(ds._parse_sina_line(
        'var hq_str_sz000001="平安银行,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,'
        '0,0,0,0,0,0,0,0,0,0,2026-10-14,11:03:20,00";') is None,
       "全 0（停牌/无数据）不当成有效快照")

    # 字段数随市场不同（实测 SH=34 / SZ=33 / BJ=33），短字段不应越界抛异常
    _sz = ('var hq_str_sz000001="平安银行,11.570,11.570,11.640,11.720,11.560,'
           '11.630,11.640,40250927,453717873.490,27200,11.630,36100,11.620,'
           '117400,11.610,497900,11.600,300000,11.590,500000,11.580,'
           '600000,11.570,700000,11.560,800000,11.550,900000,11.540,'
           '2026-10-14,11:03:20,00";')
    r2 = ds._parse_sina_line(_sz)
    ok(r2 and r2["price"] == 11.64 and abs(r2["volume"] - 402509.27) < 0.01,
       "深市（33 字段）同样正确解析")

    _orig_get = ds._get
    ds._get = lambda url, timeout=12, referer="", plain=None: (
        _sina_line + "\n" + _sz).encode("gb18030")
    fs = ds.fetch_snapshots_sina(["sh600519", "sz000001"])
    ok(set(fs) == {"sh600519", "sz000001"}, "备用源批量取回两只")
    ok(fs["sh600519"]["source"] == "sina", "备用源结果带 source=sina")
    ok(ds.fetch_snapshots_sina([]) == {}, "备用源空输入不打接口")

    def _boom(url, timeout=12, referer="", plain=None):
        raise OSError("network down")
    ds._get = _boom
    ok(ds.fetch_snapshots_sina(["sh600519"]) == {},
       "备用源整链失败时返回空字典（不抛异常打断扫描）")
    ds._get = _orig_get

    print("[18] push2 冷却/超预算时跳过它，直接走免费备用口径")
    # 实测被东财掐住时 fetch_breadth 要先烧 3.01 秒（4 台镜像轮一遍 + 5 个预算位）
    # 才降级 —— 已知打不通就别打，把这几秒还给用户。
    ds._push2_recover()
    ok(ds._push2_cheap_skip() is False, "正常状态不跳过 push2")
    ds._push2_trip(60)
    ok(ds._push2_cheap_skip() is True, "家族冷却中 → 判定为注定白打")
    # 缓存键就是裸的 "breadth"（不是 breadth+日期），清错键会读到上一组测试的缓存
    ds._cache.pop("breadth", None)
    ds._cache_ts.pop("breadth", None)
    _before = ds.push2_status()["budget_used"]
    _b = ds.fetch_breadth()
    ok(ds.push2_status()["budget_used"] == _before,
       "冷却中取涨跌家数 → 一个 push2 预算位都没花")
    # 不断言返回值内容：备用口径（开盘红实时情绪）本身也要走网络，测试机
    # 偶发取不到时会再退到「当日最后一份好数据」，两者都可能为空 —— 那是
    # 网络波动，不是这条逻辑的事。这里要钉的是**行为**：冷却中绝不去打 push2
    # （上面那条预算断言），以及**不再走 push2 的解析分支**。用一个假的
    # push2 探针把它钉死：冷却期它一次都不该被调用。
    _hit = []
    _orig_p2 = ds._push2_json
    ds._push2_json = lambda p, referer="x": (_hit.append(p) or {})
    ds._cache.pop("breadth", None)
    ds._cache_ts.pop("breadth", None)
    ds.fetch_breadth()
    ok(_hit == [], "★ 冷却中 fetch_breadth 完全没调用 push2（省下 3 秒空等）")
    ds._push2_json = _orig_p2
    ds._push2_recover()
    ok(ds._push2_cheap_skip() is False, "恢复后重新优先走 push2")

    print("[19] 回补去重状态落盘（每次重启不再重做一遍全量回补）")
    # 实测冷启动 7.2 秒里，4.2s 花在反复拉 6 个永远不会回来的假期，
    # 2.97s 花在重拉当天+前两天的收盘复盘 —— 而这些一天只可能变一次。
    kph._state()["failed"] = []
    kph._state()["repaired"] = []
    kph._state_save()
    ok(kph._gone("20261001") is False, "未记录的日期不算「已确认取不到」")
    kph._mark_gone("20261001")
    ok(kph._gone("20261001") is True, "标记后即视为取不到")
    kph._state_save()
    # 关键：跨进程也记得（重新从磁盘读，模拟看板重启）
    kph._KPH_STATE = None
    ok(kph._gone("20261001") is True, "★ 重启后仍然记得（落盘生效，不再重刷）")
    ok(kph._repaired_on("20261014") is False, "未对账的日期返回 False")
    kph._mark_repaired("20261014")
    kph._KPH_STATE = None
    ok(kph._repaired_on("20261014") is True, "对账记录同样跨进程生效")
    # 兼容旧接口：测试与旧代码会写 kph._repaired.clear()
    kph._repaired.clear()
    ok(kph._repaired_on("20261014") is False, "_repaired.clear() 能清掉落盘记忆")
    # 损坏的落盘文件不能让回补崩掉
    _p = kph._state_path()
    if _p:
        with open(_p, "w", encoding="utf-8") as f:
            f.write("{坏掉的 json")
        kph._KPH_STATE = None
        ok(kph._gone("20261001") is False, "状态文件损坏 → 当空处理（不抛异常）")
        kph._state_save()
        kph._KPH_STATE = None

    print()
    print(f"===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    sys.exit(main())
