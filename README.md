# Flight Agent

基于 **LangChain + MATLAB Engine for Python + FastAPI** 的通用 MATLAB / Simulink 工程分析 Agent，
提供 Web 和 CLI 入口，内置飞控分析与离线诊断适配器。核心分工是：**大模型理解请求、提出计划和解释证据，
已注册适配器负责计算，Python 程序负责授权、不可变证据和有来源的判据比较。**

## 阅读导航

- [项目说明](#项目说明)：项目定位、已有能力和使用边界。
- [使用说明](#使用说明)：安装启动、本机工程分析、Worker、离线诊断和兼容工作台。
- [项目框架](#项目框架)：系统分层、执行链路和内置能力。
- [详细输入说明](#详细输入说明)：表单、工程目录、接口字段、判据和运行配置。
- [详细输出说明](#详细输出说明)：状态含义、结果字段、报告内容和文件存放位置。
- [配置大模型](#配置大模型)：OpenAI 兼容接口和可选 Copilot CLI 后端。
- [项目目录结构](#项目目录结构)：主要源码文件及职责。
- [开发验证](#开发验证)、[常见问题](#常见问题)。

## 项目说明

项目的主要用途是：**提供一个已有 MATLAB / Simulink 工程，描述分析要求和可选评价指标，
由 Agent 发现工程、提出待确认方案、调用真实计算后端，并交付可追溯的结果。**
它是面向已注册能力的通用任务框架，不是只接受某一架飞机参数的固定问答页面。

Web 首页提供 **任务需求**、可选的 **评价指标** 和工程目录，不再选择 D1/D2 场景。
知识问答不要求目录、MATLAB 或 Worker；工程任务从已注册能力目录生成 v2 方案，
列明需求、资源、指标、条件、判据来源、能力版本、权限和预算，确认前不执行。
纯分析可以只报告观测、发现或表格，不强制阈值、合格结论、图形或 Word；
要求验证却未给标准时先澄清，缺少工具或输入时明确报告能力缺口，不虚构结果。

现有注册项包括本机已准备 MATLAB/Simulink 工程的已有入口执行与独立 stepinfo，
Worker 上已准备工程的 Simulink 阶跃响应/稳定裕度，
以及 Agent 主机已登记 JSON 数据的**有界离线诊断**。
配平点 1、信号约定、稳态筛选和诊断假设循环属于这些适配器，不是通用核心限制。
通用框架不等于已经支持任意飞控算法；离线诊断不主动补跑 Simulink，不执行设计修改。
同一会话增加标准或变更需求会形成新基线并重新确认；符合完整规格和哈希校验的
数值证据可复用，旧基线、回执、结论和交付保持不变。
原单文件 `.m` 上传工作台保留在 `/workbench`；CLI `analyze` 保留固定 MATLAB 入口、
规则检查和知识检索。大模型没有任意代码执行、模型编辑或参数优化工具。

> **当前是软件演示项目，不包含真实 F-16 飞行动力学模型或经审核的航空判据。**
> 自带算例只演示数据、计算、规则和报告链路，不能用于适航、放飞或真实飞行器合格判断。
> 自动化集成包含真实 MATLAB/Simulink 合成工程，但其中的受控规划器不等于在线模型服务验收。
> 接入自己的真实工程仍须核对平台原生依赖、模型服务、独立参考结果和约定容差。
> D2 自带数据也是合成夹具；可引用的诊断证据不等于已证明唯一根因，仍需真实标注案例验收。

### 功能与边界

| 能力 | 当前实现 |
|---|---|
| Web 首页 | 三栏需求与确认入口；通用展示观测、表格、来源和缺口；最近会话默认折叠，支持拖动列宽 |
| 本机 MATLAB 工程 | 静态发现已有入口/模型；确认后在工程副本执行；显式时序工件 → 独立 stepinfo → 有来源评价 → Word/JSON，不要求 HTTP Worker |
| Simulink 数值适配器 | 已准备的 Simulink 工程、配平点 1、阶跃响应和开环稳定裕度 |
| 离线诊断适配器 | 窗口统计、显式对齐信号比较、已记录参数敏感性、局部环路裕度；不调用 MATLAB worker |
| 诊断证据循环 | 保留初始基线；引用证据追加假设修订、优先级及下一轮计划，最多 8 个假设且不删除已有 ID；共享预算、缺项或未知执行状态明确停止 |
| 通用判定与交付 | 按节点、指标、工况和一致单位比较；可不要求总体结论；按需求实际生成 JSON / DOCX，保留原始分析证据 |
| 诊断交付 | DOCX / JSON 保留结构化摘要、当前假设、修订历史与证据索引；责任归属允许无法判定，建议不自动执行 |
| 共享执行与证据内核 | D1 节点、D2 轮次和通用任务使用同一有界执行驱动；设计版本、效果权限、能力版本和完整规格指纹；数值、评估和报告缓存分层 |
| 通用主能力 | 可信开发者注册严格输入/输出 schema 的能力后，自动进入规划目录与通用确认、执行和证据展示；不继承 D1 的配平点或三节点约束 |
| 会话与复用 | 追问保留任务状态；显式续接同一远程运行；已知失败可修复重试，仅补跑缺失分析 |
| 失败恢复 | 原调用对账、已知失败显式重试、新需求重新规划；旧 v1 任务保留原有有界恢复方案 |
| 受控工具扩展 | 开发者注册带输入/输出 schema 和能力依赖的只读工具，不开放模型动态导入或任意执行 |
| 兼容工作台 | `/workbench` 上传单个 UTF-8 `.m` 脚本或函数 + JSON 参数 |
| 执行后端 | 本机专用 Python 子进程 + MATLAB Engine；或显式选择的本机/远程 HTTP Worker |
| 上传工作台输出 | JSON 数据、运行日志、最多 5 张 PNG、结果与报告下载 |
| CLI / 上传后分析 | LangChain 工具调用、演示判据、带知识来源的中文解释 |
| 模型接口 | 默认使用支持工具调用的 OpenAI 兼容 Chat Completions；目录工作流也可显式使用本机 Copilot CLI |
| CLI 知识库 | 本地 Markdown 的中文双字词项与英文词项检索，不是向量数据库 |
| 暂不提供 | 自动配平、唯一根因证明、优化、模型编辑、Simate 执行、多用户登录或安全沙箱 |

当前交付范围是 **需求驱动的通用 Agent + 已注册 Simulink 与有界离线诊断适配器**，
不等于三份原始需求已全部验收。通用扩展机制不代表已实现新的飞控计算算法。
按原始文档对照：

| 原始文档 | 当前覆盖 | 未完成的验收或能力 |
|---|---|---|
| 《飞控设计Agent D1业务场景与功能流程》v0.5 | 目录发现、自然语言规划、基线确认、受控执行、结果追问、版本化交付与失败恢复 | 不同模型服务、目标平台和代表性工程的独立业务验收 |
| 《飞控设计Agent D1案例要求》v0.3 | 配平点 1 的时域响应与稳定裕度；5% 调节时间、超调及最差裕度判据与报告链路 | 真实工程的独立人工 MATLAB 参考结果、约定容差及逐项数值对照 |
| 《飞控设计Agent Demo一级Roadmap》 | D1 软件闭环；D2 假设优先级、有证据引用的修订、问题/定位/责任归属/建议和明确停止 | D2 主动补跑 Simulink、真实标注故障案例验收；D3-D6 功能 |

D3-D6 仅预留设计版本、能力效果、候选写入与采纳的共享契约，
尚未实现优化、拓扑编辑或设计候选采纳工作流。

统一入口的需求理解和工程任务规划需要大模型；旧工作台的普通 MATLAB 计算不依赖大模型。
选择本机 `matlab_project` 时，Web 主机须有已许可 MATLAB 和兼容 Engine 解释器，
不必启动 Worker；选择远程 `simulink_project` 时 Web 不需 MATLAB，但 Worker 主机需要。
D2 的已登记 JSON 数据分析只需要 Web/Agent 和模型服务，不要求 MATLAB 或 worker。
项目不附带 MATLAB、模型权重或可直接使用的 API 凭据。

## 项目框架

### 系统分层

| 层次 | 主要模块 | 职责 |
|---|---|---|
| 入口与展示 | `cli.py`、`web.py`、`static/` | Web 页面、目录 CLI、请求校验、鉴权与文件下载；前端为原生 HTML/CSS/JavaScript |
| 会话协调 | `verification/coordinator.py`、`routes.py`、`store.py` | 接收需求、维护会话和待办决定、归档旧任务、调度规划/确认/续接 |
| 需求与规划 | `verification/agent_planner.py`、`intake.py` | 根据已登记资源和能力提出回答、澄清、缺口或 v2 方案；绑定原文来源与工程选择 |
| 统一契约 | `runtime/agent_contracts.py`、`requirements.py`、`observations.py` | 用 Pydantic 定义需求、判据、依赖图、基线、观测、评价和产物 |
| 能力与资源注册 | `runtime/capabilities.py`、`capability_metadata.py`；`verification/resources.py`、`plugins.py` | 登记工具版本、输入/输出 schema、权限、可用性、资源定位与指纹 |
| 执行与证据内核 | `verification/agent_runtime.py`；`runtime/loop.py`、`budget.py`、`evidence.py` | 按依赖图执行，核对授权/预算/版本，记录调用、校验哈希、复用符合条件的证据 |
| 本机 MATLAB 适配 | `verification/matlab_projects.py`、`local_matlab.py`、`local_matlab_worker.py` | 静态发现、工程副本、兼容 Python 子进程、Engine 调用与原调用对账 |
| 远程/历史适配 | `verification/backend.py`、`worker.py`、`adapters/simulink.py` | Worker 工程发现、远程执行、续接及历史 Simulink 分析 |
| 离线诊断 | `verification/diagnostic_data.py`、`diagnostic_engine.py`、`diagnosis.py` | 读取已登记 JSON、只读计算、假设审查与有界证据循环 |
| 确定性评价与交付 | `verification/adapters/evaluation.py`、`delivery.py` | 按已确认判据比较，生成 JSON/Word，保存来源、工况、有效性与文件摘要 |
| 兼容执行链 | `agent.py`、`matlab.py`、`upload_engine.py`、`jobs.py` | 固定算例 CLI、单文件上传工作台及原工具循环，不与通用目录输入混用 |

完整文件分布见[项目目录结构](#项目目录结构)。

### 数据与控制流

```text
浏览器 / directory-task CLI：任务需求 + 可选资源 / 成果目录
              |
              v
本地 Web 服务（FastAPI，默认 127.0.0.1:8000）
  需求与来源 -> 知识回答 / 澄清 / 能力缺口 / 注册能力的 v2 计划
              |
  服务端绑定资源、来源、能力快照 -> 等待用户确认基线、范围与预算
              |
  共享有界驱动 / 权限 / 固定能力版本 / 不可变证据 / 版本化会话
              |
              +-> 注册计算：严格 schema -> 确认的依赖图 -> 观测 / 发现 / 表格
              |
              +-> 本机 matlab_project：工程副本 -> 专用兼容 Python -> MATLAB Engine
              |       已有入口/模型 -> 时序工件 -> 独立 stepinfo -> 评价/交付
              |
              +-> Worker simulink_project：本机 HTTP / SSH 隧道 / 已部署的 HTTPS
              |       MATLAB Worker（默认 127.0.0.1:9001）
              |       -> 指纹校验 + 独立工程副本 -> Python 子进程 -> MATLAB / Simulink
              |
              +-> 离线诊断：Agent 主机上的已登记 JSON 数据
              |       只读分析 -> 引用证据的审查 -> 下一轮或明确停止
              |       -> 诊断 DOCX / JSON / 证据索引
              |
              +-> 按需求追加：确定性评价 / JSON 或 Word 交付
              v
实际数值与文件 -> 通用观测与不可变回执 -> 按已确认需求评价与交付

/workbench：可信 .m + JSON -> 原上传执行 / 可选 Agent 分析
CLI analyze：固定 MATLAB 算例 -> 演示规则检查 / 本地知识检索
```

目录 Runtime 保存自己的版本化会话、计划和执行记录，不依赖 LangGraph checkpoint。
旧 CLI 的工具循环底层使用 LangGraph。Web/worker 任务调度由单线程执行池和本地文件存储
完成，没有 Redis、Celery 或业务数据库。每个服务的目录任务和旧上传任务共用执行槽位。

### 主要内置能力

下列名称是注册能力 ID，不是用户必须手工填写的命令。规划器只能选择已注册且满足条件的能力。

| 能力 ID | 输入/依赖 | 主要产出 |
|---|---|---|
| `matlab_execute_project` | 已确认本机工程、已有模型或分析入口、初始化入口、显式信号选择 | 执行回执、原始结果；配置输入/响应信号时发布 `series.json` |
| `matlab_stepinfo` | 直接依赖发布的时序工件、相同工况、明确调节带 | 超调量 `%`、调节时间 `s`、响应图及有效性诊断 |
| `simulink_step_response` | Worker 已准备工程及适配器约定 | 时域响应指标与证据 |
| `simulink_stability_margins` | Worker 已准备工程、已配置线性化点及工作点约定 | 开环稳定裕度与证据 |
| `offline_diagnosis` | Agent 主机上的已登记离线数据 | 只读分析、假设与引用证据的诊断结果 |
| `evaluate_criteria` | 已确认判据和依赖节点的真实观测 | 单项 `pass/fail/undetermined`，按要求提供总体判定 |
| `deliver_results` | 已确认交付格式及通过完整性检查的依赖证据 | `result.json`、`result.docx` 和交付索引，按需求生成 |

本机阶跃分析的典型依赖图为：

```text
matlab_execute_project -> matlab_stepinfo -> evaluate_criteria -> deliver_results
```

这只是有判据且要求报告时的典型方案，不是硬编码的固定四步。
知识回答可以没有执行节点；纯分析可没有评价节点；未请求 Word 时不强制生成 Word。
新增能力的接入方式见使用说明中的“注册通用能力”。

## 环境准备

| 组件 | 要求 |
|---|---|
| Web / Python 项目 | Python 3.11+；Web 与 worker 可以使用不同的兼容虚拟环境 |
| MATLAB worker | 已安装并激活 MATLAB，安装与发行版匹配的 MATLAB Engine Python 包 |
| 本机 MATLAB 工程 | 已许可 MATLAB；已有兼容 Python 环境及匹配 Engine/项目包；stepinfo 需 Control System Toolbox，模型仿真另需 Simulink 及工程自己的产品 |
| D1 工具箱 | Simulink、Control System Toolbox、Simulink Control Design |
| Python 与 MATLAB | 必须同时满足 MATLAB 发行版的 Python 兼容性要求，不能只看 Python 3.11+ |
| 模型服务 | 默认 API 需支持 `tools` / `tool_calls`；目录工作流也可使用已登录的本机 Copilot CLI |
| 浏览器测试（可选） | 开发依赖中的 Playwright 及其 Chromium 浏览器 |

没有 MATLAB 或模型 API 时，可以先浏览 Web 页面和运行离线测试，
但不能获得真实的 MATLAB 计算结果或真实模型报告。

## 使用说明

### A. 克隆项目并打开本机页面

首次使用，在终端执行：

```bash
git clone https://github.com/hydavinci/flight16-agent.git
cd flight16-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
flight-agent web --port 8000
```

在浏览器打开 `http://127.0.0.1:8000`，进入目录会话；单文件上传入口为 `/workbench`。
首页浏览器标签为 `Flight Agent`，两个页面共用本地 F 图标，不依赖外部图标服务。
也可通过左侧“运行环境”中的“MATLAB分析工作台”进入，并通过工作台顶部“返回 Flight Agent”回到会话页。
“最近会话”默认折叠；手动展开或收起后，调整窗口宽度不会改变其状态。
桌面首页采用三栏：左侧工程/输出目录与会话历史，中间是对话记录和底部输入框，
右侧是评价指标、方案确认、执行证据及交付下载。三栏独立滚动；
窄屏时配置栏默认折叠，可展开编辑，切换窗口宽度不会清空未发送的输入。
浅色紧凑布局优先展示对话、关键数值和报告；待确认时自动展开执行方案，完成后默认收起方案与日志。
指标摘要最多显示 6 位有效数字，完整精度、工况、判据来源及证据保留在可展开详情中，
不改变计算结果或合格判定；报告 SHA-256 位于“数据与审计记录”，成果路径可展开查看。
详情中的宽表格可横向滚动，可聚焦后用方向键查看，不挤占对话区。
桌面可拖动两条分界线自定义列宽，刷新后沿用当前浏览器保存的宽度；
双击任一分界线或聚焦后按 Enter 恢复默认布局，左右方向键微调，Shift 加方向键加大步长。
分界线仅在悬停或聚焦时显示短拖柄；键盘焦点局限于拖柄，不绘制整屏边框。
两侧至少保留 200px / 240px，对话区至少 480px；缩小窗口时自动适配，重新放大后恢复偏好宽度。
未配置模型服务时仍可查看页面和浏览目录；计算后端不可用时明确报告缺口，不会生成模拟结果。
D2 不依赖 worker；模型服务仍需配置，数据必须满足 `docs/d2-agent.md` 的登记契约。
旧工作台可以选择本地文件，其普通 MATLAB 执行不需要模型配置。
`.env.example` 中的模型名称、地址和 Key 都是占位内容，不代表已经连接模型服务。
已有 `.env` 时不要再次复制覆盖，应保留自己的配置并按需编辑。
更新代码后需重启已运行的 Web 服务：静态页面会读取新版文件，但旧进程仍使用旧 API。
如果页面提示本机 MATLAB“状态未知”，请重启后端加载新功能；刷新页面不能替代后端重启。

### A1. 本机通用 MATLAB / Simulink 工程（无需 Worker）

使用已有、已人工核验的工程，不上传单个文件。三个输入是：

1. **工程目录**：Web 主机可见的绝对目录，资源位置选 `matlab_project`。
2. **任务需求**：已有分析入口或模型、初始化入口（分别标明角色）、分析方法、工况。
3. **评价指标**：可选；要求判定时给出有来源阈值及计算带，例如超调量 ≤10%、
   调节时间 ≤3 s，stepinfo 调节带 5%。5% 是计算参数，不是验收阈值。

一组完整的表单输入示例：

| 输入项 | 示例内容 |
|---|---|
| 资源位置 | 本机 MATLAB / Simulink 工程，即 `matlab_project (agent)` |
| 资源目录 | `/absolute/path/to/prepared-project`，替换为实际已有工程的绝对目录 |
| 任务需求 | 沿用工程已有 Stick 阶跃激励，执行非线性闭环时域仿真；对文档指定的响应调用 MATLAB `stepinfo`；交付 Word 和 JSON |
| 评价指标 | 超调量 ≤10%；调节时间 ≤3 s；调节时间采用 5% 误差带 |
| 输出根目录 | `/absolute/path/to/results`，或留空使用默认位置 |

操作顺序：发送需求 → 回答缺项澄清（如有）→ 核对发现的工程、信号、判据来源及方案 →
勾选可信工程并确认当前版本 → 等待执行 → 查看数值有效性和判据结果 → 下载实际生成的报告。
上述阈值是这份示例的用户输入，不是程序预置标准；示例不保证你的工程会满足阈值。
对话气泡保留“工程分析要求”“评价指标”标题，不显示内部结束标记；保存的原文仍用于来源追踪。

服务启动命令形状如下（配置示例，不表示该命令已在你的环境运行）：

```bash
MATLAB_ALLOW_TRUSTED_PROJECTS=1 \
MATLAB_PYTHON_EXECUTABLE=/absolute/path/to/compatible/python \
flight-agent web --port 8000
```

所选解释器须已经安装本项目及匹配 Engine；未设置时使用 Web 当前解释器。
探测与执行使用同一解释器。首页“可用”只证明有界导入探测成功，不验证启动或许可。
首页与能力目录共享环境未变时的导入探测；执行前仍重新探测并检查授权。
MATLAB 缺失、许可/启动失败、未知执行状态会明确停止，不会自动安装或改系统配置。
本机工程和旧 Worker 共用 `MATLAB_ALLOW_TRUSTED_PROJECTS=1` 开关；
仍须确认准确基线版本及可信工程，工程代码/回调拥有服务账户权限，不是沙箱。

无需强制登记文件；静态发现读取有界 `.m`、模型元数据和工程文档，不执行工程。
工程已明确提供的配置直接沿用；缺少入口、模型歧义、信号或带宽来源冲突才澄清。
例如工程 README 与请求应明确提供：

```text
Existing model: models/limited_loop.slx
Existing initializer: setup/prepare.m
logsout input signal: lever (1)
logsout response signal: stroke (m)
condition: limited
stepinfo settling band 5%
```

主模型和初始化入口也支持文档中的明确声明，例如“统一从 `models/limited_loop.slx` 进入”、
“模型位置：`.\models\limited_loop.slx`”和“唯一初始化入口是 `setup/prepare.m`”。
Markdown 行内代码、引号及 Windows 相对路径只用于匹配已发现的完整路径，原文证据不改写。
候选列表、示例、否定和不确定声明不作为确定选择；真实冲突仍须澄清，方案仍须确认后执行。

首轮规划即提供按现有来源优先级和绑定规则确定的 `source_selections`，仅列出唯一、
已发现且未被否定的选择，不把单一库存候选当作来源声明；模型仍须显式提出完整方案。
即使文档说明模型回调会初始化，方案也不能省略来源明确的初始化入口。
工程需预先备齐当前 MATLAB 平台可调用的依赖；Engine 导入预检不代表许可证、
工具箱或原生 MEX 已验证。平台依赖变更后必须重新发现工程指纹并确认新方案，
Agent 不会自动编译依赖或修改工程来掩盖执行失败。
已有来源唯一明确、但模型方案遗漏或选错入口时，会将来源选择反馈给规划器修正一次，
不会静默改写方案或要求用户重复提供已有信息。此纠错与结构化格式纠错共用最多两次模型调用、
原总超时和上下文大小限制；修正后仍完整校验方案，失败则明确停止，不触发执行。

信号也支持 README Markdown 表格的“日志名/Signal name”和“单位/Unit”列。
`logsout`、`workspace` 或 `return` 来源须由同节（可继承父节）的明确说明提供，
或在表格“来源/Source”列逐行声明；不会从无关章节或其他文件补猜来源。例如：

| Signal name | Source | Unit |
| --- | --- | --- |
| lever | logsout | 1 |
| stroke | logsout | m |

日志表只证明文档列出的信号身份；输入/响应角色仍须结合分析要求和原文含义选择。
明确角色冲突、未知单位或来源歧义仍须澄清，不以配置常量代替真实时历，也不声称已验证实际日志。

这是合成测试夹具的命名示例，不是可直接运行的用户工程，也不是飞机参数。
MATLAB-only 可声明 `Existing analysis entry: analysis/thermal_response.m`、
`return input signal: signals.excitation (V)`、
`return response signal: signals.temperature (K)`、`condition: bench`，
或用 `workspace` 显式选择已有脚本输出。工程自己的实际名称/单位才是依据。
可选 `flight-agent.project.json` 沿用既有发现配置；其中历史
`input_signal` / `response_signal` 名称和单位表示 **logsout**，不是 workspace。

已有函数支持固定多输出签名，当前结果通道只读取第一个输出。
省略输入仅支持源码开头可静态确认的 `if nargin == N` / `if nargin < N`
默认参数赋值；不会猜参数、注入默认值或修改函数。其他可选参数写法仍须澄清，
`varargin` / `varargout` 仍不支持。执行时继续核对实际函数路径及声明的输入/输出数。

CLI 连接已启动的本机 Web，结构化输入使用同一确认/下载链路，例如：

```bash
flight-agent directory-task propose \
  --location matlab_project --project-root /absolute/path/to/prepared-project \
  --analysis-request '按工程文档的已有入口和信号配置执行阶跃分析，调用 stepinfo，交付 Word 和 JSON' \
  --criteria '超调量 <=10%；调节时间 <=3 s；stepinfo settling band 5%'
```

旧 `--request` 仍可用；`--scenario d1` 明确选择旧 Worker 路径，不是本机默认。

确认后只调用已有入口/已有模型，不生成分析脚本，不补建激励、日志或控制器，
不修改源模型。原工程与输出目录不得重叠；结果写入独立任务目录/指定成果目录，
同一执行节点保留函数/模型回调写入的 base 变量，并用 `SimulationInput` 临时传入
初始化脚本局部变量；同名时局部变量优先，不修改保存的模型配置。
节点之间只传带生产者、摘要、大小的时序工件，不共享隐藏 base workspace，
也不接受 LLM 提供的数组充当实测结果。已知失败要求显式重试；未知状态只能核对原
调用 ID，不能自动重发。失败诊断附带有界 MATLAB 日志尾部以保留内层错误原因；
完整日志留在调用目录的 `local-matlab/matlab.log`。
`.prj` 启动和会清空桥接工作区的脚本未完成实际验收。

本机合成全链路已用 Python 3.12.14 / MATLAB R2025b 验证：MATLAB-only 解析响应，
以及含饱和器的真正非线性闭环 Simulink 模型（脚本局部变量、函数 base 变量及混合初始化）；
实际执行、独立 stepinfo、评价、Word 曲线和 JSON 下载均走注册能力与确认接口。
5% / 2% 带分别与独立解析参考比较；数值和容差见 `docs/d1-agent.md`。
上述自动化回归使用测试工程与受控规划器，不能代替具体用户工程、
在线模型服务及其工程参考的端到端验收。

### B. 可选：在有 MATLAB 的执行机器启动 worker

本节用于旧 Simulink 适配器/远程执行和上传工作台；本机通用工程按 A1，D2 按 C2。

真实 Simulink 集成已在 macOS 运行。目录执行包含 Windows 专用进程清理路径，
但尚未在 Windows 实机验证；旧上传和 CLI 执行仍要求 macOS/Linux。
将整个项目部署到专用计算账户/机器，安装兼容的 Python、项目依赖和 MATLAB Engine
（参见[安装 Python 项目](#安装-python-项目)与[安装 MATLAB Engine](#安装-matlab-engine)）。
不要使用日常高权限账户；工程初始化、模型回调、原生编译和上传代码
都能访问该账户的文件及网络，工程副本和进程隔离不是安全沙箱。

两台机器的 `.env` 均配置相同的随机令牌。可在你自己的终端生成，再安全复制到两端：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

执行机器的 `.env`：

```dotenv
MATLAB_WORKER_TOKEN=替换为至少32个ASCII字符的随机令牌
MATLAB_ALLOW_TRUSTED_PROJECTS=1
MATLAB_ALLOW_TRUSTED_UPLOADS=0
```

启动 worker：

```bash
flight-agent worker --port 9001
```

默认只监听 `127.0.0.1`。如果 worker 在另一台机器，在你的电脑另开终端建立 SSH 隧道：

```bash
ssh -N -L 9001:127.0.0.1:9001 USER@MATLAB_HOST
```

Web 机器的 `.env`：

```dotenv
MATLAB_WORKER_URL=http://127.0.0.1:9001
MATLAB_WORKER_TOKEN=与执行机器完全相同的随机令牌
```

如果 MATLAB 安装在 Web 所在的同一台机器，**无需 SSH 隧道**：
将上面的 URL、令牌和 `MATLAB_ALLOW_TRUSTED_PROJECTS=1` 放在同一个项目 `.env` 中，
然后在两个终端分别启动 `flight-agent worker --port 9001` 和 `flight-agent web --port 8000`。
两个终端都需进入项目根目录并激活各自虚拟环境；worker 必须使用兼容 Engine 的 Python。
Web 端还需按[配置大模型](#配置大模型)接入实际模型服务。两个可信执行开关独立；
只有需要旧上传工作台时才另设 `MATLAB_ALLOW_TRUSTED_UPLOADS=1`。

若浏览器、Web 和 Worker 都在**同一台带桌面的 macOS** 上，可将 Web 启动命令替换为：

```bash
flight-agent web --native-directory-picker --port 8000
```

两个「浏览…」按钮会直接弹出系统目录选择窗口；确认后只回填绝对路径，取消不改原值，
不会自动提交任务或授权执行。该模式不适用于远程 Worker，即使它通过 SSH 隧道使用
`127.0.0.1` 地址；远程、无桌面及其他系统部署继续使用默认的网页目录浏览器。

修改配置后重启 Web 服务。也可连接已正确部署的 HTTPS worker；
客户端拒绝非回环地址的明文 HTTP，不跟随重定向，不自动重试任务提交。
不要将 worker 的任意代码执行接口暴露到公网或交给不可信用户。
即使配置访问令牌，也不等于已有多用户身份、权限或安全沙箱。

两项服务都只运行**一个进程**；不要使用多 Uvicorn workers、自动 reload 或多个实例
共享同一运行目录（`runs/verification`、`runs/verification-worker`、`runs/web`、
`runs/worker`）。每个服务一次仅接受一个正在执行/分析的任务，
繁忙时返回明确错误，不建立无限队列。

### C. 统一需求、标准与目录验证

可先直接输入“增益裕度是什么？”，无需填写目录；回答不会触发数值计算。
表单中的“资源位置”来自受信任适配器注册表，只说明目录在哪个主机、如何登记，
不是任务类型或验收标准。本机工程默认 `matlab_project`（Agent）；
旧/远程 Simulink 资源选择 `simulink_project`（Worker），
离线诊断选择 `diagnostic_data`（Agent）；无目录能力可以留空。
切换已知资源位置会清空源目录，不会把验证报告当成诊断数据；输出根目录独立保留，
需自行核对。新任务仍需确认，未知调用未对账前不能切换或重派。

使用现有 Simulink 适配器时：

1. 选择 Worker 资源位置，在表单或聊天中指定 **Worker 所在机器可见的绝对工程目录**，不是单个文件；
   远程路径不会自动上传文件。
2. 输入分析范围、工况、输出和判据，例如“分析配平点 1 的阶跃响应，输出 JSON，不做合格判定”；
   或明确提供自己的阈值、单位、工况和 Word 等交付要求。
   可同时指定 **Agent/Web 所在机器的成果根目录**；不指定则使用默认目录。
3. 核对发现的工程、指纹、分析范围、单位、阈值、稳态筛选策略和实际成果目录，
   勾选可信代码并确认当前基线。
4. 查看执行状态、数值有效性和（要求时）程序判定；下载已实际产出的报告和证据。
5. 在同一会话增加/修改阈值、调整交付或追加分析。每个新基线需要重新确认；
   工程指纹和分析条件不变时复用已有数值，只运行缺失分支。

界面优先展示当前任务、结论和报告；确认提示与可信工程警告不会折叠隐藏。
无实际结果或缺项时不显示空结果卡片；纯分析显示“分析结果”，有实际判据评价才显示
“评价结果”，仅有能力或输入缺口时显示“需求缺项”，无效分析证据仍保留显示。
新方案的每个节点（包括评价、报告交付和缓存复用）至少消耗一轮和一次调用，
两项预算都须覆盖全部节点；内部多轮分析可能消耗额外预算。预算不足的新方案在确认前
拒绝，不自动增加预算。历史任务的已确认预算和执行回执保持不变，补充预算需重新规划并确认。
原始执行日志、诊断技术记录以及非 Word 的数据/审计下载按需展开。
手机端可展开“最近会话”切换任务；状态刷新保留已展开的详情和正在阅读的消息位置。

聊天可使用明确的路径标签；含空格的路径应加引号，例如：

```text
工程目录: "/srv/models/F16 case"；输出目录: "/Users/me/flight-results"；
分析配平点 1 的阶跃响应与开环裕度，输出 JSON，不做合格判定。
```

工程路径支持 worker 使用的 POSIX、Windows 盘符和 UNC 写法；成果路径必须适用于
Agent 所在机器。多个无法区分用途的路径、或同次提交的表单与聊天路径冲突时，
Agent 会先澄清，不扫描猜测的目录。可在后续聊天中切换工程或成果根目录；
旧任务及其交付仍保留下载入口。只移动成果位置也会创建新任务目录和待确认基线，
仅复制完整性校验通过、条件仍匹配的数值证据，不修改旧交付或重复运行已有分析。
清空成果目录字段并发送可恢复默认位置。

内置配置只识别已准备的 F-16 工程布局；其他工程必须提供
`flight-agent.project.json`，不能仅放入任意 `.slx` 后期待 Agent 自动找信号或设置断点。
完整格式、模型准备约定和 API 见 `docs/d1-agent.md`。
工程快照最多 5000 个文件、512 MB，不接受符号链接；源工程不会被用于写入运行产物。

沿用已准备工程分析时，无需在任务需求中重复填写阶跃幅值、施加时刻、仿真时长、
采样或开环断点。Worker 沿用初始化后模型的仿真设置，从登记的日志信号识别单次阶跃；
调节时间按相对稳态变化量的 5% 调节带计算，使用登记的稳态筛选策略。
开环分析读取模型已有的一对有效 `openinput` / `openoutput` 点，并使用工程配置的
工作点模式与反馈符号。规划器获得这些计算约定，但具体设置与数值有效性仍由执行时
校验；缺少日志、阶跃不明确或线性化点不满足约定时明确失败或报告无效证据，不猜测。
这些约定不是验收标准，也不支持通过聊天任意覆盖工程仿真参数。

**没有自动生效的验收标准。** 每条判据记录原用户消息引用与原文，或用户明确采纳的
已提供标准/示例及其身份；模型不能自行填入“常用阈值”。缺标准先澄清。
旧演示的超调 <=10%、5% 调节时间 <=3 s、增益裕度 >=0 dB、相位裕度 >=0 deg
仅是历史示例，只有用户明确采纳才可进入新基线；其中 5% 调节带是指标定义，
稳态末窗长度、波动与漂移筛选是记录在资源中的**计算有效性策略**，不是验收阈值。
比较须匹配生产节点、指标、工况和单位，不隐式换算；只检查单项时可不要求总体结论。
要求总体判定时，全部所需证据有效才给出 PASS 或 FAIL；
有缺失、无效指标或失败分支时整体为 UNDETERMINED，同时保留已确定的单项结果。
执行失败与“不满足判据”是不同状态，0 dB / 0 deg 也不代表满足工程鲁棒性要求。

关闭浏览器不会取消远程执行。服务重启后保留中断记录，用户点击续接时只查询原 worker ID，
不重新提交仿真。只有已核实失败/中断的远程运行才能在再次确认后以新 ID 重试；
修复此类失败工程后也可重新发现并建立待确认基线。
远程状态不明时不能用重试、换工程或采纳重规划方案绕过原运行的续接。
续接保留该次运行记录的适配器版本；旧记录未保存版本时明确标记 `unrecorded`，
不会把历史计算归属于当前适配器，也不会因命中缓存而跳过在途运行的核查。
源文件或分析配置变更会使旧证据失效并要求新基线。

新 v2 任务可在已知失败后显式重试，或提交新需求重新提出待确认方案；
旧 v1 任务保留每个基线最多一次的恢复提案。提案不是执行授权，必须确认新基线。
询问原因、解释指标或等待澄清不会把失败、
未确认或中断的任务变成已完成，也不会丢失原来的待办决定。
即使追问的调度或后台处理失败，也会单独记录诊断并保留原任务的恢复状态。

Web 端保存 `runs/verification/<会话ID>/conversation.json`；任务产物保存在其下
`tasks/<任务ID>/`；v2 通用交付使用 `delivery/<调用ID>/`，
旧 v1 交付保留 `delivery-v<基线版本>/`。指定成果根目录时，
Agent 在该根目录下为任务创建唯一子目录，会话记录仍保存在默认会话位置。
成果根目录不接受相对路径、路径穿越或符号链接，也不能与源工程目录重叠。
默认位置及清空重置同样在规划前检查；已保存的不安全任务目录不能续接执行。
Worker 端保存 `runs/verification-worker/<运行ID>/`，其中 `project/` 是独立工程副本。
旧版本报告和证据保持可下载；Web 已下载的产物无需 worker 在线也可访问。
这些目录可能包含工程副本和敏感结果，应设置账户权限及保留策略。

### C2. 离线只读诊断与兼容目录 CLI

**仅运行 D2 的最短路径：**完成 A 节的项目安装，按[配置大模型](#配置大模型)接入模型服务后启动 Web。
无需安装 MATLAB、启动 worker 或设置 `MATLAB_WORKER_*`；仍需可用的工具调用模型服务。

在资源位置选择已登记离线诊断数据，指定 **Agent/Web 主机**可见的数据目录并描述诊断需求。
目录必须包含 `flight-diagnosis.json` 及其登记的 JSON 数据文件；不是任意 MATLAB 工程，
也不会自动扫描或执行工程代码。示例位于 `examples/diagnosis/synthetic-read-only/`。
该资源的目录浏览器使用 Agent 主机，不依赖 Worker 凭据。
同一会话可切换阶段；旧任务与交付归档保留，新阶段重新收集输入并确认，
不把 Worker 工程或验证报告自动作为诊断数据。

先核对控制器/对象版本、输入指纹、初始假设、允许的只读能力、分析参数和预算，再确认基线。
后续计划只能在该数据、能力与预算范围内变化；每轮结果都有独立分析 ID、规格指纹及指标引用。
假设优先级为高/中/低，表示分析顺序而非置信度。评审可凭已有有效证据追加假设、
修订陈述或调整优先级，但不得删除已有假设 ID、超过 8 个假设或改写原始基线；
修订理由与引用会另行保留。

界面与 Word/JSON 报告展示问题、定位、责任归属、依据和后续建议，并保留
支持/反驳/未知状态、假设演进、证据不足项与停止原因。模型/控制侧归属必须引用
受支持假设及对应有效证据；无法区分时为“尚无法归属”，不能从关联直接推断物理根因。
历史记录缺少结构化摘要时明确显示未记录，不补造结论。`evidence_sufficient`
表示本次假设审查认为证据足够，**不是唯一根因证明或飞行器合格结论**。
建议只供复核，不触发设计修改或额外仿真；主动补跑 Simulink 仍未实现。
只读效果是可信适配器的调用契约，不是操作系统沙箱。

服务重启后需显式续接：已有且校验通过的分析回执直接恢复；在途调用缺少回执时，
以 `unknown_execution` 停止，不自动重新派发。独立只读输入快照的恢复同样校验内容与所有权，
不会覆盖冲突文件。澄清回复可沿用经限长和脱敏的近期对话，但历史消息不能替代确认。

目录 CLI 连接**已经运行的本机 Web 服务**，与网页共用会话、确认和交付，不另开离线执行器：

```bash
flight-agent directory-task --server http://127.0.0.1:8000 propose \
  --scenario d2 --project-root "$PWD/examples/diagnosis/synthetic-read-only" \
  --request "分析已登记的时序与频响数据，提出可检验假设并明确证据不足项。"

# 用上一条输出的会话 ID 和当前基线版本替换占位值；先核对计划再确认
flight-agent directory-task confirm CONVERSATION_ID --version 1 --trusted-project
flight-agent directory-task status CONVERSATION_ID
flight-agent directory-task resume CONVERSATION_ID
```

`--server` 和 `--timeout` 放在 `propose/confirm/status/resume` 之前。
CLI 新会话不需要 `--scenario`，使用统一需求规划，例如：
`flight-agent directory-task propose --request "解释增益裕度的含义，不执行工程。"`。
旧参数保留兼容：`--scenario d1` 仅映射到 Worker 工程资源位置，
`--scenario d2` 仅映射到 Agent 登记数据位置；`auto` 不选择资源。
这些别名都不恢复硬编码任务类型或自动采纳旧判据，上面的诊断命令使用的是资源别名。
用 `propose --conversation CONVERSATION_ID --request "..."` 续聊；
可附旧 `--scenario d1|d2` 选择资源位置，未指定则保留当前资源位置。
`--output-root` 是 Agent 成果根目录，空字符串显式恢复默认位置。
CLI 自动获取页面令牌，只接受本机 HTTP origin，不跟随重定向或环境代理。
等待超时不取消服务端任务；用 `status` 读取持久状态，不能据此重复确认或重新提交。

D2 输入格式、能力参数、预算和恢复边界见 `docs/d2-agent.md`。
历史任务保留原 schema、评价及恢复行为；缺失的历史判据来源明确显示未记录，不补造。

### C3. 注册通用能力

可信应用启动代码向 `CapabilityRegistry(agent_mode=True)` 注册 `Capability(...)`，
声明稳定名称/版本、严格输入模型、`CapabilityOutcome` 输出、效果权限和
`CapabilityMetadata`（指标/单位、资源需求、有效性策略、是否允许缓存）。
无目录能力不必携带 F16、控制器/对象或配平字段；新资源类型另注册 `ResourceAdapter`。
通过 `EngineeringRuntime` 注入同一注册表、资源表和会话存储即可接入现有 Web：
无需修改核心执行器、路由或页面。完整最小装配例子见 `docs/d1-agent.md`。
插件是可信 Python 代码，不是沙箱；模型不能动态安装、导入或注册插件。

v2 基线固定需求、真实来源、资源指纹、能力完整声明、输入绑定、效果和预算。
同会话缓存须能力显式声明可复用，且实际输入、条件、数值策略、资源、
依赖输出及版本匹配；阈值仅改变评价/交付时不重算原数值，不覆盖旧回执或报告。
待执行能力升级或同版本声明变化均需重新规划确认；未知原调用只能对账。
通用结果直接展示观测、发现、表格、判据及来源和缺项，历史任务只读投影到同一视图。
测试用 RMSE 与不同名称/单位的峰值计算只在 `tests/` 注册，
演示真实合成计算、重新确认、复用、JSON/Word 下载，不进入生产装配，不冒充工程验证。

### D. 兼容工作台：上传与查看结果

打开 `/workbench`，并在 worker 端启用 `MATLAB_ALLOW_TRUSTED_UPLOADS=1`：

1. 选择本地单个 UTF-8 `.m` 文件，选择脚本/函数类型。
2. 填写 JSON 对象参数，核查源代码后勾选可信代码确认，再点击“运行 MATLAB”。
3. 页面轮询任务状态，显示 `result`、日志和 PNG 图表，可下载 JSON。
4. 可点击“Agent 分析”：复用已完成的计算，不重新执行 `.m`。兼容现有参数及指标格式
   时调用原 Agent；不兼容时显示 `undetermined`，而不是猜测判据。

输入输出约定：

```matlab
% example_script.m: script reads params and assigns result
result = struct('value', params.value * 2);
disp('Calculation complete');
plot([1 2 3], [2 4 6]);
```

```matlab
function result = example_function(params)
result = struct('value', params.value * 2);
end
```

对这两个例子填写参数 `{"value": 3}`，预期返回数据为 `{"value": 6}`。
脚本例子还会产生运行日志和一张 PNG 图；没有绘图代码的程序不会产生图片。
页面只展示程序主动返回的 `result`，不会自动导出 MATLAB 工作区的全部变量。

函数名须与上传文件名一致，首版函数声明使用上述 `result = 函数名(params)` 形式。
脚本里的 `clear` 不应删除 `params`，代码必须明确产生 `result`；不能依赖交互输入、
外部工作区变量或未上传的配套文件。不支持 ZIP、多个依赖文件、`.mlx` 或 `.slx`。

上传限制：源文件 1 MB、参数 100 KB、整体请求 2 MB、JSON 结果约 4 MB、
日志 1 MB、最多 5 张 PNG（单张 2 MB、1600 万像素）、超时 1～600 秒。
`result` 须可 JSON 序列化，不含 NaN/Infinity；图形通过 `exportgraphics` 导出，
执行机器需要支持该函数的 MATLAB 发行版。对象/UI 图形或不支持导出的内容会明确失败。
文件大小轮询和超时不是系统级内存/磁盘配额，不能用于限制恶意程序的资源使用。

浏览器提交参数时保留校验后的原始 JSON 数字文本，结果展示和下载使用服务端生成的
JSON 文本，避免 JavaScript 数值转换悄悄舍入大整数。这不改变 MATLAB 本身的数值类型
及计算精度，也不提供任意精度计算。

自带示例保留在 `examples/` 目录，可手动选择对应 `.m` 文件上传并填写参数：

| 文件 | 输入 | 展示结果 |
|---|---|---|
| `examples/web_script.m` | `frequency_hz`、`amplitude`、`samples` | 波形数据和图 |
| `examples/web_function.m` | 同上 | 函数形式的波形数据和图 |
| `examples/web_metrics.m` | `examples/parameters.json` | 兼容 Agent 演示规则的指标 |

波形参数示例：`{"frequency_hz": 1, "amplitude": 1, "samples": 200}`。
波形返回值不适用飞行器演示规则，Agent 页面显示“无法按当前规则判定”属于预期行为。

Web 任务保存在 `runs/web/<任务ID>/`，执行机器任务保存在 `runs/worker/<任务ID>/`。
两边任务 ID 分开记录，源码摘要和原始参数可用于追溯；Agent 报告引用实际上传源代码，
而不是默认演示函数。最近任务入口保留最近 20 项。
切换任务会清空上一任务的结果和报告，并暂时禁用分析、下载操作；只有新任务状态和结果
成功载入且任务 ID 匹配后才恢复可用操作。加载失败时保持禁用，可重新选择任务重试查看。
关闭网页不会取消任务；网络中断也不代表远程执行终止。客户端不会自动重复提交，
请根据页面记录的远程 ID 核查 worker 后再重试。
旧上传任务在重启后标记为 `interrupted`，不会自动重跑；目录会话的续接规则见上一节。

Web 默认仅允许本机访问，采用同源检查和页面令牌；令牌不是多用户登录。
上传源码发送到配置的 worker，不发送给大模型；执行结果在请求 Agent 分析时可能发送
到模型服务。所有结果/日志/报告在页面按文本渲染，不执行其中的 HTML。
运行目录可能包含敏感数据，需要自行设置目录权限和数据保留策略。

**当前为软件演示项目。自带 MATLAB 算例不是飞行动力学仿真，阈值不是航空设计或适航要求。
不得用演示判定代替真实飞行器评审或放飞决策。**

## 详细输入说明

### 1. Web 表单与资源位置

| 输入 | 是否必需 | 说明 |
|---|---|---|
| 任务需求 | 必需 | 自然语言说明目标、已有入口/模型、分析方法、工况及需要的交付；知识问答也从这里输入 |
| 评价指标 | 可选 | 需要合格判定时给出指标、比较关系、阈值、单位、适用工况及来源；纯分析可留空 |
| 资源位置 | 按任务 | 决定目录在哪个主机、通过哪个适配器发现，不决定任务类型或自动授权 |
| 资源目录 | 按能力 | 使用工程/数据资源的能力需要目录；知识问答和无目录能力不需要 |
| 输出根目录 | 可选 | 始终是 Agent/Web 主机上的目录，不是 Worker 路径；留空使用默认任务目录 |
| 可信工程确认 | 执行前必需 | 确认当前基线版本、权限和预算；填写表单或选择目录不等于允许执行 |

| 位置 ID | 所在主机 | 接收的目录 | 不代表什么 |
|---|---|---|---|
| `matlab_project` | Agent/Web | 已有 MATLAB / Simulink 工程 | 不需要先部署 HTTP Worker，也不会替你创建缺少的模型或激励 |
| `simulink_project` | Worker | 满足历史 Simulink 适配器约定的已准备工程 | 不是上传入口，远程路径不会自动复制到 Worker |
| `diagnostic_data` | Agent/Web | 包含 `flight-diagnosis.json` 的已登记 JSON 数据目录 | 不是任意 `.mat`、CSV 或 MATLAB 工程导入器，不会补跑仿真 |

`agent`、`worker` 表示资源位置，不是三个不同的智能体。
源目录、输出根目录必须适用于各自所在主机；不能重叠，也不能通过符号链接绕过路径检查。
默认会话目录与用户指定成果目录是两个不同概念，详见输出目录说明。

### 2. 已有工程应提供什么

本机工程没有强制统一的文件布局，以下是说明角色的示例，而不是 Agent 自动生成的模板：

```text
prepared-project/
|-- README.md                 # 推荐：已有入口、初始化、信号名称/单位、工况和指标定义
|-- models/closed_loop.slx     # 如使用 Simulink，提供实际已有的模型
|-- setup/prepare.m            # 如需要初始化，明确其角色及脚本/函数参数
|-- analysis/run_analysis.m    # 如使用 MATLAB 入口，提供实际已有的脚本/函数
`-- ...                       # 已备齐的依赖、数据和本平台可用的原生模块
```

不要求同时存在上面所有文件；选择“已有模型”或“已有入口”，并提供它真实需要的依赖。
静态发现可从工程文档、源码声明、模型元数据及可选配置中提取候选，但不会执行代码来猜入口。
项目快照上限为 5000 个文件、512 MiB，不接受符号链接。

| 需要明确的内容 | 进入执行方案后的字段/约定 |
|---|---|
| 运行方式 | `ProjectExecutionInput.mode`：`simulink` 或 `entry`；二者不能混填 |
| 模型或入口 | `model` 为已发现 `.slx/.mdl` 的相对路径；或 `entry` 指定已有 `.m`、脚本/函数类型及显式参数 |
| 初始化 | 可选 `initializer`，与分析入口分别声明；来源已经明确时不能在方案中省略 |
| 工程/工作目录 | 可选 `project_file`、`working_directory`，均须对应已发现的工程内容；`.prj` 启动仍需按实际工程验收 |
| 输入/响应信号 | `input_signal` 与 `response_signal` 成对提供；`source` 为 `logsout/workspace/return`，`path` 是明确字段路径，`unit` 是真实单位 |
| 工况 | `conditions` 记录本次分析适用条件，后续指标与判据必须匹配 |
| 指标计算 | `StepinfoInput.settling_band` 是 `0 < 值 < 1` 的比例，5% 应为 `0.05`；稳态策略影响数值有效性，不是验收标准 |

已有函数当前通过第一个输出读取结果；固定多输出签名不等于读取所有输出。
可省略参数的支持范围及 README 信号声明示例见[本机工程使用说明](#a1-本机通用-matlab--simulink-工程无需-worker)。
Worker 工程配置格式见 [D1 文档](docs/d1-agent.md)；离线数据清单与数据字段见
[D2 文档](docs/d2-agent.md) 和[合成示例](examples/diagnosis/synthetic-read-only/flight-diagnosis.json)。

### 3. 创建/追加会话的接口输入

接口字段以 [MessageRequest](src/flight_agent/verification/routes.py) 为准。
创建用 `POST /api/conversations`，追加用 `POST /api/conversations/{id}/messages`。

```json
{
  "location": "matlab_project",
  "project_root": "/absolute/path/to/prepared-project",
  "output_root": "/absolute/path/to/results",
  "analysis_request": "沿用已有 Stick 阶跃激励进行非线性闭环时域仿真，调用 stepinfo，交付 Word 和 JSON。",
  "evaluation_criteria": "超调量 ≤10%；调节时间 ≤3 s；采用 5% 调节带。"
}
```

| 字段 | 类型与约束 | 说明 |
|---|---|---|
| `analysis_request` | 非空字符串，最多 4000 字符 | 结构化分析要求；不能和 `message` 同时提供 |
| `evaluation_criteria` | 可选非空字符串，最多 4000 字符 | 与 `analysis_request` 配套；不能单独提交 |
| `message` | 非空字符串，最多 4000 字符 | 兼容的单消息形式，可含自然语言需求与明确路径；不能混用两个结构化字段 |
| `location` | 可选字符串，最多 128 字符 | 注册资源位置 ID，如 `matlab_project` |
| `project_root` | 可选字符串，最多 4096 字符 | 对应资源主机上的目录；工程能力还会检查绝对路径、指纹和布局 |
| `output_root` | 可选字符串，最多 4096 字符 | Agent 主机上的成果根目录；显式空字符串可恢复默认位置 |
| `scenario` | 可选 `auto/d1/d2` | 仅保留资源位置兼容别名；新客户端优先用 `location`，不用于自动注入场景判据 |

结构化输入还受**合并后的原文总长度不超过 4000 字符**限制，包含内部段落边界，
不是两栏可以同时各填 4000 字符。未知字段、空白文本和冲突输入会被拒绝。
原始分析要求和指标原文会保留；显示层不显示结束标记，不改变服务器保存的文本。
改变目录、阈值、范围或交付要求后，应核对新基线，不能沿用旧确认代替新授权。

### 4. 判据、方案与确认

程序不会把“通常可接受”的数值当成标准。规划后的每条判据必须绑定生产节点、指标、
比较符、有限数值阈值、单位、工况和原文来源。例如下面只是结构示例，不是完整执行方案：

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
来源可为用户消息、标准文件或明确采纳的示例；文件/示例来源还必须有 SHA-256 身份。
`node_id` 必须对应本次方案的真实生产节点，不能机械复制示例中的名称。

v2 基线还固定 `requirements`、`resources`、`nodes`、能力版本快照、
`allowed_effects` 和 `budget`。每个节点声明依赖及输入绑定，不接收模型伪造的实测数组。
默认预算为 6 轮、12 次调用、300 秒、连续无进展上限 2；实际执行以用户确认的方案为准。
评价、交付和缓存复用也消耗轮次/调用，不能按“并行层数”少算预算。
当前不支持 `write_candidate/adopt_candidate`，不会根据建议自动修改设计。

确认接口为 `POST /api/conversations/{id}/confirm`，输入：

```json
{"version": 1, "trusted": true}
```

版本必须是当前待确认基线；`trusted` 必须是布尔值 `true`，不能用字符串或省略。
部署开关 `MATLAB_ALLOW_TRUSTED_PROJECTS=1` 也不能替代这次确认。

### 5. 环境配置输入

配置模板为 [.env.example](.env.example)，不要提交真实密钥。
`.env` 从启动时的项目根目录读取，已有进程环境变量优先。

| 配置项 | 用途 |
|---|---|
| `LLM_PROVIDER` | 默认 `openai-compatible`；目录规划也支持显式选择 `copilot-cli` |
| `LLM_BASE_URL` | 模型 Chat Completions 服务，不是 Web 或 Worker 地址 |
| `LLM_MODEL` | 部署/账户实际可用的模型名；Copilot 模式可省略并使用 CLI 选择 |
| `LLM_API_KEY` | API 模式的非空凭据；无认证本地服务仍需非空占位值 |
| `LLM_TIMEOUT_SECONDS` | 模型请求总超时配置；格式/来源纠错不重置该总预算 |
| `MATLAB_PYTHON_EXECUTABLE` | 本机 MATLAB 探测与执行使用的已有兼容 Python；省略时使用 Web 解释器 |
| `MATLAB_ALLOW_TRUSTED_PROJECTS` | 设为 `1` 才允许本机/Worker 可信工程执行 |
| `MATLAB_WORKER_URL`、`MATLAB_WORKER_TOKEN` | 仅 Worker 路径需要，令牌必须两端一致；本机工程不必配置 |
| `MATLAB_ALLOW_TRUSTED_UPLOADS` | 仅兼容单文件上传执行需要；与工程执行开关独立 |

安装匹配 MATLAB 发行版的 Engine、工具箱和原生依赖是部署工作，不由 Agent 自动完成。
运行环境显示“已配置”或“Engine 导入探测通过”，都不等于已验证许可证或仿真成功。

## 详细输出说明

### 1. 必须分开阅读的四类结果

| 层次 | 字段/表现 | 如何理解 |
|---|---|---|
| 会话状态 | `ConversationView.status` | `planning` 正在规划、`waiting_for_user` 待补充/确认、`running` 执行中、`completed` 已完成、`failed` 失败、`interrupted` 已中断 |
| 任务结果完整性 | `task.execution_status` | v2 结果可为 `completed/partial/failed/interrupted`；缺少请求产出必须披露，结果尚未产生时可为空 |
| 数值有效性 | `observations[].valid/reason` | 即使 MATLAB 运行完，信号、稳态或指标无效也不能据此判定通过 |
| 验收判定 | `evaluation.checks[].status`、`evaluation.status` | 单项 `pass/fail/undetermined`；未请求总体判定时总体状态为空，纯分析没有评价结果 |

**执行成功不等于满足判据。** 有效实测值超出阈值应显示 `fail`，不应变成“执行失败”；
缺失/无效证据应显示无法判定，不应由模型补造数值。
v2 请求总体判定时，任一项无法判定则整体为 `undetermined`；否则有失败项则为 `fail`，
全部通过才为 `pass`。旧 CLI 的演示判据状态及优先级单独见后文，不能混用。

### 2. 页面/API 的通用结果

`GET /api/conversations/{id}` 返回持久化原始记录；
`GET /api/conversations/{id}/view` 返回统一的只读展示结构，包含新旧任务的投影。
字段以 [views.py](src/flight_agent/verification/views.py) 和
[observations.py](src/flight_agent/runtime/observations.py) 为准。

| 结果字段 | 内容 |
|---|---|
| `id/status/project_root/resource_location/output_root` | 会话身份、状态、资源位置和目录；请求输入中的位置字段叫 `location` |
| `messages` | 用户原文、Agent 回复和可见能力缺口；界面整理显示，不覆盖原文 |
| `task`、`archived_tasks` | 当前任务和保留的历史任务；知识回答可能没有工程任务 |
| `pending_decision`、`error` | 当前需要补充、确认、续接或重试的决定，以及实际错误 |
| `task.baseline_version/confirmation` | 基线版本、是否确认、确认时间 |
| `task.requirements/resources/permissions/budget` | 已确认需求、资源指纹、效果权限及预算 |
| `task.nodes/calls/budget_state/stage` | 计划节点、调用记录、轮次/调用消耗和执行阶段 |
| `task.observations` | 指标生产节点、指标 ID、数值或特殊值、单位、有效性、工况、证据路径 |
| `task.findings/tables` | 有证据引用的发现和结构化表格，不限于飞控固定指标 |
| `task.evaluation` | 单项比较值、实测/判据单位、阈值、原因与证据；按需求提供总体状态 |
| `task.gaps` | 缺失能力、输入或产出，限制与建议下一步 |
| `task.artifacts/result_directory/details` | 实际文件清单、成果目录、能力快照、原始能力结果及复用来源 |

有限数值放在 `value`；无穷大等特殊数值通过 `special` 表达，而不是非法 JSON 的 NaN/Infinity。
每个文件描述包含相对路径 `path`、`sha256` 和字节数 `size`，通用产物还可带 `id/node_id`。
页面摘要最多保留 6 位有效数字，完整精度仍在详情和原始结果中。

以下是**观测及单项检查的结构示例，不是完整响应，也不是实测结果**：

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

### 3. 报告与证据文件

通用交付能力按已确认 `delivery_formats` 生成文件，不保证每个任务都有下列所有产物。

| 产物 | 主要内容 |
|---|---|
| `delivery/<call_id>/result.docx` | 目标、执行/计算详情、指标与有效性、判据原文来源、单项/总体评价、发现、表格、缺项及可用 PNG 曲线 |
| `delivery/<call_id>/result.json` | `title`、`requirements`、`evidence`、`execution_status`、`evaluation_requested`、`unmet_requirements` |
| `delivery/<call_id>/delivery-index.json` | 交付输入与依赖输出摘要、产物身份和结果，用于校验/恢复，不是新的计算结果 |
| `agent/analysis-runs/<call_id>.json` | 不可变调用回执：完整调用规格、能力版本、输出摘要、起止时间、文件清单和复用来源 |
| `agent/checkpoints/<call_id>/checkpoint.json` | 有状态能力的原调用检查点；仅在该能力需要时存在 |
| `calls/<call_id>/published/series.json` | 本机工程发布的实际输入/响应时序，用于后续独立指标计算 |
| `calls/<call_id>/published/result.json` | 本机计算的结构化执行结果，不是 `delivery/` 下的汇总交付 JSON |
| `calls/<call_id>/published/step-response.png` | 本机 `stepinfo` 的输入/响应曲线和计算带，保留无效结果的诊断用途 |
| `calls/<call_id>/local-matlab/` | 工程/输入副本、运行元数据、原始回执、`matlab.log` 等，用于追查具体 MATLAB 错误 |

时序 JSON 的 `input`、`response` 各包含 `time/values/time_unit/unit/name`：
时间与数值等长、每条 2～100000 个有限样本、时间严格递增；本机 `stepinfo` 要求时间单位为 `s`。
时序必须来自被确认的直接依赖工件，不能用模型在方案里编写的数组代替。
`raw.mat`、`runtime-metadata.json`、可序列化函数返回值等文件按实际能力与执行情况保存。
诊断适配器及旧 v1 任务可能使用 `diagnosis.docx` 或 `delivery-v<version>/` 等不同命名，
实际下载应以 `artifacts` 清单为准，而不是猜文件名。

### 4. 默认目录与自定义输出

```text
<启动项目根目录>/
`-- runs/
    |-- verification/<conversation_id>/
    |   |-- conversation.json
    |   `-- tasks/<task_id>/             # 未指定输出根目录时的成果目录
    |       |-- agent/
    |       |-- calls/
    |       `-- delivery/
    |-- verification-worker/<run_id>/    # Worker 工程执行记录与 project/ 副本
    |-- web/<job_id>/                    # 兼容上传工作台记录
    `-- worker/<job_id>/                 # Worker 上传执行记录
```

如果指定 `output_root=/absolute/path/to/results`，该任务成果目录改为：

```text
/absolute/path/to/results/flight-agent-<task_id>/
```

会话索引 `conversation.json` 仍保存在默认 `runs/verification/<conversation_id>/`。
同会话的新任务会获得新任务 ID/目录；旧交付保持不变。
两种位置都由 [ConversationStore](src/flight_agent/verification/store.py) 统一核对，
不允许路径穿越、符号链接或与源工程重叠。没有执行到的阶段不会伪造对应文件。
固定算例 CLI 的 `runs/<UTC时间>-<随机ID>/` 与上面目录不同，详见“兼容 CLI 的结果与演示判据”。

### 5. 获取状态和下载

除启动信息外，下表的 API 需要同源访问及 `X-Flight-Token` 页面令牌；
页面/目录 CLI 会自行获取，外部客户端应先读取 `GET /api/bootstrap`，不要把模型 Key 当作页面令牌。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/bootstrap` | 获取本次服务的页面令牌和配置/本机 Engine 探测状态 |
| GET | `/api/resource-locations` | 查询资源位置与可用性 |
| GET | `/api/conversations/capabilities` | 查询实际注册的能力目录 |
| GET | `/api/conversations` | 最近最多 20 个会话的摘要 |
| POST | `/api/conversations`、`/api/conversations/{id}/messages` | 创建或追加需求；HTTP 202 只表示已接收 |
| GET | `/api/conversations/{id}`、`/api/conversations/{id}/view` | 读取持久状态或通用展示结构 |
| POST | `/api/conversations/{id}/confirm` | 确认当前基线并执行 |
| POST | `/api/conversations/{id}/resume` | 续接/对账原调用，不代表重新派发仿真 |
| POST | `/api/conversations/{id}/retry` | 已知失败后的显式重试，仍需版本与可信确认 |
| GET | `/api/conversations/{id}/tasks/{task_id}/artifacts/{name}` | 下载登记文件；`name` 为清单中的完整相对路径，可包含子目录 |

页面下载时核对文件身份/摘要，旧任务的文件也保留下载入口。
只配置模型、看到 HTTP 202、生成报告或 CLI 返回 0，都不能单独作为“工程判据通过”的依据。

## 安装 Python 项目

在项目根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

项目代码要求 Python 3.11+。**运行 Engine 还必须满足所装 MATLAB 发行版支持的
Python 版本要求**，不是任意 Python 3.11+ 都能运行 MATLAB。
如果现有解释器不兼容，应使用对应版本的 Python 重新创建虚拟环境。

安装 Python 项目不会安装 MATLAB，也不会安装 Engine、启动 MATLAB 或请求模型。

## 安装 MATLAB Engine

先在本机安装并激活 MATLAB，再在上面的虚拟环境中安装与该发行版匹配的 Engine。
不要在尚未选择 MATLAB 版本时盲目安装最新的 `matlabengine`。

推荐从已安装 MATLAB 的 Engine 源码目录安装，路径中的发行版需要替换为真实目录：

```bash
python -m pip install "/Applications/MATLAB_Rxxxx.app/extern/engines/python"
```

如果所用发行版的安装说明要求从 PyPI 安装，则按其官方说明选择对应 `matlabengine`
版本，并核对 Python 兼容性及 macOS 动态库配置。
MATLAB Runtime 不能代替这里所需的 MATLAB 安装与许可。
演示函数只用基础 MATLAB；真实程序使用的工具箱需另外安装和许可。

官方资料：

- MATLAB Engine 安装：`https://www.mathworks.com/help/matlab/matlab_external/install-the-matlab-engine-for-python.html`
- Python 兼容性：`https://www.mathworks.com/support/requirements/python-compatibility.html`
- Engine 官方仓库：`https://github.com/mathworks/matlab-engine-for-python`
- LangChain Agent：`https://docs.langchain.com/oss/python/langchain/agents`

## 配置大模型

编辑 `.env` 中四个配置：

```dotenv
LLM_BASE_URL=http://127.0.0.1:8001/v1
LLM_MODEL=your-tool-calling-model
LLM_API_KEY=local-placeholder
LLM_TIMEOUT_SECONDS=60
```

默认 `LLM_PROVIDER=openai-compatible`，沿用下面的 API 配置要求。

**临时使用本机 Copilot：** macOS/Linux 上安装 GitHub Copilot CLI，先在终端执行
`copilot login` 完成登录，再将 `.env` 改为：

```dotenv
LLM_PROVIDER=copilot-cli
LLM_MODEL=gpt-6.1-sol
LLM_TIMEOUT_SECONDS=180
```

模型名必须是当前账户有权使用的名称；也可省略 `LLM_MODEL`，使用 CLI 自己的模型选择。
此模式不使用 `LLM_BASE_URL` 或 `LLM_API_KEY`，无需复制 Copilot 的登录凭据。
修改配置后重启 Web。CLI 必须支持 `--output-format json` 和 `--available-tools`；
本机已用 Copilot CLI 1.0.98 验证。每次规划会新建 CLI 会话，可能比直接 API 调用慢，
并使用当前 Copilot 账户的模型额度；配置检查不会验证登录状态或发起模型请求。

此后端用于统一入口及历史任务的类型化规划与证据解释；
旧 CLI `analyze` 和 `/workbench` 的 Agent 分析仍需 `openai-compatible`，
不受影响的普通 MATLAB 执行无需大模型。
适配器在空临时目录调用 CLI，仅允许加载技能，禁止其文件、命令及网络访问工具，
不把 MATLAB/LLM API 凭据传给子进程；只传现有规划器的脱敏请求、上下文与响应 schema，
不让 CLI 直接执行工程或获得额外执行授权。返回内容必须通过原有类型与计划校验；
未完成响应、超时、传输和执行失败均明确报错，不自动重试。
规划请求使用紧凑 JSON，完全相同且共享后更小的能力输入/输出 Schema 只传一次，
目录通过 `catalog_schema_ref` 引用请求中的 `catalog_schemas`；Schema 内部的 `$defs/$ref`
保持原样。这只是请求编码去重，不删除需求、来源、能力元数据或不可用能力，
也不改变原目录与响应的完整校验、配置的总超时、纠错次数或执行确认门槛。
统一入口的回答/澄清、能力缺口和方案响应使用互斥字段约束；澄清问题和缺失输入写在
回答正文，不附带方案或顶层缺口列表。每项产出须绑定生产节点与产出 ID，无法提供的
需求必须显式登记缺口；指标有效性等附属说明可写在指标或报告描述中。
统一规划响应未通过类型或结构校验时，仅在原模型总超时内反馈脱敏校验位置和错误，
最多请求一次完整格式修正，不转发原始无效载荷、不截断需求，也不自行补值或丢弃需求。
修正后仍需通过相同的结构、来源、资源与能力校验，方案仍待用户确认；修正再次失败、
时间或上下文预算不足时明确停止，不无限循环，也不会自动执行 MATLAB。
Copilot 规划超时会区分轮次、本轮可用时间和原总预算；纠错轮只使用首轮后剩余时间，
不是重新获得一份完整超时预算。即使已收到响应文本，CLI 未在本轮限时内完成仍不采纳。
这项格式修正不适用于工具调用边界违规、未登记能力、来源伪造、网络错误或执行失败，
不改变旧 CLI 与诊断内部规划的原有失败策略。错误页面不展示原始校验载荷。
Copilot 仍可能在其本机日志或会话历史中保留请求与响应；应按资料敏感级别管理这些记录，
真实工程摘要仅应发送到组织批准的模型服务。

使用默认 API 后端时，`LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY` 必须明确配置，
不默认选择外部云服务。
这里的地址和模型名是占位示例；项目不附带或启动模型服务。
`8001` 表示单独部署的模型服务示例端口；不要把本项目的 Web 页面地址
（默认 `127.0.0.1:8000`）或 MATLAB worker 地址误填为模型 API。
无需认证的本地服务也需填写非空占位 Key。云服务使用其真实地址和凭据。
服务必须支持标准 OpenAI Chat Completions 的 `tools` / `tool_calls`，
只有普通文本聊天接口的模型不可用。供应商非标准扩展字段不在首版支持范围内。

**隐私边界：**目录 Agent 向模型发送用户请求和经过限长、路径脱敏的语义上下文，
包括能力目录、工程资源元数据、判据来源、指标和程序判定；资源摘要可包含从源码声明、
模型元数据及工程文档提取的信息。不会把完整工程或原始 MAT/时序文件作为附件交给规划模型，
但不能据此理解为工程信息完全不进入模型上下文。
脱敏不是任意文本的数据防泄漏机制，请勿在请求中粘贴密钥或未经批准的材料。
旧 CLI / 上传后分析会发送输入数值、MATLAB 指标、规则检查结果、检索片段和用户请求。
敏感材料应使用获批的内网/本地模型。代码不自动向 LangSmith 导出 tracing。
`.env` 和 `runs/` 已加入 `.gitignore`；不要提交密钥、敏感输入和运行报告。
本地运行目录也需要按资料敏感级别设置权限及清理策略。

## 兼容 CLI 的运行方式

```bash
flight-agent doctor

flight-agent analyze examples/parameters.json \
  --request "检查所有演示要求，解释升力系数和推重比结果，并引用知识库依据。"
```

未激活虚拟环境时，可直接用 `.venv/bin/flight-agent`。
也可以执行 `python -m flight_agent`。从其他目录运行时显式指定根目录：

```bash
flight-agent analyze examples/parameters.json \
  --project-root /absolute/path/to/flight-agent \
  --matlab-timeout 180 \
  --max-steps 20
```

参数相对路径以 `--project-root` 为基准；绝对参数路径也支持。
`.env` 只从该根目录读取，已有环境变量优先。`doctor` 不启动 MATLAB 或发送模型请求；
它只检查包是否可发现、配置和演示文件，不证明许可或运行时兼容性。

`analyze` 的退出码：0 表示报告生成完成，**不表示指标通过**；1 表示执行或报告失败；
130 表示用户中断。判定请读取 `assessment.json` 的 `status`。
`doctor` 返回 1 表示尚有缺项或配置错误。

## 兼容 CLI 的输入与流程

本节说明 `flight-agent analyze`：它只接受 JSON 参数，调用固定的可信 MATLAB 入口，
不执行用户上传的 `.m` 文件。`directory-task` 目录会话和 `/workbench` 的流程见前面的[使用说明](#使用说明)。
`examples/parameters.json` 演示了明确带单位的字段：

| 字段 | 单位/约束 |
|---|---|
| `schema_version` | 固定 `1.0` |
| `aircraft_type` | 固定 `demo_fixed_wing` |
| `condition` | 固定 `demo_cruise` |
| `mass_kg` | kg，有限正数 |
| `wing_area_m2` | m²，有限正数 |
| `airspeed_mps` | m/s，有限正数 |
| `air_density_kg_m3` | kg/m³，有限正数 |
| `thrust_n` | N，有限非负数 |

未知字段、数值字符串、布尔值、NaN/Infinity 和不合要求的数值会被拒绝。

```text
JSON 校验 → 本次任务绑定
                 ↓
        LangChain 模型/工具循环
          ├─ run_matlab_analysis → 独立 Python 子进程 → MATLAB Engine
          ├─ check_requirements → 全量适用规则的确定性检查
          └─ search_knowledge → 本地 Markdown 词项检索
                 ↓
       完整性/引用来源检查 → 保存报告
```

Agent 可重复检索、选择解释重点，但不能改输入、选任意 MATLAB 函数或改判据。
同次运行重复调用分析工具会复用结果；失败任务不会自动重新提交。
并行工具请求通过会话锁保护。MATLAB 调用及 Agent 图步数均有限制，
模型 HTTP 请求有超时且不自动重试。

MATLAB 子进程只调用可信的 `matlab/analyze_demo.m`，以 JSON 字符串交换输入/输出；
正常退出会执行 `engine.quit()`。超时或 worker 异常退出后，清理本次 worker 及同进程组后代。
这不是操作系统安全沙箱，无法保证清理由 MATLAB 自行脱离该进程组的进程；
异常超时后应检查 MATLAB 是否仍在占用许可，再决定是否重新运行。
系统拒绝发送清理信号或 worker 在有限等待后仍未退出时，原始执行失败/超时原因会保留，
错误和日志同时报告 `MATLAB cleanup incomplete` 及进程组 ID。此时需人工核查相关进程，
不能把该提示当作清理成功；服务不会提权或无限等待。

## 兼容 CLI 的结果与演示判据

本节是保留的演示分析契约；目录 D1 的基线、UNDETERMINED 优先级和 Word 交付见前文及
`docs/d1-agent.md`，两者不混用。

每次 Agent 分析在 `runs/<UTC时间>-<随机ID>/` 保存（Web 与 worker 计算任务目录见前文）：

| 文件 | 内容 |
|---|---|
| `input.json` | 已校验参数 |
| `metadata.json` | 执行状态、输入摘要、代码摘要、模型及规则版本 |
| `rules.json`、`matlab_source.m` | 启动时的规则与 MATLAB 入口快照 |
| `result.json` | MATLAB 返回的结构化指标 |
| `matlab.log` | MATLAB 输出或错误 |
| `assessment.json` | 完整规则检查和覆盖情况 |
| `evidence.json` | 实际检索片段及原文摘要 |
| `messages.json` | 本次 Agent 工具循环记录 |
| `report.md` | 程序判定、独立模型解释、实际证据 |

尚未执行到的阶段不会伪造相应结果文件。失败写明 `failed` 并保存原因；
模型失败不会删除已经保存的 MATLAB 结果或程序判定。
运行记录用于追溯，**不是跨进程崩溃后自动续算或 exactly-once 执行机制**。

规则支持 `<`、`<=`、`>`、`>=`，不自动转换单位、不从 PDF 动态生成阈值、不暗加容差。
机型、工况、模型版本、指标存在性、结果有效性、单位及来源均参与检查。
每项检查的 `value_unit` 保存实测单位，`unit` 保留判据单位，页面和新生成的报告分别展示。
旧判定记录缺少 `value_unit` 时标注“单位未知”，不以判据单位代替；已有报告文件不自动重写。

| 状态 | 含义 |
|---|---|
| `pass` | 全部适用项已评估并通过演示规则 |
| `fail` | 至少一项不满足；仍披露其他未判定项 |
| `undetermined` | 尚有适用项无法判定，且没有已确定的失败项 |
| `not_applicable` | 没有适用要求，不能视为通过 |

报告中的固定表格由程序生成，不由模型决定。模型文字放在“未经工程人员审核”章节。
完整性检查要求已分析、已检查、已检索及引用来源一致；
服务明确返回截断或过滤结束原因时，不会将该回复标记为完成。**它只核对引用来源，不证明
模型文字忠实于原文或不存在错误**，仍需人工复核解释、建议及引用与结论的对应关系。
同一文档的不同检索片段会累积保存在 `evidence.json` 和报告中，完全重复的片段只保留一次；
引用仍使用 `[KB:文档ID]`，`evidence.json` 中同一来源可对应多个片段。

## 扩展与替换真实程序

如果只通过 Web 运行可信程序，保持 `result = 函数名(params)` 或脚本 `params`/`result`
约定即可，不需要修改 Python 固定入口。要让新结果参与 Agent 判定，还必须适配指标和判据。

替换 CLI 的固定算例时，先准备一个可信 MATLAB 适配函数，保持“JSON 输入 → JSON 输出”的边界。
在 `src/flight_agent/matlab_worker.py` 更换固定入口，并同步诊断路径和入口快照逻辑；
不要把函数名或命令字符串开放成模型工具参数。

同时更新：

- `models.py`：真实参数格式、单位与机型/工况约束，必要的仿真状态和有效性字段。
- MATLAB 适配函数：调用已有程序，抽取有明确计算窗口、单位和有效性依据的指标。
- `rules/demo.json`：替换为经工程人员审核的真实规则，更新版本、适用范围及来源。
- `knowledge/`：放入获准使用的 Markdown 知识，保持规则来源文档和章节真实存在。
- 测试：使用人工标注的真实历史案例验证正常、失败、缺项、单位和边界情况。

代码中的 `demo_only` 与固定 demo 机型有意限制适用范围；接入真实模型时需有意识地
修订这些契约与报告声明，不能只改阈值就宣称完成工程验证。
知识检索目前是中文双字词项与英文词项匹配，不是向量语义搜索；
无匹配就返回空结果，不生成替代依据。

## 开发验证

在项目根目录激活虚拟环境并安装开发依赖：

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
python -m ruff check .
python -m mypy src
python -m build
```

默认测试不请求真实模型，也不要求 MATLAB。
测试替身只存在于 `tests/`，生产代码没有模拟成功模式。
安装好 MATLAB 和匹配 Engine 后可显式运行真实集成检查：

```bash
RUN_MATLAB_TESTS=1 python -m pytest tests/test_matlab.py -m matlab -v
RUN_MATLAB_TESTS=1 python -m pytest tests/test_verification_matlab.py -m matlab -v
```

本机通用工程验收（已有兼容环境；不自动安装）：

```bash
RUN_MATLAB_TESTS=1 MATLAB_ALLOW_TRUSTED_PROJECTS=1 \
MATLAB_PYTHON_EXECUTABLE=/absolute/path/to/compatible/python \
/absolute/path/to/compatible/python -m pytest \
  tests/test_local_matlab.py tests/test_local_matlab_integration.py -m matlab -v
```

默认离线测试中的受控规划器/fake Engine 不代表真实 MATLAB；
上述可选集成用测试夹具准备工程，不会让生产 Agent 生成缺失脚本或模型。

Web 与远程 worker 的 HTTP 测试也在默认测试中，Engine 替身仅存在于测试目录。
另可运行真实浏览器检查统一入口、注册扩展、历史任务、上传、错误与下载：

```bash
python -m playwright install chromium --only-shell
RUN_BROWSER_TESTS=1 python -m pytest \
  tests/test_agent_browser.py tests/test_verification_browser.py tests/test_diagnosis_browser.py \
  tests/test_general_browser.py tests/test_matlab_project_browser.py \
  tests/test_agent_layout_browser.py tests/test_agent_column_resize_browser.py \
  tests/test_web_browser.py -v
```

在真正安装 MATLAB 的执行机器上，验证上传脚本和函数的实际运行：

```bash
RUN_MATLAB_TESTS=1 python -m pytest tests/test_uploads.py -m matlab -v
```

目录集成检查包含真实合成 Simulink 工程、HTTP worker、确认/追加分析/改阈值、
跨 Runtime 恢复以及 Word 与证据下载；仅规划模型使用可控测试替身。
需要保留一次集成产物时，可为该测试指定一个新的、专用的 `--basetemp` 目录；
pytest 会清空该指定目录，不要指向工程、已有交付或运行目录根。

Python 单元测试和测试替身不能证明 MATLAB 函数能在其他发行版上运行。
真实模型是否兼容工具调用，必须配置实际服务后完成一次目录基线确认与交付；
CLI 模型链路另用 `analyze` 检查。工程依赖原生模块时，还须确认其与当前 MATLAB、
CPU 架构和操作系统兼容；例如工程需要的 MEX 必须预先准备完成。
服务不会自动接受系统许可、配置编译器或修改工具链来跳过失败。
具体工程的独立参考结果和约定数值容差仍是工程验收前提，不能用合成回归代替。
D2 默认测试验证已知合成数据、逐轮证据反馈、预算和重启恢复；没有真实标注的故障案例，
不能据此声称已验证实际故障识别率、根因准确率或工程可用性。

Python 包内置 Web 静态资源和受控 MATLAB 支持函数，不内置 MATLAB 产品、
用户工程、CLI 的演示入口、知识和规则文件。完整部署仍应保留项目目录，
并使用 `--project-root` 指向它。

## 项目目录结构

以下展开主要源码和运行接口；测试夹具、运行成果、虚拟环境和缓存不逐项列出。
目录名 `verification/` 保留历史命名，其中也包含当前通用 Agent 的主协调、规划和执行链路。

```text
flight16-agent/
|-- src/flight_agent/
|   |-- cli.py                  # doctor / analyze / web / worker / directory-task 命令
|   |-- web.py                  # 目录会话首页与兼容上传工作台
|   |-- worker.py               # 共享执行槽的可信目录 / 上传服务
|   |-- runtime/                # 不依赖具体飞控算例的共享内核
|   |   |-- agent_contracts.py  # v2 节点、基线、调用规格与回执
|   |   |-- requirements.py     # 目标、产出要求、判据来源及能力缺口
|   |   |-- observations.py     # 观测、发现、表格、判据检查、任务结果
|   |   |-- capabilities.py     # 受信任能力注册及 schema 边界
|   |   |-- capability_metadata.py # 版本快照、指标、资源需求和可用性
|   |   |-- resources.py        # 通用资源引用与契约
|   |   |-- contracts.py        # 效果权限、预算、指纹和证据文件
|   |   |-- loop.py             # 有界执行驱动
|   |   |-- budget.py           # 轮次、调用、时间和无进展预算
|   |   |-- evidence.py         # 不可变回执、文件发布与哈希验证
|   |   `-- tasks.py / workflows.py / intents.py # 计划、工作流及需求调度契约
|   |-- verification/           # 通用目录 Agent 与历史适配
|   |   |-- coordinator.py      # EngineeringRuntime，会话与任务协调
|   |   |-- agent_planner.py    # 通用能力规划、结构化响应与来源校验
|   |   |-- agent_runtime.py    # AgentExecutor，依赖执行、复用和对账
|   |   |-- routes.py           # /api/conversations 及请求/确认契约
|   |   |-- views.py            # 新旧任务的统一只读展示
|   |   |-- store.py            # conversation.json 与任务目录管理
|   |   |-- intake.py           # 路径识别、冲突澄清与目录约束
|   |   |-- plugins.py          # 内置能力和三类资源的装配入口
|   |   |-- resources.py        # 资源适配器注册与发现
|   |   |-- matlab_projects.py  # 本机工程清单、元数据发现与快照
|   |   |-- matlab_contracts.py # 已有入口、信号、时序及本机回执 schema
|   |   |-- local_matlab.py     # 专用解释器、子进程、结果校验与恢复
|   |   |-- local_matlab_worker.py # MATLAB Engine 执行子进程
|   |   |-- adapters/
|   |   |   |-- local_matlab.py # matlab_execute_project / matlab_stepinfo
|   |   |   |-- simulink.py     # Worker 时域响应与稳定裕度
|   |   |   |-- diagnostics.py  # 已登记离线诊断
|   |   |   |-- evaluation.py   # evaluate_criteria
|   |   |   `-- delivery.py     # deliver_results
|   |   |-- diagnostic_data.py  # flight-diagnosis.json 与输入快照
|   |   |-- diagnostic_engine.py / diagnostic_signals.py # 离线计算与信号检查
|   |   |-- diagnosis.py / diagnosis_contracts.py / diagnosis_reports.py # 诊断循环与报告
|   |   |-- projects.py / backend.py / worker.py / matlab.py # 历史 Worker 工程链路
|   |   `-- runtime.py / planner.py / compat.py # v1 执行、规划及只读兼容
|   |-- remote.py               # 远程提交、轮询和产物下载
|   |-- jobs.py                 # 单任务调度与持久化状态
|   |-- matlab.py               # CLI 固定算例的子进程适配
|   |-- matlab_worker.py        # CLI MATLAB Engine 调用
|   |-- upload_engine.py        # 上传程序的 Engine 子进程与输出校验
|   |-- agent.py                # LangChain 工具循环与报告完整性检查
|   |-- session.py              # 分析结果、证据和报告的保存
|   |-- models.py               # 飞行器演示参数、指标和判据契约
|   |-- upload_models.py        # 通用上传、任务和输出契约
|   |-- rules.py                # 确定性规则比较
|   |-- knowledge.py            # 本地知识检索
|   |-- config.py               # 模型连接配置
|   |-- copilot.py              # 可选 Copilot CLI 规划传输适配
|   |-- service.py              # HTTP 校验、鉴权和产物访问
|   |-- directories.py          # 网页/本机目录选择，不自动提交任务
|   |-- matlab_support/         # 随 Python 包发布的受控 MATLAB 桥接函数
|   |   |-- flight_agent_run_project.m   # 已有工程执行和信号导出
|   |   |-- flight_agent_local_stepinfo.m # 独立 stepinfo 与曲线
|   |   |-- flight_agent_step_metrics.m # 共享时域指标
|   |   |-- flight_agent_margin_metrics.m # 共享裕度指标
|   |   |-- flight_agent_verify.m       # 历史 Worker 工程分析
|   |   `-- flight_agent_execute_upload.m # 单文件上传执行
|   `-- static/                 # 无需 Node 构建的页面资源
|       |-- agent.html / agent.css / agent.js # 三栏通用 Agent
|       |-- index.html / style.css / app.js   # MATLAB 分析工作台
|       `-- favicon.svg         # 本地 F 站点图标
|-- matlab/                    # CLI 固定演示函数
|-- examples/                  # Web 程序、参数和 diagnosis/synthetic-read-only/ 合成数据
|-- rules/                     # 版本化 JSON 判据
|-- knowledge/                 # Markdown 依据文档
|-- tests/                     # 单元、HTTP、浏览器与可选真实 MATLAB 测试
|-- docs/d1-agent.md            # 目录工程准备、接口、续接与验收边界
|-- docs/d2-agent.md            # 只读诊断数据、证据循环、共享契约与预算边界
|-- .env.example               # 配置模板，不含真实凭据
|-- pyproject.toml             # 依赖、打包和开发工具配置
`-- README.md
```

`.venv/`、`.env`、`runs/`、构建产物和开发缓存不纳入 Git。
不要上传真实参数、私有知识库、模型密钥、worker 令牌或含敏感内容的运行报告。

## 常见问题

| 现象 / 问题 | 处理方式 |
|---|---|
| 本机工程页面能打开，但不能运行 | 检查 `MATLAB_ALLOW_TRUSTED_PROJECTS`、所选 Python/Engine、许可证、工程依赖和当前基线确认；不必为了本机工程另启 Worker |
| Worker 工程不能运行 | 检查 Worker、URL/令牌及实际部署；仅远程隧道场景需要 SSH 隧道，修改配置后重启对应服务 |
| D2 是否需要 MATLAB 或 worker？ | 不需要；配置工具调用模型服务，使用 Agent 主机上的已登记 JSON 数据目录 |
| D2 续接后显示 `unknown_execution` | 在途调用没有可核验回执，不能确认是否执行；保留记录并人工核查，不自动补跑 |
| 页面显示模型“已配置”，但分析失败 | 只表示配置格式完整，不代表端点可达或 Key 有效；确认真实模型服务支持工具调用 |
| MATLAB Engine 导入或启动失败 | 本机检查 `MATLAB_PYTHON_EXECUTABLE` 所选环境；Worker 检查其 Python、Engine、MATLAB 发行版及许可，Runtime 不能替代 |
| 上传工作台计算成功，演示 Agent 显示 `undetermined` | 该工作台的通用 `result` 不一定符合演示飞行器指标契约；可用 `web_metrics.m` 和配套参数验证格式，这不是目录 Agent 的固定输入要求 |
| HTTP 409 / Another task is running | 当前服务已有计算或分析任务，等待完成；不要启动多个实例共享同一运行目录 |
| Web API 令牌失效 / HTTP 403 | 服务重启后刷新页面获取新的页面令牌 |
| D1 网络断开后是否可以直接重试？ | 先用远程任务 ID 核查 worker；客户端超时不代表远程计算已经终止 |
| 可以直接使用 MATLAB Online 吗？ | 当前没有接入；需本机已许可 MATLAB，或显式选择安装 MATLAB 的 Worker |
| 可以直接上传 Simulink `.slx` 吗？ | 上传工作台不支持；已有 `.slx/.mdl` 应通过 `matlab_project` 提供完整工程目录，或使用满足约定的 Worker 工程 |
| 可以直接复用 Copilot 对话吗？ | 不导入已有聊天记录；可配置 `LLM_PROVIDER=copilot-cli` 使用本机 CLI 提供新的规划响应，兼容 CLI/工作台的 Agent 分析仍需 API 后端 |
| 执行已完成却显示 `fail` 或无法判定 | 执行状态、数值有效性和验收结果是不同层次；检查原始指标、单位、适用工况及每项判定原因 |
| 报告在哪里，为什么不在工程目录？ | 默认在会话的 `tasks/<task_id>/`；指定成果根目录时为其下的 `flight-agent-<task_id>/`，不向源工程写入交付 |

## 许可证

本仓库尚未添加 `LICENSE` 文件，开源许可证待维护者指定。
第三方依赖的许可证请参阅对应项目。
