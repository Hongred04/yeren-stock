# -*- coding: utf-8 -*-
"""信号区分度检验（G1–G4）—— 回答「这个评分到底分不分得开」。

为什么需要它
------------
系统里有好几个打分（美学评分 / 题材热度 / B1 各因子）。它们都在**算分**，
但从来没验过这些分**有没有区分度**：

  · 如果 82% 的股票都被打进了最高档，那"最高分"等于没打 —— 这叫**塌缩**；
  · 如果顶档和底档的收益差不多，那这个分**方向可能是错的或无效的**；
  · 如果各档之间收益忽高忽低不成单调，那这个分在**噪音区间**里。

同类项目 ticai-daban 的 `qscore` 因子实测顶档占全市场 82.7% —— 就是塌缩。
本模块把这套检验做成可复用的：对任意「(标的, 分值, 结果)」三元组给出
四项判定，谁塌缩、谁无效、谁可用，一次说清。

四项判据（G1–G4）
----------------
  G1 顶档占比   < 40%        —— > 70% 视为塌缩（分数挤在最高档）
  G2 档数       ≥ 4 且最大档 < 50%  —— 档太少或某档独占，说明切不开
  G3 顶/底 lift > 1.3×       —— 顶档结果均值 / 底档结果均值
  G4 档间单调性 Spearman |ρ| > 0.8 —— 各档中位结果与档位序号的秩相关

★ 样本量声明：档内样本 < MIN_BIN 时该档不参与 G3/G4，并在结论里注明
   "样本不足"，绝不拿 3 个样本算出一个 lift 说有效。
"""
from __future__ import annotations

from typing import Any

MIN_BIN = 8              # 每档最少样本，低于此该档不参与升降比较
MIN_TOTAL = 40           # 全样本下限，低于此不出结论
G1_COLLAPSE = 0.70       # 顶档占比超过此值 = 塌缩
G1_OK = 0.40             # 期望的顶档占比上限
G3_OK = 1.30             # 顶/底 lift 门槛
G4_OK = 0.80             # 秩相关门槛


def _f(v, d=None):
    try:
        if v is None:
            return d
        f = float(v)
        return f if f == f and abs(f) != float("inf") else d
    except (TypeError, ValueError):
        return d


