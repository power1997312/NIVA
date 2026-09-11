# NIVA — 核电仪控智能验证与追溯智能体

> **N**uclear **I**&C **V**erification **A**gent
> 把 `E:\Trace_NL`（需求文档追溯验证）与 `E:\SAMA-V1`（工程图纸解析与追溯）
> 整合为一个面向核电仪控 V&V 的智能体。

**当前阶段：P5 增值能力 ✅ 已完成**
（P0 9/9 · P1 22/22 · P3 9/9 · P3-B 16/16 · P4 12/12 · P5 11/11 全绿；**工具已实现 21/22**，
唯一待实现 `xdiff_trace` 属 P2；剧本 5/6 可用）
一句话即可驱动全链路：
```bash
$VENV -m niva.agent.orchestrator "对系统需求做需求追溯核查" --param downstream_pdf=... --param upstream_pdfs=...
$VENV -m niva.agent.orchestrator "为第 1 页图纸生成测试用例"
$VENV -m niva.agent.orchestrator --manifest   # 剧本清单（可用性自动判定）+ 工具面 + 权限审计
```

> **智能体形态基调（已定）**：不是"一个执行体挂 22 个工具"，而是
> **单编排器 + 4 个能力作用域**。运行时形态一个（可审计、可复现），
> 权限域四个（硬约束）。标准路径由剧本查表选工具、不经 LLM，所以工具多寡不构成负载；
> 真正要守住的是"图纸域与校验域**构造性禁止 LLM**"这条确定性边界 —— 它现在
> 是代码事实（跨域调用返回 `GATE_REJECTED`、未授权执行体拿不到 LLM 句柄），不是提示词自律。

---

## 为什么这样整合

这两个工程方法论上是**同构**的——都在做同一件事：*从非结构化工程材料中抽取出"带证据的可追溯关系"，并让人能审查它*。区别只在输入模态（文本 vs 图形）。

所以整合不是"拼接两个系统"，而是**抽出一个共同内核，把两个系统降格为两个域适配器**：

```
        ┌───────────────── L4 编排层（单智能体）─────────────────┐
        │  Planner → Router → Executor   ↕   Critic              │
        │  5 个 Playbook（标准场景查表执行，零模型规划）            │
        └────────────────────────┬──────────────────────────────┘
                                 │  统一工具契约（22 个 MCP 工具）
      ┌──────────────────────────┼──────────────────────────┐
      ▼                          ▼                          ▼
 文献域能力               跨域能力                   图形域能力
 doc.parse              bridge（图文锚定）          diagram.to_ir
 req.extract            test.synth（用例引擎）       diagram.trace
 trace.verify           impact / report
      │                          │                          │
      ▼                          ▼                          ▼
  ┌──────────────────── KERNEL · 统一追溯图 ────────────────────┐
  │  TraceNode（内容+定位） ── TraceEdge（关系+置信度+状态+证据）  │
  └────────────────────────────────────────────────────────────┘
      │                          │                          │
 doc adapter ← Trace_NL     knowledge（符号/位号/    diagram adapter ← SAMA-V1
 （业务代码不重写）           定值/用例库/标准）      （业务代码不重写）
```

**三条架构公理**（已落为代码事实，有契约测试守护）
1. **一切皆追溯边** —— 只有 `TraceEdge` 一种关系载体，消除多套异构结构并存的整合风险。
2. **证据必须可定位** —— 每条边至少一条带 `Locator` 的 `Evidence`；无证据边被图谱**硬拒绝**。
3. **判定链必须确定性** —— 数值/步骤/结论只由算法产生；LLM 只在三处出场（歧义裁决、语义成文、任务分流），且 `llm_opinion` **永不回写 `confidence`**。

---

## 目录结构

```
NIVA/
├─ niva/
│  ├─ kernel/        ★统一内核（已冻结）：model / ids / graph_store / registry
│  ├─ adapters/       绞杀者适配器
│  │  ├─ doc/legacy/      ← Trace_NL 整体迁入（由 tools/migrate_legacy.py 生成，不入 git）
│  │  └─ diagram/legacy/  ← SAMA-V1 整体迁入（同上）
│  ├─ services/       跨域能力：bridge / testcase / impact / report
│  ├─ agent/          编排层：planner / router / executor / critic / llm_provider / playbooks
│  ├─ knowledge/      声明式知识 SSOT
│  │  ├─ thresholds.yml      ★统一阈值表（62 键，含 env_override）
│  │  ├─ testcase_lib/       ★部件测试用例模板库（17 条 / 8 文件）
│  │  ├─ standards/          ★标准条款映射 + 校验规则台账 R1–R20
│  │  └─ symbols/ tags/ io/ setpoints/
│  ├─ server/        统一 MCP Server（22 工具）+ HTTP API
│  ├─ ui/            Streamlit 追溯配置 + 双栏证据联动工作台
│  ├─ config.py      统一配置与阈值访问 + legacy_import_path()
│  └─ obs.py         运行留痕（JSONL）+ 环境指纹 + 降级必可见
├─ eval/            评测脚本（R1 闸门 / 阈值同源 / 用例库校验 / P0 总闸）
├─ docs/            实施计划 / P0验收报告
├─ tests/           内核契约测试
└─ tools/           migrate_legacy.py（可重复执行的迁移脚本）
```

