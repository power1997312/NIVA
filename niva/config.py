# -*- coding: utf-8 -*-
"""NIVA 统一配置与阈值访问层。

职责：
    1. **路径统一** —— 所有目录常量集中在此，业务代码不得自行拼路径。
    2. **阈值统一** —— 从 ``niva/knowledge/thresholds.yml`` 读取，并保留原工程
       的环境变量覆盖能力（``env_override``），使单变量 A/B 实验不因整合而丢失。
    3. **适配器装载纪律** —— ``legacy_import_path()`` 以显式上下文管理器方式把
       既有工程目录临时挂到 ``sys.path``，避免两个 legacy 顶层模块名互相遮蔽
       （Trace_NL 与 SAMA-V1 都有 ``config.py`` / ``tests/`` 这类同名顶层模块，
        永久 sys.path 挂载必然踩坑）。

红线：``doc.*`` 阈值即判定链行为，改必重跑金标回归。
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "NIVA 需要 PyYAML。请在项目虚拟环境中安装：pip install pyyaml") from exc

# =====================================================================
# 一、路径
# =====================================================================
NIVA_ROOT: Path = Path(__file__).resolve().parent.parent
PKG_ROOT: Path = Path(__file__).resolve().parent

DOC_LEGACY: Path = PKG_ROOT / "adapters" / "doc" / "legacy"
DIAGRAM_LEGACY: Path = PKG_ROOT / "adapters" / "diagram" / "legacy"

KNOWLEDGE_ROOT: Path = PKG_ROOT / "knowledge"
THRESHOLDS_FILE: Path = KNOWLEDGE_ROOT / "thresholds.yml"

# 制品仓：解析产物 / 图谱快照 / 矩阵 / 用例集
ARTIFACTS_DIR: Path = NIVA_ROOT / "artifacts"
CACHE_DIR: Path = ARTIFACTS_DIR / "cache"
EVAL_DIR: Path = NIVA_ROOT / "eval"
DOCS_DIR: Path = NIVA_ROOT / "docs"

# 默认输入材料位置（均落在各自 legacy 树内，保证原工程可原地运行、基线可复现）
DOC_DATA_DIR: Path = DOC_LEGACY            # 其下含 用户需求/ 系统需求/ 系统设计/ 基准数据/
DIAGRAM_SAMPLE_DIR: Path = DIAGRAM_LEGACY / "样本"

# SAMA 适配器的知识根：指到 NIVA 规范知识层，而非 legacy/knowledge 种子副本
os.environ.setdefault("SAMA_KB_ROOT", str(KNOWLEDGE_ROOT))


# ---------------------------------------------------------------------
# .env 装载（P4 发现的缺口）
# ---------------------------------------------------------------------
# legacy 的 LLM 密钥放在 Trace_NL/.env，由 legacy 自己的 config.py 装载——
# 但那只在**子进程**里生效。NIVA 主进程（编排层/LLM 网关）此前读不到，
# 导致 adjudicate/narrate 在进程内"LLM 不可用"。
# 规则：NIVA 根 > doc legacy > diagram legacy，**不覆盖**已存在的环境变量。
def _load_env_files() -> list[str]:
    loaded = []
    for cand in (NIVA_ROOT / ".env", DOC_LEGACY / ".env", DIAGRAM_LEGACY / ".env"):
        if not cand.exists():
            continue
        try:
            for line in cand.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
            loaded.append(str(cand))
        except OSError:
            continue
    return loaded


LOADED_ENV_FILES = _load_env_files()


def ensure_dirs() -> None:
    for d in (ARTIFACTS_DIR, CACHE_DIR, EVAL_DIR, DOCS_DIR):
        d.mkdir(parents=True, exist_ok=True)


# =====================================================================
# 二、阈值（带 env_override）
# =====================================================================
class _Node:
    """支持点号访问的只读配置节点，叶子节点由 ``_walk`` 解包为裸值。"""

    __slots__ = ("_d",)

    def __init__(self, d: dict[str, Any]):
        object.__setattr__(self, "_d", d)

    def __getattr__(self, name: str) -> Any:
        try:
            v = self._d[name]
        except KeyError as exc:
            raise AttributeError(
                f"阈值键不存在：{name}（可用：{sorted(self._d)[:12]}）") from exc
        return _Node(v) if isinstance(v, dict) else v

    def __getitem__(self, name: str) -> Any:
        return self.__getattr__(name)

    def raw(self) -> dict[str, Any]:
        return object.__getattribute__(self, "_d")

    def __repr__(self) -> str:
        return f"<Thresholds {sorted(self._d)[:8]}>"


def _leaf(entry: Any) -> Any:
    """解包 ``{value: X, env_override: ENV}`` 形态；保留 env 覆盖能力。"""
    if isinstance(entry, dict) and "value" in entry:
        env = entry.get("env_override")
        if env:
            raw = os.environ.get(env)
            if raw is not None and raw != "":
                return _coerce(raw, entry["value"])
        return entry["value"]
    return entry


def _coerce(raw: str, template: Any) -> Any:
    """按默认值的类型解释环境变量字符串。"""
    if isinstance(template, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on", "y", "t")
    if isinstance(template, int) and not isinstance(template, bool):
        try:
            return int(float(raw))
        except ValueError:
            return template
    if isinstance(template, float):
        try:
            return float(raw)
        except ValueError:
            return template
    return raw


def _walk(d: Any) -> Any:
    if isinstance(d, dict):
        if set(d.keys()) == {"value"} or ("value" in d and len(d) <= 2):
            return _leaf(d)
        return {k: _walk(v) for k, v in d.items()}
    return d


_THRESHOLDS: Optional[_Node] = None


def thresholds(reload: bool = False) -> _Node:
    """加载并缓存阈值表。``deprecated`` 键（如 basis_rank 的 int 值）原样返回。"""
    global _THRESHOLDS
    if _THRESHOLDS is None or reload:
        if not THRESHOLDS_FILE.exists():
            raise FileNotFoundError(f"阈值表缺失：{THRESHOLDS_FILE}")
        with open(THRESHOLDS_FILE, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        # basis_rank 的值是裸 int，不是 {value:...} 形态，_walk 会原样保留
        _THRESHOLDS = _Node(_walk(raw))
    return _THRESHOLDS


def th(path: str, default: Any = None) -> Any:
    """点路径取值：``th('doc.embedding_exact')`` → 0.95。"""
    cur: Any = thresholds().raw()
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


# =====================================================================
# 三、legacy 适配器装载
# =====================================================================
_LEGACY_ROOT = {"doc": DOC_LEGACY, "diagram": DIAGRAM_LEGACY}
_LEGACY_LOCK: dict[str, bool] = {}


@contextmanager
def legacy_import_path(which: str) -> Iterator[Path]:
    """临时把某个 legacy 工程目录挂到 ``sys.path`` 首位，退出时精确还原。

    为什么必须用上下文管理器：两个原工程都存在 ``config.py``、``tests/``、
    ``kb_io.py`` 等**同名顶层模块**，且都会 ``import config`` 这类绝对导入。
    若永久挂载，后挂的会遮蔽先挂的，产生极难定位的"导入了另一个工程的 config"。

    用法::

        with legacy_import_path("diagram"):
            import lib_sama          # 解析到 SAMA-V1
        with legacy_import_path("doc"):
            import config as tn_cfg  # 解析到 Trace_NL
    """
    if which not in _LEGACY_ROOT:
        raise ValueError(f"未知适配器：{which}（可选 {sorted(_LEGACY_ROOT)}）")
    root = _LEGACY_ROOT[which]
    if not root.is_dir():
        raise FileNotFoundError(f"适配器目录不存在：{root}（请先运行 tools/migrate_legacy.py）")

    entry = str(root)
    inserted = False
    if entry not in sys.path:
        sys.path.insert(0, entry)
        inserted = True

    # 清掉可能已缓存的同名顶层模块，避免指向另一个工程的残留
    _purge_modules(which)
    _LEGACY_LOCK[which] = True
    try:
        yield root
    finally:
        _LEGACY_LOCK[which] = False
        if inserted:
            try:
                sys.path.remove(entry)
            except ValueError:
                pass


# 各 legacy 顶层模块名单：退出/进入时清理，防止跨工程串味
_LEGACY_MODULES = {
    "doc": {"config", "core", "pdfparser", "models", "models_pkg"},
    "diagram": {
        "config", "lib_sama", "kb_io", "kb_tag", "kb_sp", "kb_check", "knowledge_api",
        "pipeline", "nets", "p2_pipeline", "p3_ir", "p3_main", "p4_semantics",
        "port_graph", "view_a", "struct_narrate", "st_mermaid", "llm_view", "gen_llm",
        "trace_api", "app_queries", "bool_dict",
    },
}


def _purge_modules(which: str) -> None:
    for name in _LEGACY_MODULES.get(which, ()):
        sys.modules.pop(name, None)
