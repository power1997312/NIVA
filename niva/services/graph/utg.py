# -*- coding: utf-8 -*-
"""UTG 双域追溯图谱：构建 / 查询 / 影响分析 / 覆盖审计（架构方案 L1 数据层落地）。

**跨域衔接的诚实立场**（依据 R1 实测结论与 docx §8.7 第 6 条）：
    - 位号级图-文锚点已被证伪（0/66），**不做自动语义挂接**，避免跨域误关联；
    - 自动部分仅做 **A2b 系统代号锚点**（确定性、无 LLM），且一律落为
      `CANDIDATE` 状态 + 低置信 —— 进入"辅助人审"档，须人工确认后转正；
    - 人工确认的跨域边由 `knowledge/bridges/manual_links.yml` 登记（署名留痕）。

这正好演示了置信度四级处置的价值：粗粒度锚点不该得到高置信。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ... import config as C
from ...adapters.diagram.service import DiagramAdapter
from ...adapters.doc.service import DocAdapter
from ...kernel.graph_store import GraphStore
from ...kernel.ids import edge_id
from ...kernel.model import Evidence, Locator, TraceEdge, TraceGraph, TraceNode

RE_SYS_CODE = re.compile(r"\b([A-Z]{2,6})\b")
MANUAL_LINKS = C.KNOWLEDGE_ROOT / "bridges" / "manual_links.yml"


@dataclass
class UTGStats:
    nodes: int = 0
    edges: int = 0
    by_domain: dict = field(default_factory=dict)
    by_relation: dict = field(default_factory=dict)
    bridge_candidates: int = 0
    bridge_confirmed: int = 0
    sources: dict = field(default_factory=dict)


class UTGBuilder:
    def __init__(self, doc: Optional[DocAdapter] = None,
                 diagram: Optional[DiagramAdapter] = None,
                 doc_id: str = "SAMA") -> None:
        self.doc = doc or DocAdapter()
        self.diagram = diagram or DiagramAdapter()
        self.doc_id = doc_id

    # ------------------------------------------------------------------
    def build(self, *, matrix_id: Optional[str] = None,
              pages: Optional[list[int]] = None,
              include_cases: bool = True) -> tuple[TraceGraph, UTGStats]:
        g = TraceGraph(meta={"kind": "UTG", "doc_id": self.doc_id})
        st = UTGStats()
        st.sources["doc_matrices"] = []
        st.sources["diagram_pages"] = []

        # ---- 文献域子图 ----
        for mid in self._doc_matrices(matrix_id):
            sub = self.doc.load_graph(mid)
            if sub is None:
                continue
            _merge(g, sub)
            st.sources["doc_matrices"].append(mid)

        # ---- 图形域子图 ----
        ir_all = self.diagram.load_ir_all()
        page_list = pages or ([int((p.get("meta") or {}).get("page") or 0)
                               for p in (ir_all or {}).get("pages", [])] if ir_all else [])
        for pno in page_list:
            ir = self.diagram.load_ir(pno)
            if not ir:
                continue
            _merge(g, self.diagram.to_graph(ir, doc_id=self.doc_id))
            st.sources["diagram_pages"].append(pno)

        # ---- 跨域桥接 ----
        n_cand = self._bridge_system_codes(g)
        n_conf = self._bridge_manual(g)
        st.bridge_candidates, st.bridge_confirmed = n_cand, n_conf
        if n_cand == 0 and n_conf == 0:
            g.meta["bridge_warning"] = (
                "未产生任何跨域桥接：本语料中需求条目文本未引用图纸的系统代号，"
                "且无人工登记。需求链与图纸链当前**互不连通**，影响分析/覆盖审计"
                "只能在各自域内进行。衔接须人工登记（knowledge/bridges/manual_links.yml）。")

        st.nodes, st.edges = len(g.nodes), len(g.edges)
        st.by_domain = _count(g.nodes.values(), lambda n: n.domain)
        st.by_relation = _count(g.edges.values(), lambda e: e.relation)
        return g, st

    # ------------------------------------------------------------------
    def _doc_matrices(self, matrix_id: Optional[str]) -> list[str]:
        if matrix_id:
            return [matrix_id]
        d = C.ARTIFACTS_DIR / "matrices"
        if not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.json"))

    def _bridge_system_codes(self, g: TraceGraph) -> int:
        """A2b 系统代号锚点：图纸系统号/图名 ↔ 文档条目文本中的系统代号。

        只产生 CANDIDATE + 低置信（粗粒度锚点不该得到高置信），交人工确认。
        """
        accept = float(C.th("bridge.accept", 0.45))
        w_ref = float(C.th("bridge.base_weights.ref", 0.15))
        # 图纸侧系统代号集
        dia_codes: dict[str, str] = {}
        for n in g.nodes.values():
            if n.domain != "diagram":
                continue
            sysno = str((n.attrs or {}).get("system") or "").strip().upper()
            code = re.sub(r"^[A-Z]", "", sysno) if len(sysno) > 2 else sysno
            if code:
                dia_codes.setdefault(code, n.node_id)
        if not dia_codes:
            return 0
        # 文档侧：条目文本中的系统代号
        n = 0
        for node in list(g.nodes.values()):
            if node.domain != "doc" or node.kind != "requirement_item":
                continue
            text = str((node.payload or {}).get("content") or "") + " " + node.label
            codes = {c for c in RE_SYS_CODE.findall(text.upper())
                     if c in dia_codes and c not in {"FC", "NC", "DCS", "RPS", "PDF"}}
            if not codes:
                continue
            code = sorted(codes)[0]
            conf = round(min(1.0, w_ref * 1.0), 4)     # 仅一类锚点 → 低置信
            e = TraceEdge(
                edge_id=edge_id(dia_codes[code], node.node_id, "implements"),
                src=dia_codes[code], dst=node.node_id, relation="implements",
                status="CANDIDATE", confidence=conf, source_mode="bridge:system_code",
                evidence=[Evidence(
                    kind="tag", method="a2b_system_code_overlap", score=1.0,
                    locator=Locator(doc_id=self.doc_id, note="系统代号命中（粗粒度）"),
                    raw_snippet=code, score_scale="boolean", channel="A2b")],
                ambiguous=False,
                degrade_reason="系统代号锚点为粗粒度证据，置信度低于接受阈值，须人工确认")
            try:
                g.add_edge(e)
                n += 1
            except ValueError:
                continue
            _ = accept
        return n

    def _bridge_manual(self, g: TraceGraph) -> int:
        """人工确认的跨域边（署名留痕）。这是需求链与图纸链衔接的**正式**途径。"""
        if not MANUAL_LINKS.exists():
            return 0
        import yaml
        doc = yaml.safe_load(MANUAL_LINKS.read_text(encoding="utf-8")) or {}
        n = 0
        for lk in doc.get("links") or []:
            src, dst = lk.get("src"), lk.get("dst")
            if src not in g.nodes or dst not in g.nodes:
                continue
            rel = lk.get("relation", "implements")
            e = TraceEdge(
                edge_id=edge_id(src, dst, rel), src=src, dst=dst, relation=rel,
                status="CONFIRMED", confidence=float(lk.get("confidence", 1.0)),
                source_mode="manual",
                evidence=[Evidence(
                    kind="rule", method="manual_registration", score=1.0,
                    locator=Locator(doc_id=self.doc_id, note="人工登记"),
                    raw_snippet=str(lk.get("note") or ""), score_scale="boolean",
                    channel="human")],
                human_confirmed=True, reviewer=str(lk.get("reviewer") or "人工"),
            )
            try:
                g.add_edge(e)
                n += 1
            except ValueError:
                continue
        return n


# =====================================================================
# 查询 / 影响分析 / 覆盖审计
# =====================================================================
def utg_query(g: TraceGraph, *, domain: Optional[str] = None,
              kind: Optional[str] = None, relation: Optional[str] = None,
              status: Optional[str] = None,
              min_confidence: Optional[float] = None,
              limit: int = 50) -> dict:
    nodes = [n for n in g.nodes.values()
             if (domain is None or n.domain == domain)
             and (kind is None or n.kind == kind)]
    edges = [e for e in g.edges.values()
             if (relation is None or e.relation == relation)
             and (status is None or e.status == status)
             and (min_confidence is None or e.confidence >= float(min_confidence))]
    return {"nodes": [_brief_node(n) for n in nodes[:limit]],
            "nodes_total": len(nodes),
            "edges": [{"edge_id": e.edge_id, "src": e.src, "dst": e.dst,
                       "relation": e.relation, "status": e.status,
                       "confidence": e.confidence,
                       "human_confirmed": e.human_confirmed} for e in edges[:limit]],
            "edges_total": len(edges),
            "stats": g.stats()}


def impact_analysis(g: TraceGraph, node_id: str, change_kind: str = "text_modify"
                    ) -> dict:
    """变更影响：沿追溯链反向可达遍历（src = 下游依赖方）。"""
    if node_id not in g.nodes:
        return {"found": False, "node_id": node_id,
                "reason": "节点不在 UTG 中（未解析/未登记）"}
    affected = g.downstream_of(node_id, depth=12)
    affected.discard(node_id)
    by_domain: dict[str, list] = {}
    for nid in affected:
        n = g.nodes[nid]
        by_domain.setdefault(n.domain, []).append(
            {"node_id": nid, "label": n.label, "kind": n.kind})
    for v in by_domain.values():
        v.sort(key=lambda x: x["node_id"])
    # 影响强度分级：直接（1 跳）/ 传递（2-3 跳）/ 潜在（更远）
    by_level = {"direct": [], "transitive": [], "potential": []}
    for nid in affected:
        d = _min_dist(g, node_id, nid)
        bucket = "direct" if d == 1 else ("transitive" if d <= 3 else "potential")
        by_level[bucket].append(nid)
    need_recheck = [nid for nid in affected if g.nodes[nid].kind == "case"]
    return {"found": True, "node_id": node_id, "change_kind": change_kind,
            "affected_total": len(affected), "by_domain": by_domain,
            "by_level": {k: len(v) for k, v in by_level.items()},
            "affected_sample": {k: v[:8] for k, v in by_domain.items()},
            "need_recheck_cases": need_recheck,
            "checklist": [
                "① 复核受影响需求条目的文本与分级",
                "② 复核受影响图纸回路的实现一致性",
                "③ 重算受影响测试用例的判据（期望值须重新推演）",
                "④ 重跑对应追溯矩阵的验证阶段（着色结果失效）"],
            }


def coverage_audit(g: TraceGraph, project: str = "") -> dict:
    """覆盖审计：未追溯条目 / 无源用例 / 未确认的桥接候选。"""
    traced_targets = set()
    for e in g.edges.values():
        if e.relation in ("satisfies", "realizes", "implements") \
                and e.status in ("ACCEPTED", "CONFIRMED"):
            traced_targets.add(e.src)      # 被追溯到的下游对象
            traced_targets.add(e.dst)

    untraced = []
    for n in g.nodes.values():
        if n.domain == "doc" and n.kind == "requirement_item" and n.node_id not in traced_targets:
            untraced.append({"node_id": n.node_id, "label": n.label,
                             "reason": "需求条目未建立任何已确认追溯关系"})
    orphan_cases = []
    for n in g.nodes.values():
        if n.domain == "test" and n.node_id not in traced_targets:
            orphan_cases.append({"node_id": n.node_id, "label": n.label,
                                 "reason": "用例无图纸逻辑来源（无源用例）"})
    unconfirmed_bridge = [
        {"edge_id": e.edge_id, "src": e.src, "dst": e.dst,
         "confidence": e.confidence}
        for e in g.edges.values()
        if e.relation == "implements" and e.status == "CANDIDATE"]
    total_req = sum(1 for n in g.nodes.values()
                    if n.domain == "doc" and n.kind == "requirement_item")
    total_case = sum(1 for n in g.nodes.values() if n.domain == "test")
    return {
        "project": project,
        "untraced_items": untraced, "untraced_count": len(untraced),
        "requirement_total": total_req,
        "coverage_ratio": round((total_req - len(untraced)) / total_req, 4) if total_req else None,
        "orphan_cases": orphan_cases, "orphan_count": len(orphan_cases),
        "case_total": total_case,
        "unconfirmed_bridge": unconfirmed_bridge,
        "unconfirmed_bridge_count": len(unconfirmed_bridge),
        "warnings": [
            "桥接候选为系统代号粗粒度锚点，须经人工确认后方可计入覆盖率",
        ] if unconfirmed_bridge else [],
    }


# ---------------------------------------------------------------------
def _merge(g: TraceGraph, sub: TraceGraph) -> None:
    for nid, n in sub.nodes.items():
        if nid in g.nodes and g.nodes[nid] != n:
            continue
        g.nodes[nid] = n
    for eid, e in sub.edges.items():
        if eid in g.edges:
            continue
        try:
            g.add_edge(e, strict=False)
        except ValueError:
            continue


def _brief_node(n: TraceNode) -> dict:
    return {"node_id": n.node_id, "domain": n.domain, "kind": n.kind,
            "label": n.label, "locatable": bool(n.locator and n.locator.is_located())}


def _count(items: Iterable[Any], key) -> dict:
    d: dict[str, int] = {}
    for x in items:
        k = str(key(x))
        d[k] = d.get(k, 0) + 1
    return d


def _min_dist(g: TraceGraph, start: str, target: str) -> int:
    from collections import deque
    dist = {start: 0}
    q = deque([start])
    while q:
        cur = q.popleft()
        if cur == target:
            return dist[cur]
        for e in g.out_edges(cur):
            if e.src not in dist:
                dist[e.src] = dist[cur] + 1
                q.append(e.src)
    return 10**6
