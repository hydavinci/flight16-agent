# General Engineering Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Execution requires separate user authorization; this document does not authorize implementation, delegation, commits, or real engineering runs.

**Goal:** 将新工程任务统一为需求驱动、判据有来源、可通过注册工具扩展的 Agent，移除新任务对 D1/D2 和固定 Demo 指标的依赖。

**Architecture:** 复用严格能力注册、依赖图、有界执行和证据机制，引入独立的 v2 任务契约。现有 Simulink 与离线诊断通过适配器接入；旧任务通过兼容层读取和恢复。页面只消费通用展示契约，不按业务场景分支。

**Tech Stack:** Python >=3.11、Pydantic 2、FastAPI、现有 LangChain/API 与 Copilot CLI 后端、原生 JavaScript、pytest、Playwright；MATLAB 为可选的已注册计算后端。

**Spec:** `docs/superpowers/specs/2026-10-21-general-engineering-agent-design.md`

## Global Constraints

- “新任务不要求用户选择 D1/D2，也不以这两个名称作为核心任务类型。”
- “遇到能力缺口时明确说明，接入相应工具后才能执行。”
- “不包括生成或执行任意 MATLAB/Python 脚本、自动安装工具、修改源工程、控制器优化、候选方案采纳、插件市场或热加载。”
- “首版新任务使用独立的 `schema_version = "2.0"`，旧任务继续按原契约解码。”
- “每条验收判据都必须可追溯到用户消息，或用户明确采纳的已提供标准/示例。”
- “直接比较要求单位一致；单位换算只能由已注册且版本化的转换能力完成。”
- “执行成功、数值有效、满足验收标准是三个独立维度。”
- “首版不开放候选写入和采纳权限，也不把项目写入包装成只读能力。”
- “未知执行状态继续阻止重试、重规划和任务切换，直至原调用完成对账。”
- “旧任务按原始 schema 保存。”
- “保留现有上传工作台和 CLI 行为，不将其作为通用化重写的附带范围。”
- “真实 MATLAB 回归与工程执行须另行获得用户授权。”

额外执行约束：保留当前全部未提交变更，尤其是 Copilot CLI 接入；
不擅自提交、推送、stash、重置或清理用户数据。不修改 `.env`，
不占用或重启已有 8000/9001 服务。保留脱敏与最小模型上下文，
不得发送源码、凭据或未授权文件内容。首版不增加依赖。

---

## 执行准备与文件边界

本计划编写时，代码基线为 `4bea22a`，分支为 `feat/directory-verification-agent`。
工作区已有 `.env.example`、README、配置、CLI、MATLAB 清理、规划器及
`copilot.py` / `test_copilot.py` 的未提交变更。它们不是本计划可以覆盖的临时文件。

实际执行前加载 `using-git-worktrees`。若使用隔离工作区，必须保留并安全带入
上述非敏感变更及这两份文档，不复制 `.env`。不能安全保留时停止并询问，
不能用自动提交或 stash 代替用户决策。此准备现在不执行。

| 文件/区域 | 职责 |
| --- | --- |
| 新建 `runtime/resources.py` | 资源身份与位置引用；不执行工程发现 |
| 新建 `runtime/requirements.py` | 需求、判据、来源与指标引用 |
| 新建 `runtime/observations.py` | 通用数值、发现、表格、评价和交付结果 |
| 新建 `runtime/capability_metadata.py` | 能力声明与不含 callable 的版本快照 |
| 新建 `runtime/agent_contracts.py` | v2 计划、基线、执行范围、调用规格与回执 |
| 新建 `runtime/budget.py` | 可持久化的共享预算与进展计数 |
| 修改 `runtime/capabilities.py`、`loop.py`、`evidence.py` | 扩展既有边界，保留 v1 校验语义 |
| 修改 `verification/contracts.py`、`store.py` | v2 任务存储；旧类型不强制迁移 |
| 新建 `verification/agent_runtime.py` | v2 确认、执行、恢复与重试，不依赖具体算法 |
| 新建 `verification/agent_planner.py` | 需求优先的结构化规划与来源校验 |
| 新建 `verification/coordinator.py` | 会话入口、规划/执行调度与公开动作接口 |
| 新建 `verification/resources.py` | 受信任资源适配器、位置目录与指纹复核 |
| 新建 `verification/compat.py` | 旧 API 参数、任务展示和恢复适配 |
| 新建 `verification/views.py` | 通用页面展示契约与 v2 投影 |
| 新建 `verification/adapters/{__init__,simulink,diagnostics,evaluation,delivery}.py` | 已注册能力及具体后端知识 |
| 新建 `verification/diagnostic_engine.py` | 从旧诊断提取可复用的有界计算与复核流程 |
| 新建 `verification/plugins.py` | 显式启动装配，不从请求加载模块 |
| 修改 `verification/routes.py`、`web.py` | 通用请求、动作与目录位置，不增加算法分支 |
| 修改 `static/agent.html`、`agent.js`，按需修改 `agent.css` | 无场景选择的输入与通用结果展示 |
| 保留 `verification/runtime.py`、`general.py` 等旧入口 | 仅兼容原任务/客户端；不作为新任务核心 |
| 新建 `tests/agent_helpers.py` 与 `test_agent_*.py` | v2 契约、假工具、恢复与端到端验收 |

上表 `runtime/`、`verification/`、`static/` 均位于 `src/flight_agent/`。
任务中的文件路径以仓库根目录为起点。

### 接口与依赖总览

任务顺序为 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8 -> 9 -> 10。
其中任务 4 仅依赖 1、2、3；其余任务的直接前置条件在各节列出。
本计划不授权并行代理；默认在获得执行许可后逐项内联完成。

接口命名约定：

- 新任务使用 `AgentPlan`、`AgentBaseline`、`AgentTask`，不复用有 Demo 限制的 `PlanProposal`。
- `AgentTask` 沿用 `nodes`、`calls`、`decisions`、`artifacts`、`stage`、
  `result_directory`、`output_root`、`baseline.version/confirmed` 等存储外壳，
  使重启检测和归档可复用；不增加 `scenario` 字段。
- `CapabilityOutcome` 为 v2 输出包络；数值观测的 `node_id` 与上下文中的节点一致。
- `InvocationSpec` 记录实际派发身份。分析能力不额外纳入验收阈值或报告分组；
  评价/交付能力的真实输入自然包含各自判据/格式，不能从其身份中删除。
- `TaskView` 是新页面的唯一任务展示输入；旧任务由 `compat.py` 投影。

每个任务的最后一步只审阅与记录 diff，不自动 commit。
测试失败时修复本任务，不用跳过断言、扩大权限或放宽校验换取通过。

## Task 1: v2 需求、资源与结果契约

**前置：** 已审阅设计。

**Files:**
- Create: `src/flight_agent/runtime/resources.py`
- Create: `src/flight_agent/runtime/requirements.py`
- Create: `src/flight_agent/runtime/observations.py`
- Create: `src/flight_agent/runtime/agent_contracts.py`
- Create: `tests/agent_helpers.py`
- Create: `tests/test_agent_contracts.py`
- Modify: `src/flight_agent/verification/contracts.py`，仅将 `MetricValue` 改为共享类型的重导出。

**Interfaces:**
- `ResourceReference(id, kind, location, locator, fingerprint, version, metadata)`：
  `id/kind/location` 使用现有 `Name`，`fingerprint` 使用 `Digest`；
  `locator` 仅留在服务端，不发送给模型。位置 ID 由注册目录定义，不枚举 D1/D2。
- `CriterionSource(kind, reference, quote, sha256=None, version=None)`：
  kind 为 `user_message/standard_file/accepted_example`；
  文件来源必须有指纹，不接受虚构的来源版本。
- `MetricReference(node_id: Name, metric_id: Name)`；
  `Criterion(id, metric, operator, threshold, unit, conditions, source)`。
- `OutputRequirement(id, kind, node_id, item_id, description)`：
  kind 为 `metric/finding/table/artifact`，具体指标和条目 ID 不使用核心枚举。
  缺能力时 node_id/item_id 可以为空，但必须有对应的明确缺口。
- `CapabilityGap(requirement_id, missing_capability, missing_inputs, limitation, next_step)`
  放在 `requirements.py`，不得让核心结果类型反向依赖规划器。
