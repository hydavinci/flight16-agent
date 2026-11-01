# 使用指南

[返回项目首页](../README.md) · [项目架构](architecture.md) · [输入与输出](inputs-outputs.md)

第一次使用，按“准备环境 → 配置模型 → 运行本机工程”的顺序阅读即可。
只有一个通用 v2 运行时；远程工程与离线诊断是注册资源，没有 D1/D2 场景选择。

[本机工程](#运行本机工程) · [Worker](#使用-worker) · [离线诊断](#离线诊断) · [目录 CLI](#目录-cli) · [历史清理](#历史清理) · [常见问题](#常见问题)

## 准备环境

| 组件 | 要求 |
|---|---|
| Python 项目 | Python 3.11+ |
| 本机计算 | 已安装并许可 MATLAB，以及与其发行版匹配的 Python 和 MATLAB Engine |
| 模型仿真 | Simulink 及工程实际使用的工具箱 |
| `stepinfo` | Control System Toolbox |
| Worker 裕度分析 | 另需 Simulink Control Design |
| 大模型 | 支持工具调用的 OpenAI 兼容 API，或支持环境认证的本机 Copilot CLI |

**Python 3.11+ 只是本项目的要求，不代表任意该版本以上的 Python 都能运行 Engine。**
请先核对 MATLAB 发行版的 Python 兼容性。Web 与 MATLAB 可以使用不同的虚拟环境。

### 安装 Python 项目

在项目根目录执行，以下命令使用 macOS/Linux shell：

```bash
python3 -m pip install uv==0.12.6
uv sync --extra dev
source .venv/bin/activate
test -e .env || cp .env.example .env
```

开发依赖使用已提交的 uv.lock；CI 使用 `uv sync --locked --extra dev`，
锁文件过期时直接失败，不在 CI 中重新解析依赖。更新依赖时先运行 `uv lock`，
检查 uv.lock 差异并重新运行完整软件验证，再一起提交 pyproject.toml 与 uv.lock。
`uv sync` 默认精确同步，可能移除锁文件之外安装的 MATLAB Engine。
不要在已有 Engine 的环境中直接执行该命令；真实 MATLAB 验收使用单独的、
已许可且 Python 版本匹配的专用环境，按开发验证章节的 `--inexact` 流程运行。
安装 Python 依赖不代表已安装 MATLAB、Engine、工具箱或许可证。

### 开发验证

普通软件验证在工作树自己的 `.venv` 中执行；先按上节同步锁定依赖，再运行：

```bash
uv sync --extra dev
uv run --no-sync python -m pytest -m "not matlab" -q
uv run --no-sync python -m ruff check .
uv run --no-sync python -m mypy src
uv run --no-sync python -m build --no-isolation
```

`--no-sync` 避免验证命令再次改动环境。真实 MATLAB 验收只能在单独的、已许可且
Python 版本匹配的 Engine 专用环境中运行；为保留锁文件之外安装的 Engine，先使用
`uv sync --locked --extra dev --inexact`，不要对该环境执行默认的精确同步。

### 安装 MATLAB Engine

先安装并激活 MATLAB，再在兼容的 Python 环境中安装匹配的 Engine。
不要直接安装一个未经核对的最新版 `matlabengine`。

例如，从已安装 MATLAB 的 Engine 目录安装；请先替换下面的发行版路径：

```bash
python -m pip install "/Applications/MATLAB_Rxxxx.app/extern/engines/python"
```

如果该发行版要求通过 PyPI 安装，请使用其官方文档指定的版本，并核对动态库配置。
MATLAB Runtime 不能替代 MATLAB 安装与许可；工程依赖的工具箱和本平台 MEX 也须预先准备。

官方资料：

- Engine 安装：`https://www.mathworks.com/help/matlab/matlab_external/install-the-matlab-engine-for-python.html`
- Python 兼容性：`https://www.mathworks.com/support/requirements/python-compatibility.html`
- Engine 仓库：`https://github.com/mathworks/matlab-engine-for-python`

## 配置大模型

在项目根目录的 `.env` 中选择一种后端。启动时已有的进程环境变量优先于 `.env`。
修改配置后需要重启 Web，刷新页面不能替代后端重启。

### OpenAI 兼容 API

```dotenv
LLM_PROVIDER=openai-compatible
LLM_BASE_URL=http://127.0.0.1:8001/v1
LLM_MODEL=your-tool-calling-model
LLM_API_KEY=local-placeholder
LLM_TIMEOUT_SECONDS=60
```

地址、模型名和 Key 都是占位示例。请替换为实际部署的服务和账户可用的模型；
无需认证的本地服务也要填写非空 Key。项目不会默认连接外部云服务。

服务必须支持 Chat Completions 的 `tools` / `tool_calls`，普通文本聊天接口不够。
`LLM_BASE_URL` 不是 Flight Agent 页面地址，也不是 Worker 地址。

### 本机 Copilot CLI

macOS/Linux 上安装 GitHub Copilot CLI，并通过进程环境提供
`COPILOT_GITHUB_TOKEN`、`GH_TOKEN` 或 `GITHUB_TOKEN` 之一，再修改 `.env`：

```dotenv
LLM_PROVIDER=copilot-cli
LLM_TIMEOUT_SECONDS=180
```

可按需设置 `LLM_MODEL` 为账户有权使用的模型；不设置时使用 CLI 自己的选择。
若从 API 配置切换，记得检查或移除原来的 `LLM_MODEL` 占位值。
此模式不使用 `LLM_BASE_URL` 和 `LLM_API_KEY`。规划进程不会读取用户现有的
Copilot 配置目录或复制其中的保存登录状态；只有 CLI 支持的上述环境认证会原样传入。
如果只有 `copilot login` 保存的本机登录状态，规划会在启动 CLI 前明确报告认证不可用；
请改用受支持的环境认证，不要复制令牌文件、改写用户设置或降低隔离强度。

CLI 须支持 JSON 输出和工具过滤。每次规划会创建新会话，使用当前账户的模型额度，
在空的临时工作目录和临时 `COPILOT_HOME` 中运行，不导入已有聊天记录、自定义指令或
内置 MCP。文件读取、Shell、写入、URL 和 memory 工具被拒绝；会话同步、自动更新、
客户端遥测和 Bash 环境加载被禁用，日志级别为 `none`，临时日志目标随目录删除。
配置检查不会发起模型请求，也不会证明环境令牌仍然有效。

正常退出和已处理失败都会清理 `flight-agent-copilot-*` 临时目录。进程被 `SIGKILL`
时无法保证清理；遗留目录可能包含敏感的 CLI 临时状态，须由本机管理员直接删除，
不得收集、上传或放入支持包。

此后端支持通用 Agent 的结构化规划与证据解释，不直接执行工程。

全部环境变量见[配置项参考](inputs-outputs.md#环境变量)。

## Copilot planning failures and isolation

Copilot planning uses an empty temporary working directory and temporary
`COPILOT_HOME`. Shell, file reads, writes, URL, and memory tools remain denied.
Builtin MCP initialization and custom instructions are disabled; the temporary
profile prevents unrelated user MCP/plugin configuration from being loaded.
User configuration is never rewritten or copied.

For the locally inspected Copilot CLI 1.0.105, use a supported environment
authentication source: `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, or `GITHUB_TOKEN`.
The adapter forwards authentication only to the child process, not to diagnostics
or files. Do not paste credentials into a request or a support report.

Operational limitation: no supported switch was established that both isolates
all user MCP/plugins/session state and transparently reuses every saved-login
profile. With saved-login-only authentication from `copilot login`, the adapter
fails explicitly before inference; provide a supported environment authentication
source. It does not copy saved credentials, edit settings, or retry with a less
isolated profile. An installed CLI rejecting isolation flags is a
`process_failure`; update the installation deliberately rather than removing
restrictions automatically.

Categories and next actions:

- `authentication`: verify the supported environment authentication and account status.
- `authorization`: verify account entitlement and requested model access.
- `transport`: check DNS, proxy, WebSocket, and network connectivity.
- `timeout`: inspect the remaining shared planning deadline and reduce request scope.
- `invalid_response`: inspect the fixed summary and structured-response compatibility.
- `process_failure`: verify installation, supported flags, and process cleanup.

There is no automatic inference retry. A single existing structural correction
may follow a complete JSON response that fails schema validation, a deterministic
source-selection mismatch, or missing proposed delivery permission. It shares the
original deadline and the same exact 120,000 UTF-8-byte logical-context ceiling,
and cannot grant permission or execute a plan. Invalid JSONL, tool activity,
incomplete responses, and provider failures are not retried. A provider failure
during correction is terminal, so there is no third call.

The authenticated conversation status API exposes durable `planning_telemetry`
after SQLite restart. Share only its allowlisted metrics and optional six-field
failure object, not prompts, raw CLI output, environment variables, credentials,
or source documents.

Temporary state is removed on success and handled failures. A forcibly killed
parent can leave `flight-agent-copilot-*` directories in the operating-system
temporary location. Treat orphaned directories as sensitive, remove them locally
after confirming no associated process is active, and never attach them to a
support bundle. Disabled application diagnostics do not imply any guarantee
about the provider's own server-side retention policy.

## 运行本机工程

### 启动 Web

在 `.env` 中允许执行已核验的工程：

```dotenv
MATLAB_ALLOW_TRUSTED_PROJECTS=1
```

如果 Engine 位于另一套兼容环境，再指定那个环境的 Python：

```dotenv
MATLAB_PYTHON_EXECUTABLE=/absolute/path/to/compatible/python
```

该环境必须已安装本项目和匹配的 Engine；不设置时使用 Web 当前的 Python。
导入探测和实际执行使用同一解释器。

```bash
flight-agent web --port 8000
```

打开 `http://127.0.0.1:8000`，资源位置选择“本机 MATLAB / Simulink 工程”。
这条路径不需要启动 HTTP Worker。

“Engine 可用”只说明导入探测成功，不代表许可证、工具箱、原生依赖或仿真已通过。
部署开关也不能替代每次任务的可信工程确认；工程代码以服务账户权限执行，不是沙箱。

### 说明工程里已有的内容

Agent 静态读取工程文档、源码声明和模型元数据，不会先执行工程来猜入口。
建议在工程 README 中写清主模型、初始化入口、信号、单位和工况，例如：

```text
Existing model: models/limited_loop.slx
Existing initializer: setup/prepare.m
logsout input signal: lever (1)
logsout response signal: stroke (m)
condition: limited
stepinfo settling band 5%
```

这是命名示例，不是附带的用户工程。真实路径、信号和单位必须来自你的工程。
不要求固定布局，也不强制添加登记文件。

入口声明可以使用中英文、Markdown 行内代码和 Windows 相对路径。
候选列表、示例、否定或不确定描述不算确定选择；有真实冲突时仍会澄清。

信号也可以用表格声明。来源须在同节、父节或每行明确给出：

| Signal name | Source | Unit |
|---|---|---|
| lever | logsout | 1 |
| stroke | logsout | m |

表格只证明信号身份，不能单独决定输入/响应角色。来源支持 `logsout`、`workspace`、
`return`；不会从无关章节补猜来源，或用配置常量代替真实时序。
可选 `flight-agent.project.json` 中的历史信号配置表示 `logsout`，不能当作 workspace 选择。

MATLAB-only 工程可声明已有分析函数和返回信号：

```text
Existing analysis entry: analysis/thermal_response.m
return input signal: signals.excitation (V)
return response signal: signals.temperature (K)
condition: bench
```

函数结果当前只读取第一个输出。固定多输出签名可用；普通执行不支持 `varargin`，
线性化节点的已有准备/初始化函数可使用明确 JSON 参数的 `varargin`；`varargout` 不支持。
省略参数仅支持源码开头可静态确认的 `if nargin == N` / `if nargin < N` 默认赋值，
其他写法仍需澄清。Agent 不猜参数、不注入默认值，也不改函数。

### 提交、确认与查看结果

1. 填写工程绝对目录、分析要求和需要时的评价指标；成果目录可以留空。
2. 回答缺项澄清，核对已有模型、初始化、信号、工况和判据来源。
3. 确认当前方案版本、权限和预算，并勾选可信工程。
4. 等待执行，分别查看数值有效性和判据结果，再下载实际生成的报告。

指定外部成果目录时，即使未要求报告，运行证据也会存入该目录，因此待确认方案会申请
`write_delivery` 权限。选择目录不等于授权；确认当前方案后才创建成果目录并执行，
不会因此自动追加 Word/JSON 报告。留空时使用默认成果位置。

只要求分析时，可以不提供阈值或总体合格结论。要求验收时，标准必须有明确来源；
“5% 调节带”是计算参数，不是自动生效的验收阈值。

本机可组合已有入口/模型执行、`stepinfo`、`matlab_linearize` 和 `matlab_allmargin`。
线性化保留已有权威初始化入口；如需先选择工况，在同一节点按来源调用已有准备入口，
再初始化，并用工作区字段断言实际选择。不能将工程默认工况贴成请求工况。
工作点（初始化后 `operpoint` 或明确工作区字段）和反馈符号均须引用原文，
只读取 `getlinio` 已保存且激活的一对 SISO `openinput/openoutput`；不猜开断点、不寻优配平。
`allmargin` 只接受直接依赖发布、哈希验证的线性模型 MAT 和生产者证据，
输出 dB 幅值裕度、deg 相角裕度、全部交越、名义稳定性、有效性和图。
负裕度与名义不稳定照实保留；阈值判断仍由通用评价能力完成，报告仍按需生成。

执行使用工程副本，不向源工程写入成果，也不补建模型、激励、日志或控制器。
同一节点保留函数和回调写入的 base 变量，并通过 `SimulationInput` 临时注入初始化脚本局部变量；
同名时局部变量优先，不修改保存的模型配置。节点之间只传带哈希的明确工件，不共享隐藏工作区。
线性化在独立 Engine 中将初始化脚本局部变量与函数写入的 base 变量合并，
同名脚本变量优先；工作区断言在模型加载前及线性化后复核。

`.prj` 启动和会清空桥接工作区的脚本仍需按实际工程验收。
依赖变化后须重新发现指纹、提出方案并确认，服务不会自动编译 MEX 或修改工具链。

### 追问与恢复

在同一会话中可以增加标准、修改输出、切换工程或追加分析。
这些变化会形成新基线并重新确认；旧任务、回执和报告保持不变。
规格、版本和哈希全部匹配时才复用数值证据，只改阈值不必重跑相同计算。

已知失败可以显式重试。执行状态未知时只能核对原调用 ID，不能自动重发；
关闭浏览器或客户端超时都不代表后台计算已停止。
解释失败原因不会自动把任务变成已完成，也不会清除原来的待办决定。

完整 MATLAB 日志位于调用目录的 `local-matlab/matlab.log`；
错误信息会附带有界日志尾部，帮助保留内层错误。
文件位置和结果状态见[输出参考](inputs-outputs.md#输出状态)。

## 页面操作

桌面为三栏布局：左侧配置与历史，中间对话，右侧方案、结果与下载。窄屏可展开配置栏；
调整窗口不会清空未发送的内容。最近会话默认折叠，之后保留手动开合状态。

拖动分界线可修改列宽，刷新后保留偏好。方向键微调，Shift 加方向键加大步长；
双击或按 Enter 恢复默认，Escape 取消本次拖动。缩窄窗口只临时适配，不覆盖保存的宽度。

指标摘要最多显示 6 位有效数字，完整精度、条件和来源在详情中。
聊天仅在显示时去除结构化输入的结束标记，服务器原文和报告来源不变。

`/` 与 `/workbench` 是同一个通用工作台；没有单文件上传入口。

同一桌面 macOS 可启用系统目录选择器：

```bash
flight-agent web --native-directory-picker --port 8000
```

它只回填路径，不提交或授权。不要用于远程 Worker；即使 SSH 隧道地址是回环地址，
也不代表浏览器、Web、Worker 在同一台桌面。

## 使用 Worker

只有使用 `simulink_project` 注册资源时才需要 Worker。
Worker 可以与 Web 同机，也可以部署在另一台有 MATLAB 的计算机器上。
远程路径不会自动上传工程。

在执行机器安装项目和匹配 Engine，配置：

```dotenv
MATLAB_WORKER_TOKEN=replace-with-at-least-32-random-ASCII-characters
MATLAB_ALLOW_TRUSTED_PROJECTS=1
```

自行生成随机令牌并安全复制到两端，不要直接使用上面的占位值：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
flight-agent worker --port 9001
```

Web 端配置相同令牌和 Worker 地址：

```dotenv
MATLAB_WORKER_URL=http://127.0.0.1:9001
MATLAB_WORKER_TOKEN=the-same-private-token-as-the-worker
```

同机部署无需 SSH，两个终端分别启动 Web 和 Worker 即可。
跨机器部署时，可在 Web 机器建立隧道：

```bash
ssh -N -L 9001:127.0.0.1:9001 USER@MATLAB_HOST
```

也可使用已正确部署的 HTTPS Worker。客户端拒绝非回环明文 HTTP，不跟随重定向，
也不自动重试提交。两项服务均使用单个进程，不要让多个实例、Uvicorn workers 或 reload
共用同一运行目录；每个服务只接受一个正在执行的任务。

选择 Worker 后，工程还须符合适配器约定。本机通用发现与 Worker 发现不是同一套契约：
后者只内置已准备 F-16 布局，其他工程需提供 `flight-agent.project.json`。
阶跃分析沿用初始化后的模型设置；裕度分析读取已有的 `openinput/openoutput` 点、
工作点模式和反馈符号，不通过聊天猜断点或重写仿真参数。
完整准备方式见 [Simulink Worker 参考](simulink-worker.md)。

目录执行有 Windows 进程清理路径，但尚未在 Windows 实机验收。
原生依赖必须匹配执行平台。

## 离线诊断

选择 `diagnostic_data`，填写 Agent/Web 主机上的数据目录。
目录须包含 `flight-diagnosis.json` 和登记的 JSON 文件；它不是任意 `.mat`、CSV 或工程导入器。
此模式需要模型服务，不需要 MATLAB 或 Worker。

可从[合成示例](../examples/diagnosis/synthetic-read-only/flight-diagnosis.json)了解格式。
确认前核对版本、数据指纹、假设、只读能力、参数与预算。
后续分析只能在已确认范围内变化，最多保留 8 个假设，不删除已有 ID 或改写原始基线。

诊断成果包含最终 findings（问题、定位、责任归属、各假设的最终证据状态与引用）、
停止原因和局限；
假设演进与逐轮修订历史保留在内部 checkpoint 中，不等于最终交付内容。
只有已确认请求要求 JSON / Word 时才生成相应报告，单独诊断不会自动追加报告。
支持/反驳/未知是本次证据状态，不是唯一根因证明；
高/中/低优先级表示分析顺序，不是置信度。建议不会自动修改设计或补跑 Simulink。

恢复时有有效回执才复用；在途调用缺少可核验回执会以 `unknown_execution` 停止，
不会自动补跑。格式、能力参数与恢复边界见[离线诊断参考](offline-diagnostics.md)。

## 目录 CLI

`directory-task` 连接已启动的本机 Web，与网页共用会话，不是另一个离线执行器。

```bash
flight-agent directory-task --server http://127.0.0.1:8000 propose \
  --location matlab_project \
  --project-root /absolute/path/to/prepared-project \
  --analysis-request '沿用已有入口与信号进行阶跃分析，调用 stepinfo，交付 Word 和 JSON' \
  --criteria '超调量 <=10%；调节时间 <=3 s；stepinfo settling band 5%'
```

核对方案后，替换真实会话 ID 和当前版本再执行确认：

```bash
flight-agent directory-task confirm CONVERSATION_ID --version 1 --trusted-project
flight-agent directory-task status CONVERSATION_ID
flight-agent directory-task resume CONVERSATION_ID
```

`--server`、`--timeout` 放在子命令前。续聊用 `propose --conversation CONVERSATION_ID`；
单消息形式仍可用 `--request`，不能和 `--analysis-request` 混用。
`--output-root` 指定 Agent 成果根目录，空字符串恢复默认位置。

使用明确注册的 `--location`；不支持 `--scenario`、场景别名或工作流选择。
CLI 自行获取页面令牌，只连接本机 HTTP origin，不跟随重定向或环境代理。
等待超时不会取消服务端任务，请先查询状态，勿重复提交。

## 支持的命令

| 命令 | 用途 |
|---|---|
| `doctor` | 只检查模型配置与 Engine 导入，不启动 MATLAB 或请求模型 |
| `web` | 启动唯一的通用 Agent 工作台 |
| `worker` | 启动受控 Simulink 工程计算服务 |
| `directory-task propose/confirm/status/resume` | 连接本机 Web，共用确认、执行与恢复链路 |
| `cleanup-legacy` | 默认只读预览，显式 `--apply` 才清理正面识别的旧记录 |

不支持旧 `analyze`、上传/demo、工作流目录或恢复提案 `replan` 接口。
目录 CLI 的退出码 0 不等于判据通过；1 表示失败/中断或请求错误，130 表示用户中断。

## 历史清理

运行时将会话状态保存在 `runs/verification/conversations.sqlite3`，不读取或迁移
旧 `conversation.json` 记录（包括 v1 和 v2）。首次切换前会验证并备份旧运行目录；
旧记录与成果不会被自动删除，但不会出现在新的会话列表中。

在停止相关服务并核对备份后，从项目根目录运行：

```bash
flight-agent cleanup-legacy --project-root /absolute/path/to/flight-agent
flight-agent cleanup-legacy --project-root /absolute/path/to/flight-agent --apply
```

第一条是 dry-run，仅报告 `runs/verification/` 下的 `v1/v2/unknown/deleted`，
不修改运行目录；第二条才删除正面识别为 v1 的会话目录。要求该运行目录已存在。
未知/损坏记录、v2、符号链接和外部成果目录不会被删除。
清理先盘点全部记录，保留任何记录引用的源工程，以及保留记录引用的任务资源和成果位置。
清理通过 Python `tempfile` 在系统临时目录创建并移除私有快照，且拒绝把快照放在
实时数据库目录内；当前工作目录无需可写，也可以位于数据库目录之内。快照检查 SQLite
数据库及其当前、归档任务的源和成果所有权，不打开或修改运行目录内的数据库和
sidecar 文件。所有源定位器会逐段跟踪绝对或相对符号链接；链路经过待删目录时会保留
该目录，循环、缺失、非普通文件或遍历期间变化都会令整次删除失败关闭。
启动拒绝损坏或不支持的数据库之前，也会在私有快照上验证。启动在首次可能恢复或写入
SQLite 的操作完成前持有数据库目录的协作式独占锁和已盘点文件描述符，并在按路径打开
SQLite 后、执行第一条 SQL 前再次核对主文件和 sidecar 的身份与内容。
快照或启动期间检测到文件变化时会拒绝操作。
数据库损坏、版本不支持或所有权无法确定时，不删除任何候选目录。
若这些路径与待删目录重叠，则保留该目录；必需存在的源或成果路径无法严格解析、
或跨记录归属证据无法解析时，整次清理不执行删除，须人工处理后重试。
尚未确认且没有已发布证据的任务目录可以不存在。
清理不猜测路径，也不递归清理外部交付目录。
只有 `deleted` 列出的目录实际被删除，不能把全部 `v1` 都当作已删。

SQLite 启动按离线、单所有者契约设计：同一运行目录的维护者必须遵守目录锁，启动期间
不得由非协作进程替换数据库路径。标准库 `sqlite3` 不能从预先打开的文件描述符创建连接，
也不暴露 SQLite VFS/file-control；因此非协作进程仍可在“打开后身份复核”和第一条 SQL
之间再次替换路径。完全消除此剩余威胁需要受控原生 VFS/适配器，而不是 Python 标准库。

## 常见问题

| 现象 | 先检查什么 |
|---|---|
| 页面能打开，本机计算不能运行 | 可信工程开关、所选 Python/Engine、许可证、依赖和当前版本确认 |
| 模型“已配置”，请求仍失败 | 配置检查不验证端点可达、登录、额度或工具调用兼容性 |
| 本机裕度任务提示能力缺口 | 检查工作点/反馈符号来源、已保存激活 SISO 分析点及 Toolbox，不猜测补点 |
| Worker 入口显示可用，任务仍缺资源 | 已配置后端不等于已绑定满足工具要求的工程 |
| 执行完成，但结论为 `fail` | 计算成功与满足判据是两件事，请查看实测值与比较原因 |
| HTTP 409 | 执行槽忙，等待当前任务；不要用多实例绕过 |
| HTTP 403 或页面令牌失效 | 服务重启后刷新页面 |
| 页面显示 MATLAB 状态未知 | 重启后端加载新 API，单纯刷新静态页面不够 |
| 想直接使用 MATLAB Online | 当前没有接入，使用本机 MATLAB 或显式 Worker |

## 数据与使用边界

目录规划会发送用户请求、能力目录、资源摘要、判据来源与必要结果；
摘要可能包含从源码声明、模型元数据和文档提取的信息。
完整工程或原始 MAT/时序文件不会作为附件发给规划模型，但这不代表工程信息完全不进入上下文。

脱敏不是通用防泄漏机制。模型服务须符合资料使用要求，Copilot 本机日志也可能保留请求与响应。
项目不自动向 LangSmith 导出 tracing；`.env`、`runs/` 和真实成果不应提交到 Git。
计算账户、运行目录和保留期限由部署者管理，令牌与工程副本不构成安全沙箱。

自带算例和离线数据是软件演示。仓库不包含真实 F-16 模型或审核过的航空标准；
合成回归、报告生成和数值判据均不能代替具体工程的独立验收。

## Scheduler isolation

Independent conversations can plan concurrently. The default planning limit is
four active jobs plus 32 queued jobs. A conversation admits one operation at a
time; sending another request does not create a second active turn or authorize
a new baseline. A conflict response does not append another user message.

Local MATLAB defaults to **one** slot. `FLIGHT_LOCAL_MATLAB_SLOTS` accepts 1–8.
`FLIGHT_REMOTE_WORKER_SLOTS` defaults to 1 and accepts 1–16.
`FLIGHT_READ_ONLY_SLOTS` defaults to 4 and accepts 1–32. Delivery still requires
its confirmed write permission even though it uses the general/read-only lane.
The server selects lanes from registered effects and scheduling metadata;
requests and model plans cannot select a lane.

`FLIGHT_PLANNING_WORKERS` accepts 1–32; `FLIGHT_PLANNING_QUEUE` accepts 0–256.
`FLIGHT_EXECUTION_TURNS` defaults to 32, accepts 1–64, and must cover the sum of
the three execution-lane limits. It bounds coordination threads, including
threads waiting for a lane. These are per-process limits, not distributed
quotas. Run one Web process per writable state root. A remote limit does not
override the remote Worker's independent capacity.

Queued requests return HTTP 202 and remain visible as `queued`; the CLI and
workbench continue polling. Full admission capacity returns HTTP 429 with
`detail.code=scheduler_capacity` and `Retry-After: 1`, without creating a
conversation or appending a message. Shutdown refuses new work with HTTP 503
and drains admitted work so all thread-owned SQLite contexts have exited. No cancellation
or automatic inference retry is introduced.

After an abrupt restart, queued work is not replayed. A queued confirmation
returns to its original confirmation state. Interrupted follow-up planning
restores the original task state. A confirmed task waiting for a lane can be
explicitly resumed; no dispatched call existed until the permit was acquired.
An already dispatched unknown call retains its identity and budget charge and
cannot be retried until the original operation is reconciled. Existing evidence
and source project directories are not removed by scheduler recovery.

### Snapshot confirmation and cleanup

A changed source fingerprint before confirmation requires rediscovery and a new
confirmation. After confirmation, local operations use the selected immutable
snapshot even if the original source is edited or removed. Worker project and
offline diagnostic resource verification retain their existing behavior.

Historical v2 confirmations and invocation hashes remain readable. A historical
local task without a snapshot binding cannot dispatch new project work; propose
and confirm a new baseline. An unresolved historical call must first reconcile
its original receipt and is never resubmitted automatically.
New invocation receipts keep compact snapshot lineage only; the confirmed
baseline remains the authoritative full manifest. Historical receipts that
already contain a full manifest are verified in place and are not rewritten.

From the application checkout, preview snapshot cleanup for the default store:

    python -c "from pathlib import Path; from flight_agent.verification.store import ConversationStore; print(ConversationStore(Path('runs/verification').resolve()).cleanup_project_snapshots())"

After reviewing the report, delete only verified unreferenced managed objects:

    python -c "from pathlib import Path; from flight_agent.verification.store import ConversationStore; print(ConversationStore(Path('runs/verification').resolve()).cleanup_project_snapshots(dry_run=False))"

Use the actual conversation-store root if the application runs in another state
directory. A database error prevents deletion. Corrupt or ambiguous objects are
reported for manual investigation. Completion alone never releases evidence
retention, and deleting external files is not a request to release their snapshot.
The report may list exact internal `.publish-*` or `.quarantine-*` residue names
in `would_delete`, `deleted`, or `staging`. These entries are reclaimed only when
their name, containment, ownership, manifest identity, and inventory all verify;
retained, malformed, linked, symlinked, reparse, or special entries are skipped.
