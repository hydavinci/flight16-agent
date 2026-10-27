# Flight16 Agent

基于 **LangChain + MATLAB Engine for Python + FastAPI** 的飞控工程 Agent，
提供 Web 和 CLI 入口。核心分工是：**大模型理解请求、提出计划和解释证据，
已注册适配器负责计算，Python 程序负责授权、不可变证据和有来源的判据比较。**

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
> 目录工作流已用真实 MATLAB/Simulink 合成模型跑通，但该集成中的规划模型是测试替身。
> 真实模型服务仍需部署联调；真实 F-16 还需要平台兼容的原生依赖、独立参考结果和约定容差。
> D2 自带数据也是合成夹具；可引用的诊断证据不等于已证明唯一根因，仍需真实标注案例验收。

## 功能与边界

| 能力 | 当前实现 |
|---|---|
| Web 首页 | 统一需求与确认入口；按通用视图显示观测、发现、表格、来源和缺口，无场景或指标名称分支；手机端折叠历史 |
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
| 《飞控设计Agent D1业务场景与功能流程》v0.5 | 目录发现、自然语言规划、基线确认、受控执行、结果追问、版本化交付与失败恢复 | 真实模型服务与代表性工程的端到端业务验收 |
| 《飞控设计Agent D1案例要求》v0.3 | 配平点 1 的时域响应与稳定裕度；5% 调节时间、超调及最差裕度判据与报告链路 | 真实工程的独立人工 MATLAB 参考结果、约定容差及逐项数值对照 |
| 《飞控设计Agent Demo一级Roadmap》 | D1 软件闭环；D2 假设优先级、有证据引用的修订、问题/定位/责任归属/建议和明确停止 | D2 主动补跑 Simulink、真实标注故障案例验收；D3-D6 功能 |

D3-D6 仅预留设计版本、能力效果、候选写入与采纳的共享契约，
尚未实现优化、拓扑编辑或设计候选采纳工作流。

统一入口的需求理解和工程任务规划需要大模型；旧工作台的普通 MATLAB 计算不依赖大模型。
选择本机 `matlab_project` 时，Web 主机须有已许可 MATLAB 和兼容 Engine 解释器，
不必启动 Worker；选择远程 `simulink_project` 时 Web 不需 MATLAB，但 Worker 主机需要。
D2 的已登记 JSON 数据分析只需要 Web/Agent 和模型服务，不要求 MATLAB 或 worker。
项目不附带 MATLAB、模型权重或可直接使用的 API 凭据。

## 整体架构

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
              +-> 离线诊断：Agent 主机上的已登记 JSON 数据
              |       只读分析 -> 引用证据的审查 -> 下一轮或明确停止
              |       -> 诊断 DOCX / JSON / 证据索引
              |
              +-> 按需求追加：确定性评价 / JSON 或 Word 交付
              |
              | 本机 HTTP / SSH 隧道 / 已部署的 HTTPS
              v
MATLAB worker（FastAPI，默认 127.0.0.1:9001）
              |
              v
指纹校验 + 独立工程副本 -> Python 子进程 -> MATLAB / Simulink
              |
              v
实测 JSON / PNG / MAT -> 通用观测与不可变回执 -> 按已确认需求评价与交付

