# -*- coding: utf-8 -*-
"""生成工具族（4 个工具）的服务层：extract / match / generate / render。

本层只做参数转换、制品仓读写与编排，真实计算在 testcase/ 四个模块里。
用例集以 JSON 落盘 ``artifacts/cases/<case_set_id>.json``，
``case_set_id`` 由输入确定性派生（同输入必同 id，天然幂等）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from ... import config as C
from ...adapters.diagram.service import DiagramAdapter
from ...kernel.registry import ErrorCode, ToolResult, degraded, fail, ok
from .generate import generate_cases
from .narrate import narrate_case
from .render_case import render_case_set
from .template_match import TemplateLibrary
from .truth_propagate import evaluate_unit
from .unit_extract import UnitExtractor

CASES_DIR = None  # 运行时由 artifact_root 决定


class TestCaseService:
    scope = "generate"

    def __init__(self, artifact_root: Optional[Path] = None,
                 diagram: Optional[DiagramAdapter] = None,
                 lib: Optional[TemplateLibrary] = None) -> None:
        self.artifact_root = Path(artifact_root or C.ARTIFACTS_DIR)
        self.diagram = diagram or DiagramAdapter()
        self.lib = lib or TemplateLibrary()
        self._cases_dir = self.artifact_root / "cases"
        self._cases_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def _ir_for(self, page: Optional[int]) -> tuple[dict, int]:
        if page is not None:
            ir = self.diagram.load_ir(int(page))
            if ir is None:
                raise FileNotFoundError(
                    f"第 {page} 页 IR 不存在（out/L3/ir_page{page}.json）")
            return ir, int(page)
        ir = self.diagram.load_ir_all()
        if ir is None:
            raise FileNotFoundError("缺少 ir_all.json（请先运行图纸解析管线）")
        return ir, 0

    def _iter_units(self, page: Optional[int], unit_kind: Optional[str]):
        ir, effective_page = self._ir_for(page)
        if page is None:
            for p in ir.get("pages", []):
                pno = int((p.get("meta") or {}).get("page") or 0)
                for u in UnitExtractor(p, page=pno).extract():
                    if unit_kind and u.kind != unit_kind:
                        continue
                    yield u
        else:
            for u in UnitExtractor(ir, page=effective_page).extract():
                if unit_kind and u.kind != unit_kind:
                    continue
                yield u

    # ------------------------------------------------------------------
    # 工具 1：抽取可测单元与逻辑路径
    # ------------------------------------------------------------------
    def extract_logic_paths(self, page: Optional[int] = None,
                            unit_kind: Optional[str] = None) -> ToolResult:
        units = list(self._iter_units(page, unit_kind))
        out = []
        for u in units:
            out.append({
                "unit_id": u.unit_id, "kind": u.kind, "page": u.page,
                "label": u.label, "center": u.center,
                "n_nodes": len(u.nodes), "n_edges": len(u.in_edges),
                "predicates": len(u.predicates),
                "logic_classes": sorted({(u.sem.get(n) or {}).get("cls")
                                         for n in u.logic_nodes} - {None}),
                "outputs": u.outputs[:4],
                "warnings": u.warnings[:4],
            })
        return ok({"page": page, "units": out, "count": len(out),
                   "by_kind": _countby(out, "kind")})

    # ------------------------------------------------------------------
    # 工具 2：模板检索
    # ------------------------------------------------------------------
    def match_component_library(self, unit_id: Optional[str] = None,
                                page: Optional[int] = None,
                                lib_version: Optional[str] = None) -> ToolResult:
        hits, misses = [], []
        for u in self._iter_units(page, None):
            if unit_id and u.unit_id != unit_id:
                continue
            m = self.lib.match_unit(u)
            row = {"unit_id": u.unit_id, "cls": m.cls, "center": m.center,
                   "match_kind": m.match_kind, "score": m.score,
                   "template_case_id": (m.template or {}).get("case_id"),
                   "breakdown": m.breakdown, "veto_reasons": m.veto_reasons,
                   "warnings": m.warnings}
            (hits if m.matched else misses).append(row)
        return ok({"page": page, "hits": hits, "misses": misses,
                   "counts": {"hits": len(hits), "misses": len(misses),
                              "exact": sum(1 for h in hits if h["match_kind"] == "exact"),
                              "family": sum(1 for h in hits if h["match_kind"] == "family"),
                              "probe": sum(1 for h in hits if h["match_kind"] == "probe")},
                   "lib_cases": len(self.lib.cases)})

    # ------------------------------------------------------------------
    # 工具 3：生成用例
    # ------------------------------------------------------------------
    def generate_test_case(self, page: Optional[int] = None,
                           unit_kind: Optional[str] = None,
                           use_llm: bool = True) -> ToolResult:
        cs = generate_cases(self._ir_for(page)[0], page=page,
                            use_llm=use_llm, lib=self.lib,
                            only_kinds={unit_kind} if unit_kind else None)
        case_set_id = _case_set_id(page, unit_kind, use_llm)
        payload = {"case_set_id": case_set_id, "meta": cs.meta,
                   "stats": cs.stats, "warnings": cs.warnings,
                   "cases": cs.cases, "skipped": cs.skipped}
        p = self._cases_dir / f"{case_set_id}.json"
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                     encoding="utf-8")
        res = ok({"case_set_id": case_set_id, "path": str(p),
                  "stats": cs.stats, "warnings": cs.warnings,
                  "cases_preview": [_brief(c) for c in cs.cases[:10]],
                  "skipped_preview": cs.skipped[:10]})
        # ★ 降级语义必须区分两类，否则熔断器会被噪声淹没：
        #   能力降级（degraded=True）—— LLM 被要求但不可用，成文回退模板；
        #   数据质量标记（warnings）—— 定值未绑定(R14)/探针兜底，逐用例显式可见，
        #   但**不是**能力降级。若混为一谈，知识库占位期间 PB3 永远触发熔断。
        llm_fallback = bool(use_llm) and int(cs.stats.get("template_fallback") or 0) > 0
        if llm_fallback:
            return degraded("DEGRADED",
                            f"{cs.stats['template_fallback']} 条用例的 LLM 成文未通过校验，"
                            "已回退模板文案（期望值不受影响）",
                            data={**res.data, "llm_fallback_cases":
                                  cs.stats["template_fallback"]})
        return res

    # ------------------------------------------------------------------
    # 工具 4：渲染交付文档
    # ------------------------------------------------------------------
    def render_test_doc(self, case_set_id: str, fmt: str = "xlsx") -> ToolResult:
        p = self._cases_dir / f"{case_set_id}.json"
        if not p.exists():
            return fail(ErrorCode.NOT_FOUND, f"用例集不存在：{p}（请先 generate_test_case）")
        case_set = json.loads(p.read_text(encoding="utf-8"))
        out = self.artifact_root / "exports" / f"{case_set_id}.{fmt}"
        res = render_case_set(case_set, out, fmt=fmt)
        if res.ok:
            res.data["case_set_id"] = case_set_id
            res.data["cases"] = len(case_set.get("cases") or [])
        return res


# ---------------------------------------------------------------------
def _brief(c: dict) -> dict:
    return {"case_id": c.get("case_id"), "label": c.get("label"),
            "kind": c.get("kind"),
            "template": (c.get("template") or {}).get("case_id"),
            "match": (c.get("template") or {}).get("match_kind"),
            "probe": c.get("probe"),
            "narrative_source": c.get("narrative_source"),
            "steps": len(c.get("steps") or []),
            "warnings": len(c.get("warnings") or [])}


def _countby(rows: list[dict], key: str) -> dict:
    d: dict[str, int] = {}
    for r in rows:
        d[str(r.get(key))] = d.get(str(r.get(key)), 0) + 1
    return d


def _case_set_id(page: Optional[int], unit_kind: Optional[str], use_llm: bool) -> str:
    h = hashlib.sha256(
        f"{page}|{unit_kind}|{use_llm}".encode("utf-8")).hexdigest()[:12]
    tag = f"p{page}" if page is not None else "all"
    return f"cases-{tag}-{unit_kind or 'any'}-{h[:6]}"
