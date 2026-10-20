"use strict";

const $ = (id) => document.getElementById(id);
const API = "/api/conversations";
const statuses = {
  planning: "正在规划", waiting_for_user: "等待确认或补充",
  running: "执行中", completed: "已完成", failed: "失败", interrupted: "已中断",
};
const tools = {
  analyze_project: "工程分析", evaluate_results: "确定性评价", deliver_report: "报告交付",
  window_statistics: "窗口统计", compare_signals: "对齐比较",
  parameter_sensitivity: "记录参数敏感性", local_loop: "局部环路分析",
};
const analyses = {step_response: "时域响应", stability_margins: "稳定裕度"};
const metricAnalyses = {
  overshoot: "step_response", settling_time: "step_response",
  gain_margin: "stability_margins", phase_margin: "stability_margins",
};
const nodeStatuses = {
  pending: "待执行", running: "运行中", completed: "已完成", failed: "失败", interrupted: "已中断",
};
let token = "";
let current = null;
let selection = 0;
let sending = false;
let timer = null;
let baselineKey = "";
let historyRequest = 0;
let directoryPicker = null;
let directoryRequest = 0;
let nativeDirectoryPickerEnabled = false;
let nativePicker = null;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function notice(message) {
  $("notice").textContent = message || "";
  $("notice").hidden = !message;
}

async function response(path, options = {}) {
  const headers = {"X-Flight-Token": token};
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const result = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  if (!result.ok) {
    let detail;
    try {
      const data = await result.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data);
    } catch {
      detail = `HTTP ${result.status}`;
    }
    throw new Error(detail);
  }
  return result;
}

async function api(path, options) {
  return (await response(path, options)).json();
}

async function conversation(path, id, options) {
  const record = await api(path, options);
  if (!record || typeof record.id !== "string" || !/^[a-f0-9]{32}$/.test(record.id)
      || (id && record.id !== id)) {
    throw new Error("会话 ID 不匹配，已拒绝更新。");
  }
  return record;
}

function active() {
  return current && ["planning", "running"].includes(current.status);
}

function scenario() {
  return current?.scenario || $("scenario").value;
}

function renderScenario() {
  const diagnosis = scenario() === "d2";
  $("project-label").textContent = diagnosis ? "诊断数据目录" : "工程目录";
  $("request-label").textContent = diagnosis ? "诊断需求" : "验证需求";
  $("project-root").placeholder = diagnosis
    ? "Agent 所在机器上包含 flight-diagnosis.json 的目录"
    : "MATLAB Worker 可见的工程目录，例如 D:\\F16_package";
  $("browse-project").setAttribute("aria-label", diagnosis ? "浏览诊断数据目录" : "浏览工程目录");
  $("project-hint").textContent = diagnosis
    ? "D2 数据位于 Agent 所在机器，不需要 MATLAB Worker。只读取已登记的有界 JSON 数据；不上传或执行源工程。"
    : "路径属于 Worker 所在机器；也可在聊天中写“工程目录：/绝对路径”。路径包含空格时请用引号包围，执行前仍需单独确认可信工程。";
  $("message").placeholder = diagnosis
    ? "说明异常现象、候选原因、需要对比的数据与时间窗；未知项将明确记录。"
    : "说明分析类型、评价阈值和报告分组；也可追问已有结果。";
  $("composer-hint").textContent = diagnosis
    ? "确认只读能力和预算后，每轮根据真实回执更新假设、选择下一步或停止。"
    : "更改阈值或报告分组会复用有效证据；增加分析只运行缺失分支。";
  $("scenario-boundary").textContent = diagnosis
    ? "D2 仅对已登记的时序和频响数据进行只读诊断，记录支持、反驳、未知项与停止原因。不修改设计，不把关联当成因果证明；合成示例不代表真实工程验收。"
    : "D1 仅支持已注册的 Simulink 工程、配平点 1 的时域响应与稳定裕度。不修改源工程、控制律或模型拓扑，不自动配平、调参或推断故障根因。没有确认判据或有效证据时，结论为 UNDETERMINED。";
}

