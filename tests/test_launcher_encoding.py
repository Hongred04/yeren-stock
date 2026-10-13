# -*- coding: utf-8 -*-
"""启动器（.bat）编码回归 —— 「启动器怎么有火星文了」（用户 10-13）。

**根因**：`start.bat` 里有 `chcp 65001`，而我在它**之后**加了 UTF-8 中文注释。
cmd.exe 解析批处理时按**当时代码页的字符偏移**去定位后续字节：`chcp` 把代码页
从 936 切到 65001 后，偏移量按旧代码页算，于是切进 UTF-8 多字节序列的中间，
解出乱码 —— 再把它当成命令执行，就报
「'...' 不是内部或外部命令，也不是可运行的程序或批处理文件」。

**原始 `start.bat` 是纯 ASCII（0 个非 ASCII 字节）** —— 这不是巧合，是刻意为之：
中文输出全部交给 `launcher.py`（它在 `chcp 65001` 之后由 Python 打印，走的是
控制台 API，不受批处理解析器影响）。

本测试钉死这条不变量，并真实跑一遍 cmd 验证不再出现乱码。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

okc = badc = 0


def ok(cond, msg):
    global okc, badc
    if cond:
        okc += 1
        print("  OK  " + msg)
    else:
        badc += 1
        print("  XX  " + msg)


def raw(name: str) -> bytes:
    with open(os.path.join(_ROOT, name), "rb") as f:
        return f.read()


def bat_files() -> list[str]:
    return sorted(f for f in os.listdir(_ROOT) if f.lower().endswith(".bat"))


def main():
    print("[1] 跑过 chcp 的批处理必须是纯 ASCII（乱码的根因）")
    bats = bat_files()
    ok("start.bat" in bats, f"找到 start.bat（{bats}）")
    for name in bats:
        b = raw(name)
        text = b.decode("utf-8", errors="ignore")
        if "chcp" not in text.lower():
            ok(True, f"{name} 不含 chcp —— 无解析器切页风险")
            continue
        bad = sum(1 for x in b if x > 127)
        ok(bad == 0, f"{name} 含 chcp → 非 ASCII 字节必须为 0（实际 {bad}）")
        # 更精确：chcp 之后的字节也要全是 ASCII
        idx = text.lower().find("chcp")
        tail = text[idx:].encode("utf-8", errors="ignore")
        ok(all(x <= 127 for x in tail), f"{name}：chcp 之后没有非 ASCII 字节")

    print("\n[2] start.bat 的形式约定")
    sb = raw("start.bat")
    ok(sum(1 for x in sb if x > 127) == 0, "start.bat 是纯 ASCII")
    ok(b"\r\n" in sb, "用 CRLF 换行（Windows 批处理惯例）")
    ok(b"\r\n" in sb and (sb.count(b"\n") - sb.count(b"\r\n")) == 0,
       "没有裸 LF 混用")
    ok(sb[:3] != b"\xef\xbb\xbf", "不带 UTF-8 BOM（BOM 会让 cmd 把首行读成乱码）")

    print("\n[3] 功能没被改回去：解释器优先级 + 通达信可用性提示")
    t = sb.decode("ascii")
    ok(".venv\\Scripts\\python.exe" in t, "优先用项目内 .venv")
    ok("envs\\default\\Scripts\\python.exe" in t, "其次用装了依赖的隔离 venv")
    ok("import mootdx" in t, "会探测 mootdx 是否可用")
    ok("TDX source:" in t, "启动时打印 TDX 源可用性")
    ok("launcher.py" in t, "仍然调用 launcher.py")
    # 顺序：venv 必须排在托管基础解释器之前
    ok(t.index(".venv") < t.index("import mootdx"), "选解释器在探测之前")
    # Windows 路径必须用反斜杠：正斜杠在 cmd 的 if exist / 引号调用里行为不一致
    ok("C:/Users" not in t, "绝对路径没有写成正斜杠")
    ok("C:\\Users" in t, "绝对路径是反斜杠形式")

    print("\n[4] 其它 .bat：被 echo 的内容不能是中文（控制台是 GBK，会花屏）")
    for name in bats:
        if name == "start.bat":
            continue
        text = raw(name).decode("utf-8", errors="ignore")
        echoed = [ln for ln in text.splitlines()
                  if ln.strip().lower().startswith("echo")]
        bad = [ln for ln in echoed if any(ord(ch) > 127 for ch in ln)]
        ok(not bad, f"{name}：echo 行全 ASCII（中文行：{bad or '无'}）")

    print("\n[5] 中文输出交给 Python（chcp 之后由 Python 打印）")
    # 中文横幅在 server.py（"智能股票监控系统已启动"），launcher.py 自身是英文 ——
    # 关键是它们都在 chcp 65001 之后由 Python 输出，走控制台 API，不受批处理解析器影响。
    with open(os.path.join(_ROOT, "server.py"), encoding="utf-8") as f:
        srv = f.read()
    ok(any(ord(c) > 127 for c in srv), "server.py 里有中文横幅（由它打印）")
    with open(os.path.join(_ROOT, "launcher.py"), encoding="utf-8") as f:
        lp = f.read()
    ok("chcp" not in lp.lower(), "launcher.py 不需要自己切代码页（.bat 已切）")

    print("\n[6] 真跑一遍 cmd：不再出现「不是内部或外部命令」")
    # 把 start.bat 截到 launcher 之前（不真的起服务），用 cmd 执行，看输出。
    lines = raw("start.bat").decode("ascii").split("\r\n")
    # 截在**真正调用** launcher.py 那一行（注释里也出现过 "launcher.py"）
    cut = next((i for i, ln in enumerate(lines)
                if ln.strip().lower().startswith('"%py%" launcher.py')), len(lines))
    ok(cut < len(lines), "找到 launcher.py 调用行（截断点有效）")
    probe = "\r\n".join(lines[:cut] + ["echo __DONE__", "exit /b 0"]) + "\r\n"
    tmp = os.path.join(tempfile.mkdtemp(prefix="batenc_"), "probe.bat")
    with open(tmp, "wb") as f:
        f.write(probe.encode("ascii"))
    try:
        r = subprocess.run(["cmd", "/c", tmp], capture_output=True, timeout=90)
        out = (r.stdout + r.stderr).decode("gbk", errors="replace")
    except (OSError, subprocess.SubprocessError) as e:      # 非 Windows 上跳过
        ok(True, f"（无法调用 cmd：{type(e).__name__}，跳过实跑）")
        out = ""
    if out:
        ok("__DONE__" in out, "脚本执行到了末尾（没有中途报错退出）")
        for bad_marker in ("不是内部或外部命令", "not recognized as an internal"):
            ok(bad_marker not in out, f"输出里没有「{bad_marker}」")
        ok("Stock Monitor - Intelligent A-Share Trading System" in out,
           "横幅正常打印")
        ok("TDX source:" in out, "TDX 可用性提示正常打印")
        # 乱码检测：出现大量替换字符或罕见方块说明编码错位
        ok(out.count("\ufffd") == 0, "输出里没有替换字符（编码错位的信号）")
    shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)

    print("\n[7] 反向对照：证明上面那条不变量不是摆设")
    # 实测结论（本机 cmd.exe）：**只要 chcp 65001 之后出现非 ASCII 字节就必炸**，
    # 与换行符无关（LF / CRLF 都炸）；纯 ASCII 则完全正常。
    # 这里现场跑一遍"坏样本"，确认它真的会报乱码 —— 否则 [1] 的断言可能是空的。
    d = tempfile.mkdtemp(prefix="batneg_")
    head = "@echo off\nsetlocal\nchcp 65001 >nul 2>&1\n"
    tail = "echo   BANNER-OK\necho __DONE__\nexit /b 0\n"
    cn = "REM   没有它也能跑（自动退回腾讯/新浪），只是少一层首选源。\n"

    def run_bat(name, text, enc):
        p = os.path.join(d, name + ".bat")
        with open(p, "wb") as f:
            f.write(text.encode(enc))
        try:
            r = subprocess.run(["cmd", "/c", p], capture_output=True, timeout=60)
            return (r.stdout + r.stderr).decode("gbk", errors="replace")
        except (OSError, subprocess.SubprocessError):
            return None

    bad_out = run_bat("bad", head + cn + "REM  mootdx 是**软依赖**：有它，日K线/指数日K"
                             "走通达信（TCP，不被 IP 封）；\n" + tail, "utf-8")
    if bad_out is None:
        ok(True, "（无法调用 cmd，跳过反向对照）")
    else:
        ok("不是内部或外部命令" in bad_out or "not recognized" in bad_out,
           "坏样本（chcp 后跟 UTF-8 中文）确实报乱码 —— 护栏有意义")
        good_out = run_bat("good", head + "REM ascii only\n" + tail, "ascii")
        ok("不是内部或外部命令" not in (good_out or "")
           and "BANNER-OK" in (good_out or ""),
           "好样本（纯 ASCII）一切正常 —— 修法有效")
    shutil.rmtree(d, ignore_errors=True)

    print(f"\n===== {okc} 通过 / {badc} 失败 =====")
    return 1 if badc else 0


if __name__ == "__main__":
    raise SystemExit(main())
