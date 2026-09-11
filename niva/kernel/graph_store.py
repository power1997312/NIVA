# -*- coding: utf-8 -*-
"""UTG 双域追溯图谱存储（SQLite 三表 + NetworkX 内存分析 + JSON 快照）。

落地架构方案 §3.2 L1 数据层：
    - ``nodes`` / ``edges`` / ``provenance`` 三表（§3.3 数据层）；
    - 内存侧用 NetworkX 做可达性与影响分析（可选依赖，缺失时退化为内核自带 BFS）；
    - 每次落盘产出**带指纹的 JSON 快照**，支撑"同输入两次运行结构一致"的
      可复现性验收与图谱快照 diff。

存储选择说明：竞赛期用 SQLite 是为了零部署成本与单机可运行；接口按
"读取整个图 → 内存操作 → 写回"设计，升级到 Neo4j 时只需替换本文件。
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable, Optional

from .model import Evidence, Locator, TraceEdge, TraceGraph, TraceNode

try:
    import networkx as nx
except ImportError:  # pragma: no cover
    nx = None  # type: ignore

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id     TEXT PRIMARY KEY,
    domain      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    label       TEXT,
    payload     TEXT,
    attrs       TEXT,
    locator     TEXT
);
CREATE INDEX IF NOT EXISTS idx_nodes_domain ON nodes(domain);
CREATE INDEX IF NOT EXISTS idx_nodes_kind   ON nodes(kind);

CREATE TABLE IF NOT EXISTS edges (
    edge_id        TEXT PRIMARY KEY,
    src            TEXT NOT NULL,
    dst            TEXT NOT NULL,
    relation       TEXT NOT NULL,
    status         TEXT NOT NULL,
    confidence     REAL NOT NULL,
    source_mode    TEXT,
    evidence       TEXT,
    llm_opinion    TEXT,
    human_confirmed INTEGER DEFAULT 0,
    ambiguous      INTEGER DEFAULT 0,
    degrade_reason TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
CREATE INDEX IF NOT EXISTS idx_edges_status ON edges(status);

-- 追溯边全生命周期留痕：谁、何时、把哪条边从什么状态改成什么状态
CREATE TABLE IF NOT EXISTS provenance (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    actor       TEXT,
    action      TEXT NOT NULL,
    edge_id     TEXT,
    node_id     TEXT,
    from_status TEXT,
    to_status   TEXT,
    detail      TEXT,
    run_id      TEXT
);
CREATE INDEX IF NOT EXISTS idx_prov_edge ON provenance(edge_id);
CREATE INDEX IF NOT EXISTS idx_prov_run  ON provenance(run_id);
"""


