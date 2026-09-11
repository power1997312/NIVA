# -*- coding: utf-8 -*-
"""P3 验收 · 真值推演引擎正确率（对**手工编制基准真值表**）。

⚠ 与方案的偏差（必须显式说明）
--------------------------------
原方案 P3 验收标准写「真值推演在合成图上的正确率 ≥95%（对 ``synth_gen.truth()``
地面真值）」。实测发现 ``synth_gen.truth()`` 只返回**结构与不变式**
（``{nodes, wires, invariants}``），**没有逐激励的期望输出表** —— 该指标按原文不可实现。

因此本验收改用**独立编制的基准真值表**作为地面真值：
    - 单块级：AND / OR / NOT / XOR / VOTE_KN(2oo3)，真值表由人工按
      ``knowledge/symbols/boolean.yml`` 的 ``exec`` 语义逐格写出；
    - 组合级：阈值谓词 → AND → NOT 级联，人工推演；
    - 判别级：XOR 的 (1,1)→0 用例（可区分 XOR 与 OR —— 图面上 "=1" 与 "≥1" 形近，
      这是解析易错点，此用例正是判别器）。

基准表与被测代码**相互独立**：本文件不 import 引擎的求值函数，只调用其接口。

检查项
    A  单块真值表    5 类布尔块的 2^n 全枚举与人工基准逐格一致
    B  组合级联      谓词→AND→NOT 的期望输出与人工推演一致
    C  判别用例       XOR(1,1)=0（若引擎把 XOR 当 OR 会得 1，立即暴露）
    D  覆盖方法       n≤6 全枚举、n>6 边界抽样，口径正确
    E  真实图纸单元    在真实 SAMA IR 上抽取单元并推演（至少产出可评估单元与警告）
    F  宁缺毋滥       代数环 / 无谓词无逻辑的单元必须拒评并给出原因
    G  部件库奇偶     ClsResolver 与 legacy SymbolDict 分类一致

用法：<venv>/python.exe eval/p3_acceptance.py
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from niva import config as C                                       # noqa: E402
from niva.services.testcase.truth_propagate import evaluate_graph  # noqa: E402
from niva.services.testcase.unit_extract import (                  # noqa: E402
    ClsResolver, UnitExtractor,
)

RESULTS: list[tuple[str, bool, str]] = []
PRED = "HI_MONITOR"          # 谓词变量用阈值块充当


def rec(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'PASS' if passed else 'FAIL'}  {name:<24} {detail}")


# ---------------------------------------------------------------------
def _graph(edges: list[tuple[str, str]], **node_specs) -> tuple[dict, list, list[str]]:
    nodes = {}
    for nid, spec in node_specs.items():
        nodes[nid] = {"cls": spec[0], "params": spec[1] if len(spec) > 1 else {},
                      "label": spec[2] if len(spec) > 2 else nid}
    e = [{"src": s, "dst": d} for s, d in edges]
    # 输出 = 汇点（无出边），不是源点。此前写反导致把谓词变量当成输出，
    # 真值表整表错位 —— 这正是"验收要看分项"才能抓出的那类错误。
    outs = [n for n in nodes if not any(s == n for s, d in edges)]
    return nodes, e, outs


def _run(edges, **specs):
    nodes, e, outs = _graph(edges, **specs)
    return evaluate_graph(nodes, e, outs)


def _table(res) -> dict[tuple, dict]:
    """把引擎输出整理成 {谓词取值组合: 输出字典}。"""
    var_ids = [v["var_id"] for v in res.variables]
    out = {}
    for st, ex in zip(res.stimuli, res.expected):
        key = tuple(st[v] for v in var_ids)
        out[key] = ex
    return out


def _fmt(d: dict) -> str:
    return "; ".join(f"{k}→{v}" for k, v in sorted(d.items()))


def check_single_block_tables() -> None:
    """单块基准真值表（人工按 exec 语义逐格写出）。逻辑节点统一命名 O。"""
    cases = [
        ("AND", {}, [("P1",), ("P2",)],
         {(0, 0): 0, (0, 1): 0, (1, 0): 0, (1, 1): 1}),
        ("OR", {}, [("P1",), ("P2",)],
         {(0, 0): 0, (0, 1): 1, (1, 0): 1, (1, 1): 1}),
        ("XOR", {}, [("P1",), ("P2",)],
         {(0, 0): 0, (0, 1): 1, (1, 0): 1, (1, 1): 0}),
        ("VOTE_KN", {"k": 2, "n": 3}, [("P1",), ("P2",), ("P3",)],
         {c: int(sum(c) >= 2) for c in itertools.product((0, 1), repeat=3)}),
    ]
    all_ok = True
    detail = []
    for cls, params, ins, truth in cases:
        edges = [(p, "O") for (p,) in ins]
        specs = {p: (PRED, {}, p) for (p,) in ins}
        specs["O"] = (cls, params, cls)
        res = _run(edges, **specs)
        if not res.evaluable:
            all_ok = False
            detail.append(f"{cls}: 不可评估 {res.warnings}")
            continue
        got = _table(res)
        want = {k: {"O": v} for k, v in truth.items()}
        ok = (got == want) and res.outputs == ["O"]
        all_ok &= ok
        detail.append(f"{cls} x{len(ins)} {'✓' if ok else '✗ ' + _fmt(got)}")
    rec("A 单块真值表（AND/OR/XOR/VOTE）", all_ok, "  ".join(detail))

    res = _run([("P1", "O")], P1=(PRED, {}, "P1"), O=("NOT", {}, "NOT"))
    got = _table(res)
    ok = res.evaluable and got == {(0,): {"O": 1}, (1,): {"O": 0}}
    rec("A 单块真值表（NOT）", ok, f"{_fmt(got)} method={res.method}")


def check_composed_chain() -> None:
    """组合级联：P1,P2 → AND → O1；O1 → NOT → O2。人工推演期望。"""
    nodes = {"P1": {"cls": PRED, "params": {}, "label": "P1"},
             "P2": {"cls": PRED, "params": {}, "label": "P2"},
             "G": {"cls": "AND", "params": {}, "label": "AND"},
             "N": {"cls": "NOT", "params": {}, "label": "NOT"}}
    edges = [{"src": "P1", "dst": "G"}, {"src": "P2", "dst": "G"},
             {"src": "G", "dst": "N"}]
    res = evaluate_graph(nodes, edges, ["G", "N"])
    got = _table(res)
    want = {c: {"G": int(c[0] and c[1]), "N": int(not (c[0] and c[1]))}
            for c in itertools.product((0, 1), repeat=2)}
    ok = res.evaluable and got == want
    rec("B 组合级联（AND→NOT）", ok,
        f"输出={res.outputs} 一致={'是' if ok else _fmt(got)}")


def check_discriminator() -> None:
    """判别用例：XOR(1,1)=0。若引擎把 XOR 误当 OR，这里得 1，立即暴露。"""
    res = _run([("P1", "O"), ("P2", "O")],
               P1=(PRED, {}, "P1"), P2=(PRED, {}, "P2"), O=("XOR", {}, "=1"))
    got = _table(res)
    rec("C 判别用例 XOR(1,1)=0", got.get((1, 1), {}).get("O") == 0,
        f"XOR(1,1)={got.get((1, 1), {}).get('O')}（OR 会得 1）")


def check_coverage_method() -> None:
    """覆盖口径：n≤6 全枚举；n>6 边界抽样。"""
    specs = {f"P{i}": (PRED, {}, f"P{i}") for i in range(3)}
    r1 = _run([(f"P{i}", "G") for i in range(3)], G=("OR", {}, "OR"), **specs)
    ok1 = r1.method == "full_enumeration" and r1.coverage["combos"] == 8
    specs8 = {f"P{i}": (PRED, {}, f"P{i}") for i in range(8)}
    r2 = _run([(f"P{i}", "G") for i in range(8)], G=("OR", {}, "OR"), **specs8)
    ok2 = r2.method == "boundary_sampling" and r2.coverage["combos"] == 18
    rec("D 覆盖口径", ok1 and ok2,
        f"n=3→{r1.method}/{r1.coverage['combos']}；n=8→{r2.method}/{r2.coverage['combos']}（全0全1+单1单0）")


def check_real_ir() -> None:
    """真实 SAMA IR：抽取单元并推演。"""
    ir_path = C.DIAGRAM_LEGACY / "out" / "L3" / "ir_all.json"
    if not ir_path.exists():
        rec("E 真实图纸单元", False, "缺少 ir_all.json")
        return
    ir_all = json.loads(ir_path.read_text(encoding="utf-8"))
    total_units = evaluable = 0
    samples = []
    warn_kinds: dict[str, int] = {}
    for page in ir_all.get("pages", []):
        pno = int((page.get("meta") or {}).get("page") or 0)
        try:
            units = UnitExtractor(page, page=pno).extract()
        except Exception as exc:
            rec("E 真实图纸单元", False, f"第 {pno} 页抽取失败：{exc}")
            return
        for u in units:
            total_units += 1
            from niva.services.testcase.truth_propagate import evaluate_unit
            r = evaluate_unit(u)
            if r.evaluable:
                evaluable += 1
            for w in r.warnings:
                k = w.split("：")[0][:24]
                warn_kinds[k] = warn_kinds.get(k, 0) + 1
            if r.evaluable and len(samples) < 3:
                samples.append({"unit": u.unit_id, "kind": u.kind,
                                "label": u.label, "method": r.method,
                                "vars": len(r.variables),
                                "outputs": len(r.outputs),
                                "combos": r.coverage.get("combos")})
    rec("E 真实图纸单元", total_units > 0 and evaluable > 0,
        f"抽取 {total_units} 个单元，可推演 {evaluable} 个；样例={json.dumps(samples, ensure_ascii=False)}")
    print(f"        · 警告分布：{json.dumps(warn_kinds, ensure_ascii=False)}")


def check_refuse_to_evaluate() -> None:
    """宁缺毋滥：代数环必须拒评。"""
    res = _run([("A", "B"), ("B", "A")],
               A=("AND", {}, "A"), B=("OR", {}, "B"))
    ok = (not res.evaluable) and any("代数环" in w for w in res.warnings)
    rec("F 代数环拒评（宁缺毋滥）", ok, f"warnings={res.warnings[:1]}")

    # 无谓词无逻辑
    res2 = _run([("X", "Y")], X=("GAIN", {}, "GAIN"), Y=("IM", {}, "IM"))
    ok2 = not res2.evaluable
    rec("F 纯模拟单元拒评", ok2, f"warnings={res2.warnings[:1]}")


def check_parity() -> None:
    """符号分类奇偶：ClsResolver vs legacy SymbolDict。"""
    r = ClsResolver()
    samples = ["&", "≥1", "=1", "NOT", "2oo3", "2/4", "H/", "/L", "Δ", "F(x)",
               "IM", "A/D", "TR", "MP007", "密级：非密", "蒸汽管道隔离", ""]
    diffs = r.parity_with_legacy(samples)
    rec("G 符号分类奇偶校验", not diffs,
        f"{len(samples)} 个样本，不一致={diffs or '无'}")


def main() -> int:
    print("=" * 78)
    print("  NIVA · P3 验收（真值推演引擎 · 对手工基准真值表）")
    print("=" * 78)
    check_single_block_tables()
    check_composed_chain()
    check_discriminator()
    check_coverage_method()
    check_real_ir()
    check_refuse_to_evaluate()
    check_parity()

    npass = sum(1 for _n, ok, _d in RESULTS if ok)
    print("\n" + "=" * 78)
    print(f"  结果：{npass}/{len(RESULTS)} 项通过")
    if npass == len(RESULTS):
        print("  ✅ 真值推演引擎与人工基准真值表完全一致；")
        print("     期望值由确定性求值产生，不依赖大模型（机制性杜绝幻觉污染判据）。")
        print("     下一阶段：模板检索 + LLM 成文（含三层校验）→ 用例渲染")
        return 0
    print("  ❌ 未通过项：")
    for n, ok, d in RESULTS:
        if not ok:
            print(f"     - {n}: {d}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
