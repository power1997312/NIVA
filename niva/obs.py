# -*- coding: utf-8 -*-
"""NIVA 运行留痕（observability）。

设计目标（架构方案 §4.7 / §7.3）：
    1. **每次运行可回放** —— 一条运行 = 一个 JSONL 文件，逐事件追加落盘。
    2. **运行指纹固化** —— 输入哈希、模型版本、知识库版本、阈值快照随事件记录，
       这是"可复现性"验收（同输入两次运行图谱一致）的技术保证。
    3. **降级必可见** —— 任何降级事件强制打 ``degrade`` 标记并计入运行汇总，
       杜绝"静默降质冒充完整结论"。

刻意不引入 logging 依赖第三方 handler：单文件 JSONL + 控制台摘要即可，
既满足审计需求，也让演示环境下"导出一次运行的完整轨迹"变成一次文件复制。
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import platform
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import config as C


def _ts() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _host() -> str:
    try:
        return f"{platform.node()}/{getpass.getuser()}"
    except Exception:
        return platform.node()


@dataclass
class RunContext:
    """单次运行的上下文与留痕句柄。"""

    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    task: str = ""
    log_path: Optional[Path] = None
    started_at: float = field(default_factory=time.time)
    events: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    degrade_events: list[dict[str, Any]] = field(default_factory=list)
    fingerprint: dict[str, Any] = field(default_factory=dict)

    # ---------------- 基础留痕 ----------------
    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        rec = {"ts": _ts(), "run_id": self.run_id, "event": event}
        rec.update(fields)
        self.events.append(rec)
        if event == "degrade" or fields.get("degraded"):
            self.degrade_events.append(rec)
            self.counters["degrade"] = self.counters.get("degrade", 0) + 1
        self._append(rec)
        return rec

    def count(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def _append(self, rec: dict[str, Any]) -> None:
        if self.log_path is None:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except OSError:
            # 留痕失败不得阻断业务主流程，但必须显式提示到控制台
            print(f"[obs] 警告：运行留痕写入失败 {self.log_path}", file=sys.stderr)

    # ---------------- 指纹 ----------------
    def set_fingerprint(self, **kv: Any) -> None:
        self.fingerprint.update(kv)

    def snapshot_environment(self) -> None:
        """记录环境指纹：解释器、关键库版本、阈值表哈希。"""
        vers: dict[str, str] = {}
        for mod in ("numpy", "networkx", "pymupdf", "scipy", "transformers", "torch", "openpyxl"):
            try:
                vers[mod] = __import__(mod).__version__  # type: ignore[attr-defined]
            except Exception:
                vers[mod] = "<缺失>"
        self.set_fingerprint(
            python=sys.version.split()[0],
            platform=f"{platform.system()} {platform.release()}",
            host=_host(),
            libs=vers,
            thresholds_sha256=_file_sha(C.THRESHOLDS_FILE),
        )

    # ---------------- 汇总 ----------------
    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task": self.task,
            "elapsed_s": round(time.time() - self.started_at, 3),
            "counters": dict(self.counters),
            "degrade_count": len(self.degrade_events),
            "degrade_reasons": sorted({e.get("reason", "") for e in self.degrade_events}),
            "fingerprint": self.fingerprint,
            "log_path": str(self.log_path) if self.log_path else None,
        }

    def finish(self) -> dict[str, Any]:
        s = self.summary()
        self.emit("run_finished", **s)
        if s["degrade_count"]:
            print(f"[obs] 本运行发生 {s['degrade_count']} 次降级：{s['degrade_reasons']}")
        return s


def start_run(task: str, log: bool = True) -> RunContext:
    """开启一次带留痕的运行。"""
    C.ensure_dirs()
    ctx = RunContext(task=task)
    if log:
        ctx.log_path = C.ARTIFACTS_DIR / "runs" / f"{_stamp()}_{ctx.run_id}.jsonl"
        ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.snapshot_environment()
    ctx.emit("run_started", task=task, host=_host(), cwd=os.getcwd())
    return ctx


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _file_sha(path: Path) -> str:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except OSError:
        return "<不可读>"


def input_digest(*paths: str | Path) -> str:
    """输入材料指纹：按路径排序后对文件名+内容哈希求总哈希。

    用于"同输入两次运行"判定的输入侧锚点。
    """
    h = hashlib.sha256()
    for p in sorted(str(x) for x in paths):
        h.update(p.encode("utf-8", "replace"))
        h.update(b"\0")
        h.update(_file_sha(Path(p)).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()[:16]