- `TaskRequirements(objective, mode, requested_outputs, criteria, overall_requested,
  delivery_formats)`：mode 为 `analysis/verification`；
  requested_outputs 为 `list[OutputRequirement]`；
  verification 至少一条判据；analysis 不能携带验收判据或要求整体合格判定。
- `Observation(node_id, metric_id, label, value, unit, valid, reason, special, conditions, evidence)`；
  `Finding(id, text, status, evidence)`；
  `ResultTable(id, columns, rows, evidence)`；
  `CriterionCheck(criterion_id, metric, value, special, value_unit, operator,
  threshold, unit, status, reason, evidence)`；
  `EvaluationResult(status, checks: list[CriterionCheck])`；
  `CapabilityOutcome(observations, findings, tables, evaluation, artifacts, details)`。
- `TaskResult(execution_status, observations, findings, tables, evaluation,
  artifacts, unmet_requirements)` 放在 `observations.py`；
  unmet_requirements 为 `list[CapabilityGap]`，不能把部分完成显示为全部满足。
  实施审阅补充：Finding/ResultTable 带 node_id，OutputArtifact 在 EvidenceFile
  上增加逻辑 id/node_id；按生产者与逻辑 ID 匹配输出，不按文件扩展名猜测。
  已中断/失败/部分完成的验证可没有评价，但必须明确缺失输出；仅完成状态要求全量评价。
- 将现有 `MetricValue` 原样移到 `observations.py`，保留构造字段与全部有效性约束；
  `Observation` 复用其值语义，不改变旧 JSON 形状。
- `OutputReference(node_id, path)` 只含有界字符串/整数路径；
  `AgentNode(id, tool, tool_version, depends_on, inputs, bindings, resource_bindings)`；
  bindings 为顶层输入字段名到 `OutputReference` 的映射。
  resource_bindings 为能力声明的资源角色到 ResourceReference.id 的映射。
- `AgentPlanDraft(title, rationale, requirements, resource_ids, nodes,
  evaluation_node, allowed_effects, budget, gaps)` 是模型返回的提案，
  不含实际定位信息。`AgentPlan` 将 resource_ids 替换为服务端绑定的 resources；
  `AgentBaseline` 在相同字段上增加 `version/confirmed/confirmed_at`。
  每项所需输出必须有合法生产节点或显式 gap；有 gap 的方案必须标明部分完成范围。
- `AgentScope(baseline_version, confirmed, resources, allowed_effects)` 提供
  `authorize(effect: Effect) -> None`，拒绝候选写入/采纳。
- `InvocationSpec(capability, capability_version, inputs, resource_fingerprints,
  conditions, numerics, dependency_output_sha256)`；
  `InvocationRun(schema_version, id, spec, output, output_sha256, started_at,
  finished_at, artifacts)`，schema_version 固定为 `"2.0"`。
- `BudgetState(started_at, calls, rounds, no_progress)` 同样在本任务定义于
  `agent_contracts.py`，使 Task 3 可立即存储，Task 5 只实现计数执行器。

- [x] **1. 编写失败测试，并增加确定性的计划数据构造函数。**

`tests/agent_helpers.py` 的首个公共函数内容：

```python
def analysis_plan_data():
    return {
        "title": "Synthetic RMSE",
        "rationale": "Analyze explicitly supplied synthetic arrays.",
        "requirements": {
            "objective": "Report RMSE without a compliance decision.",
            "mode": "analysis",
            "requested_outputs": [{
                "id": "rmse-output", "kind": "metric",
                "node_id": "compute", "item_id": "rmse",
                "description": "Report the synthetic RMSE.",
            }],
            "criteria": [],
            "overall_requested": False,
            "delivery_formats": [],
        },
        "resources": [],
        "nodes": [{
            "id": "compute", "tool": "offline_rmse", "tool_version": "1",
            "inputs": {"observed": [0.0, 2.0], "reference": [0.0, 0.0]},
        }],
        "allowed_effects": ["read_only"],
        "budget": {
            "max_rounds": 3, "max_calls": 6,
            "max_seconds": 60, "max_no_progress": 2,
        },
    }
```

```python
import pytest
from pydantic import ValidationError
from flight_agent.runtime.agent_contracts import AgentPlan
from tests.agent_helpers import analysis_plan_data

def test_analysis_requires_neither_design_pair_nor_criteria():
    plan = AgentPlan.model_validate(analysis_plan_data())
    assert plan.requirements.criteria == []
    assert plan.evaluation_node is None
    assert not hasattr(plan, "design")

def test_verification_without_criteria_is_rejected():
    data = analysis_plan_data()
    data["requirements"]["mode"] = "verification"
    with pytest.raises(ValidationError, match="criteria"):
        AgentPlan.model_validate(data)
```

同时增加额外字段、NaN/Inf、重复节点/判据 ID、依赖环、未知引用、
无来源判据、无效数值无原因、候选权限与 `model_construct` 绕过测试。
同一指标不同工况可有不同判据 ID，不沿用旧“指标名唯一”限制。

- [x] **2. 运行红灯。**

Run: `.venv/bin/python -m pytest tests/test_agent_contracts.py -q`

Expected: 新模块或契约缺失导致失败，不是真实 MATLAB 或网络失败。

- [x] **3. 实现上述严格契约。**

沿用 `Contract`、`FiniteNumber`、`finite_mapping`、`fingerprint`、
`ResourceBudget` 和 `plan_ancestors`；列表/文本/引用路径均有界。
比较符用 `Literal["<", "<=", "==", ">=", ">"]`，不加入表达式字符串。
verification 的 evaluation_node 必须指向计划内节点，analysis 不得指定它。
运行结果还须覆盖实际要求的判据 ID；没有评价输出时不能把验证标为完成。
`AgentScope` 的授权核心为：

```python
def authorize(self, effect: Effect) -> None:
    if not self.confirmed:
        raise ValueError("Capability execution requires a confirmed task baseline")
    if effect in ("write_candidate", "adopt_candidate"):
        raise ValueError("Candidate effects are unsupported by agent tasks")
    if effect not in self.allowed_effects:
        raise ValueError(f"Effect is outside the confirmed permissions: {effect}")
```

`InvocationSpec.fingerprint` 只对自身有限 JSON 字段计算；
`InvocationRun` 必须重新验证输出摘要。
静态契约校验不能代替后续注册表、实际来源和资源路径校验。

- [x] **4. 运行绿灯与旧值语义回归。**

Run: `.venv/bin/python -m pytest tests/test_agent_contracts.py tests/test_verification_evaluation.py tests/test_runtime_kernel.py -q`

Expected: 全部通过；旧 `MetricValue` JSON 与特殊值行为不变。

- [x] **5. 审阅 diff，仅记录结果，不提交。**

## Task 2: 能力声明、授权与输出绑定

**前置：** Task 1。

**Files:**
- Create: `src/flight_agent/runtime/capability_metadata.py`
- Modify: `src/flight_agent/runtime/capabilities.py`
- Modify: `src/flight_agent/runtime/loop.py`
- Modify: `src/flight_agent/runtime/agent_contracts.py`
- Modify: `tests/agent_helpers.py`
- Create: `tests/test_agent_capabilities.py`

**Interfaces:**
- `MetricDefinition(id, label, unit, comparable_specials)`；
  `ResourceRequirement(role, kind, location=None, required=True)`；
  `Availability(state, reason)`，state 为 `available/needs_input/unavailable`。
- `CapabilityMetadata(metrics, resources, conditions_schema, validity_policy,
  planning_notes, cacheable=False)` 是严格声明，字段不得包含执行代码。
  上述声明类型均放在 capability_metadata.py，避免契约与注册表循环导入。
- `CapabilitySnapshot(name, version, effect, input_schema, output_schema, metadata)`：
  严格、可指纹化、不含 callable，服务端填入
  `AgentBaseline.capability_snapshots: dict[Name, CapabilitySnapshot]`。
  模型不能提交或覆盖这些快照。
