# -*- coding: utf-8 -*-
"""图形域适配器（图纸工具族 9 个工具）。

这 9 个工具在 SAMA-V1 里**已经存在**（`mcp_server.py` 原生 stdio MCP），
本适配器做的是把它们**并入 NIVA 的统一工具契约与统一错误码**，
而不是重写。真实查询逻辑仍在 ``app_queries.py``（只读、有进程内缓存）。

与方案 22 工具清单的对应
------------------------
    get_meta / list_pages / get_page / search_node / get_node /
    trace_path / get_llm_view / query_knowledge / get_segments

LLM 权限：本作用域策略为 ``none`` —— 全部为纯符号查询，不经大模型。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from ... import config as C
from ...kernel.ids import (
    node_id_diagram_block, node_id_diagram_loop, node_id_diagram_page, edge_id,
)
from ...kernel.model import Evidence, Locator, TraceEdge, TraceGraph, TraceNode
from ...kernel.registry import ErrorCode, ToolResult, fail, ok

RUNNER = Path(__file__).resolve().parent / "_runner.py"
DEFAULT_TIMEOUT = int(os.environ.get("NIVA_DIAGRAM_TIMEOUT", "600"))

IR_DIR = C.DIAGRAM_LEGACY / "out" / "L3"


class DiagramAdapter:
    """图形域工具实现。所有方法返回 ``ToolResult``。"""

    scope = "diagram"

    def __init__(self, artifact_root: Optional[Path] = None,
                 timeout: int = DEFAULT_TIMEOUT) -> None:
        self.artifact_root = Path(artifact_root or C.ARTIFACTS_DIR)
        self.timeout = timeout
        self._tmp = self.artifact_root / "rpc" / "diagram"
        self._tmp.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def _run(self, op: str, args: dict[str, Any]) -> ToolResult:
        args_path = self._tmp / f"{op}_args.json"
        res_path = self._tmp / f"{op}_result.json"
        args_path.write_text(json.dumps(args, ensure_ascii=False), encoding="utf-8")

        env = dict(os.environ)
        env["NIVA_LEGACY_ROOT"] = str(C.DIAGRAM_LEGACY)
        env["NIVA_ARTIFACT_ROOT"] = str(self.artifact_root)
        env.setdefault("PYTHONIOENCODING", "utf-8")

        cmd = [sys.executable, str(RUNNER), op, str(args_path), str(res_path)]
        try:
            p = subprocess.run(cmd, cwd=str(C.DIAGRAM_LEGACY), env=env,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return fail(ErrorCode.TIMEOUT, f"图形域操作 {op} 超时（{self.timeout}s）")
        except OSError as exc:
            return fail(ErrorCode.INTERNAL, f"无法启动子进程：{exc}")

        log_tail = (p.stdout or "")[-1200:] + (p.stderr or "")[-1200:]
        if not res_path.exists():
            return fail(ErrorCode.INTERNAL,
                        f"{op} 未产出结果（exit={p.returncode}）。日志尾部：{log_tail}")
        try:
            payload = json.loads(res_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return fail(ErrorCode.INTERNAL, f"结果 JSON 解析失败：{exc}")

        if not payload.get("ok"):
            code = payload.get("error_code", ErrorCode.INTERNAL)
            msg = payload.get("error_msg", "")
            if code == ErrorCode.INTERNAL and payload.get("traceback"):
                msg = f"{msg}\n{payload['traceback'][-600:]}"
            return fail(code, msg)

        data = payload["data"]
        warns: list[str] = []
        if isinstance(data, dict) and data.get("found") is False:
            warns.append("目标产物不存在（可能尚未解析该页，或 out/ 为空）")
            return fail(ErrorCode.NOT_FOUND, f"{op} 未找到目标产物", warnings=warns)
        return ok(data, warnings=warns)

    # ------------------------------------------------------------------
    # 9 个工具
    # ------------------------------------------------------------------
    def get_meta(self) -> ToolResult:
        return self._run("meta", {})

    def list_pages(self) -> ToolResult:
        return self._run("list_pages", {})

    def get_page(self, page: int) -> ToolResult:
        return self._run("get_page", {"page": int(page)})

    def search_node(self, query: str, page: Optional[int] = None) -> ToolResult:
        return self._run("search_node", {"query": query, "page": page})

    def get_node(self, gid: Optional[str] = None, label: Optional[str] = None,
                 page: Optional[int] = None) -> ToolResult:
        if not gid and not label:
            return fail(ErrorCode.SCHEMA_INVALID, "get_node 需要 gid 或 label 之一")
        return self._run("get_node", {"gid": gid, "label": label, "page": page})

    def trace_path(self, node: str, direction: str = "down",
                   depth: int = 12) -> ToolResult:
        return self._run("trace_path", {"node": node, "direction": direction,
                                        "depth": depth})

    def get_llm_view(self, page: Optional[int] = None) -> ToolResult:
        return self._run("get_llm_view", {"page": page})

    def query_knowledge(self, q: str) -> ToolResult:
        return self._run("query_knowledge", {"q": q})

    def get_segments(self, kind: str = "all") -> ToolResult:
        return self._run("get_segments", {"kind": kind})

    # ==================================================================
    # IR → 统一内核
    # ==================================================================
    def load_ir(self, page: int) -> Optional[dict]:
        p = IR_DIR / f"ir_page{int(page)}.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def load_ir_all(self) -> Optional[dict]:
        p = IR_DIR / "ir_all.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def to_graph(self, ir: dict, doc_id: str = "SAMA") -> TraceGraph:
        """把单页 IR 转成图形域子图。

        设计纪律 3 的体现：``payload`` **原样保留** SAMA-IR 的 node / loop / edge，
        内核只负责关系与定位，不重塑域内结构。
        """
        g = TraceGraph(meta={"domain": "diagram", "doc_id": doc_id,
                            "page": (ir.get("meta") or {}).get("page")})
        page = int((ir.get("meta") or {}).get("page") or 0)
        tb = (ir.get("meta") or {}).get("titleblock") or {}

        page_node = TraceNode(
            node_id=node_id_diagram_page(doc_id, page), domain="diagram",
            kind="page", label=str(tb.get("图名") or f"第 {page} 页"),
            payload={"titleblock": tb, "grid": (ir.get("meta") or {}).get("grid")},
            attrs={"system": tb.get("系统号"), "zone_classes": [
                z.get("text") for z in (ir.get("zones") or {}).get("class_labels") or []]},
            locator=Locator(doc_id=doc_id, page=page, note="页级定位"),
        )
        g.nodes[page_node.node_id] = page_node

        # 端点索引：IR 的 graph.edges 用**名称/标签**引用端点（如 'IM#1' → 'R'），
        # 而 graph.nodes 的 id 是 'n017' 形态。必须同时按 id 与 label 建索引，
        # 否则所有信号流边都会因"端点找不到"而被静默丢弃。
        local: dict[str, str] = {}
        for n in (ir.get("graph") or {}).get("nodes") or []:
            tag = (n.get("text") or n.get("label") or "").strip()
            nid = node_id_diagram_block(doc_id, page, n.get("id") or tag or "?")
            for key in filter(None, (n.get("id"), n.get("text"), n.get("label"), tag)):
                local.setdefault(str(key).strip(), nid)
            bbox = n.get("bbox")
            node = TraceNode(
                node_id=nid, domain="diagram",
                kind="func_block" if n.get("cls") != "bubble" else "signal_point",
                label=tag or n.get("id", ""),
                payload=n,                              # ← 原样保留
                attrs={"cls": n.get("cls"), "conf": n.get("conf"),
                       "tags": [tag] if tag else [],
                       "kb": n.get("kb") or {}},
                locator=Locator(doc_id=doc_id, page=page,
                                bbox=tuple(bbox) if bbox else None,
                                node_id=n.get("id")),
            )
            g.nodes[nid] = node
            g.add_edge(TraceEdge(
                edge_id=edge_id(nid, page_node.node_id, "contains"),
                src=nid, dst=page_node.node_id, relation="contains",
                status="CONFIRMED", confidence=1.0, source_mode="rule",
                evidence=[Evidence(
                    kind="geometry", method="ir_page_membership", score=1.0,
                    locator=node.locator, raw_snippet=node.label,
                    score_scale="boolean", channel="structure")],
            ), strict=False)

        for e in (ir.get("graph") or {}).get("edges") or []:
            s = local.get(str(e.get("src", "")).strip())
            d = local.get(str(e.get("dst", "")).strip())
            if not s or not d or s == d:
                continue
            basis = e.get("basis") or "unknown"
            conf = _basis_confidence(basis)
            g.add_edge(TraceEdge(
                edge_id=edge_id(s, d, "flows_to"),
                src=s, dst=d, relation="flows_to",
                status="CONFIRMED" if conf >= 0.8 else "CANDIDATE",
                confidence=conf, source_mode=f"diagram:{basis}",
                evidence=[Evidence(
                    kind="geometry", method=f"basis::{basis}", score=conf,
                    locator=g.nodes[d].locator, raw_snippet=f"{g.nodes[s].label}→{g.nodes[d].label}",
                    score_scale="basis_rank_normalized", channel=basis)],
            ), strict=False)

        for lp in ir.get("loops") or []:
            lid = lp.get("loop_id")
            if not lid:
                continue
            lnid = node_id_diagram_loop(doc_id, lid)
            g.nodes[lnid] = TraceNode(
                node_id=lnid, domain="diagram", kind="loop",
                label=f"{lid}（{lp.get('type', '?')}）",
                payload=lp, attrs={"type": lp.get("type"),
                                   "members": lp.get("members") or [],
                                   "domain_analog_bool": _domain_mix(lp)},
                locator=Locator(doc_id=doc_id, page=page, node_id=lid,
                                note="回路级定位（包围盒由成员节点并集推导，P2 补）"),
            )
            for m in lp.get("members") or []:
                mnid = local.get(m)
                if not mnid:
                    continue
                g.add_edge(TraceEdge(
                    edge_id=edge_id(mnid, lnid, "contains"),
                    src=mnid, dst=lnid, relation="contains",
                    status="CONFIRMED", confidence=1.0, source_mode="rule",
                    evidence=[Evidence(
                        kind="geometry", method="loop_membership", score=1.0,
                        locator=g.nodes[mnid].locator,
                        raw_snippet=f"{lid} ∋ {g.nodes[mnid].label}",
                        score_scale="boolean", channel="loop")],
                ), strict=False)
        return g


# ---------------------------------------------------------------------
def _basis_confidence(basis: str) -> float:
    """依据强度 → 置信度。直接读 ``knowledge/thresholds.yml`` 的 diagram.basis_rank，
    不在代码里硬编码（架构方案 §4 契约要点第三条）。"""
    rank = C.th("diagram.basis_rank", {}) or {}
    r = rank.get(basis)
    if r is None:
        r = C.th("diagram.default_basis_rank", 2)
    return round(min(1.0, max(0.0, float(r) / 3.0)), 4)


def _domain_mix(loop: dict) -> str:
    members = loop.get("members") or []
    return "MIXED" if len(members) > 2 else "BOOLEAN"