/workbench：可信 .m + JSON -> 原上传执行 / 可选 Agent 分析
CLI analyze：固定 MATLAB 算例 -> 演示规则检查 / 本地知识检索
```

目录 Runtime 保存自己的版本化会话、计划和执行记录，不依赖 LangGraph checkpoint。
旧 CLI 的工具循环底层使用 LangGraph。Web/worker 任务调度由单线程执行池和本地文件存储
完成，没有 Redis、Celery 或业务数据库。每个服务的目录任务和旧上传任务共用执行槽位。

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

## Web 快速开始

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
（参见下面第 1、2 节）。不要使用日常高权限账户；工程初始化、模型回调、原生编译和上传代码
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
Web 端还需按第 3 节配置实际模型服务。两个可信执行开关独立；
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

**仅运行 D2 的最短路径：**完成 A 节的项目安装，按第 3 节配置模型服务后启动 Web。
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

## 1. 安装 Python 项目

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

## 2. 安装 MATLAB Engine

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

## 3. 配置大模型

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
包括已确认判据、指标和程序判定；不读取工程源码给模型，也不发送完整 MAT/时序数据。
脱敏不是任意文本的数据防泄漏机制，请勿在请求中粘贴密钥或未经批准的材料。
旧 CLI / 上传后分析会发送输入数值、MATLAB 指标、规则检查结果、检索片段和用户请求。
敏感材料应使用获批的内网/本地模型。代码不自动向 LangSmith 导出 tracing。
`.env` 和 `runs/` 已加入 `.gitignore`；不要提交密钥、敏感输入和运行报告。
本地运行目录也需要按资料敏感级别设置权限及清理策略。

## 4. 运行

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

## 5. CLI 输入与流程

本节说明 `flight-agent analyze`：它只接受 JSON 参数，调用固定的可信 MATLAB 入口，
不执行用户上传的 `.m` 文件。`directory-task` 目录会话和 `/workbench` 的流程见前面的“Web 快速开始”。
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

## 6. CLI / 上传后分析的结果与演示判据

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

## 7. 替换成真实程序

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

## 8. 开发验证

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
  tests/test_general_browser.py tests/test_web_browser.py -v
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
CLI 模型链路另用 `analyze` 检查。2026-10-17 的本机 F-16 集成停在
`nlplant` 原生模块编译：缺少已配置的受支持 C 编译器，Xcode 许可尚未接受。
该失败不会被合成模型结果替代，也不会由服务自动接受许可或修改系统工具链。
独立 F-16 参考结果和约定数值容差仍是工程验收前提。
D2 默认测试验证已知合成数据、逐轮证据反馈、预算和重启恢复；没有真实标注的故障案例，
不能据此声称已验证实际故障识别率、根因准确率或工程可用性。

Python 包内置 Web 静态资源和受控 MATLAB 支持函数，不内置 MATLAB 产品、
用户工程、CLI 的演示入口、知识和规则文件。完整部署仍应保留项目目录，
并使用 `--project-root` 指向它。

## 9. 项目结构

```text
flight16-agent/
|-- src/flight_agent/
|   |-- cli.py                  # doctor / analyze / web / worker / directory-task 命令
|   |-- web.py                  # 目录会话首页与兼容上传工作台
|   |-- worker.py               # 共享执行槽的可信目录 / 上传服务
|   |-- runtime/               # 共享设计/权限契约、能力注册与不可变证据
|   |-- verification/          # 通用规划/执行/视图、适配器与历史兼容
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
|   |-- service.py              # HTTP 校验、鉴权和产物访问
|   |-- matlab_support/        # 打包的目录分析、指标提取和上传执行函数
|   `-- static/                # 原生 HTML / CSS / JavaScript，无需 Node 构建
|-- matlab/                    # CLI 固定演示函数
|-- examples/                  # Web 程序和参数示例
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

## 10. 常见问题

| 现象 / 问题 | 处理方式 |
|---|---|
| D1 页面能打开，但不能运行 | 检查 worker 是否启动、URL/令牌是否匹配、SSH 隧道是否保持连接；改 `.env` 后重启服务 |
| D2 是否需要 MATLAB 或 worker？ | 不需要；配置工具调用模型服务，使用 Agent 主机上的已登记 JSON 数据目录 |
| D2 续接后显示 `unknown_execution` | 在途调用没有可核验回执，不能确认是否执行；保留记录并人工核查，不自动补跑 |
| 页面显示模型“已配置”，但分析失败 | 只表示配置格式完整，不代表端点可达或 Key 有效；确认真实模型服务支持工具调用 |
| MATLAB Engine 导入或启动失败 | 本机检查 `MATLAB_PYTHON_EXECUTABLE` 所选环境；Worker 检查其 Python、Engine、MATLAB 发行版及许可，Runtime 不能替代 |
| 普通计算成功，Agent 显示 `undetermined` | 通用 `result` 不一定符合现有飞行器指标契约；先使用 `web_metrics.m` 和配套参数验证数据格式 |
| HTTP 409 / Another task is running | 当前服务已有计算或分析任务，等待完成；不要启动多个实例共享同一运行目录 |
| Web API 令牌失效 / HTTP 403 | 服务重启后刷新页面获取新的页面令牌 |
| D1 网络断开后是否可以直接重试？ | 先用远程任务 ID 核查 worker；客户端超时不代表远程计算已经终止 |
| 可以直接使用 MATLAB Online 吗？ | 当前没有接入；需本机已许可 MATLAB，或显式选择安装 MATLAB 的 Worker |
| 可以直接上传 Simulink `.slx` 吗？ | 目前不支持；模型封装、依赖部署和结果适配需另行实现并联调 |
| 可以直接复用 Copilot 对话吗？ | 当前没有 Copilot SDK 适配；应配置受支持的模型 API，而不是把对话地址当作接口 |

## 11. 许可证

本仓库尚未添加 `LICENSE` 文件，开源许可证待维护者指定。
第三方依赖的许可证请参阅对应项目。
