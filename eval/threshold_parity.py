# -*- coding: utf-8 -*-
"""阈值同源守卫（threshold parity guard）。

解决的问题
----------
架构方案 P0 要求把 Trace_NL 的阈值从 ``config.py`` 抽到
``niva/knowledge/thresholds.yml``。但**直接改 legacy config.py 会动摇基线**——
金标回归（experiments/regression.py）的字符级颜色一致率正是这些阈值的函数，
改源码后一旦数值有毫厘之差，基线就失去可比性。

因此采取"**单一事实来源 + 同源守卫**"策略：
    - NIVA 自身的所有模块只读 ``thresholds.yml``（SSOT）；
    - legacy 仍读自己的 ``config.py``（保证基线逐字节可复现）；
    - 本脚本断言**两者数值逐一相等**，任何漂移立即失败。

这样既达成"阈值可统一调参"的整合目标，又不承担改动判定链的风险。
将本脚本接入 CI / 每次 P0–P6 阶段门控。

用法：
    <venv>/python.exe eval/threshold_parity.py
退出码：0 = 同源；1 = 存在漂移（并列出差异项）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from niva import config as C                                     # noqa: E402

# thresholds.yml 键 ↔ legacy config.py 常量名
# （键名取自 config.py L155–194，逐条对齐）
PAIRS: list[tuple[str, str]] = [
    ("doc.exact_char",                 "EXACT_CHAR_THRESHOLD"),
    ("doc.embedding_exact",            "EMBEDDING_EXACT_THRESHOLD"),
    ("doc.embedding_semantic",         "EMBEDDING_SEMANTIC_THRESHOLD"),
    ("doc.embedding_unmatched",        "EMBEDDING_UNMATCHED_THRESHOLD"),
    ("doc.nli_entailment",             "NLI_ENTAILMENT_THRESHOLD"),
    ("doc.nli_neutral_embedding",      "NLI_NEUTRAL_EMBEDDING_THRESHOLD"),
    ("doc.green_path2_char_jaccard",   "GREEN_PATH2_CHAR_JACCARD"),
    ("doc.green_path2_lcs",            "GREEN_PATH2_LCS"),
    ("doc.green_len_ratio",            "GREEN_LEN_RATIO"),
    ("doc.green_fast_maxlen",          "GREEN_FAST_MAXLEN"),
    ("doc.green_fallback_embed",       "GREEN_FALLBACK_EMBED"),
    ("doc.green_fallback_minlen",      "GREEN_FALLBACK_MINLEN"),
    ("doc.green_fallback_rawratio",    "GREEN_FALLBACK_RAWRATIO"),
    ("doc.nli_contradiction",          "NLI_CONTRADICTION_THRESHOLD"),
    ("doc.blue_nli_char_jaccard",      "BLUE_NLI_CHAR_JACCARD"),
    ("doc.nli_neutral",                "NLI_NEUTRAL_THRESHOLD"),
    ("doc.blue_neutral_char_jaccard",  "BLUE_NEUTRAL_CHAR_JACCARD"),
    ("doc.blue_2c_embed",              "BLUE_2C_EMBED"),
    ("doc.blue_2c_lcs",                "BLUE_2C_LCS"),
    ("doc.blue_2c_char_jaccard",       "BLUE_2C_CHAR_JACCARD"),
    ("doc.blue_2d_embed",              "BLUE_2D_EMBED"),
    ("doc.blue_2d_char_jaccard",       "BLUE_2D_CHAR_JACCARD"),
    ("doc.blue_2d_lcs",                "BLUE_2D_LCS"),
    ("doc.min_phrase_length",          "MIN_PHRASE_LENGTH"),
    ("doc.max_embedding_seq_length",   "MAX_EMBEDDING_SEQ_LENGTH"),
    ("llm.timeout_s",                  "LLM_TIMEOUT"),
    ("llm.max_workers",                "LLM_MAX_WORKERS"),
    ("llm.max_retry",                  "LLM_MAX_RETRY"),
    ("llm.max_tokens",                 "LLM_MAX_TOKENS"),
    ("llm.max_tokens_cap",             "LLM_MAX_TOKENS_CAP"),
    ("llm.temperature",                "LLM_TEMPERATURE"),
    ("llm.auto_confirm",               "LLM_AUTO_CONFIRM"),
    ("llm.opinion_min",                "LLM_OPINION_MIN"),
    ("llm.ds_max_chars",               "LLM_DS_MAX_CHARS"),
    ("llm.cand_max_chars",             "LLM_CAND_MAX_CHARS"),
    ("llm.untraced_cands",             "LLM_UNTRACED_CANDS"),
    ("llm.default_cands",              "LLM_DEFAULT_CANDS"),
]

# 发现层常量在 core/candidate_discovery.py，不在 config.py
DISCOVERY_PAIRS: list[tuple[str, str, str]] = [
    ("doc.discovery.theta_accept",   "THETA_ACCEPT",      "core/candidate_discovery.py"),
    ("doc.discovery.ambiguous_margin", "AMBIGUOUS_MARGIN", "core/candidate_discovery.py"),
    ("doc.discovery.max_relations",  "MAX_RELATIONS",     "core/candidate_discovery.py"),
    ("doc.discovery.secondary_ratio", "SECONDARY_RATIO",  "core/candidate_discovery.py"),
    ("doc.discovery.anchor_gate",    "ANCHOR_GATE",       "core/candidate_discovery.py"),
    ("doc.discovery.anchor_boost",   "ANCHOR_BOOST",      "core/candidate_discovery.py"),
    ("doc.discovery.anchor_scale",   "ANCHOR_SCALE",      "core/candidate_discovery.py"),
    ("doc.discovery.section_penalty", "SECTION_PENALTY",  "core/candidate_discovery.py"),
    ("doc.discovery.fold_delta",     "FOLD_DELTA",        "core/candidate_discovery.py"),
    ("doc.discovery.recall_k",       "RECALL_K",          "core/candidate_discovery.py"),
    ("doc.discovery.class_mismatch_penalty", "CLASS_MISMATCH_PENALTY",
     "core/candidate_discovery.py"),
]

DISCOVERY_WEIGHTS = [("sem", 0), ("contain", 1), ("idf", 2), ("tfidf", 3)]


def _close(a, b, tol: float = 1e-9) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return a == b


def main() -> int:
    print("阈值同源守卫：thresholds.yml  ↔  legacy config.py")
    print("=" * 66)
    drift: list[str] = []

    with C.legacy_import_path("doc"):
        import config as tn                                       # noqa: N813
        import core.candidate_discovery as cd

        print(f"\n[1/2] 判定树与 LLM 阈值（{len(PAIRS)} 项）")
        for key, const in PAIRS:
            want = C.th(key)
            got = getattr(tn, const, "<缺失>")
            if got == "<缺失>":
                drift.append(f"{key}: legacy 缺少常量 {const}")
                print(f"  MISS  {key:<28} legacy 无 {const}")
                continue
            if _close(want, got):
                print(f"  ok    {key:<28} {want!r} == {const}")
            else:
                drift.append(f"{key}: thresholds={want!r} legacy={got!r}")
                print(f"  DRIFT {key:<28} thresholds={want!r} != legacy {const}={got!r}")

        print(f"\n[2/2] 候选发现层常量与权重（{len(DISCOVERY_PAIRS)} + 4 权重）")
        for key, const, loc in DISCOVERY_PAIRS:
            want = C.th(key)
            got = getattr(cd, const, "<缺失>")
            if got == "<缺失>":
                drift.append(f"{key}: candidate_discovery 缺少 {const}")
                print(f"  MISS  {key:<34} 无 {const}")
                continue
            if _close(want, got):
                print(f"  ok    {key:<34} {want!r} == {const}")
            else:
                drift.append(f"{key}: thresholds={want!r} discovery={got!r}")
                print(f"  DRIFT {key:<34} thresholds={want!r} != {const}={got!r}")

        legacy_w = tuple(getattr(cd, "DEFAULT_WEIGHTS", ()))
        for name, idx in DISCOVERY_WEIGHTS:
            want = C.th(f"doc.discovery.weights.{name}")
            got = legacy_w[idx] if idx < len(legacy_w) else "<缺失>"
            if _close(want, got):
                print(f"  ok    doc.discovery.weights.{name:<10} {want!r} == DEFAULT_WEIGHTS[{idx}]")
            else:
                drift.append(f"weights.{name}: thresholds={want!r} legacy={got!r}")
                print(f"  DRIFT weights.{name}: thresholds={want!r} != {got!r}")

    print("\n" + "=" * 66)
    if drift:
        print(f"❌ 检出 {len(drift)} 处漂移：")
        for d in drift:
            print(f"   - {d}")
        print("\n处置：以 thresholds.yml 为准同步 legacy 常量，或在 thresholds.yml 中回填，"
              "二者必须完全一致后基线才具备可比性。")
        return 1
    print("✅ 全部一致：thresholds.yml 是可信的唯一事实来源，legacy 基线可比。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
