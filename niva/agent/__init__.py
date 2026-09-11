# -*- coding: utf-8 -*-
"""编排层：单智能体 + 工具集。

Planner → Router → Executor（↕ Critic），标准场景由 playbooks/ 查表执行、
命中即跳过模型规划（"规则优先、LLM 兜底"在编排层的体现）。

LLM 的合法点位只有三个：歧义裁决、语义成文、任务分流。
"""
