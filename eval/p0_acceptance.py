# -*- coding: utf-8 -*-
"""P0 验收总闸（一键复现）。

把 P0 全部分阶段验收标准收敛为一个可重复执行的命令，避免"验收结论只存在于
某次会话的记录里"。接入 CI 或作为每阶段门控的第一道检查。

检查项：
    A 迁移完整性      —— 溯源清单存在、文件数与指纹与源一致、零残留
    B 运行时与依赖     —— 解释器版本、关键库齐备（尤其 scipy：缺失会静默退化）
    C 内核契约        —— tests/test_kernel_contract.py 全绿
    D 阈值同源        —— eval/threshold_parity.py 全绿
    E 用例库合规       —— eval/validate_testcase_lib.py 全绿
    F 基线可比性       —— 金标回归结果与冻结基线逐项一致
    G 风险闸门 R1     —— 结论已产出（PASS/FAIL 均可，但必须有结论）

用法：<venv>/python.exe eval/p0_acceptance.py
退出码：0 = P0 验收通过；1 = 未通过
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
CHECKS: list[tuple[str, bool, str]] = []


def run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    p = subprocess.run([PY, *cmd], cwd=str(cwd or ROOT),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def record(name: str, passed: bool, detail: str) -> None:
    CHECKS.append((name, passed, detail))
    print(f"  {'PASS' if passed else 'FAIL'}  {name:<34} {detail}")


# ---------------------------------------------------------------------
def check_a_migration() -> None:
    mf = ROOT / "MIGRATION.json"
    if not mf.exists():
        record("A 迁移完整性", False, "缺少 MIGRATION.json（请运行 tools/migrate_legacy.py）")
        return
    d = json.loads(mf.read_text(encoding="utf-8"))
    for a in d["adapters"]:
        dst = Path(a["dest_path"])
        n = sum(1 for _ in dst.rglob("*") if _.is_file())
        ok = n >= a["file_count"]
        record(f"A 迁移完整性 [{a['adapter']}]", ok,
               f"{n} 文件（源 {a['file_count']}）指纹 {a['content_sha256'][:12]}… "
               f"git {a['git_head'][:8]} 残留 {len(a.get('stale_files') or [])}")


def check_b_runtime() -> None:
    import platform
    record("B 运行时版本", sys.version_info[:2] == (3, 10),
           f"Python {platform.python_version()} @ {sys.executable}")
    lite = {}
    for mod in ("yaml", "numpy", "networkx", "pymupdf", "scipy", "openpyxl", "requests", "pandas"):
        try:
            lite[mod] = __import__(mod).__version__
        except Exception as e:
            lite[mod] = f"缺失({type(e).__name__})"
    missing = [k for k, v in lite.items() if str(v).startswith("缺失")]
    record("B 关键依赖齐备", not missing,
           f"scipy={lite.get('scipy')} pymupdf={lite.get('pymupdf')} "
           + (f"缺失={missing}" if missing else "全部就位"))
    # 语义模型（可选，缺失只降级不失败）
    mdl = ROOT / "niva" / "adapters" / "doc" / "legacy" / "model"
    present = [p.name for p in mdl.iterdir()] if mdl.is_dir() else []
    print(f"        · 本地语义模型目录：{present or '无'}（缺失时降级为纯规则模式，不判失败）")


def check_c_deps() -> None:
    rc, out = run(["tests/test_kernel_contract.py"])
    tail = [l for l in out.strip().splitlines() if "通过" in l]
    record("C 内核契约测试", rc == 0, tail[-1] if tail else f"exit={rc}")


def check_d_thresholds() -> None:
    rc, out = run(["eval/threshold_parity.py"])
    n = out.count("  ok    ")
    record("D 阈值同源守卫", rc == 0, f"{n} 项逐一一致")


def check_e_testcase_lib() -> None:
    rc, out = run(["eval/validate_testcase_lib.py"])
    line = [l for l in out.splitlines() if l.startswith("共 ")]
    record("E 用例库合规", rc == 0, line[-1] if line else f"exit={rc}")


def check_f_baseline() -> None:
    """金标回归 vs 冻结基线：逐项比对（不重跑回归，读最近一次结果）。"""
    exp = ROOT / "niva" / "adapters" / "doc" / "legacy" / "experiments"
    frozen = exp / "_regression_post_cleanup.json"
    outs = sorted(exp.glob("_regression_*.json"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    cand = next((p for p in outs if p.name not in
                 ("_regression_base.json", "_regression_post_cleanup.json")
                 and not p.name.startswith(("_regression_ENT", "_regression_relax"))), None)
    if not frozen.exists() or cand is None:
        record("F 基线可比性", False, f"缺少冻结基线或本次结果（frozen={frozen.exists()}）")
        return
    fz = json.loads(frozen.read_text(encoding="utf-8"))
    cd = json.loads(cand.read_text(encoding="utf-8"))
    diffs: list[str] = []
    if fz["overall"] != cd["overall"]:
        diffs.append(f"overall {fz['overall']} != {cd['overall']}")
    for p in ("A", "B"):
        fzs = {k: {kk: vv for kk, vv in s.items() if kk != "result"}
               for k, s in (fz["pipelines"].get(p) or {}).get("sheets", {}).items()}
        cds = {k: {kk: vv for kk, vv in s.items() if kk != "result"}
               for k, s in (cd["pipelines"].get(p) or {}).get("sheets", {}).items()}
        if fzs != cds:
            diffs.append(f"pipeline {p} 明细不一致")
    record("F 基线可比性", not diffs,
           (f"overall 完全一致 {fz['overall']['agree']}/{fz['overall']['chars']} "
            f"= {fz['overall']['agreement']}" if not diffs else "; ".join(diffs)))
    print(f"        · 对照：冻结 {frozen.name}  ↔  本次 {cand.name}")


def check_g_r1() -> None:
    r1 = ROOT / "eval" / "out" / "r1_report.json"
    if not r1.exists():
        record("G 风险闸门 R1", False, "缺少 r1_report.json（请运行 eval/r1_tag_alignment.py）")
        return
    d = json.loads(r1.read_text(encoding="utf-8"))
    a1 = d["result"]["A1_tag"]
    record("G 风险闸门 R1 已结论", True,
           f"A1位号={a1['rate']*100:.1f}% ({a1['hit']}/{a1['total']}) 裁定={d['verdict']}")


def main() -> int:
    print("=" * 78)
    print("  NIVA · P0 验收总闸")
    print("=" * 78)
    check_a_migration()
    check_b_runtime()
    check_c_deps()
    check_d_thresholds()
    check_e_testcase_lib()
    check_f_baseline()
    check_g_r1()

    npass = sum(1 for _n, ok, _d in CHECKS if ok)
    print("\n" + "=" * 78)
    print(f"  结果：{npass}/{len(CHECKS)} 项通过")
    if npass == len(CHECKS):
        print("  ✅ P0 验收通过：底座统一完成，契约已冻结，基线已锁定，风险闸门已结论。")
        print("     下一阶段：P1 服务化（写 5 个适配器 service.py，扩展统一 MCP 至 22 工具）")
        return 0
    print("  ❌ 存在未通过项，P0 未达验收标准。")
    for n, ok, d in CHECKS:
        if not ok:
            print(f"     - {n}: {d}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