function controls() {
  const busy = !token || sending || active() || nativePicker;
  $("send").disabled = Boolean(busy);
  $("project-root").disabled = Boolean(busy);
  $("output-root").disabled = Boolean(busy);
  $("scenario").disabled = Boolean(busy || current);
  for (const target of ["project", "output"]) {
    $(`browse-${target}`).disabled = Boolean(busy);
    $(`browse-${target}`).textContent = nativePicker?.target === target ? "正在选择…" : "浏览…";
  }
  $("message").disabled = Boolean(busy);
  $("confirm").disabled = Boolean(busy || !$("trusted").checked);
  $("retry").disabled = Boolean(busy || !$("trusted").checked);
  $("replan").disabled = Boolean(busy || !$("trusted").checked);
  $("resume").disabled = Boolean(busy);
  $("trusted").disabled = Boolean(busy);
  if (busy && directoryPicker) closeDirectoryPicker();
}

function directoryControls() {
  $("directory-up").disabled = !directoryPicker?.parent;
  $("directory-choose").disabled = !directoryPicker?.path
    || $("directory-address").value !== directoryPicker.path
    || directoryPicker.selection !== selection || !token || sending || Boolean(active());
}

function closeDirectoryPicker() {
  directoryPicker = null;
  directoryRequest += 1;
  if ($("directory-picker").open) $("directory-picker").close();
}

function resetDirectoryListing(status) {
  if (!directoryPicker) return;
  directoryRequest += 1;
  directoryPicker.path = null;
  directoryPicker.parent = null;
  $("directory-list").replaceChildren();
  $("directory-error").hidden = true;
  $("directory-error").textContent = "";
  $("directory-status").textContent = status;
  directoryControls();
}

async function loadDirectory(path) {
  const picker = directoryPicker;
  if (!picker || picker.selection !== selection || !token || sending || active()) return;
  $("directory-address").value = path;
  resetDirectoryListing("正在读取目录…");
  const request = directoryRequest;
  const relevant = () => picker === directoryPicker && request === directoryRequest
    && picker.selection === selection && $("directory-picker").open;
  try {
    const query = new URLSearchParams({target: picker.target, path});
    if (scenario() === "d2") query.set("scenario", "d2");
    const listing = await api(`/api/directories?${query}`);
    if (!relevant()) return;
    picker.path = listing.path;
    picker.parent = listing.parent;
    $("directory-address").value = listing.path;
    $("directory-list").replaceChildren(...listing.directories.map((directory) => {
      const item = element("li");
      const button = element("button", directory.name);
      button.type = "button";
      button.addEventListener("click", () => loadDirectory(directory.path));
      item.append(button);
      return item;
    }));
    $("directory-status").textContent = listing.truncated
      ? `仅显示前 ${listing.directories.length} 个子目录；可输入完整地址访问其他目录。`
      : listing.directories.length ? `${listing.directories.length} 个子目录` : "此目录没有子目录。";
    directoryControls();
  } catch (error) {
    if (!relevant()) return;
    $("directory-status").textContent = "";
    $("directory-error").textContent = `无法浏览目录：${error.message}`;
    $("directory-error").hidden = false;
  }
}

async function chooseNativeDirectory(target) {
  const input = $(`${target}-root`);
  const picker = {target, selection, value: input.value};
  nativePicker = picker;
  notice("");
  controls();
  const relevant = () => nativePicker === picker && picker.selection === selection
    && input.value === picker.value;
  try {
    const body = {target, path: picker.value};
    if (scenario() === "d2") body.scenario = "d2";
    const result = await api("/api/directories/select", {
      method: "POST", body,
    });
    if (!relevant()) return;
    if (!result || (result.path !== null && (
      typeof result.path !== "string" || !result.path.startsWith("/") || result.path.length > 4096
    ))) {
      throw new Error("目录选择器返回了无效路径。");
    }
    if (result.path !== null) input.value = result.path;
  } catch (error) {
    if (relevant()) notice(`无法选择目录：${error.message}`);
  } finally {
    if (nativePicker === picker) {
      nativePicker = null;
      controls();
    }
  }
}

