# -*- coding: utf-8 -*-
"""工具注册表与契约（架构方案 §4「十一个工具契约」的落地骨架）。

三项硬规约在此变成代码事实：
    1. **统一返回封套** —— 每个工具返回 ``{ok, data, evidence, warnings, trace,
       degraded, degrade_reason, error_code}``。编排层据 ``error_code`` 决定
       重试 / 降级 / 转人工，不必逐个工具写特例。
    2. **元数据声明** —— 注册时显式声明 ``{readonly, idempotent, side_effects}``。
       "全部只读"从文字承诺变为**可程序校验的代码事实**（``audit_readonly()``）。
    3. **成对调用固化** —— 追溯类工具的输入输出里 ``evidence`` 是必填项；
       缺证据的追溯边在图谱守门处被拒绝（见 ``TraceGraph.add_edge(strict=True)``）。

本模块不依赖具体实现，只负责"契约"。实现由 adapters / services 注入，
因此可以先用占位实现把 MCP 工具面立起来，再逐个填实（架构方案 §13 第 4 条）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------
# 错误码（枚举化 —— 编排层据码决策，不解析自然语言）
# ---------------------------------------------------------------------
class ErrorCode:
    OK = ""
    PARSE_FAILED = "PARSE_FAILED"                 # 解析失败
    NOT_VECTOR = "NOT_VECTOR"                     # 非矢量图纸（扫描件）
    KNOWLEDGE_MISSING = "KNOWLEDGE_MISSING"        # 知识缺失
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"        # 模型不可用
    TIMEOUT = "TIMEOUT"
    SCHEMA_INVALID = "SCHEMA_INVALID"              # 模式校验不通过
    NOT_FOUND = "NOT_FOUND"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"            # 占位工具
    GATE_REJECTED = "GATE_REJECTED"                # 阶段门控未通过
    DEGRADED = "DEGRADED"
    INTERNAL = "INTERNAL"


# ---------------------------------------------------------------------
# 返回封套
# ---------------------------------------------------------------------
@dataclass
class ToolResult:
    ok: bool = True
    data: Any = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)
    degraded: bool = False
    degrade_reason: str = ""
    error_code: str = ""
    error_msg: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data": self.data,
            "evidence": self.evidence,
            "warnings": self.warnings,
            "trace": self.trace,
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "error_code": self.error_code,
            "error_msg": self.error_msg,
        }


def ok(data: Any = None, **kw: Any) -> ToolResult:
    return ToolResult(ok=True, data=data, **kw)


def fail(code: str, msg: str = "", **kw: Any) -> ToolResult:
    return ToolResult(ok=False, error_code=code, error_msg=msg, **kw)


def degraded(code: str, reason: str, data: Any = None, **kw: Any) -> ToolResult:
    """降级返回：允许继续，但**必须**显式携带降级原因（"降级必可见"）。"""
    return ToolResult(ok=True, data=data, degraded=True, degrade_reason=reason,
                      error_code=code, **kw)


# ---------------------------------------------------------------------
# 能力作用域与 LLM 权限（决策记录：单编排器 + 工具作用域）
# ---------------------------------------------------------------------
# 为什么不把 22 个工具挂在一个执行体上：
#   - 标准路径由剧本查表 + 确定性 Router 选工具，**不经 LLM**，工具多寡不构成负载；
#   - 真正的风险是"确定性边界"失守：方案要求图纸理解与校验复核两个角色
#     **不可调用大模型**，若合并为单一执行体，该约束只能写在提示词里——
#     而提示词约束不是工程约束。
# 因此：仍是一个编排器（一个进程、一个 LLM 上下文、一条可复现的留痕），
#       但工具按"权限域"切成四个作用域，执行体只拿到本作用域的视图。
SCOPES: tuple[str, ...] = ("doc", "diagram", "generate", "graph")

# 每个作用域的 LLM 权限档位
LLM_NONE = "none"                    # 构造性禁止：执行体拿不到 llm_provider 句柄
LLM_ADJUDICATE = "adjudicate_only"   # 仅可发起歧义裁决
LLM_NARRATE = "narrate_only"         # 仅可生成自然语言说明

SCOPE_LLM_POLICY: dict[str, str] = {
    "doc":      LLM_ADJUDICATE,   # 需求追溯：LLM 仅裁决歧义行
    "diagram":  LLM_NONE,         # 图纸理解：全符号管线，不经大模型
    "generate": LLM_NARRATE,      # 测试设计：步骤由算法出，LLM 只写说明
    "graph":    LLM_NONE,         # 校验复核：纯规则与统计
}


# ---------------------------------------------------------------------
# 工具声明
# ---------------------------------------------------------------------
@dataclass
class ToolSpec:
    name: str                       # 如 "trace.text.verify"
    family: str                     # doc | diagram | generate | graph
    summary: str
    input_schema: dict[str, Any]
    readonly: bool                  # 是否只读（外部 MCP 白名单依据）
    idempotent: bool                # 是否幂等
    side_effects: list[str] = field(default_factory=list)  # 写盘/写库目标
    requires_llm: bool = False      # 是否依赖 LLM（决定能否纯离线跑）
    requires_knowledge: bool = False
    implemented: bool = True        # False = 契约已冻结、实现待填
    handler: Optional[Callable[..., ToolResult]] = None
    # ---- 能力作用域（P1 新增）----
    scope: str = ""                 # 属 SCOPE_LLM_POLICY 之一；空表示未归类
    llm_permission: str = ""        # 显式声明；空则取所在作用域的策略

    def effective_llm_permission(self) -> str:
        return self.llm_permission or SCOPE_LLM_POLICY.get(self.scope, LLM_NONE)

    def llm_allowed(self) -> bool:
        return self.effective_llm_permission() != LLM_NONE

    # ---- 输入校验（轻量 JSON Schema 子集：required / properties.type / enum）----
    def validate(self, args: dict[str, Any]) -> list[str]:
        errs: list[str] = []
        req = self.input_schema.get("required") or []
        for k in req:
            if k not in args or args[k] is None:
                errs.append(f"缺少必填参数：{k}")
        props = self.input_schema.get("properties") or {}
        for k, v in args.items():
            spec = props.get(k)
            if not spec:
                continue
            want = spec.get("type")
            if want and v is not None and not _type_ok(v, want):
                errs.append(f"参数 {k} 类型应为 {want}，实际 {type(v).__name__}")
            enum = spec.get("enum")
            # 显式传 None 等同于"未提供该参数"，不做枚举校验
            # （否则剧本可选参数填 None 会被误判为 SCHEMA_INVALID）
            if enum and v is not None and v not in enum:
                errs.append(f"参数 {k} 取值须属于 {enum}，实际 {v!r}")
        return errs

    def as_mcp_tool(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.summary,
            "inputSchema": self.input_schema,
            "annotations": {
                "readOnlyHint": self.readonly,
                "idempotentHint": self.idempotent,
                "destructiveHint": bool(self.side_effects),
            },
        }


def _type_ok(v: Any, want: str) -> bool:
    return {
        "string": isinstance(v, str),
        "integer": isinstance(v, int) and not isinstance(v, bool),
        "number": isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": isinstance(v, bool),
        "array": isinstance(v, list),
        "object": isinstance(v, dict),
        "null": v is None,
    }.get(want, True)


# ---------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------
class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"工具名重复注册：{spec.name}")
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise KeyError(f"未注册的工具：{name}")
        return self._tools[name]

    def all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def by_family(self, family: str) -> list[ToolSpec]:
        return [t for t in self._tools.values() if t.family == family]

    def names(self) -> list[str]:
        return sorted(self._tools)

    # ---- 调用 ----
    def call(self, name: str, args: Optional[dict[str, Any]] = None,
             run_id: str = "") -> ToolResult:
        """统一入口：校验 → 执行 → 计时 → 封套化。**不抛异常。**"""
        args = dict(args or {})
        try:
            spec = self.get(name)
        except KeyError as exc:
            return fail(ErrorCode.NOT_FOUND, str(exc))

        errs = spec.validate(args)
        if errs:
            return fail(ErrorCode.SCHEMA_INVALID, "; ".join(errs))

        if not spec.implemented or spec.handler is None:
            return fail(ErrorCode.NOT_IMPLEMENTED,
                        f"工具 {name} 的契约已冻结，实现待填（阶段 P1/P2/P3）")

        t0 = time.time()
        try:
            res = spec.handler(**args)
        except Exception as exc:
            return fail(ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}")
        if not isinstance(res, ToolResult):
            res = ok(res)
        res.trace.setdefault("tool", name)
        res.trace.setdefault("elapsed_s", round(time.time() - t0, 3))
        if run_id:
            res.trace.setdefault("run_id", run_id)
        if spec.readonly:
            res.trace.setdefault("readonly", True)
        return res

    # ---- 审计 ----
    def audit_readonly(self) -> dict[str, Any]:
        """返回非只读工具清单。对外暴露的 MCP 白名单只允许全只读工具。"""
        writers = [t.name for t in self._tools.values() if not t.readonly]
        return {"total": len(self._tools), "writers": writers,
                "all_readonly": not writers}

    def missing_implementations(self) -> list[str]:
        return sorted(t.name for t in self._tools.values() if not t.implemented)

    # ---- 作用域视图（能力隔离）----
    def scoped(self, scope: str) -> "ScopedRegistry":
        """返回某个作用域的**受限工具视图**。

        执行体只应持有这个视图：它看不到别的作用域的工具，
        因此"不该调用的工具"不是被劝阻，而是**根本不在可视范围内**。
        """
        if scope not in SCOPES:
            raise ValueError(f"未知作用域：{scope}（可选 {SCOPES}）")
        return ScopedRegistry(self, scope)

    def audit_permissions(self) -> dict[str, Any]:
        """LLM 权限审计：把"确定性边界"从文字承诺变成可程序校验的事实。

        校验两条不变式：
          1. 每个工具都已归类到某个作用域；
          2. 工具自身的 requires_llm 与所在作用域的 LLM 策略**不矛盾**
             （策略为 none 却声明 requires_llm=True，即为越界）。
        """
        violations: list[str] = []
        per_scope: dict[str, dict[str, Any]] = {}
        for t in self._tools.values():
            if not t.scope:
                violations.append(f"{t.name}: 未归类作用域")
                continue
            pol = t.effective_llm_permission()
            if pol == LLM_NONE and t.requires_llm:
                violations.append(
                    f"{t.name}: 作用域 {t.scope} 的 LLM 策略为 none，但声明 requires_llm=True")
            d = per_scope.setdefault(t.scope, {"tools": [], "llm_permission": pol,
                                               "readonly": 0, "writers": []})
            d["tools"].append(t.name)
            if t.readonly:
                d["readonly"] += 1
            else:
                d["writers"].append(t.name)
        for d in per_scope.values():
            d["tools"].sort()
            d["count"] = len(d["tools"])
            d["max_recommended"] = 12
            d["within_recommended"] = d["count"] <= 12
        return {"scopes": per_scope, "violations": violations,
                "ok": not violations}

    def manifest(self) -> dict[str, Any]:
        fam: dict[str, list[str]] = {}
        for t in self._tools.values():
            fam.setdefault(t.family, []).append(t.name)
        return {
            "total": len(self._tools),
            "by_family": {k: sorted(v) for k, v in sorted(fam.items())},
            "audit": self.audit_readonly(),
            "permissions": self.audit_permissions(),
            "pending_implementations": self.missing_implementations(),
        }


class ScopedRegistry:
    """某作用域的受限工具视图 + 该作用域的 LLM 权限声明。

    用法::

        diagram = REGISTRY.scoped("diagram")
        assert not diagram.llm_allowed          # 图纸域构造性禁止 LLM
        specs = diagram.specs()                 # 只含图纸族工具
        res = diagram.call("get_meta", {})
    """

    def __init__(self, parent: ToolRegistry, scope: str) -> None:
        self._parent = parent
        self.scope = scope
        self.llm_permission = SCOPE_LLM_POLICY[scope]

    @property
    def llm_allowed(self) -> bool:
        return self.llm_permission != LLM_NONE

    def names(self) -> list[str]:
        return sorted(t.name for t in self._parent.all() if t.scope == self.scope)

    def specs(self) -> list[ToolSpec]:
        return [t for t in self._parent.all() if t.scope == self.scope]

    def has(self, name: str) -> bool:
        try:
            return self._parent.get(name).scope == self.scope
        except KeyError:
            return False

    def call(self, name: str, args: Optional[dict[str, Any]] = None,
             run_id: str = "") -> ToolResult:
        """经本视调用。**跨作用域调用会被拒绝**——这是隔离的执行点。"""
        try:
            spec = self._parent.get(name)
        except KeyError as exc:
            return fail(ErrorCode.NOT_FOUND, str(exc))
        if spec.scope != self.scope:
            return fail(
                ErrorCode.GATE_REJECTED,
                f"越域调用被拒绝：{name} 属作用域 {spec.scope!r}，"
                f"当前执行体仅拥有 {self.scope!r}（{self.names()}）",
            )
        return self._parent.call(name, args, run_id=run_id)

    def as_mcp_tools(self) -> list[dict[str, Any]]:
        return [t.as_mcp_tool() for t in self.specs()]

    def __repr__(self) -> str:
        return (f"<ScopedRegistry scope={self.scope} tools={len(self.names())} "
                f"llm={self.llm_permission}>")


REGISTRY = ToolRegistry()
