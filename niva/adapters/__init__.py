# -*- coding: utf-8 -*-
"""适配器层：把两个既有工程降格为两个域适配器（绞杀者模式）。

纪律：`legacy/` 下的业务代码**不重写**；只在本层 `service.py` 加包壳，
把 legacy 的输出转成统一内核的 TraceNode / TraceEdge。

注意装载方式：两个 legacy 都存在 `config.py`、`tests/` 等同名顶层模块，
必须用 `niva.config.legacy_import_path()` 上下文管理器临时挂载 sys.path，
不可永久挂载。
"""
