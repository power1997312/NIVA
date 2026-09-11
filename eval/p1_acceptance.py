# -*- coding: utf-8 -*-
"""P1 验收总闸：统一 MCP 工具面 + 两条链路端到端。

验收标准（架构方案 §9 P1）
    「通过 MCP 客户端能串联完成『解析文档→建矩阵→查图纸』」
    且对外只暴露只读工具。

检查项
    A  工具面完整性    22 个工具注册齐全，四族数目 9/5/4/4
    B  作用域与权限    4 个作用域、LLM 策略正确、无权限违规、无超推荐上限
    C  作用域隔离      跨作用域调用被拒；被禁 LLM 的作用域 llm_allowed=False
    D  MCP 协议        Content-Length 帧下单次会话可 initialize → tools/list → tools/call
    E  文档链路        文档解析 → 建矩阵 → 验证 → 导出，四步串联成功
    F  图纸链路        元信息 → 页列表 → 取页 → 路径追溯，四步串联成功
    G  对外只读收窄    只读模式下写工具不出现在 tools/list，且显式调用被拒
    H  降级可见        未配 LLM 时 adjudicate 返回降级封套（而非静默失败）

用法：<venv>/python.exe eval/p1_acceptance.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from niva import config as C                                     # noqa: E402
from niva.kernel.registry import ErrorCode, REGISTRY             # noqa: E402
from niva.server.mcp_server import MCPServer                     # noqa: E402
from niva.server.tools import build_registry, external_tools     # noqa: E402

PY = sys.executable
RESULTS: list[tuple[str, bool, str]] = []
L = C.DOC_DATA_DIR


def rec(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  {'PASS' if passed else 'FAIL'}  {name:<26} {detail}")


# =====================================================================
# 一个最小 MCP 客户端（Content-Length 帧），用于真实协议往返
# =====================================================================
class MCPClient:
    def __init__(self, readonly_only: bool = False) -> None:
        args = [PY, "-m", "niva.server.mcp_server"]
        if readonly_only:
            args.append("--readonly-only")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        env["PYTHONIOENCODING"] = "utf-8"
        self.p = subprocess.Popen(
            args, cwd=str(ROOT), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self._id = 0

    def _send(self, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.p.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body))
        self.p.stdin.write(body)
        self.p.stdin.flush()

    def _recv(self, timeout_s: float = 900.0) -> dict:
        t0 = time.time()
        # 读头部
        header = b""
        while not header.endswith(b"\r\n\r\n"):
            if time.time() - t0 > timeout_s:
                raise TimeoutError("等待 MCP 响应超时")
            ch = self.p.stdout.read(1)
            if not ch:
                raise EOFError("MCP 进程已退出；stderr=" +
                               self.p.stderr.read()[-800:].decode("utf-8", "replace"))
            header += ch
        length = int(header.decode().split(":", 1)[1].split("\r\n")[0].strip())
        return json.loads(self.p.stdout.read(length).decode("utf-8"))

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method,
                    "params": params or {}})
        return self._recv()

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def tool(self, name: str, args: dict) -> tuple[bool, dict]:
        r = self.call("tools/call", {"name": name, "arguments": args})
        res = r.get("result") or {}
        payload = json.loads((res.get("content") or [{}])[0].get("text", "{}"))
        return (not res.get("isError", False)) and payload.get("ok", False), payload

    def close(self) -> None:
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()


# =====================================================================
def check_a_toolface(reg) -> None:
    m = reg.manifest()
    fam = {k: len(v) for k, v in m["by_family"].items()}
    want = {"diagram": 9, "doc": 5, "generate": 4, "graph": 4}
    rec("A 工具面完整性", m["total"] == 22 and fam == want,
        f"total={m['total']} 分布={fam}")
    # 不变式（不写快照数）：待实现的工具只能来自图谱族（P2/P5 延后项），
    # 其余四族必须全部实现 —— 否则说明有回归。
    pend = set(m["pending_implementations"])
    n_impl = len([t for t in reg.all() if t.implemented])
    allowed = {"xdiff_trace", "utg_query", "utg_impact_analysis",
               "utg_coverage_report"}
    rec("A 已实现计数", n_impl + len(pend) == 22 and pend <= allowed,
        f"已实现 {n_impl}/22，待实现 {sorted(pend)}（允许延后集={sorted(allowed)}）")


def check_b_scope(reg) -> None:
    perm = reg.audit_permissions()
    over = [s for s, d in perm["scopes"].items() if not d["within_recommended"]]
    rec("B 作用域权限审计", perm["ok"] and not over,
        f"违规={perm['violations'] or '无'}  超上限={over or '无'}")
    pol = {s: d["llm_permission"] for s, d in perm["scopes"].items()}
    rec("B LLM 策略正确", pol == {"doc": "adjudicate_only", "diagram": "none",
                                 "generate": "narrate_only", "graph": "none"},
        json.dumps(pol, ensure_ascii=False))


def check_c_isolation(reg) -> None:
    dg = reg.scoped("diagram")
    r = dg.call("parse_requirement_doc", {"pdf_path": "x"})
    ok1 = (not r.ok) and r.error_code == ErrorCode.GATE_REJECTED
    rec("C 跨作用域调用被拒", ok1, f"{r.error_code}: {(r.error_msg or '')[:60]}")
    rec("C 图纸域构造性禁 LLM", dg.llm_allowed is False,
        f"scoped('diagram').llm_permission={dg.llm_permission}")
    ok2 = not reg.scoped("generate").call("get_meta", {}).ok
    rec("C 生成域不得调图纸工具", ok2,
        f"工具面={reg.scoped('generate').names()[:3]}…")


def check_d_e_f_mcp() -> None:
    cli = MCPClient()
    try:
        init = cli.call("initialize", {"protocolVersion": "2025-03-26",
                                      "clientInfo": {"name": "niva-acceptance"}})
        cli.notify("notifications/initialized")
        info = (init.get("result") or {}).get("serverInfo") or {}
        rec("D MCP initialize", bool(info), f"{info.get('name')} v{info.get('version')} "
                                          f"mode={info.get('mode')}")
        tl = cli.call("tools/list")
        n = len((tl.get("result") or {}).get("tools") or [])
        rec("D tools/list", n == 22, f"{n} 个工具")

        # ---- E 文档链路 ----
        sysreq = str(L / "系统需求" / "DCS需求说明书.pdf")
        ok1, p1 = cli.tool("parse_requirement_doc", {"pdf_path": sysreq})
        rec("E1 parse_requirement_doc", ok1,
            f"items={p1.get('data', {}).get('counts', {}).get('items')} "
            f"sections={p1.get('data', {}).get('counts', {}).get('sections')}" if ok1
            else (p1.get("error_msg") or "")[:80])

        ok2, p2 = cli.tool("build_trace_matrix", {
            "downstream_pdf": sysreq,
            "upstream_pdfs": {
                "DCS设备技术规格书": str(L / "用户需求" / "DCS设备技术规格书.pdf"),
                "RPS系统需求规范书": str(L / "用户需求" / "RPS系统需求规范书.pdf")},
            "mode": "discover"})
        mid = (p2.get("data") or {}).get("matrix_id")
        rec("E2 build_trace_matrix", ok2 and bool(mid),
            f"matrix_id={mid} rows={(p2.get('data') or {}).get('stats', {}).get('rows')}"
            if ok2 else (p2.get("error_msg") or "")[:80])

        t0 = time.time()
        ok3, p3 = cli.tool("verify_trace", {"matrix_id": mid})
        st = (p3.get("data") or {}).get("stats", {})
        gs = (p3.get("data") or {}).get("graph_stats", {})
        rec("E3 verify_trace", ok3,
            f"{time.time()-t0:.0f}s 字符占比={st.get('downstream_char_ratio')} "
            f"图谱={gs.get('nodes')}节点/{gs.get('edges')}边" if ok3
            else (p3.get("error_msg") or "")[:80])

        ok4, p4 = cli.tool("export_matrix_excel", {"matrix_id": mid})
        rec("E4 export_matrix_excel", ok4,
            f"{(p4.get('data') or {}).get('bytes')} 字节" if ok4
            else (p4.get("error_msg") or "")[:80])

        # ---- H 降级可见 ----
        ok5, p5 = cli.tool("adjudicate_ambiguity", {"matrix_id": mid})
        deg = p5.get("degraded")
        msg = (p5.get("degrade_reason") or "")[:60]
        rec("H 降级可见（LLM 可选）", ok5 and (deg is False or bool(msg)),
            f"degraded={deg} reason={msg or '（LLM 可用，未降级）'}")

        # ---- F 图纸链路 ----
        ok6, p6 = cli.tool("get_meta", {})
        rec("F1 get_meta", ok6, f"keys={list((p6.get('data') or {}).keys())[:5]}")
        ok7, p7 = cli.tool("list_pages", {})
        rec("F2 list_pages", ok7,
            f"pages={(p7.get('data') or {}).get('pages')}")
        ok8, p8 = cli.tool("get_page", {"page": 5})
        rec("F3 get_page(5)", ok8,
            f"keys={list((p8.get('data') or {}).keys())[:6]}")
        ok9, p9 = cli.tool("trace_path", {"node": "MP101", "direction": "down",
                                         "depth": 4})
        d9 = p9.get("data") or {}
        rec("F4 trace_path", ok9 and (d9.get("n_paths") or 0) > 0,
            f"起点={d9.get('start_label')} 命中起点={d9.get('n_matched_starts')} "
            f"路径数={d9.get('n_paths')}")

        rec("D MCP 会话串联", all([ok1, ok2, ok3, ok4, ok6, ok7, ok8, ok9]),
            "同一会话内连续 9 次工具调用全部成功")
    finally:
        cli.close()


def check_g_readonly() -> None:
    cli = MCPClient(readonly_only=True)
    try:
        cli.call("initialize", {})
        cli.notify("notifications/initialized")
        tl = cli.call("tools/list")
        names = {t["name"] for t in ((tl.get("result") or {}).get("tools") or [])}
        writers = {"build_trace_matrix", "verify_trace", "export_matrix_excel",
                   "adjudicate_ambiguity", "generate_test_case", "render_test_doc"}
        leaked = names & writers
        rec("G 只读模式无写工具", not leaked,
            f"{len(names)} 个工具，泄露写工具={leaked or '无'}")

        ok, p = cli.tool("build_trace_matrix", {"downstream_pdf": "x"})
        rec("G 越权调用被拒", (not ok) and p.get("error_code") == ErrorCode.GATE_REJECTED,
            f"error_code={p.get('error_code')}")

        ok2, p2 = cli.tool("get_meta", {})
        rec("G 只读工具仍可用", ok2, "get_meta 正常返回")
    finally:
        cli.close()


def main() -> int:
    print("=" * 80)
    print("  NIVA · P1 验收总闸（统一 MCP 工具面 + 两条链路）")
    print("=" * 80)
    reg = build_registry()
    check_a_toolface(reg)
    check_b_scope(reg)
    check_c_isolation(reg)
    check_d_e_f_mcp()
    check_g_readonly()

    npass = sum(1 for _n, ok, _d in RESULTS if ok)
    print("\n" + "=" * 80)
    print(f"  结果：{npass}/{len(RESULTS)} 项通过")
    if npass == len(RESULTS):
        print("  ✅ P1 验收通过：22 工具统一在 4 个能力作用域下，")
        print("     两条独立链路均可经 MCP 协议串联跑通，对外仅暴露只读工具。")
        print("     下一阶段：P2 图文桥接（前置 Gate：完整语料重跑 R1 复测）")
        return 0
    print("  ❌ 未通过项：")
    for n, ok, d in RESULTS:
        if not ok:
            print(f"     - {n}: {d}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
