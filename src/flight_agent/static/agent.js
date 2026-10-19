"use strict";

const $ = (id) => document.getElementById(id);
const API = "/api/conversations";
const statuses = {
  planning: "正在规划", waiting_for_user: "等待确认或补充",
  running: "执行中", completed: "已完成", failed: "失败", interrupted: "已中断",
};
const tools = {
  analyze_project: "工程分析", evaluate_results: "确定性评价", deliver_report: "报告交付",
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

function controls() {
  const busy = !token || sending || active();
  $("send").disabled = Boolean(busy);
  $("project-root").disabled = Boolean(busy);
  $("output-root").disabled = Boolean(busy);
  $("message").disabled = Boolean(busy);
  $("confirm").disabled = Boolean(busy || !$("trusted").checked);
  $("retry").disabled = Boolean(busy || !$("trusted").checked);
  $("replan").disabled = Boolean(busy || !$("trusted").checked);
  $("resume").disabled = Boolean(busy);
  $("trusted").disabled = Boolean(busy);
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
  $("baseline").replaceChildren(
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
  if (sending || active() || !token) return;
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
  if (!current || sending || active()) return;
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
