# -*- coding: utf-8 -*-
"""Executor：按剧本 DAG 串行调度，带阶段门控、重试、占位符解析与全程留痕。

三条纪律：
    1. **阶段门控** —— `gate: true` 的步骤失败则不推进后续步骤（docx §4.2）。
    2. **作用域边界** —— 每步经 `registry.scoped(scope)` 调用，越域即被拒；
       剧本层再守一道，与注册表层形成双保险。
    3. **全程留痕** —— 每步记录 tool/args/结果封套/耗时/重试次数，
       落盘 JSONL，支持"过程回放"。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .. import config as C
from ..kernel.registry import REGISTRY, ToolRegistry
from ..obs import RunContext, start_run
from .playbook import Playbook
from .planner import Plan

REF = re.compile(r"\$\{(\w+)\.([\w.]+)\}")
RETRYABLE = {"TIMEOUT", "MODEL_UNAVAILABLE", "LLM_FAILED", "INTERNAL"}


@dataclass
class StepResult:
    step_id: str
    tool: str
    scope: str
    ok: bool
    attempts: int = 1
    elapsed_s: float = 0.0
    error_code: str = ""
    error_msg: str = ""
    degraded: bool = False
    degrade_reason: str = ""
    data: Any = None
    skipped: bool = False
    skip_reason: str = ""


@dataclass
class RunResult:
    plan: Plan
    steps: list[StepResult] = field(default_factory=list)
    outputs: dict = field(default_factory=dict)     # step_id -> data
    halted: bool = False
    halt_reason: str = ""
    trace_path: str = ""
    run_id: str = ""
    elapsed_s: float = 0.0
    stats: dict = field(default_factory=dict)


class Executor:
    def __init__(self, registry: Optional[ToolRegistry] = None,
                 max_retry: int = 2) -> None:
        self.registry = registry or REGISTRY
        self.max_retry = max_retry

    # ------------------------------------------------------------------
    def _resolve(self, args: dict, params: dict, outputs: dict) -> dict:
        """解析占位符：{param} 与 ${step.field.path}。未解析的保留原样并标注。"""
        out: dict[str, Any] = {}
        for k, v in (args or {}).items():
            out[k] = self._resolve_val(v, params, outputs)
        return out

    FULL_PARAM = re.compile(r"^\{([a-zA-Z_][\w]*)\}$")
    FULL_REF = re.compile(r"^\$\{(\w+)\.([\w.]+)\}$")

    @staticmethod
    def _resolve_ref(m, outputs: dict) -> Any:
        sid, path = m.group(1), m.group(2).split(".")
        cur = outputs.get(sid)
        for part in path:
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                cur = None
            if cur is None:
                break
        return cur

    def _resolve_val(self, v: Any, params: dict, outputs: dict) -> Any:
        """占位符解析。

        ★ 类型保持规则：**整串恰好是一个占位符**时，直接返回参数对象本身
          （dict/list/int/bool/None 都原样传递）。此前用字符串替换，
          导致 ``{upstream_pdfs}`` 被替换成 ``str(dict)``，下游拿到的是
          一个字符串而不是映射 —— 这类错误极难从报错看出来。
        """
        if isinstance(v, str):
            m = self.FULL_PARAM.match(v)
            if m:
                return params.get(m.group(1))
            m = self.FULL_REF.match(v)
            if m:
                return self._resolve_ref(m, outputs)

            def sub_ref(mm):
                got = self._resolve_ref(mm, outputs)
                return got if got is not None else mm.group(0)

            def sub_param(mm):
                val = params.get(mm.group(1))
                return mm.group(0) if val is None else str(val)

            s = REF.sub(sub_ref, v)
            s = re.sub(r"(?<!\$)\{([a-zA-Z_][\w]*)\}", sub_param, s)
            return s
        if isinstance(v, dict):
            return {k: self._resolve_val(x, params, outputs) for k, x in v.items()}
        if isinstance(v, list):
            return [self._resolve_val(x, params, outputs) for x in v]
        return v

    # ------------------------------------------------------------------
    def _call(self, scope: str, tool: str, args: dict,
              ctx: RunContext) -> StepResult:
        view = self.registry.scoped(scope)
        attempts, last = 0, None
        t0 = time.time()
        while attempts <= self.max_retry:
            attempts += 1
            res = view.call(tool, args)
            last = res
            if res.ok:
                break
            if res.error_code not in RETRYABLE:
                break
            time.sleep(min(0.5 * attempts, 2.0))
        sr = StepResult(step_id="", tool=tool, scope=scope, ok=last.ok,
                        attempts=attempts, elapsed_s=round(time.time() - t0, 3),
                        error_code=last.error_code, error_msg=last.error_msg,
                        degraded=last.degraded, degrade_reason=last.degrade_reason,
                        data=last.data)
        ctx.count("tool_calls")
        if last.degraded:
            ctx.count("degraded")
        if not last.ok:
            ctx.count("failed")
        return sr

    # ------------------------------------------------------------------
    def run(self, plan: Plan, ctx: Optional[RunContext] = None) -> RunResult:
        ctx = ctx or start_run(task=f"playbook:{plan.playbook_id or plan.intent}")
        rr = RunResult(plan=plan, run_id=ctx.run_id)
        if not plan.available and plan.source == "playbook":
            rr.halted = True
            rr.halt_reason = plan.reason
            return self._finish(rr, ctx)
        if plan.source == "clarify":
            rr.halted = True
            rr.halt_reason = plan.reason + "；缺失参数：" + "、".join(plan.missing_params)
            return self._finish(rr, ctx)
        if not plan.steps:
            rr.halted = True
            rr.halt_reason = plan.reason or "无可执行步骤"
            return self._finish(rr, ctx)

        outputs: dict[str, Any] = {}
        for step in plan.steps:
            args = self._resolve(step.args, plan.params, outputs)
            sr = self._call(step.scope, step.tool, args, ctx)
            sr.step_id = step.id
            rr.steps.append(sr)

            if sr.ok:
                outputs[step.id] = sr.data
                rr.outputs[step.id] = sr.data
                ctx.emit("step_ok", step=step.id, tool=step.tool,
                         elapsed_s=sr.elapsed_s, attempts=sr.attempts,
                         degraded=sr.degraded)
            else:
                ctx.emit("step_failed", step=step.id, tool=step.tool,
                         error_code=sr.error_code, error_msg=sr.error_msg[:200])
                if step.optional:
                    sr.skipped = True
                    sr.skip_reason = "optional 步骤失败不阻断"
                    continue
                if step.gate or True:   # 默认即门控：失败不推进（docx §4.2）
                    rr.halted = True
                    rr.halt_reason = (f"阶段门控：步骤 {step.id}({step.tool}) 失败 "
                                      f"[{sr.error_code}] {sr.error_msg[:160]}")
                    break

        # 未执行的步骤显式标记，不留"看起来没跑其实是没轮到"的歧义
        done = {s.step_id for s in rr.steps}
        for step in plan.steps:
            if step.id not in done:
                rr.steps.append(StepResult(step_id=step.id, tool=step.tool,
                                           scope=step.scope, ok=False,
                                           skipped=True,
                                           skip_reason="前序门控失败未执行"))
        return self._finish(rr, ctx)

    # ------------------------------------------------------------------
    def _finish(self, rr: RunResult, ctx: RunContext) -> RunResult:
        n = len(rr.steps)
        ok_n = sum(1 for s in rr.steps if s.ok)
        deg = sum(1 for s in rr.steps if s.degraded)
        fail = sum(1 for s in rr.steps if not s.ok and not s.skipped)
        skip = sum(1 for s in rr.steps if s.skipped)
        rr.stats = {"steps": n, "ok": ok_n, "failed": fail,
                    "skipped": skip, "degraded": deg,
                    "degrade_ratio": round(deg / n, 4) if n else 0.0}
        rr.elapsed_s = round(time.time() - ctx.started_at, 3)
        s = ctx.finish()
        rr.trace_path = s.get("log_path") or ""
        return rr
