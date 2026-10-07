# -*- coding: utf-8 -*-
"""全市场 A 股代码枚举。

沙箱环境下东财 clist 接口不可用，改为按交易所代码段规则枚举代码，
再用腾讯批量行情接口过滤出真实存在的标的。
"""
from __future__ import annotations

# (market前缀, 起始号, 结束号) —— 号段来自交易所代码编制规则
SEGMENTS = [
    ("sh", 600000, 605999),   # 上证主板 600/601/603/605
    ("sh", 688000, 689999),   # 科创板 688
    ("sz", 1, 3999),          # 深证主板 000/001/002/003
    ("sz", 300000, 301999),   # 创业板 300/301
    ("bj", 430000, 430999),   # 北交所 430
    ("bj", 830000, 839999),   # 北交所 83x
    ("bj", 870000, 879999),   # 北交所 87x
    ("bj", 920000, 920999),   # 北交所 920
]

# 名称/代码前缀黑名单：退市、ST 标记在后续过滤里做，这里先剔除明显无效段
INVALID_PREFIX = {
    ("sh", 605000),  # 保留，605 是有效段
}


def _zero_pad(num: int, width: int = 6) -> str:
    return str(num).zfill(width)


def enumerate_all() -> list[str]:
    """返回全市场候选代码列表，形如 ['sh600000', 'sz000001', ...]"""
    codes: list[str] = []
    for mkt, start, end in SEGMENTS:
        for n in range(start, end + 1):
            codes.append(f"{mkt}{_zero_pad(n)}")
    return codes


def to_qt_symbol(code: str) -> str:
    """内部代码 -> 腾讯接口符号（一致）。"""
    return code


def tx_prefix(market: str) -> str:
    return {"sh": "sh", "sz": "sz", "bj": "bj"}[market]


def split_code(code: str) -> tuple[str, str]:
    """'sh600000' -> ('sh', '600000')"""
    return code[:2], code[2:]


def to_secid(code: str) -> str:
    """转东财 secid：沪市 1.xxxxxx，深市/北 0.xxxxxx"""
    market, num = split_code(code)
    if market == "sh":
        return f"1.{num}"
    return f"0.{num}"


def board_of(code: str) -> str:
    market, num = split_code(code)
    if market == "sh":
        if num.startswith("688"):
            return "科创板"
        return "沪市主板"
    if market == "sz":
        if num.startswith(("300", "301")):
            return "创业板"
        return "深市主板"
    return "北交所"
