# -*- coding: utf-8 -*-
"""用例生成编排（TCG 四步流水线的串联）。

    ① unit_extract → ② template_match → ③ truth_propagate → ④ narrate

红线：步骤与期望值**只**来自 ③；④ 只写说明且须过三层校验，失败即回退模板。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ...kernel.model import Evidence, Locator
from .narrate import narrate_case
from .template_match import TemplateLibrary
from .truth_propagate import evaluate_unit
from .unit_extract import UnitExtractor


@dataclass
class CaseSet:
    cases: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


def _evidence_for(unit, nid: str, relation: str = "verifies") -> dict:
    n = unit.nodes.get(nid) or {}
    bbox = n.get("bbox")
    return {
        "kind": "geometry", "method": f"ir::{relation}", "score": 1.0,
        "locator": {"doc_id": f"p{unit.page}", "page": unit.page,
                    "bbox": list(bbox) if bbox else None,
                    "node_id": nid,
                    "note": "图纸坐标可点击定位"},
        "raw_snippet": (n.get("text") or n.get("label") or "")[:200],
        "score_scale": "boolean", "channel": "testcase",
    }


def build_case(unit, tpl, truth, seq: int, use_llm: bool = True) -> dict:
    """把一个可推演单元装配成完整用例 dict。"""
    nar = narrate_case(unit, tpl, truth, use_llm=use_llm)
    t = tpl.template or {}
    probe = bool(t.get("_probe"))
    evidence = [_evidence_for(unit, nid) for nid in
                ([unit.center] + list(unit.predicates))[:6]
                if nid in unit.nodes]
    return {
        "case_id": f"TC-P{unit.page}-{seq:03d}",
        "unit_id": unit.unit_id,
        "page": unit.page,
        "kind": unit.kind,
        "label": unit.label,
        "center": unit.center,
        "template": {"case_id": t.get("case_id"), "file": t.get("_file"),
                     "match_kind": tpl.match_kind, "score": tpl.score,
                     "breakdown": tpl.breakdown,
                     "veto_reasons": tpl.veto_reasons},
        "matched": tpl.matched and not probe,
        "probe": probe,
        "principle": nar.purpose,
        "preconditions": nar.preconditions,
        "steps": nar.steps,
        "acceptance_criteria": nar.acceptance_criteria,
        "expected_values": [s.get("expected") for s in truth.expected],
        "stimuli": truth.stimuli,
        "eval_trace": truth.traces,
        "coverage": t.get("coverage") or [],
        "standard_ref": t.get("standard_ref"),
        "evidence": evidence,
        "narrative_source": nar.source,
        "narrative_validation": nar.validation,
        "truth_method": truth.method,
        "truth_coverage": truth.coverage,
        "variables": truth.variables,
        "warnings": list(dict.fromkeys(list(unit.warnings) + list(truth.warnings)
                                       + list(nar.warnings) + list(tpl.warnings))),
    }


def generate_cases(ir: dict, page: int = 0, *,
                   use_llm: bool = True,
                   lib: Optional[TemplateLibrary] = None,
                   only_kinds: Optional[set[str]] = None) -> CaseSet:
    """对一个 IR（单页或全集页）跑完四步流水线。"""
    lib = lib or TemplateLibrary()
    units = UnitExtractor(ir, page=page).extract()
    cs = CaseSet(meta={"page": page, "units": len(units)})

    seq = 0
    for u in units:
        if only_kinds and u.kind not in only_kinds:
            continue
        # ① 已由 UnitExtractor 完成；此处判定可推演性
        if not (u.logic_nodes or u.predicates):
            cs.skipped.append({"unit_id": u.unit_id, "reason": "无逻辑块也无阈值块"})
            continue
        # ② 模板检索
        tpl = lib.match_unit(u)
        if not tpl.matched:
            cs.skipped.append({"unit_id": u.unit_id,
                               "reason": "无可用模板：" + "; ".join(tpl.warnings)[:120],
                               "cls": tpl.cls, "veto": tpl.veto_reasons})
            continue
        # ③ 真值推演
        from .truth_propagate import evaluate_unit
        truth = evaluate_unit(u)
        if not truth.evaluable:
            cs.skipped.append({"unit_id": u.unit_id,
                               "reason": "真值推演拒评：" + "; ".join(truth.warnings)[:160]})
            continue
        # ④ 语义成文
        seq += 1
        cs.cases.append(build_case(u, tpl, truth, seq, use_llm=use_llm))

    cs.stats = {
        "units": len(units),
        "cases": len(cs.cases),
        "skipped": len(cs.skipped),
        "probe_cases": sum(1 for c in cs.cases if c["probe"]),
        "family_matched": sum(1 for c in cs.cases
                              if c["template"]["match_kind"] == "family"),
        "exact_matched": sum(1 for c in cs.cases
                             if c["template"]["match_kind"] == "exact"),
        "llm_narrated": sum(1 for c in cs.cases if c["narrative_source"] == "llm"),
        "template_fallback": sum(1 for c in cs.cases
                                 if c["narrative_source"] == "template_fallback"),
        "with_missing_setpoint": sum(
            1 for c in cs.cases
            if any(v.get("setpoint") is None for v in c["variables"])),
    }
    if cs.stats["with_missing_setpoint"]:
        cs.warnings.append(
            f"{cs.stats['with_missing_setpoint']} 条用例含未绑定定值的阈值块（R14）："
            "判据为定性越限，待真实定值接入后须补数值判据")
    if cs.stats["probe_cases"]:
        cs.warnings.append(f"{cs.stats['probe_cases']} 条为通用探针兜底用例，"
                           "须由领域专家完善后方可作为正式验证依据")
    return cs
