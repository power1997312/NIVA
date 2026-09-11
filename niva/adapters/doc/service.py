# -*- coding: utf-8 -*-
"""文献域适配器（文档工具族 5 个工具）。

职责边界（绞杀者纪律）
----------------------
本文件**只做三件事**：参数转换、子进程调度、结果转统一内核对象。
legacy 的业务代码一行不改——所有真实计算都发生在 ``_runner.py`` 子进程里。

与方案 22 工具清单的对应
------------------------
    parse_requirement_doc   C1  文档解析与条目抽取
    build_trace_matrix      C2  四级需求链正/逆向矩阵构建
    verify_trace            C3  全文微块匹配验证
    adjudicate_ambiguity    C9  歧义裁决（唯一需要 LLM 的文档工具）
    export_matrix_excel     C11 着色 Excel 导出
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from ... import config as C
from ...kernel.ids import node_id_doc_req, edge_id, normalize_req_id
from ...kernel.model import (
    Evidence, Locator, TraceEdge, TraceGraph, TraceNode,
)
from ...kernel.registry import (
    ErrorCode, ToolResult, degraded, fail, ok,
)

RUNNER = Path(__file__).resolve().parent / "_runner.py"
DEFAULT_TIMEOUT = int(os.environ.get("NIVA_DOC_TIMEOUT", "3600"))

# 已知局限（必须显式声明，不得静默）
KNOWN_LIMITATION_LOCATOR = (
    "文档域 legacy 当前未对外暴露原文字符偏移，故本适配器产出的证据不含 "
    "char_start/char_end；证据暂不可点击定位。补齐定位器属 P2 任务"
    "（Trace_NL 内部已有 pos_map，需在适配器层surface出来）。"
)


class DocAdapter:
    """文献域工具实现。所有方法返回 ``ToolResult``。"""

    scope = "doc"

    def __init__(self, artifact_root: Optional[Path] = None,
                 timeout: int = DEFAULT_TIMEOUT) -> None:
        self.artifact_root = Path(artifact_root or C.ARTIFACTS_DIR)
        self.timeout = timeout
        self._tmp = self.artifact_root / "rpc" / "doc"
        self._tmp.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 子进程调度
    # ------------------------------------------------------------------
    def _run(self, op: str, args: dict[str, Any]) -> ToolResult:
        args_path = self._tmp / f"{op}_args.json"
        res_path = self._tmp / f"{op}_result.json"
        args_path.write_text(json.dumps(args, ensure_ascii=False), encoding="utf-8")
        if res_path.exists():
            try:
                res_path.unlink()
            except OSError:
                res_path.write_text("", encoding="utf-8")

        env = dict(os.environ)
        env["NIVA_ARTIFACT_ROOT"] = str(self.artifact_root)
        env["NIVA_LEGACY_ROOT"] = str(C.DOC_LEGACY)
        # legacy 自身的配置开关一律透传（阈值、设备、后端选择）
        env.setdefault("PYTHONIOENCODING", "utf-8")

        cmd = [sys.executable, str(RUNNER), op, str(args_path), str(res_path)]
        try:
            p = subprocess.run(cmd, cwd=str(C.DOC_LEGACY), env=env,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return fail(ErrorCode.TIMEOUT,
                        f"文献域操作 {op} 超时（{self.timeout}s）")
        except OSError as exc:
            return fail(ErrorCode.INTERNAL, f"无法启动子进程：{exc}")

        log_tail = (p.stdout or "")[-1500:] + (p.stderr or "")[-1500:]
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
            return fail(code, msg, warnings=[log_tail] if log_tail else [])

        data = payload["data"]
        # 「降级必可见」：未配 LLM 时 adjudicate 返回的是降级结果
        if isinstance(data, dict) and data.get("llm_enabled") is False:
            return degraded("LLM_NOT_CONFIGURED", data.get("degrade_reason", ""),
                            data=data)
        return ok(data, warnings=_collect_warnings(data, log_tail))

    # ------------------------------------------------------------------
    # 工具 1：文档解析
    # ------------------------------------------------------------------
    def parse_requirement_doc(self, pdf_path: str,
                              doc_type: Optional[str] = None) -> ToolResult:
        res = self._run("parse", {"pdf_path": pdf_path, "doc_type": doc_type})
        if not res.ok:
            return res
        d = res.data
        # 产出条目/章节节点（进图谱用）
        nodes = [self._req_node(d["doc_id"], it, "requirement_item")
                 for it in d.get("items", [])]
        nodes += [TraceNode(
            node_id=f"doc:{d['doc_id']}:sec:{s['section_no']}",
            domain="doc", kind="section", label=f"{s['section_no']} {s['title']}",
            payload=s, attrs={},
            locator=Locator(doc_id=d["doc_id"],
                            note="章节级定位（页码待 P2 补齐）"),
        ) for s in d.get("sections", []) if s.get("section_no")]
        res.data = {
            "doc_id": d["doc_id"], "doc_type": d["doc_type"],
            "pdf_path": d.get("pdf_path", pdf_path),
            "text_chars": d["text_chars"], "pages": d["pages"],
            "counts": {"items": len(d.get("items", [])),
                       "sections": len(d.get("sections", [])),
                       "content_map_entries": d.get("content_map_entries", 0)},
            "items_preview": d.get("items", [])[:20],
            "sections_preview": d.get("sections", [])[:20],
            "text_head": d.get("text_head", ""),
            "graph_nodes": [self._node_brief(n) for n in nodes],
            "locator_limitation": KNOWN_LIMITATION_LOCATOR,
        }
        return res

    # ------------------------------------------------------------------
    # 工具 2：构建追溯矩阵
    # ------------------------------------------------------------------
    def build_trace_matrix(self, *,
                           downstream_pdf: Optional[str] = None,
                           upstream_pdfs: Optional[dict[str, str]] = None,
                           design_pdfs: Optional[dict[str, str]] = None,
                           sys_req_pdf: Optional[str] = None,
                           mode: str = "table") -> ToolResult:
        kind = "design_chain" if design_pdfs else "req_chain"
        if kind == "req_chain":
            if not downstream_pdf or not upstream_pdfs:
                return fail(ErrorCode.SCHEMA_INVALID,
                            "req_chain 需要 downstream_pdf 与 upstream_pdfs")
            payload = {"downstream_pdf": downstream_pdf, "upstream_pdfs": upstream_pdfs}
        else:
            if not sys_req_pdf:
                return fail(ErrorCode.SCHEMA_INVALID,
                            "design_chain 需要 design_pdfs 与 sys_req_pdf")
            payload = {"design_pdfs": design_pdfs, "sys_req_pdf": sys_req_pdf}

        matrix_id = _matrix_id(kind, mode, payload)
        res = self._run("build", {**payload, "kind": kind, "mode": mode,
                                 "matrix_id": matrix_id})
        if not res.ok:
            return res
        res.data["confirmed_runnable_chains"] = [
            "build_trace_matrix → verify_trace → adjudicate_ambiguity → export_matrix_excel"
        ]
        return res

    # ------------------------------------------------------------------
    # 工具 3：验证
    # ------------------------------------------------------------------
    def verify_trace(self, matrix_id: str) -> ToolResult:
        res = self._run("verify", {"matrix_id": matrix_id})
        if res.ok:
            graph = self.load_graph(matrix_id)
            if graph is not None:
                res.data["graph_stats"] = graph.stats()
        return res

    # ------------------------------------------------------------------
    # 工具 4：歧义裁决（文档域唯一的 LLM 点位）
    # ------------------------------------------------------------------
    def adjudicate_ambiguity(self, matrix_id: str) -> ToolResult:
        res = self._run("adjudicate", {"matrix_id": matrix_id})
        if res.ok and isinstance(res.data, dict):
            # A3 公理：LLM 意见独立成字段，**永不回写 confidence**
            res.data["llm_opinions_are_advisory_only"] = True
            res.data["confidence_untouched_by_llm"] = True
        return res

    # ------------------------------------------------------------------
    # 工具 5：导出着色 Excel
    # ------------------------------------------------------------------
    def export_matrix_excel(self, matrix_id: str,
                            output_path: Optional[str] = None) -> ToolResult:
        return self._run("export", {"matrix_id": matrix_id,
                                    "output_path": output_path})

    # ==================================================================
    # 结果 → 统一内核（A1：一切皆追溯边）
    # ==================================================================
    def _req_node(self, doc_id: str, item: dict, kind: str) -> TraceNode:
        raw_id = item.get("item_id") or item.get("id") or ""
        nid = (node_id_doc_req(doc_id, raw_id) if raw_id
               else f"doc:{doc_id}:req:{abs(hash(item.get('content', '')))%10**10}")
        return TraceNode(
            node_id=nid, domain="doc", kind=kind,
            label=normalize_req_id(raw_id) or (item.get("content", "")[:32]),
            payload=item, attrs={},
            locator=Locator(doc_id=doc_id, note="条目内容已捕获；字符偏移待 P2"),
        )

    @staticmethod
    def _node_brief(n: TraceNode) -> dict[str, Any]:
        return {"node_id": n.node_id, "kind": n.kind, "label": n.label,
                "locator_located": bool(n.locator and n.locator.is_located())}

    def to_graph(self, matrix_payload: dict, run_id: str = "") -> TraceGraph:
        """把矩阵行转成文档域子图。

        行 → 一条 ``satisfies``/``realizes`` 追溯边；两侧内容取自上游/下游文档。
        这是"两侧不平衡"原则的体现：边只记关系与证据，内容留在节点里。
        """
        g = TraceGraph(meta={"domain": "doc", "matrix_id": matrix_payload.get("matrix_id"),
                             "run_id": run_id})
        relation = {"design_chain": "realizes"}.get(
            (matrix_payload.get("meta") or {}).get("kind", "req_chain"), "satisfies")

        for r in matrix_payload.get("rows", []):
            ds_doc = r.get("downstream_doc") or "DS"
            up_doc = r.get("upstream_doc") or "US"
            ds_raw = r.get("downstream_id") or f"seq{r.get('seq_number')}"
            up_raw = r.get("upstream_ref") or ""

            ds = self._row_node(ds_doc, ds_raw, r.get("downstream_content", ""), "下游")
            up = self._row_node(up_doc, up_raw, r.get("upstream_content", ""), "上游")
            g.nodes.setdefault(ds.node_id, ds)
            g.nodes.setdefault(up.node_id, up)

            conf = _confidence_of(r)
            status = _status_of(r)
            mr = r.get("match_result") or {}
            ev = Evidence(
                kind=_evidence_kind(r),
                method=f"legacy::{r.get('relation_source', 'table')}",
                score=float(r.get("candidate_score") or 0.0),
                locator=Locator(doc_id=ds_doc,
                                note="字符偏移待 P2 补齐（见 locator_limitation）"),
                raw_snippet=(r.get("evidence") or "")[:400],
                score_scale="legacy_fused[0,1]",
                channel=r.get("relation_source", "table"),
            )
            g.add_edge(TraceEdge(
                edge_id=edge_id(ds.node_id, up.node_id, relation),
                src=ds.node_id, dst=up.node_id, relation=relation,
                status=status, confidence=conf, source_mode=r.get("relation_source", "table"),
                evidence=[ev],
                llm_opinion=r.get("ai_opinion", ""),
                ambiguous=bool(r.get("ambiguous")),
                degrade_reason="" if mr else "未执行验证阶段，match_result 为空",
            ), strict=False)
        return g

    def _row_node(self, doc: str, raw_id: str, content: str, side: str) -> TraceNode:
        key = normalize_req_id(raw_id) if raw_id else f"{side}:{abs(hash(content))%10**9}"
        return TraceNode(
            node_id=f"doc:{doc}:req:{key}",
            domain="doc", kind="requirement_item",
            label=key or content[:32],
            payload={"raw_id": raw_id, "content": content, "side": side,
                     "doc": doc},
            attrs={},
            locator=Locator(doc_id=doc, note=f"{side}侧；字符偏移待 P2"),
        )

    def load_graph(self, matrix_id: str, run_id: str = "") -> Optional[TraceGraph]:
        p = self.artifact_root / "matrices" / f"{matrix_id}.json"
        if not p.exists():
            return None
        return self.to_graph(json.loads(p.read_text(encoding="utf-8")), run_id=run_id)


# ---------------------------------------------------------------------
def _collect_warnings(data: Any, log_tail: str) -> list[str]:
    warns: list[str] = []
    if isinstance(data, dict):
        warns += list(data.get("warnings") or [])
        warns.append(KNOWN_LIMITATION_LOCATOR)
    if "Traceback" in log_tail:
        warns.append("legacy 子进程日志含 Traceback（已按降级处理，详见返回封套）")
    return warns


def _matrix_id(kind: str, mode: str, payload: dict[str, Any]) -> str:
    """由输入确定性派生 matrix_id——同输入必同 id，天然幂等且便于复现。"""
    h = hashlib.sha256()
    h.update(f"{kind}|{mode}|".encode("utf-8"))
    h.update(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return f"{kind[:4]}-{h.hexdigest()[:12]}"


def _confidence_of(row: dict) -> float:
    mr = row.get("match_result") or {}
    cat = mr.get("overall_category")
    score = row.get("candidate_score")
    if cat == "exact":
        return min(1.0, max(float(score or 0.0), 0.95))
    if cat == "semantic":
        return min(0.94, max(float(score or 0.0), 0.70))
    if cat is None:
        return 0.0
    return float(score or 0.0)


def _status_of(row: dict) -> str:
    if row.get("ambiguous"):
        return "SUSPICIOUS"
    mr = row.get("match_result") or {}
    cat = mr.get("overall_category")
    if cat == "unmatched" or cat is None:
        return "UNTRACED"
    return "ACCEPTED"


def _evidence_kind(row: dict) -> str:
    src = row.get("relation_source") or "table"
    return {"table": "rule", "discover": "semantic", "hybrid": "semantic"}.get(src, "rule")