- 扩展既有 `Capability`，增加可选 `metadata`、`readiness`、`reconcile`、`validate_plan` 字段；
  保留原参数和默认行为。readiness 签名：
  `(resources: tuple[ResourceReference, ...]) -> Availability`；
  reconcile 与 operation 输入相同，返回 `Output | None`，只允许对账。
  validate_plan 签名为 `(plan: AgentPlan, node: AgentNode) -> None`，
  用于适配器核对判据/交付输入与已确认需求，不能执行计算。
- `CapabilityRegistry.invoke/reconcile` 和 `run_loop` 接受
  `ExecutionScope | AgentScope`；用对应严格模型从 dump 重新校验。
- `validate_bindings(plan: AgentPlan, registry: CapabilityRegistry) -> None`；
  `resolve_inputs(node: AgentNode, outputs: dict[str, CapabilityOutcome]) -> dict[str, JsonValue]`，
  放在 `agent_contracts.py`，不读取任意对象属性。
- `tests.agent_helpers.register_rmse(registry, calls, *, version="1", unit="m",
  operation=None) -> None`：仅测试插件，不装配到生产目录。

- [x] **1. 写失败测试与最小测试插件。**

`RmseInput` 含等长、非空、最多 128 项的有限数值数组 `observed/reference`。
插件声明指标 `rmse` 与传入 unit，effect 为 `read_only`、cacheable 为 True：

```python
import math

def measure(context, inputs):
    calls.append(context.call_id)
    value = math.sqrt(sum(
        (observed - reference) ** 2
        for observed, reference in zip(inputs.observed, inputs.reference, strict=True)
    ) / len(inputs.observed))
    return CapabilityOutcome(observations=[Observation(
        node_id=context.node_id, metric_id="rmse", label="RMSE",
        value=value, unit=unit, valid=True,
    )])
```

工厂用 `operation` 覆盖 measure，以测试崩溃与非法输出。
测试注册单独实例、重复名称、未知指标声明、任意新单位、不可用原因、
越权、伪造已确认 scope、额外输出字段与非法特殊值。

绑定测试必须覆盖：
直接声明依赖的完整数组/字段引用可解析；非依赖节点、未知字段、非法路径、
同一输入同时出现在 inputs 与 bindings 中均被拒绝。
间接祖先的输出需要显式加入 depends_on 后才能引用，
与 AgentContext 只暴露直接声明依赖保持一致。路径索引拒绝负值和布尔值。
预检比较声明的 schema；真实值派发前再用 input_model 严格校验，
不能用虚构占位值假装通过完整输入校验。

- [x] **2. 运行红灯。**

Run: `.venv/bin/python -m pytest tests/test_agent_capabilities.py -q`

- [x] **3. 扩展注册表与共用驱动。**

保留 v1 注册实例；新 Agent 使用另一个相同类型的注册表实例，
要求每项能力有 metadata，且 output_model 恰为 `CapabilityOutcome`。
插件特有结构放在声明的 observations/findings/tables/details 中，
不增加通用包络的顶层字段，从而可在原工具卸载后验证完成回执。
目录输出只序列化 schema、声明和 readiness 结果，绝不序列化 callable。
valid=True 的特殊值必须在该指标 comparable_specials 中；
否则拒绝输出，或由工具显式给出 valid=False 和原因，不能由评价器猜测可比性。

```python
def resolve_inputs(node, outputs):
    resolved = dict(node.inputs)
    for field, reference in node.bindings.items():
        if field in resolved or reference.node_id not in node.depends_on:
            raise ValueError("Binding conflicts with the confirmed input scope")
        value = outputs[reference.node_id].model_dump(mode="json")
        for part in reference.path:
            value = value[part]
        resolved[field] = value
    return finite_mapping(resolved)
```

上面的路径访问必须在已验证的 JSON schema、容器类型、范围与路径长度内进行，
将缺失字段/错误索引转为明确校验错误，不捕获后返回空数据。
新插件增加指标或单位不改变核心常量。
能力版本及声明/schema 指纹都绑定到基线；不改版本但改变声明也不能沿用旧确认。
计算规格记录有效性策略和相应声明指纹。默认不跨任务复用未声明 cacheable 的工具。

- [x] **4. 运行绿灯与旧授权回归。**

Run: `.venv/bin/python -m pytest tests/test_agent_capabilities.py tests/test_runtime_kernel.py tests/test_runtime_loop.py -q`

Expected: v1 候选隔离/采纳检查仍有效；v2 不接受这些权限。

- [x] **5. 审阅 diff，不提交。**

## Task 3: 不可变回执、混合历史与重启恢复

**前置：** Tasks 1、2。

**Files:**
- Modify: `src/flight_agent/runtime/evidence.py`
- Modify: `src/flight_agent/verification/contracts.py`
- Modify: `src/flight_agent/verification/store.py`
- Create: `src/flight_agent/verification/compat.py`
- Create: `tests/test_agent_evidence.py`
- Create: `tests/test_agent_history.py`

**Interfaces:**
- `persist_agent_run(root: Path, run: InvocationRun) -> Path`；
  `load_agent_run(root: Path, run_id: str, spec: InvocationSpec) -> InvocationRun | None`。
- v2 回执使用 `agent/analysis-runs/{run_id}.json`，
  不改变原 `numerical/` 与 `evaluation/` 回执路径。
- 在 `verification/contracts.py` 增加 `AgentTask` 与 `AgentExecutionNode`，
  并加入 `Task` 联合；旧任务类型不加 v2 字段。
- `AgentTask` 增加 `outputs: dict[str, CapabilityOutcome]`、
  `analysis_runs: list[InvocationRun]`、预算状态、插件 checkpoint 引用和可选结果。
- `compat.py` 先提供 `is_legacy_task(task: Task) -> bool` 与
  `has_unresolved_call(task: Task) -> bool`，供后续入口使用。

- [x] **1. 写失败测试。**

```python
import pytest
from flight_agent.runtime.agent_contracts import InvocationRun, InvocationSpec
from flight_agent.runtime.contracts import fingerprint
from flight_agent.runtime.evidence import load_agent_run, persist_agent_run

def test_v2_receipt_is_immutable_without_a_design_pair(tmp_path):
    spec = InvocationSpec(
        capability="offline_rmse", capability_version="1",
        inputs={"observed": [1.0], "reference": [0.0]},
    )
    output = {"observations": [], "details": {"value": 1.0}}
    run = InvocationRun(
        id="1" * 32, spec=spec, output=output,
        output_sha256=fingerprint(output),
        started_at="2026-10-21T00:00:00Z",
        finished_at="2026-10-21T00:00:01Z",
    )
    persist_agent_run(tmp_path, run)
    assert load_agent_run(tmp_path, run.id, spec) == run
    changed_output = {"observations": [], "details": {"value": 2.0}}
    changed = InvocationRun.model_validate({
        **run.model_dump(mode="json"),
        "output": changed_output,
        "output_sha256": fingerprint(changed_output),
    })
    with pytest.raises(ValueError, match="immutable"):
        persist_agent_run(tmp_path, changed)
```

回执保留被验证的原始输出 dict，接纳时用稳定的 `CapabilityOutcome`
及原基线中的声明快照复核，不依赖升级后的工具 schema。
测试文件摘要破坏、输出摘要破坏、规格串用、重复写不同内容、
符号链接、越界、过大回执及旧回执共存。

历史测试在临时 store 中保存 v1 验证/诊断/通用任务和 v2 任务，
重载后比较旧任务 JSON；重启把 running 调用标为 interrupted，
不能更换原调用 ID、确认版本或清除未知状态。

- [x] **2. 运行红灯。**

Run: `.venv/bin/python -m pytest tests/test_agent_evidence.py tests/test_agent_history.py -q`

- [x] **3. 提取共享安全 IO，增加 v2 包装。**

共享路径、大小、摘要、临时文件、fsync 与不可变发布逻辑；
v1/v2 的模型校验和路径选择保持分离。不得把 v2 数据塞进带
controller/plant 的 `AnalysisSpec`，也不得用 `model_construct`。

存储层保留既有 8 MB 会话上限、原子写入、重启检测和独立任务目录。
旧记录没有 schema_version 时按旧类型解码，不补写版本或重新确认。

- [x] **4. 运行绿灯与历史回归。**

