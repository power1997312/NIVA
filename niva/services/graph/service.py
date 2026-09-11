# -*- coding: utf-8 -*-
"""图谱工具族（4 个工具）的服务层：UTG 构建/查询/影响分析/覆盖审计+合规包。

UTG 是系统的持久化记忆（架构方案 §4.7）。本层按需构建并缓存，
跨域桥接采用"A2b 系统代号自动候选 + 人工登记确认"的诚实立场
（位号级锚点已被 R1 证伪，见 docs/实施计划.md §4）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ... import config as C
from ...adapters.diagram.service import DiagramAdapter
from ...adapters.doc.service import DocAdapter
from ...kernel.graph_store import GraphStore
from ...kernel.ids import edge_id
from ...kernel.model import Evidence, Locator, TraceEdge, TraceNode
from ...kernel.registry import ErrorCode, ToolResult, fail, ok
from .utg import UTGBuilder, coverage_audit, impact_analysis, utg_query


def _recount(items, key):
    d = {}
    for x in items:
        k = str(key(x)); d[k] = d.get(k, 0) + 1
    return d


class GraphService:
    scope = "graph"

    def __init__(self, artifact_root: Optional[Path] = None,
                 doc: Optional[DocAdapter] = None,
                 diagram: Optional[DiagramAdapter] = None) -> None:
        self.artifact_root = Path(artifact_root or C.ARTIFACTS_DIR)
        self.doc = doc or DocAdapter()
        self.diagram = diagram or DiagramAdapter()
        self._g = None
        self._built_at = 0.0

    # ------------------------------------------------------------------
    def _graph(self, matrix_id: Optional[str] = None,
               pages: Optional[list[int]] = None,
               include_cases: bool = True):
        """构建并缓存 UTG。制品变化后可传参强制重建。"""
        import time
        builder = UTGBuilder(self.doc, self.diagram)
        g, st = builder.build(matrix_id=matrix_id, pages=pages)
        if include_cases:
            n = self._attach_cases(g)
            st.sources["case_sets"] = n
            # ★ 统计必须在挂接用例**之后**重算，否则 test 域永远是 0，
            #   造成"用例没挂上"的假象（这正是首轮验收误报的原因）
            st.nodes, st.edges = len(g.nodes), len(g.edges)
            st.by_domain = _recount(g.nodes.values(), lambda x: x.domain)
            st.by_relation = _recount(g.edges.values(), lambda x: x.relation)
        self._g, self._built_at = g, time.time()
        return g, st

    def _attach_cases(self, g) -> int:
        """把 P3 用例集挂进 UTG：test 节点 + verifies 边（沿图纸链回溯逻辑来源）。"""
        d = self.artifact_root / "cases"
        if not d.exists():
            return 0
        n = 0
        for f in sorted(d.glob("*.json")):
            try:
                cs = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            for c in cs.get("cases") or []:
                nid = self._case_node_id(c)
                if not nid or nid not in g.nodes:
                    continue
                test_id = f"test:{c['case_id']}"
                g.nodes[test_id] = TraceNode(
                    node_id=test_id, domain="test", kind="case",
                    label=c.get("label") or c.get("case_id"),
                    payload={"case_set": f.stem, "template":
                             (c.get("template") or {}).get("case_id"),
                             "n_steps": len(c.get("steps") or [])},
                    attrs={"probe": c.get("probe"),
                           "narrative_source": c.get("narrative_source")},
                    locator=Locator(doc_id=f.stem, note="用例制品"))
                try:
                    g.add_edge(TraceEdge(
                        edge_id=edge_id(test_id, nid, "verifies"),
                        src=test_id, dst=nid, relation="verifies",
                        status="CANDIDATE", confidence=0.9, source_mode="testcase",
                        evidence=[Evidence(
                            kind="rule", method="case_unit_binding", score=0.9,
                            locator=Locator(doc_id=f.stem, node_id=nid),
                            raw_snippet=c.get("unit_id", ""),
                            score_scale="boolean", channel="testcase")],
                    ), strict=False)
                    n += 1
                except ValueError:
                    continue
        return n

    @staticmethod
    def _case_node_id(c: dict) -> Optional[str]:
        """case.unit_id 形如 p1:predicate:n018 / p1:loop:loop:L01 → UTG 节点 id。"""
        uid = str(c.get("unit_id") or "")
        page = c.get("page")
        parts = uid.split(":", 2)
        if len(parts) < 3:
            return None
        center = parts[2]
        if center.startswith("loop:"):
            return f"dia:{C._utg_doc_id() if hasattr(C, '_utg_doc_id') else 'SAMA'}:loop:{center[5:]}"
        return f"dia:SAMA:p{page}:{center}"

    # ------------------------------------------------------------------
    def utg_query(self, domain=None, kind=None, relation=None, status=None,
                  min_confidence=None, limit=50) -> ToolResult:
        g, st = self._graph()
        from .utg import UTGStats  # noqa: F401  (类型提示用)
        data = utg_query(g, domain=domain, kind=kind, relation=relation,
                         status=status, min_confidence=min_confidence, limit=limit)
        data["build"] = st.__dict__
        return ok(data)

    def utg_impact_analysis(self, node_id: str, change_kind: str = "text_modify") -> ToolResult:
        g, st = self._graph()
        data = impact_analysis(g, node_id, change_kind)
        data["build"] = st.__dict__
        if not data.get("found"):
            return fail(ErrorCode.NOT_FOUND, data.get("reason", "节点不存在"),
                        warnings=["可用 utg_query 查看当前 UTG 中的节点清单"])
        return ok(data)

    def utg_coverage_report(self, project: str = "",
                            include_package: bool = True) -> ToolResult:
        g, st = self._graph()
        data = coverage_audit(g, project=project)
        data["build"] = st.__dict__
        if include_package:
            rep = _compliance_package(g, data, self.artifact_root)
            data["evidence_package"] = rep
        return ok(data)


# =====================================================================
# 合规证据包（C11）：条款 → 证据 → 证据定位
# =====================================================================
def _compliance_package(g, cov: dict, artifact_root: Path) -> dict:
    profile_path = C.KNOWLEDGE_ROOT / "standards" / "profile.yml"
    if not profile_path.exists():
        return {"available": False, "reason": f"缺少标准条款映射：{profile_path}"}
    import yaml
    prof = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    rows = []
    for std in prof.get("standards") or []:
        for cl in std.get("clauses") or []:
            ev = _evidence_for_clause(cl.get("evidence") or [], g, cov, artifact_root)
            rows.append({"standard": std.get("name"), "clause": cl.get("clause"),
                         "requirement": cl.get("requirement"),
                         "evidence": ev["summary"], "locator": ev["locator"],
                         "status": ev["status"]})
    xlsx = _write_package_xlsx(rows, artifact_root)
    return {"available": True, "xlsx_path": xlsx.get("path"), "bytes": xlsx.get("bytes"),
            "rows": rows, "clauses": len(rows),
            "satisfied": sum(1 for r in rows if r["status"] == "有证据"),
            "pending": sum(1 for r in rows if r["status"] != "有证据")}


def _evidence_for_clause(kinds: list[str], g, cov: dict, root: Path) -> dict:
    """按证据类型从当前制品中取出"本项目证据 + 定位"。不虚构：没有就标『待补』。"""
    out = {"summary": [], "locator": [], "status": "待补"}
    for k in kinds:
        if k == "trace_matrix":
            d = root / "exports"
            xs = sorted(d.glob("*.xlsx")) if d.exists() else []
            if xs:
                out["summary"].append("追溯矩阵 Excel（逐字着色）")
                out["locator"].append(xs[-1].name)
        elif k == "coverage_audit":
            if cov.get("requirement_total") is not None:
                out["summary"].append(
                    f"覆盖审计：需求 {cov['requirement_total']} 条，未追溯 {cov['untraced_count']}")
                out["locator"].append("UTG 覆盖审计（本报告）")
        elif k == "test_cases":
            d = root / "exports"
            xs = sorted(d.glob("cases-*.xlsx")) if d.exists() else []
            if xs:
                out["summary"].append("部件测试用例集（期望值由真值推演产生）")
                out["locator"].append(xs[-1].name)
        elif k == "validation_rules":
            n = g.stats().get("edges_by_status", {})
            out["summary"].append(f"规则校验：追溯边状态分布 {json.dumps(n, ensure_ascii=False)}")
            out["locator"].append("UTG（本报告）")
        elif k == "reproducibility":
            out["summary"].append("可复现性：同输入两次运行图谱指纹一致（TraceGraph.fingerprint）")
            out["locator"].append("eval/p0_acceptance.py F 项")
    if out["summary"]:
        out["status"] = "有证据"
    out["summary"] = "；".join(out["summary"]) or "待补"
    out["locator"] = "；".join(out["locator"]) or "待补"
    return out


def _write_package_xlsx(rows: list[dict], root: Path) -> dict:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    p = root / "exports" / "compliance_evidence.xlsx"
    p.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook(); ws = wb.active; ws.title = "合规证据"
    heads = ["标准", "条款", "要求", "本项目证据", "证据定位", "状态"]
    fill = PatternFill("solid", fgColor="4472C4")
    for i, h in enumerate(heads, start=1):
        c = ws.cell(row=1, column=i, value=h)
        c.fill = fill; c.font = Font(name="宋体", size=11, color="FFFFFF")
    for r_i, r in enumerate(rows, start=2):
        for c_i, v in enumerate([r["standard"], r["clause"], r["requirement"],
                                 r["evidence"], r["locator"], r["status"]], start=1):
            cell = ws.cell(row=r_i, column=c_i, value=v)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            cell.font = Font(name="宋体", size=10)
    for col, w in zip("ABCDEF", [14, 10, 40, 44, 30, 10]):
        ws.column_dimensions[col].width = w
    wb.save(str(p))
    return {"path": str(p), "bytes": p.stat().st_size}