function openDirectoryPicker(target) {
  if (!token || sending || active() || nativePicker) return;
  if (nativeDirectoryPickerEnabled) {
    chooseNativeDirectory(target);
    return;
  }
  closeDirectoryPicker();
  directoryPicker = {target, selection, path: null, parent: null};
  $("directory-title").textContent = target === "project"
    ? scenario() === "d2" ? "选择诊断数据目录" : "选择工程目录" : "选择输出根目录";
  $("directory-host").textContent = target === "project" && scenario() !== "d2"
    ? "浏览 Worker 所在机器的目录；不会上传浏览器所在机器的文件。"
    : "浏览 Agent 所在机器的目录；不是远程 Worker 的目录。";
  $("directory-picker").showModal();
  loadDirectory($(`${target}-root`).value.trim());
}

function chooseDirectory() {
  directoryControls();
  if ($("directory-choose").disabled || !$("directory-picker").open) return;
  $(`${directoryPicker.target}-root`).value = directoryPicker.path;
  closeDirectoryPicker();
}

function table(headers, rows) {
  const wrapper = element("div", undefined, "table-scroll");
  const result = element("table");
  const head = element("thead");
  const header = element("tr");
  headers.forEach((value) => header.append(element("th", value)));
  head.append(header);
  const body = element("tbody");
  rows.forEach((values) => {
    const row = element("tr");
    values.forEach((value) => row.append(element("td", value)));
    body.append(row);
  });
  result.append(head, body);
  wrapper.append(result);
  return wrapper;
}

function details(title, value) {
  const result = element("details");
  result.append(element("summary", title), element("pre", JSON.stringify(value, null, 2)));
  return result;
}

function renderBaseline(task) {
  const baseline = task.baseline;
  const key = `${current.id}/${task.id}/${baseline.version}/${current.pending_decision?.kind || ""}`;
  if (key !== baselineKey) $("trusted").checked = false;
  baselineKey = key;
  const diagnosis = task.scenario === "d2";
  $("baseline-heading").textContent = diagnosis ? "诊断基线" : "评价基线";
  $("trust-text").textContent = diagnosis
    ? "我已核对数据范围、只读能力与预算，同意按证据进行诊断"
    : "我已核对基线，并确认该工程代码可信";
  $("trust-warning").textContent = diagnosis
    ? "只分析已登记的数据快照，不执行源工程代码、不修改控制器或模型。后续计划不得扩展已确认的范围与预算。"
    : "工程初始化代码以 Worker 用户权限在隔离副本中执行。只允许你已审查并信任的工程。";
  if (diagnosis) {
    $("baseline").replaceChildren(
      element("p", `D2 基线 v${baseline.version} · ${baseline.confirmed ? "已确认" : "尚未确认"}`),
      element("p", baseline.objective),
      element("p", `只读能力：${baseline.allowed_capabilities.join("、")}`),
      element("p", `预算：${baseline.budget.max_rounds} 轮 / ${baseline.budget.max_calls} 次调用 / ${baseline.budget.max_seconds}s；连续 ${baseline.budget.max_no_progress} 轮无新证据即停止。`),
      element("p", `数据指纹：${baseline.project_fingerprint}`, "hint"),
      element("p", `成果目录（Agent 所在机器）：${task.result_directory}`, "hint"),
      table(["假设", "待检验陈述"], baseline.hypotheses.map((item) => [item.id, item.statement])),
      details("设计版本与已登记数据", {design: baseline.design, data: task.project.summary}),
    );
  } else $("baseline").replaceChildren(
    element("p", `基线 v${baseline.version} · 配平点 ${baseline.trim_point} · ${
      baseline.confirmed ? "已确认" : "尚未确认"
    }`),
    element("p", `分析：${baseline.analyses.map((kind) => analyses[kind] || kind).join("、")}；报告：${
      task.proposal.grouping === "separate" ? "分开交付" : "合并交付"
    }`),
    element("p", `工程指纹：${baseline.project_fingerprint}`, "hint"),
    element("p", `成果目录（Agent 所在机器）：${task.result_directory}`, "hint"),
    table(["分析", "指标", "运算符", "阈值", "单位"], baseline.criteria.map((item) => [
      analyses[metricAnalyses[item.metric]] || item.metric,
      item.metric, item.operator, item.threshold, item.unit,
    ])),
    details("工程配置与稳态条件（执行前请核对）", {
      project: task.project.config, steady_state: baseline.steady_state,
    }),
  );
  const decision = current.pending_decision;
  const available = !active() && decision;
  const confirm = available && decision.kind === "confirm_baseline";
  const replan = available && decision.kind === "confirm_replan";
  const retry = available && (decision.kind === "retry" || (replan && task.pending_request));
  const resume = available && (decision.kind === "resume" || (replan && !retry));
  if (task.failure_proposal?.action === "plan") {
    $("baseline").append(details("恢复建议（尚未采纳或执行）", task.failure_proposal));
  }
  $("decision-controls").hidden = !(confirm || retry || resume || replan);
  $("decision-message").textContent = decision ? decision.message : "";
  $("confirm").hidden = !confirm;
  $("retry").hidden = !retry;
  $("resume").hidden = !resume;
  $("replan").hidden = !replan;
  $("trust-label").hidden = !(confirm || retry || replan);
  $("trust-warning").hidden = !(confirm || retry || replan);
}

