"use strict";
const $ = (id) => document.getElementById(id);
let token = "";
let selectedFile = null;
let activeId = null;
let activeRecord = null;
let activeResultJson = null;
let pollTimer = null;
let figureKey = "";
let imageUrls = [];
let viewRevision = 0;
let figureGeneration = 0;
let inputRevision = 0;

const executionLabels = {
  queued: "等待执行", running: "正在执行", completed: "执行完成",
  failed: "执行失败", interrupted: "执行中断",
};
const agentLabels = {
  not_requested: "可选步骤", running: "正在分析", completed: "分析完成",
  failed: "分析失败", undetermined: "无法按当前规则判定",
};

function notice(message) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}

async function api(path, options = {}, binary = false) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "X-Flight-Token": token,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
    },
  });
  if (!response.ok) {
    let detail = `请求失败：HTTP ${response.status}`;
    try {
      const error = await response.json();
      detail = typeof error.detail === "string" ? error.detail : JSON.stringify(error.detail);
    } catch (_) { /* Keep the HTTP status when the server did not return JSON. */ }
    throw new Error(detail);
  }
  return binary ? response.blob() : response.json();
}

function badge(id, text, state) {
  $(id).textContent = text;
  $(id).className = "badge " + (
    state === "completed" ? "success" : ["running", "queued"].includes(state) ? "running" :
      ["failed", "interrupted", "undetermined"].includes(state) ? "error" : ""
  );
}

function clearFigures() {
  figureGeneration += 1;
  for (const url of imageUrls) URL.revokeObjectURL(url);
  imageUrls = [];
  $("figures").replaceChildren();
}

async function renderFigures(record, revision) {
  const key = `${record.id}:${record.status}:${record.figures.join(",")}`;
  if (key === figureKey) return;
  figureKey = "";
  clearFigures();
  const generation = figureGeneration;
  for (const name of record.figures) {
    const blob = await api(`/api/jobs/${record.id}/artifacts/${name}`, {}, true);
    if (revision !== viewRevision || generation !== figureGeneration) return;
    const url = URL.createObjectURL(blob);
    imageUrls.push(url);
    const figure = document.createElement("figure");
    const image = document.createElement("img");
    image.src = url;
    image.alt = `MATLAB 图表 ${name}`;
    const caption = document.createElement("figcaption");
    caption.textContent = name;
    figure.append(image, caption);
    $("figures").append(figure);
  }
  if (revision === viewRevision && generation === figureGeneration) figureKey = key;
}

function renderAssessment(assessment) {
  $("assessment").replaceChildren();
  if (!assessment) return;
  const summary = document.createElement("p");
  summary.textContent = `程序判定：${assessment.status}` + (
    typeof assessment.evaluated === "number"
      ? ` · 已评估 ${assessment.evaluated} / 适用 ${assessment.applicable} / 总数 ${assessment.total}`
      : ""
  );
  $("assessment").append(summary);
  if (!assessment.checks) return;
  const table = document.createElement("table");
  const header = document.createElement("tr");
  for (const text of ["要求", "实测值", "判据", "状态"]) {
    const cell = document.createElement("th");
    cell.textContent = text;
    header.append(cell);
  }
  table.append(header);
  for (const check of assessment.checks) {
    const row = document.createElement("tr");
    const measurement = check.value == null
      ? "—" : `${check.value} ${check.value_unit ?? "单位未知"}`;
    for (const text of [
      check.rule_id, measurement,
      `${check.operator} ${check.threshold} ${check.unit}`, check.status,
    ]) {
      const cell = document.createElement("td");
      cell.textContent = text;
      row.append(cell);
    }
    table.append(row);
  }
  $("assessment").append(table);
}

