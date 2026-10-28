"use strict";

const $ = (id) => document.getElementById(id);
const API = "/api/conversations";
const statuses = {
  planning: "正在规划", waiting_for_user: "等待确认或补充", running: "执行中",
  completed: "已完成", failed: "失败", interrupted: "已中断",
};
const nodeStatuses = {
  pending: "待执行", running: "运行中", completed: "已完成", failed: "失败", interrupted: "已中断",
};
let token = "";
let current = null;
let pendingView = null;
let selection = 0;
let sending = false;
let timer = null;
let baselineKey = "";
let historyRequest = 0;
let directoryPicker = null;
let directoryRequest = 0;
let nativeDirectoryPickerEnabled = false;
let nativePicker = null;
let locations = [];

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
    method: options.method || "GET", headers,
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
  const generation = selection;
  const record = await api(path, options);
  if (!record || typeof record.id !== "string" || !/^[a-f0-9]{32}$/.test(record.id)
      || (id && record.id !== id)) {
    throw new Error("会话 ID 不匹配，已拒绝更新。");
  }
  if (path.endsWith("/view")) {
    if (generation === selection && pendingView?.id === record.id) pendingView = null;
    return record;
  }
  if (options?.method === "POST" && generation === selection) {
    pendingView = {id: record.id, selection: generation};
    localStorage.setItem("verification-conversation", record.id);
    const submittedMessage = options.body?.message ?? options.body?.analysis_request;
    if (submittedMessage !== undefined
        && $("message").value === submittedMessage) $("message").value = "";
    if (options.body?.evaluation_criteria !== undefined
        && $("evaluation-criteria").value === options.body.evaluation_criteria) {
      $("evaluation-criteria").value = "";
    }
  }
  return conversation(`${API}/${record.id}/view`, record.id);
}

function active() {
  return pendingView !== null || (current && ["planning", "running"].includes(current.status));
}

function resourceLocation() {
  return locations.find((item) => item.id === $("resource-location").value);
}

function renderLocation() {
  const location = resourceLocation();
  $("project-hint").textContent = location
    ? `资源主机：${location.location}；登记方式：${location.display_name}。位置不决定任务类型或验收标准。`
    : "知识问答与显式输入能力不需要目录；使用目录时请先选择已注册资源位置。";
}

function renderProgress(record = null) {
  let step = 0;
  let next = "直接描述需求；知识问答不需要目录，工程任务执行前需确认方案。";
  const decision = record?.pending_decision?.kind;
  if (record?.status === "planning") {
    next = record.task ? "正在处理追问或变更；现有证据保留，新增方案仍需确认。"
      : "正在整理需求、来源和能力范围，尚未获得执行授权。";
  } else if (["clarify", "capability_gap"].includes(decision)) {
    next = "请补充所需输入、标准或能力；不会据此自动执行旧方案。";
  } else if (record?.status === "completed") {
    step = 3;
    next = record.task ? "查看结果与证据；执行完成不等于合格判定通过。"
      : "知识回答已完成；这不是工程执行或验收证据。";
  } else if (record?.task) {
    step = record.task.confirmation.confirmed ? 2 : 1;
    next = step === 1 ? "核对需求、来源、资源、权限和预算，再勾选确认。"
      : decision === "resume" ? "可续接核对原调用，不会重复派发状态未知的执行。"
      : decision === "retry" ? "已知失败分支需要重新授权；不会自动重试。"
      : "按当前已确认方案处理，详见状态与执行记录。";
  }
  $("task-progress").querySelectorAll("li").forEach((item, index) => {
    if (index === step) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
    item.classList.toggle("is-complete", index < step);
  });
  $("next-action").textContent = next;
}

