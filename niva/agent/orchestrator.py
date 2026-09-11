# -*- coding: utf-8 -*-
"""Orchestrator：理解 → 规划 → 执行 → 校验 → 输出（单智能体编排入口）。

CLI：
    python -m niva.agent.orchestrator "对系统需求做追溯核查并导出矩阵" \
        --param downstream_pdf=... --param upstream_pdfs=...
    python -m niva.agent.orchestrator "为第 1 页图纸生成测试用例"
    python -m niva.agent.orchestrator --manifest
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from typing import Any, Optional

from ..server.tools import build_registry
from .critic import Critic, CriticReport
from .executor import Executor, RunResult
from .playbook import PlaybookLibrary
from .planner import Planner


class Orchestrator:
    """单编排者：持有全局状态与任务 DAG，不直接产生业务结论。"""

    def __init__(self, registry=None) -> None:
        # ★ 必须用 build_registry()：kernel.registry.REGISTRY 是空的单例，
        #   工具是在 server.tools.build_registry() 里注册的。用错会导致
        #   剧本可用性判定把所有工具都判成"未实现"。
        self.registry = registry or build_registry()
        self.lib = PlaybookLibrary(registry=self.registry)
        self.planner = Planner(self.lib, self.registry)
        self.executor = Executor(self.registry)
        self.critic = Critic(self.registry)

    # ------------------------------------------------------------------
    def manifest(self) -> dict:
        return {"playbooks": self.lib.manifest(),
                "tools": self.registry.manifest()}

    # ------------------------------------------------------------------
    def run(self, text: str, force_intent: Optional[str] = None,
            params: Optional[dict] = None) -> dict:
        params = dict(params or {})
        plan = self.planner.plan(text, force_intent=force_intent)
        if params:
            plan.params.update(params)
            plan.missing_params = [p for p in plan.missing_params
                                   if p not in params]
            # ★ 用户显式补齐了缺失参数后，必须**重新按同一意图走剧本路径**；
            #   否则计划仍停留在 clarify，executor 会再次以"参数缺失" halted，
            #   造成"参数明明给了却说要反问"的死循环。
            if plan.source == "clarify" and not plan.missing_params:
                pb = self.lib.get(plan.playbook_id)
                if pb is not None:
                    plan = self.planner.plan_from_playbook(pb, plan.params)

        rr = self.executor.run(plan)
        # 参数在 executor.run 之后补齐时需重跑（如 CLI 显式传参补齐缺失项）
        if rr.halted and "参数缺失" in (rr.halt_reason or "") and params:
            plan.missing_params = [p for p in plan.missing_params if p not in params]
            if not plan.missing_params:
                rr = self.executor.run(plan)

        report = self.critic.review(plan, rr)

        return {
            "understanding": {
                "intent": plan.intent, "source": plan.source,
                "standard_path": plan.standard_path,
                "playbook": plan.playbook_id, "confidence": plan.confidence,
                "available": plan.available,
                "reason": plan.reason, "params": plan.params,
                "missing_params": plan.missing_params,
            },
            "execution": {
                "halted": rr.halted, "halt_reason": rr.halt_reason,
                "stats": rr.stats, "elapsed_s": rr.elapsed_s,
                "run_id": rr.run_id, "trace_path": rr.trace_path,
                "steps": [{
                    "step": s.step_id, "tool": s.tool, "scope": s.scope,
                    "ok": s.ok, "skipped": s.skipped,
                    "skip_reason": s.skip_reason,
                    "attempts": s.attempts, "elapsed_s": s.elapsed_s,
                    "error_code": s.error_code,
                    "degraded": s.degraded,
                    "degrade_reason": s.degrade_reason,
                } for s in rr.steps],
            },
            "critique": report.as_dict(),
            "outputs": _summarize(rr.outputs),
            "artifacts": _artifacts(rr.outputs),
            "non_standard_path": not plan.standard_path,
        }


def _summarize(outputs: dict) -> dict:
    """只输出摘要，避免把大 JSON 全量塞进返回（完整产物在制品仓）。"""
    out = {}
    for k, v in outputs.items():
        if isinstance(v, dict):
            brief = {}
            for kk, vv in v.items():
                if isinstance(vv, list):
                    brief[kk] = f"<list:{len(vv)}>"
                elif isinstance(vv, dict):
                    brief[kk] = f"<dict:{len(vv)}>"
                elif isinstance(vv, str) and len(vv) > 160:
                    brief[kk] = vv[:160] + "…"
                else:
                    brief[kk] = vv
            out[k] = brief
        else:
            out[k] = "<non-dict>"
    return out


def _artifacts(outputs: dict) -> dict:
    arts = {}
    for k, v in outputs.items():
        if not isinstance(v, dict):
            continue
        for key in ("xlsx_path", "path", "case_set_id", "matrix_id", "doc_id"):
            if v.get(key):
                arts[f"{k}.{key}"] = v[key]
        # 下探一层（如 coverage.evidence_package.xlsx_path）
        for kk, vv in v.items():
            if isinstance(vv, dict):
                for key in ("xlsx_path", "path"):
                    if vv.get(key):
                        arts[f"{k}.{kk}.{key}"] = vv[key]
    return arts


def main() -> int:
    ap = argparse.ArgumentParser(description="NIVA 编排入口")
    ap.add_argument("instruction", nargs="?", help="自然语言指令")
    ap.add_argument("--intent", help="强制指定意图（跳过分类）")
    ap.add_argument("--param", action="append", default=[],
                    help="以 k=v 形式补齐参数，可多次")
    ap.add_argument("--manifest", action="store_true", help="打印剧本与工具面清单")
    args = ap.parse_args()

    orch = Orchestrator()
    if args.manifest:
        print(json.dumps(orch.manifest(), ensure_ascii=False, indent=2))
        return 0
    if not args.instruction:
        print("请提供自然语言指令，或使用 --manifest", file=sys.stderr)
        return 2

    params = {}
    for kv in args.param:
        k, _, v = kv.partition("=")
        try:
            params[k] = json.loads(v)
        except json.JSONDecodeError:
            params[k] = v

    rep = orch.run(args.instruction, force_intent=args.intent, params=params)
    print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    if rep["execution"]["halted"] or not rep["critique"]["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
