# -*- coding: utf-8 -*-
"""剧本库装载与可用性自动判定。

`available` 不手填：由本模块依据"剧本引用的工具是否全部已实现"**自动判定**，
从机制上避免"文档说能跑、代码跑不了"的漂移。不可用的剧本仍会出现在清单里，
并显式给出缺什么——**而不是悄悄消失**。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .. import config as C
from ..kernel.registry import ToolRegistry

PLACEHOLDER = re.compile(r"\{([a-zA-Z_][\w]*)\}|\$\{(\w+)\.([\w.]+)\}")


@dataclass
class Step:
    id: str
    scope: str
    tool: str
    args: dict = field(default_factory=dict)
    gate: bool = False            # 阶段门控：失败即停
    optional: bool = False        # 失败不阻断


@dataclass
class Playbook:
    id: str
    name: str
    intent: str
    trigger: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    steps: list[Step] = field(default_factory=list)
    available: bool = False
    unavailable_reason: str = ""
    missing_tools: list[str] = field(default_factory=list)
    path: str = ""

    def score(self, text: str) -> float:
        """触发打分：强短语 >> 普通关键词。用于 planner 的查表命中。"""
        t = str(text or "")
        s = 0.0
        for w in self.trigger.get("strong") or []:
            if w in t:
                s += 10.0
        for w in self.trigger.get("keywords") or []:
            if w in t:
                s += 1.0
        return s

    def required_params(self) -> list[str]:
        return list((self.params or {}).get("required") or [])


class PlaybookLibrary:
    def __init__(self, path: Optional[Path] = None,
                 registry: Optional[ToolRegistry] = None) -> None:
        self.path = Path(path) if path else (
            C.PKG_ROOT / "agent" / "playbooks" / "playbooks.yaml")
        self.registry = registry
        self.playbooks: list[Playbook] = []
        self._load()

    def _load(self) -> None:
        import yaml
        doc = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        for pb in doc.get("playbooks") or []:
            steps = [Step(id=s["id"], scope=s["scope"], tool=s["tool"],
                          args=s.get("args") or {}, gate=bool(s.get("gate")),
                          optional=bool(s.get("optional")))
                     for s in pb.get("steps") or []]
            self.playbooks.append(Playbook(
                id=pb["id"], name=pb.get("name", pb["id"]),
                intent=pb.get("intent", ""), trigger=pb.get("trigger") or {},
                params=pb.get("params") or {}, steps=steps,
                path=str(self.path)))

    # ------------------------------------------------------------------
    def refresh_availability(self, registry: Optional[ToolRegistry] = None) -> None:
        """依据工具实现情况自动判定每个剧本是否可用。"""
        reg = registry or self.registry
        if reg is None:
            from ..kernel.registry import REGISTRY as reg  # noqa: N813
        for pb in self.playbooks:
            missing: list[str] = []
            not_ready: list[str] = []
            for s in pb.steps:
                try:
                    spec = reg.get(s.tool)
                except KeyError:
                    missing.append(s.tool)
                    continue
                if not spec.implemented:
                    not_ready.append(s.tool)
                if spec.scope != s.scope:
                    missing.append(f"{s.tool} 作用域不符：注册为 {spec.scope}，剧本声明 {s.scope}")
            pb.missing_tools = sorted(set(missing) | set(not_ready))
            pb.available = not pb.missing_tools
            pb.unavailable_reason = (
                "以下工具未实现或作用域不符：" + "、".join(pb.missing_tools)
                if pb.missing_tools else "")

    # ------------------------------------------------------------------
    def best_match(self, text: str, min_score: float = 3.0
                   ) -> tuple[Optional[Playbook], float]:
        """返回打分最高的剧本。低于阈值视为未命中（走 ReAct 兜底）。"""
        best, bs = None, 0.0
        for pb in self.playbooks:
            s = pb.score(text)
            if s > bs:
                best, bs = pb, s
        if best is None or bs < min_score:
            return None, bs
        return best, bs

    def by_intent(self, intent: str) -> Optional[Playbook]:
        for pb in self.playbooks:
            if pb.intent == intent:
                return pb
        return None

    def get(self, pb_id: str) -> Optional[Playbook]:
        for pb in self.playbooks:
            if pb.id == pb_id:
                return pb
        return None

    def manifest(self) -> dict[str, Any]:
        return {
            "count": len(self.playbooks),
            "playbooks": [{
                "id": p.id, "name": p.name, "intent": p.intent,
                "steps": len(p.steps), "available": p.available,
                "unavailable_reason": p.unavailable_reason,
                "required_params": p.required_params(),
            } for p in self.playbooks],
        }


def extract_placeholders(text: str) -> set[str]:
    """取 {param} 占位符名（不含 ${step.field} 引用）。"""
    out = set()
    for a, _b, _c in PLACEHOLDER.findall(str(text or "")):
        if a:
            out.add(a)
    return out
