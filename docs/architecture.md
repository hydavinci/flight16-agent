# 项目架构

[返回项目首页](../README.md) · [使用指南](usage.md) · [输入与输出](inputs-outputs.md)

本项目把“理解需求”“执行计算”“评价结果”分开。
大模型提出受约束的方案，已注册工具产生证据，程序按已确认判据评价并交付。
这种分层支持扩展新能力，但不代表已经能执行任意 MATLAB 分析。
只有一个通用 v2 运行时，没有 D1/D2 场景选择。
Worker 工程与离线诊断数据都是注册资源，不是独立产品。

[能力与资源](#注册能力与资源) · [执行机制](#方案授权与证据) · [源码目录](#源码目录) · [开发验证](#开发与验证)

## 系统分层

| 层次 | 主要职责 | 代码位置 |
|---|---|---|
| 入口与展示 | Web 组合、CLI、请求校验、下载 | `web.py`、`application.py`、`web_routes/`、`cli.py` |
| 会话协调 | 需求、任务归档、确认与恢复 | `verification/coordinator.py`、`store.py` |
| 规划与契约 | 类型化方案、来源和资源绑定 | `agent_planner.py`、`runtime/` |
| 执行与证据 | 依赖图、权限、预算、回执与复用 | `agent_runtime.py`、`runtime/evidence.py` |
| 计算适配 | 本机 MATLAB、Worker、离线诊断 | `verification/adapters/` |
| 评价与交付 | 判据比较、JSON/Word、证据索引 | `adapters/evaluation.py`、`delivery.py` |

表内相对模块名以 `src/flight_agent/` 或对应的 `verification/` 目录为基准，
完整分布见下方源码树。前端是原生 HTML/CSS/JavaScript，无需 Node 构建。

`create_web_app` 保持公开入口、`scheduler_settings` 参数和 HTTP 路径，仅负责 Web 组合。
`application.py` 负责默认运行时装配；测试可按对象身份注入 `EngineeringRuntime`，
也可通过 `conversation_manager` 注入带 planning/execution 调度器的
`ConversationManager`。外部注入的 manager 和 store 由调用者管理；默认 manager
在应用退出时排空并关闭其调度器。store 没有虚构的 close 生命周期，退出后仍可读取。
`app.state.conversation_manager` 保持可用。

所有路由继续经过同源、Host、令牌、请求体大小和严格 schema 校验。
下载继续核对任务目录身份、工件登记和 SHA-256，不暴露任意文件目录。
前端由浏览器直接加载原生 ES modules，不需要 Node、npm、打包器或编译。
模块 URL 使用显式白名单和 JavaScript MIME；CSP 不增加 `unsafe-inline` 或 `unsafe-eval`。

桌面三栏独立滚动，左右默认宽度为 260/340px，最小为 200/240px，对话区至少 480px。
方向键每次调整 10px，Shift 加方向键为 50px；Enter 或双击恢复默认。
取消拖动和响应式适配保留原有列宽偏好。
分界线焦点只显示在短拖柄上，不绘制全高边框。

## 请求如何流转

```text
Web / directory-task CLI
    |
    v
需求理解 -> 回答 / 澄清 / 能力缺口 / 待确认方案
                                      |
                               用户确认当前基线
                                      |
                       权限、版本、资源与预算检查
                                      |
               +----------------------+----------------------+
               |                      |                      |
        本机 MATLAB 工程       Worker Simulink 工程       离线 JSON 数据
        工程副本 + Engine       HTTP / 隧道 / HTTPS        有界只读分析
               |                      |                      |
               +----------------------+----------------------+
                                      |
                             观测、文件与不可变回执
                                      |
                         按需评价 -> 按需生成报告
```

知识问答不要求工程目录，也不触发数值计算。纯分析可以没有判据或总体结论，
交付格式由需求决定，不强制 Word、图形或固定四节点。

Web 启动时装配 `CapabilityRegistry(agent_mode=True)`、`ResourceRegistry`、
`EngineeringRuntime`、`AgentPlanner` 和 `AgentExecutor`。
本机后端直接使用兼容 Python 子进程与 MATLAB Engine；远程后端通过 `RemoteSimulinkBackend`。

通用 Agent 的版本化状态保存在本地 SQLite WAL 数据库；不使用 LangGraph 或顶层 LangChain 运行时。
结构化模型传输保留 `langchain-core`、`langchain-openai`、`langsmith` 和 `openai`。
服务没有 Redis 或 Celery。进程内规划和执行调度器提供有界队列与分 lane 并发；
本机 MATLAB 默认仍为单槽。入队、恢复和结果发布都通过 SQLite 状态与会话预留核对，
过期规划不能覆盖更新后的会话。

## 注册能力与资源

| 能力 ID | 输入资源或依赖 | 主要产出 |
|---|---|---|
| `matlab_execute_project` | `matlab_project / agent` | 已有入口/模型结果、回执、可选时序 |
| `matlab_stepinfo` | 直接依赖发布的时序 | 超调量、调节时间、曲线与有效性说明 |
| `matlab_linearize` | `matlab_project / agent` | 已有工作点/分析点的线性模型 MAT、实际选择与运行证据 |
| `matlab_allmargin` | 直接线性化依赖发布的 MAT 与元数据 | dB/deg 裕度、全部交越、名义稳定性、有效性与图 |
| `simulink_step_response` | `simulink_project / worker` | Worker 时域指标 |
| `simulink_stability_margins` | `simulink_project / worker` | 幅值裕度、相角裕度 |
| `offline_diagnosis` | `diagnostic_data / agent` | 诊断发现、假设与证据 |
| `evaluate_criteria` | 已确认判据与真实观测 | 单项和按需总体评价 |
| `deliver_results` | 已确认格式与依赖证据 | JSON、Word、交付索引 |

同一物理目录不自动等于同一种资源。本机和 Worker 工程有不同的发现、输入和授权契约；
能力只有获得匹配的资源角色绑定后才能执行。
本机线性化/`allmargin` 同样不能借 Worker 工具名绕过这种区别。

本机有判据、要求报告的典型链路是：

```text
matlab_execute_project -> matlab_stepinfo -> evaluate_criteria -> deliver_results
matlab_linearize -> matlab_allmargin -> evaluate_criteria -> deliver_results
```

这些是组合示例，不是固定节点序列。线性化同一节点内可先调用有来源的已有准备入口，
再运行权威初始化器，验证明确工作区字段与条件相等；不依赖其他节点的 Engine 工作区。
普通入口签名限制不变；仅新线性化入口支持明确参数的已有 `varargin` 函数。
已有 `getlinio` 点、工作点来源和反馈符号写入运行证据；线性模型与生产者元数据在发布、
消费、完成和恢复时验证摘要。指标保留有符号无穷值及全部交越，不把名义稳定性混同阈值达标。

Worker 的配平点 1、预设日志/分析点和稳态策略属于适配器约定，不是通用内核的限制。
离线诊断不主动补跑 Simulink，测试里的 RMSE、峰值等能力也不属于生产注册项。

## 方案、授权与证据

### 确认固定什么

v2 基线记录需求、产出、判据来源、工程指纹、能力版本与完整声明、依赖绑定、效果权限和预算。
确认前仅做静态发现和规划，不执行工程；已有确认不授权新的范围或版本。
当前不支持 `write_candidate/adopt_candidate`，不会自动优化、编辑模型或采纳设计。

每次调用规格还绑定实际输入、条件、数值策略、资源及依赖输出摘要。
开始执行、复用和恢复时均检查这些身份，不能仅凭文件名或能力名命中缓存。
阈值变化可以只重新评价/交付，但不能覆盖旧结论或省略新基线确认。

预算约束覆盖轮次、调用、总时间和连续无进展；评价、交付、缓存复用同样计数。
默认值与允许范围见[方案与预算](inputs-outputs.md#方案与预算)。
能力升级或同版本声明变化也会要求重新规划。

### 本机执行如何隔离

发现阶段只读取有界工程内容。执行阶段先复制工程，再通过专用 Python 子进程调用 Engine。
初始化函数/回调的 base 变量和初始化脚本局部变量在同一节点按约定提供给模型；
局部变量通过 `SimulationInput` 临时注入，不修改保存的模型设置。

后续节点只接收明确发布、可验证哈希的工件，例如 `series.json`。
不会共享隐藏工作区，也不把模型填写的数组当作实测时序。
Engine 探测、启动、许可、仿真和指标有效性是不同检查，不互相替代。

### 失败与恢复如何处理

已知失败可显式重试；状态未知只能核对原调用 ID，不能盲目再发一次。
有状态能力保存原调用检查点，恢复时核对规格与回执。
缺少可核验回执时明确停止；新需求也不能绕过尚未对账的旧调用。

失败阶段保留已有证据，未完成阶段不伪造结果。
v1 历史不再读取、执行、恢复或投影；旧上传/demo 与恢复提案入口不受支持。
已有 v2 状态继续保留确认、调用身份、哈希、检查点与交付，不生成兼容任务。

## 大模型调用边界

`config.py` 选择 OpenAI 兼容 API 或 Copilot CLI；`StructuredPlanner` 请求一次类型化响应，
`AgentPlanner` 再检查结构、来源、资源、能力与产出绑定。
模型只能回答、澄清、报告缺口或提出方案，不能动态注册工具、生成任意执行命令或跳过确认。

资源文档与用户消息的确定来源用于形成 `source_selections`。
唯一且权威的模型/初始化选择若被方案遗漏，会反馈给规划器修正，而不是静默改写或要求用户重复提供。
单一库存候选不自动变成来源声明；真正冲突仍需澄清。

结构纠错与来源纠错共享最多两次模型调用，以及原总超时和上下文限制。
第二轮仅使用剩余时间；超时后即使收到文本也不采纳。
执行失败、网络错误、伪造来源或未注册能力不通过这项格式纠错自动重试。

上下文使用紧凑 JSON。相同且共享后更小的能力 schema 以 `catalog_schema_ref` 引用
`catalog_schemas`，保留原 `$defs/$ref`，不删除需求、来源或不可用能力。
这只是传输去重，不会削弱原有校验或授权。

Copilot 适配在空临时目录运行，仅允许加载技能，禁止其文件、命令和网络访问工具，
不传递 MATLAB/LLM API 凭据，也不让 CLI 直接执行工程。
数据范围与使用配置见[使用指南](usage.md#配置大模型)。

## Planning context and provider telemetry

Planning normalizes a closed subset of metric requests and selects candidates from
registered output, metric, resource-role, and dependency metadata. Unknown intent,
unknown dependency metadata, unresolved dependencies, or selected dynamic
dependencies retain the full catalog. Missing resources and unavailable relevant
backends remain visible for clarification or a capability gap. Selection is
deterministic and conservative.

Selection metadata is catalog-only and excluded from execution snapshots, so it
does not rewrite confirmed baseline fingerprints or change capability versions.
Source selection keeps complete authorized excerpts. Only explicitly scoped,
unrelated non-user excerpts may be omitted from model context; unclassified
sources and all user-message sources are retained. The complete authorized
sources, resource references, project declarations, and catalog remain local for
binding and validation. Selection grants no permission or execution authority.

The logical redacted request context is limited to exactly 120,000 UTF-8 bytes
before lossless catalog-schema packing; equality is accepted and 120,001 bytes is
rejected. The same limit applies to the initial request and structural correction.
Neither round can silently truncate requirements, quotations, or history to fit.
An oversized context returns clarification without dispatch.

Conversation `planning_telemetry` contains
`metrics.serialized_bytes`, `metrics.selected_capability_count`,
`metrics.selected_source_excerpt_count`,
`metrics.planning_duration_seconds`, and `metrics.correction_round_count`.
An optional `failure` contains exactly `category`, `redacted_request_id`,
`exit_code`, `elapsed_seconds`, `response_bytes`, and a fixed bounded `summary`.
Provider request IDs are represented by `sha256:` followed by the first 16 hex
characters of their SHA256 digest, never their original values. The six terminal categories are
`authentication`, `authorization`, `transport`, `timeout`, `invalid_response`,
and `process_failure`.

No assembled prompt, response body, credentials, environment dump, or raw stderr
is stored in telemetry. Original user messages and source documents remain under
their existing local retention rules; telemetry does not copy them. Managed
provider-failure logs use only allowlisted category and exit-code data without an
exception chain. A provider failure is not a capability gap and does not erase
the previous confirmed task.

Telemetry is persisted with the conversation in SQLite, survives restart, and is
returned by the authenticated conversation API. Plan-producing success persists
its telemetry as well as answer and clarification outcomes. Planning releases the
outer conversation-manager lock during inference, then publishes only if its
reservation is still current; a superseded reservation cannot publish stale
messages, task state, or telemetry.

## 扩展一个能力

可信应用启动代码注册 `Capability(...)`，声明稳定名称与版本、严格输入模型、
`CapabilityOutcome` 输出、效果权限及 `CapabilityMetadata`。
元数据描述指标/单位、资源角色、有效性策略和缓存政策。
需要新资源时，再注册 `ResourceAdapter`。

将注册表、资源表和会话存储注入 `EngineeringRuntime`，即可复用确认、执行、展示和交付链路。
不需要在新能力中引入 F16、配平点或固定三节点假设。
装配使用 `plugins.register_builtin_plugins`，分别注入 Worker 后端与诊断 reviewer。
插件是可信 Python 代码，不是可由模型动态安装的脚本。
资源准备见 [Simulink Worker 参考](simulink-worker.md)与[离线诊断参考](offline-diagnostics.md)。

## 源码目录

`verification/` 保留历史命名，其中包含当前通用 Agent 的主协调链路。
以下列出主要源码；运行产物、缓存和测试夹具不展开。

```text
src/flight_agent/
|-- cli.py                       # 命令行入口
|-- web.py                       # Web 组合、同源/令牌校验、安全响应头与生命周期
|-- application.py               # 运行时、计算后端与后台调度器装配和注入
|-- web_routes/
|   |-- pages.py                 # 页面和显式静态资源白名单
|   |-- configuration.py         # bootstrap 与目录配置
|   |-- metadata.py              # 能力与资源元数据
|   |-- artifacts.py             # 哈希校验后的工件下载
|   `-- errors.py                # HTTP 错误映射
|-- worker.py                    # Worker 服务与共享执行槽
|-- config.py                    # 模型配置与 API 客户端
|-- copilot.py                   # Copilot CLI 传输
|-- runtime/
|   |-- agent_contracts.py       # 计划、基线、调用规格与回执
|   |-- requirements.py          # 目标、产出、判据与缺口
|   |-- observations.py          # 观测、表格、评价与任务结果
|   |-- capabilities.py          # 能力注册
|   |-- capability_metadata.py   # 能力声明与可用性
|   |-- resources.py             # 资源契约
|   |-- contracts.py             # 效果、预算、指纹
|   |-- loop.py                  # 有界执行驱动
|   |-- budget.py                # 预算计算
|   |-- evidence.py              # 工件、摘要与不可变回执
|   |-- scheduling.py            # 有界规划队列与分 lane 执行调度
|   `-- tasks.py                 # 通用依赖图节点与祖先校验
|-- verification/
|   |-- coordinator.py           # 会话与任务协调
|   |-- agent_planner.py         # 通用规划与来源校验
|   |-- agent_runtime.py         # 依赖执行、复用和对账
|   |-- routes.py                # 会话 API
|   |-- views.py                 # v2 当前与归档任务只读视图
|   |-- store.py                 # 会话存储接口与兼容入口
|   |-- sqlite_store.py          # SQLite WAL 会话、归档与导出
|   |-- intake.py                # 目录识别、冲突与路径约束
|   |-- plugins.py               # 生产能力装配
|   |-- resources.py             # 资源适配器注册
|   |-- matlab_projects.py       # 本机发现与快照
|   |-- matlab_contracts.py      # 本机执行、信号与时序契约
|   |-- local_matlab.py          # 专用解释器与执行后端
|   |-- local_matlab_worker.py   # Engine 子进程
|   |-- adapters/
|   |   |-- local_matlab.py      # 本机执行、stepinfo、线性化与 allmargin
|   |   |-- simulink.py          # Worker 时域与裕度
|   |   |-- diagnostics.py       # 离线诊断
|   |   |-- evaluation.py        # 确定性比较
|   |   `-- delivery.py          # JSON/Word 交付
|   |-- diagnostic_data.py       # 数据清单与快照
|   |-- diagnostic_engine.py / diagnostic_signals.py
|   |-- diagnosis_contracts.py / diagnostic_planner.py
|   |-- projects.py / backend.py / worker.py / matlab.py
|   `-- legacy_cleanup.py        # 原始记录分类与显式清理，不执行旧任务
|-- remote.py                    # 远端提交、轮询与下载
|-- background.py                # Future 与后台执行槽生命周期
|-- matlab.py                    # Engine 探测与进程组清理
|-- models.py                    # 共用严格契约与有限数值
|-- service.py                   # HTTP 校验、鉴权与文件访问
|-- directories.py               # 目录选择
|-- matlab_support/
|   |-- flight_agent_run_project.m
|   |-- flight_agent_local_stepinfo.m
|   |-- flight_agent_local_linearize.m
|   |-- flight_agent_local_allmargin.m
|   |-- flight_agent_step_metrics.m
|   |-- flight_agent_margin_metrics.m
|   `-- flight_agent_verify.m
`-- static/
    |-- agent.html / agent.css
    |-- agent.js                 # 稳定 URL 的原生 ES module 入口
    |-- api.js                   # API 传输与令牌头
    |-- state.js                 # 单一会话/UI 状态
    |-- render.js                # 会话、进度与控件的文本 DOM 渲染
    |-- task-evidence.js         # 基线、授权、评价、证据与工件下载
    |-- resize-preferences.js    # 三栏调整、键盘交互与本地偏好
    |-- bootstrap.js             # 初始化、事件、轮询、历史、目录与请求时序
    `-- favicon.svg
```

其他目录：`examples/` 放合成诊断数据，`tests/` 放各层回归。
`.env.example` 是配置模板，`pyproject.toml` 管理依赖、打包和开发工具。

## 开发与验证

在项目根目录执行锁定的软件 gate：

```bash
uv sync --extra dev
uv lock --check
RUN_BROWSER_TESTS=0 RUN_MATLAB_TESTS=0 \
uv run --no-sync python -m pytest -m "not matlab and not browser" -q
uv run --no-sync python -m ruff check .
uv run --no-sync python -m mypy src
uv run --no-sync python -m build --no-isolation
```

默认测试不需要真实 MATLAB，也不请求真实模型；测试替身只在 `tests/`。
浏览器回归需先安装 Playwright Chromium：

```bash
uv run --no-sync python -m playwright install chromium --only-shell
RUN_BROWSER_TESTS=1 RUN_MATLAB_TESTS=0 \
uv run --no-sync python -m pytest -m "browser and not matlab" -q
```

Linux CI 使用 `playwright install --with-deps chromium --only-shell`。
测试通过 `serve_app` 启动各自的临时本地应用，不要求单独常驻开发服务器。

真实 MATLAB 验收使用管理员预先配置的专用兼容环境。变量必须由运行环境提供，
不能把下面的变量名当成可直接粘贴的示例路径：

```bash
test -n "$MATLAB_ACCEPTANCE_ENV"
test -f "$MATLAB_ACCEPTANCE_ENV/pyvenv.cfg"
test -x "$MATLAB_ACCEPTANCE_ENV/bin/python"
export UV_PROJECT_ENVIRONMENT="$MATLAB_ACCEPTANCE_ENV"
export MATLAB_PYTHON_EXECUTABLE="$MATLAB_ACCEPTANCE_ENV/bin/python"
"$MATLAB_PYTHON_EXECUTABLE" -c 'import matlab.engine; print(matlab.engine.__file__)'
uv sync --locked --extra dev --inexact --python "$MATLAB_PYTHON_EXECUTABLE"
RUN_MATLAB_TESTS=1 MATLAB_ALLOW_TRUSTED_PROJECTS=1 \
uv run --no-sync python -m pytest \
  tests/test_verification_matlab.py tests/test_local_matlab.py \
  tests/test_local_matlab_integration.py -m matlab -v -s
```

`MATLAB_ACCEPTANCE_ENV` 须预装匹配发行版的 Engine，并拥有 MATLAB、Simulink、
Control System Toolbox 和 Simulink Control Design 许可。`--inexact` 只用于保留
专用环境中的非锁定 Engine，不用于常规软件 CI。真实 MATLAB 不回退到 fake Engine；
未显式启用的 matlab 测试会跳过，跳过不代表验收通过。记录中须保存 MATLAB 发行版、
解释器、测试结果与失败原因。

GitHub CI 的 software job 使用 Python 3.11/3.12 矩阵；browser job 单独运行 Chromium。
licensed MATLAB job 仅允许从默认分支手动勾选 `run_matlab`，使用
`[self-hosted, matlab]` 与受审批保护的 `matlab-acceptance` 环境；
仓库变量 `MATLAB_ACCEPTANCE_ENV` 指向该 runner 的专用环境。PR 不触发真实 MATLAB。
hosted jobs 只缓存 uv 下载；不缓存虚拟环境、浏览器、MATLAB、Engine 或证据产物，
licensed MATLAB job 禁用 uv 缓存。

本分支的真实 MATLAB 验收未运行；opt-in licensed gate 仍等待受保护环境、
self-hosted runner、专用 Engine 环境、所需工具箱和许可证基础设施。

本机集成包含解析响应、真正非线性闭环、不同初始化工作区、5%/2% 调节带、错误、
报告下载、线性化与 `allmargin` 证据链。
独立参考与容差见 [Simulink Worker 参考](simulink-worker.md)。
这些测试准备夹具，不让生产 Agent 生成缺少的模型。
如需用 `--basetemp` 保留产物，只能指定新的专用目录，因为 pytest 会清空该目录。

软件回归不等于目标工程验收。真实服务、平台依赖、独立数值参考和业务流程仍须分别核对；
没有标注故障案例，也不能声称已验证诊断准确率。

Python 包包含静态资源和受控 MATLAB 支持函数，不包含 MATLAB、用户工程、
知识或规则文件。完整部署需正确设置 `--project-root`。

## CLI 与历史清理

支持 `doctor`、`web`、`worker`、`directory-task propose/confirm/status/resume`
和 `cleanup-legacy`；不提供场景、工作流目录或恢复提案接口。
`cleanup-legacy --project-root ...` 默认 dry-run，仅报告原始记录分类。
显式 `--apply` 才删除正面识别为 v1 的会话目录，保留 v2、未知记录和外部成果。
源路径必须通过 `Path.resolve(strict=True)` 严格解析；无法解析或包含于待删目录时跳过。
清理器不依赖任何旧任务契约；详见[历史清理](usage.md#历史清理)。

## Confirmed Local Project Snapshots

Static discovery is read-only. Exact-version trusted confirmation revalidates the
selected source fingerprint, verifies or atomically publishes a canonical
content-addressed snapshot, and commits its identity and manifest with the task.
The immutable object lives under the conversation store's `snapshots/objects`.
The original resource reference remains provenance; it is not rewritten into an
execution path.

New local project execution and linearization copy verified immutable bytes into
managed `work/<conversation>/<task>/calls/<call>/local-matlab/project` directories.
stepinfo and allmargin use independently copied, verified direct-dependency
artifacts, with the originating snapshot lineage in their invocation receipts.
MATLAB never runs in the immutable object. New backend working files,
`request.json`, and `state.json` do not live in an external delivery directory.
Authenticated Agent checkpoints and immutable receipts retain their established
evidence-store location and digest checks.

Every use, including cache reuse and new-style reconciliation, verifies the full
manifest and file digests. Source changes after confirmation do not redirect
execution. A later source version requires a new proposal and confirmation.
Tampered managed objects fail closed and are never silently repaired.
The confirmed baseline is the single holder of the full manifest. New invocation
specifications record only each resource's snapshot ID and source fingerprint;
verification resolves that compact lineage back to the baseline before checking
the managed object. Historical invocation receipts that embedded the full
manifest are normalized to the same lineage identity and remain immutable.

Cleanup uses a cross-process lifecycle guard and the complete SQLite reference
index. Current and archived persisted task baselines retain snapshots regardless
of terminal status, preserving retained evidence. Cleanup is explicit and dry-run
by default; it never traverses sources, Worker locators, delivery roots, or work
directories. Exact publication and quarantine names left by interrupted snapshot
operations are eligible only after the same containment, identity, manifest, and
inventory checks; retained or unsafe residues remain untouched. Read-only
permissions are not a security sandbox.
