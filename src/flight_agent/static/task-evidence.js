import {active as stateActive, nodeStatuses} from "./state.js";
import {API} from "./api.js";

export function createTaskEvidence(state, {$, element, notice, table, details, response}) {
  const active = () => stateActive(state);
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
    const key = `${state.current.id}/${view.id}/${view.baseline_version}/${state.current.pending_decision?.kind || ""}`;
    const changedBaseline = key !== state.baselineKey;
    if (changedBaseline) $("trusted").checked = false;
    state.baselineKey = key;
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
    const decision = state.current.pending_decision;
    const available = !active() && decision;
    const confirm = available && decision.kind === "confirm_baseline";
    const retry = available && decision.kind === "retry";
    const resume = available && decision.kind === "resume";
    const guidance = available && ["clarify", "capability_gap"].includes(decision.kind);
    $("decision-controls").hidden = !(confirm || retry || resume || guidance);
    const needsDecision = String(!$("decision-controls").hidden);
    if (changedBaseline || $("baseline-details").dataset.decision !== needsDecision) {
      $("baseline-details").open = needsDecision === "true";
      $("baseline-details").dataset.decision = needsDecision;
    }
    $("decision-message").textContent = String(decision?.message || "").slice(0, 4096);
    for (const [id, show] of [["confirm", confirm], ["retry", retry], ["resume", resume]]) {
      $(id).hidden = !show;
    }
    $("trust-label").hidden = !(confirm || retry);
    $("trust-warning").hidden = !(confirm || retry);
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
    const generation = state.selection;
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
      if (generation === state.selection) notice(error.message);
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  }

  function renderDelivery(task) {
    const tasks = [task, ...state.current.archived_tasks];
    $("delivery-card").hidden = !tasks.some((item) => item.artifacts.length);
    $("results-directory").textContent = `成果目录（Agent 所在机器）：${task.result_directory}`;
    $("artifacts").replaceChildren();
    const conversationId = state.current.id;
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

  return {sourceText, metricCard, renderChecks, renderEvaluation, renderTask, download, renderDelivery};
}