Run: `.venv/bin/python -m pytest tests/test_agent_evidence.py tests/test_agent_history.py tests/test_runtime_evidence.py tests/test_general_runtime.py tests/test_verification_ledger.py tests/test_conversation_bounds.py -q`

- [x] **5. 审阅 diff，不提交。**

## Task 4: 通用标量评价与交付插件

**前置：** Tasks 1、2、3。

**Files:**
- Create: `src/flight_agent/verification/adapters/__init__.py`
- Create: `src/flight_agent/verification/adapters/evaluation.py`
- Create: `src/flight_agent/verification/adapters/delivery.py`
- Create: `tests/test_agent_evaluation.py`
- Create: `tests/test_agent_delivery.py`

**Interfaces:**
- `evaluate_criteria(criteria: list[Criterion], observations: list[Observation],
  *, overall_requested: bool) -> EvaluationResult`。
- `ScalarEvaluationInput(criteria, observations, overall_requested)`；
  `scalar_evaluation_capability() -> Capability`，将 evaluate_criteria 的结果
  放入 CapabilityOutcome.evaluation。
- `DeliveryInput(title, formats, requirements)`；
  `delivery_capability() -> Capability`，只读已声明依赖的输出，
  根据已确认 requirements 和 formats 写入当前任务目录。
- `EvaluationResult.status` 可为 None（只要求逐项检验），
  `checks` 含 criterion_id、metric 引用、实际数值、比较和有效性原因。

- [x] **1. 写数值与交付失败测试。**

每个比较符都测试等于阈值、上下邻近值；`==` 不应用隐含容差。
测试同名指标不同节点/工况、单位不符、缺失值、无效值、
被工具允许或禁止的无穷大、多个区间边界和未要求整体评价。
用于构造 `Criterion` 的来源例子为：

```python
from flight_agent.runtime.requirements import Criterion

source = {
    "kind": "user_message",
    "reference": "message:0",
    "quote": "RMSE <= 0.8 m",
}
criterion = Criterion(
    id="rmse-limit",
    metric={"node_id": "compute", "metric_id": "rmse"},
    operator="<=", threshold=0.8, unit="m", conditions={},
    source=source,
)
```

同文件加入完整的标量测试：

```python
from flight_agent.runtime.observations import Observation
from flight_agent.verification.adapters.evaluation import evaluate_criteria

def test_new_metric_is_compared_without_a_core_metric_enum():
    observation = Observation(
        node_id="compute", metric_id="rmse", label="RMSE",
        value=0.9, unit="m", valid=True,
    )
    result = evaluate_criteria(
        [criterion], [observation], overall_requested=True,
    )
    assert result.status == "fail"
    assert result.checks[0].criterion_id == "rmse-limit"
```

测试“无效必需项 + 已知失败”整体 undetermined，失败行仍存在。
没有 criteria 且 analysis 模式时没有评价节点或评价结果。
交付测试用临时目录及 `python-docx` 读取生成文件：
新指标、来源、工况、无效原因和失败行必须出现；
只有 JSON 需求时不要求 PNG/MAT/Word；明确要求的 Word 缺失则交付失败。

- [x] **2. 运行红灯。**

Run: `.venv/bin/python -m pytest tests/test_agent_evaluation.py tests/test_agent_delivery.py -q`

- [x] **3. 实现注册插件，不修改旧评价。**

比较使用明确映射，不使用 eval：

```python
import operator

COMPARATORS = {
    "<": operator.lt, "<=": operator.le, "==": operator.eq,
    ">=": operator.ge, ">": operator.gt,
}

def aggregate(statuses):
    if not statuses or "undetermined" in statuses:
        return "undetermined"
    return "fail" if "fail" in statuses else "pass"
```

比较前严格匹配生产节点、指标、工况、单位及有效性。
评价输入必须与已确认的判据一致，禁止插件或 LLM 替换。
评价插件的 validate_plan 核对 criteria、overall_requested 与生产引用；
交付插件的 validate_plan 核对 requirements/formats 与整个任务需求。
这些检查通过注册回调完成，核心不比较具体插件名称。
通用交付校验依赖回执与附件摘要，正文中区分执行、有效性和评价。
写 Word/JSON 是明确 `write_delivery` 能力；内部审计回执不是隐含的业务报告节点。

- [x] **4. 运行绿灯和旧报告回归。**

Run: `.venv/bin/python -m pytest tests/test_agent_evaluation.py tests/test_agent_delivery.py tests/test_verification_evaluation.py tests/test_verification_reports.py -q`

- [x] **5. 审阅 diff，不提交。**

## Task 5: v2 有界执行、指纹复用和未知状态对账

**前置：** Tasks 1、2、3、4。

**Files:**
- Create: `src/flight_agent/runtime/budget.py`
- Create: `src/flight_agent/verification/agent_runtime.py`
- Create: `src/flight_agent/verification/resources.py`
- Modify: `src/flight_agent/verification/contracts.py`
- Modify: `tests/agent_helpers.py`
- Create: `tests/test_agent_runtime.py`
- Create: `tests/test_agent_recovery.py`

**Interfaces:**
- 使用 Task 1 的 `BudgetState`，保存在 `AgentTask`，不再定义第二种同名状态。
- `BudgetAccount(budget, state, checkpoint)`：
  `charge_call() / charge_round() / note_progress(new_evidence: bool) -> None`；
  `remaining_seconds() -> float`。扣费在派发前持久化，恢复不返还预算。
- `AgentContext(directory, scope, resources, node_id, call_id, outputs, budget,
  load_checkpoint, save_checkpoint)`；
  outputs 仅含声明的依赖，checkpoint 仅允许当前调用自己的目录。
  `resource(role: Name) -> ResourceReference` 只查该节点 resource_bindings。
  不把整个 baseline/用户判据放进分析上下文；需要需求的交付工具用严格输入声明，
  使影响输出的业务输入进入该工具自身指纹。
- `AgentExecutor(store, tools, resources)`：
  `propose(conversation: Conversation, plan: AgentPlan) -> Conversation`；
  `confirm(conversation_id: str, version: int, *, trusted: bool) -> Conversation`；
  `resume(conversation_id: str) -> Conversation`；
  `retry(conversation_id: str, version: int, *, trusted: bool) -> Conversation`。
- `ExecutionUncertain` 为执行边界异常，区别于已证实的失败；
  放在 `agent_runtime.py`，适配器只通过它报告无法证明终态的调用。
- `ResourceRegistry` 在本任务先定义于 `verification/resources.py`：
  `verify(resources: list[ResourceReference]) -> None`；
  空资源列表无需后端，真实资源 verifier 在 Task 7 注册。

- [x] **1. 写失败测试。**

```python
import pytest
from flight_agent.runtime.agent_contracts import AgentPlan
from flight_agent.runtime.capabilities import CapabilityRegistry
from flight_agent.verification.agent_runtime import AgentExecutor
from flight_agent.verification.resources import ResourceRegistry
from flight_agent.verification.store import ConversationStore
from tests.agent_helpers import analysis_plan_data, register_rmse

def test_analysis_executes_once_without_a_forced_judge_or_report(tmp_path):
    store = ConversationStore(tmp_path / "state")
    tools = CapabilityRegistry()
    calls = []
    register_rmse(tools, calls)
    executor = AgentExecutor(store, tools, ResourceRegistry())
    conversation = store.create()
    pending = executor.propose(
        conversation, AgentPlan.model_validate(analysis_plan_data()),
    )
    assert calls == []
    done = executor.confirm(
        pending.id, pending.task.baseline.version, trusted=True,
    )
    assert done.status == "completed"
    assert len(calls) == 1
    assert done.task.result.evaluation is None
    assert done.task.result.observations[0].metric_id == "rmse"
    assert done.task.result.observations[0].value == pytest.approx(2 ** 0.5)
```

恢复测试：计算完成、持久化接纳前模拟进程退出；只按原 ID 和规格找回执，
不得再次 operation。无回执且无可用 reconcile 时阻止重试/重规划/切换；
reconcile 仅查原 Worker ID。工具升级可读取旧完成回执，不能执行旧未完成节点。
测试模型/输入/工况变化需重确认；仅阈值变化创建新基线复用计算，旧评价不变。

