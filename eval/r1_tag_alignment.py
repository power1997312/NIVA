# -*- coding: utf-8 -*-
"""R1 风险闸门 · 图文锚点可对齐率验证（架构方案 §11 R1 / §13 第 1 条）。

**这是整个 NIVA 项目最大的技术风险验证，必须最先做。**

要回答的问题
------------
"图纸上的一切都有位号（Tag），需求/设计文档在描述设备时也引用位号，
 所以位号是把文与图缝起来的唯一现实锚点" —— 这个假定成立吗？

四类锚点分别测（架构方案 §5.1）
-------------------------------
    A1 位号（Tag）         MP101 / 401XU1 / YTSM007MP 类，逐字命中
    A2 图名/系统号/图号      CAM101MP安全壳大气压力 / YCAM / 图号
    A2b 系统代号与中文系统名  图纸系统 ↔ 文档缩略语表（CAM ↔ 安全壳大气监测系统）
    A3 回路号 / 跨页引用     L01 / SH.18_4 / RRP FD SH18
    A4 功能叙述关键词        安全壳压力高 / 停堆 / 保护

判定规则（架构方案 §11 R1 预设）
--------------------------------
    A1 ≥ 30%  → 按原计划走"位号为主锚点 + 语义为辅"
    A1 < 30%  → 桥接重心转向"系统代号 + 功能语义"，P2 改路线

同时输出**语料完备性告警**：若文档为节选（如"页码 8/90"），可对齐率会系统性偏低，
结论必须带此前提，不得把"节选语料上的 0%"外推为"真实工程中不可对齐"。

输出：eval/out/r1_report.json + 控制台摘要
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from niva import config as C                                     # noqa: E402
from niva.kernel.ids import normalize_tag_key                    # noqa: E402

OUT_DIR = ROOT / "eval" / "out"
IR_PATH = C.DIAGRAM_LEGACY / "out" / "L3" / "ir_all.json"

DOC_FILES = {
    "用户需求": ["DCS设备技术规格书.pdf", "RPS系统需求规范书.pdf"],
    "系统需求": ["DCS需求说明书.pdf"],
    "系统设计": ["仪控报警.pdf", "总体方案.pdf"],
}

TAG_LOOP = re.compile(r"^([A-Z]{1,6})(\d{2,4})([A-Z]{0,4})$")
TAG_XU = re.compile(r"^([A-Z]?\d{1,4})XU(\d)(?:\.([LH]\d?))?$")
TAG_PREFIXED = re.compile(r"\b([A-Z]{2,6}\d{2,4}[A-Z]{0,4})\b")
# 缩略语表：代码 行 中文名（如 "6\nCAM\n安全壳大气监测系统"）
ABBREV = re.compile(r"\b([A-Z]{2,8})\s*\n\s*([\u4e00-\u9fff][\u4e00-\u9fff（）()/、A-Za-z]{1,24})")
# 节选检测
PAGE_OF = re.compile(r"页码[：:]\s*(\d+)\s*/\s*(\d+)")


# ---------------------------------------------------------------------
def extract_pdf(path: Path) -> tuple[str, int]:
    import pymupdf
    doc = pymupdf.open(str(path))
    try:
        return "\n".join(p.get_text("text") for p in doc), doc.page_count
    finally:
        doc.close()


def load_corpus() -> dict[str, dict]:
    corpus: dict[str, dict] = {}
    for level, files in DOC_FILES.items():
        for fn in files:
            p = C.DOC_DATA_DIR / level / fn
            if not p.exists():
                print(f"  ! 缺失：{p}")
                continue
            try:
                txt, pages = extract_pdf(p)
            except Exception as exc:
                print(f"  ! 解析失败 {fn}: {exc}")
                continue
            corpus[f"{level}/{fn}"] = {"text": txt, "pages": pages, "level": level}
    return corpus


def detect_partial(corpus: dict[str, dict]) -> list[dict]:
    """检测语料是否为节选：文档内标注的"页码 x/y"中 y 远大于实际页数即为节选。"""
    warn = []
    for doc_id, v in corpus.items():
        for m in PAGE_OF.finditer(v["text"]):
            cur, total = int(m.group(1)), int(m.group(2))
            if total > v["pages"] * 1.5:
                warn.append({"doc": doc_id, "marked_page": f"{cur}/{total}",
                             "pages_present": v["pages"],
                             "note": f"文档自述共 {total} 页，语料仅有 {v['pages']} 页"})
                break
    return warn


def collect_anchors(ir: dict) -> dict[str, set[str]]:
    a: dict[str, set[str]] = {"A1_tag": set(), "A2_ref": set(), "A2b_sys": set(),
                              "A3_id": set(), "A4_term": set()}
    for page in ir.get("pages", []):
        meta = page.get("meta") or {}
        tb = meta.get("titleblock") or {}
        for key in ("图名", "系统号", "图号"):
            v = tb.get(key)
            if not v:
                continue
            a["A2_ref"].add(str(v).strip())
            for m in TAG_PREFIXED.finditer(str(v).upper()):
                a["A1_tag"].add(m.group(1))
        # 系统号 YCAM → 去区域前缀得 CAM；图名中文尾部作系统名
        sysno = str(tb.get("系统号") or "").strip().upper()
        if sysno:
            a["A2b_sys"].add(sysno)
            a["A2b_sys"].add(re.sub(r"^[A-Z]", "", sysno) or sysno)
        tname = str(tb.get("图名") or "").strip()
        cn = re.findall(r"[\u4e00-\u9fff]{2,}", tname)
        a["A2b_sys"].update(cn)

        for z in (page.get("zones") or {}).get("class_labels") or []:
            t = (z.get("text") or "").strip()
            if t:
                a["A4_term"].add(t)

        for n in (page.get("graph") or {}).get("nodes") or []:
            for field in ("text", "label"):
                v = (n.get(field) or "").strip()
                if not v:
                    continue
                up = re.sub(r"\s+", "", v).upper()
                if TAG_LOOP.fullmatch(up) or TAG_XU.fullmatch(up):
                    a["A1_tag"].add(up)
                for m in TAG_PREFIXED.finditer(up):
                    a["A1_tag"].add(m.group(1))

        for lp in page.get("loops") or []:
            lid = (lp.get("loop_id") or "").strip()
            if lid:
                a["A3_id"].add(lid)
            src = (lp.get("source") or "").strip()
            if src:
                a["A1_tag"].add(src.upper())

        sb = page.get("semantic_bindings") or {}
        for b in sb.get("threshold_bindings") or []:
            t = (b.get("tag") or "").strip().upper()
            if t:
                a["A1_tag"].add(t)
        for b in sb.get("offpage_groups") or []:
            for k in ("rrp_ref", "action_block"):
                v = (b.get(k) or "").strip()
                if v:
                    a["A3_id"].add(v)
                    if k == "action_block":
                        a["A4_term"].add(v)
        for b in sb.get("fanout") or []:
            v = (b.get("action") or "").strip()
            if v:
                a["A4_term"].add(v)
        for cp in page.get("cross_page_refs") or []:
            if isinstance(cp, dict):
                for k in ("ref", "target", "seq_no"):
                    if cp.get(k):
                        a["A3_id"].add(str(cp[k]).strip())
    a["A2b_sys"].discard("")
    return a


def _norm(s: str) -> str:
    return re.sub(r"[\s\-_/]", "", (s or "").upper())


def _hit(text_upper: str, needle: str, loose: bool = True) -> bool:
    n = _norm(needle)
    if not n:
        return False
    if n in _norm(text_upper):
        return True
    return False


def evaluate(anchors: dict[str, set[str]], corpus: dict[str, dict]) -> dict:
    result: dict[str, dict] = {}
    for kind, items in anchors.items():
        rows = []
        for raw in sorted(items):
            probes = {raw}
            if kind == "A1_tag":
                k = normalize_tag_key(raw)
                if k != raw:
                    probes.add(k)
            hits: list[str] = []
            for doc_id, v in corpus.items():
                tu = v["text"].upper()
                if any(_hit(tu, p) for p in probes):
                    hits.append(doc_id)
            rows.append({"anchor": raw, "hit": bool(hits), "docs": hits})
        total = len(rows)
        hit = sum(1 for r in rows if r["hit"])
        # 按文档层级拆分命中率（关键：说明"对齐发生在哪一层"）
        by_level: dict[str, int] = {}
        for r in rows:
            if not r["hit"]:
                continue
            for d in r["docs"]:
                lv = corpus[d]["level"]
                by_level[lv] = by_level.get(lv, 0) + 1
        result[kind] = {"total": total, "hit": hit,
                        "rate": round(hit / total, 4) if total else 0.0,
                        "hit_by_level": by_level, "rows": rows}
    return result


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if not IR_PATH.exists():
        print(f"IR 不存在：{IR_PATH}")
        return 2

    ir = json.loads(IR_PATH.read_text(encoding="utf-8"))
    print(f"IR：{IR_PATH.name}  页数={len(ir.get('pages', []))}")

    print("\n[1/4] 抽取文档语料 …")
    corpus = load_corpus()
    for k, v in corpus.items():
        print(f"    {k:<32} {len(v['text']):>8,} 字符 / {v['pages']} 页")

    print("\n[2/4] 语料完备性检查 …")
    partial = detect_partial(corpus)
    if partial:
        for w in partial:
            print(f"    ⚠ 节选：{w['doc']}  自述 {w['marked_page']}，"
                  f"实际仅 {w['pages_present']} 页")
    else:
        print("    未检出节选标记")

    print("\n[3/4] 抽取图纸侧锚点 …")
    anchors = collect_anchors(ir)
    for k, v in anchors.items():
        print(f"    {k:<10} {len(v)} 个")

    print("\n[4/4] 计算可对齐率 …")
    res = evaluate(anchors, corpus)

    labels = {"A1_tag": "A1 位号（主锚点假定）",
              "A2_ref": "A2 图名/系统号/图号",
              "A2b_sys": "A2b 系统代号/中文系统名",
              "A3_id": "A3 回路号/跨页引用",
              "A4_term": "A4 功能叙述关键词"}
    print("\n" + "=" * 68)
    for kind in ("A1_tag", "A2_ref", "A2b_sys", "A3_id", "A4_term"):
        r = res[kind]
        lv = "  ".join(f"{k}:{v}" for k, v in sorted(r["hit_by_level"].items()))
        print(f"  {labels[kind]:<24} {r['hit']:>3}/{r['total']:<3} = {r['rate']*100:5.1f}%"
              f"   {('→ ' + lv) if lv else ''}")
    print("=" * 68)

    rate = res["A1_tag"]["rate"]
    sysrate = res["A2b_sys"]["rate"]
    semrate = res["A4_term"]["rate"]

    if rate >= 0.30:
        verdict = "PASS"
        route = ("位号可对齐率达标 → 按原计划走『位号为主锚点 + 语义为辅』，"
                 "P2 桥接层技术路线不变。")
    elif sysrate >= 0.30 or semrate >= 0.30:
        verdict = "FAIL-A1 / PASS-A2bA4"
        route = ("**位号级锚点在文档域不成立**（需求/设计文档处于功能与系统抽象层，"
                 "不引用仪表位号）；但系统代号与功能语义级锚点可用 → "
                 "P2 桥接层重心从 A1 位号转向 **A2b 系统代号 + A4 功能语义**，"
                 "位号锚点降级为『图纸域内部与 IO/定值知识层专用』，"
                 "不得作为需求文档 ↔ 图纸的桥接依据。")
    else:
        verdict = "FAIL"
        route = ("四路锚点均不足 → 图文桥接在现有语料上不可行，"
                 "P2 需重新评估（建议先取得完整文档与同项目图纸再复测）。")

    print(f"\n  结论：{verdict}")
    print(f"  {route}")

    if partial:
        print(f"\n  ⚠ 重要前提：{len(partial)} 份文档为节选，"
              "上述比率系统性偏低，不可外推为真实工程结论。")

    miss = [r["anchor"] for r in res["A1_tag"]["rows"] if not r["hit"]]
    print(f"\n  A1 未命中位号 {len(miss)} 个，前 20：{miss[:20]}")
    syshit = [r["anchor"] for r in res["A2b_sys"]["rows"] if r["hit"]]
    print(f"  A2b 命中的系统锚点：{syshit}")
    semhit = [r["anchor"] for r in res["A4_term"]["rows"] if r["hit"]]
    print(f"  A4  命中的功能词：{semhit}")

    report = {
        "gate": "R1 图文锚点可对齐率",
        "ir_path": str(IR_PATH),
        "ir_pages": len(ir.get("pages", [])),
        "corpus": {k: {"chars": len(v["text"]), "pages": v["pages"], "level": v["level"]}
                   for k, v in corpus.items()},
        "partial_corpus_warnings": partial,
        "anchors": {k: sorted(v) for k, v in anchors.items()},
        "result": res,
        "verdict": verdict,
        "route_decision": route,
        "a1_threshold": 0.30,
        "generated_by": "eval/r1_tag_alignment.py",
    }
    out = OUT_DIR / "r1_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  报告：{out}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
