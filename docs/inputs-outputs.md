# 输入与输出

[返回项目首页](../README.md) · [使用指南](usage.md) · [项目架构](architecture.md)

本文是字段和文件参考。首次运行请先看使用指南；
接入客户端或核对结果时，再按下面的主题查阅。

[请求](#会话请求) · [判据](#判据与确认) · [预算](#方案与预算) · [结果](#输出状态) · [文件](#报告与证据文件) · [接口](#状态与下载接口)

## 表单与资源位置

| 输入 | 要求 | 含义 |
|---|---|---|
| 任务需求 | 必填 | 目标、分析方法、工况和需要的交付；知识问题也从这里输入 |
| 评价指标 | 可选 | 指标、比较关系、阈值、单位、适用条件及来源 |
| 资源位置 | 按能力 | 决定在哪个主机、用哪种适配器发现目录 |
| 资源目录 | 按能力 | 工程或登记数据的目录；知识问答不要求目录 |
| 输出根目录 | 可选 | Agent/Web 主机上的成果根目录，留空使用默认位置 |
| 可信工程确认 | 执行前 | 确认当前版本、权限和预算，不由填表动作代替 |

| 位置 ID | 所在主机 | 接收内容 |
|---|---|---|
| `matlab_project` | Agent/Web | 已有 MATLAB/Simulink 工程 |
| `simulink_project` | Worker | 满足 Worker 契约的已准备 Simulink 工程 |
| `diagnostic_data` | Agent/Web | 带 `flight-diagnosis.json` 的登记 JSON 数据 |

`agent/worker` 是部署位置，不是不同的智能体。资源类型也不只是目录所在的机器名：
已配置 Worker 不代表本机工程已满足 Worker 工具的资源绑定。
离线数据入口不导入任意 `.mat`、CSV，也不启动补充仿真。

路径须适用于相应主机。源工程与成果目录不能重叠，也不接受符号链接绕过路径检查。
切换资源位置会清空源目录，成果根目录独立保留；发送前请核对两者。
聊天中的路径可用“工程目录”“输出目录”等明确标签，含空格时加引号。
Worker 工程路径支持 POSIX、Windows 盘符及 UNC，成果路径始终以 Agent 主机为准。
表单与消息冲突、或多个路径用途不明确时先澄清，不猜目录。

## 会话请求

创建会话：`POST /api/conversations`。
追加消息：`POST /api/conversations/{id}/messages`。
两者使用同一请求模型 [MessageRequest](../src/flight_agent/verification/routes.py)。

```json
{
  "location": "matlab_project",
  "project_root": "/absolute/path/to/prepared-project",
  "output_root": "/absolute/path/to/results",
  "analysis_request": "沿用已有 Stick 阶跃激励，执行非线性闭环时域仿真，调用 stepinfo，交付 Word 和 JSON。",
  "evaluation_criteria": "超调量 ≤10%；调节时间 ≤3 s；采用 5% 调节带。"
}
```

| 字段 | 约束 | 说明 |
|---|---|---|
| `analysis_request` | 非空，最多 4000 字符 | 结构化分析要求 |
| `evaluation_criteria` | 可选非空，最多 4000 字符 | 只能与分析要求配套，不能单独提交 |
| `message` | 非空，最多 4000 字符 | 单消息形式，不能与上面两项混用 |
| `location` | 可选，最多 128 字符 | 注册资源位置 ID |
| `project_root` | 可选，最多 4096 字符 | 对应资源主机上的目录 |
| `output_root` | 可选，最多 4096 字符 | Agent 成果根目录，显式空字符串恢复默认 |
| `scenario` | 可选 `auto/d1/d2` | 历史资源别名；新客户端优先用 `location` |

结构化输入合并后总长度也不得超过 4000 字符，包含内部边界标记；
不能理解为两栏同时各有 4000 字符额度。未知字段、空白需求和冲突形式会被拒绝。

分析要求和判据原文会保留。页面隐藏外层结束标记只影响显示，不改变来源引用。
目录、阈值、范围或交付变化后须确认新基线，不能沿用旧确认。

## 工程与执行输入

本机工程无需固定布局，下面只说明文件角色，不是自动生成模板：

```text
prepared-project/
|-- README.md                 # 入口、信号、单位、工况与计算定义
|-- models/closed_loop.slx     # 如使用 Simulink，提供已有模型
|-- setup/prepare.m            # 如需初始化，明确其角色
|-- analysis/run_analysis.m    # 如使用 MATLAB 入口，提供已有脚本/函数
`-- ...                       # 已准备的数据和本平台依赖
```

不要求同时存在所有文件。工程清单上限为 5000 个文件、512 MiB，不接受符号链接。
静态发现来源和信号声明示例见[工程准备说明](usage.md#说明工程里已有的内容)。

| 方案字段 | 契约 |
|---|---|
| `mode` | `simulink` 或 `entry`，对应已有模型或已有入口 |
| `model` | 已发现 `.slx/.mdl` 的相对路径 |
| `entry` | 已有 `.m` 入口、脚本/函数类型及明确参数；与模型方式互斥 |
| `initializer` | 可选初始化入口，与分析入口分开；来源明确时不能省略 |
| `project_file`、`working_directory` | 已发现的工程文件、工作目录；`.prj` 启动须按实际工程验收 |
| `input_signal`、`response_signal` | 成对选择，声明 `source/path/unit` |
| `conditions` | 本次工况，后续指标与判据须匹配 |

信号来源为 `logsout/workspace/return`，`path` 是明确字段路径，单位不能猜。
本机 `matlab_execute_project` 发布结果和可选时序，不直接声明幅相裕度等任意指标。
独立 `StepinfoInput` 绑定直接依赖发布的 `series`，并明确 `settling_band` 和稳态策略。
调节带是 `0 < 值 < 1` 的比例，5% 应填 `0.05`；它不是验收阈值。

完整模型见 [matlab_contracts.py](../src/flight_agent/verification/matlab_contracts.py)。
Worker 准备契约见 [D1 参考](d1-agent.md)，离线清单与数据格式见 [D2 参考](d2-agent.md)。

### 本机线性化与裕度

`LinearizeInput` 的必填项是 `model`、`operating_point`、`feedback_sign`、
`feedback_source`。以下是字段契约，不是工程默认值：

| 字段 | 契约 |
|---|---|
| `model` | 已发现的 `.slx/.mdl` 相对路径 |
| `initializer` | 已发现 `MatlabEntry` 或 null，文档指定时必须保留 |
| `project_file`、`working_directory` | 已有工程文件/目录或 null |
| `preparations` | 最多 10 项 `{entry, source, argument_conditions: []}`，先于初始化执行 |
| `operating_point` | `{mode: "initialized" 或 "workspace", path: [], source}`；workspace 必须非空字段路径 |
| `feedback_sign`、`feedback_source` | 明确 `1` 或 `-1` 及原文来源，无默认符号 |
| `workspace_assertions` | 最多 20 项 `{path: ["变量", "字段"], condition: "条件键", source}` |
| `conditions`、`condition_sources` | 有来源的 JSON 条件，以及可选 `{条件键: source}` 原文映射 |

`source` 是 `{reference, quote}`：reference 为当前工程 `documentation` /
`workspace_sources` 的 `path`、归属设置的 `location`，或已授权用户消息的 reference；
quote 必须是其原文片段。`workspace_sources` 仅静态保留向 base 写入变量的已有 `.m`
源码片段，每文件至多 16000 字符，截断时有说明，不代表执行或有效值。
`MatlabEntry` 沿用 `path/kind/arguments/signature/evidence`。准备调用的 JSON 参数须有明确
原始调用来源，或逐项 `argument_conditions` 与已来源化的条件值相等；使用准备入口时
必须提供初始化后的实际工作区断言；每个声明的 conditions 键都必须有对应断言，
不能仅给默认工作点贴标签。工作区字段只支持标识符与结构体字段，不支持表达式。

`matlab_linearize` 的输出 `details.linear_model` 为
`{id:"linear_model", node_id, path:"calls/<call_id>/published/linear-model.mat", sha256, size}`。
`details.linearization.workspace_assertions` 是数组，
每项 `{path, condition, actual}`；actual 必须等于 `conditions[condition]`。
同层保留 `model/initializer/preparations/operating_point/feedback_sign/linearization_points/siso/sample_time`，
以及生产请求 `id/request_sha256`；MAT 自带同一 provenance，元数据保留 canonical 输入。

`AllmarginInput` 只有 `{linear_model: OutputArtifact, conditions: {}}`；
draft 中不手填工件，而写：

```json
{
  "id": "margins",
  "tool": "matlab_allmargin",
  "tool_version": "1",
  "depends_on": ["linearize"],
  "bindings": {
    "linear_model": {"node_id": "linearize", "path": ["details", "linear_model"]}
  },
  "inputs": {"conditions": {}}
}
```

工况必须与生产者和判据完全一致。实际执行还自动验证同一生产者发布的 `metadata.json`。
有效观测为 `gain_margin`（dB）、`phase_margin`（deg），各取全部交越中的最小裕度；
`value:null` 的正/负无穷通过 `special:"positive_infinity"/"negative_infinity"` 明确保留。
输出 details 保留 `gain_crossings/phase_crossings`（每项 `{margin:MetricValue, frequency:MetricValue}`）、
`frequency_unit:"rad/s"`、`closed_loop_stable`、`margin_fallback`、`valid`。
无交越时可保留无穷裕度及 undefined 频率；未定义裕度不能形成有效观测。
无效结果保留诊断和可用证据。名义不稳定、负裕度不会被改写为有效性失败或正数。

## 判据与确认

每条判据必须绑定生产节点、指标、比较符、有限数值阈值、单位、工况和原文来源。
下面是结构示例，不是可以直接执行的完整方案：

```json
{
  "id": "overshoot_limit",
  "metric": {"node_id": "stepinfo", "metric_id": "overshoot"},
  "operator": "<=",
  "threshold": 10,
  "unit": "%",
  "conditions": {"case": "closed_loop"},
  "source": {
    "kind": "user_message",
    "reference": "message:<conversation_id>:0",
    "quote": "超调量 ≤10%"
  }
}
```

比较符支持 `<`、`<=`、`==`、`>=`、`>`，不隐式换算单位或添加容差。
来源可为用户消息、标准文件或明确采纳的示例；文件/示例来源还须有 SHA-256 身份。
示例里的节点名和消息引用必须替换为本次方案的真实来源。

没有自动生效的“常用阈值”。旧演示的 10%、3 s、0 dB、0° 也只有明确采纳才可使用。
稳态窗口、波动与漂移筛选是数值有效性策略，不是验收标准。

确认接口为 `POST /api/conversations/{id}/confirm`：

```json
{"version": 1, "trusted": true}
```

版本须对应当前待确认基线，`trusted` 必须是布尔值 `true`。
部署开关不能替代这次确认，其他类型或省略均不可作为授权。

## 方案与预算

v2 基线固定 `requirements/resources/nodes`、能力快照、`allowed_effects` 和 `budget`。
节点声明依赖与输入绑定，不接收模型伪造的实测数组，也不开放任意代码执行。

| 预算项 | 默认值 | 允许范围 |
|---|---|---|
| `max_rounds` | 6 | 1～20 |
| `max_calls` | 12 | 1～40 |
| `max_seconds` | 300 | 1～3600 |
| `max_no_progress` | 2 | 1～5 |

实际以已确认方案为准。评价、交付和缓存复用也消耗轮次与调用，不按并行层数少算。
预算不足的方案在确认前拒绝，不会自动加额。单次本机执行还受剩余预算和最多 300 秒限制。
当前不支持 `write_candidate/adopt_candidate`。

## 环境变量

模板为 [.env.example](../.env.example)，配置步骤见[使用指南](usage.md#配置大模型)。
`.env` 从启动项目根目录读取，已有进程环境变量优先。

| 配置项 | 用途 |
|---|---|
| `LLM_PROVIDER` | 默认 `openai-compatible`，目录规划也支持 `copilot-cli` |
| `LLM_BASE_URL` | Chat Completions 服务，不是 Web 或 Worker 地址 |
| `LLM_MODEL` | 账户实际可用的模型名；Copilot 模式可省略 |
| `LLM_API_KEY` | API 模式的非空凭据；无认证服务也需占位值 |
| `LLM_TIMEOUT_SECONDS` | 模型请求总超时，纠错不重置预算 |
| `MATLAB_PYTHON_EXECUTABLE` | 本机探测与执行的兼容 Python；省略时使用 Web 解释器 |
| `MATLAB_ALLOW_TRUSTED_PROJECTS` | 设为 `1` 才允许可信目录工程执行 |
| `MATLAB_WORKER_URL`、`MATLAB_WORKER_TOKEN` | Worker 地址与两端一致的令牌 |
| `MATLAB_ALLOW_TRUSTED_UPLOADS` | 只用于兼容上传，与工程开关独立 |

这些配置不会安装 MATLAB、工具箱或原生依赖。“已配置”或“导入通过”均不保证真实调用成功。

## 输出状态

必须区分会话进度、产出完整性、数值有效性和验收结论：

| 层次 | 字段 | 含义 |
|---|---|---|
| 会话进度 | `status` | `planning/waiting_for_user/running/completed/failed/interrupted` |
| 任务完整性 | `task.execution_status` | `completed/partial/failed/interrupted`；尚无结果时可为空 |
| 数值有效性 | `observations[].valid/reason` | 计算运行完，不代表信号或指标一定有效 |
| 验收结论 | `evaluation` | 单项 `pass/fail/undetermined`，总体按需求提供 |

**执行成功不等于判据通过。** 有效值超限应判 `fail`，不是执行失败；
证据缺失或无效时应无法判定，不由模型补造数值。

v2 请求总体结论时，任一所需项无法判定，则总体为 `undetermined`；
否则有失败项为 `fail`，全部通过才为 `pass`。未请求总体结论时状态为空，纯分析没有评价结果。
固定算例 CLI 的历史优先级不同，见文末兼容契约。

## 通用结果字段

`GET /api/conversations/{id}` 返回原始记录；
`GET /api/conversations/{id}/view` 返回统一只读投影。
完整字段见 [views.py](../src/flight_agent/verification/views.py) 和
[observations.py](../src/flight_agent/runtime/observations.py)。

| 字段组 | 内容 |
|---|---|
| `id/status/project_root/resource_location/output_root` | 会话身份、状态与目录；请求输入里的位置字段叫 `location` |
| `messages` | 原始消息、回复和可见缺口，显示整理不覆盖原文 |
| `task/archived_tasks` | 当前与历史任务；知识回答可能没有工程任务 |
| `pending_decision/error` | 待补充、确认、续接或重试的决定，以及实际错误 |
| `task.baseline_version/confirmation` | 基线版本、是否确认与时间 |
| `task.requirements/resources/permissions/budget` | 已确认范围、资源、权限与预算 |
| `task.nodes/calls/budget_state/stage` | 计划节点、调用、预算消耗和执行阶段 |
| `task.observations/findings/tables` | 指标、证据支持的发现和结构化表格 |
| `task.evaluation/gaps` | 比较结果，以及缺失的能力、输入或产出 |
| `task.artifacts/result_directory/details` | 文件清单、目录、快照、能力原始结果和复用来源 |

数值保留完整精度；页面摘要最多显示 6 位有效数字。
有限数值放在 `value`，特殊值使用 `special`，不输出非法 JSON NaN/Infinity。
文件描述含 `path/sha256/size`，通用产物还可带 `id/node_id`。

以下仅演示观测与检查结构，**不是实测结果或完整响应**：

```json
{
  "observation": {
    "node_id": "stepinfo",
    "metric_id": "overshoot",
    "label": "overshoot",
    "value": 12.3,
    "unit": "%",
    "valid": true,
    "reason": "",
    "conditions": {"case": "closed_loop"},
    "evidence": ["calls/<call_id>/published/result.json"]
  },
  "check": {
    "criterion_id": "overshoot_limit",
    "metric": {"node_id": "stepinfo", "metric_id": "overshoot"},
    "value": 12.3,
    "value_unit": "%",
    "operator": "<=",
    "threshold": 10,
    "unit": "%",
    "status": "fail",
    "reason": "有效观测值大于已确认上限。",
    "evidence": ["calls/<call_id>/published/result.json"]
  }
}
```

## 报告与证据文件

产物随能力、需求和执行阶段而变，不保证每个任务都有下列所有文件。
通用交付按 `delivery_formats` 生成，未请求 Word 就不强制生成 Word。

| 任务内相对路径 | 内容 |
|---|---|
| `delivery/<call_id>/result.docx` | 目标、执行详情、指标、来源、评价、发现、缺项与可用曲线 |
| `delivery/<call_id>/result.json` | 需求、依赖证据、执行完整性和未满足项 |
| `delivery/<call_id>/delivery-index.json` | 交付依赖摘要、产物身份与恢复信息，不是新计算 |
| `agent/analysis-runs/<call_id>.json` | 完整调用规格、版本、输出摘要、时间、文件与复用来源 |
| `agent/checkpoints/<call_id>/checkpoint.json` | 有状态能力的原调用检查点 |
| `calls/<call_id>/published/series.json` | 本机发布的实际输入/响应时序 |
| `calls/<call_id>/published/result.json` | 本机执行结果，不是汇总交付 JSON |
| `calls/<call_id>/published/step-response.png` | `stepinfo` 曲线及计算带，无效结果也可保留诊断图 |
| `calls/<call_id>/local-matlab/` | 工程副本、运行元数据、原始回执及完整日志 |

交付 JSON 的顶层字段为 `title`、`requirements`、`evidence`、
`execution_status`、`evaluation_requested` 和 `unmet_requirements`。
Word 保留条件、有效性、判据原文与身份、单项/总体评价、证据摘要和可用 PNG。

时序的 `input/response` 各含 `time/values/time_unit/unit/name`：
每条 2～100000 个有限样本，时间与值等长且时间严格递增；本机 `stepinfo` 要求时间单位 `s`。
它必须来自确认的直接依赖工件，不能用模型编写的数组代替。

`raw.mat`、`runtime-metadata.json`、可序列化函数返回值等按实际执行保存。
诊断及旧 v1 任务可能使用 `diagnosis.docx`、`delivery-v<version>/` 等命名；
请按 `artifacts` 清单下载，不猜文件名。

## 成果存放位置

默认目录：

```text
runs/
|-- verification/<conversation_id>/
|   |-- conversation.json
|   `-- tasks/<task_id>/
|       |-- agent/
|       |-- calls/
|       `-- delivery/
|-- verification-worker/<run_id>/  # Worker 记录及 project/ 副本
|-- web/<job_id>/                  # 兼容上传 Web 记录
`-- worker/<job_id>/               # Worker 上传记录
```

设置 `output_root=/absolute/path/to/results` 后，该任务成果目录改为：

```text
/absolute/path/to/results/flight-agent-<task_id>/
```

会话索引仍位于默认 `verification/<conversation_id>/conversation.json`。
新任务获得新 ID/目录，旧交付不变。两种位置均由
[ConversationStore](../src/flight_agent/verification/store.py) 核对路径与所有权。
没有执行到的阶段不生成对应假结果文件。

## 状态与下载接口

客户端先读取 `GET /api/bootstrap`，再用 `X-Flight-Token` 页面令牌访问其他接口；
接口同时检查同源条件。不要将模型 Key 当作页面令牌。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/bootstrap` | 页面令牌、配置和 Engine 导入探测 |
| GET | `/api/resource-locations` | 资源位置与可用性 |
| GET | `/api/conversations/capabilities` | 实际能力目录 |
| GET | `/api/conversations` | 最近最多 20 个会话摘要 |
| POST | `/api/conversations` | 创建会话，202 只表示接收 |
| POST | `/api/conversations/{id}/messages` | 追加需求 |
| GET | `/api/conversations/{id}`、`/api/conversations/{id}/view` | 原始状态或通用展示 |
| POST | `/api/conversations/{id}/confirm` | 确认当前基线并执行 |
| POST | `/api/conversations/{id}/resume` | 原调用续接/对账，不重新提交仿真 |
| POST | `/api/conversations/{id}/retry` | 已知失败后显式重试，仍需版本与可信确认 |
| GET | `/api/conversations/{id}/tasks/{task_id}/artifacts/{name}` | 下载登记文件 |

`name` 是清单内的完整相对路径，可包含子目录。下载检查文件身份和摘要，旧任务也保留入口。
202、报告生成或 CLI 返回 0，均不单独证明工程判据通过。

## 兼容入口契约

### 单文件上传

输入为一个 UTF-8 `.m` 和 JSON 对象 `params`，输出为程序明确返回的可 JSON 序列化 `result`。
函数使用 `result = 函数名(params)`，脚本读取 `params` 并赋值 `result`。
普通波形示例用 `frequency_hz/amplitude/samples`，演示指标示例使用
[parameters.json](../examples/parameters.json)。

| 限制 | 上限 |
|---|---|
| 源文件 / 参数 / 整体请求 | 1 MB / 100 KB / 2 MB |
| JSON 结果 / 日志 | 约 4 MB / 1 MB |
| PNG | 最多 5 张，每张 2 MB、1600 万像素 |
| 超时 | 1～600 秒 |

结果不含 NaN/Infinity；图形通过 `exportgraphics` 导出，不支持的对象/UI 图形会失败。
轮询和超时不是系统级资源配额。浏览器保留原始 JSON 数字文本，避免额外 JavaScript 舍入，
但不会改变 MATLAB 数值类型或提供任意精度计算。

Web 与 Worker 任务 ID 分开记录。切换任务时先禁用旧结果的分析/下载，确认新身份与内容后才恢复。
最近任务保留 20 项；目录与普通上传的恢复规则不同，详见[使用指南](usage.md#单文件上传工作台)。

### 固定算例 CLI

`analyze` 输入见 [models.py](../src/flight_agent/models.py)，只接受固定演示契约：

| 字段 | 单位/约束 |
|---|---|
| `schema_version` | `1.0` |
| `aircraft_type` | `demo_fixed_wing` |
| `condition` | `demo_cruise` |
| `mass_kg` | kg，有限正数 |
| `wing_area_m2` | m²，有限正数 |
| `airspeed_mps` | m/s，有限正数 |
| `air_density_kg_m3` | kg/m³，有限正数 |
| `thrust_n` | N，有限非负数 |

未知字段、数值字符串、布尔值、NaN/Infinity 和不合要求的数值会被拒绝。
其结果位于 `runs/<UTC时间>-<随机ID>/`，不同于目录任务：

| 文件 | 内容 |
|---|---|
| `input.json`、`metadata.json` | 参数、执行状态、摘要与版本 |
| `rules.json`、`matlab_source.m` | 规则和入口快照 |
| `result.json`、`matlab.log` | 计算结果与日志 |
| `assessment.json` | 全量规则检查与覆盖 |
| `evidence.json`、`messages.json` | 检索证据与工具循环记录 |
| `report.md` | 程序判定、独立模型解释和来源 |

旧规则支持 `< <= > >=`，不做单位转换、不暗加容差。
实测 `value_unit` 与判据 `unit` 分别保存，旧记录缺实测单位时显示未知，不补造或改写旧报告。

旧状态优先级为：有失败项则 `fail`；否则缺项为 `undetermined`；
全部适用项通过为 `pass`；没有适用项为 `not_applicable`，不能当作通过。
这与 v2 总体判定的“无法判定优先”不同。

失败保留已有结果，不伪造未完成阶段；日志不是跨进程自动续算或 exactly-once 保证。
模型解释和建议需要人工复核。知识引用形如 `[KB:文档ID]`，同文档不同片段累积保留，
完全重复片段去重；引用完整性检查不证明文字一定忠实或正确。
