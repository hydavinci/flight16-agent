# Flight16 Agent

基于 **LangChain + MATLAB Engine for Python + FastAPI** 的飞行器参数分析工作台，
提供 Web 和 CLI 两种入口。核心分工是：**MATLAB 负责计算，Python 规则程序负责判定，
大模型负责检索依据和解释结果。**

Web 支持上传可信的单个 `.m` 程序，在 MATLAB worker 上运行，并在页面展示
JSON 结果、日志和 PNG 图表；计算完成后可选择 Agent 分析。
CLI 使用真实的 LangChain `create_agent` 工具循环，调用固定 MATLAB 入口、规则检查和知识检索。
大模型没有任意代码执行工具，Web 上传程序则由用户明确确认可信后执行。

> **当前是软件演示项目，不包含真实 F-16 飞行动力学模型或经审核的航空判据。**
> 自带算例只演示数据、计算、规则和报告链路，不能用于适航、放飞或真实飞行器合格判断。
> 真实 MATLAB/Engine 和模型服务的端到端联合运行尚未完成验证，需要在实际部署环境联调。

## 功能与边界

| 能力 | 当前实现 |
|---|---|
| Web 上传 | 单个 UTF-8 `.m` 脚本或函数 + JSON 参数 |
| 执行后端 | 安装 MATLAB 的本机或远程 worker，通过 MATLAB Engine 执行 |
| Web 输出 | JSON 数据、运行日志、最多 5 张 PNG、结果与报告下载 |
| Agent 分析 | LangChain 工具调用、确定性判据、带知识来源的中文解释 |
| 模型接口 | OpenAI 兼容 Chat Completions，必须支持工具调用 |
| 知识库 | 本地 Markdown 的中文双字词项与英文词项检索，不是向量数据库 |
| 任务记录 | 文件持久化、输入与代码摘要、最近任务；不自动重放中断任务 |
| 暂不提供 | MATLAB Online 接口、`.slx`/`.mlx`/ZIP 上传、多用户登录、安全沙箱、Copilot SDK 接入 |

普通 MATLAB 计算不依赖大模型。Web 所在机器无需安装 MATLAB，
**worker 所在机器仍须安装并许可 MATLAB**；同一台机器可以同时运行 Web 和 worker。
项目不附带 MATLAB、模型权重或可直接使用的 API 凭据。

## 整体架构

```text
浏览器：上传可信 .m + JSON 参数
              |
              v
本地 Web 服务（FastAPI，默认 127.0.0.1:8000）
              |
              | 本机 HTTP / SSH 隧道 / 已部署的 HTTPS
              v
MATLAB worker（FastAPI，默认 127.0.0.1:9001）
              |
              v
独立 Python 子进程 -> MATLAB Engine -> 上传的 .m 程序
              |
              v
JSON + 日志 + PNG -> Web 页面
              |
              | 用户选择“Agent 分析”，复用已有结果
              v
格式与工况校验 -> LangChain Agent
                   |-- check_requirements -> Python 规则判定
                   |-- search_knowledge -> 本地 Markdown
                   `-- 配置的模型 API -> 带来源的解释报告
```

Agent 的工具循环底层使用 LangGraph；Web/worker 任务调度由 Python 单线程执行池和
本地文件存储完成。目前没有 Redis、Celery、业务数据库或 LangGraph checkpoint 恢复机制。

## 环境准备

| 组件 | 要求 |
|---|---|
| Web / Python 项目 | Python 3.11+，首版 worker 支持 macOS/Linux |
| MATLAB worker | 已安装并激活 MATLAB，安装与发行版匹配的 MATLAB Engine Python 包 |
| Python 与 MATLAB | 必须同时满足 MATLAB 发行版的 Python 兼容性要求，不能只看 Python 3.11+ |
| 模型服务（可选） | 支持 `tools` / `tool_calls` 的兼容接口；只有 Agent 分析和 CLI 分析需要 |
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

在浏览器打开 `http://127.0.0.1:8000`。未配置 worker 时也可以查看页面、载入示例，
但运行会明确报错，不会生成模拟结果。模型配置只影响“Agent 分析”，不影响普通 MATLAB 执行。
`.env.example` 中的模型名称、地址和 Key 都是占位内容，不代表已经连接模型服务。
已有 `.env` 时不要再次复制覆盖，应保留自己的配置并按需编辑。

### B. 在有 MATLAB 的执行机器启动 worker

首版支持 macOS/Linux。将整个项目部署到专用计算账户/机器，安装兼容的 Python、
项目依赖和 MATLAB Engine（参见下面第 1、2 节）。不要使用日常高权限账户；
上传代码能读取/修改该账户可访问的文件，也可能访问网络，进程隔离并不防止这些行为。

两台机器的 `.env` 均配置相同的随机令牌。可在你自己的终端生成，再安全复制到两端：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

执行机器的 `.env`：

