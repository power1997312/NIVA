# -*- coding: utf-8 -*-
"""文献域适配器 · 子进程执行器（不对外暴露，由 service.py 调用）。

为什么用子进程而不是同进程 import
----------------------------------
1. **同名模块隔离**：Trace_NL 与 SAMA-V1 都有顶层 `config.py`，且 legacy 内部
   一律绝对导入（`import config` / `from core import ...`）。同进程无论如何
   挂 `sys.path` 都存在遮蔽风险；子进程 + `cwd=<legacy 根>` 是**零风险**做法，
   也正是 legacy 自己被设计成"在工程根目录下跑脚本"的原生形态。
2. **故障隔离**：legacy 崩溃/内存爆掉不会拖垮智能体进程，天然满足
   "能力可失效、降级必可见"。
3. **可复现性**：每次调用都是干净解释器，不残留跨调用状态。

调用契约
--------
    python _runner.py <op> <args_json_path> <result_json_path>
结果写入 ``result_json_path``，stdout 只留给 legacy 的日志（由调用方收集）。

支持的操作（与 service.py 的五个文档工具一一对应）
    parse       parse_requirement_doc
    build       build_trace_matrix
    verify      verify_trace
    adjudicate  adjudicate_ambiguity
    export      export_matrix_excel
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
import traceback
from pathlib import Path

# 以脚本方式运行本文件时，Python 只把"脚本所在目录"放进 sys.path，
# 不会加入 cwd —— 而 legacy 的模块（config / core / pdfparser）在工程根目录下。
# 因此必须显式把 legacy 根挂到 sys.path 首位。
_LEGACY_ROOT = os.environ.get("NIVA_LEGACY_ROOT") or os.getcwd()
if _LEGACY_ROOT not in sys.path:
    sys.path.insert(0, _LEGACY_ROOT)

# 制品仓根：由 service 通过环境变量注入，默认落在 NIVA 的 artifacts/
ARTIFACT_ROOT = Path(os.environ.get("NIVA_ARTIFACT_ROOT") or "artifacts")
MATRIX_DIR = ARTIFACT_ROOT / "matrices"
EXPORT_DIR = ARTIFACT_ROOT / "exports"


# =====================================================================
# 序列化辅助
# =====================================================================
def _ser_match(mr) -> dict | None:
    """MatchResult → JSON（TextRun.category 是 Enum，取 .value）。"""
    if mr is None:
        return None
    return {
        "downstream_runs": [{"text": r.text, "category": r.category.value}
                            for r in mr.downstream_runs],
        "upstream_runs": [{"text": r.text, "category": r.category.value}
                          for r in mr.upstream_runs],
        "overall_category": mr.overall_category.value,
        "downstream_text": mr.downstream_text,
        "upstream_text": mr.upstream_text,
    }


def _de_match(d: dict | None):
    """JSON → MatchResult（重建 Enum 与 TextRun）。"""
    if not d:
        return None
    from config import MatchCategory
    from core.text_matcher import MatchResult, TextRun
    return MatchResult(
        downstream_runs=[TextRun(text=r["text"], category=MatchCategory(r["category"]))
                         for r in d["downstream_runs"]],
        upstream_runs=[TextRun(text=r["text"], category=MatchCategory(r["category"]))
                       for r in d["upstream_runs"]],
        overall_category=MatchCategory(d["overall_category"]),
        downstream_text=d["downstream_text"],
        upstream_text=d["upstream_text"],
    )


def _row_to_dict(row) -> dict:
    return {
        "seq_number": row.seq_number,
        "downstream_id": row.downstream_id,
        "downstream_content": row.downstream_content,
        "upstream_doc": row.upstream_doc,
        "upstream_ref": row.upstream_ref,
        "upstream_content": row.upstream_content,
        "downstream_doc": row.downstream_doc,
        "relation_source": row.relation_source,
        "candidate_score": row.candidate_score,
        "evidence": row.evidence,
        "ambiguous": row.ambiguous,
        "ai_opinion": row.ai_opinion,
        "discovery_candidates": row.discovery_candidates,
        "match_result": _ser_match(row.match_result),
    }


def _dict_to_row(d: dict):
    from core.traceability_matrix import TraceabilityRow
    return TraceabilityRow(
        seq_number=d["seq_number"],
        downstream_id=d["downstream_id"],
        downstream_content=d["downstream_content"],
        upstream_doc=d["upstream_doc"],
        upstream_ref=d["upstream_ref"],
        upstream_content=d["upstream_content"],
        downstream_doc=d.get("downstream_doc", ""),
        match_result=_de_match(d.get("match_result")),
        relation_source=d.get("relation_source", "table"),
        candidate_score=d.get("candidate_score", 0.0),
        evidence=d.get("evidence", ""),
        ambiguous=d.get("ambiguous", False),
        ai_opinion=d.get("ai_opinion", ""),
        discovery_candidates=d.get("discovery_candidates") or [],
    )


def _load_matrix(matrix_id: str):
    from core.traceability_matrix import TraceabilityMatrix
    p = MATRIX_DIR / f"{matrix_id}.json"
    if not p.exists():
        raise FileNotFoundError(f"矩阵不存在：{p}（请先调用 build_trace_matrix）")
    d = json.loads(p.read_text(encoding="utf-8"))
    m = TraceabilityMatrix()
    m.rows = [_dict_to_row(r) for r in d["rows"]]
    m.backward_merge_groups = {int(k): v for k, v in (d.get("backward_merge_groups") or {}).items()}
    return m, d


def _save_matrix(matrix_id: str, matrix, meta: dict) -> Path:
    MATRIX_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "matrix_id": matrix_id,
        "meta": meta,
        "backward_merge_groups": matrix.backward_merge_groups,
        "rows": [_row_to_dict(r) for r in matrix.rows],
    }
    p = MATRIX_DIR / f"{matrix_id}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _stats(matrix) -> dict:
    """行级与着色级统计。着色级复用 Excel 的口径（字符级三色占比）。"""
    from collections import Counter
    by_cat = Counter()
    char_by_cat = Counter()
    ambiguous = 0
    for r in matrix.rows:
        if r.ambiguous:
            ambiguous += 1
        mr = r.match_result
        if mr is None:
            by_cat["UNMATCHED"] += 1
            continue
        by_cat[mr.overall_category.name] += 1
        for run in mr.downstream_runs:
            char_by_cat[run.category.name] += len(run.text)
    total_chars = sum(char_by_cat.values()) or 1
    return {
        "rows": len(matrix.rows),
        "ambiguous_rows": ambiguous,
        "rows_by_category": dict(by_cat),
        "downstream_chars_by_category": dict(char_by_cat),
        "downstream_char_ratio": {k: round(v / total_chars, 4)
                                  for k, v in char_by_cat.items()},
    }


# =====================================================================
# 各操作
# =====================================================================
def op_parse(a: dict) -> dict:
    from core.pdf_parser_adapter import extract_body_text, extract_full_text
    from core.requirement_extractor import (
        build_content_map, detect_document_type, extract_requirement_items,
        extract_sections)

    pdf = a["pdf_path"]
    if not Path(pdf).exists():
        raise FileNotFoundError(f"文档不存在：{pdf}")

    text = extract_full_text(pdf)
    pages = extract_body_text(pdf)
    doc_type = a.get("doc_type") or detect_document_type(text)
    sections = extract_sections(text)
    items = extract_requirement_items(text)
    cmap = build_content_map(text, doc_name=Path(pdf).stem)

    warnings: list[str] = []
    if not text.strip():
        warnings.append("文本层为空：可能是扫描件，需 OCR（当前不支持）")
    if not items:
        warnings.append("未抽到条目型需求：该文档可能是章节型，追溯将降级为章节级")

    return {
        "doc_id": Path(pdf).stem,
        "pdf_path": str(pdf),
        "doc_type": doc_type,
        "text_chars": len(text),
        "pages": len(pages),
        "sections": [{"section_no": s.section_number, "title": s.section_title,
                      "content_chars": len(s.content or "")}
                     for s in sections],
        "items": [{"item_id": i.item_id, "raw_id": i.raw_id,
                   "page_number": i.page_number,
                   "content": (i.content or "")[:400]}
                  for i in items],
        "content_map_entries": len(getattr(cmap, "_all_keys", []) or []),
        "warnings": warnings,
        "text_head": text[:300],
    }


def op_build(a: dict) -> dict:
    from core.traceability_matrix import (
        build_backward_matrix, build_backward_matrix_from_design)

    mode = a.get("mode", "table")
    kind = a.get("kind", "req_chain")
    if kind == "design_chain":
        m = build_backward_matrix_from_design(
            design_pdfs=a["design_pdfs"], sys_req_pdf=a["sys_req_pdf"], mode=mode)
        inputs = {"design_pdfs": a["design_pdfs"], "sys_req_pdf": a["sys_req_pdf"]}
    else:
        m = build_backward_matrix(
            downstream_pdf=a["downstream_pdf"],
            upstream_pdfs=a["upstream_pdfs"], mode=mode)
        inputs = {"downstream_pdf": a["downstream_pdf"],
                  "upstream_pdfs": a["upstream_pdfs"]}

    matrix_id = a["matrix_id"]
    _save_matrix(matrix_id, m, {"mode": mode, "kind": kind, "inputs": inputs,
                               "verified": False, "adjudicated": False})
    return {"matrix_id": matrix_id, "kind": kind, "mode": mode,
            "stats": _stats(m), "inputs": inputs}


def op_verify(a: dict) -> dict:
    from core.text_matcher import verify_matrix
    matrix_id = a["matrix_id"]
    m, meta = _load_matrix(matrix_id)
    if not m.rows:
        return {"matrix_id": matrix_id, "stats": _stats(m),
                "warning": "矩阵为空，无需验证"}
    verify_matrix(m)
    meta["verified"] = True
    _save_matrix(matrix_id, m, meta)
    return {"matrix_id": matrix_id, "stats": _stats(m)}


def op_adjudicate(a: dict) -> dict:
    from config import LLM_ENABLED
    matrix_id = a["matrix_id"]
    m, meta = _load_matrix(matrix_id)
    if not LLM_ENABLED:
        return {"matrix_id": matrix_id, "llm_enabled": False,
                "stats": _stats(m),
                "degrade_reason": "未配置 TRACE_NL_LLM_API_KEY，已跳过 AI 裁决；"
                                  "存疑行保持原状态待人工确认"}
    from core.llm_adjudicator import adjudicate_matrix
    result = adjudicate_matrix(m)
    meta["adjudicated"] = True
    _save_matrix(matrix_id, m, meta)
    opinions = [{"seq": r.seq_number, "downstream_id": r.downstream_id,
                 "ambiguous": r.ambiguous, "ai_opinion": r.ai_opinion}
                for r in m.rows if r.ai_opinion]
    return {"matrix_id": matrix_id, "llm_enabled": True,
            "opinions": opinions, "opinion_count": len(opinions),
            "raw": (result if isinstance(result, (dict, list)) else None),
            "stats": _stats(m)}


def op_export(a: dict) -> dict:
    from core.excel_generator import generate_excel
    matrix_id = a["matrix_id"]
    m, meta = _load_matrix(matrix_id)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(a.get("output_path") or (EXPORT_DIR / f"{matrix_id}.xlsx"))
    out.parent.mkdir(parents=True, exist_ok=True)
    generate_excel(m, str(out))
    return {"matrix_id": matrix_id, "xlsx_path": str(out),
            "bytes": out.stat().st_size if out.exists() else 0,
            "sheets_source": sorted({r.downstream_doc or "" for r in m.rows}),
            "stats": _stats(m)}


OPS = {"parse": op_parse, "build": op_build, "verify": op_verify,
       "adjudicate": op_adjudicate, "export": op_export}


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: _runner.py <op> <args_json_path> [result_json_path]", file=sys.stderr)
        return 2
    op = sys.argv[1]
    args = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    result_path = Path(sys.argv[3]) if len(sys.argv) > 3 else None

    if op not in OPS:
        payload = {"ok": False, "error_code": "UNKNOWN_OP", "error_msg": f"未知操作 {op}"}
    else:
        try:
            payload = {"ok": True, "data": OPS[op](args)}
        except FileNotFoundError as exc:
            payload = {"ok": False, "error_code": "NOT_FOUND", "error_msg": str(exc)}
        except ValueError as exc:
            payload = {"ok": False, "error_code": "PARSE_FAILED", "error_msg": str(exc)}
        except Exception as exc:  # noqa: BLE001 — 兜底，绝不静默
            payload = {"ok": False, "error_code": "INTERNAL",
                       "error_msg": f"{type(exc).__name__}: {exc}",
                       "traceback": traceback.format_exc()[-2000:]}

    blob = json.dumps(payload, ensure_ascii=False, default=str)
    if result_path:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(blob, encoding="utf-8")
        print(f"__NIVA_RESULT__ {result_path}")
    else:
        print("__NIVA_RESULT__ " + blob)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