async function render(record, revision) {
  let resultJson = activeResultJson;
  if (record.status === "completed" && resultJson === null) {
    resultJson = await (await api(`/api/jobs/${record.id}/result`, {}, true)).text();
  }
  if (revision !== viewRevision) return;
  activeRecord = record;
  activeResultJson = record.status === "completed" ? resultJson : null;
  badge("execution-status", executionLabels[record.status], record.status);
  badge("agent-status", agentLabels[record.agent_status], record.agent_status);
  $("job-meta").textContent = `任务 ${record.id} · ${record.filename}` +
    (record.matlab_version ? ` · MATLAB ${record.matlab_version}` : "") +
    (record.remote_id ? ` · 远程 ${record.remote_id}` : "");
  $("execution-error").textContent = record.error || "";
  $("execution-error").hidden = !record.error;
  $("result-data").textContent = activeResultJson ?? "尚无完成的 MATLAB 结果。";
  $("result-data").classList.toggle("empty", record.status !== "completed");
  $("execution-log").textContent = record.log || "尚无日志。";
  $("agent-error").textContent = record.agent_error || "";
  $("agent-error").hidden = !record.agent_error;
  $("report").textContent = record.report || "";
  $("report").hidden = !record.report;
  $("download-result").disabled = activeResultJson === null;
  $("download-report").disabled = !record.report;
  $("analyze-button").disabled = record.status !== "completed" ||
    ["running", "completed"].includes(record.agent_status);
  $("run-button").disabled = ["queued", "running"].includes(record.status) ||
    record.agent_status === "running";
  renderAssessment(record.assessment);
  await renderFigures(record, revision);
}

function selectJob(jobId) {
  activeId = jobId;
  activeRecord = null;
  activeResultJson = null;
  $("analyze-button").disabled = true;
  $("download-result").disabled = true;
  $("download-report").disabled = true;
  $("run-button").disabled = true;
  badge("execution-status", "正在加载", "queued");
  badge("agent-status", "等待加载", "");
  $("job-meta").textContent = `任务 ${jobId} · 正在加载…`;
  $("result-data").textContent = "正在加载任务结果。";
  $("result-data").classList.add("empty");
  $("execution-log").textContent = "尚无日志。";
  for (const id of ["execution-error", "agent-error", "report"]) {
    $(id).textContent = "";
    $(id).hidden = true;
  }
  renderAssessment(null);
  viewRevision += 1;
  figureKey = "";
  clearFigures();
  return poll(jobId, viewRevision);
}

async function poll(jobId, revision = viewRevision) {
  if (revision !== viewRevision) return;
  clearTimeout(pollTimer);
  try {
    const record = await api(`/api/jobs/${jobId}`);
    if (revision !== viewRevision) return;
    if (record.id !== jobId) throw new Error("服务器返回的任务 ID 不匹配");
    await render(record, revision);
    if (revision !== viewRevision) return;
    if (["queued", "running"].includes(record.status) || record.agent_status === "running") {
      pollTimer = setTimeout(() => poll(jobId, revision), 800);
    } else {
      await history();
    }
  } catch (error) {
    if (revision !== viewRevision) return;
    notice(`${error.message}。未自动重试执行；可在“最近任务”中重新查看状态。`);
    if (!activeRecord) {
      badge("execution-status", "加载失败", "failed");
      badge("agent-status", "未加载", "");
      $("job-meta").textContent = `任务 ${jobId} · 加载失败`;
      $("result-data").textContent = "未能加载任务结果。";
    }
    $("run-button").disabled = false;
  }
}

async function history() {
  const records = await api("/api/jobs");
  $("history").replaceChildren();
  if (!records.length) {
    $("history").textContent = "暂无任务。";
    return;
  }
  for (const record of records) {
    const button = document.createElement("button");
    button.className = "button subtle";
    button.textContent = `${record.filename} · ${executionLabels[record.status]}`;
    const detail = document.createElement("small");
    detail.textContent = `${new Date(record.created_at).toLocaleString()} · ${record.id.slice(0, 8)}`;
    button.append(detail);
    button.addEventListener("click", () => {
      notice("");
      selectJob(record.id);
    });
    $("history").append(button);
  }
}

