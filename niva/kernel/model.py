# -*- coding: utf-8 -*-
"""NIVA 统一内核数据模型（**已冻结** — P0 交付物）。

三条架构公理在此落地（架构方案 §0）：
    A1 一切皆追溯边   → 只有一个关系类型 ``TraceEdge``，没有第二套异构结构。
    A2 证据必须可定位 → ``Evidence`` 强制携带 ``Locator``，"凭什么/在哪里"可回答。
    A3 判定链必须确定性 → ``confidence`` 只能由确定性算法写入；``llm_opinion``
                        独立成字段，**永不回写 confidence**。

设计纪律（§2.3）：
    1. 不平衡两侧   —— 边只记 src/dst 与关系类型，内容永远属于节点。
    2. 证据不平移   —— ``Evidence.score`` 存原始分，归一化只在消费时做。
                       （Trace_NL 实测：锚点原始分 0.03~0.13 但判别力极强，
                        存储层一旦归一化，绝对量级信息永久丢失。）
    3. 域内结构不迁移 —— ``TraceNode.payload`` 原样保留 SAMA-IR node /
                        Trace_NL RequirementItem，内核不重塑内容。

因此本模块**只依赖标准库**，可被两个适配器、服务层、编排层、MCP 服务共同引用。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Literal, Optional

__all__ = [
    "Locator", "Evidence", "TraceNode", "TraceEdge", "TraceGraph",
    "Domain", "Relation", "EdgeStatus", "EvidenceKind",
]

# ---------------------------------------------------------------------
# 受控词表（Literal 而非 Enum：便于直接 JSON 序列化与前端消费）
# ---------------------------------------------------------------------
Domain = Literal["doc", "diagram", "test", "standard"]
"""节点所属域。doc=文献域, diagram=图形域, test=用例域, standard=标准条款域。"""

Relation = Literal[
    "satisfies",    # 系统需求 → 用户需求
    "realizes",     # 系统设计 → 系统需求
    "implements",   # 图纸回路 → 设计条目（跨域桥接）
    "refines",      # 细化（架构伞关系）
    "verifies",     # 测试用例 → 图纸回路
    "derives",      # 用例 → 用例（补充派生）
    "conflicts",    # 显式冲突（分级互斥等）
    "refs",         # 引用（图号/跨页）
    "flows_to",     # 图形域：信号流向（P1 新增，SAMA-IR 有向边）
    "contains",     # 图形域：页面/回路对其成员的包含（P1 新增）
]
"""追溯关系类型。图谱内部一切关系都收敛到这张表。

