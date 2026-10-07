# -*- coding: utf-8 -*-
"""M9-S1 自动消息抓取与定级。

解决「消息定级别」需要手动录入的麻烦：
  1. 多源抓取 7x24 快讯（新浪财经直播 → 同花顺 → 东财快讯，逐级降级）；
  2. 用关键词规则给消息定级（S 国家级 / A 部委政策 / B 行业事件 / C 个股公告）；
  3. 与 M9 聚类出的主题材做关联匹配（题材名 + 常用别名），只让
     「与主题材相关」的消息参与定级 —— 别的题材的大消息不能给本题材抬轿。

产出是「系统建议级别」：手动录入（M9 面板 message_level）永远优先；
未录入时建议级别参与 S1 门槛判定，证据（命中新闻）随信号输出供人工复核。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import datasource as ds

# ---------------------------------------------------------------- 多源抓取

SINA_7X24 = ("https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size={n}"
             "&zhibo_id=152&tag_id=0")
THS_NEWS = ("https://news.10jqka.com.cn/tapp/news/push/stock/"
            "?page=1&tag=&track=website&pagesize={n}")
EM_FASTNEWS = ("https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
               "?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize={n}"
               "&req_trace={ts}")


def _news_from_sina(n: int) -> list[dict]:
    url = SINA_7X24.format(n=min(n, 100))
    raw = ds._get(url, timeout=10, referer="https://finance.sina.com.cn/7x24/").decode(
        "utf-8", errors="ignore")
    js = json.loads(raw)
    feed = ((((js.get("result") or {}).get("data") or {}).get("feed") or {}).get("list")) or []
    out = []
    for it in feed:
        text = (it.get("rich_text") or "").strip()
        if not text:
            continue
        out.append({"time": (it.get("create_time") or "")[:19],
                    "text": text,
                    "tags": [t.get("name", "") for t in (it.get("tag") or []) if t.get("name")],
                    "source": "新浪7x24"})
    return out


def _news_from_ths(n: int) -> list[dict]:
    url = THS_NEWS.format(n=min(n, 50))
    raw = ds._get(url, timeout=10, referer="https://news.10jqka.com.cn/").decode(
        "utf-8", errors="ignore")
    js = json.loads(raw)
    lst = ((js.get("data") or {}).get("list")) or []
    out = []
    for it in lst:
        text = (it.get("title") or "").strip()
        if not text:
            continue
        tstr = ""
        try:
            tstr = datetime.fromtimestamp(int(it.get("ctime") or it.get("rtime"))).strftime(
                "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError):
            pass
        out.append({"time": tstr, "text": text, "tags": [], "source": "同花顺"})
    return out


def _news_from_em(n: int) -> list[dict]:
    url = EM_FASTNEWS.format(n=min(n, 50), ts=int(datetime.now().timestamp() * 1000))
    raw = ds._get(url, timeout=10, referer="https://kuaixun.eastmoney.com/").decode(
        "utf-8", errors="ignore")
    js = json.loads(raw)
    lst = ((js.get("data") or {}).get("fastNewsList")) or []
    out = []
    for it in lst:
        text = (it.get("title") or it.get("summary") or "").strip()
        if not text:
            continue
        out.append({"time": (it.get("showTime") or it.get("time") or "")[:19],
                    "text": text,
                    "tags": [],
                    "source": "东财快讯"})
    return out


def fetch_news(n: int = 80) -> list[dict]:
    """近 N 条 7x24 快讯，多源降级，按时间倒序。5 分钟缓存。"""
    def producer():
        for src in (_news_from_sina, _news_from_ths, _news_from_em):
            try:
                items = src(n)
            except Exception:
                items = []
            if items:
                items.sort(key=lambda x: x["time"], reverse=True)
                return items
        return []
    return ds.cached("news:flash", 300, producer)


# ---------------------------------------------------------------- 题材关联

# 行业板块名 → 快讯常用别名（板块名是 4 字截断名，直接子串会漏配）
SECTOR_ALIASES: dict[str, list[str]] = {
    "半导体": ["芯片", "晶圆", "光刻", "存储", "封测", "先进制程"],
    "汽车零部": ["汽车零", "汽车部件", "汽车零配件", "整车", "汽车产业"],
    "电池": ["固态电池", "锂电池", "动力电池", "储能", "电解液", "负极", "正极"],
    "算力": ["算力", "数据中心", "服务器", "IDC", "智算"],
    "机器人": ["机器人", "人形", "减速器", "执行器", "灵巧手"],
    "AI应用": ["AI", "人工智能", "大模型", "智能体"],
    "通信设备": ["光模块", "光通信", "CPO", "6G", "通信"],
    "消费电子": ["消费电子", "折叠屏", "苹果链", "果链", "MR"],
    "白酒": ["白酒", "酒企"],
    "光伏设备": ["光伏", "组件", "硅片", "电池片"],
    "证券": ["券商", "证券行业"],
    "银行": ["银行"],
    "房地产": ["房地产", "楼市", "住房"],
    "军工": ["军工", "国防", "航空航天", "商业航天"],
    "低空经济": ["低空", "eVTOL", "通航", "无人机"],
    "创新药": ["创新药", "新药", "CXO", "医药"],
    "黄金": ["黄金", "金价", "贵金属"],
    "稳定币": ["稳定币", "数字货币", "跨境支付", "RWA"],
    "稀土永磁": ["稀土", "永磁", "磁材"],
    "航运港口": ["航运", "造船", "港口", "集运"],
}


def _theme_hit(text: str, theme_name: str) -> bool:
    """标题/正文是否与主题材相关（板块名双向子串 + 别名表）。"""
    if not theme_name:
        return False
    t = text
    if theme_name in t or (len(t) <= 6 and t in theme_name):
        return True
    for alias in SECTOR_ALIASES.get(theme_name, []):
        if alias in t:
            return True
    return False


# ---------------------------------------------------------------- 定级规则

# (级别, 关键词) —— 依序匹配，先命中先定级
LEVEL_RULES: list[tuple[str, list[str]]] = [
    ("S", ["国务院", "中共中央", "中央政治局", "总书记", "国家战略", "顶层设计",
           "中央经济工作", "全国统一大市场", "国家层面", "重大决策部署"]),
    ("A", ["发改委", "工信部", "工业和信息化部", "央行", "中国人民银行", "证监会",
           "财政部", "金融监管总局", "能源局", "商务部", "住建部", "农业农村部",
           "国家数据局", "网信办", "民航局", "药监局", "印发", "实施方案", "行动计划",
           "指导意见", "管理条例", "试点方案", "批复", "降准", "降息", "专项债",
           "产业规划", "规划纲要", "重磅政策"]),
    ("B", ["涨价", "提价", "上调价格", "价格上调", "缺货", "供不应求", "满产", "满销",
           "订单", "签约", "中标", "扩产", "投产", "量产", "出货量", "创新高", "超预期",
           "重大突破", "全球首发", "景气", "里程碑", "价格翻倍"]),
    ("C", ["业绩预告", "业绩快报", "减持", "回购股份", "问询函", "回复函", "关注函",
           "澄清", "传闻", "据传", "拟收购", "筹划重大资产重组", "中标公告"]),
]


def _grade_text(text: str) -> str:
    for lv, kws in LEVEL_RULES:
        for kw in kws:
            if kw in text:
                return lv
    return ""


def grade(news: list[dict], theme_name: str, lookback_h: int = 36) -> dict:
    """把快讯与主题材关联并定级。

    返回 {found, level, theme_matched, scanned, window_h, evidence:[{time,text,source,level}]}
    """
    now = datetime.now()
    cutoff = now - timedelta(hours=lookback_h)
    recent = []
    for it in news:
        t = it.get("time", "")
        try:
            dt = datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                dt = datetime.strptime(t[:10], "%Y-%m-%d")
            except ValueError:
                dt = now  # 时间解析失败的条目按最新处理，不丢
        if dt >= cutoff:
            recent.append(it)

    hits: list[dict] = []
    for it in recent:
        text = it["text"]
        if not _theme_hit(text, theme_name):
            continue
        lv = _grade_text(text)
        if lv:
            hits.append({**it, "level": lv})

    order = {"S": 0, "A": 1, "B": 2, "C": 3}
    # 稳定排序：先按时间倒序，再按级别升序 → 级别高的在前，同级内最新在前
    hits.sort(key=lambda h: h["time"], reverse=True)
    hits.sort(key=lambda h: order.get(h["level"], 9))

    if not hits:
        return {"found": False, "level": "", "theme_matched": bool(theme_name),
                "scanned": len(recent), "window_h": lookback_h, "evidence": []}
    top = hits[0]["level"]
    return {"found": True, "level": top, "theme_matched": True,
            "scanned": len(recent), "window_h": lookback_h,
            "evidence": [{"time": h["time"], "text": h["text"][:80], "source": h["source"],
                          "level": h["level"]} for h in hits[:3]]}
