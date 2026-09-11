# -*- coding: utf-8 -*-
"""真值推演引擎（TCG 第③步，本项目的技术壁垒）。

**为什么必须有这一步**：测试用例的"预期结果"不能由大模型编造。
本模块在逻辑图上做确定性前向求值，把"模拟段抽象为谓词、布尔段精确求值"，
产出 激励 → 期望输出 的完整表格，并保留逐节点求值轨迹（``eval_trace``）——
这条轨迹同时就是测试步骤的自然来源。

混合域分层求解（架构方案 §5.2.4）
----------------------------------
    模拟段（MP007 → IM → Δ → H/）  →  抽象为谓词 P = "输入越过定值"
    布尔段（AND / OR / VOTE_KN …）  →  精确枚举求值

这样计算量从"连续域数值仿真"降到"枚举 + 布尔求值"，毫秒级完成；
且判据直接落在"定值 ±ε"上，正是部件测试实际要测的东西。

**与方案的偏差（必须说明）**：原方案 P3 验收写"对 ``synth_gen.truth()`` 地面真值
正确率 ≥95%"。实测发现 ``synth_gen.truth()`` 只返回**结构与不变式**，
并没有逐激励的期望输出表 —— 该指标按原文不可实现。
已改为对**手工编制的基准真值表**校验（见 ``eval/p3_acceptance.py``），
同时合成回归继续沿用 ``roundtrip.py`` 的结构同构闸门。
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ... import config as C
from .unit_extract import (
    LOGIC_CLASSES, STATEFUL_CLASSES, THRESHOLD_CLASSES, Unit,
)

ENUM_MAX_INPUTS = int(C.th("testcase.truth.boolean_full_enum_max_inputs", 6))


@dataclass
class TruthResult:
    unit_id: str
    evaluable: bool
    method: str = ""                       # full_enumeration | boundary_sampling | threshold_only
    variables: list[dict] = field(default_factory=list)
    stimuli: list[dict] = field(default_factory=list)      # [{var_id: 0/1, ...}]
    expected: list[dict] = field(default_factory=list)     # [{out_id: 0/1, ...}]
    traces: list[dict] = field(default_factory=list)       # [{node_id: 0/1, ...}]
    outputs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)        # 人类可读激励步骤


# =====================================================================
# 求值算子（与 knowledge/symbols/*.yml 的 exec 语义一一对应）
# =====================================================================
def _apply(cls: str, vals: list[int], params: dict) -> int:
    if cls == "AND":
        return int(all(vals))
    if cls == "OR":
        return int(any(vals))
    if cls == "NOT":
        return int(not vals[0])
    if cls == "XOR":
        return int(sum(vals) % 2 == 1)
    if cls == "VOTE_KN":
        k = int(params.get("k") or params.get("kn", {}).get("k") or 0)
        if k <= 0:
            raise ValueError("VOTE_KN 缺少参数 k（须由图纸实际取值填充，不得默认）")
        return int(sum(vals) >= k)
    raise ValueError(f"不支持的布尔算子：{cls}")


def _topo(nodes: dict[str, dict], ins: dict[str, list[str]],
          universe: set[str]) -> tuple[list[str], list[list[str]]]:
    """Kahn 拓扑排序（限定在 universe 内）。返回 (顺序, 环)。"""
    indeg = {n: 0 for n in universe}
    for n in universe:
        for src in ins.get(n, []):
            if src in universe:
                indeg[n] += 1
    order, queue = [], [n for n in universe if indeg[n] == 0]
    while queue:
        n = queue.pop(0)
        order.append(n)
        for e in ins.get(n, []):
            pass
        for m in universe:
            if n in ins.get(m, []) and m in indeg:
                indeg[m] -= 1
                if indeg[m] == 0:
                    queue.append(m)
    cyclic = sorted(n for n in universe if indeg[n] > 0)
    return order, cyclic


def _stimulus_set(n_vars: int) -> tuple[str, list[tuple[int, ...]]]:
    """n ≤ 6 全枚举；n > 6 用边界抽样（全0/全1/单1/单0），覆盖等价类与边界。"""
    if n_vars <= ENUM_MAX_INPUTS:
        return "full_enumeration", list(itertools.product((0, 1), repeat=n_vars))
    combos = [(0,) * n_vars, (1,) * n_vars]
    for i in range(n_vars):
        c = [0] * n_vars; c[i] = 1; combos.append(tuple(c))
        c = [1] * n_vars; c[i] = 0; combos.append(tuple(c))
    seen, uniq = set(), []
    for c in combos:
        if c not in seen:
            seen.add(c); uniq.append(c)
    return "boundary_sampling", uniq


# =====================================================================
def evaluate_graph(nodes: dict[str, dict], edges: list[dict],
                   outputs: Iterable[str], unit_id: str = "graph",
                   variables: Optional[list[dict]] = None) -> TruthResult:
    """在给定子图上做真值推演。

    Parameters
    ----------
    nodes : {node_id: {"cls": 语义类, "params": {...}, "label": str}}
    edges : [{"src":..., "dst":...}]
    outputs : 关心的输出节点（若空则取无下游节点）
    variables : 阈值谓词的描述（含定值绑定状态），用于生成人类可读激励
    """
    ins: dict[str, list[str]] = {nid: [] for nid in nodes}
    outs: dict[str, list[str]] = {nid: [] for nid in nodes}
    for e in edges:
        s, d = e.get("src"), e.get("dst")
        if s in nodes and d in nodes:
            ins[d].append(s)
            outs[s].append(d)

    preds = [nid for nid, n in nodes.items() if n.get("cls") in THRESHOLD_CLASSES]
    logic = [nid for nid, n in nodes.items() if n.get("cls") in LOGIC_CLASSES]
    universe = set(preds) | set(logic)
    warnings: list[str] = []

    if not universe:
        return TruthResult(unit_id=unit_id, evaluable=False,
                           warnings=["图中既无阈值块也无逻辑块，无布尔输出可推演"])

    # 拓扑必须先于"找输出"：纯代数环没有汇点，若先找输出会误报
    # "未找到布尔输出节点"，掩盖真正的代数环问题。
    order, cyclic = _topo(nodes, ins, universe)
    if cyclic:
        stateful_in_cycle = [n for n in cyclic
                             if nodes[n].get("cls") in STATEFUL_CLASSES]
        if stateful_in_cycle:
            warnings.append(f"回路含状态元件 {stateful_in_cycle}，已按组合逻辑近似；"
                            "精确验证需时序激励序列")
        else:
            return TruthResult(unit_id=unit_id, evaluable=False,
                               warnings=[f"存在代数环（无状态元件断环）：{cyclic}；"
                                         "不生成用例（宁缺毋滥）"],
                               variables=list(variables or []))

    if not outputs:
        outputs = [nid for nid in universe if not any(
            s in universe for s in _succ(outs, nid))]
    outputs = [o for o in outputs if o in universe]
    if not outputs:
        return TruthResult(unit_id=unit_id, evaluable=False,
                           warnings=["未找到布尔输出节点（阈值块可能未接入逻辑）"],
                           variables=list(variables or []))

    # 谓词变量表
    var_rows = list(variables) if variables else [
        {"var_id": p, "label": nodes[p].get("label", p),
         "cls": nodes[p].get("cls"), "setpoint": None,
         "setpoint_status": "missing"} for p in preds]
    var_ids = [v["var_id"] for v in var_rows] or preds

    method, combos = _stimulus_set(len(var_ids))
    stimuli, expected, traces, steps = [], [], [], []

    missing_sp = [v for v in var_rows if v.get("setpoint_status") in ("missing", "bound_no_value")]
    if missing_sp:
        warnings.append(f"{len(missing_sp)} 个阈值块未绑定具体定值（R14）："
                        "激励以『越过/低于定值』表述，不得给出数值")

    for combo in combos:
        assign = dict(zip(var_ids, combo))
        val: dict[str, int] = {}
        trace: dict[str, int] = {}
        for nid in order:
            cls = nodes[nid].get("cls")
            if cls in THRESHOLD_CLASSES:
                v = assign.get(nid, 0)
            elif cls in LOGIC_CLASSES:
                srcs = [s for s in ins.get(nid, []) if s in universe]
                if not srcs:
                    warnings.append(f"逻辑块 {nid}({cls}) 无布尔输入，跳过")
                    continue
                params = nodes[nid].get("params") or {}
                try:
                    v = _apply(cls, [val.get(s, 0) for s in srcs], params)
                except ValueError as exc:
                    warnings.append(f"{nid}: {exc}")
                    return TruthResult(unit_id=unit_id, evaluable=False,
                                       warnings=warnings,
                                       variables=var_rows)
            else:
                continue
            val[nid] = v
            trace[nid] = v
        stimuli.append(dict(assign))
        expected.append({o: val.get(o, 0) for o in outputs})
        traces.append(trace)
        steps.append({
            "stimulus": {var_rows[i].get("label", var_ids[i]): int(combo[i])
                         for i in range(len(var_ids))},
            "expected": {o: int(val.get(o, 0)) for o in outputs},
        })

    return TruthResult(
        unit_id=unit_id, evaluable=True, method=method,
        variables=var_rows, stimuli=stimuli, expected=expected, traces=traces,
        outputs=list(outputs), warnings=warnings,
        coverage={"method": method, "combos": len(combos),
                  "n_variables": len(var_ids),
                  "full_coverage": method == "full_enumeration"},
        steps=steps,
    )


def _succ(outs: dict[str, list[str]], nid: str) -> list[str]:
    return outs.get(nid, [])


def evaluate_unit(unit: Unit) -> TruthResult:
    """对抽取出的可测单元做真值推演。"""
    nodes = {}
    for nid, n in unit.nodes.items():
        s = unit.sem.get(nid) or {}
        if not s:
            continue
        nodes[nid] = {"cls": s["cls"], "params": s.get("params") or {},
                      "label": (n.get("text") or n.get("label") or "")}
    edges = [{"src": e["src"], "dst": e["dst"]} for e in unit.in_edges]
    res = evaluate_graph(nodes, edges, unit.outputs, unit_id=unit.unit_id,
                         variables=unit.variables)
    res.warnings = list(dict.fromkeys(list(unit.warnings) + list(res.warnings)))
    return res