P1 扩展说明：``flows_to`` 与 ``contains`` 是图形域上线时新增的成员。
这是**向后兼容的受控词表扩展**（只增不改），不构成对内核对结构的破坏；
但新增成员必须在此登记，以维持"一切关系都收敛到一张表"这一 A1 公理。"""

EdgeStatus = Literal[
    "CONFIRMED",    # 已确认（人工签认或高置信自动确认）
    "ACCEPTED",     # 算法接受
    "SUSPICIOUS",   # 存疑（落在模糊区间）
    "CANDIDATE",    # 候选（未过验证层，仅供审查）
    "UNTRACED",     # 未追溯到
    "QUARANTINED",  # 隔离区（解析异常项，不混入正常结果）
]

EvidenceKind = Literal[
    "semantic",   # 嵌入召回
    "lexical",    # 字符级相似/Jaccard/LCS
    "nli",        # 蕴含精判
    "anchor",     # 锚点（章节号/条目号/ID 命中）
    "tfidf",      # 稀有词/词频
    "geometry",   # 图形几何（端点规则/方向裁决/母线）
    "tag",        # 位号重合
    "rule",       # 规则校验命中
    "truth",      # 真值推演
    "llm",        # 大模型裁决意见（只作参考，不作判据）
]


# =====================================================================
# 定位器：A2 公理的载体
# =====================================================================
@dataclass
class Locator:
    """统一定位器。支撑"点关系 → 两侧同时高亮"这一演示核心能力。

    文本域用 char_start/char_end（UTF-16 或 Python 字符偏移，见 doc_id 的口径约定）；
    图形域用 bbox（pt，PDF 用户空间坐标）+ node_id/port。
    两侧字段可同时为空（表示"该证据不可定位"），但**不允许静默为空**——
    调用方须显式构造，避免"看起来有证据、其实点不开"。
    """
    doc_id: str
    page: Optional[int] = None            # 1-based 页码；文本与图形域通用
    char_start: Optional[int] = None      # 文本域：原文起偏移
    char_end: Optional[int] = None        # 文本域：原文止偏移（不含）
    bbox: Optional[tuple[float, float, float, float]] = None   # 图形域：x0,y0,x1,y1 (pt)
    node_id: Optional[str] = None         # 图形域：SAMA-IR node id
    port: Optional[str] = None            # 图形域：端口名
    note: str = ""                        # 定位口径说明（如 "页码依据 PDF 物理页"）

    def is_textual(self) -> bool:
        return self.char_start is not None and self.char_end is not None

    def is_graphical(self) -> bool:
        return self.bbox is not None or self.node_id is not None

    def is_located(self) -> bool:
        """是否真正可定位（可点击跳转）。"""
        return self.is_textual() or self.is_graphical()


# =====================================================================
# 证据
# =====================================================================
@dataclass
class Evidence:
    """一条证据。回答"凭什么"（method/score）与"在哪里"（locator）。"""
    kind: EvidenceKind
    method: str                  # 具体算法，如 "asym_sentence_agg" / "endpoint_rule_1"
    score: float                 # **原始分**，不平移、不归一，保留绝对量级
    locator: Locator
    raw_snippet: str = ""        # 命中证据的原文片段（人类可读、可引用回验）
    score_scale: str = ""        # 原始分的量纲说明，如 "cosine[0,1]" / "anchor_raw"
    note: str = ""               # 如 "~" 近似命中标记
    channel: str = ""            # 融合前的通道名（sem/contain/idf/tfidf/...）

    def __post_init__(self) -> None:
        if self.score is None:
            raise ValueError("Evidence.score 为必填：证据必须带分，禁止空证据")


# =====================================================================
# 节点
# =====================================================================
@dataclass
class TraceNode:
    """统一节点。``payload`` 原样保留域内结构（设计纪律 3）。"""
    node_id: str                 # 全局唯一，见 kernel/ids.py
    domain: Domain
    kind: str                    # requirement_item|section|func_block|wire|loop|chain|case|clause
    label: str                   # 人类可读标题
    payload: dict[str, Any] = field(default_factory=dict)   # 域特有原始结构
    attrs: dict[str, Any] = field(default_factory=dict)     # 统一属性
    locator: Optional[Locator] = None

    # attrs 的约定键（不强制，但两域适配器应尽量填充，供桥接与用例引擎使用）：
    #   tags            : list[str]  位号（原样，未归一）
    #   safety_class    : list[str]  ["F-SC1", ...]
    #   domain_analog_bool : str     "ANALOG"|"BOOLEAN"|"MIXED"
    #   params          : dict       块参数，如 {"k":2,"n":3} 或 {"hi":8.5}
    #   stateful        : bool
    #   cls             : str        符号语义类（SAMA-IR 的 34 类之一）

    def __repr__(self) -> str:  # 便于调试，避免 payload 刷屏
        return f"<TraceNode {self.node_id} {self.kind} '{self.label[:24]}'>"


# =====================================================================
# 边
# =====================================================================
@dataclass
class TraceEdge:
    """统一追溯边。A1 公理的载体。"""
    edge_id: str
    src: str                     # 下游节点 node_id
    dst: str                     # 上游节点 node_id
    relation: Relation
    status: EdgeStatus
    confidence: float            # 0–1。**只由确定性算法写入**（A3）
    source_mode: str             # table|discover|hybrid|bridge|rule|truth|llm_confirmed
    evidence: list[Evidence] = field(default_factory=list)

    # --- 留痕字段：不参与判定 ---
    llm_opinion: str = ""        # LLM 裁决意见，**永不回写 confidence**
    llm_confidence: Optional[float] = None
    llm_cached: bool = False
    llm_hallucination: bool = False   # 引用回验未命中 → 判为幻觉，强制转人工

    # --- 人机协同字段 ---
    human_confirmed: bool = False
    reviewer: str = ""
    reviewed_at: str = ""
    ambiguous: bool = False
    degrade_reason: str = ""     # 降级事实必须显式可见（"降级必可见"语义）

    def __post_init__(self) -> None:
        if not self.evidence:
            # 允许构造中间态，但必须在进入图谱前补齐。
            # 图谱侧 GraphStore.add_edge(strict=True) 会硬拒绝无证据边。
            pass
        if not (0.0 <= float(self.confidence) <= 1.0):
            raise ValueError(f"confidence 必须落在 [0,1]，实际 {self.confidence}")

    @property
    def has_evidence(self) -> bool:
        return len(self.evidence) > 0

    def evidence_verifiable(self) -> bool:
        """是否所有证据都可定位（缺一定位器即视为不可复核）。"""
        return bool(self.evidence) and all(e.locator.is_located() for e in self.evidence)


# =====================================================================
# 图谱
# =====================================================================
@dataclass
class TraceGraph:
    """统一追溯图。系统唯一的持久化记忆（§4.7）。"""
    nodes: dict[str, TraceNode] = field(default_factory=dict)
    edges: dict[str, TraceEdge] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    # ---------------- 增删 ----------------
    def add_node(self, node: TraceNode) -> TraceNode:
        old = self.nodes.get(node.node_id)
        if old is not None and old != node:
            raise ValueError(
                f"节点 ID 冲突且内容不同：{node.node_id}。"
                "统一 ID 命名空间要求 node_id 全局唯一（见 kernel/ids.py）。")
        self.nodes[node.node_id] = node
        return node

    def add_edge(self, edge: TraceEdge, strict: bool = True) -> TraceEdge:
        """写入一条边。

        ``strict=True``（默认）时执行两条守门校验：
          - 两端节点必须已存在于图谱（禁止悬空边）；
          - 必须携带至少一条证据（"成对调用固化"：候选发现→证据验证，
            缺证据验证记录的追溯边在守门处被拒绝输出）。
        """
        if strict:
            missing = [n for n in (edge.src, edge.dst) if n not in self.nodes]
            if missing:
                raise ValueError(f"悬空边 {edge.edge_id}：节点不存在 {missing}")
            if not edge.has_evidence:
                raise ValueError(
                    f"无证据边 {edge.edge_id} 被拒绝（A2：证据必须可定位）。"
                    "如确需占位，请显式传入 strict=False。")
        self.edges[edge.edge_id] = edge
        return edge

    # ---------------- 查询 ----------------
    def out_edges(self, node_id: str, relation: Optional[str] = None) -> list[TraceEdge]:
        return [e for e in self.edges.values()
                if e.dst == node_id and (relation is None or e.relation == relation)]

    def in_edges(self, node_id: str, relation: Optional[str] = None) -> list[TraceEdge]:
        return [e for e in self.edges.values()
                if e.src == node_id and (relation is None or e.relation == relation)]

    def downstream_of(self, node_id: str, depth: int = 1) -> set[str]:
        """沿 src 方向（下游）可达节点集合。变更影响分析的反向遍历基础。"""
        seen: set[str] = set()
        frontier = {node_id}
        for _ in range(max(0, depth)):
            nxt: set[str] = set()
            for n in frontier:
                for e in self.out_edges(n):
                    if e.src not in seen:
                        nxt.add(e.src)
            seen |= nxt
            frontier = nxt
            if not frontier:
                break
        return seen

    def upstream_of(self, node_id: str, depth: int = 1) -> set[str]:
        seen: set[str] = set()
        frontier = {node_id}
        for _ in range(max(0, depth)):
            nxt: set[str] = set()
            for n in frontier:
                for e in self.in_edges(n):
                    if e.dst not in seen:
                        nxt.add(e.dst)
            seen |= nxt
            frontier = nxt
            if not frontier:
                break
        return seen

    def stats(self) -> dict[str, Any]:
        by_rel: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for e in self.edges.values():
            by_rel[e.relation] = by_rel.get(e.relation, 0) + 1
            by_status[e.status] = by_status.get(e.status, 0) + 1
        by_domain: dict[str, int] = {}
        for n in self.nodes.values():
            by_domain[n.domain] = by_domain.get(n.domain, 0) + 1
        no_evidence = sum(1 for e in self.edges.values() if not e.has_evidence)
        unlocatable = sum(1 for e in self.edges.values() if not e.evidence_verifiable())
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "nodes_by_domain": by_domain,
            "edges_by_relation": by_rel,
            "edges_by_status": by_status,
            "edges_without_evidence": no_evidence,
            "edges_with_unlocatable_evidence": unlocatable,
        }

    # ---------------- 序列化 ----------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "meta": self.meta,
            "nodes": {k: asdict(v) for k, v in self.nodes.items()},
            "edges": {k: asdict(v) for k, v in self.edges.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TraceGraph":
        g = cls(meta=dict(d.get("meta") or {}))
        for k, nd in (d.get("nodes") or {}).items():
            loc = nd.get("locator")
            if isinstance(loc, dict) and loc.get("bbox") is not None:
                loc["bbox"] = tuple(loc["bbox"])
            g.nodes[k] = TraceNode(
                node_id=nd["node_id"], domain=nd["domain"], kind=nd["kind"],
                label=nd.get("label", ""), payload=nd.get("payload") or {},
                attrs=nd.get("attrs") or {},
                locator=Locator(**loc) if loc else None,
            )
        for k, ed in (d.get("edges") or {}).items():
            evs = []
            for e in (ed.get("evidence") or []):
                loc = e.get("locator") or {}
                if loc.get("bbox") is not None:
                    loc["bbox"] = tuple(loc["bbox"])
                evs.append(Evidence(
                    kind=e["kind"], method=e.get("method", ""), score=e["score"],
                    locator=Locator(**loc), raw_snippet=e.get("raw_snippet", ""),
                    score_scale=e.get("score_scale", ""), note=e.get("note", ""),
                    channel=e.get("channel", ""),
                ))
            g.edges[k] = TraceEdge(
                edge_id=ed["edge_id"], src=ed["src"], dst=ed["dst"],
                relation=ed["relation"], status=ed["status"],
                confidence=ed["confidence"], source_mode=ed.get("source_mode", ""),
                evidence=evs, llm_opinion=ed.get("llm_opinion", ""),
                llm_confidence=ed.get("llm_confidence"),
                llm_cached=ed.get("llm_cached", False),
                llm_hallucination=ed.get("llm_hallucination", False),
                human_confirmed=ed.get("human_confirmed", False),
                reviewer=ed.get("reviewer", ""), reviewed_at=ed.get("reviewed_at", ""),
                ambiguous=ed.get("ambiguous", False),
                degrade_reason=ed.get("degrade_reason", ""),
            )
        return g

    def fingerprint(self, ignore_meta_keys: Iterable[str] = ("run_at", "elapsed_s")) -> str:
        """结构指纹：用于"可复现性"验收（同输入两次运行须完全一致）。

        刻意剔除时间类 meta 字段，只对节点/边/证据的**内容**做哈希。
        """
        payload = self.to_dict()
        meta = {k: v for k, v in (payload.get("meta") or {}).items()
                if k not in set(ignore_meta_keys)}
        blob = json.dumps(
            {"meta": meta, "nodes": payload["nodes"], "edges": payload["edges"]},
            ensure_ascii=False, sort_keys=True, default=str,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
