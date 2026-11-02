"use strict";

const API = "/api/workbench";
const ACTIVE_STATUSES = new Set(["queued", "running"]);
const TERMINAL_STATUSES = new Set(["completed", "failed", "interrupted"]);
const MATLAB_KEYWORDS = new Set([
  "arguments", "break", "case", "catch", "classdef", "continue", "else", "elseif",
  "end", "enumeration", "events", "for", "function", "global", "if", "methods",
  "otherwise", "parfor", "persistent", "properties", "return", "spmd", "switch",
  "try", "while",
]);
const STATUS_LABELS = {
  queued: "等待执行",
  running: "执行中",
  completed: "执行完成",
  failed: "执行失败",
  interrupted: "执行中断",
};

const elements = {
  workerStatus: document.getElementById("worker-status"),
  notice: document.getElementById("notice"),
  file: document.getElementById("file"),
  fileDetail: document.getElementById("file-detail"),
  kind: document.getElementById("kind"),
  timeout: document.getElementById("timeout"),
  parameters: document.getElementById("parameters"),
  trusted: document.getElementById("trusted"),
  runButton: document.getElementById("run-button"),
  executionStatus: document.getElementById("execution-status"),
  runId: document.getElementById("run-id"),
  matlabVersion: document.getElementById("matlab-version"),
  resultData: document.getElementById("result-data"),
  resultDownload: document.getElementById("result-download"),
  figures: document.getElementById("figures"),
  executionLog: document.getElementById("execution-log"),
  history: document.getElementById("history"),
};

const state = {
  token: null,
  workerConfigured: false,
  submitting: false,
  selectedRunId: null,
  selectedRecord: null,
  selectionVersion: 0,
  pollTimer: null,
  objectUrls: [],
};

function showNotice(message) {
  elements.notice.textContent = message;
  elements.notice.hidden = false;
}

function clearNotice() {
  elements.notice.textContent = "";
  elements.notice.hidden = true;
}

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error);
}

function updateRunButton() {
  elements.runButton.disabled = !state.workerConfigured || state.submitting;
}

function headers() {
  if (typeof state.token !== "string" || state.token.length === 0) {
    throw new Error("页面令牌尚未就绪。");
  }
  return {"X-Flight-Token": state.token};
}

async function responseError(response) {
  let detail = "";
  try {
    const body = await response.json();
    if (body && typeof body.detail === "string") detail = body.detail;
  } catch {
    detail = "";
  }
  return new Error(detail || `请求失败（HTTP ${response.status}）。`);
}

async function getJson(path) {
  const response = await fetch(API + path, {headers: headers()});
  if (!response.ok) throw await responseError(response);
  try {
    return await response.json();
  } catch {
    throw new Error("服务器返回了无效 JSON。");
  }
}

function utf8Size(value) {
  return new TextEncoder().encode(value).byteLength;
}

function validateFiniteJson(value, depth = 0) {
  if (depth > 100) throw new Error("JSON 嵌套过深。");
  if (typeof value === "number" && !Number.isFinite(value)) {
    throw new Error("JSON 中的数字必须是有限值。");
  }
  if (value === null || typeof value === "string" || typeof value === "boolean"
      || typeof value === "number") return;
  if (Array.isArray(value)) {
    for (const item of value) validateFiniteJson(item, depth + 1);
    return;
  }
  if (typeof value === "object") {
    for (const item of Object.values(value)) validateFiniteJson(item, depth + 1);
    return;
  }
  throw new Error("JSON 包含不支持的值。");
}

function validateFilename(name) {
  if (!/^[A-Za-z][A-Za-z0-9_]{0,62}\.m$/.test(name)) {
    throw new Error("文件名必须是简单 MATLAB 标识符并以 .m 结尾。");
  }
  const stem = name.slice(0, -2);
  if (MATLAB_KEYWORDS.has(stem.toLowerCase())) {
    throw new Error("文件名不能使用 MATLAB 关键字。");
  }
  if (stem.toLowerCase().startsWith("flight_agent_")) {
    throw new Error("文件名使用了保留前缀。");
  }
  return stem;
}