function renderEvaluation(task) {
  if (task.scenario === "d2") {
    renderDiagnosis(task);
    return;
  }
  $("evaluation-heading").textContent = "评价结果";
  const evaluation = task.evaluation;
  $("evaluation-card").hidden = !evaluation;
  $("evaluation").replaceChildren();
  $("explanation").textContent = "";
  if (!evaluation) return;
  const verdict = element("p", evaluation.status.toUpperCase(), "verdict");
  verdict.dataset.verdict = evaluation.status;
  $("evaluation").append(
    verdict,
    table(["分析 / 指标", "实测值", "判据", "结论", "证据与说明"], evaluation.checks.map((item) => [
      `${analyses[item.analysis] || item.analysis} / ${item.metric}`,
      `${item.special || (item.value === null ? "不可用" : item.value)} ${item.value_unit || ""}`,
      `${item.operator} ${item.threshold} ${item.unit}`,
      item.status.toUpperCase(),
      `${item.reason}\n${item.evidence.join(", ")}`,
    ])),
  );
  evaluation.warnings.forEach((warning) => $("evaluation").append(element("p", warning, "hint")));
  $("explanation").textContent = task.explanation ? task.explanation.text : "";
  if (task.explanation_error) {
    $("explanation").textContent = `LLM 解释未完成：${task.explanation_error}。确定性评价不受影响。`;
  }
}

function renderDiagnosis(task) {
  $("evaluation-heading").textContent = "诊断证据与未知项";
  $("evaluation-card").hidden = !task.analysis_runs.length && !task.termination;
  $("evaluation").replaceChildren();
  $("explanation").textContent = "支持或反驳只针对已记录的数据与假设，不证明唯一物理根因或可放飞。";
  const labels = {supported: "支持", refuted: "反驳", unknown: "未知"};
  $("evaluation").append(
    table(["假设", "证据状态", "理由", "引用"], task.assessments.map((item) => [
      item.hypothesis_id, labels[item.status] || item.status, item.reason,
      item.evidence.map((ref) => `${ref.run_id}${ref.metric ? ` / ${ref.metric}` : ""}`).join("\n"),
    ])),
    element("p", `已记录 ${task.analysis_runs.length} 个独立分析回执、${task.rounds.length} 轮证据评审。`),
  );
  if (task.termination) {
    $("evaluation").append(element("p", `停止：${task.termination.reason} — ${task.termination.detail}`));
    task.termination.unknowns.forEach((value) => {
      $("evaluation").append(element("p", `未知 / 限制：${value}`, "hint"));
    });
  }
  task.analysis_runs.forEach((run) => {
    $("evaluation").append(details(`分析证据 ${run.id}`, {spec: run.spec, output: run.output}));
  });
  $("evaluation").append(details("追加式计划版本与证据评审", {
    plans: task.plan_revisions, rounds: task.rounds,
  }));
}

