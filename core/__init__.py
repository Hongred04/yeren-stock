# -*- coding: utf-8 -*-
"""野人体系选股引擎 —— 核心包。

显式声明为常规包（而不是靠 PEP 420 隐式命名空间包）：
Android 侧的 Chaquopy 只会把带 __init__.py 的目录识别成包，
少了这个文件，打包进 APK 后会 import 失败。
"""
