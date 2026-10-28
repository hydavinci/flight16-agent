# 项目架构

[返回项目首页](../README.md) · [使用指南](usage.md) · [输入与输出](inputs-outputs.md)

本项目把“理解需求”“执行计算”“评价结果”分开。
大模型提出受约束的方案，已注册工具产生证据，程序按已确认判据评价并交付。
这种分层支持扩展新能力，但不代表已经能执行任意 MATLAB 分析。

[能力与资源](#注册能力与资源) · [执行机制](#方案授权与证据) · [源码目录](#源码目录) · [开发验证](#开发与验证)

## 系统分层

| 层次 | 主要职责 | 代码位置 |
|---|---|---|
| 入口与展示 | Web、CLI、请求校验、下载 | `web.py`、`cli.py`、`static/` |
| 会话协调 | 需求、任务归档、确认与恢复 | `verification/coordinator.py`、`store.py` |
| 规划与契约 | 类型化方案、来源和资源绑定 | `agent_planner.py`、`runtime/` |
| 执行与证据 | 依赖图、权限、预算、回执与复用 | `agent_runtime.py`、`runtime/evidence.py` |
| 计算适配 | 本机 MATLAB、Worker、离线诊断 | `verification/adapters/` |
| 评价与交付 | 判据比较、JSON/Word、证据索引 | `adapters/evaluation.py`、`delivery.py` |

表内相对模块名以 `src/flight_agent/` 或对应的 `verification/` 目录为基准，
完整分布见下方源码树。前端是原生 HTML/CSS/JavaScript，无需 Node 构建。

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

目录 Agent 的版本化状态保存在本地文件，不依赖 LangGraph checkpoint。
旧固定算例 CLI 使用 LangChain/LangGraph 工具循环，两条路径不能混为一谈。
服务没有 Redis、Celery 或业务数据库；目录任务与兼容上传任务共享单服务执行槽。

## 注册能力与资源

| 能力 ID | 输入资源或依赖 | 主要产出 |
|---|---|---|
| `matlab_execute_project` | `matlab_project / agent` | 已有入口/模型结果、回执、可选时序 |
| `matlab_stepinfo` | 直接依赖发布的时序 | 超调量、调节时间、曲线与有效性说明 |
| `simulink_step_response` | `simulink_project / worker` | Worker 时域指标 |
| `simulink_stability_margins` | `simulink_project / worker` | 幅值裕度、相角裕度 |
| `offline_diagnosis` | `diagnostic_data / agent` | 诊断发现、假设与证据 |
| `evaluate_criteria` | 已确认判据与真实观测 | 单项和按需总体评价 |
| `deliver_results` | 已确认格式与依赖证据 | JSON、Word、交付索引 |

同一物理目录不自动等于同一种资源。本机和 Worker 工程有不同的发现、输入和授权契约；
能力只有获得匹配的资源角色绑定后才能执行。
当前本机没有线性化/`allmargin` 适配器，不能借 Worker 工具名绕过这种区别。

本机有判据、要求报告的典型链路是：

```text
matlab_execute_project -> matlab_stepinfo -> evaluate_criteria -> deliver_results
```

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

失败阶段保留已有证据，未完成阶段不伪造结果。旧 v1 任务按原契约执行和恢复，
只读投影到统一视图；缺少历史来源或适配器版本时标记未记录，不补造。

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

## 扩展一个能力

可信应用启动代码注册 `Capability(...)`，声明稳定名称与版本、严格输入模型、
`CapabilityOutcome` 输出、效果权限及 `CapabilityMetadata`。
元数据描述指标/单位、资源角色、有效性策略和缓存政策。
需要新资源时，再注册 `ResourceAdapter`。

将注册表、资源表和会话存储注入 `EngineeringRuntime`，即可复用确认、执行、展示和交付链路。
不需要在新能力中引入 F16、配平点或固定三节点假设。
最小装配例子见 [D1 技术参考](d1-agent.md)。插件是可信 Python 代码，不是可由模型动态安装的脚本。

若只扩展上传计算，保持 `params/result` 契约即可；要参与判定，还须适配观测与判据。
替换固定 CLI 算例则需同步 `models.py`、`matlab_worker.py`、入口快照、规则、知识与真实参考测试，
不能仅修改阈值就视为完成工程接入。

## 源码目录

`verification/` 保留历史命名，其中包含当前通用 Agent 的主协调链路。
以下列出主要源码；运行产物、缓存和测试夹具不展开。

```text
src/flight_agent/
|-- cli.py                       # 命令行入口
|-- web.py                       # Web 装配、页面与 API
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
|   `-- tasks.py / workflows.py / intents.py
|-- verification/
|   |-- coordinator.py           # 会话与任务协调
|   |-- agent_planner.py         # 通用规划与来源校验
|   |-- agent_runtime.py         # 依赖执行、复用和对账
|   |-- routes.py                # 会话 API
|   |-- views.py                 # 新旧任务只读视图
|   |-- store.py                 # 会话与任务目录
|   |-- intake.py                # 目录识别、冲突与路径约束
|   |-- plugins.py               # 生产能力装配
|   |-- resources.py             # 资源适配器注册
|   |-- matlab_projects.py       # 本机发现与快照
|   |-- matlab_contracts.py      # 本机执行、信号与时序契约
|   |-- local_matlab.py          # 专用解释器与执行后端
|   |-- local_matlab_worker.py   # Engine 子进程
|   |-- adapters/
|   |   |-- local_matlab.py      # 本机执行与 stepinfo
|   |   |-- simulink.py          # Worker 时域与裕度
|   |   |-- diagnostics.py       # 离线诊断
|   |   |-- evaluation.py        # 确定性比较
|   |   `-- delivery.py          # JSON/Word 交付
|   |-- diagnostic_data.py       # 数据清单与快照
|   |-- diagnostic_engine.py / diagnostic_signals.py
|   |-- diagnosis.py / diagnosis_contracts.py / diagnosis_reports.py
|   |-- projects.py / backend.py / worker.py / matlab.py
|   `-- runtime.py / planner.py / compat.py
|-- remote.py                    # 远端提交、轮询与下载
|-- jobs.py                      # 兼容上传任务调度
|-- matlab.py / matlab_worker.py # 固定 CLI 算例
|-- upload_engine.py             # 单文件上传执行
|-- agent.py                     # 兼容工具循环
|-- session.py                   # 兼容分析结果持久化
|-- models.py / upload_models.py # 兼容输入输出契约
|-- rules.py / knowledge.py      # 规则比较与知识检索
|-- service.py                   # HTTP 校验、鉴权与文件访问
|-- directories.py               # 目录选择
|-- matlab_support/
|   |-- flight_agent_run_project.m
|   |-- flight_agent_local_stepinfo.m
|   |-- flight_agent_step_metrics.m
|   |-- flight_agent_margin_metrics.m
|   |-- flight_agent_verify.m
|   `-- flight_agent_execute_upload.m
`-- static/
    |-- agent.html / agent.css / agent.js
    |-- index.html / style.css / app.js
    `-- favicon.svg
```

其他目录：`matlab/` 放固定算例，`examples/` 放示例输入和合成诊断数据，
`rules/` 放版本化判据，`knowledge/` 放 Markdown 依据，`tests/` 放各层回归。
`.env.example` 是配置模板，`pyproject.toml` 管理依赖、打包和开发工具。

## 开发与验证

在项目根目录激活虚拟环境后执行：

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
python -m ruff check .
python -m mypy src
python -m build
```

默认测试不需要真实 MATLAB，也不请求真实模型；测试替身只在 `tests/`。
浏览器回归需先安装 Playwright Chromium：

```bash
python -m playwright install chromium --only-shell
RUN_BROWSER_TESTS=1 python -m pytest \
  tests/test_agent_browser.py tests/test_verification_browser.py \
  tests/test_diagnosis_browser.py tests/test_general_browser.py \
  tests/test_matlab_project_browser.py tests/test_agent_layout_browser.py \
  tests/test_agent_column_resize_browser.py tests/test_web_browser.py -v
```

在已安装匹配 Engine 和许可证的环境中，可显式运行真实集成：

```bash
RUN_MATLAB_TESTS=1 python -m pytest \
  tests/test_matlab.py tests/test_verification_matlab.py tests/test_uploads.py -m matlab -v

RUN_MATLAB_TESTS=1 MATLAB_ALLOW_TRUSTED_PROJECTS=1 \
MATLAB_PYTHON_EXECUTABLE=/absolute/path/to/compatible/python \
/absolute/path/to/compatible/python -m pytest \
  tests/test_local_matlab.py tests/test_local_matlab_integration.py -m matlab -v
```

本机集成包含解析响应、真正非线性闭环、不同初始化工作区、5%/2% 调节带、错误与报告下载。
独立参考与容差见 [D1 技术参考](d1-agent.md)。这些测试准备夹具，不让生产 Agent 生成缺少的模型。
如需用 `--basetemp` 保留产物，只能指定新的专用目录，因为 pytest 会清空该目录。

软件回归不等于目标工程验收。真实服务、平台依赖、独立数值参考和业务流程仍须分别核对；
没有标注故障案例，也不能声称已验证诊断准确率。

Python 包包含静态资源和受控 MATLAB 支持函数，不包含 MATLAB、用户工程、
固定演示入口、知识或规则文件。完整部署需保留项目目录，并正确设置 `--project-root`。

### 当前范围与原始需求

| 原始需求 | 已覆盖 | 仍需验收或实现 |
|---|---|---|
| D1 功能流程 v0.5 | 发现、规划、确认、执行、追问、交付、恢复 | 目标平台与模型服务的独立业务验收 |
| D1 案例 v0.3 | Worker 配平点 1 的时域与裕度链路 | 真实工程参考与逐项容差对照 |
| Demo Roadmap | D1 软件闭环、有界 D2 证据循环 | 主动补跑 Simulink、真实故障案例、D3–D6 |

D3–D6 只保留部分设计版本和效果契约，不是已实现的优化、拓扑编辑或候选采纳工作流。
