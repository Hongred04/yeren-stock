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
import sys
from datetime import datetime, timedelta

from . import datasource as ds

# ---------------------------------------------------------------- 多源抓取

SINA_7X24 = ("https://zhibo.sina.com.cn/api/zhibo/feed?page={page}&page_size={n}"
             "&zhibo_id=152&tag_id=0")
THS_NEWS = ("https://news.10jqka.com.cn/tapp/news/push/stock/"
            "?page={page}&tag=&track=website&pagesize={n}")
EM_FASTNEWS = ("https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
               "?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize={n}"
               "&req_trace={ts}")
# 新浪滚动财经（标准新闻流）：比 7x24 快讯留存久得多，部委政策类标题覆盖好
SINA_ROLL = ("https://feed.mix.sina.com.cn/api/roll/get?"
             "pageid=153&lid=2516&num={n}&page={page}")


def _news_from_sina(n: int, page: int = 1) -> list[dict]:
    url = SINA_7X24.format(page=page, n=min(n, 100))
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


def _news_from_ths(n: int, page: int = 1) -> list[dict]:
    url = THS_NEWS.format(page=page, n=min(n, 50))
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


def _news_from_sina_roll(n: int, page: int = 1) -> list[dict]:
    """新浪滚动财经新闻流（标题 + 时间戳）。"""
    url = SINA_ROLL.format(n=min(n, 50), page=page)
    raw = ds._get(url, timeout=10, referer="https://finance.sina.com.cn/roll/").decode(
        "utf-8", errors="ignore")
    js = json.loads(raw)
    lst = ((js.get("result") or {}).get("data")) or []
    out = []
    for it in lst:
        text = (it.get("title") or "").strip()
        if not text:
            continue
        try:
            tstr = datetime.fromtimestamp(int(it.get("ctime"))).strftime(
                "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError):
            tstr = (it.get("intime") or "")[:19]
        out.append({"time": tstr, "text": text, "tags": [], "source": "新浪滚动"})
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


def fetch_news(n: int = 650) -> list[dict]:
    """近 N 条快讯/新闻。四源多页合并去重（按正文），时间倒序，5 分钟缓存。

    旧版「取到第一个源就返回」且只拉第一页 80 条 —— 热闹时段只覆盖一两个
    小时，隔夜部委政策（如工信部印发产业规划）早被挤出窗口，S1 因此漏定级。
    现在新浪7x24两页 + 新浪滚动八页（实测第5页回溯到昨日下午、第8页到前日
    晚上，配合交易日窗口覆盖假期）+ 同花顺两页 + 东财一页全部合并；
    单源失败只丢该源，stderr 留痕便于排查。
    """
    def producer():
        merged: dict[str, dict] = {}
        for pg in (1, 2):
            try:
                for it in _news_from_sina(100, pg):
                    merged.setdefault(it["text"], it)
            except Exception as e:
                print(f"[news] 新浪7x24 第{pg}页抓取失败: {e}", file=sys.stderr)
        for pg in (1, 2, 3, 4, 5, 6, 7, 8):
            try:
                for it in _news_from_sina_roll(50, pg):
                    merged.setdefault(it["text"], it)
            except Exception as e:
                print(f"[news] 新浪滚动 第{pg}页抓取失败: {e}", file=sys.stderr)
        for pg in (1, 2):
            try:
                for it in _news_from_ths(50, pg):
                    merged.setdefault(it["text"], it)
            except Exception as e:
                print(f"[news] 同花顺 第{pg}页抓取失败: {e}", file=sys.stderr)
        try:
            for it in _news_from_em(50):
                merged.setdefault(it["text"], it)
        except Exception as e:
            print(f"[news] 东财快讯抓取失败: {e}", file=sys.stderr)
        items = sorted(merged.values(), key=lambda x: x["time"], reverse=True)
        return items[:n]
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
           "产业规划", "规划纲要", "十五五", "重磅政策"]),
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


def grade(news: list[dict], theme_name: str, lookback_h: int = 36,
          window_start: datetime | None = None) -> dict:
    """把快讯与主题材关联并定级。

    窗口：默认近 36 小时；window_start（上上个交易日 00:00，由 mainline 传
    入）会更宽地展开 —— 长假/周末期间部委密集发文，开市首日的题材定级
    必须能吃到假期消息，按自然小时算会漏。

    返回 {found, level, theme_matched, scanned, window_h, since, evidence:[...]}
    """
    now = datetime.now()
    cutoff = now - timedelta(hours=lookback_h)
    if window_start is not None:
        cutoff = min(cutoff, window_start)
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
                "scanned": len(recent), "window_h": lookback_h,
                "since": cutoff.strftime("%m-%d %H:%M"), "evidence": []}
    top = hits[0]["level"]
    return {"found": True, "level": top, "theme_matched": True,
            "scanned": len(recent), "window_h": lookback_h,
            "since": cutoff.strftime("%m-%d %H:%M"),
            "evidence": [{"time": h["time"], "text": h["text"][:80], "source": h["source"],
                          "level": h["level"]} for h in hits[:3]]}
