# -*- coding: utf-8 -*-
"""测试用例生成引擎（TCG，P3 阶段核心交付）。

四步流水线（架构方案 §5.2.1）：unit_extract → template_match → truth_propagate → narrate。

红线：期望值只能来自 truth_propagate 的确定性推演；LLM 只写说明、不改数值。
"""