function controls() {
  const busy = Boolean(!token || sending || active() || nativePicker);
  for (const id of ["send", "project-root", "output-root", "resource-location", "message", "evaluation-criteria", "trusted", "resume"]) {
    $(id).disabled = busy;
  }
  for (const id of ["confirm", "retry", "replan"]) {
    $(id).disabled = busy || !$("trusted").checked;
  }
  for (const target of ["project", "output"]) {
    $(`browse-${target}`).disabled = busy || (target === "project" && !resourceLocation());
    $(`browse-${target}`).textContent = nativePicker?.target === target ? "正在选择…" : "浏览…";
  }
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
    if (picker.location) query.set("location", picker.location);
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
  const picker = {target, selection, value: input.value, location: $("resource-location").value};
  nativePicker = picker;
  notice("");
  controls();
  const relevant = () => nativePicker === picker && picker.selection === selection
    && input.value === picker.value && picker.location === $("resource-location").value;
  try {
    const body = {target, path: picker.value};
    if (target === "project") body.location = picker.location;
    const result = await api("/api/directories/select", {method: "POST", body});
    if (!relevant()) return;
    if (!result || (result.path !== null && (
      typeof result.path !== "string" || !result.path.startsWith("/") || result.path.length > 4096
    ))) throw new Error("目录选择器返回了无效路径。");
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
  const location = resourceLocation();
  if (target === "project" && !location) return;
  if (nativeDirectoryPickerEnabled) {
    chooseNativeDirectory(target);
    return;
  }
  closeDirectoryPicker();
  directoryPicker = {
    target, selection, path: null, parent: null,
    location: target === "project" ? location.id : null,
  };
  $("directory-title").textContent = target === "project" ? "选择资源目录" : "选择输出根目录";
  $("directory-host").textContent = target === "project" && location.location === "worker"
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
  wrapper.tabIndex = 0;
  wrapper.setAttribute("role", "region");
  wrapper.setAttribute("aria-label", headers.join("、"));
  const result = element("table");
  const head = element("thead");
  const header = element("tr");
  headers.forEach((value) => {
    const cell = element("th", value);
    cell.scope = "col";
    header.append(cell);
  });
  head.append(header);
  const body = element("tbody");
  rows.forEach((values) => {
    const row = element("tr");
    values.forEach((value) => row.append(element("td",
      typeof value === "object" && value !== null ? JSON.stringify(value) : value ?? "")));
    body.append(row);
  });
  result.append(head, body);
  wrapper.append(result);
  return wrapper;
}

function details(title, value) {
  const result = element("details");
  result.dataset.detailKey = title;
  result.append(element("summary", title), element("pre", JSON.stringify(value, null, 2)));
  return result;
}

function sourceText(criterion) {
  const source = criterion?.source;
  return source ? `${source.kind} / ${source.reference}\n${source.quote}${
    source.sha256 ? `\nSHA-256: ${source.sha256}` : ""}${source.version ? ` / v${source.version}` : ""}`
    : criterion?.source_note || "尚无明确来源";
}

function metricCard(label, value, special, unit, status, target) {
  const card = element("section", undefined, "metric-card");
  const heading = element("div", undefined, "metric-heading");
  const badge = element("span", {
    pass: "通过", fail: "未通过", undetermined: "无法判定", valid: "有效", invalid: "无效",
  }[status] || status, "metric-status");
  badge.dataset.verdict = status;
  heading.append(element("span", label), badge);
  const number = element("div", undefined, "metric-number");
  const displayed = special || (typeof value === "number" && Number.isFinite(value)
    ? Number(value.toPrecision(6)).toString()
    : "不可用");
  const numeric = element("span", displayed, "metric-value");
  numeric.title = special || String(value ?? "不可用");
  number.append(numeric, element("span", unit, "metric-unit"));
  card.append(heading, number, element("p", target, "metric-target"));
  return card;
}

function renderChecks(view, evidence) {
  const criteria = new Map(view.requirements.criteria.map((item) => [item.id, item]));
  view.evaluation.checks.forEach((check) => {
    const observation = view.observations.find((item) =>
      item.node_id === check.metric.node_id && item.metric_id === check.metric.metric_id);
    $("evaluation").append(metricCard(
      observation?.label || check.metric.metric_id, check.value, check.special, check.value_unit || "",
      check.status, `判据 ${check.operator} ${check.threshold} ${check.unit}`,
    ));
  });
  evidence.append(element("h3", "判据、工况与证据"), table(
    ["指标", "工况", "实测值", "判据 / 来源", "状态与原因", "证据"],
    view.evaluation.checks.map((check) => {
      const criterion = criteria.get(check.criterion_id);
      return [
        `${check.metric.node_id || ""} / ${check.metric.metric_id}`,
        JSON.stringify(criterion?.conditions || {}),
        `${check.special || (check.value ?? "不可用")} ${check.value_unit || ""}`,
        `${check.operator} ${check.threshold} ${check.unit}\n${sourceText(criterion)}`,
        `${check.status.toUpperCase()}\n${check.reason}`, check.evidence.join("\n"),
      ];
    }),
  ));
}

function renderEvaluation(view) {
  const hasResults = Boolean(view.evaluation || view.observations.length
    || view.findings.length || view.tables.length);
  $("evaluation-card").hidden = !hasResults && !view.gaps.length;
  $("evaluation").replaceChildren();
  $("evaluation-heading").textContent = view.evaluation
    ? "评价结果" : hasResults ? "分析结果" : "需求缺项";
  if ($("evaluation-card").hidden) return;
  const evidence = element("details");
  evidence.dataset.detailKey = "evaluation-evidence";
  evidence.append(element("summary", "完整指标与判定依据"));
  $("explanation").textContent = "执行、有效性与判定相互独立；证据不足不推断通过。摘要最多 6 位有效数字，完整记录请展开查看。";
  if (!view.evaluation && hasResults) {
    $("evaluation").append(element("p", view.requirements.mode === "analysis"
      ? "本任务未要求合格判定" : "尚无可用评价，详见任务状态与缺项"));
  } else if (view.evaluation) {
    const verdict = element("p", view.evaluation.status
      ? view.evaluation.status.toUpperCase() : "仅判定逐项条件，未要求总体结论", "verdict");
    verdict.dataset.verdict = view.evaluation.status || "";
    $("evaluation").append(verdict);
    renderChecks(view, evidence);
    view.evaluation.warnings.forEach((text) => $("evaluation").append(element("p", text, "hint")));
  }
  if (view.observations.length) {
    if (!view.evaluation) view.observations.forEach((item) => $("evaluation").append(metricCard(
      item.label, item.value, item.special, item.unit, item.valid ? "valid" : "invalid", item.reason,
    )));
    evidence.append(element("h3", "观测与数值有效性"),
      table(["指标", "值", "单位", "工况", "有效性 / 原因", "证据"], view.observations.map((item) => [
      `${item.label} / ${item.node_id}.${item.metric_id}`, item.special || (item.value ?? "不可用"),
      item.unit, JSON.stringify(item.conditions), `${item.valid ? "有效" : "无效"} ${item.reason}`,
      item.evidence.join("\n"),
      ])),
    );
  }
  if (view.evaluation?.checks.length || view.observations.length) {
    evidence.append(element("p", "摘要数值最多显示 6 位有效数字；完整精度、工况及来源保留于上表。", "hint"));
  }
  view.findings.forEach((item) => {
    const finding = element("section");
    finding.dataset.finding = item.id;
    finding.append(element("h3", `${item.id} · ${item.status}`), element("pre", item.text));
    if (item.evidence.length) finding.append(element("p", item.evidence.join("\n"), "evidence-refs"));
    $("evaluation").append(finding);
  });
  view.tables.forEach((item) => {
    const section = element("section");
    section.dataset.table = item.id;
    section.append(element("h3", item.id), table(item.columns, item.rows));
    if (item.evidence.length) section.append(element("p", item.evidence.join("\n"), "evidence-refs"));
    $("evaluation").append(section);
  });
  view.gaps.forEach((gap) => {
    $("evaluation").append(element("p",
      `能力缺口：${gap.requirement_id} / ${gap.missing_capability || ""}\n缺少输入：${
        gap.missing_inputs.join("、")}\n限制：${gap.limitation}\n下一步：${gap.next_step}`, "hint"));
  });
  evidence.append(details("技术记录", view.details));
  $("evaluation").append(evidence);
}

function renderTask(view) {
  const key = `${current.id}/${view.id}/${view.baseline_version}/${current.pending_decision?.kind || ""}`;
  const changedBaseline = key !== baselineKey;
  if (changedBaseline) $("trusted").checked = false;
  baselineKey = key;
  const executeProject = view.permissions.includes("execute_project");
  $("trust-text").textContent = "我已核对需求、资源、能力版本、权限与预算，同意执行本方案";
  $("trust-warning").textContent = executeProject
    ? "工程初始化代码以 Worker 用户权限在隔离副本中执行。只允许你已审查并信任的工程。"
    : "只授权本方案列出的能力与权限；只读分析不执行源工程代码，不沿用其他方案的授权。";
  $("baseline").replaceChildren(
    element("p", `执行方案 v${view.baseline_version} · ${view.confirmation.confirmed ? "已确认" : "尚未确认"} · ${view.title}`),
    element("p", view.requirements.objective),
    element("p", `权限：${view.permissions.join("、")}`),
    element("p", view.budget ? `预算：${view.budget.max_rounds} 轮 / ${
      view.budget.max_calls} 次调用 / ${view.budget.max_seconds}s；连续 ${
      view.budget.max_no_progress} 轮无进展停止。` : "历史记录未提供统一预算。"),
    element("p", `成果目录（Agent 所在机器）：${view.result_directory}`, "hint"),
    table(["资源", "位置", "指纹"], view.resources.map((item) => [
      `${item.id} / ${item.kind}\n${item.locator}`, item.location, item.fingerprint,
    ])),
    table(["节点", "能力 / 版本", "输入 / 依赖 / 资源"], view.nodes.map((node) => [
      node.id, `${node.tool} / ${node.tool_version || "历史记录未提供版本"}`,
      JSON.stringify({inputs: node.inputs, bindings: node.bindings,
        depends_on: node.depends_on, resources: node.resource_bindings}),
    ])),
    table(["指标", "判据", "工况", "来源"], view.requirements.criteria.map((item) => [
      `${item.metric.node_id || ""} / ${item.metric.metric_id}`,
      `${item.operator} ${item.threshold} ${item.unit}`, JSON.stringify(item.conditions),
      sourceText(item),
    ])),
    details("需求与交付要求", view.requirements),
  );
  const decision = current.pending_decision;
  const available = !active() && decision;
  const confirm = available && decision.kind === "confirm_baseline";
  const replan = available && decision.kind === "confirm_replan";
  const retry = available && (decision.kind === "retry"
    || (replan && view.confirmation.recovery_action === "retry"));
  const resume = available && (decision.kind === "resume"
    || (replan && view.confirmation.recovery_action === "resume"));
  if (replan && view.confirmation.recovery_proposal) {
    $("baseline").append(details("恢复建议（尚未采纳或执行）", view.confirmation.recovery_proposal));
  }
  $("decision-controls").hidden = !(confirm || retry || resume || replan);
  const needsDecision = String(!$("decision-controls").hidden);
  if (changedBaseline || $("baseline-details").dataset.decision !== needsDecision) {
    $("baseline-details").open = needsDecision === "true";
    $("baseline-details").dataset.decision = needsDecision;
  }
  $("decision-message").textContent = decision?.message || "";
  for (const [id, show] of [["confirm", confirm], ["retry", retry], ["resume", resume], ["replan", replan]]) {
    $(id).hidden = !show;
  }
  $("trust-label").hidden = !(confirm || retry || replan);
  $("trust-warning").hidden = !(confirm || retry || replan);
  $("stage").textContent = `当前阶段：${view.stage === "completed" ? "已交付" : view.stage}${
    view.execution_status ? ` / 执行：${view.execution_status}` : ""}`;
  $("nodes").replaceChildren(...view.nodes.map((node) => element(
    "li", `${node.tool}：${nodeStatuses[node.status] || node.status}${node.error ? ` — ${node.error}` : ""}`,
  )));
  $("execution-log").textContent = JSON.stringify({stage: view.stage, calls: view.calls}, null, 2);
  renderEvaluation(view);
  renderDelivery(view);
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
  const evidence = element("details");
  evidence.dataset.detailKey = "delivery-evidence";
  const evidenceList = element("div", undefined, "artifact-list");
  tasks.forEach((item) => item.artifacts.forEach((artifact) => {
    const prefix = item.id === task.id ? "" : `历史任务 ${item.id.slice(0, 8)} / `;
    const button = element("button", `${prefix}${artifact.path} (${artifact.size} bytes)\nSHA-256: ${artifact.sha256}`);
    button.type = "button";
    button.addEventListener("click", () => download(conversationId, item.id, artifact, button));
    if (artifact.path.endsWith(".docx")) {
      button.className = "artifact-report";
      button.textContent = `${prefix}${artifact.path.split("/").pop()}\nWord 报告 · ${artifact.size} bytes`;
      button.title = `${prefix}${artifact.path}\nSHA-256: ${artifact.sha256}`;
      evidenceList.append(element("p",
        `${prefix}${artifact.path} (${artifact.size} bytes)\nSHA-256: ${artifact.sha256}`, "artifact-metadata"));
      $("artifacts").append(button);
    } else evidenceList.append(button);
  }));
  if (evidenceList.childElementCount) {
    evidence.append(element("summary", `数据与审计记录（${evidenceList.childElementCount}）`), evidenceList);
    $("artifacts").append(evidence);
  }
}

function messageText(message) {
  if (message.role !== "user") return message.content;
  const sections = message.content.match(
    /^\[工程分析要求\]\n([\s\S]*)\n\[工程分析要求结束\](?:\n\n\[评价指标\]\n([\s\S]*)\n\[评价指标结束\])?$/,
  );
  if (!sections) return message.content;
  return `[工程分析要求]\n${sections[1]}${
    sections[2] === undefined ? "" : `\n\n[评价指标]\n${sections[2]}`}`;
}

function render(record, resetDirectories = false) {
  const previous = current;
  for (const [id, key] of [["project-root", "project_root"], ["output-root", "output_root"], ["resource-location", "resource_location"]]) {
    const input = $(id);
    if (resetDirectories || input.value.trim() === (current?.[key] || "")) input.value = record[key] || "";
  }
  current = record;
  $("conversation-title").textContent = record.task?.title
    || record.messages[0]?.content.slice(0, 100) || "工程对话";
  renderLocation();
  $("conversation-status").textContent = statuses[record.status] || record.status;
  $("conversation-status").dataset.status = record.status;
  $("conversation-status").hidden = false;
  renderProgress(record);
  const sameConversation = previous?.id === record.id;
  if (!sameConversation || JSON.stringify(previous.messages) !== JSON.stringify(record.messages)) {
    const messages = $("messages");
    const scrollTop = messages.scrollTop;
    const readingEarlier = sameConversation && messages.scrollHeight - messages.clientHeight - scrollTop > 48;
    messages.replaceChildren(...record.messages.map((message) => {
      const node = element("div", undefined, "message");
      node.dataset.role = message.role;
      node.append(element("strong", message.role === "user" ? "你" : "Agent"), element("span", messageText(message)));
      (message.gaps || []).forEach((gap) => node.append(element("p", `能力缺口：${
        gap.missing_capability || gap.requirement_id}\n缺少输入：${gap.missing_inputs.join("、")}\n限制：${
        gap.limitation}\n下一步：${gap.next_step}`)));
      return node;
    }));
    messages.scrollTop = readingEarlier ? scrollTop : messages.scrollHeight;
  }
  notice(record.error);
  const task = record.task;
  $("baseline-card").hidden = !task;
  $("execution-card").hidden = !task;
  if (!task) {
    $("evaluation-card").hidden = true;
    $("delivery-card").hidden = true;
    baselineKey = "";
    $("trusted").checked = false;
  } else if (!sameConversation || JSON.stringify(previous.task) !== JSON.stringify(task)
      || JSON.stringify(previous.pending_decision) !== JSON.stringify(record.pending_decision)
      || JSON.stringify(previous.archived_tasks) !== JSON.stringify(record.archived_tasks)
      || previous.status !== record.status) {
    const detailState = new Map();
    const sameTask = sameConversation && previous.task?.id === task.id;
    const focusedDetail = sameTask && document.activeElement?.tagName === "SUMMARY"
      ? document.activeElement.parentElement.dataset.detailKey : null;
    if (sameTask) document.querySelectorAll("#task-panel details[data-detail-key]").forEach((node) => {
      detailState.set(node.dataset.detailKey, node.open);
    });
    renderTask(task);
    document.querySelectorAll("#task-panel details[data-detail-key]").forEach((node) => {
      if (detailState.has(node.dataset.detailKey)) node.open = detailState.get(node.dataset.detailKey);
      if (node.dataset.detailKey === focusedDetail) node.querySelector("summary").focus({preventScroll: true});
    });
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
  const id = pendingView?.id || current.id;
  timer = setTimeout(async () => {
    try {
      const record = await conversation(`${API}/${id}/view`, id);
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
    const button = element("button");
    button.append(element("span", record.title, "history-title"),
      element("small", statuses[record.status] || record.status));
    button.type = "button";
    button.title = record.title;
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
  pendingView = null;
  sending = false;
  baselineKey = "";
  $("trusted").checked = false;
  for (const id of ["message", "evaluation-criteria", "project-root", "output-root", "resource-location"]) $(id).value = "";
  $("resource-location").value = locations.some((item) => item.id === "matlab_project"
    && item.availability.state === "available") ? "matlab_project" : "";
  $("messages").replaceChildren();
  $("conversation-title").textContent = "工程对话";
  $("conversation-status").textContent = "新会话";
  $("conversation-status").hidden = true;
  delete $("conversation-status").dataset.status;
  for (const id of ["baseline-card", "execution-card", "evaluation-card", "delivery-card"]) $(id).hidden = true;
  document.querySelectorAll("#conversation-history button").forEach((button) => button.setAttribute("aria-current", "false"));
  notice("");
  renderLocation();
  renderProgress();
  controls();
}

async function select(id) {
  clearConversation();
  const generation = selection;
  sending = true;
  controls();
  $("conversation-status").textContent = "正在加载";
  $("conversation-status").hidden = false;
  try {
    const record = await conversation(`${API}/${id}/view`, id);
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
  const message = $("message").value;
  if (!message.trim()) return notice("请输入任务需求。");
  const generation = selection;
  const id = current?.id || null;
  const criteria = $("evaluation-criteria").value;
  const body = criteria.trim()
    ? {analysis_request: message, evaluation_criteria: criteria} : {message};
  const location = $("resource-location").value;
  if (location && location !== current?.resource_location) body.location = location;
  const root = $("project-root").value.trim();
  const output = $("output-root").value.trim();
  if (root !== (current?.project_root || "")) body.project_root = root;
  if (output !== (current?.output_root || "")) body.output_root = output;
  sending = true;
  notice("");
  controls();
  try {
    const record = await conversation(id ? `${API}/${id}/messages` : API, id, {method: "POST", body});
    if (generation === selection) {
      localStorage.setItem("verification-conversation", record.id);
      render(record, true);
      poll();
    }
    await history();
  } catch (error) {
    if (generation === selection) {
      notice(pendingView ? `请求已接收，正在恢复展示；不会重复提交。${error.message}` : error.message);
      if (pendingView) poll();
    }
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
  const version = current.task.baseline_version;
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
    if (generation === selection) {
      notice(pendingView ? `操作已接收，正在恢复展示；不会重复提交。${error.message}` : error.message);
      if (pendingView) poll();
    }
  } finally {
    if (generation === selection) {
      sending = false;
      controls();
    }
  }
}

$("composer").addEventListener("submit", send);
$("resource-location").addEventListener("change", () => {
  if (sending || active() || nativePicker) return;
  closeDirectoryPicker();
  $("project-root").value = "";
  $("trusted").checked = false;
  renderLocation();
  controls();
});
$("browse-project").addEventListener("click", () => openDirectoryPicker("project"));
$("browse-output").addEventListener("click", () => openDirectoryPicker("output"));
$("directory-address-form").addEventListener("submit", (event) => {
  event.preventDefault();
  loadDirectory($("directory-address").value.trim());
});
$("directory-address").addEventListener("input", () => resetDirectoryListing("地址已修改，请点击“前往”读取目录。"));
$("directory-up").addEventListener("click", () => { if (directoryPicker?.parent) loadDirectory(directoryPicker.parent); });
$("directory-home").addEventListener("click", () => loadDirectory(""));
$("directory-cancel").addEventListener("click", closeDirectoryPicker);
$("directory-choose").addEventListener("click", chooseDirectory);
$("directory-picker").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeDirectoryPicker();
});
$("trusted").addEventListener("change", controls);
for (const action of ["confirm", "resume", "retry", "replan"]) $(action).addEventListener("click", () => decide(action));
$("new-conversation").addEventListener("click", () => {
  clearConversation();
  localStorage.removeItem("verification-conversation");
});

function initializeColumnResize(compact) {
  const key = "flight-agent-column-widths";
  const defaults = {left: 260, right: 340};
  const minimum = {left: 200, right: 240};
  const centerMinimum = 480;
  const handles = {left: $("left-resizer"), right: $("right-resizer")};
  let preferred = {...defaults};
  let widths = {...defaults};
  let drag = null;
  try {
    const saved = JSON.parse(localStorage.getItem(key));
    if (saved && [saved.left, saved.right].every((value) => Number.isFinite(value) && value > 0)) {
      preferred = {left: saved.left, right: saved.right};
    }
  } catch {
    // Layout preferences must not prevent the task UI from starting.
  }

  function applyWidths() {
    if (compact.matches) return;
    const total = document.documentElement.clientWidth;
    widths.left = Math.round(Math.max(minimum.left, Math.min(
      preferred.left, total - centerMinimum - minimum.right,
    )));
    widths.right = Math.round(Math.max(minimum.right, Math.min(
      preferred.right, total - centerMinimum - widths.left,
    )));
    for (const side of ["left", "right"]) {
      document.body.style.setProperty(`--${side}-column-width`, `${widths[side]}px`);
      const handle = handles[side];
      handle.setAttribute("aria-valuemin", side === "left" ? minimum.left : widths.left + centerMinimum);
      handle.setAttribute("aria-valuemax", side === "left"
        ? total - widths.right - centerMinimum : total - minimum.right);
      handle.setAttribute("aria-valuenow", side === "left" ? widths.left : total - widths.right);
      handle.setAttribute("aria-valuetext", `${side === "left" ? "左" : "右"}栏 ${widths[side]} 像素`);
    }
  }

  function saveWidths() {
    try {
      localStorage.setItem(key, JSON.stringify(preferred));
    } catch {
      // Resizing still works when browser preference storage is unavailable.
    }
  }

  function adjust(side, value) {
    const other = side === "left" ? "right" : "left";
    preferred = {...widths, [side]: Math.max(minimum[side], Math.min(
      Math.round(value), document.documentElement.clientWidth - centerMinimum - widths[other],
    ))};
    applyWidths();
  }

  function finish(cancel = false) {
    if (!drag) return;
    const previous = drag;
    drag = null;
    if (cancel) preferred = previous.preferred;
    document.body.classList.remove("is-resizing-columns");
    if (previous.handle.hasPointerCapture(previous.pointerId)) {
      previous.handle.releasePointerCapture(previous.pointerId);
    }
    applyWidths();
    if (!cancel) saveWidths();
  }

  function reset() {
    finish(true);
    preferred = {...defaults};
    applyWidths();
    saveWidths();
  }

  for (const [side, handle] of Object.entries(handles)) {
    handle.addEventListener("pointerdown", (event) => {
      if (compact.matches || event.button !== 0 || !event.isPrimary || drag) return;
      event.preventDefault();
      drag = {handle, pointerId: event.pointerId, x: event.clientX,
        width: widths[side], preferred: {...preferred}};
      handle.setPointerCapture(event.pointerId);
      handle.focus({preventScroll: true});
      document.body.classList.add("is-resizing-columns");
    });
    handle.addEventListener("pointermove", (event) => {
      if (drag?.handle !== handle || drag.pointerId !== event.pointerId) return;
      adjust(side, drag.width + (side === "left" ? 1 : -1) * (event.clientX - drag.x));
    });
    for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) {
      handle.addEventListener(name, (event) => {
        if (drag?.handle === handle && drag.pointerId === event.pointerId) finish(name !== "pointerup");
      });
    }
    handle.addEventListener("dblclick", reset);
    handle.addEventListener("keydown", (event) => {
      if (compact.matches) return;
      if (event.key === "Escape") {
        event.preventDefault();
        finish(true);
      } else if (event.key === "Enter") {
        event.preventDefault();
        reset();
      } else if (["ArrowLeft", "ArrowRight"].includes(event.key) && !drag) {
        event.preventDefault();
        const delta = (event.key === "ArrowRight" ? 1 : -1) * (event.shiftKey ? 50 : 10);
        adjust(side, widths[side] + (side === "left" ? delta : -delta));
        saveWidths();
      }
    });
  }
  window.addEventListener("resize", () => { finish(true); applyWidths(); });
  window.addEventListener("blur", () => finish(true));
  applyWidths();
}

async function start() {
  const compact = matchMedia("(max-width: 1100px)");
  function layoutPanels() {
    for (const id of ["resource-panel", "task-panel"]) $(id).open = !compact.matches;
  }
  layoutPanels();
  compact.addEventListener("change", layoutPanels);
  initializeColumnResize(compact);
  renderLocation();
  renderProgress();
  try {
    const bootstrap = await api("/api/bootstrap");
    token = bootstrap.token;
    nativeDirectoryPickerEnabled = bootstrap.native_directory_picker === true;
    $("worker-state").textContent = bootstrap.worker_configured
      ? "MATLAB Worker：已配置" : `MATLAB Worker（仅相关能力需要）：${bootstrap.worker_error}`;
    $("model-state").textContent = bootstrap.model_configured ? "LLM：已配置" : `LLM：${bootstrap.model_error}`;
    const localMatlab = bootstrap.local_matlab;
    $("local-matlab-state").textContent = localMatlab?.state === "available"
      ? "本机 MATLAB Engine：导入探测通过；启动与许可证尚未验证。"
      : localMatlab?.state === "unavailable"
        ? `本机 MATLAB Engine：不可用；仍可浏览目录及静态发现。${localMatlab.reason}`
        : "本机 MATLAB Engine：状态未知；当前后端未提供本机工程状态，请重启后端服务以加载新版功能。";
    locations = await api("/api/resource-locations");
    locations.forEach((item) => {
      const option = element("option", `${item.display_name} (${item.location})${
        item.availability.state === "available" ? "" : ` — ${item.availability.reason}`}`);
      option.value = item.id;
      option.disabled = item.availability.state !== "available";
      $("resource-location").append(option);
    });
    const saved = localStorage.getItem("verification-conversation");
    if (saved) await select(saved);
    else clearConversation();
    await history();
  } catch (error) {
    notice(`初始化失败：${error.message}`);
  }
}

start();
