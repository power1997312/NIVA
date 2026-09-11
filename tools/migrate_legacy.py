#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""NIVA P0 · 绞杀者迁移脚本（可重复执行，幂等）。

把两个既有工程按"原样迁入"原则复制进 NIVA monorepo 的适配器层，
并产出溯源清单 MIGRATION.json（来源提交、文件数、内容指纹、排除项）。

设计纪律（对应架构方案 §8.2）：
  - 业务代码一行不改；本脚本只做复制与记账。
  - 排除构建/缓存产物，避免把 7.8GB 的 .venv 拖进仓库。
  - 记录"内容指纹"而非仅 HEAD 提交号——两个原工程在迁移时工作区均非干净。

用法：
    python tools/migrate_legacy.py            # 执行迁移
    python tools/migrate_legacy.py --dry-run  # 只统计，不写盘
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

# ------------------------------------------------------------------ 配置
NIVA_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SOURCES = {
    "doc": {
        "src": r"E:\Trace_NL",
        "dst": os.path.join(NIVA_ROOT, "niva", "adapters", "doc", "legacy"),
        "note": "需求文档追溯引擎（解析/提取/发现/匹配/裁决/报告）",
    },
    "diagram": {
        "src": r"E:\SAMA-V1",
        "dst": os.path.join(NIVA_ROOT, "niva", "adapters", "diagram", "legacy"),
        "note": "工程图纸解析引擎（矢量解析/联网/方向裁决/IR/知识绑定）",
    },
}

# 目录级排除（任何层级出现即整棵跳过）
EXCLUDE_DIRS = {
    ".venv", "venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".git", ".idea", ".vscode", "node_modules",
    ".ipynb_checkpoints", "dist", "build", ".eggs",
}

# 文件级排除
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".pyd", ".so", ".log"}
EXCLUDE_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def _should_skip_dir(name: str) -> bool:
    return name in EXCLUDE_DIRS


def _should_skip_file(name: str) -> bool:
    if name in EXCLUDE_NAMES:
        return True
    # Office 锁文件（~$xxx.xlsx / .~lock.xxx#）：非数据，且常被进程占用导致 copy 失败
    if name.startswith("~$") or name.startswith(".~lock."):
        return True
    return any(name.endswith(s) for s in EXCLUDE_SUFFIXES)


def _git(repo: str, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
        return out.stdout.strip()
    except Exception as exc:  # pragma: no cover - 环境缺 git 时降级
        return f"<git 不可用: {exc}>"


def _walk_manifest(src: str) -> list[tuple[str, int]]:
    """返回 [(相对路径, 字节数)]，已按排除规则过滤。"""
    entries: list[tuple[str, int]] = []
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if not _should_skip_dir(d)]
        for fn in files:
            if _should_skip_file(fn):
                continue
            fp = os.path.join(root, fn)
            try:
                size = os.path.getsize(fp)
            except OSError:
                continue
            entries.append((os.path.relpath(fp, src), size))
    entries.sort()
    return entries


