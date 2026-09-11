# -*- coding: utf-8 -*-
"""P5 验收 · UTG 双域图谱 / 变更影响 / 覆盖审计 + 合规证据包。

检查项
    A  工具面        22 工具 / 21 已实现，唯一待实现为 xdiff_trace（P2）
    B  UTG 构建      文献域 + 图形域 + 用例域三域共存于同一图谱（A1 公理）
    C  桥接候选       系统代号锚点只产生 CANDIDATE + 低置信（粗粒度锚点不得高置信）
    D  人工登记       manual_links.yml 登记 → CONFIRMED 边出现且署名留痕
    E  影响分析       反向可达遍历给出受影响对象、影响强度分级与复核清单
    F  覆盖审计       未追溯条目 / 无源用例 / 未确认桥接候选，比率自洽
    G  合规证据包     xlsx 产出，条款→证据→定位逐条可查，无证据标"待补"
    H  剧本点亮       PB4 / PB5 由不可用转为可用（自动判定）
    I  端到端 PB4     一句话触发变更影响分析
    J  端到端 PB5     一句话触发覆盖审计 + 证据包

用法：<venv>/python.exe eval/p5_acceptance.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from niva import config as C                                        # noqa: E402
from niva.agent.orchestrator import Orchestrator                    # noqa: E402
from niva.kernel.registry import ErrorCode                          # noqa: E402
from niva.services.graph.service import GraphService                # noqa: E402
from niva.services.graph.utg import UTGBuilder                      # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'PASS' if passed else 'FAIL'}  {name:<26} {detail}")


def check_a(reg) -> None:
    m = reg.manifest()
    pend = m["pending_implementations"]
    rec("A 工具面", m["total"] == 22 and pend == ["xdiff_trace"],
        f"已实现 {m['total']-len(pend)}/22，待实现={pend}")


def check_b_c_build(gs: GraphService) -> None:
    g, st = gs._graph()
    bd = st.by_domain
    ok = all(k in bd for k in ("doc", "diagram", "test")) and st.nodes > 100
    rec("B UTG 三域共存", ok,
        f"节点 {st.nodes} / 边 {st.edges}，按域={json.dumps(bd, ensure_ascii=False)}")
    print(f"        · 来源：{json.dumps(st.sources, ensure_ascii=False)[:160]}")

    cands = [e for e in g.edges.values()
             if e.relation == "implements" and e.status == "CANDIDATE"]
    # 真实语料：需求条目文本不含图纸系统代号 → 自动候选为 0 是**正确**结果
    # （docx §8.7：系统不做自动语义挂接，衔接由人工登记）。
    # 机制正确性另用合成样本验证（见 C2）。
    rec("C 桥接候选（真实语料）", len(cands) == 0 and bool(g.meta.get("bridge_warning")),
        f"自动候选 {len(cands)} 条（本语料需求文本未引用图纸系统代号）；"
        f"已显式告警={bool(g.meta.get('bridge_warning'))}")
    return g


def check_c2_synthetic_bridge():
    """A2b 机制正确性：合成一个含系统代号的文档条目，应产生 1 条低置信候选。"""
    from niva.kernel.model import TraceNode, Locator
    b = UTGBuilder()
    g = b._bridge_system_codes.__globals__["TraceGraph"]()
    # 图纸侧：一个带系统号 CAM 的页节点
    g.nodes["dia:SAMA:p1"] = TraceNode(
        node_id="dia:SAMA:p1", domain="diagram", kind="page", label="CAM101MP",
        attrs={"system": "YCAM"})
    g.nodes["doc:X:req:<X1>"] = TraceNode(
        node_id="doc:X:req:<X1>", domain="doc", kind="requirement_item",
        label="<X1>", payload={"content": "CAM 系统应实现安全壳压力监测"})
    n = b._bridge_system_codes(g)
    e = [e for e in g.edges.values() if e.relation == "implements"]
    ok = n == 1 and len(e) == 1 and e[0].status == "CANDIDATE" and e[0].confidence < 0.45
    rec("C2 桥接机制（合成样本）", ok,
        f"合成候选 {n} 条，status={e[0].status if e else '-'} "
        f"conf={e[0].confidence if e else '-'}（<0.45 → 辅助人审档）")
    return ok


def check_d_manual(g) -> None:
    """登记一条人工确认边 → CONFIRMED 出现（署名留痕）。"""
    ml = C.KNOWLEDGE_ROOT / "bridges" / "manual_links.yml"
    original = ml.read_text(encoding="utf-8")
    # 找一个真实存在的 doc 需求节点与一个 dia 节点
    doc_n = next((n.node_id for n in g.nodes.values()
                  if n.domain == "doc" and n.kind == "requirement_item"), None)
    dia_n = next((n.node_id for n in g.nodes.values()
                  if n.domain == "diagram" and n.kind == "loop"), None)
    if not doc_n or not dia_n:
        rec("D 人工登记桥接", False, "缺少可用的 doc/dia 节点")
        return
    test_yaml = original + f'''
links:
  - src: "{dia_n}"
    dst: "{doc_n}"
    relation: "implements"
    note: "验收用临时登记（P5 acceptance）"
    reviewer: "P5-acceptance"
    confidence: 1.0
'''
    try:
        ml.write_text(test_yaml, encoding="utf-8")
        g2, _ = UTGBuilder().build()
        conf = [e for e in g2.edges.values()
                if e.relation == "implements" and e.status == "CONFIRMED"
                and e.human_confirmed]
        rec("D 人工登记桥接", bool(conf) and all(e.reviewer for e in conf),
            f"CONFIRMED 桥接边 {len(conf)} 条，reviewer={conf[0].reviewer if conf else '-'}")
    finally:
        ml.write_text(original, encoding="utf-8")     # 还原（覆盖写，非删除）


def check_e_impact(gs: GraphService) -> None:
    g, _ = gs._graph()
    best, best_n = None, -1
    for nid in list(g.nodes)[:0] or []:
        pass
    for nid in list(g.nodes.keys()):
        n_aff = len(g.downstream_of(nid, depth=12)) - 1
        if n_aff > best_n:
            best, best_n = nid, n_aff
    if best is None or best_n <= 0:
        rec("E 影响分析", False, "UTG 中无可传播节点")
        return
    r = gs.utg_impact_analysis(best)
    d = r.data or {}
    ok = (r.ok and d.get("found") and d.get("affected_total", 0) > 0
          and "checklist" in d and "by_level" in d)
    rec("E 影响分析", ok,
        f"节点 {best}（{g.nodes[best].domain}）→ 受影响 {d.get('affected_total')}，"
        f"分级={json.dumps(d.get('by_level'), ensure_ascii=False)}")
    print(f"        · 按域：{json.dumps({k: len(v) for k, v in (d.get('by_domain') or {}).items()}, ensure_ascii=False)}")
    print(f"        · 复核清单：{len(d.get('checklist') or [])} 项；需重算用例 {len(d.get('need_recheck_cases') or [])} 条")


def check_f_coverage(gs: GraphService) -> None:
    r = gs.utg_coverage_report(project="P5 验收", include_package=False)
    d = r.data or {}
    tot, un = d.get("requirement_total"), d.get("untraced_count")
    ok = (tot is not None and un is not None
          and abs((d.get("coverage_ratio") or 0) - round((tot - un) / tot, 4)) < 1e-6
          and d.get("unconfirmed_bridge_count", 0) == 0)
    rec("F 覆盖审计", ok,
        f"需求 {tot} / 未追溯 {un} / 覆盖率 {d.get('coverage_ratio')}；"
        f"无源用例 {d.get('orphan_count')}；未确认桥接 {d.get('unconfirmed_bridge_count')}"
        f"（本语料无自动候选，衔接须人工登记）")
    return d


def check_g_package(gs: GraphService) -> None:
    r = gs.utg_coverage_report(project="P5 验收", include_package=True)
    pkg = (r.data or {}).get("evidence_package") or {}
    p = pkg.get("xlsx_path")
    ok = (pkg.get("available") is True and p and Path(p).exists()
          and (pkg.get("clauses") or 0) > 0
          and (pkg.get("satisfied") or 0) + (pkg.get("pending") or 0) == (pkg.get("clauses") or -1))
    rec("G 合规证据包", ok,
        f"{Path(p).name if p else '-'} {pkg.get('bytes')}B，条款 {pkg.get('clauses')}，"
        f"有证据 {pkg.get('satisfied')} / 待补 {pkg.get('pending')}（不虚构合规结论）")


def check_h_pb(orch: Orchestrator) -> None:
    m = orch.manifest()["playbooks"]["playbooks"]
    d = {p["id"]: p for p in m}
    ok = d["PB4"]["available"] and d["PB5"]["available"]
    rec("H 剧本点亮 PB4/PB5", ok,
        f"PB4={d['PB4']['available']} PB5={d['PB5']['available']}"
        f"（PB2 仍门控：{d['PB2']['unavailable_reason'][:30]}…）")


def check_ij_e2e(orch: Orchestrator, node_id: str) -> None:
    r = orch.run("做一次变更影响分析", params={"node_id": node_id})
    ok = (not r["execution"]["halted"]) and r["critique"]["passed"]
    rec("I 端到端 PB4 影响分析", ok,
        f"steps={r['execution']['stats']} affected={r['outputs'].get('impact', {}).get('affected_total')}"
        if r["outputs"].get("impact") else f"steps={r['execution']['stats']}")

    r2 = orch.run("输出覆盖审计报告和合规证据包", params={"project": "P5 端到端"})
    ok2 = (not r2["execution"]["halted"]) and r2["critique"]["passed"]
    apath = (r2["artifacts"] or {}).get("coverage.evidence_package.xlsx_path") \
        or (r2["artifacts"] or {}).get("coverage.evidence_package.path")
    rec("J 端到端 PB5 覆盖审计", ok2,
        f"steps={r2['execution']['stats']} 证据包={Path(apath).name if apath else '无'}")


def main() -> int:
    print("=" * 78)
    print("  NIVA · P5 验收（UTG / 变更影响 / 覆盖审计 + 合规证据包）")
    print("=" * 78)
    reg = None
    from niva.server.tools import build_registry
    reg = build_registry()
    check_a(reg)
    gs = GraphService()
    g = check_b_c_build(gs)
    check_d_manual(g)
    check_c2_synthetic_bridge()
    check_e_impact(gs)
    cov = check_f_coverage(gs)
    check_g_package(gs)
    orch = Orchestrator(registry=reg)
    check_h_pb(orch)
    node = next((n for n in list(g.nodes) if g.downstream_of(n, 12) - {n}), None) \
        if g else None
    check_ij_e2e(orch, node)
    npass = sum(1 for _n, ok, _d in RESULTS if ok)
    print("\n" + "=" * 78)
    print(f"  结果：{npass}/{len(RESULTS)} 项通过")
    if npass == len(RESULTS):
        print("  ✅ UTG 三域共存、跨域桥接诚实分级（候选/人工确认）、")
        print("     变更影响秒级遍历、覆盖审计与合规证据包逐条可定位。")
        print("     唯一未实现工具：xdiff_trace（P2，Gate=完整语料重跑 R1）")
        return 0
    print("  ❌ 未通过项：")
    for n, ok, d in RESULTS:
        if not ok:
            print(f"     - {n}: {d}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
