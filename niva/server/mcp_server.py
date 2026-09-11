# -*- coding: utf-8 -*-
"""NIVA 统一 MCP Server（stdio，JSON-RPC 2.0）。

两档暴露面
----------
    --readonly-only   对外模式：只暴露白名单内的只读工具（方案 §7.4）
    默认（不传）      内部模式：暴露全部 22 个工具，供编排层使用

这不是"配置约定"，而是**可程序校验的收窄**：``external_tools()`` 三重过滤
（白名单 ∩ readonly ∩ implemented），并配有 ``--self-test`` 断言。

传输：同时兼容 MCP 标准的 Content-Length 帧与逐行 JSON，便于手工调试与
不同客户端接入。

用法
    python -m niva.server.mcp_server                  # 内部全量，stdio
    python -m niva.server.mcp_server --readonly-only  # 对外只读
    python -m niva.server.mcp_server --self-test      # 自检（不进入服务循环）
    python -m niva.server.mcp_server --manifest       # 打印工具面与权限审计
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional

from ..kernel.registry import REGISTRY, ErrorCode, ToolResult
from .tools import build_registry, external_tools

SERVER_NAME = "niva-vnv-agent"
SERVER_VERSION = "0.2.0"
PROTOCOL_VERSION = "2025-03-26"


class MCPServer:
    def __init__(self, readonly_only: bool = False) -> None:
        self.registry = build_registry()
        self.readonly_only = readonly_only

    # ------------------------------------------------------------------
    def tool_list(self) -> list[dict[str, Any]]:
        if self.readonly_only:
            return external_tools(self.registry)
        return [t.as_mcp_tool() for t in self.registry.all()]

    def _tool_names(self) -> set[str]:
        return {t["name"] for t in self.tool_list()}

    # ------------------------------------------------------------------
    def handle(self, req: dict[str, Any]) -> Optional[dict[str, Any]]:
        """处理一条 JSON-RPC 请求。通知类返回 None（不回包）。"""
        rid = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}

        def ok_res(result: Any) -> dict[str, Any]:
            return {"jsonrpc": "2.0", "id": rid, "result": result}

        def err(code: int, msg: str) -> dict[str, Any]:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": code, "message": msg}}

        if method == "initialize":
            return ok_res({
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION,
                               "mode": "readonly" if self.readonly_only else "internal"},
            })
        if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return ok_res({})
        if method == "tools/list":
            return ok_res({"tools": self.tool_list()})

        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if not name:
                return err(-32602, "tools/call 缺少 name")
            if self.readonly_only and name not in self._tool_names():
                # 对外模式下的越权调用：明确拒绝，且不泄露内部工具名
                payload = ToolResult(
                    ok=False, error_code=ErrorCode.GATE_REJECTED,
                    error_msg=f"工具 {name} 不在对外只读白名单内",
                ).as_dict()
                return ok_res({"content": [{"type": "text",
                                            "text": json.dumps(payload, ensure_ascii=False)}],
                               "isError": True})
            res = self.registry.call(name, args)
            payload = res.as_dict()
            return ok_res({
                "content": [{"type": "text",
                             "text": json.dumps(payload, ensure_ascii=False, default=str)}],
                "isError": not res.ok,
            })

        return err(-32601, f"未实现的方法：{method}")

    # ------------------------------------------------------------------
    # 传输
    # ------------------------------------------------------------------
    def serve_stdio(self) -> int:
        inp = sys.stdin.buffer
        out = sys.stdout.buffer
        while True:
            msg = _read_message(inp)
            if msg is None:
                break
            if msg == "SKIP":
                continue
            try:
                req = json.loads(msg)
            except json.JSONDecodeError as exc:
                _write_message(out, {"jsonrpc": "2.0", "id": None,
                                     "error": {"code": -32700, "message": f"解析失败: {exc}"}},
                               framed=False)
                continue
            resp = self.handle(req)
            if resp is not None:
                _write_message(out, resp, framed=_FRAMED)
        return 0

    # ------------------------------------------------------------------
    def self_test(self) -> int:
        """自检：验证工具面、权限审计、只读收窄三项不变式。"""
        print("=" * 74)
        print(f"  {SERVER_NAME} v{SERVER_VERSION} · 自检")
        print("=" * 74)
        m = self.registry.manifest()
        print(f"  工具总数：{m['total']}")
        for fam, names in m["by_family"].items():
            print(f"    {fam:<9} {len(names):>2} 个  {names}")
        print()
        perm = m["permissions"]
        print("  作用域与 LLM 权限：")
        for scope, d in perm["scopes"].items():
            flag = "OK" if d["within_recommended"] else "超推荐上限"
            print(f"    {scope:<9} {d['count']:>2} 个  只读 {d['readonly']:<2}  "
                  f"LLM={d['llm_permission']:<16} {flag}")
        print(f"    权限违规：{perm['violations'] or '无'}")
        print(f"  待实现：{m['pending_implementations']}")

        ext = external_tools(self.registry)
        writers = [t["name"] for t in ext if not t["annotations"]["readOnlyHint"]]
        assert not writers, f"对外白名单混入了写工具：{writers}"
        assert perm["ok"], "存在权限违规"
        assert m["audit"]["all_readonly"] is False, "内部工具面本应含写工具"
        print(f"\n  对外只读工具：{len(ext)} 个 {[t['name'] for t in ext]}")
        print("\n  ✅ 自检通过：作用域无违规、对外白名单无写工具。")
        return 0


# ---------------------------------------------------------------------
_FRAMED = False  # 由 _read_message 探测后置位


def _read_message(inp) -> Optional[str]:
    """读一条消息。兼容 Content-Length 帧与逐行 JSON；EOF 返回 None。"""
    global _FRAMED
    first = inp.readline()
    if not first:
        return None
    line = first.decode("utf-8", "replace").strip()
    if not line:
        return "SKIP"
    if line.lower().startswith("content-length:"):
        try:
            length = int(line.split(":", 1)[1].strip())
        except ValueError:
            return "SKIP"
        # 吃掉剩余头部直到空行
        while True:
            h = inp.readline()
            if not h or h.strip() == b"":
                break
        _FRAMED = True
        return inp.read(length).decode("utf-8", "replace")
    _FRAMED = False
    return line


def _write_message(out, obj: dict[str, Any], framed: bool = False) -> None:
    body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
    if framed:
        out.write(b"Content-Length: %d\r\n\r\n" % len(body))
    out.write(body + b"\n")
    out.flush()


def main() -> int:
    ap = argparse.ArgumentParser(description="NIVA 统一 MCP Server")
    ap.add_argument("--readonly-only", action="store_true",
                    help="对外模式：只暴露只读白名单工具")
    ap.add_argument("--self-test", action="store_true", help="自检后退出")
    ap.add_argument("--manifest", action="store_true", help="打印工具面与权限审计后退出")
    args = ap.parse_args()

    srv = MCPServer(readonly_only=args.readonly_only)
    if args.self_test:
        return srv.self_test()
    if args.manifest:
        print(json.dumps(srv.registry.manifest(), ensure_ascii=False, indent=2))
        return 0
    return srv.serve_stdio()


if __name__ == "__main__":
    sys.exit(main())