---

## 快速开始

```bash
# 运行时：复用 Trace_NL 的虚拟环境（已装齐 torch/pymupdf/scipy/networkx 全套，无需重装）
VENV=E:/Trace_NL/.venv/Scripts/python.exe

# 1) 迁移两个原工程（可重复执行，幂等，零删除）
$VENV tools/migrate_legacy.py

# 2) P0 验收总闸（一键复现全部 P0 结论）
$VENV eval/p0_acceptance.py

# 3) 单项检查
$VENV tests/test_kernel_contract.py        # 内核契约（三公理）10 项
$VENV eval/threshold_parity.py             # 阈值 SSOT 同源守卫 52 项
$VENV eval/validate_testcase_lib.py        # 用例库合规（类名真实性 / 期望值不写死）
$VENV eval/r1_tag_alignment.py             # R1 风险闸门：图文锚点可对齐率
$VENV eval/p1_acceptance.py                # P1 验收：22 工具 + 两条链路 + 只读收窄

# 4) 启动统一 MCP Server
$VENV -m niva.server.mcp_server                   # 内部模式：22 个工具
$VENV -m niva.server.mcp_server --readonly-only   # 对外模式：仅 10 个只读工具
$VENV -m niva.server.mcp_server --self-test       # 自检（工具面 + 权限审计）
$VENV -m niva.server.mcp_server --manifest        # 打印工具面与权限审计 JSON
```

---

## 两个必须知道的坑

**1. 两个 legacy 工程都有同名顶层模块**（`config.py`、`tests/`…），
永久挂 `sys.path` 必然互相遮蔽。必须用上下文管理器临时挂载：

```python
from niva import config as C
with C.legacy_import_path("diagram"):
    import lib_sama          # 解析到 SAMA-V1
with C.legacy_import_path("doc"):
    import config as tn      # 解析到 Trace_NL
```

**2. `scipy` 缺失会让 Trace_NL 静默退化为贪心匹配** —— 结果与金标不一致却不报错。
这是最难排查的一类故障，`eval/p0_acceptance.py` 的 B 项已硬断言其存在。

---

## 已实测的关键结论

| 结论 | 证据 |
|---|---|
| 迁移**零行为漂移** | 金标回归逐项复现冻结基线：总体 0.7345（7517/10234）、A 0.7844、B 0.6558/0.6148，**多绿/少绿/多蓝/少蓝计数亦完全一致** |
| 内核能同时装下两个域 | 契约测试 10/10；`payload` 原样保留、证据原始分不平移、指纹可复现 |
| 复用 venv 无兼容问题 | pymupdf 1.28.2 下基线逐字符复现（原工程锁 1.24.9 的担忧已消解） |
| **位号不能作为需求↔图纸的桥接锚点** | R1 实测：位号命中 **0/66 = 0%**；系统代号 30%、功能语义 23% 可用 → **P2 已改路线**（见 `docs/实施计划.md` §4） |
| 用例库可机器校验 | 类名必须命中符号字典；期望值不得写死数值（校验器已抓出 6 个 YAML 解析失败的真实缺陷） |

---

## 文档

- **`docs/实施计划.md`** —— P0–P6 阶段路线图、MVP 边界、R1 修正后的 P2 设计、评测指标、风险登记册
- **`docs/P0验收报告.md`** —— P0 交付物、9 项验收证据、行为等价性比对、遗留风险
- 原始架构设计见 `E:\NIVA\docs\核仪控验证智能体_架构设计与实施方案.md`

---

## 安全提醒

🔴 **`E:\Trace_NL\.env` 中存有明文 API Key，建议立即吊销轮换。**
`.gitignore` 已屏蔽 `.env`；`.gitignore` 同时排除了 `adapters/*/legacy/`（迁移产物可随时重建，
不应入库，否则会出现"同一份代码两个真身"）。
