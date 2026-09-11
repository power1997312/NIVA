# -*- coding: utf-8 -*-
"""P0 内核契约测试。

验收目标（架构方案 §9 P0 验收标准）：
    1. 统一内核能同时装下文献域与图形域的节点，且两侧 payload 原样保留。
    2. A2 公理可执行：无证据的追溯边被图谱守门硬拒绝。
    3. 统一 ID 规范与两个原工程的既有归一化行为**逐字一致**（不得引入新行为）。
    4. 阈值外置可用，且原环境变量覆盖能力未丢失。
    5. 工具注册表的只读审计与契约校验可工作。

运行：
    <venv>/python.exe -m pytest tests/test_kernel_contract.py -q
    或直接 <venv>/python.exe tests/test_kernel_contract.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from niva import config as C                                    # noqa: E402
from niva.kernel import ids                                     # noqa: E402
from niva.kernel.model import (                                 # noqa: E402
    Evidence, Locator, TraceEdge, TraceGraph, TraceNode,
)
from niva.kernel.registry import (                              # noqa: E402
    REGISTRY, ErrorCode, ToolResult, ToolSpec, fail, ok,
)


# ---------------------------------------------------------------------
def test_ids_match_legacy_behaviour():
    """ID 归一化必须与 Trace_NL `_id_core` 的既有行为一致（含文档给的样例）。"""
    cases = {
        "<FZSDCS34-SyRS005>": "syrs5",
        "<DCS-SyRS005>": "syrs5",
        "<FZSDCS34-SyRS0011>": "syrs11",
        "<DCS-SyRS011>": "syrs11",
    }
    for raw, want in cases.items():
        got = ids.id_core(raw)
        assert got == want, f"id_core({raw!r}) = {got!r}，期望 {want!r}"

    assert ids.normalize_req_id("< DCS-SyRS005 >") == "<DCS-SyRS005>"

    # 位号归一：与 SAMA-V1 kb_io._norm_tag_key 行为一致
    assert ids.normalize_tag_key("YTSM007MP") == "MP007"
    assert ids.normalize_tag_key("MP007") == "MP007"
    assert ids.normalize_tag_key(" m p007 ") == "MP007"


def test_node_id_namespaces():
    assert ids.node_id_doc_req("SyRS", "<DCS-SyRS005>") == "doc:SyRS:req:<DCS-SyRS005>"
    assert ids.node_id_doc_sec("UR", "3.2.2.1") == "doc:UR:sec:3.2.2.1"
    assert ids.node_id_diagram_block("FN-SAMA-001", 5, "MP101") == "dia:FN-SAMA-001:p5:MP101"
    assert ids.node_id_diagram_loop("FN-SAMA-001", "L01") == "dia:FN-SAMA-001:loop:L01"
    assert ids.node_id_testcase("loop:L01", 3) == "test:loop:L01:003"

    parsed = ids.split_node_id("dia:FN-SAMA-001:p5:MP101")
    assert parsed["domain"] == "diagram" and parsed["doc_id"] == "FN-SAMA-001"
    assert parsed["scope"] == "p5" and parsed["local"] == "MP101"


def test_both_domains_fit_one_kernel():
    """A1 验收：文献域条目与图形域功能块能共存于同一图谱。"""
    g = TraceGraph(meta={"project": "安全壳 SAMA 样例"})

    req = TraceNode(
        node_id=ids.node_id_doc_req("SyRS", "<DCS-SyRS005>"),
        domain="doc", kind="requirement_item",
        label="安全壳压力高自动停堆",
        payload={"raw_id": "<DCS-SyRS005>", "chapter": "3.2.2.1"},   # 域内结构原样保留
        attrs={"tags": ["MP101"], "safety_class": ["F-SC3"], "domain_analog_bool": "BOOLEAN"},
        locator=Locator(doc_id="SyRS", page=42, char_start=1024, char_end=1068,
                        note="页码依据 PDF 物理页"),
    )
    loop = TraceNode(
        node_id=ids.node_id_diagram_loop("FN-SAMA-001", "L01"),
        domain="diagram", kind="loop", label="CAM101MP 安全壳大气压力保护回路",
        payload={"loop_id": "L01", "type": "protection", "members": ["MP101", "IM#1", "IM#2"]},
        attrs={"tags": ["MP101", "401XU1"], "domain_analog_bool": "MIXED", "cls": "loop"},
        locator=Locator(doc_id="FN-SAMA-001", page=5, bbox=(120.5, 300.2, 480.0, 620.7),
                        node_id="L01"),
    )
    g.add_node(req)
    g.add_node(loop)

    ev = Evidence(
        kind="tag", method="tag_overlap_set_intersection", score=0.667,
        locator=Locator(doc_id="SyRS", page=42, char_start=1030, char_end=1038),
        raw_snippet="MP101", score_scale="overlap_ratio[0,1]", channel="A1_tag",
    )
    e = TraceEdge(
        edge_id=ids.edge_id(loop.node_id, req.node_id, "implements"),
        src=loop.node_id, dst=req.node_id, relation="implements",
        status="CANDIDATE", confidence=0.667, source_mode="bridge",
        evidence=[ev], ambiguous=True,
    )
    g.add_edge(e)

    st = g.stats()
    assert st["nodes"] == 2 and st["edges"] == 1
    assert st["nodes_by_domain"] == {"doc": 1, "diagram": 1}
    assert st["edges_without_evidence"] == 0
    assert g.nodes[loop.node_id].payload["loop_id"] == "L01"      # payload 未被重塑

    # 跨域遍历：从需求条目沿下游可达图纸回路
    assert loop.node_id in g.downstream_of(req.node_id, depth=1)


def test_a2_gate_rejects_evidence_less_edge():
    """A2 验收：无证据边必须被硬拒绝（"候选发现→证据验证"成对调用固化）。"""
    g = TraceGraph()
    a = TraceNode(node_id="doc:X:req:<A1>", domain="doc", kind="requirement_item", label="A")
    b = TraceNode(node_id="doc:Y:req:<B1>", domain="doc", kind="requirement_item", label="B")
    g.add_node(a); g.add_node(b)

    naked = TraceEdge(edge_id="e::satisfies::b=>a", src=a.node_id, dst=b.node_id,
                      relation="satisfies", status="ACCEPTED",
                      confidence=0.9, source_mode="table", evidence=[])
    try:
        g.add_edge(naked)
    except ValueError as exc:
        assert "无证据边" in str(exc)
    else:
        raise AssertionError("无证据边竟然通过了守门校验")

    # strict=False 时允许占位写入（临时中间态）
    g.add_edge(naked, strict=False)
    assert g.stats()["edges_without_evidence"] == 1

    # 悬空边也必须被拒绝
    ghost = TraceEdge(edge_id="e::x", src="doc:Z:req:<Z9>", dst=b.node_id,
                      relation="refs", status="CANDIDATE", confidence=0.5,
                      source_mode="rule",
                      evidence=[Evidence(kind="rule", method="m", score=1.0,
                                         locator=Locator(doc_id="Z"))])
    try:
        g.add_edge(ghost)
    except ValueError as exc:
        assert "悬空边" in str(exc)
    else:
        raise AssertionError("悬空边竟然通过了守门校验")


def test_evidence_score_not_normalized_and_locator_enforced():
    """设计纪律 2 验收：原始分不平移；A2 要求证据可定位。"""
    anchor = Evidence(kind="anchor", method="chapter_anchor_hit", score=0.031,
                      locator=Locator(doc_id="SyRS", page=7, char_start=10, char_end=20),
                      score_scale="anchor_raw", channel="anchor")
    assert anchor.score == 0.031        # 未被归一到 [0,1] 之外的任何尺度
    assert anchor.locator.is_located()

    e = TraceEdge(edge_id="e1", src="a", dst="b", relation="satisfies", status="ACCEPTED",
                  confidence=0.5, source_mode="hybrid", evidence=[anchor])
    assert e.evidence_verifiable()

    # 定位器为空 → 视为不可复核
    blind = Evidence(kind="semantic", method="cosine", score=0.8, locator=Locator(doc_id="X"))
    e2 = TraceEdge(edge_id="e2", src="a", dst="b", relation="satisfies", status="ACCEPTED",
                   confidence=0.5, source_mode="discover", evidence=[blind])
    assert not e2.evidence_verifiable()

    try:
        Evidence(kind="semantic", method="cosine", score=None, locator=Locator(doc_id="X"))
    except ValueError:
        pass
    else:
        raise AssertionError("score=None 应被拒绝（禁止空证据）")


def test_a3_llm_opinion_never_writes_confidence():
    """A3 验收：LLM 意见独立成字段，不覆盖判定分。"""
    ev = Evidence(kind="nli", method="nli_entailment", score=0.71,
                  locator=Locator(doc_id="UR", page=3, char_start=1, char_end=9))
    e = TraceEdge(edge_id="e", src="a", dst="b", relation="satisfies", status="SUSPICIOUS",
                  confidence=0.71, source_mode="hybrid", evidence=[ev],
                  llm_opinion="建议确认：两处均描述同一保护动作",
                  llm_confidence=0.93, llm_cached=True)
    assert e.confidence == 0.71 and e.llm_confidence == 0.93 and e.confidence != e.llm_confidence


def test_fingerprint_reproducible():
    """可复现性验收：同内容两次构造 → 指纹一致；忽略时间类 meta。"""
    def build(ts: float) -> TraceGraph:
        g = TraceGraph(meta={"run_at": ts, "project": "P"})
        n1 = TraceNode(node_id="doc:A:req:<R1>", domain="doc", kind="requirement_item", label="R1")
        n2 = TraceNode(node_id="doc:A:req:<R2>", domain="doc", kind="requirement_item", label="R2")
        g.add_node(n1); g.add_node(n2)
        g.add_edge(TraceEdge(
            edge_id="e::satisfies::R2=>R1", src=n1.node_id, dst=n2.node_id,
            relation="satisfies", status="ACCEPTED", confidence=0.9, source_mode="table",
            evidence=[Evidence(kind="lexical", method="char_jaccard", score=0.93,
                               locator=Locator(doc_id="A", page=1, char_start=0, char_end=5))]))
        return g

    assert build(1.0).fingerprint() == build(999.0).fingerprint()

    g2 = build(1.0)
    g2.edges[list(g2.edges)[0]].confidence = 0.89
    assert build(1.0).fingerprint() != g2.fingerprint()


def test_graph_roundtrip_json():
    g = TraceGraph(meta={"m": 1})
    n = TraceNode(node_id="dia:D:p1:MP101", domain="diagram", kind="func_block", label="MP101",
                  payload={"cls": "bubble"}, attrs={"tags": ["MP101"]},
                  locator=Locator(doc_id="D", page=1, bbox=(1.0, 2.0, 3.0, 4.0), node_id="MP101"))
    g.add_node(n)
    g.add_node(TraceNode(node_id="doc:D:req:<R1>", domain="doc", kind="requirement_item",
                         label="R1"))
    g.add_edge(TraceEdge(edge_id="e", src=n.node_id, dst="doc:D:req:<R1>", relation="implements",
                         status="CANDIDATE", confidence=0.8, source_mode="bridge",
                         evidence=[Evidence(kind="tag", method="overlap", score=0.5,
                                            locator=Locator(doc_id="D", page=1,
                                                            bbox=(1.0, 2.0, 3.0, 4.0)))]))
    restored = TraceGraph.from_dict(g.to_dict())
    assert restored.fingerprint() == g.fingerprint()
    assert isinstance(restored.nodes[n.node_id].locator.bbox, tuple)


def test_thresholds_externalized_and_env_override():
    """阈值外置验收：默认值与原实现一致，且环境变量覆盖仍有效。"""
    assert abs(C.th("doc.embedding_exact") - 0.95) < 1e-9
    assert abs(C.th("doc.discovery.theta_accept") - 0.45) < 1e-9
    assert abs(C.th("doc.discovery.anchor_gate") - 0.30) < 1e-9
    assert abs(C.th("bridge.tag_boost") - 0.50) < 1e-9
    assert C.th("diagram.basis_rank")["io_source"] == 3
    assert C.th("testcase.truth.boolean_full_enum_max_inputs") == 6
    assert C.th("orchestration.confidence_tiers.auto_confirm") == 0.85
    assert abs(C.th("llm.temperature") - 0.1) < 1e-9

    os.environ["TRACE_NL_TH_EMB_EXACT"] = "0.88"
    try:
        assert abs(C.thresholds(reload=True).doc.embedding_exact - 0.88) < 1e-9
    finally:
        os.environ.pop("TRACE_NL_TH_EMB_EXACT", None)
        C.thresholds(reload=True)
    assert abs(C.th("doc.embedding_exact") - 0.95) < 1e-9


def test_tool_registry_contract():
    reg = REGISTRY
    reg.register(ToolSpec(
        name="_t.read", family="doc", summary="只读示例",
        input_schema={"type": "object", "required": ["pdf_path"],
                      "properties": {"pdf_path": {"type": "string"},
                                     "mode": {"type": "string",
                                              "enum": ["table", "discover", "hybrid"]}}},
        readonly=True, idempotent=True,
        handler=lambda pdf_path, mode="hybrid": ok({"pdf_path": pdf_path, "mode": mode})))
    reg.register(ToolSpec(
        name="_t.write", family="graph", summary="写入示例",
        input_schema={"type": "object"}, readonly=False, idempotent=False,
        side_effects=["artifacts/x.xlsx"], implemented=False))

    assert reg.audit_readonly()["all_readonly"] is False
    assert set(reg.audit_readonly()["writers"]) == {"_t.write"}
    assert reg.missing_implementations() == ["_t.write"]

    r = reg.call("_t.read", {"pdf_path": "a.pdf", "mode": "table"})
    assert r.ok and r.data["mode"] == "table" and r.trace["readonly"] is True

    bad = reg.call("_t.read", {"mode": "table"})
    assert not bad.ok and bad.error_code == ErrorCode.SCHEMA_INVALID and "pdf_path" in bad.error_msg

    bad2 = reg.call("_t.read", {"pdf_path": "a.pdf", "mode": "nope"})
    assert not bad2.ok and "取值须属于" in bad2.error_msg

    pend = reg.call("_t.write", {})
    assert not pend.ok and pend.error_code == ErrorCode.NOT_IMPLEMENTED

    assert not reg.call("_t.nope", {}).ok


# ---------------------------------------------------------------------
def _main() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
