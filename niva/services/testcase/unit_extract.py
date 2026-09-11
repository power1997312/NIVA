# -*- coding: utf-8 -*-
"""可测单元抽取（TCG 第①步）。

从 SAMA-IR 抽出三类可测单元（架构方案 §5.2.2）：
    loop   回路级   —— IR.loops，保护逻辑整体功能验证（核电最关心）
    block  块级     —— 带参数的逻辑/阈值块及其输入锥
    predicate 阈值块 —— 模拟→布尔转换点，定值绑定的落点

**关键工程事实**：IR 节点的 ``cls`` 是**图元形态**（bubble/funcblock/diamond），
不是语义类。真正的语义类（AND/OR/VOTE_KN/HI_MONITOR…）必须由符号字典从
**标签文本**解析出来。本模块自带一个独立实现（读 ``knowledge/symbols/*.yml``
的 ``pat`` 正则），并可与 legacy 的 ``SymbolDict.classify_label`` 做奇偶校验——
这与 SAMA-V1 用 ``kb_parity.py`` 守护 ``bool_dict`` 是同一纪律。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ... import config as C

# 语义类分组（与 knowledge/symbols/*.yml 的条目对应）
THRESHOLD_CLASSES = {"HI_MONITOR", "LO_MONITOR", "HI_LIMIT", "LO_LIMIT"}
LOGIC_CLASSES = {"AND", "OR", "NOT", "XOR", "VOTE_KN", "SR", "RESET"}
STATEFUL_CLASSES = {"SR", "RESET"}
ANALOG_PASSTHRU = {"SUMMER", "GAIN", "FUNC", "IM", "AD", "DA", "IP", "ISOL",
                   "SELECT_MAX", "SELECT_MIN", "SELECT_W", "SWITCH_T", "TRACK",
                   "SWITCH_RL", "DC", "PID_STACK"}
# 转换类：模拟→布尔
CONVERT_CLASSES = THRESHOLD_CLASSES

MAX_CLOSURE_DEPTH = int(C.th("testcase.truth.boolean_full_enum_max_inputs", 6))
ENUM_MAX_INPUTS = int(C.th("testcase.truth.boolean_full_enum_max_inputs", 6))


# =====================================================================
# 符号语义解析（独立实现 + 可与 legacy 校验）
# =====================================================================
class ClsResolver:
    """从标签文本解析语义类。只依赖 ``knowledge/symbols/*.yml``，不依赖 legacy。"""

    def __init__(self, symbols_dir: Optional[Path] = None) -> None:
        d = Path(symbols_dir) if symbols_dir else (C.KNOWLEDGE_ROOT / "symbols")
        self._compiled: list[tuple[re.Pattern, dict]] = []
        self.entries: list[dict] = []
        for f in sorted(d.glob("*.yml")):
            import yaml
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            for e in doc.get("entries") or []:
                if not e.get("cls"):
                    continue
                self.entries.append(e)
                try:
                    self._compiled.append((re.compile(e["pat"]), e))
                except re.error:
                    continue
        # 别名也参与匹配（如 "&"→AND、"2oo3"→VOTE_KN）
        for e in self.entries:
            for al in e.get("aliases") or []:
                try:
                    self._compiled.append((re.compile(rf"^{re.escape(str(al))}$"), e))
                except re.error:
                    continue

    def classify(self, label: str) -> Optional[dict]:
        """返回 {cls, domain, stateful, params, exec, standard_ref} 或 None。

        空白/图签/注释类文本（如"密级：非密"、"蒸汽管道隔离"）返回 None。
        """
        t = re.sub(r"\s+", "", str(label or ""))
        if not t:
            return None
        for pat, e in self._compiled:
            if pat.fullmatch(t) or pat.match(t):
                dom_in = (e.get("in") or {}).get("domain", "A")
                dom_out = e.get("out", "A")
                domain = "BOOLEAN" if (dom_in == "B" and dom_out == "B") else \
                         "MIXED" if (dom_in == "B" or dom_out == "B") else "ANALOG"
                params = dict(e.get("params") or {})
                # VOTE_KN 的 k/n 必须来自图纸实际标注（如 2oo3 / 2/4），不得默认。
                # 这是"免费校验 C2 特例"的前提：参数与实际触线数不一致即 R 级检出。
                if e["cls"] == "VOTE_KN":
                    m2 = (re.search(r"(\d)\s*oo\s*(\d)", t)
                          or re.search(r"(\d)\s*/\s*(\d)", t))
                    if m2:
                        params = {"k": int(m2.group(1)), "n": int(m2.group(2))}
                    else:
                        params = {}
                return {"cls": e["cls"], "domain": domain,
                        "stateful": bool(e.get("stateful")),
                        "params": params,
                        "exec": e.get("exec", ""),
                        "standard_ref": e.get("standard_ref", "")}
        return None

    def parity_with_legacy(self, samples: list[str]) -> list[str]:
        """与 legacy ``SymbolDict.classify_label`` 比对，返回不一致样本。"""
        diffs: list[str] = []
        try:
            with C.legacy_import_path("diagram"):
                from knowledge_api import SymbolDict
                sd = SymbolDict()
        except Exception as exc:  # legacy 不可用时跳过奇偶校验
            return [f"<legacy 不可用，跳过奇偶校验: {exc}>"]
        for s in samples:
            mine = (self.classify(s) or {}).get("cls")
            r = sd.classify_label(s)
            theirs = (r or {}).get("cls") if isinstance(r, dict) else r
            if mine != theirs:
                diffs.append(f"{s!r}: niva={mine} legacy={theirs}")
        return diffs


