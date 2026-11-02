import {active as stateActive, statuses} from "./state.js";

export function createRenderer(state, {closeDirectoryPicker, renderTask}) {
  const $ = (id) => document.getElementById(id);
  const active = () => stateActive(state);
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

  function resourceLocation() {
    return state.locations.find((item) => item.id === $("resource-location").value);
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
    if (record?.status === "queued") {
      next = record.queued_action === "message"
        ? "请求已排队，尚未开始规划；不会据此获得执行授权。"
        : "工作已排队，尚未开始下一项操作；现有确认与证据保持不变。";
    } else if (record?.status === "planning") {
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
    const busy = Boolean(!state.token || state.sending || active() || state.nativePicker);
    for (const id of ["send", "project-root", "output-root", "resource-location", "message", "evaluation-criteria", "trusted", "resume"]) {
      $(id).disabled = busy;
    }
    for (const id of ["confirm", "retry"]) {
      $(id).disabled = busy || !$("trusted").checked;
    }
    for (const target of ["project", "output"]) {
      $(`browse-${target}`).disabled = busy || (target === "project" && !resourceLocation());
      $(`browse-${target}`).textContent = state.nativePicker?.target === target ? "正在选择…" : "浏览…";
    }
    if (busy && state.directoryPicker) closeDirectoryPicker();
  }

  function directoryControls() {
    $("directory-up").disabled = !state.directoryPicker?.parent;
    $("directory-choose").disabled = !state.directoryPicker?.path
      || $("directory-address").value !== state.directoryPicker.path
      || state.directoryPicker.selection !== state.selection || !state.token || state.sending || Boolean(active());
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
    const previous = state.current;
    for (const [id, key] of [["project-root", "project_root"], ["output-root", "output_root"], ["resource-location", "resource_location"]]) {
      const input = $(id);
      if (resetDirectories || input.value.trim() === (state.current?.[key] || "")) input.value = record[key] || "";
    }
    state.current = record;
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
      state.baselineKey = "";
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

  return {$, element, notice, resourceLocation, renderLocation, renderProgress, controls, directoryControls, table, details, messageText, render};
}