async function download(conversationId, taskId, artifact, button) {
  const generation = selection;
  const label = button.textContent;
  button.disabled = true;
  button.textContent = `正在下载 ${artifact.path}`;
  try {
    const path = artifact.path.split("/").map(encodeURIComponent).join("/");
    const result = await response(`${API}/${conversationId}/tasks/${taskId}/artifacts/${path}`);
    const url = URL.createObjectURL(await result.blob());
    const link = element("a");
    link.href = url;
    link.download = artifact.path.split("/").pop();
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch (error) {
    if (generation === selection) notice(error.message);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

function renderDelivery(task) {
  const tasks = [task, ...current.archived_tasks];
  $("delivery-card").hidden = !tasks.some((item) => item.artifacts.length);
  $("results-directory").textContent = `成果目录（Agent 所在机器）：${task.result_directory}`;
  $("artifacts").replaceChildren();
  const conversationId = current.id;
  tasks.forEach((item) => {
    item.artifacts.forEach((artifact) => {
      const prefix = item.id === task.id ? "" : `历史任务 ${item.id.slice(0, 8)} / `;
      const button = element("button", `${prefix}${artifact.path} (${artifact.size} bytes)`);
      button.type = "button";
      button.addEventListener("click", () => download(conversationId, item.id, artifact, button));
      $("artifacts").append(button);
    });
  });
}

function render(record, resetDirectories = false) {
  for (const [id, key] of [["project-root", "project_root"], ["output-root", "output_root"]]) {
    const input = $(id);
    if (resetDirectories || input.value.trim() === (current?.[key] || "")) {
      input.value = record[key] || "";
    }
  }
  current = record;
  $("scenario").value = record.scenario || "d1";
  renderScenario();
  $("conversation-status").textContent = statuses[record.status] || record.status;
  $("messages").replaceChildren();
  record.messages.forEach((message) => {
    const node = element("div", undefined, "message");
    node.dataset.role = message.role;
    node.append(element("strong", message.role === "user" ? "你" : "Agent"));
    node.append(element("span", message.content));
    $("messages").append(node);
  });
  $("messages").scrollTop = $("messages").scrollHeight;
  notice(record.error);
  const task = record.task;
  $("baseline-card").hidden = !task;
  $("execution-card").hidden = !task;
  $("evaluation-card").hidden = true;
  $("delivery-card").hidden = true;
  if (task) {
    renderBaseline(task);
    $("stage").textContent = `当前阶段：${task.stage}${
      task.worker_run ? ` / Worker: ${task.worker_run.stage}` : ""
    }`;
    $("nodes").replaceChildren(...task.nodes.map((node) => element(
      "li", `${tools[node.tool] || node.tool}：${nodeStatuses[node.status] || node.status}${
        node.error ? ` — ${node.error}` : ""
      }`,
    )));
    $("execution-log").textContent = [
      task.worker_run ? task.worker_run.log : "",
      JSON.stringify(task.calls, null, 2),
    ].filter(Boolean).join("\n");
    renderEvaluation(task);
    renderDelivery(task);
  }
  document.querySelectorAll("#conversation-history button").forEach((button) => {
    button.setAttribute("aria-current", String(button.dataset.id === record.id));
  });
  controls();
}

function poll() {
  clearTimeout(timer);
  if (!active()) return;
  const generation = selection;
  const id = current.id;
  timer = setTimeout(async () => {
    try {
      const record = await conversation(`${API}/${id}`, id);
      if (generation !== selection) return;
      render(record);
      if (!active()) await history();
    } catch (error) {
      if (generation === selection) notice(`无法更新任务状态：${error.message}`);
    }
    if (generation === selection) poll();
  }, 750);
}

async function history() {
  const request = ++historyRequest;
  const records = await api(API);
  if (request !== historyRequest) return;
  $("conversation-history").replaceChildren(...records.map((record) => {
    const button = element("button", `${record.title} · ${statuses[record.status] || record.status}`);
    button.type = "button";
    button.dataset.id = record.id;
    button.setAttribute("aria-current", String(current && current.id === record.id));
    button.addEventListener("click", () => select(record.id));
    return button;
  }));
}

function clearConversation() {
  selection += 1;
  closeDirectoryPicker();
  nativePicker = null;
  clearTimeout(timer);
  current = null;
  sending = false;
  baselineKey = "";
  $("trusted").checked = false;
  $("message").value = "";
  $("project-root").value = "";
  $("output-root").value = "";
  $("messages").replaceChildren();
  $("conversation-status").textContent = "新会话";
  ["baseline-card", "execution-card", "evaluation-card", "delivery-card"].forEach((id) => {
    $(id).hidden = true;
  });
  document.querySelectorAll("#conversation-history button").forEach((button) => {
    button.setAttribute("aria-current", "false");
  });
  notice("");
  renderScenario();
  controls();
}

async function select(id) {
  clearConversation();
  const generation = selection;
  sending = true;
  controls();
  $("conversation-status").textContent = "正在加载";
  try {
    const record = await conversation(`${API}/${id}`, id);
    if (generation !== selection) return;
    localStorage.setItem("verification-conversation", record.id);
    render(record, true);
    poll();
  } catch (error) {
    if (generation === selection) {
      $("conversation-status").textContent = "加载失败";
      notice(error.message);
    }
  } finally {
    if (generation === selection) {
      sending = false;
      controls();
    }
  }
}

async function send(event) {
  event.preventDefault();
  if (sending || active() || nativePicker || !token) return;
  const message = $("message").value.trim();
  const root = $("project-root").value.trim();
  const output = $("output-root").value.trim();
  if (!message) {
    notice("请输入验证需求。");
    return;
  }
  const generation = selection;
  const id = current ? current.id : null;
  const body = {message};
  if (!id && scenario() === "d2") body.scenario = "d2";
  if (root !== (current?.project_root || "")) body.project_root = root || null;
  if (output !== (current?.output_root || "")) body.output_root = output;
  sending = true;
  notice("");
  controls();
  try {
    const record = await conversation(id ? `${API}/${id}/messages` : API, id, {
      method: "POST", body,
    });
    if (generation === selection) {
      $("message").value = "";
      localStorage.setItem("verification-conversation", record.id);
      render(record, true);
      poll();
    }
    await history();
  } catch (error) {
    if (generation === selection) notice(error.message);
  } finally {
    if (generation === selection) {
      sending = false;
      controls();
    }
  }
}

async function decide(action) {
  if (!current || sending || active() || nativePicker) return;
  if (action !== "resume" && !$("trusted").checked) return;
  const generation = selection;
  const id = current.id;
  const version = current.task.baseline.version;
  sending = true;
  notice("");
  controls();
  try {
    const record = await conversation(`${API}/${id}/${action}`, id, {
      method: "POST", body: action === "resume" ? undefined : {version, trusted: true},
    });
    if (generation === selection) {
      render(record);
      poll();
    }
    await history();
  } catch (error) {
    if (generation === selection) notice(error.message);
  } finally {
    if (generation === selection) {
      sending = false;
      controls();
    }
  }
}

$("composer").addEventListener("submit", send);
$("scenario").addEventListener("change", () => {
  if (current || sending || active() || nativePicker) return;
  closeDirectoryPicker();
  $("project-root").value = "";
  renderScenario();
});
$("browse-project").addEventListener("click", () => openDirectoryPicker("project"));
$("browse-output").addEventListener("click", () => openDirectoryPicker("output"));
$("directory-address-form").addEventListener("submit", (event) => {
  event.preventDefault();
  loadDirectory($("directory-address").value.trim());
});
$("directory-address").addEventListener("input", () => {
  resetDirectoryListing("地址已修改，请点击“前往”读取目录。");
});
$("directory-up").addEventListener("click", () => {
  if (directoryPicker?.parent) loadDirectory(directoryPicker.parent);
});
$("directory-home").addEventListener("click", () => loadDirectory(""));
$("directory-cancel").addEventListener("click", closeDirectoryPicker);
$("directory-choose").addEventListener("click", chooseDirectory);
$("directory-picker").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeDirectoryPicker();
});
$("trusted").addEventListener("change", controls);
$("confirm").addEventListener("click", () => decide("confirm"));
$("resume").addEventListener("click", () => decide("resume"));
$("retry").addEventListener("click", () => decide("retry"));
$("replan").addEventListener("click", () => decide("replan"));
$("new-conversation").addEventListener("click", () => {
  clearConversation();
  localStorage.removeItem("verification-conversation");
});

async function start() {
  try {
    const bootstrap = await api("/api/bootstrap");
    token = bootstrap.token;
    nativeDirectoryPickerEnabled = bootstrap.native_directory_picker === true;
    $("worker-state").textContent = bootstrap.worker_configured
      ? "MATLAB Worker：已配置" : `MATLAB Worker：${bootstrap.worker_error}`;
    $("model-state").textContent = bootstrap.model_configured
      ? "LLM：已配置" : `LLM：${bootstrap.model_error}`;
    controls();
    await history();
    const saved = localStorage.getItem("verification-conversation");
    if (saved) await select(saved);
  } catch (error) {
    notice(`初始化失败：${error.message}`);
  }
}

start();
