# -*- coding: utf-8 -*-
"""NIVA — Nuclear I&C Verification Agent.

把 `E:\\Trace_NL`（需求文档追溯验证）与 `E:\\SAMA-V1`（工程图纸解析与追溯）
整合为面向核电仪控 V&V 的智能体。架构见 docs/ 下《架构设计与实施方案》。

分包：
    kernel/    统一内核：TraceGraph / TraceNode / TraceEdge / Evidence / Locator
    adapters/  绞杀者适配器：doc ← Trace_NL, diagram ← SAMA-V1（业务代码不重写）
    services/  跨域能力：bridge（图文锚定）、testcase（用例引擎）、impact、report
    agent/     编排层：planner / router / executor / critic / llm_provider / playbooks
    knowledge/ 声明式知识 SSOT：图符字典 / 位号文法 / IO / 定值 / 部件用例库 / 标准映射
    server/    统一 MCP 与 HTTP 接口
    ui/        Streamlit 追溯配置 + 双栏证据联动工作台
"""
__version__ = "0.1.0-p0"
__codename__ = "NIVA"
