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


def normalize(code: str) -> str:
    """把用户输入的代码规范成内部格式（带 sh/sz/bj 前缀）。

    用户手输自选/持仓时常常只写 6 位数字（603466、000001、300750），
    或写成 603466.SH / SZ000001 这类券商格式 —— 行情接口只认
    sh600519 / sz000001 / bj430047。这里统一补齐前缀；识别不了就原样返回，
    交给调用方报错。
    """
    if not code:
        return ""
    s = str(code).strip().lower().replace(" ", "")
    # 去掉交易所后缀：603466.sh / 000001.sz / 600519.ss
    for suf in (".sh", ".sz", ".bj", ".ss", ".sse", ".szse"):
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    # 已是带前缀格式
    if len(s) >= 8 and s[:2] in ("sh", "sz", "bj") and s[2:].isdigit():
        return s[:2] + s[2:8]
    # 纯数字（可能不足 6 位，补零）
    if s.isdigit():
        num = s.zfill(6)
        if len(num) != 6:
            return code
        if num.startswith(("600", "601", "603", "605", "688", "689")):
            return "sh" + num
        if num.startswith(("000", "001", "002", "003", "300", "301")):
            return "sz" + num
        if num.startswith(("430", "830", "831", "832", "833", "834", "835",
                           "836", "837", "838", "839", "870", "871", "872",
                           "873", "874", "875", "876", "877", "920")):
            return "bj" + num
        # 兜底：5/6 开头给沪、其余给深（覆盖 002 之类老段）
        return ("sh" if num.startswith("5") or num.startswith("6") else "sz") + num
    return code


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