async function readSource() {
  if (elements.file.files.length !== 1) throw new Error("请选择一个 MATLAB .m 文件。");
  const file = elements.file.files[0];
  const stem = validateFilename(file.name);
  if (file.size < 1) throw new Error("MATLAB 文件不能为空。");
  if (file.size > 1_000_000) throw new Error("MATLAB 文件不能超过 1 MB。");
  let source;
  try {
    source = new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer());
  } catch {
    throw new Error("MATLAB 文件必须是有效的 UTF-8 文本。");
  }
  if (!source || source.includes("\0")) throw new Error("MATLAB 源码为空或包含 NUL。");
  if (elements.kind.value === "function") {
    const escaped = stem.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const declaration = new RegExp(
      `^\\s*function\\s+(?:\\[[^\\]]+\\]|[A-Za-z][A-Za-z0-9_]*)`
      + `\\s*=\\s*${escaped}\\s*\\(\\s*params\\s*\\)`,
      "m",
    );
    if (!declaration.test(source)) {
      throw new Error("函数文件必须声明与文件名一致、仅接收 params 的函数。");
    }
  }
  return {file, source};
}

function readParameters() {
  let value;
  try {
    value = JSON.parse(elements.parameters.value);
  } catch {
    throw new Error("参数必须是有效的 JSON 对象。");
  }
  if (value === null || Array.isArray(value) || typeof value !== "object") {
    throw new Error("参数 JSON 顶层必须是对象。");
  }
  validateFiniteJson(value);
  const serialized = JSON.stringify(value);
  if (utf8Size(serialized) > 100_000) throw new Error("参数 JSON 不能超过 100 KB。");
  return value;
}

function readTimeout() {
  const raw = elements.timeout.value;
  const value = Number(raw);
  if (raw === "" || !Number.isInteger(value) || value < 1 || value > 600) {
    throw new Error("超时必须是 1 到 600 之间的整数秒。");
  }
  return value;
}

function newRunId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

function strictString(value, label) {
  if (typeof value !== "string") throw new Error(`${label} 字段无效。`);
  return value;
}

function validateRecord(value) {
  if (!value || Array.isArray(value) || typeof value !== "object") {
    throw new Error("运行记录格式无效。");
  }
  const id = strictString(value.id, "运行 ID");
  if (!/^[0-9a-f]{32}$/.test(id)) throw new Error("运行 ID 格式无效。");
  if (!/^[0-9a-f]{64}$/.test(strictString(value.request_sha256, "请求摘要"))) {
    throw new Error("请求摘要格式无效。");
  }
  const status = strictString(value.status, "状态");
  if (!ACTIVE_STATUSES.has(status) && !TERMINAL_STATUSES.has(status)) {
    throw new Error("运行状态无效。");
  }
  strictString(value.stage, "阶段");
  strictString(value.created_at, "创建时间");
  strictString(value.updated_at, "更新时间");
  validateFilename(strictString(value.filename, "文件名"));
  if (value.kind !== "script" && value.kind !== "function") {
    throw new Error("执行类型无效。");
  }
  if (typeof value.log !== "string" || utf8Size(value.log) > 1_000_000) {
    throw new Error("执行日志格式或大小无效。");
  }
  if (!Array.isArray(value.artifacts) || value.artifacts.length > 5) {
    throw new Error("图表清单无效。");
  }
  const paths = new Set();
  for (const artifact of value.artifacts) {
    if (!artifact || typeof artifact !== "object"
        || !/^figure-[1-5]\.png$/.test(artifact.path)
        || !/^[0-9a-f]{64}$/.test(artifact.sha256)
        || !Number.isInteger(artifact.size) || artifact.size < 0 || artifact.size > 2_000_000
        || artifact.media_type !== "image/png" || artifact.role !== "figure"
        || paths.has(artifact.path)) {
      throw new Error("图表清单字段无效。");
    }
    paths.add(artifact.path);
  }
  if (status === "completed") {
    if (value.stage !== "completed" || typeof value.matlab_version !== "string"
        || value.matlab_version.length === 0 || value.error !== null) {
      throw new Error("完成记录缺少严格的成功字段。");
    }
    validateFiniteJson(value.result);
    if (utf8Size(JSON.stringify(value.result)) > 4_000_000) {
      throw new Error("结果 JSON 超过 4 MB。");
    }
  } else {
    if (value.matlab_version !== null || value.result !== null || value.artifacts.length !== 0) {
      throw new Error("非成功记录包含成功输出。");
    }
    if (ACTIVE_STATUSES.has(status) && value.error !== null) {
      throw new Error("活动记录包含错误字段。");
    }
    if (TERMINAL_STATUSES.has(status)
        && (value.stage !== status || typeof value.error !== "string" || !value.error)) {
      throw new Error("失败记录缺少明确错误。");
    }
  }
  return value;
}

function cancelPolling() {
  state.selectionVersion += 1;
  if (state.pollTimer !== null) {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }
}

function revokeFigureUrls() {
  for (const url of state.objectUrls) URL.revokeObjectURL(url);
  state.objectUrls = [];
}