预算测试以可控时钟/伪调用推进计数，不实际等待：
耗时、调用数、轮次、无进展均有边界；嵌套工具不能重置外层预算。

- [x] **2. 运行红灯。**

Run: `.venv/bin/python -m pytest tests/test_agent_runtime.py tests/test_agent_recovery.py -q`

- [x] **3. 实现派发前落盘和严格接纳。**

```python
inputs = tools.validate_inputs(node.tool, resolve_inputs(node, task.outputs))
resources.verify(task.baseline.resources)
spec = build_invocation_spec(task.baseline, node, inputs, task.outputs)
budget.charge_call()
call = persist_running_call(conversation, task, node, spec)
context = build_context(conversation, task, node, call, budget)
output = tools.invoke(node.tool, inputs, context, scope)
run = build_invocation_run(call, spec, output)
persist_agent_run(directory, run)
accept_verified_run(task, node, call, run)
store.save(conversation)
```

在本文件定义上述 helper：

```text
build_invocation_spec(baseline: AgentBaseline, node: AgentNode,
  inputs: dict[str, JsonValue], outputs: dict[str, CapabilityOutcome]) -> InvocationSpec
build_invocation_run(call: ToolCall, spec: InvocationSpec,
  output: dict[str, JsonValue]) -> InvocationRun
accept_verified_run(task: AgentTask, node: AgentExecutionNode,
  call: ToolCall, run: InvocationRun) -> None
persist_running_call(conversation: Conversation, task: AgentTask,
  node: AgentExecutionNode, spec: InvocationSpec) -> ToolCall
build_context(conversation: Conversation, task: AgentTask,
  node: AgentExecutionNode, call: ToolCall, budget: BudgetAccount) -> AgentContext
```

AgentExecutionNode 保留 AgentNode 的接口并增加可变执行状态；
规格始终和不可变基线重新比对。persist_running_call 必须在 operation 前保存。
其输入输出均来自已确认计划或经过校验的真实结果，不读模型生成的“完成”标记。
跨任务复用必须通过原回执/声明和数值身份校验，并记录来源；
附件复制到新任务的受限路径前后都校验摘要，不覆盖原回执或原附件。
生产节点引用发生变化时显式保存派生映射，不把原观测冒充另一个节点新算的结果。

确认时和每次派发前重新校验计划、能力版本、输入、资源和效果。
当前任务目录中的审计持久化是执行记录，插件不因此取得其他目录写权限。
外部成果目录/业务交付仍要求 `write_delivery`。
异常日志与错误保留；`ExecutionUncertain` 落为 interrupted，
不能用通用 catch 将所有运行中的调用标为“可安全重试的失败”。

- [x] **4. 运行绿灯和现有执行保护回归。**

Run: `.venv/bin/python -m pytest tests/test_agent_runtime.py tests/test_agent_recovery.py tests/test_general_runtime.py tests/test_verification_replanning.py tests/test_runtime_loop.py -q`

- [x] **5. 审阅 diff，不提交。**

## Task 6: 需求优先规划与来源校验

**前置：** Tasks 1、2、3、5。

**Files:**
- Create: `src/flight_agent/verification/agent_planner.py`
- Modify: `src/flight_agent/verification/planner.py`，提取可复用的结构化调用边界与脱敏逻辑。
- Create: `tests/test_agent_planner.py`
- Modify: `tests/test_copilot.py`，仅扩展新 schema 的假 CLI 测试。

**Interfaces:**
- `AgentDecision(action, text, plan, gaps)`：
  action 为 `answer/clarify/capability_gap/plan`，各分支字段一致性严格校验。
  plan 为 Task 1 的 `AgentPlanDraft | None`；gaps 使用 Task 1 的 CapabilityGap。
- `AgentPlanner.propose(message, conversation, *, catalog, resources,
  sources) -> AgentDecision`。
- `validate_sources(requirements: TaskRequirements, sources: dict[str, CriterionSource])
  -> None`：引用真实存在、quote 与授权来源一致、文件摘要一致。
- `bind_plan(draft: AgentPlanDraft, resources: dict[str, ResourceReference],
  sources: dict[str, CriterionSource], catalog) -> AgentPlan`：
  仅服务端解析资源 ID，填入真实定位与指纹；模型不得回传 ResourceReference。
- 复用既有 `VerificationPlanner._call` 的受限 API/Copilot 调用，
  提取为明确公共 `StructuredPlanner.call(name, schema, context, *, system_prompt)`；
  旧规划器继承或组合它，不复制另一套模型客户端。
  StructuredPlanner 保留 `__init__(model: BaseChatModel | None = None)`；
  AgentPlanner 继承该构造方式，便于注入现有 ScriptedModel。

- [x] **1. 写结构化假模型失败测试。**

沿用 `tests.helpers.tool_message` 和现有注入模型方式，
构造以下实际响应并检查后端调用计数为零：
知识回答、缺判据澄清、能力缺口、纯分析方案、仅一个指标的验证方案、
两个工况的不同阈值、未知工具、无效来源和伪造文件版本。

最小澄清测试内容：

```python
from flight_agent.runtime.capabilities import CapabilityRegistry
from flight_agent.verification.agent_planner import AgentPlanner
from flight_agent.verification.store import ConversationStore
from tests.agent_helpers import register_rmse
from tests.helpers import ScriptedModel, tool_message

def test_missing_acceptance_standard_does_not_compute(tmp_path):
    calls = []
    tools = CapabilityRegistry()
    register_rmse(tools, calls)
    model = ScriptedModel(responses=[tool_message("propose_agent", {
        "action": "clarify", "text": "请提供 RMSE 的合格阈值。",
    })])
    store = ConversationStore(tmp_path / "state")
    conversation = store.create()
    decision = AgentPlanner(model=model).propose(
        "验证 RMSE 是否合格", conversation,
        catalog=tools.catalog(), resources={}, sources={},
    )
    assert decision.action == "clarify"
    assert decision.plan is None
    assert calls == []
    assert store.get(conversation.id).task is None
    assert "10%" not in decision.text
```

另一个测试让假模型直接返回“验证 + 空判据”的 plan，
要求严格校验失败且不创建已确认任务，不把它修补成默认阈值。
用唯一的自定义阈值如 7.25%、2.4 秒验证提案未被 Demo 值替换。

- [x] **2. 运行红灯。**

Run: `.venv/bin/python -m pytest tests/test_agent_planner.py tests/test_copilot.py -q`

- [x] **3. 实现不含场景默认值的规划上下文。**

新提示词包含以下约束，不使用原 D1 SYSTEM_PROMPT：

```text
按用户需求与已注册能力提出结构化响应，不选择业务场景。
验收判据必须引用已提供且获准使用的来源；缺少必要标准先澄清。
纯分析不得强加 PASS/FAIL。只检验用户要求的指标。
未知能力、工况或输入必须明确报告；禁止代码生成和隐含回退。
目录、版本、权限和历史确认由程序验证，不得宣称已经获得新授权。
```

模型只见资源 ID、可用性、能力 schema、获准的元数据/标准条款和真实结果摘要。
本地定位信息与敏感文本留在服务端。
消息来源 ID 使用当前会话的 `message:{conversation_id}:{index}`；
文件来源只接受当前登记资源，不能引用其他会话或任意路径。
保留允许长度内的完整脱敏需求；若上下文必须截断，明确标记并澄清，
不能静默截去后半段阈值。
首版不新增 PDF/OCR 读取器；未注册的标准读取方式报告缺口，
用户可直接提供相关条款。已登记的结构化标准须在本地校验来源再送入规划。

方案覆盖 required outputs，缺项保留为 gap；部分方案必须明确不完整并重新确认。
新任务不调用旧 `plan_intent` 的 D1/D2 工作流分派，
不要求所有任务有 controller/plant。
保留 Copilot 现有可执行文件限制、进程清理、JSONL 校验和无自动重试行为。

- [x] **4. 运行绿灯与两种模型调用边界回归。**

Run: `.venv/bin/python -m pytest tests/test_agent_planner.py tests/test_copilot.py tests/test_general_orchestration.py tests/test_diagnosis_planner.py -q`

Expected: 只使用假模型/假 CLI，无真实模型或工程请求。

- [x] **5. 审阅 diff，不提交。**