def _rank(xs: list[float]) -> list[float]:
    """平均秩（并列取平均）—— Spearman 需要它。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def _spearman(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 3:
        return None
    ra, rb = _rank(a), _rank(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    da = sum((ra[i] - ma) ** 2 for i in range(n)) ** 0.5
    db = sum((rb[i] - mb) ** 2 for i in range(n)) ** 0.5
    if da == 0 or db == 0:
        return None
    return num / (da * db)


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def evaluate(rows: list[dict], bins: int = 5, name: str = "",
             score_key: str = "score", ret_key: str = "ret") -> dict:
    """对一组 (score, ret) 做 G1–G4 检验。

    rows: [{"score": 82.0, "ret": 0.031, ...}, ...] —— 其它字段原样忽略。
    bins: 分档数（默认 5 档）。按 score 的**分位数**切，不用固定宽度
          （固定宽度在分数挤堆时会切出空档）。
    返回 {name, n, bins:[{lo,hi,n,mean,median}], g1..g4, verdict, note}
    """
    pairs = []
    for r in rows or []:
        s = _f(r.get(score_key))
        v = _f(r.get(ret_key))
        if s is None or v is None:
            continue
        pairs.append((s, v))
    n = len(pairs)
    out: dict[str, Any] = {"name": name, "n": n, "usable": False,
                           "bins": [], "verdict": "", "note": ""}
    if n < MIN_TOTAL:
        out["verdict"] = "样本不足"
        out["note"] = (f"只有 {n} 条有效样本（需要 ≥{MIN_TOTAL}）—— "
                       f"这么点样本上算出来的 lift 和单调性都不可信，先攒样本")
        return out

    pairs.sort(key=lambda x: x[0])
    scores = [p[0] for p in pairs]
    # 分位数切档（并列值不影响，切点按下标算）
    edges = []
    for i in range(1, bins):
        edges.append(scores[min(int(n * i / bins), n - 1)])
    groups: list[list[tuple[float, float]]] = [[] for _ in range(bins)]
    for s, v in pairs:
        k = 0
        for e in edges:
            if s >= e:
                k += 1
        groups[min(k, bins - 1)].append((s, v))

    bill = []
    for i, g in enumerate(groups):
        rets = [x[1] for x in g]
        bill.append({
            "bin": i + 1,
            "lo": g[0][0] if g else None,
            "hi": g[-1][0] if g else None,
            "n": len(g),
            "mean": round(sum(rets) / len(rets), 4) if rets else None,
            "median": round(_median(rets), 4) if rets else None,
        })
    out["bins"] = bill

    # ── G1 顶档占比 ──────────────────────────────────────────────
    top = groups[-1]
    g1 = len(top) / n
    out["g1_top_share"] = round(g1, 3)
    out["g1_pass"] = g1 < G1_COLLAPSE
    out["g1_collapsed"] = g1 > G1_COLLAPSE

    # ── G2 档数 / 最大档占比 ─────────────────────────────────────
    nonempty = [g for g in groups if len(g) >= MIN_BIN]
    biggest = max((len(g) for g in groups), default=0) / n if n else 0
    out["g2_bins_used"] = len(nonempty)
    out["g2_biggest_share"] = round(biggest, 3)
    out["g2_pass"] = len(nonempty) >= 4 and biggest < 0.50

    # ── G3 顶/底 lift ────────────────────────────────────────────
    usable = [g for g in groups if len(g) >= MIN_BIN]
    lift = None
    if len(usable) >= 2:
        lo_m = sum(x[1] for x in usable[0]) / len(usable[0])
        hi_m = sum(x[1] for x in usable[-1]) / len(usable[-1])
        # 底档均值接近 0 时 lift 会爆炸（除以 0.001 = 1000×），改用差值
        if abs(lo_m) < 1e-4:
            lift = None
            out["g3_note"] = "底档均值接近 0，lift 不稳定，改看差值"
            out["g3_delta"] = round(hi_m - lo_m, 4)
        else:
            lift = hi_m / lo_m if lo_m > 0 else None
        if lift is not None:
            out["g3_lift"] = round(lift, 3)
            out["g3_pass"] = lift > G3_OK
        else:
            out["g3_pass"] = (hi_m - lo_m) > 0.01
    else:
        out["g3_pass"] = False

    # ── G4 档间单调性 ────────────────────────────────────────────
    # ★ 判的是「有没有稳定方向」(|ρ|)，不是「方向对不对」—— 一个完全反向的
    #   因子也是强信号（只是要用负权重），把它判成"不单调"会误导使用者去
    #   丢掉一个有信息量的因子。方向问题单独在 g4_note 里点出。
    xs = [b["bin"] for b in bill if b["n"] >= MIN_BIN and b["mean"] is not None]
    ys = [b["mean"] for b in bill if b["n"] >= MIN_BIN and b["mean"] is not None]
    rho = _spearman([float(x) for x in xs], ys)
    out["g4_rho"] = round(rho, 3) if rho is not None else None
    out["g4_pass"] = bool(rho is not None and abs(rho) > G4_OK)
    out["g4_reversed"] = bool(rho is not None and rho < -G4_OK)
    if out["g4_reversed"]:
        out["g4_note"] = ("★ 方向是反的：分数越高结果越差。"
                          "信号本身有效（单调），但要用负权重或反过来解释")

    # ── 结论 ────────────────────────────────────────────────────
    # 判据顺序有意如此：先说结构性问题（塌缩/切不开），再说方向问题。
    # 反向但单调的因子不是"没用"，是"要用负权重"—— 结论措辞要区分开，
    # 否则使用者会把一个有效因子当噪音丢掉。
    g1p, g2p, g3p, g4p = (out["g1_pass"], out["g2_pass"],
                          out.get("g3_pass", False), out["g4_pass"])
    out["usable"] = g1p and g2p and g3p and g4p
    if out["g1_collapsed"]:
        out["verdict"] = "塌缩 —— 分数挤在最高档，等于没打分"
    elif not g2p:
        out["verdict"] = "切不开 —— 档位太少或某一档独占"
    elif out.get("g4_reversed"):
        out["verdict"] = "方向反了 —— 信号有效但要反着用（负权重）"
    elif not g4p:
        out["verdict"] = "方向不一致 —— 各档忽高忽低，落在噪音区间"
    elif not g3p:
        out["verdict"] = "无区分度 —— 顶档并不比底档好"
    else:
        out["verdict"] = "可用 —— 四检全过"
    out["note"] = (f"样本 {n} 条 | 顶档占比 {g1*100:.0f}% | "
                   f"有效档 {len(nonempty)}/{bins} | "
                   + (f"lift {out['g3_lift']:.2f}×" if out.get("g3_lift")
                      else f"差值 {out.get('g3_delta', 0):+.2%}")
                   + f" | 秩相关 {rho:+.2f}" if rho is not None else "")
    return out


def report(items: list[dict]) -> dict:
    """批量检验。items = [{name, rows, score_key, ret_key, bins}]。"""
    res = []
    for it in items or []:
        res.append(evaluate(it.get("rows") or [], bins=it.get("bins", 5),
                            name=it.get("name", ""),
                            score_key=it.get("score_key", "score"),
                            ret_key=it.get("ret_key", "ret")))
    bad = [r for r in res if r["n"] >= MIN_TOTAL and not r["usable"]]
    return {"items": res, "n_tested": len(res), "n_unusable": len(bad),
            "unusable": [{"name": r["name"], "verdict": r["verdict"]}
                         for r in bad]}
