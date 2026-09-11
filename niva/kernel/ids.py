# -*- coding: utf-8 -*-
"""NIVA 统一 ID 规范（**已冻结** — P0 交付物）。

对应架构方案 §2.4。三条设计意图：
    1. 全局唯一命名空间 —— 异构制品（文本条目 / 图纸功能块 / 用例）精确互引，
       消除"同一条需求在两个子系统里是两套 ID"的整合风险。
    2. 跨文档容错归一 —— ``id_core()`` 逐字复用 Trace_NL
       ``core/traceability_matrix.py::_id_core``（L407）的既有行为：
       ``<FZSDCS34-SyRS005>`` 与 ``<DCS-SyRS005>`` 归一到同一核心键。
       **这是有实测背书的策略，不重新发明。**
    3. 位号归一 —— ``normalize_tag_key()`` 逐字复用 SAMA-V1
       ``kb_io.py::_norm_tag_key``（L19）的行为：``YTSM007MP`` 与 ``MP007``
       同键。图文桥接的 A1 锚点就建立在这条规则上。

本模块只依赖标准库（kernel 层纪律）。
"""
from __future__ import annotations

import re

__all__ = [
    "node_id_doc_req", "node_id_doc_sec", "node_id_diagram_page",
    "node_id_diagram_block", "node_id_diagram_loop", "node_id_diagram_wire",
    "node_id_testcase", "node_id_standard",
    "edge_id", "make_node_id", "id_core", "normalize_tag_key",
    "split_node_id", "is_doc_id", "is_diagram_id",
]


# =====================================================================
# 一、ID 构造器（统一命名空间）
# =====================================================================
def node_id_doc_req(doc_id: str, raw_req_id: str) -> str:
    """文档条目：doc:{doc_id}:req:{归一化ID}  →  doc:SyRS:req:dcd-syrs1"""
    return f"doc:{doc_id}:req:{normalize_req_id(raw_req_id)}"


def node_id_doc_sec(doc_id: str, section_no: str) -> str:
    """文档章节：doc:{doc_id}:sec:{章节号}  →  doc:UR:sec:3.2.2.1"""
    return f"doc:{doc_id}:sec:{str(section_no).strip()}"


def node_id_diagram_page(doc_id: str, page: int) -> str:
    """图纸页面：dia:{doc_id}:p{页号}"""
    return f"dia:{doc_id}:p{page}"


def node_id_diagram_block(doc_id: str, page: int, local_id: str) -> str:
    """图纸功能块：dia:{doc_id}:p{页号}:{node_id}"""
    return f"dia:{doc_id}:p{page}:{local_id}"


def node_id_diagram_loop(doc_id: str, loop_id: str) -> str:
    """图纸回路：dia:{doc_id}:loop:{loop_id}"""
    return f"dia:{doc_id}:loop:{loop_id}"


def node_id_diagram_wire(doc_id: str, page: int, wire_id: str) -> str:
    """图纸连线：dia:{doc_id}:p{页号}:w:{wire_id}"""
    return f"dia:{doc_id}:p{page}:w:{wire_id}"


def node_id_testcase(unit_id: str, seq: int | str) -> str:
    """测试用例：test:{unit_id}:{序号}  →  test:loop:L01:003"""
    return f"test:{unit_id}:{int(seq):03d}" if str(seq).isdigit() else f"test:{unit_id}:{seq}"


def node_id_standard(clause: str) -> str:
    """标准条款：std:{标准号}:{条款号}  →  std:NBT20448:7.2"""
    return f"std:{str(clause).strip()}"


def make_node_id(domain: str, *parts: str) -> str:
    """通用逃生口：非标准形态节点用 ``make_node_id('doc','SPEC','x:1')``。

    约束：仍须遵守 ``{域前缀}:`` 起手，否则 ``split_node_id`` 无法解析。
    """
    prefix = {"doc": "doc", "diagram": "dia", "test": "test", "standard": "std"}.get(domain, domain)
    body = ":".join(str(p) for p in parts if p not in (None, ""))
    return f"{prefix}:{body}"


def edge_id(src: str, dst: str, relation: str) -> str:
    """边 ID 由三元组确定性生成。同一关系重复写入幂等覆盖，天然去重。"""
    return f"e::{relation}::{dst}=>{src}"


# =====================================================================
# 二、跨文档归一（复用 Trace_NL 既有行为）
# =====================================================================
def normalize_req_id(raw_id: str) -> str:
    """归一化下游条目 ID（保留尖括号外壳，逐字复用 _normalize_ds_id）。

    ``< DCS-SyRS005 >`` → ``<DCS-SyRS005>``
    """
    raw = (raw_id or "").strip()
    if raw.startswith("<") and raw.endswith(">"):
        return f"<{raw[1:-1].strip()}>"
    return raw


def id_core(raw_id: str) -> str:
    """ID 核心可比较键。**逐字复用** Trace_NL ``_id_core`` 的规则与示例行为。

    ``<FZSDCS34-SyRS005>``  → ``syrs5``
    ``<DCS-SyRS005>``       → ``syrs5``
    ``<FZSDCS34-SyRS0011>`` → ``syrs11``   （零填充/笔误容错）
    ``<DCS-SyRS011>``       → ``syrs11``

    规则：取 ID 中最后一组 (字母+数字)，字母转小写、数字按整数归一。
    """
    s = (raw_id or "").strip().strip("<>").replace(" ", "").replace("\n", "")
    groups = re.findall(r"([A-Za-z]+?)(\d+)", s)
    if not groups:
        return s.lower()
    alpha, num = groups[-1]
    try:
        return f"{alpha.lower()}{int(num)}"
    except ValueError:
        return f"{alpha.lower()}{num}"


_TAG_RE = re.compile(r"^([A-Z]{1,8})(\d{2,4})([A-Z]{0,8})$")


def normalize_tag_key(tag: str) -> str:
    """位号归一化绑定键。**逐字复用** SAMA-V1 ``kb_io._norm_tag_key``。

    ``YTSM007MP`` → ``MP007``；``MP007`` → ``MP007``（同键）。
    规则：键 = 尾缀信号码 + 数字段；尾缀缺省时前缀本身即信号码。
    """
    t = re.sub(r"\s+", "", (tag or "")).upper()
    m = _TAG_RE.fullmatch(t)
    if m:
        tail = m.group(3) or m.group(1)
        return f"{tail}{m.group(2)}"
    return t


# =====================================================================
# 三、解析与判定
# =====================================================================
def split_node_id(node_id: str) -> dict[str, str]:
    """拆解 node_id 为结构化字典，供前端路由与 MCP 查询使用。

    ``dia:FN-SAMA-001:p5:MP101`` →
        ``{prefix:'dia', domain:'diagram', doc_id:'FN-SAMA-001', scope:'p5', local:'MP101'}``
    """
    parts = (node_id or "").split(":")
    if not parts or not parts[0]:
        return {"prefix": "", "domain": "", "doc_id": "", "scope": "", "local": node_id or ""}
    prefix = parts[0]
    domain = {"doc": "doc", "dia": "diagram", "test": "test", "std": "standard"}.get(prefix, prefix)
    doc_id = parts[1] if len(parts) > 1 else ""
    scope = parts[2] if len(parts) > 2 else ""
    local = ":".join(parts[3:]) if len(parts) > 3 else ""
    return {"prefix": prefix, "domain": domain, "doc_id": doc_id, "scope": scope, "local": local}


def is_doc_id(node_id: str) -> bool:
    return (node_id or "").startswith("doc:")


def is_diagram_id(node_id: str) -> bool:
    return (node_id or "").startswith("dia:")