function clearSuccessOutput() {
  revokeFigureUrls();
  elements.matlabVersion.textContent = "—";
  elements.resultData.textContent = "";
  elements.resultDownload.disabled = true;
  elements.figures.replaceChildren();
  const empty = document.createElement("p");
  empty.className = "empty-output";
  empty.textContent = "暂无图表。";
  elements.figures.append(empty);
}

async function loadFigures(record, selectionVersion) {
  const cards = [];
  const urls = [];
  try {
    for (const artifact of record.artifacts) {
      const response = await fetch(
        `${API}/runs/${record.id}/artifacts/${encodeURIComponent(artifact.path)}`,
        {headers: headers()},
      );
      if (!response.ok) throw await responseError(response);
      const blob = await response.blob();
      if (blob.type !== "image/png" || blob.size !== artifact.size) {
        throw new Error(`图表 ${artifact.path} 与清单不一致。`);
      }
      const url = URL.createObjectURL(blob);
      urls.push(url);
      const figure = document.createElement("figure");
      figure.className = "figure-card";
      const image = document.createElement("img");
      image.src = url;
      image.alt = `运行 ${record.id} 的图表 ${artifact.path}`;
      const caption = document.createElement("figcaption");
      caption.textContent = artifact.path;
      figure.append(image, caption);
      cards.push(figure);
    }
    if (selectionVersion !== state.selectionVersion || record.id !== state.selectedRunId) {
      for (const url of urls) URL.revokeObjectURL(url);
      return;
    }
    revokeFigureUrls();
    state.objectUrls = urls;
    elements.figures.replaceChildren(...cards);
    if (cards.length === 0) {
      const empty = document.createElement("p");
      empty.className = "empty-output";
      empty.textContent = "本次运行没有图表。";
      elements.figures.append(empty);
    }
  } catch (error) {
    for (const url of urls) URL.revokeObjectURL(url);
    if (selectionVersion === state.selectionVersion) showNotice(errorMessage(error));
  }
}

function markHistorySelection() {
  for (const button of elements.history.querySelectorAll("button")) {
    button.setAttribute("aria-current", String(button.dataset.id === state.selectedRunId));
  }
}

async function renderRecord(record) {
  record = validateRecord(record);
  state.selectedRecord = record;
  elements.runId.textContent = record.id;
  elements.executionStatus.textContent = STATUS_LABELS[record.status];
  elements.executionStatus.dataset.status = record.status;
  elements.executionLog.textContent = record.log;
  clearSuccessOutput();
  markHistorySelection();

  if (record.status === "completed") {
    clearNotice();
    elements.matlabVersion.textContent = record.matlab_version;
    elements.resultData.textContent = JSON.stringify(record.result, null, 2);
    elements.resultDownload.disabled = false;
    await loadFigures(record, state.selectionVersion);
  } else if (TERMINAL_STATUSES.has(record.status)) {
    showNotice(record.error);
  }
}

function schedulePoll(record, selectionVersion) {
  if (!ACTIVE_STATUSES.has(record.status) || selectionVersion !== state.selectionVersion) return;
  state.pollTimer = setTimeout(async () => {
    state.pollTimer = null;
    if (selectionVersion !== state.selectionVersion) return;
    try {
      const next = validateRecord(await getJson(`/runs/${record.id}`));
      if (selectionVersion !== state.selectionVersion) return;
      await renderRecord(next);
      schedulePoll(next, selectionVersion);
    } catch (error) {
      if (selectionVersion === state.selectionVersion && record.id === state.selectedRunId) {
        showNotice(errorMessage(error));
        schedulePoll(record, selectionVersion);
      }
    }
  }, 800);
}

async function refreshHistory() {
  const records = await getJson("/runs");
  if (!Array.isArray(records)) throw new Error("最近运行列表格式无效。");
  const validated = records.map(validateRecord);
  const buttons = validated.map((record) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.id = record.id;
    const name = document.createElement("span");
    name.className = "history-name";
    name.textContent = record.filename;
    const meta = document.createElement("span");
    meta.className = "history-meta";
    const date = new Date(record.updated_at);
    const displayDate = Number.isNaN(date.getTime()) ? record.updated_at : date.toLocaleString();
    meta.textContent = `${STATUS_LABELS[record.status]} · ${displayDate}`;
    button.append(name, meta);
    button.addEventListener("click", () => selectRun(record.id));
    return button;
  });
  elements.history.replaceChildren(...buttons);
  if (buttons.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-output";
    empty.textContent = "暂无运行记录。";
    elements.history.append(empty);
  }
  markHistorySelection();
}