# =====================================================================
# 可测单元
# =====================================================================
@dataclass
class Unit:
    unit_id: str
    kind: str                    # loop | block | predicate
    page: int
    center: str                  # 中心节点 id 或 loop_id
    label: str
    nodes: dict[str, dict] = field(default_factory=dict)     # id → IR node（原样）
    sem: dict[str, dict] = field(default_factory=dict)       # id → 分类结果
    in_edges: list[dict] = field(default_factory=list)       # {src,dst,port,type,basis,status}
    predicates: list[str] = field(default_factory=list)      # 阈值块节点 id
    variables: list[dict] = field(default_factory=list)      # 谓词变量描述
    outputs: list[str] = field(default_factory=list)         # 输出节点 id
    safety_class: list[str] = field(default_factory=list)
    ports: dict[str, list] = field(default_factory=dict)   # 端口签名（模板检索用）
    warnings: list[str] = field(default_factory=list)

    @property
    def logic_nodes(self) -> list[str]:
        return [nid for nid, s in self.sem.items()
                if s and s["cls"] in LOGIC_CLASSES]


# =====================================================================
class UnitExtractor:
    def __init__(self, ir: dict, page: int = 0,
                 resolver: Optional[ClsResolver] = None,
                 doc_id: str = "SAMA") -> None:
        self.ir = ir
        self.page = int(page or (ir.get("meta") or {}).get("page") or 0)
        self.resolver = resolver or ClsResolver()
        self.doc_id = doc_id
        self._build()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        g = self.ir.get("graph") or {}
        self.nodes: dict[str, dict] = {n["id"]: n for n in g.get("nodes") or []}
        self.sem: dict[str, Optional[dict]] = {}
        for nid, n in self.nodes.items():
            label = n.get("text") or n.get("label") or ""
            self.sem[nid] = (None if n.get("cls") == "bubble"
                             else self.resolver.classify(label))

        # 有向图：from.node → to.node；记录每个节点按端口序的输入
        self.out_edges: dict[str, list[dict]] = {}
        self.in_edges: dict[str, list[dict]] = {}
        self.wires_norm: list[dict] = []
        for w in (self.ir.get("port_level") or {}).get("wires") or []:
            e = {"src": w["from"]["node"], "dst": w["to"]["node"],
                 "src_port": w["from"].get("port"), "dst_port": w["to"].get("port"),
                 "type": w.get("type"), "basis": w.get("basis"),
                 "status": w.get("status"), "sign": w["from"].get("sign")}
            self.wires_norm.append(e)
            self.out_edges.setdefault(e["src"], []).append(e)
            self.in_edges.setdefault(e["dst"], []).append(e)
        for k in self.in_edges:   # 按端口名排序，保证输入序确定
            self.in_edges[k].sort(key=lambda e: str(e["dst_port"]))

        self.ports = {p["id"]: (p.get("ports") or [])
                      for p in (self.ir.get("port_level") or {}).get("nodes") or []}
        self.tb_by_xy: dict[tuple, dict] = {}
        for b in ((self.ir.get("semantic_bindings") or {}).get("threshold_bindings") or []):
            xy = b.get("block_xy") or []
            if len(xy) >= 2 and xy[0] is not None and xy[1] is not None:
                # ★ 必须转 tuple：JSON 反序列化出来是 list，list 不可哈希，作 dict 键直接 TypeError
                self.tb_by_xy[(round(float(xy[0]), 1), round(float(xy[1]), 1))] = b

    # ------------------------------------------------------------------
    def _label_of(self, nid: str) -> str:
        n = self.nodes.get(nid) or {}
        return (n.get("text") or n.get("label") or "").strip()

    def _bbox_of(self, nid: str):
        b = (self.nodes.get(nid) or {}).get("bbox")
        return tuple(b) if b else None

    def _pred_var(self, nid: str) -> dict:
        """把一个阈值块描述成谓词变量（模拟段抽象为谓词，架构方案 §5.2.4）。"""
        s = self.sem.get(nid) or {}
        label = self._label_of(nid)
        sp = self._setpoint_of(nid)
        return {
            "var_id": nid, "label": label, "cls": s.get("cls"),
            "block": "HI_MONITOR" if str(label).startswith("H") else
                     ("LO_MONITOR" if str(label).startswith("/") or "L" in str(label)
                      else s.get("cls")),
            "setpoint": sp.get("value"), "unit": sp.get("unit"),
            "setpoint_status": sp.get("status"),
            "xu_tag": sp.get("xu_tag"), "branch": sp.get("branch"),
            "meaning": (f"{label} 输入越过定值" if "HI" in str(s.get('cls'))
                        else f"{label} 输入低于定值"),
        }

    def _setpoint_of(self, nid: str) -> dict:
        """定值绑定：优先按包围盒匹配 threshold_bindings，其次按标签。
        找不到 → status='missing'（对应规则 R14，不得编造数值）。"""
        bbox = self._bbox_of(nid)
        if bbox:
            for xy, b in self.tb_by_xy.items():
                if abs(xy[0] - bbox[0]) < 6 and abs(xy[1] - bbox[1]) < 6:
                    return {"value": None, "unit": None, "status": "bound_no_value",
                            "xu_tag": b.get("tag"), "branch": (b.get("branch_labels") or [None])[0]}
        return {"value": None, "unit": None, "status": "missing",
                "xu_tag": None, "branch": None}

    def _closure(self, nid: str, depth: int = 8) -> set[str]:
        """上游输入锥（含自身）。"""
        seen, frontier = {nid}, {nid}
        for _ in range(depth):
            nxt: set[str] = set()
            for x in frontier:
                for e in self.in_edges.get(x, []):
                    if e["src"] not in seen:
                        nxt.add(e["src"])
            seen |= nxt
            frontier = nxt
            if not frontier:
                break
        return seen

    # ------------------------------------------------------------------
    def extract(self) -> list[Unit]:
        units: list[Unit] = []
        seen_centers: set[str] = set()

        # --- 1) 逻辑块单元（含其输入锥） ---
        for nid, s in self.sem.items():
            if not s or s["cls"] not in LOGIC_CLASSES:
                continue
            cone = self._closure(nid)
            u = self._make_unit(nid, "block", cone)
            if u:
                units.append(u)
                seen_centers.add(nid)

        # --- 2) 未接入逻辑的阈值块 → 纯谓词单元（阈值跨越测试） ---
        for nid, s in self.sem.items():
            if not s or s["cls"] not in THRESHOLD_CLASSES or nid in seen_centers:
                continue
            # 若其下游存在逻辑块，则已被该逻辑单元覆盖
            downstream_logic = any(
                (self.sem.get(e["dst"]) or {}).get("cls") in LOGIC_CLASSES
                for e in self.out_edges.get(nid, []) if self.sem.get(e["dst"]))
            if downstream_logic:
                continue
            u = self._make_unit(nid, "predicate", {nid} | self._analog_leaves(nid))
            if u:
                units.append(u)
                seen_centers.add(nid)

        # --- 3) 回路级单元（IR.loops，type=protection 优先） ---
        for lp in self.ir.get("loops") or []:
            members = lp.get("members") or []
            id_by_label = {}
            for nid, n in self.nodes.items():
                lb = self._label_of(nid)
                if lb and lb not in id_by_label:
                    id_by_label[lb] = nid
            cone = {id_by_label[m] for m in members if m in id_by_label}
            cone |= {nid for nid in cone for e in self.in_edges.get(nid, [])
                     if e["src"] in self.nodes}
            if not cone:
                continue
            center = f"loop:{lp.get('loop_id')}"
            if center in seen_centers:
                continue
            u = self._make_unit(center, "loop", cone, loop=lp)
            if u:
                units.append(u)
                seen_centers.add(center)

        return units

    def _analog_leaves(self, nid: str) -> set[str]:
        cone = self._closure(nid)
        return {x for x in cone
                if (self.sem.get(x) or {}).get("cls") in ANALOG_PASSTHRU
                or self.nodes.get(x, {}).get("cls") == "bubble"}

    def _make_unit(self, center: str, kind: str, cone: set[str],
                   loop: Optional[dict] = None) -> Optional[Unit]:
        if not cone:
            return None
        label = (loop.get("loop_id") if loop else self._label_of(center)) or center
        uid = f"p{self.page}:{kind}:{center}"
        u = Unit(unit_id=uid, kind=kind, page=self.page, center=center, label=label)
        u.nodes = {nid: self.nodes[nid] for nid in cone if nid in self.nodes}
        u.sem = {nid: self.sem.get(nid) for nid in u.nodes}
        # ★ 必须用归一化后的连线（src/dst），不能用原始 wire（from/to）——
        #   两种键名不一致会导致下游取 e["src"] 时 KeyError
        u.in_edges = [e for e in self.wires_norm
                      if e["src"] in cone and e["dst"] in cone]
        u.predicates = [nid for nid in u.nodes
                        if (u.sem.get(nid) or {}).get("cls") in THRESHOLD_CLASSES]
        u.variables = [self._pred_var(nid) for nid in u.predicates]
        u.ports = {nid: self.ports.get(nid, []) for nid in u.nodes}
        u.safety_class = [z.get("text") for z in
                          (self.ir.get("zones") or {}).get("class_labels") or []
                          if z.get("text")]
        # 输出：单元内无下游（在锥内）的节点
        has_down = {e["src"] for e in u.in_edges}
        u.outputs = [nid for nid in u.nodes if nid not in has_down]
        if loop:
            u.outputs = [x for x in u.outputs
                         if self._label_of(x) in (loop.get("members") or [])] or u.outputs

        # 诚实标注不可评估的情形
        logic = u.logic_nodes
        if not logic and not u.predicates:
            u.warnings.append("单元内无逻辑块也无阈值块，不生成用例")
        if any(v.get("setpoint_status") == "missing" for v in u.variables):
            u.warnings.append("存在未绑定定值的阈值块（R14）：谓词可枚举，"
                              "但激励措辞须标注『待补定值』，不得给出具体数值")
        stateful = [nid for nid in logic
                    if (u.sem.get(nid) or {}).get("cls") in STATEFUL_CLASSES]
        if stateful:
            u.warnings.append(f"含状态元件 {stateful}：需时序激励序列，"
                              "当前版本按组合逻辑近似并在用例中标注")
        return u
