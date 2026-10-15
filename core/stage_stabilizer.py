# -*- coding: utf-8 -*-
"""情绪阶段滞回稳定器 —— 专治「一会儿说可以出手、一会儿又退潮」。

## 问题
退潮判定（M3 与 E1）都建立在**瞬时**的涨停 / 跌停 / 炸板计数上：

    亏钱面积 = (跌停 + 炸板) / (涨停 + 跌停 + 炸板)  ≥ 0.30 → 退潮

盘中这三个数一直在跳。10:30 一波炸板把面积顶过 0.30 就报「退潮、无条件清仓」，
十分钟后新涨停补上来又变回「可出手」。页面上策略建议来回横跳，
人根本没法照着执行 —— 这不是市场变了，是**采样噪声**。

## 做法（施密特触发器 + 连续确认）
- **进入退潮**：原始信号连续 `enter_n` 次、且跨越 `enter_span` 秒才认。
  但「亏钱面积 ≥ 阈值 + 强信号余量」可以直接认 —— 真塌方不用等确认。
- **退出退潮**：原始信号连续 `exit_n` 次、跨越 `exit_span` 秒，**并且**
  亏钱面积回落到「阈值 − 退出余量」以下才认。出门比进门难：
  宁可多等一会儿，也别刚清完仓又追回去（这是最伤人的反复）。
- **开盘保护**：开盘头 `warmup_min` 分钟样本太薄（涨停没几家，分母小、
  比例噪声极大），此时只认强信号，否则每天 9:35 都会先报一次「退潮」。
- **状态四档**：calm 正常 / watch 退潮预警（未确认，只降仓不清仓）/
  ebb 确认退潮 / recovering 缓解中（仍是退潮，但提示可以留意了）。
  ★ recovering 与"解除退潮"用**同一套滞回**（连续 exit_n 次 + 跨越 exit_span
  秒）：只按单帧面积回落就切的话，面积在阈值附近抖动时会「退潮 ↔ 缓解中」
  每个刷新来回跳一次（2026-10-15 实测修复）。

状态按自然日持久化在 data/stage_state.json，跨进程重启不丢；
换日自动归零。
"""
from __future__ import annotations

import json
import os
import threading
import time

from . import paths

STATE_PATH = paths.data_path("stage_state.json")
_lock = threading.Lock()
_memo: dict | None = None

# 默认阈值（可被 cfg 覆盖）
DEFAULTS = {
    "enter_n": 2,            # 连续几次原始信号才确认进入
    "enter_span": 60,        # 且至少跨越多少秒
    "strong_margin": 0.15,   # 超出阈值这么多 → 直接认定（不等确认）
    "exit_n": 2,
    "exit_span": 180,        # 退出比进入慢：180s vs 60s
    "exit_margin": 0.10,     # 还要回落到阈值 -0.10 以下
    "warmup_min": 30,        # 开盘头 30 分钟只认强信号
}


