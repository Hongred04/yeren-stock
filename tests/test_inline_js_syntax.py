# -*- coding: utf-8 -*-
"""抽出 web/*.html 里的所有内联 <script>，逐个用 node --check 验语法。

为什么需要：我改了 index.html / mobile.html 的模板字符串，模板里嵌套
反引号和 ${} —— 少一个反引号就是整页白屏，而这类错误 grep 看不出来。
node --check 是最快的兜底。
"""
import re
import subprocess
import sys
import tempfile
import os

NODE = r"C:\Users\macro\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = 0

for name in ("web/index.html", "web/mobile.html"):
    path = os.path.join(root, name)
    src = open(path, encoding="utf-8", newline="").read().replace("\r\n", "\n")
    # 只取不带 src= 的内联脚本
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", src, re.S | re.I)
    print(f"\n{name}: 找到 {len(blocks)} 个内联 script")
    for i, b in enumerate(blocks):
        if not b.strip():
            continue
        # 用 .mjs 让 node 按模块解析（源码里可能有 import/export 语义的代码）
        fd, tmp = tempfile.mkstemp(suffix=".mjs")
        os.close(fd)
        # 顶层 await / import 的代码用 .mjs；普通脚本也能被 .mjs 解析
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(b)
        r = subprocess.run([NODE, "--check", tmp],
                           capture_output=True, encoding="utf-8", timeout=60)
        ok = (r.returncode == 0)
        first_line = [l for l in (b.split("\n") or [""]) if l.strip()][:1]
        tag = f"#{i} ({len(b)} 字符)"
        if ok:
            print(f"  [OK]   {tag}")
        else:
            fails += 1
            print(f"  [FAIL] {tag}  {first_line}")
            err = (r.stderr or "").strip().split("\n")
            for l in err[:6]:
                print("         ", l)
        os.unlink(tmp)

print(f"\n===== 语法检查：{fails} 个失败 =====")
sys.exit(1 if fails else 0)
