# -*- coding: utf-8 -*-
"""部件测试用例库校验器。

不引入 jsonschema 依赖（与项目"克制选型"一致：能用 200 行标准库解决，
就不新增一个依赖），但**校验规则由 _schema.json 驱动**——schema 是契约的
唯一事实来源，本脚本不复制一份硬编码的字段清单，避免两者漂移。

四类校验：
    1. 结构校验   —— 必填字段、case_id 形制、枚举取值（全部读自 _schema.json）
    2. 类名校验   —— cls 必须命中 knowledge/symbols/*.yml 的 entries[].cls
                     （**禁止臆造类名**：这是用例库最容易出现的错误）
    3. 唯一性校验 —— case_id 全局唯一
    4. 语义校验   —— generation 与 expected_template 的一致性：
                     enumeration/interval 的期望值必须指向真值推演，
                     不得在模板里写死数值（否则就退化成"LLM 编判据"）

用法：<venv>/python.exe eval/validate_testcase_lib.py
退出码：0 = 全部通过；1 = 存在错误
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from niva import config as C                                     # noqa: E402

try:
    import yaml
except ImportError:
    print("需要 PyYAML")
    raise SystemExit(2)

LIB_ROOT = C.KNOWLEDGE_ROOT / "testcase_lib"
SCHEMA = LIB_ROOT / "_schema.json"
SYMBOLS_DIR = C.KNOWLEDGE_ROOT / "symbols"

# 数值型期望值的兜底检测：模板里出现"期望 <数字>"即视为写死判据
HARDCODED_EXPECT = re.compile(r"期望[^。；\n]{0,12}?(\d+(?:\.\d+)?)")


def load_symbol_classes() -> set[str]:
    classes: set[str] = set()
    for f in sorted(SYMBOLS_DIR.glob("*.yml")):
        d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        for e in d.get("entries") or []:
            if e.get("cls"):
                classes.add(str(e["cls"]))
    return classes


def schema_rules() -> dict[str, Any]:
    """从 _schema.json 提取校验规则（契约驱动，不重复声明）。"""
    s = json.loads(SCHEMA.read_text(encoding="utf-8"))
    case = s["definitions"]["case"]
    props = case["properties"]
    return {
        "required": case["required"],
        "case_id_pattern": re.compile(props["case_id"]["pattern"]),
        "domain_enum": props["domain"]["enum"],
        "generation_enum": props["generation"]["enum"],
        "coverage_enum": props["coverage"]["items"]["enum"],
        "applies_when_required": props["applies_when"]["required"],
        "top_required": s["required"],
    }


def validate_file(path: Path, rules: dict, known_cls: set[str],
                  seen_ids: dict[str, str]) -> tuple[list[str], list[str]]:
    errs: list[str] = []
    warns: list[str] = []
    rel = path.relative_to(LIB_ROOT).as_posix()

    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return [f"{rel}: YAML 解析失败 —— {exc}"], warns

    if not isinstance(doc, dict):
        return [f"{rel}: 顶层应为映射（含 schema_version / lib_version / cases）"], warns
    for k in rules["top_required"]:
        if k not in doc:
            errs.append(f"{rel}: 缺少顶层字段 {k}")

    cases = doc.get("cases")
    if not isinstance(cases, list) or not cases:
        errs.append(f"{rel}: cases 必须为非空数组")
        return errs, warns

    for i, c in enumerate(cases):
        tag = f"{rel}[{i}]"
        if not isinstance(c, dict):
            errs.append(f"{tag}: 条目应为映射")
            continue
        cid = c.get("case_id", "?")
        tag = f"{rel}[{cid}]"

        for k in rules["required"]:
            if c.get(k) in (None, "", [], {}):
                errs.append(f"{tag}: 缺少必填字段 {k}")

        cid = c.get("case_id", "")
        if cid and not rules["case_id_pattern"].match(str(cid)):
            errs.append(f"{tag}: case_id 形制不符（应为 CLS-XXX-001 形态）")
        if cid:
            if cid in seen_ids:
                errs.append(f"{tag}: case_id 重复（已在 {seen_ids[cid]} 定义）")
            else:
                seen_ids[cid] = rel

        dom = c.get("domain")
        if dom is not None and dom not in rules["domain_enum"]:
            errs.append(f"{tag}: domain={dom!r} 不在 {rules['domain_enum']}")

        gen = c.get("generation")
        if gen is not None and gen not in rules["generation_enum"]:
            errs.append(f"{tag}: generation={gen!r} 不在 {rules['generation_enum']}")

        cov = c.get("coverage")
        if isinstance(cov, list):
            bad = [x for x in cov if x not in rules["coverage_enum"]]
            if bad:
                errs.append(f"{tag}: coverage 含非法取值 {bad}")
            if len(set(cov)) != len(cov):
                warns.append(f"{tag}: coverage 有重复项 {cov}")
        elif cov is not None:
            errs.append(f"{tag}: coverage 应为数组")

        aw = c.get("applies_when")
        if not isinstance(aw, dict):
            errs.append(f"{tag}: applies_when 应为映射")
        else:
            for k in rules["applies_when_required"]:
                if k not in aw:
                    errs.append(f"{tag}: applies_when 缺少 {k}")
            ports = aw.get("ports")
            if isinstance(ports, dict):
                for pk, pv in ports.items():
                    if not isinstance(pv, dict):
                        errs.append(f"{tag}: applies_when.ports.{pk} 应为映射")
                        continue
                    for kk in ("min", "max"):
                        if kk in pv and pv[kk] is not None and not isinstance(pv[kk], int):
                            errs.append(f"{tag}: applies_when.ports.{pk}.{kk} 应为整数或 null")
                    if "min" in pv and "max" in pv and pv["max"] is not None \
                            and isinstance(pv["min"], int) and pv["max"] < pv["min"]:
                        errs.append(f"{tag}: ports.{pk} 的 max < min")
            elif ports is not None:
                errs.append(f"{tag}: applies_when.ports 应为映射")

        # ---- 类名必须真实存在（核心校验） ----
        # 例外：兜底探针模板（doc 声明 probe: true）允许 cls="*" 通配，
        #       但必须携带 probe 标记，不得冒充专用模板。
        cls = c.get("cls")
        is_probe_doc = bool(doc.get("probe"))
        if cls == "*" and is_probe_doc:
            if not str(c.get("safety_notes") or ""):
                errs.append(f"{tag}: 探针模板（cls=*）必须写明 safety_notes 降级纪律")
        elif cls and cls not in known_cls:
            errs.append(f"{tag}: cls={cls!r} 不在 knowledge/symbols/*.yml 的类名集合中"
                        f"（可用：{sorted(known_cls)[:8]} …）")

        # ---- 期望值不得写死数值 ----
        exp = str(c.get("expected_template") or "")
        gen_v = str(gen or "")
        if gen_v in ("enumeration", "interval", "sampling"):
            if "真值推演" not in exp:
                errs.append(f"{tag}: generation={gen_v} 的 expected_template 必须指向真值推演，"
                            f"实际为 {exp[:40]!r}")
            m = HARDCODED_EXPECT.search(exp)
            if m:
                warns.append(f"{tag}: expected_template 出现具体数值 {m.group(1)}，"
                             "确认其仅为说明性示例而非写死判据")

        if not str(c.get("evidence_hint") or "").strip():
            warns.append(f"{tag}: 未提供 evidence_hint，R20 证据完备性校验将无法约束该模板")

    return errs, warns


def main() -> int:
    if not SCHEMA.exists():
        print(f"schema 缺失：{SCHEMA}")
        return 2
    rules = schema_rules()
    known_cls = load_symbol_classes()
    print(f"符号字典类名 {len(known_cls)} 个：{sorted(known_cls)}")
    print(f"用例库根：{LIB_ROOT}\n")

    # _families.yml 是族映射元数据（无 cases），不是用例文件，须排除；
    # _generic_probe.yml 是兜底模板，保留校验，但其 cls="*" 为显式通配（见 validate_file）。
    files = sorted(p for p in LIB_ROOT.rglob("*.yml") if p.name != "_families.yml")
    if not files:
        print("未找到任何用例模板文件")
        return 1

    all_errs: list[str] = []
    all_warns: list[str] = []
    seen_ids: dict[str, str] = {}
    per_file: list[tuple[str, int]] = []

    for f in files:
        errs, warns = validate_file(f, rules, known_cls, seen_ids)
        rel = f.relative_to(LIB_ROOT).as_posix()
        try:
            n = len((yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("cases") or [])
        except Exception:
            n = 0
        per_file.append((rel, n))
        status = "OK  " if not errs else "FAIL"
        print(f"  {status} {rel:<34} {n} 条模板"
              + (f"  ({len(warns)} 警告)" if warns else ""))
        all_errs += errs
        all_warns += warns

    print(f"\n共 {len(files)} 个文件 / {sum(n for _r, n in per_file)} 条模板 / "
          f"{len(seen_ids)} 个唯一 case_id")

    if all_warns:
        print(f"\n⚠ {len(all_warns)} 条警告：")
        for w in all_warns:
            print(f"   - {w}")
    if all_errs:
        print(f"\n❌ {len(all_errs)} 条错误：")
        for e in all_errs:
            print(f"   - {e}")
        return 1
    print("\n✅ 用例库校验全部通过：结构合规、类名真实、case_id 唯一、期望值不写死。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