class GraphStore:
    """UTG 持久化门面。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._nx = None

    # ------------------------------------------------------------------
    # 序列化辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _dumps(v: Any) -> str:
        return json.dumps(v, ensure_ascii=False, default=str)

    @staticmethod
    def _loads(s: Any, default: Any) -> Any:
        if s is None or s == "":
            return default
        try:
            return json.loads(s)
        except (TypeError, json.JSONDecodeError):
            return default

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def upsert_node(self, node: TraceNode) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO nodes (node_id,domain,kind,label,payload,attrs,locator)"
            " VALUES (?,?,?,?,?,?,?)",
            (node.node_id, node.domain, node.kind, node.label,
             self._dumps(node.payload), self._dumps(node.attrs),
             self._dumps(asdict(node.locator)) if node.locator else None),
        )

    def upsert_edge(self, edge: TraceEdge) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO edges (edge_id,src,dst,relation,status,confidence,"
            "source_mode,evidence,llm_opinion,human_confirmed,ambiguous,degrade_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (edge.edge_id, edge.src, edge.dst, edge.relation, edge.status,
             float(edge.confidence), edge.source_mode,
             self._dumps([asdict(e) for e in edge.evidence]),
             edge.llm_opinion, int(edge.human_confirmed), int(edge.ambiguous),
             edge.degrade_reason),
        )

    def log_provenance(
        self, action: str, *, actor: str = "", edge_id: str = "", node_id: str = "",
        from_status: str = "", to_status: str = "", detail: Any = None, run_id: str = "",
    ) -> None:
        self.conn.execute(
            "INSERT INTO provenance (ts,actor,action,edge_id,node_id,from_status,"
            "to_status,detail,run_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), actor, action, edge_id, node_id, from_status, to_status,
             self._dumps(detail) if detail is not None else None, run_id),
        )

    def save_graph(self, graph: TraceGraph, *, actor: str = "system", run_id: str = "",
                   log: bool = True) -> None:
        """整图落库（事务内批量写）。"""
        for n in graph.nodes.values():
            self.upsert_node(n)
        for e in graph.edges.values():
            self.upsert_edge(e)
        if log:
            self.log_provenance(
                "save_graph", actor=actor, run_id=run_id,
                detail={"nodes": len(graph.nodes), "edges": len(graph.edges),
                        "fingerprint": graph.fingerprint()},
            )
        self.conn.commit()
        self._nx = None

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def load_graph(self) -> TraceGraph:
        g = TraceGraph(meta={"source": str(self.path)})
        for r in self.conn.execute("SELECT * FROM nodes"):
            loc = self._loads(r["locator"], None)
            if loc and loc.get("bbox") is not None:
                loc["bbox"] = tuple(loc["bbox"])
            g.nodes[r["node_id"]] = TraceNode(
                node_id=r["node_id"], domain=r["domain"], kind=r["kind"],
                label=r["label"] or "", payload=self._loads(r["payload"], {}),
                attrs=self._loads(r["attrs"], {}),
                locator=Locator(**loc) if loc else None,
            )
        for r in self.conn.execute("SELECT * FROM edges"):
            evs = []
            for e in self._loads(r["evidence"], []):
                l = e.get("locator") or {}
                if l.get("bbox") is not None:
                    l["bbox"] = tuple(l["bbox"])
                evs.append(Evidence(
                    kind=e["kind"], method=e.get("method", ""), score=e["score"],
                    locator=Locator(**l), raw_snippet=e.get("raw_snippet", ""),
                    score_scale=e.get("score_scale", ""), note=e.get("note", ""),
                    channel=e.get("channel", ""),
                ))
            g.edges[r["edge_id"]] = TraceEdge(
                edge_id=r["edge_id"], src=r["src"], dst=r["dst"], relation=r["relation"],
                status=r["status"], confidence=r["confidence"],
                source_mode=r["source_mode"] or "", evidence=evs,
                llm_opinion=r["llm_opinion"] or "",
                human_confirmed=bool(r["human_confirmed"]),
                ambiguous=bool(r["ambiguous"]),
                degrade_reason=r["degrade_reason"] or "",
            )
        return g

    def query_edges(self, sql_where: str = "1=1", params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        """受控直查：仅供高级筛选使用；``sql_where`` 必须由内部代码提供，禁止拼接用户输入。"""
        return list(self.conn.execute(f"SELECT * FROM edges WHERE {sql_where}", tuple(params)))

    def provenance(self, run_id: str = "") -> list[sqlite3.Row]:
        if run_id:
            return list(self.conn.execute(
                "SELECT * FROM provenance WHERE run_id=? ORDER BY ts", (run_id,)))
        return list(self.conn.execute("SELECT * FROM provenance ORDER BY ts"))

    # ------------------------------------------------------------------
    # 图分析
    # ------------------------------------------------------------------
    def networkx(self) -> Any:
        """构造有向图（边方向 = dst(上游) → src(下游)，与协议方向一致）。"""
        if nx is None:
            raise RuntimeError("networkx 未安装：请 pip install networkx（内存图分析依赖）")
        if self._nx is None:
            g = nx.DiGraph()
            for r in self.conn.execute("SELECT node_id,domain,kind,label FROM nodes"):
                g.add_node(r["node_id"], domain=r["domain"], kind=r["kind"],
                           label=r["label"] or "")
            for r in self.conn.execute(
                    "SELECT edge_id,src,dst,relation,status,confidence FROM edges"):
                g.add_edge(r["dst"], r["src"], edge_id=r["edge_id"], relation=r["relation"],
                           status=r["status"], confidence=r["confidence"])
            self._nx = g
        return self._nx

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    def snapshot(self, out_path: str | Path | None = None) -> Path:
        """导出带指纹的 JSON 快照（可复现性验收比对用）。"""
        g = self.load_graph()
        fp = g.fingerprint()
        payload = g.to_dict()
        payload["meta"] = {**(payload.get("meta") or {}),
                           "fingerprint": fp, "saved_at": time.time()}
        out = Path(out_path) if out_path else self.path.with_suffix(".snapshot.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return out

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self) -> "GraphStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