async function selectRun(runId) {
  cancelPolling();
  state.selectedRunId = runId;
  clearNotice();
  markHistorySelection();
  const selectionVersion = state.selectionVersion;
  try {
    const record = validateRecord(await getJson(`/runs/${runId}`));
    if (selectionVersion !== state.selectionVersion) return;
    await renderRecord(record);
    schedulePoll(record, selectionVersion);
  } catch (error) {
    if (selectionVersion === state.selectionVersion) showNotice(errorMessage(error));
  }
}

async function submitRun() {
  if (state.submitting || !state.workerConfigured) return;
  clearNotice();
  state.submitting = true;
  updateRunButton();
  try {
    const {file, source} = await readSource();
    const request = {
      id: newRunId(),
      filename: file.name,
      source,
      kind: elements.kind.value,
      parameters: readParameters(),
      trusted: elements.trusted.checked,
      timeout_seconds: readTimeout(),
    };
    if (!request.trusted) throw new Error("必须确认代码可信后才能执行。");
    cancelPolling();
    const response = await fetch(API + "/runs", {
      method: "POST",
      headers: {...headers(), "Content-Type": "application/json"},
      body: JSON.stringify(request),
    });
    if (!response.ok) throw await responseError(response);
    let record;
    try {
      record = validateRecord(await response.json());
    } catch (error) {
      throw new Error(`服务器运行记录无效：${errorMessage(error)}`);
    }
    if (record.id !== request.id) throw new Error("服务器返回了不匹配的运行 ID。");
    state.selectedRunId = record.id;
    const selectionVersion = state.selectionVersion;
    await renderRecord(record);
    schedulePoll(record, selectionVersion);
    try {
      await refreshHistory();
    } catch (error) {
      if (selectionVersion === state.selectionVersion) showNotice(errorMessage(error));
    }
  } catch (error) {
    showNotice(errorMessage(error));
  } finally {
    state.submitting = false;
    updateRunButton();
  }
}

async function loadExample(name) {
  clearNotice();
  try {
    const response = await fetch(`${API}/examples/${encodeURIComponent(name)}`, {
      headers: headers(),
    });
    if (!response.ok) throw await responseError(response);
    const source = await response.text();
    if (utf8Size(source) > 1_000_000) throw new Error("示例文件超过 1 MB。");
    const transfer = new DataTransfer();
    transfer.items.add(new File([source], name, {type: "text/plain"}));
    elements.file.files = transfer.files;
    elements.kind.value = name === "web_function.m" ? "function" : "script";
    updateFileDetail();
  } catch (error) {
    showNotice(errorMessage(error));
  }
}

function updateFileDetail() {
  if (elements.file.files.length !== 1) {
    elements.fileDetail.textContent = "请选择一个不超过 1 MB 的 .m 文件。";
    return;
  }
  const file = elements.file.files[0];
  elements.fileDetail.textContent = `${file.name} · ${file.size.toLocaleString()} 字节`;
}

function downloadResult() {
  const record = state.selectedRecord;
  if (!record || record.status !== "completed") return;
  const content = JSON.stringify(record.result, null, 2) + "\n";
  const url = URL.createObjectURL(new Blob([content], {type: "application/json"}));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `${record.id}-result.json`;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

async function bootstrap() {
  try {
    const response = await fetch(API + "/bootstrap");
    if (!response.ok) throw await responseError(response);
    const data = await response.json();
    if (!data || typeof data.token !== "string"
        || typeof data.worker_configured !== "boolean"
        || (data.worker_error !== null && typeof data.worker_error !== "string")) {
      throw new Error("工作台启动信息格式无效。");
    }
    state.token = data.token;
    state.workerConfigured = data.worker_configured;
    if (state.workerConfigured) {
      elements.workerStatus.textContent = "MATLAB Worker 已配置";
      elements.workerStatus.dataset.state = "available";
      try {
        await refreshHistory();
      } catch (error) {
        showNotice(errorMessage(error));
      }
    } else {
      elements.workerStatus.textContent = "MATLAB Worker 不可用";
      elements.workerStatus.dataset.state = "unavailable";
      showNotice(data.worker_error || "MATLAB Worker 未配置。");
    }
  } catch (error) {
    state.workerConfigured = false;
    elements.workerStatus.textContent = "MATLAB Worker 不可用";
    elements.workerStatus.dataset.state = "unavailable";
    showNotice(errorMessage(error));
  } finally {
    updateRunButton();
  }
}

elements.runButton.addEventListener("click", submitRun);
elements.file.addEventListener("change", updateFileDetail);
elements.resultDownload.addEventListener("click", downloadResult);
for (const button of document.querySelectorAll(".example-button")) {
  button.addEventListener("click", () => loadExample(button.dataset.example));
}
window.addEventListener("pagehide", () => {
  cancelPolling();
  revokeFigureUrls();
});

bootstrap();