## Task 7: Simulink、离线诊断和显式插件装配

**前置：** Tasks 1、2、3、4、5、6。

**Files:**
- Create: `src/flight_agent/verification/adapters/simulink.py`
- Create: `src/flight_agent/verification/adapters/diagnostics.py`
- Create: `src/flight_agent/verification/diagnostic_engine.py`
- Create: `src/flight_agent/verification/plugins.py`
- Modify: `src/flight_agent/verification/resources.py`
- Modify: `src/flight_agent/verification/diagnosis.py`
- Create: `tests/test_agent_adapters.py`
- Create: `tests/test_agent_diagnostic_engine.py`

**Interfaces:**
- `ResourceAdapter(name, kind, location, inspect, verify, browse)`；
  `ResourceRegistry.register(adapter)`；
  `inspect(adapter_name, locator) -> ResourceReference`；
  `locations() -> list[ResourceLocation]`，位置包含 ID、显示名称和可用性。
- `simulink_capabilities(backend: AnalysisBackend | None) -> list[Capability]`；
  `SimulinkInput(trim_point)`，资源通过 node.resource_bindings 的 project 角色取得。
  首版注册名为 simulink_step_response / simulink_stability_margins，
  input_model 的 trim_point 为 Literal[1]，如实表达当前工具边界。
- `DiagnosisInput(proposal, capability_versions)`；
  资源通过 diagnostic_data 角色取得，不从任意 inputs 路径读取。
  proposal 使用已校验的原 `DiagnosisProposal` 计划分支，
  其假设、数据、只读能力和内部预算全部出现在外层确认输入中。
- `DiagnosticEngine(tools, planner)`：
  `run(task: DiagnosisTask, *, checkpoint, budget: BudgetAccount) -> DiagnosisTask`。
- `diagnostic_capability(planner: VerificationPlanner) -> Capability`。
- `register_builtin_plugins(tools, resources, *, backend: AnalysisBackend | None,
  planner: VerificationPlanner) -> None`；
  启动清单只包含本地受信任工厂，不接受请求提供的模块名。
  缺 Worker 配置时注册不可用原因，不在装配阶段阻断知识/离线入口。

- [x] **1. 写适配器失败测试。**

使用 `project_tree` 和现有假 AnalysisBackend：
适配器建立 `AnalysisRunRequest`，不建立 `EvaluationBaseline` 或补充判据。
只有用户要求的分析被派发；超出支持的配平点拒绝，不能改成 1。
Fake Worker 回传两个指标也只评价所要求的一个。
readiness 缺 Worker 时不阻断无 Worker 的能力。

```python
import pytest
from pydantic import ValidationError
from flight_agent.verification.adapters.simulink import simulink_capabilities
from tests.test_verification_runtime import ControlledBackend

def test_current_simulink_tool_rejects_unsupported_trim_without_dispatch():
    backend = ControlledBackend()
    capabilities = simulink_capabilities(backend)
    step = next(item for item in capabilities if item.name == "simulink_step_response")
    with pytest.raises(ValidationError):
        step.input_model.model_validate({"trim_point": 2})
    assert backend.calls == []
```

数值结果转换断言保留原值、有效性、原因、实际工况与附件摘要；
Worker 重复状态查询使用同一 request.id 和 `resume=True`。
目录检查沿用现有文件数/大小、无 symlink、源/输出分离与指纹检查。

诊断测试保留两个成功观测轮次、假设更新、unknown、无进展终止、
内部能力版本变化与中断恢复；外层预算耗尽后不得再调用内部工具。
外层 task 不被旧 `DiagnosisRuntime.propose` 替换。

- [x] **2. 运行红灯。**

Run: `.venv/bin/python -m pytest tests/test_agent_adapters.py tests/test_agent_diagnostic_engine.py -q`

- [x] **3. 封装已有数值能力，提取诊断引擎。**

Simulink 使用已有 `AnalysisRunRequest`、`RemoteSimulinkBackend.execute`、
`AnalysisResult` 和 Worker 接口。闭合分析枚举允许存在于此适配器，
不移动到通用核心；不修改 MATLAB 数值算法。

```python
resource = context.resource("project")
request = AnalysisRunRequest(
    id=context.call_id,
    project_root=resource.locator,
    project_fingerprint=resource.fingerprint,
    trim_point=inputs.trim_point,
    analyses=[registered_analysis_kind],
    trusted=context.scope.confirmed,
    timeout_seconds=bounded_timeout,
)
```

`registered_analysis_kind` 是该工厂固定注册的分析，
`bounded_timeout` 不超过工具上限和共享剩余预算。
trusted 只能在外层已核对该资源、允许 execute_project 且用户明确同意后使用；
不能单凭一个 confirmed 布尔值绕过这些检查。

将 `diagnosis.py` 中规格校验、分析执行、证据复核和下一轮判断提取到引擎。
旧 runtime 负责会话状态和旧交付；新插件负责其 checkpoint，
两者复用同一计算引擎，不维护两套诊断算法。
插件内部的真实设计来自诊断 manifest，不能伪造 controller/plant。
内部每次派发/复核同时遵守原范围和共享预算；先保存 pending，再执行。
外层对账加载并验证原 checkpoint/内部回执，不从头再跑诊断。

所有内部回执作为附件注册到外层证据。新报告可使用通用 findings/table，
旧报告继续走原格式。新增能力只需增加工厂并登记到装配清单。

- [x] **4. 运行绿灯与已有诊断/Worker 回归。**

Run: `.venv/bin/python -m pytest tests/test_agent_adapters.py tests/test_agent_diagnostic_engine.py tests/test_diagnosis_runtime.py tests/test_verification_worker.py tests/test_verification_projects.py tests/test_verification_matlab.py -m "not matlab" -q`

- [x] **5. 审阅 diff，不提交；不进行真实 MATLAB 运行。**

## Task 8: 通用会话入口、动作接口与目录位置

**前置：** Tasks 3、5、6、7。

**Files:**
- Create: `src/flight_agent/verification/coordinator.py`
- Modify: `src/flight_agent/verification/compat.py`
- Modify: `src/flight_agent/verification/routes.py`
- Modify: `src/flight_agent/verification/runtime.py`，仅提供共享 store 注入供旧任务恢复。
- Modify: `src/flight_agent/web.py`
- Create: `tests/test_agent_web.py`
- Modify: `tests/test_directories.py`
- Modify: `tests/test_native_directories.py`
- Modify: `tests/agent_helpers.py`

**Interfaces:**
- `EngineeringRuntime(store, planner, executor, resources, legacy)`：
  `queue_message(conversation_id, message, project_root=None, *,
  output_root=None, location=None, scenario=None) -> Conversation`；
  `continue_submission(conversation_id) -> Conversation`；
  `decide(conversation_id, action, *, version=None, trusted=False) -> Conversation`。
- `MessageRequest` 增加注册位置 `location`；scenario 仅为弃用兼容字段。
- `translate_legacy_request(request, locations) -> MessageRequest` 在 compat 中，
  只转译位置等信息，不注入判据或创建新 v1 任务。
- 旧任务的 `resume/retry/replan` 由 compat 调原 runtime；
  新任务动作全部进入 AgentExecutor，不按工具名称分派。
- `/api/directories` 与 `/api/directories/select` 接受 `location`，
  根据 ResourceRegistry 选择已注册位置；output 固定在 Agent 主机。
- `GET /api/conversations/capabilities` 暴露当前能力目录；
  `GET /api/resource-locations` 暴露注册位置目录；固定路由先于 ID 路由注册。
- `create_web_app` 增加可选 engineering_runtime 注入，
  保留原 verification_runtime 测试/兼容注入；两者同时提供时显式拒绝。
- `tests.agent_helpers.make_agent_web(tmp_path, decisions)` 返回
  `(app, runtime, calls, tools)`；
  `wait_for_conversation(client, conversation_id, headers, ready)` 有界等待所需状态，
  不能把任意非 running 状态当作成功。

- [x] **1. 写 API 失败测试。**