function download(name, content, mime) {
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

$("file").addEventListener("change", () => {
  inputRevision += 1;
  selectedFile = $("file").files[0] || null;
  $("trusted").checked = false;
  $("filename").textContent = selectedFile
    ? `${selectedFile.name} · ${(selectedFile.size / 1024).toFixed(1)} KB` : "尚未选择文件。";
});

$("parameters").addEventListener("input", () => { inputRevision += 1; });
$("kind").addEventListener("change", () => { inputRevision += 1; });
$("example").addEventListener("change", () => { inputRevision += 1; });

$("load-example").addEventListener("click", async () => {
  const revision = ++inputRevision;
  try {
    const name = $("example").value;
    const blob = await api(`/api/examples/${name}`, {}, true);
    if (revision !== inputRevision) return;
    const parameters = name === "web_metrics.m"
      ? await (await api("/api/examples/parameters.json", {}, true)).text()
      : JSON.stringify({ frequency_hz: 1, amplitude: 1, samples: 200 }, null, 2);
    if (revision !== inputRevision) return;
    selectedFile = new File([blob], name, { type: "text/plain" });
    $("file").value = "";
    $("filename").textContent = `已载入 ${name}（尚未运行）`;
    $("kind").value = name === "web_script.m" ? "script" : "function";
    $("parameters").value = parameters;
    $("trusted").checked = false;
    notice("");
  } catch (error) {
    if (revision === inputRevision) notice(error.message);
  }
});

$("run-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  notice("");
  try {
    const file = selectedFile;
    const kind = $("kind").value;
    const timeoutSeconds = Number($("timeout").value);
    if (!file) throw new Error("请先选择或载入一个 .m 文件。");
    if (!file.name.endsWith(".m") || file.size > 1000000) {
      throw new Error("请选择不超过 1 MB 的 .m 文件。");
    }
    const parametersJson = $("parameters").value;
    let parameters;
    try {
      parameters = JSON.parse(parametersJson, (_key, value) => {
        if (typeof value === "number" && !Number.isFinite(value)) {
          throw new Error("参数中的数字必须是有限数值，请检查数值是否过大。");
        }
        return value;
      });
    } catch (error) {
      if (error instanceof SyntaxError) {
        throw new Error("参数不是有效 JSON，请检查引号、逗号和括号。");
      }
      throw error;
    }
    if (!parameters || Array.isArray(parameters) || typeof parameters !== "object") {
      throw new Error("JSON 参数必须是对象，例如 {\"value\": 3}。");
    }
    if (!$("trusted").checked) throw new Error("请先确认上传代码可信。");
    $("run-button").disabled = true;
    const source = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
    const metadata = JSON.stringify({
      filename: file.name, source, kind,
      trusted: true, timeout_seconds: timeoutSeconds,
    });
    const record = await api("/api/jobs", {
      method: "POST",
      // Keep validated numeric tokens intact instead of serializing rounded JS numbers.
      body: `${metadata.slice(0, -1)},"parameters":${parametersJson}}`,
    });
    await selectJob(record.id);
  } catch (error) {
    notice(error.message);
    $("run-button").disabled = false;
  }
});

$("analyze-button").addEventListener("click", async () => {
  if (!activeRecord || activeRecord.id !== activeId || activeRecord.status !== "completed" ||
      $("analyze-button").disabled) return;
  const jobId = activeRecord.id;
  const revision = viewRevision;
  try {
    notice("");
    $("analyze-button").disabled = true;
    await api(`/api/jobs/${jobId}/analyze`, {
      method: "POST", body: JSON.stringify({ request: $("request").value }),
    });
    await poll(jobId, revision);
  } catch (error) {
    if (revision !== viewRevision) return;
    notice(error.message);
    $("analyze-button").disabled = false;
  }
});
$("refresh-history").addEventListener("click", () => history().catch((e) => notice(e.message)));
$("download-result").addEventListener("click", () => {
  if (activeRecord?.id === activeId && activeResultJson !== null) {
    download("result.json", activeResultJson, "application/json");
  }
});
$("download-report").addEventListener("click", () => {
  if (activeRecord?.id === activeId && activeRecord.report) {
    download("report.md", activeRecord.report, "text/markdown;charset=utf-8");
  }
});

async function init() {
  const config = await api("/api/bootstrap");
  token = config.token;
  $("worker-state").textContent = config.worker_configured ? "已配置 · 运行时验证连接" : "未配置";
  $("model-state").textContent = config.model_configured ? "已配置 · 分析时调用" : "未配置 · 不影响 MATLAB 执行";
  if (config.worker_error) {
    $("config-help").textContent = "请在服务器 .env 配置 MATLAB_WORKER_URL 和 MATLAB_WORKER_TOKEN，" +
      "连接安装 MATLAB 的执行机器。当前页面可浏览，但不能执行；MATLAB Online 不是此处的执行后端。";
    $("config-help").hidden = false;
  }
  await history();
}
init().catch((error) => notice(error.message));
