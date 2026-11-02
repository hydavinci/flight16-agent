import {API, createApi} from "./api.js";
import {active as stateActive, createState, statuses} from "./state.js";
import {createRenderer} from "./render.js";
import {createTaskEvidence} from "./task-evidence.js";
import {initializeColumnResize} from "./resize-preferences.js";

const state = createState();
const active = () => stateActive(state);
const {response, api} = createApi(() => state.token);
let taskEvidence;
const renderer = createRenderer(state, {
  closeDirectoryPicker: () => closeDirectoryPicker(),
  renderTask: (view) => taskEvidence.renderTask(view),
});
const {
  $, element, notice, resourceLocation, renderLocation, renderProgress, controls,
  directoryControls, render,
} = renderer;
taskEvidence = createTaskEvidence(state, {...renderer, response});

async function conversation(path, id, options) {
  const generation = state.selection;
  const record = await api(path, options);
  if (!record || typeof record.id !== "string" || !/^[a-f0-9]{32}$/.test(record.id)
      || (id && record.id !== id)) {
    throw new Error("会话 ID 不匹配，已拒绝更新。");
  }
  if (path.endsWith("/view")) {
    if (generation === state.selection && state.pendingView?.id === record.id) state.pendingView = null;
    return record;
  }
  if (options?.method === "POST" && generation === state.selection) {
    state.pendingView = {id: record.id, selection: generation};
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

function closeDirectoryPicker() {
  state.directoryPicker = null;
  state.directoryRequest += 1;
  if ($("directory-picker").open) $("directory-picker").close();
}

function resetDirectoryListing(status) {
  if (!state.directoryPicker) return;
  state.directoryRequest += 1;
  state.directoryPicker.path = null;
  state.directoryPicker.parent = null;
  $("directory-list").replaceChildren();
  $("directory-error").hidden = true;
  $("directory-error").textContent = "";
  $("directory-status").textContent = status;
  directoryControls();
}

async function loadDirectory(path) {
  const picker = state.directoryPicker;
  if (!picker || picker.selection !== state.selection || !state.token || state.sending || active()) return;
  $("directory-address").value = path;
  resetDirectoryListing("正在读取目录…");
  const request = state.directoryRequest;
  const relevant = () => picker === state.directoryPicker && request === state.directoryRequest
    && picker.selection === state.selection && $("directory-picker").open;
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
  const picker = {target, selection: state.selection, value: input.value, location: $("resource-location").value};
  state.nativePicker = picker;
  notice("");
  controls();
  const relevant = () => state.nativePicker === picker && picker.selection === state.selection
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
    if (state.nativePicker === picker) {
      state.nativePicker = null;
      controls();
    }
  }
}

function openDirectoryPicker(target) {
  if (!state.token || state.sending || active() || state.nativePicker) return;
  const location = resourceLocation();
  if (target === "project" && !location) return;
  if (state.nativeDirectoryPickerEnabled) {
    chooseNativeDirectory(target);
    return;
  }
  closeDirectoryPicker();
  state.directoryPicker = {
    target, selection: state.selection, path: null, parent: null,
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
  $(`${state.directoryPicker.target}-root`).value = state.directoryPicker.path;
  closeDirectoryPicker();
}

function poll() {
  clearTimeout(state.timer);
  if (!active()) return;
  const generation = state.selection;
  const id = state.pendingView?.id || state.current.id;
  state.timer = setTimeout(async () => {
    try {
      const record = await conversation(`${API}/${id}/view`, id);
      if (generation !== state.selection) return;
      render(record);
      if (!active()) await history();
    } catch (error) {
      if (generation === state.selection) notice(`无法更新任务状态：${error.message}`);
    }
    if (generation === state.selection) poll();
  }, 750);
}

async function history() {
  const request = ++state.historyRequest;
  const records = await api(API);
  if (request !== state.historyRequest) return;
  $("conversation-history").replaceChildren(...records.map((record) => {
    const button = element("button");
    button.append(element("span", record.title, "history-title"),
      element("small", statuses[record.status] || record.status));
    button.type = "button";
    button.title = record.title;
    button.dataset.id = record.id;
    button.setAttribute("aria-current", String(state.current && state.current.id === record.id));
    button.addEventListener("click", () => select(record.id));
    return button;
  }));
}

function clearConversation() {
  state.selection += 1;
  closeDirectoryPicker();
  state.nativePicker = null;
  clearTimeout(state.timer);
  state.current = null;
  state.pendingView = null;
  state.sending = false;
  state.baselineKey = "";
  $("trusted").checked = false;
  for (const id of ["message", "evaluation-criteria", "project-root", "output-root", "resource-location"]) $(id).value = "";
  $("resource-location").value = state.locations.some((item) => item.id === "matlab_project"
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
  const generation = state.selection;
  state.sending = true;
  controls();
  $("conversation-status").textContent = "正在加载";
  $("conversation-status").hidden = false;
  try {
    const record = await conversation(`${API}/${id}/view`, id);
    if (generation !== state.selection) return;
    localStorage.setItem("verification-conversation", record.id);
    render(record, true);
    poll();
  } catch (error) {
    if (generation === state.selection) {
      $("conversation-status").textContent = "加载失败";
      notice(error.message);
    }
  } finally {
    if (generation === state.selection) {
      state.sending = false;
      controls();
    }
  }
}

async function send(event) {
  event.preventDefault();
  if (state.sending || active() || state.nativePicker || !state.token) return;
  const message = $("message").value;
  if (!message.trim()) return notice("请输入任务需求。");
  const generation = state.selection;
  const id = state.current?.id || null;
  const criteria = $("evaluation-criteria").value;
  const body = criteria.trim()
    ? {analysis_request: message, evaluation_criteria: criteria} : {message};
  const location = $("resource-location").value;
  if (location && location !== state.current?.resource_location) body.location = location;
  const root = $("project-root").value.trim();
  const output = $("output-root").value.trim();
  if (root !== (state.current?.project_root || "")) body.project_root = root;
  if (output !== (state.current?.output_root || "")) body.output_root = output;
  state.sending = true;
  notice("");
  controls();
  try {
    const record = await conversation(id ? `${API}/${id}/messages` : API, id, {method: "POST", body});
    if (generation === state.selection) {
      localStorage.setItem("verification-conversation", record.id);
      render(record, true);
      poll();
    }
    await history();
  } catch (error) {
    if (generation === state.selection) {
      notice(state.pendingView ? `请求已接收，正在恢复展示；不会重复提交。${error.message}` : error.message);
      if (state.pendingView) poll();
    }
  } finally {
    if (generation === state.selection) {
      state.sending = false;
      controls();
    }
  }
}

async function decide(action) {
  if (!state.current || state.sending || active() || state.nativePicker) return;
  if (action !== "resume" && !$("trusted").checked) return;
  const generation = state.selection;
  const id = state.current.id;
  const version = state.current.task.baseline_version;
  state.sending = true;
  notice("");
  controls();
  try {
    const record = await conversation(`${API}/${id}/${action}`, id, {
      method: "POST", body: action === "resume" ? undefined : {version, trusted: true},
    });
    if (generation === state.selection) {
      render(record);
      poll();
    }
    await history();
  } catch (error) {
    if (generation === state.selection) {
      notice(state.pendingView ? `操作已接收，正在恢复展示；不会重复提交。${error.message}` : error.message);
      if (state.pendingView) poll();
    }
  } finally {
    if (generation === state.selection) {
      state.sending = false;
      controls();
    }
  }
}

export async function start() {
  $("composer").addEventListener("submit", send);
  $("resource-location").addEventListener("change", () => {
    if (state.sending || active() || state.nativePicker) return;
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
  $("directory-up").addEventListener("click", () => { if (state.directoryPicker?.parent) loadDirectory(state.directoryPicker.parent); });
  $("directory-home").addEventListener("click", () => loadDirectory(""));
  $("directory-cancel").addEventListener("click", closeDirectoryPicker);
  $("directory-choose").addEventListener("click", chooseDirectory);
  $("directory-picker").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeDirectoryPicker();
  });
  $("trusted").addEventListener("change", controls);
  for (const action of ["confirm", "resume", "retry"]) $(action).addEventListener("click", () => decide(action));
  $("new-conversation").addEventListener("click", () => {
    clearConversation();
    localStorage.removeItem("verification-conversation");
  });
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
    state.token = bootstrap.token;
    state.nativeDirectoryPickerEnabled = bootstrap.native_directory_picker === true;
    $("worker-state").textContent = bootstrap.worker_configured
      ? "MATLAB Worker：已配置" : `MATLAB Worker（仅相关能力需要）：${bootstrap.worker_error}`;
    $("model-state").textContent = bootstrap.model_configured ? "LLM：已配置" : `LLM：${bootstrap.model_error}`;
    const localMatlab = bootstrap.local_matlab;
    $("local-matlab-state").textContent = localMatlab?.state === "available"
      ? "本机 MATLAB Engine：导入探测通过；启动与许可证尚未验证。"
      : localMatlab?.state === "unavailable"
        ? `本机 MATLAB Engine：不可用；仍可浏览目录及静态发现。${localMatlab.reason}`
        : "本机 MATLAB Engine：状态未知；当前后端未提供本机工程状态，请重启后端服务以加载新版功能。";
    state.locations = await api("/api/resource-locations");
    state.locations.forEach((item) => {
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
