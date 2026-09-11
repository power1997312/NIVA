# -*- coding: utf-8 -*-
"""Planner：任务理解（意图分类 + 参数抽取）与任务分解（剧本查表优先）。

两级机制（架构方案 §4.3 / docx §4.3）：
    1. **剧本查表优先** —— 命中即直接返回固定 DAG，**零模型规划**，
       行为可预期、可复现、可审计。
    2. **模型规划兜底** —— 仅当意图无法匹配时才走 LLM 规划（ReAct），
       且必须显式标注"非标准路径"。

意图分类同样规则优先：强短语/关键词打分即可覆盖六类标准意图；
只有规则打分并列难分时才考虑 LLM，且 LLM 只做"选意图"，不做参数编造。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..agent.llm_provider import provider as llm_provider
from ..agent.playbook import Playbook, PlaybookLibrary, extract_placeholders
from ..kernel.registry import ToolRegistry

# 六类标准意图（docx §4.3）
INTENTS = ("req_trace", "diagram_qa", "xdiff", "testcase_gen",
           "impact_analysis", "coverage_audit")

# 参数抽取：从自然语言中抓常见工程路径与页号
# 注意：字符类含 & 但不含空格 —— 含空格的路径（如 "V&V Agents"）无法从句中
# 完整抽出，须用 --param 显式传入。这是**已知局限**，不影响正确性（不猜测）。
RE_PDF = re.compile(r"[\w\u4e00-\u9fff\-/\\:.&]+\.(?:pdf|PDF)")
RE_PAGE = re.compile(r"(?:第\s*)?(\d{1,2})\s*页")
RE_NODE = re.compile(r"\b([A-Z]{1,6}\d{2,4}[A-Z]{0,4})\b")


@dataclass
class Plan:
    intent: str
    source: str                 # playbook | react_fallback | clarify
    playbook_id: Optional[str] = None
    playbook_name: str = ""
    available: bool = False
    confidence: float = 0.0
    params: dict = field(default_factory=dict)
    missing_params: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    reason: str = ""
    standard_path: bool = True  # False = 非标准路径（ReAct），必须显著标注

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "steps"} | {
            "steps": [{"id": s.id, "tool": s.tool, "scope": s.scope,
                       "gate": s.gate, "optional": s.optional}
                      for s in self.steps]}


class Planner:
    def __init__(self, library: Optional[PlaybookLibrary] = None,
                 registry: Optional[ToolRegistry] = None) -> None:
        self.lib = library or PlaybookLibrary()
        self.registry = registry
        self.lib.refresh_availability(registry)

    # ------------------------------------------------------------------
    # 参数抽取（规则优先，绝不编造）
    # ------------------------------------------------------------------
    def extract_params(self, text: str, pb: Playbook) -> dict:
        got: dict[str, Any] = {}
        pdfs = RE_PDF.findall(text or "")
        reqs = pb.required_params()
        if "downstream_pdf" in reqs and pdfs:
            got["downstream_pdf"] = pdfs[0]
            if len(pdfs) > 1:
                got["_candidate_upstreams"] = pdfs[1:]
        if "page" in reqs:
            m = RE_PAGE.search(text or "")
            if m:
                got["page"] = int(m.group(1))
        for p in reqs:
            if p in got:
                continue
            if p in ("node",):
                m = RE_NODE.search(text or "")
                if m:
                    got[p] = m.group(1)
            elif p in ("direction",):
                if re.search(r"上游|向上", text or ""):
                    got[p] = "up"
                elif re.search(r"下游|向下", text or ""):
                    got[p] = "down"
            elif p == "use_llm":
                got[p] = True
            elif p == "depth":
                got[p] = 12
        # 可选参数的默认值
        for k, v in (pb.params.get("optional") or {}).items():
            got.setdefault(k, v)
        return got

    # ------------------------------------------------------------------
    def plan(self, text: str, force_intent: Optional[str] = None) -> Plan:
        """任务理解 + 任务分解。永不抛异常；分不清就返回 clarify。"""
        t = str(text or "")
        pb, score = (None, 0.0)
        if force_intent:
            pb = self.lib.by_intent(force_intent)
            score = 99.0
        else:
            pb, score = self.lib.best_match(t)

        if pb is None:
            return self._react_fallback(t)

        params = self.extract_params(t, pb)
        missing = [p for p in pb.required_params() if p not in params]
        if missing:
            return Plan(
                intent=pb.intent, source="clarify", playbook_id=pb.id,
                playbook_name=pb.name, available=pb.available,
                confidence=min(1.0, score / 10.0), params=params,
                missing_params=missing,
                reason="参数缺失，主动反问确认；不做猜测性执行",
                steps=pb.steps)

        if not pb.available:
            return Plan(
                intent=pb.intent, source="playbook", playbook_id=pb.id,
                playbook_name=pb.name, available=False,
                confidence=min(1.0, score / 10.0), params=params,
                reason=f"剧本不可用：{pb.unavailable_reason}",
                steps=pb.steps)

        return Plan(
            intent=pb.intent, source="playbook", playbook_id=pb.id,
            playbook_name=pb.name, available=True,
            confidence=min(1.0, score / 10.0), params=params,
            steps=pb.steps,
            reason=f"剧本命中（score={score}），查表执行、零模型规划")

    def plan_from_playbook(self, pb: Playbook, params: dict) -> Plan:
        """以既定参数直接构建剧本路径计划（用于调用方补齐缺失参数之后）。

        不重新做意图分类——意图已由上一次分类确定，重分类只会引入不确定性。
        """
        return Plan(
            intent=pb.intent, source="playbook", playbook_id=pb.id,
            playbook_name=pb.name, available=pb.available,
            confidence=1.0, params=dict(params or {}),
            steps=pb.steps,
            reason=("剧本路径（参数已由调用方补齐，查表执行、零模型规划）"
                    if pb.available else f"剧本不可用：{pb.unavailable_reason}"))

    # ------------------------------------------------------------------
    def _react_fallback(self, text: str) -> Plan:
        """非标准路径：LLM 自由规划。必须显著标注。"""
        llm = llm_provider()
        if not llm.is_available:
            return Plan(
                intent="unknown", source="react_fallback", standard_path=False,
                confidence=0.0, reason=(
                    "未命中任何剧本，且 LLM 不可用——无法自由规划。"
                    "请改用标准指令（如『对系统需求做追溯核查』）或补配 "
                    "TRACE_NL_LLM_API_KEY"),
                steps=[])

        tools = [{"name": s.name, "description": s.summary,
                  "scope": s.scope, "params": s.input_schema.get("properties", {})}
                 for s in self.registry.all()] if self.registry else []
        prompt = (
            "可用工具清单（JSON）：\n" + json.dumps(tools, ensure_ascii=False, indent=1)
            + "\n\n用户请求：" + text
            + "\n\n请输出 JSON：{\"intent\": str, \"steps\": "
              "[{\"tool\": str, \"args\": {}}]}。\n"
              "硬约束：只能使用清单中的工具；不得编造工具名；不确定则输出 "
              "{\"clarify\": [\"需要向用户确认的问题\"]}。"
        )
        r = llm.chat(prompt, system="你是核电仪控 V&V 智能体的规划器。"
                                    "只做规划，不执行；输出必须符合给定 JSON 结构。",
                     temperature=0.1, json_mode=True)
        if not r.ok or not r.data:
            return Plan(intent="unknown", source="react_fallback",
                        standard_path=False, confidence=0.0,
                        reason=f"LLM 规划失败：{r.error_code}；已停止，不猜测执行")
        if r.data.get("clarify"):
            return Plan(intent="unknown", source="react_fallback",
                        standard_path=False, confidence=0.0,
                        reason="LLM 规划要求澄清：" + "；".join(r.data["clarify"][:3]))
        steps = [{"id": f"s{i+1}", "scope": s.get("scope", ""),
                  "tool": s.get("tool"), "args": s.get("args") or {}, "gate": True}
                 for i, s in enumerate((r.data.get("steps") or [])[:12])]
        # 越界工具必须在规划期就拦下，不能等到执行期才炸
        bad = [s["tool"] for s in steps
               if self.registry and s["tool"] not in self.registry.names()]
        if bad:
            return Plan(intent="unknown", source="react_fallback",
                        standard_path=False, confidence=0.0,
                        reason=f"LLM 规划引用了不存在的工具 {bad}；已拒绝（防幻觉）")
        return Plan(intent=str(r.data.get("intent", "unknown")),
                    source="react_fallback", standard_path=False,
                    confidence=0.5, steps=steps,
                    reason="非标准路径：LLM 自由规划（已留痕，未命中剧本）")