def _tree_digest(src: str, entries: list[tuple[str, int]]) -> str:
    """对整个源码树做 Merkle 式指纹：对 路径+大小+内容 逐文件哈希再汇总。"""
    h = hashlib.sha256()
    for rel, _size in entries:
        h.update(rel.encode("utf-8", "replace"))
        h.update(b"\0")
        fp = os.path.join(src, rel)
        fh = hashlib.sha256()
        try:
            with open(fp, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    fh.update(chunk)
        except OSError:
            fh.update(b"<unreadable>")
        h.update(fh.hexdigest().encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def _clear_dir(path: str) -> None:
    """已废弃：本执行环境对整个删除面（rmtree / os.remove）挂 fail-closed 拦截。

    迁移因此改为**零删除**设计：目标目录只做原地覆盖写入（见 migrate()），
    残留文件不删除、只登记上报。目标目录是本脚本独占的构建产物，
    覆盖写在语义上等价于清空重写，且删除范围无需人工审查。
    """
    raise NotImplementedError("迁移已改为零删除覆盖写；见 migrate()")


def _copy_tree(src: str, dst: str) -> tuple[int, list[str]]:
    """原地覆盖式复制（不删除任何既有文件）。返回 (写入文件数, 残留的相对路径列表)。"""
    src_files = {rel for rel, _s in _walk_manifest(src)}
    os.makedirs(dst, exist_ok=True)
    copied = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if not _should_skip_dir(d)]
        rel_root = os.path.relpath(root, src)
        target_root = dst if rel_root == "." else os.path.join(dst, rel_root)
        os.makedirs(target_root, exist_ok=True)
        for fn in files:
            if _should_skip_file(fn):
                continue
            shutil.copy2(os.path.join(root, fn), os.path.join(target_root, fn))
            copied += 1
    # 登记目标侧多出来的文件（来自更早的迁移，本环境无法删除）
    stale: list[str] = []
    for root, dirs, files in os.walk(dst):
        dirs[:] = [d for d in dirs if not _should_skip_dir(d)]
        for fn in files:
            if _should_skip_file(fn):
                continue
            rel = os.path.relpath(os.path.join(root, fn), dst)
            if rel not in src_files:
                stale.append(rel)
    return copied, sorted(stale)


def migrate(key: str, cfg: dict, dry_run: bool = False) -> dict:
    src, dst = cfg["src"], cfg["dst"]
    print(f"\n=== [{key}] {src}")
    if not os.path.isdir(src):
        raise SystemExit(f"源目录不存在：{src}")

    entries = _walk_manifest(src)
    total_bytes = sum(s for _r, s in entries)
    print(f"    待迁入：{len(entries)} 个文件 / {total_bytes / 1048576:.1f} MB")

    digest = _tree_digest(src, entries)
    print(f"    内容指纹 sha256：{digest[:16]}…")

    head = _git(src, "rev-parse", "HEAD")
    last = _git(src, "log", "-1", "--format=%H|%ad|%s", "--date=short")
    dirty = len([l for l in _git(src, "status", "--porcelain").splitlines() if l.strip()])

    record = {
        "adapter": key,
        "note": cfg["note"],
        "source_path": src,
        "dest_path": dst,
        "git_head": head,
        "git_last_commit": last,
        "dirty_entries_at_migration": dirty,
        "file_count": len(entries),
        "total_bytes": total_bytes,
        "content_sha256": digest,
        "excluded_dirs": sorted(EXCLUDE_DIRS),
        "migrated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "dry_run": dry_run,
    }

    if dry_run:
        print("    [dry-run] 未写盘")
        return record

    # 原地覆盖式写入（零删除，见 _clear_dir 说明）
    copied, stale = _copy_tree(src, dst)
    print(f"    已写入：{copied} 个文件 → {dst}")
    if copied != len(entries):
        raise SystemExit(f"复制数量不一致：预期 {len(entries)}，实际 {copied}")
    if stale:
        print(f"    ⚠ 目标侧残留 {len(stale)} 个非本次来源文件（本环境不可删除，已登记）：")
        for rel in stale[:10]:
            print(f"        - {rel}")
        if len(stale) > 10:
            print(f"        … 其余 {len(stale) - 10} 项见 MIGRATION.json")
    record["copied_files"] = copied
    record["stale_files"] = stale
    return record


def main() -> int:
    ap = argparse.ArgumentParser(description="NIVA 绞杀者迁移")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写盘")
    args = ap.parse_args()

    print(f"NIVA 根目录：{NIVA_ROOT}")
    records = [migrate(k, v, args.dry_run) for k, v in SOURCES.items()]

    manifest = {
        "generated_by": "tools/migrate_legacy.py",
        "niva_root": NIVA_ROOT,
        "policy": "原样迁入（strangler）：业务代码零修改，仅复制与记账",
        "adapters": records,
    }
    if not args.dry_run:
        path = os.path.join(NIVA_ROOT, "MIGRATION.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        print(f"\n溯源清单已写入：{path}")
    else:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
