# -*- coding: utf-8 -*-
"""语义成文（TCG 第④步）—— LLM 在本系统中的合法点位之一。

**红线（A3 公理）**：步骤与期望值由算法产生（真值推演 + 模板槽位），
LLM 只负责把结构化结果翻译成人类可读的说明。三层校验任一失败即**整体回退**
到模板文案 —— 模板文案永远可用，保证引擎不空转。

三层校验（架构方案 §5.2.5）
    ① 结构校验        输出须含 purpose/preconditions/steps/acceptance_criteria
    ② 期望值一致性     LLM 的第 i 步期望必须逐字等于算法第 i 步期望（不得增删步骤）
    ③ 实体存在性       LLM 提到的位号必须存在于材料包（不得臆造）
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ...agent.llm_provider import provider as llm_provider
from ...services.testcase.template_match import TemplateMatch
from ...services.testcase.truth_propagate import TruthResult
from ...services.testcase.unit_extract import Unit

TAG_LIKE = re.compile(r"[A-Za-z]{1,6}\d{2,4}[A-Za-z]{0,4}")

SYSTEM_PROMPT = (
    "你是核电仪控测试设计助手。你的唯一职责是把给定的结构化测试数据改写为"
    "人类可读的测试用例说明。\n"
    "硬约束：\n"
    "  1. 不得修改任何期望值；不得增删步骤；步骤顺序不得改变。\n"
    "  2. 只能使用材料包中出现的位号与设备名，禁止臆造。\n"
    "  3. 期望值必须逐字保留算法给出的结果。\n"
    "  4. 不确定时输出 {\"fallback\": true}。"
)

OUTPUT_SCHEMA_DESC = (
    '{"purpose": str, "preconditions": str, '
    '"steps": [{"no": int, "action": str, "expected": str}], '
    '"acceptance_criteria": str}'
)


@dataclass
class Narrative:
    source: str                 # llm | template_fallback
    purpose: str = ""
    preconditions: str = ""
    steps: list[dict] = field(default_factory=list)
    acceptance_criteria: str = ""
    warnings: list[str] = field(default_factory=list)
    validation: dict = field(default_factory=dict)


# =====================================================================
def _algo_steps(unit: Unit, tpl: TemplateMatch, truth: TruthResult) -> list[dict]:
    """算法步骤：由真值推演的 激励→期望 直接给出。这是唯一的步骤来源。"""
    label_of = {nid: (n.get("text") or n.get("label") or nid)
                for nid, n in unit.nodes.items()}
    var_label = {v["var_id"]: (v.get("label") or v["var_id"]) for v in truth.variables}
    steps: list[dict] = []
    for i, st in enumerate(truth.steps, start=1):
        stim = st.get("stimulus") or {}
        exp = st.get("expected") or {}
        action_parts = []
        for var_id, bit in stim.items():
            v = next((x for x in truth.variables if x["var_id"] == var_id), {})
            sp = v.get("setpoint")
            direction = "升至定值以上" if bit else "置于定值以下"
            if v.get("cls") == "LO_MONITOR" or str(v.get("block", "")).startswith("LO"):
                direction = "降至定值以下" if bit else "置于定值以上"
            target = f"{var_label.get(var_id, var_id)} 的输入"
            if sp is None:
                target += "（定值待补）"
            action_parts.append(f"将{target}{direction}")
        exp_txt = "、".join(
            f"{label_of.get(oid, oid)} = {int(bit)}" for oid, bit in exp.items())
        steps.append({
            "no": i,
            "action": "；".join(action_parts) if action_parts else "保持当前工况",
            "expected": exp_txt,
            "_stimulus": stim, "_expected_raw": exp,
        })
    return steps


def _template_narrative(unit: Unit, tpl: TemplateMatch,
                        algo: list[dict], truth: TruthResult) -> Narrative:
    t = tpl.template or {}
    principle = str(t.get("principle") or "")
    missing_sp = [v for v in truth.variables if v.get("setpoint") is None]
    pre = "无闭锁、无旁通；确认块处于正常工作工况。"
    if missing_sp:
        pre += (" 注意：" + "、".join(v.get("label", "") for v in missing_sp)
                + " 的定值未在定值库中命中（R14），本用例以定性越限为判据，"
                  "待真实定值接入后须补齐数值判据。")
    steps = [{"no": s["no"], "action": s["action"], "expected": s["expected"]}
             for s in algo]
    cov = "、".join(t.get("coverage") or [])
    acc = (f"逐步骤实测输出与期望一致；覆盖方法：{cov or '等价类'}；"
           f"依据：{t.get('standard_ref') or 'IEC 60880 §6.3'}。"
           f"期望值来源：真值推演（确定性求值），非人工估计。")
    return Narrative(source="template_fallback", purpose=principle,
                     preconditions=pre, steps=steps, acceptance_criteria=acc,
                     validation={"schema_ok": True, "expected_consistent": True,
                                 "entities_ok": True, "reason": "模板文案（未经 LLM）"})


# =====================================================================
def narrate_case(unit: Unit, tpl: TemplateMatch, truth: TruthResult,
                 use_llm: bool = True) -> Narrative:
    algo = _algo_steps(unit, tpl, truth)
    fallback = _template_narrative(unit, tpl, algo, truth)
    if not use_llm:
        fallback.validation["reason"] = "未启用 LLM（use_llm=False）"
        return fallback

    llm = llm_provider()
    if not llm.is_available:
        fallback.validation["reason"] = f"LLM 不可用：{llm.describe().get('has_key')=}"
        return fallback

    # ---- 材料包（只给结构化、去噪的派生视图）----
    allowed = set()
    for nid, n in unit.nodes.items():
        for v in (n.get("text"), n.get("label")):
            if v:
                allowed.add(str(v).strip())
    for v in truth.variables:
        if v.get("label"):
            allowed.add(v["label"])
    material = {
        "unit": {"id": unit.unit_id, "kind": unit.kind, "label": unit.label,
                 "safety_class": unit.safety_class},
        "blocks": [{"id": nid, "label": _lbl(n), "cls": (unit.sem.get(nid) or {}).get("cls")}
                   for nid, n in list(unit.nodes.items())[:24]],
        "predicates": [{"label": v.get("label"), "cls": v.get("cls"),
                        "setpoint": v.get("setpoint"), "unit": v.get("unit"),
                        "setpoint_status": v.get("setpoint_status")}
                       for v in truth.variables],
        "algorithmic_steps": [{"no": s["no"], "action": s["action"],
                               "expected": s["expected"]} for s in algo],
        "template": {"principle": (tpl.template or {}).get("principle", ""),
                     "standard_ref": (tpl.template or {}).get("standard_ref", ""),
                     "coverage": (tpl.template or {}).get("coverage", [])},
    }
    prompt = (
        "材料包（JSON）：\n" + json.dumps(material, ensure_ascii=False, indent=1)
        + "\n\n任务：为该测试用例撰写说明。输出 JSON：\n" + OUTPUT_SCHEMA_DESC
        + "\n\n步骤数量必须恰好为 "
        + str(len(algo)) + "，每步 expected 必须与 algorithmic_steps[i].expected 逐字一致。"
    )

    r = llm.chat(prompt, system=SYSTEM_PROMPT, temperature=0.1, json_mode=True)
    if not r.ok:
        fallback.validation["reason"] = f"LLM 调用失败：{r.error_code} {r.degrade_reason}"
        fallback.warnings.append("已回退模板文案；LLM 响应已留痕（prompt_sha=%s）"
                                 % r.prompt_sha)
        return fallback
    data = r.data or {}
    if data.get("fallback"):
        fallback.validation["reason"] = "模型自报不确定（fallback=true）"
        return fallback

    # ---- 三层校验 ----
    v: dict[str, Any] = {"schema_ok": False, "expected_consistent": False,
                         "entities_ok": False, "reason": ""}
    need = ("purpose", "preconditions", "steps", "acceptance_criteria")
    v["schema_ok"] = all(k in data for k in need) and isinstance(data.get("steps"), list)
    if not v["schema_ok"]:
        v["reason"] = "结构校验失败：缺少必要字段或 steps 非列表"
        return _with_validation(fallback, v)

    if len(data["steps"]) != len(algo):
        v["reason"] = f"步骤数不一致：LLM {len(data['steps'])} vs 算法 {len(algo)}"
        return _with_validation(fallback, v)

    mism = []
    for i, (ls, as_) in enumerate(zip(data["steps"], algo), start=1):
        if _norm(ls.get("expected")) != _norm(as_["expected"]):
            mism.append(f"第{i}步：LLM={ls.get('expected')!r} 算法={as_['expected']!r}")
    v["expected_consistent"] = not mism
    if mism:
        v["reason"] = "期望值一致性校验失败：" + "; ".join(mism[:3])
        return _with_validation(fallback, v)

    blob = json.dumps(data, ensure_ascii=False)
    tags = {t for t in TAG_LIKE.findall(blob)}
    unknown = sorted(t for t in tags
                     if not any(t.lower() == a.lower() or t.lower() in a.lower()
                                for a in allowed))
    v["entities_ok"] = not unknown
    if unknown:
        v["reason"] = f"实体存在性校验失败：臆造位号 {unknown[:5]}"
        return _with_validation(fallback, v)

    return Narrative(
        source="llm",
        purpose=str(data.get("purpose", "")),
        preconditions=str(data.get("preconditions", "")),
        steps=[{"no": int(s.get("no", i + 1)), "action": str(s.get("action", "")),
                "expected": str(s.get("expected", ""))}
               for i, s in enumerate(data["steps"], start=1)],
        acceptance_criteria=str(data.get("acceptance_criteria", "")),
        warnings=["LLM 成文已通过三层校验；期望值仍以算法结果为准"],
        validation={**v, "llm_cached": r.cached, "llm_model": r.model},
    )


def _with_validation(n: Narrative, v: dict) -> Narrative:
    n.validation = {**v, "fell_back": True}
    n.warnings.append("LLM 输出未通过校验，已整体回退模板文案（期望值不受影响）")
    return n


def _norm(s: Any) -> str:
    return re.sub(r"[\s，,；;。.]+", "", str(s or "")).lower()


def _lbl(n: dict) -> str:
    return (n.get("text") or n.get("label") or "")
