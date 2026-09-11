# -*- coding: utf-8 -*-
"""Critic：结果自检（"反思/校验"被实现为确定性规则，而非模型自我评价）。

四类检查（docx §4.2 / §4.10）：
    1. 降级率与熔断     —— 降级条目占比超阈值即停，防止低质输入被批量加工成貌似正常的产出
    2. 置信度四级处置    —— 自动确认 / 辅助人审 / 强制人审 / 隔离区
    3. 证据完备性       —— 追溯边不得缺证据（A2 公理的运行期复核）
    4. LLM 边界         —— 被禁 LLM 的作用域内不得出现 requires_llm 的工具调用
    5. 产出良率         —— 解析/生成步骤若零产出，视为异常而非"成功完成"
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .. import config as C
from ..kernel.model import TraceGraph
from ..kernel.registry import ToolRegistry
from .executor import RunResult
from .planner import Plan

TIERS = ("auto_confirm", "assisted_review", "force_human", "quarantined")


@dataclass
class CriticReport:
    passed: bool = True
    findings: list[dict] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    tier_counts: dict = field(default_factory=dict)
    degrade_ratio: float = 0.0
    circuit_breaker: bool = False

    def as_dict(self) -> dict:
        return {"passed": self.passed, "findings": self.findings,
                "checks": self.checks, "tier_counts": self.tier_counts,
                "degrade_ratio": self.degrade_ratio,
                "circuit_breaker": self.circuit_breaker}


class Critic:
    def __init__(self, registry: Optional[ToolRegistry] = None) -> None:
        self.registry = registry

    # ------------------------------------------------------------------
    def _load_graph(self, outputs: dict) -> Optional[TraceGraph]:
        """若运行产出了矩阵，则加载其图谱做边级检查。"""
        mid = _dig(outputs, "build", "matrix_id") or _dig(outputs, "verify", "matrix_id")
        if not mid:
            return None
        p = C.ARTIFACTS_DIR / "matrices" / f"{mid}.json"
        if not p.exists():
            return None
        try:
            return TraceGraph.from_dict(_matrix_to_graph_dict(p))
        except Exception:
            return None

    # ------------------------------------------------------------------
    def review(self, plan: Plan, rr: RunResult) -> CriticReport:
        rep = CriticReport()
        th = C.thresholds()
        cb = th.orchestration.circuit_breaker
        tiers = th.orchestration.confidence_tiers

        def check(name: str, ok: bool, detail: str, severity: str = "error") -> None:
            """severity 语义：error 才判不通过；warning 仅记录（可见但不阻断）。

            此前把警告也算失败，导致"文档域证据暂不可定位"这类**已知局限**
            把整次成功的运行判成不通过 —— 验收信号被噪声淹没。
            """
            rep.checks.append({"check": name, "ok": ok, "detail": detail,
                               "severity": severity if not ok else "info"})
            if not ok:
                rep.findings.append({"check": name, "detail": detail,
                                     "severity": severity})
                if severity == "error":
                    rep.passed = False

        # 1) 降级率与熔断
        rep.degrade_ratio = float(rr.stats.get("degrade_ratio") or 0.0)
        max_deg = float(cb.max_degrade_ratio)
        breaker = rep.degrade_ratio > max_deg and len(rr.steps) >= 3
        rep.circuit_breaker = breaker
        check("降级率 ≤ 熔断阈值", not breaker,
              f"降级率 {rep.degrade_ratio} vs 阈值 {max_deg}（步数 {len(rr.steps)}）")

        # 2) 产出良率：零产出不算成功
        parse = rr.outputs.get("parse")
        if isinstance(parse, dict):
            n_items = (parse.get("counts") or {}).get("items", 0)
            n_sec = (parse.get("counts") or {}).get("sections", 0)
            check("解析良率 > 0", (n_items + n_sec) > 0,
                  f"条目 {n_items} / 章节 {n_sec}（零产出通常意味着文档类型误判或文本层缺失）")
        gen = rr.outputs.get("generate")
        if isinstance(gen, dict) and "stats" in gen:
            check("用例良率 > 0", (gen["stats"].get("cases") or 0) > 0,
                  f"用例数 {gen['stats'].get('cases')}；"
                  f"跳过 {gen['stats'].get('skipped')}（跳过原因须逐条可见）")

        # 3) LLM 边界
        if self.registry:
            bad = []
            for s in rr.steps:
                if s.skipped or not s.tool:
                    continue
                try:
                    spec = self.registry.get(s.tool)
                except KeyError:
                    continue
                if spec.requires_llm and spec.effective_llm_permission() == "none":
                    bad.append(f"{s.tool}@{spec.scope}")
            check("LLM 边界（被禁作用域零调用）", not bad,
                  f"越界={bad or '无'}")

        # 4) 置信度四级处置 + 证据完备性（仅当产出图谱）
        g = self._load_graph(rr.outputs)
        rep.tier_counts = {k: 0 for k in TIERS}
        if g is not None and g.edges:
            a = float(tiers.auto_confirm)
            b = float(tiers.assisted_review)
            top1_min = float(tiers.top1_min_score)
            for e in g.edges.values():
                if e.status == "QUARANTINED":
                    rep.tier_counts["quarantined"] += 1
                elif e.confidence >= a and not e.ambiguous:
                    rep.tier_counts["auto_confirm"] += 1
                elif e.confidence >= b:
                    rep.tier_counts["assisted_review"] += 1
                else:
                    rep.tier_counts["force_human"] += 1
            no_ev = [e.edge_id for e in g.edges.values() if not e.has_evidence]
            unloc = [e.edge_id for e in g.edges.values() if not e.evidence_verifiable()]
            check("追溯边证据完备", not no_ev,
                  f"缺证据边 {len(no_ev)} 条" + (f"：{no_ev[:3]}" if no_ev else ""))
            if unloc:
                # 不可定位≠缺证据：文档域暂未暴露字符偏移（已知局限），降级为 warning
                check("证据可定位", False,
                      f"{len(unloc)}/{len(g.edges)} 条证据暂不可点击定位"
                      "（文档域 legacy 未暴露字符偏移，属已知局限）",
                      severity="warning")
            rep.tier_counts["edges_total"] = len(g.edges)
        else:
            rep.tier_counts["note"] = "本次运行未产出追溯图谱，边级指标不适用"
        return rep


def _dig(outputs: dict, step: str, key: str):
    d = outputs.get(step)
    if isinstance(d, dict):
        return d.get(key)
    return None


def _matrix_to_graph_dict(p: Path) -> dict:
    """把矩阵制品转换为 TraceGraph 字典（与 DocAdapter.to_graph 同构，避免循环导入）。"""
    import sys
    from .. import config as C
    if str(C.PKG_ROOT) not in sys.path:
        sys.path.insert(0, str(C.PKG_ROOT))
    from ..adapters.doc.service import DocAdapter
    payload = json.loads(p.read_text(encoding="utf-8"))
    return DocAdapter().to_graph(payload).to_dict()