保留现有 `/api/conversations` 创建、消息、确认、续接、重试和交付路径。
使用 TestClient、显式假模型与临时 store：
请求无 scenario 可回答/规划；验证缺标准不计算；
过期版本、trusted=False、跨源、错误 token、忙碌和非法状态仍拒绝。
已知失败后工程发生变化，新需求可形成新的未确认方案，不沿用旧指纹；
旧 error 不能覆盖新澄清，但原失败记录与未知执行保护必须保留。

目录测试覆盖 agent/worker 注册位置、明确冲突的 scenario/location、
无位置的歧义输入、未知位置、取消/晚到原生选择响应、远程 Worker 拒绝原生选取。
旧目录参数可转译，但新请求不默认按 D1 路径解释。

测试 helper 的内容：

```python
import time
from flight_agent.runtime.capabilities import CapabilityRegistry
from flight_agent.verification.adapters.delivery import delivery_capability
from flight_agent.verification.adapters.evaluation import scalar_evaluation_capability
from flight_agent.verification.agent_planner import AgentPlanner
from flight_agent.verification.agent_runtime import AgentExecutor
from flight_agent.verification.coordinator import EngineeringRuntime
from flight_agent.verification.resources import ResourceRegistry
from flight_agent.verification.store import ConversationStore
from flight_agent.web import create_web_app
from tests.helpers import ScriptedModel, tool_message

def make_agent_web(tmp_path, decisions):
    calls = []
    tools = CapabilityRegistry()
    register_rmse(tools, calls)
    tools.register(scalar_evaluation_capability())
    tools.register(delivery_capability())
    resources = ResourceRegistry()
    store = ConversationStore(tmp_path / "state")
    planner = AgentPlanner(model=ScriptedModel(responses=[
        tool_message("propose_agent", item) for item in decisions
    ]))
    executor = AgentExecutor(store, tools, resources)
    runtime = EngineeringRuntime(store, planner, executor, resources, legacy=None)
    app = create_web_app(tmp_path, engineering_runtime=runtime)
    return app, runtime, calls, tools

def wait_for_conversation(client, conversation_id, headers, ready):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(
            f"/api/conversations/{conversation_id}", headers=headers,
        )
        assert response.status_code == 200
        record = response.json()
        if ready(record):
            return record
        if record["status"] == "failed":
            raise AssertionError(record["error"])
        time.sleep(0.01)
    raise AssertionError("Conversation did not reach the required state")
```

知识 API 测试内容：

```python
from fastapi.testclient import TestClient
from tests.agent_helpers import make_agent_web, wait_for_conversation

def test_new_request_needs_no_scenario_directory_or_worker(tmp_path):
    app, runtime, calls, _ = make_agent_web(tmp_path, [{
        "action": "answer", "text": "这里是知识说明，不是工程计算结果。",
    }])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.get("/api/bootstrap").json()["token"]
        headers = {"x-flight-token": token}
        response = client.post("/api/conversations", headers=headers, json={
            "message": "解释 RMSE 的含义",
        })
        assert response.status_code == 202
        record = wait_for_conversation(
            client, response.json()["id"], headers,
            lambda item: item["status"] == "completed",
        )
        assert record["task"] is None
        assert calls == []
```

- [x] **2. 运行红灯。**

Run: `.venv/bin/python -m pytest tests/test_agent_web.py tests/test_directories.py tests/test_native_directories.py -q`

- [x] **3. 替换新任务入口，不在路由新增场景判断。**

```python
record = runtime.queue_message(
    conversation_id, request.message, request.project_root,
    output_root=request.output_root, location=request.location,
    scenario=request.scenario,
)
```

JobManager 仍统一调度，路由的动作判断移至公开 `decide`，
避免路由直接依赖 `VerificationTask` 和旧私有 `_known_failed_run`。
兼容适配仅看记录版本/旧类型，不把新任务退回 Demo。
同一应用只初始化一次 ConversationStore，旧 runtime 注入这个实例，
防止第二次初始化误把活跃调用标为重启中断。

bootstrap 不因 Worker 未配置而禁用知识/离线任务；
所需 Worker 的不可用信息在具体能力预检中显示。
显式注入模型时按该模型报告就绪，不要求测试使用真实模型凭据；
生产环境仍按 ModelSettings 校验，bootstrap 不暴露配置秘密。
保留同源、Web token、可信主机、Worker token、请求体和文件大小边界。
CLI 原有命令/选项和上传 workbench 继续可用，弃用字段由兼容层接收。

- [x] **4. 运行绿灯和非目录工作台回归。**

Run: `.venv/bin/python -m pytest tests/test_agent_web.py tests/test_directories.py tests/test_native_directories.py tests/test_verification_web.py tests/test_general_web.py tests/test_diagnosis_web.py tests/test_web.py tests/test_directory_cli.py -q`

旧数值契约测试可直接注入旧 runtime；测试新默认入口必须注入 EngineeringRuntime，
不能为了保留过时的场景预期而让生产入口继续走旧规划。

- [x] **5. 审阅 diff，不提交、不重启现有服务。**

## Task 9: 通用展示、移除场景选择与历史投影

**前置：** Tasks 3、4、5、8。

**Files:**
- Create: `src/flight_agent/verification/views.py`
- Modify: `src/flight_agent/verification/compat.py`
- Modify: `src/flight_agent/verification/routes.py`
- Modify: `src/flight_agent/static/agent.html`
- Modify: `src/flight_agent/static/agent.js`
- Modify: `src/flight_agent/static/agent.css`，仅必要的通用表格/来源样式。
- Create: `tests/test_agent_views.py`
- Create: `tests/test_agent_browser.py`
- Modify: `tests/test_verification_browser.py`
- Modify: `tests/test_diagnosis_browser.py`
- Modify: `tests/test_general_browser.py`

**Interfaces:**
- `TaskView(schema_version, title, baseline_version, confirmation, resources,
  requirements, permissions, budget, nodes, observations, findings, tables,
  evaluation, gaps, artifacts, error)`。
- 展示使用 `RequirementView` / `CriterionView`，而不是强行构造新的业务基线；
  CriterionView 的 source 可为空。旧记录没有来源时显示“历史记录未提供来源”，
  不能伪造 CriterionSource，也不能改写旧的评价。
- `to_task_view(task: AgentTask) -> TaskView`；
  `to_legacy_view(task: Task) -> TaskView` 只在 compat 中识别旧类型。
- 新增 `GET /api/conversations/{id}/view`，返回通用会话/任务投影；
  原 GET 记录接口不删除，旧客户端继续读取原结构。
- `renderTask(view)` 只按上述通用字段渲染，使用 textContent，
  不执行工具输出中的 HTML、JS 或表达式。

- [x] **1. 写投影和浏览器失败测试。**

```python
from playwright.sync_api import expect, sync_playwright
from tests.agent_helpers import make_agent_web
from tests.helpers import serve_app

def test_new_page_has_a_single_goal_entry(tmp_path):
    app, _, _, _ = make_agent_web(tmp_path, [])
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url)
            expect(page.locator("#scenario")).to_have_count(0)
            expect(page.get_by_label("任务需求")).to_be_visible()
            expect(page.locator("#baseline-heading")).to_have_text("执行方案")
        finally:
            browser.close()
```

在 1280 与 390 宽度验证：纯分析标记未要求评价、RMSE 新指标与新单位、
来源与工况、已知失败和总体不确定同时可见、能力缺口、历史结果、无横向溢出。
确认版本变更、会话切换及重规划后 trust 复选框重置。
只读能力不声称执行了可信工程代码；execute_project 仍显示明确代码警告。
浏览/选择目录不能提交任务或授予信任。

旧浏览器用例删除的是对已取消选择器的操作，不是确认、数据隔离、
恢复、摘要和安全边界断言；旧任务通过预置真实旧记录测试展示。

- [x] **2. 运行红灯。**

Run: `RUN_BROWSER_TESTS=1 .venv/bin/python -m pytest tests/test_agent_views.py tests/test_agent_browser.py -q`

仅使用临时 TestClient/临时端口，不连接正在运行的 8000/9001。

- [x] **3. 实现统一渲染和位置选择。**

删除 scenario 下拉框及发送/恢复其值的代码。
“验证需求”改为“任务需求”；目录位置来自注册位置目录，
不是把 D1/D2 改名藏进另一个任务选择器。
展示评价前先看是否实际要求评价，不能用缺失 evaluation 推断失败或通过。

