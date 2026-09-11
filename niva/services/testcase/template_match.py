# -*- coding: utf-8 -*-
"""部件测试模板分层检索（TCG 第②步）。

检索策略（架构方案 §5.2.3）：分层过滤 + 打分排序，避免一次全量模糊匹配。

    Layer 1  cls 精确命中            → 100 分
    Layer 2  同族回退（_families.yml） → 60 分（阈值族内 HI/LO 方向不同属**半兼容**，显式降级）
    Layer 3  端口签名吻合            → +30
    Layer 4  覆盖方法丰富度          → +10 × min(1, |coverage|/3)
    兜底    通用探针（_generic_probe.yml）→ 5 分，**必须标注 probe=true**

硬冲突一票否决（thresholds.yml `testcase.hard_conflict_veto`）：
    域不一致 / stateful 不一致 / 端口基数不兼容 → 直接淘汰，不进排序。

红线：模板的 ``expected_template`` 只指向真值推演（由 ``validate_testcase_lib.py``
强制），本模块**绝不生成任何数值判据**。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ... import config as C

THRESHOLD = {"HI_MONITOR", "LO_MONITOR", "HI_LIMIT", "LO_LIMIT"}


@dataclass
class TemplateMatch:
    unit_id: str
    center: str
    cls: str
    matched: bool
    match_kind: str = "none"        # exact | family | probe | none
    score: float = 0.0
    breakdown: dict = field(default_factory=dict)
    template: dict = field(default_factory=dict)
    veto_reasons: list[str] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)   # 被否决候选及原因
    warnings: list[str] = field(default_factory=list)


class TemplateLibrary:
    def __init__(self, lib_root: Optional[Path] = None) -> None:
        self.root = Path(lib_root) if lib_root else (C.KNOWLEDGE_ROOT / "testcase_lib")
        self.cases: list[dict] = []
        self.families: dict[str, list[str]] = {}
        self._load()

    def _load(self) -> None:
        import yaml
        for f in sorted(self.root.rglob("*.yml")):
            if f.name == "_families.yml":
                d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                self.families = d.get("families") or {}
                continue
            # 注意：_generic_probe.yml 以下划线开头但**必须加载**——它是兜底模板；
            # 只有 _families.yml（族映射，非用例）才跳过。
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            probe = bool(doc.get("probe"))
            for c in doc.get("cases") or []:
                c = dict(c)
                c["_file"] = f.relative_to(self.root).as_posix()
                c["_probe"] = probe
                self.cases.append(c)

    # ------------------------------------------------------------------
    def _family_of(self, cls: str) -> Optional[str]:
        for fam, spec in self.families.items():
            if cls in (spec.get("members") or []):
                return fam
        return None

    def _direction_of(self, cls: str) -> Optional[str]:
        fam = self.families.get("threshold_monitor") or {}
        for g, members in (fam.get("direction_groups") or {}).items():
            if cls in members:
                return g
        return None

    def _ports_of(self, ports: list[dict]) -> dict:
        return {
            "in": [p for p in ports or [] if p.get("dir") == "in"],
            "out": [p for p in ports or [] if p.get("dir") == "out"],
        }

    def _port_veto(self, tpl_ports: dict, unit_ports: dict) -> Optional[str]:
        """端口基数不兼容 → 一票否决。unit 无端口信息时不否决（信息缺失≠冲突）。"""
        if not unit_ports.get("in") and not unit_ports.get("out"):
            return None
        for side in ("in", "out"):
            want = (tpl_ports.get(side) or {})
            lo, hi = want.get("min"), want.get("max")
            if lo is None and hi is None:
                continue
            n = len(unit_ports.get(side) or [])
            if n == 0:
                continue
            if lo is not None and n < int(lo):
                return f"{side} 端口数 {n} 少于模板要求下限 {lo}"
            if hi is not None and n > int(hi):
                return f"{side} 端口数 {n} 超过模板要求上限 {hi}"
        return None

    # ------------------------------------------------------------------
    def match(self, unit_id: str, center: str, cls: str, *,
              params: Optional[dict] = None, stateful: Optional[bool] = None,
              domain: Optional[str] = None,
              ports: Optional[list[dict]] = None) -> TemplateMatch:
        """为一个块（unit 的中心节点）检索最合适的模板。"""
        mr = TemplateMatch(unit_id=unit_id, center=center, cls=cls, matched=False)
        if not cls:
            mr.match_kind = "none"
            mr.warnings.append("中心节点无语义类（可能是纯信号源/图签），不生成用例")
            return mr

        cfg = C.thresholds().testcase.match_scores
        s_exact = float(cfg.cls_exact); s_fam = float(cfg.cls_family)
        s_port = float(cfg.port_signature); s_cov = float(cfg.coverage_richness)
        veto_on = bool(C.th("testcase.hard_conflict_veto", True))

        fam = self._family_of(cls)
        up = self._ports_of(ports)
        unit_stateful = bool(stateful) if stateful is not None else None

        scored: list[tuple[float, dict, TemplateMatch]] = []
        rejected: list[dict] = []   # 被硬冲突否决的候选及原因（诊断信息，不丢弃）
        for c in self.cases:
            m = TemplateMatch(unit_id=unit_id, center=center, cls=cls, matched=False)
            m.template = c
            tcls = str(c.get("cls") or "")
            is_probe = bool(c.get("_probe"))

            # ---- 硬冲突 ----
            if not is_probe:
                # MIXED（跨域，如模拟入/布尔出的转换块）与两侧都兼容，
                # 不得作为否决依据 —— 否则阈值类模板会被全部误杀。
                if c.get("domain") and domain and c["domain"] != domain \
                        and "MIXED" not in (c["domain"], domain):
                    m.veto_reasons.append(f"域不一致：模板 {c['domain']} vs 块 {domain}")
                aw = c.get("applies_when") or {}
                t_stateful = aw.get("stateful")
                if t_stateful is not None and unit_stateful is not None \
                        and bool(t_stateful) != unit_stateful:
                    m.veto_reasons.append(
                        f"stateful 不一致：模板要求 {t_stateful}，实际 {unit_stateful}")
                pv = self._port_veto(aw.get("ports") or {}, up)
                if pv:
                    m.veto_reasons.append(pv)
            if veto_on and m.veto_reasons:
                m.match_kind = "none"
                rejected.append({"case_id": c.get("case_id"),
                                 "cls": tcls, "veto": m.veto_reasons})
                continue

            # ---- 打分 ----
            if is_probe:
                m.match_kind, base = "probe", 5.0
            elif tcls == cls:
                m.match_kind, base = "exact", s_exact
            elif fam and self._family_of(tcls) == fam:
                m.match_kind, base = "family", s_fam
                # 阈值族内 HI/LO 方向不同属半兼容：可用但必须显式警示
                d1, d2 = self._direction_of(tcls), self._direction_of(cls)
                if d1 and d2 and d1 != d2:
                    m.warnings.append(
                        f"同族但方向不同（模板 {tcls}/{d1}，实际 {cls}/{d2}）："
                        "判据方向须人工复核")
            else:
                continue

            brk = {m.match_kind: base}
            if m.match_kind in ("exact", "family"):
                pv = self._port_veto((c.get("applies_when") or {}).get("ports") or {}, up)
                if pv is None and (up.get("in") or up.get("out")):
                    brk["port_signature"] = s_port
                cov = c.get("coverage") or []
                brk["coverage_richness"] = round(s_cov * min(1.0, len(cov) / 3.0), 2)
            m.score = round(sum(brk.values()), 2)
            m.breakdown = brk
            m.matched = True
            scored.append((m.score, c.get("case_id", ""), m))

        mr.rejected = rejected
        if not scored:
            mr.match_kind = "none"
            mr.warnings.append("无可用模板（候选均被硬冲突否决，详见 rejected）"
                               if self.cases else "部件用例库为空")
            return mr

        scored.sort(key=lambda x: (-x[0], x[1]))
        best = scored[0][2]
        # ★ 否决诊断信息必须随返回对象走：best 与函数开头的 mr 是两个实例，
        #   只写 mr.rejected 会导致调用方拿不到"哪些候选被否决、为什么"。
        best.rejected = rejected
        best.breakdown["rank_candidates"] = len(scored)
        best.warnings = list(dict.fromkeys(best.warnings))
        return best

    def match_unit(self, unit) -> TemplateMatch:
        """按单元中心节点检索。中心是回路时取其首个逻辑/阈值块的类。"""
        center = unit.center
        if center.startswith("loop:"):
            for nid in unit.logic_nodes or []:
                s = unit.sem.get(nid) or {}
                if s.get("cls"):
                    center, cls = nid, s["cls"]
                    break
            else:
                cls = (unit.sem.get(unit.predicates[0]) or {}).get("cls") \
                    if unit.predicates else None
                center = unit.predicates[0] if unit.predicates else unit.center
        else:
            cls = (unit.sem.get(center) or {}).get("cls")
        if cls is None:
            # 回路单元未定位到逻辑/阈值块 → 用单元内任一已知语义类
            for nid in (unit.logic_nodes + list(unit.predicates)):
                s = unit.sem.get(nid) or {}
                if s.get("cls"):
                    center, cls = nid, s["cls"]
                    break
        n = unit.nodes.get(center) or {}
        return self.match(unit.unit_id, center, cls,
                          params=(unit.sem.get(center) or {}).get("params"),
                          stateful=(unit.sem.get(center) or {}).get("stateful"),
                          domain=(unit.sem.get(center) or {}).get("domain"),
                          ports=unit.ports.get(center))
