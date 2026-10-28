# Flight Agent

把已有 MATLAB / Simulink 工程、分析要求和评价指标，转化为可确认的执行方案与可追溯的结果。

[快速开始](#快速开始) · [使用示例](#使用示例) · [项目框架](#项目框架) · [输入与输出](#输入与输出) · [详细文档](#详细文档)

## 项目说明

Flight Agent 是一个通用工程分析 Agent，提供 Web 和 CLI 入口，当前内置飞控分析与离线诊断适配器。
它直接使用已有工程的入口和模型，不要求先改写成固定飞机算例。

```text
工程目录 + 分析要求 + 可选评价指标
              ↓
       发现工程、提出方案
              ↓
         用户确认与授权
              ↓
       执行计算、保存证据
              ↓
       按需评价、生成报告
```

大模型负责理解需求、澄清、规划和解释证据；MATLAB/Simulink 负责计算；
Python 负责授权、证据校验、判据比较与报告生成。
**实测数值来自计算后端，是否达标由程序按判据比较。**

## 当前能力

| 使用方式 | 已支持 | 运行位置 |
|---|---|---|
| 本机工程 | 已有 `.m` 入口、Simulink 模型、独立 `stepinfo` | Agent/Web 主机 |
| Worker 工程 | 已准备工程的阶跃响应、开环稳定裕度 | MATLAB Worker |
| 离线诊断 | 已登记 JSON 数据的只读分析 | Agent/Web 主机 |

本机工程无需启动 Worker。知识问答不要求工程目录，也不触发 MATLAB 计算。
大模型可使用 OpenAI 兼容 API，目录工作流也支持本机 Copilot CLI。

**当前边界：**本机尚未注册线性化与 `allmargin` 裕度工具；
Worker 资源与本机资源不能直接互换。Agent 只调用已注册能力，不执行任意生成的 MATLAB 命令，
也不会自动修改模型、补建激励或编译依赖。

## 快速开始

以下以 **本机已有 MATLAB/Simulink 工程**为主线。
需要 Python 3.11+、已许可 MATLAB、匹配的 Engine，以及工程所需工具箱。
Python 版本还须满足 MATLAB 发行版的兼容要求。

### 1. 安装项目

在项目根目录执行，以下命令使用 macOS/Linux shell：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

已有 `.env` 时不要覆盖。MATLAB 和 Engine 需要另行准备，
安装方式见[环境准备](docs/usage.md#准备环境)。

### 2. 配置模型与计算环境

编辑 `.env`，将占位值替换为实际可用的模型服务：

```dotenv
LLM_PROVIDER=openai-compatible
LLM_BASE_URL=http://127.0.0.1:8001/v1
LLM_MODEL=your-tool-calling-model
LLM_API_KEY=local-placeholder
MATLAB_ALLOW_TRUSTED_PROJECTS=1
```

模型服务须支持工具调用。`8001` 是独立模型服务的示例端口，
不是 Flight Agent 的页面或 Worker 地址。

若 Engine 安装在另一套兼容环境，再设置：

```dotenv
MATLAB_PYTHON_EXECUTABLE=/absolute/path/to/compatible/python
```

该解释器也须安装本项目。不设置时使用 Web 当前的 Python。
使用 Copilot CLI 的配置见[大模型配置](docs/usage.md#配置大模型)。

### 3. 启动 Web

```bash
flight-agent web --port 8000
```

打开 `http://127.0.0.1:8000`，选择“本机 MATLAB / Simulink 工程”。
“Engine 可用”仅说明导入探测通过，不等于许可证或仿真已经验证。
修改环境配置或更新后端代码后，需要重启服务。

## 使用示例

### 填写需求

工程必须已有对应模型、初始化、激励和信号。下面的路径与阈值仅用于说明输入方式。

**工程目录**

```text
/absolute/path/to/prepared-project
```

**工程分析要求**

```text
沿用工程已有 Stick 阶跃激励，执行非线性闭环时域仿真；
按工程文档指定的信号调用 stepinfo，交付 Word 和 JSON。
```

**评价指标**

```text
超调量 ≤10%；调节时间 ≤3 s；调节时间采用 5% 误差带。
```

输出根目录可以留空。这里的 10% 和 3 s 是用户标准，不是程序默认阈值；
5% 是指标计算带，不能代替验收标准。

### 确认并执行

1. 发送需求，回答必要的缺项澄清。
2. 核对工程、入口、信号、工况、判据来源及执行方案。
3. 确认当前版本、权限与预算，勾选可信工程后执行。
4. 查看实测指标、有效性与判据结果，下载实际生成的报告。

只想查看响应或指标时，评价指标可以留空；需要合格结论时再提供标准。
同一会话可追加分析或修改阈值，新方案需重新确认，匹配的数值证据可以复用。

### 阅读结果

| 显示 | 含义 |
|---|---|
| 执行完成 | 计算流程结束，不等于指标通过 |
| `pass` | 有效证据满足已确认判据 |
| `fail` | 有效实测值不满足判据 |
| `undetermined` | 证据缺失、无效或无法比较 |

具体状态、完整精度和来源以结果详情为准。纯分析不强制产生总体合格结论或 Word。

## 项目框架

技术栈为 **LangChain + MATLAB Engine for Python + FastAPI**；
前端使用原生 HTML/CSS/JavaScript，无需 Node 构建。

请求从 Web/CLI 进入会话协调与规划层，经注册能力和执行内核调度计算，
最后完成按需评价与交付。

主要代码位于 `src/flight_agent/`：

| 模块 | 职责 |
|---|---|
| `web.py`、`cli.py` | Web 与命令行入口 |
| `runtime/` | 需求契约、能力注册、预算和证据 |
| `verification/` | 会话、规划、执行、资源及计算适配器 |
| `matlab_support/` | 受控 MATLAB 桥接函数 |
| `static/` | 三栏 Agent 页面和兼容上传工作台 |

完整执行链路、模块职责、源码树和扩展方式见[项目架构](docs/architecture.md)。

## 输入与输出

### 输入

核心输入是**工程目录、分析要求、可选评价指标**。
已有入口、信号、单位和计算条件应来自工程文档或明确的用户说明；
缺项时先澄清，不执行代码来猜测。

资源目录属于所选主机，输出根目录始终属于 Agent/Web 主机。
源工程与成果目录不能重叠，计算在工程副本中执行。

### 输出

页面展示执行状态、观测指标、判据结果、能力缺口及证据。
需要文件交付时，按确认的格式生成 JSON、Word 和实际存在的曲线。

默认成果位于：

```text
runs/verification/<conversation_id>/tasks/<task_id>/
```

指定输出根目录后，成果位于其下的 `flight-agent-<task_id>/`。
会话记录仍保存在默认位置，旧任务和交付不会被新需求覆盖。

字段约束、请求示例、报告内容和文件清单见[输入与输出参考](docs/inputs-outputs.md)。

## 详细文档

| 文档 | 适合查什么 |
|---|---|
| [使用指南](docs/usage.md) | 环境配置、本机工程、Worker、诊断、CLI、常见问题 |
| [项目架构](docs/architecture.md) | 系统分层、能力边界、源码、扩展和开发验证 |
| [输入与输出](docs/inputs-outputs.md) | 表单/API、判据、预算、结果字段与成果目录 |

Worker 工程准备见 [D1 技术参考](docs/d1-agent.md)；
离线诊断数据格式见 [D2 技术参考](docs/d2-agent.md)。
单文件上传工作台保留在 `/workbench`，不与工程目录入口混用。

## 开发与验证

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
python -m ruff check .
python -m mypy src
```

默认测试不需要真实 MATLAB，也不调用真实模型。
浏览器与 MATLAB 集成的运行方法见[开发验证](docs/architecture.md#开发与验证)。
仓库自带的是软件演示，不包含真实 F-16 模型或审核过的航空标准。

## 许可证

仓库尚未添加 `LICENSE`；第三方依赖的许可证请参阅对应项目。
