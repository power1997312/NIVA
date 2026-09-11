# -*- coding: utf-8 -*-
"""统一工具面：22 个工具 × 4 个能力作用域。

对应方案 4-3 表：图纸族 9（已有） + 文档族 5 + 生成族 4 + 图谱族 4 = 22。

作用域划分依据（不是按数量，而是按**权限域与确定性边界**）
-------------------------------------------------------------
    doc      5 个   LLM 权限 = adjudicate_only   （唯一 LLM 点位：歧义裁决）
    diagram  9 个   LLM 权限 = none              （全符号管线，构造性禁止大模型）
    generate 4 个   LLM 权限 = narrate_only      （步骤由算法出，LLM 只写说明）
    graph    4 个   LLM 权限 = none              （纯规则与统计）

实现在 14 / 契约冻结待实现 8（生成族 4 + 图谱族 4 属 P3/P5）。
"""
from __future__ import annotations

from typing import Any, Optional

from ..adapters.diagram.service import DiagramAdapter
from ..adapters.doc.service import DocAdapter
from ..kernel.registry import (
    LLM_ADJUDICATE, LLM_NARRATE, LLM_NONE, REGISTRY, ToolRegistry, ToolResult,
    ToolSpec, ok,
)
from ..services.graph.service import GraphService
from ..services.testcase.service import TestCaseService

__all__ = ["build_registry", "DEFAULT_ARTIFACT_ROOT", "EXTERNAL_READONLY_ALLOWLIST"]


DEFAULT_ARTIFACT_ROOT = None  # 由 build_registry 的 artifacts 参数决定


# ---------------------------------------------------------------------
# 未实现工具的占位处理器：返回明确的 NOT_IMPLEMENTED，不假装可用
# ---------------------------------------------------------------------
def _placeholder(stage: str):
    def _h(**kw: Any) -> ToolResult:
        from ..kernel.registry import ErrorCode, fail
        return fail(ErrorCode.NOT_IMPLEMENTED,
                    f"契约已冻结，实现属 {stage} 阶段（见 docs/实施计划.md）")
    return _h