```javascript
function renderEvaluation(view) {
  if (!view.evaluation) {
    evaluation.textContent = view.requirements.mode === "analysis"
      ? "本任务未要求合格判定"
      : "尚无可用评价，详见任务状态与缺项";
    return;
  }
  renderChecks(view.evaluation.checks);
}
```

在同一文件定义 `renderChecks(checks)`，按数据逐行生成：
指标、工况、实测值、判据/来源、状态与原因。
总体状态独立展示，不能覆盖已失败的判据。
旧状态与记录不可为适应页面而重写。
所有文本、附件 URL、schema 外字段按既有边界处理，
不插入来自工具或模型的原始 HTML。

- [x] **4. 运行绿灯与原生选择/历史浏览器回归。**

Run: `RUN_BROWSER_TESTS=1 .venv/bin/python -m pytest tests/test_agent_views.py tests/test_agent_browser.py tests/test_verification_browser.py tests/test_diagnosis_browser.py tests/test_general_browser.py -q`

若仅因缺少 Playwright 浏览器失败，才安装对应 Chromium；
不得跳过这些测试来声称页面验收完成。

- [x] **5. 审阅 diff，不提交。**

## Task 10: 第三种工具的端到端扩展证明与发布前验收

**前置：** Tasks 1-9。

**Files:**
- Create: `tests/test_agent_acceptance.py`
- Modify: `tests/agent_helpers.py`
- Modify: `README.md`
- Modify: `docs/d1-agent.md`
- Modify: `docs/d2-agent.md`
- Modify: `src/flight_agent/cli.py`，只更新新默认入口的帮助文字，保留既有选项。
- Modify: `tests/test_directory_cli.py`，保留兼容命令断言，增加新入口请求形状检查。

**Interfaces:**
- 只通过公开的注册、消息、确认、查询和下载接口完成验收。
- RMSE 仍为测试插件，不进入生产装配，不伪称真实 MATLAB/工程分析。
- 文档提供最小注册例子，明确插件包和装配清单是扩展点，
  无需修改核心运行时、路由或页面。

- [x] **1. 写完整端到端验收测试。**

```text
安装测试注册项 offline_rmse
 -> 请求纯分析
 -> 等待确认且计算调用为 0
 -> 确认后实际运行测试插件，产生新指标与不可变回执
 -> 请求 RMSE <= 0.8 m
 -> 新基线含原用户来源，仅增加评价/交付
 -> 确认后复用原计算、重新评价并交付
 -> 原基线、原计算次数、原回执均不变
```

测试同一进程中再注册不同名称/单位的测试能力，
不改核心/路由/静态文件即可通过 API 与浏览器运行。
校验源码中无对 `offline_rmse` 的生产分支；
生产装配不包含该测试插件。

同时覆盖设计验收表：缺阈值、未知工况、缺工具、部分任务需确认、
非 F16、混合历史、未知调用禁止切换、模型双后端严格输出。

公开 API 的纯分析验收测试：

```python
import pytest
from fastapi.testclient import TestClient
from tests.agent_helpers import (
    analysis_plan_data, make_agent_web, wait_for_conversation,
)

def test_registered_rmse_runs_through_public_confirmation(tmp_path):
    draft = analysis_plan_data()
    draft.pop("resources")
    draft["resource_ids"] = []
    app, _, calls, _ = make_agent_web(tmp_path, [{
        "action": "plan", "text": "请确认合成数据计算范围。", "plan": draft,
    }])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
        response = client.post("/api/conversations", headers=headers, json={
            "message": "分析合成数组 [0,2] 与 [0,0] 的 RMSE，不判定合格。",
        })
        assert response.status_code == 202
        conversation_id = response.json()["id"]
        pending = wait_for_conversation(
            client, conversation_id, headers,
            lambda item: (item.get("pending_decision") or {}).get("kind") == "confirm_baseline",
        )
        assert calls == []
        response = client.post(
            f"/api/conversations/{conversation_id}/confirm", headers=headers,
            json={"version": pending["task"]["baseline"]["version"], "trusted": True},
        )
        assert response.status_code == 202
        done = wait_for_conversation(
            client, conversation_id, headers,
            lambda item: item["status"] == "completed",
        )
        assert len(calls) == 1
        result = done["task"]["result"]
        assert result["evaluation"] is None
        assert result["observations"][0]["value"] == pytest.approx(2 ** 0.5)
```

- [x] **2. 运行验收并核对测试有效性。**

Run: `.venv/bin/python -m pytest tests/test_agent_acceptance.py -q`

Expected: 缺少真实链路或错误复用/评价时失败，不是固定返回 fixture 数字通过。
执行记录：已有通用链路首跑 3 passed，未新增核心逻辑或伪造红灯；
断言实际合成计算、数值调用次数、来源、下载字节/哈希及旧交付不可变。

- [x] **3. 修复链路并更新直接相关文档。**

README 主流程改为统一需求入口；示例值注明只在主动采纳时使用。
旧 D1/D2 文档标记为历史契约/现有适配器说明，不能继续声称页面存在该选择。
记录真实适配器限制、判据来源、计算质量默认策略、目录主机、
授权、未知运行恢复、扩展注册和两种 LLM provider 的边界。
CLI 保留旧参数兼容，默认操作无需指定场景。

不新增产品功能以绕过验收。核心若仍要求 Demo 字段，
回到对应任务解除耦合，不为测试插件写特殊分支。

- [x] **4. 运行最终验证。**

先运行本计划所有 `test_agent_*.py`。通过后执行：

```bash
.venv/bin/python -m pytest tests -m "not matlab" -q
RUN_BROWSER_TESTS=1 .venv/bin/python -m pytest \
  tests/test_agent_browser.py tests/test_verification_browser.py \
  tests/test_diagnosis_browser.py tests/test_general_browser.py \
  tests/test_web_browser.py -q
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src
git --no-pager diff --check
```

执行记录：完整非 MATLAB 回归 1102 passed、68 skipped、14 deselected；
五组浏览器回归 69 passed。Ruff、mypy（68 个源文件）、JavaScript 语法及
`git diff --check` 均通过。此记录不代表真实 MATLAB 或模型服务联调通过。
依赖只在上述验证明确报告缺失后按现有 manifest 恢复。
真实 MATLAB 测试仍不运行，除非用户另行授权；
无法得到授权时，明确报告真实计算集成尚未回归，不宣称已验证。

- [x] **5. 完成审阅并报告精确结果，不自动提交或部署。**

实现是跨接口重大变更，加载 `requesting-code-review`，
给审阅者已批准 spec、基线、精确 diff 与检查结果；不传整个会话。
修复影响需求/正确性的反馈后重跑最小相关检查。
用户没有授权前，不 commit/push、不替换已有服务、不跑真实工程。

## 计划自审与验收映射

| 设计要求 | 实施任务 |
| --- | --- |
| 单入口、无需目录的问答、无 D1/D2 选择 | 6、8、9 |
| 有来源的自定义判据、缺标准澄清、纯分析无强制评价 | 1、4、6、9、10 |
| 指标/工况绑定、严格单位与特殊值 | 1、2、4 |
| 质量策略与验收标准分离 | 1、2、6、7、9 |
| 注册扩展、能力缺口、无动态脚本 | 2、6、7、8、10 |
| F16 假设归适配器、非 F16 无虚构设计引用 | 1、3、7、10 |
| 权限、路径、指纹、预算、未知执行保护 | 2、3、5、7、8 |
| 数值证据与模型解释分离、阈值变更不改旧结论 | 3、4、5、6、10 |
| 无强制图表/Word、所需交付不可静默丢失 | 4、9、10 |
| 历史无损、旧客户端/CLI/workbench 兼容 | 3、7、8、9、10 |
| Copilot 未提交接入保留、双模型后端共用契约 | 6、10 |
| 无真实工程执行、无自动提交或部署 | 全局约束及每个任务收尾 |

执行交接：当前仅文档已获授权。实际改造须另行获得用户许可；
默认选择逐项内联执行并设置审阅检查点。只有用户明确选择代理执行，
或适用技能另有强制要求时才委派对应工作，不启动动态工作流。
