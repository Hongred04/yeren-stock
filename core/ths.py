# -*- coding: utf-8 -*-
"""同花顺数据源（扩展数据源）。

协议知识借鉴自开源项目 fleetinglife/levistock（MIT）的接口整理，
用标准库重写，不引入任何第三方依赖。

当前提供：
  hot_rank()  人气股排行榜 —— 散户关注度维度。

为什么值得有：资金面（东财）、情绪面（E1）、题材面（M9）都在看
「钱在干什么」，但少了「人（散户）在关注什么」。人气榜前排的票
接力盘更足、关注度高；涨停股若同时是「9天9板 / 首板涨停」这类
人气标签的持有者，是市场共识的直接标注。数据只进详情页一行字，
不摆原始榜单。

接口公开、无鉴权、小时级更新；失败静默返回空值。
"""
from __future__ import annotations

import json
import sys

from . import datasource as ds
from . import universe

HOT_URL = ("https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"
           "?stock_type=a&type=hour&list_type=normal")


def hot_rank() -> dict[str, dict]:
    """人气榜 → {code: {"rank": int, "tag": "人气标签"}}（前 100 名）。

    tag 例：'9天9板' / '首板涨停' / '持续上榜'；部分股票无标签返回 ''。
    失败返回 {}。
    """
    def producer():
        raw = ds._get(HOT_URL, timeout=10,
                      referer="https://www.10jqka.com.cn/").decode(
            "utf-8", errors="ignore")
        js = json.loads(raw)
        if js.get("status_code") != 0:
            raise ValueError(f"ths: status={js.get('status_code')}")
        lst = (js.get("data") or {}).get("stock_list") or []
        out: dict[str, dict] = {}
        for i, it in enumerate(lst[:100], start=1):
            code = universe.normalize(str(it.get("code") or "").strip())
            if not code:
                continue
            tag = it.get("tag") or {}
            popular = (tag.get("popularity_tag") or "").strip()
            out[code] = {"rank": i, "tag": popular}
        return out
    try:
        return ds.cached("ths:hot", 600, producer)
    except Exception as e:
        print(f"[ths] 人气榜抓取失败: {e}", file=sys.stderr)
        return {}
