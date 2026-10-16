#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""题材标签解析的**真实数据**回归：近 30 个交易日全部涨停原因。

为什么单独开一个文件（而不是并进 test_sowing.py）：
  test_sowing.py 用的是构造出来的 reason 字符串，只能证明"我写的正则对
  我编的例子成立"。这个 bug 的可怕之处恰恰在于**例子编不出来** —— 我是拿
  真实归档扫出 138/1725 条（8.0%）脏标签才发现的。所以这里直接吃
  data/pool_history/ 的真实数据，把"脏标签率必须为 0"钉死。

判据（脏标签）：
  · 标签里还留着竖线（说明没切）
  · 标签里出现公司简介/公告的特征词（公司主要从事 / 生产线 / 万吨 / 上市公司…）
这些字符串出现在"题材标签"位置上一定是错的 —— 用户看到的是「福建+建材|
公司主要从事水泥及熟料的生产及销售，拥有7条新型干法熟料水泥生产线，」
这种把正文截断 40 字后冒充题材的东西。

跑法：python tests/test_sowing_labels_real.py
"""
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import datasource as ds   # noqa: E402
from core import sowing            # noqa: E402

ok_n = 0
bad = []


def ok(cond, msg):
    global ok_n
    if cond:
        ok_n += 1
        print(f"  OK  {msg}")
    else:
        bad.append(msg)
        print(f"  XX  {msg}")


# 公司简介 / 公告正文的特征词 —— 出现在题材标签位置上一定是解析漏切。
# ★ 必须写得**准**：第一版把「股权」「参股」也算进来，结果把
#   「证券+股权转让」「参股券商+电阻电容」这些**合法题材标签**判成脏
#   （40 条误报）。题材名里本来就有「股权转让」「参股券商」「并购重组」，
#   所以只能靠"完整的简介句式"来判，不能靠单个词。
_PROSE = re.compile(
    r"公司(主要)?(从事|是|已|拟|将|位于|依托|拥有)"   # 「公司主要从事…」式简介
    r"|是一家|主营业务|生产线|万吨|上市公司股东|"
    r"公告称|全资子公司|预计\d{4}年|归属于上市公司"
)
_SEP = re.compile(r"[；;|｜]")


def window_days(n=30):
    d = ds.pool_history_dates()
    if not d:
        return []
    return d[-n:]


def main():
    print("===== 题材标签解析：真实归档全量回归 =====\n")
    dates = window_days(30)
    print(f"[1] 归档窗口：{len(dates)} 个交易日 "
          f"({dates[0] if dates else '-'} → {dates[-1] if dates else '-'})")
    ok(len(dates) >= 5, f"归档里有足够交易日（{len(dates)}）")
    if len(dates) < 5:
        print("\n归档不足，无法做真实数据回归（冷启动环境属正常）")
        print("===== 跳过：环境无归档 =====")
        return 0

    total = 0
    bad_rows = []          # 脏标签
    blank = 0              # 解析不出标签的
    raw_bad = 0            # 原文里就带竖线（说明该切的都该走竖线分支）

    for d in dates:
        for r in ds.load_pool_history(d):
            raw = str(r.get("reason") or "").strip()
            if not raw:
                continue
            total += 1
            if _SEP.search(_SEP.split(raw)[0] or "") or "|" in _SEP.split(raw)[0]:
                pass
            head_raw = _SEP.split(raw)[0]
            if "|" in head_raw or "｜" in head_raw:
                raw_bad += 1
            lab = sowing.label_of(raw)
            if not lab:
                blank += 1
                continue
            if "|" in lab or "｜" in lab:
                bad_rows.append((d, r.get("code"), r.get("name"), lab, "残留竖线"))
            elif _PROSE.search(lab):
                bad_rows.append((d, r.get("code"), r.get("name"), lab, "混入简介/公告"))

    print(f"    涨停原因总数 {total}；原文首段含竖线 {raw_bad} 条"
          f"（{raw_bad / max(1, total) * 100:.1f}%）")
    print(f"    解析后空标签 {blank} 条")
    print()

    print("[2] 脏标签必须为 0（v1.41.6 修复前是 8.0% / 138 条）")
    if bad_rows:
        for b in bad_rows[:10]:
            print(f"      {b[0]} {b[1]} {b[2]} → {b[3]!r} [{b[4]}]")
    ok(len(bad_rows) == 0,
       f"近 30 个交易日全部涨停原因，脏标签 0 条（实测 {len(bad_rows)} 条）")
    print()

    print("[3] 长度与可读性：标签不得超长（>16 字的'标签'基本是把正文切进来了）")
    longed = []
    for d in dates:
        for r in ds.load_pool_history(d):
            raw = str(r.get("reason") or "").strip()
            if not raw:
                continue
            lab = sowing.label_of(raw)
            if lab and len(lab) > 16:
                longed.append((d, r.get("name"), lab))
    if longed:
        for x in longed[:8]:
            print(f"      {x[0]} {x[1]} → {x[2]!r}")
    # 不做硬断言（真有 16 字以上的复合题材标签），只报数
    print(f"    超 16 字的标签 {len(longed)} 条 "
          f"（占 {len(longed) / max(1, total) * 100:.1f}%）")
    ok(len(longed) / max(1, total) < 0.02,
       "超长标签占比 <2%（说明没有系统性把正文切进来）")
    print()

    print("[4] 修复前后的直接对照：同一批原文，旧口径 vs 新口径")
    # 旧口径 = 只切分号（复刻 bug 现场），新口径 = 切分号+竖线
    old_bad = 0
    for d in dates:
        for r in ds.load_pool_history(d):
            raw = str(r.get("reason") or "").strip()
            if not raw:
                continue
            old = re.split(r"[；;]", raw)[0].strip()[:40]     # ← 修复前逻辑
            if "|" in old or _PROSE.search(old):
                old_bad += 1
    new_bad = len(bad_rows)
    print(f"    旧口径脏标签 {old_bad} 条 → 新口径 {new_bad} 条")
    ok(old_bad > 0, f"旧口径确实会产出脏标签（{old_bad} 条，证明 bug 真实存在过）")
    ok(new_bad == 0, "新口径脏标签归零")
    ok(old_bad > new_bad, "修复确实起了作用（脏标签数下降）")
    print()

    print("[5] 真实样本抽查：标签必须是纯题材词")
    samples = []
    for d in dates[-3:]:
        for r in ds.load_pool_history(d):
            raw = str(r.get("reason") or "").strip()
            if raw:
                samples.append((d, r.get("name"), raw, sowing.label_of(raw)))
            if len(samples) >= 12:
                break
        if len(samples) >= 12:
            break
    for d, nm, raw, lab in samples[:10]:
        print(f"      {d} {nm}: 原文 {raw[:34]!r} → 标签 {lab!r}")
    # 有分隔符的必须解析出非空标签；没分隔符的允许诚实返回空串
    # （那种整段就是公告正文，宁可没标签也不能拿正文冒充题材）。
    with_sep = [(raw, lab) for _, _, raw, lab in samples if _SEP.search(raw)]
    ok(len(with_sep) > 0, f"抽查里有带分隔符的样本（{len(with_sep)} 条）")
    ok(all(lab for _, lab in with_sep),
       f"带分隔符的样本全部解析出标签（{len(with_sep)} 条）")
    no_sep = [(raw, lab) for _, _, raw, lab in samples if not _SEP.search(raw)]
    if no_sep:
        print(f"      无分隔符样本 {len(no_sep)} 条，标签为 "
              f"{[lab for _, lab in no_sep]!r}（空串＝诚实放弃，非 bug）")
    print()

    print(f"===== {ok_n} 通过 / {len(bad)} 失败 =====")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