```dotenv
MATLAB_WORKER_TOKEN=替换为至少32个ASCII字符的随机令牌
MATLAB_ALLOW_TRUSTED_UPLOADS=1
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
将上面的 URL、令牌和 `MATLAB_ALLOW_TRUSTED_UPLOADS=1` 放在同一个项目 `.env` 中，
然后在两个终端分别启动 `flight-agent worker --port 9001` 和 `flight-agent web --port 8000`。
两个终端都需进入项目根目录并激活虚拟环境。

修改配置后重启 Web 服务。也可连接已正确部署的 HTTPS worker；
客户端拒绝非回环地址的明文 HTTP，不跟随重定向，不自动重试任务提交。
不要将 worker 的任意代码执行接口暴露到公网或交给不可信用户。
即使配置访问令牌，也不等于已有多用户身份、权限或安全沙箱。

两项服务都只运行**一个进程**；不要使用多 Uvicorn workers、自动 reload 或多个实例
共享同一 `runs/web` / `runs/worker` 目录。每个服务一次仅接受一个正在执行/分析的任务，
繁忙时返回明确错误，不建立无限队列。

### C. 上传与查看结果

1. 选择单个 UTF-8 `.m` 文件或“载入示例”，选择脚本/函数类型。
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

自带示例：

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
关闭网页不会取消任务；网络中断也不代表远程执行终止。客户端不会自动重复提交，
请根据页面记录的远程 ID 核查 worker 后再重试。
重启服务会把遗留未完成任务标记为 `interrupted`，不会自动重跑。

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

`LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY` 必须明确配置，不默认选择外部云服务。
这里的地址和模型名是占位示例；项目不附带或启动模型服务。
`8001` 表示单独部署的模型服务示例端口；不要把本项目的 Web 页面地址
（默认 `127.0.0.1:8000`）或 MATLAB worker 地址误填为模型 API。
无需认证的本地服务也需填写非空占位 Key。云服务使用其真实地址和凭据。
服务必须支持标准 OpenAI Chat Completions 的 `tools` / `tool_calls`，
只有普通文本聊天接口的模型不可用。供应商非标准扩展字段不在首版支持范围内。

**隐私边界：**输入数值、MATLAB 指标、规则检查结果、检索片段和用户请求会发送给配置的模型端点。
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
不执行用户上传的 `.m` 文件。Web 入口支持上传可信 `.m` 程序，参见前面的“Web 快速开始”。
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

## 6. 结果与判据

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
```

Web 与远程 worker 的 HTTP 测试也在默认测试中，Engine 替身仅存在于测试目录。
另可运行真实浏览器检查上传、结果、图表、错误和 Agent 页面状态：

```bash
python -m playwright install chromium --only-shell
RUN_BROWSER_TESTS=1 python -m pytest tests/test_web_browser.py -v
```

在真正安装 MATLAB 的执行机器上，验证上传脚本和函数的实际运行：

```bash
RUN_MATLAB_TESTS=1 python -m pytest tests/test_uploads.py -m matlab -v
```

Python 单元测试和测试替身不能证明 MATLAB 函数能在你的发行版上运行；
真实模型是否兼容工具调用，也必须配置实际服务后通过 `analyze` 检查。
项目按源码目录方式运行，构建出的 Python 包不内置 MATLAB、知识和规则文件；
部署时保留整个项目目录，并使用 `--project-root` 指向它。

## 9. 项目结构

```text
flight16-agent/
|-- src/flight_agent/
|   |-- cli.py                  # doctor / analyze / web / worker 命令
|   |-- web.py                  # 本地 Web API、结果展示与可选 Agent 分析
|   |-- worker.py               # 可信上传执行服务
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
|   |-- resources/             # 受控 MATLAB 上传执行封装
|   `-- static/                # 原生 HTML / CSS / JavaScript，无需 Node 构建
|-- matlab/                    # CLI 固定演示函数
|-- examples/                  # Web 程序和参数示例
|-- rules/                     # 版本化 JSON 判据
|-- knowledge/                 # Markdown 依据文档
|-- tests/                     # 单元、HTTP、浏览器与可选真实 MATLAB 测试
|-- .env.example               # 配置模板，不含真实凭据
|-- pyproject.toml             # 依赖、打包和开发工具配置
`-- README.md
```

`.venv/`、`.env`、`runs/`、构建产物和开发缓存不纳入 Git。
不要上传真实参数、私有知识库、模型密钥、worker 令牌或含敏感内容的运行报告。

## 10. 常见问题

| 现象 / 问题 | 处理方式 |
|---|---|
| 页面能打开，但不能运行 | 检查 worker 是否启动、URL/令牌是否匹配、SSH 隧道是否保持连接；改 `.env` 后重启服务 |
| 页面显示模型“已配置”，但分析失败 | 只表示配置格式完整，不代表端点可达或 Key 有效；确认真实模型服务支持工具调用 |
| MATLAB Engine 导入或启动失败 | 检查 worker 使用的 Python、Engine 包、MATLAB 发行版及许可，不要把 MATLAB Runtime 当作替代品 |
| 普通计算成功，Agent 显示 `undetermined` | 通用 `result` 不一定符合现有飞行器指标契约；先使用 `web_metrics.m` 和配套参数验证数据格式 |
| HTTP 409 / Another task is running | 当前服务已有计算或分析任务，等待完成；不要启动多个实例共享同一运行目录 |
| Web API 令牌失效 / HTTP 403 | 服务重启后刷新页面获取新的页面令牌 |
| 网络断开后是否可以直接重试？ | 先用远程任务 ID 核查 worker；客户端超时不代表远程计算已经终止 |
| 可以直接使用 MATLAB Online 吗？ | 当前项目没有接入公共 MATLAB Online，执行仍需要安装 MATLAB 的 worker |
| 可以直接上传 Simulink `.slx` 吗？ | 目前不支持；模型封装、依赖部署和结果适配需另行实现并联调 |
| 可以直接复用 Copilot 对话吗？ | 当前没有 Copilot SDK 适配；应配置受支持的模型 API，而不是把对话地址当作接口 |

## 11. 许可证

本仓库尚未添加 `LICENSE` 文件，开源许可证待维护者指定。
第三方依赖的许可证请参阅对应项目。