# =====================================================================
def build_registry(doc: Optional[DocAdapter] = None,
                   diagram: Optional[DiagramAdapter] = None,
                   cases: Optional[TestCaseService] = None,
                   graph: Optional[GraphService] = None,
                   registry: Optional[ToolRegistry] = None) -> ToolRegistry:
    """构建并返回注册了全部 22 个工具的注册表（幂等：重复调用会重建）。

    参数留出注入点，便于测试替换适配器（例如注入假 adapter）。
    """
    reg = registry or ToolRegistry()
    doc = doc or DocAdapter()
    diagram = diagram or DiagramAdapter()
    cases = cases or TestCaseService()
    graph = graph or GraphService()

    # ---------------- 文档族（5）----------------
    reg.register(ToolSpec(
        name="parse_requirement_doc", family="doc", scope="doc",
        summary="解析需求/设计文档并抽取条目与章节（PDF 双引擎，需文本层）",
        input_schema={"type": "object", "required": ["pdf_path"],
                      "properties": {"pdf_path": {"type": "string"},
                                     "doc_type": {"type": "string"}}},
        readonly=True, idempotent=True, requires_knowledge=False,
        handler=lambda pdf_path, doc_type=None: doc.parse_requirement_doc(pdf_path, doc_type),
    ))
    reg.register(ToolSpec(
        name="build_trace_matrix", family="doc", scope="doc",
        summary="构建四级需求链正/逆向追溯矩阵（table 查表 / discover 纯发现 / hybrid 混合）",
        input_schema={"type": "object", "properties": {
            "downstream_pdf": {"type": "string"},
            "upstream_pdfs": {"type": "object"},
            "design_pdfs": {"type": "object"},
            "sys_req_pdf": {"type": "string"},
            "mode": {"type": "string", "enum": ["table", "discover", "hybrid"]}}},
        readonly=False, idempotent=True,
        side_effects=["artifacts/matrices/*.json"],
        handler=lambda **kw: doc.build_trace_matrix(**kw),
    ))
    reg.register(ToolSpec(
        name="verify_trace", family="doc", scope="doc",
        summary="对已构建矩阵执行全文微块匹配验证，产出三级着色（GREEN/BLUE/BLACK）",
        input_schema={"type": "object", "required": ["matrix_id"],
                      "properties": {"matrix_id": {"type": "string"}}},
        readonly=False, idempotent=True,
        side_effects=["artifacts/matrices/*.json"],
        requires_knowledge=True,
        handler=lambda matrix_id: doc.verify_trace(matrix_id),
    ))
    reg.register(ToolSpec(
        name="adjudicate_ambiguity", family="doc", scope="doc",
        summary="对存疑行发起受限 LLM 裁决（文档域唯一 LLM 点位；意见只作参考，不改判定分）",
        input_schema={"type": "object", "required": ["matrix_id"],
                      "properties": {"matrix_id": {"type": "string"}}},
        readonly=False, idempotent=False,
        side_effects=["artifacts/matrices/*.json", "cache/llm/*.json"],
        requires_llm=True, llm_permission=LLM_ADJUDICATE,
        handler=lambda matrix_id: doc.adjudicate_ambiguity(matrix_id),
    ))
    reg.register(ToolSpec(
        name="export_matrix_excel", family="doc", scope="doc",
        summary="导出处方式着色追溯矩阵 Excel（微块级逐字着色）",
        input_schema={"type": "object", "required": ["matrix_id"],
                      "properties": {"matrix_id": {"type": "string"},
                                     "output_path": {"type": "string"}}},
        readonly=False, idempotent=True, side_effects=["artifacts/exports/*.xlsx"],
        handler=lambda matrix_id, output_path=None: doc.export_matrix_excel(
            matrix_id, output_path),
    ))

    # ---------------- 图纸族（9，已有能力并入统一契约）----------------
    _d = [
        ("get_meta", "图纸集元信息（图集名、页数、知识快照版本）", {},
         lambda: diagram.get_meta()),
        ("list_pages", "列出已解析的图纸页号", {}, lambda: diagram.list_pages()),
        ("get_page", "取某页完整 IR（graph/edges/loops/port_level/validation 等）",
         {"required": ["page"], "properties": {"page": {"type": "integer"}}},
         lambda page: diagram.get_page(page)),
        ("search_node", "按关键词检索图纸节点（可选限定页）",
         {"required": ["query"], "properties": {"query": {"type": "string"},
                                                "page": {"type": "integer"}}},
         lambda query, page=None: diagram.search_node(query, page)),
        ("get_node", "按 gid 或标签取节点详情（含 kb 绑定与包围盒）",
         {"properties": {"gid": {"type": "string"}, "label": {"type": "string"},
                         "page": {"type": "integer"}}},
         lambda gid=None, label=None, page=None: diagram.get_node(gid, label, page)),
        ("trace_path", "上下游路径归因（含跨页拼接与证据）",
         {"required": ["node"], "properties": {
             "node": {"type": "string"},
             "direction": {"type": "string", "enum": ["up", "down"]},
             "depth": {"type": "integer"}}},
         lambda node, direction="down", depth=12: diagram.trace_path(node, direction, depth)),
        ("get_llm_view", "取给 LLM 读的受限语义视图（页面级或全局）",
         {"properties": {"page": {"type": "integer"}}},
         lambda page=None: diagram.get_llm_view(page)),
        ("query_knowledge", "查询知识层（符号字典/位号文法/IO/定值）",
         {"required": ["q"], "properties": {"q": {"type": "string"}}},
         lambda q: diagram.query_knowledge(q)),
        ("get_segments", "取锚点段 / 跨页段 / 依据图例（图纸问答的证据来源）",
         {"properties": {"kind": {"type": "string",
                                  "enum": ["all", "anchor", "cross_page", "legend"]}}},
         lambda kind="all": diagram.get_segments(kind)),
    ]
    for name, summary, sch, handler in _d:
        reg.register(ToolSpec(
            name=name, family="diagram", scope="diagram", summary=summary,
            input_schema={"type": "object", **sch},
            readonly=True, idempotent=True, requires_knowledge=True,
            llm_permission=LLM_NONE, handler=handler,
        ))

    # ---------------- 生成族（4，P3 已实现）----------------
    reg.register(ToolSpec(
        name="extract_logic_paths", family="generate", scope="generate",
        summary="从 IR 抽取可测单元与归一化逻辑路径（回路级/块级/阈值谓词级）",
        input_schema={"type": "object", "properties": {
            "page": {"type": "integer"},
            "unit_kind": {"type": "string",
                          "enum": ["loop", "block", "predicate"]}}},
        readonly=True, idempotent=True, requires_knowledge=True,
        handler=lambda page=None, unit_kind=None: cases.extract_logic_paths(page, unit_kind),
    ))
    reg.register(ToolSpec(
        name="match_component_library", family="generate", scope="generate",
        summary="按块类型与端口签名在部件用例库中分层检索测试模板（精确/同族/探针）",
        input_schema={"type": "object", "properties": {
            "unit_id": {"type": "string"}, "page": {"type": "integer"},
            "lib_version": {"type": "string"}}},
        readonly=True, idempotent=True, requires_knowledge=True,
        handler=lambda unit_id=None, page=None, lib_version=None: \
            cases.match_component_library(unit_id, page, lib_version),
    ))
    reg.register(ToolSpec(
        name="generate_test_case", family="generate", scope="generate",
        summary="生成测试用例：步骤由模板实例化、期望值由真值推演，LLM 仅写说明且过三层校验",
        input_schema={"type": "object", "properties": {
            "page": {"type": "integer"}, "unit_kind": {"type": "string"},
            "use_llm": {"type": "boolean"}}},
        readonly=False, idempotent=True, requires_llm=True,
        llm_permission=LLM_NARRATE, side_effects=["artifacts/cases/*.json"],
        handler=lambda page=None, unit_kind=None, use_llm=True: \
            cases.generate_test_case(page, unit_kind, use_llm),
    ))
    reg.register(ToolSpec(
        name="render_test_doc", family="generate", scope="generate",
        summary="把用例集渲染为交付文档（当前支持 xlsx；docx 显式降级并说明原因）",
        input_schema={"type": "object", "required": ["case_set_id"],
                      "properties": {"case_set_id": {"type": "string"},
                                     "fmt": {"type": "string", "enum": ["xlsx", "docx"]}}},
        readonly=False, idempotent=True, side_effects=["artifacts/exports/*"],
        handler=lambda case_set_id, fmt="xlsx": cases.render_test_doc(case_set_id, fmt),
    ))

    # ---------------- 图谱族（4；3 个已实现，xdiff_trace 属 P2）----------------
    reg.register(ToolSpec(
        name="utg_query", family="graph", scope="graph",
        summary="查询双域追溯图谱 UTG（按域/类型/关系/状态/置信度过滤）",
        input_schema={"type": "object", "properties": {
            "domain": {"type": "string", "enum": ["doc", "diagram", "test", "standard"]},
            "kind": {"type": "string"}, "relation": {"type": "string"},
            "status": {"type": "string"},
            "min_confidence": {"type": "number"}, "limit": {"type": "integer"}}},
        readonly=True, idempotent=True,
        handler=lambda domain=None, kind=None, relation=None, status=None,
               min_confidence=None, limit=50: graph.utg_query(
            domain, kind, relation, status, min_confidence, limit),
    ))
    reg.register(ToolSpec(
        name="utg_impact_analysis", family="graph", scope="graph",
        summary="变更影响分析：沿追溯链反向可达遍历，跨域列出受影响对象与人工复核清单",
        input_schema={"type": "object", "required": ["node_id"],
                      "properties": {"node_id": {"type": "string"},
                                     "change_kind": {"type": "string"}}},
        readonly=True, idempotent=True,
        handler=lambda node_id, change_kind="text_modify":             graph.utg_impact_analysis(node_id, change_kind),
    ))
    reg.register(ToolSpec(
        name="utg_coverage_report", family="graph", scope="graph",
        summary="覆盖审计：未追溯条目 / 无源用例 / 未确认桥接候选；可同时产出合规证据包",
        input_schema={"type": "object", "properties": {
            "project": {"type": "string"},
            "include_package": {"type": "boolean"}}},
        readonly=True, idempotent=True, requires_knowledge=True,
        handler=lambda project="", include_package=True:             graph.utg_coverage_report(project, include_package),
    ))
    reg.register(ToolSpec(
        name="xdiff_trace", family="graph", scope="graph",
        summary="SAMA/FD 设计图 ↔ DCS 组态功能图同构比对与差异分级定位",
        input_schema={"type": "object", "properties": {
            "design_ir": {"type": "string"}, "config_ir": {"type": "string"},
            "align_by": {"type": "string", "enum": ["system_code", "tag", "semantic"]}}},
        readonly=True, idempotent=True, implemented=False,
        handler=_placeholder("P2"),
    ))
    return reg


# ---------------------------------------------------------------------
# 对外 MCP 白名单：方案 §7.4「对外部智能体仅暴露只读工具，生成类工具不对外开放」
# ---------------------------------------------------------------------
EXTERNAL_READONLY_ALLOWLIST: tuple[str, ...] = (
    # 图纸族 9 个全部只读
    "get_meta", "list_pages", "get_page", "search_node", "get_node",
    "trace_path", "get_llm_view", "query_knowledge", "get_segments",
    # 文档族中纯计算、无写副作用者
    "parse_requirement_doc",
)


def external_tools(reg: ToolRegistry) -> list[dict[str, Any]]:
    """对外可见工具清单 = 白名单 ∩ 只读 ∩ 已实现。

    三重收窄，且**可程序校验**：不依赖"我们承诺只暴露只读工具"这种文字说明。
    """
    out = []
    for name in EXTERNAL_READONLY_ALLOWLIST:
        try:
            spec = reg.get(name)
        except KeyError:
            continue
        if spec.readonly and spec.implemented:
            out.append(spec.as_mcp_tool())
    return out
