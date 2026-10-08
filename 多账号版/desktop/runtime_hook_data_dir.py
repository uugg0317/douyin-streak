# -*- coding: utf-8 -*-
"""PyInstaller 运行时钩子：修正打包后的路径与环境变量。

为什么需要它：
- onedir 打包后，core/config.py 的 BASE_DIR 解析到 _internal/，于是
  .env 和 static/ 都在 _internal/ 下（构建时用 --add-data 放对了位置）；
- 但用户数据（state.json / ledger.json / config.json / logs）不应该埋在
  _internal 里，必须留在 exe 旁边，否则用户根本找不到自己的数据。

做法：只设置 DATA_DIR 环境变量。core/config.py 已支持该变量
（原本用于多账号隔离：DATA_DIR=accounts/账号1），这里复用它。

本文件在应用代码执行前被 PyInstaller 导入；它不能 import 项目内的任何模块。
"""

import os
import sys

try:
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        os.environ.setdefault("DATA_DIR", os.path.join(exe_dir, "data"))
except Exception:
    # 钩子失败不能阻断启动；config.py 会退回默认的 BASE_DIR/data
    pass
