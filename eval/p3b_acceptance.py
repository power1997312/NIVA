# -*- coding: utf-8 -*-
"""P3-B 验收 · 模板检索 + 语义成文三层校验 + 用例渲染。

检查项
    A  工具面        22 工具 / 18 已实现 / 4 待实现（图谱族属 P2/P5）
    B  精确命中       HI_MONITOR → ANA-HI_LIMIT-002（exact）
    C  同族回退       LO_MONITOR → LO_LIMIT（family），且方向差异被显式警示
    D  硬冲突否决     stateful 不一致的模板被淘汰
    E  通用探针兜底   无精确/同族命中的块 → probe 模板，probe=True 显式标注
    F  真实图纸生成   IR → 单元 → 检索 → 推演 → 成文 → 用例集，且统计口径自洽
    G  ★红线校验      用例里的期望值与真值推演结果**逐字一致**（LLM 未触碰数值）
    H  渲染           xlsx 产出；docx 显式降级并说明原因（不假装支持）
    I  对外白名单     生成族工具不出现在对外只读白名单（方案 §7.4）
    J  三层校验       期望值篡改 / 步骤增删 / 臆造位号 三种越界都必须被拒并回退模板

用法：<venv>/python.exe eval/p3b_acceptance.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from niva import config as C                                        # noqa: E402
from niva.kernel.registry import ErrorCode                          # noqa: E402
from niva.server.tools import build_registry, external_tools        # noqa: E402
from niva.services.testcase import narrate as NARR                  # noqa: E402
from niva.services.testcase.generate import generate_cases          # noqa: E402
from niva.services.testcase.template_match import TemplateLibrary   # noqa: E402
from niva.services.testcase.unit_extract import UnitExtractor       # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
LIB = TemplateLibrary()


def rec(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'PASS' if passed else 'FAIL'}  {name:<26} {detail}")


def _first_unit(ir, page, pred=None):
    for u in UnitExtractor(ir, page=page).extract():
        if pred is None or pred(u):
            return u
    return None


def check_a_toolface(reg) -> None:
    m = reg.manifest()
    pend = m["pending_implementations"]
    ok = m["total"] == 22 and set(pend) <= {"xdiff_trace"}
    rec("A 工具面", ok, f"total={m['total']} 已实现={m['total']-len(pend)} 待实现={pend}")
    perm = m["permissions"]["scopes"]["generate"]
    rec("A 生成族 LLM 策略", perm["llm_permission"] == "narrate_only",
        f"generate.llm_permission={perm['llm_permission']}")


def check_b_exact(ir) -> None:
    """HI_MONITOR 块（真实存在，页 5 有 28 处）应精确命中 ANA-HI_LIMIT-002。"""
    u = _first_unit(ir, 5, lambda u: (u.sem.get(u.center) or {}).get("cls") == "HI_MONITOR")
    if u is None:
        rec("B 精确命中", False, "页 5 未找到 HI_MONITOR 中心单元")
        return
    m = LIB.match_unit(u)
    tid = (m.template or {}).get("case_id")
    rec("B 精确命中", m.match_kind == "exact" and tid == "ANA-HI_LIMIT-002",
        f"cls=HI_MONITOR → {tid} kind={m.match_kind} score={m.score}")


def check_c_family(ir) -> None:
    """LO_MONITOR 在库中无精确命中 → 应回退同族，且方向差异被警示。"""
    u = _first_unit(ir, 5, lambda u: (u.sem.get(u.center) or {}).get("cls") == "LO_MONITOR")
    if u is None:
        u = _first_unit(None, None, lambda u: (u.sem.get(u.center) or {}).get("cls") == "LO_MONITOR") \
            if False else None
    if u is None:
        # 在全部页里找
        from niva.adapters.diagram.service import DiagramAdapter
        d = DiagramAdapter()
        for p in range(1, 10):
            irr = d.load_ir(p)
            if not irr:
                continue
            u = _first_unit(irr, p,
                            lambda x: (x.sem.get(x.center) or {}).get("cls") == "LO_MONITOR")
            if u:
                break
    if u is None:
        rec("C 同族回退", False, "未找到 LO_MONITOR 单元")
        return
    m = LIB.match_unit(u)
    warn = any("方向不同" in w for w in m.warnings)
    rec("C 同族回退", m.match_kind == "family" and warn,
        f"cls=LO_MONITOR → {(m.template or {}).get('case_id')} kind={m.match_kind} "
        f"方向警示={warn}")


def check_c_family() -> None:
    """同族回退：LO_MONITOR 无精确模板 → 应回退 LO_LIMIT（同族）并警示方向差异。

    用受控端口画像直测检索器（1 入 1 出），不受真实图纸上该块端口数影响。
    """
    ports = [{"name": "IN", "dir": "in", "kind": "ANALOG"},
             {"name": "OUT", "dir": "out", "kind": "BOOLEAN"}]
    m = LIB.match("u-fam", "n1", "LO_MONITOR", stateful=False,
                  domain="MIXED", ports=ports)
    warn = any("方向不同" in w for w in m.warnings)
    rec("C 同族回退", m.match_kind == "family" and warn,
        f"LO_MONITOR → {(m.template or {}).get('case_id')} kind={m.match_kind} "
        f"score={m.score} 方向警示={warn}")


def check_d_veto() -> None:
    """硬冲突一票否决：被否决的候选及其原因必须**可追溯**（不静默丢弃）。"""
    m = LIB.match("u-v1", "n1", "SR", stateful=False, domain="BOOLEAN")
    sr_veto = any(r.get("cls") == "SR" and any("stateful" in v for v in r["veto"])
                  for r in m.rejected)
    rec("D 硬冲突否决(stateful)", sr_veto,
        f"SR 候选因 stateful 冲突被否决并记录在案（同族 RESET 顶上，结果={m.match_kind}）")

    m2 = LIB.match("u-v2", "n2", "AND", stateful=False, domain="ANALOG")
    dom_veto = any(any("域不一致" in v for v in r["veto"]) for r in m2.rejected)
    rec("D 硬冲突否决(域)", m2.match_kind == "probe" and dom_veto,
        f"结果={m2.match_kind}；域否决记录={dom_veto}")

    m3 = LIB.match("u-v3", "n3", "HI_MONITOR", stateful=False, domain="MIXED")
    rec("D 反例：MIXED 不误杀", m3.match_kind == "exact",
        f"HI_MONITOR(MIXED) → {m3.match_kind} {(m3.template or {}).get('case_id')}")

    # 真实图纸上的端口否决：LO_MONITOR 实际 2 入，超出 LO_LIMIT 模板上限 1
    m4 = LIB.match("u-v4", "n4", "LO_MONITOR", stateful=False, domain="MIXED",
                   ports=[{"name": "TL", "dir": "in", "kind": "BOOLEAN"},
                          {"name": "T", "dir": "in", "kind": "BOOLEAN"},
                          {"name": "B", "dir": "out", "kind": "BOOLEAN"}])
    port_veto = any(any("端口数" in v for v in r["veto"]) for r in m4.rejected)
    rec("D 硬冲突否决(端口基数)", m4.match_kind == "probe" and port_veto,
        f"结果={m4.match_kind}；端口否决记录={port_veto}")


def check_e_probe() -> None:
    """库中无 SUMMER/IM 模板 → 必须走通用探针并显式标注 probe=true。

    （纯模拟运算块本就不构成可测单元，故直测检索器而非依赖真实图纸单元。）
    """
    for cls in ("SUMMER", "IM"):
        m = LIB.match(f"u-probe-{cls}", "n1", cls, stateful=False, domain="ANALOG")
        ok = m.match_kind == "probe" and bool((m.template or {}).get("_probe"))
        rec(f"E 通用探针兜底({cls})", ok,
            f"kind={m.match_kind} probe={(m.template or {}).get('_probe')} "
            f"score={m.score}")


def check_f_g_generate(reg) -> None:
    """真实图纸端到端生成 + 红线校验。"""
    import inspect
    svc = reg.get("generate_test_case").handler.__closure__ is None
    # 直接经 registry 调用（走统一契约与校验）
    r = reg.call("generate_test_case", {"page": 1, "use_llm": False})
    if not r.ok:
        rec("F 真实图纸生成", False, (r.error_msg or "")[:160])
        return
    sid = r.data["case_set_id"]
    st = r.data["stats"]
    rec("F 真实图纸生成", r.data["stats"]["cases"] >= 1,
        f"case_set_id={sid} 统计={json.dumps(st, ensure_ascii=False)}")
    for w in r.data.get("warnings") or []:
        print(f"        · 警告：{w[:120]}")

    path = C.ARTIFACTS_DIR / "cases" / f"{sid}.json"
    cs = json.loads(path.read_text(encoding="utf-8"))
    cases = cs.get("cases") or []
    if not cases:
        rec("G 红线校验(期望值未被触碰)", False, "无用例可比对")
        return

    # G：逐条重算真值，比对用例中的期望值
    bad = []
    for c in cases:
        u = _reload_unit(c)
        if u is None:
            continue
        from niva.services.testcase.truth_propagate import evaluate_unit
        t = evaluate_unit(u)
        algo_expect = [s.get("expected") for s in
                       NARR._algo_steps(u, _tpl_stub(c), t)]
        if algo_expect != [s.get("expected") for s in c.get("steps") or []]:
            bad.append(c["case_id"])
    rec("G 红线校验(期望值未被触碰)", not bad,
        f"比对 {len(cases)} 条用例的逐步期望值，不一致={bad or '无'}")


def _tpl_stub(c: dict):
    """为重算真值提供模板壳（只用到 principle/coverage/standard_ref）。"""
    class _T:
        template = {"principle": c.get("principle", ""),
                    "coverage": c.get("coverage") or [],
                    "standard_ref": c.get("standard_ref", "")}
    return _T()


def _reload_unit(c: dict):
    from niva.adapters.diagram.service import DiagramAdapter
    d = DiagramAdapter()
    ir = d.load_ir(int(c["page"]))
    if not ir:
        return None
    for u in UnitExtractor(ir, page=int(c["page"])).extract():
        if u.unit_id == c["unit_id"]:
            return u
    return None


def check_h_render(reg) -> None:
    r = reg.call("generate_test_case", {"page": 1, "use_llm": False})
    sid = (r.data or {}).get("case_set_id")
    r2 = reg.call("render_test_doc", {"case_set_id": sid, "fmt": "xlsx"})
    ok = r2.ok and Path(r2.data["xlsx_path"]).exists() and r2.data["bytes"] > 0
    rec("H 渲染 xlsx", ok,
        f"{r2.data.get('xlsx_path')} {r2.data.get('bytes')} 字节" if r2.ok
        else (r2.error_msg or "")[:120])
    r3 = reg.call("render_test_doc", {"case_set_id": sid, "fmt": "docx"})
    ok2 = r3.ok and r3.degraded and "docx" in (r3.degrade_reason or "")
    rec("H docx 显式降级", ok2, f"degrade_reason={(r3.degrade_reason or '')[:80]}")


def check_i_external(reg) -> None:
    ext = {t["name"] for t in external_tools(reg)}
    gen = set(reg.by_family("generate").__len__() * [""] )  # placeholder
    leaked = ext & {"extract_logic_paths", "match_component_library",
                    "generate_test_case", "render_test_doc"}
    rec("I 对外白名单不含生成族", not leaked, f"泄露={leaked or '无'}；对外共 {len(ext)} 个")


def check_j_three_layer(ir) -> None:
    """三层校验：用桩 LLM 注入三种越界输出，必须全部被拒并回退模板。"""
    u = _first_unit(ir, 5, lambda x: (x.sem.get(x.center) or {}).get("cls") == "HI_MONITOR")
    if u is None:
        rec("J 三层校验", False, "未找到可测单元")
        return
    from niva.services.testcase.truth_propagate import evaluate_unit
    tpl = LIB.match_unit(u)
    truth = evaluate_unit(u)
    algo = NARR._algo_steps(u, tpl, truth)
    good = {"purpose": "验证阈值块越限动作", "preconditions": "无闭锁",
            "steps": [{"no": s["no"], "action": s["action"], "expected": s["expected"]}
                      for s in algo],
            "acceptance_criteria": "实测与期望一致"}
    good["steps"][0]["action"] += "（措辞已润色）"

    class FakeProv:
        def __init__(self, data): self._d = data; self.is_available = True
        def chat(self, *a, **k):
            class R: pass
            r = R(); r.ok = True; r.data = self._d; r.cached = False
            r.model = "fake"; r.prompt_sha = "test"
            r.degrade_reason = ""
            return r

    orig = NARR.llm_provider
    try:
        # ① 合规输出 → 采用 LLM
        NARR.llm_provider = lambda *a, **k: FakeProv(good)
        n1 = NARR.narrate_case(u, tpl, truth, use_llm=True)
        ok1 = n1.source == "llm"

        # ② 篡改期望值 → 拒绝
        bad1 = json.loads(json.dumps(good))
        bad1["steps"][0]["expected"] = "输出为 1（LLM 编造）"
        NARR.llm_provider = lambda *a, **k: FakeProv(bad1)
        n2 = NARR.narrate_case(u, tpl, truth, use_llm=True)
        ok2 = n2.source == "template_fallback" and "期望值" in (n2.validation.get("reason") or "")

        # ③ 增删步骤 → 拒绝
        bad2 = json.loads(json.dumps(good))
        bad2["steps"] = bad2["steps"][:max(1, len(bad2["steps"]) - 1)]
        NARR.llm_provider = lambda *a, **k: FakeProv(bad2)
        n3 = NARR.narrate_case(u, tpl, truth, use_llm=True)
        ok3 = n3.source == "template_fallback" and "步骤数不一致" in (n3.validation.get("reason") or "")

        # ④ 臆造位号 → 拒绝
        bad3 = json.loads(json.dumps(good))
        bad3["acceptance_criteria"] = "依据 ABCD1234 信号确认"
        NARR.llm_provider = lambda *a, **k: FakeProv(bad3)
        n4 = NARR.narrate_case(u, tpl, truth, use_llm=True)
        ok4 = n4.source == "template_fallback" and "实体存在性" in (n4.validation.get("reason") or "")

        rec("J 三层校验", ok1 and ok2 and ok3 and ok4,
            f"合规采纳={ok1} 篡改拒={ok2} 增删步拒={ok3} 臆造位号拒={ok4}")
    finally:
        NARR.llm_provider = orig


def main() -> int:
    print("=" * 78)
    print("  NIVA · P3-B 验收（模板检索 + 三层校验 + 用例渲染）")
    print("=" * 78)
    reg = build_registry()
    check_a_toolface(reg)

    from niva.adapters.diagram.service import DiagramAdapter
    d = DiagramAdapter()
    ir5 = d.load_ir(5) or {}
    check_b_exact(ir5)
    check_c_family()
    check_d_veto()
    check_e_probe()
    check_f_g_generate(reg)
    check_h_render(reg)
    check_i_external(reg)
    check_j_three_layer(ir5)

    npass = sum(1 for _n, ok, _d in RESULTS if ok)
    print("\n" + "=" * 78)
    print(f"  结果：{npass}/{len(RESULTS)} 项通过")
    if npass == len(RESULTS):
        print("  ✅ 「给定图纸 → 单元 → 模板 → 真值 → 用例初稿 → 交付文档」全链路打通；")
        print("     期望值全程由确定性推演产生，LLM 越界三种情形均被程序拒绝。")
        return 0
    print("  ❌ 未通过项：")
    for n, ok, dd in RESULTS:
        if not ok:
            print(f"     - {n}: {dd}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