def _cfg(cfg: dict | None) -> dict:
    a = (cfg or {}).get("M3_sentiment", {}) or {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        v = a.get("ebb_" + k, a.get(k))
        if isinstance(v, (int, float)):
            out[k] = v
    # 允许从 E1 的配置里也读一遍（两个模块谁先调用都行）
    b = (cfg or {}).get("E1_meter", {}) or {}
    for k in DEFAULTS:
        v = b.get("ebb_" + k)
        if isinstance(v, (int, float)) and "ebb_" + k not in a:
            out[k] = v
    return out


def _blank(date: str) -> dict:
    return {"date": date, "confirmed": False, "state": "calm",
            "raw": False, "raw_true_n": 0, "raw_false_n": 0,
            "raw_true_since": 0.0, "raw_false_since": 0.0,
            "since": 0.0, "updates": 0, "note": ""}


def _load(date: str) -> dict:
    global _memo
    if _memo and _memo.get("date") == date:
        return dict(_memo)
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if obj.get("date") == date:
            _memo = obj
            return dict(obj)
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    _memo = _blank(date)
    return dict(_memo)


def _save(obj: dict) -> None:
    global _memo
    _memo = dict(obj)
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def reset(date: str = "") -> None:
    """清状态（换日 / 手动复位 / 测试用）。"""
    global _memo
    with _lock:
        _memo = _blank(date)
        _save(_memo)


def _cmp(hurt: float, threshold: float, margin: float, up: bool) -> bool:
    """带余量的阈值比较。

    必须先归一再比：0.30 - 0.10 在浮点下是 0.19999999999999998，
    直接拿 0.20 <= 0.19999999999999998 去判会**永远不成立**，
    结果就是"退潮再也解除不了"。
    """
    a = round(hurt, 4)
    b = round(threshold + margin if up else threshold - margin, 4)
    return a >= b if up else a <= b


def _alive_note(st: dict, hurt_area: float, threshold: float, c: dict) -> str:
    """没有发生状态切换时，给一句"当前读数"的说明。

    只在切换时写 note 的话，页面会长期挂着切换那一刻的旧数字
    （实测：面积已回落到 15%，页面上仍写"50% 已超阈值一个身位"）。
    """
    pct = f"{hurt_area*100:.0f}%"
    th = f"{threshold*100:.0f}%"
    if st.get("confirmed"):
        if st.get("state") == "recovering":
            return (f"亏钱面积已回落至 {pct}（阈值 {th} 下方 {c['exit_margin']*100:.0f} 个点），"
                    f"连续 {st.get('raw_false_n', 0)} 次确认缓解中 —— "
                    f"仍是退潮期，仓位不改，只是别再加空")
        return (f"退潮中：亏钱面积 {pct}（阈值 {th}），"
                f"原始信号已连续 {st.get('raw_true_n', 0)} 次 —— 按退潮执行")
    if st.get("state") == "watch":
        return (f"退潮预警未确认：亏钱面积 {pct}（阈值 {th}），"
                f"原始信号已连续 {st.get('raw_true_n', 0)} 次 —— 先降仓、别急着清")
    return f"情绪平稳：亏钱面积 {pct}（阈值 {th}）"


def stabilize(real_date: str, raw_ebb: bool, hurt_area: float,
              threshold: float = 0.30, tmin: int | None = None,
              cfg: dict | None = None) -> dict:
    """喂一次原始判定，返回**稳定后**的结论。

    real_date 换日即重置；tmin 为当日已交易分钟数（None 表示不启用开盘保护）。
    """
    c = _cfg(cfg)
    now = time.time()
    with _lock:
        st = _load(real_date)
        st["updates"] = int(st.get("updates", 0)) + 1
        st["raw"] = bool(raw_ebb)

        # ── 累计连续样本 ──
        if raw_ebb:
            if not st.get("raw_true_since"):
                st["raw_true_since"] = now
            st["raw_true_n"] = int(st.get("raw_true_n", 0)) + 1
            st["raw_false_n"] = 0
            st["raw_false_since"] = 0.0
        else:
            if not st.get("raw_false_since"):
                st["raw_false_since"] = now
            st["raw_false_n"] = int(st.get("raw_false_n", 0)) + 1
            st["raw_true_n"] = 0
            st["raw_true_since"] = 0.0

        warmed = (tmin is None) or (tmin >= c["warmup_min"])
        strong = _cmp(hurt_area, threshold, c["strong_margin"], up=True)
        confirmed = bool(st.get("confirmed"))
        prev = confirmed
        note = ""

        if not confirmed:
            # 进门：强信号直接认；否则要连续 + 跨时间 + 过了开盘保护期
            if strong:
                confirmed = True
                note = (f"亏钱面积 {hurt_area*100:.0f}% 已超阈值 "
                        f"{threshold*100:.0f}% 一个身位，直接确认退潮")
            elif (st["raw_true_n"] >= c["enter_n"]
                  and now - st["raw_true_since"] >= c["enter_span"]
                  and warmed):
                confirmed = True
                note = (f"退潮信号连续 {st['raw_true_n']} 次、"
                        f"持续 {now - st['raw_true_since']:.0f}s 才确认")
        else:
            # 出门：要连续 + 跨更长时间 + 面积明显回落
            clear = (not raw_ebb) and _cmp(hurt_area, threshold,
                                           c["exit_margin"], up=False)
            if (clear and st["raw_false_n"] >= c["exit_n"]
                    and now - st["raw_false_since"] >= c["exit_span"]):
                confirmed = False
                note = (f"亏钱面积回落到 {hurt_area*100:.0f}%（阈值 "
                        f"{threshold*100:.0f}% 下方 {c['exit_margin']*100:.0f} 个点）"
                        f"并持续 {now - st['raw_false_since']:.0f}s，解除退潮")

        if confirmed and not prev:
            st["since"] = now
            st["state"] = "ebb"
        elif not confirmed and prev:
            st["since"] = now
            st["state"] = "calm"

        # 状态细分：未确认但有预警 / 已确认但正在缓解
        if confirmed:
            clear = (not raw_ebb) and _cmp(hurt_area, threshold,
                                           c["exit_margin"], up=False)
            # ★ 2026-10-15 修：recovering 也要走滞回，不能看单帧。
            #   原来只要这一帧"原始信号为假且面积回落"就切 recovering，
            #   下一帧面积弹回又切回 ebb —— 页面上就是「退潮/缓解中」来回跳。
            #   这里的规矩与"解除退潮"完全一致：连续 exit_n 次、跨越
            #   exit_span 秒才算缓解；只是**不改 confirmed**（仍按退潮执行）。
            #   迟滞量足够让噪声进不来，又不影响真正消退时给出提示。
            eased = (clear and st["raw_false_n"] >= c["exit_n"]
                     and now - st["raw_false_since"] >= c["exit_span"])
            st["state"] = "recovering" if eased else "ebb"
        elif raw_ebb:
            st["state"] = "watch"          # 退潮预警：还没有确认
        else:
            st["state"] = "calm"

        st["confirmed"] = confirmed
        st["hurt_area"] = round(hurt_area, 3)
        st["threshold"] = threshold
        st["warmed"] = warmed
        st["strong"] = strong
        # ★ 2026-10-15 修：note 只在**状态切换**时写，切换后一直留着老数字——
        #   实测已经出现「面积 50% 已超阈值」这句挂在 hurt_area=0.15 的帧上，
        #   页面上等于一直报旧数。这里每次刷新都把当前读数补进去。
        st["note"] = note or _alive_note(st, hurt_area, threshold, c)
        st["last_ts"] = now
        _save(st)

        since = st.get("since") or now
        return {
            "confirmed": confirmed,
            "state": st["state"],
            "raw": bool(raw_ebb),
            "changed": confirmed != prev,
            "note": st["note"],
            "hurt_area": round(hurt_area, 3),
            "threshold": threshold,
            "warmed": warmed,
            "strong": strong,
            "raw_streak": st["raw_true_n"] if raw_ebb else st["raw_false_n"],
            "since_seconds": int(now - since),
            "enter_n": c["enter_n"], "exit_n": c["exit_n"],
            "enter_span": c["enter_span"], "exit_span": c["exit_span"],
            "warmup_min": c["warmup_min"],
        }


def peek(real_date: str, ready: bool = True) -> dict:
    """只读当前稳定结论（不喂新样本）—— 让 E1 与 M3 保持同一口径。

    ★ 数据未就绪时必须返回 confirmed=False：`_load()` 读的是**按自然日持久化
    的磁盘文件**（data/stage_state.json），换日自动归零但**同一交易日跨进程
    重启不丢**。冷启动那一刻三池还没抓到，文件里却可能已经躺着当天早些时候
    确认过的退潮 —— 闸门若不在这里把结论按住，"没数据"就会被读成"确认退潮"，
    用户看到的还是那个顽固 bug（首屏先喊「可以出手」）。未就绪时按上一轮真实
    结论会读出「确认退潮」，方向**偏保守**，不会造成误放行。
    """
    with _lock:
        st = _load(real_date)
    if not ready:
        return {"confirmed": False, "state": "calm",
                "hurt_area": st.get("hurt_area", 0.0), "note": st.get("note", "")}
    return {"confirmed": bool(st.get("confirmed")),
            "state": st.get("state", "calm"),
            "hurt_area": st.get("hurt_area", 0.0),
            "note": st.get("note", "")}


STATE_CN = {
    "calm": "情绪平稳",
    "watch": "退潮预警（未确认）",
    "ebb": "确认退潮",
    "recovering": "退潮缓解中",
}
