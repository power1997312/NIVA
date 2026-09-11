# -*- coding: utf-8 -*-
"""图形域适配器 · 子进程执行器（不对外暴露，由 service.py 调用）。

复用 SAMA-V1 的 ``app_queries`` —— 它本就是"只读查询核"，直接读
``out/L3/*.json`` 与 ``out/llm/*.json`` 已生成产物，无副作用、有进程内缓存。
这正是方案里"图纸工具族 9 个（已有）"的真实来源，因此本适配器**零重写**。

支持的操作
    meta / list_pages / get_page / search_node / get_node /
    trace_path / get_llm_view / query_knowledge / get_segments
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

# 同 doc/_runner：脚本方式运行不会把 cwd 加入 sys.path
_LEGACY_ROOT = os.environ.get("NIVA_LEGACY_ROOT") or os.getcwd()
if _LEGACY_ROOT not in sys.path:
    sys.path.insert(0, _LEGACY_ROOT)


def _aq():
    """延迟导入 app_queries（导入会触发知识层装载，故不放在模块顶层）。"""
    import app_queries
    return app_queries


def op_meta(a: dict) -> dict:
    return _aq().meta() or {}


def op_list_pages(a: dict) -> dict:
    pages = _aq().page_list() or []
    return {"pages": pages, "count": len(pages)}


def op_get_page(a: dict) -> dict:
    aq = _aq()
    page = int(a["page"])
    full = aq.page_full(page)
    if full is None:
        raise FileNotFoundError(f"第 {page} 页不存在或尚未解析（请检查 out/L3/ir_page{page}.json）")
    return full


def op_search_node(a: dict) -> dict:
    aq = _aq()
    q = a["query"]
    page = a.get("page")
    hits = aq.search_nodes(q, int(page) if page is not None else None) or []
    return {"query": q, "page": page, "hits": hits, "count": len(hits)}


def op_get_node(a: dict) -> dict:
    aq = _aq()
    gid = a.get("gid")
    label = a.get("label")
    page = a.get("page")
    if gid:
        node = aq.find_node_by_gid(gid)
        return {"by": "gid", "gid": gid, "node": node,
                "found": node is not None}
    if label:
        nodes = aq.find_node_by_label(label, int(page) if page is not None else None) or []
        return {"by": "label", "label": label, "nodes": nodes,
                "count": len(nodes), "found": bool(nodes)}
    raise ValueError("get_node 需要 gid 或 label 之一")


def op_trace_path(a: dict) -> dict:
    aq = _aq()
    direction = a.get("direction", "down")
    if direction not in ("up", "down"):
        raise ValueError("direction 必须是 'up' 或 'down'")
    depth = int(a.get("depth", 12))
    return aq.trace_path(a["node"], direction=direction, max_depth=depth) or {}


def op_get_llm_view(a: dict) -> dict:
    aq = _aq()
    page = a.get("page")
    if page is None:
        v = aq.llm_view_all()
        return {"scope": "all", "view": v, "found": v is not None}
    v = aq.page_llm_view(int(page))
    return {"scope": f"page{int(page)}", "view": v, "found": v is not None}


def op_query_knowledge(a: dict) -> dict:
    return _aq().knowledge_search(a["q"]) or {}


def op_get_segments(a: dict) -> dict:
    aq = _aq()
    kind = a.get("kind", "all")
    out: dict = {"kind": kind}
    if kind in ("all", "anchor"):
        out["anchor_segments"] = aq.get_anchor_segments()
    if kind in ("all", "cross_page"):
        out["cross_page_segments"] = aq.get_cross_page_segments()
    if kind in ("all", "legend"):
        out["basis_legend"] = aq.get_basis_legend()
    return out


OPS = {
    "meta": op_meta, "list_pages": op_list_pages, "get_page": op_get_page,
    "search_node": op_search_node, "get_node": op_get_node,
    "trace_path": op_trace_path, "get_llm_view": op_get_llm_view,
    "query_knowledge": op_query_knowledge, "get_segments": op_get_segments,
}


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
            payload = {"ok": False, "error_code": "SCHEMA_INVALID", "error_msg": str(exc)}
        except Exception as exc:  # noqa: BLE001
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
