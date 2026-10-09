# -*- coding: utf-8 -*-
"""M9-S1 自动消息抓取、定级与**消息记忆**。

解决「消息定级别」需要手动录入的麻烦：
  1. 多源抓取 7x24 快讯（财联社电报 → 新浪财经直播 → 央视 → 同花顺 →
     东财快讯，逐源容错）；财联社「重要」电报是编辑人工筛选的政策/宏观
     大事，精度与速度均优于通达式快讯；
  2. 用关键词规则给消息定级（S 国家级 / A 部委政策 / B 行业事件 / C 个股公告）；
  3. 与 M9 聚类出的主题材做关联匹配（题材名 + 常用别名），只让
     「与主题材相关」的消息参与定级 —— 别的题材的大消息不能给本题材抬轿；
  4. **消息记忆**：S/A/B 级结果逐日归档，在有效期内（S 20天 / A 10天 / B 5天）
     继续参与定级 —— 大级别政策不会因为日历翻页就失效。

为什么必须有第 4 条（实测教训）：快讯池只能往前翻两三天
（新浪滚动约 50 条/页、央视约 80 条/页，再深就没了），而
「工信部等七部门印发《新型电池产业发展"十五五"规划》」这种消息
发布三天后就翻不到了 —— 没有记忆的话，主线明明有国家级政策背书，
S1 却只能看到当天几条「公司澄清公告」（C级），把题材判成一日游。

产出是「系统建议级别」：手动录入（M9 面板 message_level）永远优先；
未录入时建议级别（含记忆）参与 S1 门槛判定，证据随信号输出供人工复核。
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta

from . import cls as cls_mod
from . import datasource as ds
from . import paths

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
# 央视新闻「中国」频道（jsonp 接口，80条/页）：部委联合发文类报道覆盖好，
# 关键信息常在摘要（brief）里而非标题 —— title+brief 拼接参与匹配
CCTV_PAGE = ("https://news.cctv.com/2019/07/gaiban/cmsdatainterface/page/"
             "china_{page}.jsonp?cb=news")


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


def _news_from_cctv(n: int, page: int = 1) -> list[dict]:
    """央视新闻「中国」频道。text = 标题 + 摘要（部委发文的关键词常在
    摘要里，如「工信部等七部门印发《新型电池产业发展十五五规划》」
    这条，标题只提产业规模、定级全靠摘要）。"""
    url = CCTV_PAGE.format(page=page)
    raw = ds._get(url, timeout=10, referer="https://news.cctv.com/china/").decode(
        "utf-8", errors="ignore")
    body = raw[raw.index("(") + 1: raw.rindex(")")]
    lst = ((json.loads(body).get("data") or {}).get("list")) or []
    out = []
    for it in lst:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        try:
            tstr = datetime.strptime(str(it.get("focus_date"))[:19],
                                     "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue   # 无可靠时间的条目不收，避免污染定级窗口
        brief = (it.get("brief") or "").strip()
        text = f"{title}。{brief[:80]}" if brief else title
        out.append({"time": tstr, "text": text, "tags": [], "source": "央视新闻"})
    return out[:n]


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


def _news_from_cls() -> list[dict]:
    """财联社电报（重要分类，近 2-3 天）—— 编辑人工筛选的政策/宏观大事。

    精度与速度均优于通达式快讯，是部委发文/国家战略类消息的第一手源。
    """
    try:
        return cls_mod.telegraph()
    except Exception as e:
        print(f"[news] 财联社电报抓取失败: {e}", file=sys.stderr)
        return []


def fetch_news(n: int = 2000) -> list[dict]:
    """近 N 条快讯/新闻。五源多页合并去重（按正文），时间倒序，5 分钟缓存。

    抓取深度（v1.16 加深）：新浪7x24 两页 + **新浪滚动十六页**（约回溯 3 天）+
    **央视新闻六页**（约回溯一周，部委发文报道最全 —— 七部门联合印发
    「新型电池产业发展十五五规划」那条就在第 3 页）+ 同花顺两页 + 东财一页。
    上次只抓央视两页，把第 3 页的政策消息漏掉了（实测教训）。
    单源失败只丢该源，stderr 留痕便于排查。
    """
    def producer():
        merged: dict[str, dict] = {}
        # 财联社电报（重要分类）：政策/宏观大事最快的源，放最前面
        for it in _news_from_cls():
            merged.setdefault(it["text"], it)
        for pg in (1, 2):
            try:
                for it in _news_from_sina(100, pg):
                    merged.setdefault(it["text"], it)
            except Exception as e:
                print(f"[news] 新浪7x24 第{pg}页抓取失败: {e}", file=sys.stderr)
        for pg in range(1, 17):
            try:
                for it in _news_from_sina_roll(50, pg):
                    merged.setdefault(it["text"], it)
            except Exception as e:
                print(f"[news] 新浪滚动 第{pg}页抓取失败: {e}", file=sys.stderr)
        for pg in range(1, 7):
            try:
                for it in _news_from_cctv(80, pg):
                    merged.setdefault(it["text"], it)
            except Exception as e:
                print(f"[news] 央视新闻 第{pg}页抓取失败: {e}", file=sys.stderr)
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


# ---------------------------------------------------------------- 消息记忆
# 快讯池只能翻两三天（新浪滚动/央视都按页丢弃旧闻），大级别政策发出去
# 几天后就「查无此消息」。所以把每次 S/A/B 级定级结果逐日归档：
# 在有效期内（S 20天 / A 10天 / B 5天）持续参与 S1 判定。
# C 级（个股公告/传闻）不记 —— 那是噪声，记住反而天天干扰定级。

MEM_PATH = paths.data_path("news_memory.json")
LEVEL_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3}
VALID_DAYS = {"S": 20, "A": 10, "B": 5}          # 各级消息的记忆有效期（自然日）


def _load_memory() -> list[dict]:
    try:
        with open(MEM_PATH, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("entries") or []
    except (OSError, json.JSONDecodeError, ValueError):
        return []


def _save_memory(entries: list[dict]) -> None:
    try:
        os.makedirs(os.path.dirname(MEM_PATH), exist_ok=True)
        tmp = MEM_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"entries": entries[-400:]}, f, ensure_ascii=False)
        os.replace(tmp, MEM_PATH)
    except OSError:
        pass


def remember(real_date: str, theme: str, level: str,
             evidence: list[dict] | None = None, note: str = "") -> None:
    """归档一次 S/A/B 级定级结果（同日同题材取最高级别，证据合并去重）。"""
    if not theme or level not in VALID_DAYS:
        return
    entries = _load_memory()
    items = [{"time": str(i.get("time", "")), "text": (i.get("text") or "")[:80],
              "source": i.get("source", "")} for i in (evidence or [])[:5]]
    row = next((e for e in entries
                if e.get("date") == real_date and e.get("theme") == theme), None)
    if row is None:
        entries.append({"date": real_date, "theme": theme, "level": level,
                        "items": items, "note": note, "saved": time.time()})
    else:
        if LEVEL_ORDER.get(level, 9) < LEVEL_ORDER.get(row.get("level", "C"), 9):
            row["level"] = level
        known = {i.get("text") for i in row.get("items", [])}
        row.setdefault("items", []).extend(i for i in items if i["text"] not in known)
        row["items"] = row["items"][:6]
        if note:
            row["note"] = note
    _save_memory(sorted(entries, key=lambda e: e.get("date", "")))


def memory_entries(theme: str = "") -> list[dict]:
    """给界面/自查用：全部（或某题材的）记忆条目。"""
    entries = _load_memory()
    if theme:
        entries = [e for e in entries if e.get("theme") == theme]
    return sorted(entries, key=lambda e: e.get("date", ""), reverse=True)


def lookup(theme: str, real_date: str = "") -> dict | None:
    """查题材的消息记忆：有效期内的最高级别 S/A/B。无 → None。"""
    if not theme:
        return None
    try:
        today = (datetime.strptime(real_date, "%Y%m%d")
                 if real_date else datetime.now())
    except ValueError:
        today = datetime.now()
    best = None
    for e in _load_memory():
        if e.get("theme") != theme:
            continue
        lv = e.get("level", "")
        if lv not in VALID_DAYS:
            continue
        try:
            d = datetime.strptime(e.get("date", ""), "%Y%m%d")
        except ValueError:
            continue
        if d > today:
            continue
        age = (today - d).days
        if age > VALID_DAYS[lv]:
            continue                                  # 过有效期
        if best is None or LEVEL_ORDER[lv] < LEVEL_ORDER[best["level"]]:
            best = {"level": lv, "date": e.get("date", ""), "days": age,
                    "items": e.get("items", []), "note": e.get("note", "")}
    return best


def merge_suggestion(suggestion: dict | None, mem: dict | None) -> dict:
    """把消息记忆并入今日自动定级 —— 级别取更高者。

    记忆更高时：以记忆为准，今日级别存进 today_level 备查（证据换成记忆里的
    原始消息）。记忆更低时：只作为佐证附在 memory 字段里。
    """
    sug = dict(suggestion or {"found": False})
    if not mem:
        return sug
    mem_lv = mem.get("level", "")
    sug_lv = sug.get("level") if sug.get("found") else ""
    if not sug_lv or LEVEL_ORDER.get(mem_lv, 9) < LEVEL_ORDER.get(sug_lv, 9):
        return {**sug, "found": True, "level": mem_lv, "theme_matched": True,
                "evidence": (mem.get("items") or sug.get("evidence") or []),
                "memory": {"date": mem.get("date"), "level": mem_lv,
                           "days": mem.get("days", 0)},
                "today_level": sug_lv,
                "today_evidence": sug.get("evidence") or []}
    sug["memory"] = {"date": mem.get("date"), "level": mem_lv,
                     "days": mem.get("days", 0)}
    return sug
